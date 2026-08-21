"""
depth_probe.py — разведочный скрипт: сколько квалифицирующих сообщений
реально есть в подборке каналов за последние 270 дней, помесячно. НЕ часть
основного пайплайна: свой список каналов (не input/channels_tier1.txt), свой
файл cache/dump_depth_v2.jsonl (не cache/dump.jsonl, не
cache/dump_depth_probe.jsonl). Логику извлечения, квалификации и сессию
переиспользует из crawl.py/build.py, не дублирует.

Идемпотентность по каналу через offset_id: если у канала уже есть записи в
DUMP_PATH, дальше идём от min(message_id) вглубь истории, не перечитывая уже
собранное. Тот же offset_id используется для восстановления после
FloodWaitError -- ретраим не с начала канала (как в crawl.py), а с последнего
успешно обработанного сообщения: для каналов такого объёма ретрай с нуля был
бы слишком расточительным.
"""

import asyncio
import json
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from crawl import API_HASH, API_ID, PHONE, SESSION_PATH, build_record  # noqa: E402
from build import (  # noqa: E402
    filter_hotline_contacts,
    has_erid,
    is_military_content,
    load_ref_domains,
    matches_ref_domain,
)
from config import CRAWL_PAUSE_RANGE  # noqa: E402

# 2026-08-21: expanded from the original 7 to the 11 channels actually present
# in cache/dump_depth_probe.jsonl (verified against a fresh channel scan of
# that file: its 11 distinct channels are exactly these original 7 union
# rabota_moskval / rabota_v_kaliningradeq / samare_vakansiy /
# sankt_peterburg_vakansiy from input/channels.txt -- those 4 already had
# messages in the file despite never being in this list; main()'s "channels
# outside CHANNELS are preserved as-is" behavior is what kept them there
# across runs without this script ever actively crawling them itself).
CHANNELS = [
    "rabotab_kazan", "moskvan", "v_rabota_moskve", "rabotan_samara",
    "sankt_vakansiy_peterburg", "v_rabota_ekb", "v_vakansiy_rostove",
    "rabota_moskval", "rabota_v_kaliningradeq", "samare_vakansiy",
    "sankt_peterburg_vakansiy",
]

# 2026-08-21: rolling 270-day window (was a fixed 2023-01-01 cutoff, ~3.6
# years) per the red-team audit's recommended cap. Output redirected to a new
# file rather than continuing to write into dump_depth_probe.jsonl, whose
# provenance is already unclear (a population-count reconstruction gap and
# this same 11-vs-7 channel question were both open before this change) --
# mixing newly-understood data into that file would only compound the
# ambiguity. cache/dump_depth_v2.jsonl is gitignored and never committed,
# same as every other cache/dump*.jsonl.
CUTOFF_DAYS = 270
CUTOFF = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS)
DUMP_PATH = BASE_DIR / "cache" / "dump_depth_v2.jsonl"

HEARTBEAT_EVERY = 1000


def is_qualifying(record: dict, ref_domains: set) -> bool:
    """Та же квалификация, что в build.py, без ветки tg://resolve (её тут
    нет вообще): ref_domain ИЛИ erid, минус military, минус сообщения, где
    контакты полностью состояли из hotline-номеров."""
    urls = [u["url"] for u in record["urls"]]
    if not any(matches_ref_domain(u, ref_domains) or has_erid(u) for u in urls):
        return False
    if is_military_content(record["text"]):
        return False
    _, disqualified = filter_hotline_contacts(record["contacts"])
    return not disqualified


def load_existing_records(dump_path: Path) -> dict:
    """channel -> list[record], из уже сохранённого dump_depth_probe.jsonl."""
    by_channel = defaultdict(list)
    if not dump_path.exists():
        return by_channel
    with open(dump_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            by_channel[record["channel"]].append(record)
    return by_channel


def compute_month_stats(records: list, ref_domains: set) -> dict:
    month_stats = defaultdict(lambda: {"total": 0, "qualifying": 0})
    for record in records:
        dt = datetime.fromisoformat(record["date"])
        month_key = dt.strftime("%Y-%m")
        month_stats[month_key]["total"] += 1
        if is_qualifying(record, ref_domains):
            month_stats[month_key]["qualifying"] += 1
    return month_stats


def resume_offset(existing_records: list) -> int:
    """Telethon: offset_id=0 значит "с самого нового" (обычное поведение по
    умолчанию). Если записи уже есть -- продолжаем от самого старого
    message_id вглубь, строго без пересечения с уже собранным."""
    if not existing_records:
        return 0
    return min(r["message_id"] for r in existing_records)


async def crawl_channel(client, channel: str, ref_domains: set, existing_records: list):
    """Возвращает (все_записи_канала, month_stats) -- existing_records +
    вновь собранные. Сама ретраит FloodWaitError, докручивая offset_id от
    последнего успешно обработанного сообщения, так что повтор не теряет и
    не задваивает уже полученное."""
    accumulated = list(existing_records)
    month_stats = compute_month_stats(existing_records, ref_domains)
    offset_id = resume_offset(existing_records)
    if offset_id:
        print(f"[{channel}] уже есть {len(existing_records)} сообщений в дампе, "
              f"продолжаю с offset_id={offset_id}")

    while True:
        try:
            entity = await client.get_entity(channel)
            async for message in client.iter_messages(entity, limit=None, offset_id=offset_id):
                if message.date < CUTOFF:
                    return accumulated, month_stats

                record = build_record(channel, message)
                accumulated.append(record)
                offset_id = message.id

                month_key = message.date.strftime("%Y-%m")
                month_stats[month_key]["total"] += 1
                if is_qualifying(record, ref_domains):
                    month_stats[month_key]["qualifying"] += 1

                new_count = len(accumulated) - len(existing_records)
                if new_count and new_count % HEARTBEAT_EVERY == 0:
                    print(f"  [{channel}] ...+{new_count} новых сообщений, "
                          f"дошли до {message.date:%Y-%m-%d}")

            return accumulated, month_stats
        except FloodWaitError as e:
            print(f"[{channel}] FloodWaitError, жду {e.seconds} сек, "
                  f"продолжу с offset_id={offset_id}...")
            await asyncio.sleep(e.seconds)


def save_records(dump_path: Path, records: list) -> None:
    """Пишет всё разом поверх файла (не append) -- у этого скрипта нет
    идемпотентности между запусками кроме явного resume по каналу, "a"
    молча задваивало бы строки при повторном запуске без резюма."""
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dump_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_table(title: str, month_stats: dict) -> None:
    print(f"\n{title}")
    months = sorted(month_stats.keys(), reverse=True)
    if not months:
        print("(нет данных)")
        return

    rows = []
    for month in months:
        total = month_stats[month]["total"]
        qualifying = month_stats[month]["qualifying"]
        pct = (qualifying / total * 100) if total else 0.0
        rows.append((month, str(total), str(qualifying), f"{pct:.1f}%"))

    headers = ("месяц", "всего", "квалиф.", "доля %")
    widths = [max(len(str(row[i])) for row in ([headers] + rows)) for i in range(4)]

    def fmt_row(row):
        return " | ".join(str(v).ljust(w) for v, w in zip(row, widths))

    print(fmt_row(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def merge_month_stats(all_stats: dict) -> dict:
    combined = defaultdict(lambda: {"total": 0, "qualifying": 0})
    for month_stats in all_stats.values():
        for month, s in month_stats.items():
            combined[month]["total"] += s["total"]
            combined[month]["qualifying"] += s["qualifying"]
    return combined


async def main():
    ref_domains = load_ref_domains()
    existing_by_channel = load_existing_records(DUMP_PATH)

    # Каналы вне CHANNELS, если у них вдруг уже есть данные, сохраняются как
    # есть -- этот прогон их не трогает ни в Telegram, ни в файле.
    all_records = []
    for channel, records in existing_by_channel.items():
        if channel not in CHANNELS:
            all_records.extend(records)

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    all_stats = {}

    try:
        await client.start(phone=PHONE)
        for i, channel in enumerate(CHANNELS):
            existing = existing_by_channel.get(channel, [])
            records, month_stats = await crawl_channel(client, channel, ref_domains, existing)
            all_records.extend(records)
            all_stats[channel] = month_stats

            total = sum(s["total"] for s in month_stats.values())
            qualifying = sum(s["qualifying"] for s in month_stats.values())
            print(f"[{channel}] {total} сообщений всего (2023-01-01+), {qualifying} квалифицирующих")

            if i < len(CHANNELS) - 1:
                await asyncio.sleep(random.uniform(*CRAWL_PAUSE_RANGE))
    finally:
        await client.disconnect()
        save_records(DUMP_PATH, all_records)

    for channel in CHANNELS:
        print_table(f"=== {channel} ===", all_stats[channel])
    print_table(f"=== Суммарно (эти {len(CHANNELS)} каналов) ===", merge_month_stats(all_stats))


if __name__ == "__main__":
    asyncio.run(main())

"""
probe.py — разведочный скрипт этапа "форма данных".

Смотрит на структуру постов в трёх захардкоженных TG-каналах: откуда
берутся ссылки (entities / инлайн-кнопки / голый текст), какие домены
встречаются, какие контакты видны в тексте. Ничего не резолвит и никуда,
кроме Telegram, не ходит — это чисто разведка перед основным пайплайном.
"""

import asyncio
import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
from telethon.utils import get_inner_text

# Windows-консоль по умолчанию не всегда в UTF-8 — без этого print()
# с кириллицей может упасть с UnicodeEncodeError.
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.environ["PHONE"]

# Сессия хранится в корне проекта (рядом с .env), а не в src/.
SESSION_NAME = str(BASE_DIR / "fetcher")

# Разведка: работаем ТОЛЬКО с этими тремя каналами. input/channels.txt
# на этом этапе намеренно не читаем — так задано условием.
CHANNELS = ["CISRabota", "rabota_moskval", "moskva_rabota0"]

MESSAGES_PER_CHANNEL = 300
REPORT_PATH = BASE_DIR / "probe_report.txt"
REF_DOMAINS_PATH = BASE_DIR / "input" / "ref_domains.txt"

SOURCE_ENTITY = "entities"
SOURCE_BUTTON = "кнопка"
SOURCE_TEXT = "текст"

# Фолбэк-поиск URL в голом тексте — на случай, если Telethon не построил
# entity. Требуем явную схему/www., либо "домен.tld/путь" с обязательным
# путём после домена — иначе регэксп начинает хватать всякие "п.5.1",
# сокращения с точками и т.п. как будто это домен.
URL_TEXT_REGEX = re.compile(
    r'https?://[^\s<>\[\]()"\']+'
    r'|www\.[^\s<>\[\]()"\']+'
    r'|(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,24}/[^\s<>\[\]()"\']*'
)

# @username: не считаем совпадение, если "@" — часть email (перед "@" не
# должно быть "словесного" символа/точки, иначе это локальная часть адреса).
USERNAME_REGEX = re.compile(r'(?<![\w.])@[a-zA-Z0-9_]{5,32}\b')

# "+7/8, 10-11 цифр": префикс +7 / 8 / 7, затем 10 цифр номера в
# группировке 3-3-2-2, с необязательными пробелами/скобками/дефисами
# между группами (сам префикс формально не входит в "10-11 цифр", но
# вместе с ним получаем ровно диапазон 10-11, который просит задание).
PHONE_REGEX = re.compile(
    r'(?<!\d)(?:\+7|8|7)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)'
)

# Домен обязан заканчиваться "буквенным" TLD (2+ букв) — иначе жадный
# класс символов прихватывает точку конца предложения в саму почту
# (напр. "...company.ru." вместо "...company.ru").
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9_.+-]+@(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}')

_SCHEME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://')


def _with_scheme(url: str) -> str:
    """urlsplit не находит netloc без "//" — подставляем его для URL без
    схемы (пойманных текстовым фолбэком вида "vk.cc/abc")."""
    return url if _SCHEME_RE.match(url) else "//" + url


def extract_domain(url: str) -> str:
    netloc = urlsplit(_with_scheme(url)).netloc
    netloc = netloc.split("@")[-1]  # user:pass@host -> host
    netloc = netloc.split(":")[0]   # host:port -> host
    return netloc.lower()


def url_has_erid(url: str) -> bool:
    query = urlsplit(_with_scheme(url)).query.lower()
    return bool(re.search(r'(?:^|&)erid=', query))


def extract_urls(message):
    """Список уникальных (url, источник) для сообщения.

    Приоритет источников entities > кнопка > текст: если один и тот же URL
    уже найден через entity или кнопку, повторное фолбэк-совпадение по
    тексту не добавляется — иначе частоты по доменам и статистика по
    источникам задваиваются.
    """
    raw_text = message.raw_text or ""
    found = {}

    entities = message.entities or []

    # MessageEntityUrl не хранит сам URL — это подстрока текста по
    # offset/length (в UTF-16 code units). get_inner_text уже умеет
    # корректно резать текст с учётом суррогатных пар (эмодзи и т.п.).
    url_entities = [e for e in entities if isinstance(e, MessageEntityUrl)]
    if url_entities:
        for inner in get_inner_text(raw_text, url_entities):
            found.setdefault(inner, SOURCE_ENTITY)

    for entity in entities:
        if isinstance(entity, MessageEntityTextUrl):
            found.setdefault(entity.url, SOURCE_ENTITY)  # entity.url, а не видимый текст

    # Инлайн-кнопки: рекурсивно по rows -> buttons. getattr(..., "url", None)
    # безопасно пропускает кнопки без URL (callback/switch_inline и т.п.)
    # и работает для любых url-подобных типов кнопок (Url, UrlAuth, WebView).
    markup = message.reply_markup
    if markup is not None and hasattr(markup, "rows"):
        for row in markup.rows:
            for button in row.buttons:
                url = getattr(button, "url", None)
                if url:
                    found.setdefault(url, SOURCE_BUTTON)

    for m in URL_TEXT_REGEX.finditer(raw_text):
        url = m.group(0).rstrip('.,;:!?»')
        found.setdefault(url, SOURCE_TEXT)

    return list(found.items())


def extract_contacts(text: str):
    """Список уникальных (значение, тип) контактов: email / phone / username."""
    contacts = {}
    for m in EMAIL_REGEX.finditer(text):
        contacts.setdefault((m.group(0), "email"), None)
    for m in PHONE_REGEX.finditer(text):
        contacts.setdefault((m.group(0), "phone"), None)
    for m in USERNAME_REGEX.finditer(text):
        contacts.setdefault((m.group(0), "username"), None)
    return list(contacts.keys())


def load_ref_domains():
    if not REF_DOMAINS_PATH.exists():
        return set()
    with open(REF_DOMAINS_PATH, encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


async def fetch_channel_messages(client, channel):
    while True:
        try:
            return await client.get_messages(channel, limit=MESSAGES_PER_CHANNEL)
        except FloodWaitError as e:
            print(f"  [{channel}] FloodWaitError: жду {e.seconds} сек...")
            await asyncio.sleep(e.seconds)


async def process_channel(client, channel, report):
    print(f"\n=== Канал: {channel} ===")
    stats = {
        "total": 0, "with_url": 0, "with_contact": 0, "with_both": 0,
        "oldest": None, "newest": None,
    }

    # Один "плохой" канал (приватный/несуществующий/бан) не должен ронять
    # весь прогон — на этапе разведки логичнее пропустить его и продолжить.
    try:
        messages = await fetch_channel_messages(client, channel)
    except Exception as e:
        print(f"  [{channel}] не удалось получить сообщения ({e!r}), пропускаю канал")
        return stats

    stats["total"] = len(messages)
    print(f"  получено сообщений: {stats['total']}")

    for msg in messages:
        text = msg.raw_text or ""
        urls = extract_urls(msg)
        contacts = extract_contacts(text)

        if urls:
            stats["with_url"] += 1
        if contacts:
            stats["with_contact"] += 1
        if urls and contacts:
            stats["with_both"] += 1

        if msg.date is not None:
            if stats["oldest"] is None or msg.date < stats["oldest"]:
                stats["oldest"] = msg.date
            if stats["newest"] is None or msg.date > stats["newest"]:
                stats["newest"] = msg.date

        for url, source in urls:
            domain = extract_domain(url)
            report["domain_counter"][domain] += 1
            report["domain_source_counter"][domain][source] += 1
            report["total_urls"] += 1
            if url_has_erid(url):
                report["erid_count"] += 1
            if domain in report["ref_domains"] and len(report["ref_domain_examples"][domain]) < 5:
                report["ref_domain_examples"][domain].append(url)

        if urls and contacts and len(report["both_examples"]) < 10:
            report["both_examples"].append({
                "channel": channel,
                "message_id": msg.id,
                "date": msg.date,
                "text": text,
                "urls": urls,
                "contacts": contacts,
            })

    print(f"  с URL: {stats['with_url']}, с контактом: {stats['with_contact']}, "
          f"с обоими: {stats['with_both']}")
    return stats


def format_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z") if dt else "—"


def write_report(channel_stats, report):
    lines = []
    lines.append("=" * 78)
    lines.append("БЛОК 1. Статистика по каналам")
    lines.append("=" * 78)
    for channel, s in channel_stats.items():
        lines.append(f"\nКанал: {channel}")
        lines.append(f"  Всего сообщений получено: {s['total']}")
        lines.append(f"  С хотя бы одним URL: {s['with_url']}")
        lines.append(f"  С хотя бы одним контактом: {s['with_contact']}")
        lines.append(f"  И с URL, и с контактом одновременно: {s['with_both']}")
        lines.append(f"  Самое старое сообщение в выборке: {format_dt(s['oldest'])}")
        lines.append(f"  Самое свежее сообщение в выборке: {format_dt(s['newest'])}")

    lines.append("\n" + "=" * 78)
    lines.append("БЛОК 2. Все домены из всех найденных URL (по убыванию частоты)")
    lines.append("=" * 78)
    lines.append(f"\nВсего уникальных доменов: {len(report['domain_counter'])}, "
                  f"всего URL: {report['total_urls']}\n")
    for domain, count in report["domain_counter"].most_common():
        src_counter = report["domain_source_counter"][domain]
        dominant = src_counter.most_common(1)[0][0]
        breakdown = ", ".join(f"{s}={n}" for s, n in src_counter.most_common())
        lines.append(f"  {domain} — {count}  [чаще: {dominant}]  ({breakdown})")

    lines.append("\n" + "=" * 78)
    lines.append("БЛОК 3. URL с параметром erid")
    lines.append("=" * 78)
    pct = (report["erid_count"] / report["total_urls"] * 100) if report["total_urls"] else 0.0
    lines.append(f"\n{report['erid_count']} из {report['total_urls']} URL содержат erid ({pct:.1f}%)")

    lines.append("\n" + "=" * 78)
    lines.append(f"БЛОК 4. Примеры сообщений с URL и контактом одновременно "
                  f"({len(report['both_examples'])})")
    lines.append("=" * 78)
    for i, ex in enumerate(report["both_examples"], 1):
        lines.append(f"\n--- Пример {i} ---")
        lines.append(f"Канал: {ex['channel']}")
        lines.append(f"Message ID: {ex['message_id']}")
        lines.append(f"Дата: {format_dt(ex['date'])}")
        lines.append("Текст:")
        lines.append(ex["text"] if ex["text"] else "(пусто)")
        lines.append("URL:")
        for url, source in ex["urls"]:
            lines.append(f"  - [{source}] {url}")
        lines.append("Контакты:")
        for value, kind in ex["contacts"]:
            lines.append(f"  - [{kind}] {value}")

    lines.append("\n" + "=" * 78)
    lines.append("БЛОК 5. Примеры URL по доменам из input/ref_domains.txt")
    lines.append("=" * 78)
    found_ref_domains = [d for d in sorted(report["ref_domains"]) if d in report["ref_domain_examples"]]
    if not found_ref_domains:
        lines.append("\n(ни один домен из ref_domains.txt не встретился в выборке)")
    for domain in found_ref_domains:
        lines.append(f"\n{domain}:")
        for url in report["ref_domain_examples"][domain]:
            lines.append(f"  - {url}")

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main():
    print("Если это первый запуск — Telegram пришлёт код подтверждения,")
    print("его нужно будет ввести прямо в этот терминал (сессия сохранится")
    print(f"в файл {SESSION_NAME}.session, повторно код спрашивать не будет).\n")

    report = {
        "domain_counter": Counter(),
        "domain_source_counter": defaultdict(Counter),
        "total_urls": 0,
        "erid_count": 0,
        "both_examples": [],
        "ref_domains": load_ref_domains(),
        "ref_domain_examples": defaultdict(list),
    }

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    channel_stats = {}
    try:
        await client.start(phone=PHONE)
        for i, channel in enumerate(CHANNELS):
            channel_stats[channel] = await process_channel(client, channel, report)
            if i < len(CHANNELS) - 1:
                pause = random.uniform(2, 3)
                print(f"  пауза {pause:.1f} сек перед следующим каналом...")
                await asyncio.sleep(pause)
    finally:
        await client.disconnect()

    write_report(channel_stats, report)
    print(f"\nГотово. Отчёт записан в {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())

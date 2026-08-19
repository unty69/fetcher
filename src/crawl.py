"""
crawl.py — боевой сбор сообщений: читает input/channels_tier1.txt, тянет из
каждого нового (ещё не встречавшегося в DUMP_PATH) канала сообщения за
последние CRAWL_DAYS дней и дописывает их построчно (JSONL) в DUMP_PATH, без
фильтрации по наличию url/contact -- это отдельный шаг позже.

Идемпотентность на уровне канала: канал, уже встреченный в DUMP_PATH,
целиком пропускается при следующем запуске -- повторный запуск безопасен.
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
from telethon.utils import get_inner_text

# Windows-консоль по умолчанию не всегда в UTF-8 — без этого print()
# с кириллицей может упасть с UnicodeEncodeError.
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from config import (  # noqa: E402
    CRAWL_DAYS,
    CRAWL_PAUSE_RANGE,
    DUMP_PATH,
    INPUT_CHANNELS,
    MAX_MESSAGES_PER_CHANNEL,
    SESSION_NAME,
)

load_dotenv(BASE_DIR / ".env")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.environ["PHONE"]

# Сессия хранится в корне проекта (рядом с .env), а не в src/ и не в cwd —
# как в probe.py, чтобы существующий fetcher.session находился независимо
# от того, откуда запущен скрипт.
SESSION_PATH = str(BASE_DIR / SESSION_NAME)
CHANNELS_PATH = BASE_DIR / INPUT_CHANNELS
DUMP_PATH_ABS = BASE_DIR / DUMP_PATH

SOURCE_ENTITY = "entities"
SOURCE_BUTTON = "button"
SOURCE_TEXT = "text"

UNAVAILABLE_ERRORS = (UsernameNotOccupiedError, UsernameInvalidError,
                       ChannelPrivateError, ChannelInvalidError)

# --- извлечение: та же логика (регэкспы и приоритеты источников), что в
# src/probe.py, только источники помечаются английскими тегами и результат
# отдаётся в виде словарей под формат JSONL-дампа. -----------------------

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
# между группами.
PHONE_REGEX = re.compile(
    r'(?<!\d)(?:\+7|8|7)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)'
)

# Домен обязан заканчиваться "буквенным" TLD (2+ букв) — иначе жадный
# класс символов прихватывает точку конца предложения в саму почту
# (напр. "...company.ru." вместо "...company.ru").
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9_.+-]+@(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}')


def extract_urls(message):
    """[{"url", "source"}] для сообщения. Приоритет entities > button > text
    с dedup: URL, уже найденный через entity/кнопку, повторно из текстового
    фолбэка не добавляется."""
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
    # безопасно пропускает кнопки без URL (callback/switch_inline и т.п.).
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

    return [{"url": url, "source": source} for url, source in found.items()]


def extract_contacts(text: str):
    """[{"type", "value"}] уникальных контактов: email / phone / username."""
    contacts = {}
    for m in EMAIL_REGEX.finditer(text):
        contacts.setdefault(("email", m.group(0)), None)
    for m in PHONE_REGEX.finditer(text):
        contacts.setdefault(("phone", m.group(0)), None)
    for m in USERNAME_REGEX.finditer(text):
        contacts.setdefault(("username", m.group(0)), None)
    return [{"type": kind, "value": value} for kind, value in contacts.keys()]


# --- ввод/вывод -------------------------------------------------------


def load_channels(path: Path) -> list:
    channels = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            channels.append(line)
    return channels


def load_seen_channels(dump_path: Path) -> set:
    if not dump_path.exists():
        return set()
    seen = set()
    with open(dump_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            seen.add(json.loads(line)["channel"])
    return seen


def append_records(dump_path: Path, records: list) -> None:
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dump_path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- сбор по каналу -----------------------------------------------------


def build_record(channel: str, message) -> dict:
    text = message.raw_text or ""
    return {
        "channel": channel,
        "message_id": message.id,
        "date": message.date.isoformat(),
        "text": text,
        "urls": extract_urls(message),
        "contacts": extract_contacts(text),
    }


async def crawl_channel(client, channel: str) -> list:
    entity = await client.get_entity(channel)
    cutoff = datetime.now(timezone.utc) - timedelta(days=CRAWL_DAYS)

    records = []
    async for message in client.iter_messages(entity, limit=MAX_MESSAGES_PER_CHANNEL):
        if message.date < cutoff:
            break
        records.append(build_record(channel, message))
    return records


class FloodEncountered(Exception):
    """Сигнал наверх: FloodWaitError пойман и пережидан для канала, но
    сессию стоит остановить, не продолжать по остальным каналам батча —
    сам факт флуда важнее оставшегося лимита."""
    def __init__(self, channel, seconds, records):
        self.channel = channel
        self.seconds = seconds
        self.records = records


async def process_channel(client, channel: str) -> list:
    hit_flood = False
    wait_seconds = 0
    while True:
        try:
            records = await crawl_channel(client, channel)
            if hit_flood:
                raise FloodEncountered(channel, wait_seconds, records)
            return records
        except FloodWaitError as e:
            hit_flood = True
            wait_seconds = e.seconds
            print(f"[{channel}] FloodWaitError, жду {e.seconds} сек...")
            await asyncio.sleep(e.seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    """--channels-file/--dump-path необязательные: без них поведение не
    меняется вообще (default=None -> main() берёт CHANNELS_PATH/DUMP_PATH_ABS,
    как раньше). Нужны, чтобы гонять tier2-выборку и подобное в отдельный
    дамп, не трогая tier1-дамп и его идемпотентность."""
    parser = argparse.ArgumentParser(description="Собрать сообщения каналов в JSONL-дамп.")
    parser.add_argument("--channels-file", default=None,
                         help=f"Список каналов (по умолчанию {INPUT_CHANNELS})")
    parser.add_argument("--dump-path", default=None,
                         help=f"Выходной JSONL-дамп (по умолчанию {DUMP_PATH})")
    parser.add_argument("--limit", type=int, default=None,
                         help="Максимум НОВЫХ каналов за один запуск")
    return parser


def resolve_arg_path(value, default_path: Path) -> Path:
    """Относительные пути из CLI резолвятся от BASE_DIR (не от cwd) -- та же
    логика, что даёт CHANNELS_PATH/DUMP_PATH_ABS из config-путей."""
    if value is None:
        return default_path
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


async def main():
    args = build_arg_parser().parse_args()
    channels_path = resolve_arg_path(args.channels_file, CHANNELS_PATH)
    dump_path = resolve_arg_path(args.dump_path, DUMP_PATH_ABS)

    all_channels = load_channels(channels_path)
    seen = load_seen_channels(dump_path)

    todo = []
    for channel in all_channels:
        if channel in seen:
            print(f"[{channel}] уже в дампе, пропущен")
        else:
            todo.append(channel)

    if args.limit is not None:
        todo = todo[:args.limit]
        print(f"Лимит батча: {args.limit}, к обработке сейчас: {len(todo)}")

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    try:
        await client.start(phone=PHONE)
        for i, channel in enumerate(todo):
            try:
                records = await process_channel(client, channel)
            except FloodEncountered as e:
                append_records(dump_path, e.records)
                total = len(e.records)
                with_url = sum(1 for r in e.records if r["urls"])
                with_contact = sum(1 for r in e.records if r["contacts"])
                print(f"[{channel}] {total} сообщений, {with_url} с url, {with_contact} с контактом")
                print(f"\n[СТОП] FloodWait ({e.seconds} сек) на «{e.channel}». "
                      f"Пережидано, но сессия остановлена, не продолжаем по "
                      f"оставшимся {len(todo)-i-1}. Идемпотентность сохранит "
                      f"прогресс, возобновить можно следующим запуском.")
                break
            except UNAVAILABLE_ERRORS as e:
                print(f"[{channel}] недоступен ({type(e).__name__}), пропущен")
                if i < len(todo) - 1:
                    await asyncio.sleep(random.uniform(*CRAWL_PAUSE_RANGE))
                continue
            append_records(dump_path, records)

            if len(records) == MAX_MESSAGES_PER_CHANNEL:
                print(f"  [ВНИМАНИЕ] {channel}: ровно {MAX_MESSAGES_PER_CHANNEL} "
                      f"сообщений — возможна обрезка 45-дневного окна, канал "
                      f"слишком активный для лимита")

            total = len(records)
            with_url = sum(1 for r in records if r["urls"])
            with_contact = sum(1 for r in records if r["contacts"])
            print(f"[{channel}] {total} сообщений, {with_url} с url, {with_contact} с контактом")

            if i < len(todo) - 1:
                await asyncio.sleep(random.uniform(*CRAWL_PAUSE_RANGE))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

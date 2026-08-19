"""
find_channel_mentions.py — разведка каналов-упоминаний в уже собранных
данных: username-кандидаты (из contacts type=="username" и из t.me/{username}
в текстах url) на 3+ разных каналах -- сигнал "стоит покраулить", отдельно
от того, что уже краулим. Никаких обращений к Telegram/Telethon -- чистая
обработка cache/dump*.jsonl, автономный скрипт (не переиспользует build.py --
разведка каналов не связана с CPA/МФО-пайплайном).

Всё нормализуется в lowercase до сравнения/дедупликации -- иначе один и тот
же канал под разным регистром в разных сообщениях исказил бы счётчик "числа
РАЗНЫХ каналов-источников", на котором держится вся ранжировка.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent

DUMP_PATHS = [
    BASE_DIR / "cache" / "dump.jsonl",
    BASE_DIR / "cache" / "dump_tier2.jsonl",
    BASE_DIR / "cache" / "dump_depth_probe.jsonl",
    BASE_DIR / "cache" / "dump_tier3.jsonl",
]
OUTPUT_PATH = BASE_DIR / "cache" / "channel_mentions.json"
TOP_N = 50
HIGH_CONFIDENCE_MIN_CHANNELS = 3

TME_USERNAME_RE = re.compile(r"t\.me/([A-Za-z0-9_]{5,32})")


def iter_messages(dump_paths: list):
    for path in dump_paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)


def collect_known_channels(dump_paths: list) -> set:
    """Шаг 1: множество значений "channel" по всем дампам -- то, что уже
    краулим. Отдельный проход намеренно (см. докстринг модуля/шаг 2 в ТЗ):
    известные каналы должны быть собраны ЦЕЛИКОМ до отбора кандидатов."""
    known = set()
    for msg in iter_messages(dump_paths):
        known.add(msg["channel"].lower())
    return known


def extract_candidates(msg: dict) -> set:
    """username-кандидаты из ОДНОГО сообщения: contacts (type=="username")
    и t.me/{username} в urls. Без "@", lowercase. Множество -- чтобы кандидат,
    засветившийся дважды в одном сообщении (напр. и в contacts, и в urls),
    не задвоил message_count для этого сообщения."""
    candidates = set()

    for c in msg["contacts"]:
        if c["type"] == "username":
            value = c["value"].lower()
            if value.startswith("@"):
                value = value[1:]
            candidates.add(value)

    for u in msg["urls"]:
        m = TME_USERNAME_RE.search(u["url"])
        if m:
            candidates.add(m.group(1).lower())

    return candidates


def collect_mentions(dump_paths: list, known_channels: set) -> dict:
    """Шаг 2-3: username -> {"channels": set(источников), "message_count":
    int}, уже без всего, что есть в known_channels (шаг 3)."""
    mentions = defaultdict(lambda: {"channels": set(), "message_count": 0})

    for msg in iter_messages(dump_paths):
        source_channel = msg["channel"].lower()
        for username in extract_candidates(msg):
            if username in known_channels:
                continue
            rec = mentions[username]
            rec["channels"].add(source_channel)
            rec["message_count"] += 1

    return mentions


def main():
    known_channels = collect_known_channels(DUMP_PATHS)
    print(f"known_channels (уже краулим): {len(known_channels)}")

    mentions = collect_mentions(DUMP_PATHS, known_channels)

    # Шаг 4: по числу РАЗНЫХ каналов-источников, не по числу сообщений.
    # Вторичные ключи -- только для стабильного порядка внутри одинакового
    # числа каналов, в ТЗ не оговорены отдельно.
    ranked = sorted(
        mentions.items(),
        key=lambda kv: (-len(kv[1]["channels"]), -kv[1]["message_count"], kv[0]),
    )

    total_candidates = len(ranked)
    high_confidence = sum(1 for _, rec in ranked if len(rec["channels"]) >= HIGH_CONFIDENCE_MIN_CHANNELS)
    single_mention = sum(1 for _, rec in ranked if len(rec["channels"]) == 1)

    print(f"\nВсего уникальных кандидатов: {total_candidates}")
    print(f"Встретились в {HIGH_CONFIDENCE_MIN_CHANNELS}+ разных каналах (высокая уверенность): {high_confidence}")
    print(f"Встретились только в 1 канале (низкая уверенность, вероятно личные контакты): {single_mention}")

    print(f"\n=== Топ-{min(TOP_N, total_candidates)} кандидатов по числу каналов-источников ===")
    for rank, (username, rec) in enumerate(ranked[:TOP_N], start=1):
        channels_str = ", ".join(sorted(rec["channels"]))
        print(f"{rank:>3}. {username}  |  каналов: {len(rec['channels'])}  |  "
              f"сообщений: {rec['message_count']}  |  {channels_str}")

    output_data = [
        {
            "username": username,
            "seen_in_channels": sorted(rec["channels"]),
            "message_count": rec["message_count"],
        }
        for username, rec in ranked
    ]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    print(f"\nПолный ранжированный список ({total_candidates} записей) сохранён в "
          f"{OUTPUT_PATH.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()

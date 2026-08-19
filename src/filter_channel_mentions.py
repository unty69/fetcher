"""
filter_channel_mentions.py — фильтрация cache/channel_mentions.json ДО
любого резолва через Telegram get_entity(): отсекает выбросы по числу
каналов-источников, служебные пути t.me/, помечает military_likely по
большинству военных упоминаний в исходных текстах. Без сети -- только
cache/channel_mentions.json и повторный проход по cache/dump*.jsonl (тексты
сообщений не сохранялись на прошлом шаге, только счётчики).

Резолв (get_entity()) -- отдельный шаг, здесь не запускается вообще.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402 -- только ради is_military_content(), не переписываем
from find_channel_mentions import DUMP_PATHS, extract_candidates, iter_messages  # noqa: E402

MENTIONS_PATH = BASE_DIR / "cache" / "channel_mentions.json"
OUTPUT_PATH = BASE_DIR / "cache" / "channel_mentions_filtered.json"

# Реальный разрыв в распределении -- 19 -> 350 (см. вывод распределения),
# ни одного кандидата в диапазоне 20..349. Любое значение в этом разрыве
# даёт идентичный результат фильтра; 50 -- круглое число внутри него,
# совпадает с ориентиром из ТЗ.
OUTLIER_THRESHOLD = 50

# Зарезервированные пути в t.me/<path> -- не username каналов. "s"/"bg"/"iv"
# короче 5 символов и физически не могут быть пойманы TME_USERNAME_RE
# (минимум {5,32}) -- перечислены для полноты списка из ТЗ, не потому что
# реально встретились в текущих кандидатах.
RESERVED_PATHS = {
    "joinchat", "addlist", "share", "proxy", "socks",
    "confirmphone", "setlanguage", "bg", "iv", "s",
}

MILITARY_SHARE_THRESHOLD = 0.5  # "хотя бы половина"

# Визуально похожие латинские буквы -> кириллические, ТОЛЬКО для проверки
# is_military_content() (обходит обфускацию вроде "kOHTPAKT" -- см.
# channel_mentions.json #"kohtpakt_mo", 1297 сообщений). is_military_content()
# сам не меняется и не импортирует эту таблицу -- нормализуем текст здесь,
# перед вызовом, а не переписываем функцию в build.py.
HOMOGLYPH_MAP = str.maketrans({
    "o": "о", "O": "О",
    "a": "а", "A": "А",
    "e": "е", "E": "Е",
    "p": "р", "P": "Р",
    "c": "с", "C": "С",
    "x": "х", "X": "Х",
})


def normalize_homoglyphs(text: str) -> str:
    """Только для классификации -- исходный текст сообщения (в выводе,
    если бы он куда-то шёл) не трогаем, это преобразование одноразовое,
    прямо перед is_military_content()."""
    return text.translate(HOMOGLYPH_MAP)


def load_mentions() -> list:
    with open(MENTIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


def show_distribution(records: list) -> None:
    freq = Counter(len(r["seen_in_channels"]) for r in records)
    print("Полное распределение seen_in_channels (число каналов: кандидатов), по убыванию:")
    for value in sorted(freq.keys(), reverse=True):
        print(f"  {value:>4}: {freq[value]}")


def collect_texts_for_candidates(candidates: set) -> dict:
    """username -> список текстов сообщений, где он встретился. Повторный
    проход по дампам той же extract_candidates(), что и в
    find_channel_mentions.py -- логику отбора кандидата не дублируем."""
    texts = defaultdict(list)
    for msg in iter_messages(DUMP_PATHS):
        for username in extract_candidates(msg):
            if username in candidates:
                texts[username].append(msg["text"])
    return texts


def main():
    records = load_mentions()
    print(f"Загружено кандидатов из {MENTIONS_PATH.name}: {len(records)}")

    show_distribution(records)

    after_outliers = [r for r in records if len(r["seen_in_channels"]) <= OUTLIER_THRESHOLD]
    removed_outliers = len(records) - len(after_outliers)

    after_reserved = [r for r in after_outliers if r["username"] not in RESERVED_PATHS]
    removed_reserved = len(after_outliers) - len(after_reserved)

    print(f"\nУбрано по порогу выбросов (>{OUTLIER_THRESHOLD} каналов): {removed_outliers}")
    print(f"Убрано по служебным путям Telegram: {removed_reserved}")

    surviving_usernames = {r["username"] for r in after_reserved}
    texts_by_username = collect_texts_for_candidates(surviving_usernames)

    military_likely_count = 0
    output_records = []
    for r in after_reserved:
        texts = texts_by_username.get(r["username"], [])
        if texts:
            military_count = sum(1 for t in texts if build.is_military_content(normalize_homoglyphs(t)))
            military_likely = (military_count / len(texts)) >= MILITARY_SHARE_THRESHOLD
        else:
            military_likely = False
        if military_likely:
            military_likely_count += 1
        output_records.append({**r, "military_likely": military_likely})

    output_records.sort(key=lambda r: (r["military_likely"], -len(r["seen_in_channels"])))

    print(f"Помечено military_likely=true: {military_likely_count}")
    print(f"Осталось чистых кандидатов (military_likely=false): {len(output_records) - military_likely_count}")
    print(f"Итого в выходном файле: {len(output_records)}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_records, f, ensure_ascii=False, indent=2)
    print(f"\nСохранено в {OUTPUT_PATH.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()

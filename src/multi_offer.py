"""
multi_offer.py — диагностика мультиоффера: одни и те же ID-значения
(webmaster/паблишер) на РАЗНЫХ доменах-работодателях -- признак одного
человека/агентства, продвигающего сразу несколько офферов, а не участника
одной конкретной вакансии.

Отдельный проход напрямую по cache/resolved.json (без dumps, без сети) --
готовится ДО пересборки build.py, чтобы решить, что из этого стоит закрепить
в NETWORK_RULES.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import identify  # noqa: E402 -- переиспользуем _domain_of, не дублируем разбор домена

RESOLVED_PATH = BASE_DIR / "cache" / "resolved.json"
OUTPUT_PATH = BASE_DIR / "cache" / "multi_offer_ids.txt"

ID_PARAMS = ["wm", "pid", "partner", "sub", "utm_term", "wmid",
             "affiliate_id", "web_id", "webmaster_id"]

# 250236 / 153920 -- webmaster_id/wm_id одного и того же спамера займов
# (см. cache/review_domains.txt: одинаковый текст "Где взять займ онлайн" на
# bistrodengi-mkk.ru/dobrozaim.ru/oneclickmoney.ru/... под этими двумя ID) --
# заведомо не "мультиоффер" в смысле разных работодателей, а один известный
# кластер спама. Исключаем по значению, чтобы не забивать вывод им одним.
KNOWN_SPAM_IDS = {"250236", "153920"}


def load_resolved(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def collect_id_domains(resolved: dict) -> dict:
    """value(ID) -> {domains: множество доменов-работодателей, params:
    из каких query-параметров встречалось значение, examples: {domain:
    final_url} -- по одному примеру на домен}."""
    ids = defaultdict(lambda: {"domains": set(), "params": set(), "examples": {}})

    for entry in resolved.values():
        final_url = entry.get("final_url")
        if not final_url:
            continue
        domain = identify._domain_of(final_url)
        query = parse_qs(urlsplit(final_url).query)

        for param in ID_PARAMS:
            for value in query.get(param, []):
                if not value:
                    continue
                rec = ids[value]
                rec["domains"].add(domain)
                rec["params"].add(param)
                rec["examples"].setdefault(domain, final_url)

    return ids


def build_report(ids: dict) -> list:
    lines = []
    excluded_present = sorted(v for v in KNOWN_SPAM_IDS if v in ids)
    multi = {
        value: rec for value, rec in ids.items()
        if len(rec["domains"]) >= 2 and value not in KNOWN_SPAM_IDS
    }

    lines.append(f"Всего различных ID-значений (по {len(ID_PARAMS)} параметрам: "
                 f"{', '.join(ID_PARAMS)}): {len(ids)}")
    lines.append(f"Исключено как известный спам-кластер займов: "
                 f"{', '.join(excluded_present) if excluded_present else '(не встретились)'}")
    lines.append(f"ID на 2+ разных доменах-работодателях (мультиоффер): {len(multi)}")
    lines.append("")

    ranked = sorted(multi.items(), key=lambda kv: -len(kv[1]["domains"]))
    for value, rec in ranked:
        lines.append(f"ID {value}  |  параметры: {', '.join(sorted(rec['params']))}  |  "
                     f"доменов: {len(rec['domains'])}")
        for domain in sorted(rec["domains"]):
            lines.append(f"       {domain}: {rec['examples'][domain]}")

    return lines


def main():
    resolved = load_resolved(RESOLVED_PATH)
    ids = collect_id_domains(resolved)

    report_lines = build_report(ids)
    report_text = "\n".join(report_lines)

    print(report_text)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(report_text + "\n")
    print(f"\n(отчёт продублирован в cache/multi_offer_ids.txt)")


if __name__ == "__main__":
    main()

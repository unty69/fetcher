"""
alt_cluster_review.py — выгрузка alt-кластеров (identity с 2+ username, "веб
с несколькими точками входа") из cache/identity_graph.json на ручную
разметку: независимый веб vs корпоративный HR/агентство.

Кластеры берутся ИЗ УЖЕ ГОТОВОГО identity_graph.json (посчитан
identity_graph.py на прошлом шаге) — граф здесь не пересчитывается и
enrichment через него не делается. Сырые сообщения дампов используются
только для доказательной базы (бренды/реф-ссылки/заголовки), а привязка
сообщения к кластеру идёт через уже готовый contact_to_canonical, а не
через новые связи.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Font  # noqa: E402
from openpyxl.worksheet.datavalidation import DataValidation  # noqa: E402

import build  # noqa: E402
from identity_graph import iter_dump_paths, load_messages, message_contact_nodes  # noqa: E402
from search_employers import EMPLOYERS  # noqa: E402

IDENTITY_GRAPH_PATH = BASE_DIR / "cache" / "identity_graph.json"
OUTPUT_XLSX = BASE_DIR / "output" / "alt_clusters_review.xlsx"

# Список из ТЗ этого шага — ОТЛИЧАЕТСЯ от search_employers.PERSONAL_EMAIL_PROVIDERS
# на один домен (тут есть internet.ru, там нет). Взят буквально как задан,
# не смержен с существующим списком, чтобы не менять production-скоринг
# search_employers.py.
PERSONAL_PROVIDERS = {
    "mail.ru", "yandex.ru", "yandex.com", "gmail.com", "bk.ru", "inbox.ru",
    "list.ru", "rambler.ru", "icloud.com", "outlook.com", "hotmail.com",
    "ya.ru", "internet.ru",
}

# Буквально по ТЗ, без \b -- сознательно (не расширять сверх заданного).
# Значит возможны ложные срабатывания на username, где "hr"/"kadr" и т.п.
# оказались случайной подстрокой -- ожидаемый риск при таком regex, не баг.
HR_HINT_RE = re.compile(
    r'hr|рекрут|recruit|kadr|кадр|naym|найм|vacan|rezerv|резерв|manager|менедж',
    re.IGNORECASE,
)

BRAND_PATTERNS = {name: re.compile(pat, re.IGNORECASE) for name, pat in EMPLOYERS.items()}


# --- отбор кандидатов -------------------------------------------------------


def load_identity_graph():
    with open(IDENTITY_GRAPH_PATH, encoding="utf-8") as f:
        return json.load(f)


def select_candidates(graph):
    """is_alt_cluster=True, suspected_non_discovery=False, username_count>=2
    -- буквально условие шага 1 ТЗ (уже, чем просто is_alt_cluster, который
    пускает и кластеры с 2+ телефонами без 2+ username)."""
    return {
        cid: ident for cid, ident in graph["identities"].items()
        if ident["is_alt_cluster"] and not ident["suspected_non_discovery"] and ident["username_count"] >= 2
    }


def domain_of_email(value: str) -> str:
    return value.lower().rsplit("@", 1)[-1]


def has_corp_domain(ident: dict) -> bool:
    return any(
        c["type"] == "email" and domain_of_email(c["value"]) not in PERSONAL_PROVIDERS
        for c in ident["contacts"]
    )


def has_hr_hint(ident: dict) -> bool:
    return any(
        HR_HINT_RE.search(c["value"])
        for c in ident["contacts"] if c["type"] == "username"
    )


# --- доказательная база из дампов -------------------------------------------


def title_of(text: str) -> str:
    return (text or "").split("\n", 1)[0][:120]


def collect_evidence(candidate_ids: set, contact_to_canonical: dict):
    """Один проход по всем дампам (через identity_graph.load_messages --
    та же дедупликация по channel+message_id, что и при построении графа).
    Для каждого сообщения контакты переводятся в node_key и ищутся в уже
    готовом contact_to_canonical -- НИКАКИХ новых рёбер/связей не строится,
    только чтение существующей склейки."""
    evidence = {cid: {"brands": set(), "has_ref_link": False, "titles": []} for cid in candidate_ids}

    ref_domains = build.load_ref_domains()
    messages = load_messages(iter_dump_paths())

    for msg in messages:
        nodes = message_contact_nodes(msg)
        if not nodes:
            continue
        matched_cids = {
            contact_to_canonical[node_key]
            for node_key, *_ in nodes
            if node_key in contact_to_canonical and contact_to_canonical[node_key] in candidate_ids
        }
        if not matched_cids:
            continue

        text = msg.get("text") or ""
        brands = {name for name, pat in BRAND_PATTERNS.items() if pat.search(text)}
        ref_link = any(
            build.matches_ref_domain(u["url"], ref_domains)
            for u in msg["urls"] if not build.is_tg_resolve(u["url"])
        )
        title = title_of(text)

        for cid in matched_cids:
            ev = evidence[cid]
            ev["brands"] |= brands
            ev["has_ref_link"] = ev["has_ref_link"] or ref_link
            if title and title not in ev["titles"] and len(ev["titles"]) < 3:
                ev["titles"].append(title)

    return evidence


# --- строки выгрузки ---------------------------------------------------------


def parse_dt(iso_str):
    if not iso_str:
        return None
    return datetime.fromisoformat(iso_str).replace(tzinfo=None)


def build_rows(candidates: dict, evidence: dict):
    rows = []
    for cid, ident in candidates.items():
        ev = evidence[cid]
        corp = has_corp_domain(ident)
        hr = has_hr_hint(ident)
        brands = sorted(ev["brands"])
        channels_list = ident.get("channels") or []
        rows.append({
            "вердикт": "",
            "canonical_id": cid,
            "usernames": ", ".join(sorted(c["value"] for c in ident["contacts"] if c["type"] == "username")),
            "phones": ", ".join(sorted(c["value"] for c in ident["contacts"] if c["type"] == "phone")),
            "corp_domain": corp,
            "hr_hint": hr,
            "has_ref_link": ev["has_ref_link"],
            "brands": ", ".join(brands),
            "multibrand": len(brands) >= 2,
            "channel_count": len(ident["channels"]),
            "message_count": ident["message_count"],
            "first_seen": parse_dt(ident["first_seen"]),
            "last_seen": parse_dt(ident["last_seen"]),
            "sample_titles": " | ".join(ev["titles"]),
            # canonical_id уже привязывает кластер к identity_graph.json, но
            # для поиска исходного поста в Telegram нужны сами каналы --
            # t.me/s/ открывает веб-превью без аккаунта.
            "channels": ", ".join(channels_list),
            "channel_links": " ".join(f"https://t.me/s/{ch}" for ch in channels_list),
        })

    rows.sort(key=lambda r: (
        r["corp_domain"],           # False выше
        r["hr_hint"],                # False выше
        not r["has_ref_link"],       # True выше
        not r["multibrand"],         # True выше
        -r["channel_count"],         # убыв.
    ))
    return rows


# --- вывод в xlsx -------------------------------------------------------------


FIELDNAMES = ["вердикт", "canonical_id", "usernames", "phones", "corp_domain", "hr_hint",
              "has_ref_link", "brands", "multibrand", "channel_count", "message_count",
              "first_seen", "last_seen", "sample_titles", "channels", "channel_links"]

COLUMN_WIDTHS = {
    "вердикт": 14, "canonical_id": 14, "usernames": 45, "phones": 35,
    "corp_domain": 12, "hr_hint": 10, "has_ref_link": 13, "brands": 35,
    "multibrand": 12, "channel_count": 14, "message_count": 14,
    "first_seen": 18, "last_seen": 18, "sample_titles": 80,
    "channels": 55, "channel_links": 60,
}


def write_workbook(rows: list, path: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "alt_clusters"
    ws.append(FIELDNAMES)
    for cell in ws[1]:
        cell.font = Font(bold=True, name="Arial")

    for r in rows:
        ws.append([r[k] for k in FIELDNAMES])

    dv = DataValidation(type="list", formula1='"веб,не_веб,агентство,не_уверен"', allow_blank=True)
    dv.errorTitle = "Недопустимое значение"
    dv.error = "Выбери значение из списка: веб, не_веб, агентство, не_уверен"
    ws.add_data_validation(dv)
    dv.add(f"A2:A{max(len(rows) + 1, 2)}")

    for col_letter, header in zip("ABCDEFGHIJKLMNOP", FIELDNAMES):
        ws.column_dimensions[col_letter].width = COLUMN_WIDTHS[header]

    ws.freeze_panes = "A2"

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    graph = load_identity_graph()
    candidates = select_candidates(graph)
    print(f"Кандидатов (is_alt_cluster, не suspND, username_count>=2): {len(candidates)}")

    corp_filtered = {cid: i for cid, i in candidates.items() if not has_corp_domain(i)}
    print(f"Отсеяно по corp_domain (есть email не из PERSONAL_PROVIDERS): {len(candidates) - len(corp_filtered)}")

    hr_filtered = {cid: i for cid, i in corp_filtered.items() if not has_hr_hint(i)}
    print(f"Отсеяно по hr_hint (username матчит HR_HINT_RE): {len(corp_filtered) - len(hr_filtered)}")
    print(f"Осталось чистых (без corp_domain, без hr_hint): {len(hr_filtered)}")

    evidence = collect_evidence(set(candidates.keys()), graph["contact_to_canonical"])
    rows = build_rows(candidates, evidence)

    write_workbook(rows, OUTPUT_XLSX)
    print(f"\nВыгрузка: {OUTPUT_XLSX.relative_to(BASE_DIR)} -- {len(rows)} строк")


if __name__ == "__main__":
    main()

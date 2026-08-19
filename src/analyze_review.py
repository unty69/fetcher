"""
analyze_review.py — диагностика needs_review: что скрывается за network=None
среди web-сообщений трёх дампов, ДО правки NETWORK_RULES / MFO_DOMAINS.

Ничего не резолвит по сети и не пишет в config.py — только читает
cache/resolved.json и показывает. Переиспользует существующую логику:
build.load_ref_domains / load_dump_and_classify / normalize_contact,
resolve._load_cache, identify.identify — без изменений в них самих.

Для URL, отсутствующих в cache/resolved.json, подставляется синтетический
результат method="unresolved" (final_url = сам url) -- как если бы resolve()
честно попытался и ничего не нашёл, без единого сетевого запроса.
"""

import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import identify  # noqa: E402
import resolve  # noqa: E402
from config import MFO_DOMAINS  # noqa: E402

DUMP_PATHS = [
    BASE_DIR / "cache" / "dump.jsonl",
    BASE_DIR / "cache" / "dump_tier2.jsonl",
    BASE_DIR / "cache" / "dump_depth_probe.jsonl",
]
OUTPUT_PATH = BASE_DIR / "cache" / "review_domains.txt"
TOP_N = 30

# Аффилиат-подобные query-параметры (по имени ключа, регистронезависимо,
# подстрокой -- чтобы ловить и варианты вида wmid/sub1/click_id2/affiliate_id).
CPA_QUERY_MARKERS = ["wm", "wmid", "ref", "sub", "pid", "aff", "partner", "click_id"]

# Ключевые слова МФО/займов (транслитерация, как в MFO_DOMAINS) --
# ищутся и в домене, и в тексте поста.
NON_HR_KEYWORDS = ["zaim", "credit", "loan", "money", "kredit"]

CONFIGURED_NON_HR = {d.lower() for d in MFO_DOMAINS}


# --- резолв только из кэша, без сети ---------------------------------------


def resolve_only_from_cache(urls: list, cache: dict) -> dict:
    """Аналог resolve.resolve_batch(), но без единого сетевого запроса:
    берёт только то, что уже есть в cache. Для отсутствующих URL -- как если
    бы resolve() отработал и не нашёл редирект (method="unresolved",
    final_url = сам url)."""
    results = {}
    for url in urls:
        results[url] = cache.get(url) or {"final_url": url, "method": "unresolved", "error": None}
    return results


# --- эвристики CPA / non-HR --------------------------------------------------


def matched_cpa_params(url: str) -> set:
    query = parse_qs(urlsplit(url).query)
    hits = set()
    for key in query:
        key_lower = key.lower()
        for marker in CPA_QUERY_MARKERS:
            if marker in key_lower:
                hits.add(marker)
    return hits


def matched_non_hr_keywords(haystack: str) -> set:
    lowered = haystack.lower()
    return {kw for kw in NON_HR_KEYWORDS if kw in lowered}


# --- группировка network=None по domain_final ------------------------------


def collect_review_groups(web_messages: list, identified: dict, resolved: dict) -> dict:
    """Для каждой пары (сообщение, url), где identify() вернул network=None,
    -- одна review-запись, сгруппированная по domain_final."""
    groups = defaultdict(lambda: {
        "message_keys": set(),
        "contacts": set(),
        "final_urls": set(),
        "text_example": None,
        "is_contact": False,
        "cpa_markers": set(),
        "non_hr_keywords": set(),
    })

    for wm in web_messages:
        for u in wm["urls"]:
            res = identified[u]
            if res["network"] is not None:
                continue

            domain = res["domain_final"]
            g = groups[domain]
            g["message_keys"].add((wm["channel"], wm["message_id"]))

            for c in wm["contacts"]:
                g["contacts"].add((c["type"], build.normalize_contact(c["type"], c["value"])))

            final_url = resolved[u].get("final_url") or u
            g["final_urls"].add(final_url)

            if g["text_example"] is None:
                g["text_example"] = " ".join(wm["text"].split())[:200]

            g["is_contact"] = g["is_contact"] or res["is_contact"]
            g["cpa_markers"] |= matched_cpa_params(u)
            g["cpa_markers"] |= matched_cpa_params(final_url)
            g["non_hr_keywords"] |= matched_non_hr_keywords(domain)
            g["non_hr_keywords"] |= matched_non_hr_keywords(wm["text"])

    return groups


# --- отчёт -------------------------------------------------------------------


def domain_block(rank, domain: str, g: dict, extra: str = None) -> list:
    lines = []
    flags = []
    if g["is_contact"]:
        flags.append("is_contact")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    prefix = f"{rank:>3}. " if rank is not None else "   - "
    lines.append(f"{prefix}{domain}{flag_str}  |  сообщений: {len(g['message_keys'])}  |  "
                  f"уникальных контактов: {len(g['contacts'])}")
    for i, fu in enumerate(sorted(g["final_urls"])[:2], start=1):
        lines.append(f"       final_url #{i}: {fu}")
    if g["text_example"]:
        lines.append(f"       текст: {g['text_example']!r}")
    if extra:
        lines.append(f"       {extra}")
    return lines


def build_report(groups: dict, context_lines: list) -> list:
    lines = list(context_lines)

    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]["message_keys"]), kv[0]))
    total_review_messages = len({mk for g in groups.values() for mk in g["message_keys"]})

    lines.append(f"Уникальных доменов среди network=None: {len(groups)}")
    lines.append(f"Уникальных сообщений, давших хотя бы один network=None url: {total_review_messages}")
    lines.append("")

    lines.append(f"=== Топ-{min(TOP_N, len(ranked))} доменов по числу сообщений (из {len(ranked)}) ===")
    for rank, (domain, g) in enumerate(ranked[:TOP_N], start=1):
        lines.extend(domain_block(rank, domain, g))
    lines.append("")

    cpa_candidates = [(d, g) for d, g in ranked if g["cpa_markers"]]
    lines.append(f"=== Кандидаты в NETWORK_RULES: домены с CPA-подобными query-параметрами "
                 f"({len(cpa_candidates)}) ===")
    if not cpa_candidates:
        lines.append("   (ничего не найдено)")
    for domain, g in cpa_candidates:
        extra = f"совпавшие параметры: {', '.join(sorted(g['cpa_markers']))}"
        lines.extend(domain_block(None, domain, g, extra=extra))
    lines.append("")

    non_hr_candidates = [(d, g) for d, g in ranked if g["non_hr_keywords"]]
    lines.append(f"=== Кандидаты в MFO_DOMAINS: домены МФО/займы-подобные "
                 f"({len(non_hr_candidates)}) ===")
    if not non_hr_candidates:
        lines.append("   (ничего не найдено)")
    for domain, g in non_hr_candidates:
        already = " [уже в MFO_DOMAINS]" if any(nd in domain for nd in CONFIGURED_NON_HR) else ""
        extra = f"совпавшие ключевые слова: {', '.join(sorted(g['non_hr_keywords']))}{already}"
        lines.extend(domain_block(None, domain, g, extra=extra))

    return lines


def main():
    per_file_counts = [(p, build.count_lines(p)) for p in DUMP_PATHS]
    context_lines = []
    for p, n in per_file_counts:
        context_lines.append(f"{build.display_path(p)}: {n} сообщений")
    context_lines.append(f"Всего сообщений (все файлы): {sum(n for _, n in per_file_counts)}")

    ref_domains = build.load_ref_domains()
    (qualifying_count, military_excluded, hotline_excluded, tg_repeats,
     web_messages, unique_resolve_urls) = build.load_dump_and_classify(DUMP_PATHS, ref_domains)

    context_lines.append(f"Квалифицирующих сообщений: {qualifying_count}")
    context_lines.append(f"military_excluded: {military_excluded}")
    context_lines.append(f"hotline_excluded: {hotline_excluded}")
    context_lines.append(f"Web-сообщений (с квалифицирующей http(s)-ссылкой): {len(web_messages)}")

    cache = resolve._load_cache()
    unique_resolve_urls = list(unique_resolve_urls)
    cache_hits = sum(1 for u in unique_resolve_urls if u in cache)
    context_lines.append(
        f"Уникальных URL для идентификации: {len(unique_resolve_urls)} "
        f"(в cache/resolved.json: {cache_hits}, отсутствуют -> method=unresolved: "
        f"{len(unique_resolve_urls) - cache_hits})"
    )
    context_lines.append("")

    resolved = resolve_only_from_cache(unique_resolve_urls, cache)
    identified = {u: identify.identify(u, resolved[u]) for u in unique_resolve_urls}

    review_url_count = sum(1 for u in unique_resolve_urls if identified[u]["network"] is None)
    context_lines.append(f"Уникальных URL с network=None: {review_url_count} из {len(unique_resolve_urls)}")

    groups = collect_review_groups(web_messages, identified, resolved)

    report_lines = build_report(groups, context_lines)
    report_text = "\n".join(report_lines)

    print(report_text)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(report_text + "\n")
    print(f"\n(отчёт продублирован в {build.display_path(OUTPUT_PATH)})")


if __name__ == "__main__":
    main()

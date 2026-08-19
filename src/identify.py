"""
identify.py — интерпретация результата resolve(): контакт или монетизация,
через какую сеть, персональный ли ID, есть ли erid, нужен ли ручной разбор.
"""

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CONTACT_DOMAINS, NETWORK_RULES  # noqa: E402

ERID_RE = re.compile(r'erid=', re.IGNORECASE)


def _domain_of(url: str) -> str:
    netloc = urlsplit(url).netloc
    netloc = netloc.split("@")[-1]  # user:pass@host -> host
    netloc = netloc.split(":")[0]   # host:port -> host
    return netloc.lower()


def identify(original_url: str, resolved: dict) -> dict:
    final_url = resolved.get("final_url") or original_url
    method = resolved.get("method")

    domain_final = _domain_of(final_url)
    is_contact = domain_final in CONTACT_DOMAINS

    network = None
    has_personal_id = False
    identity_params = {}

    # Контактные домены -- способ связи, не сеть монетизации (см. config.py),
    # поэтому при is_contact сеть намеренно не ищем.
    if not is_contact:
        for rule in NETWORK_RULES:
            if rule["match"] in domain_final:
                network = rule["network"]
                has_personal_id = rule["has_personal_id"]
                if has_personal_id:
                    query = parse_qs(urlsplit(final_url).query)
                    identity_params = {
                        p: query[p] for p in rule.get("id_params", []) if p in query
                    }
                    # Часть сетей (advt.pro/Workle) кладёт id в путь, не в
                    # query. Проверяем final_url и original_url той же
                    # логикой, что has_erid ниже: если резолв увёл дальше и
                    # путь с идентификатором не пережил редирект, берём его
                    # из исходной ссылки.
                    path_regex = rule.get("path_regex")
                    if path_regex:
                        m = re.search(path_regex, final_url) or re.search(path_regex, original_url)
                        if m:
                            identity_params[rule["path_id_name"]] = [m.group(1)]
                break

    # "в обоих original_url и final_url" читаю как "искать в каждом из
    # двух" (не как строгое требование присутствия в обоих сразу) -- иначе
    # пропадут случаи вроде trk.ppdu.ru, где erid есть только в исходном
    # коротком URL и не переживает редирект на лендинг.
    has_erid = bool(ERID_RE.search(original_url)) or bool(ERID_RE.search(final_url))

    needs_review = method in ("unresolved", "error") and not is_contact

    return {
        "domain_final": domain_final,
        "is_contact": is_contact,
        "network": network,
        "has_personal_id": has_personal_id,
        "identity_params": identity_params,
        "has_erid": has_erid,
        "needs_review": needs_review,
    }


def _adapt_old_resolve_test_record(record: dict) -> dict:
    """
    cache/resolve_test.json — записи старого resolve_test.py (url, final_url,
    params, domain, опционально error), без поля method. Для самопроверки на
    этих же данных достраиваем method так, как если бы это был результат
    resolve(): есть error -> "error"; final_url реально отличался от url ->
    "http" (resolve_test.py всегда шёл с allow_redirects=True, так что любое
    отличие -- настоящий HTTP-редирект); final_url совпал с url -> честно
    "unresolved" (в этих старых данных нет проверки meta-refresh/JS, поэтому
    объявлять их meta_refresh/js_location было бы додумыванием без проверки).
    """
    if "error" in record:
        return {"final_url": record["url"], "method": "error", "error": record["error"]}
    if record["final_url"] != record["url"]:
        return {"final_url": record["final_url"], "method": "http", "error": None}
    return {"final_url": record["final_url"], "method": "unresolved", "error": None}


if __name__ == "__main__":
    import json

    data_path = Path(__file__).resolve().parent.parent / "cache" / "resolve_test.json"
    with open(data_path, encoding="utf-8") as f:
        records = json.load(f)

    rows = []
    for record in records:
        adapted = _adapt_old_resolve_test_record(record)
        result = identify(record["url"], adapted)
        rows.append((
            record["url"],
            result["network"] or "-",
            "да" if result["has_personal_id"] else "нет",
            "да" if result["has_erid"] else "нет",
            "да" if result["needs_review"] else "нет",
        ))

    headers = ("url", "network", "has_personal_id", "has_erid", "needs_review")
    widths = [max(len(str(row[i])) for row in ([headers] + rows)) for i in range(len(headers))]

    def _fmt_row(row):
        return " | ".join(str(v).ljust(w) for v, w in zip(row, widths))

    print(_fmt_row(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(_fmt_row(row))

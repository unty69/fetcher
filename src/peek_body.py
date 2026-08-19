"""
peek_body.py — диагностика: почему my.saleads.pro и pxl.leads.su не дают
HTTP-редирект (resolve_test.py показал у обоих 200, редиректов 0).
Смотрим сырое тело ответа на предмет клиентского редиректа (meta refresh
или JS location.href/location.replace). Больше ничего — это диагностика.
"""

import re
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / "cache"

TIMEOUT = 10
USER_AGENT = "Mozilla/5.0"

# Те же URL, что и в resolve_test.py (там же и получили 0 редиректов).
# У pxl.leads.su в исходном списке resolve_test.py было только 4 уникальных
# URL на 5 позиций (один намеренно повторялся) — здесь взяты те же 5 позиций
# как есть, без досочинения пятого уникального.
URLS = {
    "my.saleads.pro": [
        "https://my.saleads.pro/s/gr85t",
        "https://my.saleads.pro/s/eapoc?erid=2VtzquvfJHP",
        "https://my.saleads.pro/s/k8gg0",
        "https://my.saleads.pro/s/t4i4e?erid=2VtzqxWGKWy",
        "https://my.saleads.pro/s/gsmyt?erid=2Vtzquwf1To",
    ],
    "pxl.leads.su": [
        "https://pxl.leads.su/click/4481a42fed91e7dbef7d73313618d13a",
        "https://pxl.leads.su/click/b6d3f89209503b410e953002b31a77fb",
        "https://pxl.leads.su/click/90a5992787a7a0b6959b2157ed1c2bdf",
        "https://pxl.leads.su/click/5eb6ee17127de718da42138017e916c6",
        "https://pxl.leads.su/click/4481a42fed91e7dbef7d73313618d13a",
    ],
}

LOCATION_RE = re.compile(r'location\.(?:href|replace)', re.IGNORECASE)


def find_meta_refresh_line(text: str):
    for line in text.splitlines():
        low = line.lower()
        if "<meta" in low and "refresh" in low:
            return line.strip()
    return None


def peek(domain: str, index: int, url: str) -> None:
    print(f"\n[{domain} #{index}] {url}")
    try:
        resp = requests.get(
            url, allow_redirects=False, timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as e:
        print(f"  ОШИБКА запроса: {e!r}")
        return

    text = resp.text
    out_path = CACHE_DIR / f"body_{domain}_{index}.html"
    out_path.write_text(text, encoding="utf-8", errors="replace")
    print(f"  статус: {resp.status_code}, сохранено в {out_path}")
    print(f"  длина тела: {len(text)} символов")

    meta_line = find_meta_refresh_line(text)
    if meta_line:
        print(f"  meta refresh: {meta_line}")
    else:
        print("  meta refresh: не найден")

    loc_match = LOCATION_RE.search(text)
    if loc_match:
        start = max(0, loc_match.start() - 100)
        end = min(len(text), loc_match.end() + 100)
        snippet = text[start:end].replace("\n", " ")
        print(f"  location.href/replace: ...{snippet}...")
    else:
        print("  location.href/replace: не найден")


def main():
    CACHE_DIR.mkdir(exist_ok=True)
    for domain, urls in URLS.items():
        for index, url in enumerate(urls, 1):
            peek(domain, index, url)


if __name__ == "__main__":
    main()

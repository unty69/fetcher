"""
investigate_pxl.py — статическое расследование pxl.leads.su без Playwright:
находим <script src=...lfid.min.js>, скачиваем его и читаем ТЕКСТ файла в
поисках реального API-запроса. JS нигде не выполняется, браузер не
эмулируется — только regex по исходникам.
"""

import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / "cache"
RESOLVE_TEST_JSON = CACHE_DIR / "resolve_test.json"
LFID_JS_PATH = CACHE_DIR / "lfid.min.js"

TIMEOUT = 10
USER_AGENT = "Mozilla/5.0"
SAMPLE_COUNT = 3

# В реальной разметке lfid.min.js подключается НЕ статическим
# <script src=...>, а через JS-переменную со строковым литералом
# (напр. `const primaryLfidUrl = 'https://lfid.clientctx.su/lfid.min.js';`,
# плюс отдельный fallbackLfidUrl) — видно по cache/body_pxl.leads.su_*.html
# из peek_body.py. Ищем любую кавычечную строку, содержащую "lfid.min.js",
# а не тег.
LFID_URL_RE = re.compile(r'["\']([^"\']*lfid\.min\.js[^"\']*)["\']', re.IGNORECASE)
FETCH_RE = re.compile(r'fetch\s*\(', re.IGNORECASE)
XHR_RE = re.compile(r'XMLHttpRequest|\.open\s*\(\s*["\'][A-Za-z]+["\']', re.IGNORECASE)
URL_RE = re.compile("https?://[^\\s\"')]+")
BROWSER_OBJECTS_RE = re.compile(r'\b(?:document|navigator|screen|window)\b')


def load_sample_urls(count: int) -> list:
    with open(RESOLVE_TEST_JSON, encoding="utf-8") as f:
        records = json.load(f)
    seen = set()
    urls = []
    for r in records:
        if "pxl.leads.su" in r["url"] and r["url"] not in seen:
            seen.add(r["url"])
            urls.append(r["url"])
        if len(urls) >= count:
            break
    return urls


def find_lfid_script_src(html_text: str, page_url: str):
    matches = LFID_URL_RE.findall(html_text)
    if not matches:
        return None, []
    primary = urljoin(page_url, matches[0])
    others = [urljoin(page_url, m) for m in matches[1:]]
    return primary, others


def context_around(text: str, match, before: int = 40, after: int = 150) -> str:
    start = max(0, match.start() - before)
    end = min(len(text), match.end() + after)
    return text[start:end].replace("\n", " ")


def main():
    urls = load_sample_urls(SAMPLE_COUNT)
    print(f"Беру {len(urls)} URL pxl.leads.su из resolve_test.json:")
    for u in urls:
        print(f"  {u}")

    lfid_src = None
    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] {url}")
        try:
            resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        except requests.RequestException as e:
            print(f"  ОШИБКА: {e!r}")
            continue

        src, others = find_lfid_script_src(resp.text, resp.url)
        print(f"  lfid.min.js URL: {src or 'не найден'}")
        if others:
            print(f"  другие варианты в тексте (напр. fallback): {others}")
        if src and not lfid_src:
            lfid_src = src

    if not lfid_src:
        print("\nНи на одной из страниц не нашёлся <script src=...lfid...>. Дальше идти некуда.")
        return

    print(f"\nСкачиваю {lfid_src}")
    try:
        resp = requests.get(lfid_src, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        print(f"ОШИБКА при скачивании lfid.min.js: {e!r}")
        return

    js_text = resp.text
    LFID_JS_PATH.write_text(js_text, encoding="utf-8", errors="replace")
    print(f"Сохранено в {LFID_JS_PATH}, длина {len(js_text)} символов")

    print("\n" + "=" * 70)
    print("Первые 3000 символов lfid.min.js (как есть, возможно минифицирован)")
    print("=" * 70)
    print(js_text[:3000])

    print("\n" + "=" * 70)
    print("fetch(...) / XMLHttpRequest .open(...)")
    print("=" * 70)
    fetch_hits = list(FETCH_RE.finditer(js_text))
    xhr_hits = list(XHR_RE.finditer(js_text))
    if fetch_hits:
        for m in fetch_hits:
            print(f"  fetch: ...{context_around(js_text, m)}...")
    else:
        print("  fetch(...): не найдено")
    if xhr_hits:
        for m in xhr_hits:
            print(f"  XHR: ...{context_around(js_text, m)}...")
    else:
        print("  XMLHttpRequest/.open(...): не найдено")

    print("\n" + "=" * 70)
    print("Все URL в тексте (regex https?://...)")
    print("=" * 70)
    urls_found = sorted(set(URL_RE.findall(js_text)))
    if urls_found:
        for u in urls_found:
            print(f"  {u}")
    else:
        print("  URL не найдены")

    browser_hits = len(BROWSER_OBJECTS_RE.findall(js_text))
    print(f"\nСсылок на document/navigator/screen/window в тексте: {browser_hits}")


if __name__ == "__main__":
    main()

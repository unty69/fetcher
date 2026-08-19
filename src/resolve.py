"""
resolve.py — боевая резолюция ссылок: HTTP-редирект -> meta-refresh -> JS
location.href/replace -> unresolved. Кэш на диск, без хардкода списка URL —
resolve()/resolve_batch() принимают произвольные ссылки на вход.

META_REFRESH_RE и JS_LOCATION_RE подобраны и проверены на реальных телах
ответов из cache/body_*.html (см. src/peek_body.py):
- my.saleads.pro — оба паттерна корректно вытаскивают настоящий адрес из
  статического <meta refresh> и window.location.replace("...").
- pxl.leads.su — оба НАМЕРЕННО не совпадают: там
  window.location.replace(currentUrl.toString()) собирает адрес в рантайме
  из переменной, а не из строкового литерала — статической строки для
  regex там просто нет физически. method="unresolved" для него ожидаемо,
  а не недоработка паттерна.
"""

import html
import json
import re
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_FILE = CACHE_DIR / "resolved.json"

TIMEOUT = 10
PAUSE_SECONDS = 1.5
USER_AGENT = "Mozilla/5.0"

# "refresh" и "url=" в пределах одного <meta ...> тега; порядок атрибутов
# как в реальных образцах (http-equiv раньше content/url=).
META_REFRESH_RE = re.compile(
    r'<meta[^>]+refresh[^>]+url\s*=\s*([^"\'\s>]+)', re.IGNORECASE,
)
# location.href = "..." ИЛИ location.replace("...") — только со строковым
# литералом сразу после открывающей кавычки; проверено, что НЕ ловит вызовы
# вида location.replace(someVariable) без кавычек (случай pxl.leads.su).
JS_LOCATION_RE = re.compile(
    r'location\.(?:href\s*=\s*|replace\(\s*)["\']([^"\']+)["\']', re.IGNORECASE,
)


def _normalize_scheme(url: str) -> str:
    # В ТЗ resolve.py этого шага нет явно, но без него requests.get() падает
    # с MissingSchema на голых доменах (напр. "clck.ru/3VDG5v") — а именно
    # такие URL реально попадаются из probe.py. Добавлено по аналогии с
    # resolve_test.py, чтобы боевая версия не спотыкалась на том, что уже
    # встречалось в данных.
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def resolve(url: str) -> dict:
    request_url = _normalize_scheme(url)
    try:
        resp = requests.get(
            request_url, allow_redirects=True, timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as e:
        return {"final_url": request_url, "method": "error", "error": repr(e)}

    if resp.url != request_url:
        return {"final_url": resp.url, "method": "http", "error": None}

    meta_match = META_REFRESH_RE.search(resp.text)
    if meta_match:
        return {
            "final_url": html.unescape(meta_match.group(1)),
            "method": "meta_refresh",
            "error": None,
        }

    js_match = JS_LOCATION_RE.search(resp.text)
    if js_match:
        return {
            "final_url": html.unescape(js_match.group(1)),
            "method": "js_location",
            "error": None,
        }

    return {"final_url": request_url, "method": "unresolved", "error": None}


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def resolve_batch(urls: list) -> dict:
    cache = _load_cache()
    results = {}
    made_request = False

    for url in urls:
        if url in cache:
            results[url] = cache[url]
            continue

        if made_request:
            time.sleep(PAUSE_SECONDS)

        result = resolve(url)
        results[url] = result
        cache[url] = result
        _save_cache(cache)
        made_request = True

    return results

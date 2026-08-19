"""
resolve_playwright.py — резолюция ссылок, застрявших на pxl.leads.su, через
реальный браузер: финальный редирект там собирается в рантайме через
currentUrl.toString(), статический HTTP-запрос (resolve.py) в принципе не
может это увидеть.

Ключевой нюанс (вскрылся при пересборке списка из дампов): "застрявшие" --
это НЕ ТОЛЬКО прямые ссылки на pxl.leads.su. Прямых таких ссылок в
квалифицирующих URL всего 22, и все 22 уже успешно резолвлены прошлым
прогоном. Основная масса -- это шортенеры из ref_domains.txt (почти
исключительно clck.ru, плюс единичные refk.in), которые resolve.py резолвит
ОДНИМ HTTP-хопом ДО pxl.leads.su/click/... и останавливается: resp.url
меняется, значит method="http" -- с точки зрения resolve.py "готово", хотя
сам pxl.leads.su после этого редиректит ДАЛЬШЕ уже через JS, которую голый
HTTP-запрос не видит. На вход playwright'у поэтому идёт не "исходный список
pxl.leads.su-ссылок", а результат сканирования кэша: любой закэшированный
final_url, указывающий на pxl.leads.su, независимо от текущего (ложного)
method="http" -- см. collect_todo_urls().

Пишет в тот же cache/resolved.json, что использует resolve.py -- identify.py
и build.py подхватят результат автоматически, без изменений там. resolve.py
не трогается, импортируется только путь до кэша (resolve.CACHE_FILE).
Playwright резолвит ИСХОДНЫЙ url (не final_url-заглушку) -- реальный браузер
сам проходит всю цепочку (HTTP-хопы + JS-редирект) за один заход, а
результат перезаписывает ТОТ ЖЕ ключ кэша, который уже читает identify.py
(там однохоповый lookup, не трогаем).

Список пересобирается заново при КАЖДОМ запуске из всех дампов
cache/dump*.jsonl (build.load_ref_domains/load_dump_and_classify -- та же
квалификация, что в боевой сборке, без дублирования логики), а не из
статического input/pxl_leads_urls.txt: тот список считал только прямые
pxl.leads.su-ссылки и не отражал реальный непокрытый остаток (135
url-редиректоров на данный момент).

"Уже есть в кэше" здесь значит "final_url НЕ указывает на pxl.leads.su" --
не смотрим на method вообще, потому что как раз method="http" -- основной
источник ложных "готово" (см. выше). НЕ учтён повторный прогон ПОСЛЕ
неудачной playwright-попытки (error/no_redirect) для url-цепочки через
шортенер: тогда final_url откатится на домен самого шортенера, а не
pxl.leads.su, и на следующем прогоне такой url перестанет отбираться этим
правилом. Для текущего прогона неактуально -- прошлых playwright-попыток по
этой конкретной цепочке ещё не было, все 135 сейчас честно стоят как
method="http".
"""

import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402 -- только ради load_ref_domains/load_dump_and_classify
import resolve  # noqa: E402 -- только ради resolve.CACHE_FILE

DUMP_PATHS = [
    BASE_DIR / "cache" / "dump.jsonl",
    BASE_DIR / "cache" / "dump_tier2.jsonl",
    BASE_DIR / "cache" / "dump_depth_probe.jsonl",
    BASE_DIR / "cache" / "dump_tier3.jsonl",
]
PXL_LEADS_DOMAIN = "pxl.leads.su"
CACHE_FILE = resolve.CACHE_FILE

GOTO_TIMEOUT_MS = 15000
WAIT_AFTER_GOTO_MS = 4000  # известная задержка редиректа их скрипта ~2000мс + запас
PAUSE_BETWEEN_TABS_RANGE = (1, 2)


def collect_todo_urls(cache: dict) -> list:
    """Из ВСЕХ квалифицирующих URL (4 дампа, та же квалификация, что и в
    build.py) отбирает застрявшие на pxl.leads.su:
    - URL не в кэше вообще, а сам он -- прямая ссылка на pxl.leads.su;
    - URL в кэше есть, но его final_url указывает на pxl.leads.su (метод
      кэша при этом не проверяем -- см. докстринг модуля про method="http").

    Фильтр домена -- через build.matches_ref_domain(), а не голый
    urlparse(u).netloc: часть ссылок в дампах без схемы ("pxl.leads.su/
    click/..." прямо текстом в сообщении, без "https://"), urlparse без
    build._with_scheme() отдаёт им пустой netloc и молча теряет такие URL."""
    ref_domains = build.load_ref_domains()
    *_, unique_resolve_urls = build.load_dump_and_classify(DUMP_PATHS, ref_domains)

    todo = []
    for u in sorted(unique_resolve_urls):
        entry = cache.get(u)
        if entry is None:
            if build.matches_ref_domain(u, {PXL_LEADS_DOMAIN}):
                todo.append(u)
            continue
        final_url = entry.get("final_url") or u
        if build.matches_ref_domain(final_url, {PXL_LEADS_DOMAIN}):
            todo.append(u)
    return todo


def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def _normalize_scheme(url: str) -> str:
    """page.goto() (в отличие от адресной строки браузера) требует полный
    URL со схемой -- часть ссылок в дампах хранится без "https://" (тот же
    приём, что и _normalize_scheme в resolve.py, отдельная копия здесь
    осознанно -- resolve.py не импортируем, кроме CACHE_FILE, см. докстринг
    модуля)."""
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def resolve_one(page, url: str) -> dict:
    normalized = _normalize_scheme(url)
    try:
        page.goto(normalized, timeout=GOTO_TIMEOUT_MS)
        page.wait_for_timeout(WAIT_AFTER_GOTO_MS)
        final_url = page.url
    except Exception as e:
        return {"final_url": normalized, "method": "error", "error": repr(e)}

    if final_url == normalized:
        # Не путать с успехом -- может значить, что скрипт сайта распознал
        # автоматизацию и не повёл дальше.
        return {"final_url": final_url, "method": "no_redirect", "error": None}
    return {"final_url": final_url, "method": "playwright", "error": None}


def main():
    cache = load_cache()
    todo = collect_todo_urls(cache)
    print(f"К обработке (застряли на {PXL_LEADS_DOMAIN}): {len(todo)}")

    if not todo:
        print("Нечего резолвить, браузер не запускаю.")
        return

    stats = {"playwright": 0, "no_redirect": 0, "error": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            for i, url in enumerate(todo):
                page = browser.new_page()
                try:
                    result = resolve_one(page, url)
                finally:
                    page.close()

                cache[url] = result
                save_cache(cache)
                stats[result["method"]] += 1

                if result["method"] == "error":
                    print(f"[error] {url} -> {result['error']}")
                else:
                    print(f"[{result['method']}] {url} -> {domain_of(result['final_url'])}")

                if i < len(todo) - 1:
                    time.sleep(random.uniform(*PAUSE_BETWEEN_TABS_RANGE))
        finally:
            browser.close()

    print(f"\nГотово. Успешных (playwright): {stats['playwright']}, "
          f"no_redirect: {stats['no_redirect']}, error: {stats['error']}")


if __name__ == "__main__":
    main()

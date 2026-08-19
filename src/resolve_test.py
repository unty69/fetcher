"""
resolve_test.py — проверка стабильности резолюции коротких/трекинговых
ссылок перед проектированием identify.py. Только HTTP, никакого Telegram.

Каждый URL резолвится отдельным запросом, даже если он повторяется в
списке — это специально: цель понять, отдаёт ли редиректор один и тот
же финальный URL/параметры при повторном заходе, или "плавает" (например,
свежий click-id на каждый заход). Это важно знать до того, как identify.py
начнёт полагаться на финальный URL как на стабильный идентификатор.
"""

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / "cache"
JSON_OUTPUT = CACHE_DIR / "resolve_test.json"

TIMEOUT = 10
PAUSE_SECONDS = 1.5
USER_AGENT = "Mozilla/5.0"

# Список собран из probe_report.txt (БЛОК 5). Часть URL повторяется
# намеренно (clck.ru/3V3eMb x3, goo.su/FUGvsLy x2,
# pxl.leads.su/click/4481a42... x2, vk.cc/cWLKLc x2) — на них ниже
# проверяется стабильность резолюции.
URLS = [
    "https://my.saleads.pro/s/gr85t",
    "https://my.saleads.pro/s/eapoc?erid=2VtzquvfJHP",
    "https://my.saleads.pro/s/k8gg0",
    "https://my.saleads.pro/s/t4i4e?erid=2VtzqxWGKWy",
    "https://pxl.leads.su/click/4481a42fed91e7dbef7d73313618d13a",
    "https://pxl.leads.su/click/b6d3f89209503b410e953002b31a77fb",
    "https://pxl.leads.su/click/90a5992787a7a0b6959b2157ed1c2bdf",
    "https://pxl.leads.su/click/5eb6ee17127de718da42138017e916c6",
    "https://pxl.leads.su/click/4481a42fed91e7dbef7d73313618d13a",
    "https://trk.ppdu.ru/click/2nAKIaKZ?erid=CQH36pWzJqVGXC5oLP8WVVNCNqJmbhiUPijGiu4zpwPd7",
    "https://vk.cc/cQeFHl",
    "https://vk.cc/cW97HW",
    "https://vk.cc/cWLKLc",
    "https://vk.cc/cWLKLc",
    "clck.ru/3VDG5v",
    "clck.ru/3V3eMb",
    "clck.ru/3V3eMb",
    "https://clck.ru/3SDRMw",
    "clck.ru/3V3eMb",
    "https://goo.su/FUGvsLy",
    "https://goo.su/FUGvsLy",
    "https://goo.su/VWUpf",
    "https://goo.su/7fGaCu",
    "https://goo.su/u0lSi5m",
    "https://my.saleads.pro/s/gsmyt?erid=2Vtzquwf1To",
]


def normalize_scheme(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def domain_of(url: str) -> str:
    netloc = urlsplit(url).netloc
    netloc = netloc.split("@")[-1]  # user:pass@host -> host
    netloc = netloc.split(":")[0]   # host:port -> host
    return netloc.lower()


def resolve(url: str) -> dict:
    try:
        resp = requests.get(
            url, allow_redirects=True, timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        final_url = resp.url
        return {
            "ok": True,
            "final_url": final_url,
            "params": parse_qs(urlsplit(final_url).query),
            "domain": domain_of(final_url),
            "status": resp.status_code,
            "redirect_count": len(resp.history),
        }
    except requests.RequestException as e:
        return {"ok": False, "error": repr(e)}


def to_json_record(raw_url: str, result: dict) -> dict:
    if result["ok"]:
        return {
            "url": raw_url,
            "final_url": result["final_url"],
            "params": result["params"],
            "domain": result["domain"],
        }
    return {
        "url": raw_url,
        "final_url": None,
        "params": None,
        "domain": None,
        "error": result["error"],
    }


def main():
    results = []
    json_records = []
    total = len(URLS)
    for i, raw_url in enumerate(URLS, 1):
        url = normalize_scheme(raw_url)
        print(f"\n[{i}/{total}] {raw_url}")
        result = resolve(url)
        results.append((raw_url, url, result))

        if result["ok"]:
            print(f"  финальный URL: {result['final_url']}")
            print(f"  query-параметры: {result['params']}")
            print(f"  домен: {result['domain']}")
            print(f"  статус: {result['status']}, редиректов: {result['redirect_count']}")
        else:
            print(f"  ОШИБКА: {result['error']}")

        json_records.append(to_json_record(raw_url, result))

        if i < total:
            time.sleep(PAUSE_SECONDS)

    # --- Сравнение дублей: один и тот же исходный URL, встретившийся
    # несколько раз в списке — резолвится ли он каждый раз одинаково?
    # Группируем не по хардкоду конкретных URL, а по факту повтора —
    # так надёжнее и не завязано на то, какие именно ссылки повторятся.
    groups = {}
    for raw_url, normalized_url, result in results:
        groups.setdefault(normalized_url, []).append(result)

    print("\n" + "=" * 70)
    print("Сравнение дублей (один и тот же исходный URL, 2+ раза в списке)")
    print("=" * 70)

    duplicates = {url: group for url, group in groups.items() if len(group) > 1}
    if not duplicates:
        print("Дублей в списке не найдено.")

    for url, group in duplicates.items():
        ok_results = [r for r in group if r["ok"]]
        print(f"\n{url} — встретился {len(group)} раз(а), успешных резолюций: {len(ok_results)}")

        if len(ok_results) < len(group):
            print("  часть попыток завершилась ошибкой — сравнение неполное")

        if len(ok_results) >= 2:
            finals = [r["final_url"] for r in ok_results]
            params = [r["params"] for r in ok_results]
            urls_match = all(f == finals[0] for f in finals)
            params_match = all(p == params[0] for p in params)

            print(f"  финальный URL совпал во всех повторах: {'да' if urls_match else 'НЕТ'}")
            print(f"  query-параметры совпали во всех повторах: {'да' if params_match else 'НЕТ'}")

            if not urls_match:
                for j, f in enumerate(finals, 1):
                    print(f"    попытка {j}: {f}")
            if not params_match:
                for j, p in enumerate(params, 1):
                    print(f"    попытка {j} params: {p}")

    CACHE_DIR.mkdir(exist_ok=True)
    with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(json_records, f, ensure_ascii=False, indent=2)
    print(f"\nСохранено {len(json_records)} записей в {JSON_OUTPUT}")


if __name__ == "__main__":
    main()

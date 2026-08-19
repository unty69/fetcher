import json
import os
import time

import requests

URL_PLATFORMS = "https://vacancy.quickhire.ru/api/test-platforms/?city=2415&tariff=5"
URL_CITIES = "https://vacancy.quickhire.ru/api/test-cities/"
URL_TARIFFS = "https://vacancy.quickhire.ru/api/test-tariffs/?city=2415"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://vacancy.quickhire.ru/?city=2415&tariff=5&ord=t",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
COOKIES = {"csrftoken": "aIzYwMRn96gR4g1BxeI0W6kqd9dzTZ1z"}


def fetch(url):
    response = requests.get(url, headers=HEADERS, cookies=COOKIES)
    if response.status_code != 200:
        print(response.status_code)
        print(response.text[:500])
        raise SystemExit(1)
    data = response.json()
    print(len(data))
    return data


def save(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def main():
    os.makedirs("cache", exist_ok=True)

    platforms = fetch(URL_PLATFORMS)
    save(platforms, "cache/quickhire_platforms_full.json")

    time.sleep(1.5)

    cities = fetch(URL_CITIES)
    save(cities, "cache/quickhire_cities.json")

    time.sleep(1.5)

    tariffs = fetch(URL_TARIFFS)
    save(tariffs, "cache/quickhire_tariffs.json")


if __name__ == "__main__":
    main()

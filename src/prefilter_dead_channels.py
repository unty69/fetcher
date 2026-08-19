"""
prefilter_dead_channels.py — проверяет channels_tier3_remaining.txt через
t.me/s/{channel} (обычный HTTP, без Telethon/сессии/флуд-бюджета).
Эвристика не стопроцентная (Telegram может менять разметку страницы),
поэтому результат — список на ручной просмотр, не автоисключение.
"""
import csv, re, time
from pathlib import Path
from collections import Counter
import requests

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_PATH = BASE_DIR / "input" / "channels_tier3_remaining.txt"
OUTPUT_PATH = BASE_DIR / "cache" / "tier3_prefilter.csv"
TIMEOUT, PAUSE_SECONDS = 10, 1.0
CHANNEL_INFO_RE = re.compile(r'tgme_channel_info')

def check_channel(channel: str) -> str:
    try:
        resp = requests.get(f"https://t.me/s/{channel}", timeout=TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as e:
        return f"error:{e!r}"
    if resp.status_code != 200:
        return f"http_{resp.status_code}"
    return "likely_alive" if CHANNEL_INFO_RE.search(resp.text) else "likely_dead_or_private"

def main():
    channels = [l.strip() for l in open(INPUT_PATH, encoding="utf-8") if l.strip()]
    print(f"К проверке: {len(channels)}")
    results = []
    for i, ch in enumerate(channels):
        status = check_channel(ch)
        results.append({"channel": ch, "status": status})
        print(f"[{i+1}/{len(channels)}] {ch}: {status}")
        with open(OUTPUT_PATH, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["channel", "status"]); w.writeheader(); w.writerows(results)
        if i < len(channels) - 1:
            time.sleep(PAUSE_SECONDS)
    print(f"\nИтого: {dict(Counter(r['status'] for r in results))}\nСохранено -> {OUTPUT_PATH}")

if __name__ == "__main__":
    main()

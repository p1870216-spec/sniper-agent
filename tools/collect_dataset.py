"""Сбор исторического набора твитов с контрактами — под ручную разметку.

Зачем: чтобы понять, стоит ли ставить LLM-фильтр перед отправкой алерта,
нужна доля ложных срабатываний парсера. Ждать её при 3-4 срабатываниях
в день — месяц. Из истории те же примеры достаются за один прогон.

Идёт по аккаунтам из config/kols.yml, листает last_tweets курсором,
прогоняет каждый твит через боевой parser.parse() и складывает те, где
парсер нашёл контракт. Единица разметки — не твит, а пара (твит, токен):
именно на неё пайплайн отправляет одно сообщение.

Запуск:  python tools/collect_dataset.py [--pages N] [--since-days N]

Сырые твиты кешируются в data/raw_tweets.json — повторный запуск не
перекачивает то, что уже скачано. Лимит free-tier (1 запрос / 5 с)
соблюдается паузами; на 16 аккаунтов по 15 страниц уходит ~20 минут.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_kols            # noqa: E402
from app.parser import parse                # noqa: E402
from app.providers.twitterapi_io import normalize_tweet  # noqa: E402

API = "https://api.twitterapi.io/twitter/user/last_tweets"
PAUSE = 5.5                                  # free-tier: 1 запрос / 5 с
RAW = ROOT / "data" / "raw_tweets.json"
CSV_OUT = ROOT / "data" / "labeling_set.csv"
JSONL_OUT = ROOT / "data" / "labeling_set.jsonl"

# Разметка: одна из этих меток в колонку label.
LABELS = """call      — свежий колл, по нему имеет смысл действовать
discussion— обсуждение токена, который уже торгуется
joke      — шутка, мем, риторическая фигура
warning   — предупреждение о скаме/руге
spam      — шилл-спам, накрутка
unclear   — по тексту не определить"""


def api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TWITTERAPI_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("TWITTERAPI_KEY не найден в .env")


def fetch(handle: str, pages: int, since: datetime, headers: dict) -> list[dict]:
    """Листает твиты аккаунта, пока не кончатся страницы или не упрёмся в since."""
    out: list[dict] = []
    cursor = None
    for page in range(pages):
        params = {"userName": handle}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = httpx.get(API, params=params, headers=headers, timeout=30.0)
            data = resp.json()
        except Exception as exc:                      # noqa: BLE001
            print(f"  {handle}: страница {page + 1} упала ({type(exc).__name__}), останавливаюсь")
            break

        tweets = (data.get("data") or {}).get("tweets") or []
        if not tweets:
            break
        out.extend(tweets)

        oldest = min(
            (parsedate_to_datetime(t["createdAt"]).astimezone(timezone.utc)
             for t in tweets if t.get("createdAt")),
            default=None,
        )
        if oldest and oldest < since:
            break
        if not data.get("has_next_page"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
        time.sleep(PAUSE)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=15, help="максимум страниц на аккаунт")
    ap.add_argument("--since-days", type=int, default=180, help="как глубоко копать")
    args = ap.parse_args()

    headers = {"X-API-Key": api_key()}
    since = datetime.now(timezone.utc) - timedelta(days=args.since_days)

    raw: dict[str, list[dict]] = {}
    if RAW.exists():
        raw = json.loads(RAW.read_text(encoding="utf-8"))
        print(f"кеш: {sum(len(v) for v in raw.values())} твитов по {len(raw)} аккаунтам")

    handles = list(load_kols().keys())
    for i, handle in enumerate(handles, 1):
        if handle in raw:
            print(f"[{i}/{len(handles)}] {handle}: уже в кеше ({len(raw[handle])})")
            continue
        print(f"[{i}/{len(handles)}] {handle}: качаю...", flush=True)
        tweets = fetch(handle, args.pages, since, headers)
        raw[handle] = tweets
        RAW.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        print(f"    {len(tweets)} твитов", flush=True)
        time.sleep(PAUSE)

    # --- разбор: пара (твит, токен) = одна строка разметки ---
    rows = []
    total = skipped_rt = 0
    for handle, tweets in raw.items():
        for t in tweets:
            tweet = normalize_tweet(t)
            if tweet is None:
                continue
            total += 1
            if tweet.is_retweet:
                skipped_rt += 1
                continue
            result = parse(tweet.text, expanded_urls=tweet.expanded_urls)
            if not result.has_contract:
                continue
            for token in result.tokens:
                rows.append({
                    "label": "",
                    "note": "",
                    "author": tweet.author,
                    "created_at": tweet.created_at.isoformat(),
                    "address": token.address,
                    "chain": token.chain,
                    "source": token.source,
                    "confidence": f"{token.confidence:.2f}",
                    "is_reply": int(tweet.is_reply),
                    "text": tweet.text.replace("\n", " ").strip(),
                    "url": tweet.url,
                    "tweet_id": tweet.id,
                })

    rows.sort(key=lambda r: r["created_at"], reverse=True)

    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    with JSONL_OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    uniq_addr = len({r["address"] for r in rows})
    print()
    print(f"твитов всего        : {total}")
    print(f"  из них ретвитов   : {skipped_rt} (пропущены, пайплайн их тоже отбрасывает)")
    print(f"строк для разметки  : {len(rows)}")
    print(f"уникальных адресов  : {uniq_addr}")
    print(f"авторов в наборе    : {len({r['author'] for r in rows})}")
    print()
    print(f"-> {CSV_OUT}")
    print(f"-> {JSONL_OUT}")
    print()
    print("метки для колонки label:")
    print(LABELS)


if __name__ == "__main__":
    main()

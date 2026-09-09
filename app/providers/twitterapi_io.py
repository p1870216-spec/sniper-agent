"""Адаптер twitterapi.io.

Два режима:
  * stream  — регистрируем аккаунты в их мониторинге, они шлют нам вебхук.
              Основной режим: дешевле и быстрее поллинга.
  * polling — /twitter/user/last_tweets по кругу. Запасной вариант, дороже.
              В их же доках написано не дёргать этот эндпоинт часто.

ВАЖНО: имена полей в ответе стоит сверить с актуальными доками
(https://docs.twitterapi.io) — API живой и поля периодически меняются.
Вся привязка к формату собрана в normalize_tweet(), правится в одном месте.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import Settings
from app.models import Tweet

log = logging.getLogger(__name__)


def _parse_created_at(value: Any) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value)
    # Формат твиттера: "Tue Dec 10 07:00:00 +0000 2024"
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        log.warning("не разобрал дату: %r", text)
        return datetime.now(timezone.utc)


def normalize_tweet(payload: dict[str, Any]) -> Tweet | None:
    """Сырой JSON твита -> Tweet. None, если это не твит."""
    tweet_id = payload.get("id") or payload.get("id_str")
    if not tweet_id:
        return None

    author_obj = payload.get("author") or payload.get("user") or {}
    author = (
        author_obj.get("userName")
        or author_obj.get("screen_name")
        or payload.get("userName")
        or ""
    ).lstrip("@")

    text = payload.get("text") or payload.get("full_text") or ""

    expanded: list[str] = []
    entities = payload.get("entities") or {}
    for url_obj in entities.get("urls") or []:
        link = url_obj.get("expanded_url") or url_obj.get("unwound_url")
        if link:
            expanded.append(link)

    return Tweet(
        id=str(tweet_id),
        author=author,
        text=text,
        url=payload.get("url") or f"https://x.com/{author}/status/{tweet_id}",
        created_at=_parse_created_at(payload.get("createdAt") or payload.get("created_at")),
        expanded_urls=expanded,
        is_retweet=bool(payload.get("retweeted_tweet") or text.startswith("RT @")),
        is_reply=bool(payload.get("inReplyToId") or payload.get("in_reply_to_status_id")),
        raw=payload,
    )


def extract_tweets(payload: dict[str, Any]) -> list[Tweet]:
    """Достаёт твиты из тела вебхука или из ответа last_tweets."""
    candidates: list[dict[str, Any]] = []
    for key in ("tweets", "data", "events", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            nested = value.get("tweets")
            if isinstance(nested, list):
                candidates.extend(item for item in nested if isinstance(item, dict))
    if not candidates and payload.get("id"):
        candidates.append(payload)

    tweets = [normalize_tweet(item) for item in candidates]
    return [t for t in tweets if t is not None]


class TwitterApiIoClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.twitterapi_base,
            headers={"X-API-Key": settings.twitterapi_key},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def register_stream(self, handles: list[str], webhook_url: str) -> None:
        """Подписывает аккаунты на мониторинг с доставкой в наш вебхук.

        Путь эндпоинта сверь с доками перед первым запуском — у них есть
        отдельный раздел про stream/webhook, и он обновляется.
        """
        for handle in handles:
            try:
                resp = await self._client.post(
                    "/oapi/x_user_stream/add_user_to_monitor_tweet",
                    json={"userName": handle, "webhookUrl": webhook_url},
                )
                resp.raise_for_status()
                log.info("подписан на @%s", handle)
            except httpx.HTTPError as exc:
                log.error("не смог подписаться на @%s: %s", handle, exc)

    async def fetch_last_tweets(self, handle: str, since_id: str | None = None) -> list[Tweet]:
        """Резервный поллинг. Дороже стрима — использовать точечно."""
        params: dict[str, Any] = {"userName": handle}
        if since_id:
            params["sinceId"] = since_id
        try:
            resp = await self._client.get("/twitter/user/last_tweets", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("поллинг @%s упал: %s", handle, exc)
            return []
        return extract_tweets(resp.json())

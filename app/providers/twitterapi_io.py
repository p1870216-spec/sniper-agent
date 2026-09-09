"""Адаптер twitterapi.io.

Два режима:
  * stream  — заводим правила tweet_filter, они шлют нам вебхук.
              Основной режим: дешевле и быстрее поллинга.
  * polling — /twitter/user/last_tweets по кругу. Запасной вариант, дороже.
              В их же доках написано не дёргать этот эндпоинт часто.

Формат сверен с живым ответом /twitter/user/last_tweets 2026-09-09:
поля camelCase (createdAt, author.userName, isReply) — как в api-reference.
Блог про вебхуки описывает их иначе (created_at, author.username) — там
ошибка, верить api-reference. API живой, при странностях сверять заново.
Вся привязка к формату собрана в normalize_tweet(), правится в одном месте.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import Settings
from app.models import Tweet

log = logging.getLogger(__name__)


class TwitterApiIoError(RuntimeError):
    """Отказ, пришедший с HTTP 200.

    У них ошибки oapi-эндпоинтов приезжают в теле как {"status": "error"},
    статус при этом 200 — raise_for_status() такое молча пропускает.
    """


def _checked(resp: httpx.Response) -> dict[str, Any]:
    """raise_for_status + проверка поля status в теле."""
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise TwitterApiIoError(str(data.get("msg") or "без сообщения"))
    return data if isinstance(data, dict) else {}


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

    # У ретвитов text обрезан ~140 символами с многоточием, полный текст
    # лежит в retweeted_tweet.text. Пайплайн ретвиты отбрасывает, но если
    # это изменится — брать текст оттуда, иначе CA в хвосте потеряется.
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
        is_reply=bool(
            payload.get("isReply")
            or payload.get("inReplyToId")
            or payload.get("in_reply_to_status_id")
        ),
        raw=payload,
    )


# Тег, по которому узнаём свои правила среди чужих в том же аккаунте.
RULE_TAG_PREFIX = "sniper"

# Free-tier пропускает один запрос в 5 секунд, отсюда пауза и ретраи.
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 6.0

# Лимит поля value у них — 255 символов, длинный список KOL режем на части.
RULE_VALUE_LIMIT = 255


def build_rule_values(handles: list[str], limit: int = RULE_VALUE_LIMIT) -> list[str]:
    """Собирает выражения вида "from:a OR from:b" в пределах лимита value."""
    values: list[str] = []
    current: list[str] = []
    for handle in handles:
        term = f"from:{handle.lstrip('@')}"
        candidate = " OR ".join(current + [term])
        if current and len(candidate) > limit:
            values.append(" OR ".join(current))
            current = [term]
        else:
            current.append(term)
    if current:
        values.append(" OR ".join(current))
    return values


def extract_tweets(payload: dict[str, Any]) -> list[Tweet]:
    """Достаёт твиты из тела вебхука или из ответа last_tweets.

    Реальный конверт last_tweets (сверено 2026-09-09):
        {"status", "code", "msg", "has_next_page", "next_cursor",
         "data": {"pin_tweet": ..., "tweets": [...]}}
    Твиты лежат в data.tweets, а не в топ-левел tweets, как написано в
    доках — это ловится веткой с вложенным ключом ниже.
    """
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

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Запрос с ретраями на 429 и на обрыв связи.

        На free-tier лимит — один запрос в 5 секунд, а register_stream
        делает несколько вызовов подряд и без пауз ловит 429. Сетевые
        обрывы тоже наблюдались живьём, причём на DELETE: правило тогда
        остаётся висеть, поэтому повтор здесь важнее экономии времени.
        """
        delay = RETRY_BASE_DELAY
        last: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await self._client.request(method, path, **kw)
                if resp.status_code != 429:
                    return resp
                log.warning("429 на %s, жду %.0fс", path, delay)
            except httpx.HTTPError as exc:
                last = exc
                log.warning("%s на %s, повтор через %.0fс", type(exc).__name__, path, delay)
            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(delay)
                delay *= 2
        if last is not None:
            raise last
        raise TwitterApiIoError(f"{path}: не пробился через лимит запросов")

    async def list_rules(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/oapi/tweet_filter/get_rules")
        return _checked(resp).get("rules") or []

    async def delete_rule(self, rule_id: str) -> None:
        resp = await self._request(
            "DELETE", "/oapi/tweet_filter/delete_rule", json={"rule_id": rule_id}
        )
        _checked(resp)

    async def register_stream(
        self, handles: list[str], interval_seconds: float | None = None
    ) -> list[str]:
        """Заводит правила tweet_filter под наши аккаунты. Возвращает rule_id.

        Webhook URL через API не задаётся вообще — только в веб-кабинете,
        Tweet Filter Rules -> поле Webhook URL. Поэтому здесь его нет.

        Идемпотентна: правила с нашим тегом сносятся и создаются заново,
        иначе повторный вызов молча удвоил бы счёт за проверки.

        Правило создаётся неактивным (is_effect=0), активируется отдельным
        update_rule — у них так устроено, без второго вызова оно не работает.

        Осторожно: update_rule отвечает "update rule failed", если тело не
        меняет ни одного поля. Здесь изменение всегда есть (is_effect 0->1),
        но при правках следить, чтобы вызов не превратился в no-op.
        """
        interval = (
            interval_seconds
            if interval_seconds is not None
            else self._settings.filter_interval_seconds
        )

        for rule in await self.list_rules():
            if str(rule.get("tag", "")).startswith(RULE_TAG_PREFIX):
                try:
                    await self.delete_rule(rule["rule_id"])
                    log.info("снял старое правило %s", rule["rule_id"])
                except (httpx.HTTPError, TwitterApiIoError) as exc:
                    log.error("не смог снять правило %s: %s", rule.get("rule_id"), exc)

        rule_ids: list[str] = []
        for chunk, value in enumerate(build_rule_values(handles)):
            tag = f"{RULE_TAG_PREFIX}-{chunk}"
            try:
                resp = await self._request(
                    "POST",
                    "/oapi/tweet_filter/add_rule",
                    json={"tag": tag, "value": value, "interval_seconds": interval},
                )
                rule_id = _checked(resp).get("rule_id")
                if not rule_id:
                    log.error("add_rule без rule_id: %s", resp.text[:200])
                    continue

                # Активация. update_rule требует все поля, не только is_effect.
                resp = await self._request(
                    "POST",
                    "/oapi/tweet_filter/update_rule",
                    json={
                        "rule_id": rule_id,
                        "tag": tag,
                        "value": value,
                        "interval_seconds": interval,
                        "is_effect": 1,
                    },
                )
                _checked(resp)

                rule_ids.append(rule_id)
                log.info("правило %s активно: %s", rule_id, value)
            except (httpx.HTTPError, TwitterApiIoError) as exc:
                log.error("не смог завести правило %r: %s", value, exc)

        if not rule_ids:
            log.error("не заведено ни одного правила — стрима не будет")
        return rule_ids

    async def fetch_last_tweets(self, handle: str, since_id: str | None = None) -> list[Tweet]:
        """Резервный поллинг. Дороже стрима — использовать точечно."""
        params: dict[str, Any] = {"userName": handle}
        if since_id:
            params["sinceId"] = since_id
        try:
            resp = await self._client.get("/twitter/user/last_tweets", params=params)
            data = _checked(resp)
        except (httpx.HTTPError, TwitterApiIoError) as exc:
            log.error("поллинг @%s упал: %s", handle, exc)
            return []
        return extract_tweets(data)

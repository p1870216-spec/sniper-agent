"""Регрессия на формат ответа twitterapi.io.

Payload — урезанный, но настоящий ответ /twitter/user/last_tweets,
снятый 2026-09-09. Если провайдер поменяет имена полей, тест упадёт
раньше, чем это молча превратится в потерянные алерты.
"""

import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.providers.twitterapi_io import (
    TwitterApiIoError,
    _checked,
    build_rule_values,
    extract_tweets,
    normalize_tweet,
)

# Конверт настоящий: твиты лежат в data.tweets, а не в топ-левел tweets.
LAST_TWEETS_RESPONSE = {
    "status": "success",
    "code": 0,
    "msg": "success",
    "has_next_page": True,
    "next_cursor": "DAABCgABG...",
    "data": {
        "pin_tweet": None,
        "tweets": [
            {
                "type": "tweet",
                "id": "2097565690756755932",
                "url": "https://x.com/elonmusk/status/2097565690756755932",
                "twitterUrl": "https://twitter.com/elonmusk/status/2097565690756755932",
                "text": "Worth reading https://t.co/abc123",
                "createdAt": "Wed Sep 09 06:00:00 +0000 2026",
                "isReply": False,
                "inReplyToId": None,
                "lang": "en",
                "author": {
                    "type": "user",
                    "userName": "elonmusk",
                    "id": "44196397",
                    "name": "Elon Musk",
                },
                "entities": {
                    "urls": [
                        {
                            "url": "https://t.co/abc123",
                            "display_url": "x.com/i/article/2097…",
                            "expanded_url": "https://x.com/i/article/2097390019019456512",
                            "indices": [14, 37],
                        }
                    ]
                },
                "quoted_tweet": None,
                "retweeted_tweet": None,
            }
        ],
    },
}


def test_envelope_data_tweets():
    """Твиты достаются из вложенного data.tweets."""
    tweets = extract_tweets(LAST_TWEETS_RESPONSE)
    assert len(tweets) == 1
    assert tweets[0].id == "2097565690756755932"


def test_fields_from_live_response():
    t = normalize_tweet(LAST_TWEETS_RESPONSE["data"]["tweets"][0])
    assert t.author == "elonmusk"
    assert t.url == "https://x.com/elonmusk/status/2097565690756755932"
    assert t.expanded_urls == ["https://x.com/i/article/2097390019019456512"]
    assert not t.is_retweet
    assert not t.is_reply


def test_created_at_format():
    """Формат даты у них твиттеровский, не ISO. Ошибка здесь убивает latency."""
    t = normalize_tweet(LAST_TWEETS_RESPONSE["data"]["tweets"][0])
    assert t.created_at.tzinfo is not None
    assert t.created_at.astimezone(timezone.utc).isoformat() == "2026-09-09T06:00:00+00:00"


def test_is_reply_from_boolean_field():
    """У них есть явный isReply — inReplyToId при этом может быть null."""
    payload = dict(LAST_TWEETS_RESPONSE["data"]["tweets"][0])
    payload["isReply"] = True
    payload["inReplyToId"] = None
    assert normalize_tweet(payload).is_reply


def test_retweet_detected_by_object():
    payload = dict(LAST_TWEETS_RESPONSE["data"]["tweets"][0])
    payload["retweeted_tweet"] = {"id": "1", "text": "оригинал"}
    payload["text"] = "RT @someone: обрезанный хвост…"
    assert normalize_tweet(payload).is_retweet


def test_rule_values_single_chunk():
    assert build_rule_values(["elonmusk", "@aeyakovenko"]) == [
        "from:elonmusk OR from:aeyakovenko"
    ]


def test_rule_values_respect_length_limit():
    """value у них ограничен 255 символами — длинный список режем."""
    handles = [f"kol{i:02d}" for i in range(40)]
    values = build_rule_values(handles)
    assert len(values) > 1
    assert all(len(v) <= 255 for v in values)
    # ни один аккаунт не потерялся и не продублировался
    joined = " OR ".join(values).split(" OR ")
    assert joined == [f"from:{h}" for h in handles]


def test_checked_raises_on_status_error():
    """Отказы oapi приезжают с HTTP 200 — raise_for_status их не ловит."""
    resp = httpx.Response(
        200,
        json={"status": "error", "msg": "update rule failed"},
        request=httpx.Request("POST", "https://api.twitterapi.io/oapi/tweet_filter/update_rule"),
    )
    try:
        _checked(resp)
    except TwitterApiIoError as exc:
        assert "update rule failed" in str(exc)
    else:
        raise AssertionError("отказ с 200 проехал незамеченным")


def test_checked_passes_success():
    resp = httpx.Response(
        200,
        json={"status": "success", "rule_id": "abc"},
        request=httpx.Request("POST", "https://api.twitterapi.io/oapi/tweet_filter/add_rule"),
    )
    assert _checked(resp)["rule_id"] == "abc"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
                passed += 1
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

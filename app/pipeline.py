from __future__ import annotations

import logging

from app.config import Kol, Settings, load_kols
from app.dedup import Dedup
from app.models import Tweet
from app.notifier import Notifier
from app.parser import parse

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings, dedup: Dedup, notifier: Notifier):
        self._settings = settings
        self._dedup = dedup
        self._notifier = notifier
        self._kols: dict[str, Kol] = load_kols()

    async def handle(self, tweet: Tweet) -> None:
        if await self._dedup.seen_tweet(tweet.id):
            return

        # Ретвиты почти всегда мусор: тот же контракт прилетит из оригинала.
        # Реплаи наоборот оставляем — там часто выкладывают CA под анонсом.
        if tweet.is_retweet:
            return

        result = parse(tweet.text, expanded_urls=tweet.expanded_urls)
        if not result.has_contract:
            log.debug("нет контракта в %s от @%s", tweet.id, tweet.author)
            return

        kol = self._kols.get(tweet.author.lower())

        for token in result.tokens:
            count = await self._dedup.register_token(token.address, tweet.author)
            await self._notifier.send(tweet, token, kol, count)
            log.info(
                "алерт: %s (%s) от @%s, задержка %.1fс, упоминаний %d",
                token.address,
                token.chain,
                tweet.author,
                tweet.latency_seconds,
                count,
            )

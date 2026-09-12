from __future__ import annotations

import logging

from app.analyst import Analyst
from app.config import Kol, Settings, load_kols
from app.dedup import Dedup
from app.models import Tweet
from app.notifier import Notifier
from app.parser import parse

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        dedup: Dedup,
        notifier: Notifier,
        analyst: Analyst | None = None,
    ):
        self._settings = settings
        self._dedup = dedup
        self._notifier = notifier
        self._analyst = analyst
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
            message_id = await self._notifier.send(tweet, token, kol, count)
            log.info(
                "алерт: %s (%s) от @%s, задержка %.1fс, упоминаний %d",
                token.address,
                token.chain,
                tweet.author,
                tweet.latency_seconds,
                count,
            )

            # Второй этап. Идёт после отправки намеренно: разбор занимает
            # секунды, и держать из-за него алерт значило бы разменять
            # ключевую метрику на удобство.
            if message_id is None or self._analyst is None or not self._analyst.enabled:
                continue
            verdict = await self._analyst.judge(tweet, token, count)
            if verdict is None:
                continue
            await self._notifier.attach_verdict(
                message_id, tweet, token, kol, count, verdict
            )
            log.info(
                "вердикт по %s: %s (%.0f%%) — %s",
                token.address,
                verdict.kind,
                verdict.confidence * 100,
                verdict.reason,
            )

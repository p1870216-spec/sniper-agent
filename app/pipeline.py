from __future__ import annotations

import logging

from app.analyst import ACTIONABLE, Analyst
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

            # Разбор идёт ДО отправки: в чат уходят только коллы, значит
            # решение нужно принять раньше, чем слать. Это стоит секунд
            # задержки — сознательный размен точности на скорость.
            verdict = None
            if self._analyst is not None and self._analyst.enabled:
                verdict = await self._analyst.judge(tweet, token, count)

                # Вердикт есть и это не колл — молчим.
                if verdict is not None and verdict.kind not in ACTIONABLE:
                    log.info(
                        "отброшено (%s, %.0f%%): %s от @%s — %s",
                        verdict.kind,
                        verdict.confidence * 100,
                        token.address,
                        tweet.author,
                        verdict.reason,
                    )
                    continue

                # Вердикта нет — модель недоступна. Шлём как есть: молчать
                # из-за её недоступности значит пропустить настоящий колл.
                if verdict is None:
                    log.warning(
                        "разбор недоступен, шлю %s без фильтра", token.address
                    )

            await self._notifier.send(tweet, token, kol, count, verdict)
            log.info(
                "алерт: %s (%s) от @%s, задержка %.1fс, упоминаний %d, вердикт %s",
                token.address,
                token.chain,
                tweet.author,
                tweet.latency_seconds,
                count,
                verdict.kind if verdict else "нет",
            )

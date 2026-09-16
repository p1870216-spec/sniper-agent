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
        self._drop_confidence = settings.analyst_drop_confidence
        self._kols: dict[str, Kol] = load_kols()

    async def handle(self, tweet: Tweet) -> None:
        # Каждый полученный твит — в лог, вместе с исходом. Без этого
        # «twitterapi.io не прислал» и «прислал, но потерялось» неотличимы:
        # так и не удалось установить, что случилось с $PI 16.09.
        log.info(
            "получен твит %s от @%s%s, задержка %.1fс",
            tweet.id,
            tweet.author,
            " (реплай)" if tweet.is_reply else "",
            tweet.latency_seconds,
        )

        if await self._dedup.seen_tweet(tweet.id):
            log.info("твит %s: повторная доставка, пропуск", tweet.id)
            return

        # Ретвиты почти всегда мусор: тот же контракт прилетит из оригинала.
        # Реплаи наоборот оставляем — там часто выкладывают CA под анонсом.
        if tweet.is_retweet:
            log.info("твит %s: ретвит, пропуск", tweet.id)
            return

        result = parse(tweet.text, expanded_urls=tweet.expanded_urls)
        if not result.has_contract:
            log.info("твит %s: адреса контракта в тексте нет", tweet.id)
            return

        kol = self._kols.get(tweet.author.lower())

        for token in result.tokens:
            count = await self._dedup.register_token(token.address, tweet.author)
            author_seq = await self._dedup.author_mention(token.address, tweet.author)

            # Разбор идёт ДО отправки: в чат уходят коллы, значит решение
            # нужно принять раньше, чем слать. Это стоит секунд задержки —
            # сознательный размен точности на скорость.
            verdict = None
            if self._analyst is not None and self._analyst.enabled:
                verdict = await self._analyst.judge(tweet, token, count, author_seq)

                if verdict is not None and verdict.kind not in ACTIONABLE:
                    # Молчим, только если модель уверена. На неуверенных
                    # вердиктах в ночь 15-16.09 потеряны три настоящих колла.
                    if verdict.confidence >= self._drop_confidence:
                        log.info(
                            "отброшено (%s, %.0f%%, упоминание автором №%d): %s от @%s — %s",
                            verdict.kind,
                            verdict.confidence * 100,
                            author_seq,
                            token.address,
                            tweet.author,
                            verdict.reason,
                        )
                        continue
                    log.info(
                        "не колл, но модель не уверена (%s, %.0f%% < %.0f%%) — шлю %s от @%s",
                        verdict.kind,
                        verdict.confidence * 100,
                        self._drop_confidence * 100,
                        token.address,
                        tweet.author,
                    )

                # Вердикта нет — модель недоступна. Шлём как есть: молчать
                # из-за её недоступности значит пропустить настоящий колл.
                if verdict is None:
                    log.warning(
                        "разбор недоступен, шлю %s без фильтра", token.address
                    )

            await self._notifier.send(tweet, token, kol, count, verdict)
            log.info(
                "алерт: %s (%s) от @%s, задержка %.1fс, авторов %d, "
                "упоминание автором №%d, вердикт %s",
                token.address,
                token.chain,
                tweet.author,
                tweet.latency_seconds,
                count,
                author_seq,
                f"{verdict.kind} {verdict.confidence:.0%}" if verdict else "нет",
            )

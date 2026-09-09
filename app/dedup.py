from __future__ import annotations

import redis.asyncio as redis


class Dedup:
    """Защита от повторов.

    Два независимых ключа:
      tweet:<id>    — один и тот же твит может прийти дважды (ретрай вебхука).
      token:<addr>  — один и тот же контракт от разных KOL. Здесь повтор
                      не выбрасываем молча, а отдаём счётчик: 3 упоминания
                      за короткое окно это отдельный, более сильный сигнал.
    """

    def __init__(self, url: str, ttl: int):
        self._redis = redis.from_url(url, decode_responses=True)
        self._ttl = ttl

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def seen_tweet(self, tweet_id: str) -> bool:
        key = f"tweet:{tweet_id}"
        added = await self._redis.set(key, "1", ex=3600, nx=True)
        return added is None

    async def register_token(self, address: str, author: str) -> int:
        """Возвращает, сколько разных авторов упомянули адрес за окно."""
        key = f"token:{address.lower()}"
        async with self._redis.pipeline() as pipe:
            pipe.sadd(key, author.lower())
            pipe.expire(key, self._ttl)
            pipe.scard(key)
            _, _, count = await pipe.execute()
        return int(count)

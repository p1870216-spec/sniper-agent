from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Tweet:
    """Провайдеро-независимое представление твита.

    Всё, что зависит от формата конкретного API, остаётся в app/providers/.
    Дальше по пайплайну ходит только этот объект.
    """

    id: str
    author: str                      # без @
    text: str
    url: str
    created_at: datetime
    expanded_urls: list[str] = field(default_factory=list)
    is_retweet: bool = False
    is_reply: bool = False
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def latency_seconds(self) -> float:
        """Сколько прошло от публикации до момента, когда мы твит увидели.

        Ключевая метрика проекта. Если она уползает за 5-10 секунд —
        стрим отваливается или очередь забита, надо смотреть.
        """
        return (self.received_at - self.created_at).total_seconds()

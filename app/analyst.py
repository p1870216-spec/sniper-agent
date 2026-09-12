"""LLM-разбор твита перед тем, как считать алерт руководством к действию.

Парсер отвечает на вопрос «есть ли в тексте адрес контракта» и отвечает
на него точно. Он не отвечает на вопрос «это призыв покупать сейчас или
трёп про токен, который автор держит третью неделю» — этот вопрос лежит
вне регулярок в принципе.

Замер на реальных данных (2026-09-11): из 13 срабатываний парсера по
списку KOL действующими коллами выглядели 3-4. Остальное — комментарии
о торгующихся токенах, шутки и шилл-спам.

Модуль намеренно НЕ блокирует отправку. Пайплайн шлёт алерт сразу, а
вердикт дописывает в уже отправленное сообщение через пару секунд:
ключевая метрика latency остаётся нетронутой, а суждение приходит
вовремя — за две секунды сделку всё равно не совершить.

Без ANTHROPIC_API_KEY модуль выключен, пайплайн работает как раньше.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.models import Tweet
from app.parser import TokenMention

log = logging.getLogger(__name__)

VERDICT_LOG = Path("data/verdicts.jsonl")

Kind = Literal["call", "discussion", "joke", "warning", "spam", "unclear"]

# Метки те же, что в наборе для разметки (tools/collect_dataset.py),
# чтобы вердикты модели и ручная разметка были сравнимы напрямую.
KIND_MARK: dict[str, str] = {
    "call": "🎯 колл",
    "discussion": "💬 обсуждение",
    "joke": "😄 шутка",
    "warning": "⚠️ предупреждение",
    "spam": "🗑 спам",
    "unclear": "❓ неясно",
}

# Действовать имеет смысл только по первому; остальное — контекст.
ACTIONABLE: frozenset[str] = frozenset({"call"})


class Verdict(BaseModel):
    """Структурированный ответ модели."""

    kind: Kind = Field(description="категория твита")
    confidence: float = Field(ge=0.0, le=1.0, description="уверенность 0..1")
    reason: str = Field(description="одно короткое предложение по-русски, почему так")


SYSTEM = """Ты фильтр крипто-снайпера. На вход — твит инфлюенсера, в котором
автоматический парсер нашёл адрес контракта. Твоя задача — определить, чем
этот твит является на самом деле.

Категории:
- call — свежий призыв покупать конкретный токен прямо сейчас. Адрес подан
  как руководство к действию: "CA: ...", "aped in", новый запуск.
- discussion — разговор о токене, который уже торгуется: динамика цены,
  капитализация, обновления проекта, сопровождение своей позиции. Адрес
  приведён для справки, а не как призыв входить.
- joke — шутка, мем, риторическая фигура, где адрес попал в текст мимоходом.
- warning — предупреждение о скаме, руге, ханипоте.
- spam — накрутка: повторяющийся текст, гирлянда тикеров и хештегов.
- unclear — по тексту определить невозможно.

Важно: сам факт наличия адреса ничего не решает. Крипто-инфлюенсеры постят
адрес и когда зовут покупать, и когда хвастаются иксами по старой позиции.
Различай по тому, обращён ли текст к читателю как призыв, или описывает то,
что уже произошло.

Отвечай только структурой. reason — одно короткое предложение по-русски."""

# Примеры настоящие, из data/labeling_set.csv — не выдуманные.
EXAMPLES: list[tuple[str, str]] = [
    (
        "$PONCAT  CA:  0x886d84051b933a34fa92692461615bede617f57a  Fomo link: ...",
        '{"kind":"call","confidence":0.95,'
        '"reason":"Тикер и адрес поданы как прямой призыв входить."}',
    ),
    (
        "$Ember second leg pumped from 2.2M to 68M mcap. 3rd leg is gonna melt faces.",
        '{"kind":"discussion","confidence":0.9,'
        '"reason":"Описывает уже случившийся рост по своей позиции, а не вход."}',
    ),
    (
        "If I was the president of the United States, I would force the other "
        "countries to hold a minimum of $500m in solana:6p6xgHyF... coin",
        '{"kind":"joke","confidence":0.92,'
        '"reason":"Риторическая фигура про политику, адрес попал мимоходом."}',
    ),
    (
        "This is a $Coin  This is a $Coin  This is a $Coin  Ca: 2SAJiAL5... #Coin #Coin",
        '{"kind":"spam","confidence":0.94,'
        '"reason":"Повторяющийся текст и гирлянда хештегов — накрутка."}',
    ),
]


def _render_request(tweet: Tweet, token: TokenMention, mention_count: int) -> str:
    seen = (
        f"Адрес уже упоминали {mention_count} разных авторов за последние часы."
        if mention_count > 1
        else "Адрес встречается впервые за окно дедупликации."
    )
    return (
        f"Автор: @{tweet.author}\n"
        f"Сеть: {token.chain}, адрес найден в: {token.source}\n"
        f"{seen}\n"
        f"Это реплай: {'да' if tweet.is_reply else 'нет'}\n\n"
        f"Текст твита:\n{tweet.text}"
    )


class Analyst:
    """Обёртка над Claude. Выключается отсутствием ключа, а не флагом."""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        timeout_seconds: float = 20.0,
    ):
        self._model = model
        self._timeout = timeout_seconds
        self._client = None
        if not api_key:
            log.info("ANTHROPIC_API_KEY не задан — разбор твитов выключен")
            return
        try:
            import anthropic
        except ImportError:
            log.error("пакет anthropic не установлен — разбор выключен")
            return
        # Сеть на этой машине рвётся: первый же боевой прогон дал
        # Connection error на одном твите из двух. Разбор не срочный —
        # лучше потратить лишние секунды на повтор, чем потерять вердикт.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=4)
        log.info("разбор твитов включён, модель %s", model)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def judge(
        self, tweet: Tweet, token: TokenMention, mention_count: int
    ) -> Verdict | None:
        """Вердикт по твиту. None — если разбор недоступен или упал.

        Исключения наружу не выпускает: алерт уже отправлен, и падение
        разбора не должно превращаться в потерю сообщения.
        """
        if self._client is None:
            return None

        messages: list[dict] = []
        for text, answer in EXAMPLES:
            messages.append({"role": "user", "content": text})
            messages.append({"role": "assistant", "content": answer})
        messages.append(
            {"role": "user", "content": _render_request(tweet, token, mention_count)}
        )

        try:
            response = await self._client.with_options(
                timeout=self._timeout
            ).messages.parse(
                model=self._model,
                max_tokens=4000,
                system=SYSTEM,
                messages=messages,
                output_format=Verdict,
            )
            verdict = response.parsed_output
        except Exception as exc:  # noqa: BLE001 — разбор не должен ронять пайплайн
            log.warning("разбор твита %s не удался: %s", tweet.id, exc)
            return None

        if verdict is None:
            log.warning("разбор твита %s вернул пустой результат", tweet.id)
            return None

        self._record(tweet, token, mention_count, verdict)
        return verdict

    def _record(
        self,
        tweet: Tweet,
        token: TokenMention,
        mention_count: int,
        verdict: Verdict,
    ) -> None:
        """Пишет вердикт в jsonl.

        Нужно не для отладки: это та же схема, что в наборе для ручной
        разметки, так что боевые вердикты копятся как размеченные данные
        и позже сравниваются с ручными метками.
        """
        try:
            VERDICT_LOG.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "label": verdict.kind,
                "note": verdict.reason,
                "author": tweet.author,
                "created_at": tweet.created_at.isoformat(),
                "address": token.address,
                "chain": token.chain,
                "source": token.source,
                "confidence": f"{verdict.confidence:.2f}",
                "is_reply": int(tweet.is_reply),
                "text": tweet.text.replace("\n", " ").strip(),
                "url": tweet.url,
                "tweet_id": tweet.id,
                "mention_count": mention_count,
                "model": self._model,
            }
            with VERDICT_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("не записал вердикт: %s", exc)

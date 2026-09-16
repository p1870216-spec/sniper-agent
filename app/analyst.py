"""LLM-разбор твита: решает, уходит ли алерт в чат.

Парсер отвечает на вопрос «есть ли в тексте адрес контракта» и отвечает
на него точно. Он не отвечает на вопрос «это призыв покупать сейчас или
трёп про токен, который автор держит третью неделю» — этот вопрос лежит
вне регулярок в принципе.

Разбор идёт до отправки: в чат уходят коллы. Не-колл отбрасывается, только
если модель в нём уверена (порог analyst_drop_confidence в настройках),
иначе алерт уходит с пометкой вердикта. Ночь 15→16.09 показала цену
строгости: три настоящих колла — paid (+14450%), xl, doom — были отброшены
как «обсуждение» при уверенности 60-78%.

Без ANTHROPIC_API_KEY модуль выключен, пайплайн шлёт всё найденное.
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
    translation: str = Field(description="перевод текста твита на русский")


SYSTEM = """Ты фильтр крипто-снайпера. На вход — твит инфлюенсера, в котором
автоматический парсер нашёл адрес контракта. Реши, чем этот твит является.

Аккаунты в списке — колл-каналы: подписчики покупают по их постам. Поэтому
колл у них редко выглядит как прямое «покупай». Чаще это тикер, адрес и один
довод: что механика проекта работает, сколько ещё может вырасти, что «может
поехать».

Категории:
- call — автор даёт токен как возможность для входа. Главный признак —
  ПЕРВОЕ упоминание этого адреса автором (в запросе есть строка, впервые ли
  он его упоминает). Первое упоминание с тикером и адресом считай коллом,
  даже если оно подано как наблюдение, тезис или сравнение с другим токеном.
  Повторное упоминание тоже бывает коллом, если текст прямо зовёт входить.
- discussion — сопровождение токена, который автор УЖЕ давал: отчёт о росте
  позиции, иксы, апдейты проекта. Типичный признак — повторное упоминание.
- joke — шутка, мем, риторическая фигура, где адрес попал в текст мимоходом.
- warning — предупреждение о скаме, руге, ханипоте.
- spam — накрутка: повторяющийся текст, гирлянда тикеров и хештегов.
- unclear — адрес не про токен (например, кошелёк для перевода) или по
  тексту определить невозможно.

Сомневаешься между call и discussion при первом упоминании — выбирай call:
пропущенный колл обходится дороже лишнего сообщения.

confidence — уверенность в категории от 0 до 1. Не завышай её: при
неоднозначном тексте она должна быть низкой. Неуверенные вердикты бот не
отбрасывает, а отправляет — так что честная неуверенность защищает от
пропуска.

Отвечай только структурой.
reason — одно короткое предложение по-русски.
translation — перевод текста твита на русский. Тикеры ($PONCAT), адреса
контрактов и ссылки оставляй как есть, не транслитерируй. Жаргон переводи
по смыслу, а не буквально: ape in — "заходить", rug — "скам", mcap —
"капитализация". Если твит уже по-русски, верни его без изменений."""


def _render(
    *,
    author: str,
    chain: str,
    source: str,
    mention_count: int,
    author_seq: int,
    is_reply: bool,
    text: str,
) -> str:
    seq = (
        "Автор упоминает этот адрес впервые за последние 7 дней."
        if author_seq <= 1
        else f"Автор упоминает этот адрес уже {author_seq}-й раз за последние 7 дней."
    )
    authors = (
        f"Адрес уже упоминали {mention_count} разных авторов за последние часы."
        if mention_count > 1
        else "Другие авторы этот адрес за последние часы не упоминали."
    )
    return (
        f"Автор: @{author}\n"
        f"Сеть: {chain}, адрес найден в: {source}\n"
        f"{seq}\n"
        f"{authors}\n"
        f"Это реплай: {'да' if is_reply else 'нет'}\n\n"
        f"Текст твита:\n{text}"
    )


def _render_request(
    tweet: Tweet, token: TokenMention, mention_count: int, author_seq: int
) -> str:
    return _render(
        author=tweet.author,
        chain=token.chain,
        source=token.source,
        mention_count=mention_count,
        author_seq=author_seq,
        is_reply=tweet.is_reply,
        text=tweet.text,
    )


def _example(text: str, *, author: str, chain: str, author_seq: int, **answer) -> tuple[str, str]:
    request = _render(
        author=author, chain=chain, source="text",
        mention_count=1, author_seq=author_seq, is_reply=False, text=text,
    )
    return request, json.dumps(answer, ensure_ascii=False)


# Все примеры настоящие. paid и xl — ровно те твиты, которые прежний промпт
# отбросил как «обсуждение» в ночь 15→16.09; здесь они размечены как коллы.
EXAMPLES: list[tuple[str, str]] = [
    _example(
        "$PONCAT  CA:  0x886d84051b933a34fa92692461615bede617f57a  Fomo link: ...",
        author="zenkaixbt", chain="evm", author_seq=1,
        kind="call", confidence=0.95,
        reason="Тикер и адрес поданы как прямой призыв входить.",
        translation="$PONCAT  Контракт: 0x886d84051b933a34fa92692461615bede617f57a  Ссылка на фомо: ...",
    ),
    _example(
        "$PAID  It’s really working, i just checked a launch on PF where they "
        "redirected the fees to an X user.   98kfF7rmsg1QDUEoCqNE7g7M1FdrTt92TEp2CLzypump",
        author="dr_crypto_calls", chain="solana", author_seq=1,
        kind="call", confidence=0.85,
        reason="Первое упоминание нового токена с адресом и доводом, что механика работает, — наводка на вход.",
        translation="$PAID  Это действительно работает, я только что проверил запуск на PF, где комиссии "
                    "перенаправили пользователю X.   98kfF7rmsg1QDUEoCqNE7g7M1FdrTt92TEp2CLzypump",
    ),
    _example(
        "$XL still x10 to go to catch $PAID. Older + different chain.  "
        "0x1cDb289BeFDFaC8aF945a288BCdcCc382cB34d32",
        author="dr_crypto_calls", chain="evm", author_seq=1,
        kind="call", confidence=0.85,
        reason="Впервые даёт токен с адресом и тезисом о потенциале роста — колл, хотя подан как сравнение.",
        translation="$XL ещё x10 до уровня $PAID. Старше и на другой сети.  "
                    "0x1cDb289BeFDFaC8aF945a288BCdcCc382cB34d32",
    ),
    _example(
        "$Scribe  170k -----------&gt; 2.8M mcap   16x in an hour.  Lfgooo  First mover tech.  "
        "6rHkNb7HCtkpvdnVJsBCZHH5dw3AndqEjfmbEGhooR7t",
        author="DegenCapitalLLC", chain="solana", author_seq=5,
        kind="discussion", confidence=0.85,
        reason="Пятое упоминание того же токена: автор отчитывается о росте позиции, а не даёт новый вход.",
        translation="$Scribe  170k -----------> 2,8 млн капитализации   16x за час.  Поехали  "
                    "Технология первопроходца.  6rHkNb7HCtkpvdnVJsBCZHH5dw3AndqEjfmbEGhooR7t",
    ),
    _example(
        "If I was the president of the United States, I would force the other countries "
        "to hold a minimum of $500m in solana:6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN coin",
        author="crypto_bitlord7", chain="solana", author_seq=1,
        kind="joke", confidence=0.92,
        reason="Риторическая фигура про политику, адрес попал мимоходом.",
        translation="Будь я президентом США, я бы заставил другие страны держать минимум "
                    "500 млн долларов в монете solana:6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",
    ),
    _example(
        "This is a $Coin  This is a $Coin  This is a $Coin  "
        "Ca: 2SAJiAL5FSTJ42bRivHJEYnhY7oS27ZQrJDgetDEpump #Coin #Coin",
        author="Jrem_Verse", chain="solana", author_seq=1,
        kind="spam", confidence=0.94,
        reason="Повторяющийся текст и гирлянда хештегов — накрутка.",
        translation="Это $Coin  Это $Coin  Это $Coin  "
                    "Контракт: 2SAJiAL5FSTJ42bRivHJEYnhY7oS27ZQrJDgetDEpump #Coin #Coin",
    ),
]


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
        # Connection error на одном твите из двух. Лучше потратить лишние
        # секунды на повтор, чем отправить алерт без разбора.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=4)
        log.info("разбор твитов включён, модель %s", model)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def judge(
        self,
        tweet: Tweet,
        token: TokenMention,
        mention_count: int,
        author_seq: int = 1,
    ) -> Verdict | None:
        """Вердикт по твиту. None — если разбор недоступен или упал.

        Исключения наружу не выпускает: падение разбора не должно
        превращаться в потерю алерта — пайплайн тогда шлёт без фильтра.
        """
        if self._client is None:
            return None

        messages: list[dict] = []
        for request, answer in EXAMPLES:
            messages.append({"role": "user", "content": request})
            messages.append({"role": "assistant", "content": answer})
        messages.append(
            {
                "role": "user",
                "content": _render_request(tweet, token, mention_count, author_seq),
            }
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

        self._record(tweet, token, mention_count, author_seq, verdict)
        return verdict

    def _record(
        self,
        tweet: Tweet,
        token: TokenMention,
        mention_count: int,
        author_seq: int,
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
                "translation": verdict.translation,
                "text": tweet.text.replace("\n", " ").strip(),
                "url": tweet.url,
                "tweet_id": tweet.id,
                "mention_count": mention_count,
                "author_seq": author_seq,
                "model": self._model,
            }
            with VERDICT_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("не записал вердикт: %s", exc)

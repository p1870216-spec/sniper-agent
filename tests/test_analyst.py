"""Тесты фильтра «только коллы».

Разбор идёт до отправки и решает, слать ли вообще. Два свойства, которые
здесь закреплены и которые легко сломать правкой:

  * не-колл в чат не уходит;
  * недоступность модели НЕ приводит к молчанию — иначе её падение
    означало бы пропущенный настоящий колл.

Сеть не трогается: модель подменена фейком.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyst import Analyst, Verdict
from app.config import Kol
from app.models import Tweet
from app.notifier import SHARPE_RUG_CHECK, _keyboard, render
from app.parser import TokenMention
from app.pipeline import Pipeline

ADDR = "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"
EVM_ADDR = "0x886d84051b933a34fa92692461615bede617f57a"


def make_tweet(text: str = f"CA: {ADDR}") -> Tweet:
    now = datetime.now(timezone.utc)
    return Tweet(
        id="1",
        author="degencapitalllc",
        text=text,
        url="https://x.com/degencapitalllc/status/1",
        created_at=now - timedelta(seconds=1),
        received_at=now,
    )


TOKEN = TokenMention(address=ADDR, chain="solana", source="text", confidence=0.8)
EVM_TOKEN = TokenMention(address=EVM_ADDR, chain="evm", source="text", confidence=0.85)

CALL = Verdict(
    kind="call", confidence=0.95, reason="Прямой призыв входить.",
    translation="$PONCAT Контракт: 0x886d... не пропустите вход",
)
JOKE = Verdict(
    kind="joke", confidence=0.9, reason="Риторическая фигура.",
    translation="Будь я президентом США, я бы заставил другие страны держать монету",
)


class FakeDedup:
    async def seen_tweet(self, tweet_id):
        return False

    async def register_token(self, address, author):
        return 1


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, tweet, token, kol, mention_count, verdict=None):
        self.sent.append((token.address, verdict.kind if verdict else None))
        return 42


class FakeAnalyst:
    def __init__(self, verdict=CALL, enabled=True):
        self._verdict = verdict
        self._enabled = enabled
        self.calls = 0

    @property
    def enabled(self):
        return self._enabled

    async def judge(self, tweet, token, mention_count):
        self.calls += 1
        return self._verdict


class Settings:
    pass


def run(notifier, analyst):
    pipe = Pipeline(Settings(), FakeDedup(), notifier, analyst)
    asyncio.run(pipe.handle(make_tweet()))


def test_call_is_sent():
    notifier = FakeNotifier()
    run(notifier, FakeAnalyst(CALL))
    assert notifier.sent == [(ADDR, "call")]


def test_joke_is_dropped():
    """Главное новое свойство: не-колл в чат не уходит."""
    notifier = FakeNotifier()
    run(notifier, FakeAnalyst(JOKE))
    assert notifier.sent == []


def test_every_non_call_kind_is_dropped():
    for kind in ("discussion", "joke", "warning", "spam", "unclear"):
        notifier = FakeNotifier()
        v = Verdict(kind=kind, confidence=0.8, reason="повод", translation="перевод")
        run(notifier, FakeAnalyst(v))
        assert notifier.sent == [], f"{kind} не должен отправляться"


def test_analysis_failure_still_sends():
    """Модель недоступна — шлём без фильтра, а не молчим."""
    notifier = FakeNotifier()
    run(notifier, FakeAnalyst(verdict=None))
    assert notifier.sent == [(ADDR, None)]


def test_disabled_analyst_sends_everything():
    notifier, analyst = FakeNotifier(), FakeAnalyst(enabled=False)
    run(notifier, analyst)
    assert analyst.calls == 0
    assert notifier.sent == [(ADDR, None)]


def test_no_analyst_at_all():
    notifier = FakeNotifier()
    run(notifier, None)
    assert notifier.sent == [(ADDR, None)]


def test_analyst_disabled_without_key():
    a = Analyst(None, "claude-opus-5")
    assert not a.enabled
    assert asyncio.run(a.judge(make_tweet(), TOKEN, 1)) is None


def test_render_shows_translation_not_original():
    tweet = make_tweet("aped in hard, this one runs")
    text = render(tweet, TOKEN, Kol(handle="x", tier=1), 1, CALL)
    assert "не пропустите вход" in text
    assert "aped in hard" not in text


def test_render_without_verdict_falls_back_to_original():
    tweet = make_tweet("aped in hard")
    text = render(tweet, TOKEN, Kol(handle="x", tier=1), 1)
    assert "aped in hard" in text
    assert "без разбора" in text


def test_render_escapes_model_output():
    """reason и translation приходят от модели — в HTML их пускать нельзя."""
    nasty = Verdict(
        kind="call", confidence=0.5,
        reason="<b>жирный</b>", translation="<script>alert(1)</script> и хвост",
    )
    text = render(make_tweet(), TOKEN, None, 1, nasty)
    assert "<b>жирный</b>" not in text
    assert "<script>" not in text
    assert "&lt;" in text


def test_sharpe_button_on_both_chains():
    for token in (TOKEN, EVM_TOKEN):
        urls = [
            b.url
            for row in _keyboard(token, make_tweet()).inline_keyboard
            for b in row
        ]
        assert SHARPE_RUG_CHECK in urls, f"нет кнопки Sharpe для {token.chain}"


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

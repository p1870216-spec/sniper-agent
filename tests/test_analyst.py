"""Тесты двухэтапной отправки: алерт сразу, вердикт — вдогонку.

Проверяется поведение вокруг модели, а не сама модель: сеть в тестах не
трогается. Главное свойство, которое здесь закреплено, — алерт уходит
независимо от того, что случилось с разбором.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyst import Analyst, Verdict
from app.config import Kol
from app.models import Tweet
from app.notifier import render
from app.parser import TokenMention
from app.pipeline import Pipeline

ADDR = "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"


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
VERDICT = Verdict(kind="joke", confidence=0.91, reason="Риторическая фигура.")


class FakeDedup:
    def __init__(self):
        self.count = 1

    async def seen_tweet(self, tweet_id):
        return False

    async def register_token(self, address, author):
        return self.count


class FakeNotifier:
    def __init__(self, message_id=42):
        self.message_id = message_id
        self.sent = []
        self.attached = []

    async def send(self, tweet, token, kol, mention_count):
        self.sent.append(token.address)
        return self.message_id

    async def attach_verdict(self, message_id, tweet, token, kol, mention_count, verdict):
        self.attached.append((message_id, verdict.kind))


class FakeAnalyst:
    def __init__(self, verdict=VERDICT, enabled=True):
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


def run_pipeline(notifier, analyst, text=f"CA: {ADDR}"):
    pipe = Pipeline(Settings(), FakeDedup(), notifier, analyst)
    asyncio.run(pipe.handle(make_tweet(text)))
    return pipe


def test_analyst_disabled_without_key():
    """Нет ключа — модуль молча выключен, а не падает."""
    a = Analyst(None, "claude-opus-5")
    assert not a.enabled
    assert asyncio.run(a.judge(make_tweet(), TOKEN, 1)) is None


def test_alert_sent_then_verdict_attached():
    notifier, analyst = FakeNotifier(), FakeAnalyst()
    run_pipeline(notifier, analyst)
    assert notifier.sent == [ADDR]
    assert notifier.attached == [(42, "joke")]


def test_alert_survives_failed_analysis():
    """Разбор вернул None — алерт всё равно отправлен, дописывания нет."""
    notifier, analyst = FakeNotifier(), FakeAnalyst(verdict=None)
    run_pipeline(notifier, analyst)
    assert notifier.sent == [ADDR]
    assert notifier.attached == []


def test_no_analyst_at_all():
    """Пайплайн без разбора работает как раньше."""
    notifier = FakeNotifier()
    run_pipeline(notifier, None)
    assert notifier.sent == [ADDR]
    assert notifier.attached == []


def test_disabled_analyst_not_called():
    notifier, analyst = FakeNotifier(), FakeAnalyst(enabled=False)
    run_pipeline(notifier, analyst)
    assert analyst.calls == 0
    assert notifier.attached == []


def test_no_verdict_when_send_failed():
    """Сообщения нет — дописывать некуда, модель зря не дёргаем."""
    notifier, analyst = FakeNotifier(message_id=None), FakeAnalyst()
    run_pipeline(notifier, analyst)
    assert analyst.calls == 0
    assert notifier.attached == []


def test_render_without_verdict_has_no_verdict_block():
    text = render(make_tweet(), TOKEN, Kol(handle="x", tier=1), 1)
    assert "уверенность" not in text
    assert ADDR in text


def test_render_with_verdict_shows_kind_and_reason():
    text = render(make_tweet(), TOKEN, Kol(handle="x", tier=1), 1, VERDICT)
    assert "шутка" in text
    assert "Риторическая фигура." in text
    assert "91%" in text


def test_render_escapes_verdict_reason():
    """reason приходит от модели — в HTML-разметку его пускать нельзя."""
    nasty = Verdict(kind="call", confidence=0.5, reason="<b>жирный</b> & хвост")
    text = render(make_tweet(), TOKEN, None, 1, nasty)
    assert "<b>жирный</b>" not in text
    assert "&lt;b&gt;" in text


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

"""Тесты фильтра коллов.

Разбор идёт до отправки и решает, слать ли. Свойства, которые здесь
закреплены и которые легко сломать правкой:

  * уверенный не-колл в чат не уходит;
  * НЕуверенный не-колл уходит — ночь 15-16.09 показала, что именно там
    прячутся настоящие коллы (paid, xl, doom отброшены при 60-78%);
  * недоступность модели НЕ приводит к молчанию;
  * модели передаётся, в который раз автор упоминает адрес.

Сеть не трогается: модель и Redis подменены фейками.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyst import EXAMPLES, Analyst, Verdict, _render_request
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


def verdict(kind: str, confidence: float) -> Verdict:
    return Verdict(kind=kind, confidence=confidence, reason="повод", translation="перевод")


CALL = Verdict(
    kind="call", confidence=0.95, reason="Прямой призыв входить.",
    translation="$PONCAT Контракт: 0x886d... не пропустите вход",
)


class FakeDedup:
    def __init__(self, author_seq: int = 1):
        self.author_seq = author_seq

    async def seen_tweet(self, tweet_id):
        return False

    async def register_token(self, address, author):
        return 1

    async def author_mention(self, address, author):
        return self.author_seq


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
        self.author_seq = None

    @property
    def enabled(self):
        return self._enabled

    async def judge(self, tweet, token, mention_count, author_seq=1):
        self.calls += 1
        self.author_seq = author_seq
        return self._verdict


class Settings:
    analyst_drop_confidence = 0.8


def run(notifier, analyst, dedup=None):
    pipe = Pipeline(Settings(), dedup or FakeDedup(), notifier, analyst)
    asyncio.run(pipe.handle(make_tweet()))


def test_call_is_sent():
    notifier = FakeNotifier()
    run(notifier, FakeAnalyst(CALL))
    assert notifier.sent == [(ADDR, "call")]


def test_confident_non_call_is_dropped():
    for kind in ("discussion", "joke", "warning", "spam", "unclear"):
        notifier = FakeNotifier()
        run(notifier, FakeAnalyst(verdict(kind, 0.9)))
        assert notifier.sent == [], f"уверенный {kind} не должен отправляться"


def test_uncertain_non_call_is_sent():
    """Случай paid: discussion при 60% — это слать, а не молчать."""
    notifier = FakeNotifier()
    run(notifier, FakeAnalyst(verdict("discussion", 0.6)))
    assert notifier.sent == [(ADDR, "discussion")]


def test_threshold_boundary():
    below, at = FakeNotifier(), FakeNotifier()
    run(below, FakeAnalyst(verdict("discussion", 0.79)))
    run(at, FakeAnalyst(verdict("discussion", 0.80)))
    assert below.sent == [(ADDR, "discussion")], "ниже порога — отправка"
    assert at.sent == [], "на пороге — уже уверенность, отбрасываем"


def test_analysis_failure_still_sends():
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


def test_author_seq_reaches_analyst():
    analyst = FakeAnalyst(CALL)
    run(FakeNotifier(), analyst, FakeDedup(author_seq=5))
    assert analyst.author_seq == 5


def test_request_tells_first_and_repeat_mention():
    first = _render_request(make_tweet(), TOKEN, 1, 1)
    fifth = _render_request(make_tweet(), TOKEN, 1, 5)
    assert "впервые" in first
    assert "5-й раз" in fifth


def test_examples_keep_missed_calls_as_calls():
    """paid и xl из ночи 15-16.09 должны остаться в примерах коллами."""
    calls = [
        request for request, answer in EXAMPLES
        if json.loads(answer)["kind"] == "call"
    ]
    assert any("$PAID" in r for r in calls)
    assert any("$XL" in r for r in calls)


def test_examples_are_valid_verdicts():
    for _, answer in EXAMPLES:
        Verdict(**json.loads(answer))


def test_analyst_disabled_without_key():
    a = Analyst(None, "claude-opus-5")
    assert not a.enabled
    assert asyncio.run(a.judge(make_tweet(), TOKEN, 1, 1)) is None


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

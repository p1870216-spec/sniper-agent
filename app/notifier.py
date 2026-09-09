from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Kol
from app.models import Tweet
from app.parser import TokenMention

log = logging.getLogger(__name__)

TIER_MARK = {1: "🔴", 2: "🟡", 3: "⚪"}


def _keyboard(token: TokenMention, tweet: Tweet) -> InlineKeyboardMarkup:
    addr = token.address
    if token.chain == "solana":
        rows = [
            [
                InlineKeyboardButton(text="DexScreener", url=f"https://dexscreener.com/solana/{addr}"),
                InlineKeyboardButton(text="RugCheck", url=f"https://rugcheck.xyz/tokens/{addr}"),
            ],
            [
                InlineKeyboardButton(text="pump.fun", url=f"https://pump.fun/coin/{addr}"),
                InlineKeyboardButton(text="Твит", url=tweet.url),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="DexScreener", url=f"https://dexscreener.com/search?q={addr}"),
                InlineKeyboardButton(text="Honeypot", url=f"https://honeypot.is/?address={addr}"),
            ],
            [InlineKeyboardButton(text="Твит", url=tweet.url)],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render(tweet: Tweet, token: TokenMention, kol: Kol | None, mention_count: int) -> str:
    mark = TIER_MARK.get(kol.tier if kol else 3, "⚪")
    tier_text = f"tier {kol.tier}" if kol else "не в списке"

    lines = [
        f"{mark} <b>@{html.escape(tweet.author)}</b> · {tier_text}",
        "",
        f"<code>{html.escape(token.address)}</code>",
        f"сеть: {token.chain} · источник: {token.source} · доверие: {token.confidence:.2f}",
    ]

    if mention_count > 1:
        lines.append(f"🔥 <b>упомянут {mention_count}-й раз</b> разными авторами")

    snippet = tweet.text.strip()
    if len(snippet) > 280:
        snippet = snippet[:277] + "..."
    lines += ["", f"<i>{html.escape(snippet)}</i>", "", f"задержка: {tweet.latency_seconds:.1f}с"]

    return "\n".join(lines)


class Notifier:
    def __init__(self, token: str, chat_id: str):
        self._bot = Bot(token=token)
        self._chat_id = chat_id

    async def aclose(self) -> None:
        await self._bot.session.close()

    async def send(
        self,
        tweet: Tweet,
        token: TokenMention,
        kol: Kol | None,
        mention_count: int,
    ) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=render(tweet, token, kol, mention_count),
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(token, tweet),
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 — алерт не должен ронять пайплайн
            log.exception("не отправил алерт: %s", exc)

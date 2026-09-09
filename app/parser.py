"""Извлечение адресов токенов из текста твита.

Главная сложность — Solana. Адрес там это просто base58-строка 32-44 символа,
под этот шаблон попадает половина случайных слов и хешей. Поэтому:
  1) сначала выдираем адреса из ссылок (pump.fun, dexscreener, birdeye...) —
     это самый надёжный источник, ложных срабатываний почти нет;
  2) потом ищем "голые" адреса в тексте и валидируем их через base58-декод:
     настоящий Solana-адрес декодируется ровно в 32 байта;
  3) отсекаем денилист системных адресов (WSOL, USDC, программы Raydium и т.д.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- base58 без внешних зависимостей ---------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(s: str) -> bytes | None:
    """Декодирует base58. None, если строка содержит недопустимые символы."""
    num = 0
    for ch in s:
        val = _B58_INDEX.get(ch)
        if val is None:
            return None
        num = num * 58 + val

    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break

    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * pad + body


def is_solana_address(s: str) -> bool:
    if not 32 <= len(s) <= 44:
        return False
    decoded = b58decode(s)
    return decoded is not None and len(decoded) == 32


# --- регулярки --------------------------------------------------------------

EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
BASE58_CANDIDATE_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9_]{1,14})\b")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Ссылки, из которых можно вытащить адрес напрямую.
# Порядок важен: более специфичные паттерны идут первыми.
URL_EXTRACTORS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"pump\.fun/(?:coin|board)/([1-9A-HJ-NP-Za-km-z]{32,44})", re.I), "solana"),
    (re.compile(r"birdeye\.so/token/([1-9A-HJ-NP-Za-km-z]{32,44})", re.I), "solana"),
    (re.compile(r"solscan\.io/token/([1-9A-HJ-NP-Za-km-z]{32,44})", re.I), "solana"),
    (re.compile(r"jup\.ag/(?:swap|tokens)/(?:[A-Za-z0-9]+-)?([1-9A-HJ-NP-Za-km-z]{32,44})", re.I), "solana"),
    (re.compile(r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})", re.I), "solana"),
    (re.compile(r"dexscreener\.com/(?:ethereum|base|bsc|arbitrum|polygon)/(0x[a-fA-F0-9]{40})", re.I), "evm"),
    (re.compile(r"dextools\.io/app/[a-z-]+/[a-z]+/pair-explorer/(0x[a-fA-F0-9]{40})", re.I), "evm"),
    (re.compile(r"(?:etherscan\.io|basescan\.org|bscscan\.com)/(?:token|address)/(0x[a-fA-F0-9]{40})", re.I), "evm"),
]

# Системные / общеизвестные адреса — не токен-запуски.
DENYLIST: set[str] = {
    # Solana
    "So11111111111111111111111111111111111111112",   # Wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token Program
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun program
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter v6
    # EVM
    "0x0000000000000000000000000000000000000000",
    "0xdead000000000000000042069420694206942069",
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",     # WETH
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",     # USDT
}
_DENY_LOWER = {a.lower() for a in DENYLIST}

# Слова-паразиты, которые проходят проверку длины, но не base58-декод —
# на всякий случай держим явный список частых ложных срабатываний.
KNOWN_FALSE_POSITIVES = {"pumpfun", "dexscreener"}


@dataclass
class TokenMention:
    address: str
    chain: str          # "solana" | "evm"
    source: str         # "url" | "text"
    confidence: float   # 0.0 - 1.0


@dataclass
class ParseResult:
    tokens: list[TokenMention] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    @property
    def has_contract(self) -> bool:
        return bool(self.tokens)

    @property
    def primary(self) -> TokenMention | None:
        if not self.tokens:
            return None
        return max(self.tokens, key=lambda t: t.confidence)


def _is_denied(addr: str) -> bool:
    return addr.lower() in _DENY_LOWER


def parse(text: str, expanded_urls: list[str] | None = None) -> ParseResult:
    """Разбирает текст твита.

    expanded_urls — раскрытые ссылки из entities твита. Твиттер заворачивает
    все ссылки в t.co, поэтому без них адрес из ссылки не достать. Провайдер
    обычно отдаёт их в поле entities.urls[].expanded_url.
    """
    result = ParseResult()
    seen: set[str] = set()

    urls = list(expanded_urls or [])
    urls.extend(URL_RE.findall(text))
    # dedup с сохранением порядка
    result.urls = list(dict.fromkeys(urls))

    haystack_urls = " ".join(result.urls)

    # 1. Адреса из ссылок — максимальное доверие.
    for pattern, chain in URL_EXTRACTORS:
        for match in pattern.findall(haystack_urls):
            addr = match
            if addr.lower() in seen or _is_denied(addr):
                continue
            if chain == "solana" and not is_solana_address(addr):
                continue
            seen.add(addr.lower())
            result.tokens.append(
                TokenMention(address=addr, chain=chain, source="url", confidence=0.95)
            )

    # 2. EVM-адреса в тексте — шаблон жёсткий, доверие высокое.
    for addr in EVM_RE.findall(text):
        if addr.lower() in seen or _is_denied(addr):
            continue
        seen.add(addr.lower())
        result.tokens.append(
            TokenMention(address=addr, chain="evm", source="text", confidence=0.85)
        )

    # 3. Голые Solana-адреса — только после валидации base58.
    text_without_urls = URL_RE.sub(" ", text)
    for candidate in BASE58_CANDIDATE_RE.findall(text_without_urls):
        if candidate.lower() in seen or _is_denied(candidate):
            continue
        if candidate.lower() in KNOWN_FALSE_POSITIVES:
            continue
        if not is_solana_address(candidate):
            continue
        seen.add(candidate.lower())
        result.tokens.append(
            TokenMention(address=candidate, chain="solana", source="text", confidence=0.8)
        )

    # 4. Тикеры — сами по себе не сигнал, но полезны для заголовка алерта
    #    и для поиска по DexScreener, если контракта в твите нет.
    for ticker in TICKER_RE.findall(text):
        upper = ticker.upper()
        if upper not in result.tickers:
            result.tickers.append(upper)

    return result

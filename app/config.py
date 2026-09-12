from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    twitterapi_key: str = Field(alias="TWITTERAPI_KEY")
    twitterapi_base: str = "https://api.twitterapi.io"

    # Секрет, которым подписываем URL вебхука, чтобы посторонние не слали мусор.
    webhook_secret: str = Field(alias="WEBHOOK_SECRET")
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080

    telegram_token: str = Field(alias="TELEGRAM_TOKEN")
    telegram_chat_id: str = Field(alias="TELEGRAM_CHAT_ID")

    redis_url: str = "redis://redis:6379/0"

    # Один и тот же контракт не шлём повторно в течение этого окна.
    dedup_ttl_seconds: int = 6 * 3600

    # Резервный поллер: интервал опроса, если стрим недоступен.
    poll_interval_seconds: int = 15

    # Как часто tweet_filter проверяет правило. Это нижняя граница latency:
    # твит приедет в среднем через половину интервала.
    #
    # Замерено на живом аккаунте 2026-09-12: одна проверка стоит ~15 кредитов
    # независимо от того, есть новые твиты или нет. Отсюда прямой счёт:
    #     5 с  -> ~259 000 кредитов в сутки, средняя задержка ~2.5 с
    #    15 с  ->  ~86 000 в сутки, ~7.5 с
    #    30 с  ->  ~43 000 в сутки, ~15 с
    # Допустимый диапазон 0.1-86400. Прежде чем крутить вниз, посчитай,
    # на сколько суток хватит остатка: /oapi/my/info отдаёт баланс.
    filter_interval_seconds: float = 30.0

    # Разбор твита моделью перед тем, как считать алерт руководством к
    # действию. Пустой ключ = разбор выключен, пайплайн работает как раньше.
    anthropic_api_key: str | None = None
    analyst_model: str = "claude-opus-5"
    # Разбор идёт уже после отправки алерта, так что таймаут щедрый:
    # тормозить он может только дописывание вердикта, но не сам алерт.
    analyst_timeout_seconds: float = 20.0

    kols_path: Path = Path("config/kols.yml")


class Kol(BaseModel):
    handle: str
    tier: int = 2          # 1 = топ, 3 = шум
    note: str | None = None

    @property
    def weight(self) -> float:
        return {1: 1.0, 2: 0.6, 3: 0.3}.get(self.tier, 0.3)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_kols(path: str | None = None) -> dict[str, Kol]:
    """Возвращает словарь handle(lower) -> Kol."""
    p = Path(path) if path else get_settings().kols_path
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    kols = [Kol(**item) for item in raw.get("accounts", [])]
    return {k.handle.lower().lstrip("@"): k for k in kols}

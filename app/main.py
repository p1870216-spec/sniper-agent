from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.config import get_settings, load_kols
from app.dedup import Dedup
from app.notifier import Notifier
from app.pipeline import Pipeline
from app.providers.twitterapi_io import TwitterApiIoClient, extract_tweets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("sniper")

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    dedup = Dedup(settings.redis_url, settings.dedup_ttl_seconds)
    notifier = Notifier(settings.telegram_token, settings.telegram_chat_id)
    client = TwitterApiIoClient(settings)

    state["settings"] = settings
    state["dedup"] = dedup
    state["notifier"] = notifier
    state["client"] = client
    state["pipeline"] = Pipeline(settings, dedup, notifier)

    log.info("загружено KOL: %d", len(load_kols()))
    yield

    await asyncio.gather(client.aclose(), notifier.aclose(), dedup.aclose())


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request, background: BackgroundTasks) -> dict[str, int]:
    settings = state["settings"]
    if not secrets.compare_digest(secret, settings.webhook_secret):
        raise HTTPException(status_code=404)

    payload = await request.json()
    tweets = extract_tweets(payload)

    # Отвечаем 200 сразу: провайдеры ретраят вебхук по таймауту, и обработка
    # внутри запроса приводит к дублям. Разбор уходит в фон.
    pipeline = state["pipeline"]
    for tweet in tweets:
        background.add_task(pipeline.handle, tweet)

    return {"accepted": len(tweets)}

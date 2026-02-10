import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.settings import settings
from app.ari_client import AriClient, AriWsHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

ari_handler: AriWsHandler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ari_handler

    logger.info("Инициализация ARI клиента...")
    ari_client = AriClient(
        base_url=settings.ari_base_url,
        user=settings.ari_user,
        password=settings.ari_password,
        app_name=settings.ari_app,
    )

    ws_url = (
        f"ws://asterisk:8088/ari/events?app={settings.ari_app}"
    )
    ari_handler = AriWsHandler(
        ari_client=ari_client,
        ws_url=ws_url,
        app_name=settings.ari_app,
    )

    logger.info("Запуск ARI WebSocket handler...")
    task = asyncio.create_task(ari_handler.run())

    yield

    logger.info("Остановка ARI WebSocket handler...")
    if ari_handler:
        ari_handler.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="AI Voice Operator", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


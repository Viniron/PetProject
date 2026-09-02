"""FastAPI-приложение. На Э0 в нём только живость - предметного кода нет."""

from datetime import UTC, datetime

from fastapi import FastAPI
from pydantic import BaseModel

from jarvis_api import __version__
from jarvis_api.config import get_settings

app = FastAPI(title="JARVIS API", version=__version__)


class Health(BaseModel):
    """Ответ /health. Время отдаётся, чтобы инвариант 7 был проверяем извне."""

    status: str
    version: str
    env: str
    time: datetime


@app.get("/health")
async def health() -> Health:
    """Живость процесса.

    Базу не трогает намеренно: эндпоинт должен отвечать и при пустом
    DATABASE_URL - им проверяется, что контейнер поднялся, отдельно от того,
    доступна ли база.
    """
    return Health(
        status="ok",
        version=__version__,
        env=get_settings().env,
        # Время только tz-aware и только UTC (инвариант 7). Наивный datetime
        # здесь поймал бы линтер (правило DTZ), а не ревью.
        time=datetime.now(UTC),
    )

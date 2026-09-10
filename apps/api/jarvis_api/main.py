"""FastAPI-приложение: живость и планировщик фоновых джобов (Э5).

Предметных эндпоинтов здесь по-прежнему нет - API календаря принадлежит Э6.
Зато с Э5 у процесса появилась вторая обязанность: он носит в себе
планировщик, потому что триггера ровно два - APScheduler и догон при старте,
а внешнего тика нет и не будет (ADR-020).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from jarvis_api import __version__
from jarvis_api.config import get_settings
from jarvis_api.scheduler import запустить, остановить

logger = logging.getLogger("jarvis.api")


def настроить_логи() -> None:
    """Формат логов для процесса API - тот же, что у джобов из командной строки.

    Без этого этап был бы бессмысленным на плате. uvicorn настраивает только
    свои логгеры и не трогает корневой, а `logging.basicConfig` наш код зовёт
    лишь в `main()` джобов - то есть при запуске руками. Из планировщика тот
    же джоб писал бы в корневой логгер без единого обработчика: весь дифф и
    «75 создано» пропадали бы бесследно, а `logger.error` выходил бы аварийным
    `lastResort` - без времени, без имени, в stderr. Требование «фоновые джобы
    падают громко» держалось бы на честном слове.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Свой INFO у APScheduler - это «Job executed» на каждый прогон и
    # «Added job» на каждый слот. В `docker logs` это шум поверх того
    # единственного, что там нужно читать.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Подъём и остановка планировщика.

    **Ни одного обращения к базе.** uvicorn не начинает принимать соединения,
    пока startup не закончился, а недоступная (в отличие от ненастроенной)
    база не бросает исключение - она висит в таймауте сокета. Один такой
    вызов здесь означал бы: `/health` молчит, healthcheck compose валится,
    `restart: unless-stopped` крутит контейнер по кругу. Поэтому зона owner,
    построение слотов и решение о догоне живут в рабочем потоке (см.
    `scheduler.поднять`), а здесь остаётся только запуск.

    Планировщик хранится в `app.state`, а не в переменной модуля: тесты
    входят в lifespan одного и того же приложения по нескольку раз, и второй
    планировщик рядом с живым означал бы два реконсила в один календарь.
    """
    настроить_логи()

    settings = get_settings()
    планировщик: Any | None = getattr(app.state, "планировщик", None)
    if not settings.scheduler_enabled:
        logger.info("планировщик выключен (SCHEDULER_ENABLED)")
    elif планировщик is not None:
        logger.warning("планировщик уже поднят, второй не создаю")
    else:
        # Один процесс - один планировщик. `--workers N` у uvicorn поднял бы
        # N планировщиков и N параллельных прогонов в один календарь;
        # в Dockerfile.api его нет намеренно.
        планировщик = запустить(settings)
        app.state.планировщик = планировщик

    yield

    планировщик = getattr(app.state, "планировщик", None)
    if планировщик is not None:
        остановить(планировщик)
        app.state.планировщик = None


app = FastAPI(title="JARVIS API", version=__version__, lifespan=lifespan)


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

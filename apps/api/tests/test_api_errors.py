"""Единый формат отказа (Э6, §10).

Обработчики берутся из реестра приложения, а не импортируются по имени:
так проверяется и сама регистрация. Тип, забытый в `подключить_обработчики`,
вернул бы клиенту тело не из контракта, и заметить это по зелёному прогону
было бы нельзя.

Часть отказов через HTTP не воспроизводится и воспроизводиться не должна:
до `IntegrityError` запрос не доходит, потому что раньше срабатывает
валидация Pydantic. Это правильное поведение, поэтому обработчик проверяется
прямым вызовом с синтезированным исключением.
"""

import json
from collections.abc import Awaitable
from typing import Any

import pytest
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException

from jarvis_api.api.errors import ОтказAPI
from jarvis_api.db.session import DatabaseNotConfigured
from jarvis_api.jobs.common import OwnerZoneError
from jarvis_api.main import app

ЗАПРОС = Request({"type": "http", "method": "GET", "path": "/", "headers": []})


def отдать(тип: type[Exception], исключение: Exception) -> tuple[int, dict[str, Any]]:
    """Прогнать исключение через зарегистрированный обработчик."""
    обработчик = app.exception_handlers[тип]
    ответ: Response | Awaitable[Response] = обработчик(ЗАПРОС, исключение)
    assert isinstance(ответ, JSONResponse)
    тело: dict[str, Any] = json.loads(bytes(ответ.body))
    return ответ.status_code, тело


def test_база_не_настроена_это_503_без_повтора() -> None:
    """Повтор не поможет: помочь может только owner, заполнив .env на плате."""
    статус, тело = отдать(DatabaseNotConfigured, DatabaseNotConfigured("пусто"))

    assert статус == 503
    assert тело["code"] == "database_not_configured"
    assert тело["retryable"] is False
    assert ".env" in тело["message"]


@pytest.mark.parametrize(
    ("тип", "исключение"),
    [
        (OperationalError, OperationalError("select 1", {}, Exception("нет связи"))),
        (PoolTimeout, PoolTimeout("пул исчерпан")),
    ],
)
def test_база_не_отвечает_это_503_с_повтором(тип: type[Exception], исключение: Exception) -> None:
    """Исчерпанный пул отдаётся тем же кодом: для экрана это одно и то же
    состояние - связи с базой сейчас нет, через минуту может быть."""
    статус, тело = отдать(тип, исключение)

    assert статус == 503
    assert тело["code"] == "database_unavailable"
    assert тело["retryable"] is True


def test_сломанная_зона_это_503_и_названа_в_тексте() -> None:
    """Не 500: 5xx остаётся «наша вина», а запрет §10 на пятисотку выполнен.
    Зона называется, потому что починка - одна строка UPDATE."""
    ошибка = OwnerZoneError("в настройках задана неизвестная зона 'Мордор/Барад-Дур'")

    статус, тело = отдать(OwnerZoneError, ошибка)

    assert статус == 503
    assert тело["code"] == "owner_timezone_invalid"
    assert тело["retryable"] is False
    assert "Мордор/Барад-Дур" in тело["message"]


def test_ограничение_базы_это_409() -> None:
    статус, тело = отдать(IntegrityError, IntegrityError("insert", {}, Exception("CHECK")))

    assert статус == 409
    assert тело["code"] == "conflict"


def test_переполнение_поля_это_422_а_не_пятисотка() -> None:
    """Postgres отдаёт переполнение `String(64)` типом `DataError`, а не
    `IntegrityError`. Обработчик только на второй тип оставил бы 500 на
    слишком длинной заметке к периоду - ADR-019 требует внятной ошибки."""
    статус, тело = отдать(DataError, DataError("insert", {}, Exception("too long")))

    assert статус == 422
    assert тело["code"] == "validation_error"


def test_отказ_эндпоинта_несёт_свой_код() -> None:
    """Код задаётся в одном месте с текстом, а не собирается из статуса."""
    отказ = ОтказAPI(статус=409, code="late_classes_reserved", message="ставит система")

    статус, тело = отдать(ОтказAPI, отказ)

    assert статус == 409
    assert (тело["code"], тело["message"]) == ("late_classes_reserved", "ставит система")


def test_разбор_запроса_перечисляет_поля() -> None:
    ошибка = RequestValidationError(
        [
            {
                "loc": ("query", "view"),
                "msg": "Input should be 'day' or 'week'",
                "type": "literal_error",
            }
        ]
    )

    статус, тело = отдать(RequestValidationError, ошибка)

    assert статус == 422
    assert тело["code"] == "validation_error"
    assert тело["details"] == ["query.view: Input should be 'day' or 'week'"]


def test_неожидаемое_это_500_в_нашем_формате() -> None:
    """Пятисотка - баг, а не режим, но тело всё равно наше: заглушки
    Starlette в контракте нет, и клиент разобрал бы её как чужой ответ."""
    статус, тело = отдать(Exception, RuntimeError("что-то развалилось"))

    assert статус == 500
    assert тело["code"] == "internal_error"
    assert тело["retryable"] is True


def test_отказ_фреймворка_переведён_в_наш_формат() -> None:
    """Тип - базовый, из `starlette.exceptions`: роутер при ненайденном пути
    бросает именно его, и обработчик на подклассе FastAPI до него не достаёт."""
    отказ = HTTPException(status_code=404, detail="нет такого")

    статус, тело = отдать(HTTPException, отказ)

    assert (статус, тело["code"]) == (404, "not_found")


def test_неизвестный_путь_отвечает_нашим_телом() -> None:
    """Сквозная проверка того же: тело 404 обязано быть из контракта."""
    клиент = TestClient(app)

    ответ = клиент.get("/такого-нет")

    assert ответ.status_code == 404
    assert set(ответ.json()) >= {"code", "message", "retryable"}
    assert ответ.json()["code"] == "not_found"


def test_health_не_задет_обработчиками() -> None:
    """Регресс: /health обязан отвечать 200 и не ходить в базу."""
    клиент = TestClient(app)

    ответ = клиент.get("/health")

    assert ответ.status_code == 200
    assert ответ.json()["status"] == "ok"

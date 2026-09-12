"""Единый формат отказа и перевод исключений в коды ответа (§10).

Тело ошибки одно на все отказы и описано моделью Pydantic - значит оно
попадает в контракт, и клиент Э7 получает **один** тип ошибки, а не три.
Ради этого переопределены штатные обработчики FastAPI: `HTTPException` отдаёт
`{"detail": "..."}`, а `RequestValidationError` - `{"detail": [...]}`, то есть
без этого модуля в контракте оказалось бы три разных формы тела.

§10 запрещает два способа обработки отказа - упасть и соврать - и требует
третий: показать то, что есть, и честно подписать. Отсюда `retryable`: он
говорит экрану, показывать «повторяем» или «объяснение и стоп», и снимает
с клиента необходимость сопоставлять коды со стратегиями.

Три отказа базы различаются намеренно, потому что чинятся по-разному:
не настроена (owner заполняет `.env` на плате), не отвечает (пройдёт само),
и зона в `settings` испорчена - тут не помогут ни повтор, ни ожидание.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException

from jarvis_api.db.session import DatabaseNotConfigured
from jarvis_api.jobs.common import OwnerZoneError

logger = logging.getLogger("jarvis.api")


class ErrorBody(BaseModel):
    """Тело любого отказа API.

    Имя класса латиницей, в отличие от остального кода проекта, и это не
    вкусовщина: имена моделей Pydantic попадают в контракт именами схем
    OpenAPI, а из них генератор делает имена типов TypeScript. Кириллица
    там формально законна и практически ломает автодополнение и генераторы.
    То же правило действует в `api/schemas.py`.
    """

    code: str
    message: str
    # Пройдёт ли само. `true` - «нет связи, повторяем»; `false` - «объяснение
    # и стоп»: повтор ничего не изменит, пока человек не вмешается.
    retryable: bool
    # Подробности разбора запроса, по одной строке на поле. Список строк,
    # а не структура ошибок Pydantic: в контракте она разрослась бы в схему,
    # которую клиенту всё равно только печатать.
    details: list[str] | None = None


class ОтказAPI(Exception):
    """Отказ, о котором эндпоинт знает сам: конфликт, отсутствие, запрет.

    Своим типом, а не `HTTPException`, чтобы код отказа задавался в одном
    месте с текстом и признаком повторяемости, а не собирался из статуса
    в обработчике.
    """

    def __init__(self, *, статус: int, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.статус = статус
        self.code = code
        self.message = message
        self.retryable = retryable


# Коды для отказов, которые поднимает сам фреймворк: неизвестный путь,
# неподходящий метод. Тело у них обязано быть нашим - иначе клиент, разбирая
# ответ по контракту, на 404 получит поле, которого в схеме нет.
КОДЫ_ПО_СТАТУСУ = {
    404: "not_found",
    405: "not_found",
    409: "conflict",
    422: "validation_error",
}


def _ответ(статус: int, отказ: ErrorBody) -> JSONResponse:
    return JSONResponse(status_code=статус, content=отказ.model_dump())


def _база_не_настроена(request: Request, exc: Exception) -> JSONResponse:
    return _ответ(
        503,
        ErrorBody(
            code="database_not_configured",
            message=(
                "Сервер не настроен: в .env на плате пуст DATABASE_URL. "
                "Повтор не поможет - см. .env.example"
            ),
            retryable=False,
        ),
    )


def _база_не_отвечает(request: Request, exc: Exception) -> JSONResponse:
    """Недоступная база и исчерпанный пул - один ответ для экрана.

    Пул исчерпан ровно тогда, когда планировщик гоняет цепочку в этом же
    процессе (ADR-027): десять соединений заняты, и запрос экрана обязан
    получить честный отказ, а не ждать освобождения молча.
    """
    logger.warning("база недоступна: %s", exc.__class__.__name__)
    return _ответ(
        503,
        ErrorBody(
            code="database_unavailable",
            message="База данных не отвечает. Проверьте, что контейнер db поднят",
            retryable=True,
        ),
    )


def _зона_испорчена(request: Request, exc: Exception) -> JSONResponse:
    """Неизвестная зона в `settings` - испорченные данные, а не сбой.

    Отдаётся 503, а не 500: 5xx остаётся «наша вина», а запрет §10 на
    пятисотку читается выполненным. Текст называет саму зону, потому что
    починка - это одна строка `UPDATE settings SET timezone = ...`.
    """
    return _ответ(
        503,
        ErrorBody(code="owner_timezone_invalid", message=str(exc), retryable=False),
    )


def _ограничение_базы(request: Request, exc: Exception) -> JSONResponse:
    """Нарушение ограничения базы - конфликт, а не пятисотка.

    `DataError` в паре с `IntegrityError` не педантизм: переполнение
    `String(64)` Postgres отдаёт именно им, и обработчик только на второй
    тип оставил бы 500 на слишком длинной заметке к периоду.
    """
    if isinstance(exc, DataError):
        return _ответ(
            422,
            ErrorBody(
                code="validation_error",
                message="Значение не помещается в поле - проверьте длину",
                retryable=False,
            ),
        )
    return _ответ(
        409,
        ErrorBody(
            code="conflict",
            message="Запись нарушает ограничение базы данных",
            retryable=False,
        ),
    )


def _разбор_запроса(request: Request, exc: Exception) -> JSONResponse:
    подробности: list[str] = []
    if isinstance(exc, RequestValidationError):
        подробности = [
            f"{'.'.join(str(часть) for часть in ошибка['loc'])}: {ошибка['msg']}"
            for ошибка in exc.errors()
        ]
    return _ответ(
        422,
        ErrorBody(
            code="validation_error",
            message="Запрос не разобран",
            retryable=False,
            details=подробности or None,
        ),
    )


def _отказ_эндпоинта(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ОтказAPI)
    return _ответ(
        exc.статус,
        ErrorBody(code=exc.code, message=exc.message, retryable=exc.retryable),
    )


def _отказ_фреймворка(request: Request, exc: Exception) -> JSONResponse:
    """Отказы, которые поднимает сам фреймворк: неизвестный путь, чужой метод.

    Тип берётся из `starlette.exceptions`, а не из `fastapi`. Это не
    придирка: `fastapi.HTTPException` - подкласс, роутер Starlette при
    ненайденном пути бросает **базовый**, и обработчик, повешенный на
    подкласс, до него не достаёт. Найдено тестом: неизвестный путь отдавал
    штатное `{"detail": "Not Found"}`, то есть тело не из контракта.
    """
    assert isinstance(exc, HTTPException)
    return _ответ(
        exc.status_code,
        ErrorBody(
            code=КОДЫ_ПО_СТАТУСУ.get(exc.status_code, "internal_error"),
            message=str(exc.detail),
            retryable=exc.status_code >= 500,
        ),
    )


def _неожидаемое(request: Request, exc: Exception) -> JSONResponse:
    """Пятисотка - это баг, а не режим работы, поэтому ERROR в лог.

    Тело всё равно наше: экран owner должен получить читаемое объяснение,
    а не текстовую заглушку Starlette, которой нет в контракте.
    """
    logger.error("необработанный отказ эндпоинта", exc_info=exc)
    return _ответ(
        500,
        ErrorBody(
            code="internal_error",
            message="Внутренняя ошибка сервера. Подробности в логе процесса",
            retryable=True,
        ),
    )


def подключить_обработчики(app: FastAPI) -> None:
    """Повесить все обработчики на приложение.

    Порядок регистрации значения не имеет - Starlette выбирает обработчик
    по типу исключения, - но список обязан быть полным: любой тип, забытый
    здесь, вернётся клиенту телом не из контракта.
    """
    app.add_exception_handler(DatabaseNotConfigured, _база_не_настроена)
    app.add_exception_handler(OperationalError, _база_не_отвечает)
    app.add_exception_handler(PoolTimeout, _база_не_отвечает)
    app.add_exception_handler(OwnerZoneError, _зона_испорчена)
    app.add_exception_handler(IntegrityError, _ограничение_базы)
    app.add_exception_handler(DataError, _ограничение_базы)
    app.add_exception_handler(RequestValidationError, _разбор_запроса)
    app.add_exception_handler(ОтказAPI, _отказ_эндпоинта)
    app.add_exception_handler(HTTPException, _отказ_фреймворка)
    app.add_exception_handler(Exception, _неожидаемое)


# Отказы, объявляемые эндпоинтами в контракте. Объявлять их обязательно:
# без этого FastAPI подставляет на 422 собственную схему HTTPValidationError,
# и в контракте оказывается второе тело ошибки - ровно то, чего этот модуль
# избегает. 503 объявлен у всех эндпоинтов, ходящих в базу: он не редкость,
# а штатный ответ на недоступную базу и испорченную зону (§10).
ОТКАЗЫ: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorBody, "description": "Запрос не разобран"},
    503: {"model": ErrorBody, "description": "База данных или настройки недоступны"},
}

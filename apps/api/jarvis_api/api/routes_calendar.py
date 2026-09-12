"""Эндпоинт сетки календаря (Э6, §8.4).

Роутер тонкий: разобрать параметры, позвать `domain.calendar.собрать`, отдать
схему. Ни одного расчёта здесь нет намеренно - границы недели, конфликты
и давность проверяются без HTTP.

Функция объявлена `def`, а не `async def`. Движок синхронный (ADR-021),
и запрос к базе внутри `async def` заблокировал бы цикл событий целиком -
вместе с `/health`, по которому compose решает, жив ли контейнер. Правило
ruff `ASYNC` этого не ловит, поэтому оно закреплено тестом.
"""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query

from jarvis_api.api.deps import Настройки, Сейчас, Сессия
from jarvis_api.api.errors import ОТКАЗЫ
from jarvis_api.api.schemas import CalendarOut
from jarvis_api.domain.calendar import Вид, собрать

маршрутизатор = APIRouter(prefix="/api", tags=["calendar"])


@маршрутизатор.get(
    "/calendar",
    responses=ОТКАЗЫ,
    summary="Сетка календаря на день или неделю",
)
def сетка_календаря(
    сессия: Сессия,
    настройки: Настройки,
    момент: Сейчас,
    view: Annotated[Вид, Query(description="Масштаб: день или неделя")] = "week",
    date: Annotated[
        dt.date | None,
        Query(description="Любая дата внутри периода. Не задана - сегодня в зоне owner"),
    ] = None,
) -> CalendarOut:
    """Пары университета, личные события, пометки дней и пометка давности.

    Границы периода считает сервер, а не клиент: неделя зависит от
    `settings.timezone`, которой у клиента нет (инвариант 1). Запрос за
    пределы окна забора - не ошибка, а законный вопрос: дни приходят
    с `mirror_covers = false`, то есть «сохранённых данных нет».
    """
    return CalendarOut.model_validate(собрать(сессия, настройки, момент, view, date))

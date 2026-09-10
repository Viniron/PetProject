"""Расписание планировщика (Э5): слоты, зона, уникальность заданий.

Базы здесь нет намеренно: проверяется разбор настройки и построение
триггеров - то есть ровно то место, где ошибка выражается не отказом,
а прогоном в неправильное время. Такую ошибку не видно ни в логах,
ни в тестах джобов: они просто идут на три часа позже.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from jarvis_api.config import Settings, разобрать_слоты
from jarvis_api.scheduler import собрать_расписание

МОСКВА = ZoneInfo("Europe/Moscow")


def настройки(**переопределения: object) -> Settings:
    return Settings(**переопределения)  # type: ignore[arg-type]


def test_умолчание_шесть_слотов() -> None:
    """Решение owner: каждые три часа с 06:30 до 21:30."""
    слоты = разобрать_слоты(настройки().scheduler_daily_times)

    assert слоты == (
        dt.time(6, 30),
        dt.time(9, 30),
        dt.time(12, 30),
        dt.time(15, 30),
        dt.time(18, 30),
        dt.time(21, 30),
    )


def test_разбор_терпит_пробелы_и_сортирует() -> None:
    assert разобрать_слоты(" 21:30 , 06:30 ") == (dt.time(6, 30), dt.time(21, 30))


@pytest.mark.parametrize("мусор", ["утром", "25:00", "06;30", "", "   ", ","])
def test_мусор_в_расписании_падает_при_старте(мусор: str) -> None:
    """Опечатка в .env обязана падать внятно и сразу.

    Иначе она всплывает молчанием: планировщик поднялся, слотов нет,
    и снаружи это неотличимо от рабочего состояния.

    Пустая строка - исключение: она означает «умолчание», потому что
    в compose переменные прокидываются как ${ИМЯ:-} (tests/test_compose_env).
    """
    if not мусор.strip():
        assert настройки(scheduler_daily_times=мусор).scheduler_daily_times.startswith("06:30")
        return

    with pytest.raises(ValidationError):
        настройки(scheduler_daily_times=мусор)


def test_триггеры_строятся_на_каждый_слот() -> None:
    расписание = собрать_расписание(настройки(), МОСКВА)

    assert len(расписание) == 6
    идентификаторы = [идентификатор for идентификатор, _ in расписание]
    assert len(set(идентификаторы)) == 6, "повторяющийся id - второе задание затрёт первое"
    assert идентификаторы[0] == "daily_calendar@06:30"


def test_триггер_считает_время_в_зоне_owner() -> None:
    """Главная проверка модуля: зона передана явно.

    Забыли передать - tzlocal подставит зону контейнера (UTC), и прогон
    молча уедет на три часа. Ни один другой тест этого не заметит.
    """
    _, триггер = собрать_расписание(настройки(scheduler_daily_times="06:30"), МОСКВА)[0]

    ближайший = триггер.get_next_fire_time(None, dt.datetime(2026, 9, 10, 0, 0, tzinfo=dt.UTC))

    assert ближайший.astimezone(МОСКВА).strftime("%H:%M") == "06:30"
    assert ближайший.astimezone(dt.UTC).strftime("%H:%M") == "03:30"

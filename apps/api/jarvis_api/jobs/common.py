"""Общее для всех джобов: зона owner, окно расписания, отметка прогона.

Модуль появился на Э5 и ничего нового не делает - он собирает то, что
к четвёртому этапу оказалось в трёх экземплярах. `_отметить_прогон` была
дословной копией в `sync_itmo` и `push_gcal`, а `owner_timezone`
и `sync_window` жили в джобе забора, откуда их импортировали и джоб записи,
и настройка календарей, и тесты. Планировщик стал бы четвёртым, кто
импортирует «джоб забора расписания» ради функции, к забору не относящейся.

Здесь же лечатся два дефекта, найденные при проектировании Э5:
неизвестная зона в `settings` роняла `push_gcal` трассировкой, потому что
`SyncError` из чужого модуля мимо его `except`; и запись следа об отказе
сама читала зону голым вызовом, то есть могла упасть второй раз ровно
тогда, когда отказ вызвала зона.
"""

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import JobRun, Setting

# Запасная зона, если строки настроек в базе ещё нет. Совпадает с
# server_default колонки `settings.timezone` намеренно: два разных умолчания
# дали бы расписание, сдвинутое на часы, в зависимости от того, успел ли
# кто-нибудь создать строку настроек.
FALLBACK_TIMEZONE = "Europe/Moscow"


class OwnerZoneError(RuntimeError):
    """В настройках задана зона, которой не существует.

    Своим типом, а не `SyncError` или `PushError`: зону читают все джобы,
    и отказ обязан ловиться каждым из них. Пока этот тип принадлежал джобу
    забора, джоб записи о нём не знал и падал трассировкой.
    """


def owner_timezone(session: Session) -> ZoneInfo:
    """Зона owner из настроек. Нужна, чтобы понять, что за «10:00» у портала.

    Читается из базы, а не из env: она уже там (`settings.timezone`), и вторая
    копия рано или поздно разойдётся с первой. Неизвестное имя зоны - отказ:
    подставить UTC значило бы сдвинуть всё расписание на три часа молча.
    """
    строка = session.get(Setting, 1)
    имя = строка.timezone if строка is not None else FALLBACK_TIMEZONE
    try:
        return ZoneInfo(имя)
    except (ZoneInfoNotFoundError, ValueError) as ошибка:
        raise OwnerZoneError(f"в настройках задана неизвестная зона {имя!r}") from ошибка


def зона_без_падения(session: Session) -> ZoneInfo:
    """Зона для записи следа об отказе.

    Отказ могла вызвать сама зона, и падать второй раз при записи следа
    об этом - худший из возможных исходов: пропадёт и след, и причина.
    """
    try:
        return owner_timezone(session)
    except OwnerZoneError:
        return ZoneInfo(FALLBACK_TIMEZONE)


def sync_window(settings: Settings, today: dt.date) -> tuple[dt.date, dt.date]:
    """Границы забора вокруг сегодняшнего дня, включительно.

    Одно окно на забор и на запись, из одного места: две границы, живущие
    по отдельности, расходятся молча, и пара с края окна начинает то
    появляться в календаре, то исчезать.
    """
    return (
        today - dt.timedelta(days=settings.itmo_sync_days_back),
        today + dt.timedelta(days=settings.itmo_sync_days_ahead),
    )


def начать_прогон(session: Session, job: str, run_date: dt.date, now: dt.datetime) -> None:
    """Строка `running`: прогон начался и ещё не кончился.

    Пишется и коммитится до первого шага намеренно. Процесс, убитый посреди
    работы (перезагрузка платы, SIGKILL по истечении `stop_grace_period`),
    оставляет после себя ровно эту строку с пустым `finished_at` - иначе
    от прерванного прогона не осталось бы следа нигде.

    `finished_at` и `error` очищаются: строка одна на джоб и день, и второй
    прогон за день не должен носить время окончания первого.
    """
    существующая = session.scalars(
        select(JobRun).where(JobRun.job == job, JobRun.run_date == run_date)
    ).one_or_none()
    if существующая is None:
        существующая = JobRun(job=job, run_date=run_date)
        session.add(существующая)
    существующая.started_at = now
    существующая.finished_at = None
    существующая.status = "running"
    существующая.error = None


def отметить_прогон(
    session: Session,
    job: str,
    run_date: dt.date,
    now: dt.datetime,
    status: str,
    error: str | None = None,
) -> None:
    """Строка в `job_runs`: джоб за этот день отработал.

    Одна строка на джоб и день - это ограничение базы, и оно же делает
    догоняющий запуск (§11.2) безопасным: второй прогон за день обновляет
    строку, а не заводит вторую.

    Строка ищется заново на каждый вызов, а не носится ссылкой между
    шагами: между двумя вызовами могли случиться и коммит, и `rollback()`
    внутри джоба, после которого прежний объект уже не принадлежит сессии.
    """
    существующая = session.scalars(
        select(JobRun).where(JobRun.job == job, JobRun.run_date == run_date)
    ).one_or_none()
    if существующая is None:
        существующая = JobRun(job=job, run_date=run_date, started_at=now)
        session.add(существующая)
    существующая.finished_at = now
    существующая.status = status
    существующая.error = error

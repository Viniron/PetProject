"""Джоб записи в календарь: зеркало `itmo_lessons` -> Google Calendar.

Продолжение Э3 ровно там, где тот остановился: забор довёл до базы
желаемое состояние, этот джоб доводит до Google фактическое.

**Реконсил, а не "добавить ещё раз".** Двойной запуск за день - штатный
сценарий catch-up (§11.2), и он обязан не создавать вторых копий.
Поэтому решение о каждом событии принимается сравнением трёх источников:
зеркало (что должно быть), журнал `calendar_events` (что мы писали)
и сам Google (что там лежит сейчас). Третий нужен не для красоты -
без него не переживаются два обычных случая: база восстановлена из дампа
и событие удалено в календаре руками.

**Коммит после каждого события, а не один на прогон.** Отличие от Э3
сознательное: там писалось только в свою базу, и полуприменённое окно
откатывалось целиком. Здесь запись уже ушла в чужую систему, и откат
журнала означал бы, что следующий прогон создаст в Google второе такое же
событие. Цельность транзакции (§11.2) держится на уровне одного события:
"записали в Google и запомнили это" - неделимо.

**Отказ по одному событию не останавливает остальные.** Строка помечается
`failed`, прогон идёт дальше, а в конце джоб всё равно возвращает
ненулевой код и пишет `failed` в `job_runs` (§10: фоновые джобы падают
громко). Отказ доступа - другое дело: он прекращает прогон, потому что
следующие восемьдесят событий получат ровно тот же отказ.
"""

import argparse
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.models import AuditLogEntry, CalendarEvent, ItmoLesson, JobRun, Setting
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.integrations.gcal.client import (
    GcalClient,
    GcalError,
    GcalItemError,
    build_service,
)
from jarvis_api.integrations.gcal.mapping import DesiredEvent, lesson_to_event

# Окно записи то же, что окно забора, и берётся из того же места намеренно:
# две границы, живущие по отдельности, разъезжаются молча, и пара из края
# окна начинает то появляться в календаре, то исчезать.
from jarvis_api.jobs.sync_itmo import owner_timezone, sync_window

logger = logging.getLogger("jarvis.push_gcal")

JOB_NAME = "push_gcal"

# Календарь, в который пишет этот джоб. Их три (CLAUDE.md), но два других
# наполняются на своих этапах: `study` - занятиями курсов, `events` -
# подтверждёнными черновиками захвата.
CALENDAR = "itmo"
SOURCE = "itmo"


class PushError(RuntimeError):
    """Запись в календарь не выполнена целиком."""


@dataclass(slots=True)
class PushReport:
    """Что джоб сделал бы или сделал."""

    date_start: dt.date
    date_end: dt.date
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: int = 0
    # Ключ события и причина. Список, а не счётчик: в логе должно быть
    # видно, какая именно пара не уехала, иначе разбираться придётся
    # сравнением календаря с порталом глазами.
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def есть_изменения(self) -> bool:
        return bool(self.created or self.updated or self.deleted)


@dataclass(frozen=True, slots=True)
class Действие:
    """Одно решение реконсила по одному событию."""

    вид: str  # create | update | delete
    ключ: str
    событие: DesiredEvent | None
    google_event_id: str | None


def календарь_итмо(session: Session) -> str:
    """id календаря `JARVIS · ИТМО` из настроек.

    Пусто - отказ с указанием команды, а не попытка создать календарь
    на лету: создание расшаривает календарь owner и обязано происходить
    осознанно, один раз, а не внутри ежедневного джоба.
    """
    строка = session.get(Setting, 1)
    if строка is None or not строка.gcal_itmo_id:
        raise PushError(
            "календарь JARVIS · ИТМО не создан: в settings пуст gcal_itmo_id. "
            "Заведите календари командой `make gcal-setup-apply`"
        )
    return строка.gcal_itmo_id


def границы_окна(
    date_start: dt.date, date_end: dt.date, зона: ZoneInfo
) -> tuple[dt.datetime, dt.datetime]:
    """Окно дат в моменты времени для запроса к Google.

    Конец - начало дня, следующего за последним: `date_end` включительная,
    и пара, начинающаяся в 21:00 последнего дня, обязана попасть в выдачу.
    """
    начало = dt.datetime.combine(date_start, dt.time.min, tzinfo=зона)
    конец = dt.datetime.combine(date_end + dt.timedelta(days=1), dt.time.min, tzinfo=зона)
    return начало.astimezone(dt.UTC), конец.astimezone(dt.UTC)


def желаемое(session: Session, date_start: dt.date, date_end: dt.date) -> dict[str, DesiredEvent]:
    """Что должно лежать в календаре по данным зеркала."""
    запрос = select(ItmoLesson).where(
        ItmoLesson.lesson_date >= date_start,
        ItmoLesson.lesson_date <= date_end,
    )
    return {пара.source_key: lesson_to_event(пара) for пара in session.scalars(запрос)}


def журнал(session: Session, начало: dt.datetime, конец: dt.datetime) -> dict[str, CalendarEvent]:
    """Строки журнала внутри окна. За его пределами не трогаем ничего."""
    запрос = select(CalendarEvent).where(
        CalendarEvent.source == SOURCE,
        CalendarEvent.starts_at >= начало,
        CalendarEvent.starts_at < конец,
    )
    return {строка.external_key: строка for строка in session.scalars(запрос)}


def diff(
    нужные: dict[str, DesiredEvent],
    записанные: dict[str, CalendarEvent],
    в_google: dict[str, str],
) -> list[Действие]:
    """Решение по каждому событию. Ничего не пишет.

    Правило одно: событие не трогается, только если оно **есть в Google**
    и его отпечаток совпадает с журналом. Любое расхождение - работа.

    Отсюда следуют два случая, ради которых реконсил и нужен. Событие
    удалили в Google руками: журнал говорит `synced`, но ключа нет
    в выдаче - создаём заново. База восстановлена из дампа: журнал пуст,
    а ключ в выдаче есть - обновляем существующее и запоминаем его id,
    вместо того чтобы завести вторую копию семестра.
    """
    действия: list[Действие] = []

    for ключ, событие in sorted(нужные.items(), key=lambda пара: пара[1].starts_at):
        идентификатор = в_google.get(ключ)
        строка = записанные.get(ключ)
        if идентификатор is None:
            действия.append(Действие("create", ключ, событие, None))
            continue
        совпало = (
            строка is not None
            and строка.content_hash == событие.content_hash()
            and строка.google_event_id == идентификатор
            and строка.sync_state == "synced"
        )
        if not совпало:
            действия.append(Действие("update", ключ, событие, идентификатор))

    лишние = (set(записанные) | set(в_google)) - set(нужные)
    for ключ in sorted(лишние):
        строка = записанные.get(ключ)
        идентификатор = в_google.get(ключ) or (строка.google_event_id if строка else None)
        действия.append(Действие("delete", ключ, None, идентификатор))

    return действия


def _записать_в_аудит(session: Session, status: str, ключ: str, detail: dict[str, object]) -> None:
    """След каждой записи в календарь - инвариант 8, без исключений."""
    session.add(
        AuditLogEntry(
            kind="calendar_write",
            actor=JOB_NAME,
            status=status,
            target=ключ,
            provider="google",
            detail=detail,
        )
    )


def _отметить_прогон(
    session: Session,
    run_date: dt.date,
    now: dt.datetime,
    status: str,
    error: str | None = None,
) -> None:
    """Строка в `job_runs`: джоб за этот день отработал. Одна на джоб и день."""
    существующая = session.scalars(
        select(JobRun).where(JobRun.job == JOB_NAME, JobRun.run_date == run_date)
    ).one_or_none()
    if существующая is None:
        существующая = JobRun(job=JOB_NAME, run_date=run_date, started_at=now)
        session.add(существующая)
    существующая.finished_at = now
    существующая.status = status
    существующая.error = error


def _сохранить_строку(
    session: Session,
    ключ: str,
    событие: DesiredEvent,
    google_event_id: str,
    now: dt.datetime,
) -> None:
    """Журнал после успешной записи в Google."""
    строка = session.get(CalendarEvent, ключ)
    if строка is None:
        строка = CalendarEvent(external_key=ключ, calendar=CALENDAR, source=SOURCE)
        session.add(строка)
    строка.title = событие.summary
    строка.starts_at = событие.starts_at
    строка.ends_at = событие.ends_at
    строка.location = событие.location
    строка.description = событие.description
    строка.content_hash = событие.content_hash()
    строка.google_event_id = google_event_id
    строка.sync_state = "synced"
    строка.last_error = None
    строка.synced_at = now


def _пометить_отказ(
    session: Session, ключ: str, событие: DesiredEvent | None, причина: str
) -> None:
    """Строка осталась рассинхронизированной - §10, предпоследняя строка таблицы.

    Пометка нужна и тогда, когда события в журнале ещё не было: без неё
    неудавшееся создание не оставляет следа нигде, кроме лога, а лог
    на плате никто не читает.
    """
    строка = session.get(CalendarEvent, ключ)
    if строка is None:
        if событие is None:
            return
        строка = CalendarEvent(
            external_key=ключ,
            calendar=CALENDAR,
            source=SOURCE,
            title=событие.summary,
            starts_at=событие.starts_at,
            ends_at=событие.ends_at,
            location=событие.location,
            description=событие.description,
            content_hash=событие.content_hash(),
        )
        session.add(строка)
    строка.sync_state = "failed"
    строка.last_error = причина


def применить(
    session: Session,
    client: GcalClient,
    calendar_id: str,
    действия: Sequence[Действие],
    отчёт: PushReport,
    now: dt.datetime,
) -> None:
    """Выполняет решения реконсила. Коммит после каждого события.

    Порядок внутри одного события неделим и важен: сначала Google, потом
    журнал, потом коммит. Обратный порядок оставил бы в журнале событие,
    которого в календаре нет.
    """
    for действие in действия:
        # Вложенная транзакция на событие, а не rollback всей сессии.
        # Разница найдена тестом: `session.rollback()` откатывает вообще всё
        # незакоммиченное, включая работу, которая к этому событию отношения
        # не имеет. Здесь нужно снять ровно полуприменённое одно событие.
        точка = session.begin_nested()
        try:
            if действие.вид == "create" and действие.событие is not None:
                идентификатор = client.insert(calendar_id, действие.событие.body())
                _сохранить_строку(session, действие.ключ, действие.событие, идентификатор, now)
                _записать_в_аудит(session, "ok", действие.ключ, {"action": "create"})
                отчёт.created.append(действие.ключ)
            elif действие.вид == "update" and действие.событие is not None:
                идентификатор = действие.google_event_id or ""
                client.update(calendar_id, идентификатор, действие.событие.body())
                _сохранить_строку(session, действие.ключ, действие.событие, идентификатор, now)
                _записать_в_аудит(session, "ok", действие.ключ, {"action": "update"})
                отчёт.updated.append(действие.ключ)
            else:
                if действие.google_event_id:
                    client.delete(calendar_id, действие.google_event_id)
                строка = session.get(CalendarEvent, действие.ключ)
                if строка is not None:
                    session.delete(строка)
                _записать_в_аудит(session, "ok", действие.ключ, {"action": "delete"})
                отчёт.deleted.append(действие.ключ)
            точка.commit()
        except GcalItemError as сбой:
            # Одна пара не уехала. Остальные обязаны уехать: календарь без
            # семидесяти девяти пар хуже календаря без одной.
            точка.rollback()
            _пометить_отказ(session, действие.ключ, действие.событие, str(сбой))
            _записать_в_аудит(
                session, "error", действие.ключ, {"action": действие.вид, "error": str(сбой)}
            )
            отчёт.failed.append((действие.ключ, str(сбой)))
            logger.error("событие %s: %s", действие.ключ, сбой)
        session.commit()


def описать(отчёт: PushReport, apply: bool) -> list[str]:
    """Человекочитаемый дифф. Отдельной функцией, чтобы его проверял тест."""
    строки = [
        f"окно {отчёт.date_start.isoformat()} .. {отчёт.date_end.isoformat()}",
        f"создано {len(отчёт.created)}, обновлено {len(отчёт.updated)}, "
        f"удалено {len(отчёт.deleted)}, без изменений {отчёт.unchanged}, "
        f"с ошибкой {len(отчёт.failed)}",
    ]
    for ключ in отчёт.created:
        строки.append(f"  + {ключ}")
    for ключ in отчёт.updated:
        строки.append(f"  ~ {ключ}")
    for ключ in отчёт.deleted:
        строки.append(f"  - {ключ}")
    for ключ, причина in отчёт.failed:
        строки.append(f"  ! {ключ}: {причина}")
    if not apply:
        строки.append("dry-run: в Google и в базу не записано ничего")
    return строки


def push(
    session: Session,
    settings: Settings,
    client: GcalClient,
    *,
    apply: bool,
    now: dt.datetime,
) -> PushReport:
    """Один прогон. Транзакцию за пределами событий коммитит вызывающий код."""
    зона = owner_timezone(session)
    сегодня = now.astimezone(зона).date()
    начало_дат, конец_дат = sync_window(settings, сегодня)
    calendar_id = календарь_итмо(session)
    начало, конец = границы_окна(начало_дат, конец_дат, зона)

    нужные = желаемое(session, начало_дат, конец_дат)
    записанные = журнал(session, начало, конец)
    в_google = client.list_window(calendar_id, начало, конец)

    действия = diff(нужные, записанные, в_google)
    отчёт = PushReport(date_start=начало_дат, date_end=конец_дат)
    отчёт.unchanged = len(нужные) - sum(1 for д in действия if д.вид != "delete")

    if not apply:
        # Дифф считается по тем же данным, что и запись, поэтому в dry-run
        # раскладываем действия по вёдрам отчёта, ничего не выполняя.
        for действие in действия:
            {"create": отчёт.created, "update": отчёт.updated, "delete": отчёт.deleted}[
                действие.вид
            ].append(действие.ключ)
        return отчёт

    применить(session, client, calendar_id, действия, отчёт, now)
    _отметить_прогон(
        session,
        сегодня,
        now,
        "failed" if отчёт.failed else "ok",
        f"{len(отчёт.failed)} событий не записано" if отчёт.failed else None,
    )
    session.commit()
    return отчёт


def run_once(
    session: Session,
    settings: Settings,
    client: GcalClient,
    *,
    apply: bool,
    now: dt.datetime,
) -> int:
    """Прогон поверх готовой сессии и клиента. Возвращает код возврата процесса."""
    try:
        отчёт = push(session, settings, client, apply=apply, now=now)
    except (PushError, GcalError) as сбой:
        # Откат до записи следа: полуприменённое состояние в журнале хуже
        # отсутствия записи - следующий прогон принял бы его за истину.
        session.rollback()
        if apply:
            _записать_в_аудит(session, "error", JOB_NAME, {"error": str(сбой)})
            _отметить_прогон(
                session,
                now.astimezone(owner_timezone(session)).date(),
                now,
                "failed",
                str(сбой),
            )
            session.commit()
        logger.error("запись в календарь не выполнена: %s", сбой)
        return 1

    for строка in описать(отчёт, apply):
        logger.info("%s", строка)
    logger.info("запись в календарь завершена%s", "" if apply else " (dry-run)")
    # Отказ по отдельным событиям не отменяет остальную работу, но джоб
    # обязан быть заметен позже: смотреть на него в момент запуска некому.
    return 1 if отчёт.failed else 0


def run(settings: Settings, apply: bool, now: dt.datetime | None = None) -> int:
    """Прогон целиком: свой сервис Google, своя сессия, свой код возврата."""
    момент = now or dt.datetime.now(dt.UTC)
    with get_sessionmaker()() as session:
        try:
            client = GcalClient(settings, build_service(settings))
        except GcalError as сбой:
            logger.error("запись в календарь не выполнена: %s", сбой)
            return 1
        return run_once(session, settings, client, apply=apply, now=момент)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага наружу не уходит ничего."""
    parser = argparse.ArgumentParser(description="Запись расписания JARVIS в Google Calendar")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать в календарь (без флага - только показать дифф)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

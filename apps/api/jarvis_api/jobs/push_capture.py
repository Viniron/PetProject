"""Джоб записи подтверждённых событий захвата в `JARVIS · События` (Э8).

Продолжение `push_gcal` там, где у того кончается предметная область:
тот ведёт календарь расписания по зеркалу портала, этот - календарь
событий по решениям owner. Общее у них только транспорт.

**Очередь, а не реконсил по окну.** У расписания есть внешний источник
истины, с которым можно сверяться целиком; у события захвата источник
истины - строка `calendar_events`, созданная подтверждением. Поэтому здесь
берётся не окно дат, а очередь: строки `pending` и `failed`. Событие,
подтверждённое на прошлый месяц, уедет в календарь так же, как завтрашнее.

**Перед созданием спрашиваем Google.** Между `insert` и коммитом журнала
помещается перезагрузка платы, и без этого вопроса следующий прогон
создал бы второе такое же событие. Ключ события детерминирован (ADR-042),
он лежит в приватных свойствах, и по нему уже записанное находится.

**Та же функция доставки зовётся из API.** Подтверждение не ждёт слота
планировщика: эндпоинт пробует записать событие сразу, коротким таймаутом
и без повторов. Не вышло - строка осталась `pending`, и её заберёт
ближайший прогон этого джоба; экран показывает «записывается», а не
обещает готовое (инвариант 9).
"""

import argparse
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.models import AuditLogEntry, CalendarEvent, Setting
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain.capture import ИСТОЧНИК
from jarvis_api.integrations.gcal.client import (
    GcalClient,
    GcalError,
    GcalItemError,
    build_service,
)
from jarvis_api.integrations.gcal.mapping import DesiredEvent, event_to_desired
from jarvis_api.jobs.common import OwnerZoneError, зона_без_падения, отметить_прогон

logger = logging.getLogger("jarvis.push_capture")

JOB_NAME = "push_capture"

# Запас вокруг события при поиске его в Google. Ровные границы событию
# хватило бы, но выдача Google по `timeMin`/`timeMax` - это пересечение
# интервалов, и сутки запаса стоят одного лишнего события в ответе,
# тогда как их отсутствие стоило бы дубля в календаре owner.
ЗАПАС_ПОИСКА = dt.timedelta(days=1)


class PushCaptureError(RuntimeError):
    """Запись событий захвата не выполнена."""


@dataclass(slots=True)
class CaptureReport:
    """Что джоб сделал бы или сделал."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    # Ключ и причина: в логе должно быть видно, какое именно событие
    # не уехало, - иначе разбираться придётся сравнением с календарём глазами.
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def есть_изменения(self) -> bool:
        return bool(self.created or self.updated)


def календарь_событий(session: Session) -> str:
    """id календаря `JARVIS · События` из настроек.

    Пусто - отказ с указанием команды, а не создание на лету: календарь
    создаётся и расшаривается осознанно, один раз (`make gcal-setup-apply`).
    """
    строка = session.get(Setting, 1)
    if строка is None or not строка.gcal_events_id:
        raise PushCaptureError(
            "календарь JARVIS · События не создан: в settings пуст gcal_events_id. "
            "Заведите календари командой `make gcal-setup-apply`"
        )
    return строка.gcal_events_id


def очередь(session: Session) -> list[CalendarEvent]:
    """События захвата, которых в календаре ещё нет или запись которых сорвалась.

    `synced` не трогаем: правки событий в календарном релизе нет (решение
    owner, 2026-09-17), и перезапись уже записанного была бы работой без
    причины - с квотой Google и риском затереть то, что owner поправил
    в самом календаре.
    """
    запрос = (
        select(CalendarEvent)
        .where(CalendarEvent.source == ИСТОЧНИК, CalendarEvent.sync_state != "synced")
        .order_by(CalendarEvent.starts_at)
    )
    return list(session.scalars(запрос))


def _записать_в_аудит(
    session: Session, *, actor: str, status: str, ключ: str, detail: dict[str, object]
) -> None:
    """След каждой записи в календарь - инвариант 8, без исключений."""
    session.add(
        AuditLogEntry(
            kind="calendar_write",
            actor=actor,
            status=status,
            target=ключ,
            provider="google",
            detail=detail,
        )
    )


def _найти_в_google(client: GcalClient, calendar_id: str, событие: DesiredEvent) -> str | None:
    """id события в Google по нашему ключу, если оно там уже лежит."""
    найденные = client.list_window(
        calendar_id,
        событие.starts_at - ЗАПАС_ПОИСКА,
        событие.ends_at + ЗАПАС_ПОИСКА,
    )
    return найденные.get(событие.external_key)


def доставить(
    session: Session,
    client: GcalClient,
    calendar_id: str,
    строка: CalendarEvent,
    *,
    now: dt.datetime,
    actor: str = JOB_NAME,
) -> str:
    """Одно событие в Google и отметка об этом в журнале. Коммит - на вызывающем.

    Возвращает вид выполненного действия: `create` или `update`. При отказе
    не пишет в базу ничего и пробрасывает исключение: как пометить строку,
    решает вызывающий - джоб помечает `failed`, эндпоинт оставляет `pending`
    и молча отдаёт событие в очередь.

    Порядок неделим и важен: сначала Google, потом журнал. Обратный оставил
    бы в журнале `synced` для события, которого в календаре нет.
    """
    желаемое = event_to_desired(строка)
    идентификатор = строка.google_event_id or _найти_в_google(client, calendar_id, желаемое)
    if идентификатор is None:
        идентификатор = client.insert(calendar_id, желаемое.body())
        вид = "create"
    else:
        client.update(calendar_id, идентификатор, желаемое.body())
        вид = "update"

    строка.content_hash = желаемое.content_hash()
    строка.google_event_id = идентификатор
    строка.sync_state = "synced"
    строка.last_error = None
    строка.synced_at = now
    _записать_в_аудит(
        session, actor=actor, status="ok", ключ=строка.external_key, detail={"action": вид}
    )
    return вид


def отправить_сразу(
    session: Session, settings: Settings, строка: CalendarEvent, *, now: dt.datetime
) -> bool:
    """Попытка записать только что подтверждённое событие, из эндпоинта (ADR-042).

    Все отказы гасятся: запись в Google - не условие подтверждения, событие
    уже принято и лежит в базе. Провалившаяся попытка оставляет строку
    `pending`, и её заберёт ближайший прогон джоба.

    Клиент строится на укороченных настройках: одна попытка вместо трёх
    и свой таймаут. Обычные `gcal_retries` с `http_timeout_seconds` в худшем
    случае держали бы запрос экрана две минуты - §10 запрещает именно это.
    """
    короткие = settings.model_copy(
        update={
            "gcal_retries": 0,
            "http_timeout_seconds": settings.capture_push_timeout_seconds,
        }
    )
    try:
        client = GcalClient(короткие, build_service(короткие))
        calendar_id = календарь_событий(session)
        доставить(session, client, calendar_id, строка, now=now, actor="api.capture")
    except (GcalError, GcalItemError, PushCaptureError) as сбой:
        # Не `logger.exception`: это штатная деградация, а не сбой сервера.
        # Громко падает джоб, интерфейс деградирует тихо (инвариант 9).
        logger.warning("событие %s осталось в очереди: %s", строка.external_key, сбой)
        return False
    return True


def описать(отчёт: CaptureReport, apply: bool) -> list[str]:
    """Человекочитаемый дифф. Отдельной функцией, чтобы его проверял тест."""
    строки = [
        f"создано {len(отчёт.created)}, обновлено {len(отчёт.updated)}, "
        f"с ошибкой {len(отчёт.failed)}"
    ]
    for ключ in отчёт.created:
        строки.append(f"  + {ключ}")
    for ключ in отчёт.updated:
        строки.append(f"  ~ {ключ}")
    for ключ, причина in отчёт.failed:
        строки.append(f"  ! {ключ}: {причина}")
    if not apply:
        строки.append("dry-run: в Google и в базу не записано ничего")
    return строки


def push(
    session: Session,
    settings: Settings,
    client: GcalClient | None,
    *,
    apply: bool,
    now: dt.datetime,
) -> CaptureReport:
    """Один прогон очереди.

    Клиент необязателен, и это не послабление: в dry-run Google не
    спрашивается вовсе, а очередь растёт как раз тогда, когда с ключом
    что-то не так. Джоб, отказывающийся показать очередь без ключа, был бы
    бесполезен ровно в тот день, когда очередь надо посмотреть.
    """
    отчёт = CaptureReport()
    строки = очередь(session)
    if not строки:
        # Пустая очередь - обычное состояние: событие уезжает сразу при
        # подтверждении (ADR-042), и джобу достаётся только недошедшее.
        # Отметка о прогоне всё равно ставится: без неё «очередь была пуста»
        # неотличимо от «джоб не запускался», а это разные поломки.
        if apply:
            отметить_прогон(
                session, JOB_NAME, now.astimezone(зона_без_падения(session)).date(), now, "ok"
            )
            session.commit()
        return отчёт

    if not apply:
        # В dry-run Google не спрашиваем вовсе: единственный вопрос к нему -
        # «нет ли там уже этого события», и ответ ничего не меняет, пока
        # ничего не пишется. Зато лишний поход наружу из dry-run сделал бы
        # проверку перед живым прогоном зависящей от доступности Google.
        отчёт.created.extend(строка.external_key for строка in строки)
        return отчёт

    if client is None:
        raise PushCaptureError("запись без клиента Google невозможна")
    calendar_id = календарь_событий(session)

    for строка in строки:
        # Вложенная транзакция на событие: `rollback()` всей сессии снял бы
        # и чужую незакоммиченную работу - ту же ошибку уже ловил push_gcal.
        точка = session.begin_nested()
        try:
            вид = доставить(session, client, calendar_id, строка, now=now)
            точка.commit()
            (отчёт.created if вид == "create" else отчёт.updated).append(строка.external_key)
        except GcalItemError as сбой:
            # Откат снимает полуприменённое состояние одного события;
            # пометка ставится уже после него, начисто.
            точка.rollback()
            строка.sync_state = "failed"
            строка.last_error = str(сбой)
            _записать_в_аудит(
                session,
                actor=JOB_NAME,
                status="error",
                ключ=строка.external_key,
                detail={"action": "create", "error": str(сбой)},
            )
            отчёт.failed.append((строка.external_key, str(сбой)))
            logger.error("событие %s: %s", строка.external_key, сбой)
        session.commit()

    отметить_прогон(
        session,
        JOB_NAME,
        now.astimezone(зона_без_падения(session)).date(),
        now,
        "failed" if отчёт.failed else "ok",
        f"{len(отчёт.failed)} событий не записано" if отчёт.failed else None,
    )
    session.commit()
    return отчёт


def run_once(
    session: Session,
    settings: Settings,
    client: GcalClient | None,
    *,
    apply: bool,
    now: dt.datetime,
) -> int:
    """Прогон поверх готовой сессии и клиента. Возвращает код возврата процесса."""
    try:
        отчёт = push(session, settings, client, apply=apply, now=now)
    except (PushCaptureError, OwnerZoneError, GcalError) as сбой:
        session.rollback()
        if apply:
            _записать_в_аудит(
                session, actor=JOB_NAME, status="error", ключ=JOB_NAME, detail={"error": str(сбой)}
            )
            отметить_прогон(
                session,
                JOB_NAME,
                now.astimezone(зона_без_падения(session)).date(),
                now,
                "failed",
                str(сбой),
            )
            session.commit()
        logger.error("запись событий захвата не выполнена: %s", сбой)
        return 1

    for строка in описать(отчёт, apply):
        logger.info("%s", строка)
    logger.info("запись событий захвата завершена%s", "" if apply else " (dry-run)")
    return 1 if отчёт.failed else 0


def run(settings: Settings, apply: bool, now: dt.datetime | None = None) -> int:
    """Прогон целиком: свой сервис Google, своя сессия, свой код возврата."""
    момент = now or dt.datetime.now(dt.UTC)
    with get_sessionmaker()() as session:
        client: GcalClient | None = None
        try:
            client = GcalClient(settings, build_service(settings))
        except GcalError as сбой:
            # В dry-run это не отказ: клиент там не нужен, а показать очередь
            # без ключа - именно то, ради чего цель без --apply существует.
            if apply:
                logger.error("запись событий захвата не выполнена: %s", сбой)
                return 1
            logger.warning("клиент Google не построен, dry-run покажет только очередь: %s", сбой)
        return run_once(session, settings, client, apply=apply, now=момент)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага наружу не уходит ничего."""
    parser = argparse.ArgumentParser(
        description="Запись подтверждённых событий захвата в Google Calendar"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать в календарь (без флага - только показать очередь)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

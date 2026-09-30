"""Джоб воскресного напоминания о выписках (Ф8, §15.7, ADR-030 п. 6, ADR-053).

Ставит событие «Загрузить выписки» в `JARVIS · События`, если на этой
неделе загружены не все банки. Кому и когда - решает
`domain/finance_reminder.py`, здесь только журнал и доставка.

**Отдельно от цепочки календаря** (ADR-053). Цепочка сигналит сторожу
«расписание доехало» и при неудаче догоняется при рестарте - с походом
в ИСУ. Отказ напоминания гасил бы первое и вызывал второе, хотя ни к
расписанию, ни к порталу отношения не имеет.

**Запускается на каждом воскресном слоте, а не одном.** После первой удачной
записи остальные прогоны только читают базу, а до неё каждый следующий -
это повтор после отказа Google или догон после простоя платы, и отдельной
логики для обоих не нужно.

**Доставка - та же, что у захвата** (`push_capture.доставить`): перед
созданием событие ищется в Google по ключу, иначе перезагрузка между
`insert` и коммитом журнала дала бы в следующем прогоне второе событие.

**Решение, принятое однажды, не пересматривается.** Событие в календаре
уже есть - выписку, загруженную после этого, джоб не отслеживает и событие
не трогает: правки событий в релизе нет, а перезапись затирала бы то, что
owner поправил в самом Google. Пересматривается только то, что до Google
не доехало: такой строке время и текст пересчитываются на каждом прогоне,
а ставшая ненужной (выписку успели загрузить) удаляется из журнала.
"""

import argparse
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings, разобрать_слоты
from jarvis_api.db.models import AuditLogEntry, CalendarEvent
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain import finance_reminder
from jarvis_api.integrations.gcal.client import (
    GcalClient,
    GcalError,
    GcalItemError,
    build_service,
)
from jarvis_api.integrations.gcal.mapping import SOURCE_FINANCE
from jarvis_api.jobs.common import (
    OwnerZoneError,
    owner_timezone,
    зона_без_падения,
    отметить_прогон,
)
from jarvis_api.jobs.push_capture import PushCaptureError, доставить, календарь_событий

logger = logging.getLogger("jarvis.finance_remind")

JOB_NAME = "finance_reminder"


@dataclass(frozen=True, slots=True)
class Отчёт:
    """Что джоб сделал бы или сделал. `действие` - для теста и лога."""

    # none - ничего; create/update - событие записано в Google;
    # drop - недоехавшая строка удалена из журнала; failed - запись сорвалась.
    действие: str
    причина: str
    ключ: str | None = None
    начало: dt.datetime | None = None
    # Текст события - в отчёт, чтобы dry-run показывал ровно то, что уедет
    # в календарь: запись наружу идёт только после ревью диффа (CLAUDE.md).
    описание: str | None = None


def _записать_в_аудит(
    session: Session, *, status: str, ключ: str, detail: dict[str, object]
) -> None:
    """След каждой попытки записи в календарь (инвариант 8)."""
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


def _строка(session: Session, напоминание: finance_reminder.Напоминание) -> CalendarEvent:
    """Строка журнала под напоминание: новая или недоехавшая прежняя.

    Отпечаток пуст, как у подтверждённого захвата до записи: он означает
    «в Google ещё ничего не отправляли», а считает его доставка.
    """
    строка = session.get(CalendarEvent, напоминание.ключ)
    if строка is None:
        строка = CalendarEvent(
            external_key=напоминание.ключ,
            calendar="events",
            source=SOURCE_FINANCE,
            content_hash="",
            sync_state="pending",
        )
        session.add(строка)
    строка.title = finance_reminder.ЗАГОЛОВОК
    строка.starts_at = напоминание.начало
    строка.ends_at = напоминание.конец
    строка.location = None
    строка.description = напоминание.описание
    return строка


def напомнить(
    session: Session,
    settings: Settings,
    client: GcalClient | None,
    *,
    apply: bool,
    now: dt.datetime,
) -> Отчёт:
    """Один прогон. Коммитит сам: вызывающему остаётся код возврата."""
    зона = owner_timezone(session)
    сегодня = now.astimezone(зона).date()
    решение = finance_reminder.решить(
        finance_reminder.последние_загрузки(session),
        сейчас=now,
        зона=зона,
        время=разобрать_слоты(settings.finance_reminder_at)[0],
        минут=settings.finance_reminder_minutes,
        шаг_минут=settings.finance_reminder_round_minutes,
    )
    if сегодня.weekday() != finance_reminder.ВОСКРЕСЕНЬЕ:
        # Отметки о прогоне в будни нет: джоб зовётся при каждом старте
        # процесса, и строка `job_runs` на каждый будний день говорила бы
        # «напоминание отработало» там, где работать было нечему.
        return Отчёт("none", решение.причина)

    ключ = finance_reminder.ключ_недели(сегодня)
    прежняя = session.get(CalendarEvent, ключ)
    if прежняя is not None and прежняя.sync_state == "synced":
        отчёт = Отчёт("none", "напоминание этой недели уже в календаре", ключ, прежняя.starts_at)
    elif решение.напоминание is None:
        отчёт = Отчёт("drop" if прежняя is not None else "none", решение.причина, ключ)
        if прежняя is not None and apply:
            # До Google строка не доехала (иначе была бы `synced`), и
            # напоминание больше не нужно: выписки успели загрузить или
            # воскресенье кончилось. В очереди ей делать нечего.
            session.delete(прежняя)
    elif not apply:
        # В dry-run Google не спрашиваем вовсе - по той же причине, что
        # у захвата: проверка перед живым прогоном не должна зависеть
        # от доступности Google.
        отчёт = Отчёт(
            "create",
            решение.причина,
            ключ,
            решение.напоминание.начало,
            решение.напоминание.описание,
        )
    else:
        отчёт = _доставить(session, client, решение.напоминание, решение.причина, now=now)

    if apply:
        отметить_прогон(
            session,
            JOB_NAME,
            сегодня,
            now,
            "failed" if отчёт.действие == "failed" else "ok",
            отчёт.причина if отчёт.действие == "failed" else None,
        )
        session.commit()
    return отчёт


def _доставить(
    session: Session,
    client: GcalClient | None,
    напоминание: finance_reminder.Напоминание,
    причина: str,
    *,
    now: dt.datetime,
) -> Отчёт:
    """Строка в журнал, событие в Google. Отказ помечает строку `failed`."""
    if client is None:
        raise PushCaptureError("запись без клиента Google невозможна")
    calendar_id = календарь_событий(session)
    строка = _строка(session, напоминание)
    session.flush()

    # Вложенная транзакция: откат снимает только полуприменённую доставку,
    # а сама строка журнала остаётся - с пометкой, по которой следующий
    # слот попробует снова.
    точка = session.begin_nested()
    try:
        вид = доставить(session, client, calendar_id, строка, now=now, actor=JOB_NAME)
        точка.commit()
    except (GcalError, GcalItemError) as сбой:
        точка.rollback()
        строка.sync_state = "failed"
        строка.last_error = str(сбой)
        _записать_в_аудит(
            session,
            status="error",
            ключ=напоминание.ключ,
            detail={"action": "create", "error": str(сбой)},
        )
        logger.error("напоминание %s не записано: %s", напоминание.ключ, сбой)
        return Отчёт("failed", str(сбой), напоминание.ключ, напоминание.начало)
    return Отчёт(вид, причина, напоминание.ключ, напоминание.начало, напоминание.описание)


def описать(отчёт: Отчёт, apply: bool, session: Session) -> list[str]:
    """Человекочитаемый итог. Время - по зоне owner: так его увидит календарь."""
    строки = [f"напоминание о выписках: {отчёт.причина}"]
    if отчёт.ключ is not None and отчёт.начало is not None:
        местное = отчёт.начало.astimezone(зона_без_падения(session))
        строки.append(f"  {отчёт.действие} {отчёт.ключ} на {местное:%d.%m.%Y %H:%M}")
        for строка in (отчёт.описание or "").splitlines():
            строки.append(f"    | {строка}")
    elif отчёт.действие == "drop":
        строки.append(f"  drop {отчёт.ключ}: недоехавшая строка журнала убрана")
    if not apply:
        строки.append("dry-run: в Google и в базу не записано ничего")
    return строки


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
        отчёт = напомнить(session, settings, client, apply=apply, now=now)
    except (PushCaptureError, OwnerZoneError, GcalError) as сбой:
        # Джоб фоновый и падает громко (§15.6, инвариант 9): след в базе
        # и ненулевой код, а не тишина до следующего воскресенья.
        session.rollback()
        if apply:
            _записать_в_аудит(session, status="error", ключ=JOB_NAME, detail={"error": str(сбой)})
            отметить_прогон(
                session,
                JOB_NAME,
                now.astimezone(зона_без_падения(session)).date(),
                now,
                "failed",
                str(сбой),
            )
            session.commit()
        logger.error("напоминание о выписках не выполнено: %s", сбой)
        return 1

    for строка in описать(отчёт, apply, session):
        logger.info("%s", строка)
    return 1 if отчёт.действие == "failed" else 0


def run(settings: Settings, apply: bool, now: dt.datetime | None = None) -> int:
    """Прогон целиком: свой сервис Google, своя сессия, свой код возврата.

    Точка входа планировщика, догона при старте и цели `make finance-remind`.
    """
    момент = now or dt.datetime.now(dt.UTC)
    with get_sessionmaker()() as session:
        client: GcalClient | None = None
        try:
            client = GcalClient(settings, build_service(settings))
        except GcalError as сбой:
            # В dry-run не отказ: клиент там не нужен. При apply отказ
            # случится внутри, когда он понадобится, - и только если
            # событие действительно нужно ставить: в будни и при загруженных
            # выписках битый ключ Google напоминанию не мешает.
            if apply:
                logger.warning("клиент Google не построен: %s", сбой)
            else:
                logger.warning(
                    "клиент Google не построен, dry-run покажет только решение: %s", сбой
                )
        return run_once(session, settings, client, apply=apply, now=момент)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага наружу не уходит ничего."""
    parser = argparse.ArgumentParser(
        description="Воскресное напоминание о выписках в Google Calendar"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать событие в календарь (без флага - только показать решение)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

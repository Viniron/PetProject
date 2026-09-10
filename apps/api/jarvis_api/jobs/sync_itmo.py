"""Джоб забора расписания: my.itmo.ru → зеркало `itmo_lessons`.

Записи в Google здесь нет - она принадлежит Э4. Этот джоб доводит до базы
желаемое состояние: что портал считает расписанием на ближайшие недели.

**Окно заменяется целиком.** Пара, исчезнувшая из расписания портала,
обязана исчезнуть и из зеркала - иначе reconcile Э4 никогда не удалит её
из календаря, и отменённая лекция будет звонить в телефон до конца семестра.
Отсюда же граница транзакции: удаление и вставка - одна операция (§11.2),
потому что состояние «старое стёрли, новое не записали» означает пустой
календарь на день.

**Отказ портала зеркало не трогает.** Ни строки не удаляется, `fetched_at`
остаётся прежним - и именно он даёт экрану честную пометку давности
(инвариант 9, `SPEC.md` §10). Молча подставить пустое расписание было бы
той самой «правдоподобной выдумкой», которая запрещена прямо.

Джоб при этом падает громко: ненулевой код возврата и запись в `audit_log`.
Смотреть на него в момент запуска некому, значит отказ обязан быть заметен
позже.
"""

import argparse
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx2
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.crypto import SecretCipherError, build_cipher
from jarvis_api.db.models import AuditLogEntry, ItmoLesson
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.integrations.itmo.auth import ItmoAuth, ItmoAuthError
from jarvis_api.integrations.itmo.client import ItmoApiError, ItmoClient, build_client
from jarvis_api.integrations.itmo.mapping import MappingError, MirrorRow, payload_to_rows
from jarvis_api.integrations.itmo.store import CredentialStore
from jarvis_api.jobs.common import (
    OwnerZoneError,
    owner_timezone,
    sync_window,
    зона_без_падения,
    отметить_прогон,
)

logger = logging.getLogger("jarvis.sync_itmo")

# Имя джоба. Одно и то же в `job_runs` и в `audit_log`: по нему Э5 будет
# искать след вчерашнего прогона, решая, нужен ли догоняющий запуск (§11.2).
JOB_NAME = "sync_itmo"


class SyncError(RuntimeError):
    """Забор не выполнен. Обёртка над причинами, чтобы CLI ловил одно."""


@dataclass(slots=True)
class SyncReport:
    """Что забор сделал бы или сделал. Печатается и в dry-run, и в --apply."""

    date_start: dt.date
    date_end: dt.date
    fetched_at: dt.datetime
    added: list[MirrorRow] = field(default_factory=list)
    changed: list[MirrorRow] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def всего_в_ответе(self) -> int:
        return len(self.added) + len(self.changed) + self.unchanged

    @property
    def есть_изменения(self) -> bool:
        return bool(self.added or self.changed or self.removed)


def _строка_зеркала(row: ItmoLesson) -> MirrorRow:
    """ORM-строка в то же представление, в каком приходит разобранный ответ.

    Нужно ровно для сравнения: сопоставлять поле за полем в двух местах -
    надёжный способ забыть одно из них при следующем изменении схемы.
    """
    return MirrorRow(
        source_key=row.source_key,
        lesson_date=row.lesson_date,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        subject=row.subject,
        kind=row.kind,
        teacher=row.teacher,
        room=row.room,
        building=row.building,
        mode=row.mode,
        online_url=row.online_url,
    )


def diff(
    существующие: dict[str, MirrorRow],
    свежие: Sequence[MirrorRow],
    date_start: dt.date,
    date_end: dt.date,
    fetched_at: dt.datetime,
) -> SyncReport:
    """Сравнение зеркала с ответом портала. Ничего не пишет.

    Вынесено отдельно от записи, потому что это единственная часть джоба,
    которую есть смысл проверять во всех углах: пара появилась, пара
    изменилась, пара исчезла, ничего не изменилось.
    """
    отчёт = SyncReport(date_start=date_start, date_end=date_end, fetched_at=fetched_at)

    for строка in свежие:
        прежняя = существующие.get(строка.source_key)
        if прежняя is None:
            отчёт.added.append(строка)
        elif прежняя != строка:
            отчёт.changed.append(строка)
        else:
            отчёт.unchanged += 1

    пришедшие = {строка.source_key for строка in свежие}
    отчёт.removed = sorted(ключ for ключ in существующие if ключ not in пришедшие)
    return отчёт


def _прочитать_зеркало(
    session: Session, date_start: dt.date, date_end: dt.date
) -> dict[str, MirrorRow]:
    """Строки зеркала внутри окна. За пределами окна не трогаем ничего."""
    запрос = select(ItmoLesson).where(
        ItmoLesson.lesson_date >= date_start,
        ItmoLesson.lesson_date <= date_end,
    )
    return {строка.source_key: _строка_зеркала(строка) for строка in session.scalars(запрос)}


def _применить(session: Session, отчёт: SyncReport, свежие: Sequence[MirrorRow]) -> None:
    """Приводит зеркало к тому, что отдал портал. Внутри одной транзакции.

    `fetched_at` обновляется у всех строк окна, включая неизменившиеся: он
    отвечает на вопрос «насколько свежи эти данные», а не «когда эта пара
    менялась в последний раз». Пара, не менявшаяся месяц, всё равно
    подтверждена сегодняшним ответом портала.
    """
    for ключ in отчёт.removed:
        строка = session.get(ItmoLesson, ключ)
        if строка is not None:
            session.delete(строка)
    # Удаления доводятся до базы до вставок: иначе пара, сменившая ключ
    # в пределах того же дня, столкнулась бы с уникальностью первичного ключа
    # ещё до того, как старая строка исчезнет.
    session.flush()

    for новая in свежие:
        строка = session.get(ItmoLesson, новая.source_key)
        if строка is None:
            строка = ItmoLesson(source_key=новая.source_key)
            session.add(строка)
        строка.lesson_date = новая.lesson_date
        строка.starts_at = новая.starts_at
        строка.ends_at = новая.ends_at
        строка.subject = новая.subject
        строка.kind = новая.kind
        строка.teacher = новая.teacher
        строка.room = новая.room
        строка.building = новая.building
        строка.mode = новая.mode
        строка.online_url = новая.online_url
        строка.fetched_at = отчёт.fetched_at


def _записать_в_аудит(
    session: Session,
    status: str,
    detail: dict[str, object],
) -> None:
    """След в `audit_log`. Без него «почему расписание трёхдневной давности»
    остаётся вопросом без ответа (§10)."""
    session.add(
        AuditLogEntry(
            kind="itmo_fetch",
            actor=JOB_NAME,
            status=status,
            provider="my.itmo.ru",
            detail=detail,
        )
    )


def описать(отчёт: SyncReport, apply: bool) -> list[str]:
    """Человекочитаемый дифф. Отдельной функцией, чтобы его проверял тест."""
    строки = [
        f"окно {отчёт.date_start.isoformat()} .. {отчёт.date_end.isoformat()}: "
        f"портал отдал {отчёт.всего_в_ответе} пар",
        f"новых {len(отчёт.added)}, изменённых {len(отчёт.changed)}, "
        f"исчезнувших {len(отчёт.removed)}, без изменений {отчёт.unchanged}",
    ]
    for строка in отчёт.added:
        строки.append(f"  + {строка.lesson_date} {строка.starts_at:%H:%M}Z {строка.subject}")
    for строка in отчёт.changed:
        строки.append(f"  ~ {строка.lesson_date} {строка.starts_at:%H:%M}Z {строка.subject}")
    for ключ in отчёт.removed:
        строки.append(f"  - {ключ}")
    if not apply:
        строки.append("dry-run: в базу не записано ничего")
    return строки


def sync(
    session: Session,
    settings: Settings,
    http: httpx2.Client,
    *,
    apply: bool,
    now: dt.datetime,
) -> SyncReport:
    """Один забор. Транзакцию коммитит вызывающий код, а не эта функция."""
    зона = owner_timezone(session)
    сегодня = now.astimezone(зона).date()
    начало, конец = sync_window(settings, сегодня)

    хранилище = CredentialStore(session, build_cipher(settings))
    клиент = ItmoClient(settings, http, ItmoAuth(settings, http, хранилище))

    ответ = клиент.schedule(начало, конец, now)
    свежие = payload_to_rows(ответ, зона)
    отчёт = diff(_прочитать_зеркало(session, начало, конец), свежие, начало, конец, now)

    if apply:
        _применить(session, отчёт, свежие)
        _записать_в_аудит(
            session,
            "ok",
            {
                "window": [начало.isoformat(), конец.isoformat()],
                "added": len(отчёт.added),
                "changed": len(отчёт.changed),
                "removed": len(отчёт.removed),
                "unchanged": отчёт.unchanged,
            },
        )
        отметить_прогон(session, JOB_NAME, сегодня, now, "ok")

    return отчёт


def run_once(
    session: Session,
    settings: Settings,
    http: httpx2.Client,
    *,
    apply: bool,
    now: dt.datetime,
) -> int:
    """Прогон поверх готовой сессии. Возвращает код возврата процесса.

    Отделено от `run` не ради слоёв, а ради проверяемости: тесту нужна
    своя сессия и подставной транспорт, и обработка отказа - ровно та часть,
    которую иначе пришлось бы проверять глазами на живом портале.
    """
    try:
        отчёт = sync(session, settings, http, apply=apply, now=now)
    except (
        SyncError,
        # Неизвестная зона в настройках - отказ общего модуля, а не забора:
        # читают её все джобы, ловить обязан каждый.
        OwnerZoneError,
        ItmoAuthError,
        ItmoApiError,
        MappingError,
        SecretCipherError,
    ) as сбой:
        # Откат до записи следа: иначе полуприменённое окно уедет в базу
        # вместе с отметкой об отказе, и зеркало окажется наполовину новым.
        session.rollback()
        if apply:
            зона_отказа = зона_без_падения(session)
            _записать_в_аудит(session, "error", {"error": str(сбой)})
            отметить_прогон(
                session,
                JOB_NAME,
                now.astimezone(зона_отказа).date(),
                now,
                "failed",
                str(сбой),
            )
            session.commit()
        logger.error("забор расписания не выполнен: %s", сбой)
        return 1

    # Коммит нужен в обоих режимах, и в dry-run он коммитит ровно одно -
    # токены, добытые входом. Зеркала, аудита и отметки прогона в сессии нет:
    # без `apply` их никто не создавал (см. `sync`). Выбросить же токены
    # означало бы входить паролем на каждый ручной прогон - то есть ровно то,
    # чего мы избегаем приоритетом refresh-токена.
    session.commit()

    for строка in описать(отчёт, apply):
        logger.info("%s", строка)
    logger.info("забор расписания завершён%s", "" if apply else " (dry-run)")
    return 0


def run(settings: Settings, apply: bool, now: dt.datetime | None = None) -> int:
    """Прогон целиком: свой HTTP-клиент, своя сессия, свой код возврата."""
    момент = now or dt.datetime.now(dt.UTC)
    with build_client(settings) as клиент, get_sessionmaker()() as session:
        return run_once(session, settings, клиент, apply=apply, now=момент)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага в базу не пишется ничего."""
    parser = argparse.ArgumentParser(description="Забор расписания my.itmo.ru в зеркало JARVIS")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать зеркало (без флага - только показать дифф)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

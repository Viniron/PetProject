"""Цепочка ежедневных джобов календаря и решение о догоняющем запуске.

`SPEC.md` §11.2 требует, чтобы у обоих триггеров - планировщика и catch-up
при старте - был **один и тот же runner**. Это он: планировщик зовёт `run`,
догон зовёт `догнать`, а работу в обоих случаях делает `выполнить_цепочку`.

**Почему цепочка, а не два независимых джоба.** Запись в Google по зеркалу,
которое забор не обновил, идёт по вчерашним данным, поэтому шаги связаны
порядком и догоняются вместе. Отсюда отступление от буквы §11.2 («сверяет
`last_run_at` каждого ежедневного джоба»): решение о догоне принимается
на уровне цепочки. Развилка записана в ADR-027.

**Своя строка `job_runs` под именем `daily_calendar`.** Шаги пишут свои,
но есть отказы, не принадлежащие ни одному шагу: сломанная зона в настройках,
обрыв базы между шагами, отказ построителя транспорта. Без строки цепочки
такой отказ неотличим от «процесс вообще не стартовал», а §10 требует, чтобы
фоновый отказ был заметен позже.

**Отказ шага не отменяет следующий.** Портал не ответил - зеркало осталось
прежним, и реконсил Google по нему всё равно полезен: он чинит событие,
удалённое в календаре руками. Код цепочки при этом ненулевой.
"""

import argparse
import dataclasses
import datetime as dt
import logging
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings, разобрать_слоты
from jarvis_api.db.models import AuditLogEntry, JobRun
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.integrations.gcal.client import GcalClient, GcalError, build_service
from jarvis_api.integrations.itmo.client import build_client
from jarvis_api.jobs import push_gcal, sync_itmo
from jarvis_api.jobs.common import зона_без_падения, начать_прогон, отметить_прогон

logger = logging.getLogger("jarvis.runner")

JOB_NAME = "daily_calendar"


@dataclasses.dataclass(frozen=True, slots=True)
class Шаг:
    """Один джоб внутри цепочки."""

    имя: str
    # Сигнатура повторяет `run_once` джобов, включая `apply`: цепочка обязана
    # уметь dry-run, иначе её нечем проверить перед первым живым прогоном.
    выполнить: Callable[[Session, Settings, bool, dt.datetime], int]


@dataclasses.dataclass(frozen=True, slots=True)
class Решение:
    """Нужен ли догоняющий запуск и почему. Причина - для лога, не для кода."""

    нужен: bool
    причина: str


def _забрать_расписание(session: Session, settings: Settings, apply: bool, now: dt.datetime) -> int:
    """Шаг 1: my.itmo.ru -> зеркало. Тонкая обёртка над джобом Э3."""
    with build_client(settings) as клиент:
        return sync_itmo.run_once(session, settings, клиент, apply=apply, now=now)


def _записать_в_календарь(
    session: Session, settings: Settings, apply: bool, now: dt.datetime
) -> int:
    """Шаг 2: зеркало -> Google Calendar. Тонкая обёртка над джобом Э4."""
    try:
        клиент = GcalClient(settings, build_service(settings))
    except GcalError as сбой:
        # Ключа нет или он битый - это отказ шага, а не цепочки: первый шаг
        # к этому моменту уже обновил зеркало, и терять его результат незачем.
        logger.error("запись в календарь не выполнена: %s", сбой)
        return 1
    return push_gcal.run_once(session, settings, клиент, apply=apply, now=now)


# Порядок значим: реконсил по зеркалу, которое ещё не обновили, уедет
# по вчерашним данным. Проверяется тестом, а не только этим комментарием.
ЦЕПОЧКА_КАЛЕНДАРЯ: tuple[Шаг, ...] = (
    Шаг("sync_itmo", _забрать_расписание),
    Шаг("push_gcal", _записать_в_календарь),
)


def _записать_в_аудит(session: Session, status: str, detail: dict[str, object]) -> None:
    """След цепочки целиком. Шаги пишут о себе сами, это - о прогоне."""
    session.add(AuditLogEntry(kind="job_chain", actor=JOB_NAME, status=status, detail=detail))


def нужен_догоняющий(session: Session, settings: Settings, *, now: dt.datetime) -> Решение:
    """Решение о догоняющем запуске при старте процесса (§11.2).

    Три условия, все обязательны:

    1. **Первый слот сегодня уже прошёл.** Иначе догонять нечего: расписание
       само отработает через несколько минут.
    2. **Нет успешного прогона за сегодня.** Дата - по зоне owner, а не по UTC:
       «сегодня» - это день пользователя.
    3. **Прошлая попытка за сегодня остыла.** Защита не от параллельного
       прогона (процесс один, воркер один), а от цикла перезапуска: контейнер,
       который перезапускается каждые полминуты, без остывания ходил бы в ИСУ
       столько же раз, и портал за это банит.

    Вчерашний прогон не воскрешаем вовсе - граница наверстывания ADR-016:
    вчерашнее напоминание бессмысленно и мусорит в календаре.
    """
    зона = зона_без_падения(session)
    местное = now.astimezone(зона)
    первый_слот = разобрать_слоты(settings.scheduler_daily_times)[0]
    if местное.time() < первый_слот:
        return Решение(False, f"первый слот дня ({первый_слот:%H:%M}) ещё не наступил")

    сегодня = местное.date()
    строка = session.scalars(
        select(JobRun).where(JobRun.job == JOB_NAME, JobRun.run_date == сегодня)
    ).one_or_none()
    if строка is None:
        return Решение(True, "за сегодня прогонов не было")
    if строка.status == "ok":
        return Решение(False, "цепочка уже отработала сегодня успешно")

    # `finished_at` пуст у строки, оставленной убитым процессом: там берётся
    # время начала, иначе остывание для неё не считалось бы вовсе.
    последняя = строка.finished_at or строка.started_at
    остывание = dt.timedelta(minutes=settings.scheduler_catchup_cooldown_minutes)
    if последняя is not None and now - последняя < остывание:
        return Решение(
            False,
            f"прошлая попытка ({строка.status}) была меньше "
            f"{settings.scheduler_catchup_cooldown_minutes} минут назад",
        )
    return Решение(True, f"прошлая попытка за сегодня - {строка.status}")


def выполнить_цепочку(
    session: Session,
    settings: Settings,
    *,
    apply: bool,
    now: dt.datetime,
    шаги: Sequence[Шаг] = ЦЕПОЧКА_КАЛЕНДАРЯ,
) -> int:
    """Прогон цепочки поверх готовой сессии. Возвращает код возврата.

    Транзакции: у цепочки не должно быть ни одной висящей записи в момент
    вызова шага - шаг внутри себя и коммитит, и откатывает, и его `rollback()`
    снёс бы незакоммиченное чужое. Отсюда коммит сразу после отметки о начале
    и коммит в самом конце.
    """
    run_date = now.astimezone(зона_без_падения(session)).date()
    if apply:
        начать_прогон(session, JOB_NAME, run_date, now)
        session.commit()

    отказавшие: list[str] = []
    for шаг in шаги:
        logger.info("шаг %s", шаг.имя)
        try:
            код = шаг.выполнить(session, settings, apply, now)
        except Exception:
            # Ловим широко намеренно, и это не небрежность. Список исключений
            # шага заведомо неполон: `integrations/itmo/auth.py` не заворачивает
            # httpx2.RequestError, то есть обрыв сети при входе в Keycloak летит
            # мимо всех объявленных типов. Стреляет это ровно в догоняющем
            # запуске - он идёт при старте контейнера, когда сеть после
            # перезагрузки платы чаще всего ещё не поднялась. Потерять из-за
            # этого второй шаг и остаться со строкой `running` навсегда - хуже,
            # чем поймать чужой тип.
            #
            # rollback до записи следа: после ошибки уровня драйвера сессия
            # непригодна, и попытка записать отказ упала бы второй раз.
            session.rollback()
            logger.exception("шаг %s упал неожиданно", шаг.имя)
            код = 1
        if код != 0:
            отказавшие.append(шаг.имя)

    итог = 1 if отказавшие else 0
    if apply:
        причина = "отказ на шагах: " + ", ".join(отказавшие) if отказавшие else None
        try:
            _записать_в_аудит(
                session,
                "error" if отказавшие else "ok",
                {"steps": [шаг.имя for шаг in шаги], "failed": отказавшие},
            )
            отметить_прогон(
                session, JOB_NAME, run_date, now, "failed" if отказавшие else "ok", причина
            )
            session.commit()
        except Exception:
            # База отвалилась - писать след некуда. Остаётся лог: другого
            # места, где отказ станет заметен, у внутрипроцессного джоба нет.
            session.rollback()
            logger.exception("не удалось записать итог цепочки в базу")
            итог = 1

    logger.info(
        "цепочка завершена%s%s",
        "" if apply else " (dry-run)",
        f", отказали: {', '.join(отказавшие)}" if отказавшие else "",
    )
    return итог


def run(settings: Settings, apply: bool, now: dt.datetime | None = None) -> int:
    """Прогон целиком: своя сессия, свой код возврата.

    Точка входа планировщика и цели `make daily`. Догон зовёт `догнать`,
    а не эту функцию: у него перед прогоном есть ещё решение.
    """
    момент = now or dt.datetime.now(dt.UTC)
    with get_sessionmaker()() as session:
        return выполнить_цепочку(session, settings, apply=apply, now=момент)


def догнать(settings: Settings, now: dt.datetime | None = None) -> int:
    """Догоняющий запуск при старте процесса. Второй триггер из §11.2.

    Тот же runner, что у планировщика, - отличается только тем, что перед
    прогоном спрашивает, нужен ли он вообще.
    """
    момент = now or dt.datetime.now(dt.UTC)
    with get_sessionmaker()() as session:
        решение = нужен_догоняющий(session, settings, now=момент)
        if not решение.нужен:
            logger.info("догоняющий запуск не нужен: %s", решение.причина)
            return 0
        logger.info("догоняющий запуск: %s", решение.причина)
        return выполнить_цепочку(session, settings, apply=True, now=момент)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага наружу не уходит ничего."""
    parser = argparse.ArgumentParser(
        description="Ежедневная цепочка календаря: забор расписания и запись в Google"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="выполнить по-настоящему (без флага - только показать, что было бы)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

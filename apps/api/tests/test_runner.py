"""Цепочка джобов и решение о догоняющем запуске (Э5).

Шаги здесь подставные, и это не упрощение, а суть проверки: настоящие
`sync_itmo` и `push_gcal` покрыты своими модулями, а тут проверяется то,
что принадлежит именно цепочке, - порядок, изоляция отказа, следы в базе
и правило догона. Настоящая `ЦЕПОЧКА_КАЛЕНДАРЯ` проверяется одним тестом
на состав: подставные шаги о нём ничего не знают.

База настоящая: правило догона читает `job_runs`, а вся его тонкость -
в разнице `finished_at` и `started_at` и в дате по зоне owner.
"""

import datetime as dt
from collections.abc import Iterator

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry, ItmoLesson, JobRun, Setting
from jarvis_api.jobs.runner import (
    JOB_NAME,
    ЦЕПОЧКА_КАЛЕНДАРЯ,
    Шаг,
    выполнить_цепочку,
    нужен_догоняющий,
)

# Четверг, 10 сентября 2026, 12:00 по Москве - то есть после первого слота
# (06:30) и до последнего. Все проверки догона отсчитываются от него.
ПОЛДЕНЬ = dt.datetime(2026, 9, 10, 9, 0, tzinfo=dt.UTC)
СЕГОДНЯ = dt.date(2026, 9, 10)
ВЧЕРА = dt.date(2026, 9, 9)


def настройки(**переопределения: object) -> Settings:
    return Settings(**переопределения)  # type: ignore[arg-type]


@pytest.fixture
def база(сессия: Session) -> Iterator[Session]:
    """Строка настроек с зоной owner - без неё дата прогона считалась бы по UTC."""
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    сессия.flush()
    yield сессия


def шаг(имя: str, код: int = 0, следы: list[str] | None = None) -> Шаг:
    """Подставной шаг: запоминает, что его позвали, и возвращает свой код."""

    def выполнить(session: Session, settings: Settings, apply: bool, now: dt.datetime) -> int:
        if следы is not None:
            следы.append(имя)
        return код

    return Шаг(имя, выполнить)


def строка_прогона(session: Session, job: str = JOB_NAME) -> JobRun | None:
    return session.scalars(select(JobRun).where(JobRun.job == job)).one_or_none()


def строк(session: Session, модель: type) -> int:
    return int(session.scalar(select(func.count()).select_from(модель)) or 0)


# --- решение о догоне -------------------------------------------------------


def test_догон_нужен_если_за_сегодня_прогонов_не_было(база: Session) -> None:
    решение = нужен_догоняющий(база, настройки(), now=ПОЛДЕНЬ)

    assert решение.нужен, решение.причина


def test_догон_не_нужен_до_первого_слота(база: Session) -> None:
    """06:00 по Москве: расписание отработает само через полчаса."""
    рано = dt.datetime(2026, 9, 10, 3, 0, tzinfo=dt.UTC)

    решение = нужен_догоняющий(база, настройки(), now=рано)

    assert not решение.нужен


def test_догон_не_нужен_после_успеха_за_сегодня(база: Session) -> None:
    база.add(
        JobRun(
            job=JOB_NAME,
            run_date=СЕГОДНЯ,
            started_at=ПОЛДЕНЬ - dt.timedelta(hours=5),
            finished_at=ПОЛДЕНЬ - dt.timedelta(hours=5),
            status="ok",
        )
    )
    база.flush()

    assert not нужен_догоняющий(база, настройки(), now=ПОЛДЕНЬ).нужен


def test_догон_не_нужен_пока_неудача_не_остыла(база: Session) -> None:
    """Защита от цикла перезапуска: контейнер, встающий по кругу, не должен
    ходить в ИСУ на каждый старт - портал за это банит."""
    база.add(
        JobRun(
            job=JOB_NAME,
            run_date=СЕГОДНЯ,
            started_at=ПОЛДЕНЬ - dt.timedelta(minutes=10),
            finished_at=ПОЛДЕНЬ - dt.timedelta(minutes=5),
            status="failed",
        )
    )
    база.flush()

    решение = нужен_догоняющий(база, настройки(), now=ПОЛДЕНЬ)

    assert not решение.нужен
    assert "минут" in решение.причина


def test_догон_нужен_когда_неудача_остыла(база: Session) -> None:
    база.add(
        JobRun(
            job=JOB_NAME,
            run_date=СЕГОДНЯ,
            started_at=ПОЛДЕНЬ - dt.timedelta(hours=2),
            finished_at=ПОЛДЕНЬ - dt.timedelta(hours=2),
            status="failed",
        )
    )
    база.flush()

    assert нужен_догоняющий(база, настройки(), now=ПОЛДЕНЬ).нужен


def test_догон_нужен_после_убитого_процесса(база: Session) -> None:
    """Строка `running` с пустым `finished_at` - след процесса, которого убили.

    Остывание считается по `started_at`: иначе для такой строки оно
    не считалось бы вовсе, и догон не случился бы никогда.
    """
    база.add(
        JobRun(
            job=JOB_NAME,
            run_date=СЕГОДНЯ,
            started_at=ПОЛДЕНЬ - dt.timedelta(hours=3),
            finished_at=None,
            status="running",
        )
    )
    база.flush()

    assert нужен_догоняющий(база, настройки(), now=ПОЛДЕНЬ).нужен


def test_вчерашняя_неудача_не_воскрешает_вчера(база: Session) -> None:
    """Граница наверстывания (ADR-016): догоняем сегодняшний день, не вчерашний."""
    вчерашняя = JobRun(
        job=JOB_NAME,
        run_date=ВЧЕРА,
        started_at=ПОЛДЕНЬ - dt.timedelta(days=1),
        finished_at=ПОЛДЕНЬ - dt.timedelta(days=1),
        status="failed",
    )
    база.add(вчерашняя)
    база.flush()

    assert нужен_догоняющий(база, настройки(), now=ПОЛДЕНЬ).нужен

    выполнить_цепочку(база, настройки(), apply=True, now=ПОЛДЕНЬ, шаги=(шаг("раз"),))

    строки = {строка.run_date: строка for строка in база.scalars(select(JobRun))}
    assert строки[СЕГОДНЯ].status == "ok"
    assert строки[ВЧЕРА].status == "failed", "вчерашний прогон переписан задним числом"


def test_дата_прогона_по_зоне_owner(база: Session) -> None:
    """23:30 UTC - это уже завтра в Москве, и строка обязана лечь на завтра."""
    поздно = dt.datetime(2026, 9, 10, 21, 30, tzinfo=dt.UTC)

    выполнить_цепочку(база, настройки(), apply=True, now=поздно, шаги=(шаг("раз"),))

    строка = строка_прогона(база)
    assert строка is not None
    assert строка.run_date == dt.date(2026, 9, 11)


# --- прогон цепочки ---------------------------------------------------------


def test_успешная_цепочка_пишет_след(база: Session) -> None:
    следы: list[str] = []

    код = выполнить_цепочку(
        база,
        настройки(),
        apply=True,
        now=ПОЛДЕНЬ,
        шаги=(шаг("раз", следы=следы), шаг("два", следы=следы)),
    )

    assert код == 0
    assert следы == ["раз", "два"], "шаги выполнены не по порядку"
    строка = строка_прогона(база)
    assert строка is not None
    assert строка.status == "ok"
    assert строка.finished_at is not None
    аудит = list(база.scalars(select(AuditLogEntry).where(AuditLogEntry.kind == "job_chain")))
    assert len(аудит) == 1
    assert аудит[0].status == "ok"


def test_отказ_шага_не_отменяет_следующий(база: Session) -> None:
    """Портал не ответил - зеркало прежнее, но реконсил Google всё равно нужен:
    он чинит событие, удалённое в календаре руками."""
    следы: list[str] = []

    код = выполнить_цепочку(
        база,
        настройки(),
        apply=True,
        now=ПОЛДЕНЬ,
        шаги=(шаг("раз", код=1, следы=следы), шаг("два", следы=следы)),
    )

    assert код == 1
    assert следы == ["раз", "два"], "второй шаг не выполнен из-за отказа первого"
    строка = строка_прогона(база)
    assert строка is not None
    assert строка.status == "failed"
    assert строка.error is not None and "раз" in строка.error


def test_неожиданное_исключение_не_роняет_цепочку(база: Session) -> None:
    """Обрыв сети при входе в Keycloak не завёрнут в наши типы (auth.py),
    а летит наружу голым httpx2.RequestError - и приходит это ровно
    в догоняющем запуске, сразу после перезагрузки платы."""
    следы: list[str] = []

    def падающий(session: Session, settings: Settings, apply: bool, now: dt.datetime) -> int:
        raise httpx2.ConnectError("сеть не поднялась")

    код = выполнить_цепочку(
        база,
        настройки(),
        apply=True,
        now=ПОЛДЕНЬ,
        шаги=(Шаг("раз", падающий), шаг("два", следы=следы)),
    )

    assert код == 1
    assert следы == ["два"]
    строка = строка_прогона(база)
    assert строка is not None
    assert строка.status == "failed"


def test_строка_running_переживает_откат_внутри_шага(база: Session) -> None:
    """Отметка о начале коммитится до первого шага - иначе `session.rollback()`
    внутри джоба (штатная реакция `sync_itmo` на отказ) снёс бы её.

    Проверка честная, а не артефакт фикстуры: `сессия` открыта с
    `join_transaction_mode="create_savepoint"`, то есть коммит освобождает
    savepoint во внешнюю транзакцию теста, а последующий откат снимает
    только то, что было после него.
    """
    статусы: list[str | None] = []

    def откатывающий(session: Session, settings: Settings, apply: bool, now: dt.datetime) -> int:
        session.add(
            ItmoLesson(
                source_key="мусор",
                lesson_date=СЕГОДНЯ,
                starts_at=ПОЛДЕНЬ,
                ends_at=ПОЛДЕНЬ,
                subject="не должно сохраниться",
                fetched_at=ПОЛДЕНЬ,
            )
        )
        session.rollback()
        строка = строка_прогона(session)
        статусы.append(строка.status if строка else None)
        return 1

    выполнить_цепочку(база, настройки(), apply=True, now=ПОЛДЕНЬ, шаги=(Шаг("раз", откатывающий),))

    assert статусы == ["running"], "отметка о начале не пережила откат внутри шага"
    assert строк(база, ItmoLesson) == 0


def test_двойной_прогон_за_день_не_множит_строки(база: Session) -> None:
    """Catch-up - штатный второй запуск за день (§11.2)."""
    выполнить_цепочку(база, настройки(), apply=True, now=ПОЛДЕНЬ, шаги=(шаг("раз"),))
    выполнить_цепочку(база, настройки(), apply=True, now=ПОЛДЕНЬ, шаги=(шаг("раз", код=1),))

    строки = list(база.scalars(select(JobRun).where(JobRun.job == JOB_NAME)))
    assert len(строки) == 1
    assert строки[0].status == "failed", "второй прогон не перезаписал статус"


def test_dry_run_не_пишет_ничего(база: Session) -> None:
    следы: list[str] = []

    код = выполнить_цепочку(
        база, настройки(), apply=False, now=ПОЛДЕНЬ, шаги=(шаг("раз", следы=следы),)
    )

    assert код == 0
    assert следы == ["раз"], "в dry-run шаги всё равно вызываются - своим dry-run"
    assert строк(база, JobRun) == 0
    assert строк(база, AuditLogEntry) == 0


def test_состав_настоящей_цепочки() -> None:
    """Порядок значим: реконсил по необновлённому зеркалу уедет по вчерашним данным."""
    assert [шаг.имя for шаг in ЦЕПОЧКА_КАЛЕНДАРЯ] == ["sync_itmo", "push_gcal"]


def test_сломанная_зона_не_даёт_трассировку(база: Session) -> None:
    """Отказ уровня цепочки: зона не принадлежит ни одному шагу, и без строки
    `daily_calendar` он остался бы неотличим от «процесс не стартовал»."""
    строка_настроек = база.get(Setting, 1)
    assert строка_настроек is not None
    строка_настроек.timezone = "Марс/Олимп"
    база.flush()

    код = выполнить_цепочку(база, настройки(), apply=True, now=ПОЛДЕНЬ, шаги=(шаг("раз"),))

    assert код == 0
    строка = строка_прогона(база)
    assert строка is not None, "прогон не оставил следа при неизвестной зоне"
    # Дата взята по запасной зоне - той же, что server_default колонки.
    assert строка.run_date == СЕГОДНЯ

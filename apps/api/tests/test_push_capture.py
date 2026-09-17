"""Джоб записи событий захвата (Э8): подставной Google, настоящая база.

Проверяется то же, чем опасен любой джоб записи наружу, - но на очереди,
а не на окне дат. Главных случаев три:

- **двойной прогон не задваивает событие** в календаре owner;
- **потерянный `google_event_id`** (плата перезагрузилась между записью
  в Google и коммитом журнала) не приводит ко второй копии: ключ
  детерминирован (ADR-042), и событие находится в самом календаре;
- **отказ по событию виден** - строка `failed` с причиной и ненулевой код
  возврата, а не тихий успех.
"""

import datetime as dt
from collections.abc import Iterator

import pytest
from gcal_fake import ПодставнойGoogle, собрать_сервис
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry, CalendarEvent, JobRun, Setting
from jarvis_api.integrations.gcal.client import GcalClient
from jarvis_api.jobs.push_capture import JOB_NAME, run_once, отправить_сразу

СЕЙЧАС = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.UTC)
НАЧАЛО = dt.datetime(2026, 10, 21, 14, 0, tzinfo=dt.UTC)


def настройки(**переопределения: object) -> Settings:
    основа: dict[str, object] = {"gcal_retries": 0, "google_sa_json": ""}
    основа.update(переопределения)
    return Settings(**основа)  # type: ignore[arg-type]


def событие(ключ: str, *, название: str = "Встреча с куратором") -> CalendarEvent:
    """Строка, какой её оставляет подтверждение: в очереди и без отпечатка."""
    return CalendarEvent(
        external_key=ключ,
        calendar="events",
        source="capture",
        title=название,
        starts_at=НАЧАЛО,
        ends_at=НАЧАЛО + dt.timedelta(hours=1),
        location="ауд. 2412",
        description=None,
        content_hash="",
        sync_state="pending",
    )


@pytest.fixture
def google() -> ПодставнойGoogle:
    return ПодставнойGoogle()


@pytest.fixture
def календарь(google: ПодставнойGoogle) -> str:
    return google.завести_календарь("JARVIS · События")


@pytest.fixture
def клиент(google: ПодставнойGoogle) -> GcalClient:
    return GcalClient(настройки(), собрать_сервис(google))


@pytest.fixture
def база(сессия: Session, календарь: str) -> Iterator[Session]:
    сессия.add(Setting(id=1, timezone="Europe/Moscow", gcal_events_id=календарь))
    сессия.flush()
    yield сессия


def прогон(база: Session, клиент: GcalClient, *, apply: bool = True) -> int:
    return run_once(база, настройки(), клиент, apply=apply, now=СЕЙЧАС)


def test_очередь_уезжает_в_календарь_событий(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    база.add(событие("capture:11111111-1111-4111-8111-111111111111"))
    база.flush()

    assert прогон(база, клиент) == 0

    assert google.ключи(календарь) == {"capture:11111111-1111-4111-8111-111111111111"}
    строка = база.scalars(select(CalendarEvent)).one()
    assert строка.sync_state == "synced"
    assert строка.google_event_id
    assert строка.synced_at == СЕЙЧАС
    # Отпечаток появляется вместе с записью: до неё он пуст и означает
    # «в Google ещё ничего не отправляли».
    assert строка.content_hash


def test_второй_прогон_не_задваивает(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    база.add(событие("capture:22222222-2222-4222-8222-222222222222"))
    база.flush()

    прогон(база, клиент)
    записей_после_первого = google.записей()
    assert прогон(база, клиент) == 0

    assert len(google.события_календаря(календарь)) == 1
    assert google.записей() == записей_после_первого, "второй прогон писать не должен"


def test_потерянный_журнал_не_создаёт_вторую_копию(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Плата перезагрузилась между записью в Google и коммитом журнала.

    Строка осталась `pending` без `google_event_id`, а событие в календаре
    уже есть. Прогон обязан найти его по ключу и дописать журнал, а не
    завести вторую копию - ради этого ключ и детерминирован (ADR-042).
    """
    ключ = "capture:33333333-3333-4333-8333-333333333333"
    база.add(событие(ключ))
    база.flush()
    прогон(база, клиент)
    строка = база.scalars(select(CalendarEvent)).one()
    идентификатор = строка.google_event_id
    строка.sync_state = "pending"
    строка.google_event_id = None
    база.flush()

    assert прогон(база, клиент) == 0

    assert len(google.события_календаря(календарь)) == 1
    обновлённая = база.scalars(select(CalendarEvent)).one()
    assert обновлённая.sync_state == "synced"
    assert обновлённая.google_event_id == идентификатор


def test_отказ_по_событию_виден(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Событие, которое Google отклонил, обязано остаться в очереди с причиной."""
    база.add(событие("capture:44444444-4444-4444-8444-444444444444"))
    база.flush()
    google.отказ = ("POST", "/events", 400)

    assert прогон(база, клиент) == 1

    строка = база.scalars(select(CalendarEvent)).one()
    assert строка.sync_state == "failed"
    assert строка.last_error
    отметка = база.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()
    assert отметка.status == "failed"
    отказы = list(база.scalars(select(AuditLogEntry).where(AuditLogEntry.status == "error")))
    assert len(отказы) == 1


def test_отклонённое_событие_уезжает_следующим_прогоном(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """`failed` - это «ещё не записано», а не «больше не пробуем»."""
    база.add(событие("capture:55555555-5555-4555-8555-555555555555"))
    база.flush()
    google.отказ = ("POST", "/events", 400)
    прогон(база, клиент)

    assert прогон(база, клиент) == 0

    assert len(google.события_календаря(календарь)) == 1
    assert база.scalars(select(CalendarEvent)).one().sync_state == "synced"


def test_dry_run_не_пишет_ничего(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    база.add(событие("capture:66666666-6666-4666-8666-666666666666"))
    база.flush()

    assert прогон(база, клиент, apply=False) == 0

    assert google.записей() == 0
    assert google.вызовов("GET", "/events") == 0, "dry-run не должен ходить в Google вовсе"
    assert база.scalars(select(CalendarEvent)).one().sync_state == "pending"


def test_без_календаря_событий_джоб_падает_громко(сессия: Session, клиент: GcalClient) -> None:
    """Пустой `gcal_events_id` - отказ с внятным текстом, а не тихий ноль."""
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    сессия.add(событие("capture:77777777-7777-4777-8777-777777777777"))
    сессия.flush()

    assert run_once(сессия, настройки(), клиент, apply=True, now=СЕЙЧАС) == 1

    отметка = сессия.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()
    assert отметка.status == "failed"
    assert "gcal_events_id" in (отметка.error or "")


def test_пары_итмо_джоб_не_трогает(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Очередь - только захват: расписание ведёт свой джоб и свой календарь."""
    пара = событие("itmo:2026-10-21:17:00:aaa", название="Матанализ")
    пара.calendar = "itmo"
    пара.source = "itmo"
    база.add(пара)
    база.flush()

    assert прогон(база, клиент) == 0

    assert google.записей() == 0
    assert база.scalars(select(CalendarEvent)).one().sync_state == "pending"


def test_немедленная_отправка_не_роняет_подтверждение(база: Session) -> None:
    """Без ключа Google попытка обязана тихо вернуть False (инвариант 9).

    Это тот самый путь, которым ходит эндпоинт подтверждения: отказ
    календаря не отменяет принятого события и не поднимает исключение
    в запрос owner.
    """
    строка = событие("capture:88888888-8888-4888-8888-888888888888")
    база.add(строка)
    база.flush()

    assert отправить_сразу(база, настройки(), строка, now=СЕЙЧАС) is False
    assert строка.sync_state == "pending"


def test_пустая_очередь_всё_равно_отмечается(база: Session, клиент: GcalClient) -> None:
    """«Очередь была пуста» и «джоб не запускался» - разные поломки."""
    assert прогон(база, клиент) == 0

    отметка = база.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()
    assert отметка.status == "ok"


def test_dry_run_показывает_очередь_без_клиента(база: Session) -> None:
    """Очередь растёт как раз тогда, когда с ключом Google что-то не так.

    Джоб, отказывающийся показать её без ключа, был бы бесполезен ровно
    в тот день, когда посмотреть очередь и нужно.
    """
    база.add(событие("capture:99999999-9999-4999-8999-999999999999"))
    база.flush()

    assert run_once(база, настройки(), None, apply=False, now=СЕЙЧАС) == 0

    assert база.scalars(select(CalendarEvent)).one().sync_state == "pending"

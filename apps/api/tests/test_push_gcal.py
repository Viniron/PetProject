"""Джоб записи в календарь (Э4): подставной Google, настоящая база.

Проверяется то, ради чего джоб существует, и то, чем он опасен.

Ради: календарь становится тем, что лежит в зеркале, - включая пары,
которые из расписания исчезли.

Опасен: он пишет в чужую систему, где нашей транзакции нет. Двойной запуск
за день - штатный catch-up (§11.2), и он обязан не задваивать; журнал,
потерянный вместе с базой, не должен приводить ко второй копии семестра;
отказ на одной паре не должен уносить остальные семьдесят девять.
"""

import datetime as dt
from collections.abc import Iterator

import pytest
from gcal_fake import ПодставнойGoogle, собрать_сервис
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry, CalendarEvent, ItmoLesson, JobRun, Setting
from jarvis_api.integrations.gcal.client import GcalClient
from jarvis_api.jobs.push_gcal import JOB_NAME, run_once

# Четверг. Окно джоба - неделя назад и месяц вперёд, то есть 3 сентября
# по 8 октября: обе пары ниже внутрь попадают, августовская - нет.
СЕЙЧАС = dt.datetime(2026, 9, 10, 4, 0, tzinfo=dt.UTC)


def настройки(**переопределения: object) -> Settings:
    основа: dict[str, object] = {
        # Повторов нет: SDK спит между ними, а проверять здесь надо не сон.
        "gcal_retries": 0,
    }
    основа.update(переопределения)
    return Settings(**основа)  # type: ignore[arg-type]


def пара(
    ключ: str,
    день: dt.date,
    час_utc: int,
    предмет: str = "Математический анализ",
    аудитория: str = "ауд. 2412",
) -> ItmoLesson:
    начало = dt.datetime.combine(день, dt.time(час_utc), tzinfo=dt.UTC)
    return ItmoLesson(
        source_key=ключ,
        lesson_date=день,
        starts_at=начало,
        ends_at=начало + dt.timedelta(hours=1, minutes=30),
        subject=предмет,
        kind="Лекции",
        teacher="Иванов И. И.",
        room=аудитория,
        building="Кронверкский пр., 49",
        mode="Очный",
        online_url=None,
        fetched_at=СЕЙЧАС,
    )


@pytest.fixture
def google() -> ПодставнойGoogle:
    return ПодставнойGoogle()


@pytest.fixture
def календарь(google: ПодставнойGoogle) -> str:
    return google.завести_календарь("JARVIS · ИТМО")


@pytest.fixture
def клиент(google: ПодставнойGoogle) -> GcalClient:
    return GcalClient(настройки(), собрать_сервис(google))


@pytest.fixture
def база(сессия: Session, календарь: str) -> Iterator[Session]:
    """Настройки с зоной owner и заведённым календарём ИТМО."""
    сессия.add(Setting(id=1, timezone="Europe/Moscow", gcal_itmo_id=календарь))
    сессия.add(пара("itmo:2026-09-10:10:00:aaa", dt.date(2026, 9, 10), 7))
    сессия.add(пара("itmo:2026-09-11:12:00:bbb", dt.date(2026, 9, 11), 9, предмет="Физика"))
    сессия.flush()
    yield сессия


def прогон(
    база: Session, клиент: GcalClient, *, apply: bool = True, now: dt.datetime = СЕЙЧАС
) -> int:
    return run_once(база, настройки(), клиент, apply=apply, now=now)


def строк(session: Session, модель: type) -> int:
    return int(session.scalar(select(func.count()).select_from(модель)) or 0)


def test_первый_прогон_создаёт_события(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    assert прогон(база, клиент) == 0

    assert google.ключи(календарь) == {
        "itmo:2026-09-10:10:00:aaa",
        "itmo:2026-09-11:12:00:bbb",
    }
    строки = list(база.scalars(select(CalendarEvent)))
    assert len(строки) == 2
    assert {строка.sync_state for строка in строки} == {"synced"}
    assert all(строка.google_event_id for строка in строки)
    assert all(строка.calendar == "itmo" and строка.source == "itmo" for строка in строки)


def test_повторный_прогон_ничего_не_делает(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Двойной запуск за день - штатный catch-up, а не авария (§11.2)."""
    прогон(база, клиент)
    записей_после_первого = google.записей()

    assert прогон(база, клиент) == 0

    assert google.записей() == записей_после_первого, "второй прогон писал в Google"


def test_смена_аудитории_обновляет_событие(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Пара с исправленными реквизитами переезжает, а не задваивается."""
    прогон(база, клиент)
    было = dict(google.события_календаря(календарь))
    строка = база.get(CalendarEvent, "itmo:2026-09-10:10:00:aaa")
    assert строка is not None
    прежний_id = строка.google_event_id

    пара_в_зеркале = база.get(ItmoLesson, "itmo:2026-09-10:10:00:aaa")
    assert пара_в_зеркале is not None
    пара_в_зеркале.room = "ауд. 2413"
    база.flush()

    assert прогон(база, клиент) == 0

    assert len(google.события_календаря(календарь)) == len(было), "событие задвоилось"
    assert google.вызовов("PUT", "/events/") == 1
    обновлённая = база.get(CalendarEvent, "itmo:2026-09-10:10:00:aaa")
    assert обновлённая is not None
    assert обновлённая.google_event_id == прежний_id
    тело = google.события_календаря(календарь)[str(прежний_id)]
    assert "ауд. 2413" in тело["location"]


def test_исчезнувшая_пара_удаляется(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Отменённая лекция обязана исчезнуть из календаря, а не звонить до июня."""
    прогон(база, клиент)

    убранная = база.get(ItmoLesson, "itmo:2026-09-11:12:00:bbb")
    assert убранная is not None
    база.delete(убранная)
    база.flush()

    assert прогон(база, клиент) == 0

    assert google.ключи(календарь) == {"itmo:2026-09-10:10:00:aaa"}
    assert база.get(CalendarEvent, "itmo:2026-09-11:12:00:bbb") is None


def test_журнал_потерян_события_находятся_по_ключу(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """База восстановлена из дампа: журнал пуст, а календарь полон.

    Это главный сценарий, ради которого ключ живёт в самом событии.
    Без него прогон после restore создал бы вторую копию семестра.
    """
    прогон(база, клиент)
    идентификаторы = set(google.события_календаря(календарь))

    for строка in list(база.scalars(select(CalendarEvent))):
        база.delete(строка)
    база.flush()
    google.вызовы.clear()

    assert прогон(база, клиент) == 0

    assert set(google.события_календаря(календарь)) == идентификаторы, "события задвоились"
    assert google.вызовов("POST", "/events") == 0, "создавали заново вместо обновления"
    assert строк(база, CalendarEvent) == 2


def test_событие_удалённое_в_google_создаётся_заново(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Журнал говорит `synced`, а события нет: правда за Google."""
    прогон(база, клиент)
    удалённое = next(iter(google.события_календаря(календарь)))
    del google.события[календарь][удалённое]

    assert прогон(база, клиент) == 0

    assert len(google.ключи(календарь)) == 2


def test_dry_run_не_пишет_ничего(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Буквально: ни в Google, ни в базу - включая аудит и отметку прогона.

    Исключения, которое есть у Э3 (токены, добытые входом), здесь нет:
    сервисному аккаунту нечего сохранять между прогонами.
    """
    assert прогон(база, клиент, apply=False) == 0

    assert google.записей() == 0
    assert строк(база, CalendarEvent) == 0
    assert строк(база, AuditLogEntry) == 0
    assert строк(база, JobRun) == 0


def test_отказ_на_одном_событии_не_мешает_остальным(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Календарь без одной пары лучше календаря без восьмидесяти.

    Джоб при этом всё равно падает громко: код 1 и `failed` в `job_runs` -
    смотреть на него в момент запуска некому (§10).
    """
    google.отказ = ("POST", "/events", 400)

    assert прогон(база, клиент) == 1

    assert len(google.ключи(календарь)) == 1, "вторая пара не уехала в календарь"
    неудачные = list(
        база.scalars(select(CalendarEvent).where(CalendarEvent.sync_state == "failed"))
    )
    assert len(неудачные) == 1
    assert неудачные[0].last_error
    assert неудачные[0].google_event_id is None
    прогон_дня = база.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()
    assert прогон_дня.status == "failed"
    assert база.scalars(
        select(AuditLogEntry).where(AuditLogEntry.status == "error")
    ).all(), "отказ не оставил следа в аудите"


def test_следующий_прогон_дописывает_неудавшееся(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Реконсил лечит вчерашний отказ сам, без ручного вмешательства."""
    google.отказ = ("POST", "/events", 400)
    прогон(база, клиент)

    assert прогон(база, клиент) == 0

    assert len(google.ключи(календарь)) == 2
    assert строк(база, CalendarEvent) == 2
    assert not база.scalars(select(CalendarEvent).where(CalendarEvent.sync_state == "failed")).all()


def test_недоступность_google_не_трогает_журнал(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Отказ доступа прекращает прогон целиком: писать всё равно некуда."""
    прогон(база, клиент)
    было = {
        строка.external_key: строка.content_hash for строка in база.scalars(select(CalendarEvent))
    }
    google.сетевой_отказ = True

    assert прогон(база, клиент) == 1

    google.сетевой_отказ = False
    стало = {
        строка.external_key: строка.content_hash for строка in база.scalars(select(CalendarEvent))
    }
    assert стало == было
    последний = база.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()
    assert последний.status == "failed"


def test_отказ_доступа_не_перебирает_все_события(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """403 на календаре - это нерасшаренный календарь, а не плохая пара.

    Восемьдесят одинаковых отказов в `audit_log` не добавили бы знания,
    поэтому прогон прекращается на первом.
    """
    google.отказ = ("GET", "/events", 403)

    assert прогон(база, клиент) == 1

    assert google.записей() == 0


def test_пропавший_календарь_отказ_а_не_трассировка(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Owner удалил календарь в Google: джоб обязан сказать это словами.

    Найдено живым прогоном на плате. 404 на выдаче событий - это не
    "события нет", а "нет календаря", и наверх он уходил как ошибка уровня
    события, которую прогон не ловит: вместо внятного текста и следа
    в `audit_log` получалась трассировка.
    """
    прогон(база, клиент)
    google.отказ = ("GET", "/events", 404)

    assert прогон(база, клиент) == 1

    прогон_дня = база.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()
    assert прогон_дня.status == "failed"
    assert прогон_дня.error is not None and "недоступен" in прогон_дня.error
    # Журнал не тронут: календарь пропал, но то, что мы о нём знаем, - нет.
    assert строк(база, CalendarEvent) == 2


def test_пары_вне_окна_не_трогаются(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Прошлогоднее занятие - история, а не кандидат на удаление."""
    старая = пара("itmo:2026-08-01:10:00:ccc", dt.date(2026, 8, 1), 7)
    база.add(старая)
    база.add(
        CalendarEvent(
            external_key=старая.source_key,
            calendar="itmo",
            source="itmo",
            title="Старая пара",
            starts_at=старая.starts_at,
            ends_at=старая.ends_at,
            content_hash="не важно",
            google_event_id="ev-старое",
            sync_state="synced",
        )
    )
    база.flush()

    assert прогон(база, клиент) == 0

    assert база.get(CalendarEvent, "itmo:2026-08-01:10:00:ccc") is not None
    assert google.вызовов("DELETE", "/events/") == 0


def test_время_уходит_в_utc(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Пара 10:00 по Москве лежит в календаре как 07:00Z.

    Проверяется на теле запроса, а не на нашей структуре: между ними
    и находится место, где час теряется.
    """
    прогон(база, клиент)

    тела = list(google.события_календаря(календарь).values())
    начала = {тело["start"]["dateTime"] for тело in тела}
    assert "2026-09-10T07:00:00Z" in начала


def test_без_календаря_в_настройках_джоб_отказывается(
    сессия: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Календарь заводится осознанно, отдельной командой, а не на лету."""
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    сессия.add(пара("itmo:2026-09-10:10:00:aaa", dt.date(2026, 9, 10), 7))
    сессия.flush()

    assert run_once(сессия, настройки(), клиент, apply=True, now=СЕЙЧАС) == 1

    assert google.записей() == 0
    прогон_дня = сессия.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()
    assert прогон_дня.status == "failed"
    assert прогон_дня.error is not None and "gcal_itmo_id" in прогон_дня.error


def test_пустое_зеркало_не_чистит_календарь_целиком(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Проверка симметрии с Э3: пустое зеркало - это пустой календарь.

    Выглядит опасно и таковым является, поэтому защита стоит **до** этого
    джоба: забор отличает каникулы от отказа портала по полю `code`
    и при отказе зеркало не трогает вовсе. Здесь фиксируется, что второй
    защиты нет и быть не должно - иначе исчезнувшая пара осталась бы
    в календаре навсегда.
    """
    прогон(база, клиент)
    for строка in list(база.scalars(select(ItmoLesson))):
        база.delete(строка)
    база.flush()

    assert прогон(база, клиент) == 0

    assert google.ключи(календарь) == set()
    assert строк(база, CalendarEvent) == 0

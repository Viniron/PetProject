"""Джоб воскресного напоминания (Ф8): подставной Google, настоящая база.

Правило «кому и когда» проверено в `test_finance_reminder.py` без базы.
Здесь - то, чем опасен любой джоб записи наружу (инвариант 5):

- **двойной прогон не задваивает событие** - а джоб зовётся на каждом
  воскресном слоте и при каждом старте процесса;
- **потерянный журнал не даёт второй копии** - событие находится по ключу;
- **отказ Google виден** и чинится следующим слотом;
- **dry-run не пишет никуда.**
"""

import datetime as dt
from collections.abc import Iterator
from zoneinfo import ZoneInfo

import pytest
from gcal_fake import ПодставнойGoogle, собрать_сервис
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry, CalendarEvent, FinImport, JobRun, Setting
from jarvis_api.integrations.gcal.client import GcalClient
from jarvis_api.jobs.finance_remind import JOB_NAME, run_once, напомнить, описать

МОСКВА = ZoneInfo("Europe/Moscow")
# Воскресенье 4 октября 2026, первый слот планировщика.
УТРО = dt.datetime(2026, 10, 4, 6, 30, tzinfo=МОСКВА).astimezone(dt.UTC)
ВЕЧЕР = dt.datetime(2026, 10, 4, 19, 5, tzinfo=МОСКВА).astimezone(dt.UTC)
СУББОТА = dt.datetime(2026, 10, 3, 12, 0, tzinfo=МОСКВА).astimezone(dt.UTC)
КЛЮЧ = "finance:statements:2026-10-04"


def настройки(**переопределения: object) -> Settings:
    основа: dict[str, object] = {"gcal_retries": 0, "google_sa_json": ""}
    основа.update(переопределения)
    return Settings(**основа)  # type: ignore[arg-type]


def выгрузка(банк: str, загружено: dt.datetime) -> FinImport:
    return FinImport(
        bank=банк,
        filename=f"{банк}.csv",
        sha256=f"{банк}{загружено.isoformat()}".ljust(64, "0")[:64],
        period_start=dt.date(2026, 9, 1),
        period_end=dt.date(2026, 9, 26),
        imported_at=загружено,
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
    """Книжка, в которой выписка Т-Банка загружена в прошлое воскресенье."""
    сессия.add(Setting(id=1, timezone="Europe/Moscow", gcal_events_id=календарь))
    сессия.add(выгрузка("tbank", dt.datetime(2026, 9, 27, 19, 0, tzinfo=МОСКВА)))
    сессия.flush()
    yield сессия


def прогон(
    база: Session, клиент: GcalClient | None, *, apply: bool = True, now: dt.datetime = УТРО
) -> int:
    return run_once(база, настройки(), клиент, apply=apply, now=now)


def отметка(база: Session) -> JobRun:
    return база.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()


def test_событие_уезжает_в_календарь_событий(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    assert прогон(база, клиент) == 0

    assert google.ключи(календарь) == {КЛЮЧ}
    (тело,) = google.события_календаря(календарь).values()
    assert тело["summary"] == "Загрузить выписки"
    assert тело["start"]["dateTime"] == "2026-10-04T15:00:00Z", "18:00 по Москве"
    assert тело["end"]["dateTime"] == "2026-10-04T15:30:00Z"
    assert "tbank" in тело["description"]
    # След источника называет книжку, а не захват: в этот календарь пишут двое.
    assert тело["extendedProperties"]["private"]["jarvis_source"] == "finance"

    строка = база.get(CalendarEvent, КЛЮЧ)
    assert строка is not None
    assert (строка.source, строка.calendar, строка.sync_state) == ("finance", "events", "synced")
    assert отметка(база).status == "ok"
    след = база.scalars(select(AuditLogEntry).where(AuditLogEntry.target == КЛЮЧ)).one()
    assert (след.kind, след.status, след.actor) == ("calendar_write", "ok", JOB_NAME)


def test_каждый_слот_воскресенья_не_задваивает(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Шесть слотов и догон при старте - одно событие и ни одной лишней записи."""
    прогон(база, клиент)
    записей = google.записей()

    for час in (9, 12, 15, 18, 21):
        момент = dt.datetime(2026, 10, 4, час, 30, tzinfo=МОСКВА).astimezone(dt.UTC)
        assert прогон(база, клиент, now=момент) == 0

    assert len(google.события_календаря(календарь)) == 1
    assert google.записей() == записей


def test_потерянный_журнал_не_создаёт_вторую_копию(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Плата перезагрузилась между записью в Google и коммитом журнала."""
    прогон(база, клиент)
    строка = база.get(CalendarEvent, КЛЮЧ)
    assert строка is not None
    строка.sync_state = "pending"
    строка.google_event_id = None
    база.flush()

    assert прогон(база, клиент, now=ВЕЧЕР) == 0

    assert len(google.события_календаря(календарь)) == 1
    assert google.вызовов("POST", "/events") == 1


def test_отказ_google_виден_и_чинится_следующим_слотом(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    google.отказ = ("POST", "/events", 400)

    assert прогон(база, клиент) == 1

    строка = база.get(CalendarEvent, КЛЮЧ)
    assert строка is not None
    assert строка.sync_state == "failed"
    assert строка.last_error
    assert отметка(база).status == "failed"
    отказы = list(база.scalars(select(AuditLogEntry).where(AuditLogEntry.status == "error")))
    assert len(отказы) == 1

    # Следующий слот - уже после 18:00: событие сдвигается вперёд, а не
    # ставится в прошлое, и запись в журнале та же.
    assert прогон(база, клиент, now=ВЕЧЕР) == 0

    (тело,) = google.события_календаря(календарь).values()
    assert тело["start"]["dateTime"] == "2026-10-04T16:15:00Z", "19:15 по Москве"
    assert база.get(CalendarEvent, КЛЮЧ).sync_state == "synced"  # type: ignore[union-attr]
    assert отметка(база).status == "ok"


def test_выписку_успели_загрузить_недоехавшее_убирается(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Google отказал утром, к вечеру выписка загружена - напоминать не о чем."""
    google.отказ = ("POST", "/events", 400)
    прогон(база, клиент)
    база.add(выгрузка("tbank", dt.datetime(2026, 10, 4, 12, 0, tzinfo=МОСКВА)))
    база.flush()

    assert прогон(база, клиент, now=ВЕЧЕР) == 0

    assert база.get(CalendarEvent, КЛЮЧ) is None
    assert google.события_календаря(календарь) == {}


def test_записанное_не_трогается_после_загрузки(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle, календарь: str
) -> None:
    """Правок событий в релизе нет: записанное остаётся, как было."""
    прогон(база, клиент)
    записей = google.записей()
    база.add(выгрузка("tbank", dt.datetime(2026, 10, 4, 12, 0, tzinfo=МОСКВА)))
    база.flush()

    assert прогон(база, клиент, now=ВЕЧЕР) == 0

    assert google.записей() == записей
    assert база.get(CalendarEvent, КЛЮЧ) is not None


def test_dry_run_не_пишет_ничего(база: Session, google: ПодставнойGoogle) -> None:
    """Ни в Google, ни в базу - и клиент для этого не нужен вовсе."""
    отчёт = напомнить(база, настройки(), None, apply=False, now=УТРО)

    assert отчёт.действие == "create"
    assert отчёт.ключ == КЛЮЧ
    assert google.вызовы == []
    assert база.get(CalendarEvent, КЛЮЧ) is None
    assert база.scalars(select(JobRun)).all() == []


def test_в_будни_ни_события_ни_отметки(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Джоб зовётся при каждом старте процесса; в будни ему делать нечего."""
    assert прогон(база, клиент, now=СУББОТА) == 0

    assert google.вызовы == []
    assert база.scalars(select(CalendarEvent)).all() == []
    assert база.scalars(select(JobRun)).all() == []


def test_всё_загружено_события_нет(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    база.add(выгрузка("tbank", dt.datetime(2026, 9, 30, 20, 0, tzinfo=МОСКВА)))
    база.flush()

    assert прогон(база, клиент) == 0

    assert google.записей() == 0
    assert база.scalars(select(CalendarEvent)).all() == []
    assert отметка(база).status == "ok"


def test_без_календаря_событий_падает_громко(сессия: Session, клиент: GcalClient) -> None:
    """Пустой `gcal_events_id` - ненулевой код и причина в `job_runs`."""
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    сессия.add(выгрузка("tbank", dt.datetime(2026, 9, 27, 19, 0, tzinfo=МОСКВА)))
    сессия.flush()

    assert run_once(сессия, настройки(), клиент, apply=True, now=УТРО) == 1

    assert отметка(сессия).status == "failed"
    assert "gcal_events_id" in (отметка(сессия).error or "")
    assert сессия.scalars(select(CalendarEvent)).all() == []


def test_без_клиента_google_падает_громко(база: Session) -> None:
    """Ключ Google битый, а событие нужно: отказ, а не тихий успех."""
    assert прогон(база, None) == 1

    assert отметка(база).status == "failed"


def test_dry_run_показывает_текст_события(база: Session) -> None:
    """Ревью диффа перед записью наружу (CLAUDE.md) - это ревью текста, а не ключа."""
    отчёт = напомнить(база, настройки(), None, apply=False, now=УТРО)

    строки = описать(отчёт, False, база)

    assert "  create finance:statements:2026-10-04 на 04.10.2026 18:00" in строки
    assert any("tbank: операции по 26.09.2026" in строка for строка in строки)
    assert строки[-1] == "dry-run: в Google и в базу не записано ничего"

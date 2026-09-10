"""Создание трёх календарей JARVIS (Э4): подставной Google, настоящая база.

Действие разовое, но опасное в обе стороны. Не создать - джобу некуда
писать. Создать дважды - у owner в списке появляются два `JARVIS · ИТМО`,
и какой из них наполняется, видно только по id в базе.
"""

import datetime as dt
from collections.abc import Iterator

import pytest
from gcal_fake import ПодставнойGoogle, собрать_сервис
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry, Setting
from jarvis_api.integrations.gcal.client import GcalClient
from jarvis_api.jobs.gcal_setup import КАЛЕНДАРИ, run_once

ПОЧТА = "owner@example.com"


def настройки(**переопределения: object) -> Settings:
    основа: dict[str, object] = {
        "google_calendar_owner_email": ПОЧТА,
        "gcal_retries": 0,
    }
    основа.update(переопределения)
    return Settings(**основа)  # type: ignore[arg-type]


@pytest.fixture
def google() -> ПодставнойGoogle:
    return ПодставнойGoogle()


@pytest.fixture
def клиент(google: ПодставнойGoogle) -> GcalClient:
    return GcalClient(настройки(), собрать_сервис(google))


@pytest.fixture
def база(сессия: Session) -> Iterator[Session]:
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    сессия.flush()
    yield сессия


def test_apply_создаёт_три_календаря_и_отдаёт_их_owner(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    assert run_once(база, настройки(), клиент, apply=True) == 0

    assert sorted(google.календари.values()) == sorted(название for _, название in КАЛЕНДАРИ)
    # Расшарен каждый: календарь, созданный и не отданный owner, невидим -
    # его нельзя ни найти, ни удалить из интерфейса.
    assert all(почты == [ПОЧТА] for почты in google.acl.values())

    строка = база.get(Setting, 1)
    assert строка is not None
    assert строка.gcal_itmo_id and строка.gcal_study_id and строка.gcal_events_id
    assert len({строка.gcal_itmo_id, строка.gcal_study_id, строка.gcal_events_id}) == 3


def test_календари_создаются_в_зоне_owner(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Зона берётся из настроек, а не из зоны сервера Google.

    Иначе календарь заводится в UTC, и owner видит пары со сдвигом на три
    часа - при том, что в базе время верное.
    """
    run_once(база, настройки(), клиент, apply=True)

    assert len(google.тела_календарей) == 3
    зоны = {тело["timeZone"] for тело in google.тела_календарей.values()}
    assert зоны == {"Europe/Moscow"}


def test_повторный_прогон_ничего_не_создаёт(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Идемпотентность: команда набирается второй раз без последствий."""
    run_once(база, настройки(), клиент, apply=True)
    записей = google.записей()

    assert run_once(база, настройки(), клиент, apply=True) == 0

    assert google.записей() == записей
    assert len(google.календари) == 3


def test_dry_run_не_создаёт_ничего(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    assert run_once(база, настройки(), клиент, apply=False) == 0

    assert google.записей() == 0
    assert google.календари == {}
    строка = база.get(Setting, 1)
    assert строка is not None
    assert строка.gcal_itmo_id is None


def test_календарь_удалённый_owner_заводится_заново(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """id в базе может пережить календарь: правда за Google, а не за нами."""
    run_once(база, настройки(), клиент, apply=True)
    строка = база.get(Setting, 1)
    assert строка is not None
    прежний = строка.gcal_itmo_id
    assert прежний is not None
    del google.календари[прежний]

    assert run_once(база, настройки(), клиент, apply=True) == 0

    обновлённая = база.get(Setting, 1)
    assert обновлённая is not None
    assert обновлённая.gcal_itmo_id != прежний
    assert обновлённая.gcal_itmo_id in google.календари


def test_без_почты_owner_отказ(база: Session, клиент: GcalClient, google: ПодставнойGoogle) -> None:
    """Календарь без расшаривания owner не увидит - значит и создавать нечего."""
    assert run_once(база, настройки(google_calendar_owner_email=""), клиент, apply=True) == 1

    assert google.записей() == 0


def test_строка_настроек_заводится_на_чистой_базе(
    сессия: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """На свежей плате `settings` пуста, и это не повод отказываться работать."""
    assert сессия.scalar(select(func.count()).select_from(Setting)) == 0

    assert run_once(сессия, настройки(), клиент, apply=True) == 0

    строка = сессия.get(Setting, 1)
    assert строка is not None
    assert строка.gcal_itmo_id is not None


def test_создание_попадает_в_аудит(
    база: Session, клиент: GcalClient, google: ПодставнойGoogle
) -> None:
    """Инвариант 8: запись наружу без следа не бывает."""
    run_once(база, настройки(), клиент, apply=True)

    записи = list(база.scalars(select(AuditLogEntry).where(AuditLogEntry.kind == "calendar_setup")))
    assert len(записи) == 3
    assert all(запись.provider == "google" and запись.status == "ok" for запись in записи)
    assert all(запись.detail and запись.detail["shared_with"] == ПОЧТА for запись in записи)
    # Время аудита tz-aware - инвариант 7 на живой базе, а не на модели.
    assert all(запись.at.tzinfo is not None for запись in записи)
    assert записи[0].at < dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)

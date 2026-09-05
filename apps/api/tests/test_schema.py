"""Тесты схемы: инварианты, которые обязана держать сама база.

Проверяется не то, что код делает правильно, а то, что база не даст сделать
неправильно. Разница существенная: джоб можно переписать и забыть, а
ограничение переживает переписывание.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jarvis_api.db.models import (
    CalendarEvent,
    CaptureBlob,
    CaptureDraft,
    DayFlag,
    JobRun,
    Setting,
)


def событие(**переопределения: Any) -> CalendarEvent:
    """Событие календаря с заполненным минимумом."""
    поля: dict[str, Any] = {
        "external_key": "itmo:2026-09-01:0900",
        "calendar": "itmo",
        "source": "itmo",
        "title": "Матанализ",
        "starts_at": datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        "ends_at": datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        "content_hash": "0" * 40,
    }
    поля.update(переопределения)
    return CalendarEvent(**поля)


def test_ни_одна_колонка_времени_не_потеряла_таймзону(сессия: Session) -> None:
    """Инвариант 7 проверяется схемой, а не вниманием ревьюера.

    `timestamp without time zone` в Postgres молча сохраняет то, что пришло,
    поэтому дамп, залитый на хост с другой локалью, сдвинул бы всё
    расписание на час.
    """
    наивные = сессия.execute(
        text(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = 'public' and data_type = 'timestamp without time zone'"
        )
    ).all()

    assert наивные == []


def test_время_хранится_в_utc_независимо_от_зоны_записи(сессия: Session) -> None:
    """Запись в московской зоне читается как тот же момент в UTC."""
    сессия.add(событие(starts_at=datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))))
    сессия.flush()

    # Сравнивается текст, а не datetime: наивный объект здесь запрещён
    # линтером (правило DTZ), и правильно - именно из-за таких сравнений
    # в код и просачивается время без зоны.
    в_utc = сессия.execute(
        text(
            "select to_char(starts_at at time zone 'UTC', 'YYYY-MM-DD HH24:MI') "
            "from calendar_events"
        )
    ).scalar_one()

    assert в_utc == "2026-09-01 09:00"


def test_настроек_не_может_стать_две(сессия: Session) -> None:
    """§9: настройки owner - одна строка. Вторая должна быть невозможна."""
    сессия.add(Setting(id=1))
    сессия.flush()

    сессия.add(Setting(id=2))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_событие_с_тем_же_external_key_не_задваивается(сессия: Session) -> None:
    """Инвариант 5 и §11.2: второй прогон джоба за день - штатный сценарий."""
    сессия.add(событие())
    сессия.flush()

    сессия.add(событие(title="Матанализ (перенос)"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_календарь_события_ограничен_календарями_jarvis(сессия: Session) -> None:
    """Событие вне трёх календарей JARVIS не найдётся при удалении по префиксу."""
    сессия.add(событие(calendar="личный"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_джоб_отмечается_за_день_один_раз(сессия: Session) -> None:
    """Основа catch-up (§11.2): догоняющий прогон обновляет строку, а не множит."""
    сессия.add(JobRun(job="sync-itmo", run_date=date(2026, 9, 1)))
    сессия.flush()

    сессия.add(JobRun(job="sync-itmo", run_date=date(2026, 9, 1)))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_тот_же_джоб_в_другой_день_записывается(сессия: Session) -> None:
    """Обратная сторона предыдущего: ограничение не должно мешать работе."""
    сессия.add(JobRun(job="sync-itmo", run_date=date(2026, 9, 1)))
    сессия.add(JobRun(job="sync-itmo", run_date=date(2026, 9, 2)))
    сессия.flush()

    assert сессия.execute(select(JobRun)).scalars().all() != []


def test_день_с_поздними_парами_помечается_только_один_раз(сессия: Session) -> None:
    """§4: автопометка `late_classes` идемпотентна на уровне базы."""
    сессия.add(DayFlag(starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 1), reason="late_classes"))
    сессия.flush()

    сессия.add(DayFlag(starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 1), reason="late_classes"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_ручной_период_на_тот_же_день_не_запрещён(сессия: Session) -> None:
    """Ограничение частичное: каникулы и поздние пары могут совпасть по дате."""
    сессия.add(DayFlag(starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 1), reason="late_classes"))
    сессия.add(DayFlag(starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 7), reason="каникулы"))
    сессия.flush()

    assert len(сессия.execute(select(DayFlag)).scalars().all()) == 2


def test_период_задом_наперёд_не_записывается(сессия: Session) -> None:
    """Перепутанные границы дают отрицательный период и ломают расчёт отставания."""
    сессия.add(DayFlag(starts_on=date(2026, 9, 7), ends_on=date(2026, 9, 1), reason="каникулы"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_сырьё_захвата_исчезает_вместе_с_черновиком(сессия: Session) -> None:
    """§9: блоб живёт ровно столько же, сколько черновик.

    Удаление идёт запросом, а не через ORM, потому что проверяется каскад
    в базе: именно он сработает, когда черновик уберёт джоб уборки.
    """
    номер = uuid.uuid4()
    сессия.add(CaptureDraft(id=номер, modality="image"))
    сессия.add(
        CaptureBlob(draft_id=номер, mime_type="image/jpeg", size_bytes=4, data=b"\x00\x01\x02\x03")
    )
    сессия.flush()

    сессия.execute(delete(CaptureDraft).where(CaptureDraft.id == номер))
    сессия.flush()

    assert сессия.execute(select(CaptureBlob)).scalars().all() == []


def test_сырьё_возвращается_байт_в_байт(сессия: Session) -> None:
    """Инвариант хоста 1: файл лежит в базе, а не на диске контейнера."""
    номер = uuid.uuid4()
    содержимое = bytes(range(256))
    сессия.add(CaptureDraft(id=номер, modality="audio"))
    сессия.add(
        CaptureBlob(
            draft_id=номер,
            mime_type="audio/ogg",
            size_bytes=len(содержимое),
            data=содержимое,
        )
    )
    сессия.flush()
    сессия.expire_all()

    сохранённое = сессия.execute(select(CaptureBlob.data)).scalar_one()

    assert сохранённое == содержимое


def test_черновик_захвата_знает_только_три_модальности(сессия: Session) -> None:
    """Текст, фото, голос (ADR-019). Четвёртой в первом релизе нет."""
    сессия.add(CaptureDraft(modality="видео"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()

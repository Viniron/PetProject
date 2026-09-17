"""Уборка брошенных черновиков (Э8, §9).

Джоб удаляет данные owner, поэтому проверяется и то, что он убирает,
и - важнее - то, чего он не трогает: черновик, к которому ещё вернутся,
и dry-run, который обязан быть безвредным.
"""

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry, CaptureBlob, CaptureDraft, JobRun, Setting
from jarvis_api.domain import capture
from jarvis_api.jobs.capture_cleanup import JOB_NAME, run_once

СЕЙЧАС = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.UTC)


def настройки(**переопределения: object) -> Settings:
    основа: dict[str, object] = {"capture_draft_ttl_hours": 72}
    основа.update(переопределения)
    return Settings(**основа)  # type: ignore[arg-type]


def черновик(сессия: Session, часов_назад: int, *, с_сырьём: bool = False) -> CaptureDraft:
    строка = capture.создать(сессия, modality="text", source_text="встреча")
    if с_сырьём:
        сессия.add(
            CaptureBlob(draft_id=строка.id, mime_type="image/jpeg", size_bytes=3, data=b"jpg")
        )
    строка.created_at = СЕЙЧАС - dt.timedelta(hours=часов_назад)
    сессия.flush()
    return строка


def строк(сессия: Session, модель: type) -> int:
    return int(сессия.scalar(select(func.count()).select_from(модель)) or 0)


def действие(запись: AuditLogEntry) -> object:
    assert запись.detail is not None, "след без подробностей ничего не объясняет"
    return запись.detail["action"]


def test_просроченный_черновик_убирается_вместе_с_сырьём(сессия: Session) -> None:
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    черновик(сессия, 100, с_сырьём=True)
    свежий = черновик(сессия, 1)

    assert run_once(сессия, настройки(), apply=True, now=СЕЙЧАС) == 0

    оставшиеся = list(сессия.scalars(select(CaptureDraft)))
    assert [строка.id for строка in оставшиеся] == [свежий.id]
    assert строк(сессия, CaptureBlob) == 0, "сырьё обязано уйти вместе с черновиком"


def test_dry_run_ничего_не_удаляет(сессия: Session) -> None:
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    черновик(сессия, 100)

    assert run_once(сессия, настройки(), apply=False, now=СЕЙЧАС) == 0

    assert строк(сессия, CaptureDraft) == 1
    assert строк(сессия, JobRun) == 0, "dry-run не отмечается прогоном"


def test_уборка_оставляет_след(сессия: Session) -> None:
    """Исчезнувший черновик обязан быть объясним: инвариант 8.

    Без следа пропажа неотличима от «его никогда не было», а спросят
    об этом ровно тогда, когда пропадёт нужное.
    """
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    старый = черновик(сессия, 100)

    run_once(сессия, настройки(), apply=True, now=СЕЙЧАС)

    записи = list(сессия.scalars(select(AuditLogEntry).where(AuditLogEntry.kind == "capture")))
    assert [действие(запись) for запись in записи] == ["discarded"]
    assert записи[0].target == str(старый.id)
    отметка = сессия.scalars(select(JobRun).where(JobRun.job == JOB_NAME)).one()
    assert отметка.status == "ok"


def test_срок_берётся_из_конфига(сессия: Session) -> None:
    """Ручка в env, а не число в коде: ею owner регулирует, сколько сырьё

    захвата лежит в ночных дампах.
    """
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    черновик(сессия, 10)

    run_once(сессия, настройки(capture_draft_ttl_hours=6), apply=True, now=СЕЙЧАС)

    assert строк(сессия, CaptureDraft) == 0

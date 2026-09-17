"""Домен захвата (Э8): черновик, подтверждение, срок жизни.

Проверяется без HTTP - тем же прямым вызовом, ради которого `domain/`
и отделён от `api/`. Настоящая Postgres нужна здесь не для галочки:
каскад `capture_blobs -> capture_drafts` и `jsonb` - это ровно то,
что SQLite изобразил бы иначе.

Три вещи, ради которых тесты написаны, а не «на всякий случай»:

- **ключ события детерминирован от id черновика** (ADR-042) - на этом
  держится и повтор подтверждения после обрыва, и отсутствие дублей
  в календаре owner;
- **подтверждение неделимо**: событие появилось, черновик и его сырьё
  исчезли, и всё это одной транзакцией;
- **сырьё исчезает вместе с черновиком** - отменённая фотография не должна
  пережить отмену и уехать в ночной дамп.
"""

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.db.models import AuditLogEntry, CalendarEvent, CaptureBlob, CaptureDraft
from jarvis_api.domain import capture

СЕЙЧАС = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.UTC)
НАЧАЛО = dt.datetime(2026, 10, 21, 14, 0, tzinfo=dt.UTC)
КОНЕЦ = НАЧАЛО + dt.timedelta(hours=1)


def черновик_с_сырьём(
    сессия: Session, текст: str = "встреча с куратором в четверг"
) -> CaptureDraft:
    """Черновик, к которому приложены байты.

    Модальность остаётся `text`: фотографии до Э12 не принимаются, но
    таблица сырья существует с Э2, и каскад обязан работать уже сейчас -
    иначе о нём вспомнят в день, когда фотографии начнут приходить.
    """
    строка = capture.создать(сессия, modality="text", source_text=текст)
    сессия.add(CaptureBlob(draft_id=строка.id, mime_type="image/jpeg", size_bytes=3, data=b"jpg"))
    сессия.flush()
    return строка


def строк(сессия: Session, модель: type) -> int:
    return int(сессия.scalar(select(func.count()).select_from(модель)) or 0)


def действие(запись: AuditLogEntry) -> object:
    """Поле `detail` объявлено необязательным - здесь оно обязано быть."""
    assert запись.detail is not None, "след без подробностей ничего не объясняет"
    return запись.detail["action"]


def test_ключ_события_собран_из_id_черновика(сессия: Session) -> None:
    черновик = capture.создать(сессия, modality="text", source_text="стоматолог во вторник")

    assert capture.ключ_события(черновик.id) == f"capture:{черновик.id}"


def test_идентификатор_рождается_в_коде(сессия: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Инвариант 5: ключ не берётся у базы.

    Проверка не про красоту: `gen_random_uuid()` в колонке присвоил бы id
    при вставке, и ключ события зависел бы от того, дошла ли строка до базы.
    Подменённый `uuid4` доказывает обратное - id взялся из кода, а серверное
    умолчание до него не дошло.
    """
    подставной = uuid.UUID("0f1d4d2e-1111-4111-8111-111111111111")
    # Подменяется имя внутри модуля домена, а не сам `uuid.uuid4`: так видно,
    # что id берётся оттуда, и соседние тесты остаются с настоящими uuid.
    monkeypatch.setattr("jarvis_api.domain.capture.uuid.uuid4", lambda: подставной)

    черновик = capture.создать(сессия, modality="text", source_text="y")

    assert черновик.id == подставной
    assert capture.ключ_события(черновик.id) == f"capture:{подставной}"


def test_подтверждение_создаёт_событие_и_уносит_черновик(сессия: Session) -> None:
    черновик = черновик_с_сырьём(сессия)
    идентификатор = черновик.id

    событие = capture.подтвердить(
        сессия,
        черновик,
        title="Встреча с куратором",
        starts_at=НАЧАЛО,
        ends_at=КОНЕЦ,
        location="ауд. 2412",
        description=None,
    )

    assert событие.external_key == f"capture:{идентификатор}"
    assert событие.source == "capture"
    assert событие.calendar == "events"
    # В очереди, а не «записано»: в Google событие уедет отдельным шагом.
    assert событие.sync_state == "pending"
    assert событие.google_event_id is None
    # Пустой отпечаток означает «в календарь ещё ничего не отправляли».
    assert событие.content_hash == ""

    assert строк(сессия, CaptureDraft) == 0, "черновик обязан исчезнуть в той же транзакции"
    assert строк(сессия, CaptureBlob) == 0, "сырьё обязано уйти каскадом вместе с черновиком"


def test_повтор_подтверждения_находит_то_же_событие(сессия: Session) -> None:
    """Обрыв связи после записи не должен давать второе событие (ADR-042)."""
    черновик = capture.создать(сессия, modality="text", source_text="зубной")
    идентификатор = черновик.id
    первое = capture.подтвердить(
        сессия,
        черновик,
        title="Стоматолог",
        starts_at=НАЧАЛО,
        ends_at=КОНЕЦ,
        location=None,
        description=None,
    )

    найденное = capture.событие_захвата(сессия, идентификатор)

    assert найденное is not None
    assert найденное.external_key == первое.external_key
    assert строк(сессия, CalendarEvent) == 1


def test_подтверждение_оставляет_след_в_аудите(сессия: Session) -> None:
    """Инвариант 8: решение owner обязано быть видно в журнале.

    `kind` свой, а не `calendar_write`: там живут записи в Google, и
    «owner подтвердил» не должно теряться среди действий джоба.
    """
    черновик = capture.создать(сессия, modality="text", source_text="встреча")
    capture.подтвердить(
        сессия,
        черновик,
        title="Встреча",
        starts_at=НАЧАЛО,
        ends_at=КОНЕЦ,
        location=None,
        description=None,
    )

    записи = list(сессия.scalars(select(AuditLogEntry).where(AuditLogEntry.kind == "capture")))
    assert len(записи) == 1
    assert действие(записи[0]) == "confirmed"
    assert записи[0].actor == "api.capture"


def test_отмена_уносит_сырьё(сессия: Session) -> None:
    черновик = черновик_с_сырьём(сессия)

    capture.отменить(сессия, черновик)
    сессия.flush()

    assert строк(сессия, CaptureDraft) == 0
    assert строк(сессия, CaptureBlob) == 0, "отменённая фотография не должна дожить до дампа"
    записи = list(сессия.scalars(select(AuditLogEntry).where(AuditLogEntry.kind == "capture")))
    assert [действие(запись) for запись in записи] == ["discarded"]


def test_просроченные_считаются_от_создания(сессия: Session) -> None:
    """Граница срока - ровно `ttl_hours`, и она не задевает свежий черновик."""
    старый = capture.создать(сессия, modality="text", source_text="позавчерашний")
    свежий = capture.создать(сессия, modality="text", source_text="сегодняшний")
    # created_at ставит база (server_default), поэтому время подменяется явно:
    # тест проверяет правило отбора, а не то, как быстро он сам работает.
    старый.created_at = СЕЙЧАС - dt.timedelta(hours=73)
    свежий.created_at = СЕЙЧАС - dt.timedelta(hours=71)
    сессия.flush()

    найденные = capture.просроченные(сессия, now=СЕЙЧАС, ttl_hours=72)

    assert [строка.id for строка in найденные] == [старый.id]


def test_черновики_отдаются_новыми_сверху(сессия: Session) -> None:
    первый = capture.создать(сессия, modality="text", source_text="раньше")
    второй = capture.создать(сессия, modality="text", source_text="позже")
    первый.created_at = СЕЙЧАС - dt.timedelta(hours=2)
    второй.created_at = СЕЙЧАС
    сессия.flush()

    assert [строка.id for строка in capture.черновики(сессия)] == [второй.id, первый.id]


def test_разбор_пуст_до_слоя_моделей(сессия: Session) -> None:
    """Пустой `extracted` - это «поля заполняет owner», а не «модель молчит».

    Заготовка с правдоподобными датой и временем на этом месте была бы
    выдумкой, которую инвариант 9 запрещает прямо.
    """
    черновик = capture.создать(сессия, modality="text", source_text="что-то в четверг")

    наружу = capture.из_строки(черновик)

    assert наружу.extracted is None
    assert наружу.error is None

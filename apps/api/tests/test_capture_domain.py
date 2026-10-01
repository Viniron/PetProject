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

import pdfplumber
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
    """Черновик-фотография: байты лежат в `capture_blobs` (Э12в)."""
    return capture.создать(
        сессия, modality="image", source_text=текст, картинка=("image/jpeg", b"jpg")
    )


def pdf(страниц: int) -> bytes:
    """Настоящий PDF из пустых страниц: таблица ссылок посчитана честно.

    Собирается здесь, а не лежит фикстурой: число страниц - параметр теста,
    и файл на каждое число был бы шумом в репозитории.
    """
    объекты = ["<< /Type /Catalog /Pages 2 0 R >>"]
    дети = " ".join(f"{3 + i} 0 R" for i in range(страниц))
    объекты.append(f"<< /Type /Pages /Kids [{дети}] /Count {страниц} >>")
    объекты += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"] * страниц
    вывод = b"%PDF-1.4\n"
    смещения = []
    for номер, объект in enumerate(объекты, 1):
        смещения.append(len(вывод))
        вывод += f"{номер} 0 obj\n{объект}\nendobj\n".encode("ascii")
    таблица = len(вывод)
    вывод += f"xref\n0 {len(объекты) + 1}\n0000000000 65535 f \n".encode("ascii")
    вывод += b"".join(f"{с:010d} 00000 n \n".encode("ascii") for с in смещения)
    вывод += (
        f"trailer\n<< /Size {len(объекты) + 1} /Root 1 0 R >>\n" f"startxref\n{таблица}\n%%EOF\n"
    ).encode("ascii")
    return вывод


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


def test_разбор_пуст_пока_его_не_записали(сессия: Session) -> None:
    """Пустой `extracted` - это «поля заполняет owner», а не «модель молчит».

    Заготовка с правдоподобными датой и временем на этом месте была бы
    выдумкой, которую инвариант 9 запрещает прямо.
    """
    черновик = capture.создать(сессия, modality="text", source_text="что-то в четверг")

    наружу = capture.из_строки(черновик)

    assert наружу.extracted is None
    assert наружу.error is None


def test_итог_разбора_ложится_в_черновик(сессия: Session) -> None:
    черновик = capture.создать(сессия, modality="audio", source_text="зубной в пятницу")

    capture.записать_разбор(черновик, extracted=None, error="модель для разбора не назначена")
    сессия.flush()

    наружу = capture.из_строки(черновик)
    assert наружу.modality == "audio"
    assert наружу.error == "модель для разбора не назначена"


def test_байты_фото_лежат_отдельно_от_черновика(сессия: Session) -> None:
    черновик = черновик_с_сырьём(сессия)

    сырьё = сессия.get(CaptureBlob, черновик.id)

    assert сырьё is not None
    assert (сырьё.mime_type, сырьё.size_bytes, сырьё.data) == ("image/jpeg", 3, b"jpg")


@pytest.mark.parametrize(
    ("начало", "тип"),
    [
        (bytes.fromhex("ffd8ffe0") + b"JFIF", "image/jpeg"),
        (bytes.fromhex("89504e470d0a1a0a") + b"IHDR", "image/png"),
        (b"RIFF" + bytes(4) + b"WEBPVP8 ", "image/webp"),
        (b"%PDF-1.7\n", "application/pdf"),
    ],
)
def test_формат_файла_узнаётся_по_байтам(начало: bytes, тип: str) -> None:
    assert capture.тип_файла(начало) == тип


@pytest.mark.parametrize(
    "данные",
    [b"", b"%PD", b"GIF89a", b"RIFF" + bytes(4) + b"WAVE", b"\x00\x00\x00\x18ftypheic"],
)
def test_чужой_формат_не_узнаётся(данные: bytes) -> None:
    """HEIC и GIF тоже чужие: их не принимает один из назначенных провайдеров."""
    assert capture.тип_файла(данные) is None


@pytest.mark.parametrize("страниц", [1, 5, 12])
def test_страницы_pdf_считаются(страниц: int) -> None:
    assert capture.страниц_pdf(pdf(страниц)) == страниц


def test_битый_pdf_не_читается() -> None:
    with pytest.raises(capture.ФайлНеЧитается):
        capture.страниц_pdf(b"%PDF-1.7\nnot a document")


def test_защищённый_pdf_отвергается(monkeypatch: pytest.MonkeyPatch) -> None:
    """Даже открывшийся без пароля: провайдер шифрованный PDF не примет."""

    class Документ:
        doc = type("Doc", (), {"encryption": ("Standard", {})})()
        pages = [object()]

        def __enter__(self) -> "Документ":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(pdfplumber, "open", lambda _: Документ())

    with pytest.raises(capture.ФайлНеЧитается, match="защищён"):
        capture.страниц_pdf(pdf(1))

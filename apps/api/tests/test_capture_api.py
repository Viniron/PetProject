"""Эндпоинты захвата (Э8): черновик, отмена, подтверждение.

Google здесь недоступен намеренно и ни разу не подменяется: у настроек
стенда пуст `GOOGLE_SA_JSON`, то есть немедленная запись события заведомо
не удаётся. Это и есть главная проверка этапа - **отказ календаря не
отменяет подтверждения**: событие принято, лежит в базе, стоит в очереди
и придёт в Google джобом. Сеть при этом не трогается ни разу (`CLAUDE.md`):
`build_service` отказывает на пустом ключе, не открывая соединения.
"""

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import Стенд
from sqlalchemy import func, select
from test_capture_domain import pdf
from test_capture_parse import НАЗНАЧЕНИЕ, Подставной, ответ

from jarvis_api.api.routes_capture import адаптеры_захвата
from jarvis_api.db.models import AuditLogEntry, CalendarEvent, CaptureBlob, CaptureDraft
from jarvis_api.integrations import llm
from jarvis_api.main import app

НАЧАЛО = "2026-10-21T17:00:00+03:00"
КОНЕЦ = "2026-10-21T18:00:00+03:00"

ПОДТВЕРЖДЕНИЕ: dict[str, Any] = {
    "title": "Встреча с куратором",
    "starts_at": НАЧАЛО,
    "ends_at": КОНЕЦ,
    "location": "ауд. 2412",
}


@pytest.fixture(autouse=True)
def без_google(стенд: Стенд) -> None:
    """Google в этих тестах заведомо недоступен, и это не случайность.

    Ключ обнуляется явно, а не «он и так пуст»: окружение, в котором
    прогоняются тесты, может нести настоящий GOOGLE_SA_JSON, и тогда
    подтверждение ушло бы в живой календарь. Сеть в тестах замокана
    целиком (`CLAUDE.md`), а пустой ключ отказывает до первого сокета.
    """
    стенд.настройки = стенд.настройки.model_copy(update={"google_sa_json": ""})


@pytest.fixture(autouse=True)
def модели(стенд: Стенд) -> Iterator[dict[str, llm.Адаптер]]:
    """Провайдеры захвата подменены всегда, и по умолчанию их нет вовсе.

    По той же причине, что и Google выше: окружение прогона может нести
    настоящие ключи и `LLM_ROUTING`, и тогда «Извлечь» ушло бы к живой
    модели за деньги. Тест, которому модель нужна, кладёт сюда подставную.
    """
    стенд.настройки = стенд.настройки.model_copy(update={"llm_routing": ""})
    подключённые: dict[str, llm.Адаптер] = {}
    app.dependency_overrides[адаптеры_захвата] = lambda: подключённые
    try:
        yield подключённые
    finally:
        app.dependency_overrides.pop(адаптеры_захвата, None)


def с_моделью(
    стенд: Стенд, модели: dict[str, llm.Адаптер], *сценарий: llm.Ответ | Exception
) -> Подставной:
    стенд.настройки = стенд.настройки.model_copy(update={"llm_routing": НАЗНАЧЕНИЕ})
    основной = Подставной("провайдер-а", list(сценарий))
    модели[основной.имя] = основной
    return основной


# Самые короткие файлы, которые опознаются форматом: сигнатура и немного байт.
JPEG = bytes.fromhex("ffd8ffe0") + b"JFIF" + bytes(32)


def завести(стенд: Стенд, текст: str = "встреча с куратором в четверг") -> str:
    ответ = стенд.клиент.post("/api/capture/drafts", json={"modality": "text", "text": текст})
    assert ответ.status_code == 201, ответ.text
    return str(ответ.json()["id"])


def строк(стенд: Стенд, модель: type) -> int:
    return int(стенд.сессия.scalar(select(func.count()).select_from(модель)) or 0)


def test_черновик_заводится_и_виден_в_списке(стенд: Стенд) -> None:
    идентификатор = завести(стенд)

    ответ = стенд.клиент.get("/api/capture/drafts")

    assert ответ.status_code == 200
    черновики = ответ.json()["drafts"]
    assert [строка["id"] for строка in черновики] == [идентификатор]
    # Модель не назначена: разбора нет, причина названа, и клиент это видит.
    assert черновики[0]["extracted"] is None
    assert черновики[0]["error"] == "модель для разбора не назначена"
    assert черновики[0]["modality"] == "text"
    assert черновики[0]["timezone"] == "Europe/Moscow"


def test_фото_текстовой_ручкой_не_принимается(стенд: Стенд) -> None:
    """Фото идёт multipart своей ручкой, а не base64 внутри JSON."""
    ответ = стенд.клиент.post("/api/capture/drafts", json={"modality": "image", "text": "x"})

    assert ответ.status_code == 422
    assert строк(стенд, CaptureDraft) == 0


def test_пустой_текст_не_становится_черновиком(стенд: Стенд) -> None:
    ответ = стенд.клиент.post("/api/capture/drafts", json={"modality": "text", "text": "   "})

    assert ответ.status_code == 422
    assert ответ.json()["code"] == "validation_error"


def test_текстовому_входу_нужен_текст(стенд: Стенд) -> None:
    ответ = стенд.клиент.post("/api/capture/drafts", json={"modality": "text"})

    assert ответ.status_code == 422


def test_вставленный_буфер_обмена_отвергается(стенд: Стенд) -> None:
    """Потолок длины - защита базы и дампов, а не придирка к owner."""
    стенд.настройки = стенд.настройки.model_copy(update={"capture_text_max_chars": 10})

    ответ = стенд.клиент.post("/api/capture/drafts", json={"modality": "text", "text": "а" * 11})

    assert ответ.status_code == 422
    assert "10" in ответ.json()["message"]


def test_черновик_отменяется_и_второй_раз_не_находится(стенд: Стенд) -> None:
    идентификатор = завести(стенд)

    первый = стенд.клиент.delete(f"/api/capture/drafts/{идентификатор}")
    второй = стенд.клиент.delete(f"/api/capture/drafts/{идентификатор}")

    assert первый.status_code == 204
    assert второй.status_code == 404
    assert строк(стенд, CaptureDraft) == 0


def test_подтверждение_создаёт_событие_в_очереди(стенд: Стенд) -> None:
    """Google недоступен, и это ничего не меняет для owner.

    Событие принято, черновика больше нет, а `pending` - то самое
    «записывается», которое экран говорит вместо обещания записанного
    (инвариант 9).
    """
    идентификатор = завести(стенд)

    ответ = стенд.клиент.post(f"/api/capture/drafts/{идентификатор}/confirm", json=ПОДТВЕРЖДЕНИЕ)

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["key"] == f"capture:{идентификатор}"
    assert тело["sync_state"] == "pending"
    assert тело["synced_at"] is None
    assert тело["title"] == "Встреча с куратором"
    assert строк(стенд, CaptureDraft) == 0
    assert строк(стенд, CalendarEvent) == 1


def test_повтор_подтверждения_не_множит_события(стенд: Стенд) -> None:
    """Клиент не получил ответ и нажал ещё раз - в календаре по-прежнему одно."""
    идентификатор = завести(стенд)
    первый = стенд.клиент.post(f"/api/capture/drafts/{идентификатор}/confirm", json=ПОДТВЕРЖДЕНИЕ)

    второй = стенд.клиент.post(
        f"/api/capture/drafts/{идентификатор}/confirm",
        json={**ПОДТВЕРЖДЕНИЕ, "title": "Другое название"},
    )

    assert второй.status_code == 200
    assert второй.json()["key"] == первый.json()["key"]
    # Повтор отдаёт записанное, а не переписывает его: подтверждение уже
    # состоялось, и правка событий - отдельный этап.
    assert второй.json()["title"] == "Встреча с куратором"
    assert строк(стенд, CalendarEvent) == 1


def test_подтверждение_чужого_идентификатора_даёт_404(стенд: Стенд) -> None:
    ответ = стенд.клиент.post(
        "/api/capture/drafts/0f1d4d2e-0000-4000-8000-000000000000/confirm",
        json=ПОДТВЕРЖДЕНИЕ,
    )

    assert ответ.status_code == 404
    assert ответ.json()["code"] == "not_found"


def test_время_без_зоны_не_принимается(стенд: Стенд) -> None:
    """Инвариант 7. «17:00» без зоны - это событие, сдвинутое на три часа."""
    идентификатор = завести(стенд)

    ответ = стенд.клиент.post(
        f"/api/capture/drafts/{идентификатор}/confirm",
        json={
            **ПОДТВЕРЖДЕНИЕ,
            "starts_at": "2026-10-21T17:00:00",
            "ends_at": "2026-10-21T18:00:00",
        },
    )

    assert ответ.status_code == 422
    assert строк(стенд, CalendarEvent) == 0


def test_конец_не_позже_начала_не_принимается(стенд: Стенд) -> None:
    идентификатор = завести(стенд)

    ответ = стенд.клиент.post(
        f"/api/capture/drafts/{идентификатор}/confirm",
        json={**ПОДТВЕРЖДЕНИЕ, "ends_at": НАЧАЛО},
    )

    assert ответ.status_code == 422
    assert строк(стенд, CaptureDraft) == 1, "неудачное подтверждение не должно уносить черновик"


def test_событие_приведено_к_utc(стенд: Стенд) -> None:
    """В базе время только UTC (инвариант 7), зона запроса при этом законна."""
    идентификатор = завести(стенд)

    стенд.клиент.post(f"/api/capture/drafts/{идентификатор}/confirm", json=ПОДТВЕРЖДЕНИЕ)

    строка = стенд.сессия.scalars(select(CalendarEvent)).one()
    assert строка.starts_at == dt.datetime(2026, 10, 21, 14, 0, tzinfo=dt.UTC)
    assert строка.ends_at == dt.datetime(2026, 10, 21, 15, 0, tzinfo=dt.UTC)


# --- Э12в: разбор моделью -----------------------------------------------------


def test_разбор_моделью_приходит_в_зоне_owner(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    """Модель ответила местным временем, экран видит его с зоной owner (§8.4)."""
    с_моделью(стенд, модели, ответ())

    тело = стенд.клиент.post(
        "/api/capture/drafts", json={"text": "встреча с куратором в четверг после физики"}
    ).json()

    assert тело["error"] is None
    разбор = тело["extracted"]
    assert разбор["title"] == "Встреча с куратором"
    assert разбор["starts_at"] == "2026-10-22T17:00:00+03:00"
    assert разбор["ends_at"] == "2026-10-22T18:00:00+03:00"
    assert разбор["time_uncertain"] is False
    assert разбор["duration_assumed"] is False


def test_разбор_виден_и_в_списке(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    с_моделью(стенд, модели, ответ(ends_at=None))
    завести(стенд)

    черновик = стенд.клиент.get("/api/capture/drafts").json()["drafts"][0]

    assert черновик["extracted"]["ends_at"] == "2026-10-22T18:00:00+03:00"
    assert черновик["extracted"]["duration_assumed"] is True


def test_отказ_модели_не_отменяет_черновика(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    """Геоблок на плате (до Э12г) - это форма для ручного ввода, а не 500."""
    с_моделью(стенд, модели, llm.ВыходЗаблокирован("403 Request not allowed"))

    ответ_ = стенд.клиент.post("/api/capture/drafts", json={"text": "зубной в пятницу"})

    assert ответ_.status_code == 201, ответ_.text
    assert ответ_.json()["extracted"] is None
    assert ответ_.json()["error"] == "модели недоступны из сети платы"
    assert строк(стенд, CaptureDraft) == 1


def test_голос_приходит_расшифровкой(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    основной = с_моделью(стенд, модели, ответ())

    тело = стенд.клиент.post(
        "/api/capture/drafts", json={"modality": "audio", "text": "в среду в три в деканат"}
    ).json()

    assert тело["modality"] == "audio"
    assert тело["source_text"] == "в среду в три в деканат"
    assert "надиктована" in основной.запросы[0].переменная_часть
    assert строк(стенд, CaptureBlob) == 0, "звука у сервера нет - хранить нечего"


def test_вызов_модели_в_аудите(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    с_моделью(стенд, модели, ответ())
    завести(стенд)

    записи = list(
        стенд.сессия.scalars(select(AuditLogEntry).where(AuditLogEntry.kind == "llm_call"))
    )
    assert [(з.status, з.provider, з.model) for з in записи] == [("ok", "провайдер-а", "модель-1")]
    assert записи[0].cost_usd is not None


def test_фото_разбирается_и_байты_ложатся_в_базу(
    стенд: Стенд, модели: dict[str, llm.Адаптер]
) -> None:
    основной = с_моделью(стенд, модели, ответ(title="Открытая лекция"))

    ответ_ = стенд.клиент.post(
        "/api/capture/drafts/file",
        # Заголовок клиента нарочно врёт: формат определяется по байтам.
        files={"file": ("IMG_2041.png", JPEG, "image/png")},
    )

    assert ответ_.status_code == 201, ответ_.text
    тело = ответ_.json()
    assert тело["modality"] == "image"
    assert тело["source_text"] is None
    assert тело["extracted"]["title"] == "Открытая лекция"
    assert основной.запросы[0].изображения == (llm.Изображение("image/jpeg", JPEG),)
    сырьё = стенд.сессия.scalars(select(CaptureBlob)).one()
    assert (сырьё.mime_type, сырьё.size_bytes) == ("image/jpeg", len(JPEG))


def test_подтверждение_фото_уносит_снимок(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    с_моделью(стенд, модели, ответ())
    идентификатор = стенд.клиент.post(
        "/api/capture/drafts/file", files={"file": ("a.jpg", JPEG, "image/jpeg")}
    ).json()["id"]

    ответ_ = стенд.клиент.post(f"/api/capture/drafts/{идентификатор}/confirm", json=ПОДТВЕРЖДЕНИЕ)

    assert ответ_.status_code == 200, ответ_.text
    assert строк(стенд, CaptureBlob) == 0, "снимок не живёт дольше черновика"


def test_не_картинка_отвергается_до_модели(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    основной = с_моделью(стенд, модели)

    ответ_ = стенд.клиент.post(
        "/api/capture/drafts/file", files={"file": ("a.jpg", b"GIF89a....", "image/jpeg")}
    )

    assert ответ_.status_code == 422
    assert ответ_.json()["code"] == "capture_file_unsupported"
    assert основной.запросы == [], "отвергнутый файл не стоит денег"
    assert строк(стенд, CaptureDraft) == 0


def test_большое_фото_отвергается_до_модели(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    основной = с_моделью(стенд, модели)
    стенд.настройки = стенд.настройки.model_copy(update={"capture_image_max_bytes": 16})

    ответ_ = стенд.клиент.post(
        "/api/capture/drafts/file", files={"file": ("a.jpg", JPEG, "image/jpeg")}
    )

    assert ответ_.status_code == 422
    assert ответ_.json()["code"] == "capture_file_too_large"
    assert основной.запросы == []
    assert строк(стенд, CaptureDraft) == 0


def test_pdf_разбирается_документом(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    основной = с_моделью(стенд, модели, ответ(title="Лекция"))
    файл = pdf(2)

    ответ_ = стенд.клиент.post(
        "/api/capture/drafts/file", files={"file": ("afisha.pdf", файл, "application/pdf")}
    )

    assert ответ_.status_code == 201, ответ_.text
    # Модальность та же, что у фото (решение owner): различие - в типе сырья.
    assert ответ_.json()["modality"] == "image"
    assert основной.запросы[0].изображения == (llm.Изображение("application/pdf", файл),)
    assert "PDF-документ" in основной.запросы[0].переменная_часть
    assert стенд.сессия.scalars(select(CaptureBlob)).one().mime_type == "application/pdf"


def test_длинный_pdf_отвергается_до_модели(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    основной = с_моделью(стенд, модели)
    стенд.настройки = стенд.настройки.model_copy(update={"capture_pdf_max_pages": 5})

    ответ_ = стенд.клиент.post(
        "/api/capture/drafts/file", files={"file": ("a.pdf", pdf(6), "application/pdf")}
    )

    assert ответ_.status_code == 422
    assert ответ_.json()["code"] == "capture_pdf_too_long"
    assert "6 страниц" in ответ_.json()["message"]
    assert основной.запросы == []
    assert строк(стенд, CaptureDraft) == 0


def test_битый_pdf_отвергается_до_модели(стенд: Стенд, модели: dict[str, llm.Адаптер]) -> None:
    основной = с_моделью(стенд, модели)

    ответ_ = стенд.клиент.post(
        "/api/capture/drafts/file", files={"file": ("a.pdf", b"%PDF-1.7 ...", "application/pdf")}
    )

    assert ответ_.status_code == 422
    assert ответ_.json()["code"] == "capture_file_unreadable"
    assert основной.запросы == []
    assert строк(стенд, CaptureDraft) == 0

"""Эндпоинты захвата (Э8): черновик, отмена, подтверждение.

Google здесь недоступен намеренно и ни разу не подменяется: у настроек
стенда пуст `GOOGLE_SA_JSON`, то есть немедленная запись события заведомо
не удаётся. Это и есть главная проверка этапа - **отказ календаря не
отменяет подтверждения**: событие принято, лежит в базе, стоит в очереди
и придёт в Google джобом. Сеть при этом не трогается ни разу (`CLAUDE.md`):
`build_service` отказывает на пустом ключе, не открывая соединения.
"""

import datetime as dt
from typing import Any

import pytest
from conftest import Стенд
from sqlalchemy import func, select

from jarvis_api.db.models import CalendarEvent, CaptureDraft

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
    # До слоя моделей разбора нет, и клиент обязан это видеть.
    assert черновики[0]["extracted"] is None
    assert черновики[0]["modality"] == "text"


@pytest.mark.parametrize("modality", ["image", "audio"])
def test_фото_и_голос_отвергаются_с_причиной(стенд: Стенд, modality: str) -> None:
    """Режимы видны в контракте, но погашены (решение owner 2026-09-17).

    Молчаливый приём сырья был бы хуже отказа: байты легли бы в базу,
    уехали в ночной дамп и удалились по сроку, так и не став событием.
    """
    ответ = стенд.клиент.post("/api/capture/drafts", json={"modality": modality})

    assert ответ.status_code == 422
    assert ответ.json()["code"] == "capture_modality_unavailable"
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

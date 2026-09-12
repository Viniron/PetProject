"""Эндпоинты периодов исключений (Э6, §2.5).

Проверяется то, ради чего они и заведены: период виден в сетке, вносится
и убирается, системная пометка руками не трогается, а каждая правка
оставляет след в аудите.
"""

import datetime as dt
import inspect

from conftest import Стенд
from sqlalchemy import func, select

from jarvis_api.api.routes_day_flags import внести_период, список_периодов, убрать_период
from jarvis_api.db.models import AuditLogEntry, DayFlag

СЕЙЧАС = dt.datetime(2026, 10, 14, 4, 10, tzinfo=dt.UTC)
СРЕДА = dt.date(2026, 10, 14)

ПЕРИОД = {
    "starts_on": "2026-10-12",
    "ends_on": "2026-10-16",
    "reason": "отъезд",
    "note": "олимпиада в Москве",
}


def строк_в_аудите(стенд: Стенд) -> int:
    запрос = (
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.kind == "day_flag_write")
    )
    return стенд.сессия.scalar(запрос) or 0


def список(стенд: Стенд, **параметры: str) -> dict[str, list[dict[str, object]]]:
    ответ: dict[str, list[dict[str, object]]] = стенд.клиент.get(
        "/api/day-flags", params=параметры
    ).json()
    return ответ


def test_период_создаётся_и_отдаётся_целиком(стенд: Стенд) -> None:
    ответ = стенд.клиент.post("/api/day-flags", json=ПЕРИОД)

    assert ответ.status_code == 201
    тело = ответ.json()
    assert тело["starts_on"] == "2026-10-12"
    assert тело["ends_on"] == "2026-10-16"
    assert тело["reason"] == "отъезд"
    assert тело["note"] == "олимпиада в Москве"
    assert тело["manual"] is True
    assert isinstance(тело["id"], int)


def test_созданный_период_виден_в_сетке_календаря(стенд: Стенд) -> None:
    """Ради этого эндпоинт и существует: чип на дне в макете календаря."""
    стенд.сейчас = СЕЙЧАС
    стенд.клиент.post("/api/day-flags", json=ПЕРИОД)

    дни = стенд.клиент.get("/api/calendar", params={"view": "week"}).json()["days"]

    по_датам = {день["date"]: день["flags"] for день in дни}
    assert [п["reason"] for п in по_датам["2026-10-14"]] == ["отъезд"]
    # Границы включительные с обеих сторон, а 17-е уже вне периода.
    assert по_датам["2026-10-12"] and по_датам["2026-10-16"]
    assert по_датам["2026-10-17"] == []


def test_создание_и_аудит_одной_транзакцией(стенд: Стенд) -> None:
    было = строк_в_аудите(стенд)

    id_ = стенд.клиент.post("/api/day-flags", json=ПЕРИОД).json()["id"]

    assert строк_в_аудите(стенд) == было + 1
    запись = стенд.сессия.scalars(
        select(AuditLogEntry).where(AuditLogEntry.target == str(id_))
    ).one()
    assert запись.actor == "api.day_flags"
    assert запись.status == "ok"
    assert запись.detail is not None
    assert запись.detail["action"] == "created"
    assert запись.detail["reason"] == "отъезд"


def test_период_удаляется_вместе_со_следом_в_аудите(стенд: Стенд) -> None:
    id_ = стенд.клиент.post("/api/day-flags", json=ПЕРИОД).json()["id"]
    было = строк_в_аудите(стенд)

    ответ = стенд.клиент.delete(f"/api/day-flags/{id_}")

    assert ответ.status_code == 204
    assert ответ.content == b""
    assert стенд.сессия.get(DayFlag, id_) is None
    assert строк_в_аудите(стенд) == было + 1
    последняя = стенд.сессия.scalars(
        select(AuditLogEntry).where(AuditLogEntry.target == str(id_)).order_by(AuditLogEntry.id)
    ).all()[-1]
    assert последняя.detail is not None
    assert последняя.detail["action"] == "deleted"
    # Причина названа в аудите: после удаления строки её больше негде взять.
    assert последняя.detail["reason"] == "отъезд"


def test_список_отдаёт_пересекающие_интервал(стенд: Стенд) -> None:
    """Период, начавшийся до интервала и кончившийся после, обязан попасть:
    иначе экран покажет обычную неделю посреди периода исключений."""
    стенд.сессия.add(
        DayFlag(starts_on=dt.date(2026, 10, 1), ends_on=dt.date(2026, 10, 31), reason="сессия")
    )
    стенд.сессия.add(
        DayFlag(starts_on=dt.date(2026, 9, 1), ends_on=dt.date(2026, 9, 3), reason="давнее")
    )
    стенд.сессия.flush()

    тело = список(стенд, **{"from": "2026-10-12", "to": "2026-10-18"})

    assert [п["reason"] for п in тело["flags"]] == ["сессия"]


def test_список_без_параметров_берёт_окно_забора(стенд: Стенд) -> None:
    """Умолчание - окно `sync_window` вокруг сегодня, а не «все периоды»."""
    стенд.сейчас = СЕЙЧАС
    назад = стенд.настройки.itmo_sync_days_back
    вперёд = стенд.настройки.itmo_sync_days_ahead
    стенд.сессия.add(DayFlag(starts_on=СРЕДА, ends_on=СРЕДА, reason="внутри окна"))
    стенд.сессия.add(
        DayFlag(
            starts_on=СРЕДА - dt.timedelta(days=назад + 5),
            ends_on=СРЕДА - dt.timedelta(days=назад + 1),
            reason="до окна",
        )
    )
    стенд.сессия.add(
        DayFlag(
            starts_on=СРЕДА + dt.timedelta(days=вперёд + 1),
            ends_on=СРЕДА + dt.timedelta(days=вперёд + 5),
            reason="после окна",
        )
    )
    стенд.сессия.flush()

    тело = список(стенд)

    assert [п["reason"] for п in тело["flags"]] == ["внутри окна"]


def test_список_ответом_объектом_а_не_массивом(стенд: Стенд) -> None:
    """Голый массив нельзя расширить ни одним полем, не сломав клиента."""
    тело = список(стенд)

    assert isinstance(тело, dict)
    assert тело["flags"] == []


def test_интервал_задом_наперёд_отклонён(стенд: Стенд) -> None:
    ответ = стенд.клиент.get("/api/day-flags", params={"from": "2026-10-18", "to": "2026-10-12"})

    assert ответ.status_code == 422
    assert ответ.json()["code"] == "validation_error"
    assert "задом наперёд" in ответ.json()["message"]


def test_период_задом_наперёд_отклонён(стенд: Стенд) -> None:
    """Отказ обязан назвать обе даты: сообщение драйвера о нарушенном CHECK
    owner ничего не объясняет."""
    ответ = стенд.клиент.post(
        "/api/day-flags",
        json={"starts_on": "2026-10-16", "ends_on": "2026-10-12", "reason": "отъезд"},
    )

    assert ответ.status_code == 422
    тело = ответ.json()
    assert тело["code"] == "validation_error"
    assert тело["details"] is not None
    assert any("2026-10-16" in строка and "2026-10-12" in строка for строка in тело["details"])


def test_системная_пометка_руками_не_вносится(стенд: Стенд) -> None:
    ответ = стенд.клиент.post(
        "/api/day-flags",
        json={"starts_on": "2026-10-12", "ends_on": "2026-10-12", "reason": "late_classes"},
    )

    assert ответ.status_code == 422
    assert any("SPEC §4" in строка for строка in ответ.json()["details"])


def test_системная_пометка_руками_не_снимается(стенд: Стенд) -> None:
    """409, а не 403: дело не в правах - снятая пометка вернулась бы сама."""
    строка = DayFlag(starts_on=СРЕДА, ends_on=СРЕДА, reason="late_classes")
    стенд.сессия.add(строка)
    стенд.сессия.flush()
    было = строк_в_аудите(стенд)

    ответ = стенд.клиент.delete(f"/api/day-flags/{строка.id}")

    assert ответ.status_code == 409
    assert ответ.json()["code"] == "conflict"
    assert ответ.json()["retryable"] is False
    assert стенд.сессия.get(DayFlag, строка.id) is not None
    assert строк_в_аудите(стенд) == было


def test_пустая_причина_отклонена(стенд: Стенд) -> None:
    ответ = стенд.клиент.post(
        "/api/day-flags",
        json={"starts_on": "2026-10-12", "ends_on": "2026-10-12", "reason": "   "},
    )

    assert ответ.status_code == 422


def test_слишком_длинная_заметка_отклонена_валидатором(стенд: Стенд) -> None:
    """До базы такое не доходит: 422 от Pydantic, а не DataError драйвера."""
    ответ = стенд.клиент.post(
        "/api/day-flags",
        json={
            "starts_on": "2026-10-12",
            "ends_on": "2026-10-12",
            "reason": "отъезд",
            "note": "я" * 300,
        },
    )

    assert ответ.status_code == 422
    assert ответ.json()["code"] == "validation_error"


def test_удаление_несуществующего_это_404_в_нашем_формате(стенд: Стенд) -> None:
    ответ = стенд.клиент.delete("/api/day-flags/999999")

    assert ответ.status_code == 404
    тело = ответ.json()
    assert тело["code"] == "not_found"
    assert тело["retryable"] is False
    assert "999999" in тело["message"]


def test_день_в_один_день_законен(стенд: Стенд) -> None:
    ответ = стенд.клиент.post(
        "/api/day-flags",
        json={"starts_on": "2026-10-12", "ends_on": "2026-10-12", "reason": "поездка"},
    )

    assert ответ.status_code == 201


def test_перекрывающиеся_ручные_периоды_законны(стенд: Стенд) -> None:
    """§2.5 требует пересчёта отставания, а не запрета на перекрытие."""
    стенд.клиент.post("/api/day-flags", json=ПЕРИОД)

    ответ = стенд.клиент.post(
        "/api/day-flags",
        json={"starts_on": "2026-10-14", "ends_on": "2026-10-20", "reason": "болезнь"},
    )

    assert ответ.status_code == 201
    тело = список(стенд, **{"from": "2026-10-14", "to": "2026-10-14"})
    assert {п["reason"] for п in тело["flags"]} == {"отъезд", "болезнь"}


def test_период_задним_числом_законен(стенд: Стенд) -> None:
    стенд.сейчас = СЕЙЧАС

    ответ = стенд.клиент.post(
        "/api/day-flags",
        json={"starts_on": "2026-09-01", "ends_on": "2026-09-07", "reason": "болезнь"},
    )

    assert ответ.status_code == 201


def test_чтение_списка_в_аудит_не_пишет(стенд: Стенд) -> None:
    было = строк_в_аудите(стенд)

    стенд.клиент.get("/api/day-flags")

    assert строк_в_аудите(стенд) == было


def test_эндпоинты_не_корутины(стенд: Стенд) -> None:
    """Синхронный запрос в базу внутри `async def` заблокировал бы цикл
    событий вместе с `/health`. Ruff этого не ловит."""
    for функция in (список_периодов, внести_период, убрать_период):
        assert not inspect.iscoroutinefunction(функция)

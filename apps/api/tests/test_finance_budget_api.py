"""Ручки недельного бюджета (Ф13): контракт ответов, отказы, разнос поправок.

Арифметика лимита, итога и распределения проверена прямыми вызовами
(`test_finance_budget.py`): она тихая, и гонять её через `TestClient` значило
бы проверять заодно разбор JSON. Здесь - то, что видно только через HTTP:
форма ответа, коды отказов, «сегодня» из зоны owner и то, что импорт выписки
разносит поправку в закрытую неделю.

Сеть не трогается: выписка берётся из фикстуры, моделей бюджет не зовёт.
"""

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from conftest import Стенд, байты_фикстуры
from sqlalchemy import select

from jarvis_api.db.models import FinDaySpend, FinWeekBudget

# Неделя 14-20 сентября 2026 и дни внутри неё. Даты фиксированные: тест,
# зависящий от системных часов, однажды позеленел бы по календарю.
ПОНЕДЕЛЬНИК = dt.date(2026, 9, 14)
ВТОРНИК = dt.date(2026, 9, 15)
ВОСКРЕСЕНЬЕ = dt.date(2026, 9, 20)
СЛЕДУЮЩИЙ = dt.date(2026, 9, 21)

# Полдень по Москве: в UTC это 09:00, и «сегодня» owner не съезжает на сутки.
В_ПОНЕДЕЛЬНИК = dt.datetime(2026, 9, 14, 9, 0, tzinfo=dt.UTC)
В_ВОСКРЕСЕНЬЕ = dt.datetime(2026, 9, 20, 9, 0, tzinfo=dt.UTC)
В_СЛЕДУЮЩИЙ = dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)

БЮДЖЕТ = "7000.00"
ФАЙЛ = "tbank_august.csv"


def выписка() -> dict[str, Any]:
    """Настоящая августовская выгрузка Т-Банка - та же, что у тестов импорта."""
    return {"files": (ФАЙЛ, байты_фикстуры("statements", ФАЙЛ), "text/csv")}


@pytest.fixture(autouse=True)
def понедельник(стенд: Стенд) -> None:
    """Умолчание всех проверок - утро понедельника: открыты семь дней."""
    стенд.сейчас = В_ПОНЕДЕЛЬНИК


def бюджет(стенд: Стенд, неделя: dt.date = ПОНЕДЕЛЬНИК, сумма: str = БЮДЖЕТ) -> dict[str, Any]:
    ответ = стенд.клиент.put(
        f"/api/finance/budget/weeks/{неделя.isoformat()}", json={"amount": сумма}
    )
    assert ответ.status_code == 200, ответ.text
    тело: dict[str, Any] = ответ.json()
    return тело


def день(стенд: Стенд, дата: dt.date, сумма: str, заметка: str | None = None) -> dict[str, Any]:
    ответ = стенд.клиент.put(
        f"/api/finance/budget/days/{дата.isoformat()}",
        json={"amount": сумма, "note": заметка},
    )
    assert ответ.status_code == 200, ответ.text
    тело: dict[str, Any] = ответ.json()
    return тело


def неделю(стенд: Стенд, **параметры: Any) -> dict[str, Any]:
    ответ = стенд.клиент.get("/api/finance/budget", params=параметры)
    assert ответ.status_code == 200, ответ.text
    тело: dict[str, Any] = ответ.json()
    return тело


# --- чтение -----------------------------------------------------------------


def test_неделя_без_бюджета_говорит_словами(стенд: Стенд) -> None:
    """Бюджет не задан - null, а не ноль и не среднее по прошлым неделям."""
    тело = неделю(стенд)

    assert тело["week"]["week_start"] == ПОНЕДЕЛЬНИК.isoformat()
    assert тело["week"]["week_end"] == ВОСКРЕСЕНЬЕ.isoformat()
    assert тело["week"]["budget"] is None
    assert тело["week"]["limit"]["amount"] is None
    assert тело["week"]["limit"]["budget_set"] is False
    assert тело["week"]["limit"]["open_days"] == 7
    assert тело["today"] == ПОНЕДЕЛЬНИК.isoformat()
    assert тело["timezone"] == "Europe/Moscow"


def test_копилка_и_отложенное_стоят_рядом(стенд: Стенд) -> None:
    """Две разные цифры, и разница между ними - напоминание перевести деньги."""
    тело = неделю(стенд)

    assert Decimal(тело["savings_jar"]) == Decimal("0.00")
    # Книжка пуста, счетов нет - «Отложено» честный ноль, а не «не размечено».
    assert Decimal(тело["saved_real"]) == Decimal("0.00")


def test_неделя_только_с_понедельника(стенд: Стенд) -> None:
    ответ = стенд.клиент.get("/api/finance/budget", params={"week": ВТОРНИК.isoformat()})

    assert ответ.status_code == 422, ответ.text
    assert ответ.json()["code"] == "не_понедельник"


def test_дни_недели_отдаются_все_семь(стенд: Стенд) -> None:
    бюджет(стенд)
    тело = неделю(стенд)

    дни = тело["week"]["days"]
    assert [д["day"] for д in дни][0] == ПОНЕДЕЛЬНИК.isoformat()
    assert len(дни) == 7
    assert all(д["amount"] is None and д["source"] == "unknown" for д in дни)


def test_список_недель_идёт_подряд(стенд: Стенд) -> None:
    бюджет(стенд)

    ответ = стенд.клиент.get(
        "/api/finance/budget/weeks", params={"from": ПОНЕДЕЛЬНИК.isoformat(), "weeks": 3}
    )

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert [н["week_start"] for н in тело["weeks"]] == [
        ПОНЕДЕЛЬНИК.isoformat(),
        (ПОНЕДЕЛЬНИК + dt.timedelta(days=7)).isoformat(),
        (ПОНЕДЕЛЬНИК + dt.timedelta(days=14)).isoformat(),
    ]
    assert Decimal(тело["weeks"][0]["budget"]) == Decimal(БЮДЖЕТ)
    assert тело["weeks"][1]["budget"] is None


def test_список_недель_без_параметров_начинается_с_текущей(стенд: Стенд) -> None:
    тело = стенд.клиент.get("/api/finance/budget/weeks").json()

    assert тело["from_week"] == ПОНЕДЕЛЬНИК.isoformat()
    assert len(тело["weeks"]) == 5


# --- запись -----------------------------------------------------------------


def test_бюджет_вносится_и_даёт_лимит(стенд: Стенд) -> None:
    тело = бюджет(стенд)

    assert Decimal(тело["budget"]) == Decimal(БЮДЖЕТ)
    assert Decimal(тело["limit"]["amount"]) == Decimal("1000.00")
    assert тело["limit"]["for_day"] == ПОНЕДЕЛЬНИК.isoformat()


def test_отрицательный_бюджет_отклонён(стенд: Стенд) -> None:
    ответ = стенд.клиент.put(
        f"/api/finance/budget/weeks/{ПОНЕДЕЛЬНИК.isoformat()}", json={"amount": "-1.00"}
    )

    assert ответ.status_code == 422, ответ.text


def test_вечерний_ввод_переводит_цифру_на_завтра(стенд: Стенд) -> None:
    """Без подписи «на какой день» эта цифра читается ровно наоборот."""
    бюджет(стенд)

    тело = день(стенд, ПОНЕДЕЛЬНИК, "1500.00", "обед и метро")

    assert тело["previous"] is None
    assert Decimal(тело["amount"]) == Decimal("1500.00")
    assert тело["week"]["limit"]["for_day"] == ВТОРНИК.isoformat()
    assert тело["week"]["limit"]["open_days"] == 6
    assert Decimal(тело["week"]["limit"]["amount"]) == Decimal("916.66")


def test_повторный_ввод_называет_прошлую_сумму(стенд: Стенд) -> None:
    бюджет(стенд)
    день(стенд, ПОНЕДЕЛЬНИК, "1500.00")

    тело = день(стенд, ПОНЕДЕЛЬНИК, "1800.00")

    assert Decimal(тело["previous"]) == Decimal("1500.00")
    стенд.сессия.expire_all()
    assert len(стенд.сессия.scalars(select(FinDaySpend)).all()) == 1


def test_снятие_дня_возвращает_его_в_неизвестно(стенд: Стенд) -> None:
    бюджет(стенд)
    день(стенд, ПОНЕДЕЛЬНИК, "1500.00")

    ответ = стенд.клиент.delete(f"/api/finance/budget/days/{ПОНЕДЕЛЬНИК.isoformat()}")

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["amount"] is None
    assert Decimal(тело["previous"]) == Decimal("1500.00")
    assert тело["week"]["limit"]["for_day"] == ПОНЕДЕЛЬНИК.isoformat()
    assert тело["week"]["limit"]["open_days"] == 7
    assert тело["week"]["days"][0]["source"] == "unknown"


def test_снятие_невнесённого_дня_404(стенд: Стенд) -> None:
    ответ = стенд.клиент.delete(f"/api/finance/budget/days/{ПОНЕДЕЛЬНИК.isoformat()}")

    assert ответ.status_code == 404, ответ.text
    assert ответ.json()["code"] == "нет_дня"


# --- итог недели и распределение --------------------------------------------


def закрыть_неделю(стенд: Стенд) -> None:
    """Все семь дней внесены руками - то самое воскресенье, когда неделя готова."""
    стенд.сейчас = В_ВОСКРЕСЕНЬЕ
    бюджет(стенд)
    for сдвиг in range(7):
        день(стенд, ПОНЕДЕЛЬНИК + dt.timedelta(days=сдвиг), "1000.00")


def test_воскресенье_с_внесённой_тратой_даёт_итог_вместо_лимита(стенд: Стенд) -> None:
    """Делитель обнулился - деления на ноль нет, вместо лимита итог."""
    закрыть_неделю(стенд)

    тело = неделю(стенд)

    assert тело["week"]["limit"]["amount"] is None
    assert тело["week"]["limit"]["open_days"] == 0
    assert тело["week"]["ready_to_settle"] is True
    assert Decimal(тело["week"]["outcome"]["amount"]) == Decimal("0.00")


def test_распределение_идущей_недели_отклонено(стенд: Стенд) -> None:
    бюджет(стенд)

    ответ = стенд.клиент.post(
        f"/api/finance/budget/weeks/{ПОНЕДЕЛЬНИК.isoformat()}/settle",
        json={"to_next": "0.00", "to_savings": "0.00"},
    )

    assert ответ.status_code == 409, ответ.text
    assert ответ.json()["code"] == "неделя_не_готова"


def распределить(стенд: Стенд, в_следующую: str, в_копилку: str) -> Any:
    return стенд.клиент.post(
        f"/api/finance/budget/weeks/{ПОНЕДЕЛЬНИК.isoformat()}/settle",
        json={"to_next": в_следующую, "to_savings": в_копилку},
    )


def test_излишек_расходится_по_двум_адресам(стенд: Стенд) -> None:
    стенд.сейчас = В_ВОСКРЕСЕНЬЕ
    бюджет(стенд)
    for сдвиг in range(7):
        день(стенд, ПОНЕДЕЛЬНИК + dt.timedelta(days=сдвиг), "800.00")

    ответ = распределить(стенд, "1000.00", "400.00")

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert Decimal(тело["settlement"]["to_next"]) == Decimal("1000.00")
    assert Decimal(тело["settlement"]["settled_spend"]) == Decimal("5600.00")
    assert тело["settled"] is True

    стенд.сейчас = В_СЛЕДУЮЩИЙ
    следующая = неделю(стенд, week=СЛЕДУЮЩИЙ.isoformat())
    assert Decimal(следующая["week"]["carry"]) == Decimal("1000.00")
    assert Decimal(следующая["savings_jar"]) == Decimal("400.00")


def test_распределение_обязано_сойтись_с_итогом(стенд: Стенд) -> None:
    закрыть_неделю(стенд)

    ответ = распределить(стенд, "500.00", "0.00")

    assert ответ.status_code == 409, ответ.text
    assert ответ.json()["code"] == "не_сходится"


def test_копилка_в_минус_не_уходит(стенд: Стенд) -> None:
    """Покрыть перерасход можно только тем, что в копилке есть."""
    стенд.сейчас = В_ВОСКРЕСЕНЬЕ
    бюджет(стенд)
    for сдвиг in range(7):
        день(стенд, ПОНЕДЕЛЬНИК + dt.timedelta(days=сдвиг), "1100.00")

    ответ = распределить(стенд, "0.00", "-700.00")

    assert ответ.status_code == 409, ответ.text
    assert ответ.json()["code"] == "копилка_пуста"


def test_переигрывание_меняет_адрес_но_не_сумму(стенд: Стенд) -> None:
    стенд.сейчас = В_ВОСКРЕСЕНЬЕ
    бюджет(стенд)
    for сдвиг in range(7):
        день(стенд, ПОНЕДЕЛЬНИК + dt.timedelta(days=сдвиг), "800.00")
    распределить(стенд, "0.00", "1400.00")

    повтор = распределить(стенд, "1400.00", "0.00")

    assert повтор.status_code == 200, повтор.text
    стенд.сейчас = В_СЛЕДУЮЩИЙ
    следующая = неделю(стенд, week=СЛЕДУЮЩИЙ.isoformat())
    # Копилка не удвоилась: своё же прошлое решение из неё вычтено.
    assert Decimal(следующая["savings_jar"]) == Decimal("0.00")
    assert Decimal(следующая["week"]["carry"]) == Decimal("1400.00")


def test_бюджет_распределённой_недели_заморожен(стенд: Стенд) -> None:
    закрыть_неделю(стенд)
    assert распределить(стенд, "0.00", "0.00").status_code == 200

    ответ = стенд.клиент.put(
        f"/api/finance/budget/weeks/{ПОНЕДЕЛЬНИК.isoformat()}", json={"amount": "9000.00"}
    )

    assert ответ.status_code == 409, ответ.text
    assert ответ.json()["code"] == "неделя_распределена"


# --- поправка от импорта ----------------------------------------------------


def test_импорт_разносит_поправку_в_закрытую_неделю(стенд: Стенд) -> None:
    """Второй из двух путей, которым §15.10 поручает разнос поправок.

    Выписка приносит траты недели, итог которой owner уже распределил.
    Переигрывать её нельзя - разница уходит вперёд, и без этого разноса
    она пропала бы совсем.
    """
    стенд.сейчас = В_СЛЕДУЮЩИЙ
    августовская = dt.date(2026, 8, 17)
    стенд.сессия.add(
        FinWeekBudget(
            week_start=августовская,
            amount=Decimal("10000.00"),
            to_next=Decimal("10000.00"),
            to_savings=Decimal("0.00"),
            settled_spend=Decimal("0.00"),
            settled_at=dt.datetime(2026, 8, 24, 6, 0, tzinfo=dt.UTC),
        )
    )
    стенд.сессия.flush()

    тело = стенд.клиент.post(
        "/api/finance/import",
        params={"apply": True},
        files=выписка(),
    ).json()

    assert тело["applied"] is True
    assert тело["corrected_weeks"] == [августовская.isoformat()]
    стенд.сессия.expire_all()
    строка = стенд.сессия.scalar(
        select(FinWeekBudget).where(FinWeekBudget.week_start == августовская)
    )
    # Снимок сдвинут на новый факт - иначе та же поправка применилась бы снова.
    assert строка is not None
    assert строка.settled_spend is not None and строка.settled_spend > Decimal("0.00")
    # Поправка отрицательна: неделя оказалась дороже, чем считалось при решении.
    assert Decimal(неделю(стенд, week=СЛЕДУЮЩИЙ.isoformat())["week"]["carry"]) < Decimal("0.00")


def test_импорт_без_apply_поправок_не_разносит(стенд: Стенд) -> None:
    тело = стенд.клиент.post(
        "/api/finance/import",
        files=выписка(),
    ).json()

    assert тело["applied"] is False
    assert тело["corrected_weeks"] == []

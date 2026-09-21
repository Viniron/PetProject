"""Эндпоинты книжки (Ф6): контракт ответов, отказы и импорт через HTTP.

Арифметика сальдо, окна гашения и плана импорта проверена прямыми вызовами
(`test_finance_balance.py`, `test_finance_offsets.py`, `test_finance_import.py`,
`test_finance_edit.py`, `test_finance_feed.py`). Здесь - то, что видно только
через HTTP: форма ответа, коды отказов, разбор параметров и две фазы импорта,
где `apply=false` обязан не записать ни строки.

Сеть не трогается ни разу: выписки берутся из фикстур, моделей книжка
не зовёт вовсе.
"""

import datetime as dt
import inspect
from decimal import Decimal
from typing import Any

import pytest
from conftest import Стенд, байты_фикстуры
from sqlalchemy import func, select

from jarvis_api.api.routes_finance import импорт_выписок, обзор_месяца
from jarvis_api.db.models import FinAccount, FinCategory, FinCategoryRule, FinTransaction

БАНК = "tbank"
КАРТА = "Black"
КОПИЛКА = "Накопительный счёт"
АВГУСТ = dt.date(2026, 8, 1)
СЕНТЯБРЬ = dt.date(2026, 9, 1)

В_АВГУСТЕ = dt.datetime(2026, 8, 23, 9, 0, tzinfo=dt.UTC)
ПОЗЖЕ_В_АВГУСТЕ = dt.datetime(2026, 8, 24, 9, 0, tzinfo=dt.UTC)
В_ИЮЛЕ = dt.datetime(2026, 7, 20, 9, 0, tzinfo=dt.UTC)

# «Сейчас» всех проверок - середина сентября: окно пересчёта это август
# и сентябрь, июль за краем. Дата фиксированная: тест, зависящий
# от системных часов, однажды позеленел бы по календарю.
СЕЙЧАС = dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)


def операция(стенд: Стенд, момент: dt.datetime, сумма: str, **поля: Any) -> FinTransaction:
    значение = Decimal(сумма)
    все: dict[str, Any] = {
        "bank": БАНК,
        "account": КАРТА,
        "occurred_at": момент,
        "amount": значение,
        "amount_rub": значение,
        "currency": "RUB",
        "kind": "expense" if значение < 0 else "income",
        "merchant": "Евгений В.",
        "fingerprint": f"{момент.isoformat()}{сумма}{поля.get('merchant', '')}".ljust(64, "0")[:64],
    }
    все.update(поля)
    строка = FinTransaction(**все)
    стенд.сессия.add(строка)
    стенд.сессия.flush()
    return строка


@pytest.fixture(autouse=True)
def сентябрь(стенд: Стенд) -> None:
    """Все проверки идут из одного «сейчас»: окно пересчёта обязано быть тем же."""
    стенд.сейчас = СЕЙЧАС


@pytest.fixture
def книжка(стенд: Стенд) -> Стенд:
    """Счёт с ролью и набор категорий августа."""
    стенд.сессия.add(FinAccount(bank=БАНК, name=КАРТА, role="checking"))
    стенд.сессия.add_all(
        [
            FinCategory(key="food", period_month=АВГУСТ, level=1, title="Еда", origin="owner"),
            FinCategory(key="food", period_month=СЕНТЯБРЬ, level=1, title="Еда", origin="owner"),
        ]
    )
    стенд.сессия.flush()
    return стенд


def категория(стенд: Стенд, месяц: dt.date) -> FinCategory:
    return стенд.сессия.query(FinCategory).filter_by(key="food", period_month=месяц).one()


# --- обзор месяца -----------------------------------------------------------


def test_пустой_месяц_даёт_нули_а_не_404(книжка: Стенд) -> None:
    """Книжка без этого месяца отвечает честным нулём (§15.5)."""
    ответ = книжка.клиент.get("/api/finance/overview", params={"month": "2026-08"})

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["data"]["month"] == "2026-08-01"
    assert тело["data"]["income"] == "0.00"
    assert тело["data"]["expense"] == "0.00"
    assert тело["data"]["transactions"] == 0
    assert тело["timezone"] == "Europe/Moscow"


def test_обзор_сводит_приход_расход_и_сальдо(книжка: Стенд) -> None:
    операция(книжка, В_АВГУСТЕ, "11080.02")
    операция(книжка, В_АВГУСТЕ, "-9919.81")

    тело = книжка.клиент.get("/api/finance/overview", params={"month": "2026-08"}).json()

    assert тело["data"]["income"] == "11080.02"
    assert тело["data"]["expense"] == "9919.81"
    assert тело["data"]["balance"] == "1160.21"


def test_неразмеченные_счета_молчат_словами_а_не_нулём(стенд: Стенд) -> None:
    """Ноль здесь неотличим от «ничего не откладывал» - а это разные ответы."""
    стенд.сессия.add(FinAccount(bank=БАНК, name=КАРТА, role="unknown"))
    стенд.сессия.flush()
    операция(стенд, В_АВГУСТЕ, "-1200.00")

    тело = стенд.клиент.get("/api/finance/overview", params={"month": "2026-08"}).json()

    assert тело["data"]["saved"] is None
    assert тело["accounts_marked"] is False


def test_июль_вне_окна_пересчёта(книжка: Стенд) -> None:
    """Пересчитаться могут два месяца - текущий и предыдущий (§15.5)."""
    август = книжка.клиент.get("/api/finance/overview", params={"month": "2026-08"}).json()
    июль = книжка.клиент.get("/api/finance/overview", params={"month": "2026-07"}).json()

    assert август["in_recalc_window"] is True
    assert июль["in_recalc_window"] is False


def test_месяц_по_умолчанию_текущий(книжка: Стенд) -> None:
    тело = книжка.клиент.get("/api/finance/overview").json()

    assert тело["data"]["month"] == "2026-09-01"


def test_кривой_месяц_отклонён(книжка: Стенд) -> None:
    ответ = книжка.клиент.get("/api/finance/overview", params={"month": "август"})

    assert ответ.status_code == 422
    assert ответ.json()["code"] == "validation_error"


# --- лента и карточка -------------------------------------------------------


def test_лента_отдаёт_месяц_с_эффективной_суммой(книжка: Стенд) -> None:
    расход = операция(книжка, В_АВГУСТЕ, "-5750.00")
    операция(
        книжка,
        ПОЗЖЕ_В_АВГУСТЕ,
        "2300.00",
        kind="refund",
        kind_source="manual",
        offsets_transaction_id=расход.id,
    )

    тело = книжка.клиент.get("/api/finance/transactions", params={"month": "2026-08"}).json()

    строки = {с["id"]: с for с in тело["transactions"]}
    assert тело["month"] == "2026-08-01"
    assert строки[расход.id]["effective"] == "3450.00"
    assert строки[расход.id]["counts"] is True


def test_очередь_разбора_собирается_по_всей_книжке(книжка: Стенд) -> None:
    операция(книжка, В_ИЮЛЕ, "2300.00", needs_review=True)
    операция(книжка, В_АВГУСТЕ, "-100.00")

    тело = книжка.клиент.get(
        "/api/finance/transactions", params={"all_months": True, "needs_review": True}
    ).json()

    assert тело["month"] is None
    assert len(тело["transactions"]) == 1
    assert тело["transactions"][0]["needs_review"] is True


def test_карточка_несёт_разбивку(книжка: Стенд) -> None:
    расход = операция(книжка, В_АВГУСТЕ, "-5750.00")
    гашение = операция(
        книжка,
        ПОЗЖЕ_В_АВГУСТЕ,
        "2300.00",
        kind="refund",
        kind_source="manual",
        offsets_transaction_id=расход.id,
    )

    тело = книжка.клиент.get(f"/api/finance/transactions/{расход.id}").json()

    assert тело["breakdown"]["effective"] == "3450.00"
    assert [г["id"] for г in тело["offsets"]] == [гашение.id]


def test_карточки_несуществующей_операции_нет(книжка: Стенд) -> None:
    ответ = книжка.клиент.get("/api/finance/transactions/10000")

    assert ответ.status_code == 404
    assert ответ.json()["retryable"] is False


# --- правка -----------------------------------------------------------------


def test_правка_вида_возвращает_карточку(книжка: Стенд) -> None:
    строка = операция(книжка, В_АВГУСТЕ, "2300.00")

    ответ = книжка.клиент.patch(f"/api/finance/transactions/{строка.id}", json={"kind": "income"})

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["changed"] is True
    assert тело["fields_changed"] == ["kind"]
    assert тело["transaction"]["transaction"]["kind_source"] == "manual"


def test_правка_с_запоминанием_заводит_правило(книжка: Стенд) -> None:
    строка = операция(книжка, В_АВГУСТЕ, "2300.00")

    тело = книжка.клиент.patch(
        f"/api/finance/transactions/{строка.id}",
        json={"kind": "income", "remember": True},
    ).json()

    правило = книжка.сессия.query(FinCategoryRule).one()
    assert тело["rules"] == [правило.id]
    assert правило.rule_type == "sender"


def test_категория_другого_месяца_это_конфликт(книжка: Стенд) -> None:
    строка = операция(книжка, В_АВГУСТЕ, "-1200.00")
    чужая = категория(книжка, СЕНТЯБРЬ)

    ответ = книжка.клиент.patch(
        f"/api/finance/transactions/{строка.id}", json={"category_id": чужая.id}
    )

    assert ответ.status_code == 409
    assert ответ.json()["code"] == "категория_другого_месяца"


def test_неизвестный_вид_отклонён_разбором_тела(книжка: Стенд) -> None:
    строка = операция(книжка, В_АВГУСТЕ, "-1200.00")

    ответ = книжка.клиент.patch(f"/api/finance/transactions/{строка.id}", json={"kind": "подарок"})

    assert ответ.status_code == 422
    assert ответ.json()["details"]


def test_правка_несуществующей_операции_это_404(книжка: Стенд) -> None:
    ответ = книжка.клиент.patch("/api/finance/transactions/10000", json={"excluded": True})

    assert ответ.status_code == 404


# --- гашение ----------------------------------------------------------------


def test_привязка_уменьшает_расход(книжка: Стенд) -> None:
    расход = операция(книжка, В_АВГУСТЕ, "-5750.00")
    гашение = операция(книжка, ПОЗЖЕ_В_АВГУСТЕ, "2300.00")

    ответ = книжка.клиент.post(
        f"/api/finance/transactions/{расход.id}/offsets", json={"income_id": гашение.id}
    )

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["changed"] is True
    assert тело["breakdown"]["effective"] == "3450.00"


def test_привязка_вне_окна_отклонена(книжка: Стенд) -> None:
    """Июль объявлен окончательным, и привязка его не переигрывает (§15.5)."""
    расход = операция(книжка, В_ИЮЛЕ, "-5750.00")
    гашение = операция(книжка, В_АВГУСТЕ, "2300.00")

    ответ = книжка.клиент.post(
        f"/api/finance/transactions/{расход.id}/offsets", json={"income_id": гашение.id}
    )

    assert ответ.status_code == 409
    assert ответ.json()["code"] == "вне_окна"


def test_расход_не_гасит_расход(книжка: Стенд) -> None:
    первый = операция(книжка, В_АВГУСТЕ, "-5750.00")
    второй = операция(книжка, ПОЗЖЕ_В_АВГУСТЕ, "-100.00")

    ответ = книжка.клиент.post(
        f"/api/finance/transactions/{первый.id}/offsets", json={"income_id": второй.id}
    )

    assert ответ.status_code == 409
    assert ответ.json()["code"] == "не_приход"


def test_снятие_возвращает_расходу_его_сумму(книжка: Стенд) -> None:
    расход = операция(книжка, В_АВГУСТЕ, "-5750.00")
    гашение = операция(книжка, ПОЗЖЕ_В_АВГУСТЕ, "2300.00")
    книжка.клиент.post(
        f"/api/finance/transactions/{расход.id}/offsets", json={"income_id": гашение.id}
    )

    ответ = книжка.клиент.delete(f"/api/finance/transactions/{расход.id}/offsets/{гашение.id}")

    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["breakdown"]["effective"] == "5750.00"


def test_снятие_чужой_привязки_отклонено(книжка: Стенд) -> None:
    """Адрес называет оба конца, и несовпадение - это конфликт, а не успех."""
    расход = операция(книжка, В_АВГУСТЕ, "-5750.00")
    другой = операция(книжка, В_АВГУСТЕ, "-100.00")
    гашение = операция(книжка, ПОЗЖЕ_В_АВГУСТЕ, "2300.00")
    книжка.клиент.post(
        f"/api/finance/transactions/{расход.id}/offsets", json={"income_id": гашение.id}
    )

    ответ = книжка.клиент.delete(f"/api/finance/transactions/{другой.id}/offsets/{гашение.id}")

    assert ответ.status_code == 409
    assert ответ.json()["code"] == "не_та_привязка"


# --- счета ------------------------------------------------------------------


def test_разметка_счёта_включает_отложено(стенд: Стенд) -> None:
    стенд.сессия.add_all(
        [
            FinAccount(bank=БАНК, name=КАРТА, role="checking"),
            FinAccount(bank=БАНК, name=КОПИЛКА, role="unknown"),
        ]
    )
    стенд.сессия.flush()
    операция(стенд, В_АВГУСТЕ, "3000.00", account=КОПИЛКА, kind="transfer", kind_source="manual")
    счёт = стенд.сессия.query(FinAccount).filter_by(name=КОПИЛКА).one()

    ответ = стенд.клиент.put(f"/api/finance/accounts/{счёт.id}/role", json={"role": "savings"})

    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["role"] == "savings"
    обзор = стенд.клиент.get("/api/finance/overview", params={"month": "2026-08"}).json()
    assert обзор["accounts_marked"] is True
    assert обзор["data"]["saved"] == "3000.00"


def test_роль_несуществующего_счёта_это_404(книжка: Стенд) -> None:
    ответ = книжка.клиент.put("/api/finance/accounts/10000/role", json={"role": "savings"})

    assert ответ.status_code == 404


def test_неизвестная_роль_отклонена(книжка: Стенд) -> None:
    счёт = книжка.сессия.query(FinAccount).one()

    ответ = книжка.клиент.put(f"/api/finance/accounts/{счёт.id}/role", json={"role": "депозит"})

    assert ответ.status_code == 422


# --- правила ----------------------------------------------------------------


def test_правило_заводится_и_удаляется(книжка: Стенд) -> None:
    создано = книжка.клиент.post(
        "/api/finance/rules",
        json={"rule_type": "sender", "pattern": "Евгений В.", "kind": "income"},
    )

    assert создано.status_code == 201, создано.text
    номер = создано.json()["id"]
    assert книжка.клиент.get("/api/finance/rules").json()["rules"][0]["pattern"] == "Евгений В."

    assert книжка.клиент.delete(f"/api/finance/rules/{номер}").status_code == 204
    assert книжка.клиент.get("/api/finance/rules").json()["rules"] == []


def test_дубль_правила_это_конфликт(книжка: Стенд) -> None:
    тело = {"rule_type": "sender", "pattern": "Евгений В.", "kind": "income"}
    книжка.клиент.post("/api/finance/rules", json=тело)

    ответ = книжка.клиент.post("/api/finance/rules", json={**тело, "kind": "refund"})

    assert ответ.status_code == 409
    assert ответ.json()["code"] == "правило_есть"


def test_правило_на_несуществующую_категорию_отклонено(книжка: Стенд) -> None:
    ответ = книжка.клиент.post(
        "/api/finance/rules",
        json={"rule_type": "merchant", "pattern": "Пятёрочка", "category_key": "fod"},
    )

    assert ответ.status_code == 409
    assert ответ.json()["code"] == "нет_такой_категории"


def test_категории_отдаются_набором_месяца(книжка: Стенд) -> None:
    тело = книжка.клиент.get("/api/finance/categories", params={"month": "2026-08"}).json()

    assert тело["month"] == "2026-08-01"
    assert [к["key"] for к in тело["categories"]] == ["food"]
    assert тело["categories"][0]["period_month"] == "2026-08-01"


# --- разбор -----------------------------------------------------------------


def test_разбор_без_apply_не_пишет_ни_строки(книжка: Стенд) -> None:
    """Разбор меняет вид операций, то есть сальдо: молча этого не делают."""
    строка = операция(книжка, В_АВГУСТЕ, "2300.00", kind="expense", kind_source="sign")

    ответ = книжка.клиент.post("/api/finance/recategorize")

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["applied"] is False
    assert тело["lines"]
    книжка.сессия.expire_all()
    assert строка.kind == "expense"


def test_разбор_с_apply_меняет_книжку(книжка: Стенд) -> None:
    """Входящий перевод без правила отправителя уходит в разбор (§15.5)."""
    строка = операция(книжка, В_АВГУСТЕ, "2300.00", kind="expense", kind_source="sign")

    тело = книжка.клиент.post("/api/finance/recategorize", params={"apply": True}).json()

    assert тело["applied"] is True
    книжка.сессия.expire_all()
    assert строка.kind == "refund"
    assert строка.kind_source == "default"
    assert строка.needs_review is True


# --- импорт -----------------------------------------------------------------


def выписка(имя: str = "tbank_august.csv") -> dict[str, Any]:
    """Файл формой, как его пришлёт вкладка `Finance`."""
    return {"files": (имя, байты_фикстуры("statements", имя), "text/csv")}


def строк_в_книжке(стенд: Стенд) -> int:
    return стенд.сессия.scalar(select(func.count()).select_from(FinTransaction)) or 0


def test_импорт_без_apply_показывает_дифф_и_не_пишет(стенд: Стенд) -> None:
    ответ = стенд.клиент.post("/api/finance/import", files=выписка())

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["applied"] is False
    файл = тело["files"][0]
    assert файл["bank"] == БАНК
    assert файл["added"] > 0
    assert len(файл["rows_added"]) == файл["added"]
    assert файл["new_accounts"]
    assert тело["recategorized"] is None
    стенд.сессия.expire_all()
    assert строк_в_книжке(стенд) == 0


def test_импорт_с_apply_пишет_и_разбирает(стенд: Стенд) -> None:
    тело = стенд.клиент.post("/api/finance/import", params={"apply": True}, files=выписка()).json()

    assert тело["applied"] is True
    assert тело["recategorized"]["applied"] is True
    стенд.сессия.expire_all()
    assert строк_в_книжке(стенд) == тело["files"][0]["added"]


def test_повторная_загрузка_того_же_файла_ничего_не_добавляет(стенд: Стенд) -> None:
    """Идемпотентность держит `fin_imports.sha256`, а не память сервера."""
    первый = стенд.клиент.post(
        "/api/finance/import", params={"apply": True}, files=выписка()
    ).json()
    было = строк_в_книжке(стенд)

    второй = стенд.клиент.post(
        "/api/finance/import", params={"apply": True}, files=выписка()
    ).json()

    assert первый["files"][0]["added"] > 0
    assert второй["files"][0]["already_imported"] is True
    assert второй["files"][0]["added"] == 0
    стенд.сессия.expire_all()
    assert строк_в_книжке(стенд) == было


def test_чужой_файл_отклонён_целиком(стенд: Стенд) -> None:
    """Банк не угадывается по имени файла: отказ называет пришедшие колонки."""
    ответ = стенд.клиент.post("/api/finance/import", files=выписка("unknown_bank.csv"))

    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["failed"] == 1
    assert тело["files"][0]["error"]
    стенд.сессия.expire_all()
    assert строк_в_книжке(стенд) == 0


def test_отказ_одного_файла_не_отменяет_остальные(стенд: Стенд) -> None:
    """Банк, сменивший формат, не блокирует учёт по другим (§15.3, ADR-030)."""
    файлы = [
        ("files", (имя, байты_фикстуры("statements", имя), "text/csv"))
        for имя in ("unknown_bank.csv", "tbank_august.csv")
    ]

    тело = стенд.клиент.post("/api/finance/import", params={"apply": True}, files=файлы).json()

    assert тело["failed"] == 1
    прошедший = [ф for ф in тело["files"] if ф["error"] is None]
    assert len(прошедший) == 1 and прошедший[0]["added"] > 0
    стенд.сессия.expire_all()
    assert строк_в_книжке(стенд) > 0


# --- общее ------------------------------------------------------------------


def test_эндпоинты_книжки_синхронные() -> None:
    """`async def` с запросом в базу заблокировал бы цикл вместе с `/health`.

    Правило ruff `ASYNC` этого не ловит, поэтому оно закреплено здесь -
    тем же тестом, что и у календаря.
    """
    assert inspect.iscoroutinefunction(обзор_месяца) is False
    assert inspect.iscoroutinefunction(импорт_выписок) is False

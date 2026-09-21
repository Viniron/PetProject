"""Ручная правка операции и правила разбора (Ф6, §15.4, §15.5).

Проверяется прямым вызовом, а не через HTTP: правка ставит `manual`
источником решения, и от этого зависит, переживёт ли она следующий разбор.
Ошибка здесь тихая - книжка выглядит разобранной, а `finance-categorize`
возвращает всё назад на первом же прогоне.

Отдельная забота - флаг «запомнить»: по умолчанию выключен, потому что
разовое исключение не должно расползаться на весь поток (решение owner
на Ф4а).
"""

import datetime as dt
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from jarvis_api.db.models import FinAccount, FinCategory, FinCategoryRule, FinTransaction
from jarvis_api.domain import finance_rules
from jarvis_api.domain.finance_edit import ОшибкаПравки, править
from jarvis_api.domain.finance_rules import ОшибкаПравила

МОСКВА = ZoneInfo("Europe/Moscow")
БАНК = "tbank"
КАРТА = "Black"
АВГУСТ = dt.date(2026, 8, 1)
СЕНТЯБРЬ = dt.date(2026, 9, 1)
В_АВГУСТЕ = dt.datetime(2026, 8, 23, 12, 0, tzinfo=МОСКВА)


def операция_в_базе(сессия: Session, сумма: str, **переопределения: Any) -> FinTransaction:
    значение = Decimal(сумма)
    поля: dict[str, Any] = {
        "bank": БАНК,
        "account": КАРТА,
        "occurred_at": В_АВГУСТЕ,
        "amount": значение,
        "amount_rub": значение,
        "currency": "RUB",
        "kind": "expense" if значение < 0 else "income",
        "merchant": "Евгений В.",
        "fingerprint": f"{сумма}{переопределения.get('merchant', '')}".ljust(64, "0")[:64],
    }
    поля.update(переопределения)
    строка = FinTransaction(**поля)
    сессия.add(строка)
    сессия.flush()
    return строка


@pytest.fixture
def книжка(сессия: Session) -> Session:
    """Счёт, две категории августа и одна сентября - для проверки месяца."""
    сессия.add(FinAccount(bank=БАНК, name=КАРТА, role="checking"))
    сессия.add_all(
        [
            FinCategory(key="food", period_month=АВГУСТ, level=1, title="Еда", origin="owner"),
            FinCategory(key="fun", period_month=АВГУСТ, level=1, title="Досуг", origin="owner"),
            FinCategory(key="food", period_month=СЕНТЯБРЬ, level=1, title="Еда", origin="owner"),
        ]
    )
    сессия.flush()
    return сессия


def категория(сессия: Session, ключ: str, месяц: dt.date) -> FinCategory:
    строка = сессия.query(FinCategory).filter_by(key=ключ, period_month=месяц).one()
    return строка


# --- вид и источник решения -------------------------------------------------


def test_правка_вида_ставит_manual(книжка: Session) -> None:
    """Разбор ручное решение не пересматривает - это и есть `manual` (§15.4)."""
    строка = операция_в_базе(книжка, "2300.00")

    правка = править(книжка, tx_id=строка.id, зона=МОСКВА, kind="income")

    assert правка.изменилось
    assert правка.поля == ["kind"]
    assert строка.kind == "income"
    assert строка.kind_source == "manual"


def test_подтверждение_того_же_вида_тоже_ставит_manual(книжка: Session) -> None:
    """Owner согласился с умолчанием - это решение, а не отсутствие правки.

    Без записи источника следующий разбор снова увёл бы строку в очередь
    по правилу умолчания, и owner отвечал бы на один вопрос каждую неделю.
    """
    строка = операция_в_базе(книжка, "2300.00", kind="refund", kind_source="default")

    правка = править(книжка, tx_id=строка.id, зона=МОСКВА, kind="refund")

    assert строка.kind_source == "manual"
    assert правка.поля == ["kind"]


def test_вид_не_снимается(книжка: Session) -> None:
    строка = операция_в_базе(книжка, "2300.00")

    with pytest.raises(ОшибкаПравки) as сбой:
        править(книжка, tx_id=строка.id, зона=МОСКВА, kind=None)

    assert сбой.value.код == "вид_не_снимается"


def test_перевод_без_пары_предупреждает(книжка: Session) -> None:
    """Пометка `transfer` руками законна, но второй конец не появляется."""
    строка = операция_в_базе(книжка, "-5000.00")

    правка = править(книжка, tx_id=строка.id, зона=МОСКВА, kind="transfer")

    assert строка.kind == "transfer"
    assert any("без пары" in предупреждение for предупреждение in правка.предупреждения)


# --- категория --------------------------------------------------------------


def test_категория_другого_месяца_отклонена(книжка: Session) -> None:
    """Набор категорий - версия месяца (§15.4), и чужая ломает сравнение."""
    строка = операция_в_базе(книжка, "-1200.00")
    чужая = категория(книжка, "food", СЕНТЯБРЬ)

    with pytest.raises(ОшибкаПравки) as сбой:
        править(книжка, tx_id=строка.id, зона=МОСКВА, category_id=чужая.id)

    assert сбой.value.код == "категория_другого_месяца"


def test_снятая_категория_не_возвращается_разбором(книжка: Session) -> None:
    """`manual` без категории - это «owner снял её руками» (CHECK допускает)."""
    своя = категория(книжка, "food", АВГУСТ)
    строка = операция_в_базе(книжка, "-1200.00", category_id=своя.id, category_source="mcc")

    правка = править(книжка, tx_id=строка.id, зона=МОСКВА, category_id=None)

    assert правка.поля == ["category_id"]
    assert строка.category_id is None
    assert строка.category_source == "manual"


def test_правка_одного_поля_не_трогает_остальные(книжка: Session) -> None:
    """PATCH правит присланное. Затёртая заметка - потеря текста owner."""
    своя = категория(книжка, "food", АВГУСТ)
    строка = операция_в_базе(книжка, "-1200.00", note="стол на всех", excluded=True)

    править(книжка, tx_id=строка.id, зона=МОСКВА, category_id=своя.id)

    assert строка.note == "стол на всех"
    assert строка.excluded is True


# --- запоминание правилом ---------------------------------------------------


def test_по_умолчанию_правка_разовая(книжка: Session) -> None:
    строка = операция_в_базе(книжка, "2300.00")

    правка = править(книжка, tx_id=строка.id, зона=МОСКВА, kind="income")

    assert правка.правила == []
    assert книжка.query(FinCategoryRule).count() == 0


def test_запомнить_вид_заводит_правило_отправителя(книжка: Session) -> None:
    """Образец - написание банка («Евгений В.»), с ним и сравнивает разбор."""
    строка = операция_в_базе(книжка, "2300.00")

    правка = править(книжка, tx_id=строка.id, зона=МОСКВА, kind="income", запомнить=True)

    правило = книжка.query(FinCategoryRule).one()
    assert правка.правила == [правило.id]
    assert (правило.rule_type, правило.pattern, правило.kind) == ("sender", "Евгений В.", "income")
    assert any("не пересчитана" in предупреждение for предупреждение in правка.предупреждения)


def test_запомнить_категорию_заводит_правило_мерчанта(книжка: Session) -> None:
    своя = категория(книжка, "food", АВГУСТ)
    строка = операция_в_базе(книжка, "-1200.00", merchant="Пятёрочка")

    править(книжка, tx_id=строка.id, зона=МОСКВА, category_id=своя.id, запомнить=True)

    правило = книжка.query(FinCategoryRule).one()
    assert (правило.rule_type, правило.pattern, правило.category_key) == (
        "merchant",
        "Пятёрочка",
        "food",
    )


def test_запомнить_расход_правилом_нельзя(книжка: Session) -> None:
    """Правилом отправителя запоминаются только `income` и `refund` (§15.5)."""
    строка = операция_в_базе(книжка, "2300.00")

    with pytest.raises(ОшибкаПравки) as сбой:
        править(книжка, tx_id=строка.id, зона=МОСКВА, kind="expense", запомнить=True)

    assert сбой.value.код == "вид_не_запоминается"


def test_запомнить_без_описания_отклонено(книжка: Session) -> None:
    """Правило сопоставляется с «Описанием»: пустое - сопоставлять нечем."""
    строка = операция_в_базе(книжка, "2300.00", merchant=None)

    with pytest.raises(ОшибкаПравки) as сбой:
        править(книжка, tx_id=строка.id, зона=МОСКВА, kind="income", запомнить=True)

    assert сбой.value.код == "нет_образца"


def test_повторная_правка_переписывает_своё_правило(книжка: Session) -> None:
    """Иначе вторая правка молча не запомнилась бы, а owner думал бы иначе."""
    строка = операция_в_базе(книжка, "2300.00")
    править(книжка, tx_id=строка.id, зона=МОСКВА, kind="income", запомнить=True)

    править(книжка, tx_id=строка.id, зона=МОСКВА, kind="refund", запомнить=True)

    правило = книжка.query(FinCategoryRule).one()
    assert правило.kind == "refund"


def test_запоминать_нечего_если_менялась_заметка(книжка: Session) -> None:
    строка = операция_в_базе(книжка, "2300.00")

    with pytest.raises(ОшибкаПравки) as сбой:
        править(книжка, tx_id=строка.id, зона=МОСКВА, note="от брата", запомнить=True)

    assert сбой.value.код == "нечего_запоминать"


# --- правила отдельной дверью -----------------------------------------------


def test_правило_с_опечаткой_в_ключе_отклонено(книжка: Session) -> None:
    """Опечатка не ломает ничего заметного - и потому ловится на вводе."""
    with pytest.raises(ОшибкаПравила) as сбой:
        finance_rules.создать(книжка, rule_type="merchant", pattern="Пятёрочка", category_key="fod")

    assert сбой.value.код == "нет_такой_категории"


def test_правило_обязано_что_то_решать(книжка: Session) -> None:
    with pytest.raises(ОшибкаПравила) as сбой:
        finance_rules.создать(книжка, rule_type="merchant", pattern="Пятёрочка")

    assert сбой.value.код == "нет_категории"


def test_правило_отправителя_решает_вид_а_не_категорию(книжка: Session) -> None:
    with pytest.raises(ОшибкаПравила) as сбой:
        finance_rules.создать(
            книжка, rule_type="sender", pattern="Евгений В.", category_key="food", kind="income"
        )

    assert сбой.value.код == "лишняя_категория"


def test_правило_self_ничего_не_решает(книжка: Session) -> None:
    """`self` называет написание owner, а вид решает пара концов (ADR-041)."""
    правило, изменилось = finance_rules.создать(книжка, rule_type="self", pattern="Роман В.")

    assert изменилось
    assert (правило.category_key, правило.kind) == (None, None)


def test_дубль_правила_отклонён_а_не_переписан(книжка: Session) -> None:
    """Молча переписанное правило меняет разбор всей книжки (§15.4)."""
    finance_rules.создать(книжка, rule_type="sender", pattern="Евгений В.", kind="income")

    with pytest.raises(ОшибкаПравила) as сбой:
        finance_rules.создать(книжка, rule_type="sender", pattern="Евгений В.", kind="refund")

    assert сбой.value.код == "правило_есть"


def test_то_же_правило_второй_раз_не_меняет_ничего(книжка: Session) -> None:
    правило, _ = finance_rules.создать(
        книжка, rule_type="sender", pattern="Евгений В.", kind="income"
    )

    тот_же, изменилось = finance_rules.создать(
        книжка, rule_type="sender", pattern="Евгений В.", kind="income"
    )

    assert тот_же.id == правило.id
    assert not изменилось


def test_удаление_правила_не_трогает_операции(книжка: Session) -> None:
    """Пересчёт - отдельное действие: разбор меняет сальдо двух месяцев."""
    правило, _ = finance_rules.создать(
        книжка, rule_type="sender", pattern="Евгений В.", kind="income"
    )
    строка = операция_в_базе(книжка, "2300.00", kind="income", kind_source="sender_rule")

    assert finance_rules.удалить(книжка, правило.id)
    книжка.flush()

    assert строка.kind == "income"
    assert not finance_rules.удалить(книжка, правило.id)

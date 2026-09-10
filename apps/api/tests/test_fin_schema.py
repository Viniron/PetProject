"""Тесты схемы финансовой книжки (Ф1, §15.2).

Проверяется не то, что код делает правильно, а то, что база не даст сделать
неправильно. Для книжки это важнее, чем для календаря: ошибка здесь не падает,
а тихо искажает суммы, то есть обнаруживается через месяцы и не лечится
переимпортом.

Чего здесь нет намеренно: окна привязки гашений («текущий месяц и предыдущий»,
§15.5). Оно зависит от «сейчас», CHECK такого не умеет, и живёт оно в сервисе -
поэтому его тесты приедут вместе с ним на Ф4.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jarvis_api.db.models import (
    FinAccount,
    FinCategory,
    FinCategoryRule,
    FinImport,
    FinSummary,
    FinTransaction,
)

БАНК = "tbank"
СЧЁТ = "Black"
МЕСЯЦ = date(2026, 9, 1)


def счёт(**переопределения: Any) -> FinAccount:
    """Свой счёт с заполненным минимумом."""
    поля: dict[str, Any] = {"bank": БАНК, "name": СЧЁТ, "role": "checking"}
    поля.update(переопределения)
    return FinAccount(**поля)


def операция(**переопределения: Any) -> FinTransaction:
    """Расход с заполненным минимумом. Требует счёта в `fin_accounts`."""
    поля: dict[str, Any] = {
        "bank": БАНК,
        "account": СЧЁТ,
        "occurred_at": datetime(2026, 9, 3, 13, 36, 2, tzinfo=UTC),
        "amount": Decimal("-330.00"),
        "amount_rub": Decimal("-330.00"),
        "currency": "RUB",
        "kind": "expense",
        "fingerprint": "a" * 64,
    }
    поля.update(переопределения)
    return FinTransaction(**поля)


def категория(**переопределения: Any) -> FinCategory:
    """Основная категория месяца."""
    поля: dict[str, Any] = {
        "key": "fastfood",
        "title": "Фастфуд",
        "period_month": МЕСЯЦ,
        "level": 1,
        "origin": "owner",
    }
    поля.update(переопределения)
    return FinCategory(**поля)


def выписка(**переопределения: Any) -> FinImport:
    """Факт загрузки файла."""
    поля: dict[str, Any] = {
        "bank": БАНК,
        "filename": "operations.csv",
        "sha256": "0" * 64,
    }
    поля.update(переопределения)
    return FinImport(**поля)


def _со_счётом(сессия: Session) -> None:
    """Счёт, без которого операцию некуда записать."""
    сессия.add(счёт())
    сессия.flush()


# --- дедупликация -------------------------------------------------------


def test_две_одинаковые_покупки_в_один_день_остаются_двумя(сессия: Session) -> None:
    """§15.3: два кофе по 200 ₽ в одном месте дают один ключ и две операции.

    Ловушка, ради которой правило кратности и записано: дедупликация
    «есть такая - пропустить» здесь теряет деньги owner.
    """
    _со_счётом(сессия)
    сессия.add(операция(occurrence_no=1))
    сессия.add(операция(occurrence_no=2))
    сессия.flush()

    сколько = сессия.execute(text("select count(*) from fin_transactions")).scalar_one()
    assert сколько == 2


def test_третья_копия_с_тем_же_номером_невозможна(сессия: Session) -> None:
    """Обратная сторона: кратность считается, а не игнорируется."""
    _со_счётом(сессия)
    сессия.add(операция(occurrence_no=1))
    сессия.flush()

    сессия.add(операция(occurrence_no=1))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_одинаковая_операция_из_другого_банка_не_схлопывается(сессия: Session) -> None:
    """ADR-030: без банка в ключе импорт добавил бы ноль строк вместо одной."""
    _со_счётом(сессия)
    сессия.add(счёт(bank="sber", name="Дебетовая"))
    сессия.flush()
    сессия.add(операция())
    сессия.add(операция(bank="sber", account="Дебетовая"))
    сессия.flush()

    сколько = сессия.execute(text("select count(*) from fin_transactions")).scalar_one()
    assert сколько == 2


def test_тот_же_файл_второй_раз_не_импортируется(сессия: Session) -> None:
    """§15.3: идемпотентность держит база, а не проверка в коде.

    Проверка «а не грузили ли мы уже это» слабее ограничения: два прогона
    прошли бы её оба и записали операции дважды.
    """
    сессия.add(выписка())
    сессия.flush()

    сессия.add(выписка(filename="operations (1).csv"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


# --- деньги -------------------------------------------------------------


def test_копейки_не_расходятся_на_дробях(сессия: Session) -> None:
    """`numeric`, а не `float`: три раза по 0.10 дают ровно 0.30.

    На двоичной дроби эта сумма даёт 0.30000000000000004, и за год такие
    хвосты расходятся в рубли.
    """
    _со_счётом(сессия)
    for номер in (1, 2, 3):
        сессия.add(
            операция(
                occurrence_no=номер,
                amount=Decimal("-0.10"),
                amount_rub=Decimal("-0.10"),
            )
        )
    сессия.flush()

    сумма = сессия.execute(text("select sum(amount) from fin_transactions")).scalar_one()
    assert сумма == Decimal("-0.30")


def test_операция_не_может_ссылаться_на_неизвестный_счёт(сессия: Session) -> None:
    """§15.5: «Отложено» считается по размеченным счетам.

    Операция со счётом, которого нет в разметке, сделала бы часть суммы
    невидимой для статьи накопления - и расхождение было бы тихим.
    """
    сессия.add(операция())
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_роль_счёта_ограничена_известными(сессия: Session) -> None:
    """`unknown` - рабочее состояние, а выдуманная роль - нет."""
    сессия.add(счёт(role="deposit"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


# --- гашение расхода ----------------------------------------------------


def test_расход_не_может_гасить_другой_расход(сессия: Session) -> None:
    """§15.5: гасит только приход.

    Расход, «погашающий» другой расход, - это не доля от друзей, а ошибка
    привязки, и стоит она сразу двух неверных месяцев.
    """
    _со_счётом(сессия)
    стол = операция(fingerprint="b" * 64, amount=Decimal("-5750.00"))
    сессия.add(стол)
    сессия.flush()

    сессия.add(
        операция(
            fingerprint="c" * 64,
            amount=Decimal("-230.00"),
            amount_rub=Decimal("-230.00"),
            kind="refund",
            offsets_transaction_id=стол.id,
        )
    )
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_доля_за_общую_покупку_привязывается_к_расходу(сессия: Session) -> None:
    """Обратная сторона: приход гасит расход - это штатный сценарий owner."""
    _со_счётом(сессия)
    стол = операция(fingerprint="b" * 64, amount=Decimal("-5750.00"))
    сессия.add(стол)
    сессия.flush()

    for номер in (1, 2):
        сессия.add(
            операция(
                fingerprint="c" * 64,
                occurrence_no=номер,
                amount=Decimal("230.00"),
                amount_rub=Decimal("230.00"),
                kind="refund",
                offsets_transaction_id=стол.id,
            )
        )
    сессия.flush()

    погашено = сессия.execute(
        text("select sum(amount) from fin_transactions where offsets_transaction_id = :id"),
        {"id": стол.id},
    ).scalar_one()
    assert погашено == Decimal("460.00")


def test_операция_не_гасит_саму_себя(сессия: Session) -> None:
    """Цикл из одной строки сделал бы эффективную сумму нерасчётной."""
    _со_счётом(сессия)
    приход = операция(amount=Decimal("230.00"), amount_rub=Decimal("230.00"), kind="income")
    сессия.add(приход)
    сессия.flush()

    приход.offsets_transaction_id = приход.id
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_расход_с_гашениями_нельзя_удалить(сессия: Session) -> None:
    """§15.3 запрещает удалять операции вовсе; база не даст осиротить гашение."""
    _со_счётом(сессия)
    стол = операция(fingerprint="b" * 64, amount=Decimal("-5750.00"))
    сессия.add(стол)
    сессия.flush()
    сессия.add(
        операция(
            fingerprint="c" * 64,
            amount=Decimal("230.00"),
            amount_rub=Decimal("230.00"),
            kind="refund",
            offsets_transaction_id=стол.id,
        )
    )
    сессия.flush()

    сессия.delete(стол)
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


# --- категории ----------------------------------------------------------


def test_подкатегория_живёт_внутри_основной(сессия: Session) -> None:
    """§15.4: «фастфуд» → «Макдональдс» - ровно два уровня, и они работают."""
    основная = категория()
    сессия.add(основная)
    сессия.flush()

    сессия.add(
        категория(
            key="mcdonalds",
            title="Макдональдс",
            level=2,
            parent_id=основная.id,
            parent_level=1,
            origin="ai",
        )
    )
    сессия.flush()

    сколько = сессия.execute(
        text("select count(*) from fin_categories where parent_id is not null")
    ).scalar_one()
    assert сколько == 1


def test_третьего_уровня_категорий_не_существует(сессия: Session) -> None:
    """Глубину держит база, а не соглашение в спецификации.

    Родителем объявлена подкатегория - внешний ключ не находит её среди
    строк первого уровня и отказывает.
    """
    основная = категория()
    сессия.add(основная)
    сессия.flush()
    подкатегория = категория(
        key="mcdonalds", title="Макдональдс", level=2, parent_id=основная.id, parent_level=1
    )
    сессия.add(подкатегория)
    сессия.flush()

    сессия.add(
        категория(
            key="bigmac",
            title="Биг-Мак",
            level=2,
            parent_id=подкатегория.id,
            parent_level=1,
        )
    )
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_подкатегория_не_принадлежит_категории_другого_месяца(сессия: Session) -> None:
    """Помесячные версии не должны срастаться между месяцами (ADR-030).

    Подкатегория октября с родителем из сентября сделала бы разбор
    закрытого месяца зависимым от нового - ровно то, чего owner не хотел.
    """
    сентябрьская = категория()
    сессия.add(сентябрьская)
    сессия.flush()

    сессия.add(
        категория(
            key="mcdonalds",
            title="Макдональдс",
            period_month=date(2026, 10, 1),
            level=2,
            parent_id=сентябрьская.id,
            parent_level=1,
        )
    )
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_две_основные_категории_с_одним_ключом_в_месяце_невозможны(сессия: Session) -> None:
    """Ловушка NULL: обычный UNIQUE по (месяц, родитель, ключ) это пропустил бы.

    В Postgres два NULL не равны друг другу, поэтому уникальность основных
    категорий держится частичным индексом, а не составным ограничением.
    """
    сессия.add(категория())
    сессия.flush()

    сессия.add(категория(title="Фастфуд (ещё раз)", origin="ai"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_та_же_категория_в_другом_месяце_законна(сессия: Session) -> None:
    """Обратная сторона: помесячные версии - это норма, а не дубль."""
    сессия.add(категория())
    сессия.add(категория(period_month=date(2026, 10, 1)))
    сессия.flush()

    сколько = сессия.execute(
        text("select count(*) from fin_categories where key = 'fastfood'")
    ).scalar_one()
    assert сколько == 2


def test_основная_категория_не_может_иметь_родителя(сессия: Session) -> None:
    """Форма строки проверяется базой: уровень и родитель не расходятся."""
    основная = категория()
    сессия.add(основная)
    сессия.flush()

    сессия.add(категория(key="mcdonalds", title="Макдональдс", parent_id=основная.id))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_происхождение_и_статус_категории_ограничены(сессия: Session) -> None:
    """`owner` и `ai` различаются не косметически: у них разный срок жизни."""
    сессия.add(категория(origin="нейросеть"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()

    сессия.add(категория(status="черновик"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


# --- правила и резюме ---------------------------------------------------


def test_правило_обязано_что_то_определять(сессия: Session) -> None:
    """Пустое правило - не безобидная строка.

    Разбор молча проходит мимо него, и потом «почему операция без категории»
    не имеет ответа.
    """
    сессия.add(FinCategoryRule(rule_type="merchant", pattern="OOO Leon"))
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()


def test_правило_по_отправителю_определяет_kind(сессия: Session) -> None:
    """§15.5: имя отправителя решает не категорию, а роль в сальдо."""
    сессия.add(FinCategoryRule(rule_type="sender", pattern="Мама", kind="income"))
    сессия.flush()

    что = сессия.execute(text("select kind, category_key from fin_category_rules")).one()
    assert что.kind == "income"
    assert что.category_key is None


def test_резюме_одно_на_импорт(сессия: Session) -> None:
    """§15.9: повторный расчёт заменяет строку, а не копит варианты."""
    загрузка = выписка()
    сессия.add(загрузка)
    сессия.flush()

    for текст in ("Больше на фастфуд", "Больше на фастфуд, чем в среднем"):
        сессия.add(
            FinSummary(
                import_id=загрузка.id,
                period_start=datetime(2026, 9, 1, tzinfo=UTC),
                period_end=datetime(2026, 9, 7, tzinfo=UTC),
                text_ru=текст,
            )
        )
    with pytest.raises(IntegrityError):
        сессия.flush()
    сессия.rollback()

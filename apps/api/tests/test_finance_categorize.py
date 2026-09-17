"""Разбор операций: категории, вид, перевод себе (Ф4а, §15.4, §15.5).

Проверяется не «категория проставилась», а те случаи, в которых книжка
теряет деньги молча: ручная правка, затёртая переразбором; входящий перевод,
тихо погасивший расход; перевод себе, посчитанный и расходом, и приходом.
Каждый из них выглядит как работающая книжка с неверными числами.

Первая половина файла без базы: `разобрать` - чистая функция от операций,
правил, категорий и счетов. Вторая - джоб целиком против настоящей Postgres,
на обезличенной выгрузке Т-Банка за август.
"""

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import байты_фикстуры
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import FinAccount, FinCategory, FinCategoryRule, FinTransaction
from jarvis_api.domain.finance_categorize import (
    Категория,
    Операция,
    Правило,
    Счёт,
    разобрать,
)
from jarvis_api.jobs.finance_categorize import run_once as разобрать_книжку_once
from jarvis_api.jobs.finance_import import run_once as импорт_once

МОСКВА = ZoneInfo("Europe/Moscow")
АВГУСТ = dt.date(2026, 8, 1)
ТБАНК = "tbank"
БСПБ = "bspb"
ФАЙЛ = "tbank_august.csv"

# Набор owner (списки от 2026-09-17). В тестах он нужен целиком только там,
# где проверяется ступень: в остальных достаточно одной категории.
НАБОР = (
    Категория(id=1, period_month=АВГУСТ, key="transfers", title="Переводы"),
    Категория(id=2, period_month=АВГУСТ, key="transport", title="Транспорт"),
    Категория(id=3, period_month=АВГУСТ, key="food", title="Еда"),
    Категория(id=4, period_month=АВГУСТ, key="fastfood", title="Фастфуд"),
    Категория(id=5, period_month=АВГУСТ, key="marketplaces", title="Маркетплейсы"),
    Категория(id=6, period_month=АВГУСТ, key="other", title="Остальное"),
)

СЧЕТА = (
    Счёт(bank=ТБАНК, name="Black", role="checking"),
    Счёт(bank=БСПБ, name="Зарплатный", role="checking"),
)


def операция(**переопределения: Any) -> Операция:
    """Строка книжки после импорта: вид по знаку, категории нет."""
    поля: dict[str, Any] = {
        "id": 1,
        "bank": ТБАНК,
        "account": "Black",
        "occurred_at": dt.datetime(2026, 8, 15, 12, 0, tzinfo=МОСКВА),
        "amount": Decimal("-330.00"),
        "merchant": "Фастфуд Альфа",
        "bank_category": "Фастфуд",
        "own_category": None,
        "mcc": "5814",
        "kind": "expense",
        "kind_source": "sign",
        "category_id": None,
        "category_source": None,
        "transfer_pair_id": None,
        "needs_review": False,
    }
    поля.update(переопределения)
    return Операция(**поля)


def разбор(
    операции: list[Операция],
    правила: list[Правило] | None = None,
    категории: tuple[Категория, ...] = НАБОР,
    счета: tuple[Счёт, ...] = СЧЕТА,
    окно: int = 3,
) -> dict[int, Any]:
    """Решения по идентификатору операции - так их удобнее проверять.

    В словаре только изменившиеся строки: разбор возвращает решение лишь
    там, где что-то меняется, - иначе переразбор каждый раз переписывал бы
    всю книжку и `updated_at` у неё значил бы «когда последний раз считали».
    """
    решения = разобрать(
        операции,
        правила or [],
        list(категории),
        list(счета),
        МОСКВА,
        окно_перевода_дней=окно,
    )
    return {решение.id: решение for решение in решения}


def не_перевод(решения: dict[int, Any], номер: int) -> bool:
    """Операция не стала переводом - либо решения по ней нет вовсе.

    Отсутствие решения - тоже ответ: расход, не нашедший пары, остаётся
    расходом по знаку, и менять в нём нечего.
    """
    return номер not in решения or решения[номер].kind != "transfer"


# --- ступени разбора категории ---------------------------------------------


def test_сильнейшая_ступень_выигрывает() -> None:
    """MCC слабее категории банка, та слабее мерчанта (§15.4)."""
    правила = [
        Правило(rule_type="mcc", pattern="5814", category_key="other"),
        Правило(rule_type="bank_category", pattern="Фастфуд", category_key="food"),
        Правило(rule_type="merchant", pattern="Фастфуд Альфа", category_key="fastfood"),
    ]

    решение = разбор([операция()], правила)[1]

    assert (решение.category_id, решение.category_source) == (4, "merchant")


def test_слабая_ступень_работает_без_сильной() -> None:
    правила = [Правило(rule_type="mcc", pattern="5814", category_key="food")]

    решение = разбор([операция()], правила)[1]

    assert (решение.category_id, решение.category_source) == (3, "mcc")


def test_своя_категория_банка_сопоставляется_с_названием() -> None:
    """Ступень 3 - то, что owner уже разметил в приложении банка (§15.4)."""
    решение = разбор([операция(own_category="Маркетплейсы")], [])[1]

    assert (решение.category_id, решение.category_source) == (5, "own_category")


def test_правило_на_ключ_которого_нет_в_месяце_уступает_слабому() -> None:
    """Набор месяца правит owner, и правило может указывать в пустоту.

    Это не отказ: до утверждения набора работает унаследованный (§15.4).
    Разбор спускается к более слабой ступени, а не остаётся без категории.
    """
    правила = [
        Правило(rule_type="mcc", pattern="5814", category_key="food"),
        Правило(rule_type="merchant", pattern="Фастфуд Альфа", category_key="ещё-не-заведена"),
    ]

    решение = разбор([операция()], правила)[1]

    assert (решение.category_id, решение.category_source) == (3, "mcc")


def test_категория_ищется_в_месяце_операции_по_зоне_owner() -> None:
    """31 августа 21:30 UTC - это 1 сентября в Москве, то есть другой набор.

    Инвариант 7 наоборот: в базе UTC, а месяц книжки - календарный месяц
    owner. Без приведения к зоне ночная операция искала бы категорию
    в наборе прошлого месяца и не нашла бы её.
    """
    сентябрьская = операция(
        id=2, occurred_at=dt.datetime(2026, 8, 31, 21, 30, tzinfo=dt.UTC), mcc="5814"
    )
    правила = [Правило(rule_type="mcc", pattern="5814", category_key="food")]
    сентябрь = (*НАБОР, Категория(id=7, period_month=dt.date(2026, 9, 1), key="food", title="Еда"))

    решения = разбор([операция(), сентябрьская], правила, категории=сентябрь)

    assert решения[1].category_id == 3
    assert решения[2].category_id == 7


def test_пустой_набор_не_падает_и_не_выдумывает() -> None:
    """Категорий ноль - рабочее состояние, как пустой курс (§15.4)."""
    решения = разбор([операция()], [], категории=())

    assert решения == {}


# --- ручная правка ---------------------------------------------------------


def test_ручная_категория_переживает_переразбор() -> None:
    правила = [Правило(rule_type="merchant", pattern="Фастфуд Альфа", category_key="fastfood")]
    ручная = операция(category_id=6, category_source="manual")

    решения = разбор([ручная], правила)

    assert решения == {}


def test_снятая_вручную_категория_не_возвращается() -> None:
    """`manual` при пустой категории - это «owner решил, что её нет» (§15.4)."""
    правила = [Правило(rule_type="merchant", pattern="Фастфуд Альфа", category_key="fastfood")]
    снятая = операция(category_id=None, category_source="manual")

    решения = разбор([снятая], правила)

    assert решения == {}


def test_ручной_вид_не_пересматривается_даже_парой() -> None:
    """Owner мог снять пометку с настоящей пары - вернуть её разбор не вправе."""
    расход = операция(id=1, amount=Decimal("-5000.00"), kind_source="manual", kind="expense")
    приход = операция(
        id=2,
        bank=БСПБ,
        account="Зарплатный",
        amount=Decimal("5000.00"),
        merchant="Роман В.",
        kind="income",
    )

    решения = разбор([расход, приход])

    assert 1 not in решения
    # Второй конец без первого парой не становится: пара взаимна.
    assert не_перевод(решения, 2)


def test_разбор_идемпотентен() -> None:
    """Второй прогон по тем же данным не меняет ничего."""
    правила = [Правило(rule_type="merchant", pattern="Фастфуд Альфа", category_key="fastfood")]
    первый = разбор([операция()], правила)[1]

    уже_разобранная = операция(
        kind=первый.kind,
        kind_source=первый.kind_source,
        category_id=первый.category_id,
        category_source=первый.category_source,
        needs_review=первый.needs_review,
    )

    assert разбор([уже_разобранная], правила) == {}


# --- вид: родные, возврат, умолчание ---------------------------------------


def приход(**переопределения: Any) -> Операция:
    поля: dict[str, Any] = {
        "id": 10,
        "amount": Decimal("230.00"),
        "merchant": "Виктор В.",
        "bank_category": "Переводы",
        "mcc": None,
        "kind": "income",
    }
    поля.update(переопределения)
    return операция(**поля)


def test_родной_отправитель_даёт_доход() -> None:
    правила = [Правило(rule_type="sender", pattern="Ирина В.", kind="income")]

    решение = разбор([приход(merchant="Ирина В.")], правила)[10]

    assert (решение.kind, решение.kind_source, решение.needs_review) == (
        "income",
        "sender_rule",
        False,
    )


def test_входящий_без_правила_гасит_расход_но_с_пометкой() -> None:
    """Умолчание §15.5 - `refund`, и оно не вправе случаться молча."""
    решение = разбор([приход()], [])[10]

    assert (решение.kind, решение.kind_source, решение.needs_review) == (
        "refund",
        "default",
        True,
    )


def test_правило_снимает_свою_пометку_разбора() -> None:
    """Правило появилось позже - операция перестаёт быть непонятной."""
    правила = [Правило(rule_type="sender", pattern="Виктор В.", kind="refund")]
    уже_в_разборе = приход(kind="refund", kind_source="default", needs_review=True)

    решение = разбор([уже_в_разборе], правила)[10]

    assert (решение.kind_source, решение.needs_review) == ("sender_rule", False)


def test_пометка_импорта_не_снимается_разбором() -> None:
    """Кандидат в дубли (§15.3) помечен по другой причине - она не ушла.

    Признак причины - `kind_source`: пометку разбора ставит `default`,
    пометку импорта - строка с провизорным `sign`. Сняв чужую, книжка
    потеряла бы очередь «Требует внимания» ровно там, где выбор между
    «дозрело» и «вторая покупка» стоит денег.
    """
    правила = [Правило(rule_type="sender", pattern="Виктор В.", kind="income")]
    кандидат = приход(kind_source="sign", needs_review=True)

    решение = разбор([кандидат], правила)[10]

    assert (решение.kind, решение.needs_review) == ("income", True)


def test_расход_остаётся_расходом() -> None:
    решения = разбор([операция(kind="income", kind_source="sign")], [])

    assert решения[1].kind == "expense"


# --- перевод между своими счетами (ADR-041) --------------------------------


def пара_концов(
    *,
    сумма: str = "5000.00",
    день_расхода: int = 10,
    день_прихода: int = 10,
) -> list[Операция]:
    """Списание в одном банке и зачисление в другом - два файла, две строки."""
    return [
        операция(
            id=1,
            bank=БСПБ,
            account="Зарплатный",
            amount=-Decimal(сумма),
            merchant="Роман В.",
            bank_category="Переводы",
            mcc=None,
            occurred_at=dt.datetime(2026, 8, день_расхода, 12, 0, tzinfo=МОСКВА),
        ),
        операция(
            id=2,
            bank=ТБАНК,
            account="Black",
            amount=Decimal(сумма),
            merchant="Роман В.",
            bank_category="Переводы",
            mcc=None,
            kind="income",
            occurred_at=dt.datetime(2026, 8, день_прихода, 13, 0, tzinfo=МОСКВА),
        ),
    ]


def test_пара_концов_даёт_перевод_с_обеих_сторон() -> None:
    решения = разбор(пара_концов())

    assert решения[1].kind == решения[2].kind == "transfer"
    assert решения[1].kind_source == "transfer_pair"
    assert (решения[1].transfer_pair_id, решения[2].transfer_pair_id) == (2, 1)


def test_пара_в_пределах_окна_но_не_за_ним() -> None:
    внутри = разбор(пара_концов(день_расхода=10, день_прихода=13))
    снаружи = разбор(пара_концов(день_расхода=10, день_прихода=14))

    assert внутри[1].kind == "transfer"
    assert не_перевод(снаружи, 1) and не_перевод(снаружи, 2)


def test_неразмеченный_счёт_пары_не_даёт() -> None:
    """Пока роль счёта `unknown`, перевод себе неотличим от перевода человеку."""
    счета = (Счёт(bank=ТБАНК, name="Black", role="checking"),)

    решения = разбор(пара_концов(), счета=счета)

    assert не_перевод(решения, 1) and не_перевод(решения, 2)


def test_два_кандидата_уводят_оба_конца_в_разбор() -> None:
    """Выбор наугад стоил бы двух неверных месяцев сразу (§15.5)."""
    операции = [
        *пара_концов(),
        операция(
            id=3,
            bank=ТБАНК,
            account="Black",
            amount=Decimal("5000.00"),
            merchant="Игорь И.",
            bank_category="Переводы",
            mcc=None,
            kind="income",
            occurred_at=dt.datetime(2026, 8, 10, 15, 0, tzinfo=МОСКВА),
        ),
    ]

    решения = разбор(операции)

    assert all(не_перевод(решения, номер) for номер in (1, 2, 3))
    assert решения[1].needs_review and решения[2].needs_review and решения[3].needs_review
    # И ни один из входящих концов не стал молча гасить расход.
    assert решения[2].kind_source != "default"


def test_имя_owner_без_пары_уводит_в_разбор_а_не_в_возврат() -> None:
    """Подтверждающая роль имени (ADR-041): второй файл ещё не загружен."""
    правила = [Правило(rule_type="self", pattern="Роман В.")]
    одинокий = пара_концов()[1]

    решение = разбор([одинокий], правила)[2]

    assert решение.needs_review is True
    assert решение.kind != "refund"
    assert решение.kind_source != "default"


def test_имя_owner_не_назначает_перевод_само_по_себе() -> None:
    """Однофамилец ломает имя - поэтому основание это пара, а не имя."""
    правила = [Правило(rule_type="self", pattern="Роман В.")]
    одинокий = пара_концов()[1]

    решение = разбор([одинокий], правила)[2]

    assert решение.kind != "transfer"
    assert решение.transfer_pair_id is None


def test_перевод_внутри_одного_банка_парой_не_считается() -> None:
    """Внутри банка такой перевод приезжает одной строкой, а не двумя."""
    расход, приход_ = пара_концов()
    в_одном_банке = [replace(расход, bank=ТБАНК, account="Black"), приход_]

    решения = разбор(в_одном_банке)

    assert не_перевод(решения, 1) and не_перевод(решения, 2)


# --- джоб против настоящей базы --------------------------------------------


def август() -> bytes:
    return байты_фикстуры("statements", ФАЙЛ)


def загрузить_август(сессия: Session) -> None:
    assert импорт_once(сессия, [(ФАЙЛ, август())], apply=True) == 0


def завести_набор(сессия: Session) -> None:
    """Набор августа и правила родных - то, что прислал owner 2026-09-17."""
    for ключ, название in (
        ("transfers", "Переводы"),
        ("transport", "Транспорт"),
        ("food", "Еда"),
        ("marketplaces", "Маркетплейсы"),
        ("other", "Остальное"),
    ):
        сессия.add(
            FinCategory(
                key=ключ,
                period_month=АВГУСТ,
                level=1,
                title=название,
                origin="owner",
                status="active",
            )
        )
    for имя in ("Алексей А.", "Борис Б."):
        сессия.add(FinCategoryRule(rule_type="sender", pattern=имя, kind="income", title=имя))
    сессия.flush()


def операции(сессия: Session) -> list[FinTransaction]:
    return list(сессия.scalars(select(FinTransaction).order_by(FinTransaction.id)).all())


def test_разбор_после_импорта_размечает_входящие(сессия: Session) -> None:
    """Импорт с `--apply` разбирает книжку сам.

    Иначе сальдо считалось бы по строкам с провизорным видом, и узнать
    об этом owner было бы неоткуда.
    """
    завести_набор(сессия)

    загрузить_август(сессия)

    родные = [с for с in операции(сессия) if с.merchant == "Алексей А."]
    прочие = [с for с in операции(сессия) if с.merchant == "Виктор В." and с.amount > 0]
    assert родные and {(с.kind, с.kind_source) for с in родные} == {("income", "sender_rule")}
    assert прочие and {(с.kind, с.kind_source, с.needs_review) for с in прочие} == {
        ("refund", "default", True)
    }


def test_переразбор_применяет_правило_добавленное_позже(сессия: Session) -> None:
    """Правила приезжают позже данных - ради этого и нужна отдельная команда."""
    загрузить_август(сессия)
    до = [с for с in операции(сессия) if с.merchant == "Жанна Ж."]
    assert {с.kind_source for с in до} == {"default"}

    сессия.add(
        FinCategoryRule(rule_type="sender", pattern="Жанна Ж.", kind="income", title="родная")
    )
    сессия.flush()
    assert разобрать_книжку_once(сессия, Settings(), apply=True) == 0

    после = [с for с in операции(сессия) if с.merchant == "Жанна Ж."]
    assert {(с.kind, с.kind_source, с.needs_review) for с in после} == {
        ("income", "sender_rule", False)
    }


def test_разбор_проставляет_категории_по_правилу_мерчанта(сессия: Session) -> None:
    завести_набор(сессия)
    загрузить_август(сессия)
    еда = сессия.scalars(
        select(FinCategory).where(FinCategory.key == "food", FinCategory.period_month == АВГУСТ)
    ).one()
    сессия.add(FinCategoryRule(rule_type="merchant", pattern="Фастфуд Альфа", category_key="food"))
    сессия.flush()

    assert разобрать_книжку_once(сессия, Settings(), apply=True) == 0

    размеченные = [с for с in операции(сессия) if с.merchant == "Фастфуд Альфа"]
    assert размеченные
    assert {(с.category_id, с.category_source) for с in размеченные} == {(еда.id, "merchant")}


def test_dry_run_разбора_не_пишет_ни_строки(сессия: Session) -> None:
    загрузить_август(сессия)
    сессия.add(
        FinCategoryRule(rule_type="sender", pattern="Игорь И.", kind="income", title="родной")
    )
    сессия.flush()

    assert разобрать_книжку_once(сессия, Settings(), apply=False) == 0

    игорь = [с for с in операции(сессия) if с.merchant == "Игорь И."]
    assert игорь and {с.kind_source for с in игорь} == {"default"}


def test_перевод_между_банками_опознаётся_через_базу(сессия: Session) -> None:
    """Пара концов из двух банков - то, ради чего разбор читает книжку целиком."""
    загрузить_август(сессия)
    for счёт in сессия.scalars(select(FinAccount)).all():
        счёт.role = "checking"
    сессия.add(FinAccount(bank=БСПБ, name="Зарплатный", role="checking"))
    # Второй конец перевода: 5 000 ₽ ушли из БСПБ 12 августа, пришли в Т-Банк
    # тем же днём (строка «Игорь И.» настоящей выгрузки - на ту же сумму).
    приход_тбанка = сессия.scalars(
        select(FinTransaction).where(FinTransaction.amount == Decimal("5000.00"))
    ).one()
    сессия.add(
        FinTransaction(
            bank=БСПБ,
            account="Зарплатный",
            occurred_at=приход_тбанка.occurred_at - dt.timedelta(hours=1),
            amount=Decimal("-5000.00"),
            amount_rub=Decimal("-5000.00"),
            currency="RUB",
            merchant="Роман В.",
            kind="expense",
            status="posted",
            fingerprint="перевод".ljust(64, "0"),
        )
    )
    сессия.flush()

    assert разобрать_книжку_once(сессия, Settings(), apply=True) == 0

    сессия.refresh(приход_тбанка)
    второй = сессия.scalars(select(FinTransaction).where(FinTransaction.bank == БСПБ)).one()
    assert приход_тбанка.kind == второй.kind == "transfer"
    assert приход_тбанка.transfer_pair_id == второй.id
    assert второй.transfer_pair_id == приход_тбанка.id


@pytest.mark.parametrize("apply", [False, True])
def test_книжка_без_правил_разбирается_без_отказа(сессия: Session, apply: bool) -> None:
    """Правил ноль - рабочее состояние, а не отказ (§15.4)."""
    загрузить_август(сессия)

    assert разобрать_книжку_once(сессия, Settings(), apply=apply) == 0

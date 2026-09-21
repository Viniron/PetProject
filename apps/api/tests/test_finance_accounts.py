"""Разметка своих счетов: роли checking / savings (Ф5, §15.5, ADR-030).

По этой разметке считается статья «Отложено» и по ней же разбор отличает
перевод себе от перевода человеку (ADR-041). Ошибка стоит денег дважды,
поэтому проверяются не «роль записалась», а отказы: счёт с опечаткой
в имени, роль не из списка и разметка без `--apply`.
"""

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.db.models import FinAccount, FinTransaction
from jarvis_api.domain.finance_balance import ОшибкаРоли, назначить_роль, роли_счетов
from jarvis_api.jobs.finance_accounts import run_once, описать

МОСКВА = ZoneInfo("Europe/Moscow")
БАНК = "tbank"
КАРТА = "Black"
КОПИЛКА = "Накопительный счёт"


@pytest.fixture
def счета(сессия: Session) -> Session:
    """Два счёта: один размечен owner, второй ждёт разметки."""
    сессия.add_all(
        [
            FinAccount(bank=БАНК, name=КАРТА, role="checking"),
            FinAccount(bank=БАНК, name=КОПИЛКА, role="unknown"),
        ]
    )
    сессия.commit()
    return сессия


def перечитать(сессия: Session, имя: str) -> FinAccount:
    счёт = сессия.scalar(select(FinAccount).where(FinAccount.bank == БАНК, FinAccount.name == имя))
    assert счёт is not None
    return счёт


# --- домен ----------------------------------------------------------------


def test_роли_счетов_ключ_это_пара_банк_и_имя(счета: Session) -> None:
    """Одинаково названные счета в двух банках - обычное дело, склеивать нельзя."""
    это = роли_счетов(счета)

    assert это == {(БАНК, КАРТА): "checking", (БАНК, КОПИЛКА): "unknown"}


def test_назначение_роли_пишет_строку(счета: Session) -> None:
    изменилось = назначить_роль(счета, банк=БАНК, счёт=КОПИЛКА, роль="savings")

    assert изменилось
    assert перечитать(счета, КОПИЛКА).role == "savings"


def test_та_же_роль_ничего_не_меняет(счета: Session) -> None:
    assert not назначить_роль(счета, банк=БАНК, счёт=КАРТА, роль="checking")


def test_неизвестная_роль_отклонена(счета: Session) -> None:
    with pytest.raises(ОшибкаРоли) as отказ:
        назначить_роль(счета, банк=БАНК, счёт=КАРТА, роль="копилка")

    assert отказ.value.код == "неизвестная_роль"


def test_счёт_не_заводится_разметкой(счета: Session) -> None:
    """Опечатка в имени не должна создавать двойника, на который никто не смотрит."""
    with pytest.raises(ОшибкаРоли) as отказ:
        назначить_роль(счета, банк=БАНК, счёт="Накопительный счет", роль="savings")

    assert отказ.value.код == "нет_счёта"
    assert len(роли_счетов(счета)) == 2


# --- команда --------------------------------------------------------------


def test_показ_без_аргументов_не_пишет_ничего(счета: Session) -> None:
    код = run_once(счета, банк=None, счёт=None, роль=None, apply=False)

    assert код == 0
    assert перечитать(счета, КОПИЛКА).role == "unknown"


def test_dry_run_не_пишет_роль(счета: Session) -> None:
    """Всё, что пишет, по умолчанию dry-run - и это проверяется, а не заявляется."""
    код = run_once(счета, банк=БАНК, счёт=КОПИЛКА, роль="savings", apply=False)

    assert код == 0
    assert перечитать(счета, КОПИЛКА).role == "unknown"


def test_apply_пишет_роль(счета: Session) -> None:
    код = run_once(счета, банк=БАНК, счёт=КОПИЛКА, роль="savings", apply=True)

    assert код == 0
    assert перечитать(счета, КОПИЛКА).role == "savings"


def test_частичные_аргументы_отклонены(счета: Session) -> None:
    """`--bank` без `--role` - оборванная команда, а не показ: молчать о ней нельзя."""
    код = run_once(счета, банк=БАНК, счёт=КОПИЛКА, роль=None, apply=True)

    assert код == 2


def test_несуществующий_счёт_даёт_ненулевой_код(счета: Session) -> None:
    код = run_once(счета, банк="sber", счёт=КАРТА, роль="savings", apply=True)

    assert код == 1


def test_описание_называет_неразмеченные(счета: Session) -> None:
    текст = "\n".join(описать(счета, apply=False, изменение=None))

    assert "роль не задана" in текст
    assert "«Отложено» показывает слова" in текст


def test_описание_пустой_книжки_объясняет_откуда_берутся_счета(сессия: Session) -> None:
    текст = "\n".join(описать(сессия, apply=False, изменение=None))

    assert "заводятся импортом выписки" in текст


def test_разметка_включает_отложено(счета: Session) -> None:
    """Смысл этапа целиком: пока счёт без роли, статья молчит, после - считается."""
    from jarvis_api.domain.finance_balance import собрать

    счета.add(
        FinTransaction(
            bank=БАНК,
            account=КОПИЛКА,
            occurred_at=dt.datetime(2026, 8, 23, 12, 0, tzinfo=МОСКВА),
            amount=Decimal("5000.00"),
            amount_rub=Decimal("5000.00"),
            currency="RUB",
            kind="transfer",
            kind_source="transfer_pair",
            fingerprint="отложено".ljust(64, "0")[:64],
        )
    )
    счета.commit()
    сейчас = dt.datetime(2026, 9, 21, 12, 0, tzinfo=МОСКВА)
    месяц = dt.date(2026, 8, 1)

    до = собрать(счета, месяц_обзора=месяц, сейчас=сейчас, зона=МОСКВА)
    run_once(счета, банк=БАНК, счёт=КОПИЛКА, роль="savings", apply=True)
    после = собрать(счета, месяц_обзора=месяц, сейчас=сейчас, зона=МОСКВА)

    assert до.данные.отложено is None
    assert после.данные.отложено == Decimal("5000.00")

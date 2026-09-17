"""Разбор книжки поверх базы: правила к операциям (§15.4, §15.5).

Арифметика решений живёт в `domain/finance_categorize.py` и базы не знает;
здесь - только чтение среза, печать диффа и запись. Разделение то же, что
у импорта, и по той же причине: решение «эта операция гасит расход» стоит
денег owner и обязано проверяться прямым вызовом.

**Запускается руками и из импорта.** Руками - когда owner добавил правило
или разметил счёт: правила приезжают позже данных, и книжку надо
переразобрать, не перезагружая выписки. Из импорта - потому что иначе
свежезагруженные операции лежали бы с провизорным `kind` до следующего
ручного прогона, а сальдо по ним уже считалось бы.

**Срез - вся книжка, а не новые строки.** Перевод себе опознаётся парой
концов из двух банков (ADR-041), и второй конец приезжает другим файлом
в другой день. Разбор только что загруженного не нашёл бы ни одной пары:
пара становится видна ровно тогда, когда загружен второй файл, то есть
при разборе целиком.

**Дифф печатается и в dry-run, и в `--apply`,** с причиной по каждой строке.
«Стало transfer» без причины owner проверить не может, а проверять здесь
есть что: одна неверная пара - это два неверных месяца сразу.
"""

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.models import FinAccount, FinCategory, FinCategoryRule, FinTransaction
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain.finance_categorize import (
    Категория,
    Операция,
    Правило,
    Разбор,
    Счёт,
    разобрать,
)
from jarvis_api.jobs.common import OwnerZoneError, owner_timezone

logger = logging.getLogger("jarvis.finance_categorize")


@dataclass(slots=True)
class ОтчётРазбора:
    """Что разбор сделал бы или сделал. Печатается одинаково в обеих фазах."""

    операций: int = 0
    правил: int = 0
    категорий: int = 0
    изменений: int = 0
    переводов: int = 0
    в_разбор: int = 0
    без_категории: int = 0


def _срез(session: Session) -> list[Операция]:
    """Вся книжка: операции читаются целиком ради пар переводов.

    Отменённые (`reverted`) тоже: банк вправе вернуть операцию в выгрузку
    следующим файлом, и тогда её разбор обязан быть на месте, а не начат
    заново. В сальдо они не входят - но это забота Ф5, а не разбора.
    """
    строки = session.scalars(select(FinTransaction).order_by(FinTransaction.id)).all()
    return [
        Операция(
            id=строка.id,
            bank=строка.bank,
            account=строка.account,
            occurred_at=строка.occurred_at,
            amount=строка.amount,
            merchant=строка.merchant,
            bank_category=строка.bank_category,
            own_category=строка.own_category,
            mcc=строка.mcc,
            kind=строка.kind,
            kind_source=строка.kind_source,
            category_id=строка.category_id,
            category_source=строка.category_source,
            transfer_pair_id=строка.transfer_pair_id,
            needs_review=строка.needs_review,
        )
        for строка in строки
    ]


def _правила(session: Session) -> list[Правило]:
    строки = session.scalars(select(FinCategoryRule).order_by(FinCategoryRule.id)).all()
    return [
        Правило(
            rule_type=строка.rule_type,
            pattern=строка.pattern,
            category_key=строка.category_key,
            kind=строка.kind,
        )
        for строка in строки
    ]


def _категории(session: Session) -> list[Категория]:
    строки = session.scalars(select(FinCategory).order_by(FinCategory.id)).all()
    return [
        Категория(
            id=строка.id,
            period_month=строка.period_month,
            key=строка.key,
            title=строка.title,
            status=строка.status,
        )
        for строка in строки
    ]


def _счета(session: Session) -> list[Счёт]:
    строки = session.scalars(select(FinAccount).order_by(FinAccount.id)).all()
    return [Счёт(bank=строка.bank, name=строка.name, role=строка.role) for строка in строки]


def применить(session: Session, решения: Sequence[Разбор]) -> None:
    """Записывает решения. Транзакцию коммитит вызывающий код.

    Пара проставляется с обеих сторон сама собой: оба конца лежат в одном
    срезе, и решение есть у каждого. Отдельной сшивки нет намеренно - она
    была бы вторым местом, где держится симметрия, и разошлась бы с первым.
    """
    for решение in решения:
        операция = session.get(FinTransaction, решение.id)
        if операция is None:
            continue
        операция.kind = решение.kind
        операция.kind_source = решение.kind_source
        операция.category_id = решение.category_id
        операция.category_source = решение.category_source
        операция.transfer_pair_id = решение.transfer_pair_id
        операция.needs_review = решение.needs_review


def разобрать_книжку(
    session: Session,
    settings: Settings,
    *,
    apply: bool,
) -> tuple[ОтчётРазбора, list[Разбор]]:
    """Считает разбор всей книжки и, если просят, записывает его."""
    зона = owner_timezone(session)
    операции = _срез(session)
    правила = _правила(session)
    категории = _категории(session)

    решения = разобрать(
        операции,
        правила,
        категории,
        _счета(session),
        зона,
        окно_перевода_дней=settings.finance_transfer_window_days,
    )

    отчёт = ОтчётРазбора(
        операций=len(операции),
        правил=len(правила),
        категорий=len(категории),
        изменений=len(решения),
        переводов=sum(1 for р in решения if р.transfer_pair_id is not None),
        в_разбор=sum(1 for р in решения if р.needs_review),
    )
    # Считается по итогу, а не по решениям: операция без категории могла
    # такой и остаться, то есть в решения не попасть вовсе. Число отвечает
    # на вопрос owner «сколько ещё не разобрано», а не «сколько я поменял».
    новые_категории = {р.id: р.category_id for р in решения}
    отчёт.без_категории = sum(
        1 for о in операции if новые_категории.get(о.id, о.category_id) is None
    )

    if apply:
        применить(session, решения)
    return отчёт, решения


def описать(отчёт: ОтчётРазбора, решения: Sequence[Разбор], apply: bool) -> list[str]:
    """Человекочитаемый дифф. Отдельной функцией - её проверяет тест."""
    строки = [
        f"разбор: операций {отчёт.операций}, правил {отчёт.правил}, категорий {отчёт.категорий}",
        f"  меняется {отчёт.изменений}, из них переводов {отчёт.переводов},"
        f" в разбор {отчёт.в_разбор}",
        f"  без категории после разбора: {отчёт.без_категории}",
    ]
    # Причина у каждой строки: «стало transfer» без неё непроверяемо.
    # Группировкой по причине, а не построчно: сотня одинаковых строк
    # «входящий перевод без правила» не помогает прочитать дифф.
    по_причине: dict[str, int] = {}
    for решение in решения:
        по_причине[решение.причина] = по_причине.get(решение.причина, 0) + 1
    for причина in sorted(по_причине):
        строки.append(f"  {по_причине[причина]}: {причина}")
    if отчёт.правил == 0:
        строки.append(
            "  правил ноль: книжка работает без разбора - это рабочее состояние, а не отказ (§15.4)"
        )
    if not apply:
        строки.append("  dry-run: в базу не записано ничего")
    return строки


def run_once(session: Session, settings: Settings, *, apply: bool) -> int:
    """Прогон поверх готовой сессии. Возвращает код возврата процесса."""
    try:
        отчёт, решения = разобрать_книжку(session, settings, apply=apply)
    except OwnerZoneError as сбой:
        logger.error("разбор не выполнен: %s", сбой)
        return 1

    if apply:
        session.commit()
    else:
        session.rollback()

    for строка in описать(отчёт, решения, apply):
        logger.info("%s", строка)
    return 0


def run(settings: Settings, *, apply: bool) -> int:
    """Прогон целиком: своя сессия, свой код возврата."""
    with get_sessionmaker()() as session:
        return run_once(session, settings, apply=apply)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага в базу не пишется ничего."""
    parser = argparse.ArgumentParser(description="Разбор операций книжки JARVIS по правилам owner")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать разбор (без флага - только показать дифф)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

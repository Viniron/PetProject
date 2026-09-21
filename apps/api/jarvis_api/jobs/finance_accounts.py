"""Разметка своих счетов: показать роли и поставить роль (§15.5, ADR-030).

Роль счёта - данные owner, а не догадка по имени: с несколькими банками
угадывание ошибается в деньгах, потому что по этой разметке считается
статья «Отложено» и по ней же перевод себе отличается от перевода человеку.

**Аргументами команды, а не файлом** (решение owner, 2026-09-21). Категории
и правила приезжают набором в сотни строк, и им файл нужен; счетов у owner
единицы, и формат файла ради двух строк - лишняя сущность, которую пришлось
бы описывать и проверять.

**Счёт здесь не заводится.** Имя счёта появляется в книжке импортом выписки,
где оно встретилось. Разметить счёт, которого нет, - опечатка в написании,
и завести его молча значило бы получить двойника, на который не смотрит
ни одна операция, и «Отложено», считающее по пустому счёту.
"""

import argparse
import logging
from collections.abc import Sequence

from sqlalchemy.orm import Session

from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain.finance_balance import (
    НЕИЗВЕСТНАЯ_РОЛЬ,
    РОЛИ,
    ОшибкаРоли,
    назначить_роль,
    счета_списком,
)

logger = logging.getLogger("jarvis.finance_accounts")


def описать(session: Session, apply: bool, изменение: str | None) -> list[str]:
    """Счета книжки с ролями. Отдельной функцией - её проверяет тест."""
    строки: list[str] = []
    if изменение is not None:
        строки.append(изменение)

    счета = счета_списком(session)
    if not счета:
        строки.append("счетов в книжке нет: они заводятся импортом выписки (make finance-import)")
        return строки

    строки.append(f"счетов в книжке: {len(счета)}")
    for счёт in счета:
        хвост = "  <- роль не задана" if счёт.role == НЕИЗВЕСТНАЯ_РОЛЬ else ""
        строки.append(f"  {счёт.bank} · {счёт.name}: {счёт.role}{хвост}")

    неразмеченных = sum(1 for счёт in счета if счёт.role == НЕИЗВЕСТНАЯ_РОЛЬ)
    if неразмеченных:
        # Пока хоть один счёт без роли, статья «Отложено» не показывается
        # вовсе - и owner должен понимать, почему (§15.5).
        строки.append(
            f"  не размечено {неразмеченных}: пока они есть,"
            " «Отложено» показывает слова, а не сумму (§15.5)"
        )
    if изменение is not None and not apply:
        строки.append("  dry-run: в базу не записано ничего")
    return строки


def run_once(
    session: Session,
    *,
    банк: str | None,
    счёт: str | None,
    роль: str | None,
    apply: bool,
) -> int:
    """Прогон поверх готовой сессии. Возвращает код возврата процесса.

    Без всех трёх аргументов - только показ. Это не режим «по умолчанию
    ничего не делать», а отдельное действие: посмотреть, как счёт назван
    в книжке, нужно раньше, чем ставить ему роль.
    """
    задано = [значение for значение in (банк, счёт, роль) if значение is not None]
    if задано and len(задано) != 3:
        logger.error("разметка требует --bank, --account и --role сразу")
        return 2

    изменение: str | None = None
    if len(задано) == 3:
        assert банк is not None and счёт is not None and роль is not None
        try:
            изменилось = назначить_роль(session, банк=банк, счёт=счёт, роль=роль)
        except ОшибкаРоли as отказ:
            # Отказ - обычный исход: имя счёта owner набирает руками.
            logger.error("отклонено (%s): %s", отказ.код, отказ)
            session.rollback()
            return 1
        изменение = (
            f"{банк} · {счёт}: роль {роль}"
            if изменилось
            else f"{банк} · {счёт}: роль уже {роль}, менять нечего"
        )

    строки = описать(session, apply, изменение)
    if apply:
        session.commit()
    else:
        session.rollback()

    for строка in строки:
        logger.info("%s", строка)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага в базу не пишется ничего."""
    parser = argparse.ArgumentParser(description="Роли своих счетов в книжке JARVIS (§15.5)")
    parser.add_argument("--bank", default=None, help="банк счёта, как он записан в книжке")
    parser.add_argument("--account", default=None, help="имя счёта, как оно записано в книжке")
    parser.add_argument(
        "--role",
        default=None,
        choices=РОЛИ,
        help="роль счёта: savings считается в «Отложено», checking - нет",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать роль (без флага - только показать, что изменится)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with get_sessionmaker()() as session:
        return run_once(
            session,
            банк=args.bank,
            счёт=args.account,
            роль=args.role,
            apply=args.apply,
        )


if __name__ == "__main__":
    raise SystemExit(main())

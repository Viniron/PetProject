"""Гашение расхода поверх базы: привязать, снять, показать (§15.5).

Арифметика и окно живут в `domain/finance_offsets.py` и базы не знают;
здесь - чтение строк, печать диффа и запись. Разделение то же, что
у разбора, и по той же причине: «этот расход на самом деле стоил дешевле» -
решение о деньгах owner, и проверяется оно прямым вызовом.

**Джоб, а не только домен, потому что экрана ещё нет.** Привязка приезжает
в API на Ф6 и на экран на Ф7, а разбирать входящие переводы августа owner
хочет раньше: Ф4а вывел их в очередь и ответа на вопрос «за что это» не дал.
Без команды домен Ф4б был бы кодом, который до Ф7 никто не выполнял.

**Дифф печатается и в dry-run, и в `--apply`,** и показывает расход целиком:
что было, что гасит, что осталось. «Привязано» без суммы owner проверить
не может, а ошибка здесь тихо меняет два месяца сразу.
"""

import argparse
import datetime as dt
import logging
from collections.abc import Sequence

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from jarvis_api.db.models import FinTransaction
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain.finance_offsets import (
    ОшибкаПривязки,
    Привязка,
    Разбивка,
    привязать,
    разбивка,
    снять,
)
from jarvis_api.jobs.common import OwnerZoneError, owner_timezone

logger = logging.getLogger("jarvis.finance_offset")


def описать(разбор: Разбивка, действие: Привязка | None, apply: bool) -> list[str]:
    """Человекочитаемый дифф. Отдельной функцией - её проверяет тест."""
    строки: list[str] = []
    if действие is not None:
        if not действие.изменилось:
            строки.append(f"гашение {действие.гашение_id}: и так в этом состоянии, менять нечего")
        elif действие.расход_id is None:
            строки.append(f"снята привязка гашения {действие.гашение_id}")
        else:
            строки.append(f"гашение {действие.гашение_id} привязано к расходу {действие.расход_id}")
        строки.extend(f"  внимание: {текст}" for текст in действие.предупреждения)

    строки.append(
        f"расход {разбор.расход_id}: было {разбор.сумма} ₽,"
        f" погашено {разбор.погашено} ₽, эффективная {разбор.эффективная} ₽"
    )
    for часть in разбор.части:
        хвост = f", излишек {часть.излишек} ₽ доходом" if часть.излишек > 0 else ""
        строки.append(f"  гашение {часть.гашение_id}: {часть.погасило} ₽{хвост}")
    if разбор.излишек > 0:
        # Явной строкой, а не только в частях: излишек попадает в другой
        # месяц, чем гашение, и owner обязан увидеть это, а не вычитать.
        строки.append(f"  излишек {разбор.излишек} ₽ - доход месяца поступления (§15.5)")
    for номер in разбор.вне_счёта:
        строки.append(f"  гашение {номер} в счёт не идёт: отменено, исключено или ждёт разбора")
    if not apply:
        строки.append("  dry-run: в базу не записано ничего")
    return строки


def run_once(
    session: Session,
    *,
    гашение_id: int | None,
    расход_id: int | None,
    отвязать: bool,
    показать: int | None,
    apply: bool,
    сейчас: dt.datetime | None = None,
) -> int:
    """Прогон поверх готовой сессии. Возвращает код возврата процесса.

    `сейчас` передаётся тестом: окно привязки считается от него, и проверить
    отказ «вне окна» иначе можно было бы только сменой системных часов.
    """
    момент = сейчас or dt.datetime.now(dt.UTC)
    try:
        зона = owner_timezone(session)
        действие: Привязка | None = None
        if показать is not None:
            цель = показать
        elif отвязать:
            if гашение_id is None:
                logger.error("снятие без --income: нечего снимать")
                return 2
            действие = снять(session, гашение_id=гашение_id, сейчас=момент, зона=зона)
            # Разбивку показываем по тому расходу, который только что
            # перестал гаситься: иначе дифф снятия не говорит ничего.
            цель = _расход_снятия(session, гашение_id)
        else:
            if гашение_id is None or расход_id is None:
                logger.error("привязка требует --income и --expense")
                return 2
            действие = привязать(
                session,
                гашение_id=гашение_id,
                расход_id=расход_id,
                сейчас=момент,
                зона=зона,
            )
            цель = расход_id
        разбор = разбивка(session, цель)
    except OwnerZoneError as сбой:
        logger.error("гашение не выполнено: %s", сбой)
        return 1
    except ОшибкаПривязки as отказ:
        # Отказ - обычный исход, а не сбой: окно и знаки на то и проверяются.
        # Код печатается рядом с текстом, чтобы на Ф6 тело отказа собиралось
        # из того же значения, что owner видел в терминале.
        logger.error("отклонено (%s): %s", отказ.код, отказ)
        session.rollback()
        return 1

    if apply:
        session.commit()
    else:
        session.rollback()

    for строка in описать(разбор, действие, apply):
        logger.info("%s", строка)
    return 0


def _расход_снятия(session: Session, гашение_id: int) -> int:
    """Расход, чью разбивку показывать после снятия.

    Ссылка к этому моменту уже стёрта в сессии, поэтому берётся из истории
    объекта - иначе пришлось бы читать строку до действия и держать её
    ради одной цифры.
    """
    гашение = session.get(FinTransaction, гашение_id)
    if гашение is None:
        raise ОшибкаПривязки("нет_операции", f"поступление {гашение_id} в книжке не найдено")
    if гашение.offsets_transaction_id is not None:
        return гашение.offsets_transaction_id
    прошлое = inspect(гашение).attrs.offsets_transaction_id.history.deleted
    if прошлое and прошлое[0] is not None:
        return int(прошлое[0])
    raise ОшибкаПривязки(
        "не_привязано",
        f"поступление {гашение_id} ничего не гасит: снимать нечего",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага в базу не пишется ничего."""
    parser = argparse.ArgumentParser(
        description="Гашение расхода поступлениями в книжке JARVIS (§15.5)"
    )
    parser.add_argument("--income", type=int, default=None, help="id поступления-гашения")
    parser.add_argument("--expense", type=int, default=None, help="id расхода, который гасится")
    parser.add_argument(
        "--unlink",
        action="store_true",
        help="снять привязку с поступления --income",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=None,
        help="только показать разбивку расхода с этим id",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать привязку (без флага - только показать дифф)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with get_sessionmaker()() as session:
        return run_once(
            session,
            гашение_id=args.income,
            расход_id=args.expense,
            отвязать=args.unlink,
            показать=args.show,
            apply=args.apply,
        )


if __name__ == "__main__":
    raise SystemExit(main())

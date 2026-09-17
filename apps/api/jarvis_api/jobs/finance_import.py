"""Джоб импорта выписок: файлы owner → `fin_transactions` (§15.3).

Запускается руками, а не планировщиком: канал книжки - файл, который owner
выгружает из банка примерно раз в неделю (ADR-024), и автоматического канала
нет и не планируется.

**Две фазы.** Без `--apply` джоб показывает дифф и не пишет ни строки. Правило
`CLAUDE.md` про dry-run написано для записей наружу, здесь запись внутрь своей
базы - но фаза сохраняется по другой причине: она и есть подтверждение owner.
Поштучно подтверждать сотню строк выписки бессмысленно, а файл целиком -
ровно та единица, которой owner думает.

**За один заход - несколько файлов,** по одному с каждого банка (ADR-030).
Каждый учитывается своей строкой `fin_imports`, каждый идёт своей
транзакцией, и **отказ на одном файле не отменяет остальные**: банк, сменивший
формат, не должен блокировать учёт по другим. Код возврата при этом
ненулевой - импорт падает громко, даже если упала одна восьмая работы.

**Идемпотентность держит база, а не код.** `fin_imports.sha256` уникален:
тот же файл второй раз не создаёт вторую строку импорта. Проверка «а не
грузили ли мы уже это» в коде была бы слабее - гонка двух прогонов прошла
бы её дважды. Но и сверка кратности (`domain/finance_import.py`) сама по себе
идемпотентна: тот же файл под другим именем добавит ноль строк.

**Строки в `job_runs` этот джоб не пишет.** Она одна на джоб и день, а импорт
за день запускается сколько угодно раз - вторая выписка затёрла бы след
первой. След импорта - это `fin_imports`, и он на файл, а не на день.
"""

import argparse
import datetime as dt
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.models import FinAccount, FinImport, FinTransaction
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain.finance_import import ПланИмпорта, Существующая, спланировать
from jarvis_api.integrations.statements import ParsedStatement, StatementError, parse_statement
from jarvis_api.integrations.statements.base import StatementRow
from jarvis_api.jobs.common import OwnerZoneError, owner_timezone, границы_окна
from jarvis_api.jobs.finance_categorize import описать as описать_разбор
from jarvis_api.jobs.finance_categorize import разобрать_книжку

logger = logging.getLogger("jarvis.finance_import")

# Роль нового счёта. Не заглушка, а рабочее состояние: имя счёта приезжает
# выпиской, роль знает только owner, и до разметки «Отложено» пишет
# «счета не размечены», а не ноль (§15.5).
РОЛЬ_ПО_УМОЛЧАНИЮ = "unknown"


class ОшибкаФайла(RuntimeError):
    """Импорт файла не выполнен по причине, не связанной с его разбором."""


@dataclass(slots=True)
class ОтчётФайла:
    """Что файл сделал бы или сделал. Печатается и в dry-run, и в --apply."""

    имя: str
    sha256: str
    банк: str | None = None
    период: tuple[dt.date | None, dt.date | None] = (None, None)
    строк_в_файле: int = 0
    уже_загружен: bool = False
    добавлено: int = 0
    обновлено: int = 0
    отменено: int = 0
    в_разбор: int = 0
    неизменных: int = 0
    новых_счетов: list[str] = field(default_factory=list)
    ошибка: str | None = None

    @property
    def упал(self) -> bool:
        return self.ошибка is not None


def _вид(amount: Decimal) -> str:
    """Роль операции в сальдо при заведении строки - по знаку суммы.

    Настоящее разделение делает разбор (Ф4а, `domain/finance_categorize.py`):
    `transfer` опознаётся парой концов, `refund` - правилом отправителя,
    и из одной строки файла ни то, ни другое не выводится. Колонка при этом
    `not null` с CHECK на четыре значения, «неизвестно» записать нельзя,
    и знак суммы - единственное, что честно известно про строку в момент
    заведения.

    Провизорность с Ф4а не подразумевается, а записана: `kind_source`
    остаётся `sign` (умолчание колонки), и разбор по этому признаку знает,
    что переписать эту строку он вправе, а решение owner - нет.
    """
    return "expense" if amount < 0 else "income"


def _наложить(операция: FinTransaction, строка: StatementRow, ключ: str) -> None:
    """Переносит в операцию то, что пришло из файла. И только это.

    Поля owner - категория, заметка, `excluded`, привязка гашения - не
    трогаются никогда: ручная правка обязана пережить переимпорт (§15.4).
    `kind` здесь тоже не трогается, потому что на Ф4 он станет решением
    правил, а не пересчётом из строки.
    """
    операция.occurred_at = строка.occurred_at
    операция.account = строка.account
    операция.card_last4 = строка.card_last4
    операция.amount = строка.amount
    операция.currency = строка.currency
    операция.amount_rub = строка.amount_rub
    операция.merchant = строка.merchant
    операция.bank_category = строка.bank_category
    операция.own_category = строка.own_category
    операция.mcc = строка.mcc
    операция.message = строка.message
    операция.analytics_hint = строка.analytics_hint
    операция.status = строка.status
    операция.fingerprint = ключ
    операция.source_row = dict(строка.source_row)


def _существующие(session: Session, выписка: ParsedStatement, зона: ZoneInfo) -> list[Существующая]:
    """Операции этого банка за период файла - то, с чем сверяется выгрузка.

    **За период файла, а не вся книжка.** Правило «пропала из выгрузки -
    значит отменена» действует только внутри периода, за который выгрузка
    сделана: файл за август ничего не говорит про сентябрь.

    **Кроме введённых руками.** Наличных и переводов с рук на руки
    ни в одной выписке не будет никогда, и пометить их отменёнными значило
    бы стирать из сальдо ровно то, ради чего существует ручной ввод (§15.1).
    """
    if выписка.period_start is None or выписка.period_end is None:
        return []
    начало, конец = границы_окна(выписка.period_start, выписка.period_end, зона)
    строки = session.scalars(
        select(FinTransaction).where(
            FinTransaction.bank == выписка.bank,
            FinTransaction.occurred_at >= начало,
            FinTransaction.occurred_at < конец,
            FinTransaction.entered_manually.is_(False),
        )
    ).all()
    return [
        Существующая(
            id=строка.id,
            fingerprint=строка.fingerprint,
            occurrence_no=строка.occurrence_no,
            status=строка.status,
            occurred_at=строка.occurred_at,
            card_last4=строка.card_last4,
            merchant=строка.merchant,
        )
        for строка in строки
    ]


def _новые_счета(session: Session, выписка: ParsedStatement) -> list[str]:
    """Имена счетов из файла, которых книжка ещё не знает. Только чтение."""
    известные = set(
        session.scalars(select(FinAccount.name).where(FinAccount.bank == выписка.bank)).all()
    )
    return sorted({строка.account for строка in выписка.rows} - известные)


def _завести_счета(session: Session, выписка: ParsedStatement) -> list[str]:
    """Новые имена счетов - в `fin_accounts` с ролью `unknown`.

    Обязательный шаг, а не удобство: операция ссылается на счёт составным
    внешним ключом `(bank, account)`, и без строки счёта импорт упал бы
    на ограничении базы. Роль угадывать нельзя - «Накопительный счет»
    в имени ничего не доказывает, а по этой роли считается «Отложено».
    """
    новые = _новые_счета(session, выписка)
    for имя in новые:
        session.add(FinAccount(bank=выписка.bank, name=имя, role=РОЛЬ_ПО_УМОЛЧАНИЮ))
    return новые


def _применить(session: Session, план: ПланИмпорта, банк: str, импорт: FinImport) -> None:
    """Записывает план. Транзакцию коммитит вызывающий код, а не эта функция."""
    for номер_id in план.отменённые:
        операция = session.get(FinTransaction, номер_id)
        if операция is not None:
            операция.status = "reverted"

    for номер_id in план.в_разбор:
        операция = session.get(FinTransaction, номер_id)
        if операция is not None:
            операция.needs_review = True

    for обновление in план.обновления:
        операция = session.get(FinTransaction, обновление.id)
        if операция is None:
            continue
        _наложить(операция, обновление.строка, обновление.fingerprint)
        # Импорт-источник меняется на тот, что принёс текущее значение:
        # вопрос «из какого файла эта сумма» должен иметь один ответ.
        операция.import_id = импорт.id

    for новая in план.новые:
        операция = FinTransaction(
            bank=банк,
            import_id=импорт.id,
            fingerprint=новая.fingerprint,
            occurrence_no=новая.occurrence_no,
            kind=_вид(новая.строка.amount),
            needs_review=новая.в_разбор,
        )
        _наложить(операция, новая.строка, новая.fingerprint)
        session.add(операция)


def импортировать(
    session: Session,
    имя: str,
    данные: bytes,
    зона: ZoneInfo,
    *,
    apply: bool,
    уже_в_заходе: set[str],
) -> ОтчётФайла:
    """Один файл. Отказ разбора выбрасывается наружу - его ловит `run_once`."""
    хэш = hashlib.sha256(данные).hexdigest()
    отчёт = ОтчётФайла(имя=имя, sha256=хэш)

    загружен = session.scalar(select(FinImport.id).where(FinImport.sha256 == хэш)) is not None
    # Второй файл того же захода с тем же содержимым: в dry-run строки
    # `fin_imports` ещё нет ни для одного из них, и без этой памяти дифф
    # показал бы одни и те же операции дважды.
    if загружен or хэш in уже_в_заходе:
        отчёт.уже_загружен = True
        return отчёт
    уже_в_заходе.add(хэш)

    выписка = parse_statement(данные, зона)
    отчёт.банк = выписка.bank
    отчёт.период = (выписка.period_start, выписка.period_end)
    отчёт.строк_в_файле = len(выписка.rows)

    план = спланировать(выписка.bank, _существующие(session, выписка, зона), выписка.rows, зона)
    отчёт.добавлено = len(план.новые)
    отчёт.обновлено = len(план.обновления)
    отчёт.отменено = len(план.отменённые)
    отчёт.в_разбор = len(план.в_разбор) + sum(1 for новая in план.новые if новая.в_разбор)
    отчёт.неизменных = план.неизменных

    if not apply:
        # В диффе счета названы, но не заведены: dry-run не пишет ни строки,
        # включая строки, которые «всё равно понадобятся».
        отчёт.новых_счетов = _новые_счета(session, выписка)
        return отчёт

    отчёт.новых_счетов = _завести_счета(session, выписка)
    импорт = FinImport(
        bank=выписка.bank,
        filename=имя,
        sha256=хэш,
        period_start=выписка.period_start,
        period_end=выписка.period_end,
        rows_added=отчёт.добавлено,
        # Все тронутые строки книжки: обновлённые, отменённые и ушедшие
        # в разбор. Пропущенные - те, что файл подтвердил без изменений.
        rows_updated=отчёт.обновлено + отчёт.отменено + len(план.в_разбор),
        rows_skipped=отчёт.неизменных,
    )
    session.add(импорт)
    session.flush()  # нужен id импорта: он проставляется каждой операции
    _применить(session, план, выписка.bank, импорт)
    return отчёт


def описать(отчёт: ОтчётФайла, apply: bool) -> list[str]:
    """Человекочитаемый дифф одного файла. Отдельной функцией - её проверяет тест."""
    if отчёт.упал:
        return [f"{отчёт.имя}: ОТКАЗ - {отчёт.ошибка}"]
    if отчёт.уже_загружен:
        return [f"{отчёт.имя}: уже загружен ({отчёт.sha256[:12]}), изменений нет"]

    начало, конец = отчёт.период
    период = (
        f"{начало.isoformat()} .. {конец.isoformat()}"
        if начало is not None and конец is not None
        else "операций нет"
    )
    строки = [
        f"{отчёт.имя}: банк {отчёт.банк}, период {период}, строк {отчёт.строк_в_файле}",
        f"  новых {отчёт.добавлено}, обновлённых {отчёт.обновлено}, "
        f"отменённых {отчёт.отменено}, без изменений {отчёт.неизменных}",
    ]
    if отчёт.в_разбор:
        строки.append(f"  в разбор {отчёт.в_разбор}: неоднозначные кандидаты, вне сальдо")
    if отчёт.новых_счетов:
        строки.append(
            f"  новых счетов {len(отчёт.новых_счетов)}: {', '.join(отчёт.новых_счетов)}"
            " - роль unknown, ждут разметки owner"
        )
    if not apply:
        строки.append("  dry-run: в базу не записано ничего")
    return строки


def run_once(
    session: Session,
    файлы: Sequence[tuple[str, bytes]],
    *,
    apply: bool,
) -> int:
    """Заход целиком поверх готовой сессии. Возвращает код возврата процесса.

    Транзакция на файл: успешный файл коммитится сразу, упавший
    откатывается - и следующий начинает с чистой сессии. Иначе отказ
    восьмого файла унёс бы семь уже разобранных.
    """
    try:
        зона = owner_timezone(session)
    except OwnerZoneError as сбой:
        logger.error("импорт не выполнен: %s", сбой)
        return 1

    уже_в_заходе: set[str] = set()
    отчёты: list[ОтчётФайла] = []
    for имя, данные in файлы:
        try:
            отчёт = импортировать(
                session, имя, данные, зона, apply=apply, уже_в_заходе=уже_в_заходе
            )
        except StatementError as сбой:
            session.rollback()
            отчёты.append(ОтчётФайла(имя=имя, sha256="", ошибка=str(сбой)))
            continue
        if apply:
            session.commit()
        else:
            # В dry-run в сессии осталось только чтение, но откат делается
            # явно: молчаливое доверие «тут не могло ничего записаться» -
            # ровно тот случай, когда однажды запишется.
            session.rollback()
        отчёты.append(отчёт)

    # Разбор - после всех файлов и один раз. Внутри цикла он считался бы
    # по половине захода: перевод себе опознаётся парой концов из двух
    # банков (ADR-041), и до загрузки второго файла пары ещё нет.
    # В dry-run не запускается вовсе: разбирать нечего - ни одной строки
    # в базу не легло, а разбор уже лежащего показал бы дифф, к этому
    # заходу не относящийся.
    if apply:
        try:
            отчёт_разбора, решения = разобрать_книжку(session, get_settings(), apply=True)
        except OwnerZoneError as сбой:
            session.rollback()
            logger.error("операции загружены, но не разобраны: %s", сбой)
            return 1
        session.commit()
        for строка in описать_разбор(отчёт_разбора, решения, apply=True):
            logger.info("%s", строка)

    for отчёт in отчёты:
        for строка in описать(отчёт, apply):
            (logger.error if отчёт.упал else logger.info)("%s", строка)

    упавших = sum(1 for отчёт in отчёты if отчёт.упал)
    if упавших:
        logger.error("файлов не разобрано: %d из %d", упавших, len(отчёты))
        return 1
    logger.info("импорт завершён%s", "" if apply else " (dry-run)")
    return 0


def прочитать(пути: Sequence[Path]) -> list[tuple[str, bytes]]:
    """Файлы байтами. Кодировка - часть формата банка, и её знает адаптер."""
    файлы: list[tuple[str, bytes]] = []
    for путь in пути:
        if not путь.is_file():
            raise ОшибкаФайла(f"файла нет: {путь}")
        файлы.append((путь.name, путь.read_bytes()))
    return файлы


def run(settings: Settings, пути: Sequence[Path], apply: bool) -> int:
    """Прогон целиком: своя сессия, свой код возврата."""
    try:
        файлы = прочитать(пути)
    except ОшибкаФайла as сбой:
        logger.error("импорт не выполнен: %s", сбой)
        return 1
    with get_sessionmaker()() as session:
        return run_once(session, файлы, apply=apply)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага в базу не пишется ничего."""
    parser = argparse.ArgumentParser(description="Импорт банковских выписок в книжку JARVIS")
    parser.add_argument(
        "--file",
        dest="files",
        action="append",
        required=True,
        type=Path,
        help="путь к файлу выписки (CSV или PDF); можно повторить - по файлу с каждого банка",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать операции (без флага - только показать дифф)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), args.files, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

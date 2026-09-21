"""Лента операций, карточка и давность данных книжки (Ф6, §15.7, §15.6).

Чтение книжки глазами экрана. Арифметику здесь не заводится ни одной новой:
эффективную сумму расхода считает `finance_offsets.разложить`, сальдо -
`finance_balance`, покрытие выписками - `finance_budget.покрыто_по`. Вторая
копия любого из этих правил разошлась бы с первой молча, а расходятся такие
копии в деньгах owner.

**Лента отдаёт месяц целиком, без страниц.** Операций у одного человека
десятки в месяц, а очередь разбора и вовсе штучная: страницы здесь были бы
абстракцией под несуществующее требование, и каждая из них требовала бы
стабильной сортировки поверх `occurred_at`, у которого дубли законны.

**Эффективная сумма считается только расходу.** У прихода гасить нечего,
и `null` в этом поле означает «неприменимо», а не «ноль»: ноль читался бы
как «всё погашено».
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.db.models import FinCategory, FinImport, FinTransaction
from jarvis_api.domain.finance_budget import покрыто_по
from jarvis_api.domain.finance_offsets import (
    Разбивка,
    идёт_в_счёт,
    разложить,
    строка_операции,
)

# Расход - единственный вид, которому гашение что-то уменьшает (§15.5).
РАСХОД = "expense"


@dataclass(frozen=True, slots=True)
class Строка:
    """Операция в ленте: поля книжки плюс то, что посчитано для показа."""

    id: int
    occurred_at: dt.datetime
    bank: str
    account: str
    amount: Decimal
    amount_rub: Decimal
    currency: str
    merchant: str | None
    message: str | None
    kind: str
    kind_source: str
    status: str
    excluded: bool
    needs_review: bool
    entered_manually: bool
    category_id: int | None
    category_key: str | None
    category_title: str | None
    category_source: str | None
    offsets_transaction_id: int | None
    transfer_pair_id: int | None
    # Сколько расход стоил на самом деле: своя сумма минус погашенное.
    # None у всего, что не расход, - гасить там нечего, и ноль соврал бы.
    эффективная: Decimal | None
    погашено: Decimal | None
    # Идёт ли операция в счёт сегодня: не исключена, не отменена, не стоит
    # в очереди разбора. Те же условия, что у сальдо и у траты дня.
    в_счёт: bool


@dataclass(frozen=True, slots=True)
class Карточка:
    """Операция со всем, что к ней привязано (§15.5, экран 13 дизайна)."""

    операция: Строка
    # Разбивка расхода: сколько погашено, что осталось, что стало доходом.
    # None у прихода - разбивать нечего.
    разбивка: Разбивка | None
    # Поступления, гасящие этот расход.
    гашения: list[Строка] = field(default_factory=list)
    # Расход, который гасит сама эта операция, если она поступление.
    гасит: Строка | None = None
    # Второй конец перевода себе (ADR-041).
    пара: Строка | None = None


@dataclass(frozen=True, slots=True)
class Давность:
    """Свежесть книжки: по какой день загружено и давно ли (§15.6).

    Считается по **самому старому** банку: книжка свежа настолько, насколько
    свежа её отстающая часть. Правило то же, что у `покрыто_по`, - второе
    завело бы вторую правду о том, что значит «загружено».
    """

    # Последний день, покрытый выписками всех банков. None - покрытия нет.
    покрыто_по: dt.date | None
    # Банк, по которому книжка отстаёт: его и называет плашка.
    отстающий_банк: str | None
    # Когда по этому банку загружали в последний раз.
    загружено_в: dt.datetime | None
    # Сколько дней назад это было, в зоне owner.
    дней_назад: int | None
    # Пора напоминать о выписке: срок из конфига вышел.
    просрочено: bool


def _границы(месяц_: dt.date, зона: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """Полуинтервал месяца в UTC, посчитанный по зоне owner (инвариант 7)."""
    начало = dt.datetime.combine(месяц_, dt.time.min, tzinfo=зона)
    следующий = (месяц_.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    конец = dt.datetime.combine(следующий, dt.time.min, tzinfo=зона)
    return начало.astimezone(dt.UTC), конец.astimezone(dt.UTC)


def _категории(session: Session, номера: set[int]) -> dict[int, FinCategory]:
    """Категории по id - одним запросом на всю ленту, а не по строке."""
    if not номера:
        return {}
    строки = session.scalars(select(FinCategory).where(FinCategory.id.in_(номера))).all()
    return {строка.id: строка for строка in строки}


def _собрать(
    операции: list[FinTransaction],
    гашения: dict[int, list[FinTransaction]],
    категории: dict[int, FinCategory],
) -> list[Строка]:
    """Строки ленты из операций базы. Эффективная сумма - только расходам."""
    собранные: list[Строка] = []
    for операция in операции:
        разбор = (
            разложить(
                строка_операции(операция),
                [строка_операции(г) for г in гашения.get(операция.id, [])],
            )
            if операция.kind == РАСХОД
            else None
        )
        категория = категории.get(операция.category_id) if операция.category_id else None
        собранные.append(
            Строка(
                id=операция.id,
                occurred_at=операция.occurred_at,
                bank=операция.bank,
                account=операция.account,
                amount=операция.amount,
                amount_rub=операция.amount_rub,
                currency=операция.currency,
                merchant=операция.merchant,
                message=операция.message,
                kind=операция.kind,
                kind_source=операция.kind_source,
                status=операция.status,
                excluded=операция.excluded,
                needs_review=операция.needs_review,
                entered_manually=операция.entered_manually,
                category_id=операция.category_id,
                category_key=категория.key if категория is not None else None,
                category_title=категория.title if категория is not None else None,
                category_source=операция.category_source,
                offsets_transaction_id=операция.offsets_transaction_id,
                transfer_pair_id=операция.transfer_pair_id,
                эффективная=разбор.эффективная if разбор is not None else None,
                погашено=разбор.погашено if разбор is not None else None,
                в_счёт=идёт_в_счёт(строка_операции(операция)),
            )
        )
    return собранные


def _гашения(session: Session, номера: list[int]) -> dict[int, list[FinTransaction]]:
    """Поступления, привязанные к этим расходам, - одним запросом."""
    if not номера:
        return {}
    строки = session.scalars(
        select(FinTransaction)
        .where(FinTransaction.offsets_transaction_id.in_(номера))
        .order_by(FinTransaction.occurred_at, FinTransaction.id)
    ).all()
    по_расходам: dict[int, list[FinTransaction]] = {}
    for строка in строки:
        # Ключ не None: условие запроса это гарантирует, а mypy - нет.
        ключ = строка.offsets_transaction_id
        if ключ is not None:
            по_расходам.setdefault(ключ, []).append(строка)
    return по_расходам


def лента(
    session: Session,
    *,
    месяц_: dt.date | None,
    только_разбор: bool,
    зона: ZoneInfo,
) -> list[Строка]:
    """Операции месяца или очередь разбора по всей книжке.

    Очередь разбора не ограничивается месяцем намеренно: она и есть список
    вопросов owner, и вопрос августа не перестаёт быть вопросом в сентябре.
    Месяц и очередь совмещаются: «что в этом месяце ещё не разобрано».
    """
    запрос = select(FinTransaction)
    if месяц_ is not None:
        начало, конец = _границы(месяц_, зона)
        запрос = запрос.where(
            FinTransaction.occurred_at >= начало,
            FinTransaction.occurred_at < конец,
        )
    if только_разбор:
        запрос = запрос.where(FinTransaction.needs_review.is_(True))

    # Свежая операция сверху: лента читается сверху вниз, а не листается
    # от начала времён. Вторым ключом id - у дат в выписке дубли законны,
    # и без него порядок строк одного дня менялся бы между запросами.
    операции = list(
        session.scalars(
            запрос.order_by(FinTransaction.occurred_at.desc(), FinTransaction.id.desc())
        ).all()
    )
    расходы = [о.id for о in операции if о.kind == РАСХОД]
    категории = _категории(session, {о.category_id for о in операции if о.category_id})
    return _собрать(операции, _гашения(session, расходы), категории)


def карточка(session: Session, tx_id: int) -> Карточка | None:
    """Операция со всем, что к ней привязано. None - операции нет.

    Отдаёт и разбивку расхода, и обратную ссылку поступления, и второй конец
    перевода: карточка отвечает на вопрос «почему эта строка выглядит так»,
    а ответ на него у всех трёх случаев разный.
    """
    операция = session.get(FinTransaction, tx_id)
    if операция is None:
        return None

    гашения = _гашения(session, [tx_id]).get(tx_id, []) if операция.kind == РАСХОД else []
    соседи = [
        сосед
        for сосед in (
            (
                session.get(FinTransaction, операция.offsets_transaction_id)
                if операция.offsets_transaction_id is not None
                else None
            ),
            (
                session.get(FinTransaction, операция.transfer_pair_id)
                if операция.transfer_pair_id is not None
                else None
            ),
        )
        if сосед is not None
    ]
    категории = _категории(
        session,
        {
            строка.category_id
            for строка in [операция, *гашения, *соседи]
            if строка.category_id is not None
        },
    )
    набор = [операция, *гашения, *соседи]
    # Гашения нужны каждому расходу набора, а не только главной строке:
    # у поступления в карточке виден расход, который оно гасит, и его
    # эффективная сумма обязана быть той же, что в ленте.
    собранные = _собрать(
        набор, _гашения(session, [с.id for с in набор if с.kind == РАСХОД]), категории
    )
    по_id = {строка.id: строка for строка in собранные}

    return Карточка(
        операция=по_id[операция.id],
        разбивка=(
            разложить(строка_операции(операция), [строка_операции(г) for г in гашения])
            if операция.kind == РАСХОД
            else None
        ),
        гашения=[по_id[г.id] for г in гашения],
        гасит=(
            по_id.get(операция.offsets_transaction_id)
            if операция.offsets_transaction_id is not None
            else None
        ),
        пара=(
            по_id.get(операция.transfer_pair_id) if операция.transfer_pair_id is not None else None
        ),
    )


def давность(
    session: Session,
    *,
    сейчас: dt.datetime,
    зона: ZoneInfo,
    порог_дней: int,
) -> Давность:
    """Свежесть книжки по самому старому банку (§15.6).

    Отстающим считается банк с самым ранним концом периода, а банк, чьи
    файлы периода не называют вовсе, - отстающим безусловно: покрытия он
    не даёт, и считать его свежим значило бы угадывать (то же правило,
    что в `покрыто_по`).

    Пустая книжка не просрочена: напоминать не о чем, пока не было ни одной
    выписки, - завести книжку это отдельное действие owner, а не отставание.
    """
    строки = session.execute(
        select(
            FinImport.bank,
            func.max(FinImport.period_end),
            func.max(FinImport.imported_at),
        ).group_by(FinImport.bank)
    ).all()
    if not строки:
        return Давность(None, None, None, None, просрочено=False)

    # Сначала банки без периода, затем по возрастанию конца периода.
    банк, _конец_периода, загружено = min(
        строки,
        key=lambda строка: (строка[1] is not None, строка[1] or dt.date.min),
    )
    # Покрытие спрашивается у бюджета, а не считается здесь заново: правило
    # «по самому старому банку, банк без периода не покрывает» уже записано
    # там, и вторая его копия разошлась бы с первой на первом же банке,
    # чьи файлы периода не называют.
    покрыто = покрыто_по(session)
    дней = (сейчас.astimezone(зона).date() - загружено.astimezone(зона).date()).days
    return Давность(
        покрыто_по=покрыто,
        отстающий_банк=банк,
        загружено_в=загружено,
        дней_назад=дней,
        просрочено=дней > порог_дней,
    )

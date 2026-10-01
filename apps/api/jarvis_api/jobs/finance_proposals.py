"""Переход книжки на новый месяц: наследование набора и предложение модели (Ф10).

§15.4: «Начало месяца: набор основных категорий наследуется от прошлого
месяца, модель отдельно кладёт предложения - что добавить и что убрать.
Owner правит список и утверждает; до утверждения работает унаследованный
набор, книжка не ждёт». Решения owner от 2026-10-01 записаны ADR-056.

**Переход случается в импорте, а не по расписанию.** Месяц, в котором есть
операции, а набора нет, получает набор ближайшего прошлого месяца - иначе
разбор Ф9 пропустил бы его как «месяц без набора». Без выписки месяцу
разбирать нечего, и набор ему не нужен. Импорт уже зовёт модель на
категоризацию, и второй вызов рядом не требует ни планировщика, ни догона
после перезагрузки (инвариант 11).

**Предложение - на месяц, который идёт.** Набор прошлого месяца, открытого
поздней выпиской, наследуется без вопроса: его траты уже случились,
и предлагать для них набор значит переписывать прошлое.

**Модель зовётся один раз на месяц.** Отметка - `settings.finance_proposed_month`,
и ставится она только на ответ, прошедший схему: отказ модели оставляет
месяц неспрошенным, и следующий импорт спросит снова. Деградация, а не
провал (§15.6): унаследованный набор работает и без предложения.

**Решает owner, по пункту.** Командой здесь и ручкой
`POST /api/finance/categories/{id}/decision` - один домен
(`domain/finance_proposals.py`), как у прочих действий книжки.
"""

import argparse
import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import FinCategory, FinTransaction, Setting
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain.finance_categorize import РАСХОД
from jarvis_api.domain.finance_proposals import (
    МесяцИстории,
    Основная,
    ОшибкаРешения,
    ПредложениеНабора,
    Трата,
    ждут_решения,
    описать_историю,
    описать_набор,
    прочитать_предложение,
    решить,
    свести_историю,
)
from jarvis_api.integrations import llm
from jarvis_api.jobs.common import FALLBACK_TIMEZONE, OwnerZoneError, owner_timezone
from jarvis_api.jobs.finance_categorize import причина_отказа
from jarvis_api.jobs.finance_taxonomy import унаследовать

logger = logging.getLogger("jarvis.finance_proposals")

АКТОР = "job.finance_proposals"

# Статус отменённой банком операции (§15.3): в сальдо её нет, в историю тоже.
ОТМЕНЕНА = "reverted"


@dataclass(slots=True)
class ОтчётПредложения:
    """Что ступень предложения сделала или почему не стала."""

    месяц: dt.date | None = None
    запущено: bool = False
    добавить: int = 0
    убрать: int = 0
    отклонено: list[str] = field(default_factory=list)
    # Почему модель не звали - это не отказ, а рабочее состояние.
    пропуск: str | None = None
    # Почему модель не дала ответа - словами, без имён моделей.
    отказ: str | None = None


@dataclass(slots=True)
class ОтчётПерехода:
    """Переход месяца целиком: какие месяцы открыты и что предложено."""

    # Месяц, откуда унаследован, сколько категорий перенесено.
    открыты: list[tuple[dt.date, dt.date, int]] = field(default_factory=list)
    предложение: ОтчётПредложения = field(default_factory=ОтчётПредложения)


def _первое(момент: dt.datetime, зона: ZoneInfo) -> dt.date:
    return момент.astimezone(зона).date().replace(day=1)


def _назад(месяц: dt.date, сколько: int) -> dt.date:
    номер = месяц.year * 12 + (месяц.month - 1) - сколько
    return dt.date(номер // 12, номер % 12 + 1, 1)


def _начало(месяц: dt.date, зона: ZoneInfo) -> dt.datetime:
    """Полночь первого числа в зоне owner - граница месяца (§15.5)."""
    return dt.datetime.combine(месяц, dt.time(0), tzinfo=зона)


def открыть_месяцы(session: Session, зона: ZoneInfo) -> list[tuple[dt.date, dt.date, int]]:
    """Месяцы с операциями и без набора получают набор ближайшего прошлого.

    По порядку, от раннего к позднему: выписка за два месяца сразу открывает
    оба, и второй наследует от первого, а не через его голову. Месяц раньше
    первого набора не открывается: наследовать ему не от кого, и набор
    приезжает файлом (`make finance-taxonomy`).

    Пишет всегда: зовётся только из записи импорта.
    """
    с_набором = set(session.scalars(select(FinCategory.period_month).distinct()).all())
    моменты = session.scalars(select(FinTransaction.occurred_at)).all()
    с_операциями = {_первое(момент, зона) for момент in моменты}

    открыты: list[tuple[dt.date, dt.date, int]] = []
    for месяц in sorted(с_операциями - с_набором):
        прошлые = [м for м in с_набором if м < месяц]
        if not прошлые:
            continue
        откуда = max(прошлые)
        отчёт = унаследовать(session, откуда, месяц, apply=True)
        if отчёт.унаследовано:
            с_набором.add(месяц)
            открыты.append((месяц, откуда, отчёт.унаследовано))
    session.flush()
    return открыты


def _основные(session: Session, месяц: dt.date) -> list[FinCategory]:
    return list(
        session.scalars(
            select(FinCategory)
            .where(
                FinCategory.period_month == месяц,
                FinCategory.level == 1,
                FinCategory.status == "active",
            )
            .order_by(FinCategory.key)
        ).all()
    )


def история(
    session: Session, месяц: dt.date, зона: ZoneInfo, *, месяцев: int, мерчантов: int
) -> list[МесяцИстории]:
    """Расходы прошлых месяцев, сведённые к основным категориям.

    Те же условия «идёт в счёт», что у сальдо: не отменённый, не исключённый,
    не в очереди разбора (§15.5). Иначе модель предложила бы категорию под
    траты, которых в книжке owner нет.

    Суммы - без гашений: вопрос здесь «на что уходят деньги», а не «сколько
    осталось», и доли друзей за общий стол не делают стол не едой.
    """
    начало = _назад(месяц, месяцев)
    строки = session.scalars(
        select(FinTransaction).where(
            FinTransaction.kind == РАСХОД,
            FinTransaction.excluded.is_(False),
            FinTransaction.status != ОТМЕНЕНА,
            FinTransaction.needs_review.is_(False),
            FinTransaction.occurred_at >= _начало(начало, зона),
            FinTransaction.occurred_at < _начало(месяц, зона),
        )
    ).all()

    категории = {
        строка.id: строка
        for строка in session.scalars(
            select(FinCategory).where(
                FinCategory.period_month >= начало, FinCategory.period_month < месяц
            )
        ).all()
    }

    def основной_ключ(category_id: int | None) -> str | None:
        строка = категории.get(category_id) if category_id is not None else None
        if строка is None:
            return None
        if строка.parent_id is not None:
            родитель = категории.get(строка.parent_id)
            return родитель.key if родитель is not None else None
        return строка.key

    траты = [
        Трата(
            month=_первое(строка.occurred_at, зона),
            key=основной_ключ(строка.category_id),
            # Расход в книжке со знаком минус; модели - сумма траты.
            amount=-строка.amount,
            merchant=строка.merchant,
        )
        for строка in строки
    ]
    наборы: dict[dt.date, list[Основная]] = {}
    for строка in категории.values():
        if строка.level == 1 and строка.status == "active":
            наборы.setdefault(строка.period_month, []).append(
                Основная(key=строка.key, title=строка.title, origin=строка.origin)
            )
    return свести_историю(траты, наборы, мерчантов=мерчантов)


def _настройки_owner(session: Session) -> Setting:
    строка = session.get(Setting, 1)
    if строка is None:
        строка = Setting(id=1, timezone=FALLBACK_TIMEZONE)
        session.add(строка)
        session.flush()
    return строка


def предложить(
    session: Session,
    settings: Settings,
    *,
    адаптеры: Mapping[str, llm.Адаптер] | None,
    сейчас: dt.datetime,
    зона: ZoneInfo,
) -> ОтчётПредложения:
    """Предложение набора на текущий месяц. Пишет: зовётся из записи импорта.

    Модель не зовётся в четырёх случаях, и каждый - рабочее состояние:
    провайдеров не передали (так импорт гоняют тесты), месяц уже спрошен,
    у месяца нет набора owner (модель дополняет набор, а не строит его -
    ADR-055) и нет ни одного прошлого месяца с тратами.
    """
    месяц = _первое(сейчас, зона)
    отчёт = ОтчётПредложения(месяц=месяц)
    if адаптеры is None:
        отчёт.пропуск = "модель не звали"
        return отчёт
    настройки_owner = session.get(Setting, 1)
    if настройки_owner is not None and настройки_owner.finance_proposed_month == месяц:
        отчёт.пропуск = "уже предложено"
        return отчёт

    строки = _основные(session, месяц)
    if not строки:
        # Чаще всего это первые дни месяца: выписка принесла только прошлый,
        # и набор этого месяца откроется с первой его операцией.
        отчёт.пропуск = "набор месяца ещё не открыт - откроется с первой его выпиской"
        return отчёт
    if not any(строка.origin == "owner" for строка in строки):
        отчёт.пропуск = "у месяца нет набора owner"
        return отчёт
    прошлое = история(
        session,
        месяц,
        зона,
        месяцев=settings.finance_proposal_history_months,
        мерчантов=settings.finance_proposal_merchants,
    )
    if not прошлое:
        отчёт.пропуск = "прошлых месяцев с тратами нет - предлагать не из чего"
        return отчёт

    основные = [Основная(key=с.key, title=с.title, origin=с.origin) for с in строки]
    отчёт.запущено = True
    try:
        маршруты = llm.загрузить_маршруты(settings.llm_routing)
        результат = llm.вызвать(
            session,
            задача=llm.Задача.КНИЖКА_НАБОР_КАТЕГОРИЙ,
            промпт=llm.собрать(
                "finance_categories",
                month=месяц.strftime("%Y-%m"),
                categories=описать_набор(основные),
                history=описать_историю(прошлое),
            ),
            схема=ПредложениеНабора,
            актор=АКТОР,
            сейчас=сейчас,
            зона=зона,
            настройки=settings,
            маршруты=маршруты,
            адаптеры=адаптеры,
        )
    except (llm.ОшибкаСлоя, llm.ОтказПровайдера, llm.МаршрутИспорчен) as сбой:
        logger.warning("предложение набора не получено: %s", сбой)
        отчёт.отказ = причина_отказа(сбой, для="набора категорий")
        return отчёт

    занятые = session.scalars(
        select(FinCategory.key).where(FinCategory.period_month == месяц)
    ).all()
    итог = прочитать_предложение(результат.значение, основные, занятые, прошлое)
    по_ключу = {строка.key: строка for строка in строки}
    for добавить in итог.добавить:
        session.add(
            FinCategory(
                key=добавить.key,
                period_month=месяц,
                level=1,
                title=добавить.title,
                origin="ai",
                status="proposed",
                proposal_reason=добавить.reason,
            )
        )
    for убрать in итог.убрать:
        строка = по_ключу[убрать.key]
        строка.remove_proposed = True
        строка.proposal_reason = убрать.reason
    _настройки_owner(session).finance_proposed_month = месяц
    session.flush()

    отчёт.добавить = len(итог.добавить)
    отчёт.убрать = len(итог.убрать)
    отчёт.отклонено = итог.отклонено
    return отчёт


def перейти(
    session: Session,
    settings: Settings,
    *,
    адаптеры: Mapping[str, llm.Адаптер] | None,
    сейчас: dt.datetime,
) -> ОтчётПерехода:
    """Переход месяца в записи импорта: до разбора, чтобы разбору было
    по какому набору раскладывать. Транзакцию коммитит вызывающий."""
    зона = owner_timezone(session)
    отчёт = ОтчётПерехода(открыты=открыть_месяцы(session, зона))
    отчёт.предложение = предложить(session, settings, адаптеры=адаптеры, сейчас=сейчас, зона=зона)
    return отчёт


def описать(отчёт: ОтчётПерехода) -> list[str]:
    """Строки диффа перехода. Пусто, если месяц не менялся и вопроса не было."""
    строки = [
        f"набор на {месяц:%Y-%m} унаследован из {откуда:%Y-%m}: категорий {сколько}"
        for месяц, откуда, сколько in отчёт.открыты
    ]
    п = отчёт.предложение
    if п.месяц is None:
        return строки
    if п.отказ is not None:
        строки.append(
            f"предложение набора на {п.месяц:%Y-%m}: {п.отказ} - работает унаследованный набор,"
            " следующий импорт спросит снова"
        )
    elif п.запущено:
        строки.append(
            f"предложение набора на {п.месяц:%Y-%m}: добавить {п.добавить}, убрать {п.убрать}"
            + (" - ждут решения owner: make finance-proposals" if п.добавить or п.убрать else "")
        )
        строки.extend(f"  отброшено: {причина}" for причина in п.отклонено)
    elif п.пропуск not in (None, "уже предложено", "модель не звали"):
        # «Уже предложено» печатать незачем: это каждый импорт месяца после
        # первого. «Не звали» - импорт без провайдеров, то есть тест.
        строки.append(f"предложение набора на {п.месяц:%Y-%m} не спрошено: {п.пропуск}")
    return строки


# --- команда ------------------------------------------------------------------


def _месяц_аргумента(значение: str) -> dt.date:
    try:
        год, номер = значение.split("-")
        return dt.date(int(год), int(номер), 1)
    except ValueError as ошибка:
        raise argparse.ArgumentTypeError(f"месяц {значение!r} не в формате ГГГГ-ММ") from ошибка


def _строка(категория: FinCategory) -> str:
    действие = "добавить" if категория.status == "proposed" else "убрать"
    return (
        f"  [{категория.id}] {действие} {категория.key}: {категория.title}"
        f" - {категория.proposal_reason or 'без причины'}"
    )


def run_once(
    session: Session,
    *,
    месяц: dt.date | None,
    принять: int | None,
    отклонить: int | None,
    apply: bool,
) -> int:
    """Показ предложений или решение по одному. Возвращает код возврата."""
    try:
        зона = owner_timezone(session)
    except OwnerZoneError as сбой:
        logger.error("предложения не показаны: %s", сбой)
        return 1

    номер = принять if принять is not None else отклонить
    if номер is None:
        месяц_ = месяц or _первое(dt.datetime.now(dt.UTC), зона)
        ждут = ждут_решения(session, месяц_)
        logger.info("предложения набора на %s: %d", f"{месяц_:%Y-%m}", len(ждут))
        for категория in ждут:
            logger.info("%s", _строка(категория))
        if ждут:
            logger.info("  решение: make finance-proposals-apply accept=<id> или reject=<id>")
        session.rollback()
        return 0

    try:
        решение = решить(session, номер, принять=принять is not None)
    except ОшибкаРешения as сбой:
        session.rollback()
        logger.error("решение не записано: %s", сбой)
        return 1

    к = решение.категория
    logger.info(
        "%s %s за %s: статус %s",
        "принято" if принять is not None else "отклонено",
        к.key,
        f"{к.period_month:%Y-%m}",
        к.status,
    )
    if решение.снято or решение.оставлено_ручных:
        logger.info(
            "  категория снята с операций: %d, ручных правок оставлено: %d",
            решение.снято,
            решение.оставлено_ручных,
        )
        logger.info("  разложить их заново: make finance-categorize-apply")
    if apply:
        session.commit()
    else:
        session.rollback()
        logger.info("  dry-run: в базу не записано ничего")
    return 0


def run(
    *,
    месяц: dt.date | None,
    принять: int | None,
    отклонить: int | None,
    apply: bool,
) -> int:
    with get_sessionmaker()() as session:
        return run_once(session, месяц=месяц, принять=принять, отклонить=отклонить, apply=apply)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага в базу не пишется ничего."""
    parser = argparse.ArgumentParser(
        description="Предложения модели по набору категорий месяца: показ и решение owner"
    )
    parser.add_argument(
        "--month", type=_месяц_аргумента, help="месяц ГГГГ-ММ, по умолчанию текущий"
    )
    решение = parser.add_mutually_exclusive_group()
    решение.add_argument("--accept", type=int, help="принять предложение с этим id")
    решение.add_argument("--reject", type=int, help="отклонить предложение с этим id")
    parser.add_argument(
        "--apply", action="store_true", help="записать решение (без флага - только показать)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(месяц=args.month, принять=args.accept, отклонить=args.reject, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

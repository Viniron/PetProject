"""Резюме о расходах в конце импорта (Ф11, §15.9, ADR-058).

Зовётся записью импорта - командой и `POST /api/finance/import`, - после
разбора: резюме сравнивает траты по категориям, и считать его до разбора
значило бы сравнивать неразобранное. По расписанию не считается (ADR-030
п. 5): в воскресенье выписка ещё не загружена, и резюме описывало бы
прошлую неделю, выглядя как отчёт о текущей.

**Отказ модели - деградация, а не провал** (§15.6). Модель не назначена,
недоступна, потолок исчерпан, ответ не прошёл схему или проверку кода -
строки нет, причина названа словами, код возврата импорта прежний. Резюме
не теряется: период считается от прошлого *резюме*, и следующий импорт
охватит пропущенное разом.

**Модель зовётся только при записи** (ADR-055 п. 5): dry-run, который
тратит деньги, перестаёт быть безопасным повтором.
"""

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import FinCategory, FinImport, FinSummary, FinTransaction
from jarvis_api.domain.finance_budget import СТАТУСЫ_В_СЧЁТ, покрыто_по
from jarvis_api.domain.finance_categorize import РАСХОД
from jarvis_api.domain.finance_feed import давность
from jarvis_api.domain.finance_offsets import разложить, строка_операции
from jarvis_api.domain.finance_summary import (
    ДНЕЙ_В_НЕДЕЛЕ,
    Основа,
    ОтказРезюме,
    Период,
    РезюмеМодели,
    Трата,
    границы,
    день_окончания,
    окно_истории,
    описать_основу,
    определить_период,
    прочитать_ответ,
    свести,
)
from jarvis_api.integrations import llm
from jarvis_api.jobs.finance_categorize import причина_отказа

logger = logging.getLogger("jarvis.finance_summary")

АКТОР = "job.finance_summary"


@dataclass(slots=True)
class ОтчётРезюме:
    """Что ступень резюме сделала или почему не стала."""

    период: Период | None = None
    # Записанное резюме. Пусто - смотри `пропуск` и `отказ`.
    строка: FinSummary | None = None
    # Почему модель не звали - рабочее состояние, а не отказ.
    пропуск: str | None = None
    # Почему резюме не получено - словами, без имён моделей.
    отказ: str | None = None


def _прошлое_по(session: Session, зона: ZoneInfo) -> dt.date | None:
    конец = session.scalar(select(func.max(FinSummary.period_end)))
    return день_окончания(конец, зона) if конец is not None else None


def траты(session: Session, окно: Период, зона: ZoneInfo) -> list[Трата]:
    """Расходы книжки за окно, по ключу основной категории и за вычетом гашений.

    Условия «идёт в счёт» - те же, что у сальдо и траты дня (§15.5, §15.10):
    не исключённый, не в очереди разбора, не отменённый. Гашение - тем же
    `разложить`, что у сальдо: резюме и обзор месяца обязаны показывать
    одну стоимость покупки.
    """
    начало, конец = границы(окно, зона)
    расходы = session.scalars(
        select(FinTransaction).where(
            FinTransaction.kind == РАСХОД,
            FinTransaction.excluded.is_(False),
            FinTransaction.needs_review.is_(False),
            FinTransaction.status.in_(СТАТУСЫ_В_СЧЁТ),
            FinTransaction.occurred_at >= начало,
            FinTransaction.occurred_at < конец,
        )
    ).all()
    if not расходы:
        return []

    гашения: dict[int, list[FinTransaction]] = {}
    for гашение in session.scalars(
        select(FinTransaction).where(
            FinTransaction.offsets_transaction_id.in_([р.id for р in расходы])
        )
    ).all():
        assert гашение.offsets_transaction_id is not None
        гашения.setdefault(гашение.offsets_transaction_id, []).append(гашение)

    номера = {р.category_id for р in расходы if р.category_id is not None}
    категории = {
        к.id: к for к in session.scalars(select(FinCategory).where(FinCategory.id.in_(номера)))
    }
    родители = {к.parent_id for к in категории.values() if к.parent_id is not None}
    for родитель in session.scalars(select(FinCategory).where(FinCategory.id.in_(родители))):
        категории[родитель.id] = родитель

    def основная(category_id: int | None) -> FinCategory | None:
        строка = категории.get(category_id) if category_id is not None else None
        if строка is not None and строка.parent_id is not None:
            # Подкатегория - в родителя: сравнение идёт по ключу основной
            # (§15.4), а подкатегории каждый месяц новые.
            return категории.get(строка.parent_id)
        return строка

    итог: list[Трата] = []
    for расход in расходы:
        к = основная(расход.category_id)
        итог.append(
            Трата(
                day=расход.occurred_at.astimezone(зона).date(),
                key=к.key if к is not None else None,
                title=к.title if к is not None else None,
                amount=разложить(
                    строка_операции(расход),
                    [строка_операции(г) for г in гашения.get(расход.id, [])],
                ).эффективная,
            )
        )
    return итог


def собрать_основу(
    session: Session,
    settings: Settings,
    *,
    импорты: Sequence[int],
    сейчас: dt.datetime,
    зона: ZoneInfo,
) -> Основа | str:
    """Основа резюме или причина, по которой его нет. Только чтение."""
    начало_захода = session.scalar(
        select(func.min(FinImport.period_start)).where(FinImport.id.in_(импорты))
    )
    период = определить_период(
        покрыто_по=покрыто_по(session),
        прошлое_по=_прошлое_по(session, зона),
        начало_захода=начало_захода,
    )
    if isinstance(период, str):
        отстающий = давность(
            session, сейчас=сейчас, зона=зона, порог_дней=settings.finance_stale_after_days
        ).отстающий_банк
        return f"{период} (отстаёт {отстающий})" if отстающий else период

    история = окно_истории(
        период,
        ведётся_с=session.scalar(select(func.min(FinImport.period_start))),
        недель=settings.finance_summary_history_weeks,
    )
    окно = Период(с=история.с if история is not None else период.с, по=период.по)
    return свести(
        траты(session, окно, зона),
        период,
        история,
        минимум_недель=settings.finance_summary_min_history_weeks,
    )


def подвести(
    session: Session,
    settings: Settings,
    *,
    импорты: Sequence[int],
    адаптеры: Mapping[str, llm.Адаптер] | None,
    сейчас: dt.datetime,
    зона: ZoneInfo,
) -> ОтчётРезюме:
    """Резюме захода. Пишет строку `fin_summaries`; транзакцию коммитит вызывающий.

    `импорты` - строки `fin_imports`, записанные этим заходом. Резюме
    одно на заход и достаётся последней из них: файлов в заходе несколько,
    по одному с банка, а период у резюме один.
    """
    отчёт = ОтчётРезюме()
    if not импорты:
        отчёт.пропуск = "новых выписок нет"
        return отчёт
    if адаптеры is None:
        отчёт.пропуск = "модель не звали"
        return отчёт

    основа = собрать_основу(session, settings, импорты=импорты, сейчас=сейчас, зона=зона)
    if isinstance(основа, str):
        отчёт.пропуск = основа
        return отчёт
    отчёт.период = основа.период

    if основа.история is None:
        история = "нет"
    else:
        история = (
            f"{основа.история.с:%d.%m.%Y} — {основа.история.по:%d.%m.%Y},"
            f" дней: {основа.история.дней}"
        )
        if not основа.достаточно:
            история += f" - меньше {основа.минимум_дней // ДНЕЙ_В_НЕДЕЛЕ} недель, сравнения нет"

    try:
        маршруты = llm.загрузить_маршруты(settings.llm_routing)
        результат = llm.вызвать(
            session,
            задача=llm.Задача.КНИЖКА_РЕЗЮМЕ,
            промпт=llm.собрать(
                "finance_summary",
                period_from=f"{основа.период.с:%d.%m.%Y}",
                period_to=f"{основа.период.по:%d.%m.%Y}",
                period_days=str(основа.период.дней),
                history=история,
                categories=описать_основу(основа),
            ),
            схема=РезюмеМодели,
            актор=АКТОР,
            сейчас=сейчас,
            зона=зона,
            настройки=settings,
            маршруты=маршруты,
            адаптеры=адаптеры,
        )
    except (llm.ОшибкаСлоя, llm.ОтказПровайдера, llm.МаршрутИспорчен) as сбой:
        logger.warning("резюме не получено: %s", сбой)
        отчёт.отказ = причина_отказа(сбой, для="резюме")
        return отчёт

    try:
        текст = прочитать_ответ(результат.значение, основа)
    except ОтказРезюме as сбой:
        # Вызов уже в аудите (инвариант 8) - стоимость отброшенного ответа
        # видна там, а в книжку не попадает ни строки.
        отчёт.отказ = f"ответ модели отброшен: {сбой}"
        return отчёт

    начало, конец = границы(основа.период, зона)
    отчёт.строка = FinSummary(
        import_id=max(импорты),
        period_start=начало,
        period_end=конец,
        text_ru=текст,
        basis=основа.как_json(),
        provider=результат.провайдер,
        model=результат.модель,
        tokens_in=результат.токенов_вход,
        tokens_out=результат.токенов_выход,
        cost_usd=результат.стоимость,
    )
    session.add(отчёт.строка)
    session.flush()
    return отчёт


def описать(отчёт: ОтчётРезюме) -> list[str]:
    """Строки диффа резюме. Пусто, если модель не звали (так импорт гоняют тесты)."""
    if отчёт.строка is not None and отчёт.период is not None:
        return [
            f"резюме за {отчёт.период.с:%d.%m} - {отчёт.период.по:%d.%m}:",
            f"  {отчёт.строка.text_ru}",
        ]
    if отчёт.отказ is not None:
        return [f"резюме не получено: {отчёт.отказ} - следующий импорт охватит и этот период"]
    if отчёт.пропуск not in (None, "модель не звали"):
        return [f"резюме не считалось: {отчёт.пропуск}"]
    return []


def последнее(session: Session) -> FinSummary | None:
    """Последнее резюме книжки - для обзора (§15.7). Не обязательно этого месяца."""
    return session.scalar(
        select(FinSummary).order_by(FinSummary.period_end.desc(), FinSummary.id.desc()).limit(1)
    )

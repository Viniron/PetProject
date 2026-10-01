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

**Модель - ступень 5, и она зовётся только при записи** (Ф9, ADR-055).
Dry-run, который тратит деньги, перестал бы быть безопасным повтором;
поэтому без `--apply` печатается лишь, сколько операций ушло бы модели.
Модель недоступна, не назначена или упёрлась в потолок - разбор ступенями
1-4 всё равно записан, а операции остаются без категории (§15.6): отказ
модели - деградация, а не провал разбора. Ответ модели проверяет
`domain/finance_categorize_model.py`; здесь - только вызов и запись.
"""

import argparse
import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.models import FinAccount, FinCategory, FinCategoryRule, FinTransaction
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain.finance_categorize import (
    ПО_МОДЕЛИ,
    РАСХОД,
    Категория,
    Операция,
    Правило,
    Разбор,
    Счёт,
    разобрать,
)
from jarvis_api.domain.finance_categorize_model import (
    Кандидат,
    КатегорияНабора,
    РазборОпераций,
    ключ_мерчанта,
    описать_набор,
    описать_операции,
    повторы,
    прочитать_ответ,
)
from jarvis_api.integrations import llm
from jarvis_api.jobs.common import OwnerZoneError, owner_timezone

logger = logging.getLogger("jarvis.finance_categorize")

АКТОР = "job.finance_categorize"

# Статус отменённой банком операции (§15.3). Модели она не отправляется:
# в сальдо её нет, и платить за её категорию незачем.
ОТМЕНЕНА = "reverted"


@dataclass(slots=True)
class ОтчётМодели:
    """Ступень 5: что ушло бы модели или ушло, и что из этого вышло."""

    # Расходы без категории после ступеней 1-4 - то, что ступень 5 берёт.
    ждут: int = 0
    # Из них в месяцах без основных категорий owner: модели их не отдаём.
    без_набора: int = 0
    # Категория мерчанта, однажды разобранного моделью, - без нового вызова.
    повтором: int = 0
    моделью: int = 0
    новых_категорий: int = 0
    отклонено: dict[str, int] = field(default_factory=dict)
    # Почему модель не дала ответа - словами для owner, без имён моделей.
    отказ: str | None = None
    запущена: bool = False

    @property
    def назначено(self) -> int:
        return self.повтором + self.моделью


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
    модель: ОтчётМодели = field(default_factory=ОтчётМодели)


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


def _кандидаты(
    session: Session, решения: Sequence[Разбор], зона: ZoneInfo
) -> dict[dt.date, list[Кандидат]]:
    """Расходы, которые ступени 1-4 оставили без категории, - по месяцам.

    Состояние берётся **после** разбора правилами: в dry-run решения ещё
    не записаны и накладываются поверх строк. Иначе счётчик «ушло бы
    модели» включал бы операции, которые правило вот-вот разберёт.

    По месяцам - потому что набор категорий свой у каждого месяца (§15.4),
    и операция 31 августа не вправе получить категорию сентября. Месяц -
    в зоне owner (инвариант 7).
    """
    новые = {решение.id: решение for решение in решения}
    строки = session.scalars(
        select(FinTransaction)
        .where(FinTransaction.excluded.is_(False), FinTransaction.status != ОТМЕНЕНА)
        .order_by(FinTransaction.occurred_at, FinTransaction.id)
    ).all()

    по_месяцам: dict[dt.date, list[Кандидат]] = {}
    for строка in строки:
        решение = новые.get(строка.id)
        kind = решение.kind if решение else строка.kind
        category_id = решение.category_id if решение else строка.category_id
        category_source = решение.category_source if решение else строка.category_source
        needs_review = решение.needs_review if решение else строка.needs_review
        # `category_source` проверяется отдельно от категории: `manual` при
        # пустой категории - решение owner «категории здесь нет» (§15.4),
        # и модель не вправе его переиграть.
        if kind != РАСХОД or category_id is not None or category_source is not None:
            continue
        if needs_review:
            continue
        месяц = строка.occurred_at.astimezone(зона).date().replace(day=1)
        по_месяцам.setdefault(месяц, []).append(
            Кандидат(
                id=строка.id,
                occurred_at=строка.occurred_at,
                amount=строка.amount,
                merchant=строка.merchant,
                bank_category=строка.bank_category,
                mcc=строка.mcc,
            )
        )
    return по_месяцам


def _набор_месяца(session: Session, месяц: dt.date) -> dict[str, FinCategory]:
    """Активные категории месяца по ключу.

    Подкатегория без активной основной в набор не входит: назначить её
    значило бы спрятать трату в ветку, которой owner в этом месяце не видит.
    """
    строки = session.scalars(
        select(FinCategory)
        .where(FinCategory.period_month == месяц, FinCategory.status == "active")
        .order_by(FinCategory.id)
    ).all()
    основные = {строка.id for строка in строки if строка.parent_id is None}
    return {
        строка.key: строка
        for строка in строки
        if строка.parent_id is None or строка.parent_id in основные
    }


def _для_модели(строки: Mapping[str, FinCategory]) -> list[КатегорияНабора]:
    по_id = {строка.id: строка for строка in строки.values()}
    return [
        КатегорияНабора(
            key=строка.key,
            title=строка.title,
            parent_key=по_id[строка.parent_id].key if строка.parent_id is not None else None,
        )
        for строка in строки.values()
    ]


def _прежние(session: Session) -> dict[str, str]:
    """Мерчант -> ключ категории, которую ему дала модель. Последняя выигрывает.

    Последняя, а не первая: если owner поправил набор и модель в новом
    месяце разложила мерчанта иначе, верить надо свежему ответу.
    """
    строки = session.execute(
        select(FinTransaction.merchant, FinCategory.key)
        .join(FinCategory, FinCategory.id == FinTransaction.category_id)
        .where(FinTransaction.category_source == ПО_МОДЕЛИ)
        .order_by(FinTransaction.occurred_at, FinTransaction.id)
    ).all()
    прежние: dict[str, str] = {}
    for мерчант, ключ in строки:
        имя = ключ_мерчанта(мерчант)
        if имя is not None:
            прежние[имя] = ключ
    return прежние


def _назначить(
    session: Session, назначения: Mapping[int, str], набор: Mapping[str, FinCategory]
) -> None:
    for номер, ключ in назначения.items():
        операция = session.get(FinTransaction, номер)
        if операция is None:
            continue
        операция.category_id = набор[ключ].id
        операция.category_source = ПО_МОДЕЛИ


def причина_отказа(сбой: Exception, *, для: str = "категорий") -> str:
    """Отказ слоя словами для owner. `для` - чего модель не дала.

    Без имён моделей: чинит он не модель, а назначение или маршрут платы,
    либо ждёт нового месяца. Общая с предложением набора (Ф10): два списка
    причин разошлись бы в первой же правке слоя.
    """
    if isinstance(сбой, llm.МаршрутНеНастроен):
        return f"модель для {для} не назначена"
    if isinstance(сбой, llm.ПотолокИсчерпан):
        return "месячный лимит на модели исчерпан"
    if isinstance(сбой, llm.ВыходЗаблокирован):
        # Не «модели недоступны»: чинится маршрутом платы (ADR-050).
        return "модели недоступны из сети платы"
    if isinstance(сбой, llm.ОтказПровайдера):
        return "модель отказала в разборе"
    if isinstance(сбой, llm.МаршрутИспорчен):
        return "назначение моделей испорчено, проверьте LLM_ROUTING"
    if isinstance(сбой, llm.ВсеМаршрутыОтказали):
        return "модели сейчас не ответили"
    return str(сбой)


def категоризовать_моделью(
    session: Session,
    settings: Settings,
    *,
    кандидаты: Mapping[dt.date, Sequence[Кандидат]],
    адаптеры: Mapping[str, llm.Адаптер],
    сейчас: dt.datetime,
    зона: ZoneInfo,
) -> ОтчётМодели:
    """Ступень 5 поверх базы: повтор по мерчанту, вызов порциями, запись.

    **Первый отказ слоя останавливает ступень целиком.** Не назначено,
    потолок, геоблок - следующая порция упрётся в то же самое, и каждая
    попытка - строка аудита, а при недоступном провайдере ещё и таймаут.
    Записанное до отказа остаётся: ответ оплачен и проверен.

    **Новая категория видна следующей порции.** Иначе вторая порция того же
    месяца завела бы ту же подкатегорию второй раз - и упала бы на
    уникальном индексе, утащив за собой весь разбор.
    """
    отчёт = ОтчётМодели(запущена=True, ждут=sum(len(г) for г in кандидаты.values()))
    try:
        маршруты = llm.загрузить_маршруты(settings.llm_routing)
    except llm.МаршрутИспорчен as сбой:
        logger.error("LLM_ROUTING не разбирается, категории без модели: %s", сбой)
        отчёт.отказ = причина_отказа(сбой)
        return отчёт

    прежние = _прежние(session)
    порция = settings.finance_categorize_chunk

    for месяц in sorted(кандидаты):
        набор = _набор_месяца(session, месяц)
        # Основные категории owner обязаны быть всегда (решение owner
        # 2026-10-01): модель дополняет набор, но не строит его с нуля.
        # Месяц без набора - это «owner ещё не унаследовал или не прислал
        # набор», и ответ на это - команда набора, а не выдумка модели.
        if not any(к.parent_id is None and к.origin == "owner" for к in набор.values()):
            отчёт.без_набора += len(кандидаты[месяц])
            continue

        очередь = list(кандидаты[месяц])
        while очередь:
            # Повтор - перед каждой порцией, а не один раз: мерчант, которого
            # модель разобрала в первой порции, во второй уже не платный.
            повторённые, очередь = повторы(очередь, прежние, _для_модели(набор))
            _назначить(session, повторённые, набор)
            отчёт.повтором += len(повторённые)
            if not очередь:
                break

            отправка, очередь = очередь[:порция], очередь[порция:]
            промпт = llm.собрать(
                "finance_categorize",
                month=месяц.strftime("%Y-%m"),
                categories=описать_набор(_для_модели(набор)),
                operations=описать_операции(отправка, зона),
            )
            try:
                результат = llm.вызвать(
                    session,
                    задача=llm.Задача.КНИЖКА_КАТЕГОРИЯ,
                    промпт=промпт,
                    схема=РазборОпераций,
                    актор=АКТОР,
                    сейчас=сейчас,
                    зона=зона,
                    настройки=settings,
                    маршруты=маршруты,
                    адаптеры=адаптеры,
                )
            except (llm.ОшибкаСлоя, llm.ОтказПровайдера, llm.МаршрутИспорчен) as сбой:
                logger.warning("категории моделью не получены: %s", сбой)
                отчёт.отказ = причина_отказа(сбой)
                return отчёт

            итог = прочитать_ответ(результат.значение, [к.id for к in отправка], _для_модели(набор))
            for новая in итог.новые:
                родитель = набор[новая.parent_key] if новая.parent_key is not None else None
                строка = FinCategory(
                    key=новая.key,
                    period_month=месяц,
                    level=1 if родитель is None else 2,
                    parent_id=родитель.id if родитель is not None else None,
                    parent_level=1 if родитель is not None else None,
                    title=новая.title,
                    origin="ai",
                    status="active",
                )
                session.add(строка)
                # Сразу, а не в конце: подкатегории нужен id основной,
                # заведённой этим же ответом, а назначению - id обеих.
                session.flush()
                набор[новая.key] = строка
            отчёт.новых_категорий += len(итог.новые)

            _назначить(session, итог.назначения, набор)
            отчёт.моделью += len(итог.назначения)
            for кандидат in отправка:
                ключ = итог.назначения.get(кандидат.id)
                имя = ключ_мерчанта(кандидат.merchant)
                if ключ is not None and имя is not None:
                    прежние[имя] = ключ
            for причина in итог.отклонено.values():
                отчёт.отклонено[причина] = отчёт.отклонено.get(причина, 0) + 1

    session.flush()
    return отчёт


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
    адаптеры: Mapping[str, llm.Адаптер] | None = None,
    сейчас: dt.datetime | None = None,
) -> tuple[ОтчётРазбора, list[Разбор]]:
    """Считает разбор всей книжки и, если просят, записывает его.

    Модель зовётся, только когда запись и переданы `адаптеры`. Без них
    ступень 5 не запускается вовсе, а отчёт называет, сколько её ждёт:
    так разбор зовут тесты и всё, что не должно ходить в сеть.
    """
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
        session.flush()

    кандидаты = _кандидаты(session, решения, зона)
    отчёт.модель.ждут = sum(len(группа) for группа in кандидаты.values())
    if apply and адаптеры is not None and кандидаты:
        отчёт.модель = категоризовать_моделью(
            session,
            settings,
            кандидаты=кандидаты,
            адаптеры=адаптеры,
            сейчас=сейчас or dt.datetime.now(dt.UTC),
            зона=зона,
        )
        отчёт.без_категории -= отчёт.модель.назначено
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
    строки.extend(описать_модель(отчёт.модель))
    if not apply:
        строки.append("  dry-run: в базу не записано ничего")
    return строки


def описать_модель(отчёт: ОтчётМодели) -> list[str]:
    """Строки диффа про ступень 5. Пусто, если ей нечего было делать."""
    if not отчёт.запущена:
        if отчёт.ждут == 0:
            return []
        return [f"  модели ушло бы операций: {отчёт.ждут} - она зовётся только с --apply"]

    строки = [
        f"  модель: ждали {отчёт.ждут}, разобрано {отчёт.моделью},"
        f" повтором по мерчанту {отчёт.повтором}, новых категорий {отчёт.новых_категорий}"
    ]
    if отчёт.без_набора:
        строки.append(
            f"  {отчёт.без_набора}: месяц без набора категорий owner - модели не отправлены"
        )
    for причина in sorted(отчёт.отклонено):
        строки.append(f"  {отчёт.отклонено[причина]}: без категории - {причина}")
    if отчёт.отказ is not None:
        # Деградация, а не провал (§15.6): разбор правилами записан,
        # остальное видно как неразобранное и ждёт следующего прогона.
        строки.append(f"  модель не ответила: {отчёт.отказ} - операции остались без категории")
    return строки


def run_once(
    session: Session,
    settings: Settings,
    *,
    apply: bool,
    адаптеры: Mapping[str, llm.Адаптер] | None = None,
) -> int:
    """Прогон поверх готовой сессии. Возвращает код возврата процесса."""
    try:
        отчёт, решения = разобрать_книжку(session, settings, apply=apply, адаптеры=адаптеры)
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
    # Адаптеры только для записи: dry-run модель не зовёт, и собирать
    # соединения с провайдерами ему незачем.
    адаптеры = llm.собрать_адаптеры(settings) if apply else None
    with get_sessionmaker()() as session:
        return run_once(session, settings, apply=apply, адаптеры=адаптеры)


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

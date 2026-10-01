"""Предложение набора категорий на новый месяц (Ф10, §15.4, ADR-056).

Модель смотрит на траты прошлых месяцев и предлагает, какие основные
категории добавить в набор месяца и какие убрать. Набор к этому моменту уже
унаследован и работает: предложение - вопрос owner, а не правка книжки.

Здесь - что модели показать, как прочитать её ответ и как записать решение
owner. Вызов модели и переход месяца живут в `jobs/finance_proposals.py`.

Три правила, и каждое ломается тихо.

**Ответ проверяет код, а не промпт.** Ключ не по форме, занятый ключ,
название соседа, «убрать» чужую или неизвестную категорию - пункт
отбрасывается с причиной. Промпт просит того же, но промпт - просьба,
а набор месяца - данные, по которым сравниваются месяцы (§15.9).

**Убрать можно только категорию без трат за всю показанную историю.**
Модель видит те же цифры и вправе ошибиться в чтении, а убранная категория
с тратами - это расход, который через месяц не с чем сравнить. Отказ здесь
дешевле: owner прочитает на одно предложение меньше.

**Принятое удаление снимает категорию с операций месяца, разобранных
автоматикой.** Разбор не снимает категорию, если ни одна ступень
не сработала (`finance_categorize._категория`), - и без этого операции
остались бы в категории, которой в наборе больше нет. Ручная правка
не снимается: она сильнее всех ступеней (§15.4), и выбор owner остаётся
его выбором.
"""

import datetime as dt
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.db.models import FinCategory, FinTransaction
from jarvis_api.domain.finance_categorize import ВРУЧНУЮ
from jarvis_api.domain.finance_categorize_model import ДЛИНА_КЛЮЧА, ДЛИНА_НАЗВАНИЯ, ФОРМА_КЛЮЧА

ДОБАВИТЬ = "add"
УБРАТЬ = "remove"

# Причина - текст для owner на экране категорий. Длиннее - уже не причина,
# а эссе, и вероятнее всего модель ушла от формата.
ДЛИНА_ПРИЧИНЫ = 1000


@dataclass(frozen=True, slots=True)
class Основная:
    """Основная категория набора месяца, как её видит модель."""

    key: str
    title: str
    origin: str


@dataclass(frozen=True, slots=True)
class Трата:
    """Расход прошлого месяца, сведённый к основной категории.

    `key` пуст у неразобранного: такие идут в историю отдельной строкой,
    а самые дорогие мерчанты из них - поводом для новой категории.
    """

    month: dt.date
    key: str | None
    amount: Decimal
    merchant: str | None


@dataclass(frozen=True, slots=True)
class ТратыКатегории:
    key: str
    title: str
    origin: str
    spent: Decimal
    count: int


@dataclass(frozen=True, slots=True)
class МесяцИстории:
    month: dt.date
    categories: tuple[ТратыКатегории, ...]
    uncategorized_spent: Decimal
    uncategorized_count: int
    merchants: tuple[str, ...]


class ПунктПредложения(BaseModel):
    """Один пункт ответа. `action` и форма ключа проверяются кодом:
    отказ схемы переспросил бы ответ целиком из-за одного кривого пункта."""

    model_config = ConfigDict(extra="forbid")

    action: str
    key: str
    title: str | None
    reason: str


class ПредложениеНабора(BaseModel):
    """Схема ответа модели. Принадлежит точке вызова, а не адаптеру (ADR-049)."""

    model_config = ConfigDict(extra="forbid")

    proposals: list[ПунктПредложения]


@dataclass(frozen=True, slots=True)
class Добавить:
    key: str
    title: str
    reason: str


@dataclass(frozen=True, slots=True)
class Убрать:
    key: str
    reason: str


@dataclass(slots=True)
class ИтогПредложения:
    добавить: list[Добавить] = field(default_factory=list)
    убрать: list[Убрать] = field(default_factory=list)
    # Причины отказа по пунктам, словами: их печатает дифф перехода месяца.
    отклонено: list[str] = field(default_factory=list)


def свести_историю(
    траты: Iterable[Трата],
    наборы: Mapping[dt.date, Sequence[Основная]],
    *,
    мерчантов: int,
) -> list[МесяцИстории]:
    """Траты прошлых месяцев - по месяцу и ключу основной категории.

    Месяц попадает в историю, только если в нём есть хотя бы одна трата:
    пустой месяц ничего не говорит о привычках owner, а «нулевые траты по
    всем категориям» модель прочитала бы как повод всё убрать.

    Категории набора без трат показываются с нулём. Ровно на них модель
    и опирается, предлагая убрать категорию, - пропусти их, и пустую
    категорию не отличить от неизвестной.
    """
    по_месяцам: dict[dt.date, list[Трата]] = {}
    for трата in траты:
        по_месяцам.setdefault(трата.month, []).append(трата)

    история: list[МесяцИстории] = []
    for месяц in sorted(по_месяцам):
        строки = по_месяцам[месяц]
        сумма: dict[str, Decimal] = {}
        сколько: dict[str, int] = {}
        без_ключа = Decimal("0")
        без_ключа_сколько = 0
        по_мерчанту: dict[str, Decimal] = {}
        for трата in строки:
            if трата.key is None:
                без_ключа += трата.amount
                без_ключа_сколько += 1
                if трата.merchant:
                    по_мерчанту[трата.merchant] = (
                        по_мерчанту.get(трата.merchant, Decimal("0")) + трата.amount
                    )
                continue
            сумма[трата.key] = сумма.get(трата.key, Decimal("0")) + трата.amount
            сколько[трата.key] = сколько.get(трата.key, 0) + 1

        известные = {основная.key: основная for основная in наборы.get(месяц, ())}
        # Ключ с тратами, но без строки набора (категорию убрали, а траты
        # остались под ней ручной правкой), - всё равно в истории: деньги
        # были, и сравнивать их есть с чем.
        ключи = sorted(set(известные) | set(сумма))
        категории = tuple(
            ТратыКатегории(
                key=ключ,
                title=известные[ключ].title if ключ in известные else ключ,
                origin=известные[ключ].origin if ключ in известные else "owner",
                spent=сумма.get(ключ, Decimal("0")),
                count=сколько.get(ключ, 0),
            )
            for ключ in ключи
        )
        # Порядок - сумма по убыванию, затем имя: при равных суммах выборка
        # обязана быть воспроизводимой, иначе один и тот же месяц дал бы
        # разный промпт и разный кэш.
        дорогие = sorted(по_мерчанту.items(), key=lambda пара: (-пара[1], пара[0]))
        история.append(
            МесяцИстории(
                month=месяц,
                categories=категории,
                uncategorized_spent=без_ключа,
                uncategorized_count=без_ключа_сколько,
                merchants=tuple(имя for имя, _ in дорогие[:мерчантов]),
            )
        )
    return история


def описать_набор(основные: Sequence[Основная]) -> str:
    """Набор для промпта: «- ключ: название (owner)»."""
    return "\n".join(
        f"- {к.key}: {к.title} ({к.origin})" for к in sorted(основные, key=lambda к: к.key)
    )


def описать_историю(история: Sequence[МесяцИстории]) -> str:
    """История для промпта, по JSON-строке на месяц.

    Суммы строками: `Decimal` в JSON-число стал бы двоичной дробью, и модель
    читала бы «6199.999999» там, где owner потратил 6 200 ₽.
    """
    return "\n".join(
        json.dumps(
            {
                "month": месяц.month.strftime("%Y-%m"),
                "categories": [
                    {
                        "key": к.key,
                        "title": к.title,
                        "origin": к.origin,
                        "spent": str(к.spent),
                        "count": к.count,
                    }
                    for к in месяц.categories
                ],
                "uncategorized": {
                    "spent": str(месяц.uncategorized_spent),
                    "count": месяц.uncategorized_count,
                    "top_merchants": list(месяц.merchants),
                },
            },
            ensure_ascii=False,
        )
        for месяц in история
    )


def _с_тратами(история: Iterable[МесяцИстории]) -> set[str]:
    return {к.key for месяц in история for к in месяц.categories if к.count > 0}


def прочитать_предложение(
    ответ: ПредложениеНабора,
    основные: Sequence[Основная],
    занятые: Iterable[str],
    история: Sequence[МесяцИстории],
) -> ИтогПредложения:
    """Ответ модели -> пункты, которые можно положить owner, и отказы.

    `основные` - активные основные категории месяца, `занятые` - все ключи
    месяца, включая подкатегории и прежние предложения: уникальный индекс
    не пустит второй такой ключ, а правило по ключу стало бы неоднозначным
    (то же правило, что у файла набора и у Ф9).

    Отказ одного пункта не трогает остальные.
    """
    итог = ИтогПредложения()
    по_ключу = {к.key: к for к in основные}
    ключи = set(занятые) | set(по_ключу)
    названия = {к.title.strip().casefold() for к in основные}
    с_тратами = _с_тратами(история)
    названо: set[str] = set()

    for пункт in ответ.proposals:
        ключ = пункт.key.strip()
        причина = пункт.reason.strip()
        if ключ in названо:
            итог.отклонено.append(f"ключ {ключ!r} назван дважды")
            continue
        названо.add(ключ)
        if not причина:
            итог.отклонено.append(f"{ключ!r} без причины")
            continue
        if len(причина) > ДЛИНА_ПРИЧИНЫ:
            итог.отклонено.append(f"причина у {ключ!r} длиннее {ДЛИНА_ПРИЧИНЫ} символов")
            continue

        if пункт.action == УБРАТЬ:
            прежняя = по_ключу.get(ключ)
            if прежняя is None:
                итог.отклонено.append(f"убрать {ключ!r}: такой основной категории в наборе нет")
                continue
            if прежняя.origin != "owner":
                # Категория модели живёт месяц и в следующий не переносится
                # (ADR-056): предлагать её убрать незачем.
                итог.отклонено.append(f"убрать {ключ!r}: категория заведена не owner")
                continue
            if ключ in с_тратами:
                итог.отклонено.append(f"убрать {ключ!r}: по ней были траты")
                continue
            итог.убрать.append(Убрать(key=ключ, reason=причина))
            continue

        if пункт.action != ДОБАВИТЬ:
            итог.отклонено.append(f"{ключ!r}: неизвестное действие {пункт.action!r}")
            continue
        if len(ключ) > ДЛИНА_КЛЮЧА or not ФОРМА_КЛЮЧА.match(ключ):
            итог.отклонено.append(f"добавить {ключ!r}: ключ не по форме")
            continue
        if ключ in ключи:
            итог.отклонено.append(f"добавить {ключ!r}: ключ в месяце уже занят")
            continue
        название = (пункт.title or "").strip()
        if not название:
            итог.отклонено.append(f"добавить {ключ!r}: без названия")
            continue
        if len(название) > ДЛИНА_НАЗВАНИЯ:
            итог.отклонено.append(f"добавить {ключ!r}: название длиннее {ДЛИНА_НАЗВАНИЯ} символов")
            continue
        if название.casefold() in названия:
            итог.отклонено.append(f"добавить {ключ!r}: повторяет название {название!r}")
            continue
        ключи.add(ключ)
        названия.add(название.casefold())
        итог.добавить.append(Добавить(key=ключ, title=название, reason=причина))
    return итог


# --- решение owner -----------------------------------------------------------


class ОшибкаРешения(ValueError):
    """Решать нечего: категории нет или она не ждёт решения owner."""

    def __init__(self, код: str, текст: str) -> None:
        super().__init__(текст)
        self.код = код


@dataclass(frozen=True, slots=True)
class Решение:
    """Что решение сделало с набором и книжкой."""

    категория: FinCategory
    # Операции, с которых снята категория убранной: разбор разложит их
    # заново, правилами и моделью.
    снято: int
    # Ручные правки в убранной категории: они остаются, как решил owner.
    оставлено_ручных: int


def ждут_решения(session: Session, месяц: dt.date | None = None) -> Sequence[FinCategory]:
    """Предложения, по которым owner ещё не решил: «добавить» и «убрать»."""
    запрос = select(FinCategory).where(
        (FinCategory.status == "proposed")
        | ((FinCategory.status == "active") & FinCategory.remove_proposed.is_(True))
    )
    if месяц is not None:
        запрос = запрос.where(FinCategory.period_month == месяц)
    return session.scalars(запрос.order_by(FinCategory.period_month, FinCategory.key)).all()


def решить(session: Session, category_id: int, *, принять: bool) -> Решение:
    """Решение owner по одному предложению. Транзакцию коммитит вызывающий.

    **Принятая категория становится категорией owner** (`origin = owner`):
    owner её выбрал, и со следующего месяца она наследуется наравне с его
    собственными. Иначе она прожила бы месяц, как заведённая разбором,
    и модель предлагала бы её заново каждый месяц.

    **Убранная категория не удаляется, а уходит в `rejected`** вместе
    с подкатегориями. Операции на неё ссылаются, и история «почему трата
    была здесь» не должна пропадать вместе со строкой. Подкатегории -
    потому что разбор держит в наборе всё `active`, и правило по ключу
    подкатегории продолжило бы класть траты в ветку без родителя.
    """
    категория = session.get(FinCategory, category_id)
    if категория is None:
        raise ОшибкаРешения("нет_категории", f"категории {category_id} нет")

    if категория.status == "proposed":
        if принять:
            категория.status = "active"
            категория.origin = "owner"
        else:
            категория.status = "rejected"
        session.flush()
        return Решение(категория=категория, снято=0, оставлено_ручных=0)

    if not (категория.status == "active" and категория.remove_proposed):
        raise ОшибкаРешения(
            "не_предложение",
            f"категория {категория.key!r} за {категория.period_month:%Y-%m} не ждёт решения",
        )

    if not принять:
        категория.remove_proposed = False
        session.flush()
        return Решение(категория=категория, снято=0, оставлено_ручных=0)

    ветка = session.scalars(
        select(FinCategory).where(
            (FinCategory.id == категория.id) | (FinCategory.parent_id == категория.id)
        )
    ).all()
    номера = [строка.id for строка in ветка]
    for строка in ветка:
        строка.status = "rejected"

    операции = session.scalars(
        select(FinTransaction).where(FinTransaction.category_id.in_(номера))
    ).all()
    снято = 0
    оставлено = 0
    for операция in операции:
        if операция.category_source == ВРУЧНУЮ:
            оставлено += 1
            continue
        операция.category_id = None
        операция.category_source = None
        снято += 1
    session.flush()
    return Решение(категория=категория, снято=снято, оставлено_ручных=оставлено)

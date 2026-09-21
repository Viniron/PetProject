"""Правила разбора книжки: завести, удалить, перечислить (Ф6, §15.4, §15.5).

До Ф6 правила приезжали только файлом (`make finance-taxonomy`), и это
по-прежнему главный канал: набор категорий с правилами - данные owner, как
содержание курса. Здесь - вторая дверь, узкая и штучная: owner поправил
операцию на экране и просит запомнить решение. Категории через неё не
заводятся: категория - версия месяца, и создавать её по одной строке значило
бы собирать набор месяца вслепую.

**Проверки повторяют CHECK базы намеренно.** `IntegrityError` от Postgres
даёт owner текст про ограничение `rule_decides_something`, по которому
непонятно, что именно исправить. Отказ отсюда называет причину словами
и приходит до записи.

**Ключ категории проверяется на существование.** Опечатка в ключе не ломает
ничего заметного: правило просто не находит категорию, операции остаются
без неё, и причину потом не найти. Ключ, которого нет ни в одном месяце, -
это опечатка, а не заготовка на будущее.
"""

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.db.models import FinCategory, FinCategoryRule

# Ступени разбора (CHECK `rule_type_known`). `self` - не ступень: оно говорит,
# каким написанием банк называет самого owner (ADR-041).
ТИПЫ = ("mcc", "bank_category", "merchant", "sender", "self")

# Правило отправителя - единственное, что решает вид, а не категорию (§15.5).
ВИДЫ = ("income", "refund")


class ОшибкаПравила(ValueError):
    """Правило отклонено. С кодом - из него собирается тело отказа API."""

    def __init__(self, код: str, сообщение: str) -> None:
        super().__init__(сообщение)
        self.код = код


def проверить(
    session: Session,
    *,
    rule_type: str,
    pattern: str,
    category_key: str | None,
    kind: str | None,
) -> None:
    """Проверяет форму правила до записи. Молчит, если правило законно."""
    if rule_type not in ТИПЫ:
        raise ОшибкаПравила("неизвестный_тип", f"тип правила {rule_type!r} не из {', '.join(ТИПЫ)}")
    if not pattern.strip():
        raise ОшибкаПравила("пустой_образец", "образец правила пуст: сопоставлять нечего")

    if rule_type == "sender":
        if kind not in ВИДЫ:
            raise ОшибкаПравила(
                "нет_вида",
                f"правило отправителя обязано решать вид: {' или '.join(ВИДЫ)}",
            )
        if category_key is not None:
            raise ОшибкаПравила(
                "лишняя_категория",
                "правило отправителя решает вид, а не категорию (§15.5)",
            )
        return

    if rule_type == "self":
        if kind is not None or category_key is not None:
            raise ОшибкаПравила(
                "лишнее_решение",
                "правило self не решает ни вид, ни категорию: оно только называет"
                " написание owner, а вид такой операции решает пара концов (ADR-041)",
            )
        return

    if category_key is None:
        raise ОшибкаПравила(
            "нет_категории",
            f"правило типа {rule_type!r} обязано указывать категорию",
        )
    if kind is not None:
        raise ОшибкаПравила(
            "лишний_вид",
            "вид решает только правило отправителя (§15.5)",
        )
    существует = session.scalar(
        select(FinCategory.id).where(FinCategory.key == category_key).limit(1)
    )
    if существует is None:
        raise ОшибкаПравила(
            "нет_такой_категории",
            f"категории с ключом {category_key!r} нет ни в одном месяце:"
            " набор категорий заводится файлом (make finance-taxonomy)",
        )


def найти(session: Session, *, rule_type: str, pattern: str) -> FinCategoryRule | None:
    """Правило с этим типом и образцом - тем же ключом, что уникален в базе."""
    return session.scalar(
        select(FinCategoryRule).where(
            FinCategoryRule.rule_type == rule_type,
            FinCategoryRule.pattern == pattern,
        )
    )


def создать(
    session: Session,
    *,
    rule_type: str,
    pattern: str,
    title: str | None = None,
    category_key: str | None = None,
    kind: str | None = None,
    переписать: bool = False,
) -> tuple[FinCategoryRule, bool]:
    """Заводит правило. Возвращает само правило и признак «что-то изменилось».

    `переписать` - для галочки «запомнить решение» на карточке операции:
    owner поправил ту же операцию второй раз иначе, и правило обязано
    поехать следом, иначе правка молча не запомнится. Явное заведение
    правила этим флагом не пользуется: там дубль - это отказ, потому что
    молча переписанное правило меняет разбор всей книжки.

    Транзакцию коммитит вызывающий код.
    """
    проверить(
        session,
        rule_type=rule_type,
        pattern=pattern,
        category_key=category_key,
        kind=kind,
    )

    существующее = найти(session, rule_type=rule_type, pattern=pattern)
    if существующее is not None:
        то_же = (
            существующее.category_key == category_key
            and существующее.kind == kind
            and (title is None or существующее.title == title)
        )
        if то_же:
            return существующее, False
        if not переписать:
            raise ОшибкаПравила(
                "правило_есть",
                f"правило {rule_type} для {pattern!r} уже заведено и решает иначе:"
                f" категория {существующее.category_key}, вид {существующее.kind}."
                " Сначала удалить старое",
            )
        существующее.category_key = category_key
        существующее.kind = kind
        if title is not None:
            существующее.title = title
        return существующее, True

    правило = FinCategoryRule(
        rule_type=rule_type,
        pattern=pattern,
        title=title,
        category_key=category_key,
        kind=kind,
    )
    session.add(правило)
    session.flush()  # нужен id: он уезжает в ответ и в отчёт о правке
    return правило, True


def удалить(session: Session, rule_id: int) -> bool:
    """Убирает правило. `False` - правила с таким id не было.

    Операции, разобранные этим правилом, не трогаются: их вид и категорию
    пересчитает следующий разбор, и до него книжка показывает то же, что
    показывала. Молча переписать их здесь значило бы менять сальдо двух
    месяцев на удалении одной строки справочника.
    """
    правило = session.get(FinCategoryRule, rule_id)
    if правило is None:
        return False
    session.delete(правило)
    return True


def перечислить(session: Session) -> Sequence[FinCategoryRule]:
    """Все правила в порядке «тип, образец» - для показа owner."""
    return session.scalars(
        select(FinCategoryRule).order_by(FinCategoryRule.rule_type, FinCategoryRule.pattern)
    ).all()


def категории(session: Session, месяц_: dt.date | None = None) -> Sequence[FinCategory]:
    """Категории месяца, а без месяца - все, в порядке «месяц, уровень, ключ».

    Месяцем, а не целиком: набор - версия месяца (§15.4), и выпадающий
    список на карточке операции обязан показывать набор её месяца, иначе
    owner выберет категорию, которой в этом месяце не существует.
    """
    запрос = select(FinCategory)
    if месяц_ is not None:
        запрос = запрос.where(FinCategory.period_month == месяц_)
    return session.scalars(
        запрос.order_by(FinCategory.period_month, FinCategory.level, FinCategory.key)
    ).all()

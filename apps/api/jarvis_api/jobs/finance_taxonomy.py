"""Набор категорий месяца и правила разбора - файлом от owner (§15.4, §15.5).

Категории и правила - **данные, а не код** (инвариант 2): ядро не знает
ни одного названия, как не знает названий предметов. Поэтому список приходит
тем же способом, что выписка, - файлом, который owner присылает, а джоб
применяет с фазой dry-run. Хардкодить «Еда» и «Транспорт» в исходниках
значило бы сделать книжку книжкой одного человека и одного года.

**Файл - ввод, состояние - в базе** (инвариант хоста 1). Применённый файл
нигде не хранится и ни на что не влияет: восстановленный дамп содержит
и категории, и правила, и второй раз присылать их не нужно.

**Ключ категории уникален внутри месяца целиком, включая подкатегории.**
База этого не требует - там уникальность подкатегории считается в пределах
родителя, - а разбор требует: правило ссылается на ключ, и два одинаковых
ключа под разными родителями сделали бы правило неоднозначным. Проверяется
здесь, на вводе, где ошибка ещё лечится правкой файла.

**Наследование месяца - отдельный режим, а не побочный эффект.** §15.4:
набор нового месяца наследуется от прошлого, owner правит и утверждает,
а до утверждения работает унаследованный. Копирование запускается явно
и в уже заполненный месяц не лезет: молчаливое «дополню, чего не хватает»
однажды вернуло бы категорию, которую owner из месяца убрал.
"""

import argparse
import datetime as dt
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.models import FinCategory, FinCategoryRule
from jarvis_api.db.session import get_sessionmaker

logger = logging.getLogger("jarvis.finance_taxonomy")

ТипПравила = Literal["mcc", "bank_category", "merchant", "sender", "self"]


class ОшибкаНабора(RuntimeError):
    """Файл набора не применён. Отказ целиком, как у выписки (§15.1)."""


def _первое_число(значение: str, тип_отказа: type[Exception]) -> dt.date:
    """«ГГГГ-ММ» в первое число месяца. Тип отказа зависит от места вызова.

    Через `date`, а не `strptime`: наивного `datetime` здесь не возникает
    ни на мгновение (инвариант 7), а месяц - это дата, а не момент времени.
    """
    try:
        return dt.date.fromisoformat(f"{значение}-01")
    except ValueError as ошибка:
        raise тип_отказа(f"{значение!r} - не месяц вида ГГГГ-ММ") from ошибка


class Подкатегория(BaseModel):
    """Второй уровень. Третьего не бывает - это держит база, а не соглашение."""

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str


class КатегорияФайла(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    children: list[Подкатегория] = []


class ПравилоФайла(BaseModel):
    """Правило разбора. Форма зависит от типа, и это проверяется здесь.

    `title` - как owner называет то, что банк пишет сокращённо: Т-Банк
    отдаёт отправителя как «Евгений В.», а в списке родных у owner записано
    ФИО. Сопоставляется первое, показывается второе (ADR-041).
    """

    model_config = ConfigDict(extra="forbid")

    type: ТипПравила
    pattern: str
    category: str | None = None
    kind: Literal["income", "refund"] | None = None
    title: str | None = None

    @model_validator(mode="after")
    def проверить_форму(self) -> "ПравилоФайла":
        if self.type == "sender":
            if self.kind is None:
                raise ValueError(f"правило по отправителю {self.pattern!r} без kind")
            if self.category is not None:
                raise ValueError(
                    f"правило по отправителю {self.pattern!r} определяет вид, а не категорию"
                )
        elif self.type == "self":
            if self.kind is not None or self.category is not None:
                raise ValueError(
                    f"правило self {self.pattern!r} только называет написание owner:"
                    " ни категории, ни вида у него нет (ADR-041)"
                )
        elif self.category is None:
            raise ValueError(f"правило {self.type} {self.pattern!r} без категории")
        return self


class ФайлНабора(BaseModel):
    """Набор месяца целиком. Опечатка в ключе - отказ, а не тишина."""

    model_config = ConfigDict(extra="forbid")

    month: str
    categories: list[КатегорияФайла] = []
    rules: list[ПравилоФайла] = []

    @property
    def месяц(self) -> dt.date:
        """«2026-08» - первое августа. Месяц хранится датой (§15.2)."""
        return _первое_число(self.month, ОшибкаНабора)


@dataclass(slots=True)
class ОтчётНабора:
    """Что файл сделал бы или сделал."""

    месяц: dt.date | None = None
    категорий_новых: int = 0
    категорий_переименовано: int = 0
    категорий_в_месяце: int = 0
    правил_новых: int = 0
    правил_изменено: int = 0
    унаследовано: int = 0
    заметки: list[str] = field(default_factory=list)


def прочитать(путь: Path) -> ФайлНабора:
    """Файл owner в проверенную структуру. Любая беда - отказ целиком."""
    if not путь.is_file():
        raise ОшибкаНабора(f"файла нет: {путь}")
    try:
        сырое = json.loads(путь.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as ошибка:
        raise ОшибкаНабора(f"{путь.name}: не читается как JSON - {ошибка}") from ошибка
    try:
        набор = ФайлНабора.model_validate(сырое)
    except ValidationError as ошибка:
        raise ОшибкаНабора(f"{путь.name}: {ошибка}") from ошибка
    проверить_ключи(набор)
    return набор


def проверить_ключи(набор: ФайлНабора) -> None:
    """Ключи месяца уникальны целиком, и каждое правило указывает на живой.

    Правило, указывающее на ключ, которого в наборе нет, не падает при
    разборе - оно молча не срабатывает. Ровно поэтому оно ловится здесь:
    «категория не проставилась» owner увидит через месяц и будет искать
    причину в коде.
    """
    ключи: set[str] = set()
    дубли: set[str] = set()
    for категория in набор.categories:
        for ключ in (категория.key, *(п.key for п in категория.children)):
            if ключ in ключи:
                дубли.add(ключ)
            ключи.add(ключ)
    if дубли:
        raise ОшибкаНабора(
            f"ключи повторяются в наборе месяца: {sorted(дубли)} -"
            " правило по такому ключу указывало бы сразу на две категории"
        )
    мимо = sorted(
        {
            правило.category
            for правило in набор.rules
            if правило.category is not None and правило.category not in ключи
        }
    )
    if мимо:
        raise ОшибкаНабора(f"правила ссылаются на ключи, которых нет в наборе: {мимо}")


def _применить_категории(
    session: Session, набор: ФайлНабора, отчёт: ОтчётНабора, *, apply: bool
) -> None:
    """Заводит недостающие категории месяца и переименовывает изменившиеся.

    Лишние не удаляются и не отклоняются: на них ссылаются операции этого
    месяца, а разбор прошлых месяцев не меняется никогда (§15.4). Убрать
    категорию из месяца - действие owner, и делается оно там, где видно,
    что с её операциями станет.
    """
    месяц = набор.месяц
    существующие = {
        (строка.key, строка.parent_id): строка
        for строка in session.scalars(
            select(FinCategory).where(FinCategory.period_month == месяц)
        ).all()
    }
    отчёт.категорий_в_месяце = len(существующие)

    for категория in набор.categories:
        строка = существующие.get((категория.key, None))
        if строка is None:
            отчёт.категорий_новых += 1
            if apply:
                строка = FinCategory(
                    key=категория.key,
                    period_month=месяц,
                    level=1,
                    title=категория.title,
                    origin="owner",
                    status="active",
                )
                session.add(строка)
                session.flush()  # нужен id: на него ссылаются подкатегории
        elif строка.title != категория.title:
            отчёт.категорий_переименовано += 1
            if apply:
                строка.title = категория.title

        for подкатегория in категория.children:
            родитель = строка.id if строка is not None else None
            существующая = _подкатегория(существующие, подкатегория.key, родитель)
            if существующая is None:
                отчёт.категорий_новых += 1
                if apply and строка is not None:
                    session.add(
                        FinCategory(
                            key=подкатегория.key,
                            period_month=месяц,
                            level=2,
                            parent_id=строка.id,
                            parent_level=1,
                            title=подкатегория.title,
                            origin="owner",
                            status="active",
                        )
                    )
            elif существующая.title != подкатегория.title:
                отчёт.категорий_переименовано += 1
                if apply:
                    существующая.title = подкатегория.title


def _подкатегория(
    существующие: dict[tuple[str, int | None], FinCategory],
    ключ: str,
    родитель: int | None,
) -> FinCategory | None:
    """Подкатегория месяца по ключу и родителю. `None` - её ещё нет."""
    if родитель is None:
        return None
    return существующие.get((ключ, родитель))


def _применить_правила(
    session: Session, набор: ФайлНабора, отчёт: ОтчётНабора, *, apply: bool
) -> None:
    """Правила - по паре (тип, образец): та же пара во второй раз обновляет.

    Уникальность держит база (`uq_fin_category_rules_type_pattern`), и файл,
    присланный дважды, меняет ноль строк - то же свойство, что у выписки.
    """
    существующие = {
        (строка.rule_type, строка.pattern): строка
        for строка in session.scalars(select(FinCategoryRule)).all()
    }
    for правило in набор.rules:
        строка = существующие.get((правило.type, правило.pattern))
        if строка is None:
            отчёт.правил_новых += 1
            if apply:
                session.add(
                    FinCategoryRule(
                        rule_type=правило.type,
                        pattern=правило.pattern,
                        title=правило.title,
                        category_key=правило.category,
                        kind=правило.kind,
                    )
                )
        elif (
            строка.category_key != правило.category
            or строка.kind != правило.kind
            or строка.title != правило.title
        ):
            отчёт.правил_изменено += 1
            if apply:
                строка.category_key = правило.category
                строка.kind = правило.kind
                строка.title = правило.title


def применить_файл(session: Session, набор: ФайлНабора, *, apply: bool) -> ОтчётНабора:
    """Набор месяца и правила из файла owner."""
    отчёт = ОтчётНабора(месяц=набор.месяц)
    _применить_категории(session, набор, отчёт, apply=apply)
    _применить_правила(session, набор, отчёт, apply=apply)
    return отчёт


def унаследовать(session: Session, откуда: dt.date, куда: dt.date, *, apply: bool) -> ОтчётНабора:
    """Копирует активный набор одного месяца в другой (§15.4).

    В непустой месяц не лезет: «дополню, чего не хватает» однажды вернуло бы
    категорию, которую owner из месяца убрал, и заметить это было бы нечем.
    Копируются только `active`: предложения модели (`proposed`) ждут owner
    и наследоваться не вправе.
    """
    отчёт = ОтчётНабора(месяц=куда)
    занято = session.scalar(select(FinCategory.id).where(FinCategory.period_month == куда).limit(1))
    if занято is not None:
        отчёт.заметки.append(
            f"месяц {куда.isoformat()} уже заполнен - наследование пропущено, набор правится файлом"
        )
        return отчёт

    источник = session.scalars(
        select(FinCategory)
        .where(FinCategory.period_month == откуда, FinCategory.status == "active")
        .order_by(FinCategory.level, FinCategory.id)
    ).all()
    if not источник:
        отчёт.заметки.append(f"в месяце {откуда.isoformat()} нет активных категорий")
        return отчёт

    перенос: dict[int, int] = {}
    for строка in источник:
        отчёт.унаследовано += 1
        if not apply:
            continue
        новая = FinCategory(
            key=строка.key,
            period_month=куда,
            level=строка.level,
            parent_id=перенос.get(строка.parent_id) if строка.parent_id else None,
            parent_level=строка.parent_level,
            title=строка.title,
            origin=строка.origin,
            status="active",
        )
        session.add(новая)
        session.flush()  # id нужен подкатегориям: они идут следом по `level`
        перенос[строка.id] = новая.id
    return отчёт


def описать(отчёт: ОтчётНабора, apply: bool) -> list[str]:
    """Человекочитаемый дифф. Отдельной функцией - её проверяет тест."""
    месяц = отчёт.месяц.isoformat() if отчёт.месяц is not None else "-"
    строки = [
        f"набор категорий на {месяц}: в месяце уже {отчёт.категорий_в_месяце}",
        f"  новых категорий {отчёт.категорий_новых},"
        f" переименовано {отчёт.категорий_переименовано},"
        f" унаследовано {отчёт.унаследовано}",
        f"  новых правил {отчёт.правил_новых}, изменено {отчёт.правил_изменено}",
    ]
    строки.extend(f"  {заметка}" for заметка in отчёт.заметки)
    if not apply:
        строки.append("  dry-run: в базу не записано ничего")
    строки.append("  разбор операций этим джобом не запускается: make finance-categorize")
    return строки


def run_once(
    session: Session,
    *,
    путь: Path | None,
    наследовать_из: dt.date | None,
    месяц: dt.date | None,
    apply: bool,
) -> int:
    """Прогон поверх готовой сессии. Возвращает код возврата процесса."""
    try:
        if наследовать_из is not None:
            if месяц is None:
                raise ОшибкаНабора("наследование без --month: непонятно, в какой месяц копировать")
            отчёт = унаследовать(session, наследовать_из, месяц, apply=apply)
        elif путь is not None:
            отчёт = применить_файл(session, прочитать(путь), apply=apply)
        else:
            raise ОшибкаНабора("нечего делать: нужен --file или --inherit-from")
    except ОшибкаНабора as сбой:
        session.rollback()
        logger.error("набор не применён: %s", сбой)
        return 1

    if apply:
        session.commit()
    else:
        session.rollback()

    for строка in описать(отчёт, apply):
        logger.info("%s", строка)
    return 0


def run(
    settings: Settings,
    *,
    путь: Path | None,
    наследовать_из: dt.date | None,
    месяц: dt.date | None,
    apply: bool,
) -> int:
    with get_sessionmaker()() as session:
        return run_once(session, путь=путь, наследовать_из=наследовать_из, месяц=месяц, apply=apply)


def _месяц_аргумента(значение: str) -> dt.date:
    return _первое_число(значение, argparse.ArgumentTypeError)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага в базу не пишется ничего."""
    parser = argparse.ArgumentParser(
        description="Набор категорий месяца и правила разбора книжки JARVIS"
    )
    parser.add_argument("--file", dest="file", type=Path, help="JSON-файл набора от owner")
    parser.add_argument(
        "--inherit-from",
        dest="inherit_from",
        type=_месяц_аргумента,
        help="месяц-источник ГГГГ-ММ: скопировать его набор в --month",
    )
    parser.add_argument(
        "--month",
        dest="month",
        type=_месяц_аргумента,
        help="месяц-приёмник ГГГГ-ММ для наследования",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать набор (без флага - только показать дифф)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(
        get_settings(),
        путь=args.file,
        наследовать_из=args.inherit_from,
        месяц=args.month,
        apply=args.apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())

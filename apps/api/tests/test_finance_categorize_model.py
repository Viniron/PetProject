"""Ступень 5 разбора: категория моделью (Ф9, §15.4, ADR-055).

Модель здесь подставная и отвечает по сценарию: сеть в прогоне замокана
всегда (`CLAUDE.md`). Проверяется то, что ломается тихо:

- **ответ модели проверяет код**: чужой ключ, подкатегория чужой основной,
  новая категория без названия - операция без категории, а не догадка;
- **новые категории заводятся только вместе с операцией** и видны следующей
  порции - иначе пустые категории в наборе или падение на уникальном индексе;
- **модели уходит только неразобранный расход**: ручное «категории нет»,
  очередь разбора и исключённые не отправляются;
- **отказ модели - деградация, а не провал**: правила записаны, операции
  остались без категории, причина названа словами;
- **dry-run денег не тратит**: модель без `--apply` не зовётся.

Первая половина файла без базы, вторая - против настоящей Postgres на
обезличенной выгрузке Т-Банка за август.
"""

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import Стенд, байты_фикстуры
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.api.routes_finance import адаптеры_книжки
from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry, FinCategory, FinCategoryRule, FinTransaction
from jarvis_api.domain.finance_categorize_model import (
    Кандидат,
    КатегорияНабора,
    КатегорияОперации,
    РазборОпераций,
    описать_набор,
    описать_операции,
    повторы,
    прочитать_ответ,
)
from jarvis_api.integrations import llm
from jarvis_api.jobs.finance_categorize import описать, разобрать_книжку
from jarvis_api.jobs.finance_import import run_once as импорт_once
from jarvis_api.main import app

МОСКВА = ZoneInfo("Europe/Moscow")
АВГУСТ = dt.date(2026, 8, 1)
СЕЙЧАС = dt.datetime(2026, 9, 10, 9, 0, tzinfo=dt.UTC)

НАБОР = (
    КатегорияНабора(key="food", title="Еда"),
    КатегорияНабора(key="fastfood", title="Фастфуд", parent_key="food"),
    КатегорияНабора(key="transport", title="Транспорт"),
    КатегорияНабора(key="other", title="Остальное"),
)


def элемент(
    номер: int,
    category: str | None,
    category_title: str | None = None,
    subcategory: str | None = None,
    subcategory_title: str | None = None,
) -> КатегорияОперации:
    return КатегорияОперации(
        id=номер,
        category=category,
        category_title=category_title,
        subcategory=subcategory,
        subcategory_title=subcategory_title,
    )


def ответ_модели(*элементы: КатегорияОперации) -> РазборОпераций:
    return РазборОпераций(operations=list(элементы))


def кандидат(номер: int = 1, merchant: str | None = "Кофейня Альфа", **поля: Any) -> Кандидат:
    исходное: dict[str, Any] = {
        "id": номер,
        "occurred_at": dt.datetime(2026, 8, 15, 9, 0, tzinfo=dt.UTC),
        "amount": Decimal("-250.00"),
        "merchant": merchant,
        "bank_category": None,
        "mcc": None,
    }
    return Кандидат(**{**исходное, **поля})


# --- прочитать_ответ: ответ модели против набора месяца -----------------------


def test_ключ_из_набора_назначается_как_есть() -> None:
    итог = прочитать_ответ(
        ответ_модели(элемент(1, "transport"), элемент(2, "food", subcategory="fastfood")),
        [1, 2],
        НАБОР,
    )

    assert итог.назначения == {1: "transport", 2: "fastfood"}
    assert итог.новые == []
    assert итог.отклонено == {}


def test_не_нашла_категории_значит_без_категории() -> None:
    итог = прочитать_ответ(ответ_модели(элемент(1, None)), [1], НАБОР)

    assert итог.назначения == {}
    assert итог.отклонено == {1: "модель не нашла категории"}


def test_ключ_подкатегории_на_месте_основной_отклоняется() -> None:
    """«fastfood» как основная - это не новая категория и не «Еда»: догадка
    о том, что модель имела в виду, - ровно то, чего §15.4 не допускает."""
    итог = прочитать_ответ(ответ_модели(элемент(1, "fastfood")), [1], НАБОР)

    assert итог.назначения == {}
    assert "на месте основной" in итог.отклонено[1]


def test_подкатегория_чужой_основной_отклоняется() -> None:
    итог = прочитать_ответ(
        ответ_модели(элемент(1, "transport", subcategory="fastfood")), [1], НАБОР
    )

    assert итог.назначения == {}
    assert "не из категории" in итог.отклонено[1]


def test_основная_как_подкатегория_отклоняется() -> None:
    """Ключ основной «transport» под «Едой» - тот же ключ дважды в месяце,
    и правило по ключу стало бы неоднозначным."""
    итог = прочитать_ответ(ответ_модели(элемент(1, "food", subcategory="transport")), [1], НАБОР)

    assert итог.назначения == {}
    assert 1 in итог.отклонено


def test_новая_подкатегория_заводится_один_раз_на_две_операции() -> None:
    итог = прочитать_ответ(
        ответ_модели(
            элемент(1, "food", subcategory="cafe", subcategory_title="Кафе"),
            элемент(2, "food", subcategory="cafe", subcategory_title="Кафе"),
        ),
        [1, 2],
        НАБОР,
    )

    assert итог.назначения == {1: "cafe", 2: "cafe"}
    assert [(н.key, н.title, н.parent_key) for н in итог.новые] == [("cafe", "Кафе", "food")]


def test_новая_основная_с_новой_подкатегорией() -> None:
    итог = прочитать_ответ(
        ответ_модели(
            элемент(
                1,
                "health",
                category_title="Здоровье",
                subcategory="pharmacy",
                subcategory_title="Аптеки",
            )
        ),
        [1],
        НАБОР,
    )

    assert итог.назначения == {1: "pharmacy"}
    # Основная раньше подкатегории: подкатегории при записи нужен её id.
    assert [(н.key, н.parent_key) for н in итог.новые] == [
        ("health", None),
        ("pharmacy", "health"),
    ]


@pytest.mark.parametrize(
    ("ключ", "название", "причина"),
    [
        ("health", None, "без названия"),
        ("health", "   ", "без названия"),
        ("Health", "Здоровье", "не по форме"),
        ("здоровье", "Здоровье", "не по форме"),
        ("1health", "Здоровье", "не по форме"),
        ("h" * 65, "Здоровье", "не по форме"),
        ("eda", "еда", "повторяет название"),
    ],
)
def test_новая_категория_не_по_правилам_отклоняется(
    ключ: str, название: str | None, причина: str
) -> None:
    итог = прочитать_ответ(ответ_модели(элемент(1, ключ, category_title=название)), [1], НАБОР)

    assert итог.назначения == {}
    assert итог.новые == []
    assert причина in итог.отклонено[1]


def test_новая_основная_не_заводится_если_строку_отвергли_по_подкатегории() -> None:
    """Категория, ради которой строку отвергли, осталась бы в наборе пустой."""
    итог = прочитать_ответ(
        ответ_модели(элемент(1, "health", category_title="Здоровье", subcategory="pharmacy")),
        [1],
        НАБОР,
    )

    assert итог.новые == []
    assert итог.назначения == {}
    assert "без названия" in итог.отклонено[1]


def test_повтор_пропуск_и_чужой_id() -> None:
    итог = прочитать_ответ(
        ответ_модели(
            элемент(1, "transport"),
            элемент(1, "other"),
            элемент(99, "other"),
        ),
        [1, 2],
        НАБОР,
    )

    assert итог.назначения == {}
    assert итог.отклонено == {
        1: "модель назвала операцию дважды",
        2: "модель не ответила по операции",
    }


def test_ключ_без_учёта_краёв() -> None:
    итог = прочитать_ответ(ответ_модели(элемент(1, " transport ")), [1], НАБОР)

    assert итог.назначения == {1: "transport"}


# --- повторы и промпт --------------------------------------------------------


def test_мерчант_разобранный_моделью_второй_раз_не_отправляется() -> None:
    назначено, остальные = повторы(
        [кандидат(1, "  КОФЕЙНЯ альфа "), кандидат(2, "Кофейня Бета"), кандидат(3, None)],
        {"кофейня альфа": "fastfood"},
        НАБОР,
    )

    assert назначено == {1: "fastfood"}
    assert [к.id for к in остальные] == [2, 3]


def test_повтор_не_назначает_ключ_которого_нет_в_месяце() -> None:
    """Набор месяца свой (§15.4): категории модели из сентября в октябре
    может не быть, и мерчант уходит модели заново."""
    назначено, остальные = повторы([кандидат(1)], {"кофейня альфа": "cafe"}, НАБОР)

    assert назначено == {}
    assert [к.id for к in остальные] == [1]


def test_операции_в_промпте_с_датой_owner_и_суммой_строкой() -> None:
    """31 августа 22:30 UTC - уже 1 сентября у owner (инвариант 7)."""
    строка = описать_операции(
        [
            кандидат(
                7,
                'Кафе "Альфа"; зал 2',
                occurred_at=dt.datetime(2026, 8, 31, 22, 30, tzinfo=dt.UTC),
                amount=Decimal("-1234.50"),
                mcc="5814",
            )
        ],
        МОСКВА,
    )

    assert json.loads(строка) == {
        "id": 7,
        "date": "2026-09-01",
        "amount": "-1234.50",
        "merchant": 'Кафе "Альфа"; зал 2',
        "bank_category": None,
        "mcc": "5814",
    }


def test_набор_в_промпте_с_подкатегориями_под_основной() -> None:
    assert описать_набор(НАБОР) == (
        "- food: Еда\n  - fastfood: Фастфуд\n- other: Остальное\n- transport: Транспорт"
    )


def test_промпт_собирается_и_стабильная_часть_без_данных() -> None:
    """Стабильная часть одинакова байт в байт (CLAUDE.md): данные owner -
    только в переменной."""
    промпт = llm.собрать(
        "finance_categorize",
        month="2026-08",
        categories=описать_набор(НАБОР),
        operations=описать_операции([кандидат()], МОСКВА),
    )

    assert промпт.версия == 1
    assert "Кофейня Альфа" not in промпт.стабильная_часть
    assert "Кофейня Альфа" in промпт.переменная_часть
    assert "2026-08" in промпт.переменная_часть


# --- ступень 5 поверх базы ---------------------------------------------------

ФАЙЛ = "tbank_august.csv"

НАЗНАЧЕНИЕ = json.dumps(
    {
        "finance_categorize": {
            "provider": "провайдер-а",
            "model": "модель-1",
            "max_tokens": 4096,
            "price_in": "1",
            "price_out": "5",
            "temperature": "0",
            "fallback": [
                {
                    "provider": "провайдер-б",
                    "model": "модель-2",
                    "max_tokens": 4096,
                    "price_in": "0.25",
                    "price_out": "1.50",
                }
            ],
        }
    }
)

Ответчик = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


def всё_в(ключ: str, **поля: Any) -> Ответчик:
    """Сценарий: каждой операции запроса - одна и та же категория."""

    def ответить(операции: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": о["id"],
                "category": ключ,
                "category_title": None,
                "subcategory": None,
                "subcategory_title": None,
                **поля,
            }
            for о in операции
        ]

    return ответить


@dataclass
class Подставной:
    """Провайдер, отвечающий по операциям из запроса; помнит, что ушло."""

    имя: str
    ответчик: Ответчик | None = None
    сбой: Exception | None = None
    запросы: list[llm.Запрос] = field(default_factory=list)

    def выполнить(self, запрос: llm.Запрос) -> llm.Ответ:
        self.запросы.append(запрос)
        if self.сбой is not None:
            raise self.сбой
        assert self.ответчик is not None, "модель позвали, хотя сценарий этого не ждал"
        операции = [
            json.loads(строка)
            for строка in запрос.переменная_часть.splitlines()
            if строка.startswith("{")
        ]
        тело = {"operations": self.ответчик(операции)}
        return llm.Ответ(
            текст=json.dumps(тело, ensure_ascii=False), токенов_вход=800, токенов_выход=120
        )

    def отправленные(self) -> list[int]:
        return [
            json.loads(строка)["id"]
            for запрос in self.запросы
            for строка in запрос.переменная_часть.splitlines()
            if строка.startswith("{")
        ]


def настройки(**поправки: Any) -> Settings:
    """Назначение задачи и порция больше книжки: тест, которому важно число
    вызовов, задаёт порцию сам - иначе повтор по мерчанту между порциями
    (правильное поведение) делает счёт вызовов зависимым от выгрузки."""
    return Settings().model_copy(
        update={"llm_routing": НАЗНАЧЕНИЕ, "finance_categorize_chunk": 1000, **поправки}
    )


def адаптеры(*провайдеры: Подставной) -> dict[str, llm.Адаптер]:
    return {п.имя: п for п in провайдеры}


def загрузить_август(сессия: Session) -> None:
    assert импорт_once(сессия, [(ФАЙЛ, байты_фикстуры("statements", ФАЙЛ))], apply=True) == 0


def завести_набор(сессия: Session, *, origin: str = "owner") -> None:
    основная = FinCategory(
        key="food", period_month=АВГУСТ, level=1, title="Еда", origin=origin, status="active"
    )
    сессия.add(основная)
    сессия.add(
        FinCategory(
            key="other",
            period_month=АВГУСТ,
            level=1,
            title="Остальное",
            origin=origin,
            status="active",
        )
    )
    сессия.flush()


def без_категории(сессия: Session) -> list[FinTransaction]:
    return list(
        сессия.scalars(
            select(FinTransaction)
            .where(
                FinTransaction.kind == "expense",
                FinTransaction.category_id.is_(None),
                FinTransaction.category_source.is_(None),
                FinTransaction.needs_review.is_(False),
                FinTransaction.excluded.is_(False),
            )
            .order_by(FinTransaction.id)
        ).all()
    )


def ступень(
    сессия: Session,
    *провайдеры: Подставной,
    настр: Settings | None = None,
    apply: bool = True,
) -> Any:
    отчёт, _ = разобрать_книжку(
        сессия,
        настр or настройки(),
        apply=apply,
        адаптеры=адаптеры(*провайдеры),
        сейчас=СЕЙЧАС,
    )
    return отчёт


def test_модель_раскладывает_неразобранные_расходы(сессия: Session) -> None:
    загрузить_август(сессия)
    завести_набор(сессия)
    ждут = [строка.id for строка in без_категории(сессия)]
    assert ждут, "в августовской выгрузке есть расходы без правил"
    модель = Подставной("провайдер-а", всё_в("other"))

    отчёт = ступень(сессия, модель)

    assert sorted(модель.отправленные()) == ждут
    assert len(модель.запросы) == 1
    assert отчёт.модель.отказ is None
    assert отчёт.модель.назначено == len(ждут)
    другое = сессия.scalars(select(FinCategory).where(FinCategory.key == "other")).one()
    for номер in ждут:
        строка = сессия.get(FinTransaction, номер)
        assert строка is not None
        assert (строка.category_id, строка.category_source) == (другое.id, "model")
    assert без_категории(сессия) == []


def test_вызов_модели_пишется_в_аудит(сессия: Session) -> None:
    """Инвариант 8: каждый вызов модели - строка с провайдером и стоимостью."""
    загрузить_август(сессия)
    завести_набор(сессия)

    ступень(сессия, Подставной("провайдер-а", всё_в("other")))

    вызовы = сессия.scalars(
        select(AuditLogEntry).where(AuditLogEntry.target == "finance_categorize")
    ).all()
    assert len(вызовы) == 1
    assert вызовы[0].status == "ok"
    assert вызовы[0].provider == "провайдер-а"
    assert вызовы[0].cost_usd == Decimal("0.001400")


def test_dry_run_модель_не_зовёт_и_не_пишет(сессия: Session) -> None:
    загрузить_август(сессия)
    завести_набор(сессия)
    ждут = len(без_категории(сессия))
    модель = Подставной("провайдер-а", всё_в("other"))

    отчёт = ступень(сессия, модель, apply=False)

    assert модель.запросы == []
    assert отчёт.модель.запущена is False
    assert отчёт.модель.ждут == ждут
    assert len(без_категории(сессия)) == ждут
    assert any("только с --apply" in строка for строка in описать(отчёт, [], apply=False))


def test_модель_заводит_подкатегорию_с_происхождением_ai(сессия: Session) -> None:
    загрузить_август(сессия)
    завести_набор(сессия)

    ступень(
        сессия,
        Подставной("провайдер-а", всё_в("food", subcategory="cafe", subcategory_title="Кафе")),
    )

    кафе = сессия.scalars(select(FinCategory).where(FinCategory.key == "cafe")).one()
    еда = сессия.scalars(select(FinCategory).where(FinCategory.key == "food")).one()
    assert (кафе.origin, кафе.level, кафе.parent_id, кафе.period_month) == (
        "ai",
        2,
        еда.id,
        АВГУСТ,
    )
    # Основная owner не тронута: модель её не переименовывает и не заменяет.
    assert (еда.title, еда.origin) == ("Еда", "owner")


def test_новая_категория_первой_порции_видна_второй(сессия: Session) -> None:
    """Порция в две операции: вторая порция того же месяца получает «cafe»
    уже в наборе, и уникальный индекс не падает на повторной вставке."""
    загрузить_август(сессия)
    завести_набор(сессия)
    модель = Подставной("провайдер-а", всё_в("food", subcategory="cafe", subcategory_title="Кафе"))

    отчёт = ступень(сессия, модель, настр=настройки(finance_categorize_chunk=2))

    assert len(модель.запросы) >= 2
    # Мерчант первой порции во вторую не попадает: он уже разобран.
    assert len(модель.отправленные()) + отчёт.модель.повтором == отчёт.модель.ждут
    assert "cafe" in модель.запросы[1].переменная_часть
    assert отчёт.модель.новых_категорий == 1
    assert len(сессия.scalars(select(FinCategory).where(FinCategory.key == "cafe")).all()) == 1


def test_мерчант_разобранный_моделью_берётся_из_книжки(сессия: Session) -> None:
    загрузить_август(сессия)
    завести_набор(сессия)
    ступень(сессия, Подставной("провайдер-а", всё_в("other")))

    разобранные = сессия.scalars(
        select(FinTransaction)
        .where(FinTransaction.category_source == "model")
        .order_by(FinTransaction.id)
    ).all()
    по_мерчанту: dict[str | None, list[FinTransaction]] = {}
    for строка in разобранные:
        по_мерчанту.setdefault(строка.merchant, []).append(строка)
    пара = next(группа for группа in по_мерчанту.values() if len(группа) > 1)
    пара[1].category_id = None
    пара[1].category_source = None
    сессия.flush()

    # Ответчика нет: зов модели здесь - провал теста.
    молчащая = Подставной("провайдер-а")
    отчёт = ступень(сессия, молчащая)

    assert молчащая.запросы == []
    assert отчёт.модель.повтором == 1
    assert пара[1].category_id == пара[0].category_id
    assert пара[1].category_source == "model"


def test_правило_сильнее_модели(сессия: Session) -> None:
    """Ступени 1-4 точнее модели: правило, добавленное позже, переписывает
    её категорию при переразборе (§15.4)."""
    загрузить_август(сессия)
    завести_набор(сессия)
    ступень(сессия, Подставной("провайдер-а", всё_в("other")))
    строка = сессия.scalars(
        select(FinTransaction).where(
            FinTransaction.category_source == "model", FinTransaction.merchant.is_not(None)
        )
    ).first()
    assert строка is not None and строка.merchant is not None
    сессия.add(FinCategoryRule(rule_type="merchant", pattern=строка.merchant, category_key="food"))
    сессия.flush()

    ступень(сессия, Подставной("провайдер-а", всё_в("other")))

    еда = сессия.scalars(select(FinCategory).where(FinCategory.key == "food")).one()
    assert (строка.category_id, строка.category_source) == (еда.id, "merchant")


def test_модели_не_уходят_ручное_разбор_и_исключённое(сессия: Session) -> None:
    загрузить_август(сессия)
    завести_набор(сессия)
    ручная, в_разборе, исключённая, *остальные = без_категории(сессия)
    # «Категории здесь нет» - решение owner, а не пустое место (§15.4).
    ручная.category_source = "manual"
    в_разборе.needs_review = True
    исключённая.excluded = True
    сессия.flush()
    модель = Подставной("провайдер-а", всё_в("other"))

    ступень(сессия, модель)

    assert sorted(модель.отправленные()) == [строка.id for строка in остальные]
    assert ручная.category_id is None
    assert в_разборе.category_id is None
    assert исключённая.category_id is None


def test_месяц_без_набора_owner_модели_не_отправляется(сессия: Session) -> None:
    """Основные категории owner обязаны быть всегда: модель набор дополняет,
    но не строит с нуля. Набор из одних категорий модели - тоже «без набора»."""
    загрузить_август(сессия)
    завести_набор(сессия, origin="ai")
    модель = Подставной("провайдер-а", всё_в("other"))

    отчёт = ступень(сессия, модель)

    assert модель.запросы == []
    assert отчёт.модель.без_набора == отчёт.модель.ждут > 0
    assert any("без набора категорий owner" in с for с in описать(отчёт, [], apply=True))


def test_модель_не_назначена_разбор_правилами_записан(сессия: Session) -> None:
    """Пустое назначение - рабочее состояние (§15.6): операции видны
    неразобранными, правило отправителя при этом отработало."""
    загрузить_август(сессия)
    завести_набор(сессия)
    ждут = len(без_категории(сессия))

    отчёт = ступень(сессия, Подставной("провайдер-а"), настр=настройки(llm_routing=""))

    assert отчёт.модель.отказ == "модель для категорий не назначена"
    assert len(без_категории(сессия)) == ждут
    assert отчёт.без_категории >= ждут


def test_обе_модели_недоступны_операции_без_категории(сессия: Session) -> None:
    загрузить_август(сессия)
    завести_набор(сессия)
    ждут = len(без_категории(сессия))
    основная = Подставной("провайдер-а", сбой=llm.ПровайдерНедоступен("таймаут"))
    резерв = Подставной("провайдер-б", сбой=llm.ПровайдерНедоступен("таймаут"))

    отчёт = ступень(сессия, основная, резерв, настр=настройки(finance_categorize_chunk=2))

    assert отчёт.модель.отказ == "модели сейчас не ответили"
    # Первый отказ останавливает ступень: следующая порция упёрлась бы в то же.
    assert len(основная.запросы) == 1
    assert len(без_категории(сессия)) == ждут


def test_геоблок_называет_маршрут_платы(сессия: Session) -> None:
    загрузить_август(сессия)
    завести_набор(сессия)
    основная = Подставной("провайдер-а", сбой=llm.ВыходЗаблокирован("страна"))

    отчёт = ступень(сессия, основная, Подставной("провайдер-б"))

    assert отчёт.модель.отказ == "модели недоступны из сети платы"


def test_ответ_с_чужим_ключом_оставляет_операцию_без_категории(сессия: Session) -> None:
    загрузить_август(сессия)
    завести_набор(сессия)
    ждут = len(без_категории(сессия))

    отчёт = ступень(сессия, Подставной("провайдер-а", всё_в("Нечто")))

    assert отчёт.модель.моделью == 0
    assert sum(отчёт.модель.отклонено.values()) == ждут
    assert len(без_категории(сессия)) == ждут
    assert сессия.scalars(select(FinCategory).where(FinCategory.origin == "ai")).all() == []


# --- через HTTP ---------------------------------------------------------------


@pytest.fixture
def подставная_книжки(стенд: Стенд) -> Any:
    модель = Подставной("провайдер-а", всё_в("other"))
    app.dependency_overrides[адаптеры_книжки] = lambda: адаптеры(модель)
    try:
        yield модель
    finally:
        # Стенд ставит свою подмену «провайдеров нет»; вернуть её, а не снять:
        # фикстура стенда снимет сама.
        app.dependency_overrides[адаптеры_книжки] = lambda: {}


def test_переразбор_через_api_зовёт_модель_только_с_apply(
    стенд: Стенд, подставная_книжки: Подставной
) -> None:
    загрузить_август(стенд.сессия)
    завести_набор(стенд.сессия)
    # Коммит, а не flush: показ без записи откатывает сессию ручки, и
    # несохранённый набор исчез бы вместе с ним. Коммит снимает savepoint
    # фикстуры, внешняя транзакция всё равно откатывается после теста.
    стенд.сессия.commit()
    стенд.настройки = настройки()
    ждут = len(без_категории(стенд.сессия))

    показ = стенд.клиент.post("/api/finance/recategorize")
    assert показ.status_code == 200
    assert показ.json()["model_pending"] == ждут
    assert показ.json()["by_model"] == 0
    assert подставная_книжки.запросы == []

    запись = стенд.клиент.post("/api/finance/recategorize", params={"apply": "true"})
    assert запись.status_code == 200
    тело = запись.json()
    assert тело["by_model"] == ждут
    assert тело["model_error"] is None
    assert len(подставная_книжки.запросы) == 1


def test_без_провайдеров_api_называет_причину(стенд: Стенд) -> None:
    загрузить_август(стенд.сессия)
    завести_набор(стенд.сессия)
    стенд.настройки = настройки()

    ответ = стенд.клиент.post("/api/finance/recategorize", params={"apply": "true"})

    assert ответ.status_code == 200
    assert ответ.json()["by_model"] == 0
    assert ответ.json()["model_error"] is not None

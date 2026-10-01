"""Переход месяца: наследование набора и предложение модели (Ф10, §15.4, ADR-056).

Модель подставная и отвечает по сценарию: сеть в прогоне замокана всегда
(`CLAUDE.md`). Проверяется то, что ломается тихо:

- **месяц с операциями и без набора получает набор прошлого** - иначе разбор
  Ф9 пропустил бы его как «месяц без набора», и книжка ждала бы owner;
- **категории модели не наследуются**, принятое owner - наследуется;
- **ответ модели проверяет код**: занятый ключ, кривой ключ, «убрать»
  категорию с тратами - пункт отброшен с причиной;
- **модель зовётся раз на месяц**, а отказ месяц не отмечает - следующий
  импорт спросит снова;
- **принятое удаление снимает категорию с операций автоматики**, а ручную
  правку оставляет: разбор сам категорию не снимает.

Первая половина без базы, вторая - против настоящей Postgres.
"""

import datetime as dt
import itertools
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import Стенд, байты_фикстуры
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jarvis_api.api.routes_finance import адаптеры_книжки
from jarvis_api.config import Settings
from jarvis_api.db.models import (
    AuditLogEntry,
    FinAccount,
    FinCategory,
    FinTransaction,
    Setting,
)
from jarvis_api.domain.finance_proposals import (
    МесяцИстории,
    Основная,
    ОшибкаРешения,
    ПредложениеНабора,
    ПунктПредложения,
    Трата,
    ТратыКатегории,
    описать_историю,
    описать_набор,
    прочитать_предложение,
    решить,
    свести_историю,
)
from jarvis_api.integrations import llm
from jarvis_api.jobs.finance_import import run_once as импорт_once
from jarvis_api.jobs.finance_proposals import (
    ОтчётПерехода,
    ОтчётПредложения,
    история,
    описать,
    открыть_месяцы,
    предложить,
)
from jarvis_api.jobs.finance_proposals import run_once as команда_once
from jarvis_api.jobs.finance_taxonomy import унаследовать
from jarvis_api.main import app

МОСКВА = ZoneInfo("Europe/Moscow")
ИЮЛЬ = dt.date(2026, 7, 1)
АВГУСТ = dt.date(2026, 8, 1)
СЕНТЯБРЬ = dt.date(2026, 9, 1)
ОКТЯБРЬ = dt.date(2026, 10, 1)
# «Сейчас» - сентябрь: предложение спрашивается на месяц, который идёт.
СЕЙЧАС = dt.datetime(2026, 9, 10, 9, 0, tzinfo=dt.UTC)

НАБОР = (
    Основная(key="food", title="Еда", origin="owner"),
    Основная(key="transport", title="Транспорт", origin="owner"),
    Основная(key="gifts", title="Подарки", origin="owner"),
    Основная(key="cafe", title="Кафе", origin="ai"),
)


def пункт(action: str, key: str, title: str | None = None, reason: str = "так") -> ПунктПредложения:
    return ПунктПредложения(action=action, key=key, title=title, reason=reason)


def ответ(*пункты: ПунктПредложения) -> ПредложениеНабора:
    return ПредложениеНабора(proposals=list(пункты))


def месяц_с_тратами(*ключи: str) -> МесяцИстории:
    return МесяцИстории(
        month=АВГУСТ,
        categories=tuple(
            ТратыКатегории(key=к, title=к, origin="owner", spent=Decimal("100"), count=1)
            for к in ключи
        ),
        uncategorized_spent=Decimal("0"),
        uncategorized_count=0,
        merchants=(),
    )


# --- прочитать_предложение: ответ модели против набора ----------------------


def test_добавить_и_убрать_принимаются() -> None:
    итог = прочитать_предложение(
        ответ(
            пункт("add", "pharmacy", "Аптеки", "5 операций на 2 000 ₽"),
            пункт("remove", "gifts", reason="трат не было"),
        ),
        НАБОР,
        [к.key for к in НАБОР],
        [месяц_с_тратами("food")],
    )

    assert [(д.key, д.title, д.reason) for д in итог.добавить] == [
        ("pharmacy", "Аптеки", "5 операций на 2 000 ₽")
    ]
    assert [(у.key, у.reason) for у in итог.убрать] == [("gifts", "трат не было")]
    assert итог.отклонено == []


def test_менять_нечего_это_пустой_итог() -> None:
    итог = прочитать_предложение(ответ(), НАБОР, [], [месяц_с_тратами("food")])

    assert (итог.добавить, итог.убрать, итог.отклонено) == ([], [], [])


@pytest.mark.parametrize(
    ("пункт_", "причина"),
    [
        (пункт("add", "Аптеки", "Аптеки"), "не по форме"),
        (пункт("add", "food", "Еда-2"), "уже занят"),
        (пункт("add", "fastfood", "Фастфуд"), "уже занят"),
        (пункт("add", "pharmacy", None), "без названия"),
        (пункт("add", "pharmacy", "  "), "без названия"),
        (пункт("add", "meal", "еда"), "повторяет название"),
        (пункт("add", "pharmacy", "Аптеки", reason=" "), "без причины"),
        (пункт("add", "pharmacy", "Аптеки", reason="x" * 1001), "длиннее"),
        (пункт("rename", "food", "Еда"), "неизвестное действие"),
        (пункт("remove", "pharmacy"), "такой основной категории в наборе нет"),
        (пункт("remove", "cafe"), "заведена не owner"),
        (пункт("remove", "food"), "по ней были траты"),
    ],
)
def test_пункт_с_нарушением_отбрасывается(пункт_: ПунктПредложения, причина: str) -> None:
    """`fastfood` - подкатегория: ключ занят во всём месяце, а не только
    среди основных, иначе правило по ключу стало бы неоднозначным."""
    итог = прочитать_предложение(
        ответ(пункт_), НАБОР, ["food", "fastfood", "transport"], [месяц_с_тратами("food")]
    )

    assert итог.добавить == []
    assert итог.убрать == []
    assert len(итог.отклонено) == 1
    assert причина in итог.отклонено[0]


def test_ключ_дважды_второй_пункт_отброшен() -> None:
    итог = прочитать_предложение(
        ответ(пункт("add", "pharmacy", "Аптеки"), пункт("add", "pharmacy", "Аптека")),
        НАБОР,
        [],
        [месяц_с_тратами()],
    )

    assert [д.key for д in итог.добавить] == ["pharmacy"]
    assert "назван дважды" in итог.отклонено[0]


def test_два_новых_с_одним_названием_второй_отброшен() -> None:
    итог = прочитать_предложение(
        ответ(пункт("add", "pharmacy", "Аптеки"), пункт("add", "drugstore", "аптеки")),
        НАБОР,
        [],
        [месяц_с_тратами()],
    )

    assert [д.key for д in итог.добавить] == ["pharmacy"]
    assert "повторяет название" in итог.отклонено[0]


# --- история и промпт --------------------------------------------------------


def test_история_складывает_по_месяцу_и_ключу() -> None:
    траты = [
        Трата(month=АВГУСТ, key="food", amount=Decimal("100.50"), merchant="Кафе А"),
        Трата(month=АВГУСТ, key="food", amount=Decimal("49.50"), merchant="Кафе Б"),
        Трата(month=АВГУСТ, key=None, amount=Decimal("300"), merchant="Аптека"),
        Трата(month=АВГУСТ, key=None, amount=Decimal("10"), merchant="Ларёк"),
        Трата(month=АВГУСТ, key=None, amount=Decimal("5"), merchant=None),
        Трата(month=ИЮЛЬ, key="transport", amount=Decimal("65"), merchant="Метро"),
    ]
    наборы = {АВГУСТ: [НАБОР[0], НАБОР[2]], ИЮЛЬ: [НАБОР[1]]}

    итог = свести_историю(траты, наборы, мерчантов=1)

    assert [м.month for м in итог] == [ИЮЛЬ, АВГУСТ]
    август = итог[1]
    assert [(к.key, к.spent, к.count) for к in август.categories] == [
        ("food", Decimal("150.00"), 2),
        # Категория набора без трат - с нулём: на ней модель и предлагает убрать.
        ("gifts", Decimal("0"), 0),
    ]
    assert (август.uncategorized_spent, август.uncategorized_count) == (Decimal("315"), 3)
    assert август.merchants == ("Аптека",)


def test_месяц_без_трат_в_историю_не_попадает() -> None:
    """Пустой месяц прочитался бы как «по всем категориям ноль - убрать всё»."""
    assert свести_историю([], {АВГУСТ: list(НАБОР)}, мерчантов=10) == []


def test_промпт_держит_данные_в_переменной_части() -> None:
    """Инвариант кэша (§5.2): стабильная часть - строго в начале и без данных."""
    промпт = llm.собрать(
        "finance_categories",
        month="2026-09",
        categories=описать_набор(НАБОР),
        history=описать_историю(
            свести_историю(
                [Трата(month=АВГУСТ, key=None, amount=Decimal("300"), merchant="Аптека Альфа")],
                {},
                мерчантов=10,
            )
        ),
    )

    assert промпт.версия == 1
    assert "Аптека Альфа" not in промпт.стабильная_часть
    assert "Аптека Альфа" in промпт.переменная_часть
    assert "2026-09" in промпт.переменная_часть
    # Сумма строкой: Decimal в JSON-число стал бы двоичной дробью.
    assert '"spent": "300"' in промпт.переменная_часть


# --- поверх базы --------------------------------------------------------------

НАЗНАЧЕНИЕ = json.dumps(
    {
        "finance_categories": {
            "provider": "провайдер-а",
            "model": "модель-1",
            "max_tokens": 2048,
            "price_in": "2",
            "price_out": "10",
        }
    }
)


@dataclass
class Модель:
    """Провайдер с заготовленным ответом; помнит, что ушло."""

    имя: str = "провайдер-а"
    тело: dict[str, Any] | None = None
    сбой: Exception | None = None
    запросы: list[llm.Запрос] = field(default_factory=list)

    def выполнить(self, запрос: llm.Запрос) -> llm.Ответ:
        self.запросы.append(запрос)
        if self.сбой is not None:
            raise self.сбой
        assert self.тело is not None, "модель позвали, хотя сценарий этого не ждал"
        return llm.Ответ(
            текст=json.dumps(self.тело, ensure_ascii=False), токенов_вход=900, токенов_выход=150
        )


def предлагает(*пункты: dict[str, Any]) -> Модель:
    return Модель(тело={"proposals": list(пункты)})


def настройки() -> Settings:
    return Settings().model_copy(update={"llm_routing": НАЗНАЧЕНИЕ})


def адаптеры(модель: Модель) -> dict[str, llm.Адаптер]:
    return {модель.имя: модель}


def категория(
    сессия: Session,
    месяц: dt.date,
    key: str,
    title: str,
    *,
    origin: str = "owner",
    parent: FinCategory | None = None,
    status: str = "active",
) -> FinCategory:
    строка = FinCategory(
        key=key,
        period_month=месяц,
        level=1 if parent is None else 2,
        parent_id=parent.id if parent is not None else None,
        parent_level=1 if parent is not None else None,
        title=title,
        origin=origin,
        status=status,
    )
    сессия.add(строка)
    сессия.flush()
    return строка


# Отпечаток уникален в банке: две строки с одним - это одна операция дважды.
_номера = itertools.count(1)


def расход(
    сессия: Session,
    момент: dt.datetime,
    сумма: str,
    *,
    категория_: FinCategory | None = None,
    source: str | None = None,
    merchant: str = "Магазин",
) -> FinTransaction:
    значение = Decimal(сумма)
    строка = FinTransaction(
        bank="tbank",
        account="Карта",
        occurred_at=момент,
        amount=значение,
        amount_rub=значение,
        currency="RUB",
        kind="expense",
        merchant=merchant,
        category_id=категория_.id if категория_ is not None else None,
        category_source=source if source is not None else ("merchant" if категория_ else None),
        fingerprint=f"f10-{next(_номера)}".ljust(64, "0"),
    )
    сессия.add(строка)
    сессия.flush()
    return строка


def август_с_набором(сессия: Session) -> dict[str, FinCategory]:
    """Август: набор owner, траты по «Еде», «Подарки» без трат."""
    сессия.add(FinAccount(bank="tbank", name="Карта", role="checking"))
    еда = категория(сессия, АВГУСТ, "food", "Еда")
    фастфуд = категория(сессия, АВГУСТ, "fastfood", "Фастфуд", parent=еда)
    подарки = категория(сессия, АВГУСТ, "gifts", "Подарки")
    расход(сессия, dt.datetime(2026, 8, 5, 9, 0, tzinfo=dt.UTC), "-500.00", категория_=фастфуд)
    расход(сессия, dt.datetime(2026, 8, 6, 9, 0, tzinfo=dt.UTC), "-300.00", merchant="Аптека")
    return {"food": еда, "fastfood": фастфуд, "gifts": подарки}


def набор(сессия: Session, месяц: dt.date) -> dict[str, FinCategory]:
    return {
        строка.key: строка
        for строка in сессия.scalars(
            select(FinCategory).where(FinCategory.period_month == месяц)
        ).all()
    }


def отметка(сессия: Session) -> dt.date | None:
    строка = сессия.get(Setting, 1)
    return строка.finance_proposed_month if строка is not None else None


# --- наследование -------------------------------------------------------------


def test_месяц_с_операциями_получает_набор_прошлого(сессия: Session) -> None:
    август_с_набором(сессия)
    расход(сессия, dt.datetime(2026, 9, 3, 9, 0, tzinfo=dt.UTC), "-120.00")

    открыты = открыть_месяцы(сессия, МОСКВА)

    assert открыты == [(СЕНТЯБРЬ, АВГУСТ, 3)]
    сентябрь = набор(сессия, СЕНТЯБРЬ)
    assert set(сентябрь) == {"food", "fastfood", "gifts"}
    assert сентябрь["fastfood"].parent_id == сентябрь["food"].id


def test_месяц_без_операций_не_открывается(сессия: Session) -> None:
    """Без выписки месяцу разбирать нечего, и набор ему не нужен."""
    август_с_набором(сессия)

    assert открыть_месяцы(сессия, МОСКВА) == []
    assert набор(сессия, СЕНТЯБРЬ) == {}


def test_два_месяца_сразу_открываются_цепочкой(сессия: Session) -> None:
    август_с_набором(сессия)
    расход(сессия, dt.datetime(2026, 9, 3, 9, 0, tzinfo=dt.UTC), "-120.00")
    расход(сессия, dt.datetime(2026, 10, 3, 9, 0, tzinfo=dt.UTC), "-80.00")

    открыты = открыть_месяцы(сессия, МОСКВА)

    assert [(м, откуда) for м, откуда, _ in открыты] == [(СЕНТЯБРЬ, АВГУСТ), (ОКТЯБРЬ, СЕНТЯБРЬ)]


def test_месяц_раньше_первого_набора_не_открывается(сессия: Session) -> None:
    август_с_набором(сессия)
    расход(сессия, dt.datetime(2026, 7, 3, 9, 0, tzinfo=dt.UTC), "-120.00")

    assert открыть_месяцы(сессия, МОСКВА) == []


def test_категории_модели_не_наследуются(сессия: Session) -> None:
    """Решение owner 2026-10-01: категория модели живёт месяц. Подкатегория
    owner под основной модели тоже не переносится - сиротой её схема не пустит."""
    август = август_с_набором(сессия)
    кафе = категория(сессия, АВГУСТ, "cafe", "Кафе", origin="ai")
    категория(сессия, АВГУСТ, "coffee", "Кофейни", parent=кафе)
    категория(сессия, АВГУСТ, "bakery", "Пекарни", origin="ai", parent=август["food"])
    категория(сессия, АВГУСТ, "pharmacy", "Аптеки", origin="ai", status="proposed")
    категория(сессия, АВГУСТ, "books", "Книги", status="rejected")

    отчёт = унаследовать(сессия, АВГУСТ, СЕНТЯБРЬ, apply=True)

    assert set(набор(сессия, СЕНТЯБРЬ)) == {"food", "fastfood", "gifts"}
    assert отчёт.унаследовано == 3


def test_наследование_без_записи_считает_то_же(сессия: Session) -> None:
    август = август_с_набором(сессия)
    категория(сессия, АВГУСТ, "bakery", "Пекарни", origin="ai", parent=август["food"])

    отчёт = унаследовать(сессия, АВГУСТ, СЕНТЯБРЬ, apply=False)

    assert отчёт.унаследовано == 3
    assert набор(сессия, СЕНТЯБРЬ) == {}


def test_импорт_открывает_месяц_до_разбора(сессия: Session) -> None:
    """Справка Ozon за 16.08-16.09: сентябрь приходит с ней впервые и
    получает августовский набор в той же записи импорта."""
    август_с_набором(сессия)

    код = импорт_once(
        сессия,
        [("ozon_2026-09.pdf", байты_фикстуры("statements", "ozon_2026-09.pdf"))],
        apply=True,
    )

    assert код == 0
    сентябрьские = [
        строка
        for строка in сессия.scalars(select(FinTransaction)).all()
        if строка.occurred_at.astimezone(МОСКВА).date() >= СЕНТЯБРЬ
    ]
    assert сентябрьские, "в справке есть сентябрьские операции"
    assert set(набор(сессия, СЕНТЯБРЬ)) == {"food", "fastfood", "gifts"}


# --- предложение ------------------------------------------------------------


def сентябрь_открыт(сессия: Session) -> dict[str, FinCategory]:
    август_с_набором(сессия)
    расход(сессия, dt.datetime(2026, 9, 3, 9, 0, tzinfo=dt.UTC), "-120.00")
    открыть_месяцы(сессия, МОСКВА)
    return набор(сессия, СЕНТЯБРЬ)


def спросить(сессия: Session, модель: Модель | None) -> ОтчётПредложения:
    return предложить(
        сессия,
        настройки(),
        адаптеры=адаптеры(модель) if модель is not None else None,
        сейчас=СЕЙЧАС,
        зона=МОСКВА,
    )


def test_предложение_кладёт_пункты_и_отмечает_месяц(сессия: Session) -> None:
    сентябрь_открыт(сессия)
    модель = предлагает(
        {"action": "add", "key": "pharmacy", "title": "Аптеки", "reason": "300 ₽ в августе"},
        {"action": "remove", "key": "gifts", "title": None, "reason": "трат не было"},
    )

    отчёт = спросить(сессия, модель)

    assert (отчёт.добавить, отчёт.убрать, отчёт.отказ) == (1, 1, None)
    сентябрь = набор(сессия, СЕНТЯБРЬ)
    аптеки = сентябрь["pharmacy"]
    assert (аптеки.status, аптеки.origin, аптеки.level) == ("proposed", "ai", 1)
    assert аптеки.proposal_reason == "300 ₽ в августе"
    # Убрать - признаком на унаследованной строке: до решения она работает.
    assert (сентябрь["gifts"].status, сентябрь["gifts"].remove_proposed) == ("active", True)
    assert отметка(сессия) == СЕНТЯБРЬ
    assert len(модель.запросы) == 1
    # История - прошлые месяцы, а не текущий: в запросе август.
    assert "2026-08" in модель.запросы[0].переменная_часть
    строки = описать(ОтчётПерехода(предложение=отчёт))
    assert any("ждут решения owner" in строка for строка in строки)


def test_вызов_пишется_в_аудит(сессия: Session) -> None:
    """Инвариант 8: каждый вызов модели - строка с провайдером и стоимостью."""
    сентябрь_открыт(сессия)

    спросить(сессия, предлагает())

    вызовы = сессия.scalars(
        select(AuditLogEntry).where(AuditLogEntry.target == "finance_categories")
    ).all()
    assert len(вызовы) == 1
    assert вызовы[0].provider == "провайдер-а"
    # 900 входа по $2 и 150 выхода по $10 за миллион.
    assert вызовы[0].cost_usd == Decimal("0.003300")


def test_месяц_спрашивается_один_раз(сессия: Session) -> None:
    """Ответ «менять нечего» строк не оставляет - держит отметка в settings."""
    сентябрь_открыт(сессия)
    спросить(сессия, предлагает())
    второй = предлагает()

    отчёт = спросить(сессия, второй)

    assert отчёт.пропуск == "уже предложено"
    assert второй.запросы == []


def test_отказ_модели_месяц_не_отмечает(сессия: Session) -> None:
    """Деградация (§15.6): набор работает, а следующий импорт спросит снова."""
    сентябрь_открыт(сессия)

    отчёт = спросить(сессия, Модель(сбой=llm.ВыходЗаблокирован("страна")))

    assert отчёт.отказ == "модели недоступны из сети платы"
    assert отметка(сессия) is None
    assert set(набор(сессия, СЕНТЯБРЬ)) == {"food", "fastfood", "gifts"}
    повтор = предлагает()
    спросить(сессия, повтор)
    assert len(повтор.запросы) == 1
    assert отметка(сессия) == СЕНТЯБРЬ


def test_не_назначенная_модель_называется_словами(сессия: Session) -> None:
    сентябрь_открыт(сессия)

    отчёт = предложить(
        сессия,
        Settings().model_copy(update={"llm_routing": "{}"}),
        адаптеры=адаптеры(предлагает()),
        сейчас=СЕЙЧАС,
        зона=МОСКВА,
    )

    assert отчёт.отказ == "модель для набора категорий не назначена"


def test_без_истории_модель_не_зовётся(сессия: Session) -> None:
    категория(сессия, СЕНТЯБРЬ, "food", "Еда")
    модель = предлагает()

    отчёт = спросить(сессия, модель)

    assert отчёт.пропуск is not None
    assert "прошлых месяцев с тратами нет" in отчёт.пропуск
    assert модель.запросы == []
    assert отметка(сессия) is None


def test_не_открытый_месяц_модели_не_отдаётся(сессия: Session) -> None:
    """Первые дни месяца: выписка принесла только прошлый."""
    август_с_набором(сессия)
    модель = предлагает()

    отчёт = спросить(сессия, модель)

    assert отчёт.пропуск is not None
    assert "ещё не открыт" in отчёт.пропуск
    assert модель.запросы == []


def test_месяц_без_набора_owner_модели_не_отдаётся(сессия: Session) -> None:
    """Модель дополняет набор, но не строит его (ADR-055)."""
    август_с_набором(сессия)
    категория(сессия, СЕНТЯБРЬ, "cafe", "Кафе", origin="ai")
    модель = предлагает()

    отчёт = спросить(сессия, модель)

    assert отчёт.пропуск == "у месяца нет набора owner"
    assert модель.запросы == []


def test_ответ_с_нарушениями_кладёт_только_верное(сессия: Session) -> None:
    сентябрь_открыт(сессия)

    отчёт = спросить(
        сессия,
        предлагает(
            {"action": "add", "key": "food", "title": "Еда", "reason": "дубль"},
            {"action": "remove", "key": "fastfood", "title": None, "reason": "подкатегория"},
            {"action": "add", "key": "pharmacy", "title": "Аптеки", "reason": "300 ₽"},
        ),
    )

    assert (отчёт.добавить, отчёт.убрать) == (1, 0)
    assert len(отчёт.отклонено) == 2
    assert набор(сессия, СЕНТЯБРЬ)["food"].remove_proposed is False


def test_история_сводит_подкатегорию_в_основную(сессия: Session) -> None:
    сентябрь_открыт(сессия)

    прошлое = история(сессия, СЕНТЯБРЬ, МОСКВА, месяцев=3, мерчантов=10)

    assert [м.month for м in прошлое] == [АВГУСТ]
    траты = {к.key: (к.spent, к.count) for к in прошлое[0].categories}
    assert траты == {"food": (Decimal("500.00"), 1), "gifts": (Decimal("0"), 0)}
    assert прошлое[0].merchants == ("Аптека",)


def test_история_берёт_только_то_что_идёт_в_счёт(сессия: Session) -> None:
    август_с_набором(сессия)
    for строка in сессия.scalars(select(FinTransaction)).all():
        строка.excluded = True
    сессия.flush()

    assert история(сессия, СЕНТЯБРЬ, МОСКВА, месяцев=3, мерчантов=10) == []


# --- решение owner ----------------------------------------------------------


def предложена_аптека(сессия: Session) -> FinCategory:
    сентябрь_открыт(сессия)
    спросить(
        сессия,
        предлагает({"action": "add", "key": "pharmacy", "title": "Аптеки", "reason": "300 ₽"}),
    )
    return набор(сессия, СЕНТЯБРЬ)["pharmacy"]


def test_принятая_категория_становится_категорией_owner(сессия: Session) -> None:
    аптеки = предложена_аптека(сессия)

    решение = решить(сессия, аптеки.id, принять=True)

    assert (решение.категория.status, решение.категория.origin) == ("active", "owner")
    # И со следующего месяца наследуется наравне с прочими.
    унаследовать(сессия, СЕНТЯБРЬ, ОКТЯБРЬ, apply=True)
    assert "pharmacy" in набор(сессия, ОКТЯБРЬ)


def test_отклонённое_добавление_не_наследуется(сессия: Session) -> None:
    аптеки = предложена_аптека(сессия)

    решить(сессия, аптеки.id, принять=False)

    assert аптеки.status == "rejected"
    унаследовать(сессия, СЕНТЯБРЬ, ОКТЯБРЬ, apply=True)
    assert "pharmacy" not in набор(сессия, ОКТЯБРЬ)


def test_принятое_удаление_снимает_категорию_автоматики(сессия: Session) -> None:
    """Разбор сам категорию не снимает - операции остались бы в категории,
    которой в наборе нет. Ручная правка остаётся: она сильнее всех ступеней."""
    сентябрь = сентябрь_открыт(сессия)
    подарки = сентябрь["gifts"]
    подарки.remove_proposed = True
    открытки = категория(сессия, СЕНТЯБРЬ, "cards", "Открытки", parent=подарки)
    по_правилу = расход(
        сессия, dt.datetime(2026, 9, 4, 9, 0, tzinfo=dt.UTC), "-50.00", категория_=подарки
    )
    моделью = расход(
        сессия,
        dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.UTC),
        "-60.00",
        категория_=открытки,
        source="model",
    )
    руками = расход(
        сессия,
        dt.datetime(2026, 9, 6, 9, 0, tzinfo=dt.UTC),
        "-70.00",
        категория_=подарки,
        source="manual",
    )

    решение = решить(сессия, подарки.id, принять=True)

    assert (решение.снято, решение.оставлено_ручных) == (2, 1)
    assert (по_правилу.category_id, по_правилу.category_source) == (None, None)
    assert (моделью.category_id, моделью.category_source) == (None, None)
    assert руками.category_id == подарки.id
    # Подкатегория уходит вместе с родителем: разбор держит в наборе всё active.
    assert (подарки.status, открытки.status) == ("rejected", "rejected")


def test_отклонённое_удаление_оставляет_категорию(сессия: Session) -> None:
    подарки = сентябрь_открыт(сессия)["gifts"]
    подарки.remove_proposed = True

    решить(сессия, подарки.id, принять=False)

    assert (подарки.status, подарки.remove_proposed) == ("active", False)


def test_не_решённое_удаление_в_новый_месяц_без_признака(сессия: Session) -> None:
    """Вопрос был про сентябрь; в октябре категория - снова просто категория."""
    сентябрь_открыт(сессия)["gifts"].remove_proposed = True
    сессия.flush()

    унаследовать(сессия, СЕНТЯБРЬ, ОКТЯБРЬ, apply=True)

    assert набор(сессия, ОКТЯБРЬ)["gifts"].remove_proposed is False


def test_решать_нечего_это_отказ(сессия: Session) -> None:
    еда = сентябрь_открыт(сессия)["food"]

    with pytest.raises(ОшибкаРешения) as обычная:
        решить(сессия, еда.id, принять=True)
    with pytest.raises(ОшибкаРешения) as нет:
        решить(сессия, 10**9, принять=True)

    assert обычная.value.код == "не_предложение"
    assert нет.value.код == "нет_категории"


def test_подкатегории_признак_убрать_не_ставится(сессия: Session) -> None:
    """CHECK `remove_main_only`: убирается основная, подкатегория - с ней."""
    сентябрь_открыт(сессия)["fastfood"].remove_proposed = True

    with pytest.raises(IntegrityError, match="remove_main_only"):
        сессия.flush()


# --- команда ----------------------------------------------------------------


def test_команда_без_apply_решения_не_пишет(сессия: Session) -> None:
    подарки = сентябрь_открыт(сессия)["gifts"]
    подарки.remove_proposed = True
    сессия.commit()

    код = команда_once(сессия, месяц=СЕНТЯБРЬ, принять=подарки.id, отклонить=None, apply=False)

    assert код == 0
    сессия.expire_all()
    assert набор(сессия, СЕНТЯБРЬ)["gifts"].status == "active"


def test_команда_с_apply_пишет_решение(сессия: Session) -> None:
    подарки = сентябрь_открыт(сессия)["gifts"]
    подарки.remove_proposed = True
    сессия.commit()

    код = команда_once(сессия, месяц=СЕНТЯБРЬ, принять=подарки.id, отклонить=None, apply=True)

    assert код == 0
    assert набор(сессия, СЕНТЯБРЬ)["gifts"].status == "rejected"


def test_команда_отказ_решения_код_один(сессия: Session) -> None:
    еда = сентябрь_открыт(сессия)["food"]

    assert команда_once(сессия, месяц=None, принять=еда.id, отклонить=None, apply=True) == 1


# --- через HTTP ---------------------------------------------------------------


def test_решение_через_api(стенд: Стенд) -> None:
    подарки = сентябрь_открыт(стенд.сессия)["gifts"]
    подарки.remove_proposed = True
    стенд.сессия.flush()

    ответ_ = стенд.клиент.post(
        f"/api/finance/categories/{подарки.id}/decision", json={"accept": True}
    )

    assert ответ_.status_code == 200
    тело = ответ_.json()
    assert тело["category"]["status"] == "rejected"
    assert тело["category"]["remove_proposed"] is True
    assert тело["cleared"] == 0


def test_решение_по_обычной_категории_409(стенд: Стенд) -> None:
    еда = сентябрь_открыт(стенд.сессия)["food"]

    ответ_ = стенд.клиент.post(f"/api/finance/categories/{еда.id}/decision", json={"accept": True})

    assert ответ_.status_code == 409
    assert ответ_.json()["code"] == "не_предложение"


def test_решение_по_несуществующей_404(стенд: Стенд) -> None:
    ответ_ = стенд.клиент.post("/api/finance/categories/999999999/decision", json={"accept": False})

    assert ответ_.status_code == 404


def test_импорт_через_api_отдаёт_переход_месяца(стенд: Стенд) -> None:
    август_с_набором(стенд.сессия)
    стенд.сессия.commit()
    стенд.сейчас = СЕЙЧАС
    стенд.настройки = настройки()
    модель = предлагает(
        {"action": "add", "key": "pharmacy", "title": "Аптеки", "reason": "300 ₽ в августе"}
    )
    app.dependency_overrides[адаптеры_книжки] = lambda: адаптеры(модель)

    файл = байты_фикстуры("statements", "ozon_2026-09.pdf")
    ответ_ = стенд.клиент.post(
        "/api/finance/import",
        params={"apply": "true"},
        files=[("files", ("ozon_2026-09.pdf", файл, "application/pdf"))],
    )

    assert ответ_.status_code == 200
    переход = ответ_.json()["month_transition"]
    assert переход["opened"] == [
        {"month": "2026-09-01", "inherited_from": "2026-08-01", "categories": 3}
    ]
    assert (переход["proposal_month"], переход["proposed_add"]) == ("2026-09-01", 1)
    assert переход["proposal_error"] is None
    assert len(модель.запросы) == 1

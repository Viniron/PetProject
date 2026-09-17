"""Набор категорий месяца и правила разбора (Ф4а, §15.4, §15.5).

Файл набора - ввод owner, такой же, как выписка, и проверяется тем же
способом: отказ целиком с указанием, что именно не так. Молча применённый
наполовину набор хуже отказа - правило, указывающее в пустоту, не падает,
а просто не срабатывает, и «почему категория не проставилась» owner
обнаружит через месяц.

Имена в фикстуре - заглушки обезличенной выгрузки («Алексей А.»), а не
настоящий список родных owner: он персональные данные и в репозитории
не лежит (ADR-019).
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURES_DIR
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.db.models import FinCategory, FinCategoryRule
from jarvis_api.jobs.finance_taxonomy import (
    ОшибкаНабора,
    ФайлНабора,
    run_once,
    применить_файл,
    прочитать,
    унаследовать,
)

АВГУСТ = dt.date(2026, 8, 1)
СЕНТЯБРЬ = dt.date(2026, 9, 1)
ФАЙЛ = FIXTURES_DIR / "finance" / "taxonomy_august.json"


def набор(**переопределения: Any) -> dict[str, Any]:
    """Минимальный корректный набор, который тест портит по одному месту."""
    основа: dict[str, Any] = {
        "month": "2026-08",
        "categories": [{"key": "food", "title": "Еда"}],
        "rules": [],
    }
    основа.update(переопределения)
    return основа


def разобрать_набор(данные: dict[str, Any]) -> ФайлНабора:
    return ФайлНабора.model_validate(данные)


# --- проверка файла без базы -----------------------------------------------


def test_фикстура_owner_читается() -> None:
    прочитанный = прочитать(ФАЙЛ)

    assert прочитанный.месяц == АВГУСТ
    assert [к.key for к in прочитанный.categories] == [
        "transfers",
        "transport",
        "food",
        "marketplaces",
        "other",
    ]
    еда = next(к for к in прочитанный.categories if к.key == "food")
    assert [п.key for п in еда.children] == ["fastfood", "canteen", "supermarket"]


@pytest.mark.parametrize(
    "правило",
    [
        pytest.param({"type": "sender", "pattern": "Алексей А."}, id="отправитель-без-вида"),
        pytest.param(
            {"type": "sender", "pattern": "Алексей А.", "kind": "income", "category": "food"},
            id="отправитель-с-категорией",
        ),
        pytest.param({"type": "merchant", "pattern": "Фастфуд"}, id="мерчант-без-категории"),
        pytest.param(
            {"type": "self", "pattern": "Роман В.", "kind": "income"},
            id="своё-имя-с-видом",
        ),
        pytest.param({"type": "выдумка", "pattern": "х", "category": "food"}, id="чужой-тип"),
    ],
)
def test_правило_не_той_формы_отказ(правило: dict[str, Any]) -> None:
    """Правило, которое ничего не определяет, молча не срабатывает - значит

    оно ловится на вводе, где ошибка ещё лечится правкой файла.
    """
    with pytest.raises(ValidationError):
        разобрать_набор(набор(rules=[правило]))


def test_лишний_ключ_в_файле_отказ() -> None:
    """Опечатка в имени поля - отказ, а не тихо проигнорированное поле."""
    with pytest.raises(ValidationError):
        разобрать_набор(набор(categories=[{"key": "food", "title": "Еда", "цвет": "красный"}]))


def test_повторяющийся_ключ_отказ(tmp_path: Path) -> None:
    """Ключ уникален в месяце целиком: правило по нему указывало бы на две."""
    путь = tmp_path / "набор.json"
    путь.write_text(
        json.dumps(
            набор(
                categories=[
                    {"key": "food", "title": "Еда", "children": [{"key": "food", "title": "Ещё"}]}
                ]
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ОшибкаНабора, match="повторяются"):
        прочитать(путь)


def test_правило_мимо_набора_отказ(tmp_path: Path) -> None:
    путь = tmp_path / "набор.json"
    путь.write_text(
        json.dumps(набор(rules=[{"type": "merchant", "pattern": "х", "category": "нетакой"}])),
        encoding="utf-8",
    )

    with pytest.raises(ОшибкаНабора, match="которых нет в наборе"):
        прочитать(путь)


def test_не_json_отказ(tmp_path: Path) -> None:
    путь = tmp_path / "набор.json"
    путь.write_text("{категории:", encoding="utf-8")

    with pytest.raises(ОшибкаНабора, match="не читается как JSON"):
        прочитать(путь)


def test_месяц_не_того_вида_отказ() -> None:
    with pytest.raises(ОшибкаНабора, match="не месяц"):
        _ = разобрать_набор(набор(month="август")).месяц


# --- применение против настоящей базы --------------------------------------


def категории(сессия: Session, месяц: dt.date) -> list[FinCategory]:
    return list(
        сессия.scalars(
            select(FinCategory)
            .where(FinCategory.period_month == месяц)
            .order_by(FinCategory.level, FinCategory.key)
        ).all()
    )


def test_файл_заводит_категории_и_правила(сессия: Session) -> None:
    код = run_once(сессия, путь=ФАЙЛ, наследовать_из=None, месяц=None, apply=True)

    assert код == 0
    строки = категории(сессия, АВГУСТ)
    assert len(строки) == 8  # пять основных и три подкатегории «Еды»
    еда = next(с for с in строки if с.key == "food")
    дети = [с for с in строки if с.parent_id == еда.id]
    assert {с.key for с in дети} == {"fastfood", "canteen", "supermarket"}
    assert all(с.level == 2 and с.parent_level == 1 for с in дети)
    assert all(с.origin == "owner" and с.status == "active" for с in строки)

    правила = list(сессия.scalars(select(FinCategoryRule)).all())
    assert len(правила) == 6
    своё = next(п for п in правила if п.rule_type == "self")
    assert (своё.category_key, своё.kind) == (None, None)
    родной = next(п for п in правила if п.rule_type == "sender")
    assert родной.kind == "income" and родной.title == "родной"


def test_тот_же_файл_второй_раз_ничего_не_меняет(сессия: Session) -> None:
    run_once(сессия, путь=ФАЙЛ, наследовать_из=None, месяц=None, apply=True)
    было = len(категории(сессия, АВГУСТ))

    отчёт = применить_файл(сессия, прочитать(ФАЙЛ), apply=True)

    assert (отчёт.категорий_новых, отчёт.правил_новых, отчёт.правил_изменено) == (0, 0, 0)
    assert len(категории(сессия, АВГУСТ)) == было


def test_dry_run_не_пишет_ни_строки(сессия: Session) -> None:
    код = run_once(сессия, путь=ФАЙЛ, наследовать_из=None, месяц=None, apply=False)

    assert код == 0
    assert категории(сессия, АВГУСТ) == []
    assert сессия.scalars(select(FinCategoryRule)).all() == []


def test_переименование_категории_не_заводит_вторую(сессия: Session) -> None:
    """Ключ - то, что в категории постоянно; имя owner вправе поменять."""
    run_once(сессия, путь=ФАЙЛ, наследовать_из=None, месяц=None, apply=True)
    было = len(категории(сессия, АВГУСТ))

    переименованный = прочитать(ФАЙЛ)
    переименованный.categories[1].title = "Дорога"
    отчёт = применить_файл(сессия, переименованный, apply=True)

    assert (отчёт.категорий_новых, отчёт.категорий_переименовано) == (0, 1)
    assert len(категории(сессия, АВГУСТ)) == было
    транспорт = next(с for с in категории(сессия, АВГУСТ) if с.key == "transport")
    assert транспорт.title == "Дорога"


def test_наследование_копирует_набор_с_подкатегориями(сессия: Session) -> None:
    run_once(сессия, путь=ФАЙЛ, наследовать_из=None, месяц=None, apply=True)

    код = run_once(сессия, путь=None, наследовать_из=АВГУСТ, месяц=СЕНТЯБРЬ, apply=True)

    assert код == 0
    сентябрьские = категории(сессия, СЕНТЯБРЬ)
    assert {с.key for с in сентябрьские} == {с.key for с in категории(сессия, АВГУСТ)}
    еда = next(с for с in сентябрьские if с.key == "food")
    дети = [с for с in сентябрьские if с.parent_id is not None]
    # Родитель - сентябрьская «Еда», а не августовская: подкатегория
    # не может принадлежать категории другого месяца, это держит база.
    assert дети and all(с.parent_id == еда.id for с in дети)


def test_наследование_не_лезет_в_заполненный_месяц(сессия: Session) -> None:
    """«Дополню, чего не хватает» однажды вернуло бы убранную категорию."""
    run_once(сессия, путь=ФАЙЛ, наследовать_из=None, месяц=None, apply=True)
    сессия.add(
        FinCategory(
            key="other",
            period_month=СЕНТЯБРЬ,
            level=1,
            title="Остальное",
            origin="owner",
            status="active",
        )
    )
    сессия.flush()

    отчёт = унаследовать(сессия, АВГУСТ, СЕНТЯБРЬ, apply=True)

    assert отчёт.унаследовано == 0
    assert отчёт.заметки and "уже заполнен" in отчёт.заметки[0]
    assert len(категории(сессия, СЕНТЯБРЬ)) == 1


def test_наследование_без_месяца_приёмника_отказ(сессия: Session) -> None:
    код = run_once(сессия, путь=None, наследовать_из=АВГУСТ, месяц=None, apply=True)

    assert код == 1


def test_без_аргументов_отказ(сессия: Session) -> None:
    """Пустой вызов - обычная опечатка, и отвечать он должен по-человечески."""
    assert run_once(сессия, путь=None, наследовать_из=None, месяц=None, apply=True) == 1

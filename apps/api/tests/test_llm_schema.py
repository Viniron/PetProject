"""Схема ядра в форму провайдера (Э12б, ADR-049).

Проверяется то, что ломается тихо. Схема, уехавшая без `additionalProperties`,
не отказывает - она просто перестаёт быть гарантией, и обнаружится это первым
ответом с лишним полем. Схема со ссылкой `$ref`, уехавшая к Google как есть,
не отказывает тоже - модель ответит чем-нибудь, и виноватой будет выглядеть
модель.
"""

import pytest
from pydantic import BaseModel, Field

from jarvis_api.integrations.llm.schema import (
    СхемаНеПоддерживается,
    для_anthropic,
    для_google,
)


class Place(BaseModel):
    """Вложенная модель: ради неё Pydantic заводит `$defs` и `$ref`."""

    name: str
    address: str | None = None


class Событие(BaseModel):
    title: str
    starts_at: str | None = None
    confidence: float = Field(ge=0, le=1)
    location: Place | None = None


# --- Anthropic --------------------------------------------------------------


def test_запрет_лишних_полей_везде() -> None:
    """`strict: true` не включается без него - и гарантия схемы исчезает."""
    готовая = для_anthropic(Событие.model_json_schema())

    assert готовая["additionalProperties"] is False
    вложенная = готовая["$defs"]["Place"]
    assert вложенная["additionalProperties"] is False


def test_ссылки_anthropic_не_разворачиваются() -> None:
    """Провайдер понимает `$ref` сам: разворачивать значит чинить целое."""
    готовая = для_anthropic(Событие.model_json_schema())
    assert "$defs" in готовая


def test_исходная_схема_не_портится() -> None:
    """Схема принадлежит точке вызова, и правка её на месте испортила бы её всем."""
    исходная = Событие.model_json_schema()
    для_anthropic(исходная)
    assert "additionalProperties" not in исходная


# --- Google -----------------------------------------------------------------


def test_ссылки_разворачиваются_на_месте() -> None:
    """Подмножество Google ссылок не знает; `$defs` у него нет вовсе."""
    готовая = для_google(Событие.model_json_schema())

    assert "$defs" not in готовая
    место = готовая["properties"]["location"]
    assert место["properties"]["name"]["type"] == "STRING"


def test_необязательное_поле_становится_nullable() -> None:
    """`str | None` Pydantic пишет как `anyOf` с `null` - у Google это флаг.

    Оставленная ветка `null` сделала бы недействительной всю схему, а не
    одно поле.
    """
    готовая = для_google(Событие.model_json_schema())
    поле = готовая["properties"]["starts_at"]

    assert поле["nullable"] is True
    assert поле["type"] == "STRING"
    assert "anyOf" not in поле


def test_чужие_ключи_убраны() -> None:
    """`additionalProperties` у Google не значит ничего и только обманывает."""
    готовая = для_google(Событие.model_json_schema())

    assert "additionalProperties" not in готовая
    assert "title" not in готовая
    assert "$schema" not in готовая


def test_типы_заглавными() -> None:
    """В подмножестве это перечисление, и строчное написание не гарантировано."""
    готовая = для_google(Событие.model_json_schema())

    assert готовая["type"] == "OBJECT"
    assert готовая["properties"]["title"]["type"] == "STRING"
    assert готовая["properties"]["confidence"]["type"] == "NUMBER"


def test_обязательные_поля_сохраняются() -> None:
    """`required` - единственное, чем схема вообще на что-то влияет у Google."""
    готовая = для_google(Событие.model_json_schema())
    assert "title" in готовая["required"]


def test_ссылка_наружу_отвергается_громко() -> None:
    """Молча урезанная схема выглядела бы виной модели, а не потерянным ключом."""
    with pytest.raises(СхемаНеПоддерживается, match="наружу"):
        для_google({"type": "object", "properties": {"x": {"$ref": "http://чужое/схема"}}})


def test_схема_ссылающаяся_на_себя_не_вешает_разбор() -> None:
    """Зацикленная схема - ошибка точки вызова, и узнать о ней надо до вызова."""
    закольцованная = {
        "$defs": {"Узел": {"type": "object", "properties": {"дальше": {"$ref": "#/$defs/Узел"}}}},
        "$ref": "#/$defs/Узел",
    }
    with pytest.raises(СхемаНеПоддерживается, match="глубже"):
        для_google(закольцованная)

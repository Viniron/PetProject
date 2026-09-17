"""Сигнал внешнему сторожу (Э9, ADR-043).

Сеть не задействована: `urlopen` подменяется. Проверяется то, из-за чего
сторож перестаёт что-либо значить, - согласие с чужим отказом. Сторож,
ответивший 404 на удалённый монитор, обязан считаться неполученным
сигналом, а не успехом.
"""

import urllib.error
import urllib.request
from typing import Any

import pytest

from jarvis_api.integrations import heartbeat


class Ответ:
    """Минимум от объекта `urlopen`: код и протокол контекстного менеджера."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "Ответ":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def подменить(monkeypatch: pytest.MonkeyPatch, ответ: Any) -> list[str]:
    """Возвращает список адресов, на которые ушли запросы."""
    адреса: list[str] = []

    def urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        адреса.append(request.full_url)
        if isinstance(ответ, Exception):
            raise ответ
        return ответ

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return адреса


def test_успешный_ping_уходит_по_адресу(monkeypatch: pytest.MonkeyPatch) -> None:
    адреса = подменить(monkeypatch, Ответ(200))

    heartbeat.отправить("https://hc.test/uuid", timeout=5, имя="проверка")

    assert адреса == ["https://hc.test/uuid"]


def test_отказ_сторожа_не_считается_успехом(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 - это удалённый монитор: сигнал уходит в никуда, и молчать об этом нельзя."""
    подменить(monkeypatch, Ответ(404))

    with pytest.raises(heartbeat.HeartbeatError, match="404"):
        heartbeat.отправить("https://hc.test/uuid", timeout=5, имя="проверка")


def test_недоступный_сторож_поднимает_свой_тип(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свой тип, а не URLError: вызывающие ловят его каждый по-своему."""
    подменить(monkeypatch, urllib.error.URLError("нет сети"))

    with pytest.raises(heartbeat.HeartbeatError, match="ping не дошёл"):
        heartbeat.отправить("https://hc.test/uuid", timeout=5, имя="проверка")


def test_пустой_адрес_отвергается_до_запроса(monkeypatch: pytest.MonkeyPatch) -> None:
    """Решение «сторож не настроен» принимает вызывающий, и оно у всех разное."""
    адреса = подменить(monkeypatch, Ответ(200))

    with pytest.raises(heartbeat.HeartbeatError, match="адрес сторожа пуст"):
        heartbeat.отправить("", timeout=5, имя="проверка")

    assert адреса == []


def test_имя_сигнала_попадает_в_текст_ошибки(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сторожей на плате три, и по тексту в журнале должно быть видно, чей молчит."""
    подменить(monkeypatch, Ответ(500))

    with pytest.raises(heartbeat.HeartbeatError, match="проверка восстановления"):
        heartbeat.отправить("https://hc.test/uuid", timeout=5, имя="проверка восстановления")

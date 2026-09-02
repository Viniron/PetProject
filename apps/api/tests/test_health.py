"""Тесты Э0. Проверяют ровно две вещи: процесс жив и время tz-aware.

Имена по-русски - так же, как комментарии в design/tokens.css. Префикс
test_ обязателен, его требует pytest, дальше читаемая часть.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from jarvis_api import __version__
from jarvis_api.main import app

client = TestClient(app)


def test_health_отвечает_и_без_настроенной_базы() -> None:
    """Живость не зависит от DATABASE_URL - иначе нечем проверять Э1."""
    ответ = client.get("/health")

    assert ответ.status_code == 200
    тело = ответ.json()
    assert тело["status"] == "ok"
    assert тело["version"] == __version__


def test_health_отдаёт_время_с_таймзоной_в_utc() -> None:
    """Инвариант 7: наивных datetime нет, в обмене только UTC.

    Проверяется не наличие подстроки, а разбор в объект со смещением:
    строка вида '...T12:00:00' распарсилась бы, но дала бы tzinfo is None.
    """
    момент = datetime.fromisoformat(client.get("/health").json()["time"])

    assert момент.tzinfo is not None
    assert момент.utcoffset() == timedelta(0)

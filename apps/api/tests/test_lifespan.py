"""Подъём процесса API вместе с планировщиком (Э5).

Проверяется одно свойство, и оно дороже остальных: **старт не зависит
от базы**. uvicorn не принимает соединений, пока startup не закончился,
а недоступная база не бросает исключение - она висит в таймауте сокета.
Обращение к ней в lifespan означало бы молчащий `/health`, проваленный
healthcheck и бесконечный перезапуск контейнера по `restart: unless-stopped`.

`with TestClient(app)` выполняет lifespan через anyio-портал Starlette,
поэтому ни pytest-asyncio, ни новых зависимостей здесь не нужно.
Модульный `TestClient(app)` в test_health.py при этом lifespan не запускает
и работает как прежде.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jarvis_api.config import get_settings
from jarvis_api.main import app


@pytest.fixture
def чистые_настройки(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """`get_settings` кэширован на процесс - кэш надо чистить с обеих сторон.

    Без очистки на входе тест не увидит своих переменных, без очистки
    на выходе отравит соседние модули: порядок тестов не гарантирован,
    а Settings строят почти все.
    """
    get_settings.cache_clear()
    # Пустая база - не крайний случай, а условие проверки: с настоящей
    # строкой подключения догоняющий запуск ушёл бы в живой ИСУ и Google.
    monkeypatch.setenv("DATABASE_URL", "")
    yield monkeypatch
    get_settings.cache_clear()
    app.state.планировщик = None


def test_выключенный_планировщик_не_поднимается(чистые_настройки: pytest.MonkeyPatch) -> None:
    чистые_настройки.setenv("SCHEDULER_ENABLED", "false")

    with TestClient(app) as клиент:
        assert клиент.get("/health").status_code == 200
        assert getattr(app.state, "планировщик", None) is None


def test_health_отвечает_при_недоступной_базе(чистые_настройки: pytest.MonkeyPatch) -> None:
    """Планировщик включён, базы нет - процесс обязан подняться и отвечать."""
    чистые_настройки.setenv("SCHEDULER_ENABLED", "true")

    with TestClient(app) as клиент:
        ответ = клиент.get("/health")

    assert ответ.status_code == 200
    assert ответ.json()["status"] == "ok"


def test_повторный_вход_не_плодит_планировщиков(чистые_настройки: pytest.MonkeyPatch) -> None:
    """Второй планировщик рядом с живым - это два реконсила в один календарь."""
    чистые_настройки.setenv("SCHEDULER_ENABLED", "true")

    with TestClient(app):
        первый = app.state.планировщик
        assert первый is not None
        with TestClient(app):
            assert app.state.планировщик is первый

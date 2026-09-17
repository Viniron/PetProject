"""Фикстуры тестов, которым нужна настоящая Postgres.

SQLite здесь не годится принципиально: проверяются `timestamptz`, `jsonb`,
`bytea`, частичный уникальный индекс и каскад по внешнему ключу - то есть
ровно то, чего у SQLite либо нет, либо оно ведёт себя иначе. Тест на
подменённой базе доказывал бы, что схема верна для базы, которой у нас нет.

Базы нет - тесты пропускаются с внятной причиной, а не падают: `pytest -ra`
(включён в pyproject) печатает её в сводке, поэтому «зелёный прогон,
в котором ничего не проверено» не выглядит зелёным.
"""

import datetime as dt
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from jarvis_api.api.deps import сейчас as зависимость_сейчас
from jarvis_api.config import Settings, get_settings
from jarvis_api.db.session import get_engine, get_sessionmaker, session_scope
from jarvis_api.main import app

API_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def загрузить_фикстуру(*части: str) -> Any:
    """Читает JSON-фикстуру ответа внешней системы.

    Отдельной функцией, а не фикстурой pytest, чтобы её можно было звать
    внутри параметризации - там фикстуры недоступны.
    """
    путь = FIXTURES_DIR.joinpath(*части)
    return json.loads(путь.read_text(encoding="utf-8"))


def байты_фикстуры(*части: str) -> bytes:
    """Читает фикстуру как есть, байтами.

    Байтами, а не текстом: выписка банка приезжает файлом, и её кодировка -
    часть формата, которую разбор обязан проверять сам. Прочитать её здесь
    текстом значило бы проверять разбор уже раскодированного, то есть
    не проверять кодировку вовсе.
    """
    return FIXTURES_DIR.joinpath(*части).read_bytes()


# Та самая база из infra/docker-compose.dev.yml. Значение продублировано
# осознанно: `make db-up && make test` должно работать без настройки
# окружения, а пароль тестовой базы не секрет и быть им не может -
# она эфемерная, живёт в tmpfs и слушает только loopback.
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://jarvis:jarvis-test-not-a-secret@127.0.0.1:55432/jarvis_test"
)


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL тестовой базы. В CI приходит из окружения, локально - по умолчанию."""
    return os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL


@lru_cache(maxsize=1)
def _причина_недоступности(url: str) -> str | None:
    """Одна попытка соединиться на весь прогон, с коротким таймаутом.

    Кэш здесь не оптимизация: без него каждый пропущенный тест ждал бы
    таймаута заново, и прогон без базы занимал бы минуты вместо секунд.
    """
    проба = create_engine(url, connect_args={"connect_timeout": 3})
    try:
        with проба.connect():
            return None
    except OperationalError as ошибка:
        return str(ошибка)
    finally:
        проба.dispose()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """Подключение к тестовой базе; её нет - пропускаем всё, что от неё зависит."""
    причина = _причина_недоступности(database_url)
    if причина is not None:
        pytest.skip(
            f"тестовая база недоступна ({database_url}): подними её `make db-up`; {причина}"
        )

    построенный = create_engine(database_url)
    yield построенный
    построенный.dispose()


def _alembic_config(connection: Connection) -> Config:
    """Конфиг Alembic поверх уже открытого соединения.

    Соединение передаётся через `attributes`, а не строкой подключения:
    иначе миграция шла бы по второму соединению, вне транзакции теста,
    и её было бы нечем откатить.
    """
    config = Config(str(API_DIR / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="session")
def конфиг_alembic() -> Callable[[Connection], Config]:
    """Фабрика конфигов Alembic для тестов, которым нужно гонять миграции."""
    return _alembic_config


@pytest.fixture(scope="session")
def схема(engine: Engine) -> Iterator[None]:
    """Схема, накатанная миграциями, - и только ими (инвариант хоста 3).

    `Base.metadata.create_all` здесь не используется намеренно: он проверял
    бы модели, а на Pi схему создаёт Alembic. Расхождение между ними - тот
    самый случай, ради которого этот этап и делается.
    """
    from alembic import command

    with engine.begin() as connection:
        # База могла остаться грязной после упавшего прогона. Чистим до, а не
        # после: следы неудачи нужны для разбора, а не для следующего теста.
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    with engine.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")

    yield


@pytest.fixture
def сессия(engine: Engine, схема: None) -> Iterator[Session]:
    """Сессия в транзакции, которая всегда откатывается.

    Тесты не убирают за собой руками: строки, вставленные одним, не должны
    попадаться на глаза другому - иначе порядок выполнения начинает влиять
    на результат.
    """
    with engine.connect() as connection:
        транзакция = connection.begin()
        # create_savepoint: тесты проверяют ограничения, то есть намеренно
        # ловят IntegrityError и делают rollback. Без вложенной транзакции
        # этот rollback откатывал бы внешнюю - ту, которой фикстура убирает
        # за тестом, - и SQLAlchemy справедливо ругался бы.
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session
        транзакция.rollback()


@dataclass
class Стенд:
    """Клиент API вместе с тем, что тест вправе подменить.

    Поля читаются подменами **в момент вызова**, а не при создании стенда,
    поэтому `стенд.сейчас = ...` в теле теста работает: иначе пришлось бы
    заводить по фикстуре на каждый нужный момент времени.
    """

    клиент: TestClient
    сессия: Session
    сейчас: dt.datetime
    настройки: Settings


@pytest.fixture
def стенд(сессия: Session) -> Iterator[Стенд]:
    """Клиент FastAPI поверх откатываемой сессии.

    Три тонкости, каждая из которых уже ломала бы прогон.

    **`TestClient(app)` без `with`.** Контекстный менеджер выполняет
    lifespan, то есть поднимает APScheduler - против тестовой базы, а его
    догоняющий запуск ушёл бы в живой ИСУ и живой Google. Lifespan запускает
    только `test_lifespan.py`, и делает это осознанно.

    **Коммит эндпоинта безопасен** ровно благодаря `join_transaction_mode=
    "create_savepoint"` в фикстуре `сессия`: `commit()` снимает savepoint,
    внешняя транзакция остаётся, и `транзакция.rollback()` убирает за тестом.
    «Упрощение» фикстуры до обычной сессии превратит каждый тест записи
    в утечку строк, которая проявится в другом модуле.

    **Подмены снимаются через `pop`, а не `clear()`.** Объект `app` один на
    весь прогон, и `clear()` унёс бы чужие подмены вместе со своими.
    """
    стенд = Стенд(
        клиент=TestClient(app),
        сессия=сессия,
        сейчас=dt.datetime.now(dt.UTC),
        настройки=Settings(),
    )

    def подменить_сессию() -> Iterator[Session]:
        # Генератор, а не lambda: FastAPI разбирает зависимость по её виду,
        # и функция, возвращающая итератор, была бы подставлена как значение.
        # Сессия здесь не закрывается - ею владеет фикстура `сессия`.
        yield стенд.сессия

    app.dependency_overrides[session_scope] = подменить_сессию
    app.dependency_overrides[зависимость_сейчас] = lambda: стенд.сейчас
    app.dependency_overrides[get_settings] = lambda: стенд.настройки
    try:
        yield стенд
    finally:
        for зависимость in (session_scope, зависимость_сейчас, get_settings):
            app.dependency_overrides.pop(зависимость, None)


@pytest.fixture
def клиент_без_базы(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Клиент с пустым DATABASE_URL: проверка отказа `database_not_configured`.

    Единственное место в прогоне, где чистятся кэши `lru_cache`, и чистить
    надо **все три** и **с обеих сторон**: без очистки на входе тест не
    увидит своей переменной, без очистки на выходе оставит в кэше движок
    с пустым URL и сломает все последующие тесты базы - причём падение
    будет зависеть от порядка их выполнения.
    """

    def сбросить_кэши() -> None:
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()

    сбросить_кэши()
    monkeypatch.setenv("DATABASE_URL", "")
    try:
        yield TestClient(app)
    finally:
        сбросить_кэши()

"""Фикстуры тестов, которым нужна настоящая Postgres.

SQLite здесь не годится принципиально: проверяются `timestamptz`, `jsonb`,
`bytea`, частичный уникальный индекс и каскад по внешнему ключу - то есть
ровно то, чего у SQLite либо нет, либо оно ведёт себя иначе. Тест на
подменённой базе доказывал бы, что схема верна для базы, которой у нас нет.

Базы нет - тесты пропускаются с внятной причиной, а не падают: `pytest -ra`
(включён в pyproject) печатает её в сводке, поэтому «зелёный прогон,
в котором ничего не проверено» не выглядит зелёным.
"""

import json
import os
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

API_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def загрузить_фикстуру(*части: str) -> Any:
    """Читает JSON-фикстуру ответа внешней системы.

    Отдельной функцией, а не фикстурой pytest, чтобы её можно было звать
    внутри параметризации - там фикстуры недоступны.
    """
    путь = FIXTURES_DIR.joinpath(*части)
    return json.loads(путь.read_text(encoding="utf-8"))


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

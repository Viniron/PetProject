"""Окружение Alembic.

Три способа получить подключение, в порядке убывания приоритета:

1. готовое соединение в `config.attributes["connection"]` - так миграции
   гоняют тесты, не поднимая второй пул к той же базе;
2. `-x url=...` в командной строке - разовый прогон против другой базы;
3. `DATABASE_URL` из окружения (инвариант хоста 2).

Строки подключения в `alembic.ini` нет ни в каком виде: файл лежит
в публичном репозитории (ADR-019).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from alembic import context
from sqlalchemy import Connection, create_engine

from jarvis_api.config import get_settings
from jarvis_api.db.base import Base

# Импорт моделей нужен ради побочного эффекта: без него в metadata пусто
# и autogenerate «обнаружит» удаление всех таблиц.
from jarvis_api.db import models  # noqa: F401  isort:skip

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    """URL для прогона. Пустой не подставляется - падаем громко."""
    from_command_line = context.get_x_argument(as_dictionary=True).get("url")
    url = from_command_line or get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL пуст: миграции некуда применять (см. .env.example)")
    return url


@contextmanager
def _connection() -> Iterator[Connection]:
    """Соединение снаружи, если дано, иначе своё."""
    external = config.attributes.get("connection")
    if external is not None:
        yield external
        return

    engine = create_engine(_url(), poolclass=None)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def run_migrations_offline() -> None:
    """Генерация SQL без подключения - `alembic upgrade head --sql`."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Обычный прогон против живой базы."""
    with _connection() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Смена типа колонки иначе проходит мимо autogenerate, и схема
            # тихо расходится с моделями - ровно то, что `alembic check`
            # должен ловить.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

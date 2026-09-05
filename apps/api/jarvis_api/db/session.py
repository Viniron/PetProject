"""Подключение к базе: engine и фабрика сессий.

**Движок синхронный.** Пользователь один, параллельных запросов почти нет,
а асинхронный драйвер стоил бы второго набора правил (async-фикстуры
в тестах, async-контекст в джобах, отдельный путь в Alembic) без выигрыша,
который на одном пользователе нечем измерить.

Отсюда правило, которое надо помнить в FastAPI: эндпоинт, ходящий в базу,
объявляется `def`, а не `async def`. Синхронный запрос внутри `async def`
блокирует цикл событий целиком; `def` FastAPI уводит в пул потоков сам.
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from jarvis_api.config import get_settings


class DatabaseNotConfigured(RuntimeError):
    """DATABASE_URL пуст. Отдельный тип, чтобы отличать от отказа самой базы."""


@lru_cache
def get_engine() -> Engine:
    """Единственный engine на процесс: пул соединений смысла дублировать нет."""
    url = get_settings().database_url
    if not url:
        # Громко и сразу. Пустая строка подключения даёт ошибку драйвера,
        # по которой не понять, что дело в незаполненном .env.
        raise DatabaseNotConfigured(
            "DATABASE_URL пуст - заполните его в .env (см. .env.example)",
        )
    return create_engine(
        url,
        # Pi перезагружается, база вместе с ним: без проверки соединения
        # первый запрос после её перезапуска падает на протухшем сокете.
        pool_pre_ping=True,
        # Больше и не нужно: один пользователь, джобы последовательные.
        pool_size=5,
        max_overflow=5,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    """Фабрика сессий. Без autoflush - записи должны быть явными."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def session_scope() -> Iterator[Session]:
    """Сессия на один запрос или один шаг джоба.

    Коммита здесь нет намеренно: границу транзакции ставит вызывающий код.
    Логически цельная запись - одна транзакция (§11.2), и решить, что цельно,
    может только он.
    """
    with get_sessionmaker()() as session:
        yield session

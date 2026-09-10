"""Тесты миграций. Проверяют то, от чего зависит восстановление базы.

Инвариант хоста 3 требует, чтобы структуру базы можно было повторить на
чистом хосте одним `alembic upgrade head` - перед заливкой дампа. Значит
проверяется не «миграция написана», а «накатывается на пустую базу,
откатывается дочиста и накатывается снова».
"""

from collections.abc import Callable

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, inspect, text

# Девять таблиц календарного релиза. Список зафиксирован тестом намеренно:
# курсовые таблицы SPEC §9 сюда не входят, и добавление таблицы «по дороге»
# должно быть заметным решением, а не побочным эффектом чужой правки.
ТАБЛИЦЫ_КАЛЕНДАРЯ = {
    "audit_log",
    "calendar_events",
    "capture_blobs",
    "capture_drafts",
    "day_flags",
    "integration_tokens",
    "itmo_lessons",
    "job_runs",
    "settings",
}

# Шесть таблиц финансовой книжки (Ф1, §15.2). Отдельным множеством, а не
# дописанные к предыдущему: дорожки Э и Ф идут параллельно и закрываются
# по отдельности, и по этому списку видно, где кончается одна и начинается
# другая. Курсовых таблиц по-прежнему ноль.
ТАБЛИЦЫ_КНИЖКИ = {
    "fin_accounts",
    "fin_categories",
    "fin_category_rules",
    "fin_imports",
    "fin_summaries",
    "fin_transactions",
}

ОЖИДАЕМЫЕ_ТАБЛИЦЫ = ТАБЛИЦЫ_КАЛЕНДАРЯ | ТАБЛИЦЫ_КНИЖКИ


def таблицы(engine: Engine) -> set[str]:
    """Таблицы схемы public без служебной таблицы версий Alembic."""
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_схема_после_upgrade_содержит_ровно_таблицы_релиза(engine: Engine, схема: None) -> None:
    """Объём схемы v1 - календарь и книжка, без курсов (ADR-019, ADR-020)."""
    assert таблицы(engine) == ОЖИДАЕМЫЕ_ТАБЛИЦЫ


def test_миграции_откатываются_дочиста_и_накатываются_снова(
    engine: Engine,
    схема: None,
    конфиг_alembic: Callable[[Connection], Config],
) -> None:
    """Откат обязан быть полным.

    Недоснесённая таблица обнаруживается только на чистом хосте в момент
    восстановления - то есть в худший из возможных моментов.
    """
    with engine.begin() as connection:
        command.downgrade(конфиг_alembic(connection), "base")

    assert таблицы(engine) == set()

    with engine.begin() as connection:
        command.upgrade(конфиг_alembic(connection), "head")

    assert таблицы(engine) == ОЖИДАЕМЫЕ_ТАБЛИЦЫ


def test_модели_и_миграция_не_разошлись(
    engine: Engine,
    схема: None,
    конфиг_alembic: Callable[[Connection], Config],
) -> None:
    """`alembic check`: autogenerate не находит незаписанных изменений.

    Расхождение схемы с моделями иначе живёт до первого запроса в проде:
    код ждёт колонку, которой в базе нет, потому что миграцию забыли.
    """
    with engine.begin() as connection:
        command.check(конфиг_alembic(connection))


def test_проба_бэкапа_с_э1_удаляется_первой_миграцией(
    engine: Engine,
    схема: None,
    конфиг_alembic: Callable[[Connection], Config],
) -> None:
    """`restore_probe` не должна пережить Э2.

    Таблица создавалась скриптом мимо Alembic (решение owner на Э1) и в prod
    осталась бы навсегда: autogenerate видел бы её как лишнюю при каждой
    следующей миграции.
    """
    with engine.begin() as connection:
        command.downgrade(конфиг_alembic(connection), "base")
        connection.execute(text("CREATE TABLE restore_probe (id integer primary key)"))

    with engine.begin() as connection:
        command.upgrade(конфиг_alembic(connection), "head")

    assert таблицы(engine) == ОЖИДАЕМЫЕ_ТАБЛИЦЫ

"""Базовый класс моделей и общие типы колонок."""

from datetime import datetime
from typing import Annotated

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Шаблоны имён ограничений. Без них Postgres придумывает имя сам, и оно
# не воспроизводится: миграция, созданная на одной базе, не сможет удалить
# ограничение на другой. Инвариант хоста 3 - схема повторяется на чистом
# хосте - без детерминированных имён не выполняется.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Единственный способ объявить время в этой схеме. Инвариант 7 (в БД только
# UTC, наивных datetime нет) держится не на внимательности автора модели:
# timestamptz приводит любое записанное значение к UTC, а timestamp без
# зоны молча сохранил бы то, что пришло. Проверяется тестом по
# information_schema - колонок без зоны в схеме быть не должно.
Timestamp = Annotated[datetime, mapped_column(DateTime(timezone=True))]

# То же самое для «когда создана строка»: значение ставит сервер базы,
# а не приложение, - иначе сдвинутые часы контейнера (а на Pi нет RTC,
# см. Э1) размечают историю неверно.
CreatedAt = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), server_default=func.now()),
]


class Base(DeclarativeBase):
    """Общий предок моделей. Держит metadata, с которой сверяется Alembic."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

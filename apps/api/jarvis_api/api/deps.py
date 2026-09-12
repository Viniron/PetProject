"""Зависимости эндпоинтов: сессия, настройки, «сейчас».

Все три - зависимости, а не прямые вызовы внутри функции, и каждая по своей
причине.

**Сессия** - готовый `session_scope` из `db/session.py`, без обёртки. Обёртка
под другим именем сломала бы подмену в тестах молча: `dependency_overrides`
ищет по самому объекту зависимости, и тест, подменивший `session_scope`,
получил бы эндпоинт, продолжающий ходить в настоящую базу.

**Настройки** - через `Depends`, никогда прямым вызовом `get_settings()`.
Функция кэширована на процесс (`lru_cache`), и тест, меняющий настройку,
иначе обязан был бы чистить кэш с обеих сторон и отравлял бы соседние модули.
Через зависимость кэш не мешает вовсе.

**«Сейчас»** - отдельной зависимостью, чтобы «сегодня», границы недели и порог
давности проверялись без библиотеки замораживания времени: новую зависимость
без спроса добавлять нельзя, а проверять эти вещи обязательно.
"""

import datetime as dt
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.session import session_scope


def сейчас() -> dt.datetime:
    """Текущий момент, tz-aware и в UTC (инвариант 7).

    Наивный `datetime.now()` здесь поймал бы линтер (правило DTZ), а не
    ревью: «сегодня» у owner считается переводом этого момента в его зону,
    и наивное время сдвинуло бы день на плате, живущей в UTC.
    """
    return dt.datetime.now(dt.UTC)


Сессия = Annotated[Session, Depends(session_scope)]
Настройки = Annotated[Settings, Depends(get_settings)]
Сейчас = Annotated[dt.datetime, Depends(сейчас)]

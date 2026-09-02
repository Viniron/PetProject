"""Конфигурация. Единственный источник значений - переменные окружения.

Инвариант хоста 2: платформенных API хостера на Pi нет, а самодельных
конфигов в файлах на диске быть не должно - всё, что приложение пишет мимо
Postgres, не попадёт в pg_dump и не восстановится (инвариант хоста 1).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки процесса. Читаются один раз при первом обращении."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    env: str = "dev"
    # На Э0 базы ещё нет, и это рабочее состояние, а не ошибка конфигурации:
    # /health обязан отвечать до того, как DATABASE_URL заполнен, иначе
    # нечем проверять поднятие стека на Э1.
    database_url: str = ""


@lru_cache
def get_settings() -> Settings:
    """Кэшируется намеренно: env за время жизни процесса не меняется."""
    return Settings()

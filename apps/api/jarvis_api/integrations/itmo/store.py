"""Хранилище секретов ИСУ поверх `integration_tokens`.

Три вида секрета живут в одной таблице и появляются в разное время
(`db/models.py`): пароль кладётся один раз при первом входе, refresh-токен
обновляется на каждом обмене, access-токен - каждый час.

Всё содержимое шифруется (`jarvis_api/crypto.py`). Незашифрованным здесь
не хранится ничего, включая access-токен: он живёт час, но час - это ровно
столько, сколько нужно, чтобы уехать в ночной дамп.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from jarvis_api.crypto import SecretCipher
from jarvis_api.db.models import IntegrationToken

# Имя провайдера в таблице. Одно на весь пакет: разъехавшись, оно превратит
# сохранённый токен в «токена нет», и джоб пойдёт логиниться паролем заново
# при каждом прогоне - молча и с виду успешно.
PROVIDER = "itmo"

KIND_PASSWORD = "password"
KIND_REFRESH = "refresh_token"
KIND_ACCESS = "access_token"


@dataclass(frozen=True, slots=True)
class StoredSecret:
    """Расшифрованный секрет и срок его жизни."""

    value: str
    expires_at: dt.datetime | None

    def жив(self, now: dt.datetime, leeway_seconds: int) -> bool:
        """Годен ли ещё, с запасом.

        Запас не украшение: токен, живой в момент проверки, успевает протухнуть
        по дороге до портала, и ответом будет 401 в середине джоба.
        """
        if self.expires_at is None:
            return True
        return self.expires_at - dt.timedelta(seconds=leeway_seconds) > now


class CredentialStore:
    """Чтение и запись секретов ИСУ. Транзакцию держит вызывающий код.

    Коммита здесь нет намеренно, как и в `db/session.py`: обмен токенов и
    запись расписания - одна логическая операция (§11.2), и делить её на две
    транзакции значит уметь оказаться с новым токеном и старым расписанием.
    """

    def __init__(self, session: Session, cipher: SecretCipher) -> None:
        self._session = session
        self._cipher = cipher

    def read(self, kind: str) -> StoredSecret | None:
        """Секрет или None, если его никогда не сохраняли."""
        строка = self._session.get(IntegrationToken, {"provider": PROVIDER, "kind": kind})
        if строка is None:
            return None
        return StoredSecret(
            value=self._cipher.decrypt(строка.value_encrypted),
            expires_at=строка.expires_at,
        )

    def write(self, kind: str, value: str, expires_at: dt.datetime | None = None) -> None:
        """Сохраняет секрет, перезаписывая прежний.

        Именно перезапись, а не вставка новой строки: составной первичный ключ
        (provider, kind) не даёт хранить два refresh-токена, и это правильно -
        второй означал бы, что непонятно, какой из них действующий.
        """
        зашифрованное = self._cipher.encrypt(value)
        строка = self._session.get(IntegrationToken, {"provider": PROVIDER, "kind": kind})
        if строка is None:
            self._session.add(
                IntegrationToken(
                    provider=PROVIDER,
                    kind=kind,
                    value_encrypted=зашифрованное,
                    expires_at=expires_at,
                )
            )
            return
        строка.value_encrypted = зашифрованное
        строка.expires_at = expires_at

    def drop(self, kind: str) -> None:
        """Убирает секрет. Нужен, когда портал отверг сохранённый токен."""
        строка = self._session.get(IntegrationToken, {"provider": PROVIDER, "kind": kind})
        if строка is not None:
            self._session.delete(строка)

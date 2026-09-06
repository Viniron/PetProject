"""Шифрование секретов, которые обязаны лежать в базе.

Зачем это вообще есть. Пароль ИСУ нужен приложению постоянно - без него
не выпустить новый refresh-токен, когда старый кончится. Значит, он лежит
в `integration_tokens`. Каждую ночь база целиком уезжает дампом в B2,
и **дампы не шифруются** (ADR-020, риск принят осознанно). Открытым текстом
пароль в дампе означал бы, что утечка бэкапа = утечка доступа к ИСУ.

Ключ (`ISU_CRED_KEY`) живёт только в env на плате и в базу не попадает
никогда. Это и есть вся защита: шифротекст уезжает в облако, замок остаётся
на устройстве. Граница честная и очерчена в `CLAUDE.md`: на одном хосте
лежат и шифротекст, и ключ, поэтому это защита от утечки бэкапа, а не от
компрометации самой платы.

AES-GCM, а не что-нибудь попроще: он аутентифицирует шифротекст. Подменённая
или битая строка в базе даёт ошибку расшифровки, а не молча другой пароль,
с которым потом непонятно, почему портал отвечает 401.
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jarvis_api.config import Settings

# Длина nonce для GCM. 96 бит - размер, для которого режим спроектирован;
# другой заставляет реализацию хэшировать его дополнительно и ничего не даёт.
NONCE_BYTES = 12

# Допустимые длины ключа AES. Ровно эти три, потому что 20 случайных байт
# из-под неудачной команды должны быть отвергнуты, а не дополнены нулями.
KEY_LENGTHS = (16, 24, 32)


class SecretCipherError(RuntimeError):
    """Отказ шифрования или расшифровки.

    Отдельный тип, чтобы джоб отличал «ключ не тот» от «портал не ответил»:
    первое чинится строкой в .env, второе не чинится вообще.
    """


class SecretCipher:
    """Шифрует и расшифровывает короткие строки одним симметричным ключом.

    Формат хранимого значения: `nonce (12 байт) || шифротекст+тег`. Nonce
    случайный на каждое шифрование и лежит рядом со значением - он не секрет,
    но повторять его с тем же ключом нельзя, иначе GCM теряет стойкость.
    Поэтому же здесь нет и не может быть детерминированного режима: два
    одинаковых пароля обязаны дать разные шифротексты.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) not in KEY_LENGTHS:
            raise SecretCipherError(
                f"ключ шифрования длиной {len(key)} байт: допустимы "
                f"{', '.join(str(n) for n in KEY_LENGTHS)}"
            )
        self._aead = AESGCM(key)

    @classmethod
    def from_base64(cls, encoded: str) -> "SecretCipher":
        """Ключ из .env. Там он строкой, потому что env хранит только строки.

        Ожидается вывод `openssl rand -base64 32` - ровно то, что записано
        в RUNBOOK. Пробелы по краям срезаются: скопированная из терминала
        строка регулярно приносит перевод строки, и падать на нём было бы
        издевательством.
        """
        значение = encoded.strip()
        if not значение:
            raise SecretCipherError(
                "ISU_CRED_KEY пуст - сгенерируйте `openssl rand -base64 32` "
                "и впишите в .env на плате (см. docs/RUNBOOK.md)"
            )
        try:
            ключ = base64.b64decode(значение, validate=True)
        except (binascii.Error, ValueError) as ошибка:
            raise SecretCipherError(
                "ISU_CRED_KEY не разбирается как base64 - вписано значение "
                "не из `openssl rand -base64 32`"
            ) from ошибка
        return cls(ключ)

    def encrypt(self, plaintext: str) -> bytes:
        """Шифрует строку. Возвращает то, что кладётся в `bytea`."""
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)

    def decrypt(self, blob: bytes) -> str:
        """Расшифровывает значение из базы.

        Падает громко на любом расхождении: подменённый шифротекст, обрезанная
        строка, другой ключ. Молча вернуть мусор здесь нельзя - он уехал бы
        в форму логина портала и выглядел бы как неверный пароль.
        """
        if len(blob) <= NONCE_BYTES:
            raise SecretCipherError(
                f"шифротекст длиной {len(blob)} байт короче собственного заголовка - "
                "значение в базе испорчено"
            )
        nonce, тело = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        try:
            открытый = self._aead.decrypt(nonce, тело, None)
        except InvalidTag as ошибка:
            raise SecretCipherError(
                "значение не расшифровывается: ISU_CRED_KEY не тот, которым "
                "оно шифровалось, либо строка в базе повреждена"
            ) from ошибка
        return открытый.decode("utf-8")


def build_cipher(settings: Settings) -> SecretCipher:
    """Шифр из настроек. Отдельной функцией, чтобы тесты брали свой ключ."""
    return SecretCipher.from_base64(settings.isu_cred_key)

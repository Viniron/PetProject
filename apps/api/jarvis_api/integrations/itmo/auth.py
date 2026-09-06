"""Вход в ITMO ID (Keycloak) и выдача действующего access-токена.

**Почему это не «логин по паролю».** У ITMO ID нет ни password grant, ни
возможности завести своего клиента. Единственный работающий путь, найденный
референсом `iburakov/my-itmo-ru-to-ical`, - притвориться личным кабинетом
портала и пройти обычный Authorization Code + PKCE, программно заполнив
HTML-форму Keycloak. Отсюда четыре запроса вместо одного, куки между ними
и запрет следовать редиректу: `code` лежит в заголовке `Location`
302-ответа, и клиент, послушно перешедший по нему, потеряет его.

**Чем мы отличаемся от референса.** Он читает из ответа только
`access_token` и раз в 55 минут проходит весь флоу заново по паролю.
`CLAUDE.md` требует приоритета за refresh-токеном, и причина не в экономии
запросов: каждый повторный вход по паролю - это ещё один POST логина
и пароля в чужую форму, то есть лишний повод для портала посчитать нас
подозрительными и лишний шанс, что пароль утечёт в логи прокси.

Порядок попыток: сохранённый access → refresh → пароль. Спускаемся на
ступень ниже только когда предыдущая не сработала, и каждый спуск
записывается в журнал: молчаливое падение на пароль при живом refresh -
это поломка, которую иначе нечем заметить.
"""

import base64
import datetime as dt
import hashlib
import html
import logging
import os
import re
import secrets
import urllib.parse
from dataclasses import dataclass

import httpx2

from jarvis_api.config import Settings
from jarvis_api.integrations.itmo.store import (
    KIND_ACCESS,
    KIND_PASSWORD,
    KIND_REFRESH,
    CredentialStore,
)

logger = logging.getLogger("jarvis.itmo.auth")

# Длина случайного материала для code_verifier. 40 байт - как в референсе;
# спецификация PKCE требует от 43 до 128 символов после кодирования, и
# 40 байт дают их с запасом даже после вычистки не-буквенно-цифровых знаков.
VERIFIER_RANDOM_BYTES = 40


class ItmoAuthError(RuntimeError):
    """Войти не удалось. Отдельный тип, чтобы отличать от отказа расписания."""


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """Разобранный ответ token-эндпоинта Keycloak."""

    access_token: str
    access_expires_at: dt.datetime | None
    refresh_token: str | None
    refresh_expires_at: dt.datetime | None


def _срок(now: dt.datetime, seconds: object) -> dt.datetime | None:
    """Абсолютный момент истечения из `expires_in`.

    Абсолютный, а не относительный, потому что он ложится в базу и переживает
    перезапуск процесса: `expires_in`, сохранённый как число секунд, после
    перезагрузки Pi означал бы совсем другой момент.
    """
    if not isinstance(seconds, int) or seconds <= 0:
        return None
    return now + dt.timedelta(seconds=seconds)


def make_code_verifier() -> str:
    """Секрет PKCE. Живёт ровно один вход и никуда не сохраняется."""
    сырое = base64.urlsafe_b64encode(os.urandom(VERIFIER_RANDOM_BYTES)).decode("ascii")
    # Не-алфавитно-цифровые знаки вычищаются, как в референсе: спецификация
    # их разрешает, но лишний '-' в URL - это лишний повод для несовпадения
    # на стороне, поведение которой мы проверить не можем.
    return "".join(знак for знак in сырое if знак.isalnum())


def make_code_challenge(verifier: str) -> str:
    """S256-проверка: base64url от sha256, без выравнивающих '='."""
    отпечаток = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(отпечаток).decode("ascii").rstrip("=")


class ItmoAuth:
    """Выдаёт действующий access-токен, добывая его наименее дорогим способом."""

    def __init__(self, settings: Settings, http: httpx2.Client, store: CredentialStore) -> None:
        self._settings = settings
        self._http = http
        self._store = store
        провайдер = settings.itmo_auth_provider_url.rstrip("/")
        self._authorize_url = f"{провайдер}/protocol/openid-connect/auth"
        self._token_url = f"{провайдер}/protocol/openid-connect/token"
        # Форма логина Keycloak отдаётся не HTML-тегом <form action>, а
        # переменной loginAction внутри JS на странице. Регекс привязан
        # к адресу провайдера: без привязки он цепляет первую попавшуюся
        # строку с похожим именем и уводит логин с паролем неизвестно куда.
        self._login_action_re = re.compile(
            r'"loginAction"\s*:\s*"(?P<action>' + re.escape(провайдер) + r'[^"]*)"'
        )

    # --- публичное ----------------------------------------------------------

    def access_token(self, now: dt.datetime) -> str:
        """Действующий токен. Порядок попыток описан в шапке модуля."""
        сохранённый = self._store.read(KIND_ACCESS)
        if сохранённый is not None and сохранённый.жив(
            now, self._settings.itmo_token_leeway_seconds
        ):
            return сохранённый.value

        обновлённый = self._попробовать_refresh(now)
        if обновлённый is not None:
            return обновлённый

        logger.info("вхожу в ИСУ паролем: refresh-токена нет или он не сработал")
        ответ = self._войти_паролем(now)
        self._сохранить(ответ)
        return ответ.access_token

    def invalidate_access(self) -> None:
        """Забыть access-токен: портал ответил 401 на заведомо живой токен.

        Нужно, чтобы повтор запроса пошёл за новым токеном, а не бился
        тем же самым до исчерпания попыток.
        """
        self._store.drop(KIND_ACCESS)

    # --- ступени ------------------------------------------------------------

    def _попробовать_refresh(self, now: dt.datetime) -> str | None:
        """Обмен refresh-токена. None означает «не вышло, спускаемся ниже»."""
        refresh = self._store.read(KIND_REFRESH)
        if refresh is None:
            return None
        if not refresh.жив(now, self._settings.itmo_token_leeway_seconds):
            logger.info("refresh-токен ИСУ истёк - потребуется вход паролем")
            return None

        try:
            ответ = self._обменять_refresh(refresh.value, now)
        except ItmoAuthError as ошибка:
            # Не пробрасываем: пароль на месте, и вход по нему - штатный
            # запасной путь. Но в журнал это идёт как деградация, иначе
            # сломавшийся refresh незаметен ровно до дня, когда пароль сменят.
            logger.warning("refresh-токен ИСУ отвергнут (%s), вхожу паролем", ошибка)
            self._store.drop(KIND_REFRESH)
            return None

        self._сохранить(ответ)
        return ответ.access_token

    def _пароль(self) -> str:
        """Пароль из базы, а при первом запуске - из env, с записью в базу.

        Порядок именно такой. В env пароль лежит открытым текстом, и это
        нормально: env не попадает в дамп. В базе он лежит шифротекстом,
        и это нужно, потому что база в дамп попадает. Первый вход переносит
        значение из первого места во второе.
        """
        сохранённый = self._store.read(KIND_PASSWORD)
        if сохранённый is not None:
            return сохранённый.value

        из_env = self._settings.isu_password
        if not из_env:
            raise ItmoAuthError(
                "пароля ИСУ нет ни в базе, ни в ISU_PASSWORD - "
                "впишите его в .env на плате (см. docs/RUNBOOK.md)"
            )
        self._store.write(KIND_PASSWORD, из_env)
        return из_env

    # --- сетевая часть ------------------------------------------------------

    def _обменять_refresh(self, refresh_token: str, now: dt.datetime) -> TokenResponse:
        ответ = self._http.post(
            self._token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self._settings.itmo_client_id,
                "refresh_token": refresh_token,
            },
        )
        if ответ.status_code != httpx2.codes.OK:
            raise ItmoAuthError(
                f"ITMO ID отверг refresh-токен: {ответ.status_code} {ответ.text[:200]!r}"
            )
        return self._разобрать_токены(ответ, now)

    def _войти_паролем(self, now: dt.datetime) -> TokenResponse:
        """Полный Authorization Code + PKCE с эмуляцией формы Keycloak."""
        if not self._settings.isu_login:
            raise ItmoAuthError("ISU_LOGIN пуст - впишите логин ИСУ в .env на плате")

        verifier = make_code_verifier()
        state = secrets.token_urlsafe(16)

        страница = self._http.get(
            self._authorize_url,
            params={
                "protocol": "oauth2",
                "response_type": "code",
                "client_id": self._settings.itmo_client_id,
                "redirect_uri": self._settings.itmo_redirect_uri,
                "scope": "openid",
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": make_code_challenge(verifier),
            },
        )
        if страница.status_code != httpx2.codes.OK:
            raise ItmoAuthError(f"страница входа ITMO ID недоступна: {страница.status_code}")

        совпадение = self._login_action_re.search(страница.text)
        if совпадение is None:
            # Самое хрупкое место интеграции, и оно обязано быть узнаваемым
            # в логе: разметку Keycloak меняет обновление на стороне вуза,
            # а выглядеть это будет как «джоб перестал работать».
            raise ItmoAuthError(
                "на странице входа ITMO ID не найден loginAction - "
                "разметка Keycloak изменилась, регекс в auth.py требует правки"
            )
        адрес_формы = html.unescape(совпадение.group("action"))

        # Куки первого ответа клиент несёт сам: Keycloak связывает форму
        # с сессией аутентификации именно ими, и без общего клиента POST
        # уходит в пустоту с ответом «сессия истекла».
        форма = self._http.post(
            адрес_формы,
            data={"username": self._settings.isu_login, "password": self._пароль()},
        )
        if форма.status_code != httpx2.codes.FOUND:
            # 200 здесь означает «форма показана снова», то есть чаще всего
            # неверный пароль. Тело не логируем: в нём бывает и введённый логин.
            raise ItmoAuthError(
                f"ITMO ID не принял логин и пароль: ответ {форма.status_code} "
                "вместо перенаправления (обычно это неверные учётные данные)"
            )

        разбор = urllib.parse.urlparse(форма.headers.get("Location", ""))
        параметры = urllib.parse.parse_qs(разбор.query)
        коды = параметры.get("code")
        if not коды:
            причина = параметры.get("error", ["без объяснения"])[0]
            raise ItmoAuthError(
                f"в перенаправлении ITMO ID нет параметра code - вернулось {причина!r}"
            )
        # state проверяем, только если он вернулся: спецификация обязывает
        # его вернуть, но ронять ночной джоб из-за косметики чужого сервера
        # не за что. Несовпадение - другое дело: это ответ не на наш запрос.
        вернувшийся_state = параметры.get("state", [None])[0]
        if вернувшийся_state is not None and вернувшийся_state != state:
            raise ItmoAuthError("ITMO ID вернул чужой state - ответ не на наш запрос")

        обмен = self._http.post(
            self._token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": self._settings.itmo_client_id,
                "redirect_uri": self._settings.itmo_redirect_uri,
                "code": коды[0],
                "code_verifier": verifier,
            },
        )
        if обмен.status_code != httpx2.codes.OK:
            raise ItmoAuthError(
                f"ITMO ID не обменял code на токен: {обмен.status_code} {обмен.text[:200]!r}"
            )
        return self._разобрать_токены(обмен, now)

    # --- разбор и сохранение ------------------------------------------------

    def _разобрать_токены(self, ответ: httpx2.Response, now: dt.datetime) -> TokenResponse:
        try:
            тело = ответ.json()
        except ValueError as ошибка:
            raise ItmoAuthError("ITMO ID вернул не JSON на token-эндпоинте") from ошибка
        if not isinstance(тело, dict):
            raise ItmoAuthError("ITMO ID вернул не объект на token-эндпоинте")

        access = тело.get("access_token")
        if not isinstance(access, str) or not access:
            raise ItmoAuthError("в ответе ITMO ID нет access_token")

        refresh = тело.get("refresh_token")
        return TokenResponse(
            access_token=access,
            access_expires_at=_срок(now, тело.get("expires_in")),
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            refresh_expires_at=_срок(now, тело.get("refresh_expires_in")),
        )

    def _сохранить(self, ответ: TokenResponse) -> None:
        self._store.write(KIND_ACCESS, ответ.access_token, ответ.access_expires_at)
        if ответ.refresh_token is not None:
            self._store.write(KIND_REFRESH, ответ.refresh_token, ответ.refresh_expires_at)

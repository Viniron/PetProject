"""Вход в ITMO ID (Э3): подставной Keycloak, настоящая база.

Ни один запрос наружу не уходит: весь HTTP замкнут на `httpx2.MockTransport`
(`CLAUDE.md` - сеть в тестах всегда замокана). База настоящая, потому что
проверяется в том числе то, что пароль лёг в неё шифротекстом, а не текстом.

Главное, что здесь доказывается, - **порядок попыток**: сохранённый access,
потом refresh, потом пароль. Он не косметика. Каждый лишний вход по паролю -
это POST логина и пароля в чужую форму, и молчаливое сползание на пароль при
живом refresh нечем заметить, кроме такого теста.
"""

import base64
import datetime as dt
import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.crypto import SecretCipher
from jarvis_api.db.models import IntegrationToken
from jarvis_api.integrations.itmo.auth import (
    ItmoAuth,
    ItmoAuthError,
    make_code_challenge,
    make_code_verifier,
)
from jarvis_api.integrations.itmo.store import (
    KIND_ACCESS,
    KIND_PASSWORD,
    KIND_REFRESH,
    PROVIDER,
    CredentialStore,
)

ПРОВАЙДЕР = "https://id.itmo.test/auth/realms/itmo"
ПОРТАЛ = "https://my.itmo.test/api"
ЛОГИН = "x000000"
ПАРОЛЬ = "не настоящий пароль"
КЛЮЧ = base64.b64encode(bytes(32)).decode()

СЕЙЧАС = dt.datetime(2026, 9, 5, 6, 0, tzinfo=dt.UTC)


def настройки(**переопределения: object) -> Settings:
    """Настройки теста. Адреса подставные - в настоящий ITMO ID тест не ходит."""
    основа: dict[str, object] = {
        "isu_login": ЛОГИН,
        "isu_password": ПАРОЛЬ,
        "isu_cred_key": КЛЮЧ,
        "itmo_auth_provider_url": ПРОВАЙДЕР,
        "itmo_api_base_url": ПОРТАЛ,
    }
    основа.update(переопределения)
    return Settings(**основа)  # type: ignore[arg-type]


@dataclass
class ПодставнойKeycloak:
    """Keycloak ровно в том объёме, в каком его видит наш код.

    Считает запросы по видам: именно по счётчикам проверяется, что лишнего
    входа паролем не случилось.
    """

    страница_входа: str | None = None
    вернуть_state: bool = True
    подменить_state: bool = False
    форма_отвечает: int = httpx2.codes.FOUND
    refresh_отвечает: int = httpx2.codes.OK
    выдавать_refresh: bool = True
    выдавать_access: bool = True
    access_ttl: int | None = 3600
    refresh_ttl: int | None = 30 * 24 * 3600
    выданные: list[str] = field(default_factory=list)
    вызовы: dict[str, int] = field(default_factory=dict)
    последний_state: str = ""
    последний_challenge: str = ""
    принятые_пароли: list[str] = field(default_factory=list)

    def _счёт(self, имя: str) -> None:
        self.вызовы[имя] = self.вызовы.get(имя, 0) + 1

    def _токены(self) -> dict[str, object]:
        номер = len(self.выданные) + 1
        access = f"access-{номер}"
        self.выданные.append(access)
        тело: dict[str, object] = {}
        if self.выдавать_access:
            тело["access_token"] = access
        if self.access_ttl is not None:
            тело["expires_in"] = self.access_ttl
        if self.выдавать_refresh:
            тело["refresh_token"] = f"refresh-{номер}"
            if self.refresh_ttl is not None:
                тело["refresh_expires_in"] = self.refresh_ttl
        return тело

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        путь = str(request.url).split("?")[0]

        if путь == f"{ПРОВАЙДЕР}/protocol/openid-connect/auth":
            self._счёт("auth")
            параметры = parse_qs(request.url.query.decode())
            self.последний_state = параметры["state"][0]
            self.последний_challenge = параметры["code_challenge"][0]
            разметка = self.страница_входа
            if разметка is None:
                разметка = (
                    '<html><script>var ctx = {"loginAction":"'
                    f"{ПРОВАЙДЕР}/login-actions/authenticate?session_code=abc&amp;tab_id=1"
                    '"};</script></html>'
                )
            return httpx2.Response(200, text=разметка)

        if путь == f"{ПРОВАЙДЕР}/login-actions/authenticate":
            self._счёт("form")
            поля = parse_qs(request.content.decode())
            self.принятые_пароли.append(поля["password"][0])
            if self.форма_отвечает != httpx2.codes.FOUND:
                return httpx2.Response(self.форма_отвечает, text="<html>снова форма</html>")
            state = "state-of-another-request" if self.подменить_state else self.последний_state
            # Значение ASCII не для красоты: заголовок Location кодируется
            # ascii, и настоящий Keycloak шлёт сюда UUID.
            место = "https://my.itmo.test/login/callback?code=6f1a-code"
            if self.вернуть_state:
                место = f"{место}&state={state}"
            return httpx2.Response(302, headers={"Location": место})

        if путь == f"{ПРОВАЙДЕР}/protocol/openid-connect/token":
            поля = parse_qs(request.content.decode())
            вид = поля["grant_type"][0]
            self._счёт(f"token:{вид}")
            if вид == "refresh_token" and self.refresh_отвечает != httpx2.codes.OK:
                return httpx2.Response(self.refresh_отвечает, text='{"error":"invalid_grant"}')
            return httpx2.Response(200, json=self._токены())

        raise AssertionError(f"тест ушёл на неожиданный адрес: {request.url}")


@pytest.fixture
def keycloak() -> ПодставнойKeycloak:
    return ПодставнойKeycloak()


@pytest.fixture
def клиент(keycloak: ПодставнойKeycloak) -> Iterator[httpx2.Client]:
    with httpx2.Client(
        transport=httpx2.MockTransport(keycloak), follow_redirects=False
    ) as собранный:
        yield собранный


@pytest.fixture
def хранилище(сессия: Session) -> CredentialStore:
    return CredentialStore(сессия, SecretCipher.from_base64(КЛЮЧ))


@pytest.fixture
def вход(клиент: httpx2.Client, хранилище: CredentialStore) -> Callable[..., ItmoAuth]:
    def собрать(**переопределения: object) -> ItmoAuth:
        return ItmoAuth(настройки(**переопределения), клиент, хранилище)

    return собрать


# --- полный вход паролем ----------------------------------------------------


def test_первый_вход_идёт_паролем_и_проходит_весь_флоу(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """Ни токенов, ни пароля в базе - значит PKCE целиком.

    Три запроса: страница входа, форма, обмен code на токен. Меньше -
    значит какой-то шаг пропущен, и на живом Keycloak он не пропустится.
    """
    токен = вход().access_token(СЕЙЧАС)

    assert токен == "access-1"
    assert keycloak.вызовы == {"auth": 1, "form": 1, "token:authorization_code": 1}


def test_pkce_считается_ровно_как_s256() -> None:
    """Проверка на известном значении, а не «что-то передали».

    Ошибка здесь не видна ниоткуда: Keycloak просто откажет в обмене code,
    и выглядеть это будет как неверный пароль.
    """
    verifier = "dBjftJeZ4CVPmB92K27uhbUJU1p1r-wW1gFWFOEjXk"
    ожидаемое = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())

    assert make_code_challenge(verifier) == ожидаемое.decode().rstrip("=")


def test_verifier_каждый_раз_новый_и_годен_для_pkce() -> None:
    """Спецификация требует от 43 до 128 знаков unreserved-алфавита."""
    первый, второй = make_code_verifier(), make_code_verifier()

    assert первый != второй
    assert 43 <= len(первый) <= 128
    assert первый.isalnum()


def test_challenge_уходит_в_keycloak_в_нужном_виде(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """S256 от 32 байт - это 43 знака base64url без выравнивающих '='."""
    вход().access_token(СЕЙЧАС)

    assert len(keycloak.последний_challenge) == 43
    assert "=" not in keycloak.последний_challenge


def test_пароль_из_env_переезжает_в_базу_шифротекстом(
    вход: Callable[..., ItmoAuth], сессия: Session
) -> None:
    """Смысл всей возни с ключом: в дампе пароля открытым текстом быть не должно.

    Проверяется буквально - подстрокой по байтам колонки, а не «мы же
    вызвали encrypt».
    """
    вход().access_token(СЕЙЧАС)
    сессия.flush()

    строка = сессия.get(IntegrationToken, {"provider": PROVIDER, "kind": KIND_PASSWORD})
    assert строка is not None
    assert ПАРОЛЬ.encode("utf-8") not in строка.value_encrypted
    assert SecretCipher.from_base64(КЛЮЧ).decrypt(строка.value_encrypted) == ПАРОЛЬ


def test_после_входа_сохранены_оба_токена(
    вход: Callable[..., ItmoAuth], хранилище: CredentialStore
) -> None:
    """Без сохранённого refresh следующий прогон снова пойдёт паролем."""
    вход().access_token(СЕЙЧАС)

    access = хранилище.read(KIND_ACCESS)
    refresh = хранилище.read(KIND_REFRESH)
    assert access is not None and access.value == "access-1"
    assert refresh is not None and refresh.value == "refresh-1"
    assert access.expires_at == СЕЙЧАС + dt.timedelta(seconds=3600)


# --- порядок попыток --------------------------------------------------------


def test_живой_access_токен_не_вызывает_ни_одного_запроса(
    вход: Callable[..., ItmoAuth], хранилище: CredentialStore, keycloak: ПодставнойKeycloak
) -> None:
    """Самая дешёвая ступень: токен уже есть и ещё жив."""
    хранилище.write(KIND_ACCESS, "прежний", СЕЙЧАС + dt.timedelta(hours=1))

    assert вход().access_token(СЕЙЧАС) == "прежний"
    assert keycloak.вызовы == {}


def test_истёкший_access_обновляется_refresh_ом_а_не_паролем(
    вход: Callable[..., ItmoAuth], хранилище: CredentialStore, keycloak: ПодставнойKeycloak
) -> None:
    """`CLAUDE.md`: приоритет за refresh-токеном.

    Референс на этом месте раз в 55 минут логинится паролем заново; мы так
    не делаем, и тест сторожит именно это - формы входа быть не должно.
    """
    хранилище.write(KIND_ACCESS, "протухший", СЕЙЧАС - dt.timedelta(minutes=1))
    хранилище.write(KIND_REFRESH, "живой-refresh", СЕЙЧАС + dt.timedelta(days=10))

    assert вход().access_token(СЕЙЧАС) == "access-1"
    assert keycloak.вызовы == {"token:refresh_token": 1}
    assert keycloak.принятые_пароли == []


def test_access_с_запасом_обновляется_заранее(
    вход: Callable[..., ItmoAuth], хранилище: CredentialStore, keycloak: ПодставнойKeycloak
) -> None:
    """Токен, живой ещё десять секунд, успевает протухнуть по дороге до портала.

    Запас (`itmo_token_leeway_seconds`) существует ровно поэтому, и без
    теста он молча перестал бы применяться.
    """
    хранилище.write(KIND_ACCESS, "почти протух", СЕЙЧАС + dt.timedelta(seconds=10))
    хранилище.write(KIND_REFRESH, "живой-refresh", СЕЙЧАС + dt.timedelta(days=10))

    assert вход().access_token(СЕЙЧАС) == "access-1"
    assert keycloak.вызовы == {"token:refresh_token": 1}


def test_истёкший_refresh_приводит_к_входу_паролем(
    вход: Callable[..., ItmoAuth], хранилище: CredentialStore, keycloak: ПодставнойKeycloak
) -> None:
    """Через месяц простоя refresh мёртв - и это штатный, а не аварийный путь."""
    хранилище.write(KIND_REFRESH, "мёртвый", СЕЙЧАС - dt.timedelta(days=1))

    assert вход().access_token(СЕЙЧАС) == "access-1"
    assert keycloak.вызовы == {"auth": 1, "form": 1, "token:authorization_code": 1}


def test_отвергнутый_refresh_приводит_к_входу_паролем_и_забывается(
    вход: Callable[..., ItmoAuth],
    хранилище: CredentialStore,
    keycloak: ПодставнойKeycloak,
) -> None:
    """Портал может отозвать сессию раньше срока - по нашим часам токен жив.

    Мёртвый refresh обязан быть удалён: иначе каждый следующий прогон
    начинается с заведомо бесполезного запроса.
    """
    keycloak.refresh_отвечает = httpx2.codes.BAD_REQUEST
    хранилище.write(KIND_REFRESH, "отозванный", СЕЙЧАС + dt.timedelta(days=10))

    assert вход().access_token(СЕЙЧАС) == "access-1"
    assert keycloak.вызовы["token:refresh_token"] == 1
    assert keycloak.вызовы["form"] == 1
    # Старый отозванный токен не должен остаться: он заменён выданным сейчас.
    новый = хранилище.read(KIND_REFRESH)
    assert новый is not None and новый.value == "refresh-1"


def test_пароль_берётся_из_базы_а_не_из_env_когда_он_там_есть(
    вход: Callable[..., ItmoAuth], хранилище: CredentialStore, keycloak: ПодставнойKeycloak
) -> None:
    """База - источник истины для пароля. Смена его в .env не обязана
    подхватываться молча: в базе лежит тот, которым вход уже работал."""
    хранилище.write(KIND_PASSWORD, "пароль из базы")

    вход(isu_password="другой пароль из env").access_token(СЕЙЧАС)

    assert keycloak.принятые_пароли == ["пароль из базы"]


# --- отказы -----------------------------------------------------------------


def test_неверный_пароль_даёт_понятный_отказ_без_утечки(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """Keycloak на неверный пароль показывает форму снова, а не 4xx.

    Текст ошибки не должен содержать ни пароля, ни тела ответа: он уедет
    в лог, а логи мы читаем и пересылаем.
    """
    keycloak.форма_отвечает = httpx2.codes.OK

    with pytest.raises(ItmoAuthError, match="не принял логин и пароль") as отказ:
        вход().access_token(СЕЙЧАС)

    assert ПАРОЛЬ not in str(отказ.value)
    assert ЛОГИН not in str(отказ.value)


def test_изменившаяся_разметка_keycloak_называется_своим_именем(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """Самое хрупкое место интеграции.

    Регекс по `loginAction` ломает обновление Keycloak на стороне вуза,
    и в логе это обязано читаться как «правьте регекс», а не как
    «джоб перестал работать».
    """
    keycloak.страница_входа = "<html>совсем другая страница</html>"

    with pytest.raises(ItmoAuthError, match="loginAction"):
        вход().access_token(СЕЙЧАС)


def test_чужой_state_отвергается(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """Вернувшийся state обязан совпасть - иначе это ответ не на наш запрос."""
    keycloak.подменить_state = True

    with pytest.raises(ItmoAuthError, match="чужой state"):
        вход().access_token(СЕЙЧАС)


def test_отсутствие_state_в_ответе_вход_не_ломает(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """Спецификация обязывает вернуть state, но ронять ночной джоб
    из-за косметики чужого сервера не за что."""
    keycloak.вернуть_state = False

    assert вход().access_token(СЕЙЧАС) == "access-1"


def test_пустой_логин_отвергается_до_первого_запроса(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """Незаполненный .env должен читаться как незаполненный .env."""
    with pytest.raises(ItmoAuthError, match="ISU_LOGIN пуст"):
        вход(isu_login="").access_token(СЕЙЧАС)

    assert keycloak.вызовы == {}


def test_нет_пароля_ни_в_базе_ни_в_env(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """Отдельный текст отказа: с ним owner знает, куда именно вписать значение."""
    with pytest.raises(ItmoAuthError, match="ISU_PASSWORD"):
        вход(isu_password="").access_token(СЕЙЧАС)


def test_ответ_без_access_token_считается_отказом(
    вход: Callable[..., ItmoAuth], keycloak: ПодставнойKeycloak
) -> None:
    """Пустой токен не «почти работает» - на нём портал ответит 401 позже,
    и разбираться придётся уже в другом месте."""
    keycloak.выдавать_access = False

    with pytest.raises(ItmoAuthError, match="нет access_token"):
        вход().access_token(СЕЙЧАС)


def test_токен_без_срока_считается_бессрочным_но_сохраняется(
    вход: Callable[..., ItmoAuth], хранилище: CredentialStore, keycloak: ПодставнойKeycloak
) -> None:
    """Keycloak всегда шлёт expires_in, но полагаться на это нельзя.

    Отсутствие срока не повод входить заново каждый раз: 401 от портала
    сбросит токен сам (см. client.py).
    """
    keycloak.access_ttl = None

    вход().access_token(СЕЙЧАС)
    access = хранилище.read(KIND_ACCESS)

    assert access is not None
    assert access.жив(СЕЙЧАС + dt.timedelta(days=365), 60)


def test_в_базе_нет_ни_одного_секрета_открытым_текстом(
    вход: Callable[..., ItmoAuth], сессия: Session
) -> None:
    """Общая проверка по всей таблице: дамп не должен отдавать ничего.

    Три вида секрета появляются в разное время, и забыть зашифровать один
    из них - ровно тот класс ошибки, который тест ловит, а ревью нет.
    """
    вход().access_token(СЕЙЧАС)
    сессия.flush()

    секреты = {ПАРОЛЬ.encode(), b"access-1", b"refresh-1"}
    for строка in сессия.scalars(select(IntegrationToken)):
        assert not any(секрет in строка.value_encrypted for секрет in секреты)

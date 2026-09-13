"""Аутентификация owner перед включением туннеля (Э7, ADR-033).

Условие ADR-031 звучит буквально: туннель не включается раньше, чем появится
аутентификация. Этот модуль - то, чем «появилась» отличается от «написана»:
пока он не зелёный, `COMPOSE_PROFILES=tunnel` ставить нельзя.

Проверяется не счастливый путь (он проверяется сам собой - экран либо
открывается, либо нет), а те способы пройти мимо двери, которые в рабочем
режиме невидимы: подпись чужим ключом, `alg` на выбор отправителя, токен
соседнего приложения той же команды Cloudflare, просроченный токен, чужая
почта, сервисный токен и - самое частое - незаполненные `CF_ACCESS_*`.

**Сеть замокана целиком** (CLAUDE.md): ключи подписи генерируются здесь же,
поход за JWKS замкнут на подставной транспорт. Ни один тест не ходит
в Cloudflare.

Большинству тестов база не нужна: отказ случается до эндпоинта. Поэтому
модуль работает и там, где `make db-up` не поднят, - кроме одного теста,
который обязан показать, что правильный токен действительно пускает.
"""

import datetime as dt
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx2
import jwt
import pytest
from conftest import Стенд
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from jarvis_api.api import auth
from jarvis_api.api.auth import НаборКлючей, получить_ключи
from jarvis_api.config import Settings, get_settings
from jarvis_api.main import app

ДОМЕН = "jarvis-test.cloudflareaccess.com"
AUD = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
ПОЧТА = "owner@example.com"
KID = "ключ-1"


# --- Ключи и токены ---------------------------------------------------------


@pytest.fixture(scope="module")
def пара_ключей() -> tuple[Any, Any]:
    """Одна пара на модуль: генерация RSA-2048 дорога, а нужна она одна.

    Второй, «чужой» ключ берётся отсюда же в тесте подписи - генерировать
    его отдельно значило бы удвоить самую медленную операцию модуля.
    """
    закрытый = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return закрытый, закрытый.public_key()


@pytest.fixture(scope="module")
def чужой_ключ() -> Any:
    """Ключ, которого нет в JWKS: им подписывается поддельный токен."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(открытый: Any, kid: str) -> dict[str, Any]:
    """Публичный ключ в том виде, в каком его отдаёт Cloudflare."""
    описание: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(открытый, as_dict=True)
    описание.update(kid=kid, alg="RS256", use="sig")
    return описание


@pytest.fixture
def настройки() -> Settings:
    """Боевой режим: ENV=prod и заполненные CF_ACCESS_*."""
    return Settings(
        env="prod",
        cf_access_team_domain=ДОМЕН,
        cf_access_aud=AUD,
        cf_access_allowed_email=ПОЧТА,
    )


@pytest.fixture
def выписать(пара_ключей: tuple[Any, Any]) -> Callable[..., str]:
    """Фабрика токенов. По умолчанию - такой, какой выписывает Cloudflare."""
    закрытый, _ = пара_ключей

    def выписать_токен(
        *,
        ключ: Any | None = None,
        kid: str = KID,
        aud: str | None = AUD,
        iss: str = f"https://{ДОМЕН}",
        email: str | None = ПОЧТА,
        живёт: dt.timedelta = dt.timedelta(hours=24),
        алгоритм: str = "RS256",
    ) -> str:
        сейчас = dt.datetime.now(dt.UTC)
        полезное: dict[str, Any] = {
            "iss": iss,
            "iat": int((сейчас - dt.timedelta(minutes=1)).timestamp()),
            "exp": int((сейчас + живёт).timestamp()),
            "sub": "0123abcd",
            "type": "app",
        }
        if aud is not None:
            полезное["aud"] = [aud]
        if email is not None:
            полезное["email"] = email
        return jwt.encode(
            полезное,
            ключ or закрытый,
            algorithm=алгоритм,
            headers={"kid": kid},
        )

    return выписать_токен


class ПодставныеКлючи:
    """Источник ключей без сети: тот же интерфейс, что у `НаборКлючей`."""

    def __init__(self, ключи: dict[str, Any]) -> None:
        self.ключи = ключи

    def ключ(self, kid: str, момент: dt.datetime) -> Any:
        from jarvis_api.api.errors import ОтказAPI

        if kid not in self.ключи:
            raise ОтказAPI(
                статус=401,
                code="unauthenticated",
                message="Токен подписан неизвестным ключом Cloudflare Access",
            )
        return self.ключи[kid]


@pytest.fixture
def клиент(
    настройки: Settings, пара_ключей: tuple[Any, Any]
) -> Iterator[tuple[TestClient, Settings]]:
    """Клиент в боевом режиме и без базы: отказ доступа случается раньше неё.

    Подмены снимаются через `pop`, а не `clear()`: объект `app` один на весь
    прогон (см. фикстуру `стенд` в conftest).
    """
    _, открытый = пара_ключей
    app.dependency_overrides[get_settings] = lambda: настройки
    app.dependency_overrides[получить_ключи] = lambda: ПодставныеКлючи({KID: открытый})
    try:
        yield TestClient(app), настройки
    finally:
        for зависимость in (get_settings, получить_ключи):
            app.dependency_overrides.pop(зависимость, None)


# --- Дверь заперта ----------------------------------------------------------


def test_без_токена_отказ(клиент: tuple[TestClient, Settings]) -> None:
    """Главный тест этапа: без токена сетка календаря не отдаётся."""
    ответ = клиент[0].get("/api/calendar?view=week")

    assert ответ.status_code == 401
    тело = ответ.json()
    assert тело["code"] == "unauthenticated"
    assert тело["retryable"] is False


def test_тело_отказа_из_контракта(клиент: tuple[TestClient, Settings]) -> None:
    """Отказ доступа - то же тело, что у остальных отказов (§10).

    Иначе клиент, разбирающий ответы по `openapi.json`, на 401 получит
    форму, которой в схеме нет.
    """
    from jarvis_api.api.errors import ErrorBody

    ErrorBody.model_validate(клиент[0].get("/api/day-flags").json())


@pytest.mark.parametrize(
    ("описание", "правки"),
    [
        ("чужая подпись", {"подделка": True}),
        ("просрочен", {"живёт": dt.timedelta(hours=-1)}),
        ("чужой адресат", {"aud": "aud-соседнего-приложения"}),
        ("чужая команда", {"iss": "https://чужая.cloudflareaccess.com"}),
        ("нет адресата", {"aud": None}),
        ("неизвестный ключ", {"kid": "kid-которого-нет"}),
    ],
)
def test_негодный_токен_отказ(
    клиент: tuple[TestClient, Settings],
    выписать: Callable[..., str],
    чужой_ключ: Any,
    описание: str,
    правки: dict[str, Any],
) -> None:
    """Шесть способов подсунуть валидный с виду токен. Все - 401.

    Текст ответа одинаков намеренно: экрану во всех случаях делать одно -
    отправить owner на логин Cloudflare, а перечислять отправителю, что
    именно не так с его токеном, значит помогать подбирать.
    """
    if правки.pop("подделка", False):
        правки["ключ"] = чужой_ключ

    ответ = клиент[0].get(
        "/api/calendar?view=week",
        headers={auth.ЗАГОЛОВОК_ТОКЕНА: выписать(**правки)},
    )

    assert ответ.status_code == 401, описание
    assert ответ.json()["code"] == "unauthenticated", описание


def test_алгоритм_задаёт_не_отправитель(
    клиент: tuple[TestClient, Settings], пара_ключей: tuple[Any, Any]
) -> None:
    """Классика подмены алгоритма: HS256, где секретом взят наш публичный ключ.

    Если проверяющая сторона берёт алгоритм из заголовка токена, такая
    подпись сходится - публичный ключ известен кому угодно. Отсюда жёсткий
    список `АЛГОРИТМЫ` в `auth.py`, и этот тест - его единственный сторож.

    Токен собирается руками: `jwt.encode` такую подделку сделать не даст -
    PyJWT отказывается принимать PEM как секрет HMAC. Отказ библиотеки
    защищает того, кто подписывает, а проверяем мы того, кто принимает.
    """
    import base64
    import hashlib
    import hmac
    import json

    from cryptography.hazmat.primitives import serialization

    _, открытый = пара_ключей
    pem = открытый.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def кусок(данные: dict[str, Any]) -> bytes:
        сырое = json.dumps(данные, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(сырое).rstrip(b"=")

    сейчас = dt.datetime.now(dt.UTC)
    начало = (
        кусок({"alg": "HS256", "typ": "JWT", "kid": KID})
        + b"."
        + кусок(
            {
                "iss": f"https://{ДОМЕН}",
                "aud": [AUD],
                "email": ПОЧТА,
                "iat": int(сейчас.timestamp()),
                "exp": int((сейчас + dt.timedelta(hours=1)).timestamp()),
            }
        )
    )
    подпись = base64.urlsafe_b64encode(hmac.new(pem, начало, hashlib.sha256).digest()).rstrip(b"=")
    подделка = (начало + b"." + подпись).decode("ascii")

    ответ = клиент[0].get("/api/calendar?view=week", headers={auth.ЗАГОЛОВОК_ТОКЕНА: подделка})

    assert ответ.status_code == 401


def test_чужая_почта_запрещена(
    клиент: tuple[TestClient, Settings], выписать: Callable[..., str]
) -> None:
    """Политика Access правится мышкой; вторая дверь - эта проверка.

    Отказ 403, а не 401: токен настоящий и действующий, повторный логин
    ничего не изменит.
    """
    ответ = клиент[0].get(
        "/api/calendar?view=week",
        headers={auth.ЗАГОЛОВОК_ТОКЕНА: выписать(email="someone@example.com")},
    )

    assert ответ.status_code == 403
    assert ответ.json()["code"] == "forbidden"


def test_сервисный_токен_не_пускается(
    клиент: tuple[TestClient, Settings], выписать: Callable[..., str]
) -> None:
    """У сервисного токена Access нет почты - только `common_name`.

    Машинам здесь делать нечего: джобы ходят в базу напрямую, а не по HTTP.
    """
    ответ = клиент[0].get(
        "/api/calendar?view=week", headers={auth.ЗАГОЛОВОК_ТОКЕНА: выписать(email=None)}
    )

    assert ответ.status_code == 403


def test_отказ_раньше_разбора_параметров(клиент: tuple[TestClient, Settings]) -> None:
    """Мусор в пути не должен давать ответ раньше проверки доступа.

    Иначе чужой узнаёт про эндпоинт по различию 422 и 404 - мелочь, но
    ровно та мелочь, из которой собирается карта сервиса.
    """
    assert клиент[0].delete("/api/day-flags/не-число").status_code == 401
    assert клиент[0].get("/api/calendar?view=века").status_code == 401


def test_health_открыт(клиент: tuple[TestClient, Settings]) -> None:
    """`/health` без токена. Им живёт healthcheck compose - изнутри сети Docker.

    Токена там взяться неоткуда: запрос не проходит через Cloudflare.
    Закрыть `/health` значило бы вечно перезапускающийся контейнер.
    """
    assert клиент[0].get("/health").status_code == 200


def _пути_api() -> list[tuple[str, str]]:
    """Все маршруты `/api/*` и их методы - из самого контракта.

    Из `app.openapi()`, а не обходом `app.routes`: свежий FastAPI держит
    подключённый роутер отдельным объектом, и наивный обход списка
    маршрутов молча находит один только `/health` - то есть проверка
    выглядела бы зелёной, ничего не проверяя.
    """
    описание = app.openapi()
    return [
        (путь, метод.upper())
        for путь, операции in описание["paths"].items()
        if путь.startswith("/api")
        for метод in операции
    ]


@pytest.mark.parametrize(("путь", "метод"), _пути_api())
def test_каждый_маршрут_api_закрыт(
    клиент: tuple[TestClient, Settings], путь: str, метод: str
) -> None:
    """Сторож от забытой зависимости на роутере следующего этапа.

    Роутер Э8 будет писаться по образцу соседнего, и потерянная строчка
    в `main.py` не видна ни в дифе, ни глазами. Здесь она краснеет сразу:
    новый маршрут появляется в списке автоматически.
    """
    адрес = путь.replace("{flag_id}", "1")

    ответ = клиент[0].request(метод, адрес)

    assert ответ.status_code == 401, f"{метод} {путь} отвечает без токена"


# --- Ненастроенная защита ---------------------------------------------------


@pytest.fixture
def клиент_без_настроек(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    """Клиент с пустыми CF_ACCESS_*; окружение задаётся параметром."""
    окружение = getattr(request, "param", "prod")
    app.dependency_overrides[get_settings] = lambda: Settings(env=окружение)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.parametrize("клиент_без_настроек", ["prod"], indirect=True)
def test_ненастроенная_защита_закрывает_дверь(клиент_без_настроек: TestClient) -> None:
    """Пустые CF_ACCESS_* в prod - отказ, а не тихий пропуск.

    Это главное решение ADR-033 и единственная защита от самой вероятной
    ошибки эксплуатации: строчку в `.env` забывают, а запрос при этом
    выглядит совершенно обычно.
    """
    ответ = клиент_без_настроек.get("/api/calendar?view=week")

    assert ответ.status_code == 503
    тело = ответ.json()
    assert тело["code"] == "auth_not_configured"
    # Повтор не поможет: нужна правка .env и перезапуск.
    assert тело["retryable"] is False


@pytest.mark.parametrize("клиент_без_настроек", ["dev"], indirect=True)
def test_на_машине_разработки_защиты_нет(клиент_без_настроек: TestClient) -> None:
    """ENV=dev - единственное исключение: туннеля там нет по построению.

    Проверяется не 200 (базы в этом тесте нет), а то, что отказ пришёл
    не от двери: запрос дошёл до эндпоинта и споткнулся о базу.
    """
    ответ = клиент_без_настроек.get("/api/calendar?view=week")

    assert ответ.status_code not in (401, 403)
    assert ответ.json()["code"] != "auth_not_configured"


def test_предупреждение_в_лог_при_старте(caplog: pytest.LogCaptureFixture) -> None:
    """Процесс без защиты обязан сказать об этом в лог - один раз, при старте.

    На запрос такое писать нельзя: экран опрашивает сетку, и предупреждение
    утонуло бы в собственном повторе.
    """
    with caplog.at_level("WARNING", logger="jarvis.api.auth"):
        auth.предупредить_о_режиме(Settings(env="dev"))
        auth.предупредить_о_режиме(Settings(env="prod"))
        auth.предупредить_о_режиме(
            Settings(
                env="prod",
                cf_access_team_domain=ДОМЕН,
                cf_access_aud=AUD,
                cf_access_allowed_email=ПОЧТА,
            )
        )

    записи = [запись.getMessage() for запись in caplog.records]
    assert len(записи) == 2, "настроенная защита не должна писать ничего"
    assert "ENV=dev" in записи[0]
    assert "CF_ACCESS_*" in записи[1]


def test_домен_команды_нормализуется() -> None:
    """Owner копирует домен из панели ссылкой - со схемой и слешем.

    Без нормализации `iss` собирался бы как "https://https://…/", и
    совершенно законный токен отвергался бы с текстом про подпись.
    """
    настройки = Settings(cf_access_team_domain=f"https://{ДОМЕН}/")

    assert настройки.cf_access_team_domain == ДОМЕН


# --- Кэш ключей -------------------------------------------------------------


@pytest.fixture
def jwks_сервер(monkeypatch: pytest.MonkeyPatch, пара_ключей: tuple[Any, Any]) -> SimpleNamespace:
    """Подставной Cloudflare: считает запросы и умеет отказывать.

    Подменяется не глобальный `httpx2`, а имя внутри `auth` - иначе патч
    задел бы и `TestClient`, который построен на том же httpx2.
    """
    _, открытый = пара_ключей
    состояние = SimpleNamespace(запросов=0, отвечает=200, ключи=[_jwk(открытый, KID)])

    def обработчик(запрос: httpx2.Request) -> httpx2.Response:
        состояние.запросов += 1
        assert запрос.url.path == "/cdn-cgi/access/certs"
        if состояние.отвечает != 200:
            return httpx2.Response(состояние.отвечает, text="nope")
        return httpx2.Response(200, json={"keys": состояние.ключи})

    def фабрика(**параметры: Any) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(обработчик), **параметры)

    monkeypatch.setattr(
        auth,
        "httpx2",
        SimpleNamespace(Client=фабрика, HTTPError=httpx2.HTTPError),
    )
    return состояние


def test_ключи_читаются_один_раз(jwks_сервер: SimpleNamespace, настройки: Settings) -> None:
    """Кэш - не оптимизация: без него каждый запрос экрана шёл бы в Cloudflare."""
    набор = НаборКлючей(настройки=настройки)
    момент = dt.datetime.now(dt.UTC)

    набор.ключ(KID, момент)
    набор.ключ(KID, момент + dt.timedelta(seconds=30))

    assert jwks_сервер.запросов == 1


def test_неизвестный_kid_не_гоняет_наружу_на_каждый_запрос(
    jwks_сервер: SimpleNamespace, настройки: Settings
) -> None:
    """Ротация ключей даёт законный незнакомый kid - перечитать нужно.

    Но без паузы мусорный токен со случайным kid превращается в способ
    отправлять нас в Cloudflare запрос за запросом.
    """
    набор = НаборКлючей(настройки=настройки)
    момент = dt.datetime.now(dt.UTC)
    набор.ключ(KID, момент)

    for сдвиг in (1, 2, 3):
        with pytest.raises(Exception, match="неизвестным ключом"):
            набор.ключ("чужой-kid", момент + dt.timedelta(seconds=сдвиг))

    assert jwks_сервер.запросов == 1

    пауза = настройки.cf_access_jwks_retry_seconds
    with pytest.raises(Exception, match="неизвестным ключом"):
        набор.ключ("чужой-kid", момент + dt.timedelta(seconds=пауза))

    assert jwks_сервер.запросов == 2


def test_ключи_перечитываются_по_истечении_ttl(
    jwks_сервер: SimpleNamespace, настройки: Settings
) -> None:
    набор = НаборКлючей(настройки=настройки)
    момент = dt.datetime.now(dt.UTC)

    набор.ключ(KID, момент)
    набор.ключ(KID, момент + dt.timedelta(seconds=настройки.cf_access_jwks_ttl_seconds))

    assert jwks_сервер.запросов == 2


def test_недоступный_cloudflare_не_ломает_проверку_по_кэшу(
    jwks_сервер: SimpleNamespace, настройки: Settings
) -> None:
    """§10: деградация вместо падения.

    Ключ Cloudflare действует неделями после ротации, и подделать подпись
    устаревшим публичным ключом нельзя - значит кэш остаётся годным,
    а недоступность Cloudflare не обязана выбивать owner из приложения.
    """
    набор = НаборКлючей(настройки=настройки)
    момент = dt.datetime.now(dt.UTC)
    набор.ключ(KID, момент)
    jwks_сервер.отвечает = 500

    ключ = набор.ключ(KID, момент + dt.timedelta(hours=2))

    assert ключ is not None
    assert jwks_сервер.запросов == 2


def test_пустой_кэш_и_недоступный_cloudflare_дают_503(
    jwks_сервер: SimpleNamespace, настройки: Settings
) -> None:
    """Проверять нечем - честный отказ, повторяемый (§10)."""
    from jarvis_api.api.errors import ОтказAPI

    jwks_сервер.отвечает = 502
    набор = НаборКлючей(настройки=настройки)

    with pytest.raises(ОтказAPI) as поймано:
        набор.ключ(KID, dt.datetime.now(dt.UTC))

    assert поймано.value.статус == 503
    assert поймано.value.code == "auth_unavailable"
    assert поймано.value.retryable is True


# --- Счастливый путь --------------------------------------------------------


def test_токен_owner_пускает_к_данным(
    стенд: Стенд, выписать: Callable[..., str], пара_ключей: tuple[Any, Any]
) -> None:
    """Единственный тест модуля, которому нужна база: дверь должна открываться.

    Без него набор проверок доказывал бы только то, что не пускают никого.
    """
    _, открытый = пара_ключей
    стенд.настройки = Settings(
        env="prod",
        cf_access_team_domain=ДОМЕН,
        cf_access_aud=AUD,
        cf_access_allowed_email=ПОЧТА,
    )
    app.dependency_overrides[получить_ключи] = lambda: ПодставныеКлючи({KID: открытый})
    try:
        ответ = стенд.клиент.get("/api/day-flags", headers={auth.ЗАГОЛОВОК_ТОКЕНА: выписать()})
    finally:
        app.dependency_overrides.pop(получить_ключи, None)

    assert ответ.status_code == 200


def test_почта_сверяется_без_учёта_регистра(
    клиент: tuple[TestClient, Settings], выписать: Callable[..., str]
) -> None:
    """Cloudflare может вернуть почту в другом регистре, чем написано в .env.

    Отказ owner в доступе из-за заглавной буквы выглядел бы как поломка
    аутентификации целиком - и искали бы её в подписи.
    """
    ответ = клиент[0].get(
        "/api/calendar?view=week",
        headers={auth.ЗАГОЛОВОК_ТОКЕНА: выписать(email=ПОЧТА.upper())},
    )

    assert ответ.status_code != 403

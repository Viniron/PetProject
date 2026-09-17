"""Проверка перед включением туннеля (Э7, шаг 4).

`test_auth.py` доказывает, что дверь умеет запираться. Этот модуль - про то,
что её проверили запертой на конкретной плате, с конкретным `.env`, до того
как туннель открыли. Разница между «умеет» и «заперта» стоит ровно один
незаполненный `CF_ACCESS_*`.

Главный тест здесь один: живой `/api/*`, ответивший 200 на запрос без токена,
обязан провалить прогон. Всё остальное - формы и подсказки, помогающие
понять, что именно править в `.env`.

**Сеть замокана целиком** (CLAUDE.md): и Cloudflare, и собственный API
отвечают подставным транспортом. Базы модулю не нужно - проверка ходит
по HTTP и в базу не заглядывает.
"""

from collections.abc import Callable

import httpx2
import pytest

from jarvis_api.config import Settings
from jarvis_api.jobs import tunnel_check

ДОМЕН = "jarvis-test.cloudflareaccess.com"
AUD = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
ПОЧТА = "owner@example.com"

API = "http://api-test:8000"
WEB = "http://web-test:8080"

# Имя второго маршрута (Э9б). Заперто оно или нет - видно только снаружи,
# поэтому подставной транспорт отвечает и за край Cloudflare тоже.
SSH = "ssh-test.example.ru"
ВХОД_ACCESS = f"https://{ДОМЕН}/cdn-cgi/access/login/{SSH}"


def настройки(**правки: str) -> Settings:
    """Заполненные CF_ACCESS_* по умолчанию: тест правит то, что проверяет."""
    значения: dict[str, str] = {
        "env": "prod",
        "cf_access_team_domain": ДОМЕН,
        "cf_access_aud": AUD,
        "cf_access_allowed_email": ПОЧТА,
    }
    значения.update(правки)
    return Settings(**значения)  # type: ignore[arg-type]


def клиент(
    *,
    jwks: int = 200,
    api_health: int = 200,
    api_calendar: int = 401,
    web_health: int = 200,
    web_calendar: int = 401,
    ssh_код: int = 302,
) -> httpx2.Client:
    """Подставной транспорт на все три адреса сразу.

    Коды ответов - параметры, потому что каждый из них по отдельности
    и есть предмет проверки.
    """

    def обработчик(запрос: httpx2.Request) -> httpx2.Response:
        адрес = str(запрос.url)
        # Ключи узнаются по пути, а не по домену: тест подставляет и чужой
        # домен команды, и он обязан дойти до того же обработчика.
        if адрес.endswith("/cdn-cgi/access/certs"):
            if jwks != 200:
                return httpx2.Response(jwks, text="nope")
            return httpx2.Response(200, json={"keys": [{"kid": "k1"}]})
        if адрес.startswith(f"https://{SSH}"):
            # Запертое имя до туннеля не доходит: край Cloudflare отправляет
            # на вход. Незапертое отвечает кодом, который придумал тест.
            заголовки = {"location": ВХОД_ACCESS} if 300 <= ssh_код < 400 else {}
            return httpx2.Response(ssh_код, headers=заголовки)
        основа = API if адрес.startswith(API) else WEB
        путь = адрес[len(основа) :]
        коды = {
            API: {"/health": api_health, tunnel_check.ЗАЩИЩЁННЫЙ_ПУТЬ: api_calendar},
            WEB: {"/health": web_health, tunnel_check.ЗАЩИЩЁННЫЙ_ПУТЬ: web_calendar},
        }[основа]
        return httpx2.Response(коды[путь], json={"code": "unauthenticated", "message": "нет"})

    return httpx2.Client(transport=httpx2.MockTransport(обработчик))


def провалы(пункты: list[tunnel_check.Проверка]) -> list[str]:
    """Только то, что запрещает включать туннель: пропущенное - не провал."""
    return [пункт.название for пункт in tunnel_check.провалено(пункты)]


def пропущенные(пункты: list[tunnel_check.Проверка]) -> list[str]:
    return [пункт.название for пункт in пункты if пункт.пропущена]


def пункт_ssh(пункты: list[tunnel_check.Проверка]) -> tunnel_check.Проверка:
    return next(п for п in пункты if п.название == tunnel_check.ПУНКТ_SSH)


@pytest.fixture
def прогон() -> Callable[..., list[tunnel_check.Проверка]]:
    """Прогон целиком: настройки и транспорт задаёт тест, остальное как в бою."""

    def выполнить(
        *, правки: dict[str, str] | None = None, **коды: int
    ) -> list[tunnel_check.Проверка]:
        with клиент(**коды) as подставной:
            return tunnel_check.выполнить(настройки(**(правки or {})), подставной, api=API, web=WEB)

    return выполнить


# --- Главное: открытая дверь заметна ----------------------------------------


def test_открытая_дверь_роняет_прогон(прогон: Callable[..., list[tunnel_check.Проверка]]) -> None:
    """200 без токена - это публичное расписание, и прогон обязан покраснеть.

    Ради этого пункта проверка и написана: глазами такое не видно вовсе -
    у owner экран работает одинаково в обоих случаях.
    """
    пункты = прогон(api_calendar=200)
    пункт = next(п for п in пункты if п.название == f"api: {tunnel_check.ЗАЩИЩЁННЫЙ_ПУТЬ} заперт")
    assert not пункт.прошла
    assert "ДВЕРЬ ОТКРЫТА" in пункт.подсказка


def test_дыра_только_в_прокси_тоже_заметна(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """Наружу смотрит Caddy: запертый API при открытом web - всё ещё дыра.

    Маршрут туннеля ведёт на web:8080, и проверять один только API значило бы
    смотреть не на ту дверь.
    """
    провалившиеся = провалы(прогон(web_calendar=200))
    assert провалившиеся == [f"web: {tunnel_check.ЗАЩИЩЁННЫЙ_ПУТЬ} заперт"]


def test_всё_на_месте(прогон: Callable[..., list[tunnel_check.Проверка]]) -> None:
    """Настроенная плата проходит без единого замечания."""
    assert провалы(прогон()) == []


def test_мусорный_токен_пропущен_внутрь(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """Заголовок с мусором, получивший 200, - вторая дверь не читает токен."""
    пункты = прогон(api_calendar=200)
    assert "api: мусорный токен не пускается" in провалы(пункты)


# --- Настройки: что именно править в .env -----------------------------------


def test_пустая_переменная_названа_по_имени(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """Отчёт называет незаполненную строчку, а не «аутентификация не настроена».

    Owner правит `.env` в nano по SSH: «CF_ACCESS_AUD заполнена - пусто»
    и «проверьте настройки» - разница в полчаса.
    """
    пункты = прогон(правки={"cf_access_aud": ""})
    пункт = next(п for п in пункты if п.название == "CF_ACCESS_AUD заполнена")
    assert not пункт.прошла
    assert "пусто" in пункт.подсказка


def test_заглушка_из_примера_не_считается_заполненной(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """CHANGE_ME - заполненное поле для кода и незаполненное для человека."""
    пункты = прогон(правки={"cf_access_allowed_email": "CHANGE_ME"})
    пункт = next(п for п in пункты if п.название == "CF_ACCESS_ALLOWED_EMAIL заполнена")
    assert not пункт.прошла
    assert "заглушка" in пункт.подсказка


def test_домен_команды_со_схемой_уже_срезан(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """Скопированная из панели ссылка - законное значение: схему срезает конфиг."""
    assert провалы(прогон(правки={"cf_access_team_domain": f"https://{ДОМЕН}/"})) == []


def test_чужой_домен_команды_назван(прогон: Callable[..., list[tunnel_check.Проверка]]) -> None:
    """Домен сайта вместо домена команды - частая подстановка, и она молчаливая."""
    пункты = прогон(правки={"cf_access_team_domain": "jarvis.example.com"})
    assert "CF_ACCESS_TEAM_DOMAIN похож на домен команды" in провалы(пункты)


def test_аud_не_той_формы(прогон: Callable[..., list[tunnel_check.Проверка]]) -> None:
    """AUD - 64 hex. Скопированный id приложения даёт 401 на законном токене."""
    пункты = прогон(правки={"cf_access_aud": "приложение-jarvis"})
    assert "CF_ACCESS_AUD похож на Application Audience Tag" in провалы(пункты)


def test_dev_не_годится_для_туннеля(прогон: Callable[..., list[tunnel_check.Проверка]]) -> None:
    """ENV=dev - режим без аутентификации (ADR-033), туннеля быть не должно."""
    пункты = прогон(правки={"env": "dev"})
    assert "ENV=prod" in провалы(пункты)


# --- Внешние отказы ---------------------------------------------------------


def test_недоступные_ключи_команды(прогон: Callable[..., list[tunnel_check.Проверка]]) -> None:
    """Опечатка в домене всплывает здесь, а не первым входом owner."""
    пункты = прогон(jwks=404)
    assert "ключи команды Cloudflare отдаются" in провалы(пункты)


def test_упавший_сервис_не_выдаётся_за_запертый(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """Мёртвый API тоже не отдаёт расписания - и это не то же, что запертый."""
    пункты = прогон(api_health=502)
    assert "api: /health отвечает" in провалы(пункты)


def test_ненастроенная_аутентификация_отличается_от_дыры(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """503 - дверь заперта для всех, включая owner. Подсказка обязана это сказать."""
    пункты = прогон(api_calendar=503)
    пункт = next(п for п in пункты if п.название == f"api: {tunnel_check.ЗАЩИЩЁННЫЙ_ПУТЬ} заперт")
    assert not пункт.прошла
    assert "owner" in пункт.подсказка


def test_адрес_можно_пропустить() -> None:
    """Пустой --web: проверка идёт там, где Caddy ещё не поднят."""
    with клиент() as подставной:
        пункты = tunnel_check.выполнить(настройки(), подставной, api=API, web="")
    assert not [п for п in пункты if п.название.startswith("web:")]


def test_код_возврата_ненулевой_на_провале(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make tunnel-check` обязан падать, а не печатать список и выходить нулём."""
    monkeypatch.setattr(
        tunnel_check,
        "выполнить",
        lambda *_, **__: [tunnel_check.Проверка("что-то", False, "подсказка")],
    )
    assert tunnel_check.run(настройки(), api="", web="") == 1


# --- Э9б: второй маршрут туннеля, ssh ---------------------------------------


def ssh_прогон(**коды: int) -> list[tunnel_check.Проверка]:
    """Прогон с заведённым именем ssh-приложения: иначе пункт пропускается."""
    with клиент(**коды) as подставной:
        return tunnel_check.выполнить(
            настройки(cf_access_ssh_hostname=SSH), подставной, api=API, web=WEB
        )


def test_незапертый_ssh_маршрут_роняет_прогон() -> None:
    """502 - подпись открытого маршрута: запрос дошёл до cloudflared.

    Главный тест этой группы. Такой ответ означает, что перед входом на плату
    нет двери вовсе, и owner об этом не узнает: сам он ходит с токеном.
    """
    пункты = ssh_прогон(ssh_код=502)
    assert tunnel_check.ПУНКТ_SSH in провалы(пункты)
    assert "ДВЕРЬ ОТКРЫТА" in пункт_ssh(пункты).подсказка


def test_ssh_маршрут_отвечающий_200_это_дыра() -> None:
    пункты = ssh_прогон(ssh_код=200)
    assert tunnel_check.ПУНКТ_SSH in провалы(пункты)


def test_прямой_отказ_тоже_запертое_имя() -> None:
    """401 с краю - дверь, а не дыра: за открытым маршрутом стоит sshd.

    Access отвечает редиректом не всякому запросу, и требование ровно 302
    однажды покрасило бы исправную дверь. Открытый маршрут таким кодом
    ответить не может: перед ним `cloudflared` с его 502, 503 и 404.
    """
    assert провалы(ssh_прогон(ssh_код=401)) == []
    assert провалы(ssh_прогон(ssh_код=403)) == []


def test_не_200_ещё_не_значит_заперт() -> None:
    """Запертость доказывается редиректом на вход, а не отсутствием данных.

    530 отдаёт край Cloudflare, когда за именем ничего живого нет; трактовка
    «раз не 200, то заперто» назвала бы открытый маршрут закрытым.
    """
    assert tunnel_check.ПУНКТ_SSH in провалы(ssh_прогон(ssh_код=530))


def test_запертый_ssh_маршрут_проходит() -> None:
    assert провалы(ssh_прогон()) == []


def ssh_редирект(куда: str) -> list[tunnel_check.Проверка]:
    """Маршрут отвечает редиректом, и вопрос один: чья дверь на другом конце."""

    def обработчик(запрос: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": куда})

    with httpx2.Client(transport=httpx2.MockTransport(обработчик)) as подставной:
        return tunnel_check.проверить_ssh_маршрут(настройки(cf_access_ssh_hostname=SSH), подставной)


def test_вход_ведёт_в_чужую_команду() -> None:
    """Редирект есть, но на чужой домен: впустят по чужой политике."""
    пункты = ssh_редирект("https://someone-else.cloudflareaccess.com/login")
    assert tunnel_check.ПУНКТ_SSH in провалы(пункты)
    assert "someone-else.cloudflareaccess.com" in пункт_ssh(пункты).подсказка


def test_редирект_без_адреса() -> None:
    assert tunnel_check.ПУНКТ_SSH in провалы(ssh_редирект(""))


def test_маршрута_нет_вовсе_названо_своими_словами() -> None:
    """404 от catch-all: имя вписано в .env, а маршрут не заведён."""
    пункты = ssh_прогон(ssh_код=404)
    assert tunnel_check.ПУНКТ_SSH in провалы(пункты)
    assert "catch-all" in пункт_ssh(пункты).подсказка


def test_маршрут_не_ответил_считается_непроверенным() -> None:
    """Обрыв по пути - не доказательство запертости, а её отсутствие."""

    def обработчик(запрос: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("нет связи")

    with httpx2.Client(transport=httpx2.MockTransport(обработчик)) as подставной:
        пункты = tunnel_check.проверить_ssh_маршрут(
            настройки(cf_access_ssh_hostname=SSH), подставной
        )
    assert tunnel_check.ПУНКТ_SSH in провалы(пункты)


def test_незаведённый_маршрут_не_роняет_прогон(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """До Э9б второго маршрута не было: его отсутствие туннель не запрещает."""
    пункты = прогон()
    assert провалы(пункты) == []
    assert пропущенные(пункты) == [tunnel_check.ПУНКТ_SSH]


def test_пропущенный_пункт_не_выдаётся_за_пройденный(
    прогон: Callable[..., list[tunnel_check.Проверка]],
) -> None:
    """Строка отчёта говорит SKIP и называет причину, а не печатает OK.

    Пустая строчка в `.env` при заведённом в панели маршруте - ровно тот
    случай, когда «проверено» и «нечего проверять» стоят денег: маршрут
    остаётся без присмотра, а отчёт зелёный.
    """
    пункт = пункт_ssh(прогон())
    assert not пункт.прошла
    строка = tunnel_check._строка(пункт)
    assert строка.startswith("[SKIP]")
    assert "CF_ACCESS_SSH_HOSTNAME" in строка


def test_заглушка_имени_из_примера_не_проверяется() -> None:
    with клиент() as подставной:
        пункты = tunnel_check.проверить_ssh_маршрут(
            настройки(cf_access_ssh_hostname="CHANGE_ME"), подставной
        )
    assert пропущенные(пункты) == [tunnel_check.ПУНКТ_SSH]


def test_имя_не_той_формы_названо_до_запроса() -> None:
    """Из «ssh» без домена адрес не собрать - живой запрос не делается."""
    with клиент() as подставной:
        пункты = tunnel_check.проверить_ssh_маршрут(
            настройки(cf_access_ssh_hostname="ssh"), подставной
        )
    assert провалы(пункты) == ["CF_ACCESS_SSH_HOSTNAME похож на имя"]


def test_имя_со_схемой_уже_срезано() -> None:
    """Owner копирует имя из панели ссылкой - схему снимает валидатор."""
    настроено = настройки(cf_access_ssh_hostname=f"https://{SSH}/")
    assert настроено.cf_access_ssh_hostname == SSH

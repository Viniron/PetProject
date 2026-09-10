"""Подставной Google Calendar: транспорт httplib2 с журналом вызовов.

Сеть в тестах замокана целиком (`CLAUDE.md`), а SDK Google ходит наружу
через `httplib2`, поэтому подменяется именно он: `discovery.build(http=…)`.
Это штатная точка подмены самого SDK, а не обход его внутренностей.

Почему свой транспорт, а не `HttpMockSequence` из пакета: тому задаётся
последовательность ответов, а нам нужен **сервер с состоянием**. Половина
проверок этапа - про то, что второй прогон подряд не делает ничего, что
событие, удалённое в Google руками, создаётся заново, и что журнал
восстанавливается по ключам из выдачи. Ни одну из них на списке заранее
заготовленных ответов не поставить.

`static_discovery=True` в клиенте означает, что описание API берётся из
самого пакета, поэтому сюда приходят только полезные запросы - за
discovery-документом никто не идёт.
"""

import json
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httplib2

# Базовый путь Calendar API v3. Сверяется, а не отбрасывается: запрос,
# ушедший не туда, должен уронить тест, а не тихо вернуть пустоту.
ПРЕФИКС = "/calendar/v3"


class ПодставнойGoogle:
    """Календарь в памяти: события, календари, ACL и счётчики обращений."""

    def __init__(self) -> None:
        # id календаря -> {id события: тело}
        self.события: dict[str, dict[str, Any]] = {}
        # id календаря -> название
        self.календари: dict[str, str] = {}
        # id календаря -> тело запроса на создание. Нужно, чтобы проверить
        # зону: календарь, заведённый в UTC, показывает owner пары со
        # сдвигом на три часа при верном времени в базе.
        self.тела_календарей: dict[str, dict[str, Any]] = {}
        # id календаря -> список почт, которым он отдан
        self.acl: dict[str, list[str]] = {}

        self.вызовы: list[tuple[str, str]] = []
        # Отказ, который сервер выдаст на следующий подходящий запрос.
        # (метод, кусок пути, код) - None означает "отвечай нормально".
        self.отказ: tuple[str, str, int] | None = None
        self.сетевой_отказ = False
        self._счётчик = 0

    # --- то, что видят тесты ------------------------------------------------

    def записей(self) -> int:
        """Сколько раз в Google что-то писали. Ноль - это и есть dry-run."""
        return sum(1 for метод, _ in self.вызовы if метод in ("POST", "PUT", "DELETE"))

    def вызовов(self, метод: str, кусок: str) -> int:
        return sum(1 for м, путь in self.вызовы if м == метод and кусок in путь)

    def завести_календарь(self, название: str) -> str:
        """Календарь, существующий до начала теста."""
        идентификатор = f"cal-{len(self.календари) + 1}@group.calendar.google.com"
        self.календари[идентификатор] = название
        self.события[идентификатор] = {}
        self.acl[идентификатор] = []
        return идентификатор

    def события_календаря(self, calendar_id: str) -> dict[str, dict[str, Any]]:
        return self.события.get(calendar_id, {})

    def ключи(self, calendar_id: str) -> set[str]:
        """Наши `jarvis_key` среди событий календаря."""
        return {
            тело["extendedProperties"]["private"]["jarvis_key"]
            for тело in self.события.get(calendar_id, {}).values()
        }

    # --- транспорт httplib2 -------------------------------------------------

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        redirections: int = 1,
        connection_type: Any = None,
    ) -> tuple[httplib2.Response, bytes]:
        разобранный = urlparse(uri)
        путь = разобранный.path
        self.вызовы.append((method, путь))

        if self.сетевой_отказ:
            raise httplib2.ServerNotFoundError("Google недоступен")

        if self.отказ is not None:
            метод_отказа, кусок, код = self.отказ
            if метод_отказа == method and кусок in путь:
                self.отказ = None
                return self._ответ(код, {"error": {"code": код, "message": "подставной отказ"}})

        if not путь.startswith(ПРЕФИКС):
            raise AssertionError(f"тест ушёл на неожиданный адрес: {uri}")
        хвост = путь[len(ПРЕФИКС) :].strip("/")
        части = хвост.split("/")
        разобранное_тело = json.loads(body) if body else {}

        return self._маршрут(method, части, разобранный.query, разобранное_тело, uri)

    # --- внутреннее ---------------------------------------------------------

    def _маршрут(
        self, method: str, части: list[str], query: str, тело: dict[str, Any], uri: str
    ) -> tuple[httplib2.Response, bytes]:
        # POST /calendars - создать календарь
        if части == ["calendars"] and method == "POST":
            идентификатор = self.завести_календарь(тело.get("summary", ""))
            self.тела_календарей[идентификатор] = тело
            return self._ответ(200, {"id": идентификатор, **тело})

        # GET /calendars/{id} - существует ли
        if len(части) == 2 and части[0] == "calendars" and method == "GET":
            calendar_id = _раскодировать(части[1])
            if calendar_id not in self.календари:
                return self._ответ(404, {"error": {"code": 404, "message": "нет такого"}})
            return self._ответ(200, {"id": calendar_id, "summary": self.календари[calendar_id]})

        # POST /calendars/{id}/acl - расшарить
        if len(части) == 3 and части[0] == "calendars" and части[2] == "acl" and method == "POST":
            calendar_id = _раскодировать(части[1])
            self.acl.setdefault(calendar_id, []).append(тело["scope"]["value"])
            return self._ответ(200, тело)

        # /calendars/{id}/events
        if len(части) >= 3 and части[0] == "calendars" and части[2] == "events":
            calendar_id = _раскодировать(части[1])
            if len(части) == 3:
                if method == "GET":
                    return self._выдача(calendar_id, query)
                if method == "POST":
                    return self._создать(calendar_id, тело)
            if len(части) == 4:
                event_id = _раскодировать(части[3])
                if method == "PUT":
                    return self._обновить(calendar_id, event_id, тело)
                if method == "DELETE":
                    return self._удалить(calendar_id, event_id)

        raise AssertionError(f"подставной Google не знает такого запроса: {method} {uri}")

    def _выдача(self, calendar_id: str, query: str) -> tuple[httplib2.Response, bytes]:
        параметры = parse_qs(query)
        мин = параметры.get("timeMin", [""])[0]
        макс = параметры.get("timeMax", [""])[0]
        items = [
            {"id": идентификатор, **тело}
            for идентификатор, тело in self.события.get(calendar_id, {}).items()
            if мин <= тело["start"]["dateTime"] < макс
        ]
        return self._ответ(200, {"items": items})

    def _создать(self, calendar_id: str, тело: dict[str, Any]) -> tuple[httplib2.Response, bytes]:
        self._счётчик += 1
        идентификатор = f"ev{self._счётчик}"
        self.события.setdefault(calendar_id, {})[идентификатор] = тело
        return self._ответ(200, {"id": идентификатор, **тело})

    def _обновить(
        self, calendar_id: str, event_id: str, тело: dict[str, Any]
    ) -> tuple[httplib2.Response, bytes]:
        if event_id not in self.события.get(calendar_id, {}):
            return self._ответ(404, {"error": {"code": 404, "message": "нет такого события"}})
        self.события[calendar_id][event_id] = тело
        return self._ответ(200, {"id": event_id, **тело})

    def _удалить(self, calendar_id: str, event_id: str) -> tuple[httplib2.Response, bytes]:
        if event_id not in self.события.get(calendar_id, {}):
            return self._ответ(404, {"error": {"code": 404, "message": "нет такого события"}})
        del self.события[calendar_id][event_id]
        # Google на успешное удаление отвечает 204 без тела - SDK обрабатывает
        # этот код особо, и подставлять вместо него 200 значило бы проверять
        # не тот путь.
        return httplib2.Response({"status": "204"}), b""

    @staticmethod
    def _ответ(код: int, тело: dict[str, Any]) -> tuple[httplib2.Response, bytes]:
        ответ = httplib2.Response({"status": str(код), "content-type": "application/json"})
        return ответ, json.dumps(тело).encode("utf-8")


def _раскодировать(часть: str) -> str:
    """id календаря приезжает в пути закодированным: `@` становится `%40`."""
    from urllib.parse import unquote

    return unquote(часть)


def собрать_сервис(транспорт: ПодставнойGoogle) -> Any:
    """Сервис Calendar v3 поверх подставного транспорта.

    Ровно то же, что делает `build_service`, минус учётные данные: с `http=`
    SDK не запрашивает токен, и подписывать JWT в тестах не приходится.
    """
    from googleapiclient import discovery

    # cast, а не наследование от httplib2.Http: SDK требует от объекта
    # ровно один метод `request` с этой сигнатурой, и подставной сервер
    # его даёт. Наследоваться пришлось бы ради проверки типов, а получить
    # в нагрузку живой пул соединений внутри тестов.
    return discovery.build(
        "calendar", "v3", http=cast(httplib2.Http, транспорт), static_discovery=True
    )

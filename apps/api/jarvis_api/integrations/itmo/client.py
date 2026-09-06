"""Забор расписания с my.itmo.ru.

Один эндпоинт: `GET /api/schedule/schedule/personal?date_start=&date_end=`
с заголовком `Authorization: Bearer …`. Больше портал ничего не требует -
ни User-Agent, ни Accept-Language, что проверено по референсу.

Повторы здесь есть, а в `auth.py` их нет, и это не забывчивость. Забор
расписания - идемпотентное чтение, повторять его безопасно и нужно: портал
единственный источник, и сетевая икота не повод оставить календарь без
обновления на сутки. Вход же с паролем повторять нельзя так же беспечно:
каждая попытка - это POST пароля в чужую форму и шаг к блокировке учётной
записи за подбор.
"""

import datetime as dt
import logging
import time

import httpx2
from pydantic import ValidationError

from jarvis_api.config import Settings
from jarvis_api.integrations.itmo.auth import ItmoAuth
from jarvis_api.integrations.itmo.schema import SchedulePayload

logger = logging.getLogger("jarvis.itmo.client")

SCHEDULE_PATH = "/schedule/schedule/personal"


class ItmoApiError(RuntimeError):
    """Портал не отдал расписание или отдал не то."""


class ItmoFormatError(ItmoApiError):
    """Ответ портала не разбирается.

    Отдельным типом, потому что реакция другая. Недоступность лечится
    повтором и деградацией на сохранённое зеркало; изменившийся формат
    не лечится ничем и требует правки кода (`SPEC.md` §10).
    """


class ItmoClient:
    """Тонкий клиент портала: один метод, ради которого всё и затевалось."""

    def __init__(self, settings: Settings, http: httpx2.Client, auth: ItmoAuth) -> None:
        self._settings = settings
        self._http = http
        self._auth = auth
        self._base_url = settings.itmo_api_base_url.rstrip("/")

    def schedule(self, date_start: dt.date, date_end: dt.date, now: dt.datetime) -> SchedulePayload:
        """Расписание за период. Даты включительно, формат портала - YYYY-MM-DD."""
        if date_end < date_start:
            raise ValueError(f"период задом наперёд: {date_start} .. {date_end}")

        ответ = self._запросить_с_повторами(date_start, date_end, now)
        try:
            return SchedulePayload.model_validate(ответ)
        except ValidationError as ошибка:
            # Громко и с указанием, что именно не сошлось. Разобрать «сколько
            # получилось» и записать это в календарь запрещено прямо
            # (`SPEC.md` §10): расписание из мусора хуже отсутствия расписания.
            raise ItmoFormatError(
                "формат ответа my.itmo.ru изменился - разбор не удался:\n" f"{ошибка}"
            ) from ошибка

    # --- внутреннее ---------------------------------------------------------

    def _запросить_с_повторами(
        self, date_start: dt.date, date_end: dt.date, now: dt.datetime
    ) -> object:
        последняя: Exception | None = None

        for попытка in range(1, self._settings.itmo_retries + 1):
            try:
                return self._запросить(date_start, date_end, now)
            except ItmoFormatError:
                # Повторять нечего: тот же запрос вернёт тот же нечитаемый ответ.
                raise
            except (ItmoApiError, httpx2.RequestError) as ошибка:
                последняя = ошибка
                if попытка == self._settings.itmo_retries:
                    break
                пауза = self._settings.itmo_retry_backoff_seconds * попытка
                logger.warning(
                    "my.itmo.ru не ответил (попытка %d из %d): %s; жду %.1f с",
                    попытка,
                    self._settings.itmo_retries,
                    ошибка,
                    пауза,
                )
                time.sleep(пауза)

        raise ItmoApiError(
            f"my.itmo.ru не отдал расписание за {self._settings.itmo_retries} попыток: {последняя}"
        )

    def _запросить(self, date_start: dt.date, date_end: dt.date, now: dt.datetime) -> object:
        токен = self._auth.access_token(now)
        ответ = self._http.get(
            f"{self._base_url}{SCHEDULE_PATH}",
            params={
                "date_start": date_start.isoformat(),
                "date_end": date_end.isoformat(),
            },
            headers={"Authorization": f"Bearer {токен}"},
        )

        if ответ.status_code == httpx2.codes.UNAUTHORIZED:
            # Токен был живым по нашим часам, но портал его не принял.
            # Забываем и даём следующей попытке войти заново - иначе повторы
            # бьются тем же мёртвым токеном до исчерпания.
            self._auth.invalidate_access()
            raise ItmoApiError("my.itmo.ru ответил 401 на действующий по нашим часам токен")

        if ответ.status_code != httpx2.codes.OK:
            raise ItmoApiError(
                f"my.itmo.ru ответил {ответ.status_code} на запрос расписания: "
                f"{ответ.text[:200]!r}"
            )

        try:
            return ответ.json()
        except ValueError as ошибка:
            # Не JSON - это почти всегда страница ошибки прокси или капча,
            # то есть отказ доступа, а не смена формата. Повтор уместен.
            raise ItmoApiError("my.itmo.ru вернул не JSON на запрос расписания") from ошибка


def build_client(settings: Settings) -> httpx2.Client:
    """HTTP-клиент для всей интеграции: общие куки, таймаут, без редиректов.

    Клиент один на вход и забор намеренно. Куки Keycloak должны пережить
    четыре запроса подряд, а `follow_redirects=False` обязателен: `code`
    лежит в `Location`, и клиент, перешедший по нему, его потеряет.
    Значение стоит по умолчанию у самого httpx2, но здесь оно записано явно -
    смена умолчания в библиотеке сломала бы вход молча.
    """
    return httpx2.Client(
        timeout=httpx2.Timeout(settings.http_timeout_seconds),
        follow_redirects=False,
    )

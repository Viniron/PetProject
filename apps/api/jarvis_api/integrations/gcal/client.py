"""Тонкий клиент Google Calendar поверх официального SDK.

Здесь только транспорт: собрать сервис, сходить, перевести чужие ошибки
в наши. Никакой предметной логики - что писать в календарь и когда,
решает джоб (`jobs/push_gcal.py`).

**Два типа ошибок, потому что реакции две.** Отказ по одному событию
(4xx на конкретной записи) помечает строку журнала `failed` и не мешает
остальным парам уехать в календарь. Отказ доступа или недоступность
Google прекращает прогон целиком: писать всё равно некуда, а сотня
одинаковых отказов в `audit_log` не добавит знания.

**404 - не ошибка, а факт.** Событие, которого нет при обновлении или
удалении, означает, что его удалили в Google руками. Реконсил обязан это
пережить и создать заново, а не упасть.
"""

import datetime as dt
import json
import logging
from typing import Any

import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient import discovery
from googleapiclient.errors import HttpError

from jarvis_api.config import Settings

logger = logging.getLogger("jarvis.gcal.client")

# Полный доступ к календарям: сервисный аккаунт их создаёт, расшаривает
# и наполняет. Урезанный `calendar.events` не даёт создать сам календарь.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Ключ, под которым наш `external_key` живёт в приватных свойствах события.
# Не украшение: по нему события находятся в Google, когда журнала в базе
# нет - то есть после восстановления из дампа. Без него первый же прогон
# после restore создал бы вторую копию семестра.
KEY_PROPERTY = "jarvis_key"


class GcalError(RuntimeError):
    """Google недоступен или отказал в доступе. Прогон дальше не имеет смысла."""


class GcalItemError(RuntimeError):
    """Отказ по одному событию. Остальные продолжают записываться."""


class GcalNotFound(GcalItemError):
    """События или календаря нет.

    Отдельным типом, потому что это единственный "отказ", который нормален:
    так выглядит удаление события руками в интерфейсе Google.
    """


def build_service(settings: Settings) -> Any:
    """Сервис Calendar v3 от имени service account.

    `static_discovery=True` - не косметика. Без него `build()` идёт за
    описанием API в сеть: в тестах это запрещено прямо (`CLAUDE.md`:
    ни один тест не ходит в Google), а на плате даёт отказ там, где
    у нас нет ни повторов, ни деградации, - до первого полезного запроса.

    Таймаут задаётся здесь и только здесь: `httplib2.Http()` по умолчанию
    ждёт без ограничения, а `CLAUDE.md` требует таймаут на каждом внешнем
    вызове. Джоб с зависшим сокетом не падает и не завершается - он просто
    не делает ничего до перезапуска платы.
    """
    if not settings.google_sa_json.strip():
        raise GcalError(
            "GOOGLE_SA_JSON пуст: ключ service account не задан. "
            "Заведите его по docs/RUNBOOK.md и впишите в .env одной строкой"
        )
    try:
        сырой = json.loads(settings.google_sa_json)
    except ValueError as ошибка:
        raise GcalError(f"GOOGLE_SA_JSON не разбирается как JSON: {ошибка}") from ошибка

    try:
        # ignore: у google-auth нет py.typed, и mypy --strict считает вызов
        # нетипизированным. Сторонний `google-auth-stubs` существует, но
        # отстал от библиотеки на несколько лет - четвёртая зависимость
        # ради одной строки хуже одного точечного подавления.
        учётные = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            сырой, scopes=SCOPES
        )
    except (ValueError, GoogleAuthError) as ошибка:
        raise GcalError(f"ключ service account не принят: {ошибка}") from ошибка

    транспорт = AuthorizedHttp(учётные, http=httplib2.Http(timeout=settings.http_timeout_seconds))
    return discovery.build("calendar", "v3", http=транспорт, static_discovery=True)


class GcalClient:
    """Обёртка над `service`: повторы, перевод ошибок, страницы выдачи."""

    def __init__(self, settings: Settings, service: Any) -> None:
        self._settings = settings
        self._service = service

    # --- события ------------------------------------------------------------

    def list_window(
        self, calendar_id: str, time_min: dt.datetime, time_max: dt.datetime
    ) -> dict[str, str]:
        """Карта `jarvis_key -> id события` внутри окна.

        Это и есть фактическое состояние календаря - то, что в нём лежит
        прямо сейчас, а не то, что мы туда писали. Различие принципиальное:
        журнал `calendar_events` знает о наших попытках, Google знает
        о результате, и расходятся они ровно в двух случаях, ради которых
        реконсил и существует, - база восстановлена из дампа или событие
        удалено руками.

        События без нашего ключа игнорируются: в календаре JARVIS их быть
        не должно, но если owner что-то туда добавит, мы это не тронем.
        """
        найденные: dict[str, str] = {}
        страница: str | None = None
        while True:
            # 404 здесь означает не "события нет", а "нет календаря": его
            # удалили в интерфейсе Google или отозвали доступ сервисному
            # аккаунту. Это отказ уровня прогона, а не одного события -
            # найдено живым прогоном на плате, где GcalNotFound уходил
            # наверх необработанным и джоб падал трассировкой вместо
            # внятного текста и следа в audit_log.
            ответ = self._прочитать_страницу(
                self._service.events().list(
                    calendarId=calendar_id,
                    timeMin=_rfc3339(time_min),
                    timeMax=_rfc3339(time_max),
                    showDeleted=False,
                    singleEvents=True,
                    maxResults=self._settings.gcal_page_size,
                    pageToken=страница,
                ),
                calendar_id,
            )
            for событие in ответ.get("items", []):
                ключ = событие.get("extendedProperties", {}).get("private", {}).get(KEY_PROPERTY)
                идентификатор = событие.get("id")
                if ключ and идентификатор:
                    найденные[ключ] = идентификатор
            страница = ответ.get("nextPageToken")
            if not страница:
                return найденные

    def insert(self, calendar_id: str, body: dict[str, Any]) -> str:
        """Создаёт событие, возвращает его id в Google."""
        ответ = self._вызвать(self._service.events().insert(calendarId=calendar_id, body=body))
        идентификатор = ответ.get("id")
        if not идентификатор:
            raise GcalItemError("Google принял событие, но не вернул его id")
        return str(идентификатор)

    def update(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        """Переписывает событие целиком.

        `update`, а не `patch`, намеренно: тело события мы формируем целиком
        из зеркала, и поле, исчезнувшее в источнике (пропала аудитория),
        обязано исчезнуть и в календаре. `patch` оставил бы старое значение.
        """
        self._вызвать(
            self._service.events().update(calendarId=calendar_id, eventId=event_id, body=body)
        )

    def delete(self, calendar_id: str, event_id: str) -> None:
        """Удаляет событие. Отсутствие события считается успехом."""
        try:
            self._вызвать(self._service.events().delete(calendarId=calendar_id, eventId=event_id))
        except GcalNotFound:
            logger.info("событие %s уже удалено в Google", event_id)

    # --- календари ----------------------------------------------------------

    def calendar_exists(self, calendar_id: str) -> bool:
        """Существует ли календарь с таким id и виден ли он нам."""
        try:
            self._вызвать(self._service.calendars().get(calendarId=calendar_id))
        except GcalNotFound:
            return False
        return True

    def create_calendar(self, summary: str, timezone: str) -> str:
        """Заводит календарь от имени сервисного аккаунта."""
        ответ = self._вызвать(
            self._service.calendars().insert(body={"summary": summary, "timeZone": timezone})
        )
        идентификатор = ответ.get("id")
        if not идентификатор:
            raise GcalError(f"Google создал календарь {summary!r}, но не вернул его id")
        return str(идентификатор)

    def share_calendar(self, calendar_id: str, email: str) -> None:
        """Отдаёт календарь owner.

        Роль `owner`, а не `writer`: календарь создан сервисным аккаунтом,
        и без полных прав owner не сможет ни переименовать его, ни удалить
        событие руками, ни отписаться. Сервисный аккаунт при этом остаётся
        владельцем тоже - ролей owner у календаря может быть несколько.
        """
        self._вызвать(
            self._service.acl().insert(
                calendarId=calendar_id,
                body={"role": "owner", "scope": {"type": "user", "value": email}},
            )
        )

    # --- внутреннее ---------------------------------------------------------

    def _прочитать_страницу(self, запрос: Any, calendar_id: str) -> dict[str, Any]:
        """Страница выдачи событий; пропавший календарь - отказ всего прогона."""
        try:
            return self._вызвать(запрос)
        except GcalNotFound as ошибка:
            raise GcalError(
                f"календарь {calendar_id} недоступен: он удалён в Google или "
                "у сервисного аккаунта отозван доступ. Заведите заново - "
                "`make gcal-setup-pi-apply`"
            ) from ошибка

    def _вызвать(self, запрос: Any) -> dict[str, Any]:
        """Один вызов SDK с повторами и переводом ошибок в наши.

        Повторы делает сам SDK: `num_retries` включает рандомизированный
        exponential backoff на 5xx и 429. Своего цикла здесь нет намеренно -
        два уровня повторов перемножаются, и минутная недоступность Google
        превратилась бы в получасовое ожидание джоба.
        """
        try:
            ответ = запрос.execute(num_retries=self._settings.gcal_retries)
        except HttpError as ошибка:
            статус = ошибка.status_code or 0
            if статус == 404:
                raise GcalNotFound(str(ошибка)) from ошибка
            # 401 и 403 - это не "плохое событие", а нерасшаренный календарь
            # или отозванный ключ: следующие сто событий получат тот же отказ.
            # 5xx сюда доходит уже после повторов SDK, то есть Google лежит.
            if статус in (401, 403) or статус >= 500:
                raise GcalError(f"Google отказал: {ошибка}") from ошибка
            raise GcalItemError(f"Google отклонил запрос: {ошибка}") from ошибка
        except (httplib2.HttpLib2Error, GoogleAuthError, OSError) as ошибка:
            # Сюда попадают обрыв сети, таймаут сокета и отказ в выдаче токена.
            # Всё это - "Google недоступен", а не "событие плохое".
            raise GcalError(f"Google недоступен: {ошибка}") from ошибка
        if isinstance(ответ, dict):
            return ответ
        return {}


def _rfc3339(момент: dt.datetime) -> str:
    """Время для параметров запроса. Только UTC - в базе другого и нет."""
    if момент.tzinfo is None:
        raise ValueError("наивное время в запросе к Google (инвариант 7)")
    return момент.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")

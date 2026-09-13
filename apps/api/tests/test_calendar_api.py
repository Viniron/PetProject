"""Эндпоинт сетки календаря (Э6): контракт ответа и отказы через HTTP.

Арифметика проверена в `test_calendar_domain.py` прямыми вызовами. Здесь -
то, что видно только через HTTP: форма ответа, коды отказов, разбор
параметров и то, что запрос не поднимает планировщик.
"""

import datetime as dt
import inspect

from conftest import Стенд
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.api.routes_calendar import сетка_календаря
from jarvis_api.db.models import AuditLogEntry, ItmoLesson, Setting
from jarvis_api.main import app

СЕЙЧАС = dt.datetime(2026, 10, 14, 4, 10, tzinfo=dt.UTC)
СРЕДА = dt.date(2026, 10, 14)


def пара(ключ: str, день: dt.date, час_utc: int, минута_utc: int = 0) -> ItmoLesson:
    начало = dt.datetime.combine(день, dt.time(час_utc, минута_utc), tzinfo=dt.UTC)
    return ItmoLesson(
        source_key=ключ,
        lesson_date=день,
        starts_at=начало,
        ends_at=начало + dt.timedelta(hours=1, minutes=30),
        subject="Математический анализ",
        kind="Лекции",
        teacher="Иванов И. И.",
        room="ауд. 285",
        building="Кронверкский пр., 49",
        mode="Очный",
        online_url=None,
        fetched_at=СЕЙЧАС,
    )


def test_неделя_отдаёт_семь_дней(стенд: Стенд) -> None:
    стенд.сейчас = СЕЙЧАС

    ответ = стенд.клиент.get("/api/calendar", params={"view": "week"})

    assert ответ.status_code == 200
    тело = ответ.json()
    assert тело["period"] == {
        "view": "week",
        "starts_on": "2026-10-12",
        "ends_on": "2026-10-18",
        "today": "2026-10-14",
    }
    assert [д["date"] for д in тело["days"]] == [f"2026-10-{д}" for д in range(12, 19)]
    assert тело["timezone"] == "Europe/Moscow"


def test_день_это_умолчание_вокруг_сегодня(стенд: Стенд) -> None:
    стенд.сейчас = СЕЙЧАС

    тело = стенд.клиент.get("/api/calendar", params={"view": "day"}).json()

    assert [д["date"] for д in тело["days"]] == ["2026-10-14"]
    assert тело["days"][0]["is_today"] is True


def test_пара_приходит_со_смещением_зоны_owner(стенд: Стенд) -> None:
    """Клиент раскладывает блоки по стенным часам owner. Инвариант 7 в силе:
    время tz-aware, в базе UTC - смещение проставляется на выдаче."""
    стенд.сейчас = СЕЙЧАС
    стенд.сессия.add(пара("утренняя", СРЕДА, 7))
    стенд.сессия.flush()

    тело = стенд.клиент.get("/api/calendar", params={"view": "day"}).json()
    событие = тело["days"][0]["events"][0]

    assert событие["starts_at"] == "2026-10-14T10:00:00+03:00"
    assert (событие["source"], событие["title"]) == ("itmo", "Математический анализ")
    assert (событие["lesson_kind"], событие["room"]) == ("Лекции", "ауд. 285")
    assert событие["conflict"] is False
    # Поля чужого вида приходят как null, а не пропадают: клиент обязан
    # различать «поля нет у этого вида» и «поле не заполнено».
    assert событие["location"] is None


def test_день_за_окном_забора_помечен_как_без_данных(стенд: Стенд) -> None:
    """Пустая колонка обязана быть подписанной: «сохранённых данных нет» -
    это не то же самое, что «пар нет»."""
    стенд.сейчас = СЕЙЧАС
    стенд.сессия.add(пара("пара", СРЕДА, 7))
    стенд.сессия.flush()

    внутри = стенд.клиент.get("/api/calendar", params={"view": "day", "date": "2026-10-15"})
    снаружи = стенд.клиент.get("/api/calendar", params={"view": "day", "date": "2026-12-01"})

    assert внутри.json()["days"][0]["mirror_covers"] is True
    assert снаружи.json()["days"][0]["mirror_covers"] is False
    assert снаружи.status_code == 200


def test_давность_приходит_в_ответе(стенд: Стенд) -> None:
    стенд.сейчас = СЕЙЧАС + dt.timedelta(hours=13)
    стенд.сессия.add(пара("пара", СРЕДА, 7))
    стенд.сессия.flush()

    свежесть = стенд.клиент.get("/api/calendar").json()["freshness"]

    assert свежесть["state"] == "stale"
    assert свежесть["portal"] == "unknown"
    assert свежесть["covered_from"] == "2026-10-07"
    # Момент забора в зоне owner, как и времена событий: экран печатает его
    # словами, и UTC состарил бы расписание на три часа.
    assert свежесть["fetched_at"] == "2026-10-14T07:10:00+03:00"


def test_чужой_масштаб_это_422_в_нашем_формате(стенд: Стенд) -> None:
    ответ = стенд.клиент.get("/api/calendar", params={"view": "месяц"})

    assert ответ.status_code == 422
    тело = ответ.json()
    assert тело["code"] == "validation_error"
    assert тело["retryable"] is False
    assert any("view" in строка for строка in тело["details"])


def test_нечитаемая_дата_это_422_в_нашем_формате(стенд: Стенд) -> None:
    ответ = стенд.клиент.get("/api/calendar", params={"date": "тридцатое"})

    assert ответ.status_code == 422
    assert ответ.json()["code"] == "validation_error"


def test_сломанная_зона_в_настройках_это_503(стенд: Стенд) -> None:
    """Подставить UTC значило бы сдвинуть расписание на три часа молча.
    Текст называет зону: починка - одна строка UPDATE."""
    стенд.сессия.add(Setting(id=1, timezone="Мордор/Барад-Дур"))
    стенд.сессия.flush()

    ответ = стенд.клиент.get("/api/calendar")

    assert ответ.status_code == 503
    assert ответ.json()["code"] == "owner_timezone_invalid"
    assert "Мордор/Барад-Дур" in ответ.json()["message"]


def test_без_настроенной_базы_это_503_а_health_живёт(клиент_без_базы: TestClient) -> None:
    """Ровно то разделение, ради которого `DatabaseNotConfigured` заведён
    отдельным типом: контейнер поднялся, база не настроена."""
    отказ = клиент_без_базы.get("/api/calendar")
    живость = клиент_без_базы.get("/health")

    assert отказ.status_code == 503
    assert отказ.json()["code"] == "database_not_configured"
    assert живость.status_code == 200


def test_запрос_не_пишет_в_аудит(стенд: Стенд, сессия: Session) -> None:
    """Деградацию в `audit_log` пишет тот, кто её вызвал, а не тот, кто её
    показывает: отказ портала уже записан джобом. Строка на каждую
    перерисовку экрана превратила бы аудит в журнал доступа."""
    стенд.сейчас = СЕЙЧАС + dt.timedelta(days=2)
    стенд.сессия.add(пара("пара", СРЕДА, 7))
    стенд.сессия.flush()
    было = сессия.scalar(select(func.count()).select_from(AuditLogEntry))

    ответ = стенд.клиент.get("/api/calendar")

    assert ответ.json()["freshness"]["state"] == "stale"
    assert сессия.scalar(select(func.count()).select_from(AuditLogEntry)) == было


def test_запрос_не_поднимает_планировщик(стенд: Стенд) -> None:
    """`TestClient` без `with` не выполняет lifespan. Иначе тесты API
    поднимали бы APScheduler, а его догоняющий запуск ушёл бы в живой ИСУ."""
    стенд.клиент.get("/api/calendar")

    assert getattr(app.state, "планировщик", None) is None


def test_эндпоинт_не_корутина() -> None:
    """Движок синхронный: запрос к базе внутри `async def` заблокировал бы
    цикл событий вместе с `/health`, по которому compose судит о контейнере.
    Правило ruff ASYNC этого не ловит, поэтому проверка здесь."""
    assert inspect.iscoroutinefunction(сетка_календаря) is False

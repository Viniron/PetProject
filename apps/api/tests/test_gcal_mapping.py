"""Пара зеркала -> событие календаря (Э4). Ни базы, ни сети.

Главное здесь - `content_hash`. На нём держится идемпотентность: событие
не переписывается, пока хэш совпадает, поэтому хэш, не заметивший
изменения, оставляет в календаре устаревшую пару навсегда, а хэш,
меняющийся на ровном месте, каждый прогон переписывает восемьдесят
событий и жжёт квоту.
"""

import datetime as dt

import pytest

from jarvis_api.db.models import ItmoLesson
from jarvis_api.integrations.gcal.mapping import lesson_to_event

НАЧАЛО = dt.datetime(2026, 9, 10, 7, 0, tzinfo=dt.UTC)
КОНЕЦ = dt.datetime(2026, 9, 10, 8, 30, tzinfo=dt.UTC)


def пара(**переопределения: object) -> ItmoLesson:
    основа: dict[str, object] = {
        "source_key": "itmo:2026-09-10:10:00:abc",
        "lesson_date": dt.date(2026, 9, 10),
        "starts_at": НАЧАЛО,
        "ends_at": КОНЕЦ,
        "subject": "Математический анализ",
        "kind": "Лекции",
        "teacher": "Иванов И. И.",
        "room": "ауд. 2412",
        "building": "Кронверкский пр., 49",
        "mode": "Очный",
        "online_url": None,
        "fetched_at": НАЧАЛО,
    }
    основа.update(переопределения)
    return ItmoLesson(**основа)


def test_заголовок_несёт_вид_занятия() -> None:
    """Три пары по одному предмету подряд иначе неразличимы в календаре."""
    событие = lesson_to_event(пара())
    assert событие.summary == "Математический анализ · Лекции"


def test_без_вида_занятия_заголовок_это_предмет() -> None:
    assert lesson_to_event(пара(kind=None)).summary == "Математический анализ"


def test_место_собирается_из_корпуса_и_аудитории() -> None:
    assert lesson_to_event(пара()).location == "Кронверкский пр., 49, ауд. 2412"


def test_у_дистанционной_пары_место_это_ссылка() -> None:
    """В мобильном Google Calendar поле «место» кликабельно, описание - нет."""
    событие = lesson_to_event(
        пара(room=None, building=None, mode="Дистанционный", online_url="https://zoom.us/j/1")
    )
    assert событие.location == "https://zoom.us/j/1"


def test_пустые_поля_не_превращаются_в_прочерки() -> None:
    """Инвариант 9 в календаре: отсутствие данных - отсутствие строки."""
    событие = lesson_to_event(
        пара(teacher=None, mode=None, online_url=None, room=None, building=None)
    )
    assert событие.location is None
    assert событие.description is None
    тело = событие.body()
    assert "location" not in тело
    assert "description" not in тело


def test_описание_перечисляет_только_известное() -> None:
    описание = lesson_to_event(пара(mode=None, online_url=None)).description
    assert описание is not None
    assert "Преподаватель: Иванов И. И." in описание
    assert "Формат" not in описание
    assert "Поставлено JARVIS" in описание


def test_ключ_события_это_ключ_зеркала() -> None:
    """Второго ключа поверх первого не заводим - инвариант 5."""
    тело = lesson_to_event(пара()).body()
    приватные = тело["extendedProperties"]["private"]
    assert приватные["jarvis_key"] == "itmo:2026-09-10:10:00:abc"
    assert приватные["jarvis_source"] == "itmo"


def test_время_уходит_в_utc_с_буквой_z() -> None:
    """Пара 10:00 по Москве лежит в базе как 07:00Z и такой же уезжает."""
    тело = lesson_to_event(пара()).body()
    assert тело["start"]["dateTime"] == "2026-09-10T07:00:00Z"
    assert тело["end"]["dateTime"] == "2026-09-10T08:30:00Z"
    assert тело["start"]["timeZone"] == "UTC"


def test_наивное_время_отвергается() -> None:
    """Инвариант 7 проверяется на границе, а не доверяется коду выше."""
    наивная = пара(starts_at=dt.datetime(2026, 9, 10, 7, 0))  # noqa: DTZ001 - в этом и проверка
    with pytest.raises(ValueError, match="наивное время"):
        lesson_to_event(наивная).body()


def test_хэш_стабилен_на_одинаковом_входе() -> None:
    """Иначе каждый прогон переписывал бы весь семестр."""
    assert lesson_to_event(пара()).content_hash() == lesson_to_event(пара()).content_hash()


@pytest.mark.parametrize(
    "поле,значение",
    [
        ("room", "ауд. 2413"),
        ("subject", "Физика"),
        ("kind", "Практика"),
        ("teacher", "Петров П. П."),
        ("starts_at", dt.datetime(2026, 9, 10, 8, 0, tzinfo=dt.UTC)),
        ("online_url", "https://zoom.us/j/2"),
    ],
)
def test_хэш_замечает_изменение(поле: str, значение: object) -> None:
    """Каждое из этих изменений обязано переписать событие в календаре."""
    было = lesson_to_event(пара()).content_hash()
    стало = lesson_to_event(пара(**{поле: значение})).content_hash()
    assert было != стало, f"смена {поле} не изменила отпечаток события"


def test_хэш_не_замечает_время_забора() -> None:
    """`fetched_at` меняется каждый прогон и к содержанию события не относится.

    Попади он в отпечаток - джоб переписывал бы все восемьдесят событий
    ежедневно, ничего при этом не меняя.
    """
    было = lesson_to_event(пара()).content_hash()
    стало = lesson_to_event(
        пара(fetched_at=dt.datetime(2026, 9, 11, 3, 0, tzinfo=dt.UTC))
    ).content_hash()
    assert было == стало

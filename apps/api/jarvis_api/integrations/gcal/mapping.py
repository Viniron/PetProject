"""Пара зеркала -> событие Google Calendar.

Отдельно от джоба и от клиента по той же причине, что и отображение пар
портала (`integrations/itmo/mapping.py`): это единственная часть, которую
можно проверить целиком без базы и без сети.

Два решения стоят объяснения.

**Ключ события - это `source_key` зеркала, без переобёртки.** Он уже
детерминирован от данных источника и уже несёт префикс `itmo:`
(инвариант 5). Второй ключ поверх первого добавил бы место, где они
могут разойтись, и ни одной гарантии.

**Ничего не достраивается.** Пустая аудитория остаётся пустой, а не
превращается в "аудитория не указана": инвариант 9 запрещает
правдоподобную выдумку ровно так же в календаре, как на экране.
"""

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from jarvis_api.db.models import CalendarEvent, ItmoLesson
from jarvis_api.integrations.gcal.client import KEY_PROPERTY

# Что за источник создал событие. Пишется в приватные свойства рядом
# с ключом: по нему видно, чей это след, если календарь однажды придётся
# разбирать руками.
SOURCE_PROPERTY = "jarvis_source"
SOURCE_ITMO = "itmo"
# Подтверждённый черновик захвата (Э8). Второе значение появилось вместе
# со вторым наполняемым календарём: `JARVIS · ИТМО` пишет джоб расписания,
# `JARVIS · События` - подтверждение owner.
SOURCE_CAPTURE = "capture"


@dataclass(frozen=True, slots=True)
class DesiredEvent:
    """Событие, каким оно должно быть в календаре.

    Не ORM-объект: сравнение желаемого с фактическим не должно требовать
    ни сессии, ни базы. Строка `calendar_events` собирается из этого
    набора в джобе.
    """

    external_key: str
    summary: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    location: str | None
    description: str | None
    # Кто создал событие. Со значением по умолчанию, потому что источников
    # было ровно столько же, сколько наполняемых календарей, - один;
    # захват (Э8) стал вторым и передаёт его явно.
    source: str = SOURCE_ITMO

    def body(self) -> dict[str, Any]:
        """Тело запроса к Google.

        Время уходит в UTC - в базе другого нет (инвариант 7), а показывать
        его в местной зоне будет сам Google по настройке календаря.
        """
        тело: dict[str, Any] = {
            "summary": self.summary,
            "start": {"dateTime": _rfc3339(self.starts_at), "timeZone": "UTC"},
            "end": {"dateTime": _rfc3339(self.ends_at), "timeZone": "UTC"},
            "extendedProperties": {
                "private": {KEY_PROPERTY: self.external_key, SOURCE_PROPERTY: self.source}
            },
        }
        # Пустые поля не отправляются вовсе, а не отправляются пустыми:
        # `update` переписывает событие целиком, и отсутствие ключа - это
        # и есть "поля больше нет".
        if self.location:
            тело["location"] = self.location
        if self.description:
            тело["description"] = self.description
        return тело

    def content_hash(self) -> str:
        """Отпечаток того, что уедет в Google.

        Считается от тела запроса, а не от полей по отдельности: тогда
        поле, добавленное в `body()` и забытое здесь, не сможет тихо
        разъехаться с календарём. Ключи сортируются - иначе порядок
        словаря сделал бы хэш нестабильным между версиями Python.
        """
        сырое = json.dumps(self.body(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(сырое.encode("utf-8")).hexdigest()


def lesson_to_event(lesson: ItmoLesson) -> DesiredEvent:
    """Пара расписания в событие календаря.

    Заголовок несёт вид занятия: "Матанализ" в календаре на три пары
    подряд не отличает лекцию от лабораторной, а именно это и решает,
    куда идти.
    """
    заголовок = lesson.subject
    if lesson.kind:
        заголовок = f"{lesson.subject} · {lesson.kind}"

    return DesiredEvent(
        external_key=lesson.source_key,
        summary=заголовок,
        starts_at=lesson.starts_at,
        ends_at=lesson.ends_at,
        location=_место(lesson),
        description=_описание(lesson),
    )


def event_to_desired(строка: CalendarEvent) -> DesiredEvent:
    """Подтверждённое событие захвата в событие календаря (Э8).

    Отличие от пары расписания в том, чего здесь **нет**. У пары источник -
    портал, и заголовок с описанием собираются из его полей; здесь источник -
    сам owner, и всё, что он ввёл, уезжает как есть. Ни подписи «поставлено
    JARVIS», ни достроенного места: инвариант 9 запрещает выдумку в календаре
    так же, как на экране, а «Не указано» в поле места - это выдумка.
    """
    return DesiredEvent(
        external_key=строка.external_key,
        summary=строка.title,
        starts_at=строка.starts_at,
        ends_at=строка.ends_at,
        location=строка.location,
        description=строка.description,
        source=SOURCE_CAPTURE,
    )


def _место(lesson: ItmoLesson) -> str | None:
    """Где идёт пара.

    У дистанционной пары местом становится ссылка: в мобильном Google
    Calendar поле "место" кликабельно, а описание нужно сперва открыть.
    """
    части = [часть for часть in (lesson.building, lesson.room) if часть]
    if части:
        return ", ".join(части)
    if lesson.online_url:
        return lesson.online_url
    return None


def _описание(lesson: ItmoLesson) -> str | None:
    """Подробности пары.

    Собирается только из непустого. Строки "преподаватель: —" в описании
    быть не должно: отсутствие данных у портала - это отсутствие строки,
    а не строка про отсутствие.
    """
    строки: list[str] = []
    if lesson.teacher:
        строки.append(f"Преподаватель: {lesson.teacher}")
    if lesson.mode:
        строки.append(f"Формат: {lesson.mode}")
    if lesson.online_url:
        строки.append(f"Ссылка: {lesson.online_url}")
    if not строки:
        return None
    # Подпись в конце - чтобы owner, увидев событие в общем списке
    # календарей, понимал, кто его поставил и почему оно правится само.
    строки.append("")
    строки.append("Поставлено JARVIS из расписания my.itmo.ru")
    return "\n".join(строки)


def _rfc3339(момент: dt.datetime) -> str:
    if момент.tzinfo is None:
        raise ValueError("наивное время события (инвариант 7)")
    return момент.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")

"""Превращение пары портала в строку зеркала `itmo_lessons`.

Два решения этого файла стоят объяснения.

**Ключ строки детерминирован от данных источника** (инвариант хоста 5):
ни id строки, ни имени хоста, ни времени генерации. Проверяется буквально -
база, восстановленная из дампа, при первом же заборе обязана попасть
в те же строки, а не задвоить семестр.

**Время портала - местное и без смещения.** Портал отдаёт "10:00", и что это
за десять часов, в ответе не написано нигде. Референс жёстко подставляет
+03:00. Мы берём зону из `settings.timezone` (по умолчанию Europe/Moscow) -
не из вежливости к переезду, а потому что зона уже есть в базе и вторая
её копия в коде рано или поздно разойдётся с первой. В базу уходит UTC
(инвариант 7).
"""

import datetime as dt
import hashlib
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from jarvis_api.integrations.itmo.schema import RawLesson, SchedulePayload

# Префикс ключа. Нужен не для красоты: `source_key` попадает в логи и диффы
# дальше по этапам, и там должно быть видно, из какого источника строка.
KEY_PREFIX = "itmo"

# Сколько шестнадцатеричных знаков хэша оставляем в ключе. 16 знаков - это
# 64 бита; на нескольких тысячах пар за семестр вероятность совпадения
# исчезающе мала, а ключ остаётся читаемым глазами в дампе диффа.
KEY_HASH_CHARS = 16


class MappingError(ValueError):
    """Пара не отображается в строку зеркала.

    Отдельный тип, потому что это тот же класс беды, что и ошибка разбора:
    формат портала оказался не тем, на который мы рассчитывали.
    """


@dataclass(frozen=True, slots=True)
class MirrorRow:
    """Строка зеркала до записи в базу.

    Не ORM-объект намеренно: отображение проверяется тестами без базы вообще,
    а собрать `ItmoLesson` из этого набора - одна строка в джобе.

    `fetched_at` здесь нет: он один на весь забор и принадлежит прогону,
    а не паре. Держать его в строке значило бы сравнивать его при диффе.
    """

    source_key: str
    lesson_date: dt.date
    starts_at: dt.datetime
    ends_at: dt.datetime
    subject: str
    kind: str | None
    teacher: str | None
    room: str | None
    building: str | None
    mode: str | None
    online_url: str | None


def build_source_key(day: dt.date, lesson: RawLesson) -> str:
    """Ключ пары. Меняется только вместе с тем, что делает пару другой парой.

    В хэш идут предмет, вид занятия и группа; дата и начало - открытым
    текстом. Смена аудитории, преподавателя или ссылки на Zoom ключ **не**
    меняет: это та же пара с исправленными реквизитами, и она должна
    обновить строку, а не завести вторую рядом.

    Вид занятия в ключе нужен: лекция и практика по одному предмету
    в одно время у одной группы одновременно не идут, но в расписании
    портала такие пары встречаются как две записи, и без `type` они
    схлопнулись бы в одну.
    """
    начало = lesson.начало.strftime("%H:%M")
    отпечаток = "|".join(
        [
            lesson.subject.casefold(),
            (lesson.type or "").casefold(),
            (lesson.group or "").casefold(),
        ]
    )
    хэш = hashlib.sha256(отпечаток.encode("utf-8")).hexdigest()[:KEY_HASH_CHARS]
    return f"{KEY_PREFIX}:{day.isoformat()}:{начало}:{хэш}"


def _режим(lesson: RawLesson) -> str | None:
    """Очно или дистанционно.

    Порядок неслучаен. Если портал прислал поле формата - берём его как есть,
    это его собственное слово. Не прислал, но есть ссылка на Zoom - пара
    дистанционная, и это вывод из факта, а не догадка. Ни того, ни другого
    нет - оставляем пусто. Написать «очно» на основании отсутствия ссылки
    значило бы выдумать (инвариант 9): у очной пары просто не бывает признака.
    """
    if lesson.format:
        return lesson.format
    if lesson.zoom_url:
        return "online"
    return None


def lesson_to_row(day: dt.date, lesson: RawLesson, tz: ZoneInfo) -> MirrorRow:
    """Отображает одну пару. Падает, если пара не сходится сама с собой."""
    начало_местное = dt.datetime.combine(day, lesson.начало, tzinfo=tz)
    конец_местное = dt.datetime.combine(day, lesson.конец, tzinfo=tz)

    # Референс на этом месте молча переставляет концы местами с комментарием
    # «если в событии ошибка». Мы падаем. Причина в назначении: у него
    # календарь-подписка, где кривое событие лучше отсутствующего, у нас -
    # источник для записи в Google и для расчёта времени напоминаний (§4).
    # Пара, кончающаяся раньше начала, сдвинет напоминание, и молчаливое
    # исправление скроет, что портал отдаёт мусор.
    if конец_местное <= начало_местное:
        raise MappingError(
            f"пара {lesson.subject!r} {day.isoformat()} кончается в "
            f"{lesson.time_end} не позже начала {lesson.time_start}"
        )

    return MirrorRow(
        source_key=build_source_key(day, lesson),
        lesson_date=day,
        starts_at=начало_местное.astimezone(dt.UTC),
        ends_at=конец_местное.astimezone(dt.UTC),
        subject=lesson.subject,
        kind=lesson.type,
        # Портал непостоянен в названии поля. Пустая строка после
        # str_strip_whitespace - это тоже «нет преподавателя».
        teacher=lesson.teacher_name or lesson.teacher_fio or None,
        room=lesson.room or None,
        building=lesson.building or None,
        mode=_режим(lesson),
        online_url=lesson.zoom_url or None,
    )


def payload_to_rows(payload: SchedulePayload, tz: ZoneInfo) -> list[MirrorRow]:
    """Весь ответ портала в строки зеркала.

    Задвоенные ключи внутри одного ответа - отказ, а не «последний побеждает».
    Это ровно тот случай, когда наше представление о том, что делает пару
    уникальной, разошлось с действительностью, и узнать об этом надо сразу,
    а не по пропавшей паре в календаре.
    """
    строки: dict[str, MirrorRow] = {}
    for день in payload.data:
        for пара in день.lessons:
            строка = lesson_to_row(день.date, пара, tz)
            прежняя = строки.get(строка.source_key)
            if прежняя is not None:
                raise MappingError(
                    f"две пары портала дают один ключ {строка.source_key!r}: "
                    f"{прежняя.subject!r} и {строка.subject!r} - "
                    "состав ключа больше не различает пары"
                )
            строки[строка.source_key] = строка
    return sorted(строки.values(), key=lambda r: (r.starts_at, r.source_key))

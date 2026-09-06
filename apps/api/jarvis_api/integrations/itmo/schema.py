"""Модели ответа портала. Единственное место, где сырой JSON становится типами.

Строгость здесь - это реализация строки «формат ответа портала изменился →
джоб падает громко» из `SPEC.md` §10. Обязательное поле, которое портал
переименовал, даёт ошибку валидации; писать в календарь разобранный мусор
запрещено прямо.

`extra="ignore"`, а не `"forbid"`: новое поле в ответе безопасно и появляется
у портала регулярно, ронять на нём ночной джоб не за что. Опасна ровно
обратная перемена - исчезнувшее или переименованное поле, и её ловят
объявления обязательных полей ниже.

Ограничения длины повторяют ширину колонок `itmo_lessons`. Это не дубль
ради аккуратности: портал, отдавший килобайт в поле «аудитория», должен
уронить джоб на разборе, а не на INSERT - во втором случае в логе окажется
ошибка драйвера вместо имени поля.
"""

import datetime as dt
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Ширина колонок itmo_lessons (db/models.py). Продублирована сознательно:
# импортировать ORM в слой разбора внешнего формата значило бы связать их
# так, что схема портала начала бы зависеть от нашей схемы БД.
SHORT = 64
MEDIUM = 256
LONG = 2048

# Портал отдаёт время без даты и без смещения: "10:00" либо "10:00:00".
# Смещения нет вообще - подразумевается местное время (см. mapping.py).
_TIME_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?$")

Короткое = Annotated[str, Field(max_length=SHORT)]
Среднее = Annotated[str, Field(max_length=MEDIUM)]
Длинное = Annotated[str, Field(max_length=LONG)]


def parse_portal_time(value: str) -> dt.time:
    """Разбирает "10:00" и "10:00:00". Всё прочее - отказ."""
    совпадение = _TIME_RE.match(value.strip())
    if совпадение is None:
        raise ValueError(f"время {value!r} не в формате ЧЧ:ММ портала")
    часы = int(совпадение.group("h"))
    минуты = int(совпадение.group("m"))
    секунды = int(совпадение.group("s") or 0)
    if часы > 23 or минуты > 59 or секунды > 59:
        raise ValueError(f"время {value!r} вне суток")
    return dt.time(hour=часы, minute=минуты, second=секунды)


class RawLesson(BaseModel):
    """Одна пара в том виде, в каком её отдаёт портал.

    Обязательных полей три: без названия, начала и конца пара не является
    парой, и подставить их неоткуда. Всё остальное портал регулярно оставляет
    пустым - у дистанционного занятия нет аудитории, у практики не всегда
    указан преподаватель.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    subject: Среднее
    time_start: str
    time_end: str

    # Вид занятия: "Лекции", "Практические занятия", "Лабораторные занятия".
    # Список у портала свой, переводить его в свой словарь не будем - это
    # знание о предметной области, а не о формате.
    type: Короткое | None = None

    # Два имени одного поля. Референс читает оба, и это не перестраховка:
    # портал непостоянен в названии. Какое из них взять, решает mapping.
    teacher_name: Среднее | None = None
    teacher_fio: Среднее | None = None

    room: Среднее | None = None
    building: Среднее | None = None
    group: Среднее | None = None
    note: Длинное | None = None

    # Дистанционная пара. `zoom_url` - и признак формата, и сама ссылка.
    zoom_url: Длинное | None = None
    zoom_password: Короткое | None = None
    zoom_info: Длинное | None = None

    # Поле формата в ответе портала referenced-код не читает вовсе, и в живом
    # ответе его может не быть. Объявлено необязательным именно поэтому:
    # если придёт - используем, не придёт - выведем формат по zoom_url,
    # а не выдумаем (инвариант 9).
    format: Короткое | None = None

    @field_validator("time_start", "time_end")
    @classmethod
    def _время_разбирается(cls, value: str) -> str:
        parse_portal_time(value)
        return value

    @property
    def начало(self) -> dt.time:
        return parse_portal_time(self.time_start)

    @property
    def конец(self) -> dt.time:
        return parse_portal_time(self.time_end)


class RawDay(BaseModel):
    """День расписания: дата и список пар.

    `lessons=None` встречается у пустого дня, поэтому список необязателен -
    но пустым, а не отсутствующим: дальше по коду он всегда список.
    """

    model_config = ConfigDict(extra="ignore")

    date: dt.date
    lessons: list[RawLesson] = Field(default_factory=list)

    @field_validator("lessons", mode="before")
    @classmethod
    def _пустой_день_это_пустой_список(cls, value: object) -> object:
        return [] if value is None else value


class SchedulePayload(BaseModel):
    """Ответ `/schedule/schedule/personal` целиком.

    Обёртка `{"data": [...]}` - тот самый признак, по которому видно, что
    ответ вообще от портала, а не страница ошибки прокси. Поэтому `data`
    обязательна: её отсутствие - это изменившийся формат.
    """

    model_config = ConfigDict(extra="ignore")

    data: list[RawDay]

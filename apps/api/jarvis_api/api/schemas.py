"""Схемы ответов и тел запросов - то есть сам контракт (Э6).

Все модели в одном файле намеренно: вместе они составляют контракт, и при
сверке `packages/contracts/openapi.json` их приходится читать целиком.
Разнесённые по роутерам, они заставляли бы искать по двум местам.

**Имена классов латиницей.** Остальной код проекта по-русски, но имена
моделей Pydantic попадают в контракт именами схем OpenAPI, а из них генератор
делает имена типов TypeScript: кириллица там формально законна и практически
ломает автодополнение и генераторы клиентов. Ключи полей - тоже латиницей
и snake_case, как колонки базы. Внутри строки остаются русскими: причина
периода - это то, что owner напечатал.

`from_attributes` у каждой модели: ответы собираются из датаклассов
`jarvis_api.domain`, а не из словарей. Слой `domain` про Pydantic не знает
и знать не должен - его функции проверяются без HTTP.
"""

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis_api.domain.calendar import Вид, Источник, СостояниеДавности, СостояниеПортала
from jarvis_api.domain.capture import ПРЕДЕЛ_МЕСТА, ПРЕДЕЛ_НАЗВАНИЯ, Модальность
from jarvis_api.domain.day_flags import АВТОПОМЕТКА, ПРЕДЕЛ_ЗАМЕТКИ, ПРЕДЕЛ_ПРИЧИНЫ


class EventOut(BaseModel):
    """Блок сетки: пара университета или личное событие.

    Поля разных источников не сливаются в одно: у пары есть вид занятия
    и аудитория, у личного события - место и заметка. Пустое поле приходит
    как `null`, а не пропадает (`exclude_none` не используется): клиент
    обязан различать «поля нет в этом виде события» и «поле не заполнено».

    Значения `source` - два: `itmo` и `event`. Занятий курсов (`study`)
    в календарном релизе нет: их время считается по манифесту курса (§4),
    который уехал в дорожку курсов вместе с ADR-027 п.11.
    """

    model_config = ConfigDict(from_attributes=True)

    key: str = Field(description="Стабильный ключ источника: source_key пары или external_key")
    source: Источник
    starts_at: dt.datetime = Field(description="Начало, приведённое к зоне owner")
    ends_at: dt.datetime
    title: str
    lesson_kind: str | None = Field(default=None, description="Вид занятия: лекция, практика")
    teacher: str | None = None
    room: str | None = None
    building: str | None = None
    mode: str | None = Field(default=None, description="Формат портала: Очный, Дистанционный")
    online_url: str | None = None
    location: str | None = Field(default=None, description="Место личного события")
    description: str | None = None
    conflict: bool = Field(description="Пересекается по времени с другим событием периода")


class DayFlagOut(BaseModel):
    """Период исключений, попавший на этот день (§2.5)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    reason: str = Field(description="Свободное слово owner либо late_classes от системы")
    note: str | None = None
    starts_on: dt.date
    ends_on: dt.date = Field(description="Включительно: период «с 1 по 7» содержит седьмое")
    manual: bool = Field(description="Внесён owner, а не проставлен системой")


class DayOut(BaseModel):
    """День сетки.

    `mirror_covers` отвечает на вопрос, который иначе неотличим от пустоты:
    пустой день внутри окна забора - «ничего не запланировано», а день,
    до которого забор не доходил, - «сохранённых данных нет». Схлопнуть их
    в один пустой массив значило бы соврать (инвариант 9).
    """

    model_config = ConfigDict(from_attributes=True)

    date: dt.date
    is_today: bool
    mirror_covers: bool = Field(description="День попадает в окно последнего успешного забора")
    flags: list[DayFlagOut]
    events: list[EventOut]


class FreshnessOut(BaseModel):
    """Свежесть расписания (§10).

    Полосу давности показывает `state`; `portal` только объясняет причину.
    Фразу собирает клиент: макет печатает один и тот же момент двумя
    форматами - «от 12 октября, 07:10» в полосе и «от 12.10» на блоке пары.
    """

    model_config = ConfigDict(from_attributes=True)

    state: СостояниеДавности = Field(description="never - забор ещё не проходил ни разу")
    fetched_at: dt.datetime | None = Field(description="Последний успешный ответ портала")
    portal: СостояниеПортала = Field(description="unknown - прогонов забора в журнале нет")
    covered_from: dt.date | None = Field(description="Окно вокруг даты забора, включительно")
    covered_to: dt.date | None


class PeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    view: Вид
    starts_on: dt.date
    ends_on: dt.date = Field(description="Включительно")
    today: dt.date = Field(description="Сегодня в зоне owner, независимо от периода")


class CalendarOut(BaseModel):
    """Сетка календаря на период: ответ `GET /api/calendar`."""

    model_config = ConfigDict(from_attributes=True)

    period: PeriodOut
    timezone: str = Field(description="Зона owner из settings, в которой приведены моменты")
    freshness: FreshnessOut
    days: list[DayOut] = Field(description="Один элемент на день периода, по порядку")


class DayFlagIn(BaseModel):
    """Тело `POST /api/day-flags`.

    Проверки живут здесь, а не в базе: словарь причин будет меняться, а CHECK
    ради нового слова ничего не защищает. В базе остаются два ограничения,
    которые пережить нельзя, - порядок дат и уникальность автопометки на день.
    """

    starts_on: dt.date
    ends_on: dt.date
    reason: str = Field(min_length=1, max_length=ПРЕДЕЛ_ПРИЧИНЫ)
    note: str | None = Field(default=None, max_length=ПРЕДЕЛ_ЗАМЕТКИ)

    @field_validator("reason")
    @classmethod
    def _причина_не_пустая_и_не_системная(cls, значение: str) -> str:
        """Пробелы причиной не считаются, `late_classes` руками не вносится.

        Второе - не педантизм: эту пометку ставит и снимает джоб расчёта
        времени занятий (§4), а его в календарном релизе нет. Внесённая
        руками, она обещала бы поведение, которого не существует.
        """
        очищенное = значение.strip()
        if not очищенное:
            raise ValueError("причина не может быть пустой")
        if очищенное == АВТОПОМЕТКА:
            raise ValueError(
                f"причину {АВТОПОМЕТКА!r} ставит и снимает система (SPEC §4), "
                "руками она не вносится"
            )
        return очищенное

    @model_validator(mode="after")
    def _период_не_задом_наперёд(self) -> "DayFlagIn":
        """CHECK `range_ordered` в базе остаётся страховкой, а не проверкой.

        Отказ обязан быть внятным и назвать обе даты: сообщение драйвера
        о нарушенном ограничении owner ничего не объясняет.
        """
        if self.ends_on < self.starts_on:
            raise ValueError(f"период задом наперёд: с {self.starts_on} по {self.ends_on}")
        return self


class DayFlagsOut(BaseModel):
    """Ответ `GET /api/day-flags`.

    Объектом, а не голым массивом: массив на верхнем уровне нельзя расширить
    ни одним полем, не сломав клиента, - а список периодов рано или поздно
    захочет отдавать границы запрошенного интервала.
    """

    flags: list[DayFlagOut]


class CaptureDraftIn(BaseModel):
    """Тело `POST /api/capture/drafts` (Э8, §8.4).

    Модальность объявлена всеми тремя значениями, хотя принимается одна:
    контракт обязан показывать, что режимов три, а отказ по фотографии
    и голосу - назвать причину. Спрятать их из перечисления значило бы
    описать продукт, которого не задумывали (решение owner 2026-09-17:
    режимы видны, но погашены).
    """

    modality: Модальность = Field(default="text", description="Вид входа: текст, фото или голос")
    text: str | None = Field(
        default=None,
        description="Что вставили или напечатали. Обязателен для modality=text",
    )

    @field_validator("text")
    @classmethod
    def _текст_не_пустой(cls, значение: str | None) -> str | None:
        """Пробелы текстом не считаются.

        Пустой черновик прошёл бы до формы подтверждения и превратился бы
        в событие из ничего: разбирать нечего, поля пустые, а запись
        в календарь при этом законна.
        """
        if значение is None:
            return None
        очищенное = значение.strip()
        if not очищенное:
            raise ValueError("текст захвата пуст")
        return очищенное

    @model_validator(mode="after")
    def _текстовому_входу_нужен_текст(self) -> "CaptureDraftIn":
        if self.modality == "text" and self.text is None:
            raise ValueError("для modality=text поле text обязательно")
        return self


class CaptureDraftOut(BaseModel):
    """Черновик захвата.

    `extracted` и `error` приходят пустыми до слоя моделей (Э12), и это
    состояние клиент обязан различать: пустой разбор означает «поля
    заполняет owner», а не «модель ничего не нашла».
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    modality: Модальность
    source_text: str | None
    extracted: dict[str, Any] | None = Field(
        default=None, description="Структура от модели. До слоя моделей - null"
    )
    error: str | None = Field(default=None, description="Разбор не удался - причина")
    created_at: dt.datetime


class CaptureDraftsOut(BaseModel):
    """Ответ `GET /api/capture/drafts`.

    Объектом, а не голым массивом, по той же причине, что и периоды:
    массив на верхнем уровне нельзя расширить ни одним полем, не сломав
    клиента.
    """

    drafts: list[CaptureDraftOut]


class CaptureConfirmIn(BaseModel):
    """Тело `POST /api/capture/drafts/{id}/confirm` - форма подтверждения.

    Дата и время обязательны и не угадываются. `CLAUDE.md` требует этого
    прямо: нет даты или низкая уверенность - спросить, а не подставить
    правдоподобное. Поэтому у полей нет умолчаний вроде «сегодня» и «час
    от текущего момента»: событие без времени в календарь не попадает.
    """

    title: str = Field(min_length=1, max_length=ПРЕДЕЛ_НАЗВАНИЯ)
    starts_at: dt.datetime
    ends_at: dt.datetime
    location: str | None = Field(default=None, max_length=ПРЕДЕЛ_МЕСТА)
    description: str | None = None

    @field_validator("title")
    @classmethod
    def _название_не_пустое(cls, значение: str) -> str:
        очищенное = значение.strip()
        if not очищенное:
            raise ValueError("название события не может быть пустым")
        return очищенное

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _время_с_зоной(cls, значение: dt.datetime) -> dt.datetime:
        """Инвариант 7: наивного времени в системе нет.

        Момент без зоны от клиента - это «17:00 неизвестно где». Принять
        его значило бы записать событие в UTC и показать owner на три часа
        раньше; отказ с внятным текстом честнее.
        """
        if значение.tzinfo is None:
            raise ValueError("время без часового пояса (инвариант 7): ожидается ISO со смещением")
        return значение

    @model_validator(mode="after")
    def _конец_позже_начала(self) -> "CaptureConfirmIn":
        """Событие нулевой длины тоже отвергается.

        Смежные события конфликтом не считаются (§8.4), но событие,
        которое кончается в момент своего начала, - это не расписание,
        а опечатка в одном из двух полей.
        """
        if self.ends_at <= self.starts_at:
            raise ValueError(
                f"событие кончается не позже начала: {self.starts_at} .. {self.ends_at}"
            )
        return self


class CapturedEventOut(BaseModel):
    """Событие, созданное из черновика: ответ на подтверждение.

    `sync_state` здесь не служебная подробность, а то, что экран показывает
    словами. Событие принято и лежит в базе (инвариант 1: backend -
    источник истины), но в Google оно попадает отдельным шагом, и до тех
    пор честный ответ - «записывается», а не готовое (инвариант 9).
    """

    model_config = ConfigDict(from_attributes=True)

    key: str = Field(description="external_key события: capture:<id черновика>")
    title: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    location: str | None
    description: str | None
    sync_state: str = Field(description="pending - в очереди в Google, synced - записано")
    synced_at: dt.datetime | None

"""Сетка календаря: период, что попадает в день, конфликты, давность (§8.4, §10).

Тут нет ни FastAPI, ни Pydantic намеренно. Всё, что этот модуль считает,
проверяется прямым вызовом функции: границы недели по зоне owner, покрытие
зеркала, наложения, порог давности. Через `TestClient` это проверялось бы
медленнее и с меньшей точностью - а ошибка на день в границах окна тихая.

Три решения этого модуля стоит держать в голове при чтении.

**Пары группируются по `lesson_date`, личные события - по `starts_at`
в зоне owner.** Расхождение сознательное: модель прямо запрещает выводить
дату пары из `starts_at` (`db/models.py`), иначе выборка «пары на такой-то
день» зависит от зоны, в которой выполняется запрос. Следствие: при
экзотической зоне owner пара и личное событие одного вечера могут оказаться
в соседних днях. Принято, потому что альтернатива хуже.

**Покрытие зеркала считается от даты забора, а не от сегодняшнего дня.**
Успешный забор проставляет `fetched_at` всем строкам своего окна (§10.1),
значит покрыто окно вокруг **той** даты. Портал молчит третьи сутки - хвост
окна никогда не загружался, и день оттуда обязан прийти с `mirror_covers =
False`. Считать от сегодня значило бы соврать «пар нет» про день, про
который мы не спрашивали.

**Пустота бывает трёх видов, и они различимы:** день внутри покрытия без пар
(«ничего не запланировано»), день вне покрытия («сохранённых данных нет»)
и пустое зеркало целиком (`state = never`). Инвариант 9 запрещает и падать,
и врать; схлопнуть эти три в один пустой массив - это второе.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import CalendarEvent, ItmoLesson, JobRun
from jarvis_api.domain.day_flags import Пометка, из_строки, пометки
from jarvis_api.jobs.common import owner_timezone, sync_window, границы_окна

# Имя джоба забора в `job_runs`. Импортируется из самого джоба, а не
# перепечатывается строкой: переименование джоба иначе молча сломало бы
# признак «портал не отвечает», и экран стал бы врать о свежести.
from jarvis_api.jobs.sync_itmo import JOB_NAME as ДЖОБ_ЗАБОРА

Вид = Literal["day", "week"]
Источник = Literal["itmo", "event"]
СостояниеДавности = Literal["fresh", "stale", "never"]
СостояниеПортала = Literal["ok", "failing", "unknown"]

# Календарь личных событий из трёх календарей JARVIS. Фильтр по нему
# обязателен: в `calendar_events` лежит ещё и журнал записи пар в Google
# (`calendar = 'itmo'`), и без фильтра каждая пара пришла бы дважды - из
# зеркала и из журнала - и конфликтовала бы сама с собой, то есть «⚠»
# встало бы на весь семестр. Тот же фильтр держит вне выдачи `study`:
# занятий курсов в календарном релизе нет (§4 уехал в дорожку курсов).
КАЛЕНДАРЬ_СОБЫТИЙ = "events"


@dataclass
class Событие:
    """Блок сетки. Изменяемый: `conflict` проставляется после сборки списка."""

    key: str
    source: Источник
    starts_at: dt.datetime
    ends_at: dt.datetime
    title: str
    lesson_kind: str | None = None
    teacher: str | None = None
    room: str | None = None
    building: str | None = None
    mode: str | None = None
    online_url: str | None = None
    location: str | None = None
    description: str | None = None
    conflict: bool = False


@dataclass(frozen=True)
class Давность:
    """Свежесть расписания. Показывать полосу или нет - решает `state`."""

    state: СостояниеДавности
    fetched_at: dt.datetime | None
    portal: СостояниеПортала
    covered_from: dt.date | None
    covered_to: dt.date | None


@dataclass
class День:
    date: dt.date
    is_today: bool
    mirror_covers: bool
    flags: list[Пометка] = field(default_factory=list)
    events: list[Событие] = field(default_factory=list)


@dataclass(frozen=True)
class Период:
    view: Вид
    starts_on: dt.date
    ends_on: dt.date
    today: dt.date


@dataclass(frozen=True)
class Сетка:
    period: Период
    timezone: str
    freshness: Давность
    days: list[День]


def сегодня(сейчас: dt.datetime, зона: ZoneInfo) -> dt.date:
    """«Сегодня» у owner, а не в зоне контейнера.

    Правило DTZ линтера запрещает `date.today()` именно поэтому: на плате,
    живущей в UTC, оно после 21:00 по Москве отдавало бы вчерашний день.
    """
    return сейчас.astimezone(зона).date()


def границы_периода(view: Вид, дата: dt.date) -> tuple[dt.date, dt.date]:
    """Границы периода, включительно с обеих сторон.

    Неделя - понедельник-воскресенье, как в утверждённом макете. Локаль
    и `calendar.firstweekday` не используются: первый день недели задан
    макетом, а не настройкой машины, на которой запущен процесс.
    """
    if view == "day":
        return дата, дата
    понедельник = дата - dt.timedelta(days=дата.weekday())
    return понедельник, понедельник + dt.timedelta(days=6)


def пары(session: Session, начало: dt.date, конец: dt.date) -> list[ItmoLesson]:
    """Пары зеркала внутри периода. Границы включительные."""
    запрос = (
        select(ItmoLesson)
        .where(ItmoLesson.lesson_date >= начало, ItmoLesson.lesson_date <= конец)
        .order_by(ItmoLesson.starts_at, ItmoLesson.source_key)
    )
    return list(session.scalars(запрос))


def личные_события(
    session: Session, начало: dt.datetime, конец: dt.datetime
) -> list[CalendarEvent]:
    """Личные события по моментам, а не по датам.

    `starts_at` - `timestamptz`, и сравнивать его с `date` нельзя: границы
    периода переводятся в моменты UTC через `границы_окна`, где конец
    полуоткрыт. Иначе событие в 23:00 последнего дня выпадало бы из выдачи.
    """
    запрос = (
        select(CalendarEvent)
        .where(
            CalendarEvent.calendar == КАЛЕНДАРЬ_СОБЫТИЙ,
            CalendarEvent.starts_at >= начало,
            CalendarEvent.starts_at < конец,
        )
        .order_by(CalendarEvent.starts_at, CalendarEvent.external_key)
    )
    return list(session.scalars(запрос))


def последний_забор(session: Session) -> dt.datetime | None:
    """Когда портал последний раз подтвердил расписание.

    Максимум по **всей** таблице, а не по строкам запрошенного периода:
    запрос за пустой месяц вперёд дал бы `None`, и экран сообщил бы
    «расписание никогда не загружалось» при полной базе. Максимум по таблице
    равен времени последнего успешного ответа портала, потому что успешный
    забор обновляет `fetched_at` у всех строк окна, включая неизменившиеся.
    """
    момент: dt.datetime | None = session.scalar(select(func.max(ItmoLesson.fetched_at)))
    return момент


def состояние_портала(session: Session) -> СостояниеПортала:
    """Отвечает ли портал, по следу последнего прогона забора.

    `unknown`, а не `failing`, когда строк нет вовсе: `job_runs` пуст законно
    на чистой плате и на базе, только что восстановленной из дампа. Сказать
    в этом случае «портал не отвечает» значило бы утверждать непроверенное.
    """
    запрос = (
        select(JobRun)
        .where(JobRun.job == ДЖОБ_ЗАБОРА)
        .order_by(JobRun.run_date.desc(), JobRun.started_at.desc())
        .limit(1)
    )
    строка = session.scalars(запрос).one_or_none()
    if строка is None:
        return "unknown"
    return "failing" if строка.status == "failed" else "ok"


def давность(session: Session, settings: Settings, сейчас: dt.datetime, зона: ZoneInfo) -> Давность:
    """Свежесть расписания и окно, которое забор успел подтвердить.

    `state` решает, показывать ли полосу давности; `portal` только объясняет
    причину. Разделение не косметическое: прогон, упавший десять минут назад
    при успехе сорока минутами раньше, полосой пугать не должен - данные
    действительно свежие, и полоса в этом случае была бы ложной тревогой.
    """
    забор = последний_забор(session)
    портал = состояние_портала(session)

    if забор is None:
        # `never` и `stale` не схлопываются: про пустое зеркало сказать
        # «данные от такого-то числа» нечем, и фразы у них разные.
        return Давность(
            state="never",
            fetched_at=None,
            portal=портал,
            covered_from=None,
            covered_to=None,
        )

    покрыто_с, покрыто_по = sync_window(settings, забор.astimezone(зона).date())
    порог = dt.timedelta(hours=settings.calendar_stale_after_hours)
    состояние: СостояниеДавности = "stale" if сейчас - забор > порог else "fresh"
    return Давность(
        state=состояние,
        # В зоне owner, как и времена событий: экран печатает этот момент
        # словами («от 12 октября, 07:10»), и момент в UTC означал бы
        # расписание, устаревшее на три часа раньше, чем на самом деле.
        fetched_at=забор.astimezone(зона),
        portal=портал,
        covered_from=покрыто_с,
        covered_to=покрыто_по,
    )


def _конец_для_сравнения(событие: Событие) -> dt.datetime:
    """Конец, на который можно опереться в арифметике.

    CHECK на порядок времён есть только у `day_flags`, поэтому событие
    с концом раньше начала физически возможно. Времена наружу отдаются
    настоящими: молча поменять их местами - это соврать о данных.
    """
    return max(событие.ends_at, событие.starts_at)


def пометить_конфликты(события: Sequence[Событие]) -> None:
    """Проставить `conflict` каждому событию, которое с чем-то пересекается.

    Интервалы полуоткрытые, неравенства строгие: пары 10:00-11:30 и
    11:40-13:10 не конфликтуют, и встык 14:40/14:40 - тоже. Нестрогое
    сравнение пометило бы «⚠» весь учебный день, и пометка перестала бы
    что-либо значить.

    Считается по всему периоду сразу, а не по каждому дню отдельно: событие
    через полночь иначе не увидело бы наложения с событием следующего дня.
    Событие нулевой длительности в расчёте не участвует - пустой интервал
    не пересекается ни с чем, а «минимум одна минута» было бы придуманным
    числом. Перевёрнутое событие сюда попадает как нулевое по той же причине.
    """
    участники = [с for с in события if _конец_для_сравнения(с) > с.starts_at]
    участники.sort(key=lambda с: (с.starts_at, _конец_для_сравнения(с)))

    # Проход с активным множеством. События отсортированы по началу, поэтому
    # активное пересекается с текущим тогда и только тогда, когда его конец
    # позже начала текущего - второе неравенство выполнено сортировкой.
    активные: list[Событие] = []
    for текущее in участники:
        активные = [a for a in активные if _конец_для_сравнения(a) > текущее.starts_at]
        if активные:
            текущее.conflict = True
            for сосед in активные:
                сосед.conflict = True
        активные.append(текущее)


def _из_пары(пара: ItmoLesson, зона: ZoneInfo) -> Событие:
    return Событие(
        key=пара.source_key,
        source="itmo",
        starts_at=пара.starts_at.astimezone(зона),
        ends_at=пара.ends_at.astimezone(зона),
        title=пара.subject,
        lesson_kind=пара.kind,
        teacher=пара.teacher,
        room=пара.room,
        building=пара.building,
        mode=пара.mode,
        online_url=пара.online_url,
    )


def _из_события(запись: CalendarEvent, зона: ZoneInfo) -> Событие:
    return Событие(
        key=запись.external_key,
        source="event",
        starts_at=запись.starts_at.astimezone(зона),
        ends_at=запись.ends_at.astimezone(зона),
        title=запись.title,
        location=запись.location,
        description=запись.description,
    )


def собрать(
    session: Session,
    settings: Settings,
    сейчас: dt.datetime,
    view: Вид,
    дата: dt.date | None = None,
) -> Сетка:
    """Сетка календаря на период.

    Зона owner читается из базы и может оказаться неизвестной - тогда
    `owner_timezone` бросает `OwnerZoneError`, и это правильно: подставить
    UTC значило бы сдвинуть всё расписание на три часа молча. Переводит
    отказ в честный 503 слой `api/`, а не этот модуль.
    """
    зона = owner_timezone(session)
    день_сегодня = сегодня(сейчас, зона)
    начало, конец = границы_периода(view, дата if дата is not None else день_сегодня)

    с_момента, по_момент = границы_окна(начало, конец, зона)
    размещённые: list[tuple[dt.date, Событие]] = [
        (пара.lesson_date, _из_пары(пара, зона)) for пара in пары(session, начало, конец)
    ]
    размещённые += [
        (запись.starts_at.astimezone(зона).date(), _из_события(запись, зона))
        for запись in личные_события(session, с_момента, по_момент)
    ]

    # Конфликты - по всему периоду, до раскладки по дням: см. докстринг.
    пометить_конфликты([событие for _, событие in размещённые])

    свежесть = давность(session, settings, сейчас, зона)
    все_пометки = [из_строки(строка) for строка in пометки(session, начало, конец)]

    дни: list[День] = []
    номер = начало
    while номер <= конец:
        дни.append(
            День(
                date=номер,
                is_today=номер == день_сегодня,
                mirror_covers=(
                    свежесть.covered_from is not None
                    and свежесть.covered_to is not None
                    and свежесть.covered_from <= номер <= свежесть.covered_to
                ),
                flags=[п for п in все_пометки if п.starts_on <= номер <= п.ends_on],
                events=[событие for день, событие in размещённые if день == номер],
            )
        )
        номер += dt.timedelta(days=1)

    return Сетка(
        period=Период(view=view, starts_on=начало, ends_on=конец, today=день_сегодня),
        timezone=str(зона),
        freshness=свежесть,
        days=дни,
    )

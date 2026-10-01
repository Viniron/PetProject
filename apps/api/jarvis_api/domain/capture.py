"""Захват события: черновик, подтверждение, строка календаря (Э8, §8.4, §9).

Путь один и обязательный: **вход → черновик → подтверждение → запись**
(`CLAUDE.md`). Без подтверждения наружу не уходит ничего, поэтому черновик -
не удобство, а сущность: между «owner что-то прислал» и «в календаре
появилось событие» обязан быть шаг, на котором owner видит, что именно
запишется.

**Разбора здесь нет** - он в `capture_parse.py` (Э12в). Этот модуль хранит
то, что разбор вернул: поля в `extracted` или причину отказа в `error`.
Пустой `extracted` - честное «не разобрано», и тогда поля события приходят
с формы подтверждения, а не заготовкой с выдуманными датой и временем
(инвариант 9).

**Ключ события детерминирован от идентификатора захвата** (ADR-042,
инвариант 5): `capture:<uuid черновика>`. Идентификатор рождается здесь,
в коде, а не в базе, и не выводится из содержимого. Из этого следуют две
вещи, ради которых он такой: повторное подтверждение после обрыва связи
попадает в то же событие вместо второго такого же, а правка названия или
времени однажды не превратится в дубль в Google.
"""

import datetime as dt
import io
import uuid
from dataclasses import dataclass
from typing import Literal, cast

import pdfplumber
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.db.models import AuditLogEntry, CalendarEvent, CaptureBlob, CaptureDraft

# Модальности входа. Повторяют CHECK `modality_known` в схеме: список в двух
# местах, потому что база защищает от мусора, а этот тип - от опечатки
# в коде и даёт внятный 422 вместо IntegrityError из драйвера. В контракте
# он становится перечислением.
Модальность = Literal["text", "image", "audio"]

# Входы, которые приходят текстом. Голос - тоже текст: его расшифровывает
# браузер (ADR-046), и сырого звука у сервера нет вовсе, хранить нечего.
# Модальность `audio` при этом сохраняется: модель читает расшифровку иначе,
# чем напечатанное, и в аудите видно, откуда пришло событие.
ТекстоваяМодальность = Literal["text", "audio"]

# Форматы файла захвата по сигнатуре. Заголовок `Content-Type` от клиента
# не проверяется вовсе: его пишет браузер по расширению, а модели уходит
# `media_type`, и неверный тип отвергается провайдером уже за деньги.
# Список - пересечение того, что принимают оба назначенных провайдера
# (ADR-046, ADR-054). HEIC сюда не входит: Anthropic его не принимает,
# а Safari на iPhone при выборе файла и так отдаёт JPEG.
#
# PDF хранится с модальностью `image` (решение owner 2026-09-30): для
# черновика это тот же визуальный вход, а различие живёт в `mime_type`
# сырья и в том, каким блоком адаптер отдаёт его модели.
PDF = "application/pdf"
СИГНАТУРЫ: tuple[tuple[bytes, str], ...] = (
    (bytes.fromhex("ffd8ff"), "image/jpeg"),
    (bytes.fromhex("89504e470d0a1a0a"), "image/png"),
    (b"%PDF-", PDF),
)


class ФайлНеЧитается(ValueError):
    """PDF не открылся или защищён. Текст - для owner, без имён библиотек."""


# Префикс ключа события захвата (ADR-042). Виден в `extendedProperties`
# события Google, поэтому это ещё и ответ на вопрос «кто это создал»,
# если календарь придётся разбирать руками.
ПРЕФИКС_КЛЮЧА = "capture:"

# Календарь и источник строки `calendar_events`. `events` - это
# `settings.gcal_events_id`, то есть `JARVIS · События` (Э4); `capture` -
# одно из двух значений CHECK `source_known`.
КАЛЕНДАРЬ = "events"
ИСТОЧНИК = "capture"

АКТОР = "api.capture"

# Пределы полей повторяют колонки (`db/models.py`). Как и в `day_flags`,
# они здесь ради внятного 422: длинное название иначе приходит DataError
# из драйвера, и owner читает сообщение про VARCHAR.
ПРЕДЕЛ_НАЗВАНИЯ = 256
ПРЕДЕЛ_МЕСТА = 256


@dataclass(frozen=True)
class Черновик:
    """Черновик в том виде, в каком его отдают наружу.

    Сырья (`bytea`) здесь нет и не будет: список черновиков не должен
    читать фотографии, ради чего они и вынесены в отдельную таблицу (§9).
    """

    id: uuid.UUID
    modality: Модальность
    source_text: str | None
    # Структура, извлечённая моделью. До Э12 всегда `None`, и это состояние
    # клиент обязан видеть: именно оно означает «поля заполняет owner».
    extracted: dict[str, object] | None
    error: str | None
    created_at: dt.datetime


@dataclass(frozen=True)
class ЗаписанноеСобытие:
    """Подтверждённое событие: что записано в журнал и что с ним в Google.

    `sync_state` наружу отдаётся намеренно. Событие уже принято и никуда
    не денется, но в календаре его может ещё не быть, и экран обязан уметь
    сказать «записывается» вместо того, чтобы обещать готовое (инвариант 9).
    """

    key: str
    title: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    location: str | None
    description: str | None
    sync_state: str
    synced_at: dt.datetime | None


def тип_файла(данные: bytes) -> str | None:
    """`media_type` файла захвата по первым байтам, `None` - не наш формат.

    WebP опознаётся отдельно: у него сигнатура из двух кусков, `RIFF`
    в начале и `WEBP` на восьмом байте, между ними длина файла.
    """
    for начало, тип in СИГНАТУРЫ:
        if данные.startswith(начало):
            return тип
    if данные[:4] == b"RIFF" and данные[8:12] == b"WEBP":
        return "image/webp"
    return None


def страниц_pdf(данные: bytes) -> int:
    """Число страниц PDF - то, от чего зависит цена разбора (ADR-054).

    Защищённый файл отвергается и тогда, когда открывается без пароля
    (запрет печати и копирования): провайдер принимает PDF без шифрования,
    и отказ за деньги на его стороне хуже отказа здесь.
    """
    try:
        with pdfplumber.open(io.BytesIO(данные)) as документ:
            защищён = документ.doc.encryption is not None
            страниц = len(документ.pages)
    except Exception as сбой:  # noqa: BLE001 - чужой файл, причина для owner одна
        raise ФайлНеЧитается("PDF не открывается: он повреждён или защищён паролем") from сбой
    if защищён:
        raise ФайлНеЧитается("PDF защищён - сохраните его без защиты или сделайте скриншот")
    if страниц == 0:
        raise ФайлНеЧитается("в PDF нет ни одной страницы")
    return страниц


def ключ_события(draft_id: uuid.UUID) -> str:
    """`capture:<uuid>` - ключ события, созданного из этого черновика."""
    return f"{ПРЕФИКС_КЛЮЧА}{draft_id}"


def из_строки(строка: CaptureDraft) -> Черновик:
    return Черновик(
        id=строка.id,
        # Значение пришло из базы, где его стережёт CHECK `modality_known`:
        # тот же список, что в `Модальность`, - поэтому приведение честное.
        modality=cast(Модальность, строка.modality),
        source_text=строка.source_text,
        extracted=dict(строка.extracted) if строка.extracted is not None else None,
        error=строка.error,
        created_at=строка.created_at,
    )


def событие_из_строки(строка: CalendarEvent) -> ЗаписанноеСобытие:
    return ЗаписанноеСобытие(
        key=строка.external_key,
        title=строка.title,
        starts_at=строка.starts_at,
        ends_at=строка.ends_at,
        location=строка.location,
        description=строка.description,
        sync_state=строка.sync_state,
        synced_at=строка.synced_at,
    )


def создать(
    session: Session,
    *,
    modality: Модальность,
    source_text: str | None,
    картинка: tuple[str, bytes] | None = None,
) -> CaptureDraft:
    """Завести черновик, а с фотографией - и её байты. Коммит - на вызывающем.

    Байты ложатся в `capture_blobs`, а не в строку черновика: список
    черновиков не должен читать фотографии (§9). Уносит их каскад - при
    подтверждении и при отмене, то есть дольше черновика снимок не живёт.

    Идентификатор задаётся здесь, а не оставляется `gen_random_uuid()`
    базе: из него собирается `external_key` будущего события, и ключ,
    рождённый базой, был бы ровно тем «id строки БД», который инвариант 5
    запрещает. Практическая разница видна после восстановления из дампа:
    свой uuid уезжает в дамп вместе со строкой, и реконсил находит в Google
    то же самое событие, а не создаёт второе.
    """
    строка = CaptureDraft(
        id=uuid.uuid4(),
        modality=modality,
        source_text=source_text,
        extracted=None,
        error=None,
        confirmed=False,
    )
    session.add(строка)
    session.flush()
    if картинка is not None:
        тип, данные = картинка
        session.add(
            CaptureBlob(draft_id=строка.id, mime_type=тип, size_bytes=len(данные), data=данные)
        )
        session.flush()
    return строка


def записать_разбор(
    строка: CaptureDraft, *, extracted: dict[str, object] | None, error: str | None
) -> None:
    """Итог разбора в черновик. Коммит - на вызывающем."""
    строка.extracted = extracted
    строка.error = error


def найти(session: Session, draft_id: uuid.UUID) -> CaptureDraft | None:
    """Черновик по идентификатору. `None` - решение о 404 принимает `api/`."""
    return session.get(CaptureDraft, draft_id)


def черновики(session: Session) -> list[CaptureDraft]:
    """Все живые черновики, новые сверху.

    Список нужен не для красоты: захват прерывается обрывом связи и уходом
    со страницы, и черновик, о котором нельзя узнать, превращается
    в невидимую строку с фотографией внутри, живущую до срока уборки.
    """
    запрос = select(CaptureDraft).order_by(CaptureDraft.created_at.desc())
    return list(session.scalars(запрос))


def событие_захвата(session: Session, draft_id: uuid.UUID) -> CalendarEvent | None:
    """Событие, созданное из этого черновика, если оно уже есть.

    Отвечает на вопрос, который возникает при повторе подтверждения: связь
    оборвалась после записи, клиент не получил ответ и жмёт ещё раз.
    Черновика уже нет, но событие есть - и повтор обязан вернуть именно
    его, а не завести второе такое же (ADR-042).
    """
    return session.get(CalendarEvent, ключ_события(draft_id))


def подтвердить(
    session: Session,
    черновик: CaptureDraft,
    *,
    title: str,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
    location: str | None,
    description: str | None,
) -> CalendarEvent:
    """Черновик -> строка календаря. Коммит - на вызывающем.

    Черновик удаляется здесь же, в той же транзакции, вместе с сырьём (§9:
    каскад по внешнему ключу). Иначе после обрыва между двумя коммитами
    остался бы черновик, который уже стал событием, - и owner подтвердил бы
    его второй раз.

    `content_hash` остаётся пустым намеренно. Это отпечаток **того, что
    уехало в Google**, и считает его доставка (`jobs/push_capture.py`) от
    тела запроса. Пустая строка здесь означает буквально «в календарь ещё
    ничего не отправляли» и отличается от любого настоящего отпечатка.
    """
    строка = CalendarEvent(
        external_key=ключ_события(черновик.id),
        calendar=КАЛЕНДАРЬ,
        source=ИСТОЧНИК,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        location=location,
        description=description,
        content_hash="",
        sync_state="pending",
    )
    session.add(строка)
    _записать_в_аудит(
        session,
        действие="confirmed",
        ключ=строка.external_key,
        detail={
            "modality": черновик.modality,
            "title": title,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        },
    )
    session.delete(черновик)
    session.flush()
    return строка


def отменить(session: Session, черновик: CaptureDraft) -> None:
    """Убрать черновик вместе с сырьём. Коммит - на вызывающем.

    Аудит пишется до удаления: после него ни модальности, ни текста уже нет,
    а именно они отвечают на вопрос «что тут было».
    """
    _записать_в_аудит(
        session,
        действие="discarded",
        ключ=str(черновик.id),
        detail={"modality": черновик.modality},
    )
    session.delete(черновик)


def просроченные(session: Session, *, now: dt.datetime, ttl_hours: int) -> list[CaptureDraft]:
    """Черновики, к которым не вернулись (§9).

    Порог считается от `created_at`, а не от `updated_at`, и с разбором
    (Э12в) это по-прежнему верно: разбор пишется в том же запросе, что
    заводит черновик, и сдвигает `updated_at` на секунды, а не на часы.
    Правки черновика после создания нет - появится она, и вопрос «от чего
    считать заброшенность» придётся решать заново.
    """
    порог = now - dt.timedelta(hours=ttl_hours)
    запрос = (
        select(CaptureDraft)
        .where(CaptureDraft.created_at < порог)
        .order_by(CaptureDraft.created_at)
    )
    return list(session.scalars(запрос))


def _записать_в_аудит(
    session: Session, *, действие: str, ключ: str, detail: dict[str, object]
) -> None:
    """След захвата (инвариант 8).

    Отдельный `kind`, а не `calendar_write`: там живут записи **в Google**,
    и смешивать с ними «owner подтвердил черновик» значило бы потерять
    разницу между решением человека и действием джоба. Запись в календарь
    по этому событию появится в журнале своей строкой, от доставки.
    """
    session.add(
        AuditLogEntry(
            kind="capture",
            actor=АКТОР,
            status="ok",
            target=ключ,
            detail={"action": действие, **detail},
        )
    )

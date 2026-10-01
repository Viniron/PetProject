"""Эндпоинты захвата события: черновик, отмена, подтверждение (Э8, Э12в, §8.4).

Пять ручек на один путь `вход → модель → черновик → подтверждение → запись`.
Модель зовётся **внутри** запроса, заводящего черновик (ADR-054): owner ждёт
разобранную форму, а не «приходите позже». Ожидание ограничено коротким
бюджетом вызова (`domain/capture_parse.py`), и отказ модели черновика
не отменяет - форма приходит пустой, с причиной словами.

**`PATCH` события нет.** Правка и удаление уже записанного - отдельный
этап (решение owner 2026-09-17). Здесь событие только рождается.

Как и соседние роутеры, функции объявлены `def`, а не `async def`
(ADR-021): движок синхронный, и запрос в базу внутри `async def`
заблокировал бы цикл событий вместе с `/health`.
"""

import datetime as dt
import uuid
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.orm import Session

from jarvis_api.api.deps import Настройки, Сейчас, Сессия
from jarvis_api.api.errors import ОТКАЗЫ, ErrorBody, ОтказAPI
from jarvis_api.api.schemas import (
    CaptureConfirmIn,
    CapturedEventOut,
    CaptureDraftIn,
    CaptureDraftOut,
    CaptureDraftsOut,
    CaptureParsedOut,
)
from jarvis_api.config import Settings
from jarvis_api.db.models import CaptureDraft
from jarvis_api.domain import capture, capture_parse
from jarvis_api.domain.capture import Модальность
from jarvis_api.integrations import llm
from jarvis_api.jobs.common import owner_timezone
from jarvis_api.jobs.push_capture import отправить_сразу

маршрутизатор = APIRouter(prefix="/api/capture", tags=["capture"])

# Отказ сверх общих. Необъявленный код ответа в контракт не попадёт,
# и клиент не узнает, что 404 бывает.
НЕ_НАЙДЕНО: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorBody, "description": "Черновика с таким id нет"}
}


def адаптеры_захвата(настройки: Настройки) -> dict[str, llm.Адаптер]:
    """Подключённые провайдеры на коротком бюджете (ADR-054).

    Зависимостью, а не вызовом внутри ручки: тесты подменяют её подставным
    провайдером, и сеть в прогоне не трогается ни разу (`CLAUDE.md`).
    Короткие настройки нужны уже здесь: число попыток живёт в транспорте
    адаптера, а не в запросе.
    """
    return llm.собрать_адаптеры(capture_parse.короткие_настройки(настройки))


Адаптеры = Annotated[dict[str, llm.Адаптер], Depends(адаптеры_захвата)]


def _наружу(строка: CaptureDraft, зона: ZoneInfo) -> CaptureDraftOut:
    """Черновик для экрана: моменты разбора - в зоне owner, как у сетки (§8.4)."""
    черновик = capture.из_строки(строка)
    разбор = None
    if черновик.extracted is not None:
        разбор = CaptureParsedOut.model_validate(черновик.extracted)
        разбор = разбор.model_copy(
            update={
                "starts_at": _в_зону(разбор.starts_at, зона),
                "ends_at": _в_зону(разбор.ends_at, зона),
            }
        )
    return CaptureDraftOut(
        id=черновик.id,
        modality=черновик.modality,
        source_text=черновик.source_text,
        extracted=разбор,
        error=черновик.error,
        timezone=зона.key,
        created_at=черновик.created_at,
    )


def _в_зону(момент: dt.datetime | None, зона: ZoneInfo) -> dt.datetime | None:
    return момент.astimezone(зона) if момент is not None else None


def _завести_и_разобрать(
    сессия: Session,
    *,
    настройки: Settings,
    момент: dt.datetime,
    адаптеры: dict[str, llm.Адаптер],
    modality: Модальность,
    текст: str | None,
    картинка: tuple[str, bytes] | None,
) -> CaptureDraftOut:
    """Общий путь текста и фото: черновик, разбор, коммит.

    Черновик заводится **до** вызова модели. Отказ модели слой коммитит сам,
    вместе со своим аудитом (`client.py`), и черновик уезжает в базу тем же
    коммитом - без разбора, но не потерянный. Итог разбора дописывается
    следующим.
    """
    зона = owner_timezone(сессия)
    строка = capture.создать(сессия, modality=modality, source_text=текст, картинка=картинка)
    изображение = (
        llm.Изображение(media_type=картинка[0], данные=картинка[1])
        if картинка is not None
        else None
    )
    extracted, error = capture_parse.разобрать(
        сессия,
        modality=modality,
        текст=текст,
        изображение=изображение,
        настройки=настройки,
        адаптеры=адаптеры,
        сейчас=момент,
        зона=зона,
    )
    capture.записать_разбор(строка, extracted=extracted, error=error)
    сессия.commit()
    return _наружу(строка, зона)


@маршрутизатор.post(
    "/drafts",
    status_code=status.HTTP_201_CREATED,
    responses=ОТКАЗЫ,
    summary="Завести черновик из текста или расшифровки голоса",
)
def завести_черновик(
    сессия: Сессия,
    настройки: Настройки,
    момент: Сейчас,
    адаптеры: Адаптеры,
    тело: CaptureDraftIn,
) -> CaptureDraftOut:
    """Принять напечатанное или надиктованное, разобрать моделью, вернуть черновик.

    201 и черновик приходят и тогда, когда модель отказала: причина лежит
    в `error`, поля заполняет owner. Отказ всего запроса значил бы, что
    недоступный провайдер теряет то, что owner успел написать.
    """
    if len(тело.text) > настройки.capture_text_max_chars:
        raise ОтказAPI(
            статус=422,
            code="validation_error",
            message=(
                f"текст захвата длиннее {настройки.capture_text_max_chars} символов "
                f"({len(тело.text)}): это вставленный буфер обмена, а не заметка о событии"
            ),
        )

    return _завести_и_разобрать(
        сессия,
        настройки=настройки,
        момент=момент,
        адаптеры=адаптеры,
        modality=тело.modality,
        текст=тело.text,
        картинка=None,
    )


@маршрутизатор.post(
    "/drafts/file",
    status_code=status.HTTP_201_CREATED,
    responses=ОТКАЗЫ,
    summary="Завести черновик из фото, скриншота или PDF",
    # Явный `operation_id` по той же причине, что у импорта выписок: из имени
    # русской функции FastAPI собрал бы имя схемы тела формы цепочкой
    # подчёркиваний, и оно уехало бы в клиент именем типа TypeScript.
    operation_id="capture_file",
)
def завести_из_файла(
    сессия: Сессия,
    настройки: Настройки,
    момент: Сейчас,
    адаптеры: Адаптеры,
    file: Annotated[UploadFile, File(description="Фото или скриншот (JPEG, PNG, WebP) или PDF")],
) -> CaptureDraftOut:
    """Принять файл, разобрать моделью, вернуть черновик.

    Формат, размер и число страниц PDF проверяются **до** черновика и до
    модели: отвергнутый файл не должен ни лечь в базу, ни стоить денег.
    Формат определяется по байтам, а не по заголовку клиента -
    см. `capture.тип_файла`.
    """
    предел = настройки.capture_image_max_bytes
    # На байт больше предела: этого хватает, чтобы понять «слишком большой»,
    # и не нужно читать в память файл целиком, каким бы он ни был.
    данные = file.file.read(предел + 1)
    if len(данные) > предел:
        raise ОтказAPI(
            статус=422,
            code="capture_file_too_large",
            message=f"файл больше {предел} байт - уменьшите его перед отправкой",
        )
    тип = capture.тип_файла(данные)
    if тип is None:
        raise ОтказAPI(
            статус=422,
            code="capture_file_unsupported",
            message="файл не картинка и не PDF: принимаются JPEG, PNG, WebP и PDF",
        )
    if тип == capture.PDF:
        try:
            страниц = capture.страниц_pdf(данные)
        except capture.ФайлНеЧитается as сбой:
            raise ОтказAPI(статус=422, code="capture_file_unreadable", message=str(сбой)) from сбой
        if страниц > настройки.capture_pdf_max_pages:
            raise ОтказAPI(
                статус=422,
                code="capture_pdf_too_long",
                message=(
                    f"в файле {страниц} страниц, а разбираются PDF до "
                    f"{настройки.capture_pdf_max_pages} - сохраните нужные страницы "
                    "отдельным файлом"
                ),
            )

    return _завести_и_разобрать(
        сессия,
        настройки=настройки,
        момент=момент,
        адаптеры=адаптеры,
        modality="image",
        текст=None,
        картинка=(тип, данные),
    )


@маршрутизатор.get("/drafts", responses=ОТКАЗЫ, summary="Черновики, ждущие подтверждения")
def список_черновиков(сессия: Сессия) -> CaptureDraftsOut:
    """Все живые черновики, новые сверху.

    Без фильтров и без страниц намеренно: черновик живёт часы, их единицы,
    а брошенные убирает джоб по сроку. Список, требующий фильтра, означал
    бы, что черновики копятся, - и чинить это надо было бы уборкой,
    а не параметром.
    """
    зона = owner_timezone(сессия)
    строки = capture.черновики(сессия)
    return CaptureDraftsOut(drafts=[_наружу(строка, зона) for строка in строки])


@маршрутизатор.delete(
    "/drafts/{draft_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ОТКАЗЫ | НЕ_НАЙДЕНО,
    summary="Отменить черновик",
)
def отменить_черновик(сессия: Сессия, draft_id: uuid.UUID) -> None:
    """Убрать черновик вместе с сырьём.

    Это вторая половина обещания «без подтверждения не пишем наружу»:
    отказ owner обязан уносить и то, что он прислал, - фотография, которую
    решили не сохранять, не должна остаться в ночном дампе.
    """
    строка = capture.найти(сессия, draft_id)
    if строка is None:
        raise ОтказAPI(статус=404, code="not_found", message=f"Черновика {draft_id} нет")
    capture.отменить(сессия, строка)
    сессия.commit()


@маршрутизатор.post(
    "/drafts/{draft_id}/confirm",
    responses=ОТКАЗЫ | НЕ_НАЙДЕНО,
    summary="Подтвердить черновик и записать событие",
)
def подтвердить_черновик(
    сессия: Сессия,
    настройки: Настройки,
    момент: Сейчас,
    draft_id: uuid.UUID,
    тело: CaptureConfirmIn,
) -> CapturedEventOut:
    """Черновик становится событием.

    Ответ 200, а не 201, и повтор безопасен. Черновика после подтверждения
    нет, поэтому второй такой же запрос - это обрыв связи, а не вторая
    попытка что-то создать: ключ события детерминирован от id черновика
    (ADR-042), и повтор отдаёт то же самое событие вместо второй копии
    в календаре owner.

    Запись в Google пробуется сразу же, коротким таймаутом и без повторов,
    и её отказ ответа не меняет: событие уже принято, а очередь заберёт
    его ближайшим прогоном джоба (`make sync-capture-apply` или
    планировщик). Экран при этом видит `sync_state = pending` и говорит
    «записывается» - обещать записанное, пока Google не ответил, нельзя.
    """
    черновик = capture.найти(сессия, draft_id)
    if черновик is None:
        уже_записанное = capture.событие_захвата(сессия, draft_id)
        if уже_записанное is not None:
            return CapturedEventOut.model_validate(capture.событие_из_строки(уже_записанное))
        raise ОтказAPI(
            статус=404,
            code="not_found",
            message=f"Черновика {draft_id} нет: он отменён или убран по сроку",
        )

    строка = capture.подтвердить(
        сессия,
        черновик,
        title=тело.title,
        starts_at=тело.starts_at,
        ends_at=тело.ends_at,
        location=тело.location,
        description=тело.description,
    )
    # Коммит до похода в Google: событие принято независимо от того, ответит
    # ли календарь. Обратный порядок означал бы, что недоступность Google
    # теряет решение owner.
    сессия.commit()

    if отправить_сразу(сессия, настройки, строка, now=момент):
        сессия.commit()

    return CapturedEventOut.model_validate(capture.событие_из_строки(строка))

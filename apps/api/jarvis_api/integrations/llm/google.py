"""Адаптер Google: `generateContent` и отложенная пачка (§5.2, ADR-046, ADR-049).

**Почему `:generateContent`, а не новый `interactions`.** У Google две
поверхности: `interactions` с полем `response_format` и `steps` в ответе -
и прежняя `:generateContent`. Батч говорит только второй: пачка собирается
из тех же `contents` и `generationConfig`, что и обычный вызов. Категоризация
операций (ADR-046) назначена в батч, то есть обе формы нужны в одном адаптере,
и держать их в двух непохожих видах значит проверять разбор ответа дважды.
Переезд на `interactions` - правка этого модуля, ADR-049 называет её ценой
решения.

**Схема уезжает урезанной, и это не недосмотр.** Подмножество Google не знает
ни `$ref`, ни `additionalProperties`; перевод живёт в `schema.py`. ADR-046
говорит об этом прямо: здесь схема направляет модель, а не гарантирует ответ,
- гарантию даёт проверка Pydantic в ядре и повтор при промахе.

**Стабильная часть промпта уходит в `systemInstruction`.** Она рендерится
провайдером первой, то есть граница кэшируемого префикса совпадает с границей
из `prompts.py`. Кэш при этом выключен (ADR-046): у Google хранение платное
по времени, и между еженедельными выписками протухает любой TTL.

**Мысленные токены складываются в выход.** `total_thought_tokens` приходит
отдельным счётчиком, в `candidatesTokenCount` не входит, но тарифицируется
по ставке выхода. Адаптер, взявший только видимый ответ, занизил бы стоимость
- и `audit_log` начал бы врать тихо (ADR-047).
"""

import base64
import json
import logging
from collections.abc import Iterator, Mapping
from typing import Any

import httpx2

from jarvis_api.config import Settings
from jarvis_api.integrations.llm.base import (
    ДоступБатча,
    Запрос,
    Ответ,
    ОтказПровайдера,
    СостояниеБатча,
)
from jarvis_api.integrations.llm.http import Транспорт
from jarvis_api.integrations.llm.schema import для_google

logger = logging.getLogger("jarvis.llm.google")

ИМЯ = "google"
БАЗА = "https://generativelanguage.googleapis.com/v1beta"

# Слова, которыми Google сообщает, что запрос пришёл из неподдерживаемой
# страны. Проверяется вместе с кодом ответа: та же формулировка в теле
# успешного ответа означала бы просто текст про геолокацию.
МЕТКИ_ГЕОБЛОКА = ("user location is not supported", "location is not supported for the api")

# Состояния долгой операции, после которых опрашивать больше нечего.
СОСТОЯНИЯ_КОНЦА = frozenset(
    {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
)
СОСТОЯНИЕ_УДАЧИ = "JOB_STATE_SUCCEEDED"


def опознать_геоблок(статус: int, тело: str) -> bool:
    """Отказ по стране выхода, а не по ключу и не по запросу (ADR-050)."""
    нижний = тело.lower()
    return any(метка in нижний for метка in МЕТКИ_ГЕОБЛОКА)


class АдаптерGoogle:
    """Google глазами ядра. Имён моделей здесь нет: они приходят маршрутом."""

    имя = ИМЯ

    def __init__(self, транспорт: Транспорт, *, база: str = БАЗА) -> None:
        self._транспорт = транспорт
        self._база = база.rstrip("/")

    # --- обычный вызов ------------------------------------------------------

    def выполнить(self, запрос: Запрос) -> Ответ:
        тело = self._тело(запрос)
        адрес = f"{self._база}/models/{запрос.модель}:generateContent"
        ответ = self._транспорт.отправить(адрес, тело, таймаут=запрос.таймаут_секунд)
        return _разобрать_ответ(ответ)

    # --- батч ---------------------------------------------------------------

    def проверить_батч(self) -> ДоступБатча:
        """Спрашиваем список пачек: ключ либо вправе их видеть, либо нет.

        Список, а не пробная пачка: ответ тот же, а стоит он ноль и приходит
        сразу, тогда как настоящая пачка ждала бы до суток.
        """
        try:
            тело = self._транспорт.получить(f"{self._база}/batches", таймаут=30)
        except ОтказПровайдера as ошибка:
            return ДоступБатча(доступен=False, подробность=str(ошибка))
        очередь = тело.get("operations") or тело.get("batches") or []
        return ДоступБатча(доступен=True, подробность=f"пачек в очереди: {len(очередь)}")

    def отправить_батч(self, запросы: Mapping[str, Запрос]) -> str:
        модели = {запрос.модель for запрос in запросы.values()}
        if len(модели) != 1:
            # Пачка у Google адресуется модели целиком, а не построчно.
            # Смешанная пачка молча уехала бы вся на первую попавшуюся модель.
            raise ОтказПровайдера(
                f"в одной пачке Google запросы к разным моделям: {sorted(модели)}"
            )

        модель = модели.pop()
        тело = {
            "batch": {
                "display_name": "jarvis",
                "input_config": {
                    "requests": {
                        "requests": [
                            {"request": self._тело(запрос), "metadata": {"key": ключ}}
                            for ключ, запрос in запросы.items()
                        ]
                    }
                },
            }
        }
        таймаут = max(запрос.таймаут_секунд for запрос in запросы.values())
        ответ = self._транспорт.отправить(
            f"{self._база}/models/{модель}:batchGenerateContent", тело, таймаут=таймаут
        )
        имя = ответ.get("name")
        if not isinstance(имя, str) or not имя:
            raise ОтказПровайдера(f"Google не вернул имя пачки: {json.dumps(ответ)[:400]}")
        return имя

    def состояние_батча(self, идентификатор: str) -> СостояниеБатча:
        тело = self._транспорт.получить(f"{self._база}/{идентификатор}", таймаут=60)
        состояние = _состояние(тело)
        return СостояниеБатча(готов=состояние in СОСТОЯНИЯ_КОНЦА, сырое=состояние)

    def результаты_батча(self, идентификатор: str) -> Iterator[tuple[str, Ответ | Exception]]:
        тело = self._транспорт.получить(f"{self._база}/{идентификатор}", таймаут=60)
        состояние = _состояние(тело)
        if состояние != СОСТОЯНИЕ_УДАЧИ:
            raise ОтказПровайдера(f"пачка {идентификатор} в состоянии {состояние}, результатов нет")

        исход = тело.get("response") or тело.get("dest") or {}
        встроенные = исход.get("inlinedResponses")
        if isinstance(встроенные, list):
            yield from _разобрать_встроенные(встроенные)
            return

        файл = исход.get("responsesFile")
        if not isinstance(файл, str) or not файл:
            raise ОтказПровайдера(
                f"пачка {идентификатор} удалась, но результатов в ответе нет: "
                f"{json.dumps(исход)[:400]}"
            )
        сырое = self._транспорт.скачать(
            f"{self._база.replace('/v1beta', '/download/v1beta')}/{файл}:download?alt=media",
            таймаут=120,
        )
        yield from _разобрать_jsonl(сырое)

    # --- сборка запроса -----------------------------------------------------

    def _тело(self, запрос: Запрос) -> dict[str, Any]:
        части: list[dict[str, Any]] = [{"text": запрос.переменная_часть}]
        части.extend(
            {"inlineData": {"mimeType": кадр.media_type, "data": _в_base64(кадр.данные)}}
            for кадр in запрос.изображения
        )

        конфиг: dict[str, Any] = {
            "maxOutputTokens": запрос.max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": для_google(запрос.схема),
        }
        if запрос.temperature is not None:
            # Строкой Decimal не уйдёт - JSON её не знает; float здесь
            # безопасен, потому что это не деньги, а параметр сэмплинга.
            конфиг["temperature"] = float(запрос.temperature)

        return {
            "systemInstruction": {"parts": [{"text": запрос.стабильная_часть}]},
            "contents": [{"role": "user", "parts": части}],
            "generationConfig": конфиг,
        }


def _в_base64(данные: bytes) -> str:
    return base64.b64encode(данные).decode("ascii")


def _состояние(тело: Mapping[str, Any]) -> str:
    """Состояние долгой операции: у Google оно лежит то в метаданных, то рядом."""
    метаданные = тело.get("metadata")
    if isinstance(метаданные, dict) and isinstance(метаданные.get("state"), str):
        return str(метаданные["state"])
    if isinstance(тело.get("state"), str):
        return str(тело["state"])
    return "JOB_STATE_UNSPECIFIED"


def _разобрать_ответ(тело: Mapping[str, Any]) -> Ответ:
    """Текст и счётчики из ответа `generateContent`."""
    кандидаты = тело.get("candidates")
    if not isinstance(кандидаты, list) or not кандидаты:
        # Пустой список кандидатов - это сработавший фильтр безопасности или
        # упёршийся потолок ответа. Отказ по существу: тот же запрос вернёт
        # то же самое, и резервная модель здесь не поможет.
        raise ОтказПровайдера(
            f"Google не вернул ни одного кандидата: {json.dumps(dict(тело))[:400]}"
        )

    первый = кандидаты[0]
    содержимое = первый.get("content") if isinstance(первый, dict) else None
    части = содержимое.get("parts") if isinstance(содержимое, dict) else None
    текст = "".join(
        часть["text"]
        for часть in (части or [])
        if isinstance(часть, dict) and isinstance(часть.get("text"), str)
    )

    if not текст:
        причина = первый.get("finishReason") if isinstance(первый, dict) else None
        raise ОтказПровайдера(f"Google вернул пустой ответ (finishReason={причина!r})")

    учёт = тело.get("usageMetadata")
    учёт = учёт if isinstance(учёт, dict) else {}
    мысли = _целое(учёт, "thoughtsTokenCount", "total_thought_tokens")
    return Ответ(
        текст=текст,
        токенов_вход=_целое(учёт, "promptTokenCount", "total_input_tokens"),
        # Мысли складываются в выход: их считают по ставке выхода, а в
        # `candidatesTokenCount` они не входят (см. докстринг модуля).
        токенов_выход=_целое(учёт, "candidatesTokenCount", "total_output_tokens") + мысли,
        токенов_мыслей=мысли,
        токенов_кэша=_целое(учёт, "cachedContentTokenCount", "total_cached_tokens"),
    )


def _целое(учёт: Mapping[str, Any], *имена: str) -> int:
    """Счётчик под любым из имён: у Google они разошлись между поверхностями."""
    for имя in имена:
        значение = учёт.get(имя)
        if isinstance(значение, int):
            return значение
    return 0


def _разобрать_встроенные(встроенные: list[Any]) -> Iterator[tuple[str, Ответ | Exception]]:
    for строка in встроенные:
        if not isinstance(строка, dict):
            continue
        ключ = _ключ(строка)
        ошибка = строка.get("error")
        if ошибка:
            yield ключ, ОтказПровайдера(f"Google отказал по заданию {ключ}: {ошибка}")
            continue
        try:
            yield ключ, _разобрать_ответ(строка.get("response") or {})
        except ОтказПровайдера as сбой:
            yield ключ, сбой


def _разобрать_jsonl(сырое: str) -> Iterator[tuple[str, Ответ | Exception]]:
    for номер, строка in enumerate(сырое.splitlines(), start=1):
        голая = строка.strip()
        if not голая:
            continue
        try:
            разобранная = json.loads(голая)
        except json.JSONDecodeError as сбой:
            # Одна испорченная строка не роняет пачку: остальные девяносто
            # девять заданий оплачены и разобраны.
            yield f"<строка {номер}>", ОтказПровайдера(f"строка {номер} файла результатов не JSON")
            logger.warning("строка %d файла результатов Google не разобралась: %s", номер, сбой)
            continue
        if not isinstance(разобранная, dict):
            continue
        yield from _разобрать_встроенные([разобранная])


def _ключ(строка: Mapping[str, Any]) -> str:
    метаданные = строка.get("metadata")
    if isinstance(метаданные, dict) and isinstance(метаданные.get("key"), str):
        return str(метаданные["key"])
    return "<без ключа>"


def собрать(настройки: Settings, *, клиент: httpx2.Client | None = None) -> АдаптерGoogle | None:
    """Адаптер, если ключ задан. Пустой ключ - провайдер просто не подключён."""
    ключ = настройки.gemini_api_key.strip()
    if not ключ:
        return None
    return АдаптерGoogle(
        Транспорт(
            имя=ИМЯ,
            заголовки={"x-goog-api-key": ключ, "Content-Type": "application/json"},
            попыток=настройки.llm_retries,
            пауза_секунд=настройки.llm_retry_backoff_seconds,
            геоблок=опознать_геоблок,
            клиент=клиент,
        )
    )

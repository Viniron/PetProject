"""Адаптеры Google и Anthropic (Э12б, ADR-049).

Сеть замокана целиком (CLAUDE.md): `httpx2.MockTransport` отвечает вместо
провайдера, и запросы, которые адаптер собрал, проверяются как данные.
Живая проверка ключей и маршрута наружу - отдельная команда `make llm-probe`,
и тестом она не заменяется: тест не знает, рабочий ли ключ, а провайдер
не знает, правильно ли собран запрос.

Что проверяется - ровно то, что ломается тихо:

- счётчики токенов, из которых считается стоимость (инвариант 8);
- порядок частей промпта: стабильная первой, иначе кэш молча мимо;
- `temperature` уходит, только если она задана маршрутом;
- отказ разбирается в тот класс, который решает, платить ли ещё раз.
"""

import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from jarvis_api.integrations.llm.anthropic import АдаптерAnthropic
from jarvis_api.integrations.llm.base import (
    ВыходЗаблокирован,
    Запрос,
    Изображение,
    Ответ,
    ОтказПровайдера,
    ПровайдерНедоступен,
)
from jarvis_api.integrations.llm.google import АдаптерGoogle
from jarvis_api.integrations.llm.http import Транспорт

СХЕМА = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}


def запрос(**поля: Any) -> Запрос:
    основа: dict[str, Any] = {
        "модель": "model-from-config",
        "стабильная_часть": "СТАБИЛЬНОЕ",
        "переменная_часть": "ПЕРЕМЕННОЕ",
        "схема": СХЕМА,
        "max_tokens": 256,
        "таймаут_секунд": 5,
    }
    основа.update(поля)
    return Запрос(**основа)


class Перехват:
    """Подставной провайдер: запоминает запросы, отвечает по сценарию."""

    def __init__(self, ответы: list[httpx2.Response]) -> None:
        self.ответы = ответы
        self.запросы: list[httpx2.Request] = []

    def __call__(self, запрос_http: httpx2.Request) -> httpx2.Response:
        self.запросы.append(запрос_http)
        return self.ответы.pop(0) if len(self.ответы) > 1 else self.ответы[0]

    @property
    def тело(self) -> dict[str, Any]:
        разобранное: dict[str, Any] = json.loads(self.запросы[-1].content)
        return разобранное


def собрать_google(перехват: Перехват) -> АдаптерGoogle:
    клиент = httpx2.Client(transport=httpx2.MockTransport(перехват))
    from jarvis_api.integrations.llm.google import опознать_геоблок

    return АдаптерGoogle(
        Транспорт(
            имя="google",
            заголовки={"x-goog-api-key": "test-key"},
            попыток=1,
            пауза_секунд=0,
            геоблок=опознать_геоблок,
            клиент=клиент,
        )
    )


def собрать_anthropic(перехват: Перехват) -> АдаптерAnthropic:
    клиент = httpx2.Client(transport=httpx2.MockTransport(перехват))
    from jarvis_api.integrations.llm.anthropic import опознать_геоблок

    return АдаптерAnthropic(
        Транспорт(
            имя="anthropic",
            заголовки={"x-api-key": "test-key"},
            попыток=1,
            пауза_секунд=0,
            геоблок=опознать_геоблок,
            клиент=клиент,
        )
    )


def ответ_google(*, вход: int = 10, выход: int = 4, мысли: int = 0) -> httpx2.Response:
    учёт: dict[str, Any] = {"promptTokenCount": вход, "candidatesTokenCount": выход}
    if мысли:
        учёт["thoughtsTokenCount"] = мысли
    return httpx2.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": '{"title": "Зубной"}'}]}}],
            "usageMetadata": учёт,
        },
    )


def ответ_anthropic(*, вход: int = 10, выход: int = 4) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "content": [{"type": "tool_use", "name": "answer", "input": {"title": "Зубной"}}],
            "usage": {"input_tokens": вход, "output_tokens": выход},
        },
    )


# --- Google -----------------------------------------------------------------


def test_google_собирает_запрос_со_схемой_и_потолком() -> None:
    перехват = Перехват([ответ_google()])
    собрать_google(перехват).выполнить(запрос())

    тело = перехват.тело
    assert тело["generationConfig"]["maxOutputTokens"] == 256
    assert тело["generationConfig"]["responseMimeType"] == "application/json"
    assert тело["generationConfig"]["responseSchema"]["type"] == "OBJECT"
    assert "model-from-config:generateContent" in str(перехват.запросы[-1].url)


def test_google_стабильная_часть_отдельно_от_переменной() -> None:
    """Граница кэшируемого префикса совпадает с границей из `prompts.py`.

    Слитые в одну строку части не ломают ничего видимого - ответы те же,
    ошибок нет, - но попадание в кэш исчезает, и видно это только по счёту.
    """
    перехват = Перехват([ответ_google()])
    собрать_google(перехват).выполнить(запрос())

    тело = перехват.тело
    assert тело["systemInstruction"]["parts"][0]["text"] == "СТАБИЛЬНОЕ"
    assert тело["contents"][0]["parts"][0]["text"] == "ПЕРЕМЕННОЕ"


def test_google_мысленные_токены_попадают_в_выход() -> None:
    """Они тарифицируются по ставке выхода, а в `candidatesTokenCount` их нет.

    Взяв только видимый ответ, адаптер занизил бы стоимость - и `audit_log`
    начал бы врать тихо (ADR-047).
    """
    перехват = Перехват([ответ_google(вход=10, выход=4, мысли=96)])
    ответ = собрать_google(перехват).выполнить(запрос())

    assert ответ.токенов_выход == 100
    assert ответ.токенов_мыслей == 96


def test_google_картинка_едет_тем_же_вызовом() -> None:
    перехват = Перехват([ответ_google()])
    кадр = Изображение(media_type="image/jpeg", данные=b"\xff\xd8\xff")
    собрать_google(перехват).выполнить(запрос(изображения=(кадр,)))

    части = перехват.тело["contents"][0]["parts"]
    assert части[1]["inlineData"]["mimeType"] == "image/jpeg"
    assert части[1]["inlineData"]["data"] == "/9j/"


def test_google_без_температуры_параметр_не_уходит() -> None:
    """Не всякое поколение её принимает: лишний параметр - это 400 на ровном месте."""
    перехват = Перехват([ответ_google()])
    собрать_google(перехват).выполнить(запрос())
    assert "temperature" not in перехват.тело["generationConfig"]


def test_google_температура_из_маршрута_уходит() -> None:
    перехват = Перехват([ответ_google()])
    собрать_google(перехват).выполнить(запрос(temperature=Decimal("0")))
    assert перехват.тело["generationConfig"]["temperature"] == 0


def test_google_пустой_ответ_это_отказ_а_не_фолбэк() -> None:
    """Фильтр или упёршийся потолок: резерв ответит тем же, только за деньги."""
    перехват = Перехват([httpx2.Response(200, json={"candidates": []})])
    with pytest.raises(ОтказПровайдера):
        собрать_google(перехват).выполнить(запрос())


# --- Anthropic --------------------------------------------------------------


def test_anthropic_отвечает_инструментом_со_строгой_схемой() -> None:
    """ADR-046 выбрал провайдера именно за эту гарантию, а не за цену."""
    перехват = Перехват([ответ_anthropic()])
    собрать_anthropic(перехват).выполнить(запрос())

    тело = перехват.тело
    инструмент = тело["tools"][0]
    assert инструмент["strict"] is True
    assert инструмент["input_schema"]["additionalProperties"] is False
    assert тело["tool_choice"] == {"type": "tool", "name": инструмент["name"]}


def test_anthropic_аргументы_инструмента_и_есть_ответ() -> None:
    перехват = Перехват([ответ_anthropic()])
    ответ = собрать_anthropic(перехват).выполнить(запрос())

    assert json.loads(ответ.текст) == {"title": "Зубной"}
    assert ответ.токенов_вход == 10
    assert ответ.токенов_выход == 4


def test_anthropic_стабильная_часть_в_system() -> None:
    перехват = Перехват([ответ_anthropic()])
    собрать_anthropic(перехват).выполнить(запрос())

    тело = перехват.тело
    assert тело["system"][0]["text"] == "СТАБИЛЬНОЕ"
    assert тело["messages"][0]["content"][-1]["text"] == "ПЕРЕМЕННОЕ"
    assert тело["max_tokens"] == 256


def test_anthropic_картинка_перед_текстом() -> None:
    перехват = Перехват([ответ_anthropic()])
    кадр = Изображение(media_type="image/png", данные=b"\x89PNG")
    собрать_anthropic(перехват).выполнить(запрос(изображения=(кадр,)))

    содержимое = перехват.тело["messages"][0]["content"]
    assert содержимое[0]["type"] == "image"
    assert содержимое[0]["source"]["media_type"] == "image/png"
    assert содержимое[-1]["type"] == "text"


def test_anthropic_без_инструмента_это_отказ() -> None:
    """Инструмент был принудительным: его отсутствие - потолок или фильтр."""
    перехват = Перехват([httpx2.Response(200, json={"content": [{"type": "text", "text": "нет"}]})])
    with pytest.raises(ОтказПровайдера, match="stop_reason"):
        собрать_anthropic(перехват).выполнить(запрос())


# --- разбор отказа, общий для обоих -----------------------------------------


@pytest.mark.parametrize("статус", [429, 500, 503])
def test_временный_отказ_ведёт_на_фолбэк(статус: int) -> None:
    """429 и 5xx лечатся другой моделью или ожиданием: токенов не списано."""
    перехват = Перехват([httpx2.Response(статус, text="перегрузка")])
    with pytest.raises(ПровайдерНедоступен):
        собрать_google(перехват).выполнить(запрос())


@pytest.mark.parametrize("статус", [400, 401, 404])
def test_отказ_по_существу_фолбэком_не_лечится(статус: int) -> None:
    """Резерв ответит тем же, только это будет второй платный вызов."""
    перехват = Перехват([httpx2.Response(статус, text="bad key")])
    with pytest.raises(ОтказПровайдера):
        собрать_anthropic(перехват).выполнить(запрос())


def test_геоблок_опознаётся_отдельно_от_прочих_отказов() -> None:
    """Эта причина чинится не кодом, а маршрутом платы (ADR-050).

    Без своего типа она опознавалась бы поиском подстроки в чужом тексте
    ошибки - в RUNBOOK, в пробе и в логе по отдельности.
    """
    перехват = Перехват(
        [
            httpx2.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "message": "User location is not supported for the API use.",
                        "status": "FAILED_PRECONDITION",
                    }
                },
            )
        ]
    )
    with pytest.raises(ВыходЗаблокирован, match="страну"):
        собрать_google(перехват).выполнить(запрос())


def test_геоблок_anthropic_это_безликий_403() -> None:
    """Тело настоящего отказа, снятое живой пробой с платы 21.09.2026.

    Первая версия маркеров искала слова про страну или регион - их в ответе
    нет вовсе, и геоблок выглядел отказом по ключу. Разница дорогая: одно
    чинится маршрутом платы, другое у провайдера, и перепутав их, owner
    полдня ищет поломку не там.
    """
    перехват = Перехват(
        [
            httpx2.Response(
                403, json={"error": {"type": "forbidden", "message": "Request not allowed"}}
            )
        ]
    )
    with pytest.raises(ВыходЗаблокирован, match="страну"):
        собрать_anthropic(перехват).выполнить(запрос())


def test_другой_403_остаётся_обычным_отказом() -> None:
    """Признак геоблока - пара «403 + та самая формулировка», а не один код.

    403 бывает и настоящим: организация отключена, ключ без доступа к модели.
    Записать их в маршрут платы значило бы отправить чинить исправное.
    """
    перехват = Перехват(
        [
            httpx2.Response(
                403,
                json={"error": {"type": "permission_error", "message": "Your organization..."}},
            )
        ]
    )
    with pytest.raises(ОтказПровайдера) as поймано:
        собрать_anthropic(перехват).выполнить(запрос())
    assert not isinstance(поймано.value, ВыходЗаблокирован)


def test_сетевой_отказ_это_недоступность() -> None:
    def рвётся(запрос_http: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("нет маршрута", request=запрос_http)

    клиент = httpx2.Client(transport=httpx2.MockTransport(рвётся))
    адаптер = АдаптерGoogle(
        Транспорт(имя="google", заголовки={}, попыток=1, пауза_секунд=0, клиент=клиент)
    )
    with pytest.raises(ПровайдерНедоступен):
        адаптер.выполнить(запрос())


def test_ключ_провайдера_не_попадает_в_текст_отказа() -> None:
    """Токены не логируются даже на DEBUG (CLAUDE.md)."""
    перехват = Перехват([httpx2.Response(401, text="unauthorized")])
    адаптер = АдаптерAnthropic(
        Транспорт(
            имя="anthropic",
            заголовки={"x-api-key": "sk-secret-must-not-leak"},
            попыток=1,
            пауза_секунд=0,
            клиент=httpx2.Client(transport=httpx2.MockTransport(перехват)),
        )
    )
    with pytest.raises(ОтказПровайдера) as поймано:
        адаптер.выполнить(запрос())
    assert "sk-secret-must-not-leak" not in str(поймано.value)


# --- пачки ------------------------------------------------------------------


def test_google_пачка_сопоставляется_по_ключу_а_не_по_порядку() -> None:
    """Порядок ответов не совпадает с порядком запросов ни у одного провайдера.

    Сопоставление по номеру строки однажды приписало бы категорию чужой
    операции - это потеря денег owner, а не ошибка в логе.
    """
    готовая = httpx2.Response(
        200,
        json={
            "metadata": {"state": "JOB_STATE_SUCCEEDED"},
            "response": {
                "inlinedResponses": [
                    {
                        "metadata": {"key": "вторая"},
                        "response": {
                            "candidates": [{"content": {"parts": [{"text": '{"title": "Б"}'}]}}],
                            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1},
                        },
                    },
                    {
                        "metadata": {"key": "первая"},
                        "response": {
                            "candidates": [{"content": {"parts": [{"text": '{"title": "А"}'}]}}],
                            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                        },
                    },
                ]
            },
        },
    )
    адаптер = собрать_google(Перехват([готовая]))
    исход = dict(адаптер.результаты_батча("batches/1"))

    первая, вторая = исход["первая"], исход["вторая"]
    assert isinstance(первая, Ответ) and isinstance(вторая, Ответ)
    assert json.loads(первая.текст)["title"] == "А"
    assert json.loads(вторая.текст)["title"] == "Б"


def test_anthropic_пачка_отдаёт_удачные_несмотря_на_отказ_соседа() -> None:
    """Одно мусорное задание не должно стоить повторной оплаты всей пачки."""
    строки = "\n".join(
        [
            json.dumps(
                {
                    "custom_id": "целая",
                    "result": {
                        "type": "succeeded",
                        "message": {
                            "content": [
                                {"type": "tool_use", "name": "answer", "input": {"title": "А"}}
                            ],
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    },
                }
            ),
            json.dumps({"custom_id": "битая", "result": {"type": "errored"}}),
        ]
    )
    описание = httpx2.Response(
        200, json={"processing_status": "ended", "results_url": "https://api.anthropic.com/r"}
    )
    файл = httpx2.Response(200, text=строки)
    адаптер = собрать_anthropic(Перехват([описание, файл]))

    исход = dict(адаптер.результаты_батча("msgbatch_1"))
    целая = исход["целая"]
    assert isinstance(целая, Ответ)
    assert json.loads(целая.текст)["title"] == "А"
    assert isinstance(исход["битая"], Exception)


def test_google_смешанная_пачка_отвергается() -> None:
    """Пачка у Google адресуется модели целиком: смешанная уехала бы не туда."""
    адаптер = собрать_google(Перехват([httpx2.Response(200, json={"name": "batches/1"})]))
    with pytest.raises(ОтказПровайдера, match="разным моделям"):
        адаптер.отправить_батч({"а": запрос(модель="model-a"), "б": запрос(модель="model-b")})

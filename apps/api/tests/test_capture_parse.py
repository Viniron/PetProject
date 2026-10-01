"""Разбор захвата моделью (Э12в): ответ модели -> черновик, отказы -> причина.

Модель здесь подставная и отвечает по сценарию: сеть в прогоне замокана
всегда (`CLAUDE.md`). Проверяется то, что ломается тихо:

- **время модели местное, зону ставит код** - и время, которого не бывает
  или которое бывает дважды, помечается, а не округляется;
- **сомнение решает код**: порог из настроек, подставленная длительность
  помечена, противоречие в ответе не превращается в событие нулевой длины;
- **отказ слоя - это причина словами, а не исключение наружу**: черновик
  без разбора законен, выдумка - нет (инвариант 9).

Разбор на уровне `в_черновик` - чистая арифметика и проверяется без базы.
`разобрать` зовёт настоящее ядро слоя, которое пишет аудит и считает
потолок, - поэтому ему нужна настоящая Postgres.
"""

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry
from jarvis_api.domain import capture_parse
from jarvis_api.domain.capture_parse import РазборСобытия, в_черновик
from jarvis_api.integrations import llm

МОСКВА = ZoneInfo("Europe/Moscow")
# Зона с переходами часов. У owner их нет, но зона - настройка, а не
# константа, и правило обязано работать на любой.
БЕРЛИН = ZoneInfo("Europe/Berlin")
СЕЙЧАС = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.UTC)

НАЗНАЧЕНИЕ = json.dumps(
    {
        "capture_parse": {
            "provider": "провайдер-а",
            "model": "модель-1",
            "max_tokens": 1024,
            "price_in": "0.25",
            "price_out": "1.50",
            "fallback": [
                {
                    "provider": "провайдер-б",
                    "model": "модель-2",
                    "max_tokens": 1024,
                    "price_in": "1",
                    "price_out": "5",
                }
            ],
        }
    }
)


def настройки(**поправки: Any) -> Settings:
    return Settings().model_copy(update={"llm_routing": НАЗНАЧЕНИЕ, **поправки})


def разбор(**поля: Any) -> РазборСобытия:
    исходное: dict[str, Any] = {
        "title": "Встреча с куратором",
        "starts_at": "2026-10-22T17:00:00",
        "ends_at": "2026-10-22T18:00:00",
        "location": None,
        "description": None,
        "confidence": 0.9,
    }
    return РазборСобытия.model_validate({**исходное, **поля})


def ответ(**поля: Any) -> llm.Ответ:
    return llm.Ответ(текст=разбор(**поля).model_dump_json(), токенов_вход=900, токенов_выход=60)


@dataclass
class Подставной:
    """Провайдер по сценарию; запоминает запросы, чтобы проверить, что ушло."""

    имя: str
    сценарий: list[llm.Ответ | Exception]
    запросы: list[llm.Запрос] = field(default_factory=list)

    def выполнить(self, запрос: llm.Запрос) -> llm.Ответ:
        self.запросы.append(запрос)
        что = self.сценарий.pop(0)
        if isinstance(что, Exception):
            raise что
        return что


def адаптеры(*провайдеры: Подставной) -> dict[str, llm.Адаптер]:
    return {п.имя: п for п in провайдеры}


def вызвать(
    сессия: Session,
    *провайдеры: Подставной,
    настр: Settings | None = None,
    текст: str | None = "встреча с куратором в четверг после физики",
    modality: Any = "text",
    изображение: llm.Изображение | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    return capture_parse.разобрать(
        сессия,
        modality=modality,
        текст=текст,
        изображение=изображение,
        настройки=настр or настройки(),
        адаптеры=адаптеры(*провайдеры),
        сейчас=СЕЙЧАС,
        зона=МОСКВА,
    )


# --- в_черновик: арифметика без базы ------------------------------------------


def test_местное_время_получает_зону_owner_и_хранится_в_utc() -> None:
    итог = в_черновик(разбор(), зона=МОСКВА, настройки=настройки())

    assert итог["starts_at"] == "2026-10-22T14:00:00+00:00"
    assert итог["ends_at"] == "2026-10-22T15:00:00+00:00"
    assert итог["time_uncertain"] is False
    assert итог["duration_assumed"] is False


def test_не_названная_длительность_ставится_правилом_и_помечается() -> None:
    итог = в_черновик(
        разбор(ends_at=None),
        зона=МОСКВА,
        настройки=настройки(capture_default_duration_minutes=45),
    )

    assert итог["ends_at"] == "2026-10-22T14:45:00+00:00"
    assert итог["duration_assumed"] is True


def test_без_даты_ничего_не_подставляется() -> None:
    """Нет начала - нет и конца: «час от ничего» был бы выдумкой."""
    итог = в_черновик(
        разбор(starts_at=None, ends_at=None, confidence=0.2), зона=МОСКВА, настройки=настройки()
    )

    assert итог["starts_at"] is None
    assert итог["ends_at"] is None
    assert итог["duration_assumed"] is False
    # Сомневаться не в чем: дня нет вовсе, и форма спросит его как пустое поле.
    assert итог["time_uncertain"] is False
    assert итог["title"] == "Встреча с куратором"


def test_порог_уверенности_из_настроек() -> None:
    уверенный = в_черновик(разбор(confidence=0.6), зона=МОСКВА, настройки=настройки())
    строгий = в_черновик(
        разбор(confidence=0.6),
        зона=МОСКВА,
        настройки=настройки(capture_confidence_threshold=0.7),
    )

    assert уверенный["time_uncertain"] is False
    assert строгий["time_uncertain"] is True


@pytest.mark.parametrize("значение", [-0.1, 1.7])
def test_уверенность_вне_шкалы_считается_нулём(значение: float) -> None:
    """«1.7» не значит «очень уверен» - модель не поняла шкалу."""
    итог = в_черновик(разбор(confidence=значение), зона=МОСКВА, настройки=настройки())

    assert итог["confidence"] == 0.0
    assert итог["time_uncertain"] is True


def test_конец_раньше_начала_не_становится_событием() -> None:
    итог = в_черновик(
        разбор(starts_at="2026-10-22T17:00:00", ends_at="2026-10-22T16:00:00"),
        зона=МОСКВА,
        настройки=настройки(),
    )

    assert итог["ends_at"] == "2026-10-22T15:00:00+00:00", "конец заменён правилом"
    assert итог["duration_assumed"] is True
    assert итог["time_uncertain"] is True


def test_время_в_ночь_перевода_вперёд_помечается() -> None:
    """29 марта 2026 в Берлине часы идут с 02:00 сразу на 03:00."""
    итог = в_черновик(
        разбор(starts_at="2026-03-29T02:30:00", ends_at=None),
        зона=БЕРЛИН,
        настройки=настройки(),
    )

    assert итог["time_uncertain"] is True


def test_время_в_ночь_перевода_назад_помечается() -> None:
    """25 октября 2026 в Берлине 02:30 бывает дважды."""
    итог = в_черновик(
        разбор(starts_at="2026-10-25T02:30:00", ends_at="2026-10-25T04:00:00"),
        зона=БЕРЛИН,
        настройки=настройки(),
    )

    assert итог["time_uncertain"] is True


def test_обычное_время_в_зоне_с_переходами_не_помечается() -> None:
    итог = в_черновик(
        разбор(starts_at="2026-07-01T10:00:00", ends_at="2026-07-01T11:00:00"),
        зона=БЕРЛИН,
        настройки=настройки(),
    )

    assert итог["starts_at"] == "2026-07-01T08:00:00+00:00", "летом Берлин в UTC+2"
    assert итог["time_uncertain"] is False


# --- схема ответа -------------------------------------------------------------


@pytest.mark.parametrize(
    "время",
    ["2026-10-22T17:00:00+03:00", "2026-10-22T17:00:00Z", "22.10.2026 17:00", "завтра в пять"],
)
def test_время_со_смещением_или_словами_мимо_схемы(время: str) -> None:
    """Смещение от модели - тоже догадка: зону ставит код."""
    with pytest.raises(ValidationError):
        разбор(starts_at=время)


def test_время_без_секунд_принимается() -> None:
    assert разбор(starts_at="2026-10-22T17:00").starts_at == "2026-10-22T17:00"


def test_пустые_строки_значат_нет() -> None:
    итог = разбор(title="  ", location="", description=" ")

    assert (итог.title, итог.location, итог.description) == (None, None, None)


def test_схема_требует_каждое_поле() -> None:
    """«Не нашёл» модель обязана произнести null-ом, а не пропустить поле."""
    схема = РазборСобытия.model_json_schema()

    assert set(схема["required"]) == {
        "title",
        "starts_at",
        "ends_at",
        "location",
        "description",
        "confidence",
    }
    # Числовые границы держит код, а не схема (ADR-054 п. 4).
    assert "minimum" not in json.dumps(схема)
    assert "maximum" not in json.dumps(схема)


def test_короткие_настройки_дают_одну_попытку() -> None:
    короткие = capture_parse.короткие_настройки(
        настройки(llm_retries=3, llm_timeout_seconds=60, capture_parse_timeout_seconds=7)
    )

    assert короткие.llm_retries == 1
    assert короткие.llm_timeout_seconds == 7


# --- разобрать: настоящее ядро слоя, подставной провайдер ---------------------


def test_разбор_возвращает_поля_и_пишет_аудит(сессия: Session) -> None:
    основной = Подставной("провайдер-а", [ответ()])

    extracted, error = вызвать(сессия, основной)

    assert error is None
    assert extracted is not None
    assert extracted["title"] == "Встреча с куратором"
    записи = list(сессия.scalars(select(AuditLogEntry).where(AuditLogEntry.kind == "llm_call")))
    assert [(з.status, з.target, з.actor) for з in записи] == [
        ("ok", "capture_parse", "api.capture")
    ]


def test_в_промпт_уходят_зона_момент_и_способ_входа(сессия: Session) -> None:
    основной = Подставной("провайдер-а", [ответ()])

    вызвать(сессия, основной, modality="audio", текст="зубной в пятницу вчетыре")

    переменная = основной.запросы[0].переменная_часть
    assert "2026-10-14T12:00" in переменная, "сейчас - в зоне owner, а не в UTC"
    assert "Europe/Moscow" in переменная
    assert "надиктована" in переменная
    assert "вчетыре" in переменная


def test_фото_уходит_картинкой_тем_же_вызовом(сессия: Session) -> None:
    основной = Подставной("провайдер-а", [ответ()])
    снимок = llm.Изображение(media_type="image/png", данные=b"png")

    вызвать(сессия, основной, modality="image", текст=None, изображение=снимок)

    запрос = основной.запросы[0]
    assert запрос.изображения == (снимок,)
    assert capture_parse.ТОЛЬКО_ФАЙЛ in запрос.переменная_часть
    assert "фотография" in запрос.переменная_часть


def test_таймаут_вызова_короткий(сессия: Session) -> None:
    основной = Подставной("провайдер-а", [ответ()])

    вызвать(сессия, основной, настр=настройки(capture_parse_timeout_seconds=9))

    assert основной.запросы[0].таймаут_секунд == 9


def test_ответ_мимо_формата_времени_переспрашивается(сессия: Session) -> None:
    """Смещение в ответе - мимо схемы: повтор, а не молчаливое принятие."""
    плохой = llm.Ответ(
        текст=json.dumps(
            {
                "title": "Зубной",
                "starts_at": "2026-10-22T17:00:00+03:00",
                "ends_at": None,
                "location": None,
                "description": None,
                "confidence": 0.9,
            }
        ),
        токенов_вход=900,
        токенов_выход=60,
    )
    основной = Подставной("провайдер-а", [плохой, ответ(title="Зубной")])

    extracted, error = вызвать(сессия, основной)

    assert error is None
    assert extracted is not None and extracted["title"] == "Зубной"
    assert len(основной.запросы) == 2


def test_недоступный_провайдер_уступает_резерву(сессия: Session) -> None:
    основной = Подставной("провайдер-а", [llm.ПровайдерНедоступен("таймаут")])
    резерв = Подставной("провайдер-б", [ответ()])

    extracted, error = вызвать(сессия, основной, резерв)

    assert error is None and extracted is not None
    assert len(резерв.запросы) == 1


@pytest.mark.parametrize(
    ("сбой", "причина"),
    [
        (llm.ВыходЗаблокирован("403 Request not allowed"), "модели недоступны из сети платы"),
        (llm.ОтказПровайдера("401 invalid key"), "модель отказала в разборе"),
    ],
)
def test_отказ_провайдера_становится_причиной(
    сессия: Session, сбой: Exception, причина: str
) -> None:
    основной = Подставной("провайдер-а", [сбой])
    резерв = Подставной("провайдер-б", [])

    extracted, error = вызвать(сессия, основной, резерв)

    assert (extracted, error) == (None, причина)
    assert резерв.запросы == [], "отказ по существу на резерв не уходит"


def test_молчат_все_модели(сессия: Session) -> None:
    основной = Подставной("провайдер-а", [llm.ПровайдерНедоступен("таймаут")])
    резерв = Подставной("провайдер-б", [llm.ПровайдерНедоступен("таймаут")])

    assert вызвать(сессия, основной, резерв) == (None, "модели сейчас не ответили")


def test_модель_не_назначена(сессия: Session) -> None:
    assert вызвать(сессия, настр=настройки(llm_routing="")) == (
        None,
        "модель для разбора не назначена",
    )


def test_испорченное_назначение_не_роняет_захват(сессия: Session) -> None:
    assert вызвать(сессия, настр=настройки(llm_routing="{не json")) == (
        None,
        "назначение моделей испорчено, проверьте LLM_ROUTING",
    )


def test_маршрут_к_неподключённому_провайдеру(сессия: Session) -> None:
    """Ключа провайдера нет в env - для экрана это то же «назначение неверно»."""
    assert вызвать(сессия) == (None, "назначение моделей испорчено, проверьте LLM_ROUTING")


def test_исчерпанный_потолок(сессия: Session) -> None:
    сессия.add(
        AuditLogEntry(
            kind="llm_call",
            actor="tests",
            status="ok",
            target="capture_parse",
            provider="провайдер-а",
            model="модель-1",
            cost_usd=Decimal("5"),
            at=СЕЙЧАС - dt.timedelta(hours=1),
        )
    )
    сессия.flush()
    основной = Подставной("провайдер-а", [])

    итог = вызвать(сессия, основной, настр=настройки(llm_monthly_cap_usd=5))

    assert итог == (None, "месячный лимит на модели исчерпан")
    assert основной.запросы == [], "за потолком модель не зовётся вовсе"

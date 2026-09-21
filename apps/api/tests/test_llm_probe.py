"""Живая проба моделей (Э12б): сухой прогон, аудит, потолок.

Главное здесь - первый тест: **без `--apply` не делается ни одного сетевого
вызова.** Проба тратит деньги owner, и команда, тратящая их от одного нажатия
Enter, однажды будет запущена «просто посмотреть». Проверяется это адаптером,
который взрывается при любом обращении, - не подсчётом вызовов, который сам
по себе можно забыть обновить.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry
from jarvis_api.integrations.llm.base import Запрос, Ответ, ПровайдерНедоступен
from jarvis_api.jobs import llm_probe

НАЗНАЧЕНИЕ = (
    '{"capture_parse": {"provider": "провайдер-а", "model": "модель-1", '
    '"max_tokens": 64, "price_in": "1", "price_out": "5", '
    '"fallback": [{"provider": "провайдер-б", "model": "модель-2", '
    '"max_tokens": 64, "price_in": "2", "price_out": "10"}]}}'
)


class Взрывающийся:
    """Адаптер, который не должен быть вызван ни разу."""

    имя = "провайдер-а"

    def выполнить(self, запрос: Запрос) -> Ответ:
        raise AssertionError("сухой прогон не имеет права ходить в сеть")


class Отвечающий:
    """Адаптер, отвечающий по схеме пробы."""

    def __init__(self, имя: str, ответ: Ответ | Exception | None = None) -> None:
        self.имя = имя
        self.ответ = ответ or Ответ(текст='{"ready": true}', токенов_вход=20, токенов_выход=5)
        self.вызовов = 0

    def выполнить(self, запрос: Запрос) -> Ответ:
        self.вызовов += 1
        if isinstance(self.ответ, Exception):
            raise self.ответ
        return self.ответ


def настройки(**поля: object) -> Settings:
    основа: dict[str, object] = {
        "llm_routing": НАЗНАЧЕНИЕ,
        "llm_monthly_cap_usd": Decimal("5"),
        "llm_timeout_seconds": 42,
    }
    return Settings(**{**основа, **поля})  # type: ignore[arg-type]


def test_сухой_прогон_не_ходит_в_сеть(сессия: Session) -> None:
    """Деньги owner не тратятся от одного нажатия Enter."""
    код = llm_probe.run_once(
        сессия,
        настройки(),
        apply=False,
        адаптеры={"провайдер-а": Взрывающийся(), "провайдер-б": Взрывающийся()},
    )

    assert код == 0
    assert сессия.scalars(select(AuditLogEntry)).all() == []


def test_проверяются_и_резервы_тоже(сессия: Session) -> None:
    """Резерв, о недоступности которого узнаёшь в момент отказа основной,
    - это не резерв, а вторая поломка."""
    первый = Отвечающий("провайдер-а")
    второй = Отвечающий("провайдер-б")

    код = llm_probe.run_once(
        сессия,
        настройки(),
        apply=True,
        адаптеры={"провайдер-а": первый, "провайдер-б": второй},
    )

    assert код == 0
    assert первый.вызовов == 1
    assert второй.вызовов == 1


def test_живой_вызов_пишется_в_аудит(сессия: Session) -> None:
    """Незаписанный расход - это расход, которого потолок не видит (инвариант 8)."""
    llm_probe.run_once(
        сессия,
        настройки(),
        apply=True,
        адаптеры={
            "провайдер-а": Отвечающий("провайдер-а"),
            "провайдер-б": Отвечающий("провайдер-б"),
        },
    )

    записи = сессия.scalars(select(AuditLogEntry).order_by(AuditLogEntry.id)).all()
    assert len(записи) == 2
    assert all(запись.actor == llm_probe.АКТОР for запись in записи)
    assert all((запись.detail or {})["probe"] is True for запись in записи)
    # 20 токенов входа по $1 и 5 выхода по $5 за 1M - считается та же цена,
    # что и на настоящем вызове, из того же места.
    assert записи[0].cost_usd == Decimal("0.000045")


def test_отказ_провайдера_не_роняет_прогон_и_краснеет(сессия: Session) -> None:
    """Отчёт читают глазами: один отказ из двух обязан быть виден и в коде выхода."""
    код = llm_probe.run_once(
        сессия,
        настройки(),
        apply=True,
        адаптеры={
            "провайдер-а": Отвечающий("провайдер-а", ПровайдерНедоступен("таймаут")),
            "провайдер-б": Отвечающий("провайдер-б"),
        },
    )

    assert код == 1
    записи = сессия.scalars(select(AuditLogEntry).order_by(AuditLogEntry.id)).all()
    # У не дошедшего до ответа вызова стоимости нет: токены не списаны.
    assert записи[0].status == "error"
    assert записи[0].cost_usd is None


def test_потолок_останавливает_и_пробу(сессия: Session) -> None:
    """Обойти потолок «ради диагностики» - та самая дырка, из-за которой
    предохранитель перестаёт быть предохранителем."""
    сессия.add(
        AuditLogEntry(
            kind="llm_call",
            actor="прошлое",
            status="ok",
            target="capture_parse",
            cost_usd=Decimal("5.00"),
            at=dt.datetime.now(dt.UTC),
        )
    )
    сессия.commit()

    взрыв = Взрывающийся()
    код = llm_probe.run_once(
        сессия, настройки(), apply=True, адаптеры={"провайдер-а": взрыв, "провайдер-б": взрыв}
    )

    assert код == 1


def test_испорченное_назначение_отличается_от_пустого(сессия: Session) -> None:
    """Опечатка в JSON и «не назначено» чинятся по-разному, и путать их дорого."""
    assert llm_probe.run_once(сессия, настройки(llm_routing="{сломано"), apply=False) == 2
    assert llm_probe.run_once(сессия, настройки(llm_routing=""), apply=False) == 0


@pytest.mark.parametrize("флаг", [True, False])
def test_нет_ключа_это_не_падение_а_строка_отчёта(сессия: Session, флаг: bool) -> None:
    """Провайдер без ключа - рабочее состояние, о котором надо сказать вслух."""
    код = llm_probe.run_once(сессия, настройки(), apply=флаг, адаптеры={})
    assert код == (1 if флаг else 0)

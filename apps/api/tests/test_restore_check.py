"""Ежемесячная проверка восстановления (Э9, SPEC §11.1, ADR-043).

Postgres здесь не поднимается: всё, что ходит наружу процесса, подменяется.
Проверяется не `pg_restore` - его проверять незачем, - а разбор его вывода
и правила, по которым проверка решает, что копия негодна. Ровно эти правила
и отличают настоящую проверку от «вернул ноль, значит всё хорошо».
"""

import datetime as dt
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from jarvis_api.config import Settings
from jarvis_api.integrations import heartbeat
from jarvis_api.jobs import restore_check

СЕЙЧАС = dt.datetime(2026, 9, 17, 6, 0, tzinfo=dt.UTC)

# Оглавление в том виде, в каком его печатает `pg_restore --list`.
ОГЛАВЛЕНИЕ = """;
; Archive created at 2026-09-17 03:30:00 UTC
;     dbname: jarvis
;
;
; Selected TOC Entries:
;
215; 1259 16456 TABLE public itmo_lessons jarvis
216; 1259 16470 TABLE public calendar_events jarvis
217; 1259 16480 TABLE public alembic_version jarvis
218; 1259 16490 SEQUENCE public itmo_lessons_id_seq jarvis
3412; 0 16456 TABLE DATA public itmo_lessons jarvis
3413; 0 16470 TABLE DATA public calendar_events jarvis
3550; 2606 16500 CONSTRAINT public itmo_lessons itmo_lessons_pkey jarvis
"""


def настройки(tmp_path: Path, **переопределения: Any) -> Settings:
    значения: dict[str, Any] = {
        "backup_dir": str(tmp_path),
        "postgres_password": "pw",
        "restore_heartbeat_url": "https://hc.test/restore",
    }
    значения.update(переопределения)
    return Settings(**значения)


def подложить_дамп(каталог: Path, имя: str) -> Path:
    путь = каталог / имя
    путь.write_bytes(b"dump")
    return путь


# --- разбор оглавления и счётчиков ------------------------------------------


def test_состав_берётся_из_оглавления_дампа() -> None:
    """Ожидаемый список приходит из самого дампа, а не из репозитория рядом с ним."""
    assert restore_check.таблицы_дампа(ОГЛАВЛЕНИЕ) == [
        "public.alembic_version",
        "public.calendar_events",
        "public.itmo_lessons",
    ]


def test_table_data_не_считается_второй_таблицей() -> None:
    """`TABLE` и `TABLE DATA` - две записи об одной таблице.

    Без отсечения состав раздувался бы вдвое, а проверка «таблицы на месте»
    начала бы искать таблицу с именем `DATA`.
    """
    только_данные = "3412; 0 16456 TABLE DATA public itmo_lessons jarvis"

    assert restore_check.таблицы_дампа(только_данные) == []


def test_пустое_оглавление_даёт_пустой_состав() -> None:
    """Молчаливый пустой состав - это провал пункта сверки, а не исключение."""
    assert restore_check.таблицы_дампа(";\n; Selected TOC Entries:\n;\n") == []


def test_счётчики_разбираются_из_вывода_psql() -> None:
    assert restore_check.счётчики_строк("itmo_lessons|42\nsettings|1\n\n") == {
        "itmo_lessons": 42,
        "settings": 1,
    }


def test_нечисловой_счётчик_это_отказ_а_не_ноль() -> None:
    """Изменившийся формат вывода обязан падать громко: ноль здесь означал бы
    «данные не восстановились» и гасил бы сторожа по ложной причине."""
    with pytest.raises(restore_check.RestoreCheckError, match="формат вывода psql"):
        restore_check.счётчики_строк("itmo_lessons|много")


# --- свежесть копии ---------------------------------------------------------


def test_свежая_копия_проходит(tmp_path: Path) -> None:
    дамп = подложить_дамп(tmp_path, "jarvis-20260917T033000Z.dump")

    пункт = restore_check.проверить_свежесть(дамп, СЕЙЧАС, 48)

    assert пункт.прошёл


def test_старая_копия_проваливает_проверку(tmp_path: Path) -> None:
    """Главный смысл пункта: бэкап встал месяц назад, а копии на диске остались.

    Без него ежемесячная проверка на прошлогоднем дампе отчиталась бы
    успехом и гасила бы тревогу ровно тогда, когда она нужна.
    """
    дамп = подложить_дамп(tmp_path, "jarvis-20260801T033000Z.dump")

    пункт = restore_check.проверить_свежесть(дамп, СЕЙЧАС, 48)

    assert not пункт.прошёл
    assert "ночной бэкап не работает" in пункт.подсказка


def test_возраст_считается_по_имени_а_не_по_mtime(tmp_path: Path) -> None:
    """mtime меняет копирование тома и восстановление хоста - то есть ровно те
    события, после которых проверка нужнее всего."""
    дамп = подложить_дамп(tmp_path, "jarvis-20260801T033000Z.dump")
    # Файл только что создан, и его mtime - «сейчас».
    assert restore_check.момент_дампа(дамп) == dt.datetime(2026, 8, 1, 3, 30, tzinfo=dt.UTC)


def test_чужое_имя_файла_объясняется_внятно(tmp_path: Path) -> None:
    дамп = подложить_дамп(tmp_path, "jarvis-вчерашний.dump")

    with pytest.raises(restore_check.RestoreCheckError, match="возраст такой копии"):
        restore_check.момент_дампа(дамп)


def test_пустой_каталог_копий_это_отказ(tmp_path: Path) -> None:
    with pytest.raises(restore_check.RestoreCheckError, match="нет ни одной копии"):
        restore_check.найти_дамп(настройки(tmp_path))


def test_берётся_самая_свежая_копия(tmp_path: Path) -> None:
    подложить_дамп(tmp_path, "jarvis-20260915T033000Z.dump")
    свежая = подложить_дамп(tmp_path, "jarvis-20260917T033000Z.dump")
    подложить_дамп(tmp_path, "jarvis-20260916T033000Z.dump")

    assert restore_check.найти_дамп(настройки(tmp_path)) == свежая


# --- сверка состава и наполнения --------------------------------------------


def пункты(таблицы: list[str], счётчики: dict[str, int]) -> dict[bool, list[str]]:
    """Итог сверки: какие пункты прошли, какие нет - по названиям."""
    итог: dict[bool, list[str]] = {True: [], False: []}
    for пункт in restore_check.сверить(таблицы, счётчики):
        итог[пункт.прошёл].append(пункт.название)
    return итог


def test_полная_база_проходит_все_пункты() -> None:
    итог = пункты(
        ["public.itmo_lessons", "public.alembic_version"],
        {"itmo_lessons": 42, "alembic_version": 1},
    )

    assert итог[False] == []


def test_пропавшая_таблица_ловится() -> None:
    """pg_restore мог пройти, а таблицы из оглавления в базе не оказаться."""
    результат = restore_check.сверить(
        ["public.itmo_lessons", "public.calendar_events", "public.alembic_version"],
        {"itmo_lessons": 42, "alembic_version": 1},
    )

    провал = next(п for п in результат if not п.прошёл)
    assert "calendar_events" in провал.подсказка


def test_дамп_без_версии_схемы_не_принимается() -> None:
    """Нет `alembic_version` - дамп снят не с базы приложения."""
    результат = restore_check.сверить(["public.itmo_lessons"], {"itmo_lessons": 42})

    провалы = [п for п in результат if not п.прошёл]
    assert any("дамп снят не с базы приложения" in п.подсказка for п in провалы)


def test_пустая_схема_не_считается_восстановлением() -> None:
    """Тот самый «случайный пустой успех»: схема есть, данных нет."""
    результат = restore_check.сверить(
        ["public.itmo_lessons", "public.alembic_version"],
        {"itmo_lessons": 0, "alembic_version": 1},
    )

    провалы = [п for п in результат if not п.прошёл]
    assert any("восстановилась схема, но не данные" in п.подсказка for п in провалы)


def test_дамп_без_таблиц_вовсе_проваливает_состав() -> None:
    результат = restore_check.сверить([], {"itmo_lessons": 42})

    assert not результат[0].прошёл


# --- защита живой базы ------------------------------------------------------


def test_scratch_не_может_совпасть_с_рабочей_базой(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Опечатка в .env стоила бы живой базы: имя подставляется в DROP DATABASE."""
    monkeypatch.setattr(shutil, "which", lambda имя: "/usr/bin/" + имя)

    def запрещено(*a: object, **kw: object) -> None:
        pytest.fail("до psql дойти не должно")

    monkeypatch.setattr(subprocess, "run", запрещено)

    настройки_ = настройки(tmp_path, postgres_db="jarvis", restore_scratch_db="jarvis")

    with pytest.raises(restore_check.RestoreCheckError, match="совпадает с рабочей базой"):
        restore_check.развернуть(настройки_, tmp_path / "jarvis-20260917T033000Z.dump")


def test_без_pg_restore_проверка_отказывается(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda имя: None)

    with pytest.raises(restore_check.RestoreCheckError, match="postgres-client"):
        restore_check.развернуть(настройки(tmp_path), tmp_path / "jarvis-20260917T033000Z.dump")


# --- прогон целиком ---------------------------------------------------------


def подменить_прогон(
    monkeypatch: pytest.MonkeyPatch, пункты_осмотра: list[restore_check.Пункт]
) -> list[str]:
    """Разворачивание и осмотр подменяются, ping - считается."""
    сигналы: list[str] = []
    monkeypatch.setattr(restore_check, "развернуть", lambda settings, дамп: None)
    monkeypatch.setattr(restore_check, "осмотреть", lambda settings, дамп: пункты_осмотра)
    monkeypatch.setattr(
        heartbeat,
        "отправить",
        lambda url, *, timeout, имя: сигналы.append(url),
    )
    return сигналы


ОСМОТР_УСПЕШЕН = [restore_check.Пункт("состав", True)]
ОСМОТР_ПРОВАЛЕН = [restore_check.Пункт("состав", False, "не восстановились: calendar_events")]


def test_apply_пингует_после_успешной_сверки(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    подложить_дамп(tmp_path, "jarvis-20260917T033000Z.dump")
    сигналы = подменить_прогон(monkeypatch, ОСМОТР_УСПЕШЕН)

    код = restore_check.run(настройки(tmp_path), apply=True, now=СЕЙЧАС)

    assert код == 0
    assert сигналы == ["https://hc.test/restore"]


def test_провал_сверки_не_пингует_и_падает(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Главный тест файла: сторож обязан молчать, когда копия не восстановилась."""
    подложить_дамп(tmp_path, "jarvis-20260917T033000Z.dump")
    сигналы = подменить_прогон(monkeypatch, ОСМОТР_ПРОВАЛЕН)

    код = restore_check.run(настройки(tmp_path), apply=True, now=СЕЙЧАС)

    assert код == 1
    assert сигналы == []


def test_старая_копия_не_пингует_даже_при_целом_составе(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дамп разворачивается и осмотр проходит, но копия месячной давности."""
    подложить_дамп(tmp_path, "jarvis-20260801T033000Z.dump")
    сигналы = подменить_прогон(monkeypatch, ОСМОТР_УСПЕШЕН)

    код = restore_check.run(настройки(tmp_path), apply=True, now=СЕЙЧАС)

    assert код == 1
    assert сигналы == []


def test_dry_run_проверяет_но_не_пингует(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry-run здесь - это полная проверка без единственной записи наружу."""
    подложить_дамп(tmp_path, "jarvis-20260917T033000Z.dump")
    сигналы = подменить_прогон(monkeypatch, ОСМОТР_УСПЕШЕН)

    код = restore_check.run(настройки(tmp_path), apply=False, now=СЕЙЧАС)

    assert код == 0
    assert сигналы == []


def test_молчащий_сторож_это_отказ_проверки(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В отличие от цепочки календаря (ADR-043): проверка существует ради
    сигнала наружу, и без него она не сделала ничего."""
    подложить_дамп(tmp_path, "jarvis-20260917T033000Z.dump")
    подменить_прогон(monkeypatch, ОСМОТР_УСПЕШЕН)

    def падение(url: str, *, timeout: int, имя: str) -> None:
        raise heartbeat.HeartbeatError("сторож ответил 500")

    monkeypatch.setattr(heartbeat, "отправить", падение)

    assert restore_check.run(настройки(tmp_path), apply=True, now=СЕЙЧАС) == 1


def test_apply_без_адреса_сторожа_отклоняется(tmp_path: Path) -> None:
    настройки_ = настройки(tmp_path, restore_heartbeat_url="")

    assert restore_check.run(настройки_, apply=True, now=СЕЙЧАС) == 1


def test_отсутствие_копии_даёт_ненулевой_код(tmp_path: Path) -> None:
    """Отказ любого шага - это код возврата, а не сообщение в никуда: смотреть
    на джоб в момент прогона некому, а на ненулевой код ругается systemd."""
    assert restore_check.run(настройки(tmp_path), apply=False, now=СЕЙЧАС) == 1

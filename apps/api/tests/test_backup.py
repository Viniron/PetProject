"""Тесты бэкапа. Проверяют то, что ломается тихо и обнаруживается поздно.

Сеть здесь не задействована ни в одном тесте: всё, что уходит наружу,
подменяется. Тест, ходящий в живой B2, не смог бы проверить главное -
что ping НЕ уходит, когда выгрузка провалилась.
"""

import hashlib
import shutil
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jarvis_api.config import Settings
from jarvis_api.integrations.b2 import B2Client, B2Error
from jarvis_api.jobs import backup


def настройки(tmp_path: Path, **переопределения: Any) -> Settings:
    """Настройки для теста: заполнено ровно столько, сколько нужно для --apply."""
    значения: dict[str, Any] = {
        "backup_dir": str(tmp_path),
        "backup_keep_local": 7,
        "b2_bucket": "bucket",
        "b2_key_id": "key",
        "b2_app_key": "secret",
        "heartbeat_url": "https://hc-ping.test/uuid",
        "postgres_password": "pw",
    }
    значения.update(переопределения)
    return Settings(**значения)


def подложить_дамп(directory: Path, имя: str, содержимое: bytes = b"dump") -> Path:
    путь = directory / имя
    путь.write_bytes(содержимое)
    return путь


# --- имя и сумма ------------------------------------------------------------


def test_имя_дампа_в_utc_и_сортируется_по_времени() -> None:
    """Имя начинается с метки времени UTC, поэтому сортировка строк = сортировка по дате."""
    раннее = backup.dump_filename(datetime(2026, 9, 3, 5, 0, 0, tzinfo=UTC))
    позднее = backup.dump_filename(datetime(2026, 9, 3, 15, 11, 16, tzinfo=UTC))

    assert позднее == "jarvis-20260903T151116Z.dump"
    assert раннее < позднее


def test_sha1_считается_по_содержимому_файла(tmp_path: Path) -> None:
    """Сумма берётся с диска, а не с того, что собирались записать."""
    путь = подложить_дамп(tmp_path, "jarvis-20260903T000000Z.dump", b"soderzhimoe")

    assert backup.sha1_of(путь) == hashlib.sha1(b"soderzhimoe", usedforsecurity=False).hexdigest()


# --- ретеншн ----------------------------------------------------------------


def test_ретеншн_оставляет_семь_копий_и_режет_остальные(tmp_path: Path) -> None:
    """Восьмая копия по счёту - лишняя, и удаляется именно самая старая."""
    for день in range(1, 11):
        подложить_дамп(tmp_path, f"jarvis-202609{день:02d}T000000Z.dump")

    лишние = backup.prune_local(настройки(tmp_path), apply=True)

    остались = sorted(p.name for p in tmp_path.glob("jarvis-*.dump"))
    assert len(остались) == 7
    assert [p.name for p in лишние] == [
        "jarvis-20260903T000000Z.dump",
        "jarvis-20260902T000000Z.dump",
        "jarvis-20260901T000000Z.dump",
    ]
    assert остались[0] == "jarvis-20260904T000000Z.dump"


def test_ретеншн_в_dry_run_ничего_не_удаляет(tmp_path: Path) -> None:
    """dry-run обязан быть безвредным даже для локальных файлов."""
    for день in range(1, 11):
        подложить_дамп(tmp_path, f"jarvis-202609{день:02d}T000000Z.dump")

    лишние = backup.prune_local(настройки(tmp_path), apply=False)

    assert len(лишние) == 3
    assert len(list(tmp_path.glob("jarvis-*.dump"))) == 10


def test_ретеншн_на_пустом_каталоге_не_падает(tmp_path: Path) -> None:
    """Первый прогон на чистом хосте - штатный случай, а не ошибка."""
    assert backup.prune_local(настройки(tmp_path / "нет-такого"), apply=True) == []


# --- раскладка по префиксам -------------------------------------------------


def test_обычный_день_идёт_только_в_daily(tmp_path: Path) -> None:
    """Четверг - не конец недели и не начало месяца, копия одна."""
    четверг = datetime(2026, 9, 3, 3, 30, tzinfo=UTC)
    assert четверг.isoweekday() == 4, "дата теста перестала быть четвергом"

    assert backup.target_prefixes(настройки(tmp_path), четверг) == ["daily"]


def test_воскресный_дамп_попадает_и_в_weekly(tmp_path: Path) -> None:
    """Недельная копия - последний дамп недели, а не первый."""
    воскресенье = datetime(2026, 9, 6, 3, 30, tzinfo=UTC)
    assert воскресенье.isoweekday() == 7, "дата теста перестала быть воскресеньем"

    assert backup.target_prefixes(настройки(tmp_path), воскресенье) == ["daily", "weekly"]


def test_первое_число_попадает_и_в_monthly(tmp_path: Path) -> None:
    """Месячная копия обязана выжить после удаления ежедневных облаком."""
    первое = datetime(2026, 9, 1, 3, 30, tzinfo=UTC)
    assert первое.isoweekday() != 7, "дата теста стала воскресеньем - выберите другую"

    assert backup.target_prefixes(настройки(tmp_path), первое) == ["daily", "monthly"]


def test_первое_число_в_воскресенье_идёт_во_все_три(tmp_path: Path) -> None:
    """Случай, который ломается тихо: копия обязана лечь трижды, а не дважды."""
    первое_и_воскресенье = datetime(2026, 11, 1, 3, 30, tzinfo=UTC)
    assert первое_и_воскресенье.isoweekday() == 7
    assert первое_и_воскресенье.day == 1

    prefixes = backup.target_prefixes(настройки(tmp_path), первое_и_воскресенье)

    assert prefixes == ["daily", "weekly", "monthly"]


def test_выгрузка_повторяется_для_каждого_префикса(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Один дамп - несколько копий в бакете, по одной на префикс."""
    файл = подложить_дамп(tmp_path, "jarvis-20261101T033000Z.dump")
    загружено: list[str] = []

    def загрузка(self: B2Client, bucket: str, remote: str, source: Path, sha1: str) -> str:
        загружено.append(remote)
        return "file-id"

    monkeypatch.setattr(B2Client, "authorize", lambda self: None)
    monkeypatch.setattr(B2Client, "upload", загрузка)

    ids = backup.upload_dump(настройки(tmp_path), файл, "sha1", ["daily", "weekly", "monthly"])

    assert загружено == [
        "daily/jarvis-20261101T033000Z.dump",
        "weekly/jarvis-20261101T033000Z.dump",
        "monthly/jarvis-20261101T033000Z.dump",
    ]
    assert len(ids) == 3


def test_авторизация_в_b2_одна_на_все_префиксы(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Токен B2 живёт сутки - запрашивать его на каждый префикс незачем."""
    файл = подложить_дамп(tmp_path, "jarvis-20261101T033000Z.dump")
    авторизаций = 0

    def авторизация(self: B2Client) -> None:
        nonlocal авторизаций
        авторизаций += 1

    monkeypatch.setattr(B2Client, "authorize", авторизация)
    monkeypatch.setattr(B2Client, "upload", lambda self, b, r, s, h: "file-id")

    backup.upload_dump(настройки(tmp_path), файл, "sha1", ["daily", "weekly", "monthly"])

    assert авторизаций == 1


# --- порядок шагов ----------------------------------------------------------


def test_ping_уходит_только_после_успешной_выгрузки(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Требование ADR-020, проверяемое порядком вызовов, а не чтением кода."""
    вызовы: list[str] = []

    def дамп(settings: Settings, now: datetime) -> Path:
        вызовы.append("dump")
        return подложить_дамп(tmp_path, backup.dump_filename(now))

    def выгрузка(settings: Settings, path: Path, sha1: str, prefixes: list[str]) -> list[str]:
        вызовы.append("upload")
        return ["file-id"]

    def пинг(settings: Settings) -> None:
        вызовы.append("heartbeat")

    monkeypatch.setattr(backup, "create_dump", дамп)
    monkeypatch.setattr(backup, "upload_dump", выгрузка)
    monkeypatch.setattr(backup, "send_heartbeat", пинг)

    код = backup.run(настройки(tmp_path), apply=True)

    assert код == 0
    assert вызовы == ["dump", "upload", "heartbeat"]


def test_ping_не_уходит_если_выгрузка_провалилась(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Главный тест файла: сломанный бэкап не должен отчитываться об успехе."""
    пинги: list[str] = []

    monkeypatch.setattr(
        backup,
        "create_dump",
        lambda settings, now: подложить_дамп(tmp_path, backup.dump_filename(now)),
    )

    def падение(settings: Settings, path: Path, sha1: str, prefixes: list[str]) -> list[str]:
        raise B2Error("бакет недоступен")

    monkeypatch.setattr(backup, "upload_dump", падение)
    monkeypatch.setattr(backup, "send_heartbeat", lambda settings: пинги.append("ping"))

    код = backup.run(настройки(tmp_path), apply=True)

    assert код == 1
    assert пинги == []


def test_dry_run_не_выгружает_и_не_пингует(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Без --apply наружу не уходит ни один байт."""

    def запрещено(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run обратился наружу")

    monkeypatch.setattr(
        backup,
        "create_dump",
        lambda settings, now: подложить_дамп(tmp_path, backup.dump_filename(now)),
    )
    monkeypatch.setattr(backup, "upload_dump", запрещено)
    monkeypatch.setattr(backup, "send_heartbeat", запрещено)

    assert backup.run(настройки(tmp_path), apply=False) == 0


# --- отказы -----------------------------------------------------------------


def test_упавший_pg_dump_даёт_ненулевой_код(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Инвариант 9: джоб падает громко, а не пишет мусор дальше по цепочке."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pg_dump")

    class Результат:
        returncode = 1
        stderr = "connection refused"
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Результат())
    monkeypatch.setattr(backup, "send_heartbeat", lambda settings: None)

    assert backup.run(настройки(tmp_path), apply=True) == 1


def test_пустой_дамп_считается_отказом(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pg_dump может выйти с нулём и оставить пустой файл - это не копия."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pg_dump")

    class Результат:
        returncode = 0
        stderr = ""
        stdout = ""

    def пустышка(*args: Any, **kwargs: Any) -> Результат:
        # Файл создаётся пустым - ровно то, что делает оборвавшийся pg_dump.
        (tmp_path / backup.dump_filename(datetime(2026, 9, 3, tzinfo=UTC))).touch()
        return Результат()

    monkeypatch.setattr(subprocess, "run", пустышка)

    with pytest.raises(backup.BackupError, match="пуст"):
        backup.create_dump(настройки(tmp_path), datetime(2026, 9, 3, tzinfo=UTC))


def test_отсутствие_pg_dump_объясняется_внятно(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сообщение должно указывать на причину, а не на трассировку."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(backup.BackupError, match="pg_dump"):
        backup.create_dump(настройки(tmp_path), datetime(2026, 9, 3, tzinfo=UTC))


def test_apply_без_контроля_отклоняется(tmp_path: Path) -> None:
    """Бэкап без healthchecks - бэкап, о поломке которого узнают в день восстановления."""
    with pytest.raises(backup.BackupError, match="HEARTBEAT_URL"):
        backup._require_for_apply(настройки(tmp_path, heartbeat_url=""))


def test_apply_перечисляет_все_недостающие_переменные(tmp_path: Path) -> None:
    """Одна ошибка за прогон вместо пяти последовательных."""
    with pytest.raises(backup.BackupError) as отказ:
        backup._require_for_apply(настройки(tmp_path, b2_bucket="", b2_app_key=""))

    assert "B2_BUCKET" in str(отказ.value)
    assert "B2_APP_KEY" in str(отказ.value)


# --- клиент B2 --------------------------------------------------------------


def test_b2_без_адреса_api_отказывает_сразу(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ответ без apiUrl означает негодный ключ - дальше идти нельзя."""
    monkeypatch.setattr(
        B2Client, "_request", lambda self, url, **kwargs: {"authorizationToken": "t"}
    )
    клиент = B2Client(api_url="https://api.test", key_id="k", app_key="s", timeout=5)

    with pytest.raises(B2Error, match="apiUrl"):
        клиент.authorize()


def test_b2_берёт_bucket_id_из_ключа_не_запрашивая_список(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ, выданный на один бакет, приносит его id сам - право на список не нужно."""
    monkeypatch.setattr(
        B2Client,
        "_request",
        lambda self, url, **kwargs: {
            "authorizationToken": "t",
            "accountId": "a",
            "apiInfo": {"storageApi": {"apiUrl": "https://api123.test", "bucketId": "b-42"}},
        },
    )
    клиент = B2Client(api_url="https://api.test", key_id="k", app_key="s", timeout=5)
    клиент.authorize()

    def нельзя(self: B2Client, url: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("запрошен список бакетов, хотя id уже известен")

    monkeypatch.setattr(B2Client, "_request", нельзя)

    assert клиент.bucket_id("bucket") == "b-42"


def test_b2_отвергает_расхождение_сумм(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если B2 подтвердил другую сумму, в бакете лежит не наш файл."""
    файл = подложить_дамп(tmp_path, "jarvis-20260903T000000Z.dump", b"data")

    monkeypatch.setattr(
        B2Client,
        "_request",
        lambda self, url, **kwargs: {
            "authorizationToken": "t",
            "accountId": "a",
            "apiInfo": {"storageApi": {"apiUrl": "https://api123.test", "bucketId": "b-42"}},
            "uploadUrl": "https://upload.test/f",
        },
    )

    class Ответ:
        status = 200

        def read(self) -> bytes:
            return b'{"fileId": "f-1", "contentSha1": "0000000000000000000000000000000000000000"}'

        def __enter__(self) -> "Ответ":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Ответ())

    клиент = B2Client(api_url="https://api.test", key_id="k", app_key="s", timeout=5)
    клиент.authorize()

    with pytest.raises(B2Error, match="другую сумму"):
        клиент.upload("bucket", "daily/f.dump", файл, backup.sha1_of(файл))

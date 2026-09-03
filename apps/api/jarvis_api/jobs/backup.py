"""Ночной бэкап базы: pg_dump на диск, выгрузка в B2, ping в healthchecks.

Порядок шагов - не деталь реализации, а требование ADR-020: ping уходит
**после** успешной выгрузки, а не после запуска джоба. Иначе молча
сломавшийся бэкап обнаруживается ровно в тот день, когда понадобился.

Локальные копии подрезаются тоже после выгрузки: удалять файл, который ещё
не уехал в облако, значит на секунду остаться совсем без копии.

Джоб падает громко (инвариант 9): смотреть на него в момент запуска некому,
поэтому отказ обязан быть заметен позже - ненулевым кодом возврата и записью
в журнал, а не сообщением в никуда.
"""

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from jarvis_api.config import Settings, get_settings
from jarvis_api.integrations.b2 import B2Client, B2Error

logger = logging.getLogger("jarvis.backup")

# Читаем файл блоками: дамп со временем перестанет влезать в память Pi,
# а посчитать сумму нужно будет всё равно.
_SHA1_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """Отказ бэкапа на любом шаге."""


def dump_filename(now: datetime) -> str:
    """Имя дампа. Время только UTC - иначе смена часового пояса перемешает порядок."""
    return f"jarvis-{now.strftime('%Y%m%dT%H%M%SZ')}.dump"


def create_dump(settings: Settings, now: datetime) -> Path:
    """Снимает дамп в формате custom (-Fc) - его умеет pg_restore.

    Формат не plain-text намеренно: custom сжат, восстанавливается выборочно
    и не зависит от версии psql на хосте восстановления.
    """
    if shutil.which("pg_dump") is None:
        raise BackupError("pg_dump не найден в PATH - бэкап идёт в образе с postgres-client")

    target_dir = Path(settings.backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / dump_filename(now)

    # Пароль передаётся переменной окружения, а не аргументом: аргументы
    # видны всей системе в списке процессов.
    env = dict(os.environ)
    env["PGPASSWORD"] = settings.postgres_password

    command = [
        "pg_dump",
        "--format=custom",
        "--no-password",
        f"--host={settings.postgres_host}",
        f"--port={settings.postgres_port}",
        f"--username={settings.postgres_user}",
        f"--dbname={settings.postgres_db}",
        f"--file={target}",
    ]
    logger.info("снимаю дамп в %s", target)
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        # Недоделанный файл убираем сами: иначе следующий прогон посчитает
        # обрезанный дамп полноценной копией и подрежет по нему ретеншн.
        target.unlink(missing_ok=True)
        raise BackupError(f"pg_dump вышел с кодом {result.returncode}: {result.stderr.strip()}")
    if not target.exists() or target.stat().st_size == 0:
        raise BackupError(f"pg_dump отчитался успехом, но файл {target} пуст")

    logger.info("дамп готов: %s байт", target.stat().st_size)
    return target


def sha1_of(path: Path) -> str:
    """Сумма файла, посчитанная по факту записи, а не по тому, что писали."""
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(_SHA1_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def local_dumps(settings: Settings) -> list[Path]:
    """Локальные дампы, новые первыми. Сортировка по имени - оно начинается с даты UTC."""
    directory = Path(settings.backup_dir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("jarvis-*.dump"), reverse=True)


def prune_local(settings: Settings, apply: bool) -> list[Path]:
    """Оставляет backup_keep_local свежих копий. Возвращает лишние."""
    extra = local_dumps(settings)[settings.backup_keep_local :]
    for path in extra:
        if apply:
            path.unlink(missing_ok=True)
            logger.info("удалена старая копия %s", path.name)
        else:
            logger.info("dry-run: удалил бы старую копию %s", path.name)
    return extra


def target_prefixes(settings: Settings, now: datetime) -> list[str]:
    """Префиксы, под которыми копия ляжет в бакет.

    Глубина хранения 7 ежедневных / 4 недельных / 6 месячных (ADR-020)
    задаётся правилами жизненного цикла на стороне B2, а они различают копии
    только по префиксу имени. Значит раскладывать обязан джоб: иначе все
    копии попадают под одно правило и живут одинаково недолго.

    Один и тот же дамп может уйти сразу в несколько префиксов - и это не
    дублирование по недосмотру, а именно то, что нужно: недельная копия
    обязана выжить после того, как ежедневная будет удалена облаком.
    """
    prefixes = [settings.backup_prefix]
    # Воскресенье - конец недели по ISO, поэтому недельной копией становится
    # последний дамп недели, а не первый.
    if now.isoweekday() == 7:
        prefixes.append(settings.backup_prefix_weekly)
    if now.day == 1:
        prefixes.append(settings.backup_prefix_monthly)
    return prefixes


def upload_dump(settings: Settings, path: Path, sha1: str, prefixes: list[str]) -> list[str]:
    """Выгружает дамп под каждым из префиксов. Возвращает fileId копий.

    Авторизация одна на все загрузки: токен B2 живёт сутки, и запрашивать
    его повторно на каждый префикс незачем.
    """
    client = B2Client(
        api_url=settings.b2_api_url,
        key_id=settings.b2_key_id,
        app_key=settings.b2_app_key,
        timeout=settings.http_timeout_seconds,
    )
    client.authorize()

    file_ids: list[str] = []
    for prefix in prefixes:
        remote = f"{prefix}/{path.name}"
        logger.info("выгружаю %s в бакет %s", remote, settings.b2_bucket)
        file_id = client.upload(settings.b2_bucket, remote, path, sha1)
        logger.info("выгружено, fileId=%s", file_id)
        file_ids.append(file_id)
    return file_ids


def send_heartbeat(settings: Settings) -> None:
    """Сообщает healthchecks, что копия уехала. Вызывается только после выгрузки."""
    request = urllib.request.Request(settings.heartbeat_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=settings.http_timeout_seconds) as response:
            code = int(response.status)
    except urllib.error.URLError as error:
        raise BackupError(f"ping в healthchecks не дошёл: {error.reason}") from error
    if code >= 400:
        raise BackupError(f"healthchecks ответил {code}")
    logger.info("ping отправлен, ответ %s", code)


def _require_for_apply(settings: Settings) -> None:
    """Проверяет, что для реальной записи хватает настроек.

    heartbeat_url обязателен наравне с ключами B2: бэкап без контроля - это
    бэкап, о поломке которого узнают в день восстановления (ADR-020).
    """
    missing = [
        name
        for name, value in (
            ("B2_BUCKET", settings.b2_bucket),
            ("B2_KEY_ID", settings.b2_key_id),
            ("B2_APP_KEY", settings.b2_app_key),
            ("HEARTBEAT_URL", settings.heartbeat_url),
            ("POSTGRES_PASSWORD", settings.postgres_password),
        )
        if not value
    ]
    if missing:
        raise BackupError("для --apply не заполнено: " + ", ".join(missing))


def run(settings: Settings, apply: bool, now: datetime | None = None) -> int:
    """Один прогон бэкапа. Возвращает код возврата процесса."""
    moment = now or datetime.now(UTC)
    try:
        if apply:
            _require_for_apply(settings)

        dump = create_dump(settings, moment)
        checksum = sha1_of(dump)
        logger.info("sha1 дампа: %s", checksum)

        prefixes = target_prefixes(settings, moment)

        if apply:
            upload_dump(settings, dump, checksum, prefixes)
            # Ping строго здесь: выше - выгрузка, ниже - уборка. Перенос этой
            # строки вверх ломает единственную гарантию, которую даёт контроль.
            send_heartbeat(settings)
        else:
            logger.info(
                "dry-run: не выгружаю и не пингую; выгрузил бы %s в бакет %r",
                ", ".join(f"{prefix}/{dump.name}" for prefix in prefixes),
                settings.b2_bucket or "<не задан>",
            )

        prune_local(settings, apply)
    except (BackupError, B2Error, OSError):
        logger.exception("бэкап не выполнен")
        return 1

    logger.info("бэкап завершён%s", "" if apply else " (dry-run)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run - записи наружу требуют --apply."""
    parser = argparse.ArgumentParser(description="Бэкап базы JARVIS в Backblaze B2")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="выполнить выгрузку и ping (без флага - только дамп и отчёт)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

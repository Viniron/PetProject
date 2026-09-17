"""Проверка восстановления: развернуть свежий дамп рядом и осмотреть, что вышло.

`SPEC.md` §11.1 требует делать это раз в месяц. До Э9 проверка была
shell-скриптом, который запускают руками, - то есть проверкой, о которой
вспоминают в день, когда она уже не нужна. Здесь у неё появляются
расписание (таймер systemd), разбор результата и свой сторож (ADR-043).

**Что именно проверяется.** Не «pg_restore вернул ноль» - он вернёт ноль
и на дампе пустой базы, и на прошлогодней копии. Проверяется четыре вещи,
и каждая ловит свой способ остаться без данных:

1. **Свежесть дампа.** Бэкап мог встать месяц назад, а локальные копии -
   остаться. Проверка на старом файле сказала бы «всё хорошо».
2. **Восстановление без ошибок** (`--exit-on-error`: без него pg_restore
   досыпает ошибки в вывод и возвращает ноль).
3. **Состав.** Каждая таблица из оглавления дампа оказалась в базе.
   Ожидаемый список не зашит в код намеренно: сверять дамп со списком,
   который живёт рядом с ним в одном репозитории, значит сверять его
   с самим собой. Оглавление дампа - независимый источник.
4. **Данные, а не только схема.** Хотя бы одна таблица приложения непуста,
   и `alembic_version` знает свою версию. Дамп, из которого восстановилась
   пустая схема, - это тот самый «случайный пустой успех», ради которого
   на Э1 заводилась таблица `restore_probe`.

**Живая база не трогается ничем.** Дамп разворачивается в отдельную базу
(`RESTORE_SCRATCH_DB`), которая пересоздаётся каждый прогон. Восстановление
поверх живой базы не оставляет пути назад, и автоматическому джобу такое
право не даётся вовсе - даже с `--apply`.

**Почему джоб живёт в образе бэкапа.** Только там есть одновременно
`pg_restore` нужной мажорной версии и том с копиями. Отсюда ограничение,
которое легко нарушить незаметно: в этом образе стоят лишь `pydantic`
и `pydantic-settings`. Ни SQLAlchemy, ни alembic, ни httpx2 импортировать
нельзя - отказ будет на импорте внутри контейнера, и увидит его некому
до первого ночного прогона.

**Что означает `--apply`.** Только отправку ping сторожу: единственное,
что этот джоб пишет наружу. Восстановление в отдельную базу делается
в обоих режимах - иначе dry-run не проверял бы ничего.
"""

import argparse
import dataclasses
import datetime as dt
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from jarvis_api.config import Settings, get_settings
from jarvis_api.integrations import heartbeat

logger = logging.getLogger("jarvis.restore_check")

# Имя дампа: jarvis-20260917T033000Z.dump (см. backup.dump_filename).
# Время берётся из имени, а не из mtime файла: mtime меняет копирование
# тома, перенос на другой носитель и восстановление самого хоста - то есть
# ровно те события, после которых проверка нужнее всего.
ФОРМА_ИМЕНИ = re.compile(r"^jarvis-(\d{8}T\d{6}Z)\.dump$")
ФОРМАТ_ВРЕМЕНИ = "%Y%m%dT%H%M%SZ"

# Строка оглавления `pg_restore --list` для определения таблицы:
#   215; 1259 16456 TABLE public itmo_lessons jarvis
# `TABLE DATA` отсекается lookahead: это другая запись того же оглавления,
# и по ней состав считался бы дважды.
СТРОКА_ТАБЛИЦЫ = re.compile(r"^\d+;\s+\d+\s+\d+\s+TABLE\s+(?!DATA\b)(\S+)\s+(\S+)\s")

# Таблица версии схемы. Единственное имя, зашитое в код, и это не специфика
# приложения: так называется служебная таблица Alembic, а сам Alembic сюда
# импортировать нельзя.
ТАБЛИЦА_ВЕРСИИ = "alembic_version"

# Счёт строк по всем таблицам одним запросом. `pg_stat_user_tables` знает
# их список, а `n_live_tup` оттуда брать нельзя: после восстановления
# статистика не собрана и показывает нули - то есть ровно ту ошибку,
# которую проверка обязана поймать.
ЗАПРОС_СЧЁТЧИКОВ = (
    "SELECT relname, ("
    "  xpath('/row/c/text()', query_to_xml("
    "    format('select count(*) as c from %I.%I', schemaname, relname),"
    "    false, true, ''))"
    ")[1]::text::bigint "
    "FROM pg_stat_user_tables ORDER BY relname"
)


class RestoreCheckError(RuntimeError):
    """Отказ проверки восстановления на любом шаге."""


@dataclasses.dataclass(frozen=True, slots=True)
class Пункт:
    """Строка отчёта. Печатается целиком: отчёт читают глазами."""

    название: str
    прошёл: bool
    подсказка: str = ""


def _строка(пункт: Пункт) -> str:
    знак = "OK  " if пункт.прошёл else "FAIL"
    хвост = "" if пункт.прошёл or not пункт.подсказка else f" - {пункт.подсказка}"
    return f"[{знак}] {пункт.название}{хвост}"


def найти_дамп(settings: Settings) -> Path:
    """Свежайшая локальная копия. Имя начинается с времени UTC, поэтому сортировка по имени."""
    каталог = Path(settings.backup_dir)
    копии = sorted(каталог.glob("jarvis-*.dump"), reverse=True) if каталог.is_dir() else []
    if not копии:
        raise RestoreCheckError(
            f"в {каталог} нет ни одной копии - проверять нечего, сначала нужен бэкап"
        )
    return копии[0]


def момент_дампа(путь: Path) -> dt.datetime:
    """Время снятия из имени файла, tz-aware (инвариант 7)."""
    совпадение = ФОРМА_ИМЕНИ.match(путь.name)
    if совпадение is None:
        raise RestoreCheckError(
            f"имя {путь.name!r} не похоже на копию джоба бэкапа: "
            "возраст такой копии определить нечем"
        )
    return dt.datetime.strptime(совпадение.group(1), ФОРМАТ_ВРЕМЕНИ).replace(tzinfo=dt.UTC)


def проверить_свежесть(путь: Path, now: dt.datetime, предел_часов: int) -> Пункт:
    """Ежемесячная проверка на старой копии молчит о встоявшем бэкапе.

    Отказ здесь означает не «проверка не удалась», а «проверять уже нечего»:
    самая свежая копия старше, чем должна быть, то есть ночной бэкап не
    работает - и это важнее всего остального в отчёте.
    """
    возраст = now - момент_дампа(путь)
    часов = возраст.total_seconds() / 3600
    return Пункт(
        f"копия свежая ({путь.name}, {часов:.0f} ч)",
        часов <= предел_часов,
        f"самой свежей копии {часов:.0f} ч при пределе {предел_часов} ч: "
        "ночной бэкап не работает - смотреть journalctl -u jarvis-backup.service",
    )


def _окружение(settings: Settings) -> dict[str, str]:
    """Пароль - переменной окружения, а не аргументом: аргументы видны всей системе.

    `LC_ALL=C` фиксирует язык сообщений: оглавление дампа разбирается
    регуляркой по слову `TABLE`, и локализованный вывод сломал бы разбор
    молча - состав таблиц оказался бы пустым, а проверка зелёной.
    """
    env = dict(os.environ)
    env["PGPASSWORD"] = settings.postgres_password
    env["LC_ALL"] = "C"
    return env


def _выполнить(команда: list[str], settings: Settings, *, что: str) -> str:
    """Внешняя команда с проверкой кода возврата. Возвращает stdout."""
    результат = subprocess.run(
        команда, env=_окружение(settings), capture_output=True, text=True, check=False
    )
    if результат.returncode != 0:
        raise RestoreCheckError(f"{что}: код {результат.returncode}, {результат.stderr.strip()}")
    return результат.stdout


def _psql(settings: Settings, *, база: str, sql: str, что: str) -> str:
    """Запрос к базе в формате без заголовков и выравнивания (-tA)."""
    return _выполнить(
        [
            "psql",
            f"--host={settings.postgres_host}",
            f"--port={settings.postgres_port}",
            f"--username={settings.postgres_user}",
            f"--dbname={база}",
            "--no-password",
            "-tAc",
            sql,
        ],
        settings,
        что=что,
    )


def таблицы_дампа(оглавление: str) -> list[str]:
    """Имена таблиц из `pg_restore --list`, со схемой: `public.itmo_lessons`.

    Это независимый от репозитория список ожидаемого: он лежит внутри самого
    дампа, а не рядом с кодом, который дамп снимал.
    """
    имена = []
    for строка in оглавление.splitlines():
        совпадение = СТРОКА_ТАБЛИЦЫ.match(строка.strip())
        if совпадение is not None:
            схема, имя = совпадение.groups()
            имена.append(f"{схема}.{имя}")
    return sorted(set(имена))


def счётчики_строк(вывод: str) -> dict[str, int]:
    """`relname|42` -> {relname: 42}. Пустые строки вывода пропускаются."""
    счётчики: dict[str, int] = {}
    for строка in вывод.splitlines():
        голая = строка.strip()
        if not голая or "|" not in голая:
            continue
        имя, _, число = голая.partition("|")
        try:
            счётчики[имя.strip()] = int(число.strip())
        except ValueError as ошибка:
            raise RestoreCheckError(
                f"не разобрал счётчик строк: {голая!r} - изменился формат вывода psql"
            ) from ошибка
    return счётчики


def сверить(таблицы: list[str], счётчики: dict[str, int]) -> list[Пункт]:
    """Состав и наполнение восстановленной базы.

    Три пункта, и каждый ловит свой отказ: пропавшую таблицу, дамп не от
    приложения и восстановление одной схемы без данных.
    """
    пункты: list[Пункт] = []

    ожидались = [имя.split(".", 1)[-1] for имя in таблицы]
    пропали = [имя for имя in ожидались if имя not in счётчики]
    пункты.append(
        Пункт(
            f"таблицы из дампа на месте ({len(ожидались)} шт.)",
            bool(ожидались) and not пропали,
            (
                f"в дампе нет ни одной таблицы: {len(счётчики)} в базе"
                if not ожидались
                else "не восстановились: " + ", ".join(пропали)
            ),
        )
    )

    версия = счётчики.get(ТАБЛИЦА_ВЕРСИИ)
    пункты.append(
        Пункт(
            f"{ТАБЛИЦА_ВЕРСИИ} знает версию схемы",
            версия == 1,
            (
                f"таблицы {ТАБЛИЦА_ВЕРСИИ} нет: дамп снят не с базы приложения"
                if версия is None
                else f"строк в {ТАБЛИЦА_ВЕРСИИ}: {версия}, ожидалась одна"
            ),
        )
    )

    # Пустая база восстанавливается без единой ошибки и выглядит успехом.
    # Отличить её можно только по данным, поэтому пункт обязателен, а не
    # предупреждение: строка настроек в живой базе есть всегда.
    непустые = {имя: счёт for имя, счёт in счётчики.items() if имя != ТАБЛИЦА_ВЕРСИИ and счёт > 0}
    пункты.append(
        Пункт(
            f"данные восстановились ({len(непустые)} непустых таблиц)",
            bool(непустые),
            "все таблицы пусты: восстановилась схема, но не данные",
        )
    )

    return пункты


def развернуть(settings: Settings, дамп: Path) -> None:
    """Пересоздать scratch-базу и залить в неё дамп.

    Пересоздаётся каждый прогон: проверка должна показывать состояние дампа,
    а не накопленную историю прошлых проверок.
    """
    if shutil.which("pg_restore") is None or shutil.which("psql") is None:
        raise RestoreCheckError(
            "pg_restore или psql не найдены в PATH - проверка идёт в образе с postgres-client"
        )

    база = settings.restore_scratch_db
    if база == settings.postgres_db:
        # Защита от опечатки в .env, которая стоила бы живой базы: имена
        # подставляются в DROP DATABASE, и ошибка здесь необратима.
        raise RestoreCheckError(
            f"RESTORE_SCRATCH_DB совпадает с рабочей базой ({база}): "
            "проверка не разворачивает дамп поверх живых данных"
        )

    logger.info("пересоздаю базу %s", база)
    _psql(settings, база="postgres", sql=f'DROP DATABASE IF EXISTS "{база}"', что="DROP DATABASE")
    _psql(settings, база="postgres", sql=f'CREATE DATABASE "{база}"', что="CREATE DATABASE")

    logger.info("разворачиваю %s (%s байт)", дамп.name, дамп.stat().st_size)
    _выполнить(
        [
            "pg_restore",
            f"--host={settings.postgres_host}",
            f"--port={settings.postgres_port}",
            f"--username={settings.postgres_user}",
            f"--dbname={база}",
            "--no-password",
            # Без него pg_restore досыпает ошибки в вывод и возвращает ноль:
            # битый дамп выглядел бы восстановленным.
            "--exit-on-error",
            str(дамп),
        ],
        settings,
        что="pg_restore",
    )


def осмотреть(settings: Settings, дамп: Path) -> list[Пункт]:
    """Состав дампа против состава восстановленной базы."""
    оглавление = _выполнить(["pg_restore", "--list", str(дамп)], settings, что="pg_restore --list")
    счётчики = счётчики_строк(
        _psql(
            settings,
            база=settings.restore_scratch_db,
            sql=ЗАПРОС_СЧЁТЧИКОВ,
            что="счёт строк",
        )
    )

    for имя, счёт in sorted(счётчики.items()):
        logger.info("  %-24s %8d", имя, счёт)

    return сверить(таблицы_дампа(оглавление), счётчики)


def _require_for_apply(settings: Settings) -> None:
    """Для `--apply` нужен адрес сторожа: без него ping отправлять некуда."""
    недостаёт = [
        имя
        for имя, значение in (
            ("RESTORE_HEARTBEAT_URL", settings.restore_heartbeat_url),
            ("POSTGRES_PASSWORD", settings.postgres_password),
        )
        if not значение
    ]
    if недостаёт:
        raise RestoreCheckError("для --apply не заполнено: " + ", ".join(недостаёт))


def run(settings: Settings, apply: bool, now: dt.datetime | None = None) -> int:
    """Один прогон проверки. Ненулевой код - бэкапу верить нельзя."""
    момент = now or dt.datetime.now(dt.UTC)
    try:
        if apply:
            _require_for_apply(settings)

        дамп = найти_дамп(settings)
        пункты = [проверить_свежесть(дамп, момент, settings.restore_max_dump_age_hours)]
        развернуть(settings, дамп)
        пункты += осмотреть(settings, дамп)
    except (RestoreCheckError, OSError):
        logger.exception("проверка восстановления не выполнена")
        return 1

    for пункт in пункты:
        logger.info("%s", _строка(пункт))

    провалы = [пункт for пункт in пункты if not пункт.прошёл]
    if провалы:
        # Громко: непройденная проверка означает, что копия, на которую
        # рассчитывает вся схема живучести, ничего не восстанавливает.
        logger.error(
            "не пройдено пунктов: %d. Бэкап не восстанавливается - "
            "порядок разбора в docs/RUNBOOK.md, раздел «Восстановление из бэкапа»",
            len(провалы),
        )
        return 1

    if apply:
        # Ping строго после сверки, как у бэкапа после выгрузки: сигнал
        # означает «копия проверена», а не «джоб запустился».
        try:
            heartbeat.отправить(
                settings.restore_heartbeat_url,
                timeout=settings.http_timeout_seconds,
                имя="проверка восстановления",
            )
        except heartbeat.HeartbeatError:
            # Отказ сторожа здесь - отказ джоба, в отличие от цепочки
            # календаря (ADR-043): проверка существует ради сигнала наружу,
            # и без него она не сделала ничего.
            logger.exception("сторож проверки не получил сигнал")
            return 1
    else:
        logger.info(
            "dry-run: не пингую; при --apply сигнал ушёл бы на %s",
            settings.restore_heartbeat_url or "<не задан>",
        )

    logger.info(
        "копия восстанавливается: база %s оставлена для осмотра, следующий прогон её пересоздаст",
        settings.restore_scratch_db,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. Без `--apply` проверка идёт целиком, но наружу не пишет."""
    parser = argparse.ArgumentParser(
        description="Проверка восстановления из свежей копии (Э9, SPEC §11.1)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="отправить ping сторожу после успешной сверки (без флага - только отчёт)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

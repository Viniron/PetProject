"""Планировщик в процессе API: шесть слотов в день и догон при старте.

Единственное место, где проект знает про APScheduler. Причина не в
эстетике слоёв: у пакета нет `py.typed`, стабов не существует, поэтому
под `mypy --strict` он целиком превращается в `Any` (см. pyproject).
Держать этот `Any` в одном модуле - способ не потерять проверку типов
во всём остальном коде.

**BackgroundScheduler, а не AsyncIOScheduler.** Джобы блокирующие насквозь:
`time.sleep` в повторах ИСУ, `httplib2` у Google, десятки последовательных
записей с коммитом на каждой. В asyncio-планировщике это встало бы прямо
в цикле событий, `/health` перестал бы отвечать на минуты, а healthcheck
compose (30 с × 3) признал бы контейнер мёртвым и перезапустил его - посреди
записи в чужой календарь.

**Расписание живёт в памяти процесса.** `MemoryJobStore` по умолчанию, и это
решение ADR-016, а не лень: единственная память о прогонах - таблица
`job_runs`. `SQLAlchemyJobStore` завёл бы вторую, которая расходится с первой.

**Зона передаётся явно везде.** Транзитивный `tzlocal` иначе подставит зону
контейнера - UTC, - и всё расписание молча уедет на три часа.
"""

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from jarvis_api.config import Settings, разобрать_слоты
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.jobs import runner
from jarvis_api.jobs.common import FALLBACK_TIMEZONE, зона_без_падения

logger = logging.getLogger("jarvis.scheduler")

# Идентификатор одноразовой задачи подъёма. Явный, потому что APScheduler
# без него генерирует случайный, а по случайному нечего искать в логе.
ID_ПОДЪЁМА = "bootstrap"


def собрать_расписание(settings: Settings, зона: ZoneInfo) -> list[tuple[str, Any]]:
    """Слоты из конфига в триггеры. Чистая функция - её проверяет тест."""
    расписание: list[tuple[str, Any]] = []
    for слот in разобрать_слоты(settings.scheduler_daily_times):
        расписание.append(
            (
                f"{runner.JOB_NAME}@{слот:%H:%M}",
                CronTrigger(hour=слот.hour, minute=слот.minute, timezone=зона),
            )
        )
    return расписание


def создать_планировщик(settings: Settings) -> Any:
    """Планировщик без единого задания. Заданиями его наполняет `поднять`.

    `max_workers=1` - не про экономию потоков. Именно он не даёт догону
    и слоту пойти одновременно: `max_instances` ограничивает один и тот же
    `id`, а у догона и у слота идентификаторы разные. Второй воркер здесь
    означал бы два прогона реконсила в один и тот же календарь.
    """
    return BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        job_defaults={
            # Слот, пришедшийся на занятый воркер, обязан дождаться его,
            # а не пропасть: умолчание APScheduler - одна секунда.
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": settings.scheduler_misfire_grace_seconds,
        },
        # Явно, чтобы tzlocal не подставил зону контейнера. Триггеры несут
        # свою зону, эта используется только как умолчание для тех, у кого её нет.
        timezone=dt.UTC,
    )


def _зона_owner() -> ZoneInfo:
    """Зона из базы, с падением на умолчание при любой беде.

    Выполняется в рабочем потоке, а не в lifespan, и потому имеет право
    ходить в базу: если Postgres поднимается медленнее API, ждёт здесь
    фоновый поток, а не приём соединений.
    """
    try:
        with get_sessionmaker()() as session:
            return зона_без_падения(session)
    except Exception:
        # База недоступна или не настроена. Расписание всё равно строим:
        # молчащий планировщик хуже планировщика с умолчательной зоной -
        # прогоны потом упадут громко, в `job_runs` и в лог, а тишина
        # не оставит и этого.
        logger.exception("зона owner недоступна, беру умолчание %s", FALLBACK_TIMEZONE)
        return ZoneInfo(FALLBACK_TIMEZONE)


def поднять(планировщик: Any, settings: Settings) -> None:
    """Наполняет планировщик заданиями и делает догоняющий запуск.

    Выполняется целиком в рабочем потоке - это принципиально. Всё, что
    ходит в базу или в сеть, обязано быть здесь, а не в lifespan: uvicorn
    не принимает соединений, пока startup не закончился, и одно повисшее
    подключение к базе превратилось бы в цикл перезапуска контейнера.
    """
    if not планировщик.running:
        # Процесс успел остановиться, пока подъём ждал воркера. Ставить
        # задания в остановленный планировщик бессмысленно, а на плате
        # это ещё и шум в логе при каждом рестарте контейнера.
        logger.info("планировщик уже остановлен, подъём пропущен")
        return

    зона = _зона_owner()
    for идентификатор, триггер in собрать_расписание(settings, зона):
        планировщик.add_job(
            _прогон_по_расписанию,
            trigger=триггер,
            id=идентификатор,
            args=[settings],
            replace_existing=True,
        )
    logger.info(
        "расписание джобов: %s (зона %s)",
        settings.scheduler_daily_times,
        зона.key,
    )

    try:
        runner.догнать(settings)
    except Exception:
        # Догон - лучшее усилие. Его отказ не должен уносить с собой
        # расписание, которое к этому моменту уже стоит.
        logger.exception("догоняющий запуск не выполнен")


def _прогон_по_расписанию(settings: Settings) -> None:
    """Тело слота. Тот же runner, что у догона (§11.2: runner один)."""
    try:
        runner.run(settings, apply=True)
    except Exception:
        # Исключение, вылетевшее в APScheduler, попадает в его собственный
        # логгер и не роняет процесс - но наш след нагляднее, а главное,
        # он в нашем логгере, который настроен и виден в `docker logs`.
        logger.exception("прогон по расписанию не выполнен")


def запустить(settings: Settings) -> Any:
    """Создать планировщик, поставить подъём и стартовать. Ничего не ждёт."""
    планировщик = создать_планировщик(settings)
    планировщик.add_job(
        поднять,
        trigger=DateTrigger(run_date=dt.datetime.now(dt.UTC)),
        id=ID_ПОДЪЁМА,
        args=[планировщик, settings],
        replace_existing=True,
        # None - «выполнить, как бы ни опоздал». Умолчание APScheduler здесь
        # одна секунда: занятый воркер молча выбросил бы подъём целиком,
        # и продукт остался бы без расписания вообще, ничего не сообщив.
        misfire_grace_time=None,
    )
    планировщик.start()
    return планировщик


def остановить(планировщик: Any) -> None:
    """Остановка без ожидания текущего прогона.

    `wait=True` держал бы остановку до конца записи в Google, docker прислал
    бы SIGKILL по истечении `stop_grace_period`, и получилось бы худшее из
    двух: и подвисание, и убийство посреди работы. Обрыв здесь штатен:
    `push_gcal` коммитит после каждого события, строка `job_runs` остаётся
    `running`, а следующий старт её догоняет.

    Отказ самой остановки проглатывается в лог: контейнер в этот момент уже
    уходит, и уронить процесс на выходе означало бы только испортить код
    возврата - помешать этому всё равно нечем.
    """
    try:
        планировщик.shutdown(wait=False)
    except Exception:
        logger.exception("планировщик не остановился штатно")

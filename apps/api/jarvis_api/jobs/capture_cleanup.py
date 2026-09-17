"""Джоб уборки брошенных черновиков захвата (Э8, §9).

Черновик живёт от загрузки до подтверждения или отмены. Тот, к которому
не вернулись, не исчезает сам: он остаётся строкой с текстом - а когда
появится разбор фотографий (Э12), и с байтами внутри, - которая уезжает
в каждый ночной pg_dump и оттуда в B2. Срок жизни задаётся конфигом
(`CAPTURE_DRAFT_TTL_HOURS`), а не константой в коде: это единственная
ручка, которой owner регулирует, сколько сырьё захвата лежит в бэкапах.

**Наружу не пишет ничего,** но `--apply` у него такой же, как у остальных
джобов. Причина не в симметрии команд: джоб удаляет данные owner, и цель,
делающая это по одному слову без флага, рано или поздно будет набрана
вместо соседней.
"""

import argparse
import datetime as dt
import logging
from collections.abc import Sequence

from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.domain import capture
from jarvis_api.jobs.common import зона_без_падения, отметить_прогон

logger = logging.getLogger("jarvis.capture_cleanup")

JOB_NAME = "capture_cleanup"


def cleanup(
    session: Session,
    settings: Settings,
    *,
    apply: bool,
    now: dt.datetime,
) -> list[str]:
    """Один прогон. Возвращает идентификаторы убранных (или убираемых) черновиков.

    Удаление идёт через домен, а не `delete()` запросом по условию: домен
    пишет след в `audit_log`, и без него исчезнувший черновик выглядел бы
    как никогда не существовавший. Пропавшее сырьё захвата - это ровно тот
    случай, когда owner спросит «а куда делось», и ответить нужно чем-то,
    кроме догадки.
    """
    просроченные = capture.просроченные(
        session, now=now, ttl_hours=settings.capture_draft_ttl_hours
    )
    убранные = [str(черновик.id) for черновик in просроченные]
    if not apply:
        return убранные

    for черновик in просроченные:
        capture.отменить(session, черновик)

    отметить_прогон(
        session,
        JOB_NAME,
        now.astimezone(зона_без_падения(session)).date(),
        now,
        "ok",
        None,
    )
    session.commit()
    return убранные


def run_once(session: Session, settings: Settings, *, apply: bool, now: dt.datetime) -> int:
    """Прогон поверх готовой сессии. Возвращает код возврата процесса."""
    try:
        убранные = cleanup(session, settings, apply=apply, now=now)
    except Exception:
        # Ошибка уровня драйвера делает сессию непригодной, и запись следа
        # об отказе упала бы второй раз - поэтому откат до всего остального.
        session.rollback()
        logger.exception("уборка черновиков не выполнена")
        return 1

    logger.info(
        "брошенных черновиков старше %d ч: %d%s",
        settings.capture_draft_ttl_hours,
        len(убранные),
        "" if apply else " (dry-run: ничего не удалено)",
    )
    for идентификатор in убранные:
        logger.info("  - %s", идентификатор)
    return 0


def run(settings: Settings, apply: bool, now: dt.datetime | None = None) -> int:
    """Прогон целиком: своя сессия, свой код возврата."""
    момент = now or dt.datetime.now(dt.UTC)
    with get_sessionmaker()() as session:
        return run_once(session, settings, apply=apply, now=момент)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run: без флага не удаляется ничего."""
    parser = argparse.ArgumentParser(description="Уборка брошенных черновиков захвата")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="удалить просроченные черновики (без флага - только показать)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

"""Что назначено моделям: печать разобранного `LLM_ROUTING` (Э12а, §5.1).

Не джоб: ничего не пишет, никуда не ходит, `--apply` здесь нет и быть не
может. Это ответ на вопрос, который иначе проверяется только первым живым
вызовом: **доехало ли назначение до процесса и так ли оно разобрано, как
задумал owner.**

Нужда конкретная. `LLM_ROUTING` - это JSON в одну строку в `.env` на плате,
и опечатка в нём выглядит одинаково с «задача не назначена»: и там, и там
вызов отказывает. Разница в том, что первое чинится правкой строки, а второе
- решением owner, и путать их дорого.

Печатает и задачи без назначения: пустая строка в отчёте - это тоже ответ,
и куда более внятный, чем отсутствие строки.
"""

import argparse
import logging
import sys
from collections.abc import Sequence

from jarvis_api.config import Settings, get_settings
from jarvis_api.integrations.llm.routing import Маршрут, МаршрутИспорчен, загрузить_маршруты
from jarvis_api.integrations.llm.tasks import БЕЗ_МОДЕЛИ, ПРИЧИНА_БЕЗ_МОДЕЛИ, Задача

logger = logging.getLogger("jarvis.llm.routes")


def run(settings: Settings) -> int:
    """Печатает назначение. Возвращает 2, если конфиг испорчен, иначе 0."""
    try:
        маршруты = загрузить_маршруты(settings.llm_routing)
    except МаршрутИспорчен as ошибка:
        logger.error("LLM_ROUTING не разобрался: %s", ошибка)
        return 2

    logger.info("месячный потолок на все модели: $%s", settings.llm_monthly_cap_usd)
    logger.info(
        "таймаут вызова: %s c, повторов на схему: %s",
        settings.llm_timeout_seconds,
        settings.llm_schema_retries,
    )

    if not маршруты:
        logger.info(
            "назначено: ничего. Это рабочее состояние - вызов любой задачи "
            "ответит «модель не назначена», ничего не выдумывая"
        )

    for задача in Задача:
        if задача in БЕЗ_МОДЕЛИ:
            logger.info("%-22s без модели: %s", задача.value, ПРИЧИНА_БЕЗ_МОДЕЛИ[задача])
            continue

        маршрут = маршруты.get(задача)
        if маршрут is None:
            logger.info("%-22s не назначено", задача.value)
            continue

        logger.info("%-22s %s", задача.value, _строкой(маршрут))
        for резерв in маршрут.fallback:
            logger.info("%-22s   резерв: %s", "", _строкой(резерв))
        if маршрут.degraded is not None:
            logger.info("%-22s   при потолке: %s", "", _строкой(маршрут.degraded))

    return 0


def _строкой(маршрут: Маршрут) -> str:
    return (
        f"{маршрут.provider}/{маршрут.model}, "
        f"ответ до {маршрут.max_tokens} токенов, "
        f"${маршрут.price_in}/${маршрут.price_out} за 1M"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Печать назначения моделей из LLM_ROUTING (Э12а, §5.1)"
    )
    parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    return run(get_settings())


if __name__ == "__main__":
    raise SystemExit(main())

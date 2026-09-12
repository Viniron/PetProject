"""Выгрузка контракта API в `packages/contracts/openapi.json` (Э6).

Контракт - источник истины для клиента Э7, и он **коммитится**, а не
генерируется при сборке фронта. Тогда изменение контракта видно в дифе
пул-реквеста: поле, исчезнувшее из ответа, замечает ревью, а не экран
owner. Сверка живёт тестом (`tests/test_contract.py`), а не шагом CI -
так она работает и локально через `make test`, и в CI без правки workflow.

Только stdlib намеренно: генератор клиента и валидатор OpenAPI - это
зависимости ради удобства, а клиента на Э7 генерирует уже фронтовый
инструментарий из этого же файла.
"""

import json
import logging
from pathlib import Path
from typing import Any

from jarvis_api.main import app

logger = logging.getLogger("jarvis.contract")

# Файл лежит в корне репозитория, а не внутри пакета: его потребитель -
# фронт из соседнего каталога, а не сервер. `parents[3]` - это
# apps/api/jarvis_api/contract.py -> корень. Цель `make contract`
# запускается из рабочей копии, в образе контейнера её нет.
КОРЕНЬ = Path(__file__).resolve().parents[3]
ФАЙЛ = КОРЕНЬ / "packages" / "contracts" / "openapi.json"


def схема() -> dict[str, Any]:
    """Схема живого приложения - того самого, что отвечает на запросы."""
    return app.openapi()


def сериализовать(данные: dict[str, Any]) -> str:
    """Единственный сериализатор: и запись, и сверка ходят через него.

    Два разных превратили бы расхождение в неразрешимое: `make contract`
    писал бы то, что тест отвергает, и дифф показывал бы шум форматирования.

    `sort_keys` - страховка от смены порядка ключей между версиями FastAPI:
    без неё обновление пакета даёт дифф на весь файл. `ensure_ascii=False` -
    чтобы русские описания читались в дифе, а не `\\u043f\\u0430\\u0440`.
    """
    return json.dumps(данные, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def записать() -> Path:
    """Перезаписать файл контракта. Возвращает путь - его печатает `main`."""
    ФАЙЛ.parent.mkdir(parents=True, exist_ok=True)
    ФАЙЛ.write_text(сериализовать(схема()), encoding="utf-8")
    return ФАЙЛ


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    путь = записать()
    # `print` запрещён правилом T20: вывод джобов и целей уходит в лог
    # с временем и именем, а не в голый stdout.
    logger.info("контракт выгружен: %s", путь)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

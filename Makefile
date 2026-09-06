# Единая точка входа для команд разработки.
#
# Цели одинаковы на машине owner, на Pi и в CI - расходится только путь
# к интерпретатору внутри venv, поэтому он и вынесен в переменную.
# Переопределяется снаружи: make test PY=python.

ifeq ($(OS),Windows_NT)
PY ?= apps/api/.venv/Scripts/python.exe
else
PY ?= apps/api/.venv/bin/python
endif

API := apps/api
COMPOSE := infra/docker-compose.yml
COMPOSE_PI := infra/docker-compose.pi.yml
COMPOSE_DEV := infra/docker-compose.dev.yml

# docker compose ищет .env в каталоге compose-файла, то есть в infra/, а
# лежит он в корне репозитория - поэтому --env-file указывается явно.
# Найдено на Э1 при первом подъёме стека на Pi.
#
# Если файла нет (CI), аргумент не добавляется вовсе и значения берутся из
# переменных окружения: иначе compose падает на отсутствующем файле.
ENV_FILE ?= .env
ENV_FILE_ARG := $(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE),)

DC := docker compose $(ENV_FILE_ARG) -f $(COMPOSE)
DC_PI := docker compose $(ENV_FILE_ARG) -f $(COMPOSE) -f $(COMPOSE_PI)
# У машины разработки свой env-файл, и он коммитится: в нём нет секретов,
# зато dev-база отличается от prod именем, портом и паролем (инвариант хоста 6).
DC_DEV := docker compose --env-file infra/dev.env -f $(COMPOSE) -f $(COMPOSE_DEV)

# Alembic вызывается с явным -c: пути внутри ini заданы через %(here)s,
# поэтому цель работает из корня репозитория, а не только из apps/api.
ALEMBIC := $(PY) -m alembic -c $(API)/alembic.ini

.PHONY: help venv dev test lint format compose-check up down logs \
        db-up db-down build migrate migrate-pi revision \
        sync-itmo sync-itmo-apply sync-itmo-pi plan-today \
        backup backup-apply restore-check

help:
	@echo "venv          - create apps/api/.venv and install dev extras"
	@echo "dev           - run API with reload on :8000"
	@echo "test          - pytest"
	@echo "lint          - ruff + black --check + mypy --strict"
	@echo "format        - ruff --fix + black"
	@echo "compose-check - validate every compose overlay without starting it"
	@echo "db-up         - start dev and test databases on the workstation (stage E2)"
	@echo "db-down       - stop them"
	@echo "build         - rebuild the api image after Dockerfile or dependency changes"
	@echo "up            - start the stack on the Pi (base + pi overlay)"
	@echo "down          - stop the stack"
	@echo "logs          - follow logs of the stack"
	@echo "backup        - pg_dump + report, dry-run (stage E1)"
	@echo "backup-apply  - pg_dump + upload to B2 + heartbeat ping (stage E1)"
	@echo "restore-check - restore the newest local dump into a scratch database (stage E1)"
	@echo "migrate       - alembic upgrade head against DATABASE_URL (stage E2)"
	@echo "migrate-pi    - the same inside the api container on the Pi (stage E2)"
	@echo "revision      - autogenerate a migration: make revision m=\"what changed\" (stage E2)"
	@echo "sync-itmo       - my.itmo.ru -> mirror, dry-run (stage E3)"
	@echo "sync-itmo-apply - the same, writing the mirror (stage E3)"
	@echo "sync-itmo-pi    - the same inside the api container on the Pi (stage E3)"
	@echo "plan-today    - morning planning job, dry-run (stage E5)"

venv:
	py -3.12 -m venv $(API)/.venv || python3.12 -m venv $(API)/.venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "$(API)[dev]"

dev:
	$(PY) -m uvicorn jarvis_api.main:app --reload --port 8000

test:
	$(PY) -m pytest $(API)

# Порядок неслучаен: сначала быстрый ruff, потом форматтер, потом самый
# медленный mypy. Падать дешевле на первом шаге.
lint:
	$(PY) -m ruff check $(API)
	$(PY) -m black --check $(API)
	$(PY) -m mypy --strict --config-file $(API)/pyproject.toml $(API)/jarvis_api $(API)/tests $(API)/alembic

format:
	$(PY) -m ruff check --fix $(API)
	$(PY) -m black $(API)

# Проверяются оба файла: overlay ломается ровно так же тихо, как база,
# а применяется только на Pi - то есть там, где отладка дороже всего.
compose-check:
	docker compose $(ENV_FILE_ARG) -f $(COMPOSE) config --quiet
	docker compose $(ENV_FILE_ARG) -f $(COMPOSE) -f $(COMPOSE_PI) config --quiet
	$(DC_DEV) config --quiet

# Пересборка образа. Нужна после правки Dockerfile или зависимостей:
# `up -d` поднимает уже собранный образ и молча оставляет старый, поэтому
# новый код приезжает на плату только после этой цели.
build:
	$(DC_PI) build api

up:
	$(DC_PI) up -d

down:
	$(DC_PI) down

logs:
	$(DC_PI) logs -f --tail=100

# Бэкап по умолчанию dry-run: снимает дамп, считает сумму, показывает, что
# выгрузил бы, и не отправляет наружу ни байта (CLAUDE.md).
backup:
	$(DC_PI) run --rm backup

backup-apply:
	$(DC_PI) run --rm backup --apply

restore-check:
	./infra/restore-check.sh

# Базы разработки. Тестовая эфемерная и живёт в tmpfs, dev-база - обычная:
# первую сносит каждый прогон тестов, вторую нет.
db-up:
	$(DC_DEV) up -d --wait db db-test

db-down:
	$(DC_DEV) down

# Схема накатывается только миграциями (инвариант хоста 3): create_all нет
# нигде, включая тесты, - иначе на Pi и в тестах оказались бы разные схемы.
migrate:
	$(ALEMBIC) upgrade head

# То же на Pi: там нет venv, зато есть образ с alembic внутри.
migrate-pi:
	$(DC_PI) run --rm api alembic -c /app/apps/api/alembic.ini upgrade head

# make revision m="что изменилось". Autogenerate сверяет модели с живой базой,
# поэтому DATABASE_URL должен указывать на неё, а не на пустую.
revision:
	$(ALEMBIC) revision --autogenerate -m "$(m)"

# Забор расписания. По умолчанию dry-run: показывает дифф к зеркалу и не
# пишет в базу ничего, кроме токенов, добытых входом (они - плата за доступ,
# а не результат работы; подробности в jobs/sync_itmo.py).
sync-itmo:
	$(PY) -m jarvis_api.jobs.sync_itmo

sync-itmo-apply:
	$(PY) -m jarvis_api.jobs.sync_itmo --apply

# То же на Pi: там нет venv, зато есть образ со всеми зависимостями.
sync-itmo-pi:
	$(DC_PI) run --rm api python -m jarvis_api.jobs.sync_itmo --apply

# Цели ниже перечислены в CLAUDE.md, но их реализация принадлежит следующим
# этапам. Заглушка выходит с ненулевым кодом намеренно: молчаливый успех
# несделанной работы хуже явной ошибки (инвариант 9 - падать громко).

plan-today:
	@echo "not implemented yet: stage E5, see docs/BUILD-PROGRESS.md" && exit 1

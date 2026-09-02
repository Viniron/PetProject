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

.PHONY: help venv dev test lint format compose-check migrate sync-itmo plan-today backup

help:
	@echo "venv          - create apps/api/.venv and install dev extras"
	@echo "dev           - run API with reload on :8000"
	@echo "test          - pytest"
	@echo "lint          - ruff + black --check + mypy --strict"
	@echo "format        - ruff --fix + black"
	@echo "compose-check - validate docker-compose.yml without starting it"
	@echo "migrate       - alembic upgrade head (stage E2)"
	@echo "sync-itmo     - my.itmo.ru -> Google Calendar, dry-run (stage E3)"
	@echo "plan-today    - morning planning job, dry-run (stage E5)"
	@echo "backup        - pg_dump + upload to Backblaze B2 (stage E1)"

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
	$(PY) -m mypy --strict --config-file $(API)/pyproject.toml $(API)/jarvis_api $(API)/tests

format:
	$(PY) -m ruff check --fix $(API)
	$(PY) -m black $(API)

compose-check:
	docker compose -f $(COMPOSE) config --quiet

# Цели ниже перечислены в CLAUDE.md, но их реализация принадлежит следующим
# этапам. Заглушка выходит с ненулевым кодом намеренно: молчаливый успех
# несделанной работы хуже явной ошибки (инвариант 9 - падать громко).
migrate:
	@echo "not implemented yet: schema lands in stage E2, see docs/BUILD-PROGRESS.md" && exit 1

sync-itmo:
	@echo "not implemented yet: stage E3, see docs/BUILD-PROGRESS.md" && exit 1

plan-today:
	@echo "not implemented yet: stage E5, see docs/BUILD-PROGRESS.md" && exit 1

backup:
	@echo "not implemented yet: stage E1, see docs/BUILD-PROGRESS.md" && exit 1

PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
ENV_FILE ?= .env
COMPOSE_DEV := docker compose --env-file $(ENV_FILE) -f docker/docker-compose.dev.yml

.PHONY: install fmt lint test test-integration db-up db-down db-logs

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

fmt:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy app tests

test:
	$(BIN)/pytest -m "not integration"

test-integration:
	$(BIN)/pytest -m integration

db-up:
	$(COMPOSE_DEV) up -d --wait postgres
	$(COMPOSE_DEV) exec -T postgres sh -c '\
		psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -tA \
			-c "SELECT 1 FROM pg_database WHERE datname = '\''$$POSTGRES_TEST_DB'\''" | grep -q 1 \
		|| createdb -U "$$POSTGRES_USER" "$$POSTGRES_TEST_DB"'

db-down:
	$(COMPOSE_DEV) down

db-logs:
	$(COMPOSE_DEV) logs -f postgres

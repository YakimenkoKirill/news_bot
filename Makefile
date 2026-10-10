PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install fmt lint test test-integration

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

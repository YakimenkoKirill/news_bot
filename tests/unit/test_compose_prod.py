from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE: dict[str, Any] = yaml.safe_load(
    (ROOT / "docker" / "docker-compose.prod.yml").read_text(encoding="utf-8")
)
ENTRYPOINT = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")


@pytest.fixture
def app() -> dict[str, Any]:
    service: dict[str, Any] = COMPOSE["services"]["app"]
    return service


@pytest.fixture
def postgres() -> dict[str, Any]:
    service: dict[str, Any] = COMPOSE["services"]["postgres"]
    return service


def test_restart_policy(app: dict[str, Any], postgres: dict[str, Any]) -> None:
    assert app["restart"] == "unless-stopped"
    assert postgres["restart"] == "unless-stopped"


def test_app_waits_for_healthy_postgres(app: dict[str, Any]) -> None:
    assert app["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_postgres_not_published(postgres: dict[str, Any]) -> None:
    assert "ports" not in postgres
    assert postgres["image"].startswith("postgres:16")
    assert "pg_isready" in " ".join(postgres["healthcheck"]["test"])
    assert postgres["env_file"] == "../.env"
    assert "environment" not in postgres


def test_volumes(app: dict[str, Any], postgres: dict[str, Any]) -> None:
    assert "appdata:/app/data" in app["volumes"]
    assert "pgdata:/var/lib/postgresql/data" in postgres["volumes"]
    assert {"appdata", "pgdata"} <= set(COMPOSE["volumes"])


def test_app_healthcheck_and_env(app: dict[str, Any]) -> None:
    assert app["healthcheck"]["test"] == ["CMD", "python", "-m", "app.health"]
    assert app["env_file"] == "../.env"
    assert "DATABASE_URL" not in app["environment"]
    assert app["build"]["dockerfile"] == "Dockerfile"


def test_entrypoint() -> None:
    lines = [line.strip() for line in ENTRYPOINT.splitlines()]
    assert lines[0] == "#!/bin/bash"
    assert "set -euo pipefail" in lines
    migrate = lines.index("alembic upgrade head")
    assert any(line.startswith("exec ") for line in lines[migrate:])
    assert "exec python -m app" in lines

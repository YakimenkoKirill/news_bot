import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker" / "docker-compose.dev.yml"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
VARIABLE_RE = re.compile(r"(?<!\$)\$\{([A-Z0-9_]+)")


@pytest.fixture
def compose_text() -> str:
    return COMPOSE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def postgres(compose_text: str) -> dict[str, Any]:
    compose = yaml.safe_load(compose_text)
    service: dict[str, Any] = compose["services"]["postgres"]
    return service


def _env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_postgres_image_major_16(postgres: dict[str, Any]) -> None:
    assert re.fullmatch(r"postgres:16(\.\d+)*(-[a-z0-9.]+)?", postgres["image"])


def test_healthcheck_uses_pg_isready(postgres: dict[str, Any]) -> None:
    test = postgres["healthcheck"]["test"]
    assert "pg_isready" in " ".join(test)


def test_password_only_from_variable(postgres: dict[str, Any]) -> None:
    password = postgres["environment"]["POSTGRES_PASSWORD"]
    assert re.fullmatch(r"\$\{POSTGRES_PASSWORD(:\?[^}]*)?\}", password)


def test_named_volume_for_data(compose_text: str, postgres: dict[str, Any]) -> None:
    compose = yaml.safe_load(compose_text)
    volume_name = postgres["volumes"][0].split(":", 1)[0]
    assert volume_name in compose["volumes"]


def test_compose_variables_documented_in_env_example(compose_text: str) -> None:
    used = set(VARIABLE_RE.findall(compose_text))
    assert used
    assert used <= _env_example_keys()


def test_database_url_points_to_container() -> None:
    values = dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    for key, db in (("DATABASE_URL", "POSTGRES_DB"), ("TEST_DATABASE_URL", "POSTGRES_TEST_DB")):
        expected = (
            f"postgresql+asyncpg://{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD']}"
            f"@localhost:{values['POSTGRES_PORT']}/{values[db]}"
        )
        assert values[key] == expected

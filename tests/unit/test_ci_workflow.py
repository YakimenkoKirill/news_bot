import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW: dict[Any, Any] = yaml.safe_load(
    (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
)
MAKE_TARGETS = set(
    re.findall(r"^([A-Za-z0-9_.-]+):", (ROOT / "Makefile").read_text(encoding="utf-8"), re.M)
)


def _triggers() -> dict[str, Any]:
    # PyYAML читает ключ `on` как True.
    triggers: dict[str, Any] = WORKFLOW.get("on") or WORKFLOW[True]
    return triggers


def _runs(job: str) -> list[str]:
    return [step["run"] for step in WORKFLOW["jobs"][job]["steps"] if "run" in step]


def test_four_jobs() -> None:
    assert set(WORKFLOW["jobs"]) == {"lint", "unit", "integration", "docker"}


def test_triggers_push_and_pr() -> None:
    assert set(_triggers()) == {"push", "pull_request"}


def test_postgres_16_service_with_healthcheck() -> None:
    integration = WORKFLOW["jobs"]["integration"]
    service = integration["services"]["postgres"]
    assert service["image"] == "postgres:16"
    assert "--health-cmd" in service["options"]
    assert "TEST_DATABASE_URL" in integration["env"]


def test_jobs_run_expected_targets() -> None:
    assert _runs("lint") == ["make install", "make lint"]
    assert _runs("unit") == ["make install", "make test"]
    assert _runs("integration") == [
        "make install",
        "make check-migrations",
        "make test-integration-ci",
    ]
    assert _runs("docker") == ["make docker-build"]


def test_called_targets_exist_in_makefile() -> None:
    called = {
        command.split()[1]
        for job in WORKFLOW["jobs"]
        for command in _runs(job)
        if command.startswith("make ")
    }
    assert called <= MAKE_TARGETS


def test_python_311_with_pip_cache() -> None:
    assert WORKFLOW["env"]["PYTHON_VERSION"] == "3.11"
    for job in ("lint", "unit", "integration"):
        steps = WORKFLOW["jobs"][job]["steps"]
        setup = next(step for step in steps if "setup-python" in step.get("uses", ""))
        assert setup["with"]["cache"] == "pip"


def test_no_secrets_used() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "secrets." not in text


def _makefile() -> str:
    return (ROOT / "Makefile").read_text(encoding="utf-8")


def test_integration_job_requires_database() -> None:
    """Без REQUIRE_TEST_DB пропуск интеграционных тестов остался бы незамеченным."""
    recipe = _makefile().split("test-integration-ci:")[1].split("\n\n")[0]
    assert "REQUIRE_TEST_DB=1" in recipe
    assert "-m integration" in recipe


def test_check_migrations_goes_up_down_up() -> None:
    recipe = _makefile().split("check-migrations:")[1].split("\n\n")[0]
    assert [line.split("alembic ")[1] for line in recipe.splitlines() if "alembic" in line] == [
        "upgrade head",
        "downgrade base",
        "upgrade head",
        "check",
    ]

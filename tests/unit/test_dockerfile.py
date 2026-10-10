import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = [
    line.strip()
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#")
]
INSTRUCTIONS = [
    line.strip() for line in DOCKERFILE.splitlines() if line.strip() and not line.startswith("#")
]


def test_last_user_is_not_root() -> None:
    users = [line.split(maxsplit=1)[1] for line in INSTRUCTIONS if line.startswith("USER ")]
    assert users, "в Dockerfile нет USER"
    assert users[-1] not in {"root", "0"}
    assert users[-1] == "10001"


def test_env_file_never_copied() -> None:
    copies = [line for line in INSTRUCTIONS if line.startswith(("COPY", "ADD"))]
    assert copies
    for line in copies:
        assert ".env" not in line
        assert not re.match(r"^(COPY|ADD)\s+\.\s", line), "копирование всего контекста"


def test_dockerignore_excludes_secrets_and_data() -> None:
    for pattern in (".env", "data/", "backups/", ".git/", "tests/"):
        assert pattern in DOCKERIGNORE


def test_python_311_images() -> None:
    images = re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.MULTILINE)
    assert len(images) >= 2
    for image in images:
        assert re.fullmatch(r"python:3\.11(\.\d+)?(-[a-z0-9.]+)?", image)


def test_runtime_installs_wheels_without_cache_and_runs_app() -> None:
    assert "pip install --no-cache-dir" in DOCKERFILE
    assert 'CMD ["python", "-m", "app"]' in DOCKERFILE
    assert "WORKDIR /app" in DOCKERFILE


def test_dependency_layer_built_before_code() -> None:
    builder = DOCKERFILE.split("AS runtime")[0]
    assert "COPY pyproject.toml" in builder
    assert "COPY app/" not in builder


def test_makefile_target() -> None:
    assert re.search(r"^docker-build:", (ROOT / "Makefile").read_text(encoding="utf-8"), re.M)

import re
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from app.config import CONFIGS_DIR, Settings

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
BOT_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")


def _env_fields() -> list[str]:
    return [
        name
        for name, field in Settings.model_fields.items()
        if not (isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel))
    ]


@pytest.mark.parametrize("field", _env_fields())
def test_settings_field_documented(field: str) -> None:
    variable = field.upper()
    assert f"`{variable}`" in README, f"{variable} нет в README"
    assert re.search(rf"^#?\s*{variable}=", ENV_EXAMPLE, re.M), f"{variable} нет в .env.example"


def test_test_database_url_documented() -> None:
    assert "`TEST_DATABASE_URL`" in README
    assert "TEST_DATABASE_URL=" in ENV_EXAMPLE


@pytest.mark.parametrize(
    "section",
    list(yaml.safe_load((CONFIGS_DIR / "base.yaml").read_text(encoding="utf-8"))),
)
def test_config_section_documented(section: str) -> None:
    assert f"| `{section}` |" in README


def test_no_bot_tokens() -> None:
    for text in (README, ENV_EXAMPLE):
        assert not BOT_TOKEN_RE.search(text)


def test_key_topics_present() -> None:
    for topic in (
        "make install",
        "make db-up",
        "make migrate",
        "python -m app",
        "scripts/telethon_login.py",
        "PROMPT_VERSION",
        "docker-compose.prod.yml",
        "scripts/backup.py",
        "scripts/run_report.py",
        "FloodWait",
    ):
        assert topic in README, topic

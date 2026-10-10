import importlib
import re

import pytest

import app

SUBPACKAGES = [
    "app.domain",
    "app.db",
    "app.db.repositories",
    "app.collectors",
    "app.collectors.telegram",
    "app.pipeline",
    "app.pipeline.ai",
    "app.publisher",
    "app.bot",
    "app.bot.handlers",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__name__ == name


def test_version_format() -> None:
    assert isinstance(app.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", app.__version__)

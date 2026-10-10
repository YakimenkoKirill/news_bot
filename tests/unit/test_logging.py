import json
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

from app.logging import MASK, bind_context, mask_secrets, setup_logging


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    root.handlers = handlers
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_json_line_has_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", json_output=True)
    structlog.get_logger("app.test").info("started", count=3)
    [line] = _lines(capsys)
    assert line["event"] == "started"
    assert line["level"] == "info"
    assert line["logger"] == "app.test"
    assert line["count"] == 3
    timestamp = datetime.fromisoformat(line["timestamp"])
    assert timestamp.utcoffset() is not None
    assert timestamp.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_stdlib_logger_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", json_output=True)
    logging.getLogger("third.party").warning("plain %s", "message")
    [line] = _lines(capsys)
    assert line["event"] == "plain message"
    assert line["level"] == "warning"
    assert line["logger"] == "third.party"
    assert "timestamp" in line


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("WARNING", json_output=True)
    log = structlog.get_logger("app.test")
    log.info("hidden")
    log.warning("shown")
    assert [line["event"] for line in _lines(capsys)] == ["shown"]


def test_context_inside_and_outside_block(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", json_output=True)
    log = structlog.get_logger("app.test")
    with bind_context(source_id=7, news_id=42, job="collect"):
        log.info("inside")
    log.info("outside")
    inside, outside = _lines(capsys)
    assert inside["source_id"] == 7
    assert inside["news_id"] == 42
    assert inside["job"] == "collect"
    for field in ("source_id", "news_id", "job"):
        assert field not in outside


def test_nested_context_restores_outer(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", json_output=True)
    log = structlog.get_logger("app.test")
    with bind_context(job="collect"):
        with bind_context(job="publish", news_id=1):
            log.info("inner")
        log.info("outer")
    inner, outer = _lines(capsys)
    assert inner["job"] == "publish"
    assert outer["job"] == "collect"
    assert "news_id" not in outer


def test_masks_secretstr_and_secret_keys(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", json_output=True)
    structlog.get_logger("app.test").info(
        "auth",
        credential=SecretStr("plain-secret-1"),
        bot_token="plain-secret-2",
        API_KEY="plain-secret-3",
        Password="plain-secret-4",
        apiHash="plain-secret-5",
        keywords=["ai"],
    )
    out = capsys.readouterr().out
    assert "plain-secret" not in out
    line = json.loads(out)
    for field in ("credential", "bot_token", "API_KEY", "Password", "apiHash"):
        assert line[field] == MASK
    assert line["keywords"] == ["ai"]


def test_masks_nested_dict() -> None:
    event: dict[str, Any] = {
        "event": "config",
        "settings": {"db": {"password": "p"}, "items": [{"secret": "s"}], "name": "x"},
    }
    result = dict(mask_secrets(None, "info", event))
    assert result["settings"]["db"]["password"] == MASK
    assert result["settings"]["items"][0]["secret"] == MASK
    assert result["settings"]["name"] == "x"
    assert event["settings"]["db"]["password"] == "p"


def test_unserializable_object_uses_repr(capsys: pytest.CaptureFixture[str]) -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "<Opaque>"

    setup_logging("INFO", json_output=True)
    structlog.get_logger("app.test").info("odd", obj=Opaque())
    [line] = _lines(capsys)
    assert line["obj"] == "<Opaque>"


def test_exception_is_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", json_output=True)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        structlog.get_logger("app.test").exception("failed")
    [line] = _lines(capsys)
    assert "RuntimeError: boom" in line["exception"]


def test_repeated_setup_does_not_duplicate(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", json_output=True)
    setup_logging("INFO", json_output=True)
    structlog.get_logger("app.test").info("once")
    assert len(_lines(capsys)) == 1


def test_console_output_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", json_output=False)
    structlog.get_logger("app.test").info("human", token="plain-secret")
    out = capsys.readouterr().out
    assert "human" in out
    assert "plain-secret" not in out

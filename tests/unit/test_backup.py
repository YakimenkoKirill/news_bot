from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import BackupConfig, Settings
from scripts.backup import (
    BackupError,
    backup_names,
    pg_dump_command,
    rotate,
    run_backup,
    run_pg_dump,
)

NOW = datetime(2026, 10, 10, 12, 30, 45, tzinfo=UTC)
URL = "postgresql+asyncpg://news:s3cr3t-pass@db.example:5432/news"


class FakeRun:
    def __init__(self, returncode: int = 0, stderr: str = "", writes: bool = True) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.writes = writes
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append((args, kwargs))
        if self.writes:
            Path(args[args.index("-f") + 1]).write_bytes(b"partial-or-full-dump")
        return SimpleNamespace(returncode=self.returncode, stderr=self.stderr)


def _settings(tmp_path: Path, keep_days: int = 7) -> Settings:
    return Settings.model_construct(
        database_url=URL,
        telethon_session_path=tmp_path / "data" / "telethon",
        backup=BackupConfig(keep_days=keep_days, dir=tmp_path / "backups"),
    )


def test_backup_names() -> None:
    assert backup_names(NOW) == ("db_20261010T123045Z.dump", "telethon_20261010T123045Z.session")


def test_password_not_in_argv(tmp_path: Path) -> None:
    args, extra = pg_dump_command(URL, tmp_path / "x.dump")
    assert "s3cr3t-pass" not in " ".join(args)
    assert extra == {"PGPASSWORD": "s3cr3t-pass"}
    assert args[:2] == ["pg_dump", "-Fc"]
    assert args[args.index("-h") : args.index("-h") + 2] == ["-h", "db.example"]
    assert "news" in args

    runner = FakeRun()
    run_pg_dump(URL, tmp_path / "x.dump", runner)
    [(argv, kwargs)] = runner.calls
    assert "s3cr3t-pass" not in " ".join(argv)
    assert kwargs["env"]["PGPASSWORD"] == "s3cr3t-pass"
    assert "PATH" in kwargs["env"]


def test_pg_dump_error_removes_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "x.dump"
    with pytest.raises(BackupError, match="кодом 1: connection refused"):
        run_pg_dump(URL, target, FakeRun(returncode=1, stderr="connection refused\n"))
    assert list(tmp_path.iterdir()) == []


def test_pg_dump_error_keeps_existing_backup_with_same_name(tmp_path: Path) -> None:
    target = tmp_path / "db_20261010T123045Z.dump"
    target.write_bytes(b"good-dump")
    with pytest.raises(BackupError):
        run_pg_dump(URL, target, FakeRun(returncode=1, stderr="boom"))
    assert target.read_bytes() == b"good-dump"
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_successful_dump_written_via_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "x.dump"
    runner = FakeRun()
    run_pg_dump(URL, target, runner)
    [(argv, _)] = runner.calls
    assert argv[argv.index("-f") + 1].endswith("x.dump.part")
    assert target.read_bytes() == b"partial-or-full-dump"
    assert [p.name for p in tmp_path.iterdir()] == ["x.dump"]


def test_pg_dump_missing_binary(tmp_path: Path) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("pg_dump")

    with pytest.raises(BackupError, match="не найден"):
        run_pg_dump(URL, tmp_path / "x.dump", missing)


def test_full_backup_with_session(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = tmp_path / "data" / "telethon.session"
    session.parent.mkdir()
    session.write_bytes(b"session-bytes")
    created = run_backup(settings, NOW, FakeRun())
    assert [p.name for p in created] == [
        "db_20261010T123045Z.dump",
        "telethon_20261010T123045Z.session",
    ]
    copied = tmp_path / "backups" / "telethon_20261010T123045Z.session"
    assert copied.read_bytes() == b"session-bytes"


def test_missing_session_still_saves_dump(tmp_path: Path) -> None:
    created = run_backup(_settings(tmp_path), NOW, FakeRun())
    assert [p.name for p in created] == ["db_20261010T123045Z.dump"]


def test_failed_dump_raises_and_keeps_directory_clean(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        run_backup(_settings(tmp_path), NOW, FakeRun(returncode=2, stderr="boom"))
    assert list((tmp_path / "backups").iterdir()) == []


def test_rotation_by_dates_ignores_foreign_files(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    old = NOW - timedelta(days=8)
    edge = NOW - timedelta(days=7)
    fresh = NOW - timedelta(days=1)
    names = {
        "old_dump": backup_names(old)[0],
        "old_session": backup_names(old)[1],
        "edge": backup_names(edge)[0],
        "fresh": backup_names(fresh)[0],
    }
    for name in names.values():
        (directory / name).write_text("x")
    (directory / "notes.txt").write_text("чужой файл")
    (directory / "db_manual.dump").write_text("чужой дамп")

    removed = rotate(directory, NOW, keep_days=7)
    assert sorted(p.name for p in removed) == sorted([names["old_dump"], names["old_session"]])
    remaining = sorted(p.name for p in directory.iterdir())
    assert remaining == sorted([names["edge"], names["fresh"], "notes.txt", "db_manual.dump"])

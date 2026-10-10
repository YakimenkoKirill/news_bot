"""Бэкап БД (pg_dump -Fc) и файла сессии Telethon с ротацией.

Запуск из корня репозитория:

    .venv/bin/python scripts/backup.py

Нужен pg_dump той же мажорной версии, что и сервер PostgreSQL.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.engine import make_url

from app.config import Settings, child_process_env, get_settings

log = structlog.get_logger(__name__)

TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DUMP_PREFIX = "db_"
DUMP_SUFFIX = ".dump"
SESSION_PREFIX = "telethon_"
SESSION_SUFFIX = ".session"
PARTIAL_SUFFIX = ".part"
_BACKUP_NAME = re.compile(r"^(?:db_|telethon_)(\d{8}T\d{6}Z)\.(?:dump|session)$")

Runner = Callable[..., Any]


class BackupError(RuntimeError):
    """Бэкап не удался."""


def backup_names(now: datetime) -> tuple[str, str]:
    """Имена файлов дампа и копии сессии с меткой времени UTC."""
    stamp = now.astimezone(UTC).strftime(TIMESTAMP_FORMAT)
    return f"{DUMP_PREFIX}{stamp}{DUMP_SUFFIX}", f"{SESSION_PREFIX}{stamp}{SESSION_SUFFIX}"


def pg_dump_command(database_url: str, target: Path) -> tuple[list[str], dict[str, str]]:
    """Аргументы pg_dump и дополнительные переменные окружения (пароль — только в PGPASSWORD)."""
    url = make_url(database_url)
    args = ["pg_dump", "-Fc", "-f", str(target)]
    if url.host:
        args += ["-h", url.host]
    if url.port:
        args += ["-p", str(url.port)]
    if url.username:
        args += ["-U", url.username]
    if url.database:
        args += ["-d", url.database]
    extra = {"PGPASSWORD": url.password} if url.password else {}
    return args, extra


def run_pg_dump(database_url: str, target: Path, runner: Runner = subprocess.run) -> None:
    """Выполняет pg_dump в target.

    Дамп пишется во временный файл target.part и переименовывается в target только
    при успехе, поэтому ошибка не задевает уже существующие бэкапы; частичный
    файл удаляется.

    Raises:
        BackupError: pg_dump не найден или завершился с ошибкой.
    """
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    args, extra = pg_dump_command(database_url, partial)
    try:
        result = runner(args, env=child_process_env(**extra), capture_output=True, text=True)
    except FileNotFoundError as exc:
        partial.unlink(missing_ok=True)
        raise BackupError("pg_dump не найден: установите клиент PostgreSQL") from exc
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        details = result.stderr.strip()
        raise BackupError(f"pg_dump завершился с кодом {result.returncode}: {details}")
    partial.replace(target)


def copy_session(session_file: Path, target: Path) -> bool:
    """Копирует файл сессии Telethon; False (с предупреждением), если файла нет."""
    if not session_file.is_file():
        log.warning("backup.session_missing", path=str(session_file))
        return False
    shutil.copy2(session_file, target)
    return True


def rotate(directory: Path, now: datetime, keep_days: int) -> list[Path]:
    """Удаляет бэкапы старше keep_days; файлы другого формата не трогает.

    Returns:
        Удалённые файлы.
    """
    threshold = now.astimezone(UTC) - timedelta(days=keep_days)
    removed: list[Path] = []
    for path in sorted(directory.iterdir()):
        match = _BACKUP_NAME.match(path.name)
        if not path.is_file() or match is None:
            continue
        created = datetime.strptime(match.group(1), TIMESTAMP_FORMAT).replace(tzinfo=UTC)
        if created < threshold:
            path.unlink()
            removed.append(path)
    return removed


def run_backup(settings: Settings, now: datetime, runner: Runner = subprocess.run) -> list[Path]:
    """Дамп БД, копия сессии и ротация.

    Returns:
        Созданные файлы.

    Raises:
        BackupError: Дамп не удался.
    """
    directory = settings.backup.dir
    directory.mkdir(parents=True, exist_ok=True)
    dump_name, session_name = backup_names(now)
    dump_path = directory / dump_name
    run_pg_dump(settings.database_url, dump_path, runner)
    created = [dump_path]
    session_file = Path(f"{settings.telethon_session_path}{SESSION_SUFFIX}")
    if copy_session(session_file, directory / session_name):
        created.append(directory / session_name)
    removed = rotate(directory, now, settings.backup.keep_days)
    log.info("backup.done", created=[str(p) for p in created], removed=len(removed))
    return created


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа: код 0 при успехе, 1 при ошибке дампа."""
    try:
        created = run_backup(get_settings(), datetime.now(UTC))
    except BackupError as exc:
        print(f"Ошибка бэкапа: {exc}", file=sys.stderr)
        return 1
    for path in created:
        print(f"Сохранено: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

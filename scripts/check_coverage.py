"""Проверка покрытия ключевых пакетов по отчёту coverage.json.

Запуск (после pytest --cov=app --cov-report=json):

    .venv/bin/python scripts/check_coverage.py [путь к coverage.json]

Порог берётся из [tool.coverage.report] fail_under в pyproject.toml.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / "htmlcov" / "coverage.json"
DEFAULT_PACKAGES = ("app/collectors", "app/pipeline", "app/publisher")
PERCENT = 100


class CoverageReportError(RuntimeError):
    """Отчёт покрытия отсутствует или повреждён."""


def load_threshold(pyproject: Path = ROOT / "pyproject.toml") -> float:
    """Порог fail_under из pyproject.toml."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return float(data["tool"]["coverage"]["report"]["fail_under"])


def load_report(path: Path) -> dict[str, Any]:
    """Читает coverage.json.

    Raises:
        CoverageReportError: Файла нет или он не JSON.
    """
    if not path.is_file():
        raise CoverageReportError(
            f"нет отчёта {path}: сначала запустите "
            "pytest --cov=app --cov-report=json (make coverage)"
        )
    try:
        report: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CoverageReportError(f"отчёт {path} повреждён: {exc.msg}") from exc
    return report


def package_coverage(report: Mapping[str, Any], packages: Sequence[str]) -> dict[str, float]:
    """Процент покрытых строк по каждому пакету (файлы, путь которых начинается с пакета)."""
    totals = {package: [0, 0] for package in packages}
    for filename, data in report.get("files", {}).items():
        normalized = filename.replace("\\", "/")
        summary = data["summary"]
        for package in packages:
            if normalized.startswith(package.rstrip("/") + "/"):
                totals[package][0] += int(summary["covered_lines"])
                totals[package][1] += int(summary["num_statements"])
    return {
        package: (PERCENT * covered / statements if statements else float(PERCENT))
        for package, (covered, statements) in totals.items()
    }


def below_threshold(coverage: Mapping[str, float], threshold: float) -> list[str]:
    """Пакеты с покрытием ниже порога."""
    return [package for package, percent in coverage.items() if percent < threshold]


def main(argv: Sequence[str] | None = None) -> int:
    """Печатает покрытие пакетов; код 1, если хотя бы один ниже порога."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("report", nargs="?", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--threshold", type=float, default=None)
    args = parser.parse_args(argv)

    threshold = args.threshold if args.threshold is not None else load_threshold()
    try:
        report = load_report(args.report)
    except CoverageReportError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    coverage = package_coverage(report, DEFAULT_PACKAGES)
    for package, percent in coverage.items():
        print(f"{package}: {percent:.1f}%")
    failed = below_threshold(coverage, threshold)
    if failed:
        print(f"Ниже порога {threshold:.0f}%: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"Все пакеты покрыты не меньше чем на {threshold:.0f}%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

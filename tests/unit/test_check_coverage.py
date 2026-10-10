import json
from pathlib import Path
from typing import Any

import pytest

from scripts.check_coverage import (
    DEFAULT_PACKAGES,
    CoverageReportError,
    below_threshold,
    load_report,
    load_threshold,
    main,
    package_coverage,
)


def _file(covered: int, statements: int) -> dict[str, Any]:
    return {"summary": {"covered_lines": covered, "num_statements": statements}}


REPORT = {
    "files": {
        "app/collectors/rss.py": _file(80, 100),
        "app/collectors/web.py": _file(20, 100),
        "app/pipeline/dedup.py": _file(95, 100),
        "app/publisher/limits.py": _file(69, 100),
        "app/main.py": _file(0, 50),
        "app/pipeline_extra.py": _file(0, 100),
    }
}


def _write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_package_coverage() -> None:
    coverage = package_coverage(REPORT, DEFAULT_PACKAGES)
    assert coverage == {"app/collectors": 50.0, "app/pipeline": 95.0, "app/publisher": 69.0}


def test_below_threshold() -> None:
    coverage = package_coverage(REPORT, DEFAULT_PACKAGES)
    assert below_threshold(coverage, 70) == ["app/collectors", "app/publisher"]
    assert below_threshold(coverage, 50) == []


def test_package_without_files_counts_as_covered() -> None:
    assert package_coverage({"files": {}}, ["app/empty"]) == {"app/empty": 100.0}


def test_threshold_from_pyproject() -> None:
    assert load_threshold() == 70


def test_missing_report(tmp_path: Path) -> None:
    with pytest.raises(CoverageReportError, match="make coverage"):
        load_report(tmp_path / "nope.json")


def test_broken_report(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(CoverageReportError, match="повреждён"):
        load_report(path)


def test_main_fails_with_list_of_packages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(_write(tmp_path, REPORT)), "--threshold", "70"]) == 1
    captured = capsys.readouterr()
    assert "app/collectors: 50.0%" in captured.out
    assert "Ниже порога 70%: app/collectors, app/publisher" in captured.err


def test_main_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = {"files": {f"{p}/m.py": _file(90, 100) for p in DEFAULT_PACKAGES}}
    assert main([str(_write(tmp_path, good)), "--threshold", "70"]) == 0
    assert "не меньше чем на 70%" in capsys.readouterr().out


def test_main_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "missing.json")]) == 1
    assert "нет отчёта" in capsys.readouterr().err

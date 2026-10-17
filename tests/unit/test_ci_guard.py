from pathlib import Path

import pytest

from app.config import get_test_database_url, require_test_db
from tests.integration import conftest as integration_conftest

pytest_plugins = ["pytester"]

INNER_INI = """
[pytest]
asyncio_mode = auto
markers =
    integration: тесты с реальной БД или сетью
"""


def _prepare(pytester: pytest.Pytester) -> None:
    """Вложенный прогон с собственным ini и проверяемой частью conftest."""
    pytester.makeini(INNER_INI)
    pytester.makeconftest(_conftest_source())


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Переменные БД не подхватываются из окружения разработчика и .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REQUIRE_TEST_DB", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_strict_mode_enabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_TEST_DB", value)
    assert require_test_db() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "нет"])
def test_strict_mode_disabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_TEST_DB", value)
    assert require_test_db() is False


def test_strict_mode_off_by_default() -> None:
    assert require_test_db() is False
    assert get_test_database_url() is None


def test_missing_url_skips_without_strict_mode(pytester: pytest.Pytester) -> None:
    """Локально без REQUIRE_TEST_DB поведение прежнее: skip с подсказкой."""
    _prepare(pytester)
    pytester.makepyfile(
        test_needs_db="""
        import pytest

        pytestmark = pytest.mark.integration

        def test_uses_db(test_database_url: str) -> None:
            raise AssertionError("не должно выполняться без БД")
        """
    )
    result = pytester.runpytest_subprocess("-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*TEST_DATABASE_URL не задан*"])


def test_missing_url_fails_in_strict_mode(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REQUIRE_TEST_DB", "1")
    _prepare(pytester)
    pytester.makepyfile(
        test_needs_db="""
        import pytest

        pytestmark = pytest.mark.integration

        def test_uses_db(test_database_url: str) -> None:
            raise AssertionError("не должно выполняться без БД")
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*REQUIRE_TEST_DB=1*"])
    assert result.ret != 0


def test_skipped_test_breaks_strict_run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Любой пропуск в строгом режиме — ошибка прогона, а не зелёный CI."""
    monkeypatch.setenv("REQUIRE_TEST_DB", "1")
    _prepare(pytester)
    pytester.makepyfile(
        test_skipping="""
        import pytest

        def test_skipped() -> None:
            pytest.skip("по какой-то причине")
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(skipped=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*пропущено тестов*"])


def test_skipped_test_is_fine_without_strict_mode(pytester: pytest.Pytester) -> None:
    _prepare(pytester)
    pytester.makepyfile(
        test_skipping="""
        import pytest

        def test_skipped() -> None:
            pytest.skip("по какой-то причине")
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(skipped=1)
    assert result.ret == pytest.ExitCode.OK


def _conftest_source() -> str:
    """Проверяемая часть tests/integration/conftest.py для вложенного прогона."""
    source = Path(integration_conftest.__file__).read_text(encoding="utf-8")
    head = source.split("@pytest.fixture\ndef alembic_config")[0]
    tail = source.split("def pytest_sessionfinish")[1]
    return head + "def pytest_sessionfinish" + tail

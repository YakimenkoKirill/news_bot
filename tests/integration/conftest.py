from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.db.models  # noqa: F401  регистрирует модели в Base.metadata
from app.config import get_test_database_url, require_test_db
from app.db.base import Base, create_session_factory

ROOT = Path(__file__).resolve().parents[2]
NO_DB_MESSAGE = "TEST_DATABASE_URL не задан: поднимите БД (make db-up) и заполните .env"
STRICT_HINT = (
    "REQUIRE_TEST_DB=1: интеграционные тесты обязательны. "
    "Проверьте TEST_DATABASE_URL и доступность БД."
)


@pytest.fixture(scope="session")
def strict_db() -> bool:
    """Включён ли строгий режим: без БД тесты падают, а не пропускаются."""
    return require_test_db()


@pytest.fixture(scope="session")
def test_database_url(strict_db: bool) -> str:
    """Адрес тестовой БД; без него skip, а в строгом режиме — ошибка."""
    url = get_test_database_url()
    if url is None:
        if strict_db:
            pytest.fail(f"{NO_DB_MESSAGE}. {STRICT_HINT}", pytrace=False)
        pytest.skip(NO_DB_MESSAGE)
    return url


@pytest.fixture
def alembic_config(test_database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    return config


@pytest.fixture
def migrated_db(alembic_config: Config, test_database_url: str) -> str:
    """Схема в актуальном состоянии; повторный upgrade ничего не делает."""
    command.upgrade(alembic_config, "head")
    return test_database_url


@pytest.fixture
async def db_engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    """Движок к тестовой БД с очищенными таблицами приложения."""
    engine = create_async_engine(migrated_db, poolclass=pool.NullPool)
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(db_engine)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as opened:
        yield opened


@pytest.fixture
def pipeline_now() -> datetime:
    """Подменённое «сейчас» для стадий пайплайна (совпадает по дате с now() в БД)."""
    return datetime.now(UTC).replace(microsecond=0)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """В строгом режиме пропущенный интеграционный тест ломает прогон.

    Без этой проверки опечатка в workflow превратила бы все интеграционные
    тесты в «skipped», а job остался бы зелёным.

    Args:
        session: Сессия pytest.
        exitstatus: Код выхода, который pytest собирается вернуть.
    """
    if not require_test_db():
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len(reporter.stats.get("skipped", [])) if reporter is not None else 0
    if not skipped:
        return
    print(f"REQUIRE_TEST_DB=1: пропущено тестов — {skipped}, это ошибка")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED

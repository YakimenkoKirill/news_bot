from pathlib import Path

import pytest
from alembic.config import Config

from app.config import get_test_database_url

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = get_test_database_url()
    if url is None:
        pytest.skip("TEST_DATABASE_URL не задан: поднимите БД (make db-up) и заполните .env")
    return url


@pytest.fixture
def alembic_config(test_database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    return config

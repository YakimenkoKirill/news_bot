"""Асинхронный движок SQLAlchemy, фабрика сессий и декларативная база моделей."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс моделей с детерминированными именами ограничений и индексов."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(settings: Settings) -> AsyncEngine:
    """Создаёт асинхронный движок по настройкам.

    Args:
        settings: Настройки приложения; используются database_url и секция db.

    Returns:
        Движок с параметрами пула из конфигурации.
    """
    db = settings.db
    return create_async_engine(
        settings.database_url,
        pool_size=db.pool_size,
        max_overflow=db.max_overflow,
        pool_pre_ping=db.pool_pre_ping,
        echo=db.echo,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Создаёт фабрику сессий, объекты которой не истекают после commit."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Открывает сессию-транзакцию.

    При успешном выходе из блока делает commit. При исключении в блоке или в самом
    commit делает rollback и пробрасывает исходное исключение.

    Args:
        factory: Фабрика сессий.

    Yields:
        Открытая сессия.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise

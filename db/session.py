"""
Async SQLAlchemy engine and session factory.

Usage:
    async with get_session() as session:
        result = await session.execute(...)

Call init_db() once at startup (bot/main.py) before any DB operation.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db.models import (
    Base,
    User,
    Application,
    ContextVersion,
    ApplicationAnswer,
    Template,
    Outcome,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_engine: Optional[AsyncEngine] = None
_AsyncSessionLocal: Optional[async_sessionmaker] = None


def init_db(database_url: str | None = None) -> None:
    """
    Initialise the engine and session factory.
    Must be called once before get_session() is used.
    """
    global _engine, _AsyncSessionLocal

    raw_url = database_url or os.getenv("DATABASE_URL")
    if not raw_url:
        abs_db_path = (_PROJECT_ROOT / "flam.db").resolve()
        raw_url = f"sqlite+aiosqlite:///{abs_db_path}"
    elif raw_url.startswith("sqlite+aiosqlite:///./"):
        # Resolve relative SQLite path to absolute project root path
        rel_path = raw_url.replace("sqlite+aiosqlite:///./", "")
        abs_path = (_PROJECT_ROOT / rel_path).resolve()
        raw_url = f"sqlite+aiosqlite:///{abs_path}"

    _engine = create_async_engine(raw_url, echo=False)
    _AsyncSessionLocal = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def create_tables() -> None:
    """Create all tables and perform lightweight schema migrations if columns were added."""
    assert _engine is not None, "Call init_db() first."
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
        # SQLite lightweight column migration helper
        def _sync_sqlite_columns(sync_conn):
            for table_name, table in Base.metadata.tables.items():
                try:
                    res = sync_conn.exec_driver_sql(f"PRAGMA table_info({table_name});").fetchall()
                    existing_cols = {row[1] for row in res}
                    for col in table.columns:
                        if col.name not in existing_cols:
                            col_type = col.type.compile(sync_conn.dialect)
                            sync_conn.exec_driver_sql(
                                f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type};"
                            )
                except Exception:
                    pass

        await conn.run_sync(_sync_sqlite_columns)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    assert _AsyncSessionLocal is not None, "Call init_db() first."
    async with _AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

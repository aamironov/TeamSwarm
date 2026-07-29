from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().database_url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from . import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if engine.url.get_backend_name() == "sqlite":
            await _upgrade_sqlite_schema(connection)


async def _upgrade_sqlite_schema(connection) -> None:
    queue_columns = {
        row[1] for row in (await connection.execute(text("PRAGMA table_info(task_queue)"))).all()
    }
    if "priority" not in queue_columns:
        await connection.execute(
            text("ALTER TABLE task_queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        )
    table_names = {
        row[0]
        for row in (
            await connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
        ).all()
    }
    for table in ("runs", "chat_messages"):
        if table not in table_names:
            continue
        result = await connection.execute(text(f"PRAGMA table_info({table})"))
        columns = {row[1] for row in result.all()}
        if "attachments" not in columns:
            await connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN attachments JSON NOT NULL DEFAULT '[]'")
            )
    run_result = await connection.execute(text("PRAGMA table_info(runs)"))
    run_columns = {row[1] for row in run_result.all()}
    for column, definition in (
        ("workflow", "TEXT NOT NULL DEFAULT 'standard'"),
        ("current_cycle", "INTEGER NOT NULL DEFAULT 1"),
        ("max_cycles", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if "runs" in table_names and column not in run_columns:
            await connection.execute(text(f"ALTER TABLE runs ADD COLUMN {column} {definition}"))
    task_result = await connection.execute(text("PRAGMA table_info(tasks)"))
    task_columns = {row[1] for row in task_result.all()}
    for column, definition in (
        ("agent_role", "TEXT NOT NULL DEFAULT 'general'"),
        ("model_override", "TEXT"),
        ("cycle", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if "tasks" in table_names and column not in task_columns:
            await connection.execute(text(f"ALTER TABLE tasks ADD COLUMN {column} {definition}"))


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session

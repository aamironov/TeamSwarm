import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from services.api.app.db import _upgrade_sqlite_schema


@pytest.mark.asyncio
async def test_sqlite_upgrade_adds_queue_priority_to_existing_database(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE task_queue (id TEXT PRIMARY KEY)"))
        await _upgrade_sqlite_schema(connection)
        columns = (await connection.execute(text("PRAGMA table_info(task_queue)"))).all()

    await engine.dispose()

    assert "priority" in {column[1] for column in columns}


@pytest.mark.asyncio
async def test_sqlite_upgrade_adds_phase_four_run_and_task_columns(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-phase-four.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE task_queue (id TEXT PRIMARY KEY)"))
        await connection.execute(text("CREATE TABLE runs (id TEXT PRIMARY KEY)"))
        await connection.execute(text("CREATE TABLE tasks (id TEXT PRIMARY KEY)"))
        await _upgrade_sqlite_schema(connection)
        run_columns = (await connection.execute(text("PRAGMA table_info(runs)"))).all()
        task_columns = (await connection.execute(text("PRAGMA table_info(tasks)"))).all()

    await engine.dispose()

    run_names = {column[1] for column in run_columns}
    task_names = {column[1] for column in task_columns}
    assert {"workspace_root", "write_tools_approved"} <= run_names
    assert "workflow_revision" in task_names

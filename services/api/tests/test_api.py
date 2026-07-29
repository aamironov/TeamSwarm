from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.api.app import chat, main, runtime
from services.api.app.chat import ChatService
from services.api.app.db import Base
from services.api.app.providers import MockProvider
from services.api.app.runtime import RunService


@pytest_asyncio.fixture
async def api_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AsyncIterator[httpx.AsyncClient]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api-test.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(runtime, "SessionLocal", session_factory)
    monkeypatch.setattr(chat, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "service", RunService(provider=MockProvider()))
    monkeypatch.setattr(main, "chat_service", ChatService(provider=MockProvider()))
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_api_exposes_completed_run_usage_trace_queue_and_replay(api_client) -> None:
    created = await api_client.post(
        "/runs",
        json={
            "objective": "Investigate the release.",
            "subtasks": [
                {"objective": "Collect evidence."},
                {"objective": "Summarize evidence.", "depends_on": ["task-1"]},
            ],
        },
    )

    assert created.status_code == 202
    run_id = created.json()["id"]
    await main.service._jobs[run_id]

    run = await api_client.get(f"/runs/{run_id}")
    usage = await api_client.get(f"/runs/{run_id}/usage")
    trace = await api_client.get(f"/runs/{run_id}/trace")
    queue = await api_client.get(f"/runs/{run_id}/queue")
    replay = await api_client.get(f"/runs/{run_id}/replay")

    assert run.json()["status"] == "succeeded"
    assert usage.json()["consumed_tokens"] > 0
    assert {event["kind"] for event in trace.json()["events"]} >= {
        "run_created",
        "model_routed",
        "run_succeeded",
    }
    assert {item["status"] for item in queue.json()["items"]} == {"completed"}
    assert len(replay.json()["artifacts"]) == 2

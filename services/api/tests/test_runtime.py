import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.api.app import runtime
from services.api.app.config import Settings, get_settings
from services.api.app.db import Base
from services.api.app.models import QueueItemRecord, TaskCapabilityRecord, TaskRecord
from services.api.app.providers import MockProvider, ModelProvider, ProviderResult
from services.api.app.runtime import RunService
from services.api.app.schemas import Budget, RunCreate, SubtaskInput
from services.api.app.version_control import VersionSnapshot


class UnchangedVersionControl:
    def snapshot(self, *, run_id: str, cycle: int) -> VersionSnapshot:
        return VersionSnapshot("unchanged", "test-head", "Test workspace is unchanged.")


@pytest_asyncio.fixture
async def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AsyncIterator[None]:
    """Give each runtime test an isolated SQLite database and session factory."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'teamswarm-test.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(runtime, "SessionLocal", session_factory)
    monkeypatch.setattr(runtime, "LocalGitVersionControl", UnchangedVersionControl)
    yield
    await engine.dispose()


class CapturingProvider(ModelProvider):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        self.prompts.append(prompt)
        return ProviderResult(
            text=f"result-{len(self.prompts)}",
            input_tokens=10,
            output_tokens=5,
            source="internally_metered",
        )


class EmptyThenSuccessProvider(ModelProvider):
    def __init__(self) -> None:
        self.models: list[str] = []
        self.prompts: list[str] = []

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        self.models.append(model)
        self.prompts.append(prompt)
        if len(self.models) == 1:
            return ProviderResult("", 5, 0, "internally_metered")
        return ProviderResult("repaired result", 10, 5, "internally_metered")


class ConcurrentProvider(ModelProvider):
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return ProviderResult("parallel result", 10, 5, "internally_metered")


class DeliveryCycleProvider(ModelProvider):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self.evaluations = 0

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        role = next(
            role
            for role in ("criteria", "discovery", "coding", "testing", "evaluator")
            if f"Role: {role}" in prompt
        )
        self.calls.append((role, model))
        self.prompts.append(prompt)
        if role == "evaluator":
            self.evaluations += 1
            text = "GOAL_ACHIEVED: yes" if self.evaluations == 2 else "GOAL_ACHIEVED: no"
        else:
            text = f"{role} completed"
        return ProviderResult(text, 10, 5, "internally_metered")


class RecordingVersionControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def snapshot(self, *, run_id: str, cycle: int) -> VersionSnapshot:
        self.calls.append((run_id, cycle))
        return VersionSnapshot("created", "abc123", "Saved stable delivery cycle 2 to local Git.")


async def _wait_for_run(service: RunService, run_id: str) -> None:
    await service._jobs[run_id]


@pytest.mark.asyncio
async def test_runtime_completes_direct_run_and_records_usage(isolated_runtime) -> None:
    service = RunService(provider=MockProvider())

    created = await service.create(RunCreate(objective="Summarize the incident."))
    await _wait_for_run(service, created.id)

    completed = await service.get(created.id)
    usage = await service.usage(created.id)
    trace = await service.trace(created.id)

    assert completed.status == "succeeded"
    assert completed.final_output
    assert usage.consumed_tokens > 0
    assert usage.events[0].source == "internally_metered"
    assert any(event.kind == "run_succeeded" for event in trace.events)


@pytest.mark.asyncio
async def test_run_adds_attached_text_file_to_every_task_prompt(isolated_runtime) -> None:
    provider = CapturingProvider()
    service = RunService(provider=provider)
    created = await service.create(
        RunCreate(
            objective="Review the supplied implementation.",
            attachments=[{"filename": "example.py", "content": "print('attached')"}],
        )
    )
    await _wait_for_run(service, created.id)

    assert "--- Attached file: example.py ---" in provider.prompts[0]
    assert "print('attached')" in provider.prompts[0]


@pytest.mark.asyncio
async def test_delivery_cycle_repeats_until_evaluator_accepts_and_uses_role_models(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        criteria_model="criteria-model",
        discovery_model="discovery-model",
        coding_model="coding-model",
        testing_model="testing-model",
        evaluator_model="evaluator-model",
    )
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    provider = DeliveryCycleProvider()
    service = RunService(provider=provider)
    created = await service.create(
        RunCreate(
            objective="Build the requested feature.",
            workflow="delivery_cycle",
            max_cycles=2,
            attachments=[{"filename": "requirements.md", "content": "Shared requirements"}],
        )
    )
    await _wait_for_run(service, created.id)

    completed = await service.get(created.id)
    trace = await service.trace(created.id)

    assert completed.status == "succeeded"
    assert len(completed.tasks) == 10
    assert (
        provider.calls
        == [
            ("criteria", "criteria-model"),
            ("discovery", "discovery-model"),
            ("coding", "coding-model"),
            ("testing", "testing-model"),
            ("evaluator", "evaluator-model"),
        ]
        * 2
    )
    assert any(event.kind == "goal_not_achieved" for event in trace.events)
    assert any(event.kind == "goal_accepted" for event in trace.events)
    assert all(
        "Shared user prompt:\nBuild the requested feature." in prompt for prompt in provider.prompts
    )
    assert all("Shared requirements" in prompt for prompt in provider.prompts)
    assert all("Cycle 1 handoff summary" in prompt for prompt in provider.prompts[5:])


@pytest.mark.asyncio
async def test_delivery_cycle_saves_an_accepted_cycle_to_local_git(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        criteria_model="criteria-model",
        discovery_model="discovery-model",
        coding_model="coding-model",
        testing_model="testing-model",
        evaluator_model="evaluator-model",
    )
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    versions = RecordingVersionControl()
    service = RunService(provider=DeliveryCycleProvider(), version_control=versions)
    created = await service.create(
        RunCreate(objective="Build the requested feature.", workflow="delivery_cycle", max_cycles=2)
    )
    await _wait_for_run(service, created.id)

    trace = await service.trace(created.id)

    assert versions.calls == [(created.id, 2)]
    assert trace.versions == [
        {
            "cycle": 2,
            "status": "created",
            "revision": "abc123",
            "message": "Saved stable delivery cycle 2 to local Git.",
        }
    ]
    assert any(event.kind == "stable_version_saved" for event in trace.events)


@pytest.mark.asyncio
async def test_independent_tasks_execute_concurrently(isolated_runtime) -> None:
    provider = ConcurrentProvider()
    service = RunService(provider=provider)
    created = await service.create(
        RunCreate(
            objective="Perform independent checks.",
            subtasks=[
                SubtaskInput(objective="Check first input."),
                SubtaskInput(objective="Check second input."),
            ],
        )
    )
    await _wait_for_run(service, created.id)

    assert provider.max_active == 2


@pytest.mark.asyncio
async def test_three_task_workflow_completes_with_parallel_and_dependent_stages(
    isolated_runtime,
) -> None:
    provider = CapturingProvider()
    service = RunService(provider=provider)
    created = await service.create(
        RunCreate(
            objective="Investigate and summarize the release.",
            subtasks=[
                SubtaskInput(objective="Collect customer evidence."),
                SubtaskInput(objective="Collect operational evidence."),
                SubtaskInput(objective="Synthesize the evidence.", depends_on=["task-1", "task-2"]),
            ],
        )
    )
    await _wait_for_run(service, created.id)

    completed = await service.get(created.id)

    assert completed.status == "succeeded"
    assert len(completed.tasks) == 3
    assert "Authorized upstream result" in provider.prompts[2]


@pytest.mark.asyncio
async def test_cancellation_stops_queued_work_and_records_the_decision(isolated_runtime) -> None:
    service = RunService(provider=CapturingProvider(), inline_worker_enabled=False)
    created = await service.create(RunCreate(objective="Do not start this work."))

    cancelled = await service.cancel(created.id)
    queue = await service.queue(created.id)
    trace = await service.trace(created.id)

    assert cancelled.status == "cancelled"
    assert {item.status for item in queue.items} == {"cancelled"}
    assert any(event.kind == "run_cancelled" for event in trace.events)


@pytest.mark.asyncio
async def test_cost_budget_denies_call_before_provider_invocation(isolated_runtime) -> None:
    provider = CapturingProvider()
    service = RunService(provider=provider)
    created = await service.create(
        RunCreate(
            objective="Summarize the incident.",
            budget=Budget(token_limit=12_000, cost_limit_usd=0),
        )
    )
    await _wait_for_run(service, created.id)

    completed = await service.get(created.id)
    trace = await service.trace(created.id)

    assert completed.status == "failed"
    assert provider.prompts == []
    assert any(event.kind == "cost_quota_denied" for event in trace.events)


@pytest.mark.asyncio
async def test_consolidation_deduplicates_and_reports_conflicting_equivalent_results(
    isolated_runtime,
) -> None:
    service = RunService(provider=CapturingProvider())
    created = await service.create(
        RunCreate(
            objective="Compare two independent answers.",
            subtasks=[
                SubtaskInput(objective="Determine the recommendation."),
                SubtaskInput(objective="Determine the recommendation."),
            ],
        )
    )
    await _wait_for_run(service, created.id)

    completed = await service.get(created.id)
    trace = await service.trace(created.id)

    assert "Conflicts requiring review" in (completed.final_output or "")
    assert any(event.kind == "consolidation_conflict" for event in trace.events)


@pytest.mark.asyncio
async def test_consolidator_receives_only_explicitly_granted_result_handles(
    isolated_runtime,
) -> None:
    service = RunService(provider=CapturingProvider())
    created = await service.create(
        RunCreate(
            objective="Collect two findings.",
            subtasks=[
                SubtaskInput(objective="Collect first finding."),
                SubtaskInput(objective="Collect second finding."),
            ],
        )
    )
    await _wait_for_run(service, created.id)

    trace = await service.trace(created.id)
    consolidator_grants = [
        grant for grant in trace.cache_grants if grant["recipient"] == f"consolidator:{created.id}"
    ]

    assert len(consolidator_grants) == 2
    assert all(len(grant["entry_ids"]) == 1 for grant in consolidator_grants)
    assert any(event.kind == "consolidation_grant" for event in trace.events)


@pytest.mark.asyncio
async def test_trace_records_context_manifest_and_prompt_quantification(isolated_runtime) -> None:
    service = RunService(provider=CapturingProvider())
    created = await service.create(
        RunCreate(
            objective="Review the proposal.",
            prompt_variants=["cost", "quality"],
            attachments=[{"filename": "brief.md", "content": "Shared evidence"}],
        )
    )
    await _wait_for_run(service, created.id)

    trace = await service.trace(created.id)
    manifests = [event for event in trace.events if event.kind == "context_manifest"]

    assert any(event.kind == "prompt_quantified" for event in trace.events)
    assert len(manifests) == 2
    assert all(event.metadata["selected_count"] == 1 for event in manifests)
    assert all(event.metadata["estimated_tokens"] > 0 for event in manifests)
    assert len(trace.context_manifests) == 2
    assert all(
        manifest["selected"][0]["source"] == "attached_file" for manifest in trace.context_manifests
    )


@pytest.mark.asyncio
async def test_dependent_task_receives_only_granted_upstream_context(isolated_runtime) -> None:
    provider = CapturingProvider()
    service = RunService(provider=provider)
    created = await service.create(
        RunCreate(
            objective="Investigate and summarize.",
            subtasks=[
                SubtaskInput(objective="Collect evidence."),
                SubtaskInput(objective="Analyze evidence.", depends_on=["task-1"]),
            ],
        )
    )
    await _wait_for_run(service, created.id)

    completed = await service.get(created.id)
    first_task, dependent_task = completed.tasks
    assert "Authorized upstream result" in provider.prompts[1]
    assert "result-1" in provider.prompts[1]

    async with runtime.SessionLocal() as session:
        first_context = await service._authorized_context(
            session, created.id, await session.get(TaskRecord, first_task.id)
        )
        dependent_context = await service._authorized_context(
            session, created.id, await session.get(TaskRecord, dependent_task.id)
        )

    assert first_context == ""
    assert "result-1" in dependent_context


@pytest.mark.asyncio
async def test_token_budget_denies_call_before_provider_invocation(isolated_runtime) -> None:
    provider = CapturingProvider()
    service = RunService(provider=provider)
    created = await service.create(
        RunCreate(
            objective="Summarize the incident.",
            budget=Budget(token_limit=100, cost_limit_usd=2.0),
        )
    )
    await _wait_for_run(service, created.id)

    completed = await service.get(created.id)
    usage = await service.usage(created.id)
    trace = await service.trace(created.id)

    assert completed.status == "failed"
    assert provider.prompts == []
    assert usage.consumed_tokens == 0
    assert any(event.kind == "quota_denied" for event in trace.events)


@pytest.mark.asyncio
async def test_empty_result_is_repaired_once_with_fallback_model(isolated_runtime) -> None:
    provider = EmptyThenSuccessProvider()
    service = RunService(provider=provider)
    settings = get_settings()
    created = await service.create(RunCreate(objective="Summarize the incident."))
    await _wait_for_run(service, created.id)

    completed = await service.get(created.id)
    usage = await service.usage(created.id)
    trace = await service.trace(created.id)

    assert completed.status == "succeeded"
    assert provider.models == [settings.model_for("fast"), settings.fallback_for()]
    assert "repair attempt" in provider.prompts[1]
    assert usage.events[0].model == settings.fallback_for()
    assert usage.events[0].profile == "strong"
    assert {event.kind for event in trace.events} >= {"task_attempt_failed", "task_recovered"}


@pytest.mark.asyncio
async def test_usage_last_24_hours_groups_usage_by_model(isolated_runtime) -> None:
    service = RunService(provider=MockProvider())
    created = await service.create(RunCreate(objective="Summarize the incident."))
    await _wait_for_run(service, created.id)

    window = await service.usage_last_24_hours()

    assert window.total_tokens > 0
    assert window.input_tokens > 0
    assert window.by_model[0].total_tokens == window.total_tokens


@pytest.mark.asyncio
async def test_external_worker_claims_tasks_in_dependency_order(isolated_runtime) -> None:
    provider = CapturingProvider()
    service = RunService(provider=provider, inline_worker_enabled=False)
    created = await service.create(
        RunCreate(
            objective="Collect and analyze.",
            subtasks=[
                SubtaskInput(objective="Collect evidence."),
                SubtaskInput(objective="Analyze evidence.", depends_on=["task-1"]),
            ],
        )
    )

    initial_queue = await service.queue(created.id)
    assert created.id not in service._jobs
    assert {item.status for item in initial_queue.items} == {"queued"}

    assert await service.work_once("worker-a", created.id)
    after_first_claim = await service.queue(created.id)
    assert [item.status for item in after_first_claim.items].count("completed") == 1
    assert [item.status for item in after_first_claim.items].count("queued") == 1
    assert len(provider.prompts) == 1

    assert await service.work_once("worker-b", created.id)
    completed = await service.get(created.id)
    completed_queue = await service.queue(created.id)

    assert completed.status == "succeeded"
    assert len(provider.prompts) == 2
    assert all(item.status == "completed" for item in completed_queue.items)
    assert all(item.attempts == 1 for item in completed_queue.items)
    assert "Authorized upstream result" in provider.prompts[1]
    assert "result-1" in provider.prompts[1]


@pytest.mark.asyncio
async def test_idempotency_key_returns_the_original_run(isolated_runtime) -> None:
    service = RunService(provider=MockProvider(), inline_worker_enabled=False)
    request = RunCreate(objective="Summarize the incident.", idempotency_key="incident-42")

    first = await service.create(request)
    second = await service.create(request)

    assert first.id == second.id
    assert len((await service.queue(first.id)).items) == 1


@pytest.mark.asyncio
async def test_higher_priority_ready_task_is_claimed_first(isolated_runtime) -> None:
    provider = CapturingProvider()
    service = RunService(provider=provider, inline_worker_enabled=False)
    created = await service.create(
        RunCreate(
            objective="Prioritized work.",
            subtasks=[
                SubtaskInput(objective="Low priority work.", priority=-10),
                SubtaskInput(objective="High priority work.", priority=10),
            ],
        )
    )

    assert await service.work_once("priority-worker", created.id)

    assert "High priority work." in provider.prompts[0]


@pytest.mark.asyncio
async def test_capability_denial_blocks_model_invocation(isolated_runtime) -> None:
    provider = CapturingProvider()
    service = RunService(provider=provider, inline_worker_enabled=False)
    created = await service.create(RunCreate(objective="Summarize the incident."))
    task_id = created.tasks[0].id
    async with runtime.SessionLocal() as session:
        capability = await session.scalar(
            select(TaskCapabilityRecord).where(TaskCapabilityRecord.task_id == task_id)
        )
        assert capability is not None
        capability.permissions = []
        await session.commit()

    assert await service.work_once("restricted-worker", created.id)
    completed = await service.get(created.id)
    trace = await service.trace(created.id)

    assert completed.status == "failed"
    assert provider.prompts == []
    assert any(event.kind == "capability_denied" for event in trace.events)


@pytest.mark.asyncio
async def test_replay_returns_validated_artifact_and_evaluation(isolated_runtime) -> None:
    service = RunService(provider=MockProvider())
    created = await service.create(RunCreate(objective="Summarize the incident."))
    await _wait_for_run(service, created.id)

    replay = await service.replay(created.id)

    assert len(replay.artifacts) == 1
    assert replay.artifacts[0].validation_state == "validated"
    assert replay.artifacts[0].provenance["profile"] == "fast"
    assert replay.evaluations[0]["status"] == "passed"


@pytest.mark.asyncio
async def test_worker_heartbeat_renews_a_claimed_task_lease(isolated_runtime) -> None:
    service = RunService(provider=MockProvider(), inline_worker_enabled=False)
    created = await service.create(RunCreate(objective="Summarize the incident."))
    claim = await service._claim_next("heartbeat-worker", created.id)
    assert claim is not None

    async with runtime.SessionLocal() as session:
        item = await session.get(QueueItemRecord, claim.queue_item_id)
        assert item is not None
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        item.lease_expires_at = expired_at
        await session.commit()

    await service._heartbeat_claim(claim, "heartbeat-worker")
    queue = await service.queue(created.id)

    renewed_at = queue.items[0].lease_expires_at
    assert renewed_at is not None
    assert renewed_at.replace(tzinfo=UTC) > expired_at


@pytest.mark.asyncio
async def test_global_concurrency_limit_prevents_another_claim(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RunService(provider=MockProvider(), inline_worker_enabled=False)
    created = await service.create(RunCreate(objective="Summarize the incident."))
    async with runtime.SessionLocal() as session:
        task = await session.get(TaskRecord, created.tasks[0].id)
        assert task is not None
        task.status = "running"
        await session.commit()
    monkeypatch.setattr(
        runtime,
        "get_settings",
        lambda: SimpleNamespace(max_concurrent_tasks=1),
    )

    assert await service._claim_next("limited-worker", created.id) is None

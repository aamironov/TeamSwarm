import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .attachments import render_attachments
from .config import get_settings
from .context import ContextItem, ContextOptimizer
from .db import SessionLocal
from .models import (
    ApprovalRecord,
    ArtifactRecord,
    CacheEntryRecord,
    CacheGrantRecord,
    ContextManifestRecord,
    EvaluationRecord,
    QueueItemRecord,
    RunIdempotencyRecord,
    RunRecord,
    TaskCapabilityRecord,
    TaskContractRecord,
    TaskRecord,
    ToolCallRecord,
    TraceEventRecord,
    UsageRecord,
    VersionRecord,
    WorkflowRevisionRecord,
)
from .planner import (
    PlannedTask,
    build_delivery_cycle_plan,
    build_human_approval_plan,
    build_refinement_plan,
    build_review_repair_plan,
)
from .providers import ModelProvider, ProviderResult, get_provider
from .routing import route_task
from .schemas import (
    ApprovalInput,
    ArtifactView,
    Budget,
    ModelTokenUsage,
    QueueItemView,
    ReplayView,
    RunCreate,
    RunQueueView,
    RunView,
    TaskView,
    TokenUsageWindow,
    TraceEvent,
    TraceView,
    UsageSummary,
    UsageView,
)
from .skills import SkillCatalog
from .task_generator import get_task_generator
from .tools import ToolGateway, ToolRequest, ToolResult, parse_tool_request
from .version_control import LocalGitVersionControl


@dataclass(frozen=True)
class QueueClaim:
    queue_item_id: str
    run_id: str
    task_id: str


class RunService:
    def __init__(
        self,
        provider: ModelProvider | None = None,
        inline_worker_enabled: bool | None = None,
        version_control: LocalGitVersionControl | None = None,
    ) -> None:
        self.provider = provider or get_provider()
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._claim_lock = asyncio.Lock()
        self._finalize_lock = asyncio.Lock()
        self.context_optimizer = ContextOptimizer()
        self.version_control = version_control or LocalGitVersionControl()
        self.inline_worker_enabled = (
            get_settings().inline_worker_enabled
            if inline_worker_enabled is None
            else inline_worker_enabled
        )

    async def create(self, request: RunCreate) -> RunView:
        settings = get_settings()
        budget = request.budget or Budget(
            token_limit=settings.default_token_budget,
            cost_limit_usd=settings.default_cost_budget_usd,
        )
        selected_skills = SkillCatalog(settings.skills_paths()).select(request.skills)
        if request.planner_backend != "deterministic" and (
            request.subtasks
            or request.prompt_variants
            or request.workflow != "standard"
        ):
            raise ValueError(
                "Agent planning accepts one standard objective without explicit "
                "subtasks or variants."
            )
        planning = await get_task_generator(request.planner_backend, self.provider).generate(
            request, selected_skills, settings
        )
        plan = planning.tasks
        workspace_root = settings.resolve_workspace(request.workspace_root)
        _, attachment_metadata = render_attachments(request.attachments)
        existing_run_id: str | None = None
        async with SessionLocal() as session:
            if request.idempotency_key:
                existing = await session.get(RunIdempotencyRecord, request.idempotency_key)
                if existing:
                    existing_run_id = existing.run_id
            if existing_run_id:
                return await self.get(existing_run_id)
            run = RunRecord(
                objective=request.objective,
                token_limit=budget.token_limit,
                cost_limit_usd=budget.cost_limit_usd,
                trace=[],
                attachments=[
                    {"filename": item.filename, "content": item.content, **metadata}
                    for item, metadata in zip(request.attachments, attachment_metadata, strict=True)
                ],
                skills=[skill.snapshot() for skill in selected_skills],
                planner_backend=planning.backend,
                workflow=request.workflow,
                current_cycle=1,
                max_cycles=request.max_cycles
                if request.workflow in {"delivery_cycle", "review_repair", "refinement"}
                else 1,
                workspace_root=str(workspace_root),
                write_tools_approved=request.approve_write_tools,
            )
            session.add(run)
            await session.flush()
            if request.idempotency_key:
                session.add(RunIdempotencyRecord(key=request.idempotency_key, run_id=run.id))
            for task in plan:
                session.add(
                    TaskRecord(
                        id=task.id,
                        run_id=run.id,
                        objective=task.objective,
                        dependencies=task.dependencies,
                        model_profile=task.model_profile,
                        agent_role=task.agent_role,
                        model_override=get_settings().model_for_role(
                            task.agent_role, task.model_profile
                        )
                        if task.agent_role != "general"
                        else None,
                        cycle=1,
                        workflow_revision=1,
                    )
                )
                session.add(
                    QueueItemRecord(
                        run_id=run.id,
                        task_id=task.id,
                        status="queued",
                        priority=task.priority,
                    )
                )
                session.add(
                    TaskContractRecord(
                        task_id=task.id,
                        expected_output=task.expected_output,
                        acceptance_checks=task.acceptance_checks,
                    )
                )
                session.add(
                    TaskCapabilityRecord(
                        task_id=task.id,
                        permissions=self._permissions_for(run, task.agent_role),
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
            session.add(
                WorkflowRevisionRecord(
                    run_id=run.id,
                    revision=1,
                    workflow_type=request.workflow,
                    iteration=1,
                    status="active",
                    parent_revision=None,
                    task_ids=[task.id for task in plan],
                    reason=self._initial_revision_reason(request),
                )
            )
            if request.prompt_variants:
                await self._event(
                    session,
                    run,
                    "prompt_quantified",
                    "Parent prompt fanned out into partitioned child prompts",
                    metadata={
                        "variant_count": len(request.prompt_variants),
                        "parent_prompt_hash": hashlib.sha256(
                            request.objective.encode()
                        ).hexdigest(),
                    },
                )
            await self._event(
                session,
                run,
                "task_plan_generated",
                f"Task graph generated by {planning.backend}",
                metadata={
                    "backend": planning.backend,
                    "task_count": len(plan),
                    "skill_count": len(selected_skills),
                    "model": planning.model or "",
                },
            )
            if planning.model:
                session.add(
                    UsageRecord(
                        run_id=run.id,
                        task_id=None,
                        model=planning.model,
                        profile="strong",
                        input_tokens=planning.input_tokens,
                        output_tokens=planning.output_tokens,
                        total_tokens=planning.input_tokens + planning.output_tokens,
                        estimated_cost_usd=self._estimate_cost(
                            "strong", planning.input_tokens, planning.output_tokens
                        ),
                        source=planning.usage_source,
                    )
                )
            await self._event(session, run, "run_created", "Run accepted and task graph created")
            await session.commit()
            await session.refresh(run)
            run_id = run.id
        if self.inline_worker_enabled:
            self._jobs[run_id] = asyncio.create_task(
                self.execute(run_id), name=f"teamswarm:{run_id}"
            )
        return await self.get(run_id)

    async def execute(self, run_id: str) -> None:
        """Development worker pool that pulls ready tasks for one run."""
        worker_count = min(get_settings().max_concurrent_tasks, 3)

        async def run_worker(index: int) -> None:
            worker_id = f"inline-{index}-{uuid4()}"
            while True:
                if await self.work_once(worker_id, run_id):
                    continue
                current = await self.get(run_id)
                if current.status in {"cancelled", "waiting_approval", "succeeded", "failed"}:
                    return
                await asyncio.sleep(0.01)

        try:
            await asyncio.gather(*(run_worker(index) for index in range(worker_count)))
        finally:
            self._jobs.pop(run_id, None)

    async def work_once(self, worker_id: str, run_id: str | None = None) -> bool:
        """Claim one dependency-ready task, execute it, and settle its queue lease."""
        async with self._claim_lock:
            claim = await self._claim_next(worker_id, run_id)
        if claim is None:
            if run_id:
                async with self._finalize_lock:
                    await self._finalize_run(run_id)
            return False
        try:
            execution = asyncio.create_task(self._execute_task(claim.run_id, claim.task_id))
            heartbeat_seconds = max(1, get_settings().task_lease_seconds // 3)
            while not execution.done():
                await asyncio.wait({execution}, timeout=heartbeat_seconds)
                if not execution.done():
                    await self._heartbeat_claim(claim, worker_id)
            await execution
        finally:
            await self._settle_claim(claim, worker_id)
            async with self._finalize_lock:
                await self._finalize_run(claim.run_id)
        return True

    async def _heartbeat_claim(self, claim: QueueClaim, worker_id: str) -> None:
        async with SessionLocal() as session:
            item = await session.get(QueueItemRecord, claim.queue_item_id)
            if item is None or item.status != "claimed" or item.worker_id != worker_id:
                return
            item.lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=get_settings().task_lease_seconds
            )
            await session.commit()

    async def _claim_next(self, worker_id: str, run_id: str | None) -> QueueClaim | None:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            expired = list(
                (
                    await session.scalars(
                        select(QueueItemRecord).where(
                            QueueItemRecord.status == "claimed",
                            QueueItemRecord.lease_expires_at < now,
                        )
                    )
                ).all()
            )
            for item in expired:
                task = await session.get(TaskRecord, item.task_id)
                if task and task.status == "running":
                    task.status = "pending"
                item.status = "queued"
                item.worker_id = None
                item.lease_expires_at = None
            if expired:
                await session.commit()

            query = select(QueueItemRecord).where(QueueItemRecord.status == "queued")
            if run_id:
                query = query.where(QueueItemRecord.run_id == run_id)
            active_tasks = list(
                (
                    await session.scalars(select(TaskRecord).where(TaskRecord.status == "running"))
                ).all()
            )
            if len(active_tasks) >= get_settings().max_concurrent_tasks:
                return None
            candidates = list(
                (
                    await session.scalars(
                        query.order_by(
                            QueueItemRecord.priority.desc(), QueueItemRecord.id
                        ).with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for item in candidates:
                run = await session.get(RunRecord, item.run_id)
                task = await session.get(TaskRecord, item.task_id)
                if (
                    run is None
                    or task is None
                    or run.status
                    in {
                        "cancelled",
                        "failed",
                        "succeeded",
                    }
                ):
                    item.status = "cancelled"
                    continue
                dependency_tasks = (
                    list(
                        (
                            await session.scalars(
                                select(TaskRecord).where(TaskRecord.id.in_(task.dependencies))
                            )
                        ).all()
                    )
                    if task.dependencies
                    else []
                )
                if any(dependency.status != "succeeded" for dependency in dependency_tasks):
                    continue
                if (
                    sum(active.model_profile == task.model_profile for active in active_tasks)
                    >= get_settings().max_concurrent_tasks_per_profile
                ):
                    continue
                if run.status == "pending":
                    run.status = "running"
                    await self._event(
                        session, run, "run_started", "A worker claimed the first task"
                    )
                task.status = "running"
                item.status = "claimed"
                item.attempts += 1
                item.worker_id = worker_id
                item.claimed_at = now
                item.lease_expires_at = now + timedelta(seconds=get_settings().task_lease_seconds)
                await self._event(
                    session,
                    run,
                    "task_claimed",
                    f"Worker {worker_id} claimed a dependency-ready task",
                    task.id,
                    {"worker_id": worker_id, "attempt": item.attempts},
                )
                await session.commit()
                return QueueClaim(item.id, item.run_id, item.task_id)
            await session.commit()
        return None

    async def _settle_claim(self, claim: QueueClaim, worker_id: str) -> None:
        async with SessionLocal() as session:
            item = await session.get(QueueItemRecord, claim.queue_item_id)
            task = await session.get(TaskRecord, claim.task_id)
            run = await session.get(RunRecord, claim.run_id)
            if item is None or task is None or run is None:
                return
            item.completed_at = datetime.now(UTC)
            item.lease_expires_at = None
            if task.status == "succeeded":
                item.status = "completed"
                message = f"Worker {worker_id} completed the task"
            elif task.status == "cancelled" or run.status == "cancelled":
                item.status = "cancelled"
                message = f"Worker {worker_id} stopped after cancellation"
            else:
                item.status = "failed"
                message = f"Worker {worker_id} failed the task"
            await self._event(session, run, "task_settled", message, task.id)
            await session.commit()

    async def _finalize_run(self, run_id: str) -> None:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None or run.status in {
                "cancelled",
                "waiting_approval",
                "succeeded",
                "failed",
            }:
                return
            tasks = list(
                (await session.scalars(select(TaskRecord).where(TaskRecord.run_id == run_id))).all()
            )
            if any(task.status == "failed" for task in tasks):
                run.status = "failed"
                run.error = "A mandatory task failed."
                revision = await self._workflow_revision(session, run.id, run.current_cycle)
                if revision and revision.status == "active":
                    revision.status = "failed"
                queue_items = list(
                    (
                        await session.scalars(
                            select(QueueItemRecord).where(
                                QueueItemRecord.run_id == run_id,
                                QueueItemRecord.status == "queued",
                            )
                        )
                    ).all()
                )
                for item in queue_items:
                    item.status = "cancelled"
                await self._rollback_workspace_changes(session, run)
                await self._event(session, run, "run_failed", run.error)
                await session.commit()
            elif run.workflow == "delivery_cycle":
                cycle_tasks = [task for task in tasks if task.cycle == run.current_cycle]
                if cycle_tasks and all(task.status == "succeeded" for task in cycle_tasks):
                    revision = await self._workflow_revision(session, run.id, run.current_cycle)
                    evaluator = next(
                        (task for task in cycle_tasks if task.agent_role == "evaluator"), None
                    )
                    if evaluator is None:
                        run.status = "failed"
                        run.error = "Delivery cycle has no evaluator task."
                        await self._event(session, run, "run_failed", run.error)
                    elif self._goal_achieved(evaluator.output):
                        if revision:
                            revision.status = "accepted"
                        await self._event(
                            session,
                            run,
                            "goal_accepted",
                            "Evaluator accepted the goal for the current delivery cycle.",
                            evaluator.id,
                            {"cycle": run.current_cycle},
                        )
                        await self._save_stable_version(session, run)
                        await self._consolidate(session, run, tasks)
                    elif run.current_cycle < run.max_cycles:
                        if revision:
                            revision.status = "superseded"
                        next_cycle = run.current_cycle + 1
                        await self._append_delivery_cycle(session, run, next_cycle)
                        run.current_cycle = next_cycle
                        await self._event(
                            session,
                            run,
                            "goal_not_achieved",
                            "Evaluator requested another bounded delivery cycle.",
                            evaluator.id,
                            {"completed_cycle": next_cycle - 1, "next_cycle": next_cycle},
                        )
                    else:
                        if revision:
                            revision.status = "failed"
                        run.status = "failed"
                        run.error = "Goal was not achieved before the delivery-cycle limit."
                        await self._event(
                            session, run, "goal_not_achieved", run.error, evaluator.id
                        )
                    if run.status == "failed":
                        await self._rollback_workspace_changes(session, run)
                    await session.commit()
            elif run.workflow == "review_repair":
                revision_tasks = [
                    task for task in tasks if task.workflow_revision == run.current_cycle
                ]
                if revision_tasks and all(task.status == "succeeded" for task in revision_tasks):
                    revision = await self._workflow_revision(session, run.id, run.current_cycle)
                    reviewer = next(
                        (task for task in revision_tasks if task.agent_role == "reviewer"), None
                    )
                    if reviewer is None:
                        run.status = "failed"
                        run.error = "Review/repair revision has no reviewer task."
                        if revision:
                            revision.status = "failed"
                        await self._event(session, run, "run_failed", run.error)
                    else:
                        repair_required = self._repair_decision(reviewer.output)
                        if repair_required is None:
                            run.status = "failed"
                            run.error = "Reviewer omitted the required REPAIR_REQUIRED marker."
                            if revision:
                                revision.status = "failed"
                            await self._event(
                                session, run, "review_invalid", run.error, reviewer.id
                            )
                        elif not repair_required:
                            if revision:
                                revision.status = "accepted"
                            await self._event(
                                session,
                                run,
                                "review_accepted",
                                "Reviewer accepted the workspace outcome.",
                                reviewer.id,
                                {"revision": run.current_cycle},
                            )
                            await self._save_stable_version(session, run)
                            await self._consolidate(session, run, tasks)
                        elif run.current_cycle < run.max_cycles:
                            if revision:
                                revision.status = "superseded"
                            next_revision = run.current_cycle + 1
                            await self._append_review_repair_revision(
                                session, run, revision_tasks, next_revision
                            )
                            run.current_cycle = next_revision
                            await self._event(
                                session,
                                run,
                                "repair_requested",
                                "Reviewer created one bounded targeted repair revision.",
                                reviewer.id,
                                {
                                    "completed_revision": next_revision - 1,
                                    "next_revision": next_revision,
                                },
                            )
                        else:
                            run.status = "failed"
                            run.error = (
                                "Repair remained necessary at the workflow revision limit."
                            )
                            if revision:
                                revision.status = "failed"
                            await self._event(
                                session, run, "repair_limit_reached", run.error, reviewer.id
                            )
                    if run.status == "failed":
                        await self._rollback_workspace_changes(session, run)
                    await session.commit()
            elif run.workflow == "refinement":
                revision_tasks = [
                    task for task in tasks if task.workflow_revision == run.current_cycle
                ]
                if revision_tasks and all(task.status == "succeeded" for task in revision_tasks):
                    revision = await self._workflow_revision(session, run.id, run.current_cycle)
                    evaluator = next(
                        (
                            task
                            for task in revision_tasks
                            if task.agent_role == "refinement_evaluator"
                        ),
                        None,
                    )
                    decision = self._refinement_decision(evaluator.output if evaluator else None)
                    if evaluator is None or decision is None:
                        run.status = "failed"
                        run.error = "Refinement evaluator omitted its required completion marker."
                        if revision:
                            revision.status = "failed"
                        await self._event(
                            session,
                            run,
                            "refinement_invalid",
                            run.error,
                            evaluator.id if evaluator else None,
                        )
                    elif decision:
                        if revision:
                            revision.status = "accepted"
                        await self._event(
                            session,
                            run,
                            "refinement_accepted",
                            "Refinement evaluator accepted the measurable outcome.",
                            evaluator.id,
                            {"revision": run.current_cycle},
                        )
                        await self._save_stable_version(session, run)
                        await self._consolidate(session, run, tasks)
                    elif run.current_cycle < run.max_cycles:
                        if revision:
                            revision.status = "superseded"
                        next_revision = run.current_cycle + 1
                        await self._append_refinement_revision(
                            session, run, revision_tasks, next_revision
                        )
                        run.current_cycle = next_revision
                        await self._event(
                            session,
                            run,
                            "refinement_continued",
                            "Evaluator requested another bounded refinement revision.",
                            evaluator.id,
                            {"next_revision": next_revision},
                        )
                    else:
                        run.status = "failed"
                        run.error = "Refinement did not converge before its revision limit."
                        if revision:
                            revision.status = "failed"
                        await self._event(
                            session, run, "refinement_limit_reached", run.error, evaluator.id
                        )
                    if run.status == "failed":
                        await self._rollback_workspace_changes(session, run)
                    await session.commit()
            elif run.workflow == "human_approval":
                revision_tasks = [
                    task for task in tasks if task.workflow_revision == run.current_cycle
                ]
                if revision_tasks and all(task.status == "succeeded" for task in revision_tasks):
                    revision = await self._workflow_revision(session, run.id, run.current_cycle)
                    if run.current_cycle == 1:
                        run.status = "waiting_approval"
                        if revision:
                            revision.status = "waiting_approval"
                        await self._event(
                            session,
                            run,
                            "approval_required",
                            "Execution is paused pending an explicit human decision.",
                            revision_tasks[0].id,
                            {"revision": 1},
                        )
                    else:
                        if revision:
                            revision.status = "accepted"
                        await self._event(
                            session,
                            run,
                            "approved_execution_verified",
                            "The approved execution revision completed.",
                            metadata={"revision": run.current_cycle},
                        )
                        await self._save_stable_version(session, run)
                        await self._consolidate(session, run, tasks)
                    await session.commit()
            elif tasks and all(task.status == "succeeded" for task in tasks):
                revision = await self._workflow_revision(session, run.id, 1)
                if revision:
                    revision.status = "accepted"
                await self._consolidate(session, run, tasks)
                await session.commit()

    async def _save_stable_version(self, session: AsyncSession, run: RunRecord) -> None:
        """Persist the local-Git outcome without allowing it to fail the run."""
        snapshot = await asyncio.to_thread(
            self.version_control.snapshot,
            run_id=run.id,
            cycle=run.current_cycle,
            workspace_root=run.workspace_root,
        )
        session.add(
            VersionRecord(
                run_id=run.id,
                cycle=run.current_cycle,
                status=snapshot.status,
                revision=snapshot.revision,
                message=snapshot.message,
            )
        )
        accepted_calls = list(
            (
                await session.scalars(
                    select(ToolCallRecord).where(
                        ToolCallRecord.run_id == run.id,
                        ToolCallRecord.rollback_status == "pending",
                    )
                )
            ).all()
        )
        for call in accepted_calls:
            call.rollback_status = "committed"
        event_kind = {
            "created": "stable_version_saved",
            "unchanged": "stable_version_unchanged",
            "unavailable": "stable_version_unavailable",
            "failed": "stable_version_failed",
        }[snapshot.status]
        await self._event(
            session,
            run,
            event_kind,
            snapshot.message,
            metadata={
                "cycle": run.current_cycle,
                "revision": snapshot.revision or "",
            },
        )

    async def _rollback_workspace_changes(
        self, session: AsyncSession, run: RunRecord
    ) -> None:
        calls = list(
            (
                await session.scalars(
                    select(ToolCallRecord)
                    .where(
                        ToolCallRecord.run_id == run.id,
                        ToolCallRecord.side_effect.is_(True),
                        ToolCallRecord.status == "succeeded",
                        ToolCallRecord.rollback_status == "pending",
                    )
                    .order_by(ToolCallRecord.created_at.desc(), ToolCallRecord.id.desc())
                )
            ).all()
        )
        if not calls:
            return
        gateway = ToolGateway(
            Path(run.workspace_root or "."),
            timeout_seconds=get_settings().tool_timeout_seconds,
            max_output_chars=get_settings().max_tool_output_chars,
        )
        for call in calls:
            result = await asyncio.to_thread(gateway.rollback, call.rollback_json)
            call.rollback_status = result.status
            await self._event(
                session,
                run,
                f"tool_rollback_{result.status}",
                result.output,
                call.task_id,
                {
                    "tool": call.tool_name,
                    "original_result_hash": call.result_hash,
                },
            )

    async def _append_delivery_cycle(
        self, session: AsyncSession, run: RunRecord, cycle: int
    ) -> None:
        request = RunCreate(
            objective=run.objective,
            workflow="delivery_cycle",
            max_cycles=run.max_cycles,
        )
        prior_tasks = list(
            (
                await session.scalars(
                    select(TaskRecord).where(
                        TaskRecord.run_id == run.id,
                        TaskRecord.cycle == cycle - 1,
                        TaskRecord.status == "succeeded",
                    )
                )
            ).all()
        )
        carryover_entry = None
        evaluator = next((task for task in prior_tasks if task.agent_role == "evaluator"), None)
        if evaluator:
            compacted_handoffs = "\n\n".join(
                self._compact_handoff(task.agent_role, task.output or "") for task in prior_tasks
            )
            carryover_entry = CacheEntryRecord(
                run_id=run.id,
                source_task_id=evaluator.id,
                output=f"Cycle {cycle - 1} handoff summary:\n{compacted_handoffs}",
                state="validated",
            )
            session.add(carryover_entry)
            await session.flush()
            session.add(
                ArtifactRecord(
                    run_id=run.id,
                    source_task_id=evaluator.id,
                    cache_entry_id=carryover_entry.id,
                    kind="cycle_handoff_summary",
                    content=carryover_entry.output,
                    content_hash=hashlib.sha256(carryover_entry.output.encode()).hexdigest(),
                    provenance={"cycle": cycle - 1, "compaction": "deterministic-v1"},
                )
            )
        plan = build_delivery_cycle_plan(request, cycle)
        for task in plan:
            session.add(
                TaskRecord(
                    id=task.id,
                    run_id=run.id,
                    objective=task.objective,
                    dependencies=task.dependencies,
                    model_profile=task.model_profile,
                    agent_role=task.agent_role,
                    model_override=get_settings().model_for_role(
                        task.agent_role, task.model_profile
                    ),
                    cycle=cycle,
                    workflow_revision=cycle,
                )
            )
            if carryover_entry:
                session.add(
                    CacheGrantRecord(
                        run_id=run.id,
                        recipient=f"task:{task.id}",
                        entry_ids=[carryover_entry.id],
                        purpose="previous delivery-cycle compacted handoff",
                    )
                )
            session.add(
                QueueItemRecord(
                    run_id=run.id,
                    task_id=task.id,
                    status="queued",
                    priority=task.priority,
                )
            )
            session.add(
                TaskContractRecord(
                    task_id=task.id,
                    expected_output=task.expected_output,
                    acceptance_checks=task.acceptance_checks,
                )
            )
            session.add(
                TaskCapabilityRecord(
                    task_id=task.id,
                    permissions=self._permissions_for(run, task.agent_role),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        session.add(
            WorkflowRevisionRecord(
                run_id=run.id,
                revision=cycle,
                workflow_type=run.workflow,
                iteration=cycle,
                status="active",
                parent_revision=cycle - 1,
                task_ids=[task.id for task in plan],
                reason="Evaluator requested another bounded delivery cycle.",
            )
        )

    async def _append_review_repair_revision(
        self,
        session: AsyncSession,
        run: RunRecord,
        prior_tasks: list[TaskRecord],
        revision: int,
    ) -> None:
        reviewer = next((task for task in prior_tasks if task.agent_role == "reviewer"), None)
        if reviewer is None:
            raise ValueError("Cannot create a repair revision without reviewer evidence.")
        summary = "\n\n".join(
            self._compact_handoff(task.agent_role, task.output or "") for task in prior_tasks
        )
        carryover = CacheEntryRecord(
            run_id=run.id,
            source_task_id=reviewer.id,
            output=f"Review/repair revision {revision - 1} evidence:\n{summary}",
            state="validated",
        )
        session.add(carryover)
        await session.flush()
        session.add(
            ArtifactRecord(
                run_id=run.id,
                source_task_id=reviewer.id,
                cache_entry_id=carryover.id,
                kind="workflow_revision_handoff",
                content=carryover.output,
                content_hash=hashlib.sha256(carryover.output.encode()).hexdigest(),
                provenance={"revision": revision - 1, "compaction": "deterministic-v1"},
            )
        )
        request = RunCreate(
            objective=run.objective,
            workflow="review_repair",
            max_cycles=run.max_cycles,
            approve_write_tools=run.write_tools_approved,
            workspace_root=run.workspace_root,
        )
        plan = build_review_repair_plan(request, revision)
        for task in plan:
            session.add(
                TaskRecord(
                    id=task.id,
                    run_id=run.id,
                    objective=task.objective,
                    dependencies=task.dependencies,
                    model_profile=task.model_profile,
                    agent_role=task.agent_role,
                    model_override=get_settings().model_for_role(
                        task.agent_role, task.model_profile
                    ),
                    cycle=revision,
                    workflow_revision=revision,
                )
            )
            session.add(
                CacheGrantRecord(
                    run_id=run.id,
                    recipient=f"task:{task.id}",
                    entry_ids=[carryover.id],
                    purpose="prior immutable workflow revision evidence",
                )
            )
            session.add(
                QueueItemRecord(
                    run_id=run.id,
                    task_id=task.id,
                    status="queued",
                    priority=task.priority,
                )
            )
            session.add(
                TaskContractRecord(
                    task_id=task.id,
                    expected_output=task.expected_output,
                    acceptance_checks=task.acceptance_checks,
                )
            )
            session.add(
                TaskCapabilityRecord(
                    task_id=task.id,
                    permissions=self._permissions_for(run, task.agent_role),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        session.add(
            WorkflowRevisionRecord(
                run_id=run.id,
                revision=revision,
                workflow_type=run.workflow,
                iteration=revision,
                status="active",
                parent_revision=revision - 1,
                task_ids=[task.id for task in plan],
                reason="Reviewer identified actionable defects requiring targeted repair.",
            )
        )

    async def _append_refinement_revision(
        self,
        session: AsyncSession,
        run: RunRecord,
        prior_tasks: list[TaskRecord],
        revision: int,
    ) -> None:
        evaluator = next(
            (task for task in prior_tasks if task.agent_role == "refinement_evaluator"),
            prior_tasks[-1],
        )
        carryover = await self._revision_handoff(
            session,
            run,
            evaluator,
            prior_tasks,
            revision,
            "refinement_revision_handoff",
        )
        request = RunCreate(
            objective=run.objective,
            workflow="refinement",
            max_cycles=run.max_cycles,
            approve_write_tools=run.write_tools_approved,
            workspace_root=run.workspace_root,
        )
        plan = build_refinement_plan(request, revision)
        await self._append_planned_revision(
            session,
            run,
            plan,
            revision,
            carryover,
            "Evaluator requested another bounded refinement pass.",
        )

    async def _append_human_execution_revision(
        self,
        session: AsyncSession,
        run: RunRecord,
        proposal_tasks: list[TaskRecord],
    ) -> None:
        source = proposal_tasks[-1]
        carryover = await self._revision_handoff(
            session,
            run,
            source,
            proposal_tasks,
            2,
            "approved_proposal",
        )
        request = RunCreate(
            objective=run.objective,
            workflow="human_approval",
            approve_write_tools=run.write_tools_approved,
            workspace_root=run.workspace_root,
        )
        plan = build_human_approval_plan(request, revision=2)
        await self._append_planned_revision(
            session,
            run,
            plan,
            2,
            carryover,
            "Human approved the bounded execution proposal.",
        )

    async def _revision_handoff(
        self,
        session: AsyncSession,
        run: RunRecord,
        source: TaskRecord,
        tasks: list[TaskRecord],
        next_revision: int,
        kind: str,
    ) -> CacheEntryRecord:
        summary = "\n\n".join(
            self._compact_handoff(task.agent_role, task.output or "") for task in tasks
        )
        carryover = CacheEntryRecord(
            run_id=run.id,
            source_task_id=source.id,
            output=f"Workflow revision {next_revision - 1} evidence:\n{summary}",
            state="validated",
        )
        session.add(carryover)
        await session.flush()
        session.add(
            ArtifactRecord(
                run_id=run.id,
                source_task_id=source.id,
                cache_entry_id=carryover.id,
                kind=kind,
                content=carryover.output,
                content_hash=hashlib.sha256(carryover.output.encode()).hexdigest(),
                provenance={
                    "revision": next_revision - 1,
                    "compaction": "deterministic-v1",
                },
            )
        )
        return carryover

    async def _append_planned_revision(
        self,
        session: AsyncSession,
        run: RunRecord,
        plan: list[PlannedTask],
        revision: int,
        carryover: CacheEntryRecord,
        reason: str,
    ) -> None:
        for task in plan:
            session.add(
                TaskRecord(
                    id=task.id,
                    run_id=run.id,
                    objective=task.objective,
                    dependencies=task.dependencies,
                    model_profile=task.model_profile,
                    agent_role=task.agent_role,
                    model_override=get_settings().model_for_role(
                        task.agent_role, task.model_profile
                    ),
                    cycle=revision,
                    workflow_revision=revision,
                )
            )
            session.add(
                CacheGrantRecord(
                    run_id=run.id,
                    recipient=f"task:{task.id}",
                    entry_ids=[carryover.id],
                    purpose="prior immutable workflow revision evidence",
                )
            )
            session.add(
                QueueItemRecord(
                    run_id=run.id,
                    task_id=task.id,
                    status="queued",
                    priority=task.priority,
                )
            )
            session.add(
                TaskContractRecord(
                    task_id=task.id,
                    expected_output=task.expected_output,
                    acceptance_checks=task.acceptance_checks,
                )
            )
            session.add(
                TaskCapabilityRecord(
                    task_id=task.id,
                    permissions=self._permissions_for(run, task.agent_role),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        session.add(
            WorkflowRevisionRecord(
                run_id=run.id,
                revision=revision,
                workflow_type=run.workflow,
                iteration=revision,
                status="active",
                parent_revision=revision - 1,
                task_ids=[task.id for task in plan],
                reason=reason,
            )
        )

    @staticmethod
    async def _workflow_revision(
        session: AsyncSession, run_id: str, revision: int
    ) -> WorkflowRevisionRecord | None:
        return await session.scalar(
            select(WorkflowRevisionRecord).where(
                WorkflowRevisionRecord.run_id == run_id,
                WorkflowRevisionRecord.revision == revision,
            )
        )

    @staticmethod
    def _compact_handoff(role: str, output: str) -> str:
        limit = 1_200
        excerpt = output[:limit]
        suffix = ""
        if len(output) > limit:
            suffix = "\n[Compacted; see source artifact for full output.]"
        return f"Role {role} handoff:\n{excerpt}{suffix}"

    @staticmethod
    def _permissions_for(run: RunRecord, role: str) -> list[str]:
        declared = {
            tool
            for skill in run.skills
            for tool in skill.get("allowed_tools", [])
            if tool in ToolGateway.registry
        }
        read_tools = {
            "workspace.list_files",
            "workspace.read_file",
            "workspace.run_command",
            "git.status",
        }
        write_tools = {"workspace.write_file", "workspace.replace_text"}
        role_tools = {
            "criteria": read_tools,
            "discovery": read_tools,
            "testing": read_tools,
            "reviewer": read_tools,
            "coding": read_tools | write_tools,
            "builder": read_tools | write_tools,
            "repairer": read_tools | write_tools,
            "refiner": read_tools | write_tools,
            "refinement_evaluator": read_tools,
            "branch": read_tools | write_tools,
            "map": read_tools,
            "reducer": read_tools,
            "approval_proposer": read_tools,
            "approved_executor": read_tools | write_tools,
            "approval_verifier": read_tools,
            "evaluator": read_tools,
        }.get(role, read_tools | write_tools)
        return [
            "model:invoke",
            "read:granted-artifacts",
            *(f"tool:{tool}" for tool in sorted(declared & role_tools)),
        ]

    @staticmethod
    def _initial_revision_reason(request: RunCreate) -> str:
        if request.workflow == "conditional" and request.conditional:
            selected = "true" if request.conditional.condition else "false"
            skipped = "false" if request.conditional.condition else "true"
            return (
                f"Deterministic condition selected the {selected} branch; "
                f"the {skipped} branch was skipped."
            )
        if request.workflow == "map_reduce" and request.map_reduce:
            return f"Mapped {len(request.map_reduce.items)} declared partitions before reduction."
        if request.workflow == "human_approval":
            return "Created a read-only proposal revision before protected execution."
        return "Initial validated task graph."

    @staticmethod
    def _goal_achieved(output: str | None) -> bool:
        return bool(output and "goal_achieved: yes" in output.casefold())

    @staticmethod
    def _repair_decision(output: str | None) -> bool | None:
        normalized = output.casefold() if output else ""
        if "repair_required: yes" in normalized:
            return True
        if "repair_required: no" in normalized:
            return False
        return None

    @staticmethod
    def _refinement_decision(output: str | None) -> bool | None:
        normalized = output.casefold() if output else ""
        if "refinement_complete: yes" in normalized:
            return True
        if "refinement_complete: no" in normalized:
            return False
        return None

    async def _execute_task(self, run_id: str, task_id: str) -> None:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            task = await session.get(TaskRecord, task_id)
            if run is None or task is None or run.status == "cancelled":
                return
            capability = await session.scalar(
                select(TaskCapabilityRecord).where(TaskCapabilityRecord.task_id == task.id)
            )
            contract = await session.scalar(
                select(TaskContractRecord).where(TaskContractRecord.task_id == task.id)
            )
            if (
                capability is None
                or contract is None
                or "model:invoke" not in capability.permissions
                or self._is_expired(capability.expires_at)
            ):
                task.status = "failed"
                task.error = "Task capability does not permit model invocation."
                await self._event(session, run, "capability_denied", task.error, task.id)
                await session.commit()
                return
            permissions = list(capability.permissions)
            task.status = "running"
            await self._event(session, run, "task_started", "Task started", task.id)
            context_items = await self._context_items(session, run, task)
            context_budget = min(
                get_settings().context_token_budget,
                max(100, run.token_limit // 2),
            )
            optimized_context = self.context_optimizer.optimize(context_items, context_budget)
            context = optimized_context.text
            session.add(
                ContextManifestRecord(
                    run_id=run.id,
                    task_id=task.id,
                    optimizer_version=optimized_context.manifest.optimizer_version,
                    budget_tokens=optimized_context.manifest.budget_tokens,
                    estimated_tokens=optimized_context.manifest.estimated_tokens,
                    selected=optimized_context.manifest.selected,
                    omitted=optimized_context.manifest.omitted,
                )
            )
            await self._event(
                session,
                run,
                "context_manifest",
                "Task context package selected and token-budgeted",
                task.id,
                {
                    "selected_count": len(optimized_context.manifest.selected),
                    "omitted_count": len(optimized_context.manifest.omitted),
                    "estimated_tokens": optimized_context.manifest.estimated_tokens,
                    "budget_tokens": optimized_context.manifest.budget_tokens,
                    "context_hash": hashlib.sha256(context.encode()).hexdigest(),
                },
            )
            route = route_task(get_settings(), task.model_profile, len(context))
            model = task.model_override or route.model
            await self._event(
                session,
                run,
                "model_routed",
                route.reason,
                task.id,
                {
                    "model": model,
                    "agent_role": task.agent_role,
                    "profile": task.model_profile,
                    "provider": route.provider,
                    "location": route.location,
                    "fallback_model": route.fallback_model,
                    "estimated_context_tokens": route.estimated_context_tokens,
                },
            )
            projected_tokens = max(200, (len(task.objective) + len(context)) // 4 + 500)
            consumed = await self._consumed_tokens(session, run_id)
            if consumed + projected_tokens > run.token_limit:
                task.status = "failed"
                task.error = "Token budget would be exceeded before model invocation."
                await self._event(session, run, "quota_denied", task.error, task.id)
                await session.commit()
                return
            projected_cost = self._estimate_cost(
                task.model_profile, max(0, projected_tokens - 500), 500
            )
            consumed_cost = await self._consumed_cost(session, run_id)
            if consumed_cost + projected_cost > run.cost_limit_usd:
                task.status = "failed"
                task.error = "Cost budget would be exceeded before model invocation."
                await self._event(session, run, "cost_quota_denied", task.error, task.id)
                await session.commit()
                return
            await self._event(
                session, run, "usage_reserved", f"Reserved {projected_tokens} tokens", task.id
            )
            await session.commit()

        settings = get_settings()
        gateway = ToolGateway(
            Path(run.workspace_root or "."),
            timeout_seconds=settings.tool_timeout_seconds,
            max_output_chars=settings.max_tool_output_chars,
        )
        prompt = self._render_prompt(
            task.objective,
            context,
            contract.expected_output,
            gateway.prompt_catalog(permissions),
        )
        await self._record_attempt_event(
            run_id,
            task_id,
            "prompt_rendered",
            "Versioned task prompt rendered from the authorized context package",
            {
                "prompt_version": contract.prompt_version,
                "rendered_prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                "context_hash": hashlib.sha256(context.encode()).hexdigest(),
            },
        )
        attempts = [(model, task.model_profile)]
        if settings.max_task_attempts > 1:
            attempts.append((route.fallback_model, "strong"))
        attempts = attempts[: settings.max_task_attempts]
        result = None
        selected_model = model
        selected_profile = task.model_profile
        last_error = "Unknown provider failure."
        for attempt_number, (attempt_model, attempt_profile) in enumerate(attempts, start=1):
            attempt_prompt = prompt
            if attempt_number > 1:
                attempt_prompt = (
                    f"{prompt}\n\nThis is a repair attempt. Return a non-empty, complete answer."
                )
            try:
                candidate = await self._generate_with_tools(
                    run_id=run_id,
                    task_id=task_id,
                    prompt=attempt_prompt,
                    model=attempt_model,
                    gateway=gateway,
                    permissions=permissions,
                    write_approved=run.write_tools_approved,
                    max_calls=settings.max_tool_calls_per_task,
                    provider_timeout=settings.task_timeout_seconds,
                    profile=attempt_profile,
                )
                self._validate_provider_result(candidate)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # Normalize errors into a bounded recovery path.
                last_error = str(error) or type(error).__name__
                await self._record_attempt_event(
                    run_id,
                    task_id,
                    "task_attempt_failed",
                    f"Attempt {attempt_number} with {attempt_model} failed: {last_error}",
                    {"attempt": attempt_number, "model": attempt_model},
                )
                continue
            result = candidate
            selected_model = attempt_model
            selected_profile = attempt_profile
            if attempt_number > 1:
                await self._record_attempt_event(
                    run_id,
                    task_id,
                    "task_recovered",
                    f"Attempt {attempt_number} succeeded with {attempt_model}",
                    {"attempt": attempt_number, "model": attempt_model},
                )
            break

        if result is None:
            async with SessionLocal() as session:
                run = await session.get(RunRecord, run_id)
                task = await session.get(TaskRecord, task_id)
                if run and task:
                    task.status = "failed"
                    task.error = f"Task failed after {len(attempts)} attempt(s): {last_error}"
                    await self._event(session, run, "task_failed", task.error, task.id)
                    await session.commit()
            return

        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            task = await session.get(TaskRecord, task_id)
            if run is None or task is None:
                return
            task.output = result.text
            task.status = "succeeded"
            usage = UsageRecord(
                run_id=run_id,
                task_id=task.id,
                model=selected_model,
                profile=selected_profile,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.input_tokens + result.output_tokens,
                estimated_cost_usd=self._estimate_cost(
                    selected_profile, result.input_tokens, result.output_tokens
                ),
                source=result.source,
            )
            session.add(usage)
            entry = CacheEntryRecord(
                run_id=run_id, source_task_id=task.id, output=result.text, state="validated"
            )
            session.add(entry)
            await session.flush()
            session.add(
                ArtifactRecord(
                    run_id=run_id,
                    source_task_id=task.id,
                    cache_entry_id=entry.id,
                    content=result.text,
                    content_hash=hashlib.sha256(result.text.encode()).hexdigest(),
                    provenance={
                        "model": selected_model,
                        "profile": selected_profile,
                        "usage_source": result.source,
                    },
                )
            )
            session.add(
                EvaluationRecord(
                    run_id=run_id,
                    task_id=task.id,
                    evaluator="deterministic_contract_v1",
                    status="passed",
                    score=1.0,
                    rationale="The result is non-empty and satisfied the MVP contract check.",
                )
            )
            downstream = list(
                (await session.scalars(select(TaskRecord).where(TaskRecord.run_id == run_id))).all()
            )
            for recipient in downstream:
                if task.id in recipient.dependencies:
                    session.add(
                        CacheGrantRecord(
                            run_id=run_id,
                            recipient=f"task:{recipient.id}",
                            entry_ids=[entry.id],
                            purpose="lead-planned dependency",
                        )
                    )
            session.add(
                CacheGrantRecord(
                    run_id=run_id,
                    recipient=f"consolidator:{run_id}",
                    entry_ids=[entry.id],
                    purpose="lead-planned consolidation",
                )
            )
            await self._event(
                session,
                run,
                "consolidation_grant",
                "Task result granted to the run consolidator",
                task.id,
            )
            await self._event(
                session, run, "usage_settled", f"Settled {usage.total_tokens} tokens", task.id
            )
            await self._event(session, run, "task_succeeded", "Task result cached", task.id)
            await session.commit()

    async def _authorized_context(
        self, session: AsyncSession, run_id: str, task: TaskRecord
    ) -> str:
        run = await session.get(RunRecord, run_id)
        if run is None:
            return ""
        return "\n\n".join(item.text for item in await self._context_items(session, run, task))

    async def _context_items(
        self, session: AsyncSession, run: RunRecord, task: TaskRecord
    ) -> list[ContextItem]:
        items = [
            ContextItem(
                id=f"attachment:{item['content_hash'][:12]}",
                source="attached_file",
                text=item["content"],
                priority=100,
                authority=100,
                required=True,
                metadata={"filename": item["filename"], "content_hash": item["content_hash"]},
            )
            for item in run.attachments
        ]
        items.extend(
            ContextItem(
                id=f"skill:{skill['name']}:{skill['content_hash'][:12]}",
                source="agent_skill",
                text=skill["instructions"],
                priority=95,
                authority=95,
                required=True,
                metadata={
                    "name": skill["name"],
                    "content_hash": skill["content_hash"],
                },
            )
            for skill in run.skills
        )
        capability = await session.scalar(
            select(TaskCapabilityRecord).where(TaskCapabilityRecord.task_id == task.id)
        )
        if (
            capability is None
            or "read:granted-artifacts" not in capability.permissions
            or self._is_expired(capability.expires_at)
        ):
            return items
        grants = list(
            (
                await session.scalars(
                    select(CacheGrantRecord).where(
                        CacheGrantRecord.run_id == run.id,
                        CacheGrantRecord.recipient == f"task:{task.id}",
                    )
                )
            ).all()
        )
        entry_ids = [entry_id for grant in grants for entry_id in grant.entry_ids]
        if not entry_ids:
            return items
        artifacts = list(
            (
                await session.scalars(
                    select(ArtifactRecord).where(ArtifactRecord.cache_entry_id.in_(entry_ids))
                )
            ).all()
        )
        if artifacts:
            for artifact in artifacts:
                source_task = await session.get(TaskRecord, artifact.source_task_id)
                role = source_task.agent_role if source_task else "unknown"
                items.append(
                    ContextItem(
                        id=f"artifact:{artifact.id}",
                        source="agent_handoff",
                        text=(
                            f"Authorized upstream result [{artifact.id[:8]}] from {role}: "
                            f"{artifact.content}"
                        ),
                        priority=90,
                        authority=90,
                        metadata={"source_task_id": artifact.source_task_id, "role": role},
                    )
                )
            return items
        entries = list(
            (
                await session.scalars(
                    select(CacheEntryRecord).where(CacheEntryRecord.id.in_(entry_ids))
                )
            ).all()
        )
        items.extend(
            ContextItem(
                id=f"cache:{entry.id}",
                source="agent_handoff",
                text=f"Authorized upstream result: {entry.output}",
                priority=90,
                authority=90,
            )
            for entry in entries
        )
        return items

    async def _consolidate(
        self, session: AsyncSession, run: RunRecord, tasks: list[TaskRecord]
    ) -> None:
        grants = list(
            (
                await session.scalars(
                    select(CacheGrantRecord).where(
                        CacheGrantRecord.run_id == run.id,
                        CacheGrantRecord.recipient == f"consolidator:{run.id}",
                    )
                )
            ).all()
        )
        entry_ids = [entry_id for grant in grants for entry_id in grant.entry_ids]
        entries = {
            entry.source_task_id: entry
            for entry in (
                await session.scalars(
                    select(CacheEntryRecord).where(CacheEntryRecord.id.in_(entry_ids))
                )
            ).all()
        }
        completed = [
            (task, entries[task.id].output)
            for task in tasks
            if task.id in entries and entries[task.id].output
        ]
        if not completed:
            run.status = "failed"
            run.error = "No task result was granted to the consolidator."
            await self._event(session, run, "run_failed", run.error)
            return

        unique: list[tuple[TaskRecord, str]] = []
        seen_outputs: set[str] = set()
        for task, output in completed:
            normalized = " ".join(output.split()).casefold()
            if normalized in seen_outputs:
                await self._event(
                    session,
                    run,
                    "consolidation_duplicate",
                    "Duplicate task output omitted from consolidated result",
                    task.id,
                )
                continue
            seen_outputs.add(normalized)
            unique.append((task, output))

        by_objective: dict[str, list[tuple[TaskRecord, str]]] = {}
        for task, output in unique:
            by_objective.setdefault(" ".join(task.objective.split()).casefold(), []).append(
                (task, output)
            )
        conflicts = [group for group in by_objective.values() if len(group) > 1]
        sections = [
            f"Task {index + 1} [{task.id[:8]}]: {output}"
            for index, (task, output) in enumerate(unique)
        ]
        if conflicts:
            conflict_lines = [
                f"- {group[0][0].objective}: "
                + ", ".join(f"task {task.id[:8]}" for task, _ in group)
                for group in conflicts
            ]
            sections.insert(0, "Conflicts requiring review:\n" + "\n".join(conflict_lines))
            await self._event(
                session,
                run,
                "consolidation_conflict",
                "Equivalent tasks produced different outputs; all outputs were retained.",
            )
        run.final_output = "\n\n".join(sections)
        run.status = "succeeded"
        accepted_calls = list(
            (
                await session.scalars(
                    select(ToolCallRecord).where(
                        ToolCallRecord.run_id == run.id,
                        ToolCallRecord.rollback_status == "pending",
                    )
                )
            ).all()
        )
        for call in accepted_calls:
            call.rollback_status = "committed"
        await self._event(
            session, run, "self_evaluation", "Output is non-empty; accepted by MVP validator"
        )
        await self._event(session, run, "run_succeeded", "Consolidated result accepted")

    async def _generate_with_tools(
        self,
        *,
        run_id: str,
        task_id: str,
        prompt: str,
        model: str,
        gateway: ToolGateway,
        permissions: list[str],
        write_approved: bool,
        max_calls: int,
        provider_timeout: int,
        profile: str,
    ) -> ProviderResult:
        transcript: list[str] = []
        input_tokens = 0
        output_tokens = 0
        source = "internally_metered"
        for call_index in range(max_calls + 1):
            current_prompt = prompt
            if transcript:
                current_prompt += "\n\nTool interaction transcript:\n" + "\n\n".join(transcript)
            candidate = await asyncio.wait_for(
                self.provider.generate(current_prompt, model),
                timeout=provider_timeout,
            )
            self._validate_provider_result(candidate)
            input_tokens += candidate.input_tokens
            output_tokens += candidate.output_tokens
            source = candidate.source
            await self._enforce_tool_loop_budget(
                run_id, profile, input_tokens, output_tokens
            )
            tool_request = parse_tool_request(candidate.text)
            if tool_request is None:
                return ProviderResult(
                    candidate.text,
                    input_tokens,
                    output_tokens,
                    source,
                )
            if call_index >= max_calls:
                raise ValueError(f"Task exceeded its {max_calls}-call tool limit.")
            tool_result = await self._invoke_tool(
                run_id,
                task_id,
                gateway,
                tool_request,
                permissions,
                write_approved,
            )
            sanitized_arguments = json.dumps(
                self._sanitize_tool_arguments(tool_request.arguments), sort_keys=True
            )
            transcript.append(
                f"Agent requested {tool_request.name} with "
                f"{sanitized_arguments}"
                f"\nTool status: {tool_result.status}\nTool result:\n{tool_result.output}"
            )
            if tool_result.status == "denied":
                raise ValueError(f"Tool call denied: {tool_result.output}")
        raise ValueError("Tool loop terminated unexpectedly.")

    async def _enforce_tool_loop_budget(
        self,
        run_id: str,
        profile: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise ValueError("Run disappeared during model invocation.")
            consumed_tokens = await self._consumed_tokens(session, run_id)
            if consumed_tokens + input_tokens + output_tokens > run.token_limit:
                raise ValueError("Tool loop exceeded the run token budget.")
            consumed_cost = await self._consumed_cost(session, run_id)
            incremental_cost = self._estimate_cost(profile, input_tokens, output_tokens)
            if consumed_cost + incremental_cost > run.cost_limit_usd:
                raise ValueError("Tool loop exceeded the run cost budget.")

    async def _invoke_tool(
        self,
        run_id: str,
        task_id: str,
        gateway: ToolGateway,
        request: ToolRequest,
        permissions: list[str],
        write_approved: bool,
    ) -> ToolResult:
        if request.idempotency_key:
            async with SessionLocal() as session:
                existing = await session.scalar(
                    select(ToolCallRecord).where(
                        ToolCallRecord.run_id == run_id,
                        ToolCallRecord.task_id == task_id,
                        ToolCallRecord.idempotency_key == request.idempotency_key,
                    )
                )
                if existing:
                    run = await session.get(RunRecord, run_id)
                    sanitized = self._sanitize_tool_arguments(request.arguments)
                    if existing.tool_name != request.name or existing.arguments_json != sanitized:
                        if run:
                            await self._event(
                                session,
                                run,
                                "tool_idempotency_conflict",
                                "An idempotency key was reused with different tool arguments.",
                                task_id,
                                {"tool": request.name},
                            )
                            await session.commit()
                        return ToolResult(
                            "denied",
                            "Idempotency key conflicts with an earlier tool call.",
                            request.name in ToolGateway.write_tools,
                            "denied",
                        )
                    if run:
                        await self._event(
                            session,
                            run,
                            "tool_replayed",
                            "Mutating tool call resolved from its idempotency record.",
                            task_id,
                            {"tool": existing.tool_name},
                        )
                        await session.commit()
                    return ToolResult(
                        existing.status,
                        existing.result_excerpt,
                        existing.side_effect,
                        existing.approval_state,
                    )
        result = await asyncio.to_thread(
            gateway.execute,
            request,
            permissions,
            write_approved,
        )
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                return result
            session.add(
                ToolCallRecord(
                    run_id=run_id,
                    task_id=task_id,
                    tool_name=request.name,
                    arguments_json=self._sanitize_tool_arguments(request.arguments),
                    status=result.status,
                    result_excerpt=result.output[:2_000],
                    result_hash=result.output_hash,
                    side_effect=result.side_effect,
                    approval_state=result.approval_state,
                    idempotency_key=request.idempotency_key,
                    rollback_json=result.rollback or {},
                    rollback_status="pending" if result.rollback else "not_required",
                )
            )
            await self._event(
                session,
                run,
                f"tool_{result.status}",
                f"{request.name} {result.status}.",
                task_id,
                {
                    "tool": request.name,
                    "side_effect": result.side_effect,
                    "approval_state": result.approval_state,
                    "result_hash": result.output_hash,
                },
            )
            await session.commit()
        return result

    @staticmethod
    def _sanitize_tool_arguments(arguments: dict) -> dict:
        sanitized = dict(arguments)
        if isinstance(sanitized.get("content"), str):
            content = sanitized["content"]
            sanitized["content"] = {
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "characters": len(content),
            }
        if isinstance(sanitized.get("new"), str):
            replacement = sanitized["new"]
            sanitized["new"] = {
                "content_hash": hashlib.sha256(replacement.encode()).hexdigest(),
                "characters": len(replacement),
            }
        if isinstance(sanitized.get("old"), str):
            original = sanitized["old"]
            sanitized["old"] = {
                "content_hash": hashlib.sha256(original.encode()).hexdigest(),
                "characters": len(original),
            }
        return sanitized

    @staticmethod
    def _render_prompt(
        objective: str,
        context: str,
        expected_output: str,
        tool_catalog: str = "No tools are authorized for this task.",
    ) -> str:
        return (
            "You are a TeamSwarm specialist agent. Complete the bounded objective. "
            "Return a concise, evidence-aware result.\n\n"
            f"Objective: {objective}\n\nExpected output: {expected_output}"
            f"\n\nAuthorized context:\n{context or 'None'}"
            f"\n\n{tool_catalog}"
        )

    @staticmethod
    def _validate_provider_result(result: object) -> None:
        text = getattr(result, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Provider returned an empty result.")

    @staticmethod
    def _is_expired(timestamp: datetime | None) -> bool:
        if timestamp is None:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp < datetime.now(UTC)

    async def _record_attempt_event(
        self,
        run_id: str,
        task_id: str,
        kind: str,
        message: str,
        metadata: dict[str, str | int | float | bool],
    ) -> None:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                return
            await self._event(session, run, kind, message, task_id, metadata)
            await session.commit()

    @staticmethod
    def _estimate_cost(profile: str, input_tokens: int, output_tokens: int) -> float:
        rates = {"fast": (0.001, 0.004), "strong": (0.005, 0.03)}
        input_rate, output_rate = rates[profile]
        return round((input_tokens / 1_000 * input_rate) + (output_tokens / 1_000 * output_rate), 6)

    @staticmethod
    async def _consumed_tokens(session: AsyncSession, run_id: str) -> int:
        usages = list(
            (await session.scalars(select(UsageRecord).where(UsageRecord.run_id == run_id))).all()
        )
        return sum(event.total_tokens for event in usages)

    @staticmethod
    async def _consumed_cost(session: AsyncSession, run_id: str) -> float:
        usages = list(
            (await session.scalars(select(UsageRecord).where(UsageRecord.run_id == run_id))).all()
        )
        return sum(event.estimated_cost_usd for event in usages)

    @staticmethod
    async def _event(
        session: AsyncSession,
        run: RunRecord,
        kind: str,
        message: str,
        task_id: str | None = None,
        metadata: dict[str, str | int | float | bool] | None = None,
    ) -> None:
        session.add(
            TraceEventRecord(
                run_id=run.id,
                task_id=task_id,
                kind=kind,
                message=message,
                metadata_json=metadata or {},
            )
        )

    @staticmethod
    def _revision_view(revision: WorkflowRevisionRecord) -> dict:
        return {
            "revision": revision.revision,
            "workflow_type": revision.workflow_type,
            "iteration": revision.iteration,
            "status": revision.status,
            "parent_revision": revision.parent_revision,
            "task_ids": revision.task_ids,
            "reason": revision.reason,
            "created_at": revision.created_at.isoformat(),
        }

    @staticmethod
    def _tool_call_view(call: ToolCallRecord) -> dict:
        return {
            "task_id": call.task_id,
            "tool_name": call.tool_name,
            "arguments": call.arguments_json,
            "status": call.status,
            "result_excerpt": call.result_excerpt,
            "result_hash": call.result_hash,
            "side_effect": call.side_effect,
            "approval_state": call.approval_state,
            "idempotency_key": call.idempotency_key,
            "rollback_status": call.rollback_status,
            "created_at": call.created_at.isoformat(),
        }

    @staticmethod
    def _approval_view(approval: ApprovalRecord) -> dict:
        return {
            "workflow_revision": approval.workflow_revision,
            "decision": approval.decision,
            "comment": approval.comment,
            "created_at": approval.created_at.isoformat(),
        }

    async def get(self, run_id: str) -> RunView:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(run_id)
            tasks = list(
                (await session.scalars(select(TaskRecord).where(TaskRecord.run_id == run_id))).all()
            )
            queue_by_task = {
                item.task_id: item
                for item in (
                    await session.scalars(
                        select(QueueItemRecord).where(QueueItemRecord.run_id == run_id)
                    )
                ).all()
            }
            return RunView(
                id=run.id,
                objective=run.objective,
                status=run.status,
                created_at=run.created_at,
                updated_at=run.updated_at,
                final_output=run.final_output,
                error=run.error,
                tasks=[
                    TaskView(
                        id=task.id,
                        objective=task.objective,
                        dependencies=task.dependencies,
                        model_profile=task.model_profile,
                        agent_role=task.agent_role,
                        model_override=task.model_override,
                        workflow_revision=task.workflow_revision,
                        status=task.status,
                        output=task.output,
                        error=task.error,
                        queue_status=queue_by_task.get(task.id).status
                        if task.id in queue_by_task
                        else None,
                        worker_id=queue_by_task.get(task.id).worker_id
                        if task.id in queue_by_task
                        else None,
                    )
                    for task in tasks
                ],
            )

    async def queue(self, run_id: str) -> RunQueueView:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(run_id)
            items = list(
                (
                    await session.scalars(
                        select(QueueItemRecord)
                        .where(QueueItemRecord.run_id == run_id)
                        .order_by(QueueItemRecord.id)
                    )
                ).all()
            )
            return RunQueueView(
                run_id=run_id,
                items=[
                    QueueItemView(
                        id=item.id,
                        task_id=item.task_id,
                        status=item.status,
                        priority=item.priority,
                        attempts=item.attempts,
                        worker_id=item.worker_id,
                        lease_expires_at=item.lease_expires_at,
                    )
                    for item in items
                ],
            )

    async def replay(self, run_id: str) -> ReplayView:
        """Return immutable artifacts and decisions needed to inspect a completed run."""
        run = await self.get(run_id)
        async with SessionLocal() as session:
            artifacts = list(
                (
                    await session.scalars(
                        select(ArtifactRecord).where(ArtifactRecord.run_id == run_id)
                    )
                ).all()
            )
            evaluations = list(
                (
                    await session.scalars(
                        select(EvaluationRecord).where(EvaluationRecord.run_id == run_id)
                    )
                ).all()
            )
            revisions = list(
                (
                    await session.scalars(
                        select(WorkflowRevisionRecord)
                        .where(WorkflowRevisionRecord.run_id == run_id)
                        .order_by(WorkflowRevisionRecord.revision)
                    )
                ).all()
            )
            tool_calls = list(
                (
                    await session.scalars(
                        select(ToolCallRecord)
                        .where(ToolCallRecord.run_id == run_id)
                        .order_by(ToolCallRecord.created_at, ToolCallRecord.id)
                    )
                ).all()
            )
            approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRecord)
                        .where(ApprovalRecord.run_id == run_id)
                        .order_by(ApprovalRecord.created_at, ApprovalRecord.id)
                    )
                ).all()
            )
        return ReplayView(
            run_id=run_id,
            objective=run.objective,
            tasks=run.tasks,
            artifacts=[
                ArtifactView(
                    id=artifact.id,
                    source_task_id=artifact.source_task_id,
                    kind=artifact.kind,
                    content_hash=artifact.content_hash,
                    validation_state=artifact.validation_state,
                    provenance=artifact.provenance,
                )
                for artifact in artifacts
            ],
            evaluations=[
                {
                    "task_id": evaluation.task_id,
                    "evaluator": evaluation.evaluator,
                    "status": evaluation.status,
                    "score": evaluation.score,
                    "rationale": evaluation.rationale,
                }
                for evaluation in evaluations
            ],
            workflow_revisions=[self._revision_view(revision) for revision in revisions],
            tool_calls=[self._tool_call_view(call) for call in tool_calls],
            approvals=[self._approval_view(approval) for approval in approvals],
        )

    async def trace(self, run_id: str) -> TraceView:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(run_id)
            grants = list(
                (
                    await session.scalars(
                        select(CacheGrantRecord).where(CacheGrantRecord.run_id == run_id)
                    )
                ).all()
            )
            records = list(
                (
                    await session.scalars(
                        select(TraceEventRecord)
                        .where(TraceEventRecord.run_id == run_id)
                        .order_by(TraceEventRecord.created_at, TraceEventRecord.id)
                    )
                ).all()
            )
            manifests = list(
                (
                    await session.scalars(
                        select(ContextManifestRecord)
                        .where(ContextManifestRecord.run_id == run_id)
                        .order_by(ContextManifestRecord.created_at, ContextManifestRecord.id)
                    )
                ).all()
            )
            versions = list(
                (
                    await session.scalars(
                        select(VersionRecord)
                        .where(VersionRecord.run_id == run_id)
                        .order_by(VersionRecord.created_at, VersionRecord.id)
                    )
                ).all()
            )
            revisions = list(
                (
                    await session.scalars(
                        select(WorkflowRevisionRecord)
                        .where(WorkflowRevisionRecord.run_id == run_id)
                        .order_by(WorkflowRevisionRecord.revision)
                    )
                ).all()
            )
            tool_calls = list(
                (
                    await session.scalars(
                        select(ToolCallRecord)
                        .where(ToolCallRecord.run_id == run_id)
                        .order_by(ToolCallRecord.created_at, ToolCallRecord.id)
                    )
                ).all()
            )
            approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRecord)
                        .where(ApprovalRecord.run_id == run_id)
                        .order_by(ApprovalRecord.created_at, ApprovalRecord.id)
                    )
                ).all()
            )
            return TraceView(
                run_id=run_id,
                cache_grants=[
                    {"recipient": grant.recipient, "entry_ids": grant.entry_ids} for grant in grants
                ],
                events=[
                    TraceEvent(
                        timestamp=record.created_at,
                        kind=record.kind,
                        message=record.message,
                        task_id=record.task_id,
                        metadata=record.metadata_json,
                    )
                    for record in records
                ],
                context_manifests=[
                    {
                        "task_id": manifest.task_id,
                        "optimizer_version": manifest.optimizer_version,
                        "budget_tokens": manifest.budget_tokens,
                        "estimated_tokens": manifest.estimated_tokens,
                        "selected": manifest.selected,
                        "omitted": manifest.omitted,
                    }
                    for manifest in manifests
                ],
                versions=[
                    {
                        "cycle": version.cycle,
                        "status": version.status,
                        "revision": version.revision,
                        "message": version.message,
                    }
                    for version in versions
                ],
                workflow_revisions=[
                    self._revision_view(revision) for revision in revisions
                ],
                tool_calls=[self._tool_call_view(call) for call in tool_calls],
                approvals=[self._approval_view(approval) for approval in approvals],
            )

    async def usage(self, run_id: str) -> UsageSummary:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(run_id)
            events = list(
                (
                    await session.scalars(select(UsageRecord).where(UsageRecord.run_id == run_id))
                ).all()
            )
            return UsageSummary(
                run_id=run_id,
                token_limit=run.token_limit,
                cost_limit_usd=run.cost_limit_usd,
                consumed_tokens=sum(event.total_tokens for event in events),
                reserved_tokens=0,
                consumed_cost_usd=round(sum(event.estimated_cost_usd for event in events), 6),
                events=[
                    UsageView(
                        model=event.model,
                        profile=event.profile,
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        total_tokens=event.total_tokens,
                        estimated_cost_usd=event.estimated_cost_usd,
                        source=event.source,
                        created_at=event.created_at,
                    )
                    for event in events
                ],
            )

    async def usage_last_24_hours(self) -> TokenUsageWindow:
        window_end = datetime.now(UTC)
        window_start = window_end - timedelta(hours=24)
        async with SessionLocal() as session:
            events = list(
                (
                    await session.scalars(
                        select(UsageRecord).where(UsageRecord.created_at >= window_start)
                    )
                ).all()
            )
        by_model: dict[str, dict[str, int]] = {}
        for event in events:
            totals = by_model.setdefault(
                event.model,
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
            totals["input_tokens"] += event.input_tokens
            totals["output_tokens"] += event.output_tokens
            totals["total_tokens"] += event.total_tokens
        return TokenUsageWindow(
            window_start=window_start,
            window_end=window_end,
            input_tokens=sum(event.input_tokens for event in events),
            output_tokens=sum(event.output_tokens for event in events),
            total_tokens=sum(event.total_tokens for event in events),
            by_model=[
                ModelTokenUsage(model=model, **totals)
                for model, totals in sorted(
                    by_model.items(), key=lambda item: item[1]["total_tokens"], reverse=True
                )
            ],
        )

    async def cancel(self, run_id: str) -> RunView:
        async with SessionLocal() as session:
            run = await session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(run_id)
            run.status = "cancelled"
            items = list(
                (
                    await session.scalars(
                        select(QueueItemRecord).where(
                            QueueItemRecord.run_id == run_id,
                            QueueItemRecord.status.in_(["queued", "claimed"]),
                        )
                    )
                ).all()
            )
            for item in items:
                item.status = "cancelled"
                item.lease_expires_at = None
            revision = await self._workflow_revision(session, run.id, run.current_cycle)
            if revision and revision.status == "active":
                revision.status = "cancelled"
            await self._rollback_workspace_changes(session, run)
            await self._event(session, run, "run_cancelled", "Cancellation requested")
            await session.commit()
        job = self._jobs.get(run_id)
        if job:
            job.cancel()
        return await self.get(run_id)

    async def decide_approval(self, run_id: str, decision: ApprovalInput) -> RunView:
        async with self._finalize_lock:
            async with SessionLocal() as session:
                run = await session.get(RunRecord, run_id)
                if run is None:
                    raise KeyError(run_id)
                if run.workflow != "human_approval" or run.status != "waiting_approval":
                    raise ValueError("Run is not waiting at a human approval gate.")
                existing = await session.scalar(
                    select(ApprovalRecord).where(ApprovalRecord.run_id == run_id)
                )
                if existing:
                    raise ValueError("This approval gate already has a recorded decision.")
                session.add(
                    ApprovalRecord(
                        run_id=run_id,
                        workflow_revision=run.current_cycle,
                        decision=decision.decision,
                        comment=decision.comment,
                    )
                )
                revision = await self._workflow_revision(session, run.id, run.current_cycle)
                if decision.decision == "reject":
                    run.status = "failed"
                    run.error = "Human rejected the protected execution proposal."
                    if revision:
                        revision.status = "rejected"
                    await self._rollback_workspace_changes(session, run)
                    await self._event(
                        session,
                        run,
                        "approval_rejected",
                        run.error,
                        metadata={"revision": run.current_cycle},
                    )
                else:
                    if revision:
                        revision.status = "approved"
                    proposal_tasks = list(
                        (
                            await session.scalars(
                                select(TaskRecord).where(
                                    TaskRecord.run_id == run_id,
                                    TaskRecord.workflow_revision == run.current_cycle,
                                )
                            )
                        ).all()
                    )
                    run.write_tools_approved = True
                    await self._append_human_execution_revision(
                        session, run, proposal_tasks
                    )
                    run.current_cycle = 2
                    run.status = "running"
                    await self._event(
                        session,
                        run,
                        "approval_granted",
                        "Human approved the bounded proposal; execution resumed.",
                        metadata={"revision": 1, "execution_revision": 2},
                    )
                await session.commit()
        if decision.decision == "approve" and self.inline_worker_enabled:
            self._jobs[run_id] = asyncio.create_task(
                self.execute(run_id), name=f"teamswarm:{run_id}:approved"
            )
        return await self.get(run_id)

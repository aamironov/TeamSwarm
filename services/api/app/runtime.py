import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .attachments import render_attachments
from .config import get_settings
from .context import ContextItem, ContextOptimizer
from .db import SessionLocal
from .models import (
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
    TraceEventRecord,
    UsageRecord,
    VersionRecord,
)
from .planner import build_delivery_cycle_plan, build_plan
from .providers import ModelProvider, get_provider
from .routing import route_task
from .schemas import (
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
        plan = build_plan(request)
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
                workflow=request.workflow,
                current_cycle=1,
                max_cycles=request.max_cycles if request.workflow == "delivery_cycle" else 1,
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
                        permissions=["model:invoke", "read:granted-artifacts"],
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
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
                if current.status in {"cancelled", "succeeded", "failed"}:
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
            if run is None or run.status in {"cancelled", "succeeded", "failed"}:
                return
            tasks = list(
                (await session.scalars(select(TaskRecord).where(TaskRecord.run_id == run_id))).all()
            )
            if any(task.status == "failed" for task in tasks):
                run.status = "failed"
                run.error = "A mandatory task failed."
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
                await self._event(session, run, "run_failed", run.error)
                await session.commit()
            elif run.workflow == "delivery_cycle":
                cycle_tasks = [task for task in tasks if task.cycle == run.current_cycle]
                if cycle_tasks and all(task.status == "succeeded" for task in cycle_tasks):
                    evaluator = next(
                        (task for task in cycle_tasks if task.agent_role == "evaluator"), None
                    )
                    if evaluator is None:
                        run.status = "failed"
                        run.error = "Delivery cycle has no evaluator task."
                        await self._event(session, run, "run_failed", run.error)
                    elif self._goal_achieved(evaluator.output):
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
                        run.status = "failed"
                        run.error = "Goal was not achieved before the delivery-cycle limit."
                        await self._event(
                            session, run, "goal_not_achieved", run.error, evaluator.id
                        )
                    await session.commit()
            elif tasks and all(task.status == "succeeded" for task in tasks):
                await self._consolidate(session, run, tasks)
                await session.commit()

    async def _save_stable_version(self, session: AsyncSession, run: RunRecord) -> None:
        """Persist the local-Git outcome without allowing it to fail the run."""
        snapshot = await asyncio.to_thread(
            self.version_control.snapshot,
            run_id=run.id,
            cycle=run.current_cycle,
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
        for task in build_delivery_cycle_plan(request, cycle):
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
                    permissions=["model:invoke", "read:granted-artifacts"],
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
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
    def _goal_achieved(output: str | None) -> bool:
        return bool(output and "goal_achieved: yes" in output.casefold())

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

        prompt = self._render_prompt(task.objective, context, contract.expected_output)
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
        settings = get_settings()
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
                candidate = await asyncio.wait_for(
                    self.provider.generate(attempt_prompt, attempt_model),
                    timeout=settings.task_timeout_seconds,
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
        await self._event(
            session, run, "self_evaluation", "Output is non-empty; accepted by MVP validator"
        )
        await self._event(session, run, "run_succeeded", "Consolidated result accepted")

    @staticmethod
    def _render_prompt(objective: str, context: str, expected_output: str) -> str:
        return (
            "You are a TeamSwarm specialist agent. Complete the bounded objective. "
            "Return a concise, evidence-aware result.\n\n"
            f"Objective: {objective}\n\nExpected output: {expected_output}"
            f"\n\nAuthorized context:\n{context or 'None'}"
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
            await self._event(session, run, "run_cancelled", "Cancellation requested")
            await session.commit()
        job = self._jobs.get(run_id)
        if job:
            job.cancel()
        return await self.get(run_id)

from dataclasses import dataclass
from uuid import uuid4

from .schemas import RunCreate


@dataclass(frozen=True)
class PlannedTask:
    id: str
    objective: str
    dependencies: list[str]
    model_profile: str
    expected_output: str
    acceptance_checks: list[str]
    priority: int
    agent_role: str = "general"
    model_override: str | None = None


def _profile_for(objective: str) -> str:
    complexity_markers = ("analyze", "design", "compare", "implement", "code", "research")
    return (
        "strong"
        if len(objective) > 500 or any(word in objective.lower() for word in complexity_markers)
        else "fast"
    )


def build_plan(request: RunCreate) -> list[PlannedTask]:
    """Create a small, deterministic DAG for the MVP.

    Clients may supply up to three explicit subtasks. Without them, the request
    stays single-agent; semantic LLM planning is deliberately deferred.
    """
    if request.workflow == "delivery_cycle":
        if request.prompt_variants or request.subtasks:
            raise ValueError(
                "The delivery-cycle workflow accepts one prompt, not subtasks or variants."
            )
        return build_delivery_cycle_plan(request, cycle=1)
    if request.workflow == "review_repair":
        if request.prompt_variants or request.subtasks:
            raise ValueError(
                "The review/repair workflow accepts one prompt, not subtasks or variants."
            )
        return build_review_repair_plan(request, revision=1)
    if request.workflow == "conditional":
        return build_conditional_plan(request)
    if request.workflow == "map_reduce":
        return build_map_reduce_plan(request)
    if request.workflow == "refinement":
        return build_refinement_plan(request, revision=1)
    if request.workflow == "human_approval":
        return build_human_approval_plan(request, revision=1)
    if request.prompt_variants and request.subtasks:
        raise ValueError("Prompt quantification cannot be combined with explicit subtasks.")
    if request.prompt_variants:
        normalized_variants = [variant.strip().casefold() for variant in request.prompt_variants]
        if not all(normalized_variants) or len(set(normalized_variants)) != len(normalized_variants):
            raise ValueError("Prompt variants must be non-empty, distinct coverage dimensions.")
        variant_ids = [str(uuid4()) for _ in request.prompt_variants]
        variants = [
            PlannedTask(
                task_id,
                f"{request.objective}\n\nCoverage dimension: {variant}",
                [],
                _profile_for(request.objective),
                request.expected_output,
                request.acceptance_checks,
                request.priority,
                agent_role="quantified_variant",
            )
            for task_id, variant in zip(variant_ids, request.prompt_variants, strict=True)
        ]
        return [
            *variants,
            PlannedTask(
                str(uuid4()),
                "Consolidate the granted quantified-prompt findings. Preserve each coverage "
                "dimension's provenance, remove duplicates, and explicitly report conflicts.",
                variant_ids,
                "strong",
                request.expected_output,
                request.acceptance_checks,
                request.priority,
                agent_role="quantification_consolidator",
            ),
        ]
    if not request.subtasks:
        return [
            PlannedTask(
                str(uuid4()),
                request.objective,
                [],
                _profile_for(request.objective),
                request.expected_output,
                request.acceptance_checks,
                request.priority,
            )
        ]

    task_ids = [str(uuid4()) for _ in request.subtasks]
    aliases = {f"task-{index + 1}": task_id for index, task_id in enumerate(task_ids)}
    planned: list[PlannedTask] = []
    for index, item in enumerate(request.subtasks):
        dependencies: list[str] = []
        for dependency in item.depends_on:
            if dependency not in aliases:
                raise ValueError(
                    f"Unknown dependency '{dependency}'. Use task-1 through task-{len(task_ids)}."
                )
            task_id = aliases[dependency]
            if task_id == task_ids[index]:
                raise ValueError("A task cannot depend on itself.")
            dependencies.append(task_id)
        planned.append(
            PlannedTask(
                task_ids[index],
                item.objective,
                dependencies,
                _profile_for(item.objective),
                item.expected_output,
                item.acceptance_checks,
                item.priority,
                agent_role="subtask",
            )
        )
    _validate_acyclic(planned)
    return planned


def build_delivery_cycle_plan(request: RunCreate, cycle: int) -> list[PlannedTask]:
    """Build one criteria→discovery→coding→tests→evaluation cycle."""
    roles = [
        ("criteria", "Invent measurable acceptance criteria for the user's requested outcome."),
        (
            "discovery",
            "Discover the implementation approach, relevant files, risks, and dependencies.",
        ),
        ("coding", "Implement the requested outcome using the discovery findings."),
        ("testing", "Add and run focused unit tests for the implementation."),
        (
            "evaluator",
            "Decide whether the goal is achieved against the criteria and test evidence.",
        ),
    ]
    ids = [str(uuid4()) for _ in roles]
    dependencies = [[], [ids[0]], [ids[1]], [ids[2]], [ids[0], ids[1], ids[2], ids[3]]]
    planned: list[PlannedTask] = []
    for index, ((role, instruction), task_id) in enumerate(zip(roles, ids, strict=True)):
        profile = "strong" if role in {"criteria", "coding", "evaluator"} else "fast"
        objective = (
            f"Shared user prompt:\n{request.objective}\n\n"
            f"Cycle: {cycle}\nRole: {role}\nResponsibility: {instruction}"
        )
        expected_output = {
            "criteria": "Acceptance criteria, non-goals, risks, and measurable completion checks.",
            "discovery": (
                "Relevant files or systems, implementation approach, risks, and open questions."
            ),
            "coding": "Changed files, implementation decisions, commands run, and remaining work.",
            "testing": "Tests added or run, results, failures, and coverage gaps.",
            "evaluator": "Evidence for each criterion and the required GOAL_ACHIEVED marker.",
        }[role]
        if role == "evaluator":
            objective += "\nReturn exactly one line: GOAL_ACHIEVED: yes or GOAL_ACHIEVED: no."
        planned.append(
            PlannedTask(
                task_id,
                objective,
                dependencies[index],
                profile,
                expected_output,
                request.acceptance_checks,
                request.priority,
                agent_role=role,
            )
        )
    return planned


def build_review_repair_plan(request: RunCreate, revision: int) -> list[PlannedTask]:
    """Build an immutable builder/reviewer or repairer/reviewer graph revision."""
    worker_id = str(uuid4())
    reviewer_id = str(uuid4())
    if revision == 1:
        role = "builder"
        responsibility = (
            "Implement the requested outcome in the approved workspace. Inspect relevant files, "
            "make focused changes, and run verification using only granted tools."
        )
    else:
        role = "repairer"
        responsibility = (
            "Apply only the actionable repairs identified by the prior reviewer, then rerun "
            "focused verification using only granted tools."
        )
    worker = PlannedTask(
        worker_id,
        (
            f"Shared user prompt:\n{request.objective}\n\nWorkflow revision: {revision}\n"
            f"Role: {role}\nResponsibility: {responsibility}"
        ),
        [],
        "strong",
        "Changed files, tool evidence, verification results, and remaining risks.",
        request.acceptance_checks,
        request.priority,
        agent_role=role,
    )
    reviewer = PlannedTask(
        reviewer_id,
        (
            f"Shared user prompt:\n{request.objective}\n\nWorkflow revision: {revision}\n"
            "Role: reviewer\nResponsibility: Inspect the workspace and upstream evidence. "
            "Identify only concrete, actionable defects. Return a short review followed by "
            "exactly one line: REPAIR_REQUIRED: yes or REPAIR_REQUIRED: no."
        ),
        [worker_id],
        "strong",
        "Evidence-based review and the required REPAIR_REQUIRED marker.",
        ["non_empty"],
        request.priority,
        agent_role="reviewer",
    )
    return [worker, reviewer]


def build_conditional_plan(request: RunCreate) -> list[PlannedTask]:
    if request.conditional is None:
        raise ValueError("The conditional workflow requires conditional configuration.")
    selected = (
        request.conditional.if_true
        if request.conditional.condition
        else request.conditional.if_false
    )
    branch = "true" if request.conditional.condition else "false"
    return [
        PlannedTask(
            str(uuid4()),
            (
                f"Shared user prompt:\n{request.objective}\n\nRole: branch\n"
                f"Selected deterministic branch: {branch}\nBranch objective: {selected}"
            ),
            [],
            _profile_for(selected),
            request.expected_output,
            request.acceptance_checks,
            request.priority,
            agent_role="branch",
        )
    ]


def build_map_reduce_plan(request: RunCreate) -> list[PlannedTask]:
    if request.map_reduce is None:
        raise ValueError("The map/reduce workflow requires map_reduce configuration.")
    map_ids = [str(uuid4()) for _ in request.map_reduce.items]
    tasks = [
        PlannedTask(
            task_id,
            (
                f"Shared user prompt:\n{request.objective}\n\nRole: map\n"
                f"Independent partition: {item}"
            ),
            [],
            _profile_for(item),
            "A partition result with evidence and provenance.",
            request.acceptance_checks,
            request.priority,
            agent_role="map",
        )
        for task_id, item in zip(map_ids, request.map_reduce.items, strict=True)
    ]
    tasks.append(
        PlannedTask(
            str(uuid4()),
            (
                f"Shared user prompt:\n{request.objective}\n\nRole: reducer\n"
                "Consolidate every granted map result, deduplicate claims, retain conflicts, "
                "and preserve partition-level provenance."
            ),
            map_ids,
            "strong",
            request.expected_output,
            request.acceptance_checks,
            request.priority,
            agent_role="reducer",
        )
    )
    return tasks


def build_refinement_plan(request: RunCreate, revision: int) -> list[PlannedTask]:
    refiner_id = str(uuid4())
    evaluator_id = str(uuid4())
    return [
        PlannedTask(
            refiner_id,
            (
                f"Shared user prompt:\n{request.objective}\n\nWorkflow revision: {revision}\n"
                "Role: refiner\nCreate or improve the workspace outcome using prior revision "
                "evidence and granted tools."
            ),
            [],
            "strong",
            "Refined artifact, changes, verification evidence, and remaining risks.",
            request.acceptance_checks,
            request.priority,
            agent_role="refiner",
        ),
        PlannedTask(
            evaluator_id,
            (
                f"Shared user prompt:\n{request.objective}\n\nWorkflow revision: {revision}\n"
                "Role: refinement_evaluator\nEvaluate measurable completion. Return exactly "
                "one line: REFINEMENT_COMPLETE: yes or REFINEMENT_COMPLETE: no."
            ),
            [refiner_id],
            "strong",
            "Evidence and the required REFINEMENT_COMPLETE marker.",
            ["non_empty"],
            request.priority,
            agent_role="refinement_evaluator",
        ),
    ]


def build_human_approval_plan(request: RunCreate, revision: int) -> list[PlannedTask]:
    if revision == 1:
        return [
            PlannedTask(
                str(uuid4()),
                (
                    f"Shared user prompt:\n{request.objective}\n\nRole: approval_proposer\n"
                    "Inspect the workspace and propose the exact bounded change, risks, tools, "
                    "and verification plan. Do not mutate the workspace."
                ),
                [],
                "strong",
                "A concrete execution proposal for human approval.",
                request.acceptance_checks,
                request.priority,
                agent_role="approval_proposer",
            )
        ]
    executor_id = str(uuid4())
    return [
        PlannedTask(
            executor_id,
            (
                f"Shared user prompt:\n{request.objective}\n\nRole: approved_executor\n"
                "Execute only the human-approved proposal using granted tools."
            ),
            [],
            "strong",
            "Approved changes and verification evidence.",
            request.acceptance_checks,
            request.priority,
            agent_role="approved_executor",
        ),
        PlannedTask(
            str(uuid4()),
            (
                f"Shared user prompt:\n{request.objective}\n\nRole: approval_verifier\n"
                "Verify that execution stayed within the approved proposal and succeeded."
            ),
            [executor_id],
            "strong",
            "Verification of scope and outcome.",
            request.acceptance_checks,
            request.priority,
            agent_role="approval_verifier",
        ),
    ]


def _validate_acyclic(tasks: list[PlannedTask]) -> None:
    dependencies = {task.id: set(task.dependencies) for task in tasks}
    completed: set[str] = set()
    while dependencies:
        ready = {task_id for task_id, required in dependencies.items() if required <= completed}
        if not ready:
            raise ValueError("Task graph contains a dependency cycle.")
        completed.update(ready)
        for task_id in ready:
            dependencies.pop(task_id)

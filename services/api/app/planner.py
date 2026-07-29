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
    if request.prompt_variants and request.subtasks:
        raise ValueError("Prompt quantification cannot be combined with explicit subtasks.")
    if request.prompt_variants:
        return [
            PlannedTask(
                str(uuid4()),
                f"{request.objective}\n\nCoverage dimension: {variant}",
                [],
                _profile_for(request.objective),
                request.expected_output,
                request.acceptance_checks,
                request.priority,
            )
            for variant in request.prompt_variants
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

import pytest

from services.api.app.planner import build_plan
from services.api.app.schemas import RunCreate, SubtaskInput


def test_single_objective_stays_single_agent() -> None:
    plan = build_plan(RunCreate(objective="Summarize the incident."))
    assert len(plan) == 1
    assert plan[0].model_profile == "fast"


def test_explicit_tasks_preserve_dependency() -> None:
    plan = build_plan(
        RunCreate(
            objective="Investigate and summarize.",
            subtasks=[
                SubtaskInput(objective="Collect evidence."),
                SubtaskInput(objective="Analyze evidence.", depends_on=["task-1"]),
            ],
        )
    )
    assert plan[1].dependencies == [plan[0].id]
    assert plan[1].model_profile == "strong"


def test_cycle_is_rejected() -> None:
    with pytest.raises(ValueError, match="cycle"):
        build_plan(
            RunCreate(
                objective="Invalid plan.",
                subtasks=[
                    SubtaskInput(objective="First.", depends_on=["task-2"]),
                    SubtaskInput(objective="Second.", depends_on=["task-1"]),
                ],
            )
        )


def test_unknown_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown dependency"):
        build_plan(
            RunCreate(
                objective="Invalid plan.",
                subtasks=[SubtaskInput(objective="First.", depends_on=["task-3"])],
            )
        )


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot depend on itself"):
        build_plan(
            RunCreate(
                objective="Invalid plan.",
                subtasks=[SubtaskInput(objective="First.", depends_on=["task-1"])],
            )
        )


def test_complex_objective_routes_to_strong_profile() -> None:
    plan = build_plan(RunCreate(objective="Design a provider-neutral orchestration service."))
    assert plan[0].model_profile == "strong"


def test_prompt_quantification_creates_bounded_partitioned_tasks() -> None:
    plan = build_plan(
        RunCreate(
            objective="Assess the launch plan.",
            prompt_variants=["cost risks", "delivery risks", "quality risks"],
        )
    )

    assert len(plan) == 3
    assert all("Coverage dimension:" in task.objective for task in plan)
    assert {task.objective.rsplit("Coverage dimension: ", 1)[1] for task in plan} == {
        "cost risks",
        "delivery risks",
        "quality risks",
    }


def test_prompt_quantification_cannot_be_combined_with_explicit_subtasks() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        build_plan(
            RunCreate(
                objective="Invalid plan.",
                subtasks=[SubtaskInput(objective="A task.")],
                prompt_variants=["variant"],
            )
        )


def test_delivery_cycle_builds_the_required_role_sequence() -> None:
    plan = build_plan(RunCreate(objective="Implement the feature.", workflow="delivery_cycle"))

    assert [task.agent_role for task in plan] == [
        "criteria",
        "discovery",
        "coding",
        "testing",
        "evaluator",
    ]
    assert plan[1].dependencies == [plan[0].id]
    assert plan[2].dependencies == [plan[1].id]
    assert plan[3].dependencies == [plan[2].id]
    assert plan[4].dependencies == [task.id for task in plan[:4]]

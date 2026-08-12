import pytest

from services.api.app.planner import build_plan
from services.api.app.schemas import (
    ConditionalWorkflowInput,
    MapReduceWorkflowInput,
    RunCreate,
    SubtaskInput,
)


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

    assert len(plan) == 4
    variants = plan[:-1]
    consolidator = plan[-1]
    assert all("Coverage dimension:" in task.objective for task in variants)
    assert {task.objective.rsplit("Coverage dimension: ", 1)[1] for task in variants} == {
        "cost risks",
        "delivery risks",
        "quality risks",
    }
    assert consolidator.agent_role == "quantification_consolidator"
    assert consolidator.dependencies == [task.id for task in variants]


def test_prompt_quantification_cannot_be_combined_with_explicit_subtasks() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        build_plan(
            RunCreate(
                objective="Invalid plan.",
                subtasks=[SubtaskInput(objective="A task.")],
                prompt_variants=["variant"],
            )
        )


def test_prompt_quantification_rejects_duplicate_coverage_dimensions() -> None:
    with pytest.raises(ValueError, match="distinct coverage dimensions"):
        build_plan(
            RunCreate(
                objective="Invalid plan.",
                prompt_variants=["Cost risks", " cost risks "],
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


def test_conditional_workflow_creates_only_the_selected_branch() -> None:
    plan = build_plan(
        RunCreate(
            objective="Choose a release action.",
            workflow="conditional",
            conditional=ConditionalWorkflowInput(
                condition=False,
                if_true="Deploy the release.",
                if_false="Hold the release.",
            ),
        )
    )

    assert len(plan) == 1
    assert "Selected deterministic branch: false" in plan[0].objective
    assert "Hold the release." in plan[0].objective
    assert "Deploy the release." not in plan[0].objective


def test_map_reduce_workflow_fans_out_and_has_one_controlled_reducer() -> None:
    plan = build_plan(
        RunCreate(
            objective="Assess the launch.",
            workflow="map_reduce",
            map_reduce=MapReduceWorkflowInput(items=["cost", "quality", "delivery"]),
        )
    )

    assert [task.agent_role for task in plan] == ["map", "map", "map", "reducer"]
    assert plan[-1].dependencies == [task.id for task in plan[:-1]]


def test_refinement_and_human_approval_create_typed_initial_revisions() -> None:
    refinement = build_plan(
        RunCreate(objective="Improve the implementation.", workflow="refinement")
    )
    approval = build_plan(
        RunCreate(objective="Prepare a protected change.", workflow="human_approval")
    )

    assert [task.agent_role for task in refinement] == [
        "refiner",
        "refinement_evaluator",
    ]
    assert [task.agent_role for task in approval] == ["approval_proposer"]

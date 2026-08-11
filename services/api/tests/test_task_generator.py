import pytest

from services.api.app.config import Settings
from services.api.app.providers import ModelProvider, ProviderResult
from services.api.app.schemas import RunCreate
from services.api.app.task_generator import ProviderPlanningAgent


class PlanningProvider(ModelProvider):
    async def generate(self, prompt: str, model: str) -> ProviderResult:
        return ProviderResult(
            """{"tasks":[
              {"id":"task-1","objective":"Collect evidence.","depends_on":[],
               "expected_output":"Evidence list.","acceptance_checks":["non_empty"],"priority":5},
              {"id":"task-2","objective":"Analyze evidence.","depends_on":["task-1"],
               "expected_output":"Supported conclusion.",
               "acceptance_checks":["non_empty"],"priority":0}
            ]}""",
            20,
            10,
            "provider_reported",
        )


@pytest.mark.asyncio
async def test_provider_planning_agent_generates_a_validated_dag() -> None:
    result = await ProviderPlanningAgent(PlanningProvider()).generate(
        RunCreate(objective="Investigate the failure."),
        [],
        Settings(_env_file=None, strong_model="planner-model"),
    )

    assert result.backend == "provider-agent"
    assert result.model == "planner-model"
    assert result.tasks[1].dependencies == [result.tasks[0].id]


class InvalidPlanningProvider(ModelProvider):
    async def generate(self, prompt: str, model: str) -> ProviderResult:
        return ProviderResult("not JSON", 1, 1, "internally_metered")


@pytest.mark.asyncio
async def test_provider_planning_agent_rejects_unstructured_output() -> None:
    with pytest.raises(ValueError, match="invalid task graph"):
        await ProviderPlanningAgent(InvalidPlanningProvider()).generate(
            RunCreate(objective="Investigate the failure."),
            [],
            Settings(),
        )

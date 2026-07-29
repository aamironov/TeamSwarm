"""Planning-agent backends that generate validated TeamSwarm task DAGs."""

import json
import os
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .planner import PlannedTask, build_plan
from .providers import ModelProvider
from .schemas import RunCreate, SubtaskInput
from .skills import AgentSkill


class GeneratedTask(BaseModel):
    id: str = Field(pattern=r"^task-[1-9][0-9]*$")
    objective: str = Field(min_length=3, max_length=8_000)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    expected_output: str = Field(min_length=3, max_length=2_000)
    acceptance_checks: list[str] = Field(default_factory=lambda: ["non_empty"], max_length=8)
    priority: int = Field(default=0, ge=-100, le=100)


class GeneratedPlan(BaseModel):
    tasks: list[GeneratedTask] = Field(min_length=1, max_length=8)


@dataclass(frozen=True)
class PlanningResult:
    tasks: list[PlannedTask]
    backend: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    usage_source: str = "internally_metered"


class TaskGenerator:
    async def generate(
        self, request: RunCreate, skills: list[AgentSkill], settings: Settings
    ) -> PlanningResult:
        raise NotImplementedError


class DeterministicTaskGenerator(TaskGenerator):
    async def generate(
        self, request: RunCreate, skills: list[AgentSkill], settings: Settings
    ) -> PlanningResult:
        return PlanningResult(build_plan(request), "deterministic")


class ProviderPlanningAgent(TaskGenerator):
    """A small planning-only agent using TeamSwarm's configured model provider."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    async def generate(
        self, request: RunCreate, skills: list[AgentSkill], settings: Settings
    ) -> PlanningResult:
        model = settings.model_for("strong")
        result = await self.provider.generate(_planning_prompt(request, skills), model)
        tasks = _parse_plan(result.text, request)
        return PlanningResult(
            tasks,
            "provider-agent",
            model,
            result.input_tokens,
            result.output_tokens,
            result.source,
        )


class AutoGenPlanningAgent(TaskGenerator):
    """Optional Microsoft AutoGen AssistantAgent used only as TeamSwarm's planner."""

    async def generate(
        self, request: RunCreate, skills: list[AgentSkill], settings: Settings
    ) -> PlanningResult:
        try:
            from autogen_agentchat.agents import AssistantAgent
            from autogen_ext.models.openai import OpenAIChatCompletionClient
        except ImportError as error:
            raise ValueError(
                "AutoGen planning requires the 'autogen' optional dependency."
            ) from error

        arguments: dict[str, str] = {"model": settings.model_for("strong")}
        if settings.provider_mode == "openai":
            arguments["api_key"] = os.getenv("OPENAI_API_KEY", "")
        elif settings.provider_mode == "sglang":
            arguments.update(api_key="EMPTY", base_url=f"{settings.sglang_base_url.rstrip('/')}/v1")
        elif settings.provider_mode == "ollama":
            arguments.update(
                api_key="ollama",
                base_url=f"{settings.ollama_base_url.rstrip('/')}/v1",
            )
        else:
            raise ValueError("AutoGen planning requires openai, sglang, or ollama provider mode.")

        client = OpenAIChatCompletionClient(**arguments)
        agent = AssistantAgent(
            "planning_agent",
            model_client=client,
            system_message=(
                "You are a planning agent. Break goals into a bounded acyclic task graph. "
                "You plan and delegate only; you never execute tasks. Return only valid JSON."
            ),
        )
        try:
            result = await agent.run(task=_planning_prompt(request, skills))
            content = result.messages[-1].content
        finally:
            await client.close()
        if not isinstance(content, str):
            raise ValueError("AutoGen planning agent did not return text.")
        return PlanningResult(
            _parse_plan(content, request),
            "autogen",
            settings.model_for("strong"),
        )


def get_task_generator(
    backend: Literal["deterministic", "provider-agent", "autogen"],
    provider: ModelProvider,
) -> TaskGenerator:
    if backend == "provider-agent":
        return ProviderPlanningAgent(provider)
    if backend == "autogen":
        return AutoGenPlanningAgent()
    return DeterministicTaskGenerator()


def _planning_prompt(request: RunCreate, skills: list[AgentSkill]) -> str:
    advertised = "\n".join(f"- {skill.name}: {skill.description}" for skill in skills) or "- none"
    instructions = "\n\n".join(
        f"## Skill: {skill.name}\n{skill.instructions}" for skill in skills
    )
    return f"""Create a task graph for this goal:
{request.objective}

Available selected skills:
{advertised}

Activated skill instructions:
{instructions or "None"}

Return only JSON with this schema:
{{"tasks":[{{"id":"task-1","objective":"...","depends_on":[],
"expected_output":"...","acceptance_checks":["non_empty"],"priority":0}}]}}

Rules:
- Produce 1 to 8 tasks.
- Use sequential task-N IDs without gaps.
- Dependencies may reference only task IDs in this plan.
- Keep the graph acyclic and make independent work parallel.
- Each task must be independently actionable and name the relevant activated skill.
"""


def _parse_plan(text: str, request: RunCreate) -> list[PlannedTask]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", maxsplit=1)[-1].rsplit("```", maxsplit=1)[0].strip()
    try:
        generated = GeneratedPlan.model_validate(json.loads(candidate))
    except (json.JSONDecodeError, ValidationError) as error:
        raise ValueError(f"Planning agent returned an invalid task graph: {error}") from error
    expected_ids = [f"task-{index}" for index in range(1, len(generated.tasks) + 1)]
    if [task.id for task in generated.tasks] != expected_ids:
        raise ValueError("Planning agent task IDs must be sequential from task-1.")
    delegated = request.model_copy(
        update={
            "planner_backend": "deterministic",
            "subtasks": [
                SubtaskInput(
                    objective=task.objective,
                    depends_on=task.depends_on,
                    expected_output=task.expected_output,
                    acceptance_checks=task.acceptance_checks,
                    priority=task.priority,
                )
                for task in generated.tasks
            ],
        }
    )
    return build_plan(delegated)

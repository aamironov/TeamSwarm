from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TaskStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]
RunStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]
QueueStatus = Literal["queued", "claimed", "completed", "failed", "cancelled"]


class Budget(BaseModel):
    token_limit: int = Field(default=12_000, ge=100, le=1_000_000)
    cost_limit_usd: float = Field(default=2.0, ge=0)


class SubtaskInput(BaseModel):
    objective: str = Field(min_length=3, max_length=8_000)
    depends_on: list[str] = Field(default_factory=list)
    expected_output: str = Field(default="A concise, evidence-aware result.", max_length=2_000)
    acceptance_checks: list[str] = Field(default_factory=lambda: ["non_empty"], max_length=8)
    priority: int = Field(default=0, ge=-100, le=100)


class PromptAttachmentInput(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=100_000)


class RunCreate(BaseModel):
    objective: str = Field(min_length=3, max_length=20_000)
    subtasks: list[SubtaskInput] = Field(default_factory=list, max_length=3)
    budget: Budget | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    expected_output: str = Field(default="A concise, evidence-aware result.", max_length=2_000)
    acceptance_checks: list[str] = Field(default_factory=lambda: ["non_empty"], max_length=8)
    priority: int = Field(default=0, ge=-100, le=100)
    attachments: list[PromptAttachmentInput] = Field(default_factory=list, max_length=8)
    prompt_variants: list[str] = Field(default_factory=list, max_length=3)
    workflow: Literal["standard", "delivery_cycle"] = "standard"
    max_cycles: int = Field(default=2, ge=1, le=3)


class TaskView(BaseModel):
    id: str
    objective: str
    dependencies: list[str]
    model_profile: Literal["fast", "strong"]
    agent_role: str = "general"
    model_override: str | None = None
    status: TaskStatus
    output: str | None = None
    error: str | None = None
    queue_status: QueueStatus | None = None
    worker_id: str | None = None


class UsageView(BaseModel):
    model: str
    profile: Literal["fast", "strong"]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    source: Literal["provider_reported", "internally_metered", "estimated"]
    created_at: datetime


class RunView(BaseModel):
    id: str
    objective: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    final_output: str | None = None
    error: str | None = None
    tasks: list[TaskView] = Field(default_factory=list)


class TraceEvent(BaseModel):
    timestamp: datetime
    kind: str
    message: str
    task_id: str | None = None
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class TraceView(BaseModel):
    run_id: str
    cache_grants: list[dict[str, str | list[str]]]
    events: list[TraceEvent]
    context_manifests: list[dict] = Field(default_factory=list)
    versions: list[dict[str, str | int | None]] = Field(default_factory=list)


class UsageSummary(BaseModel):
    run_id: str
    token_limit: int
    cost_limit_usd: float
    consumed_tokens: int
    reserved_tokens: int
    consumed_cost_usd: float
    events: list[UsageView]


class AvailableModel(BaseModel):
    id: str
    provider: str
    location: Literal["local", "remote"]
    availability: Literal["available", "configured", "not_installed"]
    profiles: list[Literal["fast", "strong", "fallback"]]


class ModelCatalog(BaseModel):
    active_provider: str
    models: list[AvailableModel]


class ModelTokenUsage(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


class TokenUsageWindow(BaseModel):
    window_start: datetime
    window_end: datetime
    input_tokens: int
    output_tokens: int
    total_tokens: int
    by_model: list[ModelTokenUsage]


class QueueItemView(BaseModel):
    id: str
    task_id: str
    status: QueueStatus
    priority: int
    attempts: int
    worker_id: str | None = None
    lease_expires_at: datetime | None = None


class RunQueueView(BaseModel):
    run_id: str
    items: list[QueueItemView]


class ArtifactView(BaseModel):
    id: str
    source_task_id: str
    kind: str
    content_hash: str
    validation_state: str
    provenance: dict


class ReplayView(BaseModel):
    run_id: str
    objective: str
    tasks: list[TaskView]
    artifacts: list[ArtifactView]
    evaluations: list[dict]


class ProjectCreate(BaseModel):
    directory: str = Field(min_length=1, max_length=2_000)
    name: str | None = Field(default=None, max_length=160)


class ProjectView(BaseModel):
    id: str
    name: str
    directory: str
    created_at: datetime


class ChatCreate(BaseModel):
    project_id: str
    title: str = Field(default="New chat", min_length=1, max_length=200)


class ChatMessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    model_profile: Literal["fast", "strong"] = "fast"
    attachments: list[PromptAttachmentInput] = Field(default_factory=list, max_length=8)


class AttachmentView(BaseModel):
    filename: str
    content_hash: str


class ChatMessageView(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    model: str | None = None
    context_hash: str | None = None
    attachments: list[AttachmentView] = Field(default_factory=list)
    created_at: datetime


class ChatView(BaseModel):
    id: str
    project_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageView] = Field(default_factory=list)

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TEAMSWARM_", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./teamswarm.db"
    provider_mode: str = "mock"
    fast_model: str = "gpt-5.6-terra"
    strong_model: str = "gpt-5.6-sol"
    fallback_model: str = "gpt-5.6-sol"
    ollama_base_url: str = "http://localhost:11434"
    ollama_think: bool = False
    ollama_min_free_memory_gb: float = 0
    ollama_model_memory_reserves_gb: str = ""
    ollama_fast_model: str = "llama3.2:3b"
    ollama_strong_model: str = "qwen3:8b"
    ollama_fallback_model: str = "qwen3:8b"
    sglang_base_url: str = "http://localhost:30000"
    sglang_fast_model: str = "Qwen/Qwen3-4B"
    sglang_strong_model: str = "Qwen/Qwen3-8B"
    sglang_fallback_model: str = "Qwen/Qwen3-8B"
    criteria_model: str | None = None
    discovery_model: str | None = None
    coding_model: str | None = None
    testing_model: str | None = None
    evaluator_model: str | None = None
    task_timeout_seconds: int = 45
    max_task_attempts: int = 2
    task_lease_seconds: int = 120
    inline_worker_enabled: bool = True
    max_concurrent_tasks: int = 4
    max_concurrent_tasks_per_profile: int = 2
    max_tool_calls_per_task: int = 8
    tool_timeout_seconds: int = 120
    max_tool_output_chars: int = 50_000
    context_token_budget: int = 6_000
    code_context_enabled: bool = False
    code_context_max_files: int = 500
    code_context_max_items: int = 6
    response_cache_enabled: bool = True
    semantic_response_cache_enabled: bool = False
    semantic_response_cache_min_similarity: float = 0.92
    project_context_roots: str = "."
    skill_roots: str = "./skills"
    default_token_budget: int = 12_000
    default_cost_budget_usd: float = 2.0
    api_cors_origin: str = "http://localhost:3000"

    def model_for(self, profile: str) -> str:
        provider_mode = self.provider_mode.lower()
        if provider_mode == "ollama":
            return self.ollama_strong_model if profile == "strong" else self.ollama_fast_model
        if provider_mode == "sglang":
            return self.sglang_strong_model if profile == "strong" else self.sglang_fast_model
        return self.strong_model if profile == "strong" else self.fast_model

    def fallback_for(self) -> str:
        provider_mode = self.provider_mode.lower()
        if provider_mode == "ollama":
            return self.ollama_fallback_model
        if provider_mode == "sglang":
            return self.sglang_fallback_model
        return self.fallback_model

    def ollama_required_free_memory_bytes(self, model: str) -> int:
        """Return the configured host-memory floor for an Ollama model."""
        reserve_gb = self.ollama_min_free_memory_gb
        for entry in self.ollama_model_memory_reserves_gb.split(","):
            if not entry.strip():
                continue
            name, separator, value = entry.partition("=")
            if not separator or not name.strip() or not value.strip():
                raise ValueError(
                    "TEAMSWARM_OLLAMA_MODEL_MEMORY_RESERVES_GB entries must use model=GB."
                )
            if name.strip() == model:
                reserve_gb = max(reserve_gb, float(value.strip()))
        return int(max(0, reserve_gb) * 1024**3)

    def model_for_role(self, role: str, profile: str) -> str:
        configured = {
            "criteria": self.criteria_model,
            "discovery": self.discovery_model,
            "coding": self.coding_model,
            "testing": self.testing_model,
            "evaluator": self.evaluator_model,
        }.get(role)
        return configured or self.model_for(profile)

    def project_roots(self) -> list[Path]:
        return [
            Path(root.strip()).expanduser().resolve()
            for root in self.project_context_roots.split(",")
        ]

    def skills_paths(self) -> list[Path]:
        return [Path(root.strip()).expanduser().resolve() for root in self.skill_roots.split(",")]

    def resolve_workspace(self, requested: str | None) -> Path:
        roots = self.project_roots()
        candidate = Path(requested).expanduser().resolve() if requested else roots[0]
        if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
            raise ValueError("Workspace root is outside TEAMSWARM_PROJECT_CONTEXT_ROOTS.")
        if not candidate.is_dir():
            raise ValueError("Workspace root must be an existing directory.")
        return candidate


@lru_cache
def get_settings() -> Settings:
    return Settings()

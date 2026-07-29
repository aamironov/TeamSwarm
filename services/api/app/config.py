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
    context_token_budget: int = 6_000
    project_context_roots: str = "."
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


@lru_cache
def get_settings() -> Settings:
    return Settings()

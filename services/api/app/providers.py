import asyncio
import os
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI

from .config import get_settings
from .memory import available_memory_bytes


@dataclass(frozen=True)
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    source: str


@dataclass(frozen=True)
class AvailableModelRecord:
    id: str
    provider: str
    location: Literal["local", "remote"]
    availability: Literal["available", "configured", "not_installed"]
    profiles: list[Literal["fast", "strong", "fallback"]]


class ModelProvider:
    async def generate(self, prompt: str, model: str) -> ProviderResult:
        raise NotImplementedError


class MockProvider(ModelProvider):
    async def generate(self, prompt: str, model: str) -> ProviderResult:
        objective = prompt.split("Objective:", maxsplit=1)[-1].strip().splitlines()[0]
        text = f"[{model} mock result] Completed: {objective}"
        return ProviderResult(
            text, max(1, len(prompt) // 4), max(1, len(text) // 4), "internally_metered"
        )


class OpenAIProvider(ModelProvider):
    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when TEAMSWARM_PROVIDER_MODE=openai.")
        self.client = AsyncOpenAI(api_key=api_key)

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        response = await self.client.responses.create(model=model, input=prompt)
        usage = response.usage
        return ProviderResult(
            text=response.output_text,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            source="provider_reported",
        )


_bytez_limiters: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _bytez_limiter(max_concurrency: int) -> asyncio.Semaphore:
    """Share the Bytez account limit across providers on the current event loop."""
    loop = asyncio.get_running_loop()
    by_limit = _bytez_limiters.setdefault(loop, {})
    return by_limit.setdefault(max_concurrency, asyncio.Semaphore(max_concurrency))


class BytezProvider(ModelProvider):
    """Adapter for Bytez's OpenAI-compatible hosted model API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        max_completion_tokens: int | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        settings = get_settings()
        configured_api_key = (
            settings.bytez_api_key.get_secret_value() if settings.bytez_api_key else None
        )
        resolved_api_key = api_key or configured_api_key
        if client is None and not resolved_api_key:
            raise RuntimeError(
                "TEAMSWARM_BYTEZ_API_KEY is required when TEAMSWARM_PROVIDER_MODE=bytez."
            )
        self.base_url = (base_url or settings.bytez_base_url).rstrip("/")
        self.client = client or AsyncOpenAI(
            base_url=self.base_url,
            api_key=resolved_api_key,
        )
        self.max_completion_tokens = max(
            1,
            max_completion_tokens or settings.bytez_max_completion_tokens,
        )
        self.max_concurrency = max(1, max_concurrency or settings.bytez_max_concurrency)

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        async with _bytez_limiter(self.max_concurrency):
            response = await self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=self.max_completion_tokens,
            )
        text = response.choices[0].message.content
        if not isinstance(text, str):
            raise RuntimeError("Bytez returned a response without text content.")
        usage = getattr(response, "usage", None)
        if usage is None:
            return ProviderResult(
                text=text,
                input_tokens=_estimate_tokens(prompt),
                output_tokens=_estimate_tokens(text),
                source="estimated",
            )
        return ProviderResult(
            text=text,
            input_tokens=_as_nonnegative_int(getattr(usage, "prompt_tokens", 0)),
            output_tokens=_as_nonnegative_int(getattr(usage, "completion_tokens", 0)),
            source="provider_reported",
        )


class OpenRouterProvider(ModelProvider):
    """Adapter for OpenRouter's OpenAI-compatible chat API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        max_completion_tokens: int | None = None,
    ) -> None:
        settings = get_settings()
        configured_api_key = (
            settings.openrouter_api_key.get_secret_value()
            if settings.openrouter_api_key
            else None
        )
        resolved_api_key = api_key or configured_api_key
        if client is None and not resolved_api_key:
            raise RuntimeError(
                "TEAMSWARM_OPENROUTER_API_KEY or OPENROUTER_API_KEY is required "
                "when TEAMSWARM_PROVIDER_MODE=openrouter."
            )
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        default_headers = {"X-OpenRouter-Title": settings.openrouter_app_name}
        if settings.openrouter_site_url:
            default_headers["HTTP-Referer"] = settings.openrouter_site_url
        self.client = client or AsyncOpenAI(
            base_url=self.base_url,
            api_key=resolved_api_key,
            default_headers=default_headers,
        )
        self.max_completion_tokens = max(
            1,
            max_completion_tokens or settings.openrouter_max_completion_tokens,
        )

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        response = await self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self.max_completion_tokens,
        )
        text = response.choices[0].message.content
        if not isinstance(text, str):
            raise RuntimeError("OpenRouter returned a response without text content.")
        usage = getattr(response, "usage", None)
        if usage is None:
            return ProviderResult(
                text=text,
                input_tokens=_estimate_tokens(prompt),
                output_tokens=_estimate_tokens(text),
                source="estimated",
            )
        return ProviderResult(
            text=text,
            input_tokens=_as_nonnegative_int(getattr(usage, "prompt_tokens", 0)),
            output_tokens=_as_nonnegative_int(getattr(usage, "completion_tokens", 0)),
            source="provider_reported",
        )


class OllamaProvider(ModelProvider):
    """Adapter for a locally running Ollama server and any pulled model."""

    def __init__(
        self,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        available_memory: Callable[[], int | None] = available_memory_bytes,
    ) -> None:
        self.base_url = (base_url or get_settings().ollama_base_url).rstrip("/")
        self.transport = transport
        self.available_memory = available_memory

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        self._ensure_memory_available(model)
        # The orchestrator owns task timeouts. Do not let HTTPX's shorter default
        # read timeout terminate a local model while it is loading or generating.
        async with httpx.AsyncClient(
            base_url=self.base_url,
            transport=self.transport,
            timeout=None,
        ) as client:
            response = await client.post(
                "/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    # Qwen 3 models otherwise expose a lengthy reasoning trace
                    # as response text, consuming the run's bounded output budget.
                    "think": get_settings().ollama_think,
                },
            )
        response.raise_for_status()
        payload = response.json()
        text = payload.get("response")
        if not isinstance(text, str):
            raise RuntimeError("Ollama returned a response without text content.")
        return ProviderResult(
            text=text,
            input_tokens=_as_nonnegative_int(payload.get("prompt_eval_count")),
            output_tokens=_as_nonnegative_int(payload.get("eval_count")),
            source="provider_reported",
        )

    def _ensure_memory_available(self, model: str) -> None:
        required = get_settings().ollama_required_free_memory_bytes(model)
        if not required:
            return
        available = self.available_memory()
        if available is None:
            raise RuntimeError(
                "Unable to determine available host memory; refusing the configured local model."
            )
        if available < required:
            gib = 1024**3
            raise RuntimeError(
                f"Insufficient host memory for {model}: {available / gib:.1f} GiB available, "
                f"but the configured safety floor is {required / gib:.1f} GiB."
            )

    async def list_models(self) -> list[str]:
        """Return locally pulled models, or an empty list when Ollama is unavailable."""
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                transport=self.transport,
                timeout=2.0,
            ) as client:
                response = await client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        models = response.json().get("models", [])
        return sorted(
            item["name"]
            for item in models
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )


class SGLangProvider(ModelProvider):
    """Adapter for an SGLang server's OpenAI-compatible chat API."""

    def __init__(
        self,
        base_url: str | None = None,
        client: Any | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        server_url = (base_url or get_settings().sglang_base_url).rstrip("/")
        self.base_url = server_url
        self.client = client or AsyncOpenAI(base_url=f"{server_url}/v1", api_key="EMPTY")
        self.transport = transport

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        response = await self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content
        if not isinstance(text, str):
            raise RuntimeError("SGLang returned a response without text content.")
        usage = response.usage
        return ProviderResult(
            text=text,
            input_tokens=_as_nonnegative_int(getattr(usage, "prompt_tokens", 0)),
            output_tokens=_as_nonnegative_int(getattr(usage, "completion_tokens", 0)),
            source="provider_reported",
        )

    async def list_models(self) -> list[str]:
        """Return models reported by SGLang, or an empty list when it is unavailable."""
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                transport=self.transport,
                timeout=2.0,
            ) as client:
                response = await client.get("/v1/models")
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        models = response.json().get("data", [])
        return sorted(
            item["id"]
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )


def _as_nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


async def get_model_catalog() -> list[AvailableModelRecord]:
    """Return configured remote models and models reported by local servers."""
    settings = get_settings()
    remote_profiles: dict[str, list[Literal["fast", "strong", "fallback"]]] = {}
    for profile, model in (
        ("fast", settings.fast_model),
        ("strong", settings.strong_model),
        ("fallback", settings.fallback_model),
    ):
        remote_profiles.setdefault(model, []).append(profile)  # type: ignore[arg-type]

    local_profiles: dict[str, list[Literal["fast", "strong", "fallback"]]] = {}
    for profile, model in (
        ("fast", settings.ollama_fast_model),
        ("strong", settings.ollama_strong_model),
        ("fallback", settings.ollama_fallback_model),
    ):
        local_profiles.setdefault(model, []).append(profile)  # type: ignore[arg-type]
    installed_local_models = await OllamaProvider(settings.ollama_base_url).list_models()
    sglang_profiles: dict[str, list[Literal["fast", "strong", "fallback"]]] = {}
    for profile, model in (
        ("fast", settings.sglang_fast_model),
        ("strong", settings.sglang_strong_model),
        ("fallback", settings.sglang_fallback_model),
    ):
        sglang_profiles.setdefault(model, []).append(profile)  # type: ignore[arg-type]
    installed_sglang_models = await SGLangProvider(settings.sglang_base_url).list_models()
    bytez_profiles: dict[str, list[Literal["fast", "strong", "fallback"]]] = {}
    for profile, model in (
        ("fast", settings.bytez_fast_model),
        ("strong", settings.bytez_strong_model),
        ("fallback", settings.bytez_fallback_model),
    ):
        bytez_profiles.setdefault(model, []).append(profile)  # type: ignore[arg-type]
    openrouter_profiles: dict[str, list[Literal["fast", "strong", "fallback"]]] = {}
    for profile, model in (
        ("fast", settings.openrouter_fast_model),
        ("strong", settings.openrouter_strong_model),
        ("fallback", settings.openrouter_fallback_model),
    ):
        openrouter_profiles.setdefault(model, []).append(profile)  # type: ignore[arg-type]

    catalog = [
        AvailableModelRecord(
            id=model,
            provider="openai",
            location="remote",
            availability="configured",
            profiles=profiles,
        )
        for model, profiles in remote_profiles.items()
    ]
    catalog.extend(
        AvailableModelRecord(
            id=model,
            provider="bytez",
            location="remote",
            availability="configured",
            profiles=profiles,
        )
        for model, profiles in bytez_profiles.items()
    )
    catalog.extend(
        AvailableModelRecord(
            id=model,
            provider="openrouter",
            location="remote",
            availability="configured",
            profiles=profiles,
        )
        for model, profiles in openrouter_profiles.items()
    )
    for model in sorted(set(installed_local_models) | set(local_profiles)):
        catalog.append(
            AvailableModelRecord(
                id=model,
                provider="ollama",
                location="local",
                availability="available" if model in installed_local_models else "not_installed",
                profiles=local_profiles.get(model, []),
            )
        )
    for model in sorted(set(installed_sglang_models) | set(sglang_profiles)):
        catalog.append(
            AvailableModelRecord(
                id=model,
                provider="sglang",
                location="local",
                availability="available" if model in installed_sglang_models else "not_installed",
                profiles=sglang_profiles.get(model, []),
            )
        )
    return catalog


def get_provider() -> ModelProvider:
    provider_mode = get_settings().provider_mode.lower()
    if provider_mode == "openai":
        return OpenAIProvider()
    if provider_mode == "ollama":
        return OllamaProvider()
    if provider_mode == "sglang":
        return SGLangProvider()
    if provider_mode == "bytez":
        return BytezProvider()
    if provider_mode == "openrouter":
        return OpenRouterProvider()
    if provider_mode == "mock":
        return MockProvider()
    raise ValueError(f"Unsupported TEAMSWARM_PROVIDER_MODE: {provider_mode}")

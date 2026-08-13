import asyncio
import json

import httpx
import pytest

from services.api.app import providers
from services.api.app.config import Settings
from services.api.app.providers import (
    BytezProvider,
    MockProvider,
    OllamaProvider,
    OpenRouterProvider,
    SGLangProvider,
)


@pytest.mark.asyncio
async def test_mock_provider_reports_internal_usage() -> None:
    result = await MockProvider().generate("Objective: Test mock provider", "gpt-5.6-terra")
    assert "Completed" in result.text
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.source == "internally_metered"


@pytest.mark.asyncio
async def test_bytez_provider_uses_openai_compatible_chat_api() -> None:
    class FakeCompletions:
        async def create(self, **kwargs):
            assert kwargs == {
                "model": "Qwen/Qwen3-4B",
                "messages": [{"role": "user", "content": "Complete the task."}],
                "max_tokens": 512,
            }
            message = type("Message", (), {"content": "Bytez result"})()
            choice = type("Choice", (), {"message": message})()
            usage = type("Usage", (), {"prompt_tokens": 11, "completion_tokens": 7})()
            return type("Response", (), {"choices": [choice], "usage": usage})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": FakeCompletions()})()},
    )()
    result = await BytezProvider(
        client=client,
        max_completion_tokens=512,
    ).generate("Complete the task.", "Qwen/Qwen3-4B")

    assert result.text == "Bytez result"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.source == "provider_reported"


@pytest.mark.asyncio
async def test_bytez_provider_estimates_usage_when_response_omits_it() -> None:
    class FakeCompletions:
        async def create(self, **kwargs):
            message = type("Message", (), {"content": "Estimated Bytez result"})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice], "usage": None})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": FakeCompletions()})()},
    )()
    result = await BytezProvider(client=client).generate(
        "Complete the task.", "Qwen/Qwen3-4B"
    )

    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.source == "estimated"


def test_bytez_provider_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAMSWARM_BYTEZ_API_KEY", raising=False)
    monkeypatch.setattr(
        providers,
        "get_settings",
        lambda: Settings(_env_file=None),
    )

    with pytest.raises(RuntimeError, match="TEAMSWARM_BYTEZ_API_KEY"):
        BytezProvider()


def test_bytez_api_key_loads_from_the_project_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TEAMSWARM_BYTEZ_API_KEY=test-key\n")

    settings = Settings(_env_file=env_file)

    assert settings.bytez_api_key is not None
    assert settings.bytez_api_key.get_secret_value() == "test-key"
    assert "test-key" not in repr(settings)


@pytest.mark.asyncio
async def test_bytez_provider_shares_its_concurrency_limit_across_instances() -> None:
    class FakeCompletions:
        active = 0
        max_active = 0

        async def create(self, **kwargs):
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            await asyncio.sleep(0.01)
            type(self).active -= 1
            message = type("Message", (), {"content": "Bytez result"})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice], "usage": None})()

    def client():
        return type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": FakeCompletions()})()},
        )()

    first = BytezProvider(client=client(), max_concurrency=1)
    second = BytezProvider(client=client(), max_concurrency=1)

    await asyncio.gather(
        first.generate("First task.", "Qwen/Qwen3-4B"),
        second.generate("Second task.", "Qwen/Qwen3-4B"),
    )

    assert FakeCompletions.max_active == 1


def test_get_provider_selects_bytez(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        providers,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            provider_mode="bytez",
            bytez_api_key="test-key",
        ),
    )

    assert isinstance(providers.get_provider(), BytezProvider)


@pytest.mark.asyncio
async def test_openrouter_provider_uses_openai_compatible_chat_api() -> None:
    class FakeCompletions:
        async def create(self, **kwargs):
            assert kwargs == {
                "model": "openrouter/free",
                "messages": [{"role": "user", "content": "Complete the task."}],
                "max_tokens": 512,
            }
            message = type("Message", (), {"content": "OpenRouter result"})()
            choice = type("Choice", (), {"message": message})()
            usage = type("Usage", (), {"prompt_tokens": 13, "completion_tokens": 8})()
            return type("Response", (), {"choices": [choice], "usage": usage})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": FakeCompletions()})()},
    )()
    result = await OpenRouterProvider(
        client=client,
        max_completion_tokens=512,
    ).generate("Complete the task.", "openrouter/free")

    assert result.text == "OpenRouter result"
    assert result.input_tokens == 13
    assert result.output_tokens == 8
    assert result.source == "provider_reported"


def test_openrouter_provider_configures_attribution_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(providers, "AsyncOpenAI", fake_client)
    monkeypatch.setattr(
        providers,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            openrouter_api_key="test-key",
            openrouter_site_url="https://teamswarm.example",
            openrouter_app_name="TeamSwarm Test",
        ),
    )

    OpenRouterProvider()

    assert captured == {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "test-key",
        "default_headers": {
            "X-OpenRouter-Title": "TeamSwarm Test",
            "HTTP-Referer": "https://teamswarm.example",
        },
    }


def test_openrouter_provider_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAMSWARM_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        providers,
        "get_settings",
        lambda: Settings(_env_file=None),
    )

    with pytest.raises(RuntimeError, match="TEAMSWARM_OPENROUTER_API_KEY"):
        OpenRouterProvider()


def test_openrouter_api_key_loads_from_its_standard_env_name(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=test-key\n")

    settings = Settings(_env_file=env_file)

    assert settings.openrouter_api_key is not None
    assert settings.openrouter_api_key.get_secret_value() == "test-key"
    assert "test-key" not in repr(settings)


def test_get_provider_selects_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        providers,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            provider_mode="openrouter",
            openrouter_api_key="test-key",
        ),
    )

    assert isinstance(providers.get_provider(), OpenRouterProvider)


@pytest.mark.asyncio
async def test_model_catalog_includes_configured_openrouter_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_models(self) -> list[str]:
        return []

    monkeypatch.setattr(providers.OllamaProvider, "list_models", no_models)
    monkeypatch.setattr(providers.SGLangProvider, "list_models", no_models)
    monkeypatch.setattr(
        providers,
        "get_settings",
        lambda: Settings(_env_file=None),
    )

    catalog = await providers.get_model_catalog()
    openrouter = next(model for model in catalog if model.provider == "openrouter")

    assert openrouter.id == "openrouter/free"
    assert openrouter.location == "remote"
    assert openrouter.availability == "configured"
    assert openrouter.profiles == ["fast", "strong", "fallback"]


@pytest.mark.asyncio
async def test_ollama_provider_uses_non_streaming_generate_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        assert json.loads(request.content) == {
            "model": "llama3.2:3b",
            "prompt": "Complete the task.",
            "stream": False,
            "think": False,
        }
        return httpx.Response(
            200,
            json={"response": "Local result", "prompt_eval_count": 11, "eval_count": 7},
        )

    provider = OllamaProvider(
        base_url="http://ollama.test",
        transport=httpx.MockTransport(handler),
        available_memory=lambda: 100 * 1024**3,
    )
    result = await provider.generate("Complete the task.", "llama3.2:3b")

    assert result.text == "Local result"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.source == "provider_reported"


@pytest.mark.asyncio
async def test_ollama_provider_rejects_a_model_before_request_when_memory_is_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            ollama_model_memory_reserves_gb="qwen3-coder-next:latest=64",
        ),
    )
    provider = OllamaProvider(
        base_url="http://ollama.test",
        available_memory=lambda: 32 * 1024**3,
    )

    with pytest.raises(RuntimeError, match="Insufficient host memory"):
        await provider.generate("Complete the task.", "qwen3-coder-next:latest")


@pytest.mark.asyncio
async def test_ollama_provider_lists_only_installed_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.2:3b"}]})

    provider = OllamaProvider(
        base_url="http://ollama.test",
        transport=httpx.MockTransport(handler),
    )

    assert await provider.list_models() == ["llama3.2:3b", "qwen3:8b"]


@pytest.mark.asyncio
async def test_sglang_provider_uses_openai_compatible_chat_api() -> None:
    class FakeCompletions:
        async def create(self, **kwargs):
            assert kwargs == {
                "model": "Qwen/Qwen3-4B",
                "messages": [{"role": "user", "content": "Complete the task."}],
            }
            message = type("Message", (), {"content": "SGLang result"})()
            choice = type("Choice", (), {"message": message})()
            usage = type("Usage", (), {"prompt_tokens": 11, "completion_tokens": 7})()
            return type("Response", (), {"choices": [choice], "usage": usage})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": FakeCompletions()})()},
    )()
    result = await SGLangProvider(base_url="http://sglang.test", client=client).generate(
        "Complete the task.", "Qwen/Qwen3-4B"
    )

    assert result.text == "SGLang result"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.source == "provider_reported"


@pytest.mark.asyncio
async def test_sglang_provider_lists_models_from_openai_models_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "Qwen/Qwen3-4B"}, {"id": "Qwen/Qwen3-8B"}]},
        )

    provider = SGLangProvider(
        base_url="http://sglang.test",
        transport=httpx.MockTransport(handler),
    )

    assert await provider.list_models() == ["Qwen/Qwen3-4B", "Qwen/Qwen3-8B"]

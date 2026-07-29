import json

import httpx
import pytest

from services.api.app.providers import MockProvider, OllamaProvider, SGLangProvider


@pytest.mark.asyncio
async def test_mock_provider_reports_internal_usage() -> None:
    result = await MockProvider().generate("Objective: Test mock provider", "gpt-5.6-terra")
    assert "Completed" in result.text
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.source == "internally_metered"


@pytest.mark.asyncio
async def test_ollama_provider_uses_non_streaming_generate_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        assert json.loads(request.content) == {
            "model": "llama3.2:3b",
            "prompt": "Complete the task.",
            "stream": False,
        }
        return httpx.Response(
            200,
            json={"response": "Local result", "prompt_eval_count": 11, "eval_count": 7},
        )

    provider = OllamaProvider(
        base_url="http://ollama.test",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.generate("Complete the task.", "llama3.2:3b")

    assert result.text == "Local result"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.source == "provider_reported"


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

from services.api.app.config import Settings
from services.api.app.routing import route_task


def test_ollama_route_marks_model_as_local_and_preserves_fallback() -> None:
    settings = Settings(provider_mode="ollama")

    route = route_task(settings, "strong", 12_000)

    assert route.location == "local"
    assert route.model == "qwen3:8b"
    assert route.fallback_model == "qwen3:8b"
    assert route.estimated_context_tokens == 3_000


def test_openai_fast_route_marks_model_as_remote() -> None:
    settings = Settings(provider_mode="openai")

    route = route_task(settings, "fast", 400)

    assert route.location == "remote"
    assert route.model == settings.fast_model
    assert "fast profile" in route.reason


def test_sglang_route_marks_model_as_local_and_preserves_fallback() -> None:
    settings = Settings(provider_mode="sglang")

    route = route_task(settings, "strong", 400)

    assert route.location == "local"
    assert route.model == "Qwen/Qwen3-8B"
    assert route.fallback_model == "Qwen/Qwen3-8B"

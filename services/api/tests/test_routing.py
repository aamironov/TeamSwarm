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


def test_bytez_route_uses_remote_free_tier_models() -> None:
    settings = Settings(provider_mode="bytez")

    route = route_task(settings, "strong", 400)

    assert route.location == "remote"
    assert route.model == "Qwen/Qwen3-4B"
    assert route.fallback_model == "Qwen/Qwen3-4B"


def test_openrouter_route_uses_remote_free_models_router() -> None:
    settings = Settings(provider_mode="openrouter")

    route = route_task(settings, "strong", 400)

    assert route.location == "remote"
    assert route.model == "openrouter/free"
    assert route.fallback_model == "openrouter/free"


def test_delivery_roles_can_route_to_distinct_local_models() -> None:
    settings = Settings(
        provider_mode="ollama",
        criteria_model="llama3.2:3b",
        discovery_model="qwen3:8b",
        coding_model="qwen3-coder-next:latest",
        testing_model="qwen3:8b",
        evaluator_model="qwen3:8b",
    )

    assert settings.model_for_role("criteria", "fast") == "llama3.2:3b"
    assert settings.model_for_role("discovery", "strong") == "qwen3:8b"
    assert settings.model_for_role("coding", "strong") == "qwen3-coder-next:latest"
    assert settings.model_for_role("testing", "strong") == "qwen3:8b"
    assert settings.model_for_role("evaluator", "strong") == "qwen3:8b"

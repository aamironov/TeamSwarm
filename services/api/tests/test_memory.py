from services.api.app.config import Settings


def test_model_specific_memory_floor_overrides_the_general_floor() -> None:
    settings = Settings(
        _env_file=None,
        ollama_min_free_memory_gb=4,
        ollama_model_memory_reserves_gb="llama3.2:3b=6,qwen3:8b=12",
    )

    assert settings.ollama_required_free_memory_bytes("llama3.2:3b") == 6 * 1024**3
    assert settings.ollama_required_free_memory_bytes("qwen3:8b") == 12 * 1024**3
    assert settings.ollama_required_free_memory_bytes("other") == 4 * 1024**3


def test_invalid_model_memory_floor_is_rejected() -> None:
    settings = Settings(_env_file=None, ollama_model_memory_reserves_gb="qwen3:8b")

    try:
        settings.ollama_required_free_memory_bytes("qwen3:8b")
    except ValueError as error:
        assert "model=GB" in str(error)
    else:
        raise AssertionError("Malformed model memory floor was accepted")

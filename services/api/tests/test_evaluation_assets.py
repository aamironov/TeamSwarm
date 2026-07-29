from pathlib import Path


def test_promptfoo_baseline_covers_local_models_and_contract_checks() -> None:
    root = Path("evals")
    config = (root / "promptfooconfig.yaml").read_text()
    cases = (root / "cases" / "baseline.yaml").read_text()

    assert "ollama:chat:llama3.2:3b" in config
    assert "ollama:chat:qwen3:8b" in config
    assert "type: javascript" in cases
    assert "trade-off" in cases

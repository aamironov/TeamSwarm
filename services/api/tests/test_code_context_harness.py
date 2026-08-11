"""Progress measurements for the bounded repository-context retrieval path.

These are deliberately deterministic apart from the reported elapsed time: token
reduction is a release gate, while throughput is an observable benchmark rather
than a machine-dependent pass/fail threshold.
"""

from __future__ import annotations

from time import perf_counter

from services.api.app.code_context import RepositoryCodeIndex
from services.api.app.context import ContextItem, ContextOptimizer


def _write_workspace(root) -> None:
    for number in range(24):
        (root / f"unrelated_{number}.py").write_text(
            f"def unrelated_feature_{number}(value):\n"
            "    notes = 'unrelated implementation detail ' * 60\n"
            "    return f'{value}:{notes}'\n",
            encoding="utf-8",
        )
    (root / "orders.py").write_text(
        "def normalize_payload(raw):\n"
        "    return raw.strip()\n\n"
        "def process_order(raw):\n"
        "    return normalize_payload(raw)\n",
        encoding="utf-8",
    )


def test_repository_context_harness_measures_cached_transfer_throughput_and_token_savings(
    tmp_path,
) -> None:
    """The retrieval optimization must preserve relevant code while sending less context."""
    _write_workspace(tmp_path)
    index = RepositoryCodeIndex.build(tmp_path)
    full_repository_tokens = sum(
        ContextItem(
            id=path.relative_to(tmp_path).as_posix(),
            source="workspace_file",
            text=path.read_text(encoding="utf-8"),
            priority=1,
            authority=1,
        ).token_estimate
        for path in sorted(tmp_path.glob("*.py"))
    )

    # The runtime caches the index, so measure only the per-task retrieval and
    # bounded context packaging that happen on a cache hit.
    started = perf_counter()
    retrieved = index.retrieve("Fix process_order payload normalization", limit=2)
    semantic_retrieved = index.retrieve("Repair normalization", limit=2)
    optimized = ContextOptimizer().optimize(retrieved, budget_tokens=200)
    elapsed_seconds = perf_counter() - started

    transferred_tokens = optimized.manifest.estimated_tokens
    tokens_saved = full_repository_tokens - transferred_tokens
    token_savings_percent = tokens_saved / full_repository_tokens * 100
    tokens_per_second = transferred_tokens / elapsed_seconds
    selected_symbols = {item.metadata["symbol"] for item in retrieved}
    semantic_symbols = {item.metadata["symbol"] for item in semantic_retrieved}

    print(
        "Context retrieval harness: "
        f"full_repository_tokens={full_repository_tokens}, "
        f"transferred_tokens={transferred_tokens}, "
        f"tokens_saved={tokens_saved}, "
        f"token_savings_percent={token_savings_percent:.2f}, "
        f"token_transfer_tokens_per_second={tokens_per_second:.2f}, "
        f"semantic_target_recall={int('normalize_payload' in semantic_symbols)}"
    )

    assert "process_order" in selected_symbols
    assert "normalize_payload" in semantic_symbols
    assert transferred_tokens <= 200
    assert tokens_saved > 0
    assert token_savings_percent >= 90
    assert tokens_per_second > 0

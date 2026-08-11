from services.api.app.code_context import RepositoryCodeIndex


def test_code_index_combines_symbol_search_with_call_graph_expansion(tmp_path) -> None:
    source = tmp_path / "orders.py"
    source.write_text(
        "def normalize_payload(raw):\n"
        "    return raw.strip()\n\n"
        "def process_order(raw):\n"
        "    return normalize_payload(raw)\n",
        encoding="utf-8",
    )

    index = RepositoryCodeIndex.build(tmp_path)
    results = index.retrieve("Update process_order", limit=2)

    assert [item.metadata["symbol"] for item in results] == [
        "process_order",
        "normalize_payload",
    ]
    assert all(item.source == "workspace_code" for item in results)
    assert results[0].metadata["path"] == "orders.py"
    assert results[0].metadata["index_revision"] == index.revision


def test_code_index_revision_changes_when_workspace_changes(tmp_path) -> None:
    source = tmp_path / "feature.py"
    source.write_text("def before():\n    return 1\n", encoding="utf-8")
    first = RepositoryCodeIndex.workspace_revision(tmp_path)

    source.write_text("def after_change():\n    return 200\n", encoding="utf-8")
    second = RepositoryCodeIndex.workspace_revision(tmp_path)

    assert first != second


def test_hybrid_index_recalls_morphological_matches_and_records_signal_provenance(
    tmp_path,
) -> None:
    source = tmp_path / "payloads.py"
    source.write_text(
        "def normalize_payload(raw):\n"
        "    return raw.strip().lower()\n\n"
        "def process_order(raw):\n"
        "    return normalize_payload(raw)\n",
        encoding="utf-8",
    )

    index = RepositoryCodeIndex.build(tmp_path)
    results = index.retrieve("Repair normalization", limit=2)

    assert [item.metadata["symbol"] for item in results] == [
        "normalize_payload",
        "process_order",
    ]
    assert results[0].metadata["retrieval_signal"] == "semantic"
    assert "semantic" in results[0].metadata["retrieval_signals"]
    assert "graph" in results[1].metadata["retrieval_signals"]
    assert results[0].metadata["embedding_version"] == "feature-hash-word-trigram-v1"
    assert results[0].metadata["index_version"] == "python-hybrid-graph-v2"
    assert results[0].metadata["index_revision"] == index.revision
    assert results[0].metadata["path"] == "payloads.py"
    assert results[0].metadata["start_line"] == "1"
    assert int(results[0].metadata["end_line"]) >= 2
    assert float(results[0].metadata["retrieval_score"]) > 0
    assert results[0].metadata["contextual_header"] in results[0].text


def test_hybrid_index_deduplicates_identical_chunks_before_bounded_reranking(
    tmp_path,
) -> None:
    duplicate = "def shared_helper(value):\n    return value.strip()\n"
    (tmp_path / "first.py").write_text(duplicate, encoding="utf-8")
    (tmp_path / "second.py").write_text(duplicate, encoding="utf-8")

    index = RepositoryCodeIndex.build(tmp_path)
    results = index.retrieve("shared_helper", limit=10)

    assert len(results) == 1
    assert results[0].metadata["symbol"] == "shared_helper"


def test_hybrid_index_caps_the_reranking_candidate_pool(tmp_path) -> None:
    for number in range(40):
        (tmp_path / f"shared_{number}.py").write_text(
            f"def shared_helper_{number}(value):\n    return value + {number}\n",
            encoding="utf-8",
        )

    index = RepositoryCodeIndex.build(tmp_path)
    results = index.retrieve("shared helper", limit=100)

    assert len(results) == index.max_rerank_candidates

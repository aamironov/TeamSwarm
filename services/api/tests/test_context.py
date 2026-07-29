from services.api.app.context import ContextItem, ContextOptimizer


def test_context_optimizer_keeps_required_high_signal_items_and_omits_low_priority_items() -> None:
    optimizer = ContextOptimizer()
    result = optimizer.optimize(
        [
            ContextItem(
                id="required",
                source="attachment",
                text="Critical requirements " * 20,
                priority=100,
                authority=100,
                required=True,
            ),
            ContextItem(
                id="high",
                source="handoff",
                text="High signal handoff " * 10,
                priority=90,
                authority=90,
            ),
            ContextItem(
                id="low",
                source="log",
                text="Low priority output " * 40,
                priority=1,
                authority=1,
            ),
        ],
        budget_tokens=180,
    )

    selected_ids = {item["id"] for item in result.manifest.selected}
    omitted_ids = {item["id"] for item in result.manifest.omitted}

    assert "required" in selected_ids
    assert "high" in selected_ids
    assert "low" in omitted_ids
    assert result.manifest.estimated_tokens <= result.manifest.budget_tokens


def test_context_optimizer_deduplicates_equal_content_with_provenance() -> None:
    optimizer = ContextOptimizer()
    result = optimizer.optimize(
        [
            ContextItem("first", "artifact", "same evidence", 10, 10),
            ContextItem("second", "artifact", "same evidence", 10, 10),
        ],
        budget_tokens=100,
    )

    assert [item["id"] for item in result.manifest.selected] == ["first"]
    assert result.manifest.omitted[0]["id"] == "second"
    assert result.manifest.omitted[0]["disposition"] == "duplicate"

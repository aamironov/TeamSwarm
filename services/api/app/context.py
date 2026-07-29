"""Deterministic, provenance-preserving context selection for model calls."""

import hashlib
from dataclasses import dataclass, field


def estimate_tokens(text: str) -> int:
    """Conservative MVP estimate until provider tokenizer accounting is available."""
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class ContextItem:
    id: str
    source: str
    text: str
    priority: int
    authority: int
    required: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class ContextManifest:
    selected: list[dict[str, object]]
    omitted: list[dict[str, object]]
    estimated_tokens: int
    budget_tokens: int
    optimizer_version: str = "deterministic-v1"


@dataclass(frozen=True)
class OptimizedContext:
    text: str
    manifest: ContextManifest


class ContextOptimizer:
    """Select the smallest high-signal, policy-approved evidence package."""

    version = "deterministic-v1"

    def optimize(self, items: list[ContextItem], budget_tokens: int) -> OptimizedContext:
        deduplicated: list[ContextItem] = []
        omitted: list[dict[str, object]] = []
        seen_hashes: set[str] = set()
        for item in items:
            if item.content_hash in seen_hashes:
                omitted.append(self._manifest_item(item, "duplicate"))
                continue
            seen_hashes.add(item.content_hash)
            deduplicated.append(item)

        ordered = sorted(
            deduplicated,
            key=lambda item: (not item.required, -(item.priority + item.authority), item.id),
        )
        selected: list[ContextItem] = []
        used_tokens = 0
        for item in ordered:
            tokens = item.token_estimate
            if used_tokens + tokens <= budget_tokens:
                selected.append(item)
                used_tokens += tokens
                continue
            remaining = budget_tokens - used_tokens
            if (item.required or item.priority >= 80) and remaining >= 32:
                excerpt = self._excerpt(item, remaining)
                selected.append(excerpt)
                used_tokens += excerpt.token_estimate
                omitted.append(self._manifest_item(item, "compacted"))
            else:
                omitted.append(self._manifest_item(item, "budget"))

        manifest = ContextManifest(
            selected=[self._manifest_item(item, "selected") for item in selected],
            omitted=omitted,
            estimated_tokens=used_tokens,
            budget_tokens=budget_tokens,
            optimizer_version=self.version,
        )
        return OptimizedContext(
            text="\n\n".join(self._render_item(item) for item in selected),
            manifest=manifest,
        )

    @staticmethod
    def _excerpt(item: ContextItem, budget_tokens: int) -> ContextItem:
        characters = max(1, budget_tokens * 4 - 80)
        text = (
            item.text[:characters]
            + "\n[Context compacted; inspect source artifact for full content.]"
        )
        return ContextItem(
            id=item.id,
            source=item.source,
            text=text,
            priority=item.priority,
            authority=item.authority,
            required=item.required,
            metadata={**item.metadata, "compacted": "true"},
        )

    @staticmethod
    def _render_item(item: ContextItem) -> str:
        if item.source == "attached_file":
            return f"--- Attached file: {item.metadata['filename']} ---\n{item.text}"
        provenance = " ".join(f"{key}={value}" for key, value in sorted(item.metadata.items()))
        return f"--- {item.source} [{item.id}] {provenance} ---\n{item.text}"

    @staticmethod
    def _manifest_item(item: ContextItem, disposition: str) -> dict[str, object]:
        return {
            "id": item.id,
            "source": item.source,
            "content_hash": item.content_hash,
            "token_estimate": item.token_estimate,
            "required": item.required,
            "disposition": disposition,
            "metadata": item.metadata,
        }

"""Bounded hybrid retrieval for Python repository context."""

from __future__ import annotations

import ast
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .context import ContextItem

TERM_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SKIPPED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "site-packages",
    "venv",
}


def _terms(value: str) -> set[str]:
    terms: set[str] = set()
    for token in TERM_PATTERN.findall(value):
        lowered = token.casefold()
        terms.add(lowered)
        terms.update(part for part in lowered.split("_") if part)
    return terms


class EmbeddingProvider(Protocol):
    """Versioned embedding contract used by repository indexes and queries."""

    version: str

    def embed(self, value: str) -> tuple[float, ...]: ...


class HashingEmbedder:
    """Dependency-free embeddings for local, deterministic semantic recall.

    Word features preserve exact concepts while character trigrams connect
    morphological variants such as ``normalize`` and ``normalization``. The
    interface can later be replaced by a learned embedding adapter without
    changing index or manifest contracts.
    """

    version = "feature-hash-word-trigram-v1"

    def __init__(self, dimensions: int = 384) -> None:
        if dimensions <= 0:
            raise ValueError("Embedding dimensions must be positive.")
        self.dimensions = dimensions

    def embed(self, value: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimensions
        words = sorted(_terms(value))
        features: list[tuple[str, float]] = [(f"word:{word}", 2.0) for word in words]
        for word in words:
            if len(word) >= 4:
                padded = f"^{word}$"
                features.extend(
                    (f"tri:{padded[index:index + 3]}", 0.35)
                    for index in range(len(padded) - 2)
                )
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign * weight
        magnitude = math.sqrt(sum(component * component for component in vector))
        if magnitude == 0:
            return tuple(vector)
        return tuple(component / magnitude for component in vector)


def _similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding dimensions do not match.")
    return max(0.0, sum(a * b for a, b in zip(left, right)))


@dataclass(frozen=True)
class CodeNode:
    id: str
    path: str
    symbol: str
    kind: str
    text: str
    contextual_header: str
    embedding: tuple[float, ...]
    start_line: int
    end_line: int
    references: frozenset[str]


class RepositoryCodeIndex:
    """In-memory hybrid index with lexical, embedding, and graph rankings."""

    version = "python-hybrid-graph-v2"
    reciprocal_rank_constant = 60
    semantic_floor = 0.08
    max_rerank_candidates = 24

    def __init__(
        self,
        root: Path,
        nodes: list[CodeNode],
        revision: str,
        embedder: EmbeddingProvider,
    ) -> None:
        self.root = root
        self.nodes = nodes
        self.revision = revision
        self.embedding_version = embedder.version
        self._embedder = embedder
        self._by_symbol: dict[str, set[str]] = {}
        self._by_id = {node.id: node for node in nodes}
        self._callers_by_symbol: dict[str, set[str]] = {}
        for node in nodes:
            self._by_symbol.setdefault(node.symbol.casefold(), set()).add(node.id)
            for reference in node.references:
                self._callers_by_symbol.setdefault(reference, set()).add(node.id)

    @staticmethod
    def _paths(root: Path, max_files: int) -> list[Path]:
        return [
            path
            for path in sorted(root.rglob("*.py"))
            if not any(part in SKIPPED_PARTS for part in path.relative_to(root).parts)
        ][:max_files]

    @classmethod
    def workspace_revision(cls, root: Path, max_files: int = 500) -> str:
        root = root.resolve()
        revision_hasher = hashlib.sha256()
        for path in cls._paths(root, max_files):
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            revision_hasher.update(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}".encode())
        return revision_hasher.hexdigest()

    @classmethod
    def build(
        cls,
        root: Path,
        max_files: int = 500,
        embedder: EmbeddingProvider | None = None,
    ) -> RepositoryCodeIndex:
        root = root.resolve()
        embedder = embedder or HashingEmbedder()
        paths = cls._paths(root, max_files)
        revision = cls.workspace_revision(root, max_files)
        nodes: list[CodeNode] = []
        for path in paths:
            relative = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            lines = source.splitlines()
            for item in ast.walk(tree):
                if not isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                start = item.lineno
                end = min(getattr(item, "end_lineno", start), start + 119)
                excerpt = "\n".join(lines[start - 1 : end])[:12_000]
                references = frozenset(
                    child.func.id.casefold()
                    for child in ast.walk(item)
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                )
                kind = "class" if isinstance(item, ast.ClassDef) else "function"
                signature = lines[start - 1].strip() if lines else item.name
                docstring = (ast.get_docstring(item) or "").splitlines()
                summary = f" Purpose: {docstring[0][:300]}" if docstring else ""
                header = (
                    f"Python {kind} {item.name} in {relative}. "
                    f"Signature: {signature}.{summary}"
                )
                nodes.append(
                    CodeNode(
                        id=f"{relative}:{item.name}:{start}",
                        path=relative,
                        symbol=item.name,
                        kind=kind,
                        text=excerpt,
                        contextual_header=header,
                        embedding=embedder.embed(f"{header}\n{excerpt}"),
                        start_line=start,
                        end_line=end,
                        references=references,
                    )
                )
        return cls(root, nodes, revision, embedder)

    @staticmethod
    def _rank(scores: dict[str, float]) -> list[str]:
        return [
            item_id
            for item_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            if score > 0
        ]

    def _graph_scores(self, seed_ids: list[str]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for seed_rank, seed_id in enumerate(seed_ids, start=1):
            seed = self._by_id[seed_id]
            neighbor_ids = set(self._callers_by_symbol.get(seed.symbol.casefold(), set()))
            for reference in seed.references:
                neighbor_ids.update(self._by_symbol.get(reference, set()))
            for neighbor_id in neighbor_ids:
                if neighbor_id != seed_id:
                    scores[neighbor_id] = max(scores.get(neighbor_id, 0), 1 / seed_rank)
        return scores

    def retrieve(
        self,
        query: str,
        limit: int = 6,
        embedder: EmbeddingProvider | None = None,
    ) -> list[ContextItem]:
        query_terms = _terms(query)
        if not query_terms or not self.nodes or limit <= 0:
            return []
        embedder = embedder or self._embedder
        if embedder.version != self.embedding_version:
            raise ValueError("Query embedder version does not match the repository index.")
        query_embedding = embedder.embed(query)
        lexical_scores: dict[str, float] = {}
        semantic_scores: dict[str, float] = {}
        exact_scores: dict[str, float] = {}
        for node in self.nodes:
            symbol_terms = _terms(node.symbol)
            path_terms = _terms(node.path)
            text_terms = _terms(node.text)
            lexical = (
                8 * len(query_terms & symbol_terms)
                + 4 * len(query_terms & path_terms)
                + len(query_terms & text_terms)
            )
            exact = 1.0 if node.symbol.casefold() in query.casefold() else 0.0
            if exact:
                lexical += 12
                exact_scores[node.id] = exact
            if lexical:
                lexical_scores[node.id] = float(lexical)
            semantic = _similarity(query_embedding, node.embedding)
            if semantic >= self.semantic_floor:
                semantic_scores[node.id] = semantic

        seed_ids = list(
            dict.fromkeys(self._rank(lexical_scores)[:3] + self._rank(semantic_scores)[:3])
        )
        graph_scores = self._graph_scores(seed_ids)
        rankings = {
            "lexical": self._rank(lexical_scores),
            "semantic": self._rank(semantic_scores),
            "graph": self._rank(graph_scores),
        }
        weights = {"lexical": 1.0, "semantic": 0.9, "graph": 0.75}
        fused_scores: dict[str, float] = {}
        for signal, ranking in rankings.items():
            for rank, item_id in enumerate(ranking, start=1):
                fused_scores[item_id] = fused_scores.get(item_id, 0) + weights[signal] / (
                    self.reciprocal_rank_constant + rank
                )

        pool_limit = min(self.max_rerank_candidates, max(limit, limit * 4))
        fused_ranking = self._rank(fused_scores)[:pool_limit]
        max_lexical = max(lexical_scores.values(), default=1.0)
        max_graph = max(graph_scores.values(), default=1.0)
        reranked: list[tuple[float, str]] = []
        seen_content: set[str] = set()
        for item_id in fused_ranking:
            node = self._by_id[item_id]
            content_hash = hashlib.sha256(node.text.encode()).hexdigest()
            if content_hash in seen_content:
                continue
            seen_content.add(content_hash)
            rerank_score = (
                fused_scores[item_id] * 20
                + lexical_scores.get(item_id, 0) / max_lexical
                + semantic_scores.get(item_id, 0)
                + 0.15 * graph_scores.get(item_id, 0) / max_graph
                + 2 * exact_scores.get(item_id, 0)
            )
            reranked.append((rerank_score, item_id))
        reranked.sort(key=lambda item: (-item[0], self._by_id[item[1]].path, item[1]))

        results: list[ContextItem] = []
        for rerank_score, item_id in reranked[:limit]:
            node = self._by_id[item_id]
            signals = {
                "exact": exact_scores.get(item_id, 0),
                "graph": graph_scores.get(item_id, 0) / max_graph,
                "lexical": lexical_scores.get(item_id, 0) / max_lexical,
                "semantic": semantic_scores.get(item_id, 0),
            }
            active_signals = [name for name, score in signals.items() if score > 0]
            winning_signal = max(signals, key=signals.get)
            results.append(
                ContextItem(
                    id=f"code:{node.id}",
                    source="workspace_code",
                    text=f"[{node.contextual_header}]\n{node.text}",
                    priority=min(79, 40 + round(rerank_score * 10)),
                    authority=60,
                    metadata={
                        "contextual_header": node.contextual_header,
                        "embedding_version": self.embedding_version,
                        "end_line": str(node.end_line),
                        "index_revision": self.revision,
                        "index_version": self.version,
                        "kind": node.kind,
                        "path": node.path,
                        "retrieval_score": f"{rerank_score:.6f}",
                        "retrieval_signal": winning_signal,
                        "retrieval_signals": ",".join(active_signals),
                        "start_line": str(node.start_line),
                        "symbol": node.symbol,
                    },
                )
            )
        return results

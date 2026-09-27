"""Reciprocal rank fusion.

Twelve lines, no hyperparameters worth tuning, and it consistently beats either
input. The reason it is preferred over a weighted score blend is that dense cosine
and BM25 scores are on incomparable scales: normalising them requires a tuning
constant per corpus, and the tuning would have to be redone whenever the corpus
or embedder changed. RRF only reads rank order, so it is scale-free.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional, Sequence


def rrf(
    ranked_lists: Sequence[Sequence[str]],
    k: int = 60,
    weights: Optional[Sequence[float]] = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists. Returns (id, score) sorted by descending score.

    `k` damps the contribution of top ranks so a single list cannot dominate; 60
    is the value from the original Cormack et al. paper and is not worth tuning.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights must match the number of ranked lists")

    scores: dict[str, float] = defaultdict(float)
    for ranked, weight in zip(ranked_lists, weights):
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] += weight / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def rank_positions(ranked: Sequence[str]) -> dict[str, int]:
    """id -> 0-based rank, for reporting which retriever found what."""
    return {doc_id: i for i, doc_id in enumerate(ranked)}

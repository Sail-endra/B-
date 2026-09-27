"""BM25 sparse retrieval.

This is here because the corpus is notation-heavy. "Cobb-Douglas", "IS-LM",
"Solow residual", "amortized analysis", "Fibonacci heap" are exact strings a
student types, and a dense encoder handles them only adequately -- especially the
local LSA one, where a rare hyphenated term is largely absorbed by the reduction.
BM25 matches them exactly. The complement runs the other way for questions like
"why do central banks raise rates when prices climb", which has no useful keywords.

Tokenisation keeps hyphenated and dotted technical terms whole *and* also emits
their parts, so "cobb-douglas" matches both a query for the full term and one for
"douglas".
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from rank_bm25 import BM25Okapi

from ..models import Chunk

_TOKEN = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")
_STOP = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "is", "are",
    "was", "were", "be", "been", "it", "its", "this", "that", "these", "those", "as",
    "by", "with", "from", "what", "which", "does", "do", "how", "why", "when",
}


def stem(token: str) -> Optional[str]:
    """Conservative suffix stripping.

    Deliberately minimal: plurals and a few verb endings only. A full Porter
    stemmer would conflate technical terms this corpus needs kept apart, and the
    stem is *added* alongside the original token rather than replacing it, so
    exact matching on "Cobb-Douglas" or "heaps" is never weakened -- only widened.
    Without this, "Fibonacci heaps" fails to match "Fibonacci heap" and the
    question is falsely refused.
    """
    if len(token) < 4:
        return None
    for suffix, min_len in (("ies", 5), ("es", 5), ("s", 4), ("ing", 6), ("ed", 5)):
        if token.endswith(suffix) and len(token) >= min_len:
            base = token[: -len(suffix)]
            if suffix == "ies":
                base += "y"
            if len(base) >= 3 and base != token:
                return base
    return None


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []

    def emit(token: str) -> None:
        if token in _STOP or len(token) < 2:
            return
        tokens.append(token)
        if (base := stem(token)) is not None:
            tokens.append(base)

    for match in _TOKEN.finditer((text or "").lower()):
        token = match.group(0)
        if token in _STOP:
            continue
        emit(token)
        # Emit the parts of a compound term as well, so "cobb-douglas" is findable
        # by either half without losing the exact-match advantage of the whole.
        if "-" in token or "." in token:
            for part in re.split(r"[-.]", token):
                if part:
                    emit(part)
    return tokens


class SparseIndex:
    def __init__(self, chunks: Sequence[Chunk]):
        self.chunk_ids = [c.id for c in chunks]
        # Index the contextual-prefixed text so chapter-phrased queries
        # ("chapter 11 phillips curve") hit lexically too.
        corpus = [tokenize(c.embed_text) for c in chunks]
        self._empty = not corpus or all(not doc for doc in corpus)
        self.bm25: Optional[BM25Okapi] = None if self._empty else BM25Okapi(corpus)

    def search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        if self.bm25 is None:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_k]
        return [(self.chunk_ids[i], float(scores[i])) for i in ranked if scores[i] > 0]

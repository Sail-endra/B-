"""Cross-encoder style reranking, with a calibrated 0-1 output.

Three backends behind one interface, chosen by what is configured:

  * Cohere Rerank, when a key is present -- the production path.
  * An LLM pointwise scorer, when Anthropic is available but Cohere is not.
  * A lexical proximity-and-coverage heuristic, always available, no network.

The heuristic is not a neural cross-encoder and does not claim to be, but it is
not a reimplementation of BM25 either: it scores *term coverage* and *proximity*,
which a bag-of-words retriever ignores entirely. That is why it still moves the
ablation -- it reorders passages that BM25 scored identically.

The output scale matters as much as the ordering. Refusal is calibrated on the
reranker score rather than on cosine distance because cosine over a fixed corpus
is poorly calibrated: an out-of-corpus question still has a nearest neighbour, and
that neighbour's cosine is often unremarkable rather than obviously low.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Optional, Sequence

from ..config import settings
from ..models import Chunk
from .sparse import tokenize

# Coverage is scored against at most this many query terms, ranked by IDF.
_MAX_QUERY_TERMS = 8

# Question-framing vocabulary, dropped before query terms are ranked by IDF.
#
# IDF on a small corpus is actively misleading here: a textbook almost never says
# "still", "claims" or "support", so those score HIGHER than "unemployment" and
# "inflation", which appear in many chunks. Ranking by raw IDF therefore selects
# precisely the words that carry no topic information, and a compositional
# question ends up scored on its scaffolding instead of its subject.
#
# These are removed only for reranking. BM25 keeps them indexed, since dropping
# terms from the index costs recall on exact-phrase queries.
_FRAMING = {
    "still", "support", "supports", "supported", "claim", "claims", "claimed",
    "say", "says", "said", "saying", "tell", "tells", "explain", "explains",
    "describe", "describes", "discuss", "discusses", "according", "actually",
    "really", "just", "also", "more", "most", "such", "using", "use", "uses",
    "used", "given", "give", "gives", "get", "gets", "make", "makes", "made",
    "would", "could", "should", "may", "might", "can", "will", "there", "here",
    "about", "textbook", "chapter", "course", "reading", "lecture", "slide",
    "slides", "material", "materials", "data", "show", "shows", "shown",
    "mean", "means", "happen", "happens", "think", "know", "need", "want",
}


class Reranker(ABC):
    name = "abstract"

    @abstractmethod
    def score(self, query: str, chunks: Sequence[Chunk]) -> list[float]:
        """Return one score in [0, 1] per chunk, aligned with the input order."""


class LexicalReranker(Reranker):
    """Coverage x proximity x specificity, squashed to [0, 1]."""

    name = "lexical-heuristic"

    def __init__(self, corpus: Optional[Sequence[Chunk]] = None):
        # Document frequency over the corpus gives us IDF, so that matching
        # "Cobb-Douglas" counts for far more than matching "model".
        self.doc_freq: Counter[str] = Counter()
        self.n_docs = 0
        if corpus:
            for chunk in corpus:
                self.n_docs += 1
                for token in set(tokenize(chunk.embed_text)):
                    self.doc_freq[token] += 1

    def _idf(self, token: str) -> float:
        if self.n_docs == 0:
            return 1.0
        df = self.doc_freq.get(token, 0)
        return math.log((self.n_docs + 1) / (df + 0.5))

    def score(self, query: str, chunks: Sequence[Chunk]) -> list[float]:
        query_tokens = [t for t in tokenize(query)]
        if not query_tokens:
            return [0.0] * len(chunks)
        unique_query = list(dict.fromkeys(query_tokens))
        # Drop framing vocabulary first -- see _FRAMING. Keep everything if the
        # question was nothing but framing, so the score never becomes undefined.
        content_query = [t for t in unique_query if t not in _FRAMING]
        unique_query = content_query or unique_query

        # Score against the most informative query terms only, not all of them.
        #
        # Without this cap the score falls as the question gets longer: a
        # compositional question ("Does the data still support what chapter 11
        # claims about unemployment and inflation?") spreads its coverage across
        # a dozen terms that no single passage contains, scores ~0.03, and is
        # falsely refused even though the correct passage is ranked first. Since
        # this score drives the refusal threshold, length-sensitivity there is a
        # correctness bug, not a tuning preference.
        idf = {t: self._idf(t) for t in unique_query}
        top_terms = sorted(unique_query, key=lambda t: -idf[t])[:_MAX_QUERY_TERMS]
        idf = {t: idf[t] for t in top_terms}
        total_idf = sum(idf.values()) or 1.0
        query_phrase = " ".join(query_tokens)

        out = []
        for chunk in chunks:
            doc_tokens = tokenize(chunk.embed_text)
            if not doc_tokens:
                out.append(0.0)
                continue
            positions: dict[str, list[int]] = {}
            for i, token in enumerate(doc_tokens):
                if token in idf:
                    positions.setdefault(token, []).append(i)

            matched = set(positions)
            coverage = sum(idf[t] for t in matched) / total_idf

            proximity = 0.0
            if len(matched) > 1:
                window = self._min_window(positions, len(matched))
                # A window as tight as the query itself scores ~1; a diffuse
                # scatter across a long passage scores near 0.
                proximity = len(matched) / max(window, len(matched))
            elif matched:
                proximity = 0.5

            phrase_bonus = 0.15 if query_phrase and query_phrase in " ".join(doc_tokens) else 0.0

            raw = 0.70 * coverage + 0.30 * coverage * proximity + phrase_bonus
            out.append(max(0.0, min(1.0, raw)))
        return out

    @staticmethod
    def _min_window(positions: dict[str, list[int]], need: int) -> int:
        """Smallest span of the document containing every matched query term."""
        flat = sorted((pos, token) for token, plist in positions.items() for pos in plist)
        if not flat:
            return 10**6
        best = 10**6
        counts: Counter[str] = Counter()
        left = 0
        for right, (pos_r, tok_r) in enumerate(flat):
            counts[tok_r] += 1
            while len(counts) == need:
                pos_l, tok_l = flat[left]
                best = min(best, pos_r - pos_l + 1)
                counts[tok_l] -= 1
                if counts[tok_l] == 0:
                    del counts[tok_l]
                left += 1
        return best


class CohereReranker(Reranker):
    name = "cohere"

    def __init__(self, api_key: str, model: str = "rerank-english-v3.0"):
        import cohere  # imported lazily; optional dependency

        self._client = cohere.Client(api_key)
        self.model = model

    def score(self, query: str, chunks: Sequence[Chunk]) -> list[float]:
        if not chunks:
            return []
        docs = [c.text for c in chunks]
        resp = self._client.rerank(
            query=query, documents=docs, model=self.model, top_n=len(docs)
        )
        scores = [0.0] * len(chunks)
        for result in resp.results:
            scores[result.index] = float(result.relevance_score)
        return scores


class LLMReranker(Reranker):
    """Pointwise relevance scoring with the cheap model, batched into one call."""

    name = "llm-pointwise"

    def __init__(self, fallback: Reranker):
        self.fallback = fallback

    def score(self, query: str, chunks: Sequence[Chunk]) -> list[float]:
        if not chunks:
            return []
        from ..llm import get_llm

        llm = get_llm()
        numbered = "\n\n".join(
            f"[{i}] {c.text[:600]}" for i, c in enumerate(chunks)
        )
        prompt = (
            "Score how well each passage answers the question, 0.0 to 1.0.\n"
            "A passage that merely mentions the topic scores low; one that states "
            "the answer scores high.\n\n"
            f"Question: {query}\n\nPassages:\n{numbered}\n\n"
            'Reply with JSON only: {"scores": [{"i": 0, "s": 0.0}, ...]} '
            "with one entry per passage."
        )
        try:
            data = llm.json(prompt, max_tokens=1200)
            scores = [0.0] * len(chunks)
            for item in data.get("scores", []):
                idx = int(item["i"])
                if 0 <= idx < len(chunks):
                    scores[idx] = max(0.0, min(1.0, float(item["s"])))
            return scores
        except Exception:  # noqa: BLE001 - reranking must never break a query
            return self.fallback.score(query, chunks)


def get_reranker(corpus: Optional[Sequence[Chunk]] = None) -> Reranker:
    lexical = LexicalReranker(corpus)
    if settings.offline:
        return lexical
    if settings.cohere_api_key:
        try:
            return CohereReranker(settings.cohere_api_key)
        except Exception:  # noqa: BLE001 - missing optional dep or bad key
            pass
    if settings.has_anthropic:
        return LLMReranker(fallback=lexical)
    return lexical

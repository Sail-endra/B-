"""Dense retrieval over the chunk embedding matrix.

A full cosine scan. At the scale this actually runs at -- low thousands of chunks
-- the scan is single-digit milliseconds, comfortably inside the latency budget,
and it avoids an index dependency that would need rebuilding on every ingest.
Swapping in pgvector's ivfflat changes this file and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..config import ROOT, settings
from ..embed import Embedder, get_embedder
from ..store import SQLiteStore




class DenseIndex:
    def __init__(self, store: SQLiteStore, user_id: str, embedder: Optional[Embedder] = None):
        self.store = store
        self.user_id = user_id
        self.embedder = embedder or self._load_embedder(user_id)
        self.ids, self.matrix = store.vector_matrix(user_id)

    @staticmethod
    def _load_embedder(user_id: str = "") -> Embedder:
        """The local embedder must be restored from the state saved at ingest --
        refitting on a different corpus would put queries in a different space."""
        from ..corpus.ingest_service import embedder_state_path

        embedder = get_embedder()
        if embedder.stateful:
            path = embedder_state_path(user_id)
            if not embedder.load(path):
                raise RuntimeError(
                    f"no fitted embedder for user {user_id!r} at {path}. "
                    "Upload materials for this user first."
                )
        return embedder

    def search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        if not self.ids:
            return []
        vector = self.embedder.embed_query(query)
        norm = float(np.linalg.norm(vector))
        if norm == 0:
            return []
        scores = self.matrix @ (vector / norm)
        k = min(top_k, len(self.ids))
        # argpartition then sort the slice -- avoids a full sort of every chunk.
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(self.ids[i], float(scores[i])) for i in top]

    def search_many(self, queries: Sequence[str], top_k: int) -> list[list[tuple[str, float]]]:
        """Batched variant -- one embedding call for the whole eval sweep."""
        if not self.ids or not queries:
            return [[] for _ in queries]
        vectors = self.embedder.embed(list(queries))
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        all_scores = (vectors / norms) @ self.matrix.T
        k = min(top_k, len(self.ids))
        out = []
        for scores in all_scores:
            top = np.argpartition(-scores, k - 1)[:k]
            top = top[np.argsort(-scores[top])]
            out.append([(self.ids[i], float(scores[i])) for i in top])
        return out

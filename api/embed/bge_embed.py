"""Dense embeddings via a local sentence-transformers model (bge-small-en-v1.5).

This is the key change from the fitted TF-IDF+SVD embedder: it is *stateless*.
The old local embedder had to be fit on the corpus and its state pickled to disk,
which is what created the per-user fitted-state coupling behind two multi-tenancy
bugs. A pretrained sentence encoder has no per-corpus state — every chunk and
every query map into the same fixed space with no fitting step — so that whole
class of bug is gone, and ingest stays key-free (the model runs locally).

BGE asks that retrieval *queries* carry a short instruction prefix while the
passages do not; `embed_query` applies it, `embed` (used for passages) does not.
"""

from __future__ import annotations

import os
import threading
from typing import Optional, Sequence

import numpy as np

from .base import Embedder

_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class BgeEmbedder(Embedder):
    name = "bge-small-en-v1.5"
    stateful = False
    dim = 384

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        import torch
        from sentence_transformers import SentenceTransformer

        # Use the whole CPU for the matmuls. torch defaults to a subset of cores;
        # on machines where that default is low this is a real speedup, and it is
        # harmless where more threads don't help (measured: negligible on a 10-core
        # M-series, but a win on smaller default thread pools).
        try:
            torch.set_num_threads(max(1, os.cpu_count() or 1))
        except Exception:  # noqa: BLE001 - never let thread tuning break init
            pass
        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()
        self._encode_lock = threading.Lock()

    def fit(self, corpus: Sequence[str]) -> None:
        """No-op. A pretrained encoder has no per-corpus state to fit -- which is
        the entire point of moving off the TF-IDF+SVD embedder."""

    def _encode(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # A cached model may be used by an API request while a background
        # material job is embedding. Serialize model access without loading a
        # second copy into memory.
        with self._encode_lock:
            vecs = self._model.encode(
                list(texts),
                normalize_embeddings=True,   # cosine == dot product downstream
                convert_to_numpy=True,
                show_progress_bar=False,
                batch_size=64,
            )
        return vecs.astype(np.float32)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([_QUERY_PREFIX + text])[0]

    # Stateless: nothing to persist or restore.
    def save(self, path) -> None:  # pragma: no cover
        return None

    def load(self, path) -> bool:  # pragma: no cover
        return True

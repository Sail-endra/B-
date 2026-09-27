from __future__ import annotations

import time
from typing import Sequence

import numpy as np

from .base import Embedder

_BATCH = 128
_MAX_RETRIES = 3


class OpenAIEmbedder(Embedder):
    name = "openai"
    stateful = False

    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        from openai import OpenAI

        self.model = model
        self.name = model
        self._client = OpenAI(api_key=api_key)
        self.dim = 1536

    def fit(self, corpus: Sequence[str]) -> None:
        """Stateless -- nothing to fit."""

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            batch = [t if (t and t.strip()) else " " for t in texts[start : start + _BATCH]]
            vectors.extend(self._embed_batch(batch))
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        last: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.embeddings.create(model=self.model, input=batch)
                return [d.embedding for d in resp.data]
            except Exception as exc:  # noqa: BLE001 - surfaced after retries
                last = exc
                time.sleep(2**attempt)
        raise RuntimeError(f"embedding failed after {_MAX_RETRIES} attempts: {last}")

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

import numpy as np


class Embedder(ABC):
    name: str = "abstract"
    dim: int = 0
    # Stateful embedders are fit on the corpus and their state must be persisted
    # and restored (the TF-IDF+SVD one). Stateless embedders map into a fixed
    # pretrained space with no fitting -- which is what removes the per-user
    # fitted-state coupling behind the old multi-tenancy bugs.
    stateful: bool = True

    @abstractmethod
    def fit(self, corpus: Sequence[str]) -> None:
        """Local backends need to see the corpus before they can embed. Remote
        backends ignore this."""

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return an (n, dim) float32 matrix, one row per input."""

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a retrieval query. Default is identical to a passage; encoders
        that want an asymmetric query representation (BGE) override this."""
        return self.embed_one(text)

    def save(self, path) -> None:  # pragma: no cover - optional
        """Persist any fitted state. Remote backends are stateless."""

    def load(self, path) -> bool:  # pragma: no cover - optional
        """Restore fitted state. Returns False when nothing was restored."""
        return False

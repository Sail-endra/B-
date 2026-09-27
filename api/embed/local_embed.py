"""Deterministic offline embedder: TF-IDF over word and character n-grams,
reduced with truncated SVD (i.e. LSA), then L2-normalised.

This is not competitive with a modern sentence encoder and is not pretending to
be. It exists so the pipeline is fully runnable and measurable without a network
call, and because a *semantic* baseline is more honest than a hash: LSA genuinely
captures some synonymy, so the dense-vs-sparse ablation still shows the effect it
is supposed to show rather than collapsing to "dense is noise".

Character n-grams are included because the corpus is notation-heavy -- they keep
"Cobb-Douglas" and "IS-LM" partially intact through the reduction.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion

from .base import Embedder


class LocalEmbedder(Embedder):
    name = "local-tfidf-svd"

    def __init__(self, dim: int = 384, random_state: int = 0):
        self.dim = dim
        self.random_state = random_state
        self._features: Optional[FeatureUnion] = None
        self._svd: Optional[TruncatedSVD] = None
        self._fitted = False

    def _build_features(self, n_docs: int) -> FeatureUnion:
        """Vectoriser settings scaled to corpus size.

        The defaults assume a corpus of hundreds of documents. A stranger's first
        upload is often a single file, where `max_df=0.85` drops below `min_df=1`
        and sklearn raises "max_df corresponds to < documents than min_df" -- a
        hard crash on the very first thing a new user does. Small corpora keep
        every term instead; there is nothing to prune.
        """
        tiny = n_docs < 10
        blocks = [
            (
                "word",
                TfidfVectorizer(
                    lowercase=True, sublinear_tf=True, ngram_range=(1, 2),
                    min_df=1, max_df=1.0 if tiny else 0.85,
                    strip_accents="unicode",
                ),
            )
        ]
        # Char n-grams need min_df=2 to stay a sane size, which is impossible
        # below two documents.
        if n_docs >= 2:
            blocks.append(
                (
                    "char",
                    TfidfVectorizer(
                        lowercase=True, sublinear_tf=True, analyzer="char_wb",
                        ngram_range=(3, 5), min_df=1 if tiny else 2,
                        max_features=60_000,
                    ),
                )
            )
        return FeatureUnion(blocks)

    def fit(self, corpus: Sequence[str]) -> None:
        corpus = [t for t in corpus if t and t.strip()]
        if not corpus:
            raise ValueError("cannot fit LocalEmbedder on an empty corpus")

        try:
            self._features = self._build_features(len(corpus))
            matrix = self._features.fit_transform(corpus)
        except ValueError:
            # Last resort: word unigrams, every term kept. Covers corpora that
            # are all stop words, or a single very short document.
            self._features = FeatureUnion([
                ("word", TfidfVectorizer(
                    lowercase=True, min_df=1, max_df=1.0,
                    token_pattern=r"(?u)\b\w+\b", strip_accents="unicode",
                ))
            ])
            matrix = self._features.fit_transform(corpus)

        # SVD needs n_components < n_features and < n_samples.
        n_components = int(min(self.dim, matrix.shape[1] - 1, max(1, matrix.shape[0] - 1)))
        if n_components < 1:
            # One document, one term: no reduction is possible or needed.
            self._svd = None
            self.dim = matrix.shape[1]
            self._fitted = True
            return
        self._svd = TruncatedSVD(n_components=n_components, random_state=self.random_state)
        self._svd.fit(matrix)
        self.dim = n_components
        self._fitted = True

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not self._fitted or self._features is None:
            raise RuntimeError("LocalEmbedder.fit() must be called before embed()")
        safe = [t if (t and t.strip()) else " " for t in texts]
        sparse = self._features.transform(safe)
        reduced = sparse.toarray() if self._svd is None else self._svd.transform(sparse)
        norms = np.linalg.norm(reduced, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (reduced / norms).astype(np.float32)

    def save(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            pickle.dump(
                {"features": self._features, "svd": self._svd, "dim": self.dim}, fh
            )

    def load(self, path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        with path.open("rb") as fh:
            state = pickle.load(fh)
        self._features = state["features"]
        self._svd = state["svd"]
        self.dim = state["dim"]
        self._fitted = True
        return True

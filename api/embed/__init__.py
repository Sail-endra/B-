"""Embedding backends, behind one interface.

  * OpenAI `text-embedding-3-small`  -- when a key is present (stateless).
  * BGE `bge-small-en-v1.5`          -- local sentence-transformers (stateless).
  * TF-IDF + truncated-SVD           -- fitted, deterministic, key-free (stateful).

The dense leg is BGE by default now: a pretrained sentence encoder with no
per-corpus fitting, which is what let us delete the fitted-embedder state and the
multi-tenancy bug class that came with it. TF-IDF remains for tests (fast,
deterministic) and as the last-resort fallback when sentence-transformers is not
installed. Selection is `COPILOT_EMBEDDER`: auto | bge | tfidf | openai.
"""

from __future__ import annotations

import threading

from .base import Embedder
from .local_embed import LocalEmbedder
from .openai_embed import OpenAIEmbedder


_BGE_INSTANCES: dict[str, Embedder] = {}
_BGE_LOCK = threading.Lock()


def _bge_embedder(model_name: str) -> Embedder:
    """Load one stateless BGE model per process and reuse it across ingest jobs."""
    with _BGE_LOCK:
        embedder = _BGE_INSTANCES.get(model_name)
        if embedder is None:
            from .bge_embed import BgeEmbedder
            embedder = BgeEmbedder(model_name)
            _BGE_INSTANCES[model_name] = embedder
        return embedder


def _clear_embedder_cache() -> None:
    """Test hook; production callers should keep the process-level model warm."""
    with _BGE_LOCK:
        _BGE_INSTANCES.clear()


def _bge_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


def get_embedder(force_local: bool = False) -> Embedder:
    from ..config import settings

    mode = "tfidf" if force_local else settings.embedder_mode

    if mode == "tfidf":
        return LocalEmbedder()
    if mode == "openai":
        return OpenAIEmbedder(settings.openai_api_key, settings.embed_model)
    if mode == "bge":
        return _bge_embedder(settings.bge_model)

    # auto
    if settings.has_openai:
        return OpenAIEmbedder(settings.openai_api_key, settings.embed_model)
    if _bge_available():
        return _bge_embedder(settings.bge_model)
    return LocalEmbedder()


__all__ = ["Embedder", "LocalEmbedder", "OpenAIEmbedder", "get_embedder"]

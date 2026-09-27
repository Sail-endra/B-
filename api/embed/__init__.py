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

from .base import Embedder
from .local_embed import LocalEmbedder
from .openai_embed import OpenAIEmbedder


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
        from .bge_embed import BgeEmbedder
        return BgeEmbedder(settings.bge_model)

    # auto
    if settings.has_openai:
        return OpenAIEmbedder(settings.openai_api_key, settings.embed_model)
    if _bge_available():
        from .bge_embed import BgeEmbedder
        return BgeEmbedder(settings.bge_model)
    return LocalEmbedder()


__all__ = ["Embedder", "LocalEmbedder", "OpenAIEmbedder", "get_embedder"]

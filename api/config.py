"""Runtime configuration.

Everything degrades to an offline-deterministic mode when a key is absent, so the
whole system -- ingest, retrieval, eval -- runs and produces real numbers with no
network at all. Which backend was actually used is recorded in every eval report,
because "recall@5 = 0.89" means nothing without knowing what embedded the corpus.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader -- avoids a hard dependency for a 10-line job."""
    for candidate in (ROOT / ".env", ROOT / ".env.local"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentBudget:
    """Hard limits on the agent loop. Unbounded loops are how you get a 90-second
    response and a $4 bill per question."""

    max_steps: int = 6
    max_tool_tokens: int = 12_000
    hard_timeout_s: float = 25.0
    # A timeout must render whatever the agent had, not a blank error -- partial
    # evidence with the steps shown is more useful than nothing.
    partial_on_timeout: bool = True


@dataclass(frozen=True)
class RetrievalConfig:
    dense_top_k: int = 50
    sparse_top_k: int = 50
    rrf_k: int = 60
    fused_top_k: int = 30
    final_top_k: int = 6
    # Calibrated on the REAL corpus (2,182 chunks), not the synthetic one.
    # `python -m eval.run_refusal --calibrate` sweeps the reranker score and picks
    # the F1-maximising value, composed with the intent gate. At 0.327:
    # precision 0.963 / recall 0.765 / F1 0.852, answering 97.9% of in-corpus
    # questions.
    #
    # The synthetic-corpus value was 0.317 with precision 0.737 -- close in
    # threshold, very different in what it bought, which is why the threshold is
    # never portable across corpora. Re-run the sweep after any retrieval change.
    refusal_threshold: float = 0.327
    # Compound-query handling. Decompose a multi-part question into weighted
    # sub-queries and fuse; boost chunks whose chapter title matches the concept.
    # Both shipped on -- they are what makes "what is the budget constraint? how
    # does it apply?" rank the chapter titled Budget Constraint first. The boost
    # weight is added to the reranker score (~0.3-0.5 scale); a full title match
    # (similarity 1.0) is decisive without swamping a strong non-title match.
    decompose: bool = True
    decompose_method: str = "heuristic"
    chapter_boost_weight: float = 0.30
    # Index 400-token children for precision, return 1200-token parents for context.
    child_tokens: int = 400
    parent_tokens: int = 1200
    child_overlap: int = 64


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: Optional[str] = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    anthropic_base_url: Optional[str] = field(default_factory=lambda: os.getenv("ANTHROPIC_BASE_URL"))
    openai_api_key: Optional[str] = field(default_factory=lambda: os.getenv("OPENAI_API_KEY"))
    fred_api_key: Optional[str] = field(default_factory=lambda: os.getenv("FRED_API_KEY"))
    cohere_api_key: Optional[str] = field(default_factory=lambda: os.getenv("COHERE_API_KEY"))
    gemini_api_key: Optional[str] = field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    # 'gemini-flash-lite-latest' is the stable free-tier alias that tracks the
    # current Flash-Lite, so a pinned version going away can't break the default.
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))

    elevenlabs_api_key: Optional[str] = field(default_factory=lambda: os.getenv("ELEVENLABS_API_KEY"))
    elevenlabs_voice_id: str = field(default_factory=lambda: os.getenv("ELEVENLABS_VOICE_ID", ""))
    elevenlabs_tts_model: str = field(
        default_factory=lambda: os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
    )
    elevenlabs_stt_model: str = field(
        default_factory=lambda: os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
    )

    answer_model: str = field(default_factory=lambda: os.getenv("ANSWER_MODEL", "claude-sonnet-4-5"))
    cheap_model: str = field(default_factory=lambda: os.getenv("CHEAP_MODEL", "claude-haiku-4-5-20251001"))
    embed_model: str = field(default_factory=lambda: os.getenv("EMBED_MODEL", "text-embedding-3-small"))
    # Dense-leg selection: auto | bge | tfidf | openai. `auto` prefers OpenAI
    # when keyed, else a local sentence-transformers model, else TF-IDF+SVD.
    # Tests pin this to `tfidf` for speed and determinism.
    embedder_mode: str = field(default_factory=lambda: os.getenv("COPILOT_EMBEDDER", "auto"))
    bge_model: str = field(default_factory=lambda: os.getenv("BGE_MODEL", "BAAI/bge-small-en-v1.5"))
    # Local Ollama backend: a fully private answerer that needs no consent. Set
    # OLLAMA_MODEL (e.g. "qwen2.5:7b") to enable it for the product path.
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", ""))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434"))

    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", str(ROOT / "data" / "copilot.db"))))
    corpus_dir: Path = field(default_factory=lambda: Path(os.getenv("CORPUS_DIR", str(ROOT / "materials"))))
    seed_dir: Path = field(default_factory=lambda: ROOT / "seed")
    eval_dir: Path = field(default_factory=lambda: ROOT / "eval")

    # Force every provider to its deterministic offline backend. Used by tests and
    # by `make eval-fast` so retrieval metrics never depend on network weather.
    offline: bool = field(default_factory=lambda: _flag("COPILOT_OFFLINE"))

    # Single-user until auth lands. The schema carries user_id everywhere from day
    # one -- that column is the expensive-to-retrofit part; the auth flow is not.
    default_user_id: str = "local-user"

    # The privacy boundary for the PRODUCT answer path. Free-tier Gemini trains on
    # its inputs, so a product/cohort question must not reach it without the user
    # opting in. Product generation is allowed only when either:
    #   - an Anthropic key is set (the user's own paid key; API inputs are not used
    #     for training, so this is implicit consent), or
    #   - the user explicitly turns this on, accepting that their questions may be
    #     sent to free-tier Gemini and used to improve Google's models.
    # Off by default: with neither, the product returns the grounded extractive
    # answer and no question ever leaves for Gemini. The benchmark path is separate
    # (get_llm(allow_gemini=True)) and never gated by this.
    product_ai_consent: bool = field(default_factory=lambda: _flag("COPILOT_PRODUCT_AI_CONSENT"))

    budget: AgentBudget = field(default_factory=AgentBudget)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key) and not self.offline

    @property
    def product_generation_allowed(self) -> bool:
        """May the PRODUCT path call a generative model at all?"""
        if self.offline:
            return False
        if self.has_anthropic:
            return True
        return bool(self.gemini_api_key) and self.product_ai_consent

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key) and not self.offline

    @property
    def has_fred(self) -> bool:
        return bool(self.fred_api_key) and not self.offline

    @property
    def embeddings_label(self) -> str:
        """The dense leg actually in use, for eval attribution -- computed without
        importing torch just to name it."""
        mode = self.embedder_mode
        if mode == "tfidf":
            return "local-tfidf-svd"
        if mode == "openai" or (mode == "auto" and self.has_openai):
            return self.embed_model
        if mode == "bge":
            return self.bge_model
        # auto, no key: bge if installed else tfidf
        import importlib.util
        if importlib.util.find_spec("sentence_transformers") is not None:
            return self.bge_model
        return "local-tfidf-svd"

    @property
    def llm_label(self) -> str:
        """Name the answerer actually in use, for eval attribution.

        If a model has already been instantiated (the eval primes it), report that
        concrete instance -- the benchmark runs on free-tier Gemini, and labelling
        that run "deterministic-stub" would silently misattribute every number it
        produces. Otherwise fall back to what the config alone implies. The import
        is lazy so config.py stays free of an llm.py dependency."""
        if self.has_anthropic:
            return self.answer_model
        try:
            from . import llm as _llm_mod

            active = _llm_mod._llm
        except Exception:  # noqa: BLE001 - never let labelling break a run
            active = None
        if active is not None and getattr(active, "available", False):
            return getattr(active, "label", None) or type(active).__name__
        return "deterministic-stub"

    def describe_backends(self) -> dict[str, str]:
        """Recorded into every eval run so a metric is always attributable."""
        return {
            "embeddings": self.embeddings_label,
            "llm": self.llm_label,
            "reranker": "cohere" if (self.cohere_api_key and not self.offline)
            else ("llm-pointwise" if self.has_anthropic else "lexical-heuristic"),
            "fred": "live" if self.has_fred else "synthetic",
            "offline": str(self.offline),
        }


settings = Settings()

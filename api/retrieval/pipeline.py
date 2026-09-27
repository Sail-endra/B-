"""The retrieval pipeline.

    query
     |-- dense:  cosine over pgvector/numpy, top 50
     `-- sparse: BM25, top 50
            |
       reciprocal rank fusion (k=60)
            |
          top 30
            |
       cross-encoder rerank
            |
       top 6 + calibrated confidence

Every stage is switchable via `PipelineConfig`, which is what makes the ablation
table in docs/EVAL.md a recording of work already done rather than extra work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

from ..config import settings
from ..ids import parse_chapter_mentions
from ..models import Chunk, RetrievalResult, ScoredChunk
from ..store import SQLiteStore, get_store
from ..syllabus.link import title_similarity
from .decompose import Decomposition, decompose
from .dense import DenseIndex
from .fuse import rank_positions, rrf
from .intent import Intent, classify
from .rerank import Reranker, get_reranker
from .sparse import SparseIndex


@dataclass(frozen=True)
class PipelineConfig:
    """Stage switches. The defaults are the full pipeline; the ablation runner
    flips them one at a time."""

    use_dense: bool = True
    use_sparse: bool = True
    use_rerank: bool = True
    use_parent: bool = True
    # "rank"       -- reranker reorders the fused candidates (the textbook design)
    # "confidence" -- fused order is kept, reranker only supplies the calibrated
    #                 score used for refusal.
    #
    # Default was "confidence" on the tiny synthetic corpus, where reordering
    # lowered r@1 -- a small-corpus artifact, since top-50 over ~114 chunks left
    # the reranker nothing to discriminate between. On the real corpus the
    # candidate set is ~2% and reordering wins on the `natural` slice, so `rank`
    # ships. The `generated` slice (vocabulary-inflated, n=10) still prefers
    # `confidence`; see eval/RESULTS.md for the current ablation rather than a
    # number pasted here.
    rerank_mode: str = "rank"
    # Compose question-intent with relevance when deciding to refuse.
    use_intent_gate: bool = True
    # Decompose a compound question into weighted sub-queries and fuse their
    # rankings. Off by default so the ablation baseline is the single-query
    # pipeline; the shipped config turns it on.
    decompose: bool = False
    decompose_method: str = "heuristic"  # "heuristic" | "llm"
    # Boost chunks whose chapter TITLE matches the concept sub-query. 0.0 disables
    # it (ablation baseline). The boost is added to the ranking score only; the
    # refusal confidence stays the reranker's top score, so the gate is untouched.
    chapter_boost_weight: float = 0.0
    dense_top_k: int = 50
    sparse_top_k: int = 50
    rrf_k: int = 60
    fused_top_k: int = 30
    final_top_k: int = 6
    refusal_threshold: float = 0.42

    @classmethod
    def from_settings(cls) -> "PipelineConfig":
        r = settings.retrieval
        return cls(
            dense_top_k=r.dense_top_k,
            sparse_top_k=r.sparse_top_k,
            rrf_k=r.rrf_k,
            fused_top_k=r.fused_top_k,
            final_top_k=r.final_top_k,
            refusal_threshold=r.refusal_threshold,
            decompose=r.decompose,
            decompose_method=r.decompose_method,
            chapter_boost_weight=r.chapter_boost_weight,
        )

    def label(self) -> str:
        parts = []
        if self.use_dense:
            parts.append("dense")
        if self.use_sparse:
            parts.append("bm25")
        if self.use_dense and self.use_sparse:
            parts = ["dense+bm25(rrf)"]
        if self.use_rerank:
            parts.append("rerank")
        if self.use_parent:
            parts.append("parent-child")
        if self.decompose:
            parts.append("decompose")
        if self.chapter_boost_weight > 0.0:
            parts.append(f"title-boost({self.chapter_boost_weight:g})")
        return " + ".join(parts) or "none"


class RetrievalPipeline:
    def __init__(
        self,
        store: Optional[SQLiteStore] = None,
        user_id: str = settings.default_user_id,
        config: Optional[PipelineConfig] = None,
    ):
        self.store = store or get_store()
        self.user_id = user_id
        self.config = config or PipelineConfig.from_settings()

        self._chunks: list[Chunk] = self.store.all_chunks(user_id)
        self._by_id: dict[str, Chunk] = {c.id: c for c in self._chunks}
        self._dense: Optional[DenseIndex] = None
        self._sparse: Optional[SparseIndex] = None
        self._reranker: Optional[Reranker] = None
        self._data_link_cache: dict[str, bool] = {}
        # {course_id: {chapter_num: title}}, for the chapter-title retrieval boost.
        self._chapter_titles: Optional[dict[str, dict[int, str]]] = None
        self._decompose_llm: Any = None
        # The last query's decomposition, exposed so the answer/data layers can see
        # the detected sub-intents (e.g. whether an example was asked for).
        self.last_decomposition: Optional[Decomposition] = None

    # Indexes are built lazily so an ablation run that disables a stage does not
    # pay to construct it.
    @property
    def dense(self) -> DenseIndex:
        if self._dense is None:
            self._dense = DenseIndex(self.store, self.user_id)
        return self._dense

    @property
    def sparse(self) -> SparseIndex:
        if self._sparse is None:
            self._sparse = SparseIndex(self._chunks)
        return self._sparse

    @property
    def reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = get_reranker(self._chunks)
        return self._reranker

    def with_config(self, config: PipelineConfig) -> "RetrievalPipeline":
        """Share the loaded corpus and indexes across ablation configurations."""
        clone = RetrievalPipeline.__new__(RetrievalPipeline)
        clone.store = self.store
        clone.user_id = self.user_id
        clone.config = config
        clone._chunks = self._chunks
        clone._by_id = self._by_id
        clone._dense = self._dense
        clone._sparse = self._sparse
        clone._reranker = self._reranker
        clone._data_link_cache = self._data_link_cache
        clone._chapter_titles = self._chapter_titles
        clone._decompose_llm = self._decompose_llm
        clone.last_decomposition = None
        return clone

    @property
    def chapter_titles(self) -> dict[str, dict[int, str]]:
        """{course_id: {chapter_num: title}}, derived from the loaded chunks.

        The chunks already carry chapter_title, so no extra query is needed. Used
        by the chapter-title retrieval boost -- the same title signal that took
        syllabus linking from 19% to 100%, reused for ranking.
        """
        if self._chapter_titles is None:
            titles: dict[str, dict[int, str]] = {}
            for c in self._chunks:
                if not c.chapter_num or not c.chapter_title:
                    continue
                titles.setdefault(c.course_id, {}).setdefault(c.chapter_num, c.chapter_title)
            self._chapter_titles = titles
        return self._chapter_titles

    # -- main --------------------------------------------------------------

    def search(
        self,
        query: str,
        course_id: Optional[str] = None,
        chapter: Optional[int] = None,
        top_k: Optional[int] = None,
    ) -> RetrievalResult:
        cfg = self.config
        top_k = top_k or cfg.final_top_k

        allowed: Optional[set[str]] = None
        if course_id or chapter is not None:
            allowed = {
                c.id
                for c in self._chunks
                if (course_id is None or c.course_id == course_id)
                and (chapter is None or c.chapter_num == chapter)
            }
            if not allowed:
                return RetrievalResult(
                    query=query,
                    chunks=[],
                    confidence=0.0,
                    refused=True,
                    reason=f"no materials loaded for course={course_id} chapter={chapter}",
                    backend=settings.describe_backends(),
                )

        # Decompose a compound question into weighted sub-queries. Each sub-query
        # contributes its own dense and sparse ranked list, all fused together, so
        # the concept is retrieved on its own instead of being averaged into noise
        # by the framing and example clauses. The concept sub-query is weighted up.
        decomp = self._decompose(query, cfg)
        self.last_decomposition = decomp

        ranked_lists, weights, dense_pos, sparse_pos = self._retrieval_lists(
            query, decomp, cfg, allowed
        )
        if not ranked_lists:
            return RetrievalResult(
                query=query,
                chunks=[],
                confidence=0.0,
                refused=True,
                reason="no candidates retrieved",
                backend=settings.describe_backends(),
            )

        fused = rrf(ranked_lists, k=cfg.rrf_k, weights=weights)[: cfg.fused_top_k]
        candidate_ids = [cid for cid, _ in fused]
        candidates = [self._by_id[cid] for cid in candidate_ids if cid in self._by_id]

        # Chapter-title boost: the same title-overlap signal that fixed syllabus
        # linking, reused as a ranking prior. A chunk from a chapter whose TITLE
        # matches the concept is pushed up. Added to the ranking score only -- the
        # refusal confidence stays the reranker's pool maximum, so the gate is
        # untouched by this.
        concept = decomp.concept or query if decomp else query

        def title_boost(chunk: Chunk) -> float:
            if cfg.chapter_boost_weight <= 0.0:
                return 0.0
            sim = title_similarity(concept, chunk.chapter_title or "")
            return cfg.chapter_boost_weight * sim

        # Two uses of the reranker, fed different queries on purpose:
        #  * ORDERING uses the full query -- it carries all the relevance signal,
        #    and dropping the non-concept clauses measurably hurt ranking (r@10).
        #  * the refusal CONFIDENCE uses the concept alone on a compound question.
        #    The full string ("...how does it play into the real world? give me an
        #    example...") is the very noise decomposition removes, and scoring it
        #    depressed the confidence the gate reads, false-refusing a question the
        #    materials answer. For a simple query concept == query, so confidence is
        #    computed exactly as before and the calibrated threshold still applies.
        compound = bool(decomp and decomp.is_compound and decomp.concept)

        fused_scores = dict(fused)
        pool_confidence: Optional[float] = None
        if cfg.use_rerank and candidates:
            if cfg.rerank_mode == "rank":
                scores = self.reranker.score(query, candidates)
                if compound:
                    concept_scores = self.reranker.score(decomp.concept, candidates)
                    pool_confidence = max(concept_scores) if concept_scores else 0.0
                else:
                    pool_confidence = max(scores) if scores else 0.0
                order = sorted(
                    range(len(candidates)),
                    key=lambda i: -(scores[i] + title_boost(candidates[i])),
                )
                ranked = [(candidates[i], scores[i], scores[i]) for i in order][:top_k]
            else:
                # Keep the fused ordering (optionally boosted); score only the head.
                if cfg.chapter_boost_weight > 0.0:
                    order = sorted(
                        range(len(candidates)),
                        key=lambda i: -(fused_scores.get(candidates[i].id, 0.0) + title_boost(candidates[i])),
                    )
                    candidates = [candidates[i] for i in order]
                head = candidates[:top_k]
                scores = self.reranker.score(query, head)
                ranked = [
                    (c, fused_scores.get(c.id, 0.0), s) for c, s in zip(head, scores)
                ]
        else:
            if cfg.chapter_boost_weight > 0.0:
                candidates = sorted(
                    candidates,
                    key=lambda c: -(fused_scores.get(c.id, 0.0) + title_boost(c)),
                )
            ranked = [(c, fused_scores.get(c.id, 0.0), None) for c in candidates[:top_k]]

        # Parent text is what gets displayed; the child is what matched.
        parent_lookup: dict[str, str] = {}
        if cfg.use_parent and ranked:
            hydrated = self.store.get_chunks([c.id for c, _, _ in ranked], with_parents=True)
            parent_lookup = {c.id: c.parent_text for c in hydrated}

        scored: list[ScoredChunk] = []
        for chunk, score, rerank_score in ranked:
            display = replace(chunk, parent_text=parent_lookup.get(chunk.id, "") if cfg.use_parent else "")
            scored.append(
                ScoredChunk(
                    chunk=display,
                    score=float(score),
                    dense_rank=dense_pos.get(chunk.id),
                    sparse_rank=sparse_pos.get(chunk.id),
                    rerank_score=None if rerank_score is None else float(rerank_score),
                )
            )

        # A chapter the corpus does not have. "Summarise chapter 62" against a
        # 37-chapter book retrieves plausible passages and scores well, because
        # every one of them is about the right subject -- the *number* is the only
        # thing wrong, and only the corpus knows the range.
        if cfg.use_intent_gate and (out_of_range := self._chapter_out_of_range(query, course_id)):
            return RetrievalResult(
                query=query, chunks=scored, confidence=0.0, refused=True,
                reason=out_of_range, gate_refused=True,
                backend=settings.describe_backends(),
            )

        # Confidence is the reranker's best score over the whole candidate pool,
        # computed before the boost reordered the head, so the refusal gate sees
        # exactly the value it saw before decomposition/boost were added.
        confidence = (
            pool_confidence if pool_confidence is not None
            else self._confidence(scored, cfg)
        )

        # Relevance and intent are separate signals; see api/retrieval/intent.py.
        # A question whose topic is in the corpus but whose answer is not scores
        # high on relevance, so the threshold alone cannot catch it.
        gate_refused = False
        refused = confidence < cfg.refusal_threshold
        reason = (
            f"top passage scored {confidence:.2f}, below the calibrated "
            f"threshold of {cfg.refusal_threshold:.2f}"
            if refused
            else ""
        )
        if cfg.use_intent_gate:
            intent, detail = classify(query)
            if intent is Intent.OUT_OF_SCOPE:
                refused, gate_refused, reason = True, True, detail
            elif intent is Intent.QUANTITATIVE_REQUEST and not self._has_data_link(course_id):
                # "Show me the trend in enslaved household size", "what is the
                # correlation between positionality and research validity" --
                # the topic is in the readings, so relevance scores them highly,
                # but no series exists to compute over. Relevance cannot see that;
                # the course's capability flag can.
                refused, gate_refused, reason = True, True, detail
            elif intent is Intent.NEEDS_LIVE_DATA:
                # Previously this fell through so the agent could route it to
                # fetch_series. With the data tools removed there is no path to an
                # answer, so asking for a current value is now a refusal.
                refused, gate_refused, reason = True, True, detail

        return RetrievalResult(
            query=query,
            chunks=scored,
            confidence=confidence,
            refused=refused,
            reason=reason,
            gate_refused=gate_refused,
            backend=settings.describe_backends(),
        )

    # -- decomposition & fusion -------------------------------------------

    def _decompose(self, query: str, cfg: PipelineConfig) -> Optional[Decomposition]:
        if not cfg.decompose:
            return None
        llm = None
        if cfg.decompose_method == "llm":
            if self._decompose_llm is None:
                from ..llm import get_llm

                self._decompose_llm = get_llm()
            llm = self._decompose_llm
        return decompose(query, llm=llm, prefer_llm=(cfg.decompose_method == "llm"))

    def _one_query_lists(
        self, text: str, cfg: PipelineConfig, allowed: Optional[set[str]]
    ) -> tuple[list[str], list[str]]:
        """Dense and sparse id lists for a single query string, course-scoped."""
        dense_hits = (
            self.dense.search(text, cfg.dense_top_k * (3 if allowed else 1))
            if cfg.use_dense else []
        )
        sparse_hits = (
            self.sparse.search(text, cfg.sparse_top_k * (3 if allowed else 1))
            if cfg.use_sparse else []
        )
        if allowed is not None:
            dense_hits = [h for h in dense_hits if h[0] in allowed][: cfg.dense_top_k]
            sparse_hits = [h for h in sparse_hits if h[0] in allowed][: cfg.sparse_top_k]
        return [cid for cid, _ in dense_hits], [cid for cid, _ in sparse_hits]

    def _retrieval_lists(
        self,
        query: str,
        decomp: Optional[Decomposition],
        cfg: PipelineConfig,
        allowed: Optional[set[str]],
    ) -> tuple[list[list[str]], list[float], dict[str, int], dict[str, int]]:
        """Build the ranked id lists to fuse, with per-list RRF weights.

        Single-query: one dense + one sparse list (weight 1). Compound: dense and
        sparse per sub-query, each weighted by the sub-query's weight, so the
        concept contributes twice the framing. `dense_pos`/`sparse_pos` report the
        best (concept) ranks for provenance display.
        """
        subqueries = (
            decomp.subqueries if (decomp and decomp.is_compound)
            else [None]  # sentinel: use the original query
        )

        lists: list[list[str]] = []
        weights: list[float] = []
        dense_pos: dict[str, int] = {}
        sparse_pos: dict[str, int] = {}
        for sub in subqueries:
            text = sub.text if sub is not None else query
            weight = sub.weight if sub is not None else 1.0
            dense_ids, sparse_ids = self._one_query_lists(text, cfg, allowed)
            if cfg.use_dense and dense_ids:
                lists.append(dense_ids)
                weights.append(weight)
                for cid, pos in rank_positions(dense_ids).items():
                    dense_pos.setdefault(cid, pos)
            if cfg.use_sparse and sparse_ids:
                lists.append(sparse_ids)
                weights.append(weight)
                for cid, pos in rank_positions(sparse_ids).items():
                    sparse_pos.setdefault(cid, pos)
        return lists, weights, dense_pos, sparse_pos

    def _has_data_link(self, course_id: Optional[str]) -> bool:
        """The course's derived capability, cached per pipeline.

        With no course selected we assume a data link exists, so that a
        quantitative question is never refused on a capability we did not
        actually check.
        """
        if course_id is None:
            return True
        if course_id not in self._data_link_cache:
            course = self.store.course(self.user_id, course_id)
            self._data_link_cache[course_id] = bool(course and course.has_data_link)
        return self._data_link_cache[course_id]

    def _chapter_out_of_range(self, query: str, course_id: Optional[str]) -> str:
        """Return a reason when the query names a chapter beyond what exists."""
        mentioned = [r.chapter for r in parse_chapter_mentions(query, course_id or "X")]
        if not mentioned:
            return ""
        scope = [
            c for c in self._chunks
            if (course_id is None or c.course_id == course_id) and c.chapter_num
        ]
        if not scope:
            return ""
        highest = max(c.chapter_num for c in scope)
        beyond = [n for n in mentioned if n > highest]
        if not beyond:
            return ""
        return (
            f"asks about chapter {beyond[0]}, but the loaded materials for this "
            f"course only go up to chapter {highest}"
        )

    @staticmethod
    def _confidence(scored: Sequence[ScoredChunk], cfg: PipelineConfig) -> float:
        """Confidence is the reranker's top score when reranking is on.

        Without reranking there is no calibrated scale available -- RRF scores are
        a function of list length, not of relevance -- so the pipeline reports 1.0
        and refuses nothing. That is a real limitation of the no-rerank ablation
        rows and is stated as such in docs/EVAL.md rather than papered over.
        """
        if not scored:
            return 0.0
        if not cfg.use_rerank:
            return 1.0
        # In "confidence" mode the fused order is kept, so the best calibration
        # signal is the strongest reranker score in the returned head, not
        # necessarily the score of the first passage.
        scores = [s.rerank_score for s in scored if s.rerank_score is not None]
        return max(scores) if scores else 0.0

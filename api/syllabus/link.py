"""Link syllabus rows to textbook chapters by meaning, not by number.

Chapter-number matching is wrong in the normal case, not the edge case. A
syllabus citing Varian 10e against a PDF of the 8e has correct chapter *titles*
and wrong chapter *numbers*; a syllabus that writes "Week 5: Consumer Choice"
states no number at all. Either way the number is the least reliable thing on the
row, so it is demoted to a weak tie-breaker and similarity does the work:

    syllabus row topic text  vs  chapter title
      primary:   token overlap (Jaccard + containment)
      backup:    embedding cosine, for genuine paraphrases
      tiebreak:  a small bonus if a stated number happens to agree
    -> best chapter, with a confidence score

Measured with `scripts/check_linking.py` on the ECON 303 outline (10e numbers vs an
8e PDF). The title signal decisively beats number matching where they disagree --
"Technology (ch. 19)" binds to *Profit Maximization* by number but not by title.
Absolute link rates depend on the embedder's score scale: the confidence floor and
ambiguity margin here were set for TF-IDF and are conservative under BGE (more rows
flagged for review), which is a calibration item, not a linking-logic one -- run
the script for the live numbers rather than trusting a figure pasted here.

Rows below the confidence floor are flagged for the onboarding review screen,
alongside the low-confidence rows from the double-extraction diff. A wrong link
is worse than an unlinked row: it silently attributes engagement and exam
readiness to the wrong chapter, and nothing downstream can detect it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from ..embed import Embedder
from ..ids import ChapterRef, parse_chapter_mentions

# Below this, a row is flagged rather than linked. Chosen so that an unrelated
# topic does not silently bind to the nearest chapter; measured in
# scripts/check_linking.py against real syllabi.
CONFIDENCE_FLOOR = 0.34

# A stated chapter number agrees with the candidate: a nudge, never a decision.
# Capped low on purpose -- if the number could outvote the title, the 10e-vs-8e
# case would resolve to the wrong chapter with high confidence.
NUMBER_AGREEMENT_BONUS = 0.08

# Embedding similarity is rescaled before competing with title overlap: on a
# corpus-fitted local embedder a good match scores ~0.25, so unscaled it could
# never win a tie it deserved.
EMBEDDING_WEIGHT = 1.5

_CH_PARENS = re.compile(r"\(\s*ch\.?\s*\d+\s*\)|\bch\.?\s*\d+\b", re.I)


def _title_tokens(text: str) -> set[str]:
    """Content tokens of a title or a syllabus topic, with the chapter reference
    stripped -- "(ch. 19)" is provenance, not subject matter."""
    from ..retrieval.sparse import tokenize

    return set(tokenize(_CH_PARENS.sub(" ", text or "")))


def title_similarity(query: str, title: str) -> float:
    """Blend of Jaccard and containment.

    Containment carries the paraphrase case: "Week 5: Consumer Choice" against a
    chapter titled "Choice" shares every token of the shorter side, which Jaccard
    alone would discount to 0.33.

    Public because it is reused as a retrieval prior (the chapter-title boost in
    `api/retrieval/pipeline.py`), not only for syllabus linking.
    """
    q, t = _title_tokens(query), _title_tokens(title)
    if not q or not t:
        return 0.0
    overlap = len(q & t)
    if not overlap:
        return 0.0
    jaccard = overlap / len(q | t)
    containment = overlap / min(len(q), len(t))
    return 0.5 * jaccard + 0.5 * containment


@dataclass
class ChapterCandidate:
    course_id: str
    source_id: str
    chapter_num: int
    title: str
    text: str = ""          # title + sections + opening prose, for embedding
    embedding: Optional[np.ndarray] = None

    @property
    def ref(self) -> str:
        return str(ChapterRef(self.course_id, self.source_id, self.chapter_num))


@dataclass
class LinkResult:
    chapter_ref: Optional[str]
    chapter_title: str
    score: float
    method: str
    needs_review: bool
    runner_up: Optional[tuple[str, float]] = None

    def as_dict(self) -> dict[str, object]:
        return {
            "chapter_ref": self.chapter_ref,
            "chapter_title": self.chapter_title,
            "score": round(self.score, 4),
            "method": self.method,
            "needs_review": self.needs_review,
            "runner_up": self.runner_up,
        }


class ChapterLinker:
    """Embeds a course's chapters once, then links many syllabus rows against it."""

    def __init__(
        self,
        candidates: Sequence[ChapterCandidate],
        embedder: Embedder,
        floor: float = CONFIDENCE_FLOOR,
    ):
        self.candidates = list(candidates)
        self.embedder = embedder
        self.floor = floor
        self._matrix: Optional[np.ndarray] = None
        if self.candidates:
            self._matrix = self._embed_candidates()

    def _embed_candidates(self) -> np.ndarray:
        texts = [c.text or c.title or f"Chapter {c.chapter_num}" for c in self.candidates]
        matrix = self.embedder.embed(texts)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms
        for candidate, vector in zip(self.candidates, matrix):
            candidate.embedding = vector
        return matrix

    def link(
        self,
        topic: str,
        readings: Optional[Sequence[str]] = None,
        course_id: Optional[str] = None,
    ) -> LinkResult:
        """Link one syllabus row. `topic` and `readings` are free text."""
        if not self.candidates or self._matrix is None:
            return LinkResult(None, "", 0.0, "no-chapters", needs_review=False)

        query = " ".join([topic or ""] + list(readings or [])).strip()
        if not query:
            return LinkResult(None, "", 0.0, "empty-row", needs_review=True)

        # Title similarity is the PRIMARY signal, embedding similarity the backup.
        #
        # Chapter titles are short proper names ("Cost Curves", "Technology") and
        # a syllabus row usually restates them almost verbatim. Comparing two
        # short strings token-wise is both more accurate and more inspectable
        # than comparing them in a vector space fitted on 400-token chunks: that
        # space cannot represent a three-word query at all, and scored 3/16 on
        # this syllabus against 16/16 for token overlap. Embeddings still earn
        # their place on genuine paraphrases ("Consumer Choice" -> "Choice"),
        # where they break ties the tokens leave open.
        lexical = np.array([title_similarity(query, c.title) for c in self.candidates])

        embedded = np.zeros(len(self.candidates))
        vector = self.embedder.embed_query(query)
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            embedded = self._matrix @ (vector / norm)

        scores = np.maximum(lexical, EMBEDDING_WEIGHT * embedded)

        # The stated number is a tie-breaker only.
        stated = {
            ref.chapter
            for ref in parse_chapter_mentions(query, course_id or "X")
        }
        method = "semantic"
        if stated:
            bonus = np.array(
                [
                    NUMBER_AGREEMENT_BONUS if c.chapter_num in stated else 0.0
                    for c in self.candidates
                ]
            )
            if bonus.any():
                scores = scores + bonus
                method = "semantic+number"

        order = np.argsort(-scores)
        best_index = int(order[0])
        best = self.candidates[best_index]
        best_score = float(scores[best_index])
        runner: Optional[tuple[str, float]] = None
        if len(order) > 1:
            second = self.candidates[int(order[1])]
            runner = (second.title or f"Ch {second.chapter_num}", float(scores[int(order[1])]))

        # An ambiguous top-two is as much a reason to review as a low top score:
        # two chapters scoring alike means the title text did not discriminate.
        ambiguous = runner is not None and (best_score - runner[1]) < 0.02
        needs_review = best_score < self.floor or ambiguous
        if ambiguous and best_score >= self.floor:
            method += "/ambiguous"

        return LinkResult(
            chapter_ref=best.ref if not needs_review else None,
            chapter_title=best.title,
            score=best_score,
            method=method,
            needs_review=needs_review,
            runner_up=runner,
        )


def number_only_link(
    topic: str,
    readings: Sequence[str],
    candidates: Sequence[ChapterCandidate],
    course_id: str,
) -> LinkResult:
    """The old behaviour, kept solely as a comparison baseline.

    Used by scripts/check_linking.py to measure what semantic linking buys on
    real syllabi. Not used by the product.
    """
    query = " ".join([topic or ""] + list(readings or []))
    stated = [ref.chapter for ref in parse_chapter_mentions(query, course_id)]
    by_num = {c.chapter_num: c for c in candidates}
    for number in stated:
        if number in by_num:
            hit = by_num[number]
            return LinkResult(hit.ref, hit.title, 1.0, "number-only", needs_review=False)
    return LinkResult(None, "", 0.0, "number-only-miss", needs_review=True)

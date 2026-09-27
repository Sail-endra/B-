"""Domain types shared across ingest, retrieval, the agent and the API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from .math_text import canonicalize_math


class SourceType(str, Enum):
    TEXTBOOK = "textbook"
    SLIDES = "slides"
    NOTES = "notes"
    READINGS = "readings"
    SYLLABUS = "syllabus"
    # Text exported from the separate BB Plus Study Library. It enters the same
    # retrieval corpus as uploaded material, but keeps its origin identifiable.
    BBPLUS = "bbplus"
    # Problem sets, worksheets, past exams. Indexed separately as practice
    # problems, NEVER added to the retrieval corpus -- an uploaded homework must
    # not be citable as if it were the textbook.
    ASSESSMENT = "assessment"


class BlockKind(str, Enum):
    """Chunker block classification. Tables, equations and numbered definitions are
    never split -- a half-equation retrieves as noise and reads as an error."""

    PROSE = "prose"
    EQUATION = "equation"
    TABLE = "table"
    DEFINITION = "definition"


@dataclass
class Course:
    """A course exists because a user uploaded something. Never seeded."""

    course_id: str
    user_id: str
    code: str
    title: str
    term: str = ""
    # Derived at ingest from corpus content -- see api/corpus/capability.py.
    # Never set from a course-code list: codes differ at every school.
    has_data_link: bool = False
    data_link_reason: str = ""


@dataclass
class Source:
    source_id: str
    user_id: str
    course_id: str
    title: str
    type: SourceType
    file: str
    pages: int = 0
    # Ingest outcome, surfaced in the upload UI. "failed" means the file was
    # image-only and OCR could not read it -- never a silent empty ingest.
    status: str = "ok"
    status_detail: str = ""
    ocr_pages: int = 0
    file_hash: str = ""


@dataclass
class Chunk:
    """A retrievable child chunk. `parent_text` is what gets shown; `embed_text`
    (with the contextual prefix) is what gets embedded."""

    id: str
    user_id: str
    source_id: str
    course_id: str
    chapter_num: int
    chapter_title: str
    section: str
    page_start: int
    page_end: int
    text: str
    parent_id: Optional[str] = None
    parent_text: str = ""
    kind: BlockKind = BlockKind.PROSE
    token_count: int = 0
    embedding: Optional[list[float]] = None

    @property
    def chapter_ref(self) -> str:
        return f"{self.course_id}:{self.source_id}:{self.chapter_num}"

    @property
    def embed_text(self) -> str:
        """Contextual prefix improves retrieval on chapter-phrased questions and is
        deliberately excluded from the displayed text."""
        head = f"[{self.course_id} / Chapter {self.chapter_num}: {self.chapter_title}"
        if self.section:
            head += f" / {self.section}"
        return f"{head}] {self.text}"

    def citation(self) -> "Citation":
        return Citation(
            chunk_id=self.id,
            source_id=self.source_id,
            course_id=self.course_id,
            chapter_num=self.chapter_num,
            chapter_title=self.chapter_title,
            page_start=self.page_start,
            page_end=self.page_end,
        )


@dataclass
class Citation:
    chunk_id: str
    source_id: str
    course_id: str
    chapter_num: int
    chapter_title: str
    page_start: int
    page_end: int

    def label(self) -> str:
        pages = (
            f"p. {self.page_start}"
            if self.page_start == self.page_end
            else f"pp. {self.page_start}-{self.page_end}"
        )
        # Journal articles and standalone readings have no chapters. Printing
        # "Ch 0" on every ANTH citation reads as a bug to the student and makes a
        # correct citation look untrustworthy.
        if not self.chapter_num:
            return f"[{self.course_id}, {pages}]"
        return f"[{self.course_id}, Ch {self.chapter_num}, {pages}]"

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["label"] = self.label()
        return out


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float
    dense_rank: Optional[int] = None
    sparse_rank: Optional[int] = None
    rerank_score: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.id,
            "course_id": self.chunk.course_id,
            "chapter_num": self.chunk.chapter_num,
            "chapter_title": self.chunk.chapter_title,
            "section": self.chunk.section,
            "text": canonicalize_math(self.chunk.parent_text or self.chunk.text),
            "snippet": canonicalize_math(self.chunk.text),
            "score": round(self.score, 4),
            "rerank_score": None if self.rerank_score is None else round(self.rerank_score, 4),
            "citation": self.chunk.citation().to_dict(),
        }


@dataclass
class RetrievalResult:
    query: str
    chunks: list[ScoredChunk]
    confidence: float
    refused: bool
    reason: str = ""
    backend: dict[str, str] = field(default_factory=dict)
    # True when the intent gate or a corpus-range check refused this query on
    # grounds independent of the score threshold.
    #
    # Exposed so the calibration harness can compose
    #   refused = (confidence < threshold) or gate_refused
    # by reading the pipeline's own verdict instead of re-deriving it. Two
    # separate measurement bugs came from the harness modelling a subset of the
    # pipeline's refusal logic; there is now one source of truth.
    gate_refused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "passages": [c.to_dict() for c in self.chunks],
            "confidence": round(self.confidence, 4),
            "refused": self.refused,
            "gate_refused": self.gate_refused,
            "reason": self.reason,
            "backend": self.backend,
        }


# --------------------------------------------------------------------------
# Syllabus / dashboard
# --------------------------------------------------------------------------


class AssessmentKind(str, Enum):
    """The types extraction must classify into -- not merely detect. Ordered so
    the exam-like kinds group together for readiness rollups."""
    EXAM = "exam"
    QUIZ = "quiz"
    HOMEWORK = "homework"
    PROBLEM_SET = "problem_set"
    PAPER = "paper"
    PROJECT = "project"
    READING_RESPONSE = "reading_response"
    PRESENTATION = "presentation"
    # Kept so old extracted rows and dashboard exam-readiness still resolve.
    MIDTERM = "midterm"
    FINAL = "final"

    @property
    def is_exam(self) -> bool:
        return self in {AssessmentKind.EXAM, AssessmentKind.QUIZ,
                        AssessmentKind.MIDTERM, AssessmentKind.FINAL}

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


class AssessmentStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DONE = "done"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


@dataclass
class Meeting:
    # Optional: an undated "Course outline" still yields meetings, they simply
    # carry no date until one is known. The dashboard renders these separately.
    date: Optional[date]
    topic: str
    chapter_refs: list[str] = field(default_factory=list)
    readings: list[str] = field(default_factory=list)
    confidence: str = "high"


@dataclass
class Assessment:
    date: Optional[date]
    kind: AssessmentKind
    covers: list[str] = field(default_factory=list)
    weight: float = 0.0
    title: str = ""
    confidence: str = "high"


@dataclass
class AssessmentRecord:
    """A gradable item as stored: classified kind, optional due date, user-set
    status, and a flag for rows a person added or corrected. This is the source
    of truth for the assessments list and the .ics feed, distinct from the
    transient `Assessment` produced during extraction."""

    id: str
    user_id: str
    course_id: str
    kind: AssessmentKind
    title: str
    due_date: Optional[date] = None
    weight: float = 0.0
    chapter_refs: list[str] = field(default_factory=list)
    status: AssessmentStatus = AssessmentStatus.NOT_STARTED
    user_entered: bool = False
    source: str = "extracted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "kind": self.kind.value,
            "kind_label": self.kind.label,
            "title": self.title,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "weight": self.weight,
            "chapter_refs": self.chapter_refs,
            "status": self.status.value,
            "status_label": self.status.label,
            "user_entered": self.user_entered,
            "source": self.source,
        }


@dataclass
class SyllabusExtraction:
    course_id: str
    code: str
    title: str
    term_start: Optional[date]
    term_end: Optional[date]
    meeting_days: list[str] = field(default_factory=list)
    meeting_time: str = ""
    meetings: list[Meeting] = field(default_factory=list)
    assessments: list[Assessment] = field(default_factory=list)


@dataclass
class Engagement:
    user_id: str
    chapter_ref: str
    questions_asked: int = 0
    ps_questions_hit: int = 0
    notes_exported: int = 0
    last_touched_at: Optional[datetime] = None

    def score(self) -> float:
        """Derived, never clicked. Saturating so one obsessive chapter does not
        wash out the scale."""
        raw = self.questions_asked + 2.0 * self.ps_questions_hit + 1.5 * self.notes_exported
        return min(1.0, raw / 10.0)


@dataclass
class AgentStep:
    index: int
    kind: str  # "tool_call" | "tool_result" | "text" | "error"
    name: str = ""
    detail: str = ""
    payload: Any = None
    elapsed_ms: int = 0


@dataclass
class AgentAnswer:
    question: str
    answer: str
    citations: list[Citation]
    steps: list[AgentStep]
    refused: bool
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    truncated: bool = False
    # Explanation layer: a generative gloss on top of the verbatim answer. Empty
    # when no model is configured -- the extractive answer stands on its own.
    explanation: str = ""
    depth: str = "concise"
    explained: bool = False
    # Which backend produced the answer, shown in the UI so an extractive
    # fallback can never masquerade as a generated explanation.
    backend: str = "extractive"

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": canonicalize_math(self.answer),
            "citations": [c.to_dict() for c in self.citations],
            "steps": [asdict(s) for s in self.steps],
            "refused": self.refused,
            "artifacts": self.artifacts,
            "latency_ms": self.latency_ms,
            "truncated": self.truncated,
            "explanation": canonicalize_math(self.explanation),
            "depth": self.depth,
            "explained": self.explained,
            "backend": self.backend,
        }


@dataclass
class Problem:
    """A practice problem. `origin` gates the integrity line: an uploaded problem
    never exposes a final answer; a generated variant does. `verified` is true
    only when sympy independently reproduced the answer."""

    id: str
    user_id: str
    course_id: str
    prompt: str
    origin: str = "uploaded"            # uploaded | generated
    source_id: str = ""
    parent_id: str = ""
    number: str = ""
    type: str = "short_answer"
    topic: str = ""
    chapter_ref: str = ""
    difficulty: str = "standard"
    given_solution: str = ""
    answer: str = ""
    solution_steps: str = ""
    verified: bool = False
    verify_method: str = ""
    needs_review: bool = False

    def public(self, *, with_solution: bool = False) -> dict[str, Any]:
        """Serialised for the client. The final answer and worked steps are
        withheld unless explicitly released -- and NEVER for an uploaded original,
        which the caller enforces structurally."""
        out = {
            "id": self.id, "course_id": self.course_id, "origin": self.origin,
            "number": self.number, "prompt": canonicalize_math(self.prompt), "type": self.type,
            "topic": self.topic, "chapter_ref": self.chapter_ref,
            "difficulty": self.difficulty, "verified": self.verified,
            "verify_method": self.verify_method, "needs_review": self.needs_review,
        }
        if with_solution and self.origin == "generated":
            out["answer"] = canonicalize_math(self.answer)
            out["solution_steps"] = canonicalize_math(self.solution_steps)
        return out

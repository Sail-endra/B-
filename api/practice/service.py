"""Ingest an uploaded assessment into practice problems -- never into retrieval.

The flow, entirely separate from `ingest_files`:

    extract text -> parse into problems (uploaded originals)
      -> link each to a chapter by title overlap
      -> generate variants per original -> sympy-verify -> keep or discard
      -> store originals and kept variants in `practice_problems`

Nothing here writes to `chunks`, so an uploaded homework can never be retrieved
or cited as if it were the textbook. `origin='uploaded'` on the originals is what
the integrity line keys on: those never expose a final answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..ids import ChapterRef
from ..models import Problem, Source, SourceType
from ..retrieval.sparse import tokenize
from ..store import SQLiteStore
from ..syllabus.link import title_similarity
from ingest.extract import extract

_LINK_FLOOR = 0.30
_GENERIC_CHAPTER_TERMS = set(tokenize(
    "curve curves line lines graph graphs function functions equation equations "
    "set sets problem problems example examples"
))


@dataclass
class AssessmentReport:
    source_id: str
    filename: str
    parsed: int = 0
    needs_review: int = 0
    variants_kept: int = 0
    variants_discarded: int = 0
    variants_generated: int = 0
    unverified: int = 0
    status: str = "ok"
    detail: str = ""

    @property
    def discard_rate(self) -> float:
        return self.variants_discarded / self.variants_generated if self.variants_generated else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "filename": self.filename,
            "parsed": self.parsed, "needs_review": self.needs_review,
            "variants_kept": self.variants_kept,
            "variants_discarded": self.variants_discarded,
            "variants_generated": self.variants_generated,
            "unverified": self.unverified,
            "discard_rate": round(self.discard_rate, 3),
            "status": self.status, "detail": self.detail,
        }


def _chapter_linker(store: SQLiteStore, user_id: str, course_id: str):
    chapters = store.chapters(user_id, course_id)
    def link(topic: str, prompt: str) -> str:
        if not chapters:
            return ""
        query = f"{topic} {prompt[:120]}"
        query_terms = set(tokenize(query)) - _GENERIC_CHAPTER_TERMS
        best, best_score = None, 0.0
        for ch in chapters:
            # A generic word such as "curves" must not bind an indifference
            # curve question to a chapter titled "Cost Curves". Require at
            # least one topic-specific shared term before trusting the overlap.
            title_terms = set(tokenize(ch["title"])) - _GENERIC_CHAPTER_TERMS
            if not query_terms.intersection(title_terms):
                continue
            score = title_similarity(query, ch["title"])
            if score > best_score:
                best, best_score = ch, score
        if best and best_score >= _LINK_FLOOR:
            return str(ChapterRef(course_id, best["source_id"], best["chapter_num"]))
        return ""
    return link


def ingest_assessment(
    path: Path, *, user_id: str, course_id: str, store: SQLiteStore, llm: Any,
    variants_per_problem: int = 2,
    on_progress: Optional[Callable[..., None]] = None,
) -> AssessmentReport:
    filename = path.name
    from ..corpus.ingest_service import _source_id, _sha256

    source_id = _source_id(course_id, filename)
    report = AssessmentReport(source_id=source_id, filename=filename)

    def prog(**patch):
        if on_progress:
            on_progress(0, **patch)

    prog(stage="reading", pages_done=0, pages_total=0)
    source = Source(source_id=source_id, user_id=user_id, course_id=course_id,
                    title=Path(filename).stem.replace("_", " ")[:80],
                    type=SourceType.ASSESSMENT, file=str(path))
    try:
        source.file_hash = _sha256(path)
    except Exception:  # noqa: BLE001
        source.file_hash = ""

    try:
        doc = extract(path, source_id, on_page=lambda st, d, t: prog(stage=st, pages_done=d, pages_total=t))
    except Exception as exc:  # noqa: BLE001
        source.status, source.status_detail = "failed", f"could not read: {exc}"[:160]
        store.upsert_source(source)
        report.status, report.detail = "failed", source.status_detail
        prog(stage="failed", status="failed", detail=report.detail)
        return report

    text = "\n\n".join(seg.text for seg in doc.segments)
    source.pages = doc.page_count
    store.upsert_source(source)
    store.clear_problems_for_source(user_id, source_id)

    if not getattr(llm, "available", False):
        report.status, report.detail = "failed", "AI is required to parse problems; turn on AI or add a key"
        prog(stage="failed", status="failed", detail=report.detail)
        return report

    prog(stage="parsing")
    from .parse import parse_problems
    from .generate import generate_variants

    parsed = parse_problems(text, llm)
    report.parsed = len(parsed)
    link = _chapter_linker(store, user_id, course_id)

    originals: list[Problem] = []
    for pr in parsed:
        chapter_ref = link(pr["topic"], pr["prompt"])
        original = Problem(
            id=uuid.uuid4().hex, user_id=user_id, course_id=course_id,
            source_id=source_id, origin="uploaded", number=pr["number"],
            prompt=pr["prompt"], type=pr["type"], topic=pr["topic"],
            chapter_ref=chapter_ref, difficulty=pr["difficulty"],
            given_solution=pr["given_solution"], needs_review=pr["needs_review"],
        )
        originals.append(original)
        if pr["needs_review"]:
            report.needs_review += 1

    to_store = list(originals)
    prog(stage="generating", pages_done=0, pages_total=len(originals))
    for i, original in enumerate(originals, 1):
        if original.needs_review:
            # Do not multiply a low-confidence parse into polished-looking
            # practice content. The original remains visible for human review.
            prog(stage="generating", pages_done=i, pages_total=len(originals))
            continue
        variants = generate_variants(
            {"topic": original.topic, "prompt": original.prompt, "type": original.type,
             "given_solution": original.given_solution},
            llm, n=variants_per_problem)
        report.variants_generated += len(variants)
        for v in variants:
            if v.get("_discarded"):
                report.variants_discarded += 1
                continue
            if not v.get("verified"):
                report.unverified += 1
            else:
                report.variants_kept += 1
            to_store.append(Problem(
                id=uuid.uuid4().hex, user_id=user_id, course_id=course_id,
                source_id=source_id, origin="generated", parent_id=original.id,
                prompt=v["prompt"], type=v.get("type", "numeric"),
                topic=original.topic, chapter_ref=original.chapter_ref,
                difficulty=original.difficulty, answer=v.get("answer", ""),
                solution_steps=v.get("solution_steps", ""),
                verified=bool(v.get("verified")),
                verify_method=v.get("verify_method", "unverified"),
            ))
        prog(stage="generating", pages_done=i, pages_total=len(originals))

    store.insert_problems(to_store)
    prog(stage="done", status="ok",
         detail=(f"{report.parsed} problems · {report.needs_review} need review · "
                 f"{report.variants_kept} verified variants · "
                 f"{report.variants_discarded}/{report.variants_generated} discarded · "
                 f"{report.unverified} unverified"))
    return report

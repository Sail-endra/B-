"""Commit an extracted syllabus into `syllabus_schedule`, linking rows to chapters.

Kept separate from extraction so the review screen sits between them. Two kinds
of uncertainty reach that screen from here:

  * low-confidence *extraction* -- the two prompts disagreed about the row
  * low-confidence *linking*    -- the row's topic did not clearly match a chapter

Both are flagged on the row, with the score and what it matched against, so the
user is correcting a specific claim rather than re-reading the whole schedule.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional, Sequence

from ..models import AssessmentRecord, SyllabusExtraction
from ..store import SQLiteStore
from .link import ChapterCandidate, ChapterLinker, LinkResult


def build_linker(
    store: SQLiteStore, user_id: str, course_id: str
) -> Optional[ChapterLinker]:
    """Load the course's embedded chapter titles. None when no materials exist --
    a syllabus-only course still gets a working schedule, just with no links."""
    rows = store.chapters(user_id, course_id)
    if not rows:
        return None
    from ..embed import get_embedder
    from ..corpus.ingest_service import embedder_state_path

    embedder = get_embedder()
    if embedder.stateful and not embedder.load(embedder_state_path(user_id)):
        return None
    candidates = [
        ChapterCandidate(
            course_id=row["course_id"],
            source_id=row["source_id"],
            chapter_num=row["chapter_num"],
            title=row["title"],
            text=row["title"],
        )
        for row in rows
    ]
    linker = ChapterLinker(candidates, embedder)
    # Reuse the vectors computed at ingest rather than re-embedding titles.
    import numpy as np

    stored = [row["embedding"] for row in rows]
    if all(v is not None for v in stored):
        matrix = np.vstack(stored).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        linker._matrix = matrix / norms
    return linker


def to_rows(
    extraction: SyllabusExtraction,
    user_id: str,
    linker: Optional[ChapterLinker] = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def link_for(topic: str, readings: Sequence[str]) -> LinkResult:
        if linker is None:
            return LinkResult(None, "", 0.0, "no-materials", needs_review=False)
        return linker.link(topic, readings, extraction.course_id)

    for meeting in extraction.meetings:
        result = link_for(meeting.topic, meeting.readings)
        rows.append(
            {
                "id": uuid.uuid4().hex,
                "user_id": user_id,
                "course_id": extraction.course_id,
                "date": meeting.date.isoformat() if meeting.date else None,
                "kind": "lecture",
                "topic": meeting.topic,
                "chapter_refs": [result.chapter_ref] if result.chapter_ref else [],
                "readings": meeting.readings,
                "weight": 0.0,
                "confidence": meeting.confidence,
                "link_score": result.score,
                "link_method": result.method,
                "link_title": result.chapter_title,
                # Either kind of uncertainty sends the row to review.
                "needs_review": int(result.needs_review or meeting.confidence == "low"),
            }
        )

    for assessment in extraction.assessments:
        covered: list[str] = []
        scores: list[float] = []
        flagged = False
        titles: list[str] = []
        for item in assessment.covers or [assessment.title]:
            result = link_for(item, [])
            if result.chapter_ref:
                covered.append(result.chapter_ref)
                titles.append(result.chapter_title)
            scores.append(result.score)
            flagged = flagged or result.needs_review
        rows.append(
            {
                "id": uuid.uuid4().hex,
                "user_id": user_id,
                "course_id": extraction.course_id,
                "date": assessment.date.isoformat() if assessment.date else None,
                "kind": "exam" if assessment.kind.is_exam else "due",
                "topic": assessment.title or assessment.kind.label.title(),
                "chapter_refs": covered,
                "readings": [],
                "weight": assessment.weight,
                "confidence": assessment.confidence,
                "link_score": (sum(scores) / len(scores)) if scores else 0.0,
                "link_method": "semantic" if linker else "no-materials",
                "link_title": "; ".join(titles)[:120],
                "needs_review": int(flagged or assessment.confidence == "low"),
            }
        )
    return rows


def assessment_records(
    extraction: SyllabusExtraction, user_id: str, linker: Optional[ChapterLinker] = None
) -> list[AssessmentRecord]:
    """First-class gradable items from the extraction, for the assessments list
    and the .ics feed. Distinct from the schedule rows, which drive the calendar
    week view. An undated item still becomes a record -- due_date is simply None."""
    out: list[AssessmentRecord] = []
    for a in extraction.assessments:
        covered: list[str] = []
        if linker is not None:
            for item in a.covers or [a.title]:
                r = linker.link(item, [], extraction.course_id)
                if r.chapter_ref:
                    covered.append(r.chapter_ref)
        out.append(
            AssessmentRecord(
                id=uuid.uuid4().hex,
                user_id=user_id,
                course_id=extraction.course_id,
                kind=a.kind,
                title=a.title or a.kind.label.title(),
                due_date=a.date,
                weight=a.weight,
                chapter_refs=covered,
                source="extracted",
                user_entered=False,
            )
        )
    return out


def commit(
    store: SQLiteStore,
    extraction: SyllabusExtraction,
    user_id: str,
    replace: bool = True,
    linker: Optional[ChapterLinker] = None,
) -> dict[str, Any]:
    if replace:
        store.clear_schedule(user_id, extraction.course_id)
    if linker is None:
        linker = build_linker(store, user_id, extraction.course_id)
    rows = to_rows(extraction, user_id, linker)
    store.insert_schedule_rows(rows)

    # Replace extracted assessments (never the student's manual ones) and re-add.
    store.clear_extracted_assessments(user_id, extraction.course_id)
    for record in assessment_records(extraction, user_id, linker):
        store.upsert_assessment(record)
    linked = sum(1 for r in rows if r["chapter_refs"])
    return {
        "course_id": extraction.course_id,
        "rows": len(rows),
        "lectures": sum(1 for r in rows if r["kind"] == "lecture"),
        "exams": sum(1 for r in rows if r["kind"] == "exam"),
        "due": sum(1 for r in rows if r["kind"] == "due"),
        "linked": linked,
        "unlinked": len(rows) - linked,
        "needs_review": sum(1 for r in rows if r["needs_review"]),
        "has_materials": linker is not None,
    }

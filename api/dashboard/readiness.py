"""Dashboard panels: today, this week, exam readiness, study plan.

Engagement is *derived* from what the student actually asked about, never from a
checkbox. That is the difference between a tracker abandoned in week three and one
that stays honest. A manual override exists because work happens offline, but it
is stored separately, labelled self-reported, and never overwrites the derived
signal -- the derived bar is what makes the readiness view worth looking at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from ..models import Engagement
from ..store import SQLiteStore


@dataclass
class ChapterReadiness:
    chapter_ref: str
    course_id: str
    chapter_num: int
    label: str
    derived: float
    manual: Optional[float]
    questions_asked: int
    ps_questions_hit: int
    notes_exported: int
    has_materials: bool

    @property
    def status(self) -> str:
        """Maps onto the colour legend: emerald engaged, amber partial, red none."""
        if self.derived >= 0.6:
            return "engaged"
        if self.derived > 0.0:
            return "partial"
        return "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_ref": self.chapter_ref,
            "course_id": self.course_id,
            "chapter": self.chapter_num,
            "label": self.label,
            "derived": round(self.derived, 3),
            "manual": None if self.manual is None else round(self.manual, 3),
            "status": self.status,
            "questions_asked": self.questions_asked,
            "ps_questions_hit": self.ps_questions_hit,
            "notes_exported": self.notes_exported,
            "has_materials": self.has_materials,
        }


@dataclass
class ExamReadiness:
    course_id: str
    title: str
    exam_date: str
    days_away: int
    chapters: list[ChapterReadiness] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)

    @property
    def overall(self) -> float:
        if not self.chapters:
            return 0.0
        return sum(c.derived for c in self.chapters) / len(self.chapters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "title": self.title,
            "date": self.exam_date,
            "days_away": self.days_away,
            "overall": round(self.overall, 3),
            "chapters": [c.to_dict() for c in self.chapters],
            "plan": self.plan,
        }


def _chapter_label(store: SQLiteStore, chapter_ref: str) -> tuple[str, bool]:
    """Chapter title from the corpus, and whether materials exist for it."""
    try:
        _, source_id, chapter = chapter_ref.split(":")
    except ValueError:
        return chapter_ref, False
    row = store.conn.execute(
        "SELECT chapter_title FROM chunks WHERE source_id=? AND chapter_num=? "
        "AND chapter_title != '' LIMIT 1",
        (source_id, int(chapter)),
    ).fetchone()
    if row is None:
        return f"Ch {chapter}", False
    return row["chapter_title"], True


def chapter_readiness(
    store: SQLiteStore, user_id: str, chapter_refs: list[str]
) -> list[ChapterReadiness]:
    engagement = store.engagement(user_id)
    manual = store.manual_progress(user_id)
    out = []
    for ref in dict.fromkeys(chapter_refs):
        entry = engagement.get(ref, Engagement(user_id=user_id, chapter_ref=ref))
        label, has_materials = _chapter_label(store, ref)
        parts = ref.split(":")
        out.append(
            ChapterReadiness(
                chapter_ref=ref,
                course_id=parts[0] if parts else "",
                chapter_num=int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0,
                label=label,
                derived=entry.score(),
                manual=manual.get(ref),
                questions_asked=entry.questions_asked,
                ps_questions_hit=entry.ps_questions_hit,
                notes_exported=entry.notes_exported,
                has_materials=has_materials,
            )
        )
    return sorted(out, key=lambda c: (c.course_id, c.chapter_num))


def next_assessment(
    store: SQLiteStore, user_id: str, today: Optional[date] = None
) -> Optional[dict[str, Any]]:
    today = today or date.today()
    rows = store.schedule(user_id, start=today.isoformat())
    for row in rows:
        if row["kind"] == "exam":
            return row
    return None


def exam_readiness(
    store: SQLiteStore, user_id: str, today: Optional[date] = None
) -> Optional[ExamReadiness]:
    today = today or date.today()
    exam = next_assessment(store, user_id, today)
    if exam is None:
        return None
    exam_date = date.fromisoformat(exam["date"])
    days_away = (exam_date - today).days

    chapters = chapter_readiness(store, user_id, exam["chapter_refs"])
    readiness = ExamReadiness(
        course_id=exam["course_id"],
        title=exam.get("topic") or "Exam",
        exam_date=exam["date"],
        days_away=days_away,
        chapters=chapters,
    )
    readiness.plan = build_study_plan(chapters, days_away)
    return readiness


def build_study_plan(
    chapters: list[ChapterReadiness], days_away: int
) -> list[dict[str, Any]]:
    """Chapters ordered by inverse engagement, spread across the days available.

    Deliberately simple: priority ordering plus a lighter final day. Optimising the
    spacing further would be a lot of scheduling logic for a panel whose whole job
    is to answer "where do I start", which the ordering already answers.
    """
    if not chapters or days_away <= 0:
        return []
    ordered = sorted(chapters, key=lambda c: (c.derived, -c.questions_asked))
    usable_days = max(1, min(days_away, 10))
    # Leave the last day lighter -- review, not new material.
    working_days = max(1, usable_days - 1)
    per_day = max(1, (len(ordered) + working_days - 1) // working_days)

    plan: list[dict[str, Any]] = []
    for index in range(0, len(ordered), per_day):
        day_number = len(plan) + 1
        plan.append(
            {
                "day": day_number,
                "days_before_exam": max(1, days_away - day_number + 1),
                "chapters": [
                    {
                        "chapter_ref": c.chapter_ref,
                        "label": c.label,
                        "status": c.status,
                        "reason": "no engagement" if c.derived == 0 else "needs review",
                    }
                    for c in ordered[index : index + per_day]
                ],
            }
        )
    if plan:
        plan[-1]["note"] = "lighter load: review only"
    return plan


def week_view(
    store: SQLiteStore, user_id: str, today: Optional[date] = None
) -> list[dict[str, Any]]:
    """Seven columns, each shaded by the engagement of the chapters it covers.
    The gaps are the entire point of the panel."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    rows = store.schedule(user_id, start=monday.isoformat(), end=sunday.isoformat())

    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["date"], []).append(row)

    out = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        items = by_day.get(day.isoformat(), [])
        refs = [r for item in items for r in item["chapter_refs"]]
        readiness = chapter_readiness(store, user_id, refs) if refs else []
        engagement_score = (
            sum(c.derived for c in readiness) / len(readiness) if readiness else None
        )
        out.append(
            {
                "date": day.isoformat(),
                "weekday": day.strftime("%a"),
                "is_today": day == today,
                "items": [
                    {
                        "course_id": item["course_id"],
                        "kind": item["kind"],
                        "topic": item["topic"],
                        "chapter_refs": item["chapter_refs"],
                        "confidence": item["confidence"],
                    }
                    for item in items
                ],
                "engagement": None if engagement_score is None else round(engagement_score, 3),
            }
        )
    return out


def today_view(
    store: SQLiteStore, user_id: str, today: Optional[date] = None
) -> dict[str, Any]:
    today = today or date.today()
    rows = store.schedule(user_id, start=today.isoformat(), end=today.isoformat())
    upcoming = store.schedule(
        user_id,
        start=(today + timedelta(days=1)).isoformat(),
        end=(today + timedelta(days=21)).isoformat(),
    )
    return {
        "date": today.isoformat(),
        "today": [
            {
                "course_id": r["course_id"],
                "kind": r["kind"],
                "topic": r["topic"],
                "chapter_refs": r["chapter_refs"],
                "readings": r["readings"],
            }
            for r in rows
        ],
        "upcoming": [
            {
                "date": r["date"],
                "course_id": r["course_id"],
                "kind": r["kind"],
                "topic": r["topic"],
                "days_away": (date.fromisoformat(r["date"]) - today).days,
            }
            for r in upcoming
            if r["kind"] in {"exam", "due"}
        ][:8],
    }

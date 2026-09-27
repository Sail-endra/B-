"""Two progress signals on one axis, so the gap between them is the insight.

  * Class pace     -- share of the syllabus scheduled up to today. Purely
                      date-driven, no engagement input. It is where the course
                      is, not where the student is.
  * Your progress  -- share of scheduled topics up to today the student has
                      actually engaged with, from the derived engagement signal.

The label is deliberately "engagement", never "mastery" or "understanding": it
measures what was asked about, not what was learned. The gap (pace minus
progress) is reported as "topics behind" because that number is the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from ..store import SQLiteStore
from .readiness import chapter_readiness

# A chapter counts as "engaged" at or above this derived score. Matches the
# "partial vs engaged" boundary the readiness view already uses.
_ENGAGED_AT = 0.3


@dataclass
class CourseProgress:
    course_id: str
    code: str
    meetings_total: int
    meetings_to_date: int
    topics_to_date: int
    topics_engaged: int

    @property
    def pace(self) -> float:
        return self.meetings_to_date / self.meetings_total if self.meetings_total else 0.0

    @property
    def progress(self) -> float:
        return self.topics_engaged / self.topics_to_date if self.topics_to_date else 0.0

    @property
    def behind(self) -> int:
        # Topics the course has reached that the student has not engaged with.
        return max(0, self._paced_topics - self.topics_engaged)

    @property
    def _paced_topics(self) -> int:
        return self.topics_to_date

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "code": self.code,
            "pace": round(self.pace, 4),
            "progress": round(self.progress, 4),
            "meetings_to_date": self.meetings_to_date,
            "meetings_total": self.meetings_total,
            "topics_engaged": self.topics_engaged,
            "topics_to_date": self.topics_to_date,
            "behind": self.behind,
            # The UI must never call this mastery; the label ships from here.
            "progress_label": "engagement",
        }


@dataclass
class ProgressReport:
    courses: list[CourseProgress] = field(default_factory=list)

    def rollup(self) -> dict[str, Any]:
        mt = sum(c.meetings_total for c in self.courses)
        md = sum(c.meetings_to_date for c in self.courses)
        td = sum(c.topics_to_date for c in self.courses)
        te = sum(c.topics_engaged for c in self.courses)
        return {
            "pace": round(md / mt, 4) if mt else 0.0,
            "progress": round(te / td, 4) if td else 0.0,
            "meetings_to_date": md, "meetings_total": mt,
            "topics_engaged": te, "topics_to_date": td,
            "behind": max(0, td - te),
            "progress_label": "engagement",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "rollup": self.rollup(),
            "courses": [c.to_dict() for c in self.courses],
            "note": "Progress measures what you asked about, not how well you learned it.",
        }


def _course_code(store: SQLiteStore, user_id: str, course_id: str) -> str:
    c = store.course(user_id, course_id)
    return c.code if c else course_id


def progress_report(
    store: SQLiteStore, user_id: str, today: Optional[date] = None
) -> ProgressReport:
    today = today or date.today()
    today_iso = today.isoformat()
    report = ProgressReport()

    by_course: dict[str, dict] = {}
    for row in store.schedule(user_id):                     # dated rows only
        c = by_course.setdefault(row["course_id"], {"total": 0, "to_date": 0, "refs": set()})
        if row["kind"] != "lecture":
            continue
        c["total"] += 1
        if row["date"] and row["date"] <= today_iso:
            c["to_date"] += 1
            for ref in row["chapter_refs"]:
                c["refs"].add(ref)

    for course_id, agg in sorted(by_course.items()):
        if agg["total"] == 0:
            continue
        refs = sorted(agg["refs"])
        readiness = {r.chapter_ref: r for r in chapter_readiness(store, user_id, refs)}
        engaged = sum(
            1 for ref in refs
            if ref in readiness and readiness[ref].derived >= _ENGAGED_AT
        )
        report.courses.append(
            CourseProgress(
                course_id=course_id,
                code=_course_code(store, user_id, course_id),
                meetings_total=agg["total"],
                meetings_to_date=agg["to_date"],
                topics_to_date=len(refs),
                topics_engaged=engaged,
            )
        )
    return report

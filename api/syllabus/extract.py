"""Syllabus extraction.

Two structured extractions with different prompts, run in parallel, then diffed
row by row. Rows both passes agree on are accepted silently; only disagreements
reach the review screen. That diff is what makes "upload a syllabus, get a working
semester in two minutes" honest -- without it you either trust unchecked output or
make the user proofread forty rows, and neither is two minutes.

Normalisations every real syllabus needs at least one of:

  * relative dates -- "Week 3" expands against term_start and meeting_days
  * chapter formats -- "Mankiw Ch. 14", "14.1-14.3", "pp. 287-310" all normalise
  * holidays -- skipped, never shifted
  * no textbook mapping -- chapter_refs stays empty and the schedule still works
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from ..ids import ChapterRef, parse_chapter_mentions
from ..models import Assessment, AssessmentKind, Meeting, SyllabusExtraction

_WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_WEEK_REF = re.compile(r"\bweek\s+(\d{1,2})\b", re.I)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_HOLIDAY = re.compile(
    r"\b(no class|holiday|break|recess|reading day|thanksgiving|spring break)\b", re.I
)
_EXAM = re.compile(r"\b(midterm|final exam|final|quiz|exam)\b", re.I)
# Metadata lines that happen to contain a date but are not schedule rows.
_HEADER_LINE = re.compile(
    r"^\s*(term|semester|instructor|meets?|office|email|grading|prereq)\b", re.I
)
_DUE = re.compile(r"\b(due|deadline|submit|problem set|ps\s*\d|paper|essay)\b", re.I)

EXTRACTION_PROMPT_A = """Extract the schedule from this course syllabus.

Return JSON only, with this exact shape:
{"course": {"code": "ECON 303", "title": "...", "term_start": "YYYY-MM-DD",
            "term_end": "YYYY-MM-DD", "meeting_days": ["Tue","Thu"],
            "meeting_time": "11:00-12:20"},
 "meetings": [{"date": "YYYY-MM-DD", "topic": "...", "readings": ["Ch 14.1-14.3"],
               "confidence": "high"}],
 "assessments": [{"date": "YYYY-MM-DD", "kind": "midterm", "covers": ["Ch 9","Ch 11"],
                  "weight": 0.25, "title": "Midterm 1", "confidence": "high"}]}

Rules:
- Use only dates the syllabus states or that follow from "Week N" plus the term start.
- Skip holidays and breaks. Do not shift later rows to fill them.
- If a schedule/outline row has no date (e.g. a numbered "course outline"), STILL
  return it as a meeting with "date": null. Never drop a row for lacking a date.
- Classify each assessment "kind" as one of: homework, problem_set, paper, exam,
  quiz, project, reading_response, presentation (use exam for midterms/finals).
- Capture "weight" as a fraction (0.25) where the syllabus states it, else 0.
- If the term dates are not stated, set them to null rather than guessing.
- confidence is "low" whenever you inferred rather than read the value.

SYLLABUS:
"""

EXTRACTION_PROMPT_B = """You are converting a syllabus into a calendar. Work row by row
through any schedule table, then scan the prose for anything a table missed.

For each row identify: the date, what is covered, what is due, and whether it is an
assessment. Expand "Week N" using the term start date and the meeting days. Treat
"no class", holidays and breaks as skipped dates, not as shifted content.

Output JSON only:
{"course": {"code": "...", "title": "...", "term_start": "YYYY-MM-DD",
            "term_end": "YYYY-MM-DD", "meeting_days": [...], "meeting_time": "..."},
 "meetings": [{"date": "...", "topic": "...", "readings": [...], "confidence": "high|low"}],
 "assessments": [{"date": "...", "kind": "homework|problem_set|paper|exam|quiz|project|reading_response|presentation",
                  "covers": [...], "weight": 0.0, "title": "...", "confidence": "high|low"}]}

Prefer null over a guess. Mark anything inferred as low confidence. An undated
"course outline" row is still a meeting with "date": null -- never drop it.

SYLLABUS:
"""


@dataclass
class RowDiff:
    key: str
    field: str
    value_a: Any
    value_b: Any
    resolved: Any = None

    @property
    def agrees(self) -> bool:
        return _norm(self.value_a) == _norm(self.value_b)


@dataclass
class DiffedExtraction:
    extraction: SyllabusExtraction
    conflicts: list[RowDiff] = field(default_factory=list)
    agreed_rows: int = 0
    total_rows: int = 0
    backend: str = ""

    @property
    def agreement_rate(self) -> float:
        return self.agreed_rows / self.total_rows if self.total_rows else 1.0

    @property
    def needs_review(self) -> bool:
        return bool(self.conflicts)


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, list):
        return sorted(_norm(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# Date handling
# ---------------------------------------------------------------------------


def parse_date(text: str, default_year: int) -> Optional[date]:
    if not text:
        return None
    if (m := _ISO_DATE.search(text)) is not None:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if (m := _US_DATE.search(text)) is not None:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3) or default_year)
        if year < 100:
            year += 2000
        return date(year, month, day)
    if (m := _MONTH_DAY.search(text)) is not None:
        month = _MONTHS[m.group(1).lower()[:4].rstrip(".")[:4]] if m.group(1).lower()[:4] in _MONTHS else _MONTHS.get(m.group(1).lower()[:3])
        if month:
            return date(default_year, month, int(m.group(2)))
    return None


def expand_week(
    week_number: int, term_start: date, meeting_days: list[str]
) -> list[date]:
    """"Week 3" -> the actual meeting dates in that week.

    Week 1 is the week containing term_start. Expansion is against the Monday of
    that week so a term starting on a Thursday does not shift every later week.
    """
    if not meeting_days:
        return []
    week_one_monday = term_start - timedelta(days=term_start.weekday())
    target_monday = week_one_monday + timedelta(weeks=week_number - 1)
    out = []
    for day in meeting_days:
        index = _WEEKDAYS.get(day.strip().lower()[:9])
        if index is None:
            continue
        candidate = target_monday + timedelta(days=index)
        if candidate >= term_start:
            out.append(candidate)
    return sorted(out)


def classify_row(text: str) -> str:
    if _EXAM.search(text or ""):
        return "exam"
    if _DUE.search(text or ""):
        return "due"
    return "lecture"


def is_holiday(text: str) -> bool:
    return bool(_HOLIDAY.search(text or ""))


def normalise_chapter_refs(
    readings: list[str], course_id: str, primary_source: Optional[str]
) -> list[str]:
    """Free-text readings -> canonical chapter refs.

    Returns an empty list when nothing parses. That is a normal case, not an
    error: a course with no textbook mapping still gets a working schedule, and
    Ask simply stays disabled for it.
    """
    refs: list[ChapterRef] = []
    for reading in readings or []:
        refs.extend(parse_chapter_mentions(reading, course_id))
    out: list[str] = []
    for ref in refs:
        try:
            bound = ref.resolve([primary_source] if primary_source else [], prefer=primary_source)
        except Exception:  # noqa: BLE001 - unresolvable ref is dropped, not fatal
            continue
        out.append(str(bound))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def read_syllabus(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        import docx

        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    if suffix == ".pdf":
        import pymupdf

        pdf = pymupdf.open(str(path))
        text = "\n".join(page.get_text("text") for page in pdf)
        pdf.close()
        return text
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_payload(
    payload: dict[str, Any], course_id: str, primary_source: Optional[str]
) -> SyllabusExtraction:
    course = payload.get("course", {}) or {}
    default_year = date.today().year
    term_start = parse_date(str(course.get("term_start") or ""), default_year)
    term_end = parse_date(str(course.get("term_end") or ""), default_year)
    meeting_days = [str(d) for d in course.get("meeting_days", []) or []]

    meetings: list[Meeting] = []
    for raw in payload.get("meetings", []) or []:
        when = parse_date(str(raw.get("date") or ""), term_start.year if term_start else default_year)
        topic = str(raw.get("topic", ""))
        if not topic.strip():
            continue  # a row with neither date nor topic is nothing
        if is_holiday(topic):
            continue  # skipped, never shifted -- but an undated topic is kept (when may be None)
        readings = [str(r) for r in raw.get("readings", []) or []]
        meetings.append(
            Meeting(
                date=when,
                topic=topic,
                chapter_refs=normalise_chapter_refs(
                    readings + [topic], course_id, primary_source
                ),
                readings=readings,
                confidence=str(raw.get("confidence", "high")),
            )
        )

    assessments: list[Assessment] = []
    for raw in payload.get("assessments", []) or []:
        when = parse_date(str(raw.get("date") or ""), term_start.year if term_start else default_year)
        if not str(raw.get("title", "")).strip() and when is None:
            continue  # neither a title nor a date -> nothing to store
        try:
            kind = AssessmentKind(str(raw.get("kind", "quiz")).lower().replace(" ", "_"))
        except ValueError:
            kind = AssessmentKind.QUIZ
        covers = [str(c) for c in raw.get("covers", []) or []]
        assessments.append(
            Assessment(
                date=when,
                kind=kind,
                covers=normalise_chapter_refs(covers, course_id, primary_source),
                weight=float(raw.get("weight") or 0.0),
                title=str(raw.get("title", "")),
                confidence=str(raw.get("confidence", "high")),
            )
        )

    return SyllabusExtraction(
        course_id=course_id,
        code=str(course.get("code", course_id)),
        title=str(course.get("title", "")),
        term_start=term_start,
        term_end=term_end,
        meeting_days=meeting_days,
        meeting_time=str(course.get("meeting_time", "")),
        meetings=sorted(meetings, key=lambda m: (m.date is None, m.date or date.max)),
        assessments=sorted(assessments, key=lambda a: (a.date is None, a.date or date.max)),
    )


def double_extract(
    text: str, course_id: str, primary_source: Optional[str] = None,
    allow_gemini: bool = False,
) -> DiffedExtraction:
    """Two passes, diffed. Falls back to a single deterministic parse offline.

    `allow_gemini` opts this call into the free-tier model. It defaults False so
    the product upload path never sends a stranger's syllabus to Gemini's free
    tier; the owner's own material (benchmark, or an explicit consent flow) passes
    True."""
    from ..llm import get_llm

    llm = get_llm(allow_gemini=allow_gemini)
    if not llm.available:
        payload = heuristic_extract(text, course_id)
        extraction = _parse_payload(payload, course_id, primary_source)
        return DiffedExtraction(
            extraction=extraction,
            conflicts=[],
            agreed_rows=len(extraction.meetings) + len(extraction.assessments),
            total_rows=len(extraction.meetings) + len(extraction.assessments),
            backend="heuristic (single pass; no model configured, so no diff)",
        )

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(llm.json, EXTRACTION_PROMPT_A + text[:12000], max_tokens=4000)
        future_b = pool.submit(llm.json, EXTRACTION_PROMPT_B + text[:12000], max_tokens=4000)
        payload_a, payload_b = future_a.result(), future_b.result()

    extraction_a = _parse_payload(payload_a, course_id, primary_source)
    extraction_b = _parse_payload(payload_b, course_id, primary_source)
    return diff(extraction_a, extraction_b, llm.backend)


def diff(a: SyllabusExtraction, b: SyllabusExtraction, backend: str = "") -> DiffedExtraction:
    """Row-by-row comparison keyed on date. Only disagreements reach review."""
    conflicts: list[RowDiff] = []

    for field_name in ("term_start", "term_end", "meeting_days", "meeting_time"):
        value_a, value_b = getattr(a, field_name), getattr(b, field_name)
        if _norm(value_a) != _norm(value_b):
            conflicts.append(RowDiff("course", field_name, value_a, value_b, value_a or value_b))

    # Key on (date, normalised topic) so undated rows -- all with date None --
    # do not collide, and so the same topic on the same day matches across passes.
    def mkey(m):
        return (m.date.isoformat() if m.date else "no-date", (m.topic or "").strip().lower())

    meetings_a = {mkey(m): m for m in a.meetings}
    meetings_b = {mkey(m): m for m in b.meetings}
    agreed = 0
    for k in sorted(set(meetings_a) | set(meetings_b)):
        row_a, row_b = meetings_a.get(k), meetings_b.get(k)
        key = f"meeting:{k[0]}:{k[1][:24]}"
        if row_a is None or row_b is None:
            conflicts.append(
                RowDiff(key, "presence", bool(row_a), bool(row_b), row_a or row_b)
            )
            continue
        row_conflicts = [
            RowDiff(key, name, getattr(row_a, name), getattr(row_b, name), getattr(row_a, name))
            for name in ("topic", "chapter_refs")
            if _norm(getattr(row_a, name)) != _norm(getattr(row_b, name))
        ]
        if row_conflicts:
            conflicts.extend(row_conflicts)
        else:
            agreed += 1

    def akey(x):
        return (x.date.isoformat() if x.date else "no-date", x.kind.value,
                (x.title or "").strip().lower()[:24])
    assessments_a = {akey(x): x for x in a.assessments}
    assessments_b = {akey(x): x for x in b.assessments}
    for key_tuple in sorted(set(assessments_a) | set(assessments_b)):
        row_a, row_b = assessments_a.get(key_tuple), assessments_b.get(key_tuple)
        key = f"assessment:{key_tuple[0]}:{key_tuple[1]}"
        if row_a is None or row_b is None:
            conflicts.append(
                RowDiff(key, "presence", bool(row_a), bool(row_b), row_a or row_b)
            )
            continue
        row_conflicts = [
            RowDiff(key, name, getattr(row_a, name), getattr(row_b, name), getattr(row_a, name))
            for name in ("covers", "weight")
            if _norm(getattr(row_a, name)) != _norm(getattr(row_b, name))
        ]
        if row_conflicts:
            conflicts.extend(row_conflicts)
        else:
            agreed += 1

    total = len(set(meetings_a) | set(meetings_b)) + len(set(assessments_a) | set(assessments_b))
    # Pass A is the committed base; conflicts are surfaced for review on top of it.
    return DiffedExtraction(
        extraction=a,
        conflicts=conflicts,
        agreed_rows=agreed,
        total_rows=total,
        backend=backend or "llm double-extraction",
    )


def heuristic_extract(text: str, course_id: str) -> dict[str, Any]:
    """Deterministic parser for the offline path and as an LLM sanity check.

    Handles the common shape: one schedule row per line, a date at the front, the
    topic and readings after it.
    """
    default_year = date.today().year
    course: dict[str, Any] = {"code": course_id, "title": "", "meeting_days": []}
    meetings: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []

    # Longest alternative first: "ends" must be tried before "end", or the regex
    # matches "end" and then fails on the leftover "s".
    if (m := re.search(r"\b(?:term|semester)\s+(?:starts|start|begins)\s*:?\s*(\S+)", text, re.I)):
        if (parsed := parse_date(m.group(1), default_year)):
            course["term_start"] = parsed.isoformat()
    if (m := re.search(r"\b(?:term|semester)\s+(?:ends|end)\s*:?\s*(\S+)", text, re.I)):
        if (parsed := parse_date(m.group(1), default_year)):
            course["term_end"] = parsed.isoformat()
    if (m := re.search(r"\b(meets?|meeting days?)\s*:?\s*([A-Za-z,/ ]+)", text, re.I)):
        days = [d for d in re.split(r"[,/ ]+", m.group(2)) if d.lower()[:3] in
                {k[:3] for k in _WEEKDAYS}]
        course["meeting_days"] = days[:4]
    if (m := re.search(r"^([A-Z]{2,5}\s*\d{3})\s*[:\-]?\s*(.+)$", text.strip(), re.M)):
        course["code"] = m.group(1).strip()
        course["title"] = m.group(2).strip()[:80]

    term_start = parse_date(str(course.get("term_start", "")), default_year)

    for line in text.splitlines():
        line = line.strip()
        if len(line) < 6:
            continue
        when = parse_date(line, term_start.year if term_start else default_year)
        if when is None and term_start and (wm := _WEEK_REF.search(line)):
            dates = expand_week(int(wm.group(1)), term_start, course.get("meeting_days", []))
            when = dates[0] if dates else None
        if when is None:
            continue
        if is_holiday(line):
            continue
        # Header lines carry a date but are not schedule rows.
        if _HEADER_LINE.match(line):
            continue
        kind = classify_row(line)
        body = re.sub(r"^[^|]*\|", "", line).strip() or line
        if kind == "exam":
            assessments.append(
                {
                    "date": when.isoformat(),
                    "kind": "final" if re.search(r"\bfinal\b", line, re.I) else "midterm",
                    "covers": [body],
                    "weight": 0.0,
                    "title": body[:60],
                    "confidence": "low",
                }
            )
        elif kind == "due":
            # A deadline is an assessment, not a lecture -- it belongs on the
            # dashboard's due list rather than in the week's teaching rows.
            assessments.append(
                {
                    "date": when.isoformat(),
                    "kind": "paper" if re.search(r"\b(paper|essay)\b", line, re.I)
                    else "problem_set",
                    "covers": [body],
                    "weight": 0.0,
                    "title": body[:60],
                    "confidence": "high",
                }
            )
        else:
            meetings.append(
                {
                    "date": when.isoformat(),
                    "topic": body[:120],
                    "readings": [body],
                    "confidence": "low" if _WEEK_REF.search(line) else "high",
                }
            )

    return {"course": course, "meetings": meetings, "assessments": assessments}

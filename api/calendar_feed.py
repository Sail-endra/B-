"""Read-only iCalendar (.ics) feed of a student's assessments.

A subscribe-able feed, not a notification service: the student adds one URL to
Google or Apple Calendar and their existing calendar app does the reminding, on
the phone they already check. It costs nothing to run and needs no scheduler --
the feed is regenerated on every read, so edits to the schedule show up the next
time the calendar client refreshes.

One VEVENT per dated, not-done assessment, each with three VALARM triggers at
-P7D, -P3D and -P1D. Completed items are excluded. Exam events carry their
covered chapters in the description.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable

from .models import AssessmentRecord, AssessmentStatus

_ALARMS = ("-P7D", "-P3D", "-P1D")


def _escape(text: str) -> str:
    """RFC 5545 text escaping: backslash, comma, semicolon, newline."""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 requires lines <= 75 octets, continued with CRLF + a space."""
    out, raw = [], line.encode("utf-8")
    while len(raw) > 75:
        # Break on a character boundary at or before 75 octets.
        cut = 75
        while (raw[cut] & 0xC0) == 0x80:   # don't split a UTF-8 sequence
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


def _dt_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _vevent(a: AssessmentRecord, dtstamp: str) -> list[str]:
    day = a.due_date.strftime("%Y%m%d")
    summary = f"{a.kind.label.title()}: {a.title}" if a.title else a.kind.label.title()
    if a.course_id:
        summary = f"[{a.course_id.upper()}] {summary}"

    desc_parts = []
    if a.weight:
        desc_parts.append(f"Weight: {a.weight:.0%}" if a.weight <= 1 else f"Weight: {a.weight}")
    if a.kind.is_exam and a.chapter_refs:
        chapters = ", ".join(r.split(":")[-1] for r in a.chapter_refs)
        desc_parts.append(f"Covers chapters: {chapters}")
    if a.user_entered:
        desc_parts.append("(added by you)")
    description = _escape("  ".join(desc_parts))

    lines = [
        "BEGIN:VEVENT",
        f"UID:{a.id}@course-copilot",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{day}",
        f"DTEND;VALUE=DATE:{day}",
        f"SUMMARY:{_escape(summary)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{description}")
    lines.append("TRANSP:TRANSPARENT")
    for trigger in _ALARMS:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:{trigger}",
            f"DESCRIPTION:{_escape(summary)}",
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def build_ics(assessments: Iterable[AssessmentRecord], calendar_name: str = "Course Copilot") -> str:
    """Render a full VCALENDAR. Undated or completed items are skipped."""
    dtstamp = _dt_stamp()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Course Copilot//Assessments//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for a in assessments:
        if a.due_date is None or a.status is AssessmentStatus.DONE:
            continue
        lines += _vevent(a, dtstamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"

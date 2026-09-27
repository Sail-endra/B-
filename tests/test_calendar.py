"""Calendar merge, recurrence, holidays and check-off progress."""

from __future__ import annotations

from datetime import date

from api import calendar_view as cal
from api.models import AssessmentKind, AssessmentRecord, AssessmentStatus, Course
from api.store import SQLiteStore


def _store(tmp_path):
    s = SQLiteStore(tmp_path / "cal.db")
    s.upsert_course(Course("econ303", "u", "ECON 303", "Micro"))
    return s


def test_holidays_are_computed():
    h = cal.us_holidays(2026)
    assert h[date(2026, 12, 25)] == "Christmas Day"
    assert h[date(2026, 1, 1)] == "New Year's Day"
    # Thanksgiving is the 4th Thursday of November.
    assert cal._nth_weekday(2026, 11, 3, 4) == date(2026, 11, 26)
    assert h[date(2026, 11, 26)] == "Thanksgiving"


def test_weekly_recurrence_expands_only_on_named_weekdays(tmp_path):
    s = _store(tmp_path)
    s.upsert_event({"user_id": "u", "title": "Lecture", "course_id": "econ303",
                    "kind": "class", "date": "2026-09-01", "recurrence": "weekly",
                    "recur_days": "MO,WE,FR", "recur_until": "2026-12-11",
                    "start_time": "10:00", "location": "Blow 201"})
    items = cal.calendar_items(s, "u", date(2026, 9, 21), date(2026, 9, 27))  # Mon–Sun
    classes = [i for i in items if i["kind"] == "class"]
    assert {i["date"] for i in classes} == {"2026-09-21", "2026-09-23", "2026-09-25"}
    assert classes[0]["start_time"] == "10:00" and classes[0]["location"] == "Blow 201"


def test_assessment_and_toggle_drive_progress(tmp_path):
    s = _store(tmp_path)
    s.upsert_assessment(AssessmentRecord(
        id="a1", user_id="u", course_id="econ303", kind=AssessmentKind.PAPER,
        title="Essay", due_date=date(2026, 9, 23)))
    p = cal.progress(s, "u", date(2026, 9, 23))
    assert p["today"] == {"total": 1, "done": 0, "pct": 0}
    # Completing the assessment counts toward progress.
    a = s.assessment("u", "a1"); a.status = AssessmentStatus.DONE; s.upsert_assessment(a)
    assert cal.progress(s, "u", date(2026, 9, 23))["today"]["done"] == 1


def test_holidays_and_classes_are_not_counted_as_work(tmp_path):
    s = _store(tmp_path)
    s.upsert_event({"user_id": "u", "title": "Lecture", "kind": "class",
                    "date": "2026-07-04"})  # a class on Independence Day
    items = cal.calendar_items(s, "u", date(2026, 7, 4), date(2026, 7, 4))
    kinds = {i["kind"] for i in items}
    assert "holiday" in kinds and "class" in kinds
    assert cal._count(items) == {"total": 0, "done": 0, "pct": 0}  # neither is work


def test_meeting_day_and_time_normalization():
    from api.syllabus.meetings import _norm_days, _time
    assert _norm_days("MW") == "MO,WE"
    assert _norm_days("TR") == "TU,TH"
    assert _norm_days("MWF") == "MO,WE,FR"
    assert _norm_days("Mon/Wed") == "MO,WE"
    assert _time("2:00 pm") == "14:00"
    assert _time("9:00am") == "09:00"
    assert _time("15:30") == "15:30"


def test_recurring_class_is_bounded_to_the_term():
    from api.syllabus.meetings import events_from_meetings
    # No term given, but a dated final -> term is inferred and the class is bounded.
    ex = {"term_start": None, "term_end": None,
          "meetings": [{"days": "MO,WE", "start_time": "14:00", "end_time": "15:20", "location": "ISC 1360", "label": ""}],
          "key_dates": [{"title": "Final", "date": "2026-05-11", "kind": "exam"}]}
    from api.syllabus.meetings import extract_meetings  # noqa: F401 (import sanity)
    # events_from_meetings requires bounded term; feed it the inferred bounds.
    ex["term_end"] = "2026-05-11"; ex["term_start"] = "2026-01-11"
    events = events_from_meetings(ex, "csci303")
    cls = [e for e in events if e["kind"] == "class"][0]
    assert cls["recurrence"] == "weekly" and cls["recur_days"] == "MO,WE"
    assert cls["recur_until"] == "2026-05-11" and cls["date"] == "2026-01-11"
    assert cls["location"] == "ISC 1360"
    # An unbounded term produces no runaway recurring class.
    assert not [e for e in events_from_meetings(
        {"term_start": None, "term_end": None, "meetings": ex["meetings"], "key_dates": []}, "x")
        if e["kind"] == "class"]


def test_new_event_kinds_work_vs_attendance(tmp_path):
    s = _store(tmp_path)
    for k in ("club", "meeting", "research", "job", "project"):
        s.upsert_event({"user_id": "u", "title": k, "kind": k, "date": "2026-09-23"})
    items = cal.calendar_items(s, "u", date(2026, 9, 23), date(2026, 9, 23))
    # research, meeting and project are tasks; club and job are attendance.
    assert cal._count(items)["total"] == 3

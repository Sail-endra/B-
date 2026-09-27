"""Tests for the .ics feed, progress math, and assessment records."""

from __future__ import annotations

import os

os.environ.setdefault("COPILOT_OFFLINE", "1")
os.environ.setdefault("COPILOT_EMBEDDER", "tfidf")

from datetime import date  # noqa: E402

import pytest  # noqa: E402

from api.calendar_feed import build_ics  # noqa: E402
from api.dashboard.progress import progress_report  # noqa: E402
from api.models import AssessmentKind, AssessmentRecord, AssessmentStatus  # noqa: E402
from api.store.sqlite_store import SQLiteStore  # noqa: E402

USER = "u1"


@pytest.fixture()
def store(tmp_path):
    s = SQLiteStore(tmp_path / "t.db")
    yield s
    s.close()


def _a(**kw) -> AssessmentRecord:
    base = dict(id=kw.pop("id", "a1"), user_id=USER, course_id="ECON303",
                kind=AssessmentKind.EXAM, title="Midterm")
    base.update(kw)
    return AssessmentRecord(**base)


# -- .ics feed ---------------------------------------------------------------


def test_ics_has_one_vevent_per_dated_incomplete_item_with_three_alarms():
    items = [
        _a(id="1", title="PS1", kind=AssessmentKind.PROBLEM_SET, due_date=date(2026, 10, 1)),
        _a(id="2", title="Exam", kind=AssessmentKind.EXAM, due_date=date(2026, 10, 8)),
    ]
    ics = build_ics(items)
    assert ics.count("BEGIN:VEVENT") == 2
    # three alarms per event
    assert ics.count("BEGIN:VALARM") == 6
    for trigger in ("TRIGGER:-P7D", "TRIGGER:-P3D", "TRIGGER:-P1D"):
        assert ics.count(trigger) == 2


def test_ics_excludes_completed_and_undated():
    items = [
        _a(id="1", title="Done", due_date=date(2026, 10, 1), status=AssessmentStatus.DONE),
        _a(id="2", title="Undated", due_date=None),
        _a(id="3", title="Live", due_date=date(2026, 10, 2)),
    ]
    ics = build_ics(items)
    assert ics.count("BEGIN:VEVENT") == 1
    assert "Live" in ics and "Done" not in ics and "Undated" not in ics


def test_ics_exam_carries_covered_chapters():
    ics = build_ics([_a(kind=AssessmentKind.EXAM, due_date=date(2026, 10, 8),
                        chapter_refs=["ECON303:tb:14", "ECON303:tb:15"])])
    # Unfold RFC 5545 continuation lines, then account for comma-escaping.
    unfolded = ics.replace("\r\n ", "")
    assert "Covers chapters: 14\\, 15" in unfolded


def test_ics_is_crlf_and_folds_long_lines():
    long_title = "A very long assessment title " * 6
    ics = build_ics([_a(title=long_title, due_date=date(2026, 10, 1))])
    assert "\r\n" in ics
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, repr(line)


def test_ics_escapes_special_characters():
    ics = build_ics([_a(title="Essay: cause, effect; and more", due_date=date(2026, 10, 1))])
    assert "\\," in ics and "\;" in ics


# -- feed token --------------------------------------------------------------


def test_feed_token_is_stable_and_unguessable(store):
    t1 = store.feed_token(USER)
    t2 = store.feed_token(USER)
    assert t1 == t2 and len(t1) >= 32
    assert store.user_for_token(t1) == USER
    assert store.user_for_token("nope") is None


def test_feed_tokens_differ_per_user(store):
    assert store.feed_token("alice") != store.feed_token("bob")


# -- progress: two signals ---------------------------------------------------


def _seed_schedule(store, dated_past, dated_future, engaged_refs):
    rows = []
    for i in range(dated_past):
        rows.append({"user_id": USER, "course_id": "ECON303", "date": f"2026-09-0{i+1}",
                     "kind": "lecture", "topic": f"T{i}",
                     "chapter_refs": [f"ECON303:tb:{i}"], "readings": []})
    for i in range(dated_future):
        rows.append({"user_id": USER, "course_id": "ECON303", "date": f"2026-12-1{i}",
                     "kind": "lecture", "topic": f"F{i}",
                     "chapter_refs": [f"ECON303:tb:{100+i}"], "readings": []})
    store.insert_schedule_rows(rows)
    for ref in engaged_refs:
        store.bump_engagement(USER, ref, questions=5)


def test_pace_is_date_driven_only(store):
    store.upsert_course_stub(USER, "ECON303", "ECON 303", "Micro")
    _seed_schedule(store, dated_past=4, dated_future=6, engaged_refs=[])
    rep = progress_report(store, USER, today=date(2026, 10, 1)).rollup()
    # 4 of 10 meetings are on/before Oct 1
    assert rep["meetings_to_date"] == 4 and rep["meetings_total"] == 10
    assert rep["pace"] == pytest.approx(0.4)
    # no engagement -> zero progress, and behind == topics reached
    assert rep["progress"] == 0.0 and rep["behind"] == 4


def test_progress_counts_only_engaged_topics_to_date(store):
    store.upsert_course_stub(USER, "ECON303", "ECON 303", "Micro")
    _seed_schedule(store, dated_past=4, dated_future=0,
                   engaged_refs=["ECON303:tb:0", "ECON303:tb:1"])
    rep = progress_report(store, USER, today=date(2026, 10, 1)).rollup()
    assert rep["topics_engaged"] == 2 and rep["topics_to_date"] == 4
    assert rep["behind"] == 2


def test_progress_never_labels_itself_mastery(store):
    store.upsert_course_stub(USER, "ECON303", "ECON 303", "Micro")
    _seed_schedule(store, 2, 0, [])
    d = progress_report(store, USER, today=date(2026, 10, 1)).to_dict()
    blob = str(d).lower()
    assert "mastery" not in blob and "understanding" not in blob
    assert d["rollup"]["progress_label"] == "engagement"


# -- undated schedule rows ---------------------------------------------------


def test_undated_rows_are_stored_and_queried_separately(store):
    store.insert_schedule_rows([
        {"user_id": USER, "course_id": "ECON303", "date": "2026-09-01",
         "kind": "lecture", "topic": "Dated", "chapter_refs": [], "readings": []},
        {"user_id": USER, "course_id": "ECON303", "date": None,
         "kind": "lecture", "topic": "Undated", "chapter_refs": [], "readings": []},
    ])
    dated = store.schedule(USER, start="2026-01-01", end="2026-12-31")
    undated = store.schedule_undated(USER)
    assert [r["topic"] for r in dated] == ["Dated"]
    assert [r["topic"] for r in undated] == ["Undated"]


# -- assessment records ------------------------------------------------------


def test_manual_assessments_survive_extraction_replace(store):
    manual = _a(id="m1", title="My own", source="manual", user_entered=True,
                due_date=date(2026, 10, 1))
    extracted = _a(id="e1", title="From syllabus", source="extracted",
                   due_date=date(2026, 10, 2))
    store.upsert_assessment(manual)
    store.upsert_assessment(extracted)
    store.clear_extracted_assessments(USER, "ECON303")
    remaining = [a.title for a in store.assessments(USER)]
    assert remaining == ["My own"], "re-committing a syllabus must not delete manual items"

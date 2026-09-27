from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from api import main
from api.models import BlockKind, Chunk, Course, Source, SourceType
from api.store.sqlite_store import SQLiteStore


def seed_course(store: SQLiteStore, user: str, course_id: str = "ECON303", source_id: str = "econ303_book"):
    store.upsert_course(Course(course_id, user, "ECON 303", "Microeconomics"))
    store.upsert_source(Source(source_id, user, course_id, "Textbook", SourceType.TEXTBOOK, f"{user}/{source_id}.pdf"))
    store.insert_chunks([Chunk(
        id=f"{user}_{source_id}_{n}", user_id=user, source_id=source_id, course_id=course_id,
        chapter_num=1, chapter_title="Demand", section="", page_start=n, page_end=n,
        text=f"Chunk {n}", kind=BlockKind.PROSE, token_count=2, embedding=[.1, .2],
    ) for n in (1, 2)])
    store.replace_chapters(user, source_id, [(course_id, 1, "Demand", 1, 10, [.1, .2])])
    store.insert_schedule_rows([{"id": f"{user}_{course_id}_lecture", "user_id": user,
        "course_id": course_id, "date": None, "kind": "lecture", "topic": "Demand",
        "chapter_refs": [f"{course_id}:{source_id}:1"]}])
    with store.conn:
        store.conn.execute("INSERT INTO assessments(id,user_id,course_id,title,due_date) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_assessment", user, course_id, "Midterm", "2026-10-20"))
        store.conn.execute("INSERT INTO practice_problems(id,user_id,course_id,source_id,prompt) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_problem", user, course_id, source_id, "Problem"))
        store.conn.execute("INSERT INTO practice_problems(id,user_id,course_id,source_id,origin,parent_id,prompt) VALUES(?,?,?,?,?,?,?)",
                           (f"{user}_{course_id}_variant", user, course_id, source_id, "generated", f"{user}_{course_id}_problem", "Variant"))
        store.conn.execute("INSERT INTO practice_attempts(id,user_id,problem_id,chapter_ref,correct) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_attempt", user, f"{user}_{course_id}_problem", f"{course_id}:{source_id}:1", 1))
        store.conn.execute("INSERT INTO practice_attempts(id,user_id,problem_id,chapter_ref,correct) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_chapter_attempt", user, "missing-problem", f"{course_id}:{source_id}:1", 0))
        store.conn.execute("INSERT INTO parents(id,user_id,source_id,text) VALUES(?,?,?,?)",
                           (f"{user}_{source_id}_parent", user, source_id, "Parent text"))
        store.conn.execute("INSERT INTO engagement(user_id,chapter_ref,questions_asked) VALUES(?,?,?)",
                           (user, f"{course_id}:{source_id}:1", 1))
        store.conn.execute("INSERT INTO manual_progress(user_id,chapter_ref,value,updated_at) VALUES(?,?,?,?)",
                           (user, f"{course_id}:{source_id}:1", .5, "now"))
        store.conn.execute("INSERT INTO calendar_events(id,user_id,course_id,title,notes) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_event", user, course_id, "Lecture", "from syllabus"))
        store.conn.execute("INSERT INTO task_status(user_id,item_id,done,updated_at) VALUES(?,?,?,?)",
                           (user, f"{user}_{course_id}_lecture", 1, "now"))
        store.conn.execute("INSERT INTO study_plans(id,user_id,assessment_id,generated_at,plan) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_plan", user, f"{user}_{course_id}_assessment", "now", "[]"))
        store.conn.execute("INSERT INTO conversations(id,user_id,course_id,created_at,title) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_conversation", user, course_id, "now", "Question"))
        store.conn.execute("INSERT INTO messages(id,conversation_id,user_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
                           (f"{user}_{course_id}_message", f"{user}_{course_id}_conversation", user, "user", "Help", "now"))
        store.conn.execute("INSERT INTO artifacts(id,message_id,user_id,kind,spec) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_artifact", f"{user}_{course_id}_message", user, "chart", "{}"))
        store.conn.execute("INSERT INTO ingest_jobs(id,user_id,course_id,files) VALUES(?,?,?,?)",
                           (f"{user}_{course_id}_job", user, course_id, "[]"))
        store.conn.execute("INSERT INTO agent_runs(id,user_id,question,course_id,ran_at) VALUES(?,?,?,?,?)",
                           (f"{user}_{course_id}_run", user, "Question", course_id, "now"))
        store.conn.execute("INSERT INTO grade_extractions(id,user_id,course_id,syllabus_hash,schema_json,created_at) VALUES(?,?,?,?,?,?)",
                           (f"{user}_{course_id}_grade", user, course_id, "hash", json.dumps({}), "now"))
        store.conn.execute("INSERT INTO grade_rules(user_id,course_id,schema_json) VALUES(?,?,?)",
                           (user, course_id, json.dumps({})))
        store.conn.execute("INSERT INTO grade_scores(user_id,course_id,item_id,score) VALUES(?,?,?,?)",
                           (user, course_id, "item", 90))
        store.conn.execute("INSERT INTO grade_syllabus_selections(user_id,course_id,source_id,file_name) VALUES(?,?,?,?)",
                           (user, course_id, source_id, "Syllabus.pdf"))


def test_deleting_course_a_leaves_course_b_chunks_chapters_and_schedule(tmp_path):
    # Standalone fixture keeps this file independent from test-module fixtures.
    store = SQLiteStore(tmp_path / "course-a-b.sqlite")
    try:
        seed_course(store, "alice", "A", "shared_A_book")
        seed_course(store, "alice", "B", "shared_B_book")
        store.delete_course_scope("alice", "A", "entire_course")
        assert store.chunk_count_for_course("alice", "A") == 0
        assert store.chunk_count_for_course("alice", "B") == 2
        assert len(store.chapters("alice", "B")) == 1
        assert len(store.schedule("alice", course_id="B")) == 1
    finally:
        store.close()


@pytest.fixture
def store(tmp_path):
    instance = SQLiteStore(tmp_path / "delete.sqlite")
    yield instance
    instance.close()


def test_identically_named_course_and_source_for_another_user_are_untouched(store):
    seed_course(store, "alice", "ECON303", "econ303_shared")
    seed_course(store, "bob", "ECON303", "econ303_shared")
    store.delete_course_scope("bob", "ECON303", "entire_course")
    assert store.chunk_count_for_course("alice", "ECON303") == 2
    assert len(store.chapters("alice", "ECON303")) == 1
    assert len(store.schedule("alice", course_id="ECON303")) == 1
    assert store.course("alice", "ECON303") is not None
    assert store.sources("alice", "ECON303")


def test_product_delete_all_cannot_touch_benchmark_courses_or_corpus(store, monkeypatch):
    seed_course(store, "local-user", "PRODUCT", "shared_source")
    seed_course(store, "benchmark", "BENCH", "shared_source")
    before = store.chunk_count("benchmark")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "local-user")
    preview = main.course_deletion_preview()
    assert {course["course_id"] for course in preview["courses"]} == {"PRODUCT"}
    assert preview["reupload_cache"] is False
    request = main.CourseDeletionRequest(selected=[main.CourseDeletionItem(
        course_id="PRODUCT", scope="entire_course")], confirmation="DELETE ALL", select_all=True)
    main.delete_selected_courses(request)
    assert store.chunk_count("benchmark") == before == 2
    assert store.course("benchmark", "BENCH") is not None
    with pytest.raises(PermissionError):
        store.delete_course_scope("benchmark", "BENCH", "entire_course")


def test_full_delete_removes_all_course_records_and_relationship_orphans(store):
    seed_course(store, "alice")
    store.delete_course_scope("alice", "ECON303", "entire_course")
    direct = ("courses", "sources", "chunks", "chapters", "syllabus_schedule", "assessments",
              "practice_problems", "practice_attempts", "grade_extractions", "grade_rules",
              "grade_scores", "grade_syllabus_selections", "calendar_events", "ingest_jobs", "agent_runs")
    for table in direct:
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id='alice'").fetchone()[0] == 0, table
    relationships = {
        "parents": "SELECT COUNT(*) FROM parents WHERE user_id='alice'",
        "practice_attempts": "SELECT COUNT(*) FROM practice_attempts WHERE user_id='alice'",
        "engagement": "SELECT COUNT(*) FROM engagement WHERE user_id='alice'",
        "manual_progress": "SELECT COUNT(*) FROM manual_progress WHERE user_id='alice'",
        "study_plans": "SELECT COUNT(*) FROM study_plans WHERE user_id='alice'",
        "task_status": "SELECT COUNT(*) FROM task_status WHERE user_id='alice'",
        "conversations": "SELECT COUNT(*) FROM conversations WHERE user_id='alice'",
        "messages": "SELECT COUNT(*) FROM messages WHERE user_id='alice'",
        "artifacts": "SELECT COUNT(*) FROM artifacts WHERE user_id='alice'",
    }
    for table, sql in relationships.items():
        assert store.conn.execute(sql).fetchone()[0] == 0, table
    for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        table = row[0]
        columns = {column[1] for column in store.conn.execute(f'PRAGMA table_info("{table}")')}
        if {"user_id", "course_id"} <= columns:
            assert store.conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id=? AND course_id=?',
                ("alice", "ECON303"),
            ).fetchone()[0] == 0, table


def test_materials_only_removes_corpus_and_keeps_schedule_assessments_and_engagement(store):
    seed_course(store, "alice")
    before_assessments = len(store.assessments("alice", "ECON303"))
    store.delete_course_scope("alice", "ECON303", "materials")
    assert store.chunk_count_for_course("alice", "ECON303") == 0
    assert store.chapters("alice", "ECON303") == []
    assert len(store.schedule("alice", course_id="ECON303")) == 1
    assert len(store.assessments("alice", "ECON303")) == before_assessments == 1
    assert store.conn.execute("SELECT COUNT(*) FROM engagement WHERE user_id='alice'").fetchone()[0] == 1
    assert store.course("alice", "ECON303") is not None


def test_syllabus_only_removes_schedule_and_assessments_but_keeps_chunks_chapters_and_engagement(store, monkeypatch):
    seed_course(store, "alice")
    monkeypatch.setattr(main, "get_store", lambda: store)
    store.upsert_source(Source("econ303_syllabus", "alice", "ECON303", "Syllabus",
                               SourceType.SYLLABUS, "alice/syllabus.pdf"))
    token = store.feed_token("alice")
    assert "SUMMARY:[ECON303] Homework: Midterm" in main.calendar_feed(token).body.decode()
    store.delete_course_scope("alice", "ECON303", "syllabus")
    assert store.chunk_count_for_course("alice", "ECON303") == 2
    assert len(store.chapters("alice", "ECON303")) == 1
    assert store.schedule("alice", course_id="ECON303") == []
    assert store.assessments("alice", "ECON303") == []
    assert store.conn.execute("SELECT COUNT(*) FROM engagement WHERE user_id='alice'").fetchone()[0] == 1
    assert {source.source_id for source in store.sources("alice", "ECON303")} == {"econ303_book"}
    # The endpoint rebuilds the .ics from current assessment rows on every GET.
    assert "SUMMARY:[ECON303] Homework: Midterm" not in main.calendar_feed(token).body.decode()


def test_failed_full_delete_rolls_back_all_changes(store):
    seed_course(store, "alice")
    before = store.course_deletion_counts("alice", "ECON303")
    store.conn.execute("CREATE TRIGGER fail_course_chunk_delete BEFORE DELETE ON chunks BEGIN SELECT RAISE(ABORT, 'forced failure'); END")
    with pytest.raises(Exception, match="forced failure"):
        store.delete_course_scope("alice", "ECON303", "entire_course")
    assert store.course("alice", "ECON303") is not None
    assert store.course_deletion_counts("alice", "ECON303") == before

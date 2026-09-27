"""Multi-tenancy regression tests.

Two bugs shipped because nothing here existed:

  * `clear_source` deleted by `source_id` alone. Source ids are derived from
    course id + filename, so two users who upload the same textbook to the same
    course code collide, and one user's ingest silently deleted the other's
    corpus.
  * The fitted local embedder was written to a single global file, so any user's
    ingest moved every other user's vectors into a different space. Retrieval
    kept working and kept returning worse answers, which is the worst way for a
    bug to behave.

Both were found only because a benchmark number collapsed. These tests fail on
the old code and are the cheap version of that discovery.
"""

from __future__ import annotations

import os

os.environ.setdefault("COPILOT_OFFLINE", "1")
os.environ.setdefault("COPILOT_EMBEDDER", "tfidf")  # fast, deterministic; BGE is the serving path

import pytest  # noqa: E402

from api.models import BlockKind, Chunk, Course, Source, SourceType  # noqa: E402
from api.store.sqlite_store import SQLiteStore  # noqa: E402

ALICE, BOB = "alice", "bob"


@pytest.fixture()
def store(tmp_path):
    s = SQLiteStore(tmp_path / "t.db")
    yield s
    s.close()


def _chunk(user, source_id, course_id, ordinal, text="budget constraint and utility"):
    return Chunk(
        id=f"{user}_{source_id}_{ordinal:03d}", user_id=user, source_id=source_id,
        course_id=course_id, chapter_num=1, chapter_title="Ch", section="",
        page_start=ordinal, page_end=ordinal, text=text, kind=BlockKind.PROSE,
        token_count=5, embedding=[0.1, 0.2, 0.3],
    )


def _seed(store, user, course_id="ECON303", source_id="econ303_tb", n=3):
    store.upsert_course(Course(course_id, user, "ECON 303", "Micro"))
    store.upsert_source(Source(source_id, user, course_id, "Text",
                               SourceType.TEXTBOOK, "f.pdf"))
    store.insert_chunks([_chunk(user, source_id, course_id, i) for i in range(n)])


# -- the two bugs that shipped ----------------------------------------------


def test_clear_source_does_not_delete_another_users_chunks(store):
    """The exact bug: identical source_id, two users, one ingest."""
    _seed(store, ALICE, n=4)
    _seed(store, BOB, n=4)
    assert store.chunk_count(ALICE) == 4 and store.chunk_count(BOB) == 4

    store.clear_source(BOB, "econ303_tb")          # Bob re-uploads

    assert store.chunk_count(BOB) == 0, "Bob's own chunks should be cleared"
    assert store.chunk_count(ALICE) == 4, "Alice's chunks must be untouched"


def test_embedder_state_is_per_user():
    from api.corpus.ingest_service import embedder_state_path

    assert embedder_state_path(ALICE) != embedder_state_path(BOB)


def test_embedder_state_path_is_filesystem_safe():
    """User ids become filenames. A traversal or a separator must not escape."""
    from api.corpus.ingest_service import embedder_state_path

    nasty = embedder_state_path("../../etc/passwd")
    assert ".." not in nasty.name and "/" not in nasty.name


def test_source_id_collision_keeps_both_rows(store):
    """Same course code, same filename, two users -> two independent sources."""
    _seed(store, ALICE)
    _seed(store, BOB)
    assert len(store.sources(ALICE)) == 1
    assert len(store.sources(BOB)) == 1
    store.upsert_source(Source("econ303_tb", BOB, "ECON303", "Bob's retitle",
                               SourceType.TEXTBOOK, "b.pdf"))
    assert store.sources(ALICE)[0].title == "Text", "Alice's source was overwritten"
    assert store.sources(BOB)[0].title == "Bob's retitle"


# -- isolation of every user-scoped table ------------------------------------


def test_courses_are_isolated(store):
    _seed(store, ALICE)
    store.upsert_course(Course("ANTH201", BOB, "ANTH 201", "Arch"))
    assert [c.course_id for c in store.courses(ALICE)] == ["ECON303"]
    assert [c.course_id for c in store.courses(BOB)] == ["ANTH201"]
    assert store.course(ALICE, "ANTH201") is None


def test_chapters_are_isolated(store):
    _seed(store, ALICE)
    _seed(store, BOB)
    store.replace_chapters(ALICE, "econ303_tb", [("ECON303", 1, "Alice Ch", 1, 9, None)])
    store.replace_chapters(BOB, "econ303_tb", [("ECON303", 1, "Bob Ch", 1, 9, None)])
    assert store.chapters(ALICE, "ECON303")[0]["title"] == "Alice Ch"
    assert store.chapters(BOB, "ECON303")[0]["title"] == "Bob Ch"


def test_schedule_is_isolated(store):
    for user in (ALICE, BOB):
        _seed(store, user)
        store.insert_schedule_rows([{
            "user_id": user, "course_id": "ECON303", "date": "2026-10-01",
            "kind": "lecture", "topic": f"{user} topic", "chapter_refs": [],
            "readings": [],
        }])
    assert [r["topic"] for r in store.schedule(ALICE)] == ["alice topic"]
    store.clear_schedule(BOB, "ECON303")
    assert len(store.schedule(ALICE)) == 1, "clearing Bob's schedule hit Alice's"


def test_engagement_and_manual_progress_are_isolated(store):
    store.bump_engagement(ALICE, "ECON303:s:1", questions=3)
    store.bump_engagement(BOB, "ECON303:s:1", questions=7)
    assert store.engagement(ALICE)["ECON303:s:1"].questions_asked == 3
    assert store.engagement(BOB)["ECON303:s:1"].questions_asked == 7

    store.set_manual_progress(ALICE, "ECON303:s:1", 0.2)
    store.set_manual_progress(BOB, "ECON303:s:1", 0.9)
    assert store.manual_progress(ALICE)["ECON303:s:1"] == pytest.approx(0.2)
    assert store.manual_progress(BOB)["ECON303:s:1"] == pytest.approx(0.9)


def test_delete_course_only_touches_one_user(store):
    _seed(store, ALICE)
    _seed(store, BOB)
    store.delete_course(BOB, "ECON303")
    assert store.chunk_count(BOB) == 0 and not store.courses(BOB)
    assert store.chunk_count(ALICE) == 3 and len(store.courses(ALICE)) == 1


def test_agent_runs_are_isolated(store):
    store.log_agent_run(ALICE, "q", "ECON303", 1, ["search_textbook"], False, 10, [])
    store.log_agent_run(BOB, "q", "ECON303", 1, ["search_textbook"], False, 10, [])
    assert len(store.agent_runs(ALICE)) == 1
    assert len(store.agent_runs(BOB)) == 1


def test_vector_matrix_never_leaks_another_user(store):
    _seed(store, ALICE, n=5)
    _seed(store, BOB, n=2)
    ids_a, mat_a = store.vector_matrix(ALICE)
    assert len(ids_a) == 5 and mat_a.shape[0] == 5
    assert all(i.startswith("alice_") for i in ids_a)


def test_every_user_scoped_table_has_a_user_id_column(store):
    """A new table without user_id is the next instance of this bug class."""
    expected = {
        "courses", "sources", "chapters", "parents", "chunks", "conversations",
        "messages", "artifacts", "syllabus_schedule", "engagement",
        "manual_progress", "study_plans", "agent_runs",
    }
    for table in expected:
        cols = {r[1] for r in store.conn.execute(f"PRAGMA table_info({table})")}
        assert "user_id" in cols, f"{table} is not user-scoped"


# -- concurrency -------------------------------------------------------------


def test_concurrent_ingests_from_two_users_do_not_interfere(tmp_path):
    """Many users writing at once must each end up with exactly their own rows.

    A single sqlite3.Connection is shared across the process; two threads calling
    execute()/commit() on it simultaneously raise SystemError('error return
    without exception set'). A barrier releases every worker at the same instant
    so the contention is real, and several rounds per worker keep the connection
    under sustained concurrent load -- this fails hard without the store's write
    lock, rather than passing by lucky timing.
    """
    import threading

    shared = SQLiteStore(tmp_path / "c.db")
    users = {"alice": 12, "bob": 7, "carol": 20, "dave": 3, "erin": 15}
    barrier = threading.Barrier(len(users))
    errors: list[Exception] = []

    def ingest(user: str, n: int) -> None:
        try:
            barrier.wait()                       # all workers start together
            for _round in range(4):
                shared.upsert_course(Course("ECON303", user, "ECON 303", "Micro"))
                shared.upsert_source(Source("econ303_tb", user, "ECON303", "T",
                                            SourceType.TEXTBOOK, "f.pdf"))
                shared.clear_source(user, "econ303_tb")
                shared.insert_chunks(
                    [_chunk(user, "econ303_tb", "ECON303", i) for i in range(n)])
        except Exception as exc:  # noqa: BLE001 - surfaced after the join
            errors.append(exc)

    threads = [threading.Thread(target=ingest, args=(u, n)) for u, n in users.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # clear_source before each round means the final count is one round's worth,
    # and crucially each user sees only their own rows.
    for user, n in users.items():
        assert shared.chunk_count(user) == n, user
    shared.close()


# -- the harness must model the pipeline it measures -------------------------


def test_gate_refused_is_reported_by_the_pipeline():
    """Both refusal measurement bugs came from the harness re-deriving the
    pipeline's decision. The pipeline now reports it; this asserts it is there."""
    from api.models import RetrievalResult

    assert "gate_refused" in RetrievalResult.__dataclass_fields__
    r = RetrievalResult(query="q", chunks=[], confidence=0.0, refused=True,
                        gate_refused=True)
    assert r.to_dict()["gate_refused"] is True


# -- upload failure states ---------------------------------------------------


@pytest.mark.parametrize(
    "name,content,expect_status,expect_phrase",
    [
        ("empty.pdf", b"", "failed", "empty"),
        ("corrupt.pdf", b"this is not a pdf at all", "failed", "not a readable PDF"),
        ("truncated.pdf", b"%PDF-1.4\n%garbage", "failed", "not a readable PDF"),
        ("notes.xyz", b"hello", "unsupported", "not supported"),
    ],
)
def test_broken_uploads_report_a_reason_instead_of_crashing(
    tmp_path, store, name, content, expect_status, expect_phrase
):
    """A stranger uploading a corrupt file must be told which file and why.

    These previously raised out of ingest, returned a bare HTTP 500, and took
    every other file in the same batch down with them.
    """
    from api.corpus.ingest_service import ingest_files

    path = tmp_path / name
    path.write_bytes(content)
    report = ingest_files([path], user_id=ALICE, course_id="T100", store=store)

    assert len(report.files) == 1
    entry = report.files[0]
    assert entry.status == expect_status
    assert expect_phrase.lower() in entry.detail.lower()


def test_one_bad_file_does_not_sink_the_batch(tmp_path, store):
    from api.corpus.ingest_service import ingest_files

    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    good = tmp_path / "good.md"
    good.write_text(
        "<!-- chapter: 1 | title: Opening -->\n<!-- page: 1 -->\n"
        + "The consumer maximises utility subject to a budget constraint. " * 20
    )
    report = ingest_files([bad, good], user_id=ALICE, course_id="T100", store=store)

    by_name = {f.filename: f for f in report.files}
    assert by_name["bad.pdf"].status == "failed"
    assert by_name["good.md"].status == "ok" and by_name["good.md"].chunks > 0


def test_failure_detail_does_not_leak_server_paths(tmp_path, store):
    from api.corpus.ingest_service import ingest_files

    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"nope")
    report = ingest_files([path], user_id=ALICE, course_id="T100", store=store)
    assert str(tmp_path) not in report.files[0].detail


def test_prose_reference_to_a_chapter_is_not_a_chapter_heading():
    """"chapter 7 emphasizes the symbolic importance of..." is a sentence, not a
    heading. Accepting it invented the only chapter an entire course could offer
    the syllabus linker, titled with a fragment of someone else's prose."""
    from ingest.extract import looks_like_chapter_heading

    assert not looks_like_chapter_heading(
        "emphasizes the symbolic and cultural importance of El Presidio"
    )
    assert not looks_like_chapter_heading("discusses how the author frames this")
    assert looks_like_chapter_heading("The Market")
    assert looks_like_chapter_heading("")


# -- chunk-id collisions across users (the "benchmark lost 1238 chunks" bug) --


def _chunk_for(store, user, source_id="econ303_tb", course="econ303", n=5):
    from api.ids import chunk_id
    return [
        Chunk(id=chunk_id(user, source_id, i), user_id=user, source_id=source_id,
              course_id=course, chapter_num=1, chapter_title="Ch", section="",
              page_start=i + 1, page_end=i + 1, text=f"{user} chunk {i}",
              kind=BlockKind.PROSE, token_count=5, embedding=[0.1, 0.2, 0.3])
        for i in range(n)
    ]


def test_same_file_same_course_two_users_do_not_overwrite(store):
    """The real failure: both users upload tb_varian.pdf to a course that slugs to
    'econ303'. Chunk ids were source+ordinal, so INSERT OR REPLACE moved one
    user's chunks onto the other. Namespaced ids must keep them separate."""
    store.insert_chunks(_chunk_for(store, ALICE, n=8))
    store.insert_chunks(_chunk_for(store, BOB, n=3))
    assert store.chunk_count(ALICE) == 8, "Alice's chunks were overwritten"
    assert store.chunk_count(BOB) == 3


def test_chunk_ids_are_user_unique():
    from api.ids import chunk_id
    assert chunk_id(ALICE, "s", 0) != chunk_id(BOB, "s", 0)


def test_page_resolution_is_user_scoped(store):
    """Gold resolution by (source, page range) must not return another user's
    chunks that share the same source_id."""
    store.insert_chunks(_chunk_for(store, ALICE, n=4))
    store.insert_chunks(_chunk_for(store, BOB, n=4))
    a_ids = set(store.resolve_pages_to_chunks("econ303_tb", 1, 4, user_id=ALICE))
    b_ids = set(store.resolve_pages_to_chunks("econ303_tb", 1, 4, user_id=BOB))
    assert a_ids and b_ids and a_ids.isdisjoint(b_ids)


def test_vector_matrix_cache_is_per_user(store):
    """One shared cache returned the first user's matrix for everyone."""
    store.insert_chunks(_chunk_for(store, ALICE, n=6))
    store.insert_chunks(_chunk_for(store, BOB, n=2))
    ids_a, _ = store.vector_matrix(ALICE)
    ids_b, _ = store.vector_matrix(BOB)   # must NOT return Alice's cached matrix
    assert len(ids_a) == 6 and len(ids_b) == 2
    assert set(ids_a).isdisjoint(set(ids_b))


def test_delete_course_cascades_and_spares_others(store):
    """delete_course must leave no orphans (parents keyed by source_id,
    engagement by chapter_ref prefix, assessments by course_id) and must not
    touch another course or another user that share ids."""
    import uuid

    from api.models import AssessmentKind, AssessmentRecord

    _seed(store, ALICE, "ECON303", "econ303_tb", n=3)
    _seed(store, ALICE, "LIT200", "lit_tb", n=2)        # same user, other course
    _seed(store, BOB, "ECON303", "econ303_tb", n=3)     # other user, same ids
    store.insert_parents([("ALICE_econ303_tb_p0", ALICE, "econ303_tb", "parent")])
    store.insert_parents([("BOB_econ303_tb_p0", BOB, "econ303_tb", "parent")])
    store.bump_engagement(ALICE, "ECON303:econ303_tb:1", questions=2)
    store.bump_engagement(ALICE, "LIT200:lit_tb:1", questions=1)
    store.upsert_assessment(AssessmentRecord(
        id=uuid.uuid4().hex, user_id=ALICE, course_id="ECON303",
        kind=AssessmentKind.EXAM, title="Midterm"))

    store.delete_course(ALICE, "ECON303")

    def count(sql, *p):
        return store.conn.execute(sql, p).fetchone()[0]

    # Everything for ALICE/ECON303 is gone -- no orphans.
    assert count("select count(*) from chunks where user_id=? and course_id=?", ALICE, "ECON303") == 0
    assert count("select count(*) from parents where user_id=? and source_id=?", ALICE, "econ303_tb") == 0
    assert count("select count(*) from engagement where user_id=? and chapter_ref like ?", ALICE, "ECON303:%") == 0
    assert count("select count(*) from assessments where user_id=? and course_id=?", ALICE, "ECON303") == 0
    assert count("select count(*) from courses where user_id=? and course_id=?", ALICE, "ECON303") == 0

    # The other course and the other user are untouched.
    assert count("select count(*) from chunks where user_id=? and course_id=?", ALICE, "LIT200") == 2
    assert count("select count(*) from engagement where user_id=? and chapter_ref like ?", ALICE, "LIT200:%") == 1
    assert count("select count(*) from chunks where user_id=? and course_id=?", BOB, "ECON303") == 3
    assert count("select count(*) from parents where user_id=? and source_id=?", BOB, "econ303_tb") == 1


def test_reingesting_identical_file_is_cached(tmp_path, store):
    """Re-uploading the same bytes to the same course is recognised by content
    hash and skipped, instead of re-extracting and re-embedding."""
    from api.corpus.ingest_service import ingest_files

    good = tmp_path / "book.md"
    good.write_text(
        "<!-- chapter: 1 | title: Opening -->\n<!-- page: 1 -->\n"
        + "The consumer maximises utility subject to a budget constraint. " * 20
    )
    first = ingest_files([good], user_id=ALICE, course_id="T100", store=store)
    assert first.files[0].status == "ok" and first.total_chunks > 0
    before = store.chunk_count_for_course(ALICE, "T100")

    second = ingest_files([good], user_id=ALICE, course_id="T100", store=store)
    assert second.files[0].status == "ok"
    assert "reused" in second.files[0].detail.lower()
    assert second.total_chunks == 0                    # nothing re-embedded
    assert store.chunk_count_for_course(ALICE, "T100") == before  # unchanged


def test_ingest_job_roundtrip(store):
    job_id = "job123"
    store.create_job(job_id, ALICE, "T100",
                     [{"filename": "a.pdf", "stage": "queued", "pages_done": 0,
                       "pages_total": 0, "chunks": 0}])
    store.update_job(job_id, status="running",
                     files=[{"filename": "a.pdf", "stage": "embedding",
                             "pages_done": 640, "pages_total": 1239, "chunks": 1239}])
    job = store.get_job(job_id, ALICE)
    assert job["status"] == "running"
    assert job["files"][0]["stage"] == "embedding" and job["files"][0]["pages_done"] == 640
    assert store.get_job(job_id, BOB) is None          # scoped by user

"""SQLite-backed store.

Vectors are float32 blobs scanned with numpy. At the scale this project actually
runs at -- low thousands of chunks -- a full scan costs single-digit milliseconds,
which is well inside the retrieval latency budget and avoids an index dependency.
The `Store` surface is what Postgres/pgvector would implement; see
docs/ARCHITECTURE.md for the migration, which is a driver swap, not a rewrite.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from ..models import (
    AssessmentKind,
    AssessmentRecord,
    AssessmentStatus,
    BlockKind,
    Chunk,
    Course,
    Engagement,
    Problem,
    Source,
    SourceType,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pack_vector(vec: Sequence[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack_vector(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def _serialized(method):
    """Serialize a method against the store's write lock.

    A single sqlite3.Connection is shared across the whole process. Two threads
    calling execute()/commit() on it at once corrupt its C-level state and raise
    SystemError("error return without exception set"). SQLite already serializes
    writes to the file; this makes access to the connection *object* serial too,
    which is the part Python does not guard. The lock is reentrant so a decorated
    method may call another (e.g. the course stub calls upsert_course).
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Guards every write against concurrent access to the shared connection.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL lets a reader (the Ask tab) proceed while the background ingest
        # worker writes; synchronous=NORMAL is the safe, much faster pairing with
        # WAL for this workload. In-memory DBs (tests) do not support WAL.
        if str(self.db_path) != ":memory:":
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.OperationalError:
                pass
        self.conn.executescript(SCHEMA_PATH.read_text())
        self._migrate()
        # Cache keyed by user -- one shared cache returned one user's matrix for
        # everyone, which silently mixed corpora across tenants.
        self._vector_cache: dict[str, tuple[list[str], np.ndarray]] = {}

    def _migrate(self) -> None:
        """Additive migrations for DBs created before a column existed. CREATE
        TABLE IF NOT EXISTS never adds a column to an existing table, so a new
        column needs an explicit, idempotent ALTER."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sources)")}
        if "file_hash" not in cols:
            self.conn.execute("ALTER TABLE sources ADD COLUMN file_hash TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        pref_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(user_prefs)")}
        if "consent_decided" not in pref_cols:
            self.conn.execute(
                "ALTER TABLE user_prefs ADD COLUMN consent_decided INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.commit()
        voice_columns = {
            "voice_consent": "INTEGER NOT NULL DEFAULT 0",
            "voice_only_mode": "INTEGER NOT NULL DEFAULT 0",
            "auto_submit_voice": "INTEGER NOT NULL DEFAULT 1",
            "selected_voice_id": "TEXT NOT NULL DEFAULT ''",
            "speech_speed": "REAL NOT NULL DEFAULT 1.0",
        }
        for name, declaration in voice_columns.items():
            if name not in pref_cols:
                self.conn.execute(f"ALTER TABLE user_prefs ADD COLUMN {name} {declaration}")
                self.conn.commit()
        for table, columns in {
            "grade_extractions": {"source_id": "TEXT NOT NULL DEFAULT ''", "file_name": "TEXT NOT NULL DEFAULT ''"},
            "grade_rules": {"source_id": "TEXT NOT NULL DEFAULT ''", "syllabus_hash": "TEXT NOT NULL DEFAULT ''"},
        }.items():
            present = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- seed -------------------------------------------------------------

    @_serialized
    def upsert_course(self, course: Course) -> None:
        self.conn.execute(
            "INSERT INTO courses(course_id, user_id, code, title, term, has_data_link, "
            "data_link_reason, created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, course_id) DO UPDATE SET code=excluded.code, "
            "title=excluded.title, term=excluded.term, "
            "has_data_link=excluded.has_data_link, "
            "data_link_reason=excluded.data_link_reason",
            (course.course_id, course.user_id, course.code, course.title, course.term,
             int(course.has_data_link), course.data_link_reason, _now()),
        )
        self.conn.commit()

    @_serialized
    def upsert_source(self, source: Source) -> None:
        self.conn.execute(
            "INSERT INTO sources(source_id, user_id, course_id, title, type, file, pages, "
            "status, status_detail, ocr_pages, file_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, source_id) DO UPDATE SET title=excluded.title, "
            "type=excluded.type, file=excluded.file, pages=excluded.pages, "
            "status=excluded.status, status_detail=excluded.status_detail, "
            "ocr_pages=excluded.ocr_pages, file_hash=excluded.file_hash",
            (source.source_id, source.user_id, source.course_id, source.title,
             source.type.value, source.file, source.pages, source.status,
             source.status_detail, source.ocr_pages, source.file_hash),
        )
        self.conn.commit()

    def source_with_hash(self, user_id: str, course_id: str, file_hash: str,
                         source_id: Optional[str] = None) -> Optional[str]:
        """source_id of an already-ingested, OK file with this content hash in this
        course, or None. BB Plus may supply a stable item source_id so two
        Blackboard items with identical bytes still retain distinct identities."""
        if not file_hash:
            return None
        source_filter = " AND s.source_id=?" if source_id else ""
        params: tuple[Any, ...] = (user_id, course_id, file_hash, source_id) if source_id else (user_id, course_id, file_hash)
        row = self.conn.execute(
            "SELECT s.source_id FROM sources s WHERE s.user_id=? AND s.course_id=? "
            "AND s.file_hash=? AND s.status='ok' "
            + source_filter + " "
            "AND EXISTS (SELECT 1 FROM chunks c WHERE c.user_id=s.user_id AND c.source_id=s.source_id) "
            "LIMIT 1",
            params,
        ).fetchone()
        return row["source_id"] if row else None

    def courses(self, user_id: str) -> list[Course]:
        rows = self.conn.execute(
            "SELECT * FROM courses WHERE user_id=? ORDER BY code", (user_id,)
        ).fetchall()
        return [
            Course(r["course_id"], r["user_id"], r["code"], r["title"], r["term"],
                   bool(r["has_data_link"]), r["data_link_reason"])
            for r in rows
        ]

    def bbplus_course_mappings(self, user_id: str) -> list[dict[str, str]]:
        rows = self.conn.execute(
            "SELECT blackboard_course_id,blackboard_course_name,course_id "
            "FROM bbplus_course_mappings WHERE user_id=? ORDER BY blackboard_course_name,blackboard_course_id",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def bbplus_course_mapping(self, user_id: str, blackboard_course_id: str) -> Optional[dict[str, str]]:
        row = self.conn.execute(
            "SELECT blackboard_course_id,blackboard_course_name,course_id "
            "FROM bbplus_course_mappings WHERE user_id=? AND blackboard_course_id=?",
            (user_id, blackboard_course_id),
        ).fetchone()
        return dict(row) if row else None

    @_serialized
    def set_bbplus_course_mapping(self, user_id: str, blackboard_course_id: str,
                                  blackboard_course_name: str, course_id: str) -> None:
        if user_id == "benchmark":
            raise PermissionError("The benchmark user cannot be changed through product integrations.")
        if not blackboard_course_id or len(blackboard_course_id) > 300:
            raise ValueError("A valid Blackboard course ID is required.")
        if self.course(user_id, course_id) is None:
            raise KeyError("course not found")
        now = _now()
        self.conn.execute(
            "INSERT INTO bbplus_course_mappings(user_id,blackboard_course_id,blackboard_course_name,course_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,blackboard_course_id) DO UPDATE SET "
            "blackboard_course_name=excluded.blackboard_course_name,course_id=excluded.course_id,updated_at=excluded.updated_at",
            (user_id, blackboard_course_id, blackboard_course_name[:300], course_id, now, now),
        )
        self.conn.commit()

    @_serialized
    def upsert_course_stub(self, user_id: str, course_id: str, code: str, title: str) -> None:
        """A course with a syllabus but no materials is a normal state, not an
        error: the dashboard works, Ask stays disabled for it."""
        self.upsert_course(Course(course_id, user_id, code, title))

    def course(self, user_id: str, course_id: str) -> Optional[Course]:
        r = self.conn.execute(
            "SELECT * FROM courses WHERE user_id=? AND course_id=?", (user_id, course_id)
        ).fetchone()
        if r is None:
            return None
        return Course(r["course_id"], r["user_id"], r["code"], r["title"], r["term"],
                      bool(r["has_data_link"]), r["data_link_reason"])

    def sources(self, user_id: str, course_id: Optional[str] = None) -> list[Source]:
        sql = "SELECT * FROM sources WHERE user_id=?"
        params: list[Any] = [user_id]
        if course_id:
            sql += " AND course_id=?"
            params.append(course_id)
        rows = self.conn.execute(sql + " ORDER BY source_id", params).fetchall()
        return [
            Source(r["source_id"], r["user_id"], r["course_id"], r["title"],
                   SourceType(r["type"]), r["file"], r["pages"], r["status"],
                   r["status_detail"], r["ocr_pages"],
                   r["file_hash"] if "file_hash" in r.keys() else "")
            for r in rows
        ]

    # -- background ingest jobs -------------------------------------------

    @_serialized
    def create_job(self, job_id: str, user_id: str, course_id: str, files: list[dict]) -> None:
        self.conn.execute(
            "INSERT INTO ingest_jobs(id, user_id, course_id, status, created_at, updated_at, files) "
            "VALUES(?,?,?,?,?,?,?)",
            (job_id, user_id, course_id, "queued", _now(), _now(), json.dumps(files)),
        )
        self.conn.commit()

    @_serialized
    def update_job(self, job_id: str, *, status: Optional[str] = None,
                   files: Optional[list[dict]] = None) -> None:
        sets, params = ["updated_at=?"], [_now()]
        if status is not None:
            sets.append("status=?"); params.append(status)
        if files is not None:
            sets.append("files=?"); params.append(json.dumps(files))
        params.append(job_id)
        self.conn.execute(f"UPDATE ingest_jobs SET {', '.join(sets)} WHERE id=?", params)
        self.conn.commit()

    def get_job(self, job_id: str, user_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM ingest_jobs WHERE id=? AND user_id=?", (job_id, user_id)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"], "course_id": row["course_id"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "files": json.loads(row["files"]),
        }

    # -- chapters (semantic syllabus linking) -----------------------------

    @_serialized
    def replace_chapters(self, user_id: str, source_id: str, rows: Sequence[tuple]) -> None:
        """rows: (course_id, chapter_num, title, page_start, page_end, embedding)"""
        self.conn.execute(
            "DELETE FROM chapters WHERE user_id=? AND source_id=?", (user_id, source_id)
        )
        self.conn.executemany(
            "INSERT INTO chapters(user_id, course_id, source_id, chapter_num, title, "
            "page_start, page_end, embedding) VALUES(?,?,?,?,?,?,?,?)",
            [
                (user_id, course_id, source_id, num, title, ps, pe,
                 None if emb is None else pack_vector(emb))
                for (course_id, num, title, ps, pe, emb) in rows
            ],
        )
        self.conn.commit()

    def chapters(self, user_id: str, course_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM chapters WHERE user_id=? AND course_id=? "
            "ORDER BY source_id, chapter_num",
            (user_id, course_id),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["embedding"] = unpack_vector(r["embedding"])
            out.append(d)
        return out

    # -- chunks -----------------------------------------------------------

    @_serialized
    def clear_source(self, user_id: str, source_id: str) -> None:
        """Scoped by user. Source ids are derived from course id + filename, so two
        users who upload the same textbook to the same course code collide -- an
        unscoped delete then wipes the other user's corpus."""
        self.conn.execute(
            "DELETE FROM chunks WHERE user_id=? AND source_id=?", (user_id, source_id)
        )
        self.conn.execute(
            "DELETE FROM parents WHERE user_id=? AND source_id=?", (user_id, source_id)
        )
        self.conn.commit()
        self._vector_cache = {}

    @_serialized
    def insert_parents(self, parents: Iterable[tuple[str, str, str, str]]) -> None:
        """(parent_id, user_id, source_id, text)"""
        self.conn.executemany(
            "INSERT OR REPLACE INTO parents(id, user_id, source_id, text) VALUES(?,?,?,?)",
            list(parents),
        )
        self.conn.commit()

    @_serialized
    def insert_chunks(self, chunks: Sequence[Chunk]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO chunks(id, user_id, source_id, course_id, chapter_num, "
            "chapter_title, section, page_start, page_end, text, parent_id, kind, token_count, "
            "ordinal, embedding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    c.id,
                    c.user_id,
                    c.source_id,
                    c.course_id,
                    c.chapter_num,
                    c.chapter_title,
                    c.section,
                    c.page_start,
                    c.page_end,
                    c.text,
                    c.parent_id,
                    c.kind.value,
                    c.token_count,
                    i,
                    None if c.embedding is None else pack_vector(c.embedding),
                )
                for i, c in enumerate(chunks)
            ],
        )
        self.conn.commit()
        self._vector_cache = {}

    def _row_to_chunk(self, row: sqlite3.Row, parent_text: str = "") -> Chunk:
        return Chunk(
            id=row["id"],
            user_id=row["user_id"],
            source_id=row["source_id"],
            course_id=row["course_id"],
            chapter_num=row["chapter_num"],
            chapter_title=row["chapter_title"],
            section=row["section"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            text=row["text"],
            parent_id=row["parent_id"],
            parent_text=parent_text,
            kind=BlockKind(row["kind"]),
            token_count=row["token_count"],
        )

    def all_chunks(self, user_id: str, course_id: Optional[str] = None) -> list[Chunk]:
        sql = "SELECT * FROM chunks WHERE user_id=?"
        params: list[Any] = [user_id]
        if course_id:
            sql += " AND course_id=?"
            params.append(course_id)
        sql += " ORDER BY source_id, ordinal"
        return [self._row_to_chunk(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_chunks(self, chunk_ids: Sequence[str], with_parents: bool = True) -> list[Chunk]:
        if not chunk_ids:
            return []
        marks = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"SELECT * FROM chunks WHERE id IN ({marks})", list(chunk_ids)
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        parent_texts: dict[str, str] = {}
        if with_parents:
            pids = [r["parent_id"] for r in rows if r["parent_id"]]
            if pids:
                pmarks = ",".join("?" * len(pids))
                for pr in self.conn.execute(
                    f"SELECT id, text FROM parents WHERE id IN ({pmarks})", pids
                ).fetchall():
                    parent_texts[pr["id"]] = pr["text"]
        # Preserve caller ordering -- ranking is the whole point.
        out = []
        for cid in chunk_ids:
            row = by_id.get(cid)
            if row is None:
                continue
            out.append(self._row_to_chunk(row, parent_texts.get(row["parent_id"] or "", "")))
        return out

    def chunk_count(self, user_id: Optional[str] = None) -> int:
        if user_id:
            return self.conn.execute(
                "SELECT COUNT(*) c FROM chunks WHERE user_id=?", (user_id,)
            ).fetchone()["c"]
        return self.conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]

    def chunk_count_for_course(self, user_id: str, course_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM chunks WHERE user_id=? AND course_id=?",
            (user_id, course_id),
        ).fetchone()["c"]

    @_serialized
    def delete_course(self, user_id: str, course_id: str) -> None:
        self.delete_course_scope(user_id, course_id, "entire_course")

    @staticmethod
    def _safe_delete_user(user_id: str) -> None:
        # The eval corpus is immutable from product-side operations.
        if user_id == "benchmark":
            raise PermissionError("The benchmark user cannot be deleted through course management.")

    def course_deletion_counts(self, user_id: str, course_id: str) -> dict[str, Any]:
        """Counts the rows/files affected by each independently selectable scope."""
        self._safe_delete_user(user_id)
        if self.course(user_id, course_id) is None:
            raise KeyError("course not found")

        def count(sql: str, params: Sequence[Any]) -> int:
            return int(self.conn.execute(sql, params).fetchone()[0])

        base = (user_id, course_id)
        material_source_sql = "SELECT source_id FROM sources WHERE user_id=? AND course_id=? AND type IN ('textbook','slides','notes','bbplus')"
        syllabus_source_sql = "SELECT source_id FROM sources WHERE user_id=? AND course_id=? AND (type='syllabus' OR lower(file) LIKE '%syllabus%' OR lower(file) LIKE '%course_outline%' OR lower(file) LIKE '%course-outline%' OR lower(file) LIKE '%syll%')"
        assessment_source_sql = "SELECT source_id FROM sources WHERE user_id=? AND course_id=? AND type='assessment'"
        mats = count("SELECT COUNT(*) FROM sources WHERE user_id=? AND course_id=? AND type IN ('textbook','slides','notes','bbplus')", base)
        material_chunks = count(f"SELECT COUNT(*) FROM chunks WHERE user_id=? AND course_id=? AND source_id IN ({material_source_sql})", base + base)
        material_chapters = count(f"SELECT COUNT(*) FROM chapters WHERE user_id=? AND course_id=? AND source_id IN ({material_source_sql})", base + base)
        material_parents = count(f"SELECT COUNT(*) FROM parents WHERE user_id=? AND source_id IN ({material_source_sql})", (user_id,) + base)
        syllabus_sources = count(f"SELECT COUNT(*) FROM sources WHERE user_id=? AND course_id=? AND source_id IN ({syllabus_source_sql})", base + base)
        syllabus_chunks = count(f"SELECT COUNT(*) FROM chunks WHERE user_id=? AND course_id=? AND source_id IN ({syllabus_source_sql})", base + base)
        syllabus_chapters = count(f"SELECT COUNT(*) FROM chapters WHERE user_id=? AND course_id=? AND source_id IN ({syllabus_source_sql})", base + base)
        syllabus_parents = count(f"SELECT COUNT(*) FROM parents WHERE user_id=? AND source_id IN ({syllabus_source_sql})", (user_id,) + base)
        schedules = count("SELECT COUNT(*) FROM syllabus_schedule WHERE user_id=? AND course_id=?", base)
        assessments = count("SELECT COUNT(*) FROM assessments WHERE user_id=? AND course_id=?", base)
        problems = count("SELECT COUNT(*) FROM practice_problems WHERE user_id=? AND course_id=?", base)
        variants = count("SELECT COUNT(*) FROM practice_problems WHERE user_id=? AND course_id=? AND origin='generated'", base)
        attempts = count("SELECT COUNT(*) FROM practice_attempts WHERE user_id=? AND (problem_id IN (SELECT id FROM practice_problems WHERE user_id=? AND course_id=?) OR substr(chapter_ref,1,?)=?)", (user_id, *base, len(course_id) + 1, course_id + ":"))
        engagements = count("SELECT COUNT(*) FROM engagement WHERE user_id=? AND substr(chapter_ref,1,?)=?", (user_id, len(course_id) + 1, course_id + ":"))
        manual_progress = count("SELECT COUNT(*) FROM manual_progress WHERE user_id=? AND substr(chapter_ref,1,?)=?", (user_id, len(course_id) + 1, course_id + ":"))
        schedule_events = count("SELECT COUNT(*) FROM calendar_events WHERE user_id=? AND course_id=? AND notes='from syllabus'", base)
        calendar_events = count("SELECT COUNT(*) FROM calendar_events WHERE user_id=? AND course_id=?", base)
        conversations = count("SELECT COUNT(*) FROM conversations WHERE user_id=? AND course_id=?", base)
        messages = count("SELECT COUNT(*) FROM messages WHERE user_id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=? AND course_id=?)", (user_id,) + base)
        artifacts = count("SELECT COUNT(*) FROM artifacts WHERE user_id=? AND message_id IN (SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id AND c.user_id=m.user_id WHERE c.user_id=? AND c.course_id=?)", (user_id,) + base)
        jobs = count("SELECT COUNT(*) FROM ingest_jobs WHERE user_id=? AND course_id=?", base)
        runs = count("SELECT COUNT(*) FROM agent_runs WHERE user_id=? AND course_id=?", base)
        syllabus_task_status = count("SELECT COUNT(*) FROM task_status WHERE user_id=? AND (item_id IN (SELECT id FROM syllabus_schedule WHERE user_id=? AND course_id=?) OR item_id IN (SELECT id FROM calendar_events WHERE user_id=? AND course_id=? AND notes='from syllabus'))", (user_id, *base, *base))
        study_plans = count("SELECT COUNT(*) FROM study_plans WHERE user_id=? AND assessment_id IN (SELECT id FROM assessments WHERE user_id=? AND course_id=?)", (user_id, *base))
        all_task_status = count("SELECT COUNT(*) FROM task_status WHERE user_id=? AND (item_id IN (SELECT id FROM syllabus_schedule WHERE user_id=? AND course_id=?) OR item_id IN (SELECT id FROM calendar_events WHERE user_id=? AND course_id=?) OR item_id IN (SELECT id FROM assessments WHERE user_id=? AND course_id=?) OR substr(item_id,1,?)=?)", (user_id, *base, *base, *base, len(course_id) + 1, course_id + ":"))
        all_parents = count("SELECT COUNT(*) FROM parents WHERE user_id=? AND source_id IN (SELECT source_id FROM sources WHERE user_id=? AND course_id=? UNION SELECT source_id FROM chunks WHERE user_id=? AND course_id=? UNION SELECT source_id FROM chapters WHERE user_id=? AND course_id=? UNION SELECT source_id FROM practice_problems WHERE user_id=? AND course_id=?)", (user_id, *base, *base, *base, *base))
        grade_rows = sum(count(f"SELECT COUNT(*) FROM {table} WHERE user_id=? AND course_id=?", base)
                         for table in ("grade_extractions", "grade_rules", "grade_scores", "grade_syllabus_selections"))
        syllabus_files = syllabus_sources
        assessment_files = count(f"SELECT COUNT(*) FROM sources WHERE user_id=? AND course_id=? AND source_id IN ({assessment_source_sql})", base + base)
        return {
            "materials": {"sources": mats, "chunks": material_chunks, "chapters": material_chapters,
                          "parents": material_parents},
            "syllabus": {"sources": syllabus_sources, "chunks": syllabus_chunks,
                         "chapters": syllabus_chapters, "parents": syllabus_parents,
                         "schedule_rows": schedules, "task_status": syllabus_task_status,
                         "study_plans": study_plans,
                         "assessments": assessments, "calendar_events": schedule_events,
                         "grade_records": grade_rows},
            "assessment_uploads": {"sources": assessment_files, "practice_problems": problems,
                                   "generated_variants": variants, "attempts": attempts,
                                   "parents": count(f"SELECT COUNT(*) FROM parents WHERE user_id=? AND source_id IN ({assessment_source_sql})", (user_id,) + base)},
            "entire_course": {"chunks": count("SELECT COUNT(*) FROM chunks WHERE user_id=? AND course_id=?", base),
                "chapters": count("SELECT COUNT(*) FROM chapters WHERE user_id=? AND course_id=?", base),
                "sources": count("SELECT COUNT(*) FROM sources WHERE user_id=? AND course_id=?", base),
                "schedule_rows": schedules, "assessments": assessments,
                "practice_problems": problems, "generated_variants": variants, "attempts": attempts,
                "engagement_records": engagements, "manual_progress": manual_progress,
                "conversations": conversations, "messages": messages, "artifacts": artifacts,
                "parents": all_parents, "study_plans": study_plans, "task_status": all_task_status,
                "calendar_events": calendar_events, "grade_records": grade_rows,
                "ingest_jobs": jobs, "agent_runs": runs},
        }

    @_serialized
    def delete_course_scope(self, user_id: str, course_id: str, scope: str) -> dict[str, Any]:
        """Perform one scoped purge atomically. Every content query includes user_id.

        Returns file paths to unlink only after the SQL transaction commits.
        """
        self._safe_delete_user(user_id)
        if scope not in {"materials", "syllabus", "assessment_uploads", "entire_course"}:
            raise ValueError("Unknown deletion scope.")
        if self.course(user_id, course_id) is None:
            raise KeyError("course not found")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        paths: list[str] = []
        try:
            def values(sql: str, params: Sequence[Any]) -> list[str]:
                return [str(r[0]) for r in conn.execute(sql, params).fetchall() if r[0] is not None]

            base = (user_id, course_id)
            all_sources = values("SELECT source_id FROM sources WHERE user_id=? AND course_id=?", base)
            selected_types = {
                "materials": ("textbook", "slides", "notes", "bbplus"),
                "syllabus": ("syllabus",),
                "assessment_uploads": ("assessment",),
                "entire_course": ("textbook", "slides", "notes", "readings", "assessment", "syllabus", "bbplus"),
            }[scope]
            marks = ",".join("?" for _ in selected_types)
            selected_sources = values(
                f"SELECT source_id FROM sources WHERE user_id=? AND course_id=? AND type IN ({marks})",
                base + selected_types)
            if scope == "syllabus":
                selected_sources = values(
                    "SELECT source_id FROM sources WHERE user_id=? AND course_id=? AND (type='syllabus' OR lower(file) LIKE '%syllabus%' OR lower(file) LIKE '%course_outline%' OR lower(file) LIKE '%course-outline%' OR lower(file) LIKE '%syll%')",
                    base)
            if scope == "entire_course":
                paths.extend(values("SELECT file FROM sources WHERE user_id=? AND course_id=?", base))
            elif selected_sources:
                sm = ",".join("?" for _ in selected_sources)
                paths.extend(values(f"SELECT file FROM sources WHERE user_id=? AND course_id=? AND source_id IN ({sm})", base + tuple(selected_sources)))

            if scope == "materials" and selected_sources:
                sm = ",".join("?" for _ in selected_sources)
                scoped = (user_id, *selected_sources)
                conn.execute(f"DELETE FROM chunks WHERE user_id=? AND course_id=? AND source_id IN ({sm})", base + tuple(selected_sources))
                conn.execute(f"DELETE FROM chapters WHERE user_id=? AND course_id=? AND source_id IN ({sm})", base + tuple(selected_sources))
                conn.execute(f"DELETE FROM parents WHERE user_id=? AND source_id IN ({sm})", scoped)
                conn.execute(f"DELETE FROM sources WHERE user_id=? AND course_id=? AND source_id IN ({sm})", base + tuple(selected_sources))
            elif scope == "syllabus":
                conn.execute("DELETE FROM task_status WHERE user_id=? AND (item_id IN (SELECT id FROM syllabus_schedule WHERE user_id=? AND course_id=?) OR item_id IN (SELECT id FROM calendar_events WHERE user_id=? AND course_id=? AND notes='from syllabus'))", (user_id, *base, *base))
                conn.execute("DELETE FROM study_plans WHERE user_id=? AND assessment_id IN (SELECT id FROM assessments WHERE user_id=? AND course_id=?)", (user_id, *base))
                conn.execute("DELETE FROM syllabus_schedule WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM assessments WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM calendar_events WHERE user_id=? AND course_id=? AND notes='from syllabus'", base)
                for table in ("grade_extractions", "grade_rules", "grade_scores", "grade_syllabus_selections"):
                    conn.execute(f"DELETE FROM {table} WHERE user_id=? AND course_id=?", base)
                if selected_sources:
                    sm = ",".join("?" for _ in selected_sources)
                    conn.execute(f"DELETE FROM chunks WHERE user_id=? AND course_id=? AND source_id IN ({sm})", base + tuple(selected_sources))
                    conn.execute(f"DELETE FROM chapters WHERE user_id=? AND course_id=? AND source_id IN ({sm})", base + tuple(selected_sources))
                    conn.execute(f"DELETE FROM parents WHERE user_id=? AND source_id IN ({sm})", (user_id, *selected_sources))
                    conn.execute(f"DELETE FROM sources WHERE user_id=? AND course_id=? AND source_id IN ({sm})", base + tuple(selected_sources))
            elif scope == "assessment_uploads":
                conn.execute("DELETE FROM practice_attempts WHERE user_id=? AND (problem_id IN (SELECT id FROM practice_problems WHERE user_id=? AND course_id=?) OR substr(chapter_ref,1,?)=?)", (user_id, *base, len(course_id) + 1, course_id + ":"))
                conn.execute("DELETE FROM practice_problems WHERE user_id=? AND course_id=?", base)
                if selected_sources:
                    sm = ",".join("?" for _ in selected_sources)
                    conn.execute(f"DELETE FROM parents WHERE user_id=? AND source_id IN ({sm})", (user_id, *selected_sources))
                    conn.execute(f"DELETE FROM sources WHERE user_id=? AND course_id=? AND source_id IN ({sm})", base + tuple(selected_sources))
            else:
                # Capture associated IDs before deleting parents so every dependent
                # row can be removed under the same transaction and same user.
                problem_source_ids = values("SELECT DISTINCT source_id FROM practice_problems WHERE user_id=? AND course_id=?", base)
                chapter_prefix = course_id + ":"
                conn.execute("DELETE FROM artifacts WHERE user_id=? AND message_id IN (SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id AND c.user_id=m.user_id WHERE c.user_id=? AND c.course_id=?)", (user_id, *base))
                conn.execute("DELETE FROM messages WHERE user_id=? AND conversation_id IN (SELECT id FROM conversations WHERE user_id=? AND course_id=?)", (user_id, *base))
                conn.execute("DELETE FROM conversations WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM practice_attempts WHERE user_id=? AND (problem_id IN (SELECT id FROM practice_problems WHERE user_id=? AND course_id=?) OR substr(chapter_ref,1,?)=?)", (user_id, *base, len(course_id) + 1, course_id + ":"))
                conn.execute("DELETE FROM practice_problems WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM study_plans WHERE user_id=? AND assessment_id IN (SELECT id FROM assessments WHERE user_id=? AND course_id=?)", (user_id, *base))
                conn.execute("DELETE FROM task_status WHERE user_id=? AND (item_id IN (SELECT id FROM syllabus_schedule WHERE user_id=? AND course_id=?) OR item_id IN (SELECT id FROM calendar_events WHERE user_id=? AND course_id=?) OR item_id IN (SELECT id FROM assessments WHERE user_id=? AND course_id=?) OR substr(item_id,1,?)=?)", (user_id, *base, *base, *base, len(course_id) + 1, course_id + ":"))
                conn.execute("DELETE FROM syllabus_schedule WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM assessments WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM calendar_events WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM agent_runs WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM ingest_jobs WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM grade_extractions WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM grade_rules WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM grade_scores WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM grade_syllabus_selections WHERE user_id=? AND course_id=?", base)
                conn.execute("DELETE FROM manual_progress WHERE user_id=? AND substr(chapter_ref,1,?)=?", (user_id, len(chapter_prefix), chapter_prefix))
                conn.execute("DELETE FROM engagement WHERE user_id=? AND substr(chapter_ref,1,?)=?", (user_id, len(chapter_prefix), chapter_prefix))
                # Include orphaned source-linked content even if its source row
                # was already missing, while preserving colliding users' records.
                source_ids = set(all_sources)
                source_ids.update(values("SELECT source_id FROM chunks WHERE user_id=? AND course_id=?", base))
                source_ids.update(values("SELECT source_id FROM chapters WHERE user_id=? AND course_id=?", base))
                source_ids.update(problem_source_ids)
                if source_ids:
                    sm = ",".join("?" for _ in source_ids)
                    conn.execute(f"DELETE FROM parents WHERE user_id=? AND source_id IN ({sm})", (user_id, *source_ids))
                # Discover direct course-owned tables from the live schema so
                # adding a feature cannot silently leave rows behind. Refuse
                # to guess tenant scope if a future table omits user_id.
                table_names = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in table_names:
                    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                    if "course_id" not in columns:
                        continue
                    if "user_id" not in columns:
                        raise RuntimeError(f"Course-owned table {table} has no user_id deletion boundary.")
                    conn.execute(f'DELETE FROM "{table}" WHERE user_id=? AND course_id=?', base)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self._vector_cache.pop(user_id, None)
        return {"files": list(dict.fromkeys(paths)), "deleted": True}

    @_serialized
    def save_grade_extraction(self, user_id: str, course_id: str, syllabus_hash: str,
                              schema: dict[str, Any], backend: str, source_id: str = "",
                              file_name: str = "") -> str:
        extraction_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO grade_extractions(id,user_id,course_id,syllabus_hash,schema_json,backend,status,created_at,source_id,file_name) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (extraction_id, user_id, course_id, syllabus_hash, json.dumps(schema), backend,
             "pending", _now(), source_id, file_name),
        )
        self.conn.commit()
        return extraction_id

    @_serialized
    def confirm_grade_rules(self, user_id: str, course_id: str, schema: dict[str, Any],
                            extraction_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO grade_rules(user_id,course_id,schema_json,confirmed_at,source_id,syllabus_hash) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id,course_id) DO UPDATE SET schema_json=excluded.schema_json,confirmed_at=excluded.confirmed_at,source_id=excluded.source_id,syllabus_hash=excluded.syllabus_hash",
            (user_id, course_id, json.dumps(schema), _now(), "", ""),
        )
        if extraction_id:
            extraction = self.conn.execute(
                "SELECT source_id,syllabus_hash FROM grade_extractions WHERE id=? AND user_id=? AND course_id=?",
                (extraction_id, user_id, course_id),
            ).fetchone()
            if extraction:
                self.conn.execute(
                    "UPDATE grade_rules SET source_id=?,syllabus_hash=? WHERE user_id=? AND course_id=?",
                    (extraction["source_id"], extraction["syllabus_hash"], user_id, course_id),
                )
            self.conn.execute(
                "UPDATE grade_extractions SET status='confirmed' WHERE id=? AND user_id=? AND course_id=?",
                (extraction_id, user_id, course_id),
            )
        self.conn.commit()

    @_serialized
    def save_grade_syllabus_selection(self, user_id: str, course_id: str, source_id: str,
                                      file_name: str, syllabus_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO grade_syllabus_selections(user_id,course_id,source_id,file_name,syllabus_hash,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id,course_id) DO UPDATE SET source_id=excluded.source_id,file_name=excluded.file_name,syllabus_hash=excluded.syllabus_hash,updated_at=excluded.updated_at",
            (user_id, course_id, source_id, file_name, syllabus_hash, _now()),
        )
        self.conn.commit()

    def grade_syllabus_selection(self, user_id: str, course_id: str) -> Optional[dict[str, str]]:
        row = self.conn.execute(
            "SELECT source_id,file_name,syllabus_hash FROM grade_syllabus_selections WHERE user_id=? AND course_id=?",
            (user_id, course_id),
        ).fetchone()
        return dict(row) if row else None

    def grade_extraction_for_syllabus(self, user_id: str, course_id: str, source_id: str,
                                      syllabus_hash: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id,syllabus_hash,schema_json,backend,status,created_at,source_id,file_name FROM grade_extractions WHERE user_id=? AND course_id=? AND source_id=? AND syllabus_hash=? ORDER BY created_at DESC LIMIT 1",
            (user_id, course_id, source_id, syllabus_hash),
        ).fetchone()
        if not row:
            return None
        try:
            parsed = json.loads(row["schema_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return {"id": row["id"], "syllabus_hash": row["syllabus_hash"],
                "schema": parsed, "backend": row["backend"],
                "status": row["status"], "created_at": row["created_at"],
                "source_id": row["source_id"], "file_name": row["file_name"]}

    def grade_predictor(self, user_id: str, course_id: str) -> dict[str, Any]:
        rules = self.conn.execute(
            "SELECT schema_json,confirmed_at,source_id,syllabus_hash FROM grade_rules WHERE user_id=? AND course_id=?",
            (user_id, course_id),
        ).fetchone()
        pending = self.conn.execute(
            "SELECT id,syllabus_hash,schema_json,backend,status,created_at FROM grade_extractions "
            "WHERE user_id=? AND course_id=? ORDER BY created_at DESC LIMIT 1", (user_id, course_id),
        ).fetchone()
        scores = self.conn.execute(
            "SELECT item_id,score FROM grade_scores WHERE user_id=? AND course_id=?", (user_id, course_id),
        ).fetchall()
        return {"confirmed": json.loads(rules["schema_json"]) if rules else None,
                "confirmed_at": rules["confirmed_at"] if rules else None,
                "confirmed_source_id": rules["source_id"] if rules else "",
                "confirmed_syllabus_hash": rules["syllabus_hash"] if rules else "",
                "pending": ({"id": pending["id"], "syllabus_hash": pending["syllabus_hash"],
                             "schema": json.loads(pending["schema_json"]), "backend": pending["backend"],
                             "status": pending["status"], "created_at": pending["created_at"]} if pending else None),
                "scores": {row["item_id"]: row["score"] for row in scores}}

    @_serialized
    def set_grade_score(self, user_id: str, course_id: str, item_id: str, score: float | None) -> None:
        if score is None:
            self.conn.execute("DELETE FROM grade_scores WHERE user_id=? AND course_id=? AND item_id=?",
                              (user_id, course_id, item_id))
        else:
            self.conn.execute(
                "INSERT INTO grade_scores(user_id,course_id,item_id,score,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id,course_id,item_id) DO UPDATE SET score=excluded.score,updated_at=excluded.updated_at",
                (user_id, course_id, item_id, score, _now()),
            )
        self.conn.commit()

    def resolve_pages_to_chunks(
        self, source_id: str, page_start: int, page_end: int,
        user_id: Optional[str] = None,
    ) -> list[str]:
        """Eval gold labels are (source, page range). Resolving them at eval time
        rather than storing chunk ids means a re-chunk does not silently invalidate
        the question set. Scoped by user when given: two users can hold the same
        source_id (same file, same course code), and an unscoped match would leak
        one user's chunk ids into another's gold resolution."""
        sql = "SELECT id FROM chunks WHERE source_id=? AND page_start<=? AND page_end>=?"
        params: list[Any] = [source_id, page_end, page_start]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        sql += " ORDER BY ordinal"
        return [r["id"] for r in self.conn.execute(sql, params).fetchall()]

    # -- vectors ----------------------------------------------------------

    def vector_matrix(self, user_id: str) -> tuple[list[str], np.ndarray]:
        """Row-normalised matrix of one user's embedded chunks, cached per user."""
        if user_id in self._vector_cache:
            return self._vector_cache[user_id]
        rows = self.conn.execute(
            "SELECT id, embedding FROM chunks WHERE user_id=? AND embedding IS NOT NULL "
            "ORDER BY source_id, ordinal",
            (user_id,),
        ).fetchall()
        if not rows:
            self._vector_cache[user_id] = ([], np.zeros((0, 1), dtype=np.float32))
            return self._vector_cache[user_id]
        ids = [r["id"] for r in rows]
        mat = np.vstack([unpack_vector(r["embedding"]) for r in rows]).astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._vector_cache[user_id] = (ids, mat / norms)
        return self._vector_cache[user_id]

    # -- engagement -------------------------------------------------------

    @_serialized
    def bump_engagement(
        self,
        user_id: str,
        chapter_ref: str,
        questions: int = 0,
        ps_hits: int = 0,
        exports: int = 0,
    ) -> None:
        self.conn.execute(
            "INSERT INTO engagement(user_id, chapter_ref, questions_asked, ps_questions_hit, "
            "notes_exported, last_touched_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id, chapter_ref) DO UPDATE SET "
            "questions_asked = questions_asked + excluded.questions_asked, "
            "ps_questions_hit = ps_questions_hit + excluded.ps_questions_hit, "
            "notes_exported = notes_exported + excluded.notes_exported, "
            "last_touched_at = excluded.last_touched_at",
            (user_id, chapter_ref, questions, ps_hits, exports, _now()),
        )
        self.conn.commit()

    def engagement(self, user_id: str) -> dict[str, Engagement]:
        rows = self.conn.execute(
            "SELECT * FROM engagement WHERE user_id=?", (user_id,)
        ).fetchall()
        return {
            r["chapter_ref"]: Engagement(
                user_id=r["user_id"],
                chapter_ref=r["chapter_ref"],
                questions_asked=r["questions_asked"],
                ps_questions_hit=r["ps_questions_hit"],
                notes_exported=r["notes_exported"],
                last_touched_at=(
                    datetime.fromisoformat(r["last_touched_at"]) if r["last_touched_at"] else None
                ),
            )
            for r in rows
        }

    @_serialized
    def set_manual_progress(self, user_id: str, chapter_ref: str, value: float) -> None:
        self.conn.execute(
            "INSERT INTO manual_progress(user_id, chapter_ref, value, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, chapter_ref) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (user_id, chapter_ref, max(0.0, min(1.0, value)), _now()),
        )
        self.conn.commit()

    def manual_progress(self, user_id: str) -> dict[str, float]:
        rows = self.conn.execute(
            "SELECT chapter_ref, value FROM manual_progress WHERE user_id=?", (user_id,)
        ).fetchall()
        return {r["chapter_ref"]: r["value"] for r in rows}

    # -- schedule ---------------------------------------------------------

    @_serialized
    def update_schedule_row(
        self, user_id: str, row_id: str, chapter_refs=None,
        topic: Optional[str] = None, needs_review: bool = False,
    ) -> int:
        """Apply one review-screen correction. A user-confirmed link is recorded
        as method 'user' with full confidence, so a later re-link never silently
        overwrites a human decision with a semantic guess."""
        sets, params = [], []
        if chapter_refs is not None:
            sets.append("chapter_refs=?"); params.append(json.dumps(list(chapter_refs)))
            sets.append("link_method=?"); params.append("user")
            sets.append("link_score=?"); params.append(1.0)
        if topic is not None:
            sets.append("topic=?"); params.append(topic)
        sets.append("needs_review=?"); params.append(int(needs_review))
        params.extend([user_id, row_id])
        cur = self.conn.execute(
            f"UPDATE syllabus_schedule SET {', '.join(sets)} WHERE user_id=? AND id=?",
            params,
        )
        self.conn.commit()
        return cur.rowcount

    @_serialized
    def clear_schedule(self, user_id: str, course_id: str) -> None:
        self.conn.execute(
            "DELETE FROM syllabus_schedule WHERE user_id=? AND course_id=?", (user_id, course_id)
        )
        self.conn.commit()

    @_serialized
    def insert_schedule_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO syllabus_schedule(id, user_id, course_id, date, kind, "
            "topic, chapter_refs, readings, weight, confidence, link_score, link_method, "
            "link_title, needs_review) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    r.get("id") or uuid.uuid4().hex,
                    r["user_id"],
                    r["course_id"],
                    r["date"],
                    r["kind"],
                    r.get("topic", ""),
                    json.dumps(r.get("chapter_refs", [])),
                    json.dumps(r.get("readings", [])),
                    r.get("weight", 0.0),
                    r.get("confidence", "high"),
                    r.get("link_score", 0.0),
                    r.get("link_method", ""),
                    r.get("link_title", ""),
                    int(r.get("needs_review", 0)),
                )
                for r in rows
            ],
        )
        self.conn.commit()

    def schedule(
        self,
        user_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        course_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM syllabus_schedule WHERE user_id=?"
        params: list[Any] = [user_id]
        # A date filter implies dated rows only; undated rows are fetched via
        # schedule_undated() so callers decide whether to include them.
        if start:
            sql += " AND date IS NOT NULL AND date>=?"
            params.append(start)
        if end:
            sql += " AND date IS NOT NULL AND date<=?"
            params.append(end)
        if course_id:
            sql += " AND course_id=?"
            params.append(course_id)
        sql += " ORDER BY date IS NULL, date, course_id"
        out = []
        for r in self.conn.execute(sql, params).fetchall():
            d = dict(r)
            d["chapter_refs"] = json.loads(d["chapter_refs"])
            d["readings"] = json.loads(d["readings"])
            out.append(d)
        return out

    def schedule_undated(
        self, user_id: str, course_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Rows with no date -- the common case for an undated 'course outline'.
        Rendered in their own dashboard section rather than dropped."""
        sql = "SELECT * FROM syllabus_schedule WHERE user_id=? AND date IS NULL"
        params: list[Any] = [user_id]
        if course_id:
            sql += " AND course_id=?"
            params.append(course_id)
        sql += " ORDER BY course_id, topic"
        out = []
        for r in self.conn.execute(sql, params).fetchall():
            d = dict(r)
            d["chapter_refs"] = json.loads(d["chapter_refs"])
            d["readings"] = json.loads(d["readings"])
            out.append(d)
        return out

    # -- logs -------------------------------------------------------------

    @_serialized
    def cache_search(self, query: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO series_search_cache(query, fetched_at, payload) "
            "VALUES(?,?,?)", (query.strip().lower(), _now(), json.dumps(payload)),
        )
        self.conn.commit()

    def cached_search(self, query: str, ttl_s: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT fetched_at, payload FROM series_search_cache WHERE query=?",
            (query.strip().lower(),),
        ).fetchone()
        if row is None:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])).total_seconds()
        return None if age > ttl_s else json.loads(row["payload"])

    @_serialized
    def cache_series(self, series_id: str, transform: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO series_cache(series_id, transform, fetched_at, payload) "
            "VALUES(?,?,?,?)", (series_id, transform, _now(), json.dumps(payload)),
        )
        self.conn.commit()

    def cached_series(self, series_id: str, transform: str, ttl_s: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT fetched_at, payload FROM series_cache WHERE series_id=? AND transform=?",
            (series_id, transform),
        ).fetchone()
        if row is None:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])).total_seconds()
        if age > ttl_s:
            return None
        payload = json.loads(row["payload"])
        payload["fetched_at"] = row["fetched_at"]
        return payload

    # -- assessments ------------------------------------------------------

    @_serialized
    def upsert_assessment(self, a: AssessmentRecord) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO assessments(id, user_id, course_id, kind, title, due_date, "
            "weight, chapter_refs, status, user_entered, source, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, title=excluded.title, "
            "due_date=excluded.due_date, weight=excluded.weight, "
            "chapter_refs=excluded.chapter_refs, status=excluded.status, "
            "user_entered=excluded.user_entered, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (a.id, a.user_id, a.course_id, a.kind.value, a.title,
             a.due_date.isoformat() if a.due_date else None, a.weight,
             json.dumps(a.chapter_refs), a.status.value, int(a.user_entered),
             a.source, now, now),
        )
        self.conn.commit()

    @_serialized
    def delete_assessment(self, user_id: str, assessment_id: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM assessments WHERE user_id=? AND id=?", (user_id, assessment_id)
        )
        self.conn.commit()
        return cur.rowcount

    @_serialized
    def clear_extracted_assessments(self, user_id: str, course_id: str) -> None:
        """Re-committing a syllabus replaces its extracted items but must never
        delete what the student added or edited by hand."""
        self.conn.execute(
            "DELETE FROM assessments WHERE user_id=? AND course_id=? AND source='extracted'",
            (user_id, course_id),
        )
        self.conn.commit()

    def _row_to_assessment(self, r: sqlite3.Row) -> AssessmentRecord:
        from datetime import date as _date
        return AssessmentRecord(
            id=r["id"], user_id=r["user_id"], course_id=r["course_id"],
            kind=AssessmentKind(r["kind"]), title=r["title"],
            due_date=_date.fromisoformat(r["due_date"]) if r["due_date"] else None,
            weight=r["weight"], chapter_refs=json.loads(r["chapter_refs"]),
            status=AssessmentStatus(r["status"]), user_entered=bool(r["user_entered"]),
            source=r["source"],
        )

    def assessments(self, user_id: str, course_id: Optional[str] = None) -> list[AssessmentRecord]:
        sql = "SELECT * FROM assessments WHERE user_id=?"
        params: list[Any] = [user_id]
        if course_id:
            sql += " AND course_id=?"
            params.append(course_id)
        # NULL due dates sort last; then by date, then title.
        sql += " ORDER BY due_date IS NULL, due_date, title"
        return [self._row_to_assessment(r) for r in self.conn.execute(sql, params).fetchall()]

    def assessment(self, user_id: str, assessment_id: str) -> Optional[AssessmentRecord]:
        r = self.conn.execute(
            "SELECT * FROM assessments WHERE user_id=? AND id=?", (user_id, assessment_id)
        ).fetchone()
        return self._row_to_assessment(r) if r else None

    # -- calendar feed token ----------------------------------------------

    @_serialized
    def feed_token(self, user_id: str) -> str:
        """Stable per-user token for the read-only .ics feed. Created once."""
        r = self.conn.execute(
            "SELECT token FROM calendar_feeds WHERE user_id=?", (user_id,)
        ).fetchone()
        if r is not None:
            return r["token"]
        token = uuid.uuid4().hex + uuid.uuid4().hex   # 256 bits, unguessable
        self.conn.execute(
            "INSERT INTO calendar_feeds(user_id, token, created_at) VALUES(?,?,?)",
            (user_id, token, _now()),
        )
        self.conn.commit()
        return token

    def user_for_token(self, token: str) -> Optional[str]:
        r = self.conn.execute(
            "SELECT user_id FROM calendar_feeds WHERE token=?", (token,)
        ).fetchone()
        return r["user_id"] if r else None

    # -- calendar events + task check-off ---------------------------------

    _EVENT_COLS = ("id", "user_id", "course_id", "title", "kind", "date",
                   "start_time", "end_time", "location", "notes", "recurrence",
                   "recur_days", "recur_until", "done")

    @_serialized
    def upsert_event(self, event: dict[str, Any]) -> str:
        event = dict(event)
        if not event.get("id"):
            event["id"] = uuid.uuid4().hex
        row = {k: event.get(k) for k in self._EVENT_COLS}
        row["user_id"] = event["user_id"]
        row["done"] = int(bool(event.get("done", 0)))
        self.conn.execute(
            "INSERT INTO calendar_events(id, user_id, course_id, title, kind, date, "
            "start_time, end_time, location, notes, recurrence, recur_days, recur_until, "
            "done, created_at) VALUES(:id,:user_id,:course_id,:title,:kind,:date,"
            ":start_time,:end_time,:location,:notes,:recurrence,:recur_days,:recur_until,"
            ":done,:created_at) "
            "ON CONFLICT(id) DO UPDATE SET course_id=excluded.course_id, "
            "title=excluded.title, kind=excluded.kind, date=excluded.date, "
            "start_time=excluded.start_time, end_time=excluded.end_time, "
            "location=excluded.location, notes=excluded.notes, "
            "recurrence=excluded.recurrence, recur_days=excluded.recur_days, "
            "recur_until=excluded.recur_until, done=excluded.done",
            {**row,
             "course_id": event.get("course_id", "") or "",
             "kind": event.get("kind", "custom"),
             "start_time": event.get("start_time", "") or "",
             "end_time": event.get("end_time", "") or "",
             "location": event.get("location", "") or "",
             "notes": event.get("notes", "") or "",
             "recurrence": event.get("recurrence", "none") or "none",
             "recur_days": event.get("recur_days", "") or "",
             "recur_until": event.get("recur_until"),
             "created_at": _now()},
        )
        self.conn.commit()
        return event["id"]

    def events(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM calendar_events WHERE user_id=?", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def event(self, user_id: str, event_id: str) -> Optional[dict[str, Any]]:
        r = self.conn.execute(
            "SELECT * FROM calendar_events WHERE user_id=? AND id=?", (user_id, event_id)
        ).fetchone()
        return dict(r) if r else None

    @_serialized
    def delete_event(self, user_id: str, event_id: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM calendar_events WHERE user_id=? AND id=?", (user_id, event_id)
        )
        self.conn.commit()
        return cur.rowcount

    @_serialized
    def delete_events_from_syllabus(self, user_id: str, course_id: str) -> int:
        """Remove events a prior syllabus extraction created for this course, so a
        re-extract replaces rather than duplicates them. User-added events (a
        different note) are untouched."""
        cur = self.conn.execute(
            "DELETE FROM calendar_events WHERE user_id=? AND course_id=? AND notes='from syllabus'",
            (user_id, course_id),
        )
        self.conn.commit()
        return cur.rowcount

    @_serialized
    def set_task_done(self, user_id: str, item_id: str, done: bool) -> None:
        self.conn.execute(
            "INSERT INTO task_status(user_id, item_id, done, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, item_id) DO UPDATE SET done=excluded.done, "
            "updated_at=excluded.updated_at",
            (user_id, item_id, int(bool(done)), _now()),
        )
        self.conn.commit()

    def task_done(self, user_id: str) -> set[str]:
        return {r["item_id"] for r in self.conn.execute(
            "SELECT item_id FROM task_status WHERE user_id=? AND done=1", (user_id,))}

    # -- per-user preferences (privacy boundary) --------------------------

    def get_prefs(self, user_id: str) -> dict[str, Any]:
        r = self.conn.execute(
            "SELECT llm_consent, consent_decided, gemini_api_key, anthropic_api_key, depth, "
            "voice_consent, voice_only_mode, auto_submit_voice, selected_voice_id, speech_speed "
            "FROM user_prefs WHERE user_id=?", (user_id,)
        ).fetchone()
        if not r:
            return {"llm_consent": False, "consent_decided": False, "gemini_api_key": "",
                    "anthropic_api_key": "", "depth": "concise", "voice_consent": False,
                    "voice_only_mode": False, "auto_submit_voice": True,
                    "selected_voice_id": "", "speech_speed": 1.0}
        return {"llm_consent": bool(r["llm_consent"]),
                "consent_decided": bool(r["consent_decided"]),
                "gemini_api_key": r["gemini_api_key"],
                "anthropic_api_key": r["anthropic_api_key"], "depth": r["depth"],
                "voice_consent": bool(r["voice_consent"]),
                "voice_only_mode": bool(r["voice_only_mode"]),
                "auto_submit_voice": bool(r["auto_submit_voice"]),
                "selected_voice_id": r["selected_voice_id"], "speech_speed": r["speech_speed"]}

    @_serialized
    def set_prefs(self, user_id: str, **fields: Any) -> None:
        cur = self.get_prefs(user_id)
        cur.update({k: v for k, v in fields.items() if v is not None})
        self.conn.execute(
            "INSERT INTO user_prefs(user_id, llm_consent, consent_decided, gemini_api_key, "
            "anthropic_api_key, depth, voice_consent, voice_only_mode, auto_submit_voice, "
            "selected_voice_id, speech_speed, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET llm_consent=excluded.llm_consent, "
            "consent_decided=excluded.consent_decided, "
            "gemini_api_key=excluded.gemini_api_key, "
            "anthropic_api_key=excluded.anthropic_api_key, depth=excluded.depth, "
            "voice_consent=excluded.voice_consent, voice_only_mode=excluded.voice_only_mode, "
            "auto_submit_voice=excluded.auto_submit_voice, "
            "selected_voice_id=excluded.selected_voice_id, speech_speed=excluded.speech_speed, "
            "updated_at=excluded.updated_at",
            (user_id, int(bool(cur["llm_consent"])), int(bool(cur["consent_decided"])),
             cur["gemini_api_key"], cur["anthropic_api_key"], cur["depth"],
             int(bool(cur["voice_consent"])), int(bool(cur["voice_only_mode"])),
             int(bool(cur["auto_submit_voice"])), cur["selected_voice_id"],
             float(cur["speech_speed"]), _now()),
        )
        self.conn.commit()

    # -- practice problems (kept OUT of the retrieval corpus) -------------

    @_serialized
    def insert_problems(self, problems: Sequence[Problem]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO practice_problems(id, user_id, course_id, source_id, "
            "origin, parent_id, number, prompt, type, topic, chapter_ref, difficulty, "
            "given_solution, answer, solution_steps, verified, verify_method, needs_review, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (p.id, p.user_id, p.course_id, p.source_id, p.origin, p.parent_id,
                 p.number, p.prompt, p.type, p.topic, p.chapter_ref, p.difficulty,
                 p.given_solution, p.answer, p.solution_steps, int(p.verified),
                 p.verify_method, int(p.needs_review), _now())
                for p in problems
            ],
        )
        self.conn.commit()

    def _row_to_problem(self, r: sqlite3.Row) -> Problem:
        return Problem(
            id=r["id"], user_id=r["user_id"], course_id=r["course_id"],
            prompt=r["prompt"], origin=r["origin"], source_id=r["source_id"],
            parent_id=r["parent_id"], number=r["number"], type=r["type"],
            topic=r["topic"], chapter_ref=r["chapter_ref"], difficulty=r["difficulty"],
            given_solution=r["given_solution"], answer=r["answer"],
            solution_steps=r["solution_steps"], verified=bool(r["verified"]),
            verify_method=r["verify_method"], needs_review=bool(r["needs_review"]),
        )

    def problem(self, user_id: str, problem_id: str) -> Optional[Problem]:
        r = self.conn.execute(
            "SELECT * FROM practice_problems WHERE user_id=? AND id=?",
            (user_id, problem_id),
        ).fetchone()
        return self._row_to_problem(r) if r else None

    def problems(self, user_id: str, course_id: Optional[str] = None,
                 origin: Optional[str] = None) -> list[Problem]:
        sql = "SELECT * FROM practice_problems WHERE user_id=?"
        params: list[Any] = [user_id]
        if course_id:
            sql += " AND course_id=?"; params.append(course_id)
        if origin:
            sql += " AND origin=?"; params.append(origin)
        sql += " ORDER BY created_at, number"
        return [self._row_to_problem(r) for r in self.conn.execute(sql, params)]

    @_serialized
    def clear_problems_for_source(self, user_id: str, source_id: str) -> None:
        # Also removes generated variants descended from this source's problems.
        self.conn.execute(
            "DELETE FROM practice_problems WHERE user_id=? AND (source_id=? OR "
            "parent_id IN (SELECT id FROM practice_problems WHERE user_id=? AND source_id=?))",
            (user_id, source_id, user_id, source_id),
        )
        self.conn.commit()

    @_serialized
    def record_attempt(self, user_id: str, problem_id: str, chapter_ref: str,
                       correct: bool, help_level: int) -> None:
        self.conn.execute(
            "INSERT INTO practice_attempts(id, user_id, problem_id, chapter_ref, "
            "correct, help_level, attempted_at) VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, user_id, problem_id, chapter_ref, int(correct),
             help_level, _now()),
        )
        self.conn.commit()

    def attempts(self, user_id: str, problem_ids: Optional[Sequence[str]] = None) -> list[dict]:
        sql = "SELECT problem_id, chapter_ref, correct, help_level, attempted_at " \
              "FROM practice_attempts WHERE user_id=?"
        params: list[Any] = [user_id]
        rows = self.conn.execute(sql, params).fetchall()
        out = [{"problem_id": r["problem_id"], "chapter_ref": r["chapter_ref"],
                "correct": bool(r["correct"]), "help_level": r["help_level"],
                "attempted_at": r["attempted_at"]} for r in rows]
        if problem_ids is not None:
            ids = set(problem_ids)
            out = [a for a in out if a["problem_id"] in ids]
        return out

    @_serialized
    def log_agent_run(
        self,
        user_id: str,
        question: str,
        course_id: Optional[str],
        steps: int,
        tools: Sequence[str],
        refused: bool,
        latency_ms: int,
        citations: Sequence[str],
    ) -> None:
        self.conn.execute(
            "INSERT INTO agent_runs(id, user_id, question, course_id, steps, tools, refused, "
            "latency_ms, citations, ran_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                user_id,
                question,
                course_id,
                steps,
                json.dumps(list(tools)),
                int(refused),
                latency_ms,
                json.dumps(list(citations)),
                _now(),
            ),
        )
        self.conn.commit()

    def agent_runs(self, user_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agent_runs"
        params: list[Any] = []
        if user_id:
            sql += " WHERE user_id=?"
            params.append(user_id)
        sql += " ORDER BY ran_at DESC"
        out = []
        for r in self.conn.execute(sql, params).fetchall():
            d = dict(r)
            d["tools"] = json.loads(d["tools"])
            d["citations"] = json.loads(d["citations"])
            out.append(d)
        return out

    @_serialized
    def record_eval_run(self, label: str, metrics: dict[str, Any], commit_sha: str = "") -> str:
        run_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO eval_runs(id, commit_sha, ran_at, label, metrics) VALUES(?,?,?,?,?)",
            (run_id, commit_sha, _now(), label, json.dumps(metrics)),
        )
        self.conn.commit()
        return run_id

    def eval_runs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM eval_runs ORDER BY ran_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d["metrics"])
            out.append(d)
        return out


_store: Optional[SQLiteStore] = None


def get_store(db_path: Optional[Path] = None) -> SQLiteStore:
    global _store
    if db_path is not None:
        return SQLiteStore(db_path)
    if _store is None:
        from ..config import settings

        _store = SQLiteStore(settings.db_path)
    return _store


def reset_store() -> None:
    global _store
    if _store is not None:
        _store.close()
    _store = None

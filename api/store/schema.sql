-- Course Copilot schema.
--
-- Multi-tenant from the ground up: `user_id` is on every table that holds user
-- content, including `courses` and `sources`, which previously came from
-- committed seed CSVs. Nothing is seeded any more -- a course row exists only
-- because some user uploaded a syllabus or a textbook.
--
-- Shaped for pgvector: embeddings live in dedicated columns. On SQLite a vector
-- is a float32 blob scanned in numpy; on Postgres it becomes `vector(n)` with an
-- ivfflat index and the rest of the schema is unchanged.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS courses (
    course_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    code          TEXT NOT NULL,
    title         TEXT NOT NULL,
    term          TEXT NOT NULL DEFAULT '',
    -- Capability flag, DERIVED at ingest from corpus + syllabus content.
    -- Never configured from a course-code list: codes differ at every school.
    has_data_link INTEGER NOT NULL DEFAULT 0,
    data_link_reason TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, course_id)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    course_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    type       TEXT NOT NULL,
    file       TEXT NOT NULL,
    pages      INTEGER NOT NULL DEFAULT 0,
    -- Ingest outcome, surfaced in the upload UI. An image-only PDF that could
    -- not be OCR'd must never look like a successful upload.
    status     TEXT NOT NULL DEFAULT 'ok',
    status_detail TEXT NOT NULL DEFAULT '',
    ocr_pages  INTEGER NOT NULL DEFAULT 0,
    -- sha256 of the uploaded bytes, so re-uploading the same file to the same
    -- course is recognised and skipped instead of re-extracted and re-embedded.
    file_hash  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, source_id)
);

-- Background ingest jobs. Upload returns a job id immediately; a worker thread
-- runs extraction/embedding and updates per-file progress here, which the UI
-- polls. `files` is JSON: one object per file with stage, pages_done/total,
-- chunks, status, detail and an ETA, so the UI shows "OCR, page 340 of 770"
-- rather than an indeterminate spinner.
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    course_id  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    files      TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON ingest_jobs(user_id, created_at);

-- Chapter titles, embedded. This table is what makes syllabus linking semantic:
-- a syllabus row's topic text is matched against these titles by similarity,
-- never against chapter numbers, because a syllabus citing Varian 10e against a
-- PDF of the 8e has correct titles and wrong numbers.
CREATE TABLE IF NOT EXISTS chapters (
    user_id     TEXT NOT NULL,
    course_id   TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    chapter_num INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    page_start  INTEGER NOT NULL DEFAULT 0,
    page_end    INTEGER NOT NULL DEFAULT 0,
    embedding   BLOB,
    PRIMARY KEY (user_id, source_id, chapter_num)
);

CREATE INDEX IF NOT EXISTS idx_chapters_course ON chapters(user_id, course_id);

CREATE TABLE IF NOT EXISTS parents (
    id        TEXT PRIMARY KEY,
    user_id   TEXT NOT NULL,
    source_id TEXT NOT NULL,
    text      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    course_id     TEXT NOT NULL,
    chapter_num   INTEGER NOT NULL,
    chapter_title TEXT NOT NULL DEFAULT '',
    section       TEXT NOT NULL DEFAULT '',
    page_start    INTEGER NOT NULL DEFAULT 0,
    page_end      INTEGER NOT NULL DEFAULT 0,
    text          TEXT NOT NULL,
    parent_id     TEXT,
    kind          TEXT NOT NULL DEFAULT 'prose',
    token_count   INTEGER NOT NULL DEFAULT 0,
    ordinal       INTEGER NOT NULL DEFAULT 0,
    embedding     BLOB
);

CREATE INDEX IF NOT EXISTS idx_chunks_user    ON chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_course  ON chunks(user_id, course_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chapter ON chunks(user_id, course_id, chapter_num);
-- Benchmark gold labels resolve through this index: (source, page range) -> ids.
CREATE INDEX IF NOT EXISTS idx_chunks_pages   ON chunks(source_id, page_start, page_end);

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    course_id  TEXT,
    created_at TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    tool_calls      TEXT,
    citations       TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    message_id TEXT,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    spec       TEXT NOT NULL
);

-- FRED search results cached per concept query, not per hardcoded series id.
CREATE TABLE IF NOT EXISTS series_search_cache (
    query      TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_cache (
    series_id  TEXT NOT NULL,
    transform  TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (series_id, transform)
);

CREATE TABLE IF NOT EXISTS syllabus_schedule (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    course_id    TEXT NOT NULL,
    date         TEXT,
    kind         TEXT NOT NULL,          -- lecture | due | exam
    topic        TEXT NOT NULL DEFAULT '',
    chapter_refs TEXT NOT NULL DEFAULT '[]',
    readings     TEXT NOT NULL DEFAULT '[]',
    weight       REAL NOT NULL DEFAULT 0,
    confidence   TEXT NOT NULL DEFAULT 'high',
    -- Semantic link provenance, so the review screen can show *why* a row was
    -- flagged and the user can see what it was matched against.
    link_score   REAL NOT NULL DEFAULT 0,
    link_method  TEXT NOT NULL DEFAULT '',
    link_title   TEXT NOT NULL DEFAULT '',
    needs_review INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sched_user_date ON syllabus_schedule(user_id, date);
CREATE INDEX IF NOT EXISTS idx_sched_review    ON syllabus_schedule(user_id, needs_review);

CREATE TABLE IF NOT EXISTS engagement (
    user_id         TEXT NOT NULL,
    chapter_ref     TEXT NOT NULL,
    questions_asked INTEGER NOT NULL DEFAULT 0,
    ps_questions_hit INTEGER NOT NULL DEFAULT 0,
    notes_exported  INTEGER NOT NULL DEFAULT 0,
    last_touched_at TEXT,
    PRIMARY KEY (user_id, chapter_ref)
);

-- Self-reported progress is stored separately and never overwrites the derived
-- signal. The derived bar is what makes the readiness view honest.
CREATE TABLE IF NOT EXISTS manual_progress (
    user_id     TEXT NOT NULL,
    chapter_ref TEXT NOT NULL,
    value       REAL NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, chapter_ref)
);

CREATE TABLE IF NOT EXISTS study_plans (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    assessment_id TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    plan          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id         TEXT PRIMARY KEY,
    commit_sha TEXT NOT NULL DEFAULT '',
    ran_at     TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    metrics    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    question    TEXT NOT NULL,
    course_id   TEXT,
    steps       INTEGER NOT NULL DEFAULT 0,
    tools       TEXT NOT NULL DEFAULT '[]',
    refused     INTEGER NOT NULL DEFAULT 0,
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    citations   TEXT NOT NULL DEFAULT '[]',
    ran_at      TEXT NOT NULL
);

-- Gradable items, first-class rather than rows hidden inside the schedule.
-- Populated by extraction (kind classified, weight captured where stated) and by
-- manual add/edit. `due_date` is nullable: an undated syllabus still yields an
-- item, it simply has no date until the student sets one. `user_entered` marks
-- rows a person added or corrected, distinct from extracted ones.
CREATE TABLE IF NOT EXISTS assessments (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    course_id    TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'homework',
    title        TEXT NOT NULL DEFAULT '',
    due_date     TEXT,                    -- ISO date, or NULL when undated
    weight       REAL NOT NULL DEFAULT 0,
    chapter_refs TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'not_started',  -- not_started|in_progress|done
    user_entered INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'extracted',    -- extracted|manual
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_assess_user_due ON assessments(user_id, due_date);
CREATE INDEX IF NOT EXISTS idx_assess_course   ON assessments(user_id, course_id);

-- Grade Predictor keeps AI proposals, student-approved rules and entered scores
-- separate. Hypothetical what-if values are deliberately never written here.
CREATE TABLE IF NOT EXISTS grade_extractions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    syllabus_hash TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    backend TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_grade_extractions_course
    ON grade_extractions(user_id, course_id, created_at);
CREATE TABLE IF NOT EXISTS grade_rules (
    user_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    confirmed_at TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    syllabus_hash TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(user_id, course_id)
);
CREATE TABLE IF NOT EXISTS grade_syllabus_selections (
    user_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    syllabus_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(user_id, course_id)
);
CREATE TABLE IF NOT EXISTS grade_scores (
    user_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    score REAL NOT NULL CHECK(score >= 0 AND score <= 100),
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(user_id, course_id, item_id)
);

-- Per-user preferences that govern the privacy boundary. `llm_consent` is the
-- explicit opt-in to sending questions to a free-tier model whose inputs train
-- the provider; default 0, so no user's data reaches such a model without it.
-- A user may instead supply their own key (gemini/anthropic), which is not
-- training-eligible and needs no consent. Enforced in code, not by convention.
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id        TEXT PRIMARY KEY,
    llm_consent    INTEGER NOT NULL DEFAULT 0,
    consent_decided INTEGER NOT NULL DEFAULT 0,
    gemini_api_key TEXT NOT NULL DEFAULT '',
    anthropic_api_key TEXT NOT NULL DEFAULT '',
    depth          TEXT NOT NULL DEFAULT 'concise',
    voice_consent  INTEGER NOT NULL DEFAULT 0,
    voice_only_mode INTEGER NOT NULL DEFAULT 0,
    auto_submit_voice INTEGER NOT NULL DEFAULT 1,
    selected_voice_id TEXT NOT NULL DEFAULT '',
    speech_speed REAL NOT NULL DEFAULT 1.0,
    updated_at     TEXT NOT NULL DEFAULT ''
);

-- Practice problems, kept ENTIRELY OUT of the retrieval `chunks` table so an
-- uploaded homework can never be cited as if it were the textbook. `origin`
-- gates the integrity line: an "uploaded" problem may never expose a final
-- solution; a "generated" variant may. `verified` is true only when sympy
-- independently reproduced the answer.
CREATE TABLE IF NOT EXISTS practice_problems (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    course_id     TEXT NOT NULL,
    source_id     TEXT NOT NULL DEFAULT '',   -- the assessment file it came from
    origin        TEXT NOT NULL DEFAULT 'uploaded',   -- uploaded | generated
    parent_id     TEXT NOT NULL DEFAULT '',           -- generated: the source problem
    number        TEXT NOT NULL DEFAULT '',
    prompt        TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'short_answer',
    topic         TEXT NOT NULL DEFAULT '',
    chapter_ref   TEXT NOT NULL DEFAULT '',
    difficulty    TEXT NOT NULL DEFAULT 'standard',
    given_solution TEXT NOT NULL DEFAULT '',
    answer        TEXT NOT NULL DEFAULT '',            -- final answer (generated only)
    solution_steps TEXT NOT NULL DEFAULT '',           -- worked method (generated only)
    verified      INTEGER NOT NULL DEFAULT 0,
    verify_method TEXT NOT NULL DEFAULT '',             -- sympy | unverified
    needs_review  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_problems_course ON practice_problems(user_id, course_id);
CREATE INDEX IF NOT EXISTS idx_problems_chapter ON practice_problems(user_id, chapter_ref);

-- One row per attempt at a problem: correctness and how much help was used, so
-- the dashboard can weight practice engagement and missed problems can resurface.
CREATE TABLE IF NOT EXISTS practice_attempts (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    problem_id   TEXT NOT NULL,
    chapter_ref  TEXT NOT NULL DEFAULT '',
    correct      INTEGER NOT NULL DEFAULT 0,
    help_level   INTEGER NOT NULL DEFAULT 0,   -- 0 none, 1 hint, 2 method, 3 solution
    attempted_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_attempts_user ON practice_attempts(user_id, problem_id);

-- User-created calendar events: recurring class meetings (with time + location),
-- personal deadlines, or anything the syllabus did not capture. Recurring events
-- (weekly on given weekdays until a date) are expanded on read; one-off events
-- have a single date. `done` marks a one-off deadline-like event complete.
CREATE TABLE IF NOT EXISTS calendar_events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    course_id   TEXT NOT NULL DEFAULT '',        -- '' = personal, no course
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'custom',   -- class|deadline|exam|project|custom|holiday
    date        TEXT,                             -- ISO date (start date if recurring)
    start_time  TEXT NOT NULL DEFAULT '',         -- 'HH:MM' or ''
    end_time    TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    recurrence  TEXT NOT NULL DEFAULT 'none',      -- none | weekly
    recur_days  TEXT NOT NULL DEFAULT '',          -- 'MO,WE,FR' weekday codes
    recur_until TEXT,                              -- ISO date the recurrence stops
    done        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_user ON calendar_events(user_id, date);

-- A generic done flag for any calendar item (a syllabus lecture concept, a
-- deadline, an event) keyed by that item's stable id, so checking a task off in
-- the calendar does not require a column on every source table. Assessments keep
-- their own richer status; this covers the rest.
CREATE TABLE IF NOT EXISTS task_status (
    user_id    TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, item_id)
);

-- One stable, unguessable token per user for the read-only .ics calendar feed.
-- The feed regenerates on read, so calendar clients pick up schedule edits.
CREATE TABLE IF NOT EXISTS calendar_feeds (
    user_id    TEXT PRIMARY KEY,
    token      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_feed_token ON calendar_feeds(token);

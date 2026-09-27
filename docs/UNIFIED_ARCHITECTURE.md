# Unified BB Plus + Course Copilot architecture

## Repository audit and ownership

### Course Copilot

| Responsibility | Existing owner |
|---|---|
| Local API and service entry point | `api/main.py` (FastAPI, port 8471) |
| Course/material domain types | `api/models.py` |
| PDF, DOCX, Markdown, and text extraction | `ingest/extract.py`, `api/corpus/ingest_service.py` |
| Parent/child chunking and protected math/table blocks | `ingest/chunk.py` |
| Embeddings and vector persistence | `api/embed/`, `api/store/sqlite_store.py` |
| Course/user-scoped retrieval and refusal | `api/retrieval/` |
| Anthropic, Gemini, and deterministic/local fallback | `api/llm.py`, `api/gemini.py`, `api/config.py` |
| Answer and explanation depth | `api/agent/`, `api/main.py` |
| Benchmark and claim-level groundedness | `eval/`, `eval/benchmark/` |
| Existing standalone Course Copilot client | `web/` (retained for local-only workflows) |

The SQLite store is the canonical source for indexed material, course mappings,
retrieval, provider preferences, and Course Copilot application state. `user_id`
is supplied by the server's `current_user()` seam; clients cannot select another
user by sending a request field.

### BB Plus

| Responsibility | Existing owner |
|---|---|
| Blackboard page integration and network observation | `extension/bridge.js`, `extension/content.js` |
| Course outline parsing, Student View, and Debug View | `extension/content.js`, `extension/styles.css` |
| Authenticated Blackboard file fetch and safe message staging | `extension/lib/stage.js` |
| PDF/DOCX/PPTX/HTML parsing into structured blocks | `extension/offscreen.js`, `extension/lib/ir.js` |
| Extension background orchestration and query surface | `extension/background.js` |
| Browser-profile source documents and figure assets | `extension/lib/db.js` (IndexedDB) |
| Permission prompt and extension enable/disable | `extension/popup.html`, `extension/popup.js`, `extension/manifest.json` |

BB Plus is a Manifest V3 Chrome extension. It has no application server, package
manager, build step, or frontend test runner. Its primary student UI is the
Blackboard-injected drawer; the action popup manages per-site and local-service
permissions. Blackboard session cookies are used only in the Blackboard tab.

## Target responsibilities

```text
Blackboard page
  └─ BB Plus extension (course discovery, parse, structured local library, UI)
       ├─ explicit Blackboard-course ↔ Course Copilot-course mapping
       ├─ structured document sync → Course Copilot ingestion job
       └─ Ask + depth + source citations → Course Copilot API
            └─ SQLite → extraction/chunking → embeddings/retrieval → LLM
```

- The BB Plus drawer is the primary Blackboard-facing shell and design system.
- Course Copilot remains the only implementation of ingestion into the searchable
  corpus, retrieval, refusal, answer generation, explanations, citations, and
  model selection. No JavaScript RAG or second chat backend is added.
- `extension/` contains the complete BB Plus extension source in this repository;
  it is not a submodule and has no independent server or database service.
- The extension IndexedDB and Course Copilot SQLite both remain because they
  store different representations: BB Plus retains full-fidelity parsed blocks
  and content-addressed figure assets in the browser profile; SQLite stores the
  searchable, user/course-scoped chunk index and application state. The bridge
  sends text/math/table structure used by retrieval; the current Course Copilot
  retriever does not index binary image assets.
- A mapping table keyed by `(user_id, blackboard_course_id)` explicitly points to
  an existing Course Copilot `course_id`. Names and filenames are display/source
  metadata only and are never treated as identity or authorization.
- Every bridge route derives `user_id` server-side and verifies that the mapped
  Course Copilot course belongs to that user. It does not accept `user_id` from
  the extension. The current product has a single configured local user and is
  intended to bind to loopback; this merge does not add login/authentication.
- A stable Blackboard material ID is transformed into a deterministic safe
  `bbplus_` filename. The existing SHA-256 ingest cache skips unchanged content;
  changed content refreshes the same source through the normal ingestion path.
- The extension requests access only to `http://127.0.0.1:8471/*` after an
  explicit click in its action popup. No Blackboard credentials, cookies, or
  provider API keys are sent to Course Copilot by the extension.

## Compatibility and migration

The existing Course Copilot SQLite file is not recreated or renamed. The course
mapping table is additive (`CREATE TABLE IF NOT EXISTS`) and the API/store retain
existing upload and Ask contracts. BB Plus's current IndexedDB schema is not
changed. The installed extension starts synchronizing only after the user maps a
Blackboard course to a Course Copilot course and explicitly starts sync.

The bridge serializes headings, paragraphs, lists, tables, code, and source math
to Markdown-like text with page/slide markers. Image blocks contribute captions
when available; their binary assets remain in BB Plus IndexedDB and are not sent
to the current text retrieval API. Unsupported/unparsed content is surfaced as a
per-file warning rather than represented as successfully indexed prose.

## Known boundary

The standalone `web/` client remains available for Course Copilot's broader
local features and ordinary file uploads. Blackboard browsing, course mapping,
BB Plus library status/sync, and course-scoped Ask are surfaced in the BB Plus
drawer. They share the same API and SQLite store; there is no copied database,
separate retrieval pipeline, or second provider implementation.

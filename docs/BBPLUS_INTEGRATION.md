# BB Plus + Course Copilot integration

BB Plus is the Blackboard-facing UI and owns Blackboard discovery, authenticated file access, document parsing, structured browser-local documents, and figure assets. Course Copilot is the canonical local service for searchable course materials, user/course isolation, ingestion, retrieval, refusal, providers, explanations, and citations. This is a Chrome extension plus one local service, not a second backend or a copied RAG implementation.

## Data flow

```text
Blackboard signed-in tab
  → BB Plus course discovery and parser
  → BB Plus IndexedDB (structured blocks and local figure assets)
  → explicit mapping: Blackboard course ID → Course Copilot course ID
  → local BB Plus adapter route
  → existing Course Copilot job and ingest_files pipeline
  → chunks / embeddings / chapters in SQLite
  → mapped-course retrieval and grounded answer
```

The browser IndexedDB and server SQLite remain because they hold different representations. BB Plus preserves full parsed IR and content-addressed assets; SQLite is the canonical retrieval index and application store. No images or Blackboard cookies are sent to the API. Text, LaTeX, tables, code, page/slide locations, and available image captions are sent only after the user selects **Sync BB Plus library**.

## Setup and permissions

1. Start Course Copilot on this computer at `http://127.0.0.1:8471`.
2. Load this repository’s `extension/` directory in Chrome using `chrome://extensions` → Developer mode → **Load unpacked**.
3. From the extension popup, enable BB Plus on the current Blackboard HTTPS origin.
4. Choose **Connect Course Copilot** in that popup. The optional host permission is limited to `http://127.0.0.1:8471/*`.
5. In the drawer, select a Blackboard course. Map it to an existing Course Copilot course or create and map a new course.
6. Use **Sync BB Plus library**, wait for the indexing job to finish, and then Ask a question from that course.

The popup never asks for or stores a provider key. The extension API URL is fixed to loopback and is not configurable by page content. Disconnecting in the popup removes the optional local-server permission.

## API contract

All integration handlers use `current_user()` from the server. The extension sends no `user_id`. Product integration routes reject the reserved `benchmark` user. The Blackboard course ID is treated as an opaque string and is never assumed to equal the Course Copilot ID.

### Read local state

`GET /api/integrations/bbplus/state`

```json
{
  "courses": [{
    "course_id": "econ303",
    "code": "ECON303",
    "title": "Intermediate Microeconomics",
    "term": "Fall",
    "chunks": 12,
    "sources": [{"source_id": "…", "title": "…", "pages": 49, "status": "ok"}]
  }],
  "mappings": [{
    "blackboard_course_id": "opaque-blackboard-id",
    "blackboard_course_name": "Intermediate Microeconomics",
    "course_id": "econ303"
  }]
}
```

### Map an existing course

`PUT /api/integrations/bbplus/course-mappings/{blackboard_course_id}`

```json
{"course_id": "econ303", "course_name": "Intermediate Microeconomics"}
```

The server returns 404 if that course is not owned by the current user. The mapping is stored by `(user_id, blackboard_course_id)` and removed when its Course Copilot course is fully deleted. Materials-only and syllabus-only cleanup retain the map.

### Create and map a course

`POST /api/integrations/bbplus/course-mappings/{blackboard_course_id}/create`

```json
{"code": "ECON303", "title": "Intermediate Microeconomics", "term": "Fall"}
```

The server creates the Course Copilot course under the configured user and maps the Blackboard ID to it.

### Sync structured documents

`POST /api/integrations/bbplus/course-mappings/{blackboard_course_id}/materials`

```json
{
  "documents": [{
    "item_id": "stable-blackboard-item-id",
    "course_id": "opaque-blackboard-id",
    "title": "Budget Line",
    "source_type": "pdf",
    "blocks": [
      {"type": "heading", "level": 2, "text": "Budget constraint", "page": 49},
      {"type": "paragraph", "text": "…", "page": 49},
      {"type": "math", "latex": "p_x x + p_y y = m", "page": 50},
      {"type": "table", "rows": [["Item", "Value"]]}
    ]
  }]
}
```

Supported blocks retain their hierarchy/readable content, including tables, equations, code, captions, and page/slide markers. Image pixels and unparsed blocks are not represented as searchable prose. If an item has no readable text, table, equation, code, or caption, the route returns 422 with code `unreadable_material` and a human-readable reason. A document course ID that differs from the route course ID returns 409. The batch limit is 100 documents and 24 MB serialized text; a single document is limited to 2 MB of text.

The returned `job_id` is polled through the normal `GET /api/jobs/{job_id}` contract. Job lookup remains scoped to the current server user. Job files expose queued/running/done/failed status, extraction progress, chunk counts, and file-level errors. A terminal `done` job may still contain an individual unsupported/failed file, so inspect the `files` rows.

The adapter generates a stable filename from a hash of the Blackboard item ID. The display title stays in the document content, so a title change does not change the source identity. Filenames are metadata only. The existing ingest service computes a SHA-256 hash of serialized content: an unchanged document for that stable item is reused, and changed content replaces that item's BB Plus source and chunks. Distinct Blackboard items remain distinct sources even if their content matches. Sources retain type `bbplus` and are covered by the existing Materials-only cleanup.

### Ask the mapped course

`POST /api/integrations/bbplus/course-mappings/{blackboard_course_id}/ask`

```json
{"question": "What determines the slope of the budget line?", "depth": "concise"}
```

`depth` is `concise` or `in_depth`. The adapter resolves the map for the current user and invokes the existing `Agent` with the Course Copilot course ID. It returns the same fields as `/api/ask`: answer, explanation, provider/backend label, refusal state, and source/chapter/page citations.

## Persistence and migration

`bbplus_course_mappings` is added by the normal idempotent schema initialization. Existing databases are not deleted, reseeded, or renamed. Course IDs, source IDs, and chunk IDs are unchanged. The only additional persistent relationship is the user-scoped Blackboard mapping. On full-course deletion, the generic user-and-course scoped deletion also removes these mappings in the same SQLite transaction.

Each Course Copilot chunk continues to be scoped to `user_id`, `course_id`, and its stable chunk ID. Existing per-user vector caches are invalidated by normal ingestion and deletion hooks. No integration request can set its own tenant ID or map to another user’s course.

## Security boundary and limits

- The API has no login. `default_user_id` is the local product user; keep Uvicorn bound to loopback and do not expose port 8471 to a network.
- CORS allows the local app and Chrome extension origins; browser requests with an unrelated `Origin` cannot mutate the API. This is a local development protection, not network authentication.
- Chrome asks for Blackboard site permission and Course Copilot loopback permission separately. The latter can be revoked in the extension popup.
- `.env`, provider API keys, cookies, and Blackboard credentials remain outside extension messages and storage.
- Re-indexing is Course Copilot’s canonical pipeline. The existing content-hash cache avoids repeat extraction/embedding for unchanged serialized content while its source remains indexed.
- BB Plus retains complete structural IR and local image assets; Course Copilot currently retrieves text and captions only. Vision retrieval would need a separately designed backend contract.

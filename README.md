# BB Plus + Course Copilot

A local study application for Blackboard. **BB Plus is the Blackboard-facing product shell**: it discovers courses, reads course content through the signed-in Blackboard tab, and keeps a structured study library in the browser. **Course Copilot is the intelligence and persistence layer**: it indexes material, isolates each user and course, retrieves evidence, answers questions, explains answers, and runs the schedule, practice, and grade tools.

The Blackboard drawer is the primary workflow. The local Course Copilot page remains available for course setup, ordinary file uploads, schedules, practice, grades, voice settings, and course cleanup. Both clients use the same API and SQLite data.

## Start locally

```bash
make venv
cp .env.example .env  # optional; add provider keys only if desired
make serve
```

Open [http://127.0.0.1:8471](http://127.0.0.1:8471). The server is intended to stay on this computer. The app works without API keys using its deterministic grounded fallback. Configure Gemini or Anthropic in `.env` to enable generated explanations; follow the app’s existing consent and provider settings.

To add BB Plus in Chrome:

1. Open `chrome://extensions`, turn on **Developer mode**, and choose **Load unpacked**.
2. Select this repository’s `extension/` directory.
3. Open Blackboard over HTTPS and use the BB Plus extension popup to enable it for that Blackboard site.
4. In the same popup, choose **Connect Course Copilot**. This asks for access only to `http://127.0.0.1:8471/*`.
5. Open the BB Plus drawer, select a course, map it to a Course Copilot course (or create one), then sync its BB Plus library. Wait for indexing to finish before asking questions.

The extension uses the Blackboard tab’s existing session to discover and parse course materials. It does not receive Blackboard cookies or provider API keys. Course material text is sent only to the local Course Copilot server when the user chooses **Sync BB Plus library**. Later AI requests follow Course Copilot’s configured provider and consent rules.

To set the ElevenLabs key without putting it in shell history or printing it, run `./.venv/bin/python scripts/configure_elevenlabs.py` from the project root. `.env` and local databases are ignored by Git; `.env.example` contains placeholders only.

## What the product does

- **Blackboard study library:** course discovery, outline browsing, authenticated file retrieval, PDF/DOCX/PPTX/HTML parsing, structured text/math/table blocks, and browser-local figure assets.
- **Course Copilot Ask:** course-scoped hybrid retrieval, refusal when evidence is insufficient, concise or in-depth answers, a separate grounded explanation, and source/chapter/page citations.
- **Materials:** standard PDF/DOCX/Markdown/text uploads and BB Plus sync use one extraction, chunking, embedding, deduplication, and indexing pipeline. BB Plus sources retain `bbplus` identity and are included in materials-only cleanup.
- **Schedule and course tools:** syllabus analysis and review, schedule/calendar, practice and assessment handling, and syllabus-based grade prediction remain in the standalone page.
- **Voice:** optional ElevenLabs speech-to-text and text-to-speech remain server-side and require the existing explicit consent.

## Architecture

```text
Blackboard tab
  └─ BB Plus extension (course discovery, parsing, structured local library, drawer UI)
       └─ explicit Blackboard course ID → Course Copilot course ID mapping
            └─ local FastAPI API (current_user seam; no client-supplied user ID)
                 ├─ SQLite: canonical courses, sources, chunks, embeddings, and app state
                 ├─ extraction → chunking → embeddings → hybrid retrieval/refusal
                 └─ Gemini / Anthropic / deterministic fallback → answer + explanation
```

Codebase 2 is a Manifest V3 Chrome extension, not a separate web server. Its IndexedDB and Course Copilot’s SQLite have different jobs: BB Plus keeps the browser’s structured parsed documents and content-addressed images; SQLite is the canonical searchable course index and product store. The retriever currently indexes text, math, tables, code, and image captions, but not binary image assets.

Course mappings are stored in SQLite by `(user_id, blackboard_course_id)`. Blackboard names and filenames are display/source metadata, never identity or authorization. The mapping is additive and does not recreate or rename the existing database. See [the unified architecture](docs/UNIFIED_ARCHITECTURE.md) and [the BB Plus integration contract](docs/BBPLUS_INTEGRATION.md).

Key modules:

- `extension/content.js`, `extension/background.js`, `extension/offscreen.js`, `extension/lib/`: Blackboard integration, drawer, parsing, and browser-local library.
- `api/main.py`, `api/integrations/bbplus.py`: local API and structured BB Plus adapter.
- `api/corpus/`, `ingest/`: extraction, chunking, deduplication, and indexing.
- `api/retrieval/`, `api/agent/`, `api/llm.py`, `api/gemini.py`: scoped retrieval, refusal, provider abstraction, answer and explanation generation.
- `api/store/sqlite_store.py`, `api/store/schema.sql`: canonical persistence and additive schema.
- `eval/`, `tests/`: benchmark, claim-level groundedness harness, and regression tests.
- `web/`: full local product page, restyled to use BB Plus’s white/neutral surfaces, system sans-serif type, blue action color, and compact controls.

## Data isolation and safety

Every material read, write, cleanup, and retrieval operation uses the server-derived `user_id`; clients never choose it. Chunk access is further restricted by course. Stable chunk IDs and the existing source/user scope are unchanged. The `benchmark` user is read-only through BB Plus integration and product deletion paths. Existing deletion remains transactional and invalidates the in-memory vector cache for the affected user.

The API has no login yet and assigns requests to the configured `default_user_id` (normally `local-user`). Keep it bound to loopback. Browser CORS is limited to the local app and Chrome extension origins; cross-site browser writes are rejected. This protects the local development workflow from ordinary web-page requests, but it is not a substitute for authentication if the server is ever exposed beyond this computer.

Never commit `.env`, uploaded materials, SQLite databases, embedding caches, benchmark output containing generated text, or build artifacts. Confirm with `git status --short` before committing.

## Configuration and providers

Copy `.env.example` to `.env` as needed. All keys are optional:

- `GEMINI_API_KEY` or `ANTHROPIC_API_KEY`: generated grounded explanations and provider-backed agent behavior.
- `OPENAI_API_KEY`: OpenAI embeddings when configured; otherwise the local embedder is available.
- `FRED_API_KEY`: real-world data; without it, data is synthetic and labeled.
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`: optional voice features.
- `COPILOT_OFFLINE=1`: force offline deterministic providers. `make serve` sets `COPILOT_OFFLINE=0` so configured providers can be used.

Provider-specific logic stays behind Course Copilot’s current abstraction. The extension receives only the generated answer and citations; it never receives API keys.

## BB Plus sync flow

1. BB Plus reads Blackboard course records from the authenticated page and the user selects or creates the Course Copilot target.
2. The extension stores the explicit map on the local server. The server checks that the target course belongs to its current user.
3. On explicit sync, the background service worker reads structured BB Plus documents and sends their blocks to the mapped route. Binary assets and Blackboard credentials are not sent.
4. The adapter turns headings, paragraphs, lists, tables, math, code, captions, and page/slide markers into deterministic `bbplus_` Markdown files. Unreadable documents produce a machine-readable error rather than a false success.
5. The existing ingest worker extracts, chunks, embeds, indexes, and returns a job ID. The drawer polls the normal user-scoped job endpoint and shows file status.
6. Ask sends the Blackboard course ID to a server adapter. It resolves the mapping, calls the existing Agent with that Course Copilot course ID, and returns the normal answer, explanation, backend label, refusal state, and citations.

The file name uses a hash of the stable Blackboard item ID; the readable display title stays inside the document. Content hashing skips unchanged bytes for that stable item; changed content reuses its source identity and refreshes its chunks. Distinct Blackboard items retain distinct sources even when their content matches. The API limits a sync to 100 documents and 24 MB of serialized text per request. Blackboard items beyond those limits are reported rather than silently truncated.

## Run checks

```bash
make test
node --check extension/background.js
node --check extension/content.js
node --check extension/popup.js
python3 -m json.tool extension/manifest.json
```

`make test` runs the full backend test suite with deterministic local embeddings. The tests include BB Plus mapping, structured sync, polling, retrieval, repeat-sync idempotency, course/user boundaries, benchmark protections, deletion scopes, and rollback behavior.

Retrieval evaluation:

```bash
make eval-fast
```

The full explanation-groundedness harness is separate from `make test` and uses Gemini generation plus claim judging:

```bash
COPILOT_OFFLINE=0 ./.venv/bin/python -m eval.run_explanation_grounding \
  --json eval/explanation_grounding.json
```

That harness sends benchmark passages and generated explanation text to the configured Gemini service. Run it only when that external processing is approved and the key is configured; do not treat offline unit tests as a Gemini benchmark result.

## Known limits

- Blackboard access requires Chrome, an HTTPS Blackboard site, and a user-granted site permission.
- Course Copilot runs locally and currently has a single configured product user, not login or multi-user authentication.
- BB Plus preserves binary images locally, but the Course Copilot retrieval index currently receives only their captions, not image pixels.
- A BB Plus sync over 100 documents or 24 MB of extracted text must be divided into smaller requests.
- Groundedness evaluation requires a configured Gemini key and provider access; unit-test success does not establish a live Gemini result.
- BB Plus standalone library parsing and Course Copilot’s searchable index are separate representations, not competing retrieval systems.

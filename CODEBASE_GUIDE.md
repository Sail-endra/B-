# B+ — Codebase Guide

Everything a new contributor (human or agent) needs to be productive here: what
the product is, where it's going, how it's built, and the non-obvious rules and
quirks that will bite you if you don't know them. Read this before making
changes. Pair it with `AGENTS.md` (the UI/UX freeze rule).

> Naming note: the product is **B+** (short for **Blackboard Plus**). The
> in-course AI assistant is **AI Lookup Chat**. Older names — "Bb+", "BB Plus",
> "Course Copilot", "Study Hub" — are retired from all user-facing text. They
> still survive as **internal code identifiers** (`courseCopilotFetch`,
> `COURSE_COPILOT_ORIGIN`, `BBX_CP_*` messages, `/api/integrations/bbplus/…`
> routes, the `bbplus` source type). Those are wire/contract names — leave them;
> renaming them is churn with breakage risk and no user benefit.

---

## 1. What B+ is

A Chrome (MV3) extension that layers a study assistant on top of a student's
real **Blackboard Ultra** site. It:

1. discovers the student's current courses from Blackboard's own network traffic;
2. retrieves each course's files (PDFs, slides, docs, pages);
3. builds a searchable, per-course library;
4. answers questions about a course **grounded in that course's materials**, with
   clickable citations back to the source file on Blackboard;
5. falls back to general knowledge, clearly labelled, when the materials don't
   cover a question.

Today it has two halves:

- **The extension** (`extension/`) — the UI the student sees, plus all Blackboard
  scraping and the auto-setup lifecycle. Runs in the browser.
- **The backend** (`api/`) — a local FastAPI server at **`http://127.0.0.1:8471`**
  that does the heavy lifting: file extraction (PyMuPDF + OCR), chunking,
  embeddings, hybrid retrieval, and LLM answer synthesis. It also serves a
  standalone web app (`web/`) with the same features in a browser tab.

## 2. Where it's going — the north star

**We are deprecating the website (`127.0.0.1:8471`) side of things.** The plan is
to make the backend's functions **native to the sidebar**, so the whole product
becomes the extension plus, at most, *a small bit of code running alongside it* —
no separate web app to open, ideally no separate server process to babysit.

Concretely, that means over time migrating what currently lives in `api/` into
the extension (or a minimal local helper the extension manages), and retiring the
`web/` standalone UI. **When you add or change a feature, prefer designs that make
this migration easier**: keep the extension↔backend contract narrow and explicit,
avoid deepening the web app, and treat `web/` as legacy. The end state a student
experiences: install B+, open Blackboard, everything just works in the sidebar.

Until then, both halves coexist and the contract between them is the
`BBX_CP_*` messages (extension→background→backend) and the
`/api/integrations/bbplus/…` HTTP routes.

## 3. Repository map

```
api/                FastAPI backend (the "server" we intend to fold into the extension)
  main.py           All HTTP endpoints; app wiring; ingest job runner
  agent/            Answer generation
    loop.py         Agent: benchmark path (ask/ask_async) + product path (answer_product)
    product_answer.py  Product-only: file router, synthesis, general fallback, overview
    explain.py      Cited "explanation" gloss (web/default path)
    tools.py        search_textbook (+ FRED data tools, benchmark)
  retrieval/        Hybrid retrieval: dense + sparse → RRF → rerank → refusal gate
    pipeline.py     RetrievalPipeline.search() — the one entry point
  corpus/
    ingest_service.py  extract → chunk → embed → store (ingest_files)
  embed/            Embedding backends (BGE default, TF-IDF for tests, OpenAI optional)
  store/            SQLiteStore (pgvector-shaped) + schema.sql
  integrations/bbplus.py  serialize_document, safe_material_filename
  llm.py            LLM wrappers: Anthropic, Gemini, Ollama, extractive stub; resolve_product_llm
  gemini.py         Free-tier Gemini client (cached to disk)
  calendar_view.py, grade_predictor.py, practice/, dashboard/, voice/  feature areas
ingest/
  extract.py        PDF (PyMuPDF)/DOCX/markdown extraction with a heading detector
  ocr.py            OCR for image-only pages (needs a `tesseract` binary)
extension/          The Chrome MV3 extension (the future home of everything)
  manifest.json     MV3 manifest (name "B+")
  content.js        THE big file (~5.7k lines): sidebar UI, Blackboard scraping,
                    auto-prep lifecycle, answer rendering, citations, math
  background.js     Service worker: BBX_* message router, backend fetch, staging
  offscreen.js      Offscreen doc: pdf.js/mammoth/pptx parsing to IR blocks
  bridge.js         MAIN-world hook that forwards Blackboard's JSON to content.js
  lib/              stage.js (download/stage/upload), work.js (mapLimit/matchCourse/
                    queryRelevance), audit.js, db.js (IndexedDB), ir.js
  vendor/           pdf.js, mammoth, jszip, katex.min.js (math)
  popup.html/js     The toolbar popup (enable site, connect backend)
  styles.css        All sidebar styling
web/                Legacy standalone web app (index.html + js/app.js). Being retired.
eval/               Benchmark harness (grounding, refusal, retrieval ablations)
tests/              pytest suites + one Node test (extension_work.test.cjs)
data/               SQLite DB (copilot.db), uploads/, gemini_cache/
docs/               EVAL.md, RESULTS.md, design notes
```

## 4. How to run & test

```bash
make serve      # backend at 127.0.0.1:8471 (uvicorn --reload)
make test       # COPILOT_EMBEDDER=tfidf python -m pytest tests -q
node tests/extension_work.test.cjs   # the extension's pure-JS unit tests
make eval / eval-fast / grounding    # benchmark runs (see eval/ and docs/)
```

Extension: load `extension/` as an unpacked extension in Chrome. **After changing
any content-script file (`content.js`, `lib/*`, `vendor/*`, `styles.css`) you must
reload the extension** — dynamically-registered content scripts pin their file
list at registration time (see `background.js registerOrigin`), and a new file
(e.g. `vendor/katex.min.js`) only loads after a reload/`onInstalled`.

**Verifying UI without real Blackboard:** the live Blackboard scraping/download
path can't be reached from CI or a harness. The pattern used throughout this repo
is a throwaway `extension/__harness__.html` that stubs `chrome.*`, `BBStage`,
`BBAudit`, injects a fake course via a `NETWORK_JSON` postMessage, and loads the
real `content.js`. Serve it with `python -m http.server` and drive it in a
browser. Always delete the harness and stop the server afterwards. Never use the
user's real dev port 8471 for scratch servers; use 8480/8492.

## 5. The extension: auto-setup lifecycle

The student configures **nothing**. On a Blackboard page:

1. `content.js start()` → `ensureUi()` builds the drawer; a `MutationObserver`,
   `popstate`/`hashchange`, and Blackboard's own course-list network traffic
   (captured via `bridge.js` → `NETWORK_JSON` messages) feed course discovery.
2. `prepareAllCourses()` (the single authoritative lifecycle) runs for **every**
   current course, active/visible course first, the rest in the background. Per
   course: `ensureCourseMapping` (create/link a backend course, persist mapping)
   → `probeCourseData` (discover the course's file outline) →
   `startActiveCoursePreparation` (download → extract → chunk → embed → store).
3. The sidebar shows "Compiling files…" per class and "Preparing your courses…"
   overall, flipping to "Ready to go".
4. It is **idempotent**: mapping is cached, probes are guarded, and preparation
   skips a course whose material *signature* is unchanged; the backend also skips
   files whose content hash is unchanged (status `"reused"`). Reloading Blackboard
   or the sidebar does not duplicate work.

Developer diagnostics (a Debug toggle, "Build study library"/"Verify library",
raw-JSON tabs) are hidden unless the page URL has **`?bbxdev=1`** (nothing
persisted; see the `DEV_MODE` constant).

## 6. Ingestion & extraction (files → searchable chunks)

Two extraction paths exist; **PDFs prefer the backend**:

- **Backend (preferred, stronger):** `POST …/materials/files` takes raw file
  bytes (base64), runs `ingest/extract.py` — **PyMuPDF text + OCR** for
  image-only/handwritten pages (`ingest/ocr.py`) — then chunks and embeds.
  `content.js processBatch` downloads PDFs (`BBStage.fetchRaw`), checks the `%PDF`
  magic, and uploads them **batched** into one job. **On any failure it falls
  through to the offscreen path** — additive, never removes coverage.
- **Offscreen (fallback, weaker):** `offscreen.js` parses PDF text layers
  (pdf.js), DOCX (mammoth), and PPTX (`<a:t>` runs) into IR blocks, which are
  serialized to a `.md` and sent via `BBX_CP_SYNC_COURSE` → `/materials`.

Idempotency is by **content hash** (`source.file_hash`, `source_with_hash`). The
Blackboard display title is carried through via `file_titles` so a citation reads
"Week 2.1 Slides.pdf", not the hashed filename.

**OCR caveat:** OCR needs a `tesseract` binary. It is **absent on this dev Mac**,
so OCR only activates where tesseract is installed (deployment). The PyMuPDF text
path improves extraction everywhere regardless.

## 7. Retrieval & answering

There are **two answer paths**, and the split is load-bearing:

- **Benchmark path** — `Agent.ask` / `ask_async` (`agent/loop.py`). Strictly
  grounded: answers only from the corpus, else refuses with `NOT_IN_MATERIALS`.
  **The eval harness measures this.** Do not add fallbacks, web knowledge, or
  anything that changes its groundedness/refusal numbers.
- **Product path** — `Agent.answer_product(..., numbered=True)`, used by the
  sidebar (bbplus ask endpoint) and the web `/api/ask`. This is where the good
  UX lives:
  1. **Overview questions** ("what do I need to know about this course?") →
     synthesised from the course's real structure (chapter titles, schedule,
     assessments), not a passage lookup.
  2. **LLM file router** (`product_answer.route_files`) — the model is shown the
     **whole file catalogue** (title + type + one-line synopsis) and picks the
     relevant files. This is what surfaces the *syllabus* for policy questions and
     terse *slide decks* that embedding+rerank buries. Router picks lead;
     embedding-ranked files backfill; capped at 8 sources.
  3. **Single clean synthesis** (`product_answer.synthesize`) over numbered
     sources → one Markdown answer with `[n]` citations by source. The model
     decides grounded-vs-general itself and is told to ignore layout noise
     (slide/page/figure markers; `clean_snippet` also pre-strips them). No raw
     extractive quote-stitching.
  4. `finalize_citations` renumbers used `[n]` to gap-free `1..M`;
     `AgentAnswer.citations[i]` is the source cited as `[i+1]`.

The retrieval pipeline (`retrieval/pipeline.py`): dense (BGE/pgvector-style) +
sparse (BM25) → reciprocal-rank fusion → cross-encoder rerank → calibrated
**refusal gate** (`refusal_threshold`) + an intent gate. The product path calls
`search(top_k=18)` for a wider, source-diverse pool; the benchmark keeps the
default. **Do not retune the pipeline for the product in ways that move the
benchmark** — widen/select on the product side instead.

## 8. Answer rendering (extension)

`content.js renderAnswer` / `makeCitationContext`:

- **Markdown → DOM** with a small safe renderer (`renderMarkdownInto`): headings,
  bold/italic, code, lists, blockquotes, links, and math. Text is written with
  `textContent`; only `http(s)` links become anchors — nothing the model returns
  is interpreted as HTML.
- **Math** via **KaTeX → native MathML** (`renderTex`): Chrome renders `<math>`
  itself, so **no KaTeX CSS or fonts are shipped**. Falls back to a Unicode
  approximation (`formatMath`) if KaTeX is missing or the expression won't parse.
- **Citations**: in-text `[n]` become clickable superscripts; a numbered
  "Sources" list shows only cited files. Both link to the file's **Blackboard
  page**, resolved by recomputing `sha256(itemId)[:16]` (which the backend embeds
  in the source id) and matching it against the course outline (title fallback;
  local `.../sources/{id}/file` as last resort).

## 9. Privacy & the LLM boundary

`resolve_product_llm(prefs)` (`llm.py`) picks the product answerer per user:
own Anthropic/Gemini key → local Ollama → server Anthropic key → consented
free-tier Gemini → extractive stub. Free-tier Gemini is **only** used with
explicit `llm_consent`. The benchmark path uses `get_llm(allow_gemini=True)`
explicitly and never touches product/cohort data. Multi-tenant isolation is
absolute: **every** store query is scoped by `user_id`; the `benchmark` user is
unreachable from product endpoints (`_require_non_benchmark_product_user`).

## 10. Performance notes

- The BGE embedding model has a **~7s one-time cold load**; each file after that
  is ~0.2–0.3s. It's cached per process (`_BGE_INSTANCES`) and **warmed at
  startup** (`@app.on_event("startup")`, skipped when offline).
- `_material_ingest_semaphore` is **1** — ingestion is serialized (BGE encode
  isn't thread-safe). Don't raise it blindly.
- Files are uploaded **batched** into one ingest job; the extension polls jobs
  with an adaptive `300→1500ms` backoff.

## 11. Rules, constraints & gotchas (read this list)

- **UI/UX freeze** (`AGENTS.md`): don't change the app's visual design/layout
  unless explicitly asked. Rebrands and requested UI work are the exception.
- **Never move benchmark numbers.** The eval measures the grounded `ask` path and
  the retrieval pipeline. Product improvements go through `answer_product` /
  product-only `top_k`, never by editing the pipeline defaults or the refusal gate.
- **Multi-tenancy is sacred.** A past bug collapsed the benchmark corpus (2,183 →
  944 chunks) because a delete matched by `source_id` across users. Every
  destructive or read query is scoped by `user_id`. There are deletion tests that
  assert cross-user/cross-course isolation — keep them green.
- **Two answer paths, one grounding contract.** See §7. Keep them separate.
- **Internal names vs product name.** See the naming note at the top. Don't rename
  `bbplus`/`BBX_CP`/`courseCopilot*` identifiers.
- **Reload the extension** after content-script changes (§4).
- **Don't use port 8471 for scratch servers** — it's the user's dev server.
- **`canonicalize_math`** (in `to_dict`) rewrites `letterDigit`→`letter_{digit}`
  inside math and can mangle stray tokens; the product path avoids embedding raw
  ids in answers precisely to dodge this.
- **OCR needs tesseract** (absent locally) — see §6.
- `content.js` is one ~5.7k-line IIFE. Helper functions are module-scoped and
  hoisted, so placement is flexible; grep for the function name.

## 12. Testing map

- `tests/test_product_answer.py` — the product path: router, synthesis, general
  fallback, overview, citation numbering, `clean_snippet`, wider source pool.
- `tests/test_bbplus_autosetup.py` — endpoints: idempotent create/map, no-dup
  re-sync, course isolation, cited-source file serving, server-side file
  extraction, `source_id` hash derivation.
- `tests/test_delete.py` — the 7 scoped-deletion invariants (isolation, no
  orphans, transactional rollback).
- `tests/test_bbplus_integration.py`, `test_bbplus_bridge.py`,
  `test_extension_work.py` + `tests/extension_work.test.cjs`,
  `test_calendar.py`, `test_grade_predictor.py`, `test_practice_workflow.py`,
  `test_multiuser.py`, `test_ingest_performance.py`, … — feature + safety suites.

Full suite is ~200 tests and should stay green (`make test`). The extension JS has
no full DOM test runner; verify UI behaviour with a harness (§4) and syntax-check
with `node --check`.

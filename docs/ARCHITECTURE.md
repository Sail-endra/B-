# Architecture

## Shape

```
POST /api/courses/{id}/materials   ingest/extract.py   text/PDF/DOCX + provenance
             |                           |
             +---------------------- ingest/chunk.py   parent/child, protected blocks
                                         |
                                    api/embed/*         BGE/OpenAI/TF-IDF
                                         |
                                    api/store           local SQLite
                              |
     ┌────────────────────────┴────────────────────────┐
     │                                                 │
api/retrieval                                    api/dashboard
  dense + sparse -> RRF -> rerank -> refusal        schedule x engagement
     │                                                 │
api/agent/loop.py  ── tools ──> api/data/*         api/main.py (FastAPI)
                                 fred, transforms,       │
                                 divergence            web/ (static SPA)
```

## Decisions worth defending

### SQLite, shaped for pgvector

Vectors are float32 blobs scanned with numpy. At this scale a full cosine scan is
single-digit milliseconds — comfortably inside the latency budget — and it avoids
an index that would need rebuilding on every ingest.

The schema is written as Postgres would want it. `user_id` is on every user-scoped
table from the first commit, because that column is the expensive thing to
retrofit across eight tables; the auth *flow* is not. Migrating means changing
`embedding BLOB` to `vector(1536)`, adding an ivfflat index, and swapping the
driver in `api/store/`. `api/retrieval/dense.py` is the only other file that
touches vectors directly.

### One canonical chapter reference

`{course_id}:{source_id}:{chapter_num}`, defined in `api/ids.py`.

This string is the join key between `syllabus_schedule`, `engagement` and
`chunks`. The original plan specified it two different ways in two sections, and
neither form disambiguates a course whose textbook *and* slides both have a
chapter 11 — which ECON 303 does here. Partial references (a syllabus that says
only "Ch. 14") resolve against the course's primary source; genuine ambiguity
raises rather than guesses, because a wrong binding silently corrupts every
engagement and readiness number downstream.

### Parent/child chunking with protected blocks

400-token children are indexed for precision; each points at a ~1200-token parent
that supplies display context. Equations, tables and numbered definitions are
detected and never split — a half-equation retrieves as noise and reads to a
student as a bug.

The contextual prefix `[COURSE / Chapter n: Title / Section]` is prepended to the
*embedded* text only, never to what is shown.

A subtlety that caused a real defect: a parent can span several pages, so a
sentence quoted from parent text cannot carry the child's citation. The
`search_textbook` tool returns `snippet` (the child, whose page range the citation
describes) separately from `context` (the parent), and answers are composed from
the snippet.

### Retrieval: RRF, then rerank for *confidence* rather than for order

```
query ├── dense  (cosine, top 50)
      └── sparse (BM25, top 50)
              └── RRF k=60 → top 30 → rerank → top 6 + confidence
```

RRF is preferred over a weighted score blend because cosine and BM25 scores are on
incomparable scales; normalising them needs a per-corpus tuning constant, and RRF
reads only rank order.

The reranker defaults to `confidence` mode — fused order kept, reranker supplying
only the calibrated score. That is not the textbook design, and it is there
because the ablation said so: with the lexical backend, reordering *lowers* r@1.
But reranking cannot simply be dropped, because RRF scores are a function of list
length rather than relevance and so cannot drive a refusal threshold. With a real
cross-encoder configured, `reorder` should win; `make eval-ablate` reports both.

### Refusal = relevance AND intent

Calibrating on relevance alone has a ceiling that the eval harness found: a
cluster of out-of-corpus questions score as high as genuine ones because their
topic is in the corpus even though their answer is not.

```
"Who is the current chair of the Federal Reserve?"   0.335
"Translate the Phillips curve equation into French."  0.319
"Write the solution to problem set 4 question 3."     0.311
```

No threshold separates those — the passage returned really is about the Federal
Reserve. `api/retrieval/intent.py` scores what the question asks the system to
*do*, and composes with relevance. Keyword gates, not a model call: cheap,
deterministic, inspectable, and every pattern is justified by a question in the
eval set.

### Agent loop

Four tools, not five. `render_diagram` is absent: the plan cut diagrams and then
left the tool in the spec.

Budgets are enforced from the first commit (`api/agent/budget.py`): 6 steps,
12k tool tokens, 25s wall clock. A timeout renders what the agent had, with the
steps shown, rather than a blank error. Independent tool calls in one turn run
concurrently via `asyncio.gather`.

Two grounding rules bind: assertions require a citation, and FRED numbers are
labelled as data and never attributed to the textbook. The `no_data_link` guard
refuses the data tools for a course that has none — which is what makes "degrades
gracefully on humanities courses" a property rather than a claim.

### Divergence engine

Three guards, in `api/data/divergence.py`:

1. **Conditions are honoured.** Textbook macro claims are nearly always
   conditional — short run, expectations fixed, absent supply shocks. An extractor
   that drops them manufactures false divergences. When a claim carries untestable
   conditions the comparison is withheld and that is what the card says.
2. **Transforms are stated.** CPI is never correlated as a raw level and called
   inflation. Each series' default transform lives in `seed/fred_series.csv`.
3. **No structural break test.** A Chow test needs a break date chosen
   independently of the data; picking 2020 by eye invalidates the p-value. What is
   reported is a descriptive sub-period comparison, labelled as such.

Synthetic FRED data (no key configured) is stamped `synthetic=True`, the flag
propagates through every transform, and every card built on it carries a hard
warning. Fabricated data presented as a finding about the world is the one failure
mode here that would genuinely mislead a student.

## Offline mode

Every provider has a deterministic offline backend, so ingest, retrieval and the
whole eval harness run with no network and no key. `settings.describe_backends()`
is recorded into every eval report, because a metric without its backend is not a
measurement.

The offline answerer is *extractive* — it selects and quotes retrieved sentences.
That makes groundedness structurally 1.000 under it, which the report says in
bold rather than presenting as an achievement.

## What is not built

- **Auth.** Schema is ready (`user_id` everywhere); no login flow.
- **Supabase/pgvector.** SQLite only; migration path above.
- **Google Calendar.** Deliberately cut — OAuth verification for an unverified app
  with external users takes weeks, and `syllabus_schedule` carries the dashboard.
- **Obsidian export, chunk-summary second vector.** Cut for time; neither is
  load-bearing.
- **Deployment.** Runs locally on :8471.

# Course Copilot

Upload a syllabus and your textbooks; get a chatbot scoped to those textbooks and
a dashboard built from that syllabus. Nothing about any particular course is
configured in the repo -- courses are created at upload time and scoped to a user.

Three tabs. **Ask** — grounded question answering over the materials *you*
uploaded, for the course you selected. **Schedule** — week view, upcoming work
and exam readiness, built from your syllabus. **Practice** — assignments kept
outside textbook search, parsed into problems, with verified variants and staged
help.

FRED is not a tab. It is an evidence card inside Ask, shown only for a course
whose uploaded corpus actually has a real-world data link -- a capability derived
from the content, never from a course code.

## Quick start

```bash
make venv
if [ ! -f .env ]; then cp .env.example .env; fi  # optional provider settings
make serve
```

Then open http://localhost:8471, create a course, and upload a syllabus and some
textbooks. `make benchmark` builds the labelled evaluation corpus instead, which
needs materials the repo does not ship.

No API keys are required. Every provider has a deterministic offline backend, so
the whole system — ingest, retrieval, agent loop, eval harness — runs and produces
real numbers with nothing configured. Add keys to `.env` (see `.env.example`) to
use the production path; which backend produced any given metric is recorded in
every eval report.

To enable voice, add `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` to `.env` and
restart the app. `ELEVENLABS_STT_MODEL` and `ELEVENLABS_TTS_MODEL` are optional;
defaults are `scribe_v2` and `eleven_flash_v2_5`. In **AI & voice settings**,
allow voice processing, then choose a voice and speech speed. The key remains on
the server. Voice consent covers sending microphone audio for transcription and
educational answer text for speech generation. Course Copilot does not save raw
recordings; ElevenLabs processes requests under its own service terms. Revoking
voice consent disables Voice Only Mode. Typed questions and other study features
remain available if voice is not configured or the provider is unavailable.

To set the ElevenLabs key without putting it in shell history or printing it,
run `./.venv/bin/python scripts/configure_elevenlabs.py` from the project root.
The script prompts with hidden input and updates the existing `.env` entry in
place. `.env`, `.env.local`, and other `.env.*` files are ignored by Git; only
the placeholder `.env.example` is included as a template.

The first-use privacy screen explains which question and textbook passages are
sent to free-tier Gemini, and that those inputs may be used to improve Google's
models. That path is off until the user opts in. A personal API key or configured
local Ollama model does not use that free-tier path. Assignment files are stored
as Practice sources and never enter textbook retrieval.

```bash
make eval        # full suite, writes eval/RESULTS.md
make eval-fast   # retrieval + refusal, seconds
make test        # unit tests
```

## What it does

**Grounded answering.** Hybrid retrieval — BM25 and dense vectors fused with
reciprocal rank fusion, then reranked — over your own materials. Every factual
claim carries a citation to a page. When the materials do not contain the answer,
it refuses instead of guessing.

**Calibrated refusal.** The threshold is swept on a labelled question set rather
than picked by feel, and composes two signals: passage relevance, and what the
question asks the system to *do*. The second exists because relevance alone cannot
tell "what does chapter 15 say about the federal funds rate" from "what is the
federal funds rate right now" — the retrieved passage is identical.

**Divergence checking.** Extracts a relationship claim from a chapter *together
with its conditions*, maps its variables to FRED series, and compares sub-periods
on a stated transform. When the chapter's claim is conditional — short run,
expectations fixed, absent supply shocks — it says so and withholds the
comparison rather than manufacturing a disagreement.

**Derived engagement.** Every question you ask increments the chapter it touched.
Exam readiness and the study plan are computed from that log. A manual override
exists for offline work, is stored separately, labelled self-reported, and never
overwrites the derived signal.

**Voice access.** Ask by voice records only after an explicit click and voice
consent, sends the temporary clip through the backend to ElevenLabs, then places
the transcript in the existing question box. Normal mode lets you edit it before
submitting. Voice Only Mode can submit it automatically and read the resulting
grounded answer aloud; the microphone still requires a separate click for every
recording. Answers, practice prompts, staged help, and test feedback have
read-aloud controls with play, pause, resume, stop, and replay. Generated audio is
cached only in the current browser session.

**Graceful degradation.** ANTH 201 and ENGL 150 have no data link. A question
about stratigraphy is answered from the readings and never reaches for a time
series, however quantitatively it is phrased — enforced by a guard, and measured
by a dedicated eval slice.

## Measured

The latest saved report covers an 82-question labelled set (48 in-corpus) and a
2,183-chunk benchmark corpus. Its detailed tables and backend labels are in
[`eval/RESULTS.md`](eval/RESULTS.md). The retrieval report was regenerated
offline; its separate Gemini explanation-groundedness figures are historical
and predate the latest database rebuild.

Run the explanation sweep separately from `make eval`:

```bash
COPILOT_OFFLINE=0 ./.venv/bin/python -m eval.run_explanation_grounding \
  --json eval/explanation_grounding.json
```

It uses Gemini for benchmark-only explanation generation and claim judging. Its
JSON output contains generated text and is ignored by Git.

## Using your own materials

Create a course in the app and upload files. PDF, DOCX, Markdown and text are
supported. Image-only pages are OCR'd automatically (`rapidocr-onnxruntime`, no
system packages); a file that still cannot be read is reported by name with a
reason and never contributes empty chunks.

Syllabus rows link to chapters by **title similarity**, not chapter number: a
syllabus citing a 10th-edition chapter number against an 8th-edition PDF still
lands on the right chapter. Rows that do not link confidently, and rows the two
extraction passes disagreed about, are the only ones shown on the review screen.

Syllabus onboarding: `POST /api/onboard/extract` runs two structured extractions
with different prompts, diffs them row by row, and returns only the disagreements
for review. Rows both passes agree on are accepted silently.

For integrations, list courses with `GET /api/courses`, upload one or more files
using multipart `files` fields at `POST /api/courses/{course_id}/materials`, and
poll the returned `job_id` at `GET /api/jobs/{job_id}` until `done` or `failed`.
While the local server is running, `http://127.0.0.1:8471/docs` shows the API
schema. The API currently has no login or API key and assigns requests to the
configured local user; keep it bound to loopback and do not expose it to a
network. See [the BB Plus integration guide](docs/BBPLUS_INTEGRATION.md) for its
extension contract.

For a local integration, list courses with `GET /api/courses`, upload one or
more files using multipart `files` fields at
`POST /api/courses/{course_id}/materials`, and poll the returned `job_id` at
`GET /api/jobs/{job_id}` until `done` or `failed`. See
[`docs/BBPLUS_INTEGRATION.md`](docs/BBPLUS_INTEGRATION.md) for the BB Plus text
bridge contract. The API currently has no login or API key and assigns requests
to the configured local user, so keep it bound to loopback and do not expose it
to a network.

## Layout

```
api/        retrieval, agent, data, dashboard, syllabus, store
ingest/     extract -> chunk -> embed -> index
eval/       runners, report generator, and benchmark/ (fixed labelled corpus)
web/        static SPA (no build step)
docs/       ARCHITECTURE.md, EVAL.md
```

## UI/UX freeze

The application's interface is frozen after the final polish pass. Future work
must not change its visual design, layout, styling, navigation, or interaction
patterns unless the user explicitly requests a UI/UX change. See the root
`AGENTS.md` project instruction for the full rule.

## Honest limitations

- The benchmark has 2,183 chunks across four courses. Results characterize only
  those benchmark materials and labels, not arbitrary textbooks.
- Without `FRED_API_KEY`, economic series are **synthetic**, stamped as such, and
  every divergence card built on them carries a hard warning.
- Product generation depends on the configured provider and privacy settings;
  the explanation groundedness benchmark requires `GEMINI_API_KEY` and
  `COPILOT_OFFLINE=0`.
- No auth yet. The schema is multi-tenant, but the API maps every request to the
  configured `local-user`; `current_user()` in `api/main.py` is the seam real
  authentication would replace. Keep the server loopback-only.
- Voice features require an ElevenLabs API key, a configured default voice, and
  browser microphone permission. Automated tests mock ElevenLabs; live provider
  requests have not been verified without an account configured in this app.
- No deployment, no calendar integration. See ARCHITECTURE.md.

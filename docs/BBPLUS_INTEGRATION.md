# BB Plus Study Library integration

This repository is the Course Copilot (AI/UI) half. The BB Plus Chrome extension
remains the source of Blackboard discovery, file downloading, and document
parsing. Connect the two through the existing Course Copilot course-materials
upload API; do not copy IndexedDB or Blackboard parsing code into this backend.

## First integration contract

1. In the extension, call `chrome.runtime.sendMessage` with
   `BBX_LIBRARY_QUERY` and `{ courseId: blackboardCourseId }`.
2. Let the user map that Blackboard course ID to one Course Copilot `course_id`.
   These are unrelated opaque identifiers: preserve the Blackboard ID as a
   string and never assume it equals a Course Copilot ID. List Course Copilot
   courses with `GET /api/courses`; the response has a `courses` array. Persist
   the mapping in the extension, keyed by the Blackboard ID.
3. For each returned document, use its `text` field from
   `BBIR.flattenToText(doc)`. Do not send file bytes through extension runtime
   messages. Side 2 does not retain original file bytes after parsing.
4. From the extension service worker, upload each document as UTF-8 plain text
   in the multipart form field named `files` to
   `POST http://127.0.0.1:8471/api/courses/{course_id}/materials`. Send one or
   more files in that field. Prefix the filename with `bbplus_` and include a
   stable short hash of `itemId` plus a sanitized title, for example
   `bbplus_a12bc34d_elasticity.txt`. Keep Blackboard IDs and raw titles out of
   paths; the filename is metadata, not an authorization value.
5. A successful upload returns HTTP 200 and JSON shaped like
   `{"job_id":"…","course_id":"…","files":["bbplus_…txt"]}`. Poll
   `GET http://127.0.0.1:8471/api/jobs/{job_id}`. The job object contains `id`,
   `course_id`, `status`, `created_at`, `updated_at`, and `files`; each file
   reports its filename, stage, page progress, chunk count, status, and detail.
   Job states are `queued`, `running`, `done`, and `failed`. Stop polling on
   `done` or `failed`; inspect per-file `status` (`ok`, `failed`, or
   `unsupported`) and `detail`, because a completed job may include a file-level
   extraction failure. A job belonging to another configured user returns 404.

## Local API and security contract

- The materials route is `POST /api/courses/{course_id}/materials`; it accepts
  multipart `files` fields and returns a background ingest job. Poll with
  `GET /api/jobs/{job_id}`. Use `GET /api/courses` for course mapping.
- There is currently **no authentication header or login**. The server assigns
  every request to `default_user_id` (currently `local-user`). Job polling is
  scoped to that configured user, but this is not multi-user authentication.
  Keep the API bound to loopback (`127.0.0.1`); do not expose port 8471 to a
  network. The extension must not accept a user-provided API URL. A future
  networked deployment needs authentication and an origin allowlist.
- The API currently allows cross-origin requests (`Access-Control-Allow-Origin:
  *`). Browser extensions still need host permission for the local origin
  (`http://127.0.0.1:8471/*`; include `http://localhost:8471/*` if users may
  open the app through that host). Use the same hostname consistently for the
  app and requests. The local API does not require a custom request header.
- `course_id` is a Course Copilot identifier returned by the course list API;
  it is not the Blackboard course ID. Blackboard course IDs are opaque strings
  sent only to `BBX_LIBRARY_QUERY` and retained in the extension's mapping.
- `BBX_LIBRARY_QUERY` results must have a readable `text` string from
  `BBIR.flattenToText(doc)`. Upload that string encoded as UTF-8. Empty strings
  should be skipped. Course Copilot computes a SHA-256 hash of uploaded bytes;
  an unchanged file in the same user/course is skipped as “reused”. A changed
  document should keep its stable `itemId`-based filename so the existing source
  is refreshed rather than duplicated.
- The app must be running locally and accept connections on port 8471. There is
  no additional BB Plus environment variable. Existing Course Copilot provider,
  offline, and consent settings govern later AI actions; ingest itself uses the
  configured embedder. `.env` values stay in Course Copilot and must never be
  copied into extension storage or messages.

The extension service worker will need host permission for the local Course
Copilot origin (`http://127.0.0.1:8471/*`; include the `localhost` variant if
the app is accessed that way). Keep the extension's existing Blackboard host
permission narrow. This bridge transfers parsed text to the local app; it does
not directly contact an external service. Subsequent AI actions remain subject
to Course Copilot's existing provider and consent settings.

## Why this boundary is merge-friendly

Course Copilot processes the text through its existing upload, chunking,
embedding, and indexing flow. The resulting sources use type `bbplus`, so they
remain identifiable and are included in the Materials-only cleanup scope. No
retrieval, refusal-gate, or evaluation behavior needs a separate BB Plus path.
The source hash is computed from the imported text by the existing ingest flow,
making identical exports idempotent while their indexed source remains present.

The initial bridge intentionally uses the stable flattened-text API. Flattening
omits images and math blocks, and may lose some table structure. Original PDFs
and images are not available from the current BB Plus library after parsing.
Preserving structured blocks or assets should be a separately versioned API
extension once both repositories are available; do not silently treat flattened
text as a lossless copy.

## Shared ownership

Only the extension repository should change its scan, download, IndexedDB, and
parser logic. Only this repository should decide how imported text enters the
Course Copilot corpus. Keep the adapter contract above as the seam when both
codebases are brought together on the other device.

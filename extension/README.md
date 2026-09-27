# BB Plus

BB Plus is a local Chrome extension that adds a lightweight course/content browser to Blackboard Ultra while keeping a full diagnostic mode for development.

## Unified product status

This extension is the Blackboard-facing UI for the merged BB Plus + Course Copilot application. The integration adds explicit Blackboard-to-Course-Copilot course mapping, user-triggered structured material sync, ingestion-job polling, and course-scoped Ask with answer depth, explanations, and citations. Read the repository [README](../README.md) and [final integration contract](../docs/BBPLUS_INTEGRATION.md) for setup and the supported architecture. Some sections below describe the extension’s pre-merge design and are historical where they conflict with that contract.

## Current UI

The extension has two modes:

- **Student View** (default): shows a compact list of courses. Clicking a course loads the parsed course outline.
- **Debug**: exposes the full diagnostics UI, including raw Blackboard JSON, page diagnostics, request traces, typed content, and course-data probing.

The floating launcher uses `BB Plus.png`. Replace that file with the real square logo later; the code already loads it as the launcher image.

## Blackboard data model discovered so far

### Course list

The reliable course-list structure is:

```text
network response
└── body.results[*].course
    ├── displayName
    └── term.name
```

Only objects with both `course.displayName` and `course.term.name` are treated as courses.

Term filtering uses exact matching against `course.term.name`.

### Course-list persistence

Raw network responses are transient, but parsed course records and the successful course-list endpoint are cached locally. The extension replays the same-origin course-list request after navigation so the course list survives entering/exiting courses.

### Ultra content handlers

The extension currently recognizes these Blackboard Ultra handlers:

```text
resource/x-bb-folder
resource/x-bb-document
resource/x-bb-file
resource/x-bb-lesson
resource/x-bb-asmt-test-link
external/course/LTI/forum link handlers
```

Visible types are:

```text
Folder
Learning Module
Document
File
Link
Assessment
```

### Documents

Ultra Documents are represented by two related API objects:

```text
resource/x-bb-folder
contentHandler.isBbPage = true
└── child resource/x-bb-document
```

Those are collapsed into **one visible Document**.

Canonical document route:

```text
/ultra/courses/{courseId}/document/{documentId}?view=content&state=view
```

Use the outer `isBbPage` wrapper ID as `documentId`.

Embedded PDFs/images/files found in the document body are shown as children of that one Document.

### Folders

Regular Blackboard folders use:

```text
resource/x-bb-folder
```

Canonical folder destination:

```text
/ultra/courses/{courseId}/outline
```

Folder children are recursively traversed through the Learn course-content children API. Folder traversal is prioritized ahead of attachment fallback requests so nested folders do not get starved by noisy 400/404 attachment checks.

### Learning Modules

Learning Modules use:

```text
resource/x-bb-lesson
```

They are separate from ordinary folders but are also recursively traversable containers.

This was important for courses that looked like “two links and nothing else” because nearly all visible content was actually nested inside Learning Modules.

### Files

Blackboard course-content files use:

```text
resource/x-bb-file
```

Canonical file route:

```text
/ultra/courses/{courseId}/file/{fileId}?courseId={courseId}
```

A real `x-bb-file` content node is displayed even if the current response does not expose a direct `bbcswebdav` download URL.

For embedded attachments inside Documents, direct download URLs are still preferred when available.

Recognized direct file/media extensions include:

```text
pdf, doc/docx, ppt/pptx, xls/xlsx, csv, txt, zip,
png, jpg/jpeg, gif, webp,
mp4, m4v, mov, webm,
mp3, m4a, wav
```

### Assessments

Assessment/test links are represented by:

```text
resource/x-bb-asmt-test-link
```

The useful assessment identifier is:

```text
contentHandler.assessmentId
```

Canonical route:

```text
/ultra/courses/{courseId}/assessment/{assessmentId}/overview?courseId={courseId}
```

### External links

For external links, prefer:

```text
contentHandler.url
```

This avoids Blackboard's `/ultra/redirect` indirection when the final destination is already present in the JSON.

### Alternate formats / Ally

Ignore Ally/alternative-format/generated conversion objects in the student-facing file list.

In particular, generated items with names containing `combined` and no stable direct URL are not useful because they are compiled on request.

They remain visible only indirectly through raw debug JSON if needed.

### Synthetic root

Blackboard can expose a structural root container (`ROOT`, `Course Root`, or an explicitly synthetic object). That root should not be shown to students. Its children are promoted into the visible top-level outline.

## Course-data probing

The extension uses read-only same-origin GET requests with the already-authenticated Blackboard browser session.

It tests public course/content endpoints and also learns same-origin Ultra JSON endpoints while the user browses a course.

Important diagnostic distinction:

```text
HTTP OK != JSON OK
```

An HTTP 200 can still be an HTML shell. The debug UI shows both HTTP and JSON success counts.

### Traversal strategy

Course probing uses a priority queue:

1. folder/module `children` requests
2. content detail / observed internal endpoints
3. low-priority attachment fallbacks

This avoids losing nested content to a request budget consumed by predictable 400/404 attachment probes.

## UI architecture

- `bridge.js`: runs in the page's MAIN world and observes same-origin fetch/XHR JSON.
- `content.js`: parses/caches data, renders Student View + Debug View, and performs read-only probes.
- `background.js`: host permission / extension setup.
- `styles.css`: injected BB Plus UI.
- `BB Plus.png`: floating launcher logo.
- `popup.html` / `popup.js`: extension popup controls.

## Privacy

BB Plus reads data already visible to the signed-in student session. It does not collect Blackboard passwords or export course data to a backend.

Raw diagnostic JSON can contain private educational information, so it should be reviewed/redacted before sharing.


## v2.1 visual polish

- Project/extension name is **BB Plus**.
- `bb-plus.png` is the launcher icon used inside Blackboard.
- Chrome extension/action icons use `bb-plus-16.png`, `bb-plus-32.png`, `bb-plus-48.png`, and `bb-plus-128.png`, generated from the same source image.
- To replace the logo later, replace `bb-plus.png` and regenerate/copy matching icon sizes if desired.
- Blackboard-matched palette:
  - borders: `#CDCDCD`, `0.0625rem` / 1px
  - header/top bar: `#FFFFFF`
  - main content background: `#F8F8F8`
  - primary text/dark elements: `#262626`
- The old BB Plus footer bar has been removed.


## v2.2 — preload, assessment IDs, alternate-format filtering

### Assessments
Blackboard assessment content has two distinct IDs:
- the course content item's `id`
- `contentHandler.assessmentId`

BB Plus now launches assessments **only** with `contentHandler.assessmentId`:

`/ultra/courses/{courseId}/assessment/{assessmentId}/overview?courseId={courseId}`

It never substitutes the content-node ID. In Debug/outline metadata, assessments show `assessmentId=...` explicitly to avoid confusion.

### Alternate formats
Ally/alternative-format/generated conversion artifacts are removed before the student outline is built. `combined` generated derivatives are excluded even if a canonical-looking file route can be constructed for them.

### Automatic course preloading
As soon as the course-list JSON is observed (or restored after navigation), BB Plus schedules course-outline loading automatically.

Preload behavior:
- up to 4 courses load concurrently;
- uses the preferred Blackboard course ID only;
- starts with the public `/contents` tree;
- recursively follows folders, learning modules, and document wrappers;
- only fetches single-item detail for document bodies during fast preload;
- skips resources/course metadata/attachment fallback probes unless Debug performs a full probe.

A compact student-facing outline is cached in `chrome.storage.local`, so previously loaded courses can display immediately after navigation while the current page refreshes them in the background.


## v2.3 — document wrappers inside Learning Modules

Anthology's Ultra content model defines `resource/x-bb-document` as the body of
an Ultra document and says that body must be a child of an
`resource/x-bb-folder` page wrapper where `isBbPage=true`.

In practice, a `/children` listing may not always contain the full
`isBbPage` metadata. BB Plus therefore recognizes an Ultra Document in either
of two ways:

1. explicit: outer `x-bb-folder` has `contentHandler.isBbPage=true`
2. structural: an `x-bb-folder` directly owns an `x-bb-document` child

Both cases collapse to one visible Document. Files associated with either the
document body ID or wrapper ID are nested under that Document.

Fast preload also performs one content-detail read for folder-like containers,
after scheduling their children, to recover `isBbPage` when Blackboard omitted
it from the listing.

Orphan attachment observations are no longer promoted to the course root.
Legitimate root-level files still appear because they are represented by real
`resource/x-bb-file` course-content nodes.

The compact outline cache is versioned; v2.3 invalidates older cached trees once
so hierarchy fixes are visible immediately after upgrading.


## v2.4 — exhaustive sibling resolution

The v2.3 hierarchy fix could still miss a sibling Ultra Document in a large or
deep tree because of probe queue ordering.

Fast preload previously prioritized recursive `children` calls over content
detail calls. A folder/module branch could therefore keep expanding until the
fast request cap was reached, while a sibling page wrapper's detail request was
still waiting in the normal queue.

v2.4 introduces a `critical` queue. For every ID returned by a root
`/contents` listing or a `/children` listing:

1. its `/contents/{contentId}` detail request is queued as **critical**
2. if it is a container, its `/children` request is queued next
3. recursive traversal continues afterward

This means sibling content objects are resolved before deeper branches can
starve them.

Document bodies (`resource/x-bb-document`) and possible page wrappers also get
critical detail resolution during student preload.

Debug probe results now retain:
- `discoveredContentIds`
- `unresolvedContentIds`

so a specific ID can be checked directly if Blackboard returned it in a listing
but its detail endpoint never resolved.

The fast request ceiling is increased to 220, but the queue remains focused on
course content rather than the broader Debug-only endpoint set.

Outline cache schema version 4 invalidates older cached trees after upgrade.


## v2.4.1 — Ally false-positive fix

A missing document was traced to the alternate-format filter itself.

The document title:

`Project 2: Literally Loving Linked Lists LOL`

contains the character sequence `ally` inside the word `Literally`. The previous
filter used a loose `/ally/i` substring match and therefore incorrectly treated
that normal Blackboard Document as an Ally alternate-format artifact.

The filter is now field-aware:

- `Ally` is matched only as a standalone provider/source token, URL namespace,
  or path segment.
- arbitrary course titles are never filtered merely because they contain the
  letters `ally`.
- `alternative format` / `alternate format` phrases are still filtered.
- generated `combined` entries are filtered only when conversion/Ally context
  supports that interpretation (or when they are non-linkable non-x-bb-file
  placeholders).

Outline cache schema version 5 invalidates any cached tree that omitted the
document because of this false positive.


## v2.5 — filesystem export

BB Plus can export the normalized course tree from the Student View header.

Chrome extensions cannot silently choose an arbitrary absolute path such as the
Desktop or the extension installation directory. The `chrome.downloads` API
accepts paths relative to the browser's Downloads directory, including
subdirectories.

BB Plus therefore exports to:

`Downloads/BB Plus/Courses/`

Example:

```text
BB Plus/
└── Courses/
    └── Example Course/
        ├── course.json
        ├── Week 1/
        │   ├── _item.json
        │   ├── Lecture.pdf
        │   └── Notes/
        │       ├── _item.json
        │       └── handout.pdf
        └── Project 1/
            ├── _item.json
            └── specification.pdf
```

Rules:
- each course contains `course.json` with normalized course metadata and the
  complete normalized outline;
- Folder, Learning Module, and Document objects become directories containing
  `_item.json`;
- real files are downloaded when BB Plus has a direct byte-download URL;
- if BB Plus knows only a Blackboard UI/viewer URL, it writes a
  `*.bbplus.json` pointer instead of pretending the HTML viewer is the file;
- Links and Assessments are exported as JSON records containing IDs and URLs;
- the directory/file names are sanitized for cross-platform filesystem use.

The export mechanism uses the normal authenticated browser session for
Blackboard HTTP(S) downloads. No credentials, cookies, or authorization
headers are written into the exported metadata.


## v2.6 — selected-term export + rendered Document PDFs

Export now uses only the courses in BB Plus's currently selected term.

`Downloads/BB Plus/Courses/_export.json` records the selected term and the exact
courses included in that export.

### Blackboard Documents

A visible Ultra Document remains a directory because it can contain embedded
files, but BB Plus now also renders the authenticated Blackboard Document page
to PDF:

```text
Document Name/
├── _item.json
├── Document Name.pdf
├── attachment-1.pdf
└── image.png
```

The PDF is generated from the actual rendered Blackboard page, not from the
metadata JSON. BB Plus opens the Document URL in a temporary inactive tab,
waits for Ultra to render, uses Chrome DevTools Protocol `Page.printToPDF`, then
closes the tab.

The extension's own `#bbx-root` UI is hidden from print output.

This uses Chrome's required `debugger` permission. Chrome does not permit the
`debugger` permission to be optional. The permission is used only during an
explicit Export operation, on temporary Blackboard Document tabs, and BB Plus
detaches immediately after each PDF is generated.

Document PDFs are rendered sequentially for reliability. Normal metadata/file
downloads continue in parallel.


## v2.7 — direct HTML Document export

The PDF/debugger export introduced in v2.6 has been removed.

Blackboard Documents are now exported directly from the API/BBML content that
BB Plus already collected. No hidden tabs are opened, no page is printed, and
the extension no longer requests Chrome's `debugger` permission.

Example:

```text
Document Name/
├── _item.json
├── Document Name.html
├── attachment.pdf
└── image.png
```

The HTML file contains:
- the Blackboard Document's collected HTML/BBML body;
- the document title;
- a `<base>` pointing at the Blackboard origin so root-relative Blackboard
  links continue to resolve when the file is opened in a browser;
- a small local stylesheet for readable standalone viewing;
- embedded machine-readable BB Plus metadata in an
  `application/json` script element.

The compact local outline cache also preserves the extracted Document markup so
a later export can still produce the HTML file without retaining the entire raw
network response.

This is intentionally optimized for local processing/search/indexing rather
than pixel-perfect reproduction of Blackboard's page chrome. Child attachments
continue to be exported separately in the mirrored directory.

## v2.8 — local study library (in-memory ingestion pipeline)

This introduces a second, deliberately separate pipeline from the filesystem
export in v2.5–v2.7. Export exists to prove course content can be fetched and
formatted at all, and it stays useful as a manual backup — but it writes
straight to disk via `chrome.downloads` and never gives the extension the
bytes back in JS, so it can't feed anything else. The features BB Plus is
actually being built for (schedule/due-date generation, AI-guided study
Q&A, practice-problem generation from course material) need the parsed
content to live *in* the extension, in memory/IndexedDB — never on disk —
so those can plug into the same local study library instead of each
re-fetching and re-parsing course content on their own.

### Design: one structural format, not one file format

Every source (a Blackboard "document," a PDF, a DOCX, a PPTX, a manually
uploaded file) is parsed into the same block-based IR instead of being
converted into a single output file format. Converting everything to, say,
PDF-of-record or to plain text was considered and rejected: it's exactly
the kind of lossy homogenization that would have thrown away tables,
figures, and (for docx) real embedded math. Blocks: `heading`, `paragraph`,
`list`, `table`, `image` (referenced by content hash, not inlined), `math`
(kept verbatim when the source has it), `code`, and `unparsed` (an honest
placeholder for content that couldn't be extracted, rather than a guess).
See `lib/ir.js` for the exact shapes.

### Where things run

- **`content.js`** (already running on the authenticated Blackboard tab) —
  walks the course outline exactly like export does, but instead of queuing
  a disk-download job, it fetches file bytes itself
  (`fetch(url, { credentials: "include" })`, so the existing session cookie
  just works) and hands them to the background service worker. Documents
  don't even need a fetch — their HTML/BBML body was already collected
  during outline probing.
- **`background.js`** — the orchestrator and the only writer to
  IndexedDB. It lazily creates a `chrome.offscreen` document to do the
  actual parsing, and closes it again once a sync finishes.
- **`offscreen.js`** (inside `offscreen.html`, a hidden `chrome.offscreen`
  document) — the one place bytes turn into IR blocks. It's stateless: it
  never touches storage, just parses and hands blocks + any newly-found
  image assets back to `background.js`. Offscreen documents are MV3's
  supported way to get a real DOM (`DOMParser`, `<canvas>`, WASM) without a
  visible tab, which content scripts don't reliably offer and service
  workers don't offer at all.

This split (fetch in the tab, parse+store in the background page) means the
offscreen document can be opened and closed per sync run without losing
anything — nothing about it is stateful — and it means `content.js` never
holds more than one file's bytes in memory at a time longer than it takes to
hand them off.

### Storage: `lib/db.js`, three IndexedDB object stores, per browser profile

- `documents` — one row per ingested item (id, course, title, source type,
  a content hash used to skip re-parsing an unchanged file, and its blocks)
- `assets` — a content-addressed blob store for images/figures, so a figure
  reused across a textbook is only ever stored once
- `derived` — a cache for expensive results keyed by whatever a future
  consumer wants (e.g. `schedule::<courseId>`), invalidated automatically
  whenever a document in that course changes

The extension retains one IndexedDB per browser profile for full-fidelity
parsed blocks and local figure assets. In the merged application, the user may
explicitly sync readable document structure to the local Course Copilot API;
Course Copilot SQLite is the canonical searchable index, retrieval system, and
application store. The extension does not persist provider credentials or send
Blackboard cookies. The extension-side fetch and parse loops remain bounded;
Course Copilot owns its separate ingestion and retrieval resource lifecycle.

### Format coverage

| Source | Parser | Needs vendoring? |
|---|---|---|
| Blackboard "document" (HTML/BBML) | `DOMParser` walk in `offscreen.js` | no — works today |
| `.pdf` | `pdf.js` — text per page, plus a full-page image render, but *only* on pages whose operator list shows an actual embedded image, so a long text-only textbook doesn't balloon in size | yes |
| `.docx` | `mammoth` → HTML (images intercepted into content-addressed assets) → same HTML→IR walk as documents | yes |
| `.pptx` | `JSZip` unzips the slide/relationship XML directly — no dedicated pptx library needed | yes |

See `vendor/README.md` for exactly what to drop in. Until a library is
vendored, that format's items land in the "couldn't be processed" banner
with a `missing-vendor-library:*` reason instead of failing the whole sync.

### Math and figures: kept, not guessed

LaTeX/MathML is preserved verbatim wherever the source already has it. A
scanned or rasterized equation in a PDF is *not* run through OCR pretending
to reconstruct LaTeX — reconstructing real math from a rendered image is a
hard, unreliable problem — it's kept as an `image` block with its
surrounding text, meant to be handed to a vision-capable model directly at
query time (for practice problems, say) rather than lossily flattened
ahead of time. The one known gap is Word's native equation objects inside a
`.docx`, which mammoth doesn't convert — see the note in `offscreen.js`.

### The "couldn't retrieve/process this" banner

The v2.5–v2.7 export path already tagged files with no `downloadUrl` with an
explanatory note buried in a `.bbplus.json` sidecar nobody would open. That
case, plus unsupported formats and missing-vendor-library cases, now
surface as one list in the Study Hub panel after a sync
(`renderIngestBanner()` in `content.js`), each row showing the course,
title, why it didn't make it in, a link to open it in Blackboard directly,
and — for the case where the file itself is presumably a supported format
and Blackboard just didn't expose a fetchable URL — a small upload control
that runs the manually-selected file through the exact same parser, filed
under the exact same catalog entry it was meant to fill.

### Querying the library back out

`BBX_LIBRARY_QUERY` (handled in `background.js`, `queryLibrary()`) is the
one read path every future feature is meant to share, rather than each
reimplementing search: pass `{ courseId }` for one Blackboard course to read
its full block arrays and flattened text. The Course Copilot adapter reads
these stored documents inside the background service worker and sends supported
blocks to the local server only after the user chooses to sync. Course Copilot
uses its existing chunking, embeddings, hybrid retrieval, refusal gate, provider
abstraction, and citations; there is no JavaScript RAG implementation in the
extension.

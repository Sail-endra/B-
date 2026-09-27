importScripts("lib/ir.js", "lib/db.js");

// ---- Offscreen document lifecycle ---------------------------------------
//
// The offscreen document does the actual work of ingestion: it downloads
// each file, parses it and writes it to IndexedDB (see offscreen.js for why
// bytes must never pass through chrome.runtime messaging). It is created
// on demand and closed after it has been idle for a while - NOT after every
// batch, which previously meant reloading pdf.js/mammoth/JSZip every 5
// files.

let offscreenReady = null;
let offscreenIdleTimer = null;
const OFFSCREEN_IDLE_MS = 90000;

async function ensureOffscreenDocument() {
  clearTimeout(offscreenIdleTimer);
  if (await chrome.offscreen.hasDocument()) return;
  if (offscreenReady) return offscreenReady;
  offscreenReady = chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["DOM_PARSER"],
    justification: "Download and parse course files (HTML/PDF/DOCX/PPTX/text/images) into the local study library."
  });
  try {
    await offscreenReady;
  } finally {
    offscreenReady = null;
  }
}

function scheduleOffscreenClose() {
  clearTimeout(offscreenIdleTimer);
  offscreenIdleTimer = setTimeout(async () => {
    try {
      if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument();
    } catch (_) {}
  }, OFFSCREEN_IDLE_MS);
}

// ---- Ingestion (forwarding only) -------------------------------------------
//
// `job` shapes (all JSON-safe - no ArrayBuffers anywhere):
//   { kind: "fetch",  itemId, courseId, courseName, title, sourceType, url, mimeType, pageUrl }
//   { kind: "markup", itemId, courseId, courseName, title, sourceType: "html", markup }
//   { kind: "upload", itemId, courseId, courseName, title, sourceType, mimeType, base64 }

async function runIngestJobs(jobs) {
  const safeJobs = Array.isArray(jobs) ? jobs.slice(0, 5000) : [];
  const results = new Array(safeJobs.length);
  let cursor = 0;

  await ensureOffscreenDocument();

  // Concurrency applies to downloads; offscreen.js serializes the parse
  // step itself. Kept modest out of politeness to Blackboard.
  async function worker() {
    while (cursor < safeJobs.length) {
      const index = cursor++;
      const job = safeJobs[index];
      try {
        const result = await chrome.runtime.sendMessage({ target: "offscreen", type: "BBX_INGEST_ONE", job });
        results[index] = result || { itemId: job.itemId, title: job.title, courseName: job.courseName, ok: false, stage: "parse", reason: "no response from parser" };
      } catch (err) {
        results[index] = { itemId: job.itemId, title: job.title, courseName: job.courseName, ok: false, stage: "parse", reason: `parser unavailable: ${err?.message || err}` };
      }
    }
  }

  try {
    await Promise.all(Array.from({ length: Math.min(3, safeJobs.length || 1) }, worker));
  } finally {
    scheduleOffscreenClose();
  }
  return { results };
}

// Lightweight: ids + whether each stored document has real content. Used
// by verification, which previously pulled every document's full text and
// blocks (whole textbooks) through messaging just to read their ids.
async function libraryStatus(courseId) {
  const docs = await BBDB.listDocumentsByCourse(courseId);
  return docs.map((doc) => ({
    itemId: doc.itemId,
    title: doc.title || "",
    contentful: (doc.blocks || []).some((b) => b.type !== "unparsed"),
    parserVersion: doc.parserVersion || 0
  }));
}

// ---- Reading the library back out ---------------------------------------
//
// This is the "plug into a lot of programs" surface from the design
// discussion: schedule generation, the study chatbot, and practice-problem
// generation all resolve "which document(s) is this about" through this
// same metadata-first lookup rather than each reimplementing search.

async function queryLibrary(query) {
  let docs;
  if (query.courseId) {
    docs = await BBDB.listDocumentsByCourse(query.courseId);
  } else if (query.text) {
    docs = await BBDB.findByCourseOrTitle(query.text);
  } else {
    docs = [];
  }

  return {
    documents: docs.map((doc) => ({
      itemId: doc.itemId,
      courseId: doc.courseId,
      courseName: doc.courseName,
      title: doc.title,
      sourceType: doc.sourceType,
      ingestedAt: doc.ingestedAt,
      warnings: doc.warnings,
      text: BBIR.flattenToText(doc),
      blocks: doc.blocks
    }))
  };
}

// ---- Course Copilot service adapter -------------------------------------
// The extension owns Blackboard discovery and local structured IR. Course
// Copilot remains canonical for indexed chunks, retrieval, and answers.
const COURSE_COPILOT_ORIGIN = "http://127.0.0.1:8471";
const COURSE_COPILOT_PERMISSION = `${COURSE_COPILOT_ORIGIN}/*`;

async function courseCopilotFetch(path, options = {}) {
  const granted = await chrome.permissions.contains({ origins: [COURSE_COPILOT_PERMISSION] });
  if (!granted) {
    throw new Error("Course Copilot is not connected. Open the BB Plus extension menu and connect it first.");
  }
  let response;
  try {
    response = await fetch(`${COURSE_COPILOT_ORIGIN}${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      credentials: "omit"
    });
  } catch (error) {
    throw new Error(`Could not reach Course Copilot at 127.0.0.1:8471. Start the local server and retry. ${error?.message || ""}`.trim());
  }
  let body = {};
  try { body = await response.json(); } catch (_) {}
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : detail?.message;
    throw new Error(message || `Course Copilot returned HTTP ${response.status}.`);
  }
  return body;
}

async function syncLibraryToCourseCopilot(blackboardCourseId) {
  const docs = await BBDB.listDocumentsByCourse(blackboardCourseId);
  if (!docs.length) throw new Error("BB Plus has no saved materials for this course yet. Build the study library first.");
  if (docs.length > 100) throw new Error("This course has more than 100 saved items. Sync smaller groups from the Course Copilot materials page.");
  const documents = docs.map((doc) => ({
    item_id: doc.itemId,
    course_id: doc.courseId,
    title: doc.title || "Blackboard material",
    source_type: doc.sourceType || "unknown",
    blocks: Array.isArray(doc.blocks) ? doc.blocks : []
  }));
  return courseCopilotFetch(
    `/api/integrations/bbplus/course-mappings/${encodeURIComponent(blackboardCourseId)}/materials`,
    { method: "POST", body: JSON.stringify({ documents }) }
  );
}

const ENABLED_KEY = "bbx_enabled_origins";

function originPattern(origin) {
  return `${origin}/*`;
}

function scriptId(origin, suffix) {
  // Stable, Chrome-safe registration id.
  let hash = 5381;
  for (const ch of origin) {
    hash = ((hash << 5) + hash) ^ ch.charCodeAt(0);
    hash >>>= 0;
  }
  return `bbx_${hash.toString(16)}_${suffix}`;
}

async function enabledOrigins() {
  const stored = await chrome.storage.local.get(ENABLED_KEY);
  return Array.isArray(stored[ENABLED_KEY]) ? stored[ENABLED_KEY] : [];
}

async function setEnabled(origin, enabled) {
  const origins = new Set(await enabledOrigins());
  enabled ? origins.add(origin) : origins.delete(origin);
  await chrome.storage.local.set({ [ENABLED_KEY]: [...origins] });
}

async function unregisterOrigin(origin) {
  const ids = [scriptId(origin, "bridge"), scriptId(origin, "content")];
  try {
    await chrome.scripting.unregisterContentScripts({ ids });
  } catch (_) {
    // It's fine if they were not registered.
  }
}

async function registerOrigin(origin) {
  await unregisterOrigin(origin);

  const matches = [originPattern(origin)];

  await chrome.scripting.registerContentScripts([
    {
      id: scriptId(origin, "bridge"),
      matches,
      js: ["bridge.js"],
      runAt: "document_start",
      world: "MAIN",
      persistAcrossSessions: true
    },
    {
      id: scriptId(origin, "content"),
      matches,
      js: ["lib/stage.js", "lib/audit.js", "content.js"],
      css: ["styles.css"],
      runAt: "document_start",
      world: "ISOLATED",
      persistAcrossSessions: true
    }
  ]);

  await setEnabled(origin, true);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message?.type === "BBX_ENABLE_ORIGIN") {
      await registerOrigin(message.origin);
      if (message.tabId) await chrome.tabs.reload(message.tabId);
      sendResponse({ ok: true });
      return;
    }

    if (message?.type === "BBX_DISABLE_ORIGIN") {
      await unregisterOrigin(message.origin);
      await setEnabled(message.origin, false);
      try {
        await chrome.permissions.remove({ origins: [originPattern(message.origin)] });
      } catch (_) {}
      if (message.tabId) await chrome.tabs.reload(message.tabId);
      sendResponse({ ok: true });
      return;
    }

    if (message?.type === "BBX_GET_STATUS") {
      const origins = await enabledOrigins();
      sendResponse({ ok: true, enabled: origins.includes(message.origin) });
      return;
    }

    if (message?.type === "BBX_CP_STATE") {
      sendResponse({ ok: true, ...(await courseCopilotFetch("/api/integrations/bbplus/state")) });
      return;
    }

    if (message?.type === "BBX_CP_SAVE_MAPPING") {
      const body = await courseCopilotFetch(
        `/api/integrations/bbplus/course-mappings/${encodeURIComponent(message.blackboardCourseId)}`,
        { method: "PUT", body: JSON.stringify({ course_id: message.courseId, course_name: message.courseName || "" }) }
      );
      sendResponse({ ok: true, ...body });
      return;
    }

    if (message?.type === "BBX_CP_CREATE_AND_MAP") {
      const body = await courseCopilotFetch(
        `/api/integrations/bbplus/course-mappings/${encodeURIComponent(message.blackboardCourseId)}/create`,
        { method: "POST", body: JSON.stringify({ code: message.code, title: message.title, term: message.term || "" }) }
      );
      sendResponse({ ok: true, ...body });
      return;
    }

    if (message?.type === "BBX_CP_SYNC_COURSE") {
      sendResponse({ ok: true, ...(await syncLibraryToCourseCopilot(message.blackboardCourseId)) });
      return;
    }

    if (message?.type === "BBX_CP_JOB") {
      const body = await courseCopilotFetch(`/api/jobs/${encodeURIComponent(message.jobId)}`, { method: "GET" });
      sendResponse({ ok: true, ...body });
      return;
    }

    if (message?.type === "BBX_CP_ASK") {
      const body = await courseCopilotFetch(
        `/api/integrations/bbplus/course-mappings/${encodeURIComponent(message.blackboardCourseId)}/ask`,
        { method: "POST", body: JSON.stringify({ question: message.question, depth: message.depth || "concise" }) }
      );
      sendResponse({ ok: true, ...body });
      return;
    }

    // ---- Study library ingestion (in-memory, never touches disk) ----
    //
    // Fetches bytes in the content script, parses them into IR blocks here
    // (via the offscreen document), and persists only to IndexedDB. Nothing
    // is written to the filesystem.



    if (message?.type === "BBX_INGEST_JOBS") {
      const result = await runIngestJobs(message.jobs || []);
      sendResponse({ ok: true, ...result });
      return;
    }

    if (message?.type === "BBX_STAGE_CHUNK") {
      await BBDB.putStagingChunk(message.stageKey, message.index, message.data);
      sendResponse({ ok: true });
      return;
    }

    if (message?.type === "BBX_STAGING_CLEAR") {
      await BBDB.clearStaging();
      // Remove the record that every page-embedded file overwrote under the
      // empty id "" in builds before 2.9.4.
      await BBDB.deleteDocument("");
      sendResponse({ ok: true });
      return;
    }

    if (message?.type === "BBX_LIBRARY_STATUS") {
      sendResponse({ ok: true, items: await libraryStatus(message.courseId) });
      return;
    }

    if (message?.type === "BBX_LIBRARY_QUERY") {
      const result = await queryLibrary(message.query || {});
      sendResponse({ ok: true, ...result });
      return;
    }

    if (message?.type === "BBX_OFFSCREEN_ERROR") {
      // See offscreen.js - this is how errors from the offscreen document
      // (which is too short-lived to reliably catch in chrome://inspect)
      // become visible here instead, in the persistent service-worker
      // console (chrome://extensions -> "service worker").
      const where = message.source ? ` at ${message.source}:${message.line}:${message.col}` : "";
      console.error(`[BBX offscreen]${where}`, message.message, message.stack || "");
      sendResponse({ ok: true });
      return;
    }

    if (message?.type === "BBX_LIBRARY_CLEAR_COURSE") {
      const removed = await BBDB.deleteDocumentsForCourse(message.courseId);
      await BBDB.invalidateDerivedForCourse(message.courseId);
      sendResponse({ ok: true, removed });
      return;
    }

    sendResponse({ ok: false, error: "Unknown message" });
  })().catch((error) => {
    console.error("[BBX background]", error);
    sendResponse({ ok: false, error: error?.message || String(error) });
  });

  return true;
});

// Dynamically registered content scripts persist across extension updates
// with the file list they were registered with. After an update that adds a
// file (v2.9.2 added lib/stage.js, v2.9.5 lib/audit.js), old registrations would keep loading
// only content.js and the sync would crash. Re-register every enabled
// origin on install/update so registrations always match this version.
chrome.runtime.onInstalled.addListener(async () => {
  for (const origin of await enabledOrigins()) {
    try {
      await registerOrigin(origin);
    } catch (err) {
      console.warn("[BBX background] re-register failed for", origin, err?.message || err);
    }
  }
});

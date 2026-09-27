// lib/db.js
//
// Local storage for the study library, in the extension's IndexedDB.
// Opened by two extension contexts that share one origin (and therefore one
// database): offscreen.js, which writes (it downloads + parses files, and
// file bytes cannot be passed through chrome.runtime messaging - that is
// JSON-only, an ArrayBuffer arrives as {}), and background.js, which reads
// to answer library queries. Both open the same DB_VERSION, so there is no
// version-upgrade race between them.
//
// Three object stores:
//   documents  - one row per ingested item: { itemId, courseId, ..., blocks }
//   assets     - content-addressed blob store for images/figures, keyed by
//                sha-256 hash so a figure reused across a textbook is only
//                ever stored once
//   derived    - cached expensive results keyed by a caller-chosen id, e.g.
//                "schedule::_40123_1" or "summary::_3634794_1", so a query
//                like "generate my schedule" doesn't re-run extraction on
//                every ask
//
// This remains a browser-local, per-profile representation. On explicit user
// sync, readable structured content is sent to the local Course Copilot server;
// raw files, Blackboard cookies, image bytes, and provider credentials are not.

(function (root) {
  "use strict";

  const DB_NAME = "bbplus_study_library";
  const DB_VERSION = 2; // v2: + staging store

  let dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);

      req.onupgradeneeded = () => {
        const db = req.result;

        if (!db.objectStoreNames.contains("documents")) {
          const store = db.createObjectStore("documents", { keyPath: "itemId" });
          store.createIndex("byCourse", "courseId", { unique: false });
        }

        if (!db.objectStoreNames.contains("assets")) {
          // keyPath is the content hash itself - "put" is naturally
          // idempotent, so re-ingesting a document that reuses an image
          // already on disk is a no-op for that asset.
          db.createObjectStore("assets", { keyPath: "hash" });
        }

        if (!db.objectStoreNames.contains("derived")) {
          db.createObjectStore("derived", { keyPath: "key" });
        }

        // Temporary: downloaded file bytes (as base64 chunks) waiting to be
        // parsed. See lib/stage.js. Deleted as soon as the file is parsed.
        if (!db.objectStoreNames.contains("staging")) {
          db.createObjectStore("staging", { keyPath: "id" });
        }
      };

      // Another context (the other of background.js / offscreen.js) may need
      // to upgrade the schema. A connection that doesn't close on request
      // blocks that upgrade forever, so close and reopen lazily instead.
      req.onblocked = () => console.warn("[BBDB] upgrade waiting for another open connection to close");
      req.onsuccess = () => {
        const db = req.result;
        db.onversionchange = () => { db.close(); dbPromise = null; };
        resolve(db);
      };
      req.onerror = () => reject(req.error || new Error("Failed to open study library database."));
    });
    return dbPromise;
  }

  function tx(db, storeNames, mode) {
    return db.transaction(storeNames, mode);
  }

  function reqToPromise(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error("IndexedDB request failed."));
    });
  }

  // ---- documents ------------------------------------------------------

  async function putDocument(doc) {
    const db = await openDb();
    const t = tx(db, ["documents"], "readwrite");
    await reqToPromise(t.objectStore("documents").put(doc));
    return doc;
  }

  async function getDocument(itemId) {
    const db = await openDb();
    const t = tx(db, ["documents"], "readonly");
    return reqToPromise(t.objectStore("documents").get(itemId)) || null;
  }

  async function deleteDocument(itemId) {
    const db = await openDb();
    const t = tx(db, ["documents"], "readwrite");
    await reqToPromise(t.objectStore("documents").delete(itemId));
  }

  async function listDocumentsByCourse(courseId) {
    const db = await openDb();
    const t = tx(db, ["documents"], "readonly");
    const index = t.objectStore("documents").index("byCourse");
    return reqToPromise(index.getAll(courseId));
  }

  async function deleteDocumentsForCourse(courseId) {
    const docs = await listDocumentsByCourse(courseId);
    const db = await openDb();
    const t = tx(db, ["documents"], "readwrite");
    const store = t.objectStore("documents");
    for (const doc of docs) store.delete(doc.itemId);
    return docs.length;
  }

  // Cheap metadata-first lookup: matches on course code/name/title
  // substrings before anyone falls back to full-text search. This is what
  // answers "how many classes this year for Math 103" without needing
  // embeddings - see README "Querying".
  async function findByCourseOrTitle(queryText) {
    const db = await openDb();
    const t = tx(db, ["documents"], "readonly");
    const all = await reqToPromise(t.objectStore("documents").getAll());
    const q = String(queryText || "").toLowerCase();
    if (!q) return all;
    return all.filter((doc) =>
      (doc.courseName || "").toLowerCase().includes(q) ||
      (doc.title || "").toLowerCase().includes(q)
    );
  }

  // ---- assets -----------------------------------------------------------
  //
  // Stored as raw ArrayBuffer + mimeType. Dedup by content hash, BUT an
  // existing record only counts if it really holds bytes: builds before
  // 2.9.1 sent assets through runtime messaging and stored {} in place of
  // the image, and a pure "exists -> skip" check would keep that garbage
  // forever.

  async function putAsset(hash, bytes, meta = {}) {
    const db = await openDb();
    const existing = await reqToPromise(tx(db, ["assets"], "readonly").objectStore("assets").get(hash));
    if (existing && existing.bytes instanceof ArrayBuffer && existing.bytes.byteLength > 0) return existing;
    const record = { hash, bytes, mimeType: meta.mimeType || "", storedAt: new Date().toISOString() };
    await reqToPromise(tx(db, ["assets"], "readwrite").objectStore("assets").put(record));
    return record;
  }

  async function getAsset(hash) {
    const db = await openDb();
    const t = tx(db, ["assets"], "readonly");
    return reqToPromise(t.objectStore("assets").get(hash));
  }

  // ---- staging (downloaded bytes awaiting parse) ------------------------

  function stagingId(stageKey, index) {
    return `${stageKey}#${String(index).padStart(5, "0")}`;
  }

  async function putStagingChunk(stageKey, index, data) {
    const db = await openDb();
    await reqToPromise(tx(db, ["staging"], "readwrite").objectStore("staging").put({ id: stagingId(stageKey, index), data, stagedAt: Date.now() }));
  }

  // Returns the chunks' base64 strings in order; throws if any is missing.
  async function getStagingChunks(stageKey, chunks) {
    const db = await openDb();
    const store = tx(db, ["staging"], "readonly").objectStore("staging");
    const parts = await Promise.all(Array.from({ length: chunks }, (_, i) => reqToPromise(store.get(stagingId(stageKey, i)))));
    const missing = parts.findIndex((p) => !p || typeof p.data !== "string");
    if (missing !== -1) throw new Error(`staged chunk ${missing + 1}/${chunks} is missing`);
    return parts.map((p) => p.data);
  }

  async function deleteStaging(stageKey, chunks) {
    const db = await openDb();
    const store = tx(db, ["staging"], "readwrite").objectStore("staging");
    await Promise.all(Array.from({ length: chunks }, (_, i) => reqToPromise(store.delete(stagingId(stageKey, i)))));
  }

  async function clearStaging() {
    const db = await openDb();
    await reqToPromise(tx(db, ["staging"], "readwrite").objectStore("staging").clear());
  }

  // ---- derived (cache) ----------------------------------------------------

  async function putDerived(key, value) {
    const db = await openDb();
    const t = tx(db, ["derived"], "readwrite");
    await reqToPromise(t.objectStore("derived").put({ key, value, cachedAt: new Date().toISOString() }));
    return value;
  }

  async function getDerived(key) {
    const db = await openDb();
    const t = tx(db, ["derived"], "readonly");
    const row = await reqToPromise(t.objectStore("derived").get(key));
    return row ? row.value : null;
  }

  async function invalidateDerivedForCourse(courseId) {
    const db = await openDb();
    const t = tx(db, ["derived"], "readwrite");
    const store = t.objectStore("derived");
    const all = await reqToPromise(store.getAll());
    for (const row of all) {
      if (String(row.key || "").includes(courseId)) store.delete(row.key);
    }
  }

  root.BBDB = {
    openDb,
    putDocument,
    getDocument,
    deleteDocument,
    listDocumentsByCourse,
    deleteDocumentsForCourse,
    findByCourseOrTitle,
    putAsset,
    getAsset,
    putStagingChunk,
    getStagingChunks,
    deleteStaging,
    clearStaging,
    putDerived,
    getDerived,
    invalidateDerivedForCourse
  };
})(typeof globalThis !== "undefined" ? globalThis : this);

// lib/stage.js - runs in the Blackboard tab (content-script world), loaded
// before content.js.
//
// Why downloads happen HERE and not in the extension's offscreen document:
// in real Blackboard, a fetch from this tab is a same-origin request that
// carries the user's session exactly like Blackboard's own requests do. That
// is the only download path that has been observed working against the real
// site (v2.9.0: every file downloaded). v2.9.1 moved downloads into the
// offscreen document, which is a different origin, may not carry the
// session, and fetched each file only after earlier files finished parsing
// - after Blackboard's time-stamped file links may have expired.
//
// Why bytes are sent as base64 text chunks: chrome.runtime messaging is
// JSON-only. An ArrayBuffer sent through it arrives as {} (verified in a
// real MV3 extension). Strings survive. Chunks keep each message far below
// Chrome's 64 MiB message limit, so large textbooks work. background.js
// writes each chunk to the IndexedDB "staging" store; the offscreen parser
// reads them from there. No ArrayBuffer ever crosses a message.

(function (root) {
  "use strict";

  const CHUNK_BYTES = 8 * 1024 * 1024; // raw bytes per message (~10.7 MB as base64)

  function fetchError(reason) {
    const err = new Error(reason);
    err.stage = "fetch";
    return err;
  }

  // Detects an HTML page served in place of a file (login page, error page,
  // expired link). Checks the first 2 KB for HTML markers anywhere, not just
  // at byte 0 - real error pages can start with whitespace, a BOM, or a
  // comment, which the previous "starts with <html" check missed.
  function looksLikeHtml(bytes) {
    const head = new TextDecoder("utf-8").decode(bytes.subarray(0, 2048)).toLowerCase();
    return /<!doctype html|<html[\s>]|<head[\s>]|<body[\s>]/.test(head);
  }

  function toBase64(u8) {
    let binary = "";
    const STEP = 0x8000;
    for (let i = 0; i < u8.length; i += STEP) {
      binary += String.fromCharCode.apply(null, u8.subarray(i, i + STEP));
    }
    return btoa(binary);
  }

  // Sends bytes to the extension as base64 chunks. Returns the handle the
  // offscreen parser needs to read them back.
  async function stageBytes(itemId, u8) {
    const stageKey = `${itemId}::${Date.now()}::${Math.random().toString(36).slice(2)}`;
    const chunks = Math.max(1, Math.ceil(u8.byteLength / CHUNK_BYTES));
    for (let index = 0; index < chunks; index++) {
      const data = toBase64(u8.subarray(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES));
      let reply;
      try {
        reply = await chrome.runtime.sendMessage({ type: "BBX_STAGE_CHUNK", stageKey, index, data });
      } catch (err) {
        const e = new Error(`staging-failed: ${err.message}`);
        e.stage = "stage";
        throw e;
      }
      if (!reply?.ok) {
        const e = new Error(`staging-failed: ${reply?.error || "no response from background"}`);
        e.stage = "stage";
        throw e;
      }
    }
    return { stageKey, chunks, byteLength: u8.byteLength };
  }

  // Downloads one Blackboard file from this tab and stages it.
  //
  // credentials: "same-origin" (NOT "include"): Blackboard answers with a
  // redirect to a signed S3 URL that sends Access-Control-Allow-Origin: *.
  // A wildcard is illegal with "include" and the browser blocks the
  // response. "same-origin" sends the session cookie to Blackboard and drops
  // it on the S3 hop, where it isn't needed (verified in real Chromium).
  async function fetchAndStage(job) {
    let res;
    try {
      res = await fetch(job.url, { credentials: "same-origin" });
    } catch (err) {
      throw fetchError(`fetch-failed: ${err.message}`);
    }
    if (!res.ok) throw fetchError(`fetch-failed: HTTP ${res.status}`);

    const contentType = (res.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    const u8 = new Uint8Array(await res.arrayBuffer());
    if (!u8.byteLength) throw fetchError("fetch-failed: file was empty (0 bytes)");

    const expectsMarkup = job.sourceType === "html" || job.sourceType === "text";
    if (!expectsMarkup && (contentType === "text/html" || looksLikeHtml(u8))) {
      const snippet = new TextDecoder("utf-8").decode(u8.subarray(0, 300)).replace(/\s+/g, " ").trim().slice(0, 120);
      throw fetchError(`fetch-failed: Blackboard sent a web page instead of the file (HTTP ${res.status}, starts: "${snippet}")`);
    }

    const staged = await stageBytes(job.itemId, u8);
    const mimeType = (contentType && contentType !== "application/octet-stream") ? contentType : "";
    return { ...staged, mimeType };
  }

  // Download a file's bytes WITHOUT staging them (same fetch + HTML guard as
  // fetchAndStage). Used to hand a PDF's raw bytes to the backend extractor,
  // which is far stronger than the in-browser parser and can OCR scans.
  async function fetchRaw(job) {
    let res;
    try {
      res = await fetch(job.url, { credentials: "same-origin" });
    } catch (err) {
      throw fetchError(`fetch-failed: ${err.message}`);
    }
    if (!res.ok) throw fetchError(`fetch-failed: HTTP ${res.status}`);
    const contentType = (res.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    const u8 = new Uint8Array(await res.arrayBuffer());
    if (!u8.byteLength) throw fetchError("fetch-failed: file was empty (0 bytes)");
    const expectsMarkup = job.sourceType === "html" || job.sourceType === "text";
    if (!expectsMarkup && (contentType === "text/html" || looksLikeHtml(u8))) {
      throw fetchError("fetch-failed: Blackboard sent a web page instead of the file");
    }
    return { u8, mimeType: (contentType && contentType !== "application/octet-stream") ? contentType : "" };
  }

  // Standalone File items (resource/x-bb-file) have NO download address in
  // Blackboard's public API - only fileName and mimeType. Ultra's own
  // internal folder listing does: each file result carries
  //   contentDetail["resource/x-bb-file"].file.permanentUrl
  //   = "/bbcswebdav/pid-...-dt-content-rid-..._1/xid-..._1"
  // (captured from W&M's Blackboard, 4000.21). This lists one folder the
  // same way Ultra does, following paging.nextPage, and returns
  // contentId -> { url, mimeType, fileName }.
  // includeInActivityTracking=false: Ultra's own flag, so listing doesn't
  // mark anything as viewed.
  // Lists one container's children through Ultra's internal listing,
  // following paging.nextPage. `onRequest` is called once per HTTP request
  // (used to enforce a request budget).
  async function listChildren(origin, courseId, parentId, onRequest) {
    const results = [];
    let path = `/learn/api/v1/courses/${encodeURIComponent(courseId)}/contents/${encodeURIComponent(parentId)}` +
      `/children?@view=Summary&limit=100&includeInActivityTracking=false`;
    for (let page = 0; path && page < 50; page++) {
      onRequest?.();
      const res = await fetch(new URL(path, origin).href, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!res.ok) {
        const err = new Error(`folder listing returned HTTP ${res.status}`);
        err.status = res.status;
        throw err;
      }
      const data = await res.json();
      results.push(...(Array.isArray(data?.results) ? data.results : []));
      path = data?.paging?.nextPage || "";
    }
    return results;
  }

  function handlerOf(item) {
    return typeof item?.contentHandler === "string" ? item.contentHandler : firstString(item?.contentHandler?.id);
  }

  function firstString(v) { return typeof v === "string" ? v : ""; }

  async function resolvePermanentUrls(origin, courseId, parentId) {
    const found = new Map();
    for (const item of await listChildren(origin, courseId, parentId)) {
      const file = item?.contentDetail?.["resource/x-bb-file"]?.file;
      if (item?.id && file?.permanentUrl) {
        found.set(item.id, {
          url: new URL(file.permanentUrl, origin).href,
          mimeType: file.mimeType || "",
          fileName: file.fileName || ""
        });
      }
    }
    return found;
  }

  // One content item via the public API (verified 200 on W&M's Blackboard:
  // /learn/api/public/v1/courses/_40318_1/contents/_3618508_1).
  async function getContentItem(origin, courseId, contentId, onRequest) {
    onRequest?.();
    const res = await fetch(new URL(`/learn/api/public/v1/courses/${encodeURIComponent(courseId)}/contents/${encodeURIComponent(contentId)}`, origin).href,
      { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!res.ok) {
      const err = new Error(`content lookup returned HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  // A body field may be a string (public API) or { rawText, displayText }
  // (Ultra's internal listing). Returns HTML/text or "".
  function bodyHtmlOf(item) {
    const b = item?.body;
    if (typeof b === "string") return b;
    if (b && typeof b === "object") return firstString(b.displayText) || firstString(b.rawText);
    return "";
  }

  // Independent census of a course: walks every folder / learning module /
  // page from the given start ids using Ultra's internal listing - a
  // separate source from the scanner's outline, so it can reveal items the
  // scanner never saw. Records each page's body HTML; when the listing
  // gives none, it fetches the page itself (public API) for its body.
  //
  // Start ids are derived from the scanner's outline and can include PAGE
  // ids (the scanner records files found inside a page with the page as
  // their parent, even when the page itself isn't in the outline). Listing
  // a page's children returns 400, which is normal - such a start id is
  // looked up individually and recorded as the page it is, not reported
  // as a failed folder (v2.9.5 reported ~30 of these as problems).
  const CONTAINER_HANDLER = /x-bb-(folder|lesson|document)/i;

  async function censusCourse(origin, courseId, rootIds, { maxRequests = 600 } = {}) {
    const items = new Map();
    const errors = [];
    const listed = new Set();
    const queue = rootIds.map((id) => ({ id, kind: "start" }));
    let requests = 0;
    let capReached = false;
    const count = () => { requests++; };

    function record(child, parentId) {
      if (!child?.id || items.has(child.id)) return null;
      const handler = handlerOf(child);
      const file = child?.contentDetail?.["resource/x-bb-file"]?.file;
      const entry = {
        id: child.id,
        parentId: parentId || firstString(child.parentId),
        title: String(child.title || ""),
        handler,
        fileName: file?.fileName || "",
        isFile: Boolean(file),
        bodyHtml: bodyHtmlOf(child)
      };
      items.set(child.id, entry);
      return entry;
    }

    while (queue.length) {
      const { id, kind } = queue.shift();
      if (listed.has(id)) continue;
      if (requests >= maxRequests) { capReached = true; break; }
      listed.add(id);

      let children;
      try {
        children = await listChildren(origin, courseId, id, count);
      } catch (err) {
        if (kind === "page") continue; // a page with no children
        if (kind === "start") {
          // Probably a page, not a folder: identify it instead of failing.
          try {
            const item = await getContentItem(origin, courseId, id, count);
            if (/x-bb-document/i.test(handlerOf(item))) { record(item, firstString(item.parentId)); continue; }
            errors.push({ parentId: id, kind: `start item (${handlerOf(item) || "unknown type"})`, error: err.message });
          } catch (lookupErr) {
            errors.push({ parentId: id, kind: "start item", error: `${err.message}; lookup: ${lookupErr.message}` });
          }
          continue;
        }
        errors.push({ parentId: id, kind, error: err.message });
        continue;
      }

      for (const child of children) {
        const entry = record(child, id);
        if (entry && CONTAINER_HANDLER.test(entry.handler)) {
          queue.push({ id: entry.id, kind: /document/i.test(entry.handler) ? "page" : "folder" });
        }
      }
    }

    // Pages whose listing entry carried no body: fetch the page itself.
    for (const entry of items.values()) {
      if (!/x-bb-document/i.test(entry.handler) || entry.bodyHtml) continue;
      if (requests >= maxRequests) { capReached = true; break; }
      try {
        const item = await getContentItem(origin, courseId, entry.id, count);
        entry.bodyHtml = bodyHtmlOf(item) || firstString(item?.description);
        entry.bodySource = "item";
      } catch (err) {
        entry.bodyError = err.message;
      }
    }

    return { items: [...items.values()], requests, capReached, errors, containersListed: listed.size };
  }

  root.BBStage = { fetchAndStage, fetchRaw, stageBytes, toBase64, listChildren, getContentItem, resolvePermanentUrls, censusCourse, looksLikeHtml, CHUNK_BYTES };
})(typeof globalThis !== "undefined" ? globalThis : this);

// lib/ir.js
//
// Shared "intermediate representation" (IR) for anything BB Plus ingests for
// local AI study features: Blackboard documents, PDFs, DOCX, PPTX, and
// manually-uploaded files.
//
// Design goal: one structural format, not one file format. We never convert
// a source into another *file* format (e.g. HTML -> PDF or PDF -> plain
// text). Instead every source is decomposed into an ordered list of typed
// "blocks" that all downstream consumers (schedule extraction, Q&A, practice
// problems) read the same way. Nothing is thrown away: tables stay tables,
// images stay images (referenced by hash, not inlined), math stays math
// (verbatim LaTeX/MathML when the source has it, or an image block when it
// doesn't).
//
// This file has no DOM dependency and no external libraries, so it loads
// safely in every context BB Plus runs code in: the isolated-world content
// script, the background service worker, and the offscreen document.
// Attach to globalThis rather than `window` for exactly that reason.

(function (root) {
  "use strict";

  // ---- Block constructors -------------------------------------------------
  // Every block is a plain JSON-serializable object with a `type` tag.
  // `page` (PDF), `slide` (PPTX) or `order` (HTML/DOCX) is carried where
  // available so a consumer can reconstruct position without re-parsing.

  function headingBlock(level, text, extra = {}) {
    return { type: "heading", level: clampLevel(level), text: cleanText(text), ...extra };
  }

  function paragraphBlock(text, extra = {}) {
    return { type: "paragraph", text: cleanText(text), ...extra };
  }

  function listBlock(items, ordered = false, extra = {}) {
    return { type: "list", ordered: Boolean(ordered), items: (items || []).map(cleanText), ...extra };
  }

  function tableBlock(rows, extra = {}) {
    return {
      type: "table",
      rows: (rows || []).map((row) => (row || []).map(cleanText)),
      ...extra
    };
  }

  // `assetId` refers to a row in the `assets` object store (see lib/db.js),
  // keyed by content hash. Blocks never carry raw bytes themselves so the
  // same figure reused across a textbook is only ever stored once.
  function imageBlock(assetId, extra = {}) {
    return { type: "image", assetId, caption: extra.caption ? cleanText(extra.caption) : "", ...extra };
  }

  // `confidence: "source"` means the LaTeX/MathML came from the document
  // itself (docx math, a professor's raw notes) and is exact. Never
  // synthesize a `"source"` confidence value from OCR or guesswork — if the
  // math had to be reconstructed rather than read, represent it as an
  // `imageBlock` instead and let a vision-capable model read it at query
  // time. See README "Math and figures" for the reasoning.
  function mathBlock(latex, extra = {}) {
    return { type: "math", latex: String(latex || ""), confidence: extra.confidence || "source", ...extra };
  }

  function codeBlock(text, extra = {}) {
    return { type: "code", text: String(text ?? ""), language: extra.language || "", ...extra };
  }

  // Used when a source couldn't be parsed at all (e.g. a PDF/DOCX/PPTX
  // arriving before the matching parser library has been vendored in, or a
  // genuinely corrupt file). This block flows into the exact same
  // "unresolved" reporting path as a file with no downloadUrl, so the user
  // gets one consistent "we couldn't fully process these" surface rather
  // than two.
  function unparsedBlock(reason, extra = {}) {
    return { type: "unparsed", reason: String(reason || "unknown"), ...extra };
  }

  function clampLevel(level) {
    const n = Number(level);
    if (!Number.isFinite(n)) return 2;
    return Math.min(6, Math.max(1, Math.round(n)));
  }

  function cleanText(value) {
    return String(value ?? "").replace(/\s+/g, " ").trim();
  }

  // ---- Document envelope ---------------------------------------------------
  // The unit stored in the `documents` object store (see lib/db.js). One per
  // ingested item (a Blackboard file/document, or a manually uploaded file).

  function makeDocument({ itemId, courseId, courseName, title, sourceType, sourceHash, blocks, warnings }) {
    return {
      schemaVersion: 1,
      itemId: String(itemId || ""),
      courseId: String(courseId || ""),
      courseName: String(courseName || ""),
      title: String(title || ""),
      sourceType: String(sourceType || "unknown"), // "html" | "pdf" | "docx" | "pptx" | "upload"
      sourceHash: String(sourceHash || ""),
      ingestedAt: new Date().toISOString(),
      warnings: Array.isArray(warnings) ? warnings : [],
      blocks: Array.isArray(blocks) ? blocks : []
    };
  }

  // Cheap, hash-stable text view used for keyword search and for sending a
  // whole document's prose to a model. Deliberately drops images/tables
  // structure-wise but keeps table *contents* readable, since a model
  // reading a flattened table is usually fine, whereas losing it entirely
  // for a schedule/grading table would not be.
  function flattenToText(doc) {
    const lines = [];
    for (const b of doc.blocks || []) {
      switch (b.type) {
        case "heading":
          lines.push(`${"#".repeat(b.level)} ${b.text}`);
          break;
        case "paragraph":
          if (b.text) lines.push(b.text);
          break;
        case "list":
          for (const item of b.items || []) lines.push(`- ${item}`);
          break;
        case "table":
          for (const row of b.rows || []) lines.push(row.join(" | "));
          break;
        case "math":
          lines.push(`$${b.latex}$`);
          break;
        case "code":
          lines.push(b.text);
          break;
        case "image":
          lines.push(`[figure${b.caption ? ": " + b.caption : ""}]`);
          break;
        default:
          break;
      }
    }
    return lines.join("\n");
  }

  // SHA-256 over an ArrayBuffer or string, used as `sourceHash` (cache
  // invalidation key: if a file on Blackboard is replaced, its hash
  // changes and we re-ingest instead of serving a stale cached parse) and
  // as the `assets` store key (so an identical figure reused across a
  // textbook is only ever stored once).
  async function hash(data) {
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  root.BBIR = {
    heading: headingBlock,
    paragraph: paragraphBlock,
    list: listBlock,
    table: tableBlock,
    image: imageBlock,
    math: mathBlock,
    code: codeBlock,
    unparsed: unparsedBlock,
    makeDocument,
    flattenToText,
    hash,
    cleanText
  };
})(typeof globalThis !== "undefined" ? globalThis : this);

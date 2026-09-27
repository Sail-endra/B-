// offscreen.js
//
// Runs inside offscreen.html - a hidden, DOM-capable extension page
// (chrome.offscreen). This is the ONE place in the extension that turns raw
// bytes into IR blocks (see lib/ir.js). It is stateless: it never touches
// IndexedDB itself. background.js sends it { sourceType, payload }, it sends
// back { blocks, assets, warnings }, and background.js is the only writer to
// the study library database. That split means this document can be closed
// between ingestion runs without losing anything.
//
// Every parser here degrades instead of throwing: if a needed vendor
// library isn't present, or a figure can't be fetched, the missing piece
// becomes an `unparsed`/warning entry rather than failing the whole item.
// That's what lets the UI show one consistent "we couldn't fully process
// these" list regardless of *why* something didn't make it in.

(() => {
  "use strict";

  // ---- Surface otherwise-invisible errors ------------------------------
  //
  // chrome://inspect/#other only lists this document while it happens to
  // exist, and background.js creates/destroys it per ingestion run - so
  // catching it in time is a race. Anything that throws *outside* the
  // per-parse try/catch below (most importantly: a vendored library like
  // pdf.min.js hitting the extension-page CSP - e.g. an eval()/new Function()
  // call from an older UMD build - while it's still just loading as a
  // <script> tag) would otherwise be a silent, unrecoverable failure with
  // no console to see it in. Forwarding to background.js's console means it
  // shows up in the persistent service-worker inspector instead (chrome://
  // extensions -> "service worker"), which doesn't have that timing problem.

  function reportOffscreenError(payload) {
    chrome.runtime.sendMessage({ type: "BBX_OFFSCREEN_ERROR", ...payload }).catch(() => {});
  }

  window.addEventListener("error", (event) => {
    reportOffscreenError({
      message: event.message,
      source: event.filename,
      line: event.lineno,
      col: event.colno,
      stack: event.error?.stack
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    reportOffscreenError({
      message: event.reason?.message || String(event.reason),
      stack: event.reason?.stack
    });
  });

  // ---- HTML -> blocks -------------------------------------------------
  //
  // Used for two sources: Blackboard "document" items (their body is
  // already HTML/BBML) and DOCX files after mammoth converts them to HTML.
  // Both funnel through here so there is exactly one HTML->IR implementation
  // to maintain.

  async function htmlToBlocks(markup, warnings) {
    const doc = new DOMParser().parseFromString(markup, "text/html");
    const blocks = [];
    await walk(doc.body, blocks, warnings);
    return blocks;
  }

  // Walks childNodes (not just element children) so that NO visible text is
  // dropped. Before v2.9.4 this only looked at element children and only
  // turned p/div/h*/li/table/pre into blocks, so text sitting directly in
  // <body>, or inside <font>/<center>/<span>/<br> layouts (common in
  // exported .html files), silently vanished - e.g. "UNSAFE_HTML_*.html"
  // produced zero blocks. Inline content now accumulates into a paragraph
  // that is flushed at every block boundary.
  const SKIP_TAGS = new Set(["script", "style", "noscript", "template", "head", "title", "meta", "link", "svg", "iframe", "object", "button", "input", "select", "textarea"]);
  const BLOCK_SELECTOR = "p,div,ul,ol,table,img,pre,h1,h2,h3,h4,h5,h6,blockquote,section,article,header,footer,main,aside,center,hr,dl,figure";

  async function walk(node, blocks, warnings) {
    let inline = [];
    const flushInline = () => {
      const text = inline.join("").replace(/[ \t\f\v\u00a0]+/g, " ").replace(/\s*\n\s*/g, "\n").trim();
      if (text) blocks.push(BBIR.paragraph(text));
      inline = [];
    };

    for (const child of Array.from(node.childNodes || [])) {
      if (child.nodeType === 3) { inline.push(child.textContent); continue; } // text node
      if (child.nodeType !== 1) continue;                                    // comments etc.
      const tag = child.tagName.toLowerCase();
      if (SKIP_TAGS.has(tag)) continue;
      if (tag === "br") { inline.push("\n"); continue; }

      // Inline wrapper (span, a, b, font, ...) with no block content inside:
      // part of the current paragraph.
      const isStructural = /^(h[1-6]|p|div|ul|ol|table|pre|code|img|blockquote|section|article|header|footer|main|aside|center|hr|dl|figure|li)$/.test(tag);
      if (!isStructural && !child.querySelector(BLOCK_SELECTOR)) {
        inline.push(child.textContent);
        continue;
      }

      flushInline();

      if (/^h[1-6]$/.test(tag)) {
        const text = child.textContent;
        if (text.trim()) blocks.push(BBIR.heading(Number(tag[1]), text));
        continue;
      }
      if (tag === "ul" || tag === "ol") {
        const items = Array.from(child.querySelectorAll(":scope > li")).map((li) => li.textContent).filter((t) => t.trim());
        if (items.length) blocks.push(BBIR.list(items, tag === "ol"));
        continue;
      }
      if (tag === "table") {
        const rows = Array.from(child.querySelectorAll("tr")).map((tr) =>
          Array.from(tr.querySelectorAll("td, th")).map((cell) => cell.textContent)
        );
        if (rows.length) blocks.push(BBIR.table(rows));
        continue;
      }
      if (tag === "pre" || tag === "code") {
        if (child.textContent.trim()) blocks.push(BBIR.code(child.textContent));
        continue;
      }
      if (tag === "img") {
        await handleImg(child, blocks, warnings);
        continue;
      }
      if (tag === "hr") continue;

      // p, div, center, section, li, ... - recurse; its inline text becomes
      // its own paragraph(s) and nested blocks keep their structure.
      await walk(child, blocks, warnings);
    }
    flushInline();
  }

  async function handleImg(img, blocks, warnings) {
    const src = img.getAttribute("src") || "";
    const caption = img.getAttribute("alt") || img.getAttribute("title") || "";

    if (src.startsWith("bbir-asset://")) {
      // Already hashed and queued by parseDocx's image handler below.
      blocks.push(BBIR.image(src.slice("bbir-asset://".length), { caption }));
      return;
    }

    if (!/^https?:/i.test(src)) {
      warnings.push(`Skipped an embedded image with an unsupported source (${src.slice(0, 60)}).`);
      return;
    }

    try {
      // Runs in the offscreen document, so this needs the site's host
      // permission (already required for content-script access) for
      // cookies to be attached; Blackboard-hosted figure URLs on an
      // authenticated session resolve the same way a normal in-page
      // <img> would.
      const res = await fetch(src, { credentials: "same-origin" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const bytes = await res.arrayBuffer();
      const hash = await BBIR.hash(bytes);
      pendingAssets.push({ hash, bytes, mimeType: res.headers.get("content-type") || "" });
      blocks.push(BBIR.image(hash, { caption }));
    } catch (err) {
      warnings.push(`Could not fetch a figure (${src.slice(0, 80)}): ${err.message}`);
      blocks.push(BBIR.unparsed("image-fetch-failed", { caption, originalUrl: src }));
    }
  }

  // Assets discovered mid-parse accumulate here and get flushed back to
  // background.js alongside the blocks for a single parse call. Reset per
  // call in handleParse() below.
  let pendingAssets = [];

  // ---- PDF -> blocks --------------------------------------------------
  //
  // Requires vendor/pdf.min.js + vendor/pdf.worker.min.js (see
  // vendor/README.md). Text is extracted per page with a lightweight
  // heading heuristic (short line + notably larger font than the page's
  // median counts as a heading). This is intentionally not
  // typesetting-accurate - it's tuned to be good enough for search and
  // schedule/Q&A grounding, not to reproduce the PDF's layout.
  //
  // Pages are only rasterized into an image block when pdf.js's operator
  // list shows an actual embedded image paint op on that page. That keeps a
  // 400-page text-only textbook cheap while still preserving every chart,
  // diagram, or scanned figure at full visual fidelity - the thing a naive
  // "PDF -> plain text" conversion would have thrown away.

  async function parsePdf(arrayBuffer, warnings) {
    if (typeof pdfjsLib === "undefined") {
      warnings.push("PDF parsing library not vendored - see vendor/README.md.");
      return [BBIR.unparsed("missing-vendor-library:pdfjs")];
    }
    if (pdfjsLib.GlobalWorkerOptions && !pdfjsLib.GlobalWorkerOptions.workerSrc) {
      pdfjsLib.GlobalWorkerOptions.workerSrc = "vendor/pdf.worker.min.js";
    }

    const blocks = [];
    // isEvalSupported: false stops pdf.js from compiling font glyphs with
    // new Function(), which the extension-page CSP forbids (it would throw
    // mid-render on some PDFs, not at load time).
    const loadingTask = pdfjsLib.getDocument({ data: arrayBuffer, isEvalSupported: false });
    const pdf = await loadingTask.promise;
    const myParse = parseGeneration;
    let pagesRasterized = 0;
    let pagesWithText = 0;

    try {
    for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
      // Stop promptly if this parse was abandoned (timed out), instead of
      // grinding on in the background and competing with the next file.
      if (myParse !== parseGeneration) throw new Error("cancelled");
      const page = await pdf.getPage(pageNum);

      // --- text, with a naive heading heuristic ---
      const textContent = await page.getTextContent();
      const lines = groupItemsIntoLines(textContent.items);
      const medianSize = median(lines.map((l) => l.fontSize).filter(Boolean)) || 10;

      let currentParagraph = [];
      const flush = () => {
        if (currentParagraph.length) {
          blocks.push(BBIR.paragraph(currentParagraph.join(" "), { page: pageNum }));
          currentParagraph = [];
        }
      };

      for (const line of lines) {
        const text = line.text.trim();
        if (!text) { flush(); continue; }
        const isHeadingLike = line.fontSize > medianSize * 1.3 && text.length < 90;
        if (isHeadingLike) {
          flush();
          blocks.push(BBIR.heading(text.length < 40 ? 2 : 3, text, { page: pageNum }));
        } else {
          currentParagraph.push(text);
        }
      }
      flush();
      if (lines.some((l) => l.text.trim())) pagesWithText++;

      // --- page image, only if this page actually paints an image ---
      // Bounded: a scanned textbook paints an image on EVERY page, and
      // rendering hundreds of full pages made big PDFs run past the time
      // limit. The first MAX_PAGE_IMAGES figure pages are kept; the rest are
      // noted in warnings (the table-of-contents work will fetch specific
      // pages on demand instead).
      if (pagesRasterized >= MAX_PAGE_IMAGES) continue;
      try {
        const opList = await page.getOperatorList();
        if (pageHasImageOps(opList)) {
          pagesRasterized++;
          const viewport = page.getViewport({ scale: 1.5 });
          const canvas = document.createElement("canvas");
          canvas.width = viewport.width;
          canvas.height = viewport.height;
          const ctx = canvas.getContext("2d");
          // intent: "print" - the default "display" intent schedules
          // rendering with requestAnimationFrame, which never fires in a
          // hidden offscreen document, so render() hung forever on any page
          // with a figure (verified in the real extension).
          await page.render({ canvasContext: ctx, viewport, intent: "print" }).promise;
          const bytes = await canvasToArrayBuffer(canvas);
          const hash = await BBIR.hash(bytes);
          pendingAssets.push({ hash, bytes, mimeType: "image/png" });
          blocks.push(BBIR.image(hash, { page: pageNum, caption: `Page ${pageNum} figure(s)` }));
        }
      } catch (err) {
        warnings.push(`Could not rasterize page ${pageNum} for figure preservation: ${err.message}`);
      }
    }
    } finally {
      loadingTask.destroy(); // release pdf.js worker memory for this document
    }

    if (pagesRasterized >= MAX_PAGE_IMAGES) {
      warnings.push(`Only the first ${MAX_PAGE_IMAGES} pages containing figures were saved as images.`);
    }
    if (!pagesWithText) {
      // No text layer at all: a scanned/image-only PDF. Say so explicitly
      // instead of letting it look like a generic parse failure.
      warnings.push("This PDF has no text layer (likely a scan); only page images were saved.");
      if (!blocks.length) blocks.push(BBIR.unparsed("scanned-pdf-no-text"));
    }

    return blocks;
  }

  function groupItemsIntoLines(items) {
    const lines = [];
    let current = { text: "", fontSize: 0, sizes: [] };
    for (const item of items) {
      const size = Math.abs(item.transform?.[0]) || 0;
      current.text += (current.text ? " " : "") + item.str;
      if (size) current.sizes.push(size);
      if (item.hasEOL) {
        current.fontSize = median(current.sizes) || 0;
        lines.push(current);
        current = { text: "", fontSize: 0, sizes: [] };
      }
    }
    if (current.text.trim()) {
      current.fontSize = median(current.sizes) || 0;
      lines.push(current);
    }
    return lines;
  }

  const MAX_PAGE_IMAGES = 40;

  function pageHasImageOps(opList) {
    const OPS = pdfjsLib.OPS;
    const imageOps = new Set([OPS.paintImageXObject, OPS.paintInlineImageXObject, OPS.paintImageMaskXObject].filter(Boolean));
    return opList.fnArray.some((fn) => imageOps.has(fn));
  }

  function canvasToArrayBuffer(canvas) {
    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (!blob) return reject(new Error("canvas.toBlob failed"));
        blob.arrayBuffer().then(resolve, reject);
      }, "image/png");
    });
  }

  function median(nums) {
    if (!nums.length) return 0;
    const sorted = [...nums].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  }

  // ---- DOCX -> blocks ---------------------------------------------------
  //
  // Requires vendor/mammoth.browser.min.js (see vendor/README.md). Mammoth
  // converts DOCX -> HTML; we intercept its image handling so images become
  // content-addressed assets instead of inline base64, then run the result
  // through the same htmlToBlocks() used for Blackboard documents.
  //
  // Math: mammoth does not convert OOXML math (<m:oMath>) to MathML/LaTeX,
  // so equations built with Word's equation editor currently fall back to
  // whatever mammoth already does with them (usually a placeholder or
  // dropped run) rather than being silently invented as prose. Anyone who
  // hits this in practice should tell us the specific case - it's the
  // clearest candidate for a dedicated OOXML-math -> LaTeX block later.

  async function parseDocx(arrayBuffer, warnings) {
    if (typeof mammoth === "undefined") {
      warnings.push("DOCX parsing library not vendored - see vendor/README.md.");
      return [BBIR.unparsed("missing-vendor-library:mammoth")];
    }

    const result = await mammoth.convertToHtml(
      { arrayBuffer },
      {
        convertImage: mammoth.images.imgElement(async (image) => {
          try {
            const base64 = await image.read("base64");
            const bytes = base64ToArrayBuffer(base64);
            const hash = await BBIR.hash(bytes);
            pendingAssets.push({ hash, bytes, mimeType: image.contentType || "" });
            return { src: `bbir-asset://${hash}` };
          } catch (err) {
            warnings.push(`Could not extract an embedded DOCX image: ${err.message}`);
            return { src: "" };
          }
        })
      }
    );

    for (const msg of result.messages || []) {
      if (msg.type === "warning") warnings.push(`mammoth: ${msg.message}`);
    }

    return htmlToBlocks(result.value, warnings);
  }

  function base64ToArrayBuffer(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  // ---- PPTX -> blocks ---------------------------------------------------
  //
  // Requires only vendor/jszip.min.js (see vendor/README.md) - a PPTX is a
  // zip of XML parts, so no dedicated "pptx parser" library is needed.
  // Text runs (<a:t>) are grouped by paragraph (<a:p>) per slide; images are
  // resolved through each slide's relationship file so a figure ends up
  // attached to the specific slide that uses it.

  async function parsePptx(arrayBuffer, warnings) {
    if (typeof JSZip === "undefined") {
      warnings.push("PPTX parsing library (JSZip) not vendored - see vendor/README.md.");
      return [BBIR.unparsed("missing-vendor-library:jszip")];
    }

    const zip = await JSZip.loadAsync(arrayBuffer);
    const slideNames = Object.keys(zip.files)
      .filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n))
      .sort((a, b) => slideNumber(a) - slideNumber(b));

    const blocks = [];
    const parser = new DOMParser();

    for (const name of slideNames) {
      const slideNum = slideNumber(name);
      const xmlText = await zip.file(name).async("string");
      const xml = parser.parseFromString(xmlText, "application/xml");

      blocks.push(BBIR.heading(2, `Slide ${slideNum}`, { slide: slideNum }));

      for (const p of Array.from(xml.getElementsByTagName("a:p"))) {
        const runs = Array.from(p.getElementsByTagName("a:t")).map((t) => t.textContent);
        const text = runs.join("").trim();
        if (text) blocks.push(BBIR.paragraph(text, { slide: slideNum }));
      }

      await attachSlideImages(zip, name, slideNum, xml, blocks, warnings);
    }

    return blocks;
  }

  function slideNumber(path) {
    const m = path.match(/slide(\d+)\.xml$/);
    return m ? Number(m[1]) : 0;
  }

  async function attachSlideImages(zip, slidePath, slideNum, xml, blocks, warnings) {
    const relsPath = slidePath.replace("slides/", "slides/_rels/") + ".rels";
    const relsFile = zip.file(relsPath);
    if (!relsFile) return;

    const relsXml = new DOMParser().parseFromString(await relsFile.async("string"), "application/xml");
    const targetsById = new Map();
    for (const rel of Array.from(relsXml.getElementsByTagName("Relationship"))) {
      targetsById.set(rel.getAttribute("Id"), rel.getAttribute("Target"));
    }

    const blipIds = Array.from(xml.getElementsByTagName("a:blip"))
      .map((blip) => blip.getAttribute("r:embed"))
      .filter(Boolean);

    for (const rid of blipIds) {
      const target = targetsById.get(rid);
      if (!target) continue;
      const mediaPath = new URL(target, "zip:///ppt/slides/x.xml").pathname.replace(/^\//, "");
      const mediaFile = zip.file(mediaPath);
      if (!mediaFile) {
        warnings.push(`Slide ${slideNum} referenced an image not found in the archive: ${target}`);
        continue;
      }
      try {
        const bytes = await mediaFile.async("arraybuffer");
        const hash = await BBIR.hash(bytes);
        pendingAssets.push({ hash, bytes, mimeType: mimeTypeForExt(mediaPath) });
        blocks.push(BBIR.image(hash, { slide: slideNum, caption: `Slide ${slideNum} image` }));
      } catch (err) {
        warnings.push(`Could not read slide ${slideNum} image (${target}): ${err.message}`);
      }
    }
  }

  function mimeTypeForExt(path) {
    const ext = (path.split(".").pop() || "").toLowerCase();
    return { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", bmp: "image/bmp", emf: "image/x-emf" }[ext] || "application/octet-stream";
  }

  // ---- Plain text / source code -> blocks ------------------------------
  //
  // Code and notes are kept verbatim in a single code block: splitting a
  // .py file into "paragraphs" would destroy indentation, which is meaning.

  function parseText(text, warnings) {
    const body = String(text || "");
    if (!body.trim()) {
      warnings.push("File was empty.");
      return [BBIR.unparsed("empty-file")];
    }
    return [BBIR.code(body)];
  }

  // ---- Standalone images -> blocks --------------------------------------
  //
  // Stored as a content-addressed asset exactly like figures inside PDFs,
  // so a vision-capable model can be handed the original pixels later.

  async function parseImage(arrayBuffer, mimeType) {
    const hash = await BBIR.hash(arrayBuffer);
    pendingAssets.push({ hash, bytes: arrayBuffer, mimeType: mimeType || "application/octet-stream" });
    return [BBIR.image(hash, {})];
  }

  // ---- Ingestion: download -> parse -> store, all in this document -------
  //
  // chrome.runtime messaging is JSON-only: an ArrayBuffer sent through it
  // arrives as {} (verified in a real MV3 extension). So file bytes must
  // never cross a message boundary. Jobs arrive here as JSON (URLs, ids,
  // strings), and this document - which has the extension's host permission
  // for Blackboard - downloads the file itself, parses it, and writes
  // documents + assets straight into IndexedDB (same extension origin as
  // background.js, so it is the same database). Only a small JSON result
  // goes back.

  // Bump when parser output changes, so stored documents get re-parsed.
  // v3: everything stored before this fix may hold "{}" in place of bytes.
  const PARSER_VERSION = 4; // v4: HTML walker no longer drops loose/inline text

  class StageError extends Error {
    constructor(stage, reason) { super(reason); this.stage = stage; }
  }

  async function parseSource(sourceType, payload, mimeType, warnings) {
    switch (sourceType) {
      case "html": return htmlToBlocks(payload, warnings);
      case "pdf": return parsePdf(payload, warnings);
      case "docx": return parseDocx(payload, warnings);
      case "pptx": return parsePptx(payload, warnings);
      case "text": return parseText(payload, warnings);
      case "image": return parseImage(payload, mimeType);
      default: return [BBIR.unparsed(`unsupported-source-type:${sourceType}`)];
    }
  }

  async function acquirePayload(job) {
    if (job.kind === "markup") {
      return { payload: String(job.markup || ""), mimeType: "text/html" };
    }
    if (job.kind !== "staged") {
      throw new StageError("stage", `unknown job kind: ${job.kind}`);
    }

    // Bytes were downloaded in the Blackboard tab and staged as base64
    // chunks in IndexedDB (see lib/stage.js) - never passed via messaging.
    let parts;
    try {
      parts = await BBDB.getStagingChunks(job.stageKey, job.chunks);
    } catch (err) {
      throw new StageError("stage", `staging-failed: ${err.message}`);
    }
    const decoded = parts.map((b64) => new Uint8Array(base64ToArrayBuffer(b64)));
    const total = decoded.reduce((n, p) => n + p.byteLength, 0);
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const part of decoded) { bytes.set(part, offset); offset += part.byteLength; }
    BBDB.deleteStaging(job.stageKey, job.chunks).catch(() => {});

    if (job.byteLength && total !== job.byteLength) {
      throw new StageError("stage", `staging-failed: expected ${job.byteLength} bytes, got ${total}`);
    }

    const payload = (job.sourceType === "html" || job.sourceType === "text")
      ? new TextDecoder("utf-8").decode(bytes)
      : bytes.buffer;
    return { payload, mimeType: job.mimeType || "" };
  }

  function hasContent(doc) {
    return (doc?.blocks || []).some((b) => b.type !== "unparsed");
  }

  // A parser that never settles must not stall every file queued behind it
  // (parsing is serialized). Budget scales with size: big textbooks get
  // longer, but nothing gets forever.
  function parseTimeoutMs(payload) {
    const bytes = typeof payload === "string" ? payload.length : (payload?.byteLength || 0);
    return Math.min(10 * 60000, 60000 + Math.ceil(bytes / (1024 * 1024)) * 15000);
  }

  // Incremented when a parse is abandoned; long-running parsers check it
  // between pages and stop.
  let parseGeneration = 0;

  function withTimeout(promise, ms) {
    let timer;
    return Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => { parseGeneration++; reject(new Error(`timed out after ${Math.round(ms / 1000)}s`)); }, ms); })
    ]).finally(() => clearTimeout(timer));
  }

  // Parsing is serialized: parsers push extracted figures into the shared
  // `pendingAssets` array, so two concurrent parses would reset/steal each
  // other's assets. Downloads (the slow part) still run concurrently.
  let parseChain = Promise.resolve();
  function serialized(fn) {
    const run = parseChain.then(fn, fn);
    parseChain = run.catch(() => {});
    return run;
  }

  async function ingestJob(job) {
    const base = {
      itemId: job.itemId, courseId: job.courseId, title: job.title,
      courseName: job.courseName, url: job.pageUrl || ""
    };

    try {
      // Every item must have its own storage key. An empty id is how 51
      // files overwrote each other before v2.9.4 - refuse it outright.
      if (!String(job.itemId || "").trim()) {
        throw new StageError("store", "store-failed: item has no id");
      }
      const { payload, mimeType } = await acquirePayload(job);
      const sourceHash = await BBIR.hash(payload);

      const existing = await BBDB.getDocument(job.itemId);
      if (existing && existing.sourceHash === sourceHash && existing.parserVersion === PARSER_VERSION && hasContent(existing)) {
        return { ...base, ok: true, skipped: "unchanged" };
      }

      return await serialized(async () => {
        pendingAssets = [];
        const warnings = [];
        let blocks;
        try {
          blocks = await withTimeout(parseSource(job.sourceType, payload, mimeType, warnings), parseTimeoutMs(payload));
        } catch (err) {
          throw new StageError("parse", `parse-failed: ${err.message}`);
        }
        const assets = pendingAssets;
        pendingAssets = [];

        if (!blocks.some((b) => b.type !== "unparsed")) {
          if (existing) await BBDB.deleteDocument(job.itemId); // drop a stale shell
          const reason = blocks.find((b) => b.type === "unparsed")?.reason || "no-content-extracted";
          return { ...base, ok: false, stage: "parse", reason };
        }

        for (const asset of assets) {
          await BBDB.putAsset(asset.hash, asset.bytes, { mimeType: asset.mimeType });
        }
        const doc = BBIR.makeDocument({
          itemId: job.itemId, courseId: job.courseId, courseName: job.courseName,
          title: job.title, sourceType: job.sourceType, sourceHash, blocks, warnings
        });
        doc.parserVersion = PARSER_VERSION;
        await BBDB.putDocument(doc);
        await BBDB.invalidateDerivedForCourse(job.courseId);

        // Read it back: success means "it is in the database", not "we
        // believe we wrote it".
        const stored = await BBDB.getDocument(job.itemId);
        if (!stored || !hasContent(stored)) {
          return { ...base, ok: false, stage: "store", reason: "store-failed: document not readable after write" };
        }
        return { ...base, ok: true, blockCount: blocks.length, assetCount: assets.length, warnings };
      });
    } catch (err) {
      return { ...base, ok: false, stage: err?.stage || "parse", reason: err?.message || String(err) };
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.target !== "offscreen" || message?.type !== "BBX_INGEST_ONE") return false;
    ingestJob(message.job || {}).then(sendResponse, (err) =>
      sendResponse({ itemId: message.job?.itemId, ok: false, stage: "parse", reason: err?.message || String(err) })
    );
    return true; // keep the channel open for the async response
  });
})();

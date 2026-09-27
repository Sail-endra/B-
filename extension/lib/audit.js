// lib/audit.js - pure comparison logic for "Verify library" (no DOM, no
// chrome APIs, so it can be unit-tested directly). Loaded before content.js.
//
// Verify used to compare the library only against BB Plus's own scan of the
// course. If the scan missed something, the sync and Verify both missed it
// and still reported success. These checks use independent evidence:
//   - a census of the course from Ultra's own folder listing
//   - the file references inside each page's own markup

(function (root) {
  "use strict";

  // Ids of every content item in the scanner's outline, containers included.
  function outlineIds(flatOutline) {
    return new Set(flatOutline.map((item) => String(item?.id || "")).filter(Boolean));
  }

  // Where the census should start: parents of outline items that are not
  // themselves in the outline (normally just the course's root folder).
  function censusRoots(flatOutline) {
    const ids = outlineIds(flatOutline);
    const roots = new Set();
    for (const item of flatOutline) {
      const parent = String(item?.parentId || "");
      if (parent && !ids.has(parent)) roots.add(parent);
    }
    return [...roots];
  }

  // What the census found that the scanner did not.
  //   important: things that carry study content (files, pages, folders)
  //   other:     quizzes, links, tools etc. - listed, but not content
  function missedByScan(censusItems, flatOutline) {
    const ids = outlineIds(flatOutline);
    const missed = censusItems.filter((item) => !ids.has(item.id));
    const isContent = (item) => item.isFile || /x-bb-(file|document|folder|lesson)/i.test(item.handler || "");
    return {
      important: missed.filter(isContent),
      other: missed.filter((item) => !isContent(item))
    };
  }

  function decodeAttr(value) {
    return String(value || "")
      .replace(/&quot;/g, '"').replace(/&#34;/g, '"').replace(/&#39;/g, "'")
      .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  }

  // Files a page's markup refers to. Blackboard references files either by
  // a normal /bbcswebdav/.../xid-N_1 link or by a placeholder such as
  // "@X@EmbeddedFile.requestUrlStub@X@bbcswebdav/xid-N_1", and describes
  // them in a data-bbfile='{"linkName": ...}' attribute. The xid is the
  // file's stable id in both forms.
  function fileRefsFromMarkup(markup) {
    const html = String(markup || "");
    const refs = [];
    const anchorRe = /<a\b[^>]*>/gi;
    let match;
    while ((match = anchorRe.exec(html))) {
      const tag = match[0];
      const href = decodeAttr((tag.match(/\bhref\s*=\s*"([^"]*)"/i) || tag.match(/\bhref\s*=\s*'([^']*)'/i) || [])[1]);
      const rawMeta = (tag.match(/\bdata-bbfile\s*=\s*"([^"]*)"/i) || tag.match(/\bdata-bbfile\s*=\s*'([^']*)'/i) || [])[1];
      let name = "";
      if (rawMeta) {
        try {
          const meta = JSON.parse(decodeAttr(rawMeta));
          name = String(meta.linkName || meta.fileName || meta.alternativeText || "");
        } catch (_) {}
      }
      const xid = (href.match(/xid-\d+_\d+/) || [])[0] || "";
      if (xid || name || /bbcswebdav/i.test(href)) refs.push({ xid, name, href });
    }
    // Also catch xid references outside anchors (e.g. embedded images).
    for (const xid of new Set(html.match(/xid-\d+_\d+/g) || [])) {
      if (!refs.some((r) => r.xid === xid)) refs.push({ xid, name: "", href: "" });
    }
    return refs;
  }

  // Which of a page's file references are NOT in the library. A reference
  // counts as found if a stored document's id contains its xid (page-embedded
  // files are stored as "embedded:<page>:/bbcswebdav/.../xid-N_1") or a
  // stored document's title equals its file name.
  function unmatchedFileRefs(refs, storedDocs) {
    const ids = storedDocs.map((d) => String(d.itemId || ""));
    const titles = new Set(storedDocs.map((d) => String(d.title || "").trim().toLowerCase()).filter(Boolean));
    return refs.filter((ref) => {
      if (ref.xid && ids.some((id) => id.includes(ref.xid))) return false;
      if (ref.name && titles.has(ref.name.trim().toLowerCase())) return false;
      return true;
    });
  }

  // Pages from the census whose body has text (or images) that the scan's
  // own jobs don't already index. Covers pages the scanner never saw and
  // Ultra's "ultraDocumentBody" children, where a page's visible body is
  // actually stored; those are titled after their parent page.
  // `textOf(html)` returns visible text (injected: content.js has a DOM).
  function censusTextJobs(censusItems, outlineJobs, textOf) {
    const indexedByScan = new Set(outlineJobs.filter((j) => j.kind === "markup").map((j) => j.itemId));
    const byId = new Map(censusItems.map((i) => [i.id, i]));
    const scanTitle = new Map(outlineJobs.map((j) => [j.itemId, j.title]));
    const jobs = [];
    for (const item of censusItems) {
      if (!/x-bb-document/i.test(item.handler || "") || indexedByScan.has(item.id)) continue;
      const html = String(item.bodyHtml || "");
      const text = textOf(html);
      if (!text && !/<img[\s>]/i.test(html)) continue;
      let title = item.title;
      if (!title || title === "ultraDocumentBody") {
        title = byId.get(item.parentId)?.title || scanTitle.get(item.parentId) || "Untitled page";
      }
      jobs.push({ itemId: item.id, parentId: item.parentId, title, markup: html, textChars: text.length });
    }
    return jobs;
  }

  // Accounts for every census item the scan did not see. Returns
  // { problems: [text], notes: [text] }. An item is fine if:
  //   - it is in the library with content, or
  //   - it is a page/body with no text of its own, and every file its markup
  //     links is in the library, or
  //   - it is a folder / learning module (its contents are checked as items).
  // Anything else that carries study content is a problem.
  function censusCoverage(censusItems, flatOutline, storedDocs, textOf) {
    const scanIds = outlineIds(flatOutline);
    const withContent = storedDocs.filter((d) => d.contentful);
    const storedIds = new Set(withContent.map((d) => d.itemId));
    const problems = [];
    const notes = [];
    const titleById = new Map(censusItems.map((i) => [i.id, i.title]));
    for (const item of censusItems) {
      if (scanIds.has(item.id) || storedIds.has(item.id)) continue;
      const handler = item.handler || "";
      const label = item.title === "ultraDocumentBody"
        ? `"${titleById.get(item.parentId) || item.parentId}" (its body)`
        : `"${item.title}"`;
      if (item.isFile) {
        problems.push(`File ${label}${item.fileName ? ` (${item.fileName})` : ""} is on Blackboard but not in the library.`);
      } else if (/x-bb-document/i.test(handler)) {
        const html = String(item.bodyHtml || "");
        if (item.bodyError && !html) {
          problems.push(`Page ${label}: could not read its body (${item.bodyError}).`);
          continue;
        }
        if (textOf(html) || /<img[\s>]/i.test(html)) {
          problems.push(`Page ${label} has text that is not in the library.`);
          continue;
        }
        const missingRefs = unmatchedFileRefs(fileRefsFromMarkup(html), withContent);
        for (const ref of missingRefs) problems.push(`Page ${label} links a file that is not in the library: ${ref.name || ref.xid || ref.href}`);
        if (!missingRefs.length) notes.push(`Page ${label}: no text of its own; its linked files are in the library.`);
      } else if (/x-bb-(folder|lesson)/i.test(handler)) {
        notes.push(`Folder ${label} was not in the scan; its contents are checked individually.`);
      } else {
        notes.push(`Not study content: ${label} [${handler}]`);
      }
    }
    return { problems, notes };
  }

  root.BBAudit = { outlineIds, censusRoots, missedByScan, fileRefsFromMarkup, unmatchedFileRefs, censusTextJobs, censusCoverage };
})(typeof globalThis !== "undefined" ? globalThis : this);

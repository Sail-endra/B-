// lib/work.js - small shared helpers for bounded extension work.
(function (root) {
  "use strict";

  async function mapLimit(items, concurrency, worker) {
    const input = Array.from(items || []);
    const results = new Array(input.length);
    let cursor = 0;
    const limit = Math.max(1, Math.min(input.length || 1, Number(concurrency) || 1));

    async function run() {
      while (true) {
        const index = cursor++;
        if (index >= input.length) return;
        results[index] = await worker(input[index], index);
      }
    }

    await Promise.all(Array.from({ length: limit }, run));
    return results;
  }

  function createSemaphore(concurrency) {
    const limit = Math.max(1, Number(concurrency) || 1);
    let active = 0;
    const waiters = [];
    return async function withSlot(work) {
      if (active >= limit) await new Promise((resolve) => waiters.push(resolve));
      else active += 1;
      try {
        return await work();
      } finally {
        const next = waiters.shift();
        if (next) next();
        else active -= 1;
      }
    };
  }

  function normalize(value) {
    return String(value || "")
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  const QUERY_STOP_WORDS = new Set(["about", "after", "before", "does", "explain", "from", "give", "into", "that", "them", "there", "this", "what", "when", "where", "which", "with", "would", "your"]);

  function queryRelevance(query, candidate) {
    const terms = [...new Set(normalize(query).split(" ").filter((term) => term.length > 2 && !QUERY_STOP_WORDS.has(term)))];
    if (!terms.length) return 0;
    const text = normalize(candidate);
    const textTerms = new Set(text.split(" "));
    const overlap = terms.reduce((score, term) => score + (textTerms.has(term) ? 1 : 0), 0);
    const phrase = normalize(query);
    return overlap + (phrase.length > 5 && text.includes(phrase) ? terms.length : 0);
  }

  function matchCourse(record, courses) {
    const code = normalize(record?.courseCode);
    const title = normalize(record?.displayName);
    const term = normalize(record?.termName);
    const list = Array.from(courses || []);
    const termMatches = (course) => !term || !normalize(course?.term) || term === normalize(course.term);

    let matches = code
      ? list.filter((course) => normalize(course?.code) === code && termMatches(course))
      : [];
    if (matches.length === 1) return matches[0];
    if (matches.length > 1) return null;

    matches = title
      ? list.filter((course) => normalize(course?.title) === title && termMatches(course))
      : [];
    return matches.length === 1 ? matches[0] : null;
  }

  const api = { mapLimit, createSemaphore, normalize, queryRelevance, matchCourse };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.BBCourseWork = api;
})(typeof globalThis !== "undefined" ? globalThis : self);

(() => {
  const OUTLINE_CACHE_VERSION = 5;
  if (window.__BBX_CONTENT_INSTALLED__) return;
  window.__BBX_CONTENT_INSTALLED__ = true;

  const STORAGE_KEY = `bbx_data::${location.origin}`;
  // Developer diagnostics (the course/raw-JSON tabs, the manual "Build study
  // library"/"Verify library" actions, the Student/Debug toggle) are hidden
  // from the normal student experience. They come back only when the page URL
  // carries ?bbxdev=1, and nothing about that is persisted.
  const DEV_MODE = new URLSearchParams(location.search).get("bbxdev") === "1";
  // The local AI Lookup Chat backend (matches background.js). Citations link
  // straight to the stored source file it serves.
  const COURSE_COPILOT_ORIGIN = "http://127.0.0.1:8471";
  const TERM_RE = /\b(spring|summer|fall|autumn|winter)\s+(20\d{2})\b/i;
  const REVERSE_TERM_RE = /\b(20\d{2})\s+(spring|summer|fall|autumn|winter)\b/i;
  const MAX = {
    courses: 180,
    assignments: 700,
    files: 1400,
    resources: 1400
  };

  const state = {
    courses: new Map(),
    assignments: new Map(),
    files: new Map(),
    resources: new Map(),
    terms: new Set(),
    selectedTerm: "",
    selectedCourseKey: "",
    courseLoads: new Map(),
    courseEndpoints: new Map(),
    diagnostics: {
      network: [],
      domCourses: [],
      terms: [],
      events: []
    },
    diagnosticTab: "courses",
    uiMode: "student",
    studentSelectedCourse: "",
    exactCourses: new Map(),
    courseListEndpoints: new Set(),
    learnedCourseEndpoints: new Map(),
    selectedProbeCourse: "",
    courseProbeResults: new Map(),
    courseProbeStatus: new Map(),
    courseOutlineCache: new Map(),
    courseObservedNetwork: new Map(),
    lastUpdated: null
  };

  let saveTimer = null;
  let renderTimer = null;
  let scanTimer = null;
  let preloadRunning = false;
  // The single authoritative auto-preparation lifecycle (see prepareAllCourses):
  // discovery → mapping → material sync → library compilation, run for every
  // current course with the active one prioritized. These guard against the
  // sweep stacking on itself when discovery events arrive in bursts.
  let prepareSweepScheduled = false;
  let prepareSweepRunning = false;
  let prepareSweepRerun = false;
  const activePreparation = new Map();
  const activePreparationStatus = new Map();
  const activePreparationReadyIds = new Map();
  const activePreparationFailedIds = new Map();
  const activePreparationSignatures = new Map();
  const activePreparationIsPartial = new Map();
  const activePreparationQueries = new Map();
  const queuedActivePreparations = new Map();
  const activePreparedDescriptors = new Map();
  const courseMappingTasks = new Map();
  const courseMappingCache = new Map();

  const DIAG_LIMIT = 80;

  function diagSafe(value, depth = 0, seen = new WeakSet()) {
    if (depth > 7) return "[depth limit]";
    if (value == null || typeof value === "number" || typeof value === "boolean") return value;
    if (typeof value === "string") return value.length > 8000 ? value.slice(0, 8000) + "…[truncated]" : value;
    if (typeof value !== "object") return String(value);
    if (seen.has(value)) return "[circular]";
    seen.add(value);

    if (Array.isArray(value)) {
      return value.slice(0, 200).map((v) => diagSafe(v, depth + 1, seen));
    }

    const out = {};
    for (const [k, v] of Object.entries(value).slice(0, 200)) {
      out[k] = diagSafe(v, depth + 1, seen);
    }
    return out;
  }

  function diagPush(bucket, value) {
    if (!state.diagnostics?.[bucket]) return;
    state.diagnostics[bucket].push(value);
    const limit = bucket === "network" ? 24 : DIAG_LIMIT;
    while (state.diagnostics[bucket].length > limit) {
      state.diagnostics[bucket].shift();
    }
  }

  function diagEvent(type, details = {}) {
    diagPush("events", {
      at: new Date().toISOString(),
      type,
      ...diagSafe(details)
    });
  }


  function exactCourseKey(record) {
    return `${firstText(record?.id, record?.displayName)}::${firstText(record?.termName)}`;
  }

  function rememberExactCourse(record) {
    const displayName = cleanText(firstText(record?.displayName));
    const termName = cleanText(firstText(record?.termName));
    if (!displayName || !termName) return "";

    const normalized = {
      id: firstText(record?.id),
      displayName,
      termName,
      courseCode: cleanText(firstText(record?.courseCode)),
      description: cleanText(firstText(record?.description)),
      rawCourse: record?.rawCourse && typeof record.rawCourse === "object" ? record.rawCourse : {},
      source: record?.source || {}
    };

    const key = exactCourseKey(normalized);
    state.exactCourses.set(key, {
      ...(state.exactCourses.get(key) || {}),
      ...normalized
    });
    return key;
  }

  function learnExactCoursesFromResponse(body, sourceUrl) {
    const results = Array.isArray(body?.results) ? body.results : null;
    if (!results) return 0;

    let learned = 0;
    for (let resultIndex = 0; resultIndex < results.length; resultIndex++) {
      const item = results[resultIndex];
      const course = item?.course;
      const displayName = cleanText(firstText(course?.displayName));
      const termName = cleanText(firstText(course?.term?.name));
      if (!displayName || !termName) continue;

      const id = firstText(course?.id, course?.courseId, item?.courseId, item?.id);
      rememberExactCourse({
        id,
        displayName,
        termName,
        courseCode: cleanText(firstText(
          course?.courseId,
          course?.courseCode,
          course?.externalId,
          course?.courseNumber
        )),
        description: cleanText(firstText(
          course?.description,
          course?.descriptionHtml,
          course?.shortDescription
        )),
        rawCourse: course,
        source: { url: firstText(sourceUrl), resultIndex }
      });
      learned += 1;
    }

    if (learned && sourceUrl) {
      try {
        const u = new URL(sourceUrl, location.href);
        if (u.origin === location.origin) state.courseListEndpoints.add(u.href);
      } catch (_) {}
      // New courses observed in live Blackboard traffic — prepare them too.
      schedulePrepareAllCourses("network-discovery");
    }
    return learned;
  }

  function learnEndpointForKnownCourse(url) {
    if (!url) return;
    let parsed;
    try {
      parsed = new URL(url, location.href);
      if (parsed.origin !== location.origin) return;
    } catch (_) {
      return;
    }

    for (const record of state.exactCourses.values()) {
      const id = firstText(record.id);
      if (!id) continue;
      const plain = decodeURIComponent(parsed.href);
      if (!plain.includes(id) && !plain.includes(encodeURIComponent(id))) continue;

      const key = exactCourseKey(record);
      if (!state.learnedCourseEndpoints.has(key)) {
        state.learnedCourseEndpoints.set(key, new Set());
      }
      state.learnedCourseEndpoints.get(key).add(parsed.href);
    }
  }

  function exactCourseForPageContext() {
    let context = {};
    try { context = currentCourseContext() || {}; } catch (_) {}

    const contextId = firstText(context.courseId);
    const contextName = cleanText(firstText(context.courseName)).toLowerCase();

    for (const [key, record] of state.exactCourses) {
      const ids = new Set([
        firstText(record.id),
        firstText(record.rawCourse?.id),
        firstText(record.rawCourse?.courseId),
        firstText(record.rawCourse?.uuid)
      ].filter(Boolean));

      if (contextId && ids.has(contextId)) return [key, record];
      if (contextName && record.displayName?.toLowerCase() === contextName) return [key, record];
    }
    return null;
  }

  function rememberCourseScopedNetwork(url, body) {
    const match = exactCourseForPageContext();
    if (!match) return;

    const [key] = match;
    const list = state.courseObservedNetwork.get(key) || [];
    list.push({
      at: new Date().toISOString(),
      url: firstText(url),
      body
    });
    while (list.length > 24) list.shift();
    state.courseObservedNetwork.set(key, list);

    try {
      const parsed = new URL(url, location.href);
      if (parsed.origin === location.origin) {
        if (!state.learnedCourseEndpoints.has(key)) {
          state.learnedCourseEndpoints.set(key, new Set());
        }
        state.learnedCourseEndpoints.get(key).add(parsed.href);
      }
    } catch (_) {}
  }

  function serializeLearnedCourseEndpoints() {
    const out = {};
    for (const [key, urls] of state.learnedCourseEndpoints) {
      out[key] = [...urls].slice(-40);
    }
    return out;
  }

  function restoreLearnedCourseEndpoints(value) {
    if (!value || typeof value !== "object") return;
    for (const [key, urls] of Object.entries(value)) {
      if (!Array.isArray(urls)) continue;
      state.learnedCourseEndpoints.set(key, new Set(urls.slice(-40)));
    }
  }


  function text(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function firstText(...values) {
    for (const value of values) {
      const result = text(value);
      if (result) return result;
    }
    return "";
  }

  function cleanText(value) {
    const raw = firstText(value);
    if (!raw) return "";
    if (!/[<>]/.test(raw)) return raw.replace(/\s+/g, " ").trim();
    try {
      const doc = new DOMParser().parseFromString(raw, "text/html");
      return firstText(doc.body?.textContent).replace(/\s+/g, " ").trim();
    } catch {
      return raw.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    }
  }

  const COURSE_UI_LABELS = new Set([
    "course website", "view course", "books and tools", "books & tools",
    "institution tools", "institutional tools", "course tools", "tools",
    "content", "grades", "messages", "calendar", "announcements",
    "organizations", "activity stream", "courses"
  ]);

  function isUiCourseLabel(value) {
    const name = cleanText(value).toLowerCase().replace(/[.:]+$/, "");
    return !name || COURSE_UI_LABELS.has(name) || Boolean(normalizeTerm(name));
  }

  function plausibleCourseId(value) {
    const id = firstText(value);
    return Boolean(id && id.length >= 3 && !/^(course|courses|outline|content|tools?)$/i.test(id));
  }

  function canonicalCourseName(anchor) {
    const direct = cleanText(firstText(anchor?.innerText, anchor?.textContent, anchor?.getAttribute?.("aria-label"), anchor?.title));
    if (direct && !isUiCourseLabel(direct)) return direct;

    const card = anchor?.closest?.(
      '[data-testid*="course" i], [class*="course-card" i], [class*="courseCard" i], article, li, [role="listitem"]'
    );
    if (!card) return "";

    const candidates = card.querySelectorAll(
      'h1, h2, h3, h4, [role="heading"], [data-testid*="title" i], [class*="course-title" i], [class*="courseTitle" i]'
    );
    for (const node of candidates) {
      const value = cleanText(node.textContent);
      if (value && !isUiCourseLabel(value) && value.length <= 240) return value;
    }
    return "";
  }

  function rememberCourseEndpoint(courseId, sourceUrl) {
    if (!plausibleCourseId(courseId) || !sourceUrl) return;
    try {
      const url = new URL(sourceUrl, location.href);
      if (url.origin !== location.origin) return;
      const set = state.courseEndpoints.get(courseId) || new Set();
      set.add(url.href);
      while (set.size > 30) set.delete(set.values().next().value);
      state.courseEndpoints.set(courseId, set);
    } catch (_) {}
  }

  function absUrl(value) {
    if (!value || typeof value !== "string") return "";
    try {
      const url = new URL(value, location.href);
      return /^https?:$/.test(url.protocol) ? url.href : "";
    } catch {
      return "";
    }
  }

  function normalizeTerm(value) {
    const raw = cleanText(value);
    if (!raw) return "";

    let match = raw.match(TERM_RE);
    if (match) {
      const season = match[1].toLowerCase() === "autumn" ? "Fall" :
        match[1][0].toUpperCase() + match[1].slice(1).toLowerCase();
      return `${season} ${match[2]}`;
    }

    match = raw.match(REVERSE_TERM_RE);
    if (match) {
      const seasonRaw = match[2].toLowerCase();
      const season = seasonRaw === "autumn" ? "Fall" :
        seasonRaw[0].toUpperCase() + seasonRaw.slice(1);
      return `${season} ${match[1]}`;
    }

    return "";
  }

  function dateish(value) {
    const s = firstText(value);
    if (!s) return "";
    const d = new Date(s);
    return Number.isNaN(d.valueOf()) ? s : d.toISOString();
  }

  function keyFor(record) {
    return firstText(record.id, record.url, record.name, record.title);
  }

  function courseKey(course) {
    return firstText(course?.id, course?.url, course?.name);
  }

  function cappedSet(map, record, cap) {
    const key = keyFor(record);
    if (!key) return "";

    const merged = { ...(map.get(key) || {}) };
    for (const [field, value] of Object.entries(record)) {
      if (value !== "" && value !== null && value !== undefined) merged[field] = value;
    }
    map.set(key, merged);

    while (map.size > cap) map.delete(map.keys().next().value);
    return key;
  }

  function rememberTerm(term) {
    const normalized = normalizeTerm(term);
    if (!normalized) return "";
    state.terms.add(normalized);
    return normalized;
  }

  function termFromObject(obj) {
    if (!obj || typeof obj !== "object") return "";
    const nested = [obj.term, obj.academicTerm, obj.academicPeriod, obj.period, obj.session];
    const candidates = [
      obj.termName,
      obj.termLabel,
      obj.academicTermName,
      obj.academicPeriodName,
      typeof obj.term === "string" ? obj.term : ""
    ];

    for (const candidate of candidates) {
      const found = normalizeTerm(candidate);
      if (found) return found;
    }

    for (const value of nested) {
      if (!value || typeof value !== "object") continue;
      const found = normalizeTerm(firstText(value.displayName, value.name, value.title, value.label));
      if (found) return found;
    }

    return "";
  }

  function instructorsFromObject(obj) {
    const values = [obj.instructor, obj.instructors, obj.faculty, obj.teachers, obj.owners];
    const names = [];

    function add(value) {
      if (!value) return;
      if (typeof value === "string") {
        const cleaned = cleanText(value);
        if (cleaned) names.push(cleaned);
        return;
      }
      if (Array.isArray(value)) {
        value.forEach(add);
        return;
      }
      if (typeof value === "object") {
        const name = firstText(
          value.displayName,
          value.name,
          [value.firstName, value.lastName].filter(Boolean).join(" "),
          value.fullName
        );
        if (name) names.push(cleanText(name));
      }
    }

    values.forEach(add);
    return [...new Set(names)].join(", ");
  }

  function urlFromObject(obj) {
    const direct = firstText(
      obj.url,
      obj.href,
      obj.downloadUrl,
      obj.downloadURL,
      obj.launchUrl,
      obj.webUrl,
      obj.contentUrl
    );
    if (direct) return direct;

    if (obj.links && typeof obj.links === "object") {
      for (const value of Object.values(obj.links)) {
        if (typeof value === "string") return value;
        if (value && typeof value === "object") {
          const candidate = firstText(value.href, value.url);
          if (candidate) return candidate;
        }
      }
    }
    return "";
  }

  function courseIdFromUrl(value) {
    try {
      const url = new URL(value, location.href);
      const queryId = firstText(url.searchParams.get("course_id"), url.searchParams.get("courseId"));
      if (queryId) return queryId;

      const matches = [
        url.pathname.match(/\/ultra\/courses\/([^/?#]+)/i),
        url.pathname.match(/\/courses\/([^/?#]+)/i)
      ];
      for (const match of matches) {
        if (match?.[1] && !["course", "courses"].includes(match[1].toLowerCase())) {
          return decodeURIComponent(match[1]);
        }
      }
    } catch (_) {}
    return "";
  }

  function detectSelectedTermFromDom() {
    const candidates = [];

    for (const option of document.querySelectorAll("select option:checked")) {
      candidates.push(option.textContent);
    }

    for (const node of document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"]')) {
      candidates.push(node.getAttribute("aria-label"), node.textContent);
    }

    for (const candidate of candidates) {
      const term = normalizeTerm(candidate);
      if (term) return term;
    }
    return "";
  }

  function currentCourseContext() {
    const id = courseIdFromUrl(location.href);
    if (!id) return { courseId: "", courseName: "", term: detectSelectedTermFromDom() };

    const heading = document.querySelector("h1, [role='heading'][aria-level='1'], [data-testid*='course' i] h2");
    const courseName = firstText(heading?.textContent);
    const knownCourse = [...state.courses.values()].find((course) => course.id === id);

    return {
      courseId: id,
      courseName: firstText(knownCourse?.name, courseName),
      term: firstText(knownCourse?.term, detectSelectedTermFromDom())
    };
  }

  function sourceContext(sourceUrl) {
    const courseId = courseIdFromUrl(sourceUrl);
    const knownCourse = courseId
      ? [...state.courses.values()].find((course) => course.id === courseId)
      : null;
    return {
      courseId,
      courseName: firstText(knownCourse?.name),
      term: firstText(knownCourse?.term)
    };
  }

  function addCourse(record) {
    const name = cleanText(firstText(record.name, record.title));
    const id = firstText(record.id, courseIdFromUrl(record.url));
    if (!name || isUiCourseLabel(name) || !plausibleCourseId(id)) return;

    const term = rememberTerm(firstText(record.term));
    const normalized = {
      id,
      name,
      code: cleanText(firstText(record.code)),
      term,
      description: cleanText(firstText(record.description)),
      instructor: cleanText(firstText(record.instructor)),
      startDate: dateish(record.startDate),
      endDate: dateish(record.endDate),
      url: absUrl(record.url),
      source: firstText(record.source)
    };

    const key = cappedSet(state.courses, normalized, MAX.courses);
    if (key) changed();
  }

  function addAssignment(record) {
    const name = cleanText(firstText(record.name, record.title));
    if (!name) return;
    const term = rememberTerm(record.term);
    cappedSet(
      state.assignments,
      {
        id: firstText(record.id),
        name,
        courseId: firstText(record.courseId),
        courseName: cleanText(firstText(record.courseName)),
        term,
        dueDate: firstText(record.dueDate),
        url: absUrl(record.url),
        source: firstText(record.source)
      },
      MAX.assignments
    );
    changed();
  }

  function addFile(record) {
    const name = cleanText(firstText(record.name, record.title));
    if (!name) return;
    const term = rememberTerm(record.term);
    cappedSet(
      state.files,
      {
        id: firstText(record.id),
        name,
        courseId: firstText(record.courseId),
        courseName: cleanText(firstText(record.courseName)),
        term,
        mimeType: firstText(record.mimeType),
        url: absUrl(record.url),
        source: firstText(record.source)
      },
      MAX.files
    );
    changed();
  }

  function addResource(record) {
    const name = cleanText(firstText(record.name, record.title));
    if (!name) return;
    const term = rememberTerm(record.term);
    cappedSet(
      state.resources,
      {
        id: firstText(record.id),
        name,
        courseId: firstText(record.courseId),
        courseName: cleanText(firstText(record.courseName)),
        term,
        description: cleanText(firstText(record.description)),
        kind: cleanText(firstText(record.kind)),
        url: absUrl(record.url),
        source: firstText(record.source)
      },
      MAX.resources
    );
    changed();
  }

  function changed() {
    // Background Blackboard traffic can be extremely chatty. Persist what we
    // learn, but don't rebuild the visible panel while the user is interacting.
    state.lastUpdated = new Date().toISOString();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 250);
  }

  async function save() {
    try {
      const snapshot = {
        courses: [...state.courses.values()],
        assignments: [...state.assignments.values()],
        files: [...state.files.values()],
        resources: [...state.resources.values()],
        terms: [...state.terms],
        selectedTerm: state.selectedTerm,
        diagnostics: {
          ...state.diagnostics,
          network: []
        },
        diagnosticTab: state.diagnosticTab,
        uiMode: state.uiMode,
        studentSelectedCourse: state.studentSelectedCourse,
        exactCourses: [...state.exactCourses.values()],
        courseListEndpoints: [...state.courseListEndpoints].slice(-12),
        learnedCourseEndpoints: serializeLearnedCourseEndpoints(),
        selectedProbeCourse: state.selectedProbeCourse,
        courseOutlineCacheVersion: OUTLINE_CACHE_VERSION,
        courseOutlineCache: [...state.courseOutlineCache.entries()],
        lastUpdated: state.lastUpdated
      };
      await chrome.storage.local.set({ [STORAGE_KEY]: snapshot });
    } catch (error) {
      console.debug("[BBX] storage failed", error);
    }
  }

  async function restore() {
    try {
      const stored = (await chrome.storage.local.get(STORAGE_KEY))[STORAGE_KEY];
      if (!stored) return;
      for (const course of stored.courses || []) {
        const id = firstText(course.id, courseIdFromUrl(course.url));
        if (plausibleCourseId(id) && !isUiCourseLabel(course.name)) {
          cappedSet(state.courses, { ...course, id }, MAX.courses);
        }
      }
      for (const item of stored.assignments || []) cappedSet(state.assignments, item, MAX.assignments);
      for (const file of stored.files || []) cappedSet(state.files, file, MAX.files);
      for (const resource of stored.resources || []) cappedSet(state.resources, resource, MAX.resources);
      for (const term of stored.terms || []) rememberTerm(term);
      state.selectedTerm = normalizeTerm(stored.selectedTerm) || "";
      state.diagnosticTab = ["courses", "courseData", "raw", "page"].includes(stored.diagnosticTab)
        ? stored.diagnosticTab
        : "courses";
      state.uiMode = (DEV_MODE && stored.uiMode === "debug") ? "debug" : "student";
      state.studentSelectedCourse = firstText(stored.studentSelectedCourse);
      for (const record of stored.exactCourses || []) rememberExactCourse(record);
      for (const endpoint of stored.courseListEndpoints || []) {
        try {
          const u = new URL(endpoint, location.href);
          if (u.origin === location.origin) state.courseListEndpoints.add(u.href);
        } catch (_) {}
      }
      restoreLearnedCourseEndpoints(stored.learnedCourseEndpoints);
      state.selectedProbeCourse = firstText(stored.selectedProbeCourse);
      if (stored.courseOutlineCacheVersion === OUTLINE_CACHE_VERSION) {
        for (const entry of stored.courseOutlineCache || []) {
          if (!Array.isArray(entry) || entry.length !== 2) continue;
          const [key, value] = entry;
          if (key && value?.outline) state.courseOutlineCache.set(key, value);
        }
      }
      if (stored.diagnostics && typeof stored.diagnostics === "object") {
        for (const key of ["network", "domCourses", "terms", "events"]) {
          if (Array.isArray(stored.diagnostics[key])) {
            state.diagnostics[key] = stored.diagnostics[key].slice(-DIAG_LIMIT);
          }
        }
      }
      state.lastUpdated = stored.lastUpdated || null;
    } catch (_) {}
  }

  function inspectObject(obj, path, sourceUrl, inheritedContext = {}) {
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return inheritedContext;

    const pathText = path.join(".").toLowerCase();
    const kind = firstText(obj.type, obj.kind, obj.contentType, obj.mimeType).toLowerCase();
    const name = firstText(
      obj.courseName,
      obj.displayName,
      obj.name,
      obj.title,
      obj.fileName,
      obj.filename,
      obj.label
    );
    const id = firstText(obj.id, obj.courseId, obj.course_id, obj.uuid, obj.contentId);
    const url = urlFromObject(obj);
    const ownTerm = termFromObject(obj);

    const pathParts = path.map((part) => String(part).toLowerCase());
    const explicitCoursePath = pathParts.some((part) => part === "course" || part === "courses");
    const courseContext =
      kind.includes("course") ||
      Boolean(obj.courseCode || obj.courseNumber) ||
      (explicitCoursePath && !pathText.includes("assignment") && !pathText.includes("assessment"));

    const assignmentContext =
      pathText.includes("assignment") ||
      pathText.includes("assessment") ||
      kind.includes("assignment") ||
      kind.includes("assessment") ||
      Boolean(obj.dueDate || obj.due || obj.due_date);

    const fileContext =
      pathText.includes("attachment") ||
      pathText.includes("file") ||
      kind.includes("file") ||
      Boolean(obj.fileName || obj.filename || obj.mimeType || obj.downloadUrl);

    const syllabusContext = /syllab(us|i)/i.test(name) || /syllab(us|i)/i.test(pathText);
    const sourcePath = (() => { try { return new URL(sourceUrl, location.href).pathname.toLowerCase(); } catch { return ""; } })();
    const contentContext =
      syllabusContext ||
      sourcePath.includes("/contents") ||
      pathText.includes("content") ||
      pathText.includes("document") ||
      pathText.includes("material") ||
      kind.includes("document") ||
      kind.includes("content");

    let context = {
      courseId: firstText(obj.courseId, obj.course_id, inheritedContext.courseId),
      courseName: firstText(obj.courseName, obj.course?.name, inheritedContext.courseName),
      term: firstText(ownTerm, inheritedContext.term)
    };

    if (courseContext && name && (id || url)) {
      const courseId = firstText(obj.courseId, obj.course_id, id, courseIdFromUrl(url), inheritedContext.courseId);
      const term = firstText(ownTerm, inheritedContext.term);
      const courseName = cleanText(name);
      addCourse({
        id: courseId,
        name: courseName,
        code: firstText(obj.courseCode, obj.courseNumber, obj.code, obj.externalId),
        term,
        description: firstText(obj.description, obj.courseDescription, obj.summary, obj.details),
        instructor: instructorsFromObject(obj),
        startDate: firstText(obj.startDate, obj.start, obj.availability?.duration?.start),
        endDate: firstText(obj.endDate, obj.end, obj.availability?.duration?.end),
        url,
        source: sourceUrl
      });
      context = { courseId, courseName, term };
    }

    if (assignmentContext && name) {
      addAssignment({
        id,
        name,
        courseId: firstText(obj.courseId, obj.course_id, context.courseId),
        courseName: firstText(obj.courseName, obj.course?.name, context.courseName),
        term: firstText(ownTerm, context.term),
        dueDate: dateish(obj.dueDate || obj.due || obj.due_date),
        url,
        source: sourceUrl
      });
    }

    if (fileContext && name) {
      addFile({
        id,
        name,
        courseId: firstText(obj.courseId, obj.course_id, context.courseId),
        courseName: firstText(obj.courseName, obj.course?.name, context.courseName),
        term: firstText(ownTerm, context.term),
        mimeType: firstText(obj.mimeType, obj.contentType),
        url,
        source: sourceUrl
      });
    }

    if (contentContext && name && !assignmentContext) {
      addResource({
        id,
        name,
        courseId: firstText(obj.courseId, obj.course_id, context.courseId),
        courseName: firstText(obj.courseName, obj.course?.name, context.courseName),
        term: firstText(ownTerm, context.term),
        description: firstText(obj.description, obj.body, obj.text, obj.summary),
        kind: syllabusContext ? "Syllabus" : firstText(obj.type, obj.kind, obj.contentType, "Content"),
        url,
        source: sourceUrl
      });
    }

    return context;
  }

  function ingestJson(root, sourceUrl) {
    const sourceCourseId = courseIdFromUrl(sourceUrl);
    if (sourceCourseId) rememberCourseEndpoint(sourceCourseId, sourceUrl);
    const seen = new WeakSet();
    let visited = 0;
    const VISIT_LIMIT = 24_000;
    const rootContext = sourceContext(sourceUrl);

    function walk(value, path = [], depth = 0, inheritedContext = rootContext) {
      if (visited++ > VISIT_LIMIT || depth > 11 || value == null) return;
      if (typeof value !== "object") return;
      if (seen.has(value)) return;
      seen.add(value);

      if (Array.isArray(value)) {
        for (const child of value) walk(child, path, depth + 1, inheritedContext);
        return;
      }

      const context = inspectObject(value, path, sourceUrl, inheritedContext);
      for (const [key, child] of Object.entries(value)) {
        walk(child, [...path, key], depth + 1, context);
      }
    }

    walk(root);
  }

  function looksLikeCourseHref(href) {
    const h = href.toLowerCase();
    return h.includes("/ultra/courses/") || h.includes("course_id=") || h.includes("/courses/");
  }

  function looksLikeFileHref(href) {
    const h = href.toLowerCase();
    return (
      h.includes("/bbcswebdav/") ||
      h.includes("download") ||
      /\.(pdf|docx?|pptx?|xlsx?|csv|txt|zip|png|jpe?g|gif|webp|mp4|m4v|mov|webm|mp3|m4a|wav)(?:[?#]|$)/i.test(h)
    );
  }

  function isSyllabusName(name) {
    return /\bsyllab(us|i)\b/i.test(name || "");
  }

  async function hydrateCourse(course) {
    const key = courseKey(course);
    if (!key || state.courseLoads.get(key) === "loading") return;

    let target = absUrl(course.url);
    if (!target && course.id) {
      target = `${location.origin}/ultra/courses/${encodeURIComponent(course.id)}/outline`;
    }
    if (!target) return;

    try {
      const url = new URL(target);
      if (url.origin !== location.origin) return;

      state.courseLoads.set(key, "loading");
      render();

      const response = await fetch(url.href, {
        credentials: "include",
        headers: { "Accept": "text/html,application/json;q=0.9,*/*;q=0.8" }
      });
      if (!response.ok) throw new Error(`Blackboard returned ${response.status}`);

      const contentType = (response.headers.get("content-type") || "").toLowerCase();
      if (contentType.includes("json")) {
        ingestJson(await response.json(), url.href);
      } else {
        const html = await response.text();
        if (html && html.length < 5_000_000) {
          const doc = new DOMParser().parseFromString(html, "text/html");
          const context = { courseId: course.id, courseName: course.name, term: course.term };

          for (const a of doc.querySelectorAll("a[href]")) {
            const name = cleanText(firstText(a.textContent, a.getAttribute("aria-label"), a.title));
            const href = absUrl(a.getAttribute("href"));
            if (!name || !href) continue;

            if (looksLikeFileHref(href)) {
              addFile({ ...context, name, url: href, source: `course-fetch:${url.href}` });
            }
            if (isSyllabusName(name)) {
              addResource({ ...context, name, kind: "Syllabus", url: href, source: `course-fetch:${url.href}` });
            }
          }

          for (const script of doc.querySelectorAll('script[type="application/json"], script[type="application/ld+json"]')) {
            const raw = firstText(script.textContent);
            if (!raw || raw.length > 2_000_000) continue;
            try { ingestJson(JSON.parse(raw), url.href); } catch (_) {}
          }
        }
      }

      // Re-query JSON endpoints Blackboard has already used for this course.
      const replay = [...(state.courseEndpoints.get(course.id) || [])];
      // Also try documented public read endpoints. Some institutions allow these
      // through the signed-in browser session; failures are harmless.
      replay.push(
        `${location.origin}/learn/api/public/v3/courses/${encodeURIComponent(course.id)}`,
        `${location.origin}/learn/api/public/v1/courses/${encodeURIComponent(course.id)}/contents?limit=100`
      );

      for (const endpoint of [...new Set(replay)].slice(0, 32)) {
        try {
          const endpointUrl = new URL(endpoint, location.href);
          if (endpointUrl.origin !== location.origin) continue;
          const r = await fetch(endpointUrl.href, {
            credentials: "include",
            headers: { "Accept": "application/json" }
          });
          if (!r.ok) continue;
          const type = (r.headers.get("content-type") || "").toLowerCase();
          if (!type.includes("json")) continue;
          ingestJson(await r.json(), endpointUrl.href);
        } catch (_) {}
      }

      state.courseLoads.set(key, "loaded");
    } catch (error) {
      console.debug("[BBX] course hydration failed", error);
      state.courseLoads.set(key, "error");
    }

    render();
  }


  function collectDomDiagnostics(anchors) {
    const courseCandidates = [];

    for (const a of anchors) {
      try {
        const href = absUrl(a.getAttribute("href"));
        if (!href || !looksLikeCourseHref(href)) continue;

        const card = a.closest(
          "li, article, [role='listitem'], [role='row'], " +
          "[data-testid*='course' i], [class*='course' i]"
        ) || a.parentElement;

        courseCandidates.push({
          href,
          anchorText: cleanText(firstText(
            a.innerText,
            a.textContent,
            a.getAttribute("aria-label"),
            a.title
          )),
          canonicalCourseName: (() => {
            try { return canonicalCourseName(a) || ""; }
            catch (_) { return ""; }
          })(),
          extractedCourseId: courseIdFromUrl(href) || "",
          surroundingText: cleanText(firstText(card?.innerText, card?.textContent)).slice(0, 4000),
          element: {
            tag: a.tagName || "",
            className: typeof a.className === "string" ? a.className.slice(0, 1200) : "",
            ariaLabel: firstText(a.getAttribute("aria-label")),
            title: firstText(a.title)
          },
          container: {
            tag: card?.tagName || "",
            role: firstText(card?.getAttribute?.("role")),
            testId: firstText(card?.getAttribute?.("data-testid")),
            className: typeof card?.className === "string" ? card.className.slice(0, 1200) : ""
          }
        });
      } catch (error) {
        courseCandidates.push({
          diagnosticError: String(error?.message || error)
        });
      }
    }

    state.diagnostics.domCourses = courseCandidates.slice(-DIAG_LIMIT);

    try {
      state.diagnostics.terms = [...document.querySelectorAll(
        "[aria-selected='true'], select option:checked, [role='tab'], h1, h2, h3"
      )].map((el) => ({
        tag: el.tagName || "",
        text: cleanText(firstText(el.innerText, el.textContent, el.value)).slice(0, 1000),
        selected: firstText(el.getAttribute?.("aria-selected")),
        role: firstText(el.getAttribute?.("role")),
        testId: firstText(el.getAttribute?.("data-testid"))
      })).filter((x) => x.text).slice(0, DIAG_LIMIT);
    } catch (error) {
      state.diagnostics.terms = [{ diagnosticError: String(error?.message || error) }];
    }

    let detectedTerm = "";
    try {
      detectedTerm = detectSelectedTermFromDom() || "";
    } catch (error) {
      diagEvent("term-detector-error", { error: String(error?.message || error) });
    }

    diagEvent("dom-scan", {
      anchors: anchors.length,
      courseCandidates: courseCandidates.length,
      detectedTerm
    });
  }

  function scanDom() {
    const detectedTerm = detectSelectedTermFromDom();
    if (detectedTerm) {
      rememberTerm(detectedTerm);
      if (!state.selectedTerm) state.selectedTerm = detectedTerm;
    }

    const pageContext = currentCourseContext();
    const anchors = [...document.querySelectorAll("a[href]")];

    for (const a of anchors) {
      const name = cleanText(firstText(a.innerText, a.textContent, a.getAttribute("aria-label"), a.title));
      if (!name) continue;

      const href = absUrl(a.getAttribute("href"));
      if (!href) continue;

      if (looksLikeCourseHref(href)) {
        const courseId = courseIdFromUrl(href);
        const courseName = canonicalCourseName(a);
        if (courseId && courseName) {
          addCourse({
            id: courseId,
            name: courseName,
            term: detectedTerm,
            url: href,
            source: "DOM"
          });
        }
      }

      if (looksLikeFileHref(href)) {
        addFile({
          name,
          courseId: pageContext.courseId,
          courseName: pageContext.courseName,
          term: pageContext.term,
          url: href,
          source: "DOM"
        });
      }

      if (isSyllabusName(name)) {
        addResource({
          name,
          courseId: pageContext.courseId,
          courseName: pageContext.courseName,
          term: pageContext.term,
          kind: "Syllabus",
          url: href,
          source: "DOM"
        });
      }
    }

    const dueCandidates = [...document.querySelectorAll(
      '[data-testid*="due" i], [class*="due" i], [aria-label*="due" i]'
    )];

    for (const node of dueCandidates.slice(0, 300)) {
      const container = node.closest("li, article, section, [role='row'], [role='listitem']") || node.parentElement;
      const raw = firstText(container?.innerText);
      if (!raw || raw.length > 1000) continue;

      const lines = raw.split("\n").map((s) => s.trim()).filter(Boolean);
      const name = lines.find((line) => !/due/i.test(line)) || lines[0];
      const dueLine = lines.find((line) => /due/i.test(line)) || "";

      if (name && dueLine) {
        addAssignment({
          name,
          courseId: pageContext.courseId,
          courseName: pageContext.courseName,
          term: pageContext.term,
          dueDate: dueLine,
          source: "DOM"
        });
      }
    }
    try { collectDomDiagnostics(anchors); }
    catch (error) { diagEvent("dom-diagnostic-error", { error: String(error?.message || error) }); }

  }

  function scheduleScan() {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scanDom, 350);
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.origin !== location.origin) return;
    const message = event.data;
    if (!message?.__bbx || message.type !== "NETWORK_JSON") return;

    diagPush("network", {
      at: new Date().toISOString(),
      url: message.url || "network",
      // Keep the captured JSON object verbatim for schema inspection.
      body: message.body
    });
    const learnedExact = learnExactCoursesFromResponse(message.body, message.url || "network");
    learnEndpointForKnownCourse(message.url || "");
    rememberCourseScopedNetwork(message.url || "", message.body);
    diagEvent("network-json", {
      url: message.url || "network",
      exactCoursesLearned: learnedExact
    });
    changed();
    if (learnedExact) refreshStudentListIfIdle();

    ingestJson(message.body, message.url || "network");
  });

  // Show a newly discovered class in the list without disrupting a student who
  // is mid-question: only repaint the class-list view (no class open), and only
  // when the visible set of classes is actually stale.
  let listRefreshTimer = null;
  function refreshStudentListIfIdle() {
    clearTimeout(listRefreshTimer);
    listRefreshTimer = setTimeout(() => {
      if (state.uiMode !== "student" || state.studentSelectedCourse) return;
      const shown = document.querySelectorAll(".bbx-student-course").length;
      if (shown !== studentCourseRecords().length) render();
    }, 400);
  }

  function ensureUi() {
    if (document.getElementById("bbx-root")) return;

    const root = document.createElement("div");
    root.id = "bbx-root";

    const launcher = document.createElement("button");
    launcher.id = "bbx-launcher";
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Open B+");

    const launcherImage = document.createElement("img");
    launcherImage.src = chrome.runtime.getURL("bb-plus.png");
    launcherImage.alt = "B+";
    launcher.append(launcherImage);

    const drawer = document.createElement("aside");
    drawer.id = "bbx-drawer";
    drawer.setAttribute("aria-label", "B+");
    drawer.setAttribute("aria-hidden", "true");

    const header = document.createElement("div");
    header.className = "bbx-header";

    const heading = document.createElement("div");
    heading.className = "bbx-brand";
    const title = document.createElement("h2");
    title.textContent = "B+";
    heading.append(title);

    const modeToggle = document.createElement("button");
    modeToggle.className = "bbx-mode-toggle";
    modeToggle.type = "button";
    modeToggle.textContent = state.uiMode === "debug" ? "Student View" : "Debug";
    modeToggle.addEventListener("click", () => {
      state.uiMode = state.uiMode === "debug" ? "student" : "debug";
      modeToggle.textContent = state.uiMode === "debug" ? "Student View" : "Debug";
      save();
      render();
    });

    const ingestButton = document.createElement("button");
    ingestButton.className = "bbx-ingest-button";
    ingestButton.type = "button";
    ingestButton.textContent = "Build study library";
    ingestButton.title = "Fetch and parse course files into a local, in-browser study library (nothing is downloaded to disk)";
    ingestButton.addEventListener("click", () => ingestAllCourses(ingestButton));

    // Verification is deliberately a *separate* action from syncing, not
    // folded silently into it: a sync tells you what it did just now; this
    // tells you, right now, whether the live course outline and what's
    // actually sitting in IndexedDB agree - which catches a stale library
    // (opened the drawer days after the last sync, new files posted since)
    // just as well as a bug in the sync itself.
    const verifyButton = document.createElement("button");
    verifyButton.className = "bbx-ingest-button bbx-verify-button";
    verifyButton.type = "button";
    verifyButton.textContent = "Verify library";
    verifyButton.title = "Check every file Blackboard currently lists against what's actually stored locally";
    verifyButton.addEventListener("click", () => runVerifyLibrary(verifyButton));

    const headerButtons = document.createElement("div");
    headerButtons.className = "bbx-header-buttons";
    headerButtons.id = "bbx-header-tools";
    headerButtons.append(ingestButton, verifyButton);

    const close = document.createElement("button");
    close.className = "bbx-close";
    close.type = "button";
    close.setAttribute("aria-label", "Close B+");
    close.textContent = "×";

    // The developer view toggle and its diagnostic buttons only exist under
    // ?bbxdev=1. Students never see setup controls; preparation is automatic.
    if (DEV_MODE) header.append(heading, headerButtons, modeToggle, close);
    else header.append(heading, close);

    const termBar = document.createElement("div");
    termBar.id = "bbx-term-bar";

    const ingestBanner = document.createElement("div");
    ingestBanner.id = "bbx-ingest-banner";
    ingestBanner.hidden = true;

    const verifyBanner = document.createElement("div");
    verifyBanner.id = "bbx-verify-banner";
    verifyBanner.hidden = true;

    const summary = document.createElement("div");
    summary.id = "bbx-summary";

    const body = document.createElement("div");
    body.id = "bbx-body";


    drawer.append(header, termBar, ingestBanner, verifyBanner, summary, body);
    root.append(launcher, drawer);
    document.documentElement.append(root);

    function setOpen(open) {
      drawer.classList.toggle("bbx-open", open);
      drawer.setAttribute("aria-hidden", open ? "false" : "true");
      launcher.classList.toggle("bbx-hidden", open);
      if (open) render();
    }

    launcher.addEventListener("click", () => setOpen(true));
    close.addEventListener("click", () => setOpen(false));
    render();
  }

  function makeStat(label, value) {
    const card = document.createElement("div");
    card.className = "bbx-stat";
    const number = document.createElement("strong");
    number.textContent = String(value);
    const name = document.createElement("span");
    name.textContent = label;
    card.append(number, name);
    return card;
  }

  function safeLink(url, label, className = "") {
    if (!url) {
      const span = document.createElement("span");
      span.textContent = label;
      if (className) span.className = className;
      return span;
    }

    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = label;
    if (className) a.className = className;
    return a;
  }

  function sameCourse(item, course) {
    if (!item || !course) return false;
    if (item.courseId && course.id && item.courseId === course.id) return true;
    if (item.courseName && course.name && item.courseName.toLowerCase() === course.name.toLowerCase()) return true;
    return false;
  }

  function matchesSelectedTerm(item) {
    if (!state.selectedTerm) return true;
    return normalizeTerm(item?.term) === state.selectedTerm;
  }

  function termRank(term) {
    const normalized = normalizeTerm(term);
    const match = normalized.match(/^(Spring|Summer|Fall|Winter) (20\d{2})$/);
    if (!match) return 0;
    const seasonRank = { Spring: 1, Summer: 2, Fall: 3, Winter: 4 }[match[1]] || 0;
    return Number(match[2]) * 10 + seasonRank;
  }

  function renderTermBar() {
    const bar = document.getElementById("bbx-term-bar");
    if (!bar) return;

    // Course records are the source of truth. Do not populate the selector
    // from arbitrary term strings observed elsewhere in Blackboard.
    const terms = [...new Set(
      [...state.courses.values()]
        .map((course) => normalizeTerm(course.term))
        .filter(Boolean)
    )].sort((a, b) => termRank(b) - termRank(a));

    if (state.selectedTerm && !terms.includes(state.selectedTerm)) {
      state.selectedTerm = "";
    }

    if (!state.selectedTerm) {
      const detected = normalizeTerm(detectSelectedTermFromDom());
      state.selectedTerm = terms.includes(detected) ? detected : (terms[0] || "");
    }

    const label = document.createElement("label");
    label.htmlFor = "bbx-term-select";
    label.textContent = "Term";

    const select = document.createElement("select");
    select.id = "bbx-term-select";

    const all = document.createElement("option");
    all.value = "";
    all.textContent = terms.length ? "All discovered terms" : "All discovered courses";
    all.selected = !state.selectedTerm;
    select.append(all);

    for (const term of terms) {
      const option = document.createElement("option");
      option.value = term;
      option.textContent = term;
      option.selected = term === state.selectedTerm;
      select.append(option);
    }

    select.addEventListener("change", () => {
      state.selectedTerm = select.value;
      state.selectedCourseKey = "";
      changed();
    });

    const hint = document.createElement("span");
    hint.textContent = terms.length
      ? "Terms come only from discovered course cards."
      : "Browse the Blackboard Courses page to associate courses with a term.";

    bar.replaceChildren(label, select, hint);
  }

  function section(titleText, items, formatter, emptyText = "Nothing discovered yet. Browse Blackboard normally and this will fill in.") {
    const section = document.createElement("section");
    section.className = "bbx-section";
    const title = document.createElement("h3");
    title.textContent = titleText;
    section.append(title);

    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "bbx-empty";
      empty.textContent = emptyText;
      section.append(empty);
      return section;
    }

    const list = document.createElement("div");
    list.className = "bbx-list";
    for (const item of items.slice(0, 24)) {
      const row = document.createElement("div");
      row.className = "bbx-row";
      formatter(row, item);
      list.append(row);
    }
    section.append(list);
    return section;
  }

  function infoRow(label, value) {
    const row = document.createElement("div");
    row.className = "bbx-info-row";
    const key = document.createElement("span");
    key.textContent = label;
    const val = document.createElement("strong");
    val.textContent = value;
    row.append(key, val);
    return row;
  }

  function friendlyDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return value;
    return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" }).format(date);
  }

  function renderCourseDetail(course) {
    const summary = document.getElementById("bbx-summary");
    const body = document.getElementById("bbx-body");
    if (!summary || !body) return;

    summary.replaceChildren();

    const relatedAssignments = [...state.assignments.values()].filter((item) => sameCourse(item, course));
    const relatedFiles = [...state.files.values()].filter((item) => sameCourse(item, course));
    const relatedResources = [...state.resources.values()].filter((item) => sameCourse(item, course));
    const syllabus = [
      ...relatedResources.filter((item) => isSyllabusName(item.name) || /syllabus/i.test(item.kind)),
      ...relatedFiles.filter((item) => isSyllabusName(item.name))
    ];

    const header = document.createElement("div");
    header.className = "bbx-course-detail-header";

    const back = document.createElement("button");
    back.type = "button";
    back.className = "bbx-back";
    back.textContent = "← Courses";
    back.addEventListener("click", () => {
      state.selectedCourseKey = "";
      render();
    });

    const title = document.createElement("h3");
    title.textContent = course.name;

    const meta = document.createElement("p");
    meta.textContent = [course.code, course.term].filter(Boolean).join(" · ");

    header.append(back, title, meta);

    const loadState = state.courseLoads.get(courseKey(course));
    if (loadState) {
      const loadStatus = document.createElement("p");
      loadStatus.className = "bbx-load-status";
      loadStatus.textContent = loadState === "loading"
        ? "Checking the course page for additional student-visible data…"
        : loadState === "loaded"
          ? "Course page checked for additional data."
          : "The direct course-page check did not expose additional data; observed Ultra API data will still appear here.";
      header.append(loadStatus);
    }

    if (course.url) {
      const open = safeLink(course.url, "Open course in Blackboard", "bbx-open-course");
      header.append(open);
    }

    const details = document.createElement("section");
    details.className = "bbx-section bbx-detail-card";
    const detailTitle = document.createElement("h3");
    detailTitle.textContent = "Course info";
    details.append(detailTitle);

    const info = document.createElement("div");
    info.className = "bbx-info-grid";
    const rows = [
      ["Term", course.term],
      ["Course code", course.code],
      ["Instructor", course.instructor],
      ["Starts", friendlyDate(course.startDate)],
      ["Ends", friendlyDate(course.endDate)]
    ].filter(([, value]) => value);

    if (rows.length) rows.forEach(([label, value]) => info.append(infoRow(label, value)));
    else {
      const empty = document.createElement("p");
      empty.className = "bbx-empty";
      empty.textContent = "No structured course metadata has been observed yet.";
      info.append(empty);
    }
    details.append(info);

    if (course.description) {
      const description = document.createElement("p");
      description.className = "bbx-description";
      description.textContent = course.description;
      details.append(description);
    }

    const syllabusSection = section(
      "Syllabus",
      syllabus,
      (row, item) => {
        const primary = document.createElement("div");
        primary.className = "bbx-primary";
        primary.append(safeLink(item.url, item.name));
        const meta = document.createElement("div");
        meta.className = "bbx-meta";
        meta.textContent = [item.kind, item.mimeType].filter(Boolean).join(" · ");
        row.append(primary, meta);
      },
      "No syllabus item has been observed yet. Open the course's Content page once so Blackboard loads its course materials, then reopen this class here."
    );

    const resourceSection = section(
      "Course materials",
      [...relatedResources, ...relatedFiles].filter((item, index, arr) => {
        const key = firstText(item.id, item.url, item.name);
        return key && arr.findIndex((other) => firstText(other.id, other.url, other.name) === key) === index && !isSyllabusName(item.name);
      }),
      (row, item) => {
        const primary = document.createElement("div");
        primary.className = "bbx-primary";
        primary.append(safeLink(item.url, item.name));
        const meta = document.createElement("div");
        meta.className = "bbx-meta";
        meta.textContent = [item.kind, item.mimeType].filter(Boolean).join(" · ");
        row.append(primary, meta);
      },
      "No course materials have been observed yet."
    );

    const assignmentSection = section(
      "Assignments",
      relatedAssignments.sort((a, b) => (Date.parse(a.dueDate || "") || Infinity) - (Date.parse(b.dueDate || "") || Infinity)),
      (row, item) => {
        const primary = document.createElement("div");
        primary.className = "bbx-primary";
        primary.append(safeLink(item.url, item.name));
        const meta = document.createElement("div");
        meta.className = "bbx-meta";
        meta.textContent = item.dueDate ? `Due ${friendlyDate(item.dueDate)}` : "";
        row.append(primary, meta);
      },
      "No assignments have been observed for this course yet."
    );

    body.replaceChildren(header, details, syllabusSection, resourceSection, assignmentSection);
  }

  function renderCourseList() {
    const summary = document.getElementById("bbx-summary");
    const body = document.getElementById("bbx-body");
    if (!summary || !body) return;

    const courses = [...state.courses.values()]
      .filter((x) => x.name && matchesSelectedTerm(x))
      .sort((a, b) => a.name.localeCompare(b.name));

    const courseIds = new Set(courses.map((course) => course.id).filter(Boolean));
    const courseNames = new Set(courses.map((course) => course.name.toLowerCase()));
    const belongsToVisibleCourse = (item) =>
      (item.courseId && courseIds.has(item.courseId)) ||
      (item.courseName && courseNames.has(item.courseName.toLowerCase()));

    const assignments = [...state.assignments.values()]
      .filter((x) => x.name && (belongsToVisibleCourse(x) || (matchesSelectedTerm(x) && !x.courseId && !x.courseName)))
      .sort((a, b) => (Date.parse(a.dueDate || "") || Infinity) - (Date.parse(b.dueDate || "") || Infinity));

    const files = [...state.files.values()]
      .filter((x) => x.name && (belongsToVisibleCourse(x) || (matchesSelectedTerm(x) && !x.courseId && !x.courseName)))
      .sort((a, b) => a.name.localeCompare(b.name));

    summary.replaceChildren(
      makeStat("Courses", courses.length),
      makeStat("Assignments", assignments.length),
      makeStat("Files", files.length)
    );

    const courseSection = section(
      `Courses · ${state.selectedTerm || "detected term"}`,
      courses,
      (row, item) => {
        row.classList.add("bbx-course-row");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "bbx-course-button";

        const primary = document.createElement("span");
        primary.className = "bbx-course-name";
        primary.textContent = item.name;

        const meta = document.createElement("span");
        meta.className = "bbx-meta";
        meta.textContent = [item.code, item.instructor].filter(Boolean).join(" · ") || "View discovered course data";

        const chevron = document.createElement("span");
        chevron.className = "bbx-chevron";
        chevron.textContent = "›";

        button.append(primary, meta, chevron);
        button.addEventListener("click", () => {
          state.selectedCourseKey = courseKey(item);
          renderCourseDetail(item);
          hydrateCourse(item);
        });
        row.replaceChildren(button);
      },
      `No courses have been associated with ${state.selectedTerm || "the selected term"} yet. On Blackboard's Courses page, select that term once so the extension can observe the course-to-term mapping.`
    );

    const proof = document.createElement("section");
    proof.className = "bbx-section bbx-proof";
    const proofTitle = document.createElement("h3");
    proofTitle.textContent = "Proof of access";
    const proofText = document.createElement("p");
    proofText.textContent = "Click a class above to query course-specific data and show any syllabus, files, content, assignments, and metadata Blackboard exposes to your signed-in session.";
    proof.append(proofTitle, proofText);

    body.replaceChildren(courseSection, proof);
  }


  function exactCourseRecordsFromNetwork() {
    // Re-process current raw captures in case this build was hot-reloaded.
    for (const entry of state.diagnostics.network) {
      learnExactCoursesFromResponse(entry?.body, entry?.url || "");
    }
    return [...state.exactCourses.values()]
      .filter((r) => r.displayName && r.termName);
  }

  function detectedPageTermSafe() {
    try {
      return cleanText(firstText(detectSelectedTermFromDom()));
    } catch (_) {
      return "";
    }
  }

  function availableExactTerms(records) {
    return [...new Set(records.map((r) => r.termName).filter(Boolean))]
      .sort((a, b) => termRank(b) - termRank(a) || a.localeCompare(b));
  }

  function syncSelectedTermToExactCourses(records) {
    const terms = availableExactTerms(records);

    // Preserve an explicit valid B+ selection. The current Blackboard
    // page term is only an initializer/fallback, not an override.
    if (state.selectedTerm && terms.includes(state.selectedTerm)) return;

    const detected = detectedPageTermSafe();
    if (detected && terms.includes(detected)) {
      state.selectedTerm = detected;
      return;
    }

    state.selectedTerm = terms[0] || "";
  }


  async function refreshCourseListFromKnownEndpoints() {
    const endpoints = [...state.courseListEndpoints].slice(-8);
    if (!endpoints.length) return;

    for (const endpoint of endpoints) {
      try {
        const u = new URL(endpoint, location.href);
        if (u.origin !== location.origin) continue;

        const response = await fetch(u.href, {
          credentials: "include",
          headers: { "Accept": "application/json" }
        });
        if (!response.ok) {
          diagEvent("course-list-replay", { url: u.href, status: response.status });
          continue;
        }

        const type = (response.headers.get("content-type") || "").toLowerCase();
        if (!type.includes("json")) continue;

        const body = await response.json();
        diagPush("network", {
          at: new Date().toISOString(),
          url: u.href,
          replayed: true,
          body
        });
        const learned = learnExactCoursesFromResponse(body, u.href);
        diagEvent("course-list-replay", {
          url: u.href,
          status: response.status,
          exactCoursesLearned: learned
        });
      } catch (error) {
        diagEvent("course-list-replay-error", {
          url: endpoint,
          error: String(error?.message || error)
        });
      }
    }
    changed();
    // Blackboard has just told us which courses exist: kick the automatic
    // discover → map → sync → compile lifecycle for all of them.
    schedulePrepareAllCourses("course-list");
  }

  function collectSameOriginUrls(value, out = new Set(), depth = 0, seen = new WeakSet()) {
    if (depth > 7 || value == null) return out;
    if (typeof value === "string") {
      try {
        const u = new URL(value, location.href);
        if (u.origin === location.origin && /^https?:$/.test(u.protocol)) out.add(u.href);
      } catch (_) {}
      return out;
    }
    if (typeof value !== "object") return out;
    if (seen.has(value)) return out;
    seen.add(value);

    if (Array.isArray(value)) {
      for (const item of value.slice(0, 250)) collectSameOriginUrls(item, out, depth + 1, seen);
      return out;
    }
    for (const child of Object.values(value).slice(0, 250)) {
      collectSameOriginUrls(child, out, depth + 1, seen);
    }
    return out;
  }

  function courseIdentifierCandidates(record) {
    const values = [];
    const add = (value) => {
      const v = firstText(value);
      if (v && !values.includes(v)) values.push(v);
    };

    add(record?.id);
    add(record?.rawCourse?.id);
    add(record?.rawCourse?.courseId);

    const uuid = firstText(record?.rawCourse?.uuid);
    if (uuid) add(uuid.startsWith("uuid:") ? uuid : `uuid:${uuid}`);

    return values.slice(0, 4);
  }

  function courseProbeCandidates(record) {
    const urls = [];
    const seen = new Set();
    const add = (url) => {
      if (!url || seen.has(url)) return;
      try {
        const parsed = new URL(url, location.href);
        if (parsed.origin !== location.origin) return;
        seen.add(parsed.href);
        urls.push(parsed.href);
      } catch (_) {}
    };

    for (const courseId of courseIdentifierCandidates(record)) {
      const encoded = encodeURIComponent(courseId);
      add(`${location.origin}/learn/api/public/v3/courses/${encoded}`);
      add(`${location.origin}/learn/api/public/v1/courses/${encoded}`);
      add(`${location.origin}/learn/api/public/v1/courses/${encoded}/contents?limit=200`);
      add(`${location.origin}/learn/api/public/v1/courses/${encoded}/resources?limit=200`);
    }

    const key = exactCourseKey(record);
    for (const url of state.learnedCourseEndpoints.get(key) || []) add(url);
    for (const url of collectSameOriginUrls(record?.rawCourse || {})) add(url);

    return urls.slice(0, 40);
  }


  function absoluteHttpUrl(value) {
    const raw = firstText(value);
    if (!raw) return "";
    try {
      const url = new URL(raw, location.href);
      return /^https?:$/.test(url.protocol) ? url.href : "";
    } catch (_) {
      return "";
    }
  }

  function linksFromBbml(value) {
    const html = firstText(value);
    if (!html || !/[<>]/.test(html)) return [];

    try {
      const doc = new DOMParser().parseFromString(html, "text/html");
      const results = [];
      for (const a of doc.querySelectorAll("a[href]")) {
        const href = absoluteHttpUrl(a.getAttribute("href"));
        if (!href) continue;

        let fileMeta = {};
        const rawFile = firstText(a.getAttribute("data-bbfile"));
        if (rawFile) {
          try { fileMeta = JSON.parse(rawFile); } catch (_) {}
        }

        results.push({
          url: href,
          text: cleanText(firstText(a.textContent)),
          bbType: firstText(a.getAttribute("data-bbtype")),
          fileName: cleanText(firstText(
            fileMeta?.linkName,
            fileMeta?.alternativeText,
            a.getAttribute("download"),
            a.textContent
          )),
          mimeType: firstText(fileMeta?.mimeType),
          isDirectFile:
            /attachment|file|image/i.test(firstText(a.getAttribute("data-bbtype"))) ||
            /\/bbcswebdav\//i.test(href) ||
            /\.(pdf|docx?|pptx?|xlsx?|csv|txt|zip|png|jpe?g|gif|webp|mp4|m4v|mov|webm|mp3|m4a|wav)(?:[?#]|$)/i.test(href)
        });
      }
      return results;
    } catch (_) {
      return [];
    }
  }

  function alternateUiUrl(obj) {
    if (!obj || typeof obj !== "object") return "";

    const links = Array.isArray(obj.links) ? obj.links : [];
    const preferred = [
      ...links.filter((link) => /alternate|ui|view/i.test(firstText(link?.rel, link?.title))),
      ...links
    ];

    for (const link of preferred) {
      const href = absoluteHttpUrl(firstText(link?.href, link?.url));
      if (href) return href;
    }

    return "";
  }

  function bestUiUrlFromObject(obj) {
    if (!obj || typeof obj !== "object") return "";

    const direct = absoluteHttpUrl(firstText(
      obj.url,
      obj.href,
      obj.webUrl,
      obj.launchUrl,
      obj.contentUrl
    ));
    if (direct) return direct;

    const alternate = alternateUiUrl(obj);
    if (alternate) return alternate;

    const handlerUrl = absoluteHttpUrl(firstText(
      obj.contentHandler?.url,
      obj.contentHandler?.href
    ));
    if (handlerUrl) return handlerUrl;

    return "";
  }

  function preferredUltraCourseId(record) {
    const candidates = [
      firstText(record?.id),
      firstText(record?.rawCourse?.id),
      firstText(record?.rawCourse?.courseId),
      firstText(record?.rawCourse?.uuid)
    ].filter(Boolean);

    candidates.sort((a, b) => {
      const score = (value) => {
        let n = 0;
        if (/^_.+_\d+$/.test(value)) n += 100;
        if (!/^uuid:/i.test(value)) n += 20;
        return n;
      };
      return score(b) - score(a);
    });

    return candidates[0] || "";
  }

  function exactAssessmentId(raw = {}) {
    const direct = firstText(
      raw?.contentHandler?.assessmentId,
      raw?.assessmentId,
      raw?.assessment?.id
    );
    if (direct) return direct;

    // Last-resort extraction only from an actual assessment route already
    // present in the payload; never substitute the content item's own id.
    const urls = allHttpUrlsFromObject(raw);
    for (const url of urls) {
      const match = url.match(/\/assessment\/([^/?#]+)\/overview/i);
      if (match?.[1]) {
        try { return decodeURIComponent(match[1]); }
        catch (_) { return match[1]; }
      }
    }
    return "";
  }

  function canonicalUltraUrl(type, courseId, objectId, raw = {}) {
    const c = firstText(courseId);
    const id = firstText(objectId);
    if (!c) return "";

    if (type === "folder") {
      return `${location.origin}/ultra/courses/${encodeURIComponent(c)}/outline`;
    }

    if (type === "document" && id) {
      return `${location.origin}/ultra/courses/${encodeURIComponent(c)}` +
        `/document/${encodeURIComponent(id)}?view=content&state=view`;
    }

    if (type === "file" && id) {
      return `${location.origin}/ultra/courses/${encodeURIComponent(c)}` +
        `/file/${encodeURIComponent(id)}?courseId=${encodeURIComponent(c)}`;
    }

    if (type === "assessment") {
      const assessmentId = exactAssessmentId(raw);
      if (assessmentId) {
        return `${location.origin}/ultra/courses/${encodeURIComponent(c)}` +
          `/assessment/${encodeURIComponent(assessmentId)}/overview` +
          `?courseId=${encodeURIComponent(c)}`;
      }
    }

    return "";
  }

  function isAlternateFormatArtifact(candidate) {
    if (!candidate) return false;

    const raw = candidate.raw || candidate;
    const name = cleanText(firstText(
      candidate.name,
      candidate.title,
      candidate.fileName,
      candidate.filename,
      raw?.name,
      raw?.title,
      raw?.fileName,
      raw?.filename,
      raw?.contentHandler?.file?.fileName,
      raw?.contentHandler?.file?.name
    ));

    const url = absoluteHttpUrl(firstText(
      candidate.url,
      candidate.href,
      candidate.downloadUrl,
      candidate.downloadURL,
      raw?.url,
      raw?.href
    ));

    const path = firstText(candidate.path);

    // Strong metadata fields where "Ally" / alternate-format terminology is
    // meaningful. Do not scan arbitrary course titles for the substring
    // "ally" — e.g. "Literally" contains "ally".
    const metadataMarker = [
      firstText(raw?.type),
      firstText(raw?.kind),
      firstText(raw?.format),
      firstText(raw?.formatType),
      firstText(raw?.alternativeFormatType),
      firstText(raw?.conversionType),
      firstText(raw?.provider),
      firstText(raw?.source),
      firstText(raw?.contentHandler?.file?.format),
      firstText(raw?.contentHandler?.file?.source),
      firstText(raw?.contentHandler?.file?.provider)
    ].join(" ");

    if (
      /alternative.?format|alternate.?format|conversion|converted.?format|generated.?format/i.test(metadataMarker)
    ) {
      return true;
    }

    // Ally should only match as a provider/path namespace or standalone token,
    // never as a substring inside ordinary words such as "Literally".
    if (/(^|[^a-z0-9])ally([^a-z0-9]|$)/i.test(metadataMarker)) return true;
    if (/(^|[\/_.-])ally([\/_.-]|$)/i.test(path)) return true;
    if (/\/ally(?:\/|$)|[?&](?:provider|source)=ally(?:&|$)/i.test(url)) return true;

    // Explicit alternate-format names are safe to filter by phrase.
    if (/alternative.?format|alternate.?format/i.test(name)) return true;

    // Ally/generated "combined" derivatives are compiled on request. Only
    // treat "combined" as generated when the surrounding metadata/path also
    // indicates a conversion/alternate-format object, or the item has no
    // stable URL and is not a normal Blackboard x-bb-file content node.
    if (/\bcombined\b/i.test(name)) {
      const handler = firstText(
        raw?.contentHandler?.id,
        raw?.contentHandlerId
      ).toLowerCase();

      const generatedContext =
        /alternative.?format|alternate.?format|conversion|generated|ally/i.test(metadataMarker) ||
        /alternative.?format|alternate.?format|conversion|ally/i.test(path);

      if (generatedContext) return true;

      if (!url && !/resource\/x-bb-file|x-bb-file/i.test(handler)) return true;
    }

    return false;
  }

  function ultraNodeKind(node) {
    const raw = node?.raw || {};
    const handler = firstText(
      node?.handlerId,
      raw?.contentHandler?.id,
      raw?.contentHandlerId,
      raw?.handler?.id
    ).toLowerCase();

    const explicit = firstText(raw?.type, raw?.kind, raw?.contentType).toLowerCase();
    const isBbPage = raw?.contentHandler?.isBbPage === true || raw?.isBbPage === true;

    if (/resource\/x-bb-asmt-test-link|assessment|test|quiz|exam|assignment/i.test(handler) ||
        /assessment|test|quiz|exam|assignment/i.test(explicit)) {
      return "assessment";
    }

    // Blackboard Learning Modules use the lesson handler. They behave like
    // containers for traversal, but are not ordinary folders in the UI.
    if (/resource\/x-bb-lesson|x-bb-lesson/i.test(handler)) {
      return "learningModule";
    }

    if (/resource\/x-bb-folder|x-bb-folder/i.test(handler)) {
      return isBbPage ? "documentWrapper" : "folder";
    }

    if (/resource\/x-bb-document|x-bb-document/i.test(handler)) {
      return "documentBody";
    }

    if (/resource\/x-bb-file|x-bb-file/i.test(handler)) {
      return "file";
    }

    if (/externallink|courselink|forumlink|blti-link|(^|[\/_-])link($|[\/_-])/i.test(handler)) {
      return "link";
    }

    if (/lesson|learning.?module/i.test(explicit)) return "learningModule";
    if (/document/i.test(explicit)) return "documentBody";
    if (/file/i.test(explicit)) return "file";
    if (/link/i.test(explicit)) return "link";
    if (/folder|container/i.test(explicit)) return "folder";

    return "unknown";
  }

  function classifyContentNode(node) {
    const kind = ultraNodeKind(node);
    if (kind === "documentWrapper" || kind === "documentBody") return "document";
    if (kind === "learningModule") return "learningModule";
    if (["folder", "file", "link", "assessment"].includes(kind)) return kind;
    return "content";
  }

  function directDownloadUrlForFile(raw = {}) {
    const bbml = [
      ...linksFromBbml(raw?.body),
      ...linksFromBbml(raw?.description)
    ].filter((link) => link.isDirectFile && !isAlternateFormatArtifact(link));

    const fromBbml =
      bbml.find((link) => /\/bbcswebdav\//i.test(link.url)) ||
      bbml[0];
    if (fromBbml?.url) return fromBbml.url;

    const candidates = [
      raw?.downloadUrl,
      raw?.downloadURL,
      raw?.file?.downloadUrl,
      raw?.file?.url,
      raw?.contentHandler?.file?.downloadUrl,
      raw?.contentHandler?.file?.url
    ].map(absoluteHttpUrl)
      .filter(Boolean)
      .filter((url) => !isAlternateFormatArtifact({ url, raw }));

    return (
      candidates.find((url) => /\/bbcswebdav\//i.test(url)) ||
      candidates.find((url) =>
        /\.(pdf|docx?|pptx?|xlsx?|csv|txt|zip|png|jpe?g|gif|webp|mp4|m4v|mov|webm|mp3|m4a|wav)(?:[?#]|$)/i.test(url)
      ) ||
      candidates[0] ||
      ""
    );
  }

  function bestDirectUrlForNode(node, type, courseId = "", canonicalId = "") {
    const raw = node?.raw || node || {};
    const objectId = firstText(canonicalId, node?.id, raw?.id, raw?.contentId);

    // Known Blackboard content objects get deterministic Ultra routes.
    if (["folder", "document", "file", "assessment"].includes(type)) {
      const canonical = canonicalUltraUrl(type, courseId, objectId, raw);
      if (canonical) return canonical;
    }

    const handler = firstText(node?.handlerId, raw?.contentHandler?.id).toLowerCase();

    if (type === "link" || /externallink|blti-link/i.test(handler)) {
      const target = absoluteHttpUrl(firstText(
        raw?.contentHandler?.url,
        raw?.contentHandler?.href,
        raw?.launchUrl,
        raw?.url
      ));
      if (target) return target;
    }

    // This branch is for embedded document attachments / non-content-node
    // file observations that don't have a Blackboard content ID.
    if (type === "file") {
      return directDownloadUrlForFile(raw);
    }

    // Learning Modules do not have a user-provided canonical route yet.
    // Prefer Blackboard's own UI target so the title remains clickable.
    if (type === "learningModule") {
      return alternateUiUrl(raw) || bestUiUrlFromObject(raw);
    }

    return alternateUiUrl(raw) || bestUiUrlFromObject(raw);
  }


  function contentTypeLabel(type) {
    return ({
      folder: "Folder",
      learningModule: "Learning Module",
      document: "Document",
      file: "File",
      link: "Link",
      assessment: "Assessment",
      content: "Content"
    })[type] || "Content";
  }

  function isSyntheticRootNode(node) {
    if (!node) return false;
    const raw = node.raw || {};
    const title = cleanText(firstText(node.title, raw.title, raw.name));
    return (
      raw.synthetic === true ||
      /^root$/i.test(title) ||
      /^course\s+root$/i.test(title)
    );
  }

  function buildCourseOutline(nodes, files, courseRecord = {}) {
    const courseId = preferredUltraCourseId(courseRecord);
    const nodeById = new Map();
    const childrenByParent = new Map();

    const normalizeName = (value) =>
      cleanText(firstText(value)).toLowerCase().replace(/\s+/g, " ").trim();

    function directContentChildren(nodeId) {
      return childrenByParent.get(nodeId) || [];
    }

    function directDocumentBody(nodeId) {
      return directContentChildren(nodeId).find(
        (child) => ultraNodeKind(child) === "documentBody"
      ) || null;
    }

    function effectiveUltraKind(node) {
      const kind = ultraNodeKind(node);

      // Some /children listings omit contentHandler.isBbPage on the outer
      // x-bb-folder. The relationship is still unambiguous: Anthology defines
      // an Ultra document body (x-bb-document) as the child of the page
      // wrapper. Infer that wrapper structurally instead of calling it Folder.
      if (kind === "folder" && directDocumentBody(node.id)) {
        return "documentWrapper";
      }

      return kind;
    }

    for (const node of nodes || []) {
      nodeById.set(node.id, node);
      if (node.parentId) {
        if (!childrenByParent.has(node.parentId)) childrenByParent.set(node.parentId, []);
        childrenByParent.get(node.parentId).push(node);
      }
    }

    // Consolidate file observations. Only linkable/direct files are visible.
    const fileByKey = new Map();
    for (const rawFile of files || []) {
      if (isAlternateFormatArtifact(rawFile)) continue;

      const raw = rawFile.raw || {};
      const parentId = firstText(
        rawFile.parentId,
        raw.contentId,
        raw.parentId,
        raw.content?.id
      );
      const title = cleanText(firstText(rawFile.name)) || "(unnamed file)";
      const mimeType = firstText(rawFile.mimeType);
      const url = absoluteHttpUrl(firstText(
        rawFile.url,
        bestDirectUrlForNode({ raw }, "file", courseId)
      ));

      // Don't show Ally/combined/non-linkable alternate-format placeholders.
      if (!url) continue;
      if (isAlternateFormatArtifact({ name: title, url, raw, path: rawFile.path })) continue;

      const key = `url:${url}`;
      if (!fileByKey.has(key)) {
        fileByKey.set(key, {
          id: firstText(rawFile.id),
          parentId,
          title,
          type: "file",
          handlerId: "resource/x-bb-file",
          hasChildren: false,
          url,
          downloadUrl: url,
          mimeType,
          raw,
          children: []
        });
      }
    }

    const filesByParent = new Map();
    for (const file of fileByKey.values()) {
      if (!filesByParent.has(file.parentId)) filesByParent.set(file.parentId, []);
      filesByParent.get(file.parentId).push(file);
    }

    const consumed = new Set();

    function fileChildrenFor(...parentIds) {
      const seenUrls = new Set();
      const result = [];

      for (const parentId of parentIds.filter(Boolean)) {
        for (const file of filesByParent.get(parentId) || []) {
          if (!file.url || seenUrls.has(file.url)) continue;
          seenUrls.add(file.url);
          result.push(file);
        }
      }
      return result;
    }

    function buildNode(node) {
      if (!node || consumed.has(node.id)) return null;
      if (isAlternateFormatArtifact({ name: node.title, url: node.url, path: node.path, raw: node.raw })) {
        consumed.add(node.id);
        return null;
      }

      const kind = effectiveUltraKind(node);

      // Ultra Document normalization:
      // resource/x-bb-folder + isBbPage=true is the visible page wrapper.
      // Its child resource/x-bb-document is the page body. Collapse both into
      // ONE visible Document using the wrapper ID/URL.
      if (kind === "documentWrapper") {
        consumed.add(node.id);

        const rawChildren = directContentChildren(node.id);
        const bodyNode = directDocumentBody(node.id);
        if (bodyNode) consumed.add(bodyNode.id);

        const nestedVisible = [];
        for (const child of rawChildren) {
          if (bodyNode && child.id === bodyNode.id) continue;
          const built = buildNode(child);
          if (built) nestedVisible.push(built);
        }

        // BBML attachments are normally associated with the body content ID,
        // while the user-facing document route uses the wrapper ID.
        const embeddedFiles = fileChildrenFor(
          bodyNode?.id,
          node.id
        );

        return {
          id: node.id,
          parentId: node.parentId,
          title: cleanText(firstText(node.title, bodyNode?.title)) || "(untitled document)",
          type: "document",
          handlerId: "resource/x-bb-folder:isBbPage",
          hasChildren: false,
          url: canonicalUltraUrl("document", courseId, node.id, node.raw),
          mimeType: "",
          raw: {
            wrapper: node.raw,
            body: bodyNode?.raw || null
          },
          children: [...embeddedFiles, ...nestedVisible]
        };
      }

      // A document body is never a separate visible object in Ultra when its
      // isBbPage wrapper is present.
      if (kind === "documentBody") {
        const parent = node.parentId ? nodeById.get(node.parentId) : null;
        if (parent && effectiveUltraKind(parent) === "documentWrapper") {
          consumed.add(node.id);
          return null;
        }

        // Fallback for an orphan body: show it once as a document.
        consumed.add(node.id);
        return {
          id: node.id,
          parentId: node.parentId,
          title: node.title || "(untitled document)",
          type: "document",
          handlerId: node.handlerId,
          hasChildren: false,
          url: canonicalUltraUrl("document", courseId, node.id, node.raw),
          mimeType: "",
          raw: node.raw,
          children: fileChildrenFor(node.id)
        };
      }

      if (kind === "learningModule") {
        consumed.add(node.id);
        const children = [];
        for (const child of childrenByParent.get(node.id) || []) {
          const built = buildNode(child);
          if (built) children.push(built);
        }

        return {
          id: node.id,
          parentId: node.parentId,
          title: node.title || "(untitled learning module)",
          type: "learningModule",
          handlerId: node.handlerId,
          hasChildren: true,
          url: bestDirectUrlForNode(node, "learningModule", courseId),
          mimeType: "",
          raw: node.raw,
          children
        };
      }

      if (kind === "folder") {
        consumed.add(node.id);
        const children = [];
        for (const child of childrenByParent.get(node.id) || []) {
          const built = buildNode(child);
          if (built) children.push(built);
        }

        return {
          id: node.id,
          parentId: node.parentId,
          title: node.title || "(untitled folder)",
          type: "folder",
          handlerId: node.handlerId,
          hasChildren: true,
          url: canonicalUltraUrl("folder", courseId, node.id, node.raw),
          mimeType: "",
          raw: node.raw,
          syntheticRoot: isSyntheticRootNode(node),
          children
        };
      }

      if (kind === "file") {
        consumed.add(node.id);

        const directCandidates = [
          ...fileChildrenFor(node.id),
          ...[...fileByKey.values()].filter((file) =>
            normalizeName(file.title) === normalizeName(node.title) &&
            (!file.parentId || !node.parentId || file.parentId === node.parentId)
          )
        ];
        const direct = directCandidates.find((file) => file.url);

        // A proper x-bb-file content item is always displayable. Prefer the
        // deterministic Ultra file route so PDFs/videos don't disappear when
        // Blackboard withholds a bbcswebdav URL from this response.
        const downloadUrl =
          direct?.url ||
          directDownloadUrlForFile(node.raw);

        const url =
          canonicalUltraUrl("file", courseId, node.id, node.raw) ||
          downloadUrl ||
          bestDirectUrlForNode(node, "file", courseId);

        return {
          id: node.id,
          parentId: node.parentId,
          title: node.title ||
            cleanText(firstText(node.raw?.contentHandler?.file?.fileName)) ||
            direct?.title ||
            "(file)",
          type: "file",
          handlerId: node.handlerId,
          hasChildren: false,
          url,
          downloadUrl,
          mimeType: firstText(
            node.raw?.contentHandler?.file?.mimeType,
            direct?.mimeType
          ),
          raw: node.raw,
          children: []
        };
      }

      if (kind === "link") {
        consumed.add(node.id);
        return {
          id: node.id,
          parentId: node.parentId,
          title: node.title || "(link)",
          type: "link",
          handlerId: node.handlerId,
          hasChildren: false,
          url: bestDirectUrlForNode(node, "link", courseId),
          mimeType: "",
          raw: node.raw,
          children: []
        };
      }

      if (kind === "assessment") {
        consumed.add(node.id);
        const assessmentId = exactAssessmentId(node.raw);

        return {
          id: node.id,
          objectId: assessmentId,
          assessmentId,
          parentId: node.parentId,
          title: node.title || "(assessment)",
          type: "assessment",
          handlerId: node.handlerId,
          hasChildren: false,
          // Never fall back to the content-node ID as an assessment ID.
          url: assessmentId
            ? canonicalUltraUrl("assessment", courseId, node.id, node.raw)
            : "",
          mimeType: "",
          raw: node.raw,
          children: []
        };
      }

      consumed.add(node.id);
      return null;
    }

    // Build from nodes whose parent isn't a known content node.
    let roots = [];
    for (const node of nodes || []) {
      if (consumed.has(node.id)) continue;
      if (node.parentId && nodeById.has(node.parentId)) continue;

      const built = buildNode(node);
      if (built) roots.push(built);
    }

    // Do not promote orphan attachment observations to the course root.
    // A legitimate root-level Blackboard File is represented by an x-bb-file
    // content node above. Attachment records whose document/body parent could
    // not be resolved stay out of Student View rather than appearing globally.

    // Suppress Blackboard's structural ROOT container even when other
    // root-level links/items are returned alongside it.
    roots = roots.flatMap((item) =>
      item.type === "folder" && item.syntheticRoot
        ? (item.children || [])
        : [item]
    );

    // Final duplicate guard. For files, URL is identity. For content, ID is.
    function dedupe(items) {
      const map = new Map();

      for (const item of items) {
        item.children = dedupe(item.children || []);

        const key = item.type === "file"
          ? `file:${item.url}`
          : `${item.type}:${item.id || normalizeName(item.title)}`;

        const existing = map.get(key);
        if (!existing) {
          map.set(key, item);
          continue;
        }

        if (!existing.url && item.url) existing.url = item.url;
        if (!existing.children.length && item.children.length) existing.children = item.children;
      }

      return [...map.values()];
    }

    roots = dedupe(roots);

    const sort = (items) => {
      items.sort((a, b) => a.title.localeCompare(b.title));
      for (const item of items) sort(item.children || []);
    };
    sort(roots);

    return roots;
  }
  function flattenCourseOutline(items, out = []) {
    for (const item of items || []) {
      out.push(item);
      flattenCourseOutline(item.children, out);
    }
    return out;
  }

  function contentNodesFromValue(value) {
    const byId = new Map();
    const seen = new WeakSet();

    function richness(node) {
      return (
        (node.handlerId ? 30 : 0) +
        (node.parentId ? 5 : 0) +
        (node.raw?.contentHandler ? 10 : 0) +
        (node.raw?.links ? 5 : 0) +
        (node.raw?.body ? 3 : 0)
      );
    }

    function maybeAdd(raw, path = []) {
      if (!raw || typeof raw !== "object") return;

      const id = firstText(raw.id, raw.contentId);
      const title = cleanText(firstText(raw.title, raw.displayName, raw.name));
      const parentId = firstText(raw.parentId, raw.parent?.id);
      const handlerId = firstText(
        raw.contentHandler?.id,
        raw.contentHandlerId,
        raw.handler?.id
      );

      // Course content objects are identified primarily by Blackboard's
      // contentHandler. This avoids accidentally promoting nested file/image/
      // Ally metadata to a course-outline node.
      if (!id || !handlerId) return;

      const node = {
        id,
        parentId,
        title: title || "(untitled)",
        handlerId,
        hasChildren:
          raw.hasChildren === true ||
          Number(raw.childCount || raw.childrenCount || 0) > 0,
        folderLike: /x-bb-folder/i.test(handlerId),
        url: bestUiUrlFromObject(raw),
        path: path.join("."),
        raw
      };

      if (isAlternateFormatArtifact({
        name: node.title,
        url: node.url,
        path: node.path,
        raw
      })) return;

      const key = id;
      const existing = byId.get(key);
      if (!existing || richness(node) > richness(existing)) byId.set(key, node);
    }

    function walk(node, path = [], depth = 0) {
      if (depth > 10 || node == null || typeof node !== "object") return;
      if (seen.has(node)) return;
      seen.add(node);

      if (Array.isArray(node)) {
        node.slice(0, 500).forEach((item, i) => {
          maybeAdd(item, [...path, i]);
          walk(item, [...path, i], depth + 1);
        });
        return;
      }

      maybeAdd(node, path);

      for (const [key, child] of Object.entries(node).slice(0, 500)) {
        if (/^(body|description|attachments?|files?|images?|alternativeFormats?)$/i.test(key)) {
          continue;
        }
        walk(child, [...path, key], depth + 1);
      }
    }

    walk(value);
    return [...byId.values()].slice(0, 1000);
  }
  function attachmentCandidatesFromValue(value) {
    const attachments = [];
    const seenObjects = new WeakSet();
    const seenKeys = new Set();

    function add(candidate) {
      if (isAlternateFormatArtifact(candidate)) return;
      const id = firstText(candidate?.id);
      const name = cleanText(firstText(candidate?.name));
      const mimeType = firstText(candidate?.mimeType);
      const url = absoluteHttpUrl(candidate?.url);
      const parentId = firstText(candidate?.parentId);

      if (!id && !name && !url && !mimeType) return;
      const key = `${id}::${parentId}::${name.toLowerCase()}::${mimeType}::${url}`;
      if (seenKeys.has(key)) return;
      seenKeys.add(key);
      attachments.push({
        ...candidate,
        id,
        name,
        mimeType,
        url,
        parentId
      });
    }

    function walk(node, path = [], depth = 0) {
      if (depth > 10 || node == null || typeof node !== "object") return;
      if (seenObjects.has(node)) return;
      seenObjects.add(node);

      if (Array.isArray(node)) {
        node.slice(0, 500).forEach((item, i) => walk(item, [...path, i], depth + 1));
        return;
      }

      const pathText = path.join(".").toLowerCase();
      const id = firstText(node.id, node.attachmentId, node.fileId);
      const parentId = firstText(node.contentId, node.parentId, node.content?.id);
      const name = cleanText(firstText(
        node.fileName, node.filename, node.displayName, node.name, node.title
      ));
      const mimeType = firstText(node.mimeType, node.contentType);
      const rawUrl = firstText(
        node.downloadUrl, node.downloadURL, node.url, node.href, node.webUrl, node.contentUrl
      );
      const url = absoluteHttpUrl(rawUrl);

      const attachmentLike =
        /attachment|attachments|file|files|resource|resources/i.test(pathText) ||
        Boolean(node.attachmentId || node.fileId || node.fileName || node.filename || mimeType) ||
        /\.(pdf|docx?|pptx?|xlsx?|csv|txt|zip|png|jpe?g|gif|webp|mp4|m4v|mov|webm|mp3|m4a|wav)(?:[?#]|$)/i.test(url);

      if (attachmentLike) {
        add({
          id,
          parentId,
          name,
          mimeType,
          url,
          path: path.join("."),
          raw: node
        });
      }

      // Ultra documents can carry downloadable files inside BBML rather than
      // a conventional attachment array/object.
      for (const link of [
        ...linksFromBbml(node.body),
        ...linksFromBbml(node.description)
      ]) {
        if (!link.isDirectFile || isAlternateFormatArtifact(link)) continue;
        add({
          id: "",
          parentId: firstText(node.id, node.contentId, node.parentId),
          name: cleanText(firstText(link.fileName, link.text)) || "(attachment)",
          mimeType: firstText(link.mimeType),
          url: link.url,
          path: `${path.join(".")}.bbml`,
          raw: {
            contentId: firstText(node.id, node.contentId),
            bbType: link.bbType
          }
        });
      }

      for (const [k, child] of Object.entries(node).slice(0, 500)) {
        walk(child, [...path, k], depth + 1);
      }
    }

    walk(value);
    return attachments.slice(0, 1000);
  }


  function fileCandidatesFromValue(value) {
    const out = [];
    const keys = new Set();
    const seen = new WeakSet();

    function add(candidate) {
      if (isAlternateFormatArtifact(candidate)) return;

      const name = cleanText(firstText(candidate.name));
      const url = absoluteHttpUrl(candidate.url);
      const mimeType = firstText(candidate.mimeType);
      const parentId = firstText(candidate.parentId);

      // Hide non-linkable pseudo/generated file rows. Raw JSON still keeps them.
      if (!url && !mimeType) return;

      const key = url
        ? `url:${url}`
        : `${parentId}::${name.toLowerCase()}::${mimeType.toLowerCase()}`;
      if (keys.has(key)) return;
      keys.add(key);

      out.push({ ...candidate, name, url, mimeType, parentId });
    }

    function walk(node, path = [], depth = 0) {
      if (depth > 9 || node == null || typeof node !== "object") return;
      if (seen.has(node)) return;
      seen.add(node);

      if (Array.isArray(node)) {
        node.slice(0, 500).forEach((item, i) => walk(item, [...path, i], depth + 1));
        return;
      }

      const pathText = path.join(".").toLowerCase();
      if (/alternative.?format|ally|conversion|converted.?format/i.test(pathText)) return;

      const name = cleanText(firstText(
        node.fileName, node.filename, node.displayName, node.name, node.title
      ));
      const mimeType = firstText(node.mimeType, node.contentType);
      const url = absoluteHttpUrl(firstText(
        node.downloadUrl,
        node.downloadURL,
        node.file?.downloadUrl,
        node.file?.url,
        node.href,
        node.url
      ));
      const parentId = firstText(node.contentId, node.parentId, node.content?.id);

      const directFile =
        /\/bbcswebdav\//i.test(url) ||
        /\.(pdf|docx?|pptx?|xlsx?|csv|txt|zip|png|jpe?g|gif|webp|mp4|m4v|mov|webm|mp3|m4a|wav)(?:[?#]|$)/i.test(url) ||
        Boolean(node.fileName || node.filename || node.downloadUrl || node.downloadURL);

      if (directFile) {
        add({
          id: firstText(node.fileId, node.attachmentId),
          parentId,
          name,
          mimeType,
          url,
          path: path.join("."),
          raw: node
        });
      }

      for (const [key, child] of Object.entries(node).slice(0, 500)) {
        walk(child, [...path, key], depth + 1);
      }
    }

    walk(value);
    return out.slice(0, 1000);
  }
  async function probeOneUrl(url) {
    const attempt = {
      url,
      startedAt: new Date().toISOString(),
      status: null,
      ok: false,
      contentType: "",
      body: null,
      error: ""
    };

    try {
      const response = await fetch(url, {
        credentials: "include",
        headers: { "Accept": "application/json, text/plain, */*" }
      });

      attempt.status = response.status;
      attempt.ok = response.ok;
      attempt.contentType = (response.headers.get("content-type") || "").toLowerCase();

      const text = await response.text();
      if (attempt.contentType.includes("json")) {
        try { attempt.body = JSON.parse(text); }
        catch (_) { attempt.body = { parseError: true, text: text.slice(0, 20000) }; }
      } else {
        attempt.body = {
          textSnippet: text.slice(0, 20000),
          length: text.length
        };
      }
    } catch (error) {
      attempt.error = String(error?.message || error);
    }
    return attempt;
  }

  function compactOutline(items) {
    return (items || []).map((item) => ({
      id: firstText(item.id),
      objectId: firstText(item.objectId),
      assessmentId: firstText(item.assessmentId),
      parentId: firstText(item.parentId),
      title: cleanText(firstText(item.title)),
      type: firstText(item.type),
      handlerId: firstText(item.handlerId),
      url: firstText(item.url),
      downloadUrl: firstText(item.downloadUrl),
      documentHtml: item.type === "document" ? documentMarkupFromItem(item) : "",
      mimeType: firstText(item.mimeType),
      children: compactOutline(item.children || [])
    }));
  }

  function cacheProbeOutline(key, result) {
    if (!key || !result?.outline) return;
    state.courseOutlineCache.set(key, {
      outline: compactOutline(result.outline),
      finishedAt: result.finishedAt || new Date().toISOString()
    });
    changed();
  }

  async function preloadKnownCourses(recordsOverride = null) {
    if (preloadRunning) return;
    let records = Array.isArray(recordsOverride)
      ? recordsOverride
      : exactCourseRecordsFromNetwork();
    if (!records.length) return;

    preloadRunning = true;
    try {
      const pending = records.filter((record) => {
        const key = exactCourseKey(record);
        return key && state.courseProbeStatus.get(key) !== "loading";
      });

      // A few courses in parallel is substantially faster than serial loading
      // without creating a huge burst of requests against Blackboard.
      const CONCURRENCY = 2;
      let cursor = 0;

      async function worker() {
        while (cursor < pending.length) {
          const index = cursor++;
          const record = pending[index];
          try {
            await probeCourseData(record, true, { silent: true, fast: true });
          } catch (error) {
            diagEvent("course-preload-failed", {
              course: record.displayName,
              error: String(error?.message || error)
            });
          }
        }
      }

      await Promise.all(
        Array.from({ length: Math.min(CONCURRENCY, pending.length) }, () => worker())
      );
    } finally {
      preloadRunning = false;
      save();
    }
  }

  async function probeCourseData(record, force = false, options = {}) {
    const silent = options?.silent === true;
    const fast = options?.fast === true;
    const key = exactCourseKey(record);
    if (!key) return null;
    if (state.courseProbeStatus.get(key) === "loading") return null;
    if (!force && state.courseProbeResults.has(key)) {
      return state.courseProbeResults.get(key);
    }

    state.courseProbeStatus.set(key, "loading");
    if (!silent) render();

    const result = {
      course: {
        id: record.id,
        displayName: record.displayName,
        termName: record.termName
      },
      initiatedFrom: location.href,
      startedAt: new Date().toISOString(),
      attempts: [],
      contentNodes: [],
      folders: [],
      fileCandidates: [],
      outline: [],
      observedWhileBrowsingCourse: state.courseObservedNetwork.get(key) || []
    };

    const critical = [];
    const high = [];
    const normal = [];
    const low = [];
    const queuedUrls = new Set();
    const seenUrls = new Set();
    const discoveredContentIds = new Set();
    const preferredId = preferredUltraCourseId(record);
    const identifiers = fast && preferredId
      ? [preferredId]
      : courseIdentifierCandidates(record);

    const enqueue = (url, priority = "normal") => {
      if (!url) return;
      try {
        const parsed = new URL(url, location.href);
        if (parsed.origin !== location.origin) return;
        if (queuedUrls.has(parsed.href) || seenUrls.has(parsed.href)) return;
        queuedUrls.add(parsed.href);
        (
          priority === "critical" ? critical :
          priority === "high" ? high :
          priority === "low" ? low :
          normal
        ).push(parsed.href);
      } catch (_) {}
    };

    const nextUrl = () => {
      const url =
        critical.shift() ||
        high.shift() ||
        normal.shift() ||
        low.shift() ||
        "";
      if (url) queuedUrls.delete(url);
      return url;
    };

    const enqueueChildren = (contentId) => {
      if (!contentId) return;
      for (const courseId of identifiers) {
        const c = encodeURIComponent(courseId);
        const item = encodeURIComponent(contentId);
        // Children are highest priority: this guarantees folder-in-folder
        // traversal completes before attachment fallback requests can consume
        // the safety request cap.
        enqueue(
          `${location.origin}/learn/api/public/v1/courses/${c}/contents/${item}/children?limit=200`,
          "high"
        );
      }
    };

    const enqueueDetail = (contentId, priority = "normal") => {
      if (!contentId) return;
      for (const courseId of identifiers) {
        enqueue(
          `${location.origin}/learn/api/public/v1/courses/${encodeURIComponent(courseId)}` +
          `/contents/${encodeURIComponent(contentId)}`,
          priority
        );
      }
    };

    const enqueueAttachmentFallback = (contentId) => {
      if (!contentId) return;
      for (const courseId of identifiers) {
        const c = encodeURIComponent(courseId);
        const item = encodeURIComponent(contentId);
        // These are intentionally low priority. Many Ultra content types do
        // not support attachment endpoints, so 400/404 responses here should
        // never prevent tree traversal.
        enqueue(
          `${location.origin}/learn/api/public/v1/courses/${c}/contents/${item}/attachments?limit=200`,
          "low"
        );
        enqueue(
          `${location.origin}/learn/api/public/v1/courses/${c}/contents/${item}/attachment?limit=200`,
          "low"
        );
      }
    };

    // The student preload starts with the single useful root content call.
    // Debug/full mode keeps the broader diagnostics.
    for (const courseId of identifiers) {
      const c = encodeURIComponent(courseId);
      enqueue(`${location.origin}/learn/api/public/v1/courses/${c}/contents?limit=200`, "high");

      if (!fast) {
        enqueue(`${location.origin}/learn/api/public/v1/courses/${c}/resources?limit=200`, "normal");
        enqueue(`${location.origin}/learn/api/public/v3/courses/${c}`, "normal");
        enqueue(`${location.origin}/learn/api/public/v1/courses/${c}`, "normal");
      }
    }

    if (!fast) {
      for (const url of state.learnedCourseEndpoints.get(key) || []) enqueue(url, "normal");
      for (const url of collectSameOriginUrls(record?.rawCourse || {})) enqueue(url, "normal");
    }

    for (const observed of result.observedWhileBrowsingCourse) {
      if (!observed?.body || typeof observed.body !== "object") continue;
      result.contentNodes.push(...contentNodesFromValue(observed.body));
      result.fileCandidates.push(...attachmentCandidatesFromValue(observed.body));
      result.fileCandidates.push(...fileCandidatesFromValue(observed.body));
    }

    const MAX_REQUESTS = fast ? 220 : 260;

    const REQUEST_CONCURRENCY = 4;
    let lastPartialSignature = "";
    let lastPartialRequestCount = 0;
    while (
      (critical.length || high.length || normal.length || low.length) &&
      result.attempts.length < MAX_REQUESTS
    ) {
      const batchUrls = [];
      while (batchUrls.length < REQUEST_CONCURRENCY &&
             result.attempts.length + batchUrls.length < MAX_REQUESTS &&
             (critical.length || high.length || normal.length || low.length)) {
        const url = nextUrl();
        if (!url || seenUrls.has(url)) continue;
        seenUrls.add(url);
        batchUrls.push(url);
      }
      if (!batchUrls.length) break;
      const attempts = await BBCourseWork.mapLimit(batchUrls, REQUEST_CONCURRENCY, probeOneUrl);
      for (const attempt of attempts) {
      result.attempts.push(attempt);
      if (!(attempt.ok && attempt.body && typeof attempt.body === "object")) continue;

      const nodes = contentNodesFromValue(attempt.body);
      const foundFiles = [
        ...attachmentCandidatesFromValue(attempt.body),
        ...fileCandidatesFromValue(attempt.body)
      ];

      result.contentNodes.push(...nodes);
      result.fileCandidates.push(...foundFiles);

      const lowerUrl = attempt.url.toLowerCase();

      for (const node of nodes) {
        const kind = ultraNodeKind(node);
        const type = classifyContentNode(node);

        if (kind === "folder" || kind === "documentWrapper" || kind === "learningModule") {
          // Resolve the container itself before recursively expanding branches.
          // This prevents a sibling document wrapper from being starved behind
          // a large tree of nested modules/folders.
          if (fast) enqueueDetail(node.id, "critical");
          enqueueChildren(node.id);
          continue;
        }

        // Fast preload only needs detail for the x-bb-document body, where
        // embedded attachment BBML may live. Files/links/assessments already
        // contain enough IDs to construct their destinations.
        if (fast) {
          if (kind === "documentBody") enqueueDetail(node.id, "critical");
        } else {
          if (["document", "file", "link", "assessment", "content"].includes(type)) {
            enqueueDetail(node.id);
          }

          if (type === "document" || type === "file") {
            const nodeAlreadyHasDirectFile =
              attachmentCandidatesFromValue(node.raw || {}).some((f) => f.url);
            if (!nodeAlreadyHasDirectFile) enqueueAttachmentFallback(node.id);
          }

          for (const linked of collectSameOriginUrls(node.raw || {})) {
            try {
              const linkedUrl = new URL(linked);
              if (/\/api\/|\/learn\/api\//i.test(linkedUrl.pathname)) {
                enqueue(linkedUrl.href, "normal");
              }
            } catch (_) {}
          }
        }
      }

      // A children listing can contain another folder even when the parent
      // folder is nested several levels deep. Classify each returned result
      // and recurse only when Blackboard says it is a container/has children.
      const results = Array.isArray(attempt.body?.results) ? attempt.body.results : [];
      if (/\/contents(?:\/[^/?]+\/children|\?|$)/i.test(lowerUrl)) {
        for (const item of results.slice(0, 200)) {
          const contentId = firstText(item?.id, item?.contentId);
          if (!contentId) continue;

          discoveredContentIds.add(contentId);

          const handler = firstText(item?.contentHandler?.id, item?.type, item?.kind);
          const isContainer =
            /folder|lesson|learning.?module|module|container/i.test(handler);

          if (fast) {
            // Always resolve every listed content object first. Listing payloads
            // can omit isBbPage/contentHandler details, so relying on the list
            // alone can silently lose one sibling document.
            enqueueDetail(contentId, "critical");
          } else {
            enqueueDetail(contentId, "normal");
          }

          if (isContainer) enqueueChildren(contentId);
        }
      }

      if (/\/resources(?:\?|$)/i.test(lowerUrl)) {
        for (const item of results.slice(0, 200)) {
          const resourceId = firstText(item?.id, item?.resourceId);
          if (!resourceId) continue;
          if (
            item?.hasChildren ||
            /folder|lesson|learning.?module|module/i.test(firstText(item?.contentHandler?.id, item?.type, item?.kind))
          ) {
            for (const courseId of identifiers) {
              enqueue(
                `${location.origin}/learn/api/public/v1/courses/${encodeURIComponent(courseId)}` +
                `/resources/${encodeURIComponent(resourceId)}/children?limit=200`,
                "high"
              );
            }
          }
        }
      }
      }
      if (typeof options.onDiscovery === "function") {
        const partialOutline = buildCourseOutline(result.contentNodes, result.fileCandidates, record);
        const partialJobs = buildCourseIngestJobs(record, partialOutline).filter((item) =>
          item.kind === "fetch" || item.kind === "markup" ||
          (item.kind === "unresolved" && item.reason === "no-download-url" && item.parentId)
        );
        const signature = partialJobs.map((item) => `${item.itemId}:${item.kind}:${item.url || ""}`).sort().join("|");
        const enoughNewWork = !lastPartialSignature ||
          result.attempts.length - lastPartialRequestCount >= 12;
        if (partialJobs.length && signature !== lastPartialSignature && enoughNewWork) {
          lastPartialSignature = signature;
          lastPartialRequestCount = result.attempts.length;
          try {
            options.onDiscovery({
              ...result,
              attempts: result.attempts.slice(),
              contentNodes: result.contentNodes.slice(),
              fileCandidates: result.fileCandidates.slice(),
              outline: partialOutline,
              partial: true
            });
          } catch (error) {
            diagEvent("course-probe-partial-update-failed", {
              course: record.displayName,
              error: String(error?.message || error)
            });
          }
        }
      }
    }

    const nodeMap = new Map();
    for (const node of result.contentNodes) {
      const nodeKey = node.id
        ? `id:${node.id}`
        : `${node.parentId}::${node.title}::${node.handlerId}`;
      const existing = nodeMap.get(nodeKey);
      if (!existing) {
        nodeMap.set(nodeKey, node);
      } else {
        // Prefer richer versions returned by single-item detail endpoints.
        nodeMap.set(nodeKey, {
          ...existing,
          ...node,
          raw: { ...(existing.raw || {}), ...(node.raw || {}) },
          url: firstText(node.url, existing.url)
        });
      }
    }
    result.contentNodes = [...nodeMap.values()];

    const resolvedContentIds = new Set(
      result.contentNodes.map((node) => firstText(node.id)).filter(Boolean)
    );
    result.discoveredContentIds = [...discoveredContentIds];
    result.unresolvedContentIds = [...discoveredContentIds].filter(
      (id) => !resolvedContentIds.has(id)
    );

    result.folders = result.contentNodes.filter((node) => classifyContentNode(node) === "folder");

    const fileMap = new Map();
    for (const file of result.fileCandidates) {
      const parentId = firstText(file?.parentId, file?.raw?.contentId, file?.raw?.parentId);
      const name = cleanText(firstText(file?.name)).toLowerCase();
      const id = firstText(file?.id);
      const key = id
        ? `id:${id}`
        : `ctx:${parentId}::${name}::${firstText(file?.mimeType).toLowerCase()}`;

      const existing = fileMap.get(key);
      const score = (candidate) =>
        (candidate?.url ? 10 : 0) +
        (/\/bbcswebdav\//i.test(firstText(candidate?.url)) ? 10 : 0) +
        (candidate?.mimeType ? 2 : 0);

      if (!existing || score(file) > score(existing)) fileMap.set(key, file);
    }
    result.fileCandidates = [...fileMap.values()];

    result.outline = buildCourseOutline(result.contentNodes, result.fileCandidates, record);
    result.finishedAt = new Date().toISOString();
    result.requestCapReached =
      result.attempts.length >= MAX_REQUESTS &&
      (critical.length || high.length || normal.length || low.length);

    state.courseProbeResults.set(key, result);
    state.courseProbeStatus.set(key, "done");
    cacheProbeOutline(key, result);
    diagEvent("course-data-probe", {
      course: record.displayName,
      attempts: result.attempts.length,
      successful: result.attempts.filter((a) => a.ok).length,
      successfulJson: result.attempts.filter((a) => a.ok && a.contentType.includes("json")).length,
      contentNodes: result.contentNodes.length,
      folders: result.folders.length,
      files: result.fileCandidates.length,
      assessments: flattenCourseOutline(result.outline).filter((x) => x.type === "assessment").length,
      documents: flattenCourseOutline(result.outline).filter((x) => x.type === "document").length,
      discoveredContentIds: result.discoveredContentIds.length,
      unresolvedContentIds: result.unresolvedContentIds,
      requestCapReached: result.requestCapReached,
      initiatedFrom: result.initiatedFrom
    });
    if (!silent) render();
    return result;
  }


  function prettyJson(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch (error) {
      return JSON.stringify({ error: String(error) }, null, 2);
    }
  }

  function diagPre(value) {
    const pre = document.createElement("pre");
    pre.className = "bbx-json";
    pre.textContent = prettyJson(value);
    return pre;
  }

  function makeTabButton(id, label) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "bbx-tab-button";
    button.textContent = label;
    button.setAttribute("aria-selected", state.diagnosticTab === id ? "true" : "false");
    if (state.diagnosticTab === id) button.classList.add("bbx-active");

    button.addEventListener("click", () => {
      state.diagnosticTab = id;
      save();
      render();
    });
    return button;
  }

  function renderCoursesTab(container, records) {
    syncSelectedTermToExactCourses(records);
    const terms = availableExactTerms(records);

    const controls = document.createElement("div");
    controls.className = "bbx-course-controls";

    const label = document.createElement("label");
    label.htmlFor = "bbx-exact-term-select";
    label.textContent = "Term";

    const select = document.createElement("select");
    select.id = "bbx-exact-term-select";

    if (!terms.length) {
      const option = document.createElement("option");
      option.textContent = "No labeled course terms captured";
      option.value = "";
      select.append(option);
      select.disabled = true;
    } else {
      for (const term of terms) {
        const option = document.createElement("option");
        option.value = term;
        option.textContent = term;
        option.selected = term === state.selectedTerm;
        select.append(option);
      }
    }

    select.addEventListener("change", () => {
      state.selectedTerm = select.value;
      save();
      render();
    });

    const detected = document.createElement("div");
    detected.className = "bbx-detected-term";
    const detectedValue = detectedPageTermSafe();
    detected.textContent = detectedValue
      ? `Current-page detected term: ${detectedValue}`
      : "Current-page detected term: none";

    controls.append(label, select, detected);
    container.append(controls);

    const filtered = records.filter((r) => r.termName === state.selectedTerm);

    const count = document.createElement("div");
    count.className = "bbx-course-count";
    count.textContent = state.selectedTerm
      ? `${filtered.length} Blackboard course${filtered.length === 1 ? "" : "s"} in ${state.selectedTerm}`
      : `${records.length} labeled Blackboard courses captured`;
    container.append(count);

    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "bbx-empty-state";
      empty.textContent =
        "No captured body.results[*].course objects match this term yet. " +
        "Browse the Blackboard Courses page for that term and reopen B+.";
      container.append(empty);
      return;
    }

    const list = document.createElement("div");
    list.className = "bbx-exact-course-list";

    for (const record of filtered) {
      const details = document.createElement("details");
      details.className = "bbx-exact-course";

      const summary = document.createElement("summary");

      const title = document.createElement("span");
      title.className = "bbx-exact-course-title";
      title.textContent = record.displayName;

      const term = document.createElement("span");
      term.className = "bbx-exact-course-term";
      term.textContent = record.termName;

      summary.append(title, term);

      const provenance = document.createElement("div");
      provenance.className = "bbx-provenance";
      provenance.textContent =
        `Diagnostics.Network[${record.source.networkIndex}].Body.results[${record.source.resultIndex}].course`;

      const rawHeading = document.createElement("div");
      rawHeading.className = "bbx-subheading";
      rawHeading.textContent = "Raw course object";

      details.append(summary, provenance, rawHeading, diagPre(record.rawCourse));
      list.append(details);
    }

    container.append(list);
  }


  function renderOutlineItem(item, depth = 0) {
    const row = document.createElement("div");
    row.className = `bbx-outline-item bbx-outline-${item.type}`;
    row.style.setProperty("--bbx-depth", String(depth));

    const icon = document.createElement("span");
    icon.className = "bbx-outline-icon";
    icon.textContent = ({
      folder: "▸",
      learningModule: "▣",
      document: "▤",
      file: "⇩",
      link: "↗",
      assessment: "✓",
      content: "•"
    })[item.type] || "•";

    const main = document.createElement("div");
    main.className = "bbx-outline-main";

    const title = item.url
      ? document.createElement("a")
      : document.createElement("span");

    title.className = "bbx-outline-title";
    title.textContent = item.title || "(untitled)";
    if (item.url) {
      title.href = item.url;
      title.target = "_blank";
      title.rel = "noopener noreferrer";
    }

    const meta = document.createElement("div");
    meta.className = "bbx-outline-meta";
    meta.textContent = [
      contentTypeLabel(item.type),
      item.mimeType,
      item.handlerId,
      item.type === "assessment" && item.assessmentId
        ? `assessmentId=${item.assessmentId}`
        : (item.id ? `id=${item.id}` : "")
    ].filter(Boolean).join(" · ");

    main.append(title, meta);
    row.append(icon, main);

    const wrapper = document.createElement("div");
    wrapper.className = "bbx-outline-wrapper";
    wrapper.append(row);

    if (item.children?.length) {
      const children = document.createElement("div");
      children.className = "bbx-outline-children";
      for (const child of item.children) {
        children.append(renderOutlineItem(child, depth + 1));
      }
      wrapper.append(children);
    }

    return wrapper;
  }

  function renderTypedSection(container, titleText, items) {
    const heading = document.createElement("div");
    heading.className = "bbx-tab-heading";
    heading.textContent = `${titleText} (${items.length})`;
    container.append(heading);

    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "bbx-empty-state";
      empty.textContent = `No ${titleText.toLowerCase()} identified in the current probe.`;
      container.append(empty);
      return;
    }

    const list = document.createElement("div");
    list.className = "bbx-flat-type-list";

    for (const item of items) {
      const row = document.createElement("div");
      row.className = `bbx-flat-type-row bbx-outline-${item.type}`;

      const type = document.createElement("span");
      type.className = "bbx-type-pill";
      type.textContent = contentTypeLabel(item.type);

      const title = item.url
        ? document.createElement("a")
        : document.createElement("span");
      title.textContent = item.title;
      if (item.url) {
        title.href = item.url;
        title.target = "_blank";
        title.rel = "noopener noreferrer";
      }

      row.append(type, title);
      list.append(row);
    }

    container.append(list);
  }

  function renderCourseDataTab(container, records) {
    syncSelectedTermToExactCourses(records);
    const termRecords = state.selectedTerm
      ? records.filter((r) => r.termName === state.selectedTerm)
      : records;

    const header = document.createElement("div");
    header.className = "bbx-course-data-header";

    const termLine = document.createElement("div");
    termLine.className = "bbx-tab-hint";
    termLine.textContent =
      `Testing access from: ${location.pathname}${location.search}${location.hash}`;

    const select = document.createElement("select");
    select.className = "bbx-course-data-select";

    if (!termRecords.length) {
      const option = document.createElement("option");
      option.textContent = "No labeled courses captured";
      option.value = "";
      select.append(option);
      select.disabled = true;
      header.append(termLine, select);
      container.append(header);
      return;
    }

    if (!state.selectedProbeCourse ||
        !termRecords.some((r) => exactCourseKey(r) === state.selectedProbeCourse)) {
      state.selectedProbeCourse = exactCourseKey(termRecords[0]);
    }

    for (const record of termRecords) {
      const option = document.createElement("option");
      option.value = exactCourseKey(record);
      option.textContent = record.displayName;
      option.selected = option.value === state.selectedProbeCourse;
      select.append(option);
    }

    select.addEventListener("change", () => {
      state.selectedProbeCourse = select.value;
      save();
      render();
    });

    header.append(termLine, select);
    container.append(header);

    const record = termRecords.find((r) => exactCourseKey(r) === state.selectedProbeCourse) || termRecords[0];
    const key = exactCourseKey(record);
    const status = state.courseProbeStatus.get(key) || "";
    const probe =
      state.courseProbeResults.get(key) ||
      state.courseOutlineCache.get(key);
    const observedCount = state.courseObservedNetwork.get(key)?.length || 0;

    const actions = document.createElement("div");
    actions.className = "bbx-course-data-actions";

    const probeButton = document.createElement("button");
    probeButton.type = "button";
    probeButton.className = "bbx-copy-button";
    probeButton.textContent = status === "loading" ? "Probing…" : (probe ? "Probe again" : "Probe course data");
    probeButton.disabled = status === "loading";
    probeButton.addEventListener("click", () => probeCourseData(record, true));

    const learned = state.learnedCourseEndpoints.get(key)?.size || 0;
    const note = document.createElement("div");
    note.className = "bbx-tab-hint";
    note.textContent =
      `${learned} course-specific endpoint${learned === 1 ? "" : "s"} learned; ` +
      `${observedCount} JSON response${observedCount === 1 ? "" : "s"} captured while actually browsing this course. ` +
      "Folder children are traversed recursively before attachment fallbacks; direct file/external URLs are preferred when Blackboard exposes them.";

    actions.append(probeButton, note);
    container.append(actions);

    if (!probe && status !== "loading") {
      const empty = document.createElement("div");
      empty.className = "bbx-empty-state";
      empty.textContent =
        "No probe has run yet. Run this from /Ultra/Course first. If content is incomplete, " +
        "enter the course, open Course Content and one folder/module, then reopen B+ and probe again.";
      container.append(empty);
      return;
    }

    if (status === "loading") {
      const loading = document.createElement("div");
      loading.className = "bbx-empty-state";
      loading.textContent =
        "Recursively querying course metadata, content containers, folder children, resources, and attachments…";
      container.append(loading);
      return;
    }

    const successes = probe.attempts.filter((a) => a.ok);
    const jsonSuccesses = successes.filter((a) => a.contentType.includes("json"));

    const outlineFlatForStats = flattenCourseOutline(probe.outline || []);
    const stats = document.createElement("div");
    stats.className = "bbx-probe-stats bbx-probe-stats-wide";
    stats.append(
      makeStat("Requests", probe.attempts.length),
      makeStat("JSON OK", jsonSuccesses.length),
      makeStat("Folders", outlineFlatForStats.filter((x) => x.type === "folder").length),
      makeStat("Docs", outlineFlatForStats.filter((x) => x.type === "document").length),
      makeStat("Links", outlineFlatForStats.filter((x) => x.type === "link").length),
      makeStat("Assess.", outlineFlatForStats.filter((x) => x.type === "assessment").length),
      makeStat("Files", outlineFlatForStats.filter((x) => x.type === "file").length)
    );
    container.append(stats);

    const verdict = document.createElement("div");
    verdict.className = "bbx-access-summary";
    if (probe.fileCandidates.length) {
      verdict.textContent =
        `Content/file access confirmed. ${probe.contentNodes.length} content node(s), ` +
        `${probe.folders.length} folder/container(s), and ${probe.fileCandidates.length} file/resource candidate(s) were found.`;
    } else if (probe.contentNodes.length) {
      verdict.textContent =
        `The course content tree is accessible (${probe.contentNodes.length} node(s), ` +
        `${probe.folders.length} folder/container(s)), but no attachment/file object has been identified yet.`;
    } else if (jsonSuccesses.length) {
      verdict.textContent =
        "Structured JSON access is working, but none of the successful responses look like course-content nodes.";
    } else {
      verdict.textContent =
        "No tested course-data endpoint returned successful JSON. HTTP 200 may just be an HTML Ultra shell; compare HTTP OK with JSON OK.";
    }
    container.append(verdict);

    if (probe.requestCapReached) {
      const warning = document.createElement("div");
      warning.className = "bbx-empty-state";
      warning.textContent =
        "Traversal hit the safety cap before all queued branches were exhausted. " +
        "The folder-first queue means nested folders were prioritized, but the diagnostic requests below can show what remains.";
      container.append(warning);
    }

    const flatOutline = flattenCourseOutline(probe.outline || []);
    const assessments = flatOutline.filter((x) => x.type === "assessment");
    const learningModules = flatOutline.filter((x) => x.type === "learningModule");
    const links = flatOutline.filter((x) => x.type === "link");
    const documents = flatOutline.filter((x) => x.type === "document");
    const files = flatOutline.filter((x) => x.type === "file");

    const outlineHeading = document.createElement("div");
    outlineHeading.className = "bbx-tab-heading";
    outlineHeading.textContent = `Course outline (${flatOutline.length})`;
    container.append(outlineHeading);

    if (probe.outline?.length) {
      const outline = document.createElement("div");
      outline.className = "bbx-course-outline";
      for (const item of probe.outline) {
        outline.append(renderOutlineItem(item, 0));
      }
      container.append(outline);
    } else {
      const emptyOutline = document.createElement("div");
      emptyOutline.className = "bbx-empty-state";
      emptyOutline.textContent = "No typed course outline could be built from the current probe.";
      container.append(emptyOutline);
    }

    renderTypedSection(container, "Learning Modules", learningModules);
    renderTypedSection(container, "Assessments", assessments);
    renderTypedSection(container, "Links", links);
    renderTypedSection(container, "Documents", documents);
    renderTypedSection(container, "Files", files);

    if (probe.folders.length) {
      const heading = document.createElement("div");
      heading.className = "bbx-tab-heading";
      heading.textContent = `Folders / containers (${probe.folders.length})`;
      container.append(heading);

      for (const folder of probe.folders.slice(0, 100)) {
        const details = document.createElement("details");
        details.className = "bbx-probe-attempt";
        const summary = document.createElement("summary");
        summary.textContent =
          `${folder.title || "(untitled folder)"} · id=${folder.id || "?"} · ${folder.handlerId || "container"}`;
        details.append(summary, diagPre(folder));
        container.append(details);
      }
    }

    if (probe.contentNodes.length) {
      const heading = document.createElement("div");
      heading.className = "bbx-tab-heading";
      heading.textContent = `All content nodes (${probe.contentNodes.length})`;
      container.append(heading);

      const details = document.createElement("details");
      details.className = "bbx-probe-attempt";
      const summary = document.createElement("summary");
      summary.textContent = "View parsed content tree nodes";
      details.append(summary, diagPre(probe.contentNodes));
      container.append(details);
    }


    if (probe.observedWhileBrowsingCourse?.length) {
      const observed = document.createElement("details");
      observed.className = "bbx-probe-attempt";
      const summary = document.createElement("summary");
      summary.textContent =
        `Raw JSON captured while browsing this course (${probe.observedWhileBrowsingCourse.length})`;
      observed.append(summary, diagPre(probe.observedWhileBrowsingCourse));
      container.append(observed);
    }

    const attemptsHeading = document.createElement("div");
    attemptsHeading.className = "bbx-tab-heading";
    attemptsHeading.textContent = "Course-data request diagnostics";
    container.append(attemptsHeading);

    for (const attempt of probe.attempts) {
      const details = document.createElement("details");
      details.className = "bbx-probe-attempt";

      const summary = document.createElement("summary");
      const jsonLabel = attempt.contentType.includes("json") ? " JSON" : "";
      summary.textContent =
        `${attempt.status ?? "ERR"} ${attempt.ok ? "OK" : ""}${jsonLabel} · ${attempt.url}`;

      details.append(summary, diagPre(attempt));
      container.append(details);
    }
  }


  function renderRawTab(container) {
    const heading = document.createElement("div");
    heading.className = "bbx-tab-heading";
    heading.textContent = `${state.diagnostics.network.length} captured Blackboard JSON responses`;

    const hint = document.createElement("div");
    hint.className = "bbx-tab-hint";
    hint.textContent =
      "These are the raw same-origin JSON response objects observed in this page session. " +
      "Course extraction in this build only uses body.results[*].course.displayName and body.results[*].course.term.name. " +
      "Up to the 24 most recent JSON responses are retained in memory.";

    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "bbx-copy-button";
    copy.textContent = "Copy raw Blackboard JSON";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(prettyJson(state.diagnostics.network));
        copy.textContent = "Copied";
        setTimeout(() => { copy.textContent = "Copy raw Blackboard JSON"; }, 1200);
      } catch (_) {
        copy.textContent = "Copy failed";
      }
    });

    container.append(heading, hint, copy, diagPre(state.diagnostics.network));
  }

  function renderPageTab(container) {
    const page = {
      url: location.href,
      title: document.title,
      detectedTerm: detectedPageTermSafe(),
      storedSelectedTerm: state.selectedTerm,
      termRelatedElements: state.diagnostics.terms,
      domCourseCandidates: state.diagnostics.domCourses,
      eventLog: state.diagnostics.events
    };
    container.append(diagPre(page));
  }


  function documentMarkupFromItem(item) {
    if (typeof item?.documentHtml === "string" && item.documentHtml.trim()) {
      return item.documentHtml;
    }

    const raw = item?.raw || {};

    const directCandidates = [
      raw?.body?.body,
      raw?.body?.description,
      raw?.body?.content,
      raw?.body?.text,
      raw?.wrapper?.body,
      raw?.wrapper?.description,
      raw?.body,
      raw?.description,
      raw?.content,
      raw?.text
    ];

    for (const candidate of directCandidates) {
      if (typeof candidate === "string" && candidate.trim()) {
        return candidate;
      }
    }

    // Blackboard payload shapes can vary slightly. Search only plausible
    // document-text fields rather than serializing the entire raw object.
    const seen = new WeakSet();
    const found = [];

    function walk(node, depth = 0) {
      if (depth > 6 || node == null || typeof node !== "object") return;
      if (seen.has(node)) return;
      seen.add(node);

      if (Array.isArray(node)) {
        for (const child of node.slice(0, 100)) walk(child, depth + 1);
        return;
      }

      for (const [key, value] of Object.entries(node).slice(0, 200)) {
        if (
          typeof value === "string" &&
          /^(body|description|content|text|html|bbml)$/i.test(key) &&
          value.trim()
        ) {
          found.push(value);
        } else if (value && typeof value === "object") {
          walk(value, depth + 1);
        }
      }
    }

    walk(raw);

    found.sort((a, b) => b.length - a.length);
    return found[0] || "";
  }

  // ---------------------------------------------------------------------
  // Study library ingestion.
  //
  // Fetches bytes into memory (never touching the filesystem) and hands
  // them to background.js to parse into IR blocks (see lib/ir.js) and
  // store in IndexedDB (see lib/db.js) for the schedule/Q&A/practice-problem
  // features to read back later.

  // Text-like files (code, notes, data) are indexed verbatim as text/code
  // blocks - no vendored library needed.
  const TEXT_EXTENSIONS = new Set([
    "txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml", "tex", "bib", "log",
    "py", "ipynb", "java", "c", "h", "cpp", "cc", "hpp", "cs", "js", "ts", "jsx", "tsx", "rb",
    "go", "rs", "swift", "kt", "m", "r", "sql", "sh", "bat", "ps1", "s", "asm", "hs", "ml",
    "scala", "pl", "php", "lua", "jl", "v", "vhd", "sv", "mat", "rmd", "css"
  ]);
  const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"]);
  const MEDIA_EXTENSIONS = new Set(["mp4", "mov", "m4v", "webm", "avi", "mkv", "mp3", "m4a", "wav", "aac", "ogg"]);

  function sourceTypeForMime(mimeType) {
    const m = String(mimeType || "").toLowerCase().split(";")[0].trim();
    if (!m || m === "application/octet-stream" || m === "binary/octet-stream") return null; // says nothing - use the filename
    if (m.includes("pdf")) return "pdf";
    if (m.includes("wordprocessingml")) return "docx";
    if (m.includes("presentationml")) return "pptx";
    if (m.includes("html")) return "html";
    if (m.startsWith("image/")) return "image";
    if (m.startsWith("video/") || m.startsWith("audio/")) return "media";
    if (m.startsWith("text/") || m.includes("json") || m.includes("xml") || m.includes("x-python") || m.includes("javascript")) return "text";
    return null;
  }

  function sourceTypeForFilename(name) {
    const ext = (String(name || "").split(".").pop() || "").toLowerCase();
    if (ext === "pdf") return "pdf";
    if (ext === "docx") return "docx";
    if (ext === "pptx") return "pptx";
    if (ext === "html" || ext === "htm") return "html";
    if (TEXT_EXTENSIONS.has(ext)) return "text";
    if (IMAGE_EXTENSIONS.has(ext)) return "image";
    if (MEDIA_EXTENSIONS.has(ext)) return "media";
    return null;
  }

  // Mime first (it's what the server says), filename second (Blackboard
  // frequently reports "application/octet-stream" or nothing at all, which
  // previously made perfectly readable .html/.py files "unsupported").
  function absoluteUrl(url) {
    try { return new URL(url, location.href).href; } catch { return String(url || ""); }
  }

  function sourceTypeForItem(item) {
    return sourceTypeForMime(item?.mimeType) || sourceTypeForFilename(firstText(item?.title));
  }

  // Legacy .doc/.ppt (pre-2007 binary Office) are a different format from
  // docx/pptx - mammoth/JSZip can't read them, so they're reported as
  // unsupported instead of being sent to a parser that will throw.

  // ---- Outline walking: ONE place that knows "container vs leaf" ------
  //
  // The diagnostics panel elsewhere in this file already proves the outline
  // can contain six item types: folder, learningModule, document, file,
  // link, assessment. Until this fix, addItem() below only recognized four
  // of them - a "link" (web link) or "assessment" (quiz/test/assignment)
  // node fell through with no branch matching at all, producing *no job,
  // not even an "unresolved" one*. It didn't fail loudly or quietly log a
  // skip - it just never existed anywhere in the accounting. That's the
  // dangerous kind of bug for a "make sure everything is indexed" goal:
  // the sync could report "0 failures" while an entire course's worth of
  // assignments and links were invisible the whole time.
  //
  // Fixed by giving every leaf type an explicit outcome (including a
  // catch-all for any *future* type this outline builder ever produces),
  // and by pulling the container-recursion logic out into one walker used
  // by both the sync path (buildCourseIngestJobs) and the verification
  // path (verifyLibraryCoverage) below - so they cannot drift apart on
  // what counts as a container vs. something that needs to be accounted
  // for.

  function walkOutlineLeaves(outline, visit) {
    function walk(item) {
      if (!item) return;
      if (item.type === "folder" || item.type === "learningModule") {
        for (const child of item.children || []) walk(child);
        return;
      }
      visit(item);
      // A Blackboard "document" item can itself contain nested content
      // (it's not purely a leaf) - recurse into its children too, same as
      // the original implementation did.
      if (item.type === "document") {
        for (const child of item.children || []) walk(child);
      }
    }
    for (const item of outline || []) walk(item);
  }

  // Classifies exactly one leaf (never a container) into one of:
  //   "markup"     - a Blackboard document body, ready to ingest as html
  //   "fetch"      - a file with a resolvable download URL + supported mime
  //   "unresolved" - anything else, always with a specific machine-readable
  //                  `reason` so it can be reported, never merely dropped
  // Files found as links inside a Blackboard page have no content id of
  // their own (the scanner records id: ""). Before v2.9.4 they were all
  // stored under the same database key "" and overwrote each other. This
  // derives a stable, unique id from the page they live in plus the file's
  // bbcswebdav path - NOT the full URL, whose query string carries
  // timestamps that change on every sync.
  function stableItemId(item) {
    const own = firstText(item.id);
    if (own) return own;
    let path = "";
    try { path = new URL(firstText(item.downloadUrl, item.url), location.href).pathname; } catch (_) {}
    return `embedded:${firstText(item.parentId) || "root"}:${path || cleanText(firstText(item.title)) || "unnamed"}`;
  }

  // Text a Blackboard page body actually shows, ignoring markup.
  function visibleTextOf(markup) {
    if (!markup) return "";
    try {
      const doc = new DOMParser().parseFromString(markup, "text/html");
      doc.querySelectorAll("script,style,noscript,template").forEach((n) => n.remove());
      return (doc.body?.textContent || "").replace(/\s+/g, " ").trim();
    } catch (_) {
      return String(markup).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    }
  }

  function classifyLeafItem(item) {
    const itemId = stableItemId(item);
    const title = cleanText(firstText(item.title)) || `Untitled ${item.type || "item"}`;
    const url = firstText(item.url);

    if (item.type === "document") {
      const markup = documentMarkupFromItem(item);
      // A page with no text and no images of its own (typically a wrapper
      // whose content is an attached file, indexed separately) is not a
      // failure - it's reported as skipped, with the markup size so a page
      // whose body we failed to *find* (0 chars) is distinguishable.
      if (!visibleTextOf(markup) && !/<img[\s>]/i.test(markup)) {
        return { itemId, title, kind: "unresolved", reason: "empty-page", url: firstText(item.url), markupChars: String(markup || "").length };
      }
      return { itemId, title, kind: "markup", sourceType: "html", markup };
    }

    if (item.type === "file") {
      const sourceType = sourceTypeForItem(item);
      if (sourceType === "media") {
        return { itemId, title, kind: "unresolved", reason: "media-file", url };
      }
      if (!sourceType) {
        return { itemId, title, kind: "unresolved", reason: "unsupported-format", url, mimeType: item.mimeType };
      }
      if (!item.downloadUrl) {
        // parentId: the folder whose internal listing has this file's
        // permanentUrl (see BBStage.resolvePermanentUrls).
        return { itemId, title, kind: "unresolved", reason: "no-download-url", url, sourceType, parentId: firstText(item.parentId), mimeType: item.mimeType || "" };
      }
      // Absolute: the offscreen document (chrome-extension:// origin) does the
      // download, so a relative URL would resolve against the wrong origin.
      return { itemId, title, kind: "fetch", sourceType, url: absoluteUrl(item.downloadUrl), pageUrl: url, mimeType: item.mimeType || "" };
    }

    if (item.type === "link") {
      return { itemId, title, kind: "unresolved", reason: "external-link", url };
    }

    if (item.type === "assessment") {
      return { itemId, title, kind: "unresolved", reason: "assessment", url };
    }

    // A type this file has never seen before. Surfaced explicitly (with the
    // literal type name in the reason) rather than silently vanishing, so a
    // future Blackboard content type shows up as a visible, searchable gap
    // instead of a mysteriously-missing file.
    return { itemId, title, kind: "unresolved", reason: `unhandled-item-type:${item.type || "unknown"}`, url };
  }

  // A fetch job likely to be a PDF, from the signals available before download.
  // PDFs are routed to the backend's PyMuPDF+OCR extractor; anything we are not
  // sure about stays on the in-browser path, so we never double-download.
  function _looksPdf(item) {
    const mime = (item.mimeType || "").toLowerCase();
    if (mime.includes("pdf")) return true;
    const hay = `${item.title || ""} ${item.url || ""} ${item.filename || ""}`.toLowerCase();
    return /\.pdf(\?|#|$)/.test(hay);
  }

  // Walks the course outline to produce ingest job *descriptors*: nothing is fetched
  // here, that happens in the worker pool in runIngest() below so fetch
  // concurrency stays bounded regardless of how large a course's outline is.
  function buildCourseIngestJobs(record, outline) {
    const courseId = firstText(record.id);
    const courseName = cleanText(record.displayName) || courseId;
    const jobs = [];
    const seen = new Map(); // itemId -> job
    walkOutlineLeaves(outline, (item) => {
      const job = { ...classifyLeafItem(item), courseId, courseName };
      const prior = seen.get(job.itemId);
      if (prior) {
        // The same file linked twice is one file - keep one copy.
        if (prior.url && prior.url === job.url) return;
        // Anything else sharing an id must never overwrite it in storage.
        let n = 2;
        while (seen.has(`${job.itemId}#${n}`)) n++;
        job.itemId = `${job.itemId}#${n}`;
      }
      seen.set(job.itemId, job);
      jobs.push(job);
    });
    return jobs;
  }

  // Cross-checks a course's *live* outline against what's actually persisted
  // in IndexedDB right now - not against what a past sync's transient banner
  // claimed (that's gone the moment the drawer closes). This is the
  // authoritative "is everything really in there" answer: every indexable
  // leaf either shows up in the library or shows up in `missing`, full stop.
  // "Verify library" - an audit, not just a lookup. Earlier builds only
  // compared the library with B+'s own scan, so anything the scan
  // missed was invisible to both and the result still said "all present".
  // Per course this checks, with independent evidence where possible:
  //   1. census:   everything Ultra's own folder listing shows vs. the scan
  //   2. scan:     whether the scanner stopped early or left folders unopened
  //   3. library:  every indexable item (incl. standalone files resolved via
  //                the folder listing) is stored with real content, and every
  //                file item in the census is stored
  //   4. pages:    every file referenced in a skipped page's markup is stored
  async function verifyLibraryCoverage(onProgress) {
    const records = studentCourseRecords();
    onProgress?.("Rescanning courses…");
    await preloadKnownCourses(records); // fresh scan, so scanner flags are real, not from a cached outline
    const report = [];

    for (const [index, record] of records.entries()) {
      const key = exactCourseKey(record);
      const courseId = firstText(record.id);
      const courseName = cleanText(record.displayName) || courseId;
      onProgress?.(`Auditing ${index + 1}/${records.length}…`);
      const probe = state.courseProbeResults.get(key);
      const outline = probe?.outline || state.courseOutlineCache.get(key)?.outline || [];
      const course = { courseId, courseName, problems: [], notes: [] };
      report.push(course);

      if (!outline.length && state.courseProbeStatus.get(key) !== "done") {
        course.problems.push({ check: "scan", text: "B+ could not scan this course at all." });
        continue;
      }

      // 2. scanner truncation
      if (probe?.requestCapReached) {
        course.problems.push({ check: "scan", text: `The scanner hit its ${probe.attempts?.length || ""}-request limit before finishing; some folders may not have been scanned.` });
      }
      const unopened = probe?.unresolvedContentIds || [];
      if (unopened.length) {
        course.problems.push({ check: "scan", text: `The scanner found ${unopened.length} content id(s) it never opened: ${unopened.slice(0, 8).join(", ")}${unopened.length > 8 ? "…" : ""}` });
      }

      // library contents
      let stored = [];
      try {
        const status = await libraryStatusByCourse(courseId);
        stored = [...status.values()];
      } catch (error) {
        course.problems.push({ check: "library", text: `Could not read the library: ${error?.message || error}` });
        continue;
      }
      const storedWithContent = new Set(stored.filter((d) => d.contentful).map((d) => d.itemId));

      // 3a. every indexable item from the scan
      const jobs = buildCourseIngestJobs(record, outline);
      const indexable = jobs.filter((j) => j.kind !== "unresolved" || String(j.reason || "").startsWith("no-download-url"));
      const missing = indexable.filter((j) => !storedWithContent.has(j.itemId));
      course.indexableCount = indexable.length;
      course.indexedCount = indexable.length - missing.length;
      for (const j of missing) {
        const outcome = lastSyncOutcome.get(j.itemId);
        course.problems.push({ check: "library", text: `Not in library: ${j.title}${outcome ? ` (last sync: ${reasonLabel(outcome.reason)})` : ""}` });
      }

      // 4. files referenced by skipped pages
      const flat = flattenCourseOutline(outline);
      const byId = new Map(flat.map((item) => [firstText(item.id), item]));
      let refsChecked = 0;
      for (const page of jobs.filter((j) => j.reason === "empty-page")) {
        const refs = BBAudit.fileRefsFromMarkup(documentMarkupFromItem(byId.get(page.itemId) || {}));
        refsChecked += refs.length;
        for (const ref of BBAudit.unmatchedFileRefs(refs, stored.filter((d) => d.contentful))) {
          course.problems.push({ check: "pages", text: `Page "${page.title}" links a file that is not in the library: ${ref.name || ref.xid || ref.href}` });
        }
        if (!refs.length) {
          course.notes.push(`Page "${page.title}" has no text and no file links in its markup.`);
        }
      }
      course.pageRefsChecked = refsChecked;

      // 1 + 3b. independent census
      const roots = BBAudit.censusRoots(flat);
      if (!roots.length) {
        course.problems.push({ check: "census", text: "Could not determine where the course's content starts, so the independent census was not run." });
        continue;
      }
      let census;
      try {
        census = await BBStage.censusCourse(location.origin, courseId, roots);
      } catch (error) {
        course.problems.push({ check: "census", text: `Census failed: ${error?.message || error}` });
        continue;
      }
      course.censusCount = census.items.length;
      course.censusRequests = census.requests;
      if (census.capReached) course.problems.push({ check: "census", text: "The census hit its request limit; the comparison is partial." });
      for (const err of census.errors) {
        course.problems.push({ check: "census", text: `Could not list ${err.kind} ${err.parentId}: ${err.error}` });
      }
      // Pages with text found by the census count as indexable content.
      const textJobs = BBAudit.censusTextJobs(census.items, jobs, visibleTextOf);
      course.indexableCount += textJobs.length;
      course.indexedCount += textJobs.filter((t) => storedWithContent.has(t.itemId)).length;
      const coverage = BBAudit.censusCoverage(census.items, flat, stored, visibleTextOf);
      for (const text of coverage.problems) course.problems.push({ check: "census", text });
      course.notes.push(...coverage.notes);
    }

    return report;
  }

  function verifyReportText(report) {
    const lines = [`B+ ${chrome.runtime.getManifest().version} verify report — ${new Date().toISOString()}`, ""];
    for (const c of report || []) {
      lines.push(`== ${c.courseName}: library ${c.indexedCount ?? "?"}/${c.indexableCount ?? "?"} · census ${c.censusCount ?? "?"} items (${c.censusRequests ?? "?"} requests) · page file refs checked ${c.pageRefsChecked ?? 0}`);
      for (const p of c.problems) lines.push(`PROBLEM [${p.check}] ${p.text}`);
      for (const n of c.notes) lines.push(`note ${n}`);
      lines.push("");
    }
    return lines.join("\n");
  }

  let lastVerifyReport = null; // transient - not persisted, rebuilt each run

  async function runVerifyLibrary(button) {
    const originalText = button?.textContent || "Verify library";
    if (button) { button.disabled = true; button.textContent = "Verifying…"; }
    try {
      lastVerifyReport = await verifyLibraryCoverage((text) => { if (button) button.textContent = text; });
    } catch (error) {
      console.error("[B+ verify]", error);
      // A crashed audit must never look like a clean one.
      lastVerifyReport = [{ courseName: "Verify", problems: [{ check: "error", text: `The audit itself failed: ${error?.message || error}` }], notes: [] }];
    }
    renderVerifyBanner();
    if (button) { button.textContent = originalText; button.disabled = false; }
  }

  function renderVerifyBanner() {
    const el = document.getElementById("bbx-verify-banner");
    if (!el) return;
    el.replaceChildren();
    if (!lastVerifyReport) { el.hidden = true; return; }
    el.hidden = false;

    const problems = lastVerifyReport.flatMap((c) => c.problems.map((p) => ({ ...p, courseName: c.courseName })));
    const indexable = lastVerifyReport.reduce((n, c) => n + (c.indexableCount || 0), 0);
    const indexed = lastVerifyReport.reduce((n, c) => n + (c.indexedCount || 0), 0);
    const census = lastVerifyReport.reduce((n, c) => n + (c.censusCount || 0), 0);

    const line = document.createElement("div");
    line.className = "bbx-ingest-line";
    line.textContent = problems.length
      ? `Verify: ${problems.length} problem(s) · library ${indexed}/${indexable} · Blackboard census ${census} items`
      : `Verified: library ${indexed}/${indexable}, and an independent census of ${census} Blackboard items found nothing the scan missed.`;

    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "bbx-ingest-copy";
    copy.textContent = "Copy report";
    copy.addEventListener("click", async () => {
      const text = verifyReportText(lastVerifyReport);
      try { await navigator.clipboard.writeText(text); copy.textContent = "Copied ✓"; }
      catch (_) { console.log(text); copy.textContent = "Printed to console"; }
      setTimeout(() => { copy.textContent = "Copy report"; }, 1800);
    });
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "bbx-ingest-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.textContent = "×";
    dismiss.addEventListener("click", () => { lastVerifyReport = null; renderVerifyBanner(); });
    line.append(copy, dismiss);
    el.append(line);

    // Problems are listed openly (not in a collapsed section).
    if (problems.length) {
      const list = document.createElement("ul");
      list.className = "bbx-ingest-why";
      for (const p of problems.slice(0, 40)) {
        const li = document.createElement("li");
        li.textContent = `${p.courseName}: ${p.text}`;
        list.append(li);
      }
      if (problems.length > 40) {
        const li = document.createElement("li");
        li.textContent = `…and ${problems.length - 40} more — use Copy report.`;
        list.append(li);
      }
      el.append(list);
    }
  }


  let lastIngestSummary = null; // transient - not persisted, rebuilt each run

  function renderIngestBanner() {
    const el = document.getElementById("bbx-ingest-banner");
    if (!el) return;

    if (!lastIngestSummary || lastIngestSummary.dismissed) {
      el.hidden = true;
      el.replaceChildren();
      return;
    }

    const { succeeded = [], unresolved = [], failed = [], fetchFailed = [], courseFailures = [] } = lastIngestSummary;
    el.hidden = false;
    el.replaceChildren();

    const line = document.createElement("div");
    line.className = "bbx-ingest-line";
    const parts = [];
    if (succeeded.length) parts.push(`${succeeded.length} added to your study library`);
    if (fetchFailed.length) parts.push(`${fetchFailed.length} couldn't be downloaded`);
    if (failed.length) parts.push(`${failed.length} downloaded but couldn't be read`);
    if (unresolved.length) parts.push(`${unresolved.length} skipped (not indexable)`);
    if (courseFailures.length) parts.push(`${courseFailures.length} course(s) couldn't be scanned at all — try syncing again`);
    line.textContent = `Study library sync: ${parts.join(" · ") || "nothing to do"}`;

    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "bbx-ingest-copy";
    copy.textContent = "Copy report";
    copy.addEventListener("click", async () => {
      const text = ingestReportText(lastIngestSummary);
      try {
        await navigator.clipboard.writeText(text);
        copy.textContent = "Copied ✓";
      } catch (_) {
        console.log(text);
        copy.textContent = "Printed to console";
      }
      setTimeout(() => { copy.textContent = "Copy report"; }, 1800);
    });

    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "bbx-ingest-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.textContent = "×";
    dismiss.addEventListener("click", () => {
      lastIngestSummary.dismissed = true;
      renderIngestBanner();
    });
    line.append(copy, dismiss);
    el.append(line);

    // The reasons, visible without expanding anything: grouped counts for
    // every failure. (The full per-file list below is collapsed, which is
    // why earlier reports arrived as rows of empty bullets.)
    const failures = [...courseFailures, ...fetchFailed, ...failed];
    if (failures.length) {
      const groups = new Map();
      for (const row of failures) {
        const key = reasonGroup(row.reason);
        groups.set(key, (groups.get(key) || 0) + 1);
      }
      const why = document.createElement("ul");
      why.className = "bbx-ingest-why";
      for (const [label, count] of [...groups.entries()].sort((a, b) => b[1] - a[1])) {
        const li = document.createElement("li");
        li.textContent = `${count} × ${label}`;
        why.append(li);
      }
      el.append(why);
    }

    const problems = [...courseFailures, ...fetchFailed, ...failed, ...unresolved];
    if (!problems.length) return;

    const details = document.createElement("details");
    details.className = "bbx-ingest-details";
    const summaryEl = document.createElement("summary");
    summaryEl.textContent = "Details — files that didn't make it in, and why (downloads that failed can be uploaded manually)";
    details.append(summaryEl);

    const list = document.createElement("ul");
    list.className = "bbx-ingest-list";
    for (const row of problems) {
      const li = document.createElement("li");

      const label = document.createElement("div");
      label.className = "bbx-ingest-row-label";
      label.textContent = `${row.title || "Untitled"} — ${row.courseName || "Unknown course"}`;
      li.append(label);

      const reasonText = document.createElement("div");
      reasonText.className = "bbx-ingest-row-reason";
      reasonText.textContent = reasonLabel(row.reason);
      li.append(reasonText);

      const actions = document.createElement("div");
      actions.className = "bbx-ingest-row-actions";
      if (row.url) actions.append(safeLink(row.url, "Open in Blackboard", "bbx-ingest-open-link"));

      // Only offer manual upload for the case where the *file itself* is
      // presumably a format we can parse and Blackboard just didn't expose
      // a fetchable URL - not for formats we don't have a parser for at all,
      // since uploading those wouldn't change the outcome.
      if ((row.stage === "fetch" || String(row.reason || "").startsWith("no-download-url")) && row.courseId && row.itemId) {
        actions.append(makeManualUploadInput(row));
      }
      li.append(actions);
      list.append(li);
    }
    details.append(list);
    el.append(details);
  }

  // Groups per-file reasons that differ only in per-file detail.
  function reasonGroup(reason) {
    const r = String(reason || "");
    if (r.startsWith("fetch-failed: Blackboard sent a web page")) return "Blackboard sent a web page (login/error page) instead of the file";
    return reasonLabel(r).slice(0, 140);
  }

  function ingestReportText(summary) {
    const { succeeded = [], unresolved = [], failed = [], fetchFailed = [], courseFailures = [] } = summary || {};
    const lines = [
      `B+ ${chrome.runtime.getManifest().version} sync report — ${new Date().toISOString()}`,
      `indexed=${succeeded.length} downloadFailed=${fetchFailed.length} readFailed=${failed.length} skipped=${unresolved.length} coursesUnscanned=${courseFailures.length}`,
      ""
    ];
    for (const [label, rows] of [["COURSE SCAN FAILED", courseFailures], ["DOWNLOAD FAILED", fetchFailed], ["READ FAILED", failed], ["SKIPPED", unresolved]]) {
      for (const r of rows) lines.push(`${label} | ${r.courseName || ""} | ${r.title || ""} | ${r.stage || ""} | ${r.reason || ""}${r.markupChars !== undefined ? ` (${r.markupChars} chars of markup)` : ""}`);
    }
    return lines.join("\n");
  }

  function reasonLabel(reason) {
    switch (reason) {
      case "no-download-url":
        return "B+ hasn't found this file's download address in Blackboard's data (v2.7 couldn't either). Upload it manually for now.";
      case "unsupported-format":
        return "This file format isn't supported for local parsing yet.";
      case "missing-vendor-library:pdfjs":
        return "PDF parsing library isn't installed in this build.";
      case "missing-vendor-library:mammoth":
        return "DOCX parsing library isn't installed in this build.";
      case "missing-vendor-library:jszip":
        return "PPTX parsing library isn't installed in this build.";
      case "scanned-pdf-no-text":
        return "Scanned PDF with no text layer and no readable page images.";
      case "external-link":
        return "This is a link to an external site, not a course file — nothing to index.";
      case "assessment":
        return "This is a quiz, test, or assignment — B+ doesn't index interactive assessments yet.";
      case "empty-page":
        return "Blackboard page with no text of its own — its attached files are indexed separately.";
      case "media-file":
        return "Video/audio file — skipped (not text-indexable yet).";
      case "empty-file":
        return "The file was empty.";
      case "no-content-extracted":
        return "The file downloaded, but no text or images could be extracted from it.";
      case "outline-scan-incomplete":
        return "B+ couldn't read this course's file listing at all this run — none of its files were even attempted.";
      case "not-in-library":
        return "Not in the library, and not synced yet in this page session — run Build study library to see why.";
      case "reported-ok-but-not-in-library":
        return "The sync reported success, but the file isn't in the database — a storage bug, worth reporting.";
      case "store-failed: document not readable after write":
        return "Parsed, but couldn't be read back from the database after saving — a storage bug.";
      default:
        if (String(reason || "").startsWith("no-download-url:")) {
          return `No download address found (Blackboard folder listing failed: ${reason.slice("no-download-url:".length).trim()}). Upload it manually for now.`;
        }
        if (String(reason || "").startsWith("parse-failed:")) {
          return `Downloaded, but the parser failed (${reason.slice("parse-failed:".length).trim()}).`;
        }
        if (String(reason || "").startsWith("staging-failed:")) {
          return `Downloaded, but handing the file to the parser failed (${reason.slice("staging-failed:".length).trim()}).`;
        }
        if (String(reason || "").startsWith("fetch-failed:")) {
          return `Download failed (${reason.slice("fetch-failed:".length).trim()}).`;
        }
        if (String(reason || "").startsWith("unhandled-item-type:")) {
          return `Blackboard returned a content type B+ doesn't recognize yet (${reason.slice("unhandled-item-type:".length)}).`;
        }
        return reason || "Could not process this file.";
    }
  }

  function makeManualUploadInput(row) {
    const label = document.createElement("label");
    label.className = "bbx-ingest-upload";
    label.textContent = "Upload file";
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".pdf,.docx,.pptx,.html,.htm,.txt,.md,.py,.java,.c,.cpp,.h,.png,.jpg,.jpeg,.gif,.webp";
    input.addEventListener("change", async () => {
      const file = input.files?.[0];
      if (!file) return;
      label.textContent = "Uploading…";
      const result = await ingestUploadedFile(file, row);
      label.textContent = result.ok ? "Uploaded ✓" : `Failed: ${result.reason || "error"}`;
      if (result.ok) {
        for (const key of ["unresolved", "fetchFailed", "failed"]) {
          lastIngestSummary[key] = (lastIngestSummary[key] || []).filter((r) => r !== row);
        }
        lastSyncOutcome.delete(row.itemId);
        lastIngestSummary.succeeded = [...(lastIngestSummary.succeeded || []), result];
        setTimeout(renderIngestBanner, 900);
      }
    });
    label.append(input);
    return label;
  }

  async function ingestUploadedFile(file, row) {
    const sourceType = sourceTypeForFilename(file.name) || sourceTypeForMime(file.type);
    if (!sourceType || sourceType === "media") return { ok: false, reason: "unrecognized file type" };

    try {
      const staged = await BBStage.stageBytes(row.itemId, new Uint8Array(await file.arrayBuffer()));
      const response = await chrome.runtime.sendMessage({
        type: "BBX_INGEST_JOBS",
        jobs: [{
          kind: "staged",
          itemId: row.itemId, // reuse the original item's id so this fills the exact catalog slot
          courseId: row.courseId,
          courseName: row.courseName,
          title: row.title,
          sourceType,
          mimeType: file.type || "",
          stageKey: staged.stageKey,
          chunks: staged.chunks,
          byteLength: staged.byteLength
        }]
      });
      const result = response?.results?.[0];
      return result?.ok ? { ok: true, title: row.title, courseName: row.courseName } : { ok: false, reason: result?.reason || "parse failed" };
    } catch (error) {
      return { ok: false, reason: error?.message || String(error) };
    }
  }

  // Remembered per page session so "Verify library" can say *why* a missing
  // file is missing (its last sync outcome) instead of guessing.
  const lastSyncOutcome = new Map(); // itemId -> { reason, stage }

  async function libraryStatusByCourse(courseId) {
    const response = await chrome.runtime.sendMessage({ type: "BBX_LIBRARY_STATUS", courseId });
    if (!response?.ok) throw new Error(response?.error || "library status query failed");
    return new Map((response.items || []).map((i) => [i.itemId, i]));
  }

  async function ingestAllCourses(button) {
    const originalText = button?.textContent || "Build study library";
    if (button) { button.disabled = true; button.textContent = "Preparing…"; }

    try {
      const records = studentCourseRecords();
      if (!records.length) throw new Error("No courses are available in the selected term.");
      await preloadKnownCourses(records);

      const descriptors = [];
      const courseFailures = [];
      for (const record of records) {
        const key = exactCourseKey(record);
        const probe = state.courseProbeResults.get(key);
        const cached = state.courseOutlineCache.get(key);
        const outline = probe?.outline || cached?.outline || [];
        const probeStatus = state.courseProbeStatus.get(key) || "";

        // Empty outline + probe never reached "done" = the scan failed, not
        // an empty course. Reported, not silently skipped.
        if (!outline.length && probeStatus !== "done") {
          courseFailures.push({ itemId: null, title: "(entire course)", courseName: cleanText(record.displayName) || key, reason: "outline-scan-incomplete" });
          continue;
        }
        const courseJobs = buildCourseIngestJobs(record, outline);
        const courseId = firstText(record.id);
        const courseName = cleanText(record.displayName) || courseId;

        // The scan misses some pages entirely, and never reads Ultra's
        // "ultraDocumentBody" children, where a page's visible body lives.
        // The census (Ultra's own folder listing) finds them; every page it
        // finds with text is indexed too.
        if (button) button.textContent = `Checking ${courseName.slice(0, 18)}…`;
        try {
          const census = await BBStage.censusCourse(location.origin, courseId, BBAudit.censusRoots(flattenCourseOutline(outline)));
          const extra = BBAudit.censusTextJobs(census.items, courseJobs, visibleTextOf);
          // Pages whose text is now indexed: via their body child, or directly.
          const bodiesIndexed = new Set([...extra.map((e) => e.parentId), ...extra.map((e) => e.itemId)]);
          for (const e of extra) {
            courseJobs.push({ kind: "markup", itemId: e.itemId, courseId, courseName, title: e.title, sourceType: "html", markup: e.markup, url: "" });
          }
          // A wrapper page whose body child is now indexed isn't "skipped".
          for (let i = courseJobs.length - 1; i >= 0; i--) {
            if (courseJobs[i].reason === "empty-page" && bodiesIndexed.has(courseJobs[i].itemId)) courseJobs.splice(i, 1);
          }
          if (census.capReached || census.errors.length) {
            courseFailures.push({ itemId: null, title: "(course check)", courseName, reason: `census incomplete: ${census.capReached ? "request limit reached; " : ""}${census.errors.map((e) => `${e.parentId}: ${e.error}`).join("; ")}` });
          }
        } catch (error) {
          courseFailures.push({ itemId: null, title: "(course check)", courseName, reason: `census failed: ${error?.message || error}` });
        }

        descriptors.push(...courseJobs);
      }

      // Standalone File items: the public API gives no download address,
      // but Ultra's internal folder listing does. One listing per folder.
      const needUrl = descriptors.filter((d) => d.kind === "unresolved" && d.reason === "no-download-url" && d.parentId);
      const folders = new Map();
      for (const d of needUrl) {
        const key = `${d.courseId}::${d.parentId}`;
        if (!folders.has(key)) folders.set(key, []);
        folders.get(key).push(d);
      }
      let foldersDone = 0;
      for (const [key, items] of folders) {
        if (button) button.textContent = `Finding files ${++foldersDone}/${folders.size}…`;
        const [courseId, parentId] = key.split("::");
        try {
          const found = await BBStage.resolvePermanentUrls(location.origin, courseId, parentId);
          for (const d of items) {
            const hit = found.get(d.itemId);
            if (!hit) continue;
            d.kind = "fetch";
            d.pageUrl = d.url || "";   // the Ultra page link, kept for "Open in Blackboard"
            d.url = hit.url;           // the actual file
            d.mimeType = d.mimeType || hit.mimeType;
            delete d.reason;
          }
        } catch (error) {
          for (const d of items) d.reason = `no-download-url: ${error?.message || error}`;
        }
      }

      const unresolved = descriptors.filter((d) => d.kind === "unresolved");
      const toFetch = descriptors.filter((d) => d.kind === "fetch");
      const markupJobs = descriptors.filter((d) => d.kind === "markup");
      const results = [];

      try { await chrome.runtime.sendMessage({ type: "BBX_STAGING_CLEAR" }); } catch (_) {}

      // Phase 1 - download every file NOW, from this tab (the only download
      // path proven against real Blackboard), while the time-stamped links
      // from the fresh outline scan are still valid. Bytes are staged into
      // the extension's database as base64 chunks (see lib/stage.js).
      const stagedJobs = [];
      let fetchCursor = 0;
      let fetched = 0;
      async function downloadWorker() {
        while (fetchCursor < toFetch.length) {
          const d = toFetch[fetchCursor++];
          try {
            const staged = await BBStage.fetchAndStage(d);
            stagedJobs.push({
              kind: "staged", itemId: d.itemId, courseId: d.courseId, courseName: d.courseName,
              title: d.title, sourceType: d.sourceType, pageUrl: d.pageUrl || "",
              mimeType: d.mimeType || staged.mimeType || "",
              stageKey: staged.stageKey, chunks: staged.chunks, byteLength: staged.byteLength
            });
          } catch (error) {
            results.push({
              itemId: d.itemId, courseId: d.courseId, title: d.title, courseName: d.courseName,
              url: d.pageUrl || "", ok: false, stage: error?.stage || "fetch",
              reason: error?.message || String(error)
            });
          }
          fetched++;
          if (button) button.textContent = `Downloading ${fetched}/${toFetch.length}…`;
        }
      }
      await Promise.all(Array.from({ length: Math.min(3, toFetch.length || 1) }, downloadWorker));

      // Phase 2 - parse + store (offscreen document, reading staged bytes).
      const jobs = [
        ...markupJobs.map((d) => ({ kind: "markup", itemId: d.itemId, courseId: d.courseId, courseName: d.courseName, title: d.title, sourceType: "html", markup: d.markup, pageUrl: "" })),
        ...stagedJobs
      ];
      const BATCH_SIZE = 3;
      for (let i = 0; i < jobs.length; i += BATCH_SIZE) {
        const batch = jobs.slice(i, i + BATCH_SIZE);
        if (button) button.textContent = `Reading ${Math.min(i + BATCH_SIZE, jobs.length)}/${jobs.length}…`;
        try {
          const response = await chrome.runtime.sendMessage({ type: "BBX_INGEST_JOBS", jobs: batch });
          if (!response?.ok) throw new Error(response?.error || "no response");
          results.push(...response.results);
        } catch (error) {
          // One broken batch must not abort the sync or vanish silently.
          for (const job of batch) {
            results.push({ itemId: job.itemId, courseId: job.courseId, title: job.title, courseName: job.courseName, ok: false, stage: "parse", reason: `ingest-message-failed: ${error?.message || error}` });
          }
        }
      }

      // Check the database itself, not the per-job "ok" flags.
      const verifiedMissing = [];
      for (const courseId of [...new Set(jobs.map((j) => j.courseId))]) {
        let status;
        try { status = await libraryStatusByCourse(courseId); } catch (_) { continue; }
        for (const r of results.filter((x) => x.ok && x.courseId === courseId)) {
          if (!status.get(r.itemId)?.contentful) {
            verifiedMissing.push({ ...r, ok: false, stage: "store", reason: "reported-ok-but-not-in-library" });
          }
        }
      }
      const missingIds = new Set(verifiedMissing.map((m) => m.itemId));
      const succeeded = results.filter((r) => r.ok && !missingIds.has(r.itemId));
      const fetchFailed = results.filter((r) => !r.ok && r.stage === "fetch");
      const failed = [...results.filter((r) => !r.ok && r.stage !== "fetch"), ...verifiedMissing];

      for (const row of [...fetchFailed, ...failed, ...unresolved]) {
        if (row.itemId) lastSyncOutcome.set(row.itemId, { reason: row.reason, stage: row.stage || "skipped" });
      }
      for (const row of succeeded) lastSyncOutcome.delete(row.itemId);

      lastIngestSummary = { succeeded, fetchFailed, failed, unresolved, courseFailures, dismissed: false };
      renderIngestBanner();

      // Full detail in the console too, so a run can be diagnosed from one paste.
      const problemRows = [...courseFailures, ...fetchFailed, ...failed, ...unresolved];
      console.groupCollapsed(`[B+ ingest] ${succeeded.length} indexed, ${fetchFailed.length} download failures, ${failed.length} read failures, ${unresolved.length} skipped`);
      console.table(problemRows.map((r) => ({ course: r.courseName, title: r.title, stage: r.stage || "skipped", reason: r.reason })));
      console.groupEnd();

      if (button) {
        button.textContent = `Synced ${succeeded.length}`;
        setTimeout(() => { button.textContent = originalText; button.disabled = false; }, 2200);
      }
    } catch (error) {
      console.error("[B+ ingest]", error);
      if (button) {
        button.textContent = "Sync failed";
        button.disabled = false;
        setTimeout(() => { button.textContent = originalText; }, 2500);
      }
    }
  }

  function studentCourseRecords() {
    const records = exactCourseRecordsFromNetwork();
    syncSelectedTermToExactCourses(records);
    return state.selectedTerm
      ? records.filter((record) => record.termName === state.selectedTerm)
      : records;
  }

  function makeStudentCourseButton(record) {
    const key = exactCourseKey(record);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "bbx-student-course";

    const name = document.createElement("span");
    name.className = "bbx-student-course-title";
    name.textContent = record.displayName;

    const statusLine = document.createElement("span");
    statusLine.className = "bbx-student-course-status";
    statusLine.dataset.bbxPrepCourse = key;
    const status = activePreparationStatus.get(key);
    statusLine.textContent = prepLabel(status);
    statusLine.dataset.tone = prepPhaseTone(status);

    button.append(name, statusLine);
    button.addEventListener("click", () => {
      state.studentSelectedCourse = key;
      state.selectedProbeCourse = key;
      save();
      render();
      // Selecting a class reprioritizes it in the background sweep and forces a
      // fresh probe so its materials are current.
      schedulePrepareAllCourses("select-course");
      probeActiveCourse(record, true).catch((error) => {
        diagEvent("active-course-probe-failed", { course: record.displayName, error: String(error?.message || error) });
      });
    });
    return button;
  }

  function probeActiveCourse(record, force = false) {
    const key = exactCourseKey(record);
    if (state.courseProbeStatus.get(key) === "loading") return Promise.resolve(null);
    if (force) {
      state.courseProbeResults.delete(key);
      state.courseOutlineCache.delete(key);
      if (!activePreparation.has(key)) {
        activePreparedDescriptors.delete(key);
        activePreparationSignatures.delete(key);
        activePreparationReadyIds.delete(key);
        activePreparationFailedIds.delete(key);
        setActivePreparationStatus(record, { phase: "discovering", total: 0, ready: 0, failed: 0 });
      }
    }
    return probeCourseData(record, force, {
      fast: true,
      onDiscovery(partial) {
        state.courseProbeResults.set(key, partial);
        state.courseProbeStatus.set(key, "loading");
        cacheProbeOutline(key, partial);
        render();
      }
    });
  }

  function courseCodeFor(record) {
    const name = cleanText(record?.displayName);
    const fromTitle = name.match(/^([A-Z]{2,8}\s*[- ]?\s*\d{2,5}[A-Z]?(?:[- ]\d{1,3})?)/i)?.[1];
    return cleanText(firstText(fromTitle, record?.courseCode, name)).slice(0, 80);
  }

  async function extensionRequest(message) {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw new Error(response?.error || "AI Lookup Chat could not complete that request.");
    return response;
  }

  async function ensureCourseMapping(record) {
    const key = exactCourseKey(record);
    if (courseMappingCache.has(key)) return courseMappingCache.get(key);
    if (courseMappingTasks.has(key)) return courseMappingTasks.get(key);
    const task = (async () => {
      const blackboardCourseId = firstText(record.id);
      if (!blackboardCourseId) throw new Error("Blackboard did not provide a stable class ID.");
      let apiState = await extensionRequest({ type: "BBX_CP_STATE" });
      let mapping = (apiState.mappings || []).find((item) => item.blackboard_course_id === blackboardCourseId);
      if (!mapping) {
        const candidate = BBCourseWork.matchCourse({
          ...record,
          courseCode: courseCodeFor(record)
        }, apiState.courses || []);
        if (candidate) {
          await extensionRequest({
            type: "BBX_CP_SAVE_MAPPING", blackboardCourseId,
            courseId: candidate.course_id, courseName: record.displayName
          });
        } else {
          await extensionRequest({
            type: "BBX_CP_CREATE_AND_MAP", blackboardCourseId,
            code: courseCodeFor(record), title: cleanText(record.displayName),
            term: cleanText(record.termName)
          });
        }
        apiState = await extensionRequest({ type: "BBX_CP_STATE" });
        mapping = (apiState.mappings || []).find((item) => item.blackboard_course_id === blackboardCourseId);
      }
      if (!mapping) throw new Error("AI Lookup Chat could not save the Blackboard class link.");
      const linked = { apiState, mapping };
      courseMappingCache.set(key, linked);
      return linked;
    })();
    courseMappingTasks.set(key, task);
    try { return await task; }
    finally { courseMappingTasks.delete(key); }
  }

  function setActivePreparationStatus(record, next) {
    const key = exactCourseKey(record);
    const status = { ...(activePreparationStatus.get(key) || {}), ...next };
    activePreparationStatus.set(key, status);
    const label = prepLabel(status);
    for (const node of document.querySelectorAll("[data-bbx-material-course]")) {
      if (node.dataset.bbxMaterialCourse === key) {
        node.textContent = label;
        node.dataset.tone = prepPhaseTone(status);
      }
    }
    for (const node of document.querySelectorAll("[data-bbx-retry-course]")) {
      if (node.dataset.bbxRetryCourse === key) node.hidden = !["failed", "partial"].includes(status.phase);
    }
    refreshPrepIndicators();
  }

  async function waitForCourseCopilotJob(jobId, record, readyIds, failedIds) {
    if (!jobId) throw new Error("AI Lookup Chat did not return an indexing job ID.");
    for (let attempt = 0; attempt < 900; attempt++) {
      const job = await extensionRequest({ type: "BBX_CP_JOB", jobId });
      for (const item of job.files || []) {
        const id = item.item_id || item.itemId;
        if (!id) continue;
        if (item.status === "ok" || item.status === "reused") {
          readyIds.add(id);
          failedIds.delete(id);
        } else if (item.status === "failed" || item.status === "unsupported") {
          failedIds.add(id);
        }
      }
      setActivePreparationStatus(record, {
        phase: "running", ready: readyIds.size,
        failed: failedIds.size
      });
        if (job.status === "done" || job.status === "failed") {
        for (const item of job.files || []) {
          const id = item.item_id || item.itemId;
          if (id && !["ok", "reused"].includes(item.status)) failedIds.add(id);
        }
        if (job.status === "failed") throw new Error("AI Lookup Chat could not finish one material batch.");
        return;
      }
      // Poll fast at first (most batches finish in well under a second once the
      // embedding model is warm), then back off so a long job stays cheap.
      await new Promise((resolve) => setTimeout(resolve, Math.min(300 + attempt * 150, 1500)));
    }
    throw new Error("AI Lookup Chat is still preparing this class. Reopen the class to check progress.");
  }

  function activeOutlineSignature(record, outline) {
    return buildCourseIngestJobs(record, outline || [])
      .filter((item) => item.kind === "fetch" || item.kind === "markup" ||
        (item.kind === "unresolved" && item.reason === "no-download-url" && item.parentId))
      .map((item) => `${item.itemId}:${item.kind}:${item.url || ""}`)
      .sort().join("|");
  }

  async function startActiveCoursePreparation(record, outline, { retry = false, partial = false } = {}) {
    const key = exactCourseKey(record);
    const signature = activeOutlineSignature(record, outline);
    const current = retry ? null : activePreparationStatus.get(key);
    if (activePreparation.has(key)) {
      if (signature && (signature !== activePreparationSignatures.get(key) ||
          (!partial && activePreparationIsPartial.get(key)))) {
        queuedActivePreparations.set(key, { outline, partial, signature });
      }
      return activePreparation.get(key);
    }
    if (!retry && ["complete", "partial", "failed"].includes(current?.phase) &&
        signature === activePreparationSignatures.get(key)) return null;

    let processed = activePreparedDescriptors.get(key);
    if (!processed || retry) {
      processed = new Set();
      activePreparedDescriptors.set(key, processed);
    }
    let readyIds = activePreparationReadyIds.get(key);
    let failedIds = activePreparationFailedIds.get(key);
    if (!readyIds || retry) {
      readyIds = new Set();
      activePreparationReadyIds.set(key, readyIds);
    }
    if (!failedIds || retry) {
      failedIds = new Set();
      activePreparationFailedIds.set(key, failedIds);
    }
    activePreparationSignatures.set(key, signature);
    activePreparationIsPartial.set(key, partial);

    const task = (async () => {
      const courseId = firstText(record.id);
      setActivePreparationStatus(record, {
        phase: "discovering", total: current?.total || 0,
        ready: readyIds.size, failed: failedIds.size, message: ""
      });
      try {
        const descriptors = buildCourseIngestJobs(record, outline || []);
        const descriptorKey = (item) => `${item.itemId}::${item.url || item.kind}`;
        const queryPriority = (item) => BBCourseWork.queryRelevance(
          activePreparationQueries.get(key) || "",
          [item.title, item.name, item.filename, item.courseName].filter(Boolean).join(" ")
        );
        const direct = descriptors.filter((item) =>
          (item.kind === "fetch" || item.kind === "markup") && !processed.has(descriptorKey(item))
        );
        const unresolvedFiles = descriptors.filter((item) =>
          item.kind === "unresolved" && item.reason === "no-download-url" && item.parentId &&
          !processed.has(descriptorKey(item))
        );
        const total = Math.max(current?.total || 0, readyIds.size + failedIds.size) + direct.length + unresolvedFiles.length;
        setActivePreparationStatus(record, {
          phase: "running", total, ready: readyIds.size, failed: failedIds.size
        });

        async function processBatch(batch) {
          if (!batch.length) return;
          for (const item of batch) processed.add(descriptorKey(item));

          // PDF materials go to the backend's PyMuPDF+OCR extractor (far
          // stronger than the in-browser parser, and it OCRs scans/handwriting).
          // Downloaded in parallel, then uploaded in size-bounded batches so the
          // backend embeds a whole batch in one pass. Any file that fails this
          // path falls through to the parser below — this only adds coverage.
          const serverHandled = new Set();
          const pdfCandidates = batch.filter((item) => item.kind === "fetch" && _looksPdf(item));
          if (pdfCandidates.length) {
            const uploads = [];
            await BBCourseWork.mapLimit(pdfCandidates, 3, async (item) => {
              try {
                const u8 = (await BBStage.fetchRaw(item)).u8;
                const isPdf = u8.length > 4 && u8[0] === 0x25 && u8[1] === 0x50 && u8[2] === 0x44 && u8[3] === 0x46;
                if (!isPdf || u8.length > 40 * 1024 * 1024) return; // leave for the parser path
                uploads.push({ item, base64: BBStage.toBase64(u8) });
              } catch (error) {
                diagEvent("server-extract-fetch-failed", {
                  course: record.displayName, item: item.title, error: String(error?.message || error),
                });
              }
            });

            // Group under a ~24 MB base64 budget per request (chrome messaging).
            const groups = [];
            let group = [], size = 0;
            for (const up of uploads) {
              if (group.length && size + up.base64.length > 24 * 1024 * 1024) {
                groups.push(group); group = []; size = 0;
              }
              group.push(up); size += up.base64.length;
            }
            if (group.length) groups.push(group);

            for (const g of groups) {
              try {
                const reply = await extensionRequest({
                  type: "BBX_CP_INGEST_FILES", blackboardCourseId: courseId,
                  files: g.map(({ item, base64 }) => ({
                    item_id: item.itemId, title: item.title || item.itemId,
                    filename: `${item.title || item.itemId}.pdf`, content_base64: base64,
                  })),
                });
                const skippedIds = new Set((reply.skipped || []).map((s) => s.item_id));
                for (const id of skippedIds) failedIds.add(id);
                if (reply.job_id) await waitForCourseCopilotJob(reply.job_id, record, readyIds, failedIds);
                for (const { item } of g) {
                  if (!skippedIds.has(item.itemId)) serverHandled.add(descriptorKey(item));
                }
              } catch (error) {
                diagEvent("server-extract-fallback", {
                  course: record.displayName, error: String(error?.message || error),
                });
                // Whole group failed -> those items fall through to the parser.
              }
            }
          }

          const fetchJobs = batch.filter((item) => item.kind === "fetch" && !serverHandled.has(descriptorKey(item)));
          const stagedResults = await BBCourseWork.mapLimit(fetchJobs, 3, async (item) => {
            try {
              const staged = await BBStage.fetchAndStage(item);
              return {
                ok: true,
                job: {
                  kind: "staged", itemId: item.itemId, courseId: item.courseId,
                  courseName: item.courseName, title: item.title,
                  sourceType: item.sourceType, pageUrl: item.pageUrl || "",
                  mimeType: item.mimeType || staged.mimeType || "",
                  stageKey: staged.stageKey, chunks: staged.chunks,
                  byteLength: staged.byteLength
                }
              };
            } catch (error) {
              return { ok: false, item, reason: error?.message || String(error) };
            }
          });

          const ingestJobs = batch.filter((item) => item.kind === "markup").map((item) => ({
            kind: "markup", itemId: item.itemId, courseId: item.courseId,
            courseName: item.courseName, title: item.title,
            sourceType: "html", markup: item.markup, pageUrl: ""
          }));
          for (const result of stagedResults) {
            if (result.ok) ingestJobs.push(result.job);
            else failedIds.add(result.item.itemId);
          }

          let parsed = [];
          if (ingestJobs.length) {
            try {
              const response = await extensionRequest({ type: "BBX_INGEST_JOBS", jobs: ingestJobs });
              parsed = response.results || [];
              for (const item of response.skipped || []) failedIds.add(item.item_id);
            } catch (error) {
              for (const item of ingestJobs) failedIds.add(item.itemId);
              diagEvent("active-course-parse-batch-failed", { course: record.displayName, error: String(error?.message || error) });
            }
          }
          for (const item of parsed) if (!item.ok) failedIds.add(item.itemId);
          setActivePreparationStatus(record, { phase: "running", ready: readyIds.size, failed: failedIds.size });

          const ids = [...new Set(parsed.filter((item) => item.ok).map((item) => item.itemId))];
          if (!ids.length) return;
          try {
            const sync = await extensionRequest({
              type: "BBX_CP_SYNC_COURSE", blackboardCourseId: courseId, itemIds: ids
            });
            for (const item of sync.skipped || []) failedIds.add(item.item_id);
            if (sync.job_id) await waitForCourseCopilotJob(sync.job_id, record, readyIds, failedIds);
          } catch (error) {
            for (const id of ids) {
              readyIds.delete(id);
              failedIds.add(id);
            }
            diagEvent("active-course-sync-batch-failed", { course: record.displayName, error: String(error?.message || error) });
          }
        }

        // Prefer Blackboard pages and smaller, already-resolved materials so
        // the first answer can be grounded while file downloads continue.
        const BATCH_SIZE = 4;
        const directQueue = direct.slice();
        while (directQueue.length) {
          directQueue.sort((a, b) => queryPriority(b) - queryPriority(a) ||
            Number(a.kind !== "markup") - Number(b.kind !== "markup"));
          await processBatch(directQueue.splice(0, BATCH_SIZE));
        }

        if (unresolvedFiles.length) {
          const folders = new Map();
          for (const item of unresolvedFiles) {
            const folderKey = `${item.courseId}::${item.parentId}`;
            if (!folders.has(folderKey)) folders.set(folderKey, []);
            folders.get(folderKey).push(item);
          }
          const folderResults = await BBCourseWork.mapLimit([...folders.entries()], 3, async ([folderKey, items]) => {
            const [folderCourseId, parentId] = folderKey.split("::");
            try {
              const found = await BBStage.resolvePermanentUrls(location.origin, folderCourseId, parentId);
              for (const item of items) {
                const hit = found.get(item.itemId);
                if (hit) {
                  item.kind = "fetch";
                  item.url = hit.url;
                  item.mimeType = item.mimeType || hit.mimeType;
                }
              }
              return items.filter((item) => item.kind === "fetch");
            } catch (_) { return []; }
          });
          const resolved = folderResults.flat();
          const resolvedIds = new Set(resolved.map((item) => item.itemId));
          for (const item of unresolvedFiles) if (!resolvedIds.has(item.itemId)) failedIds.add(item.itemId);
          const resolvedQueue = resolved.slice();
          while (resolvedQueue.length) {
            resolvedQueue.sort((a, b) => queryPriority(b) - queryPriority(a));
            await processBatch(resolvedQueue.splice(0, BATCH_SIZE));
          }
        }

        // A later completeness pass finds page bodies that the public course
        // tree did not expose. It runs after the first materials are searchable.
        if (!partial) {
          try {
            const roots = BBAudit.censusRoots(flattenCourseOutline(outline || []));
            if (roots.length) {
              const census = await BBStage.censusCourse(location.origin, courseId, roots);
              const extras = BBAudit.censusTextJobs(census.items, descriptors, visibleTextOf);
              const unseen = extras.filter((item) => !readyIds.has(item.itemId) && !failedIds.has(item.itemId));
              if (unseen.length) {
                setActivePreparationStatus(record, { phase: "running", total: total + unseen.length, ready: readyIds.size, failed: failedIds.size });
                const extraJobs = unseen.map((item) => ({
                  itemId: item.itemId, courseId, courseName: cleanText(record.displayName) || courseId,
                  title: item.title, kind: "markup", sourceType: "html", markup: item.markup
                }));
                for (let index = 0; index < extraJobs.length; index += BATCH_SIZE) {
                  await processBatch(extraJobs.slice(index, index + BATCH_SIZE));
                }
              }
            }
          } catch (error) {
            diagEvent("active-course-completeness-pass-failed", { course: record.displayName, error: String(error?.message || error) });
          }
        }

        setActivePreparationStatus(record, {
          phase: partial ? "discovering" : failedIds.size ? "partial" : "complete",
          total: Math.max(total, readyIds.size + failedIds.size),
          ready: readyIds.size, failed: failedIds.size
        });
      } catch (error) {
        setActivePreparationStatus(record, {
          phase: "failed", message: error?.message || String(error),
          total: 0, ready: readyIds.size, failed: failedIds.size
        });
      }
    })();
    activePreparation.set(key, task);
    try { await task; }
    finally {
      activePreparation.delete(key);
      activePreparationIsPartial.delete(key);
      const queued = queuedActivePreparations.get(key);
      queuedActivePreparations.delete(key);
      if (queued && (queued.signature !== signature || queued.partial !== partial)) {
        startActiveCoursePreparation(record, queued.outline, { partial: queued.partial });
      }
    }
    return null;
  }

  // --- Authoritative auto-preparation lifecycle -------------------------
  //
  // One owner for: discover every current course → map it to a AI Lookup Chat
  // course (creating one if needed) → discover its materials → sync them →
  // compile the study library. Runs automatically once Blackboard has exposed
  // the course list, for ALL current courses, with the active/visible course
  // prioritized so it is usable first while the rest compile in the background.
  //
  // Idempotent by construction: ensureCourseMapping caches + de-dupes per
  // course, probeCourseData guards on a per-course "loading" flag and reuses
  // cached outlines, and startActiveCoursePreparation skips a course whose
  // material signature is unchanged. The sweep can therefore be triggered
  // liberally (page load, SPA navigation, new courses appearing) without
  // duplicating work.

  function activeCourseKey() {
    return state.studentSelectedCourse || state.selectedProbeCourse || "";
  }

  function schedulePrepareAllCourses(reason = "") {
    if (prepareSweepScheduled) return;
    prepareSweepScheduled = true;
    // Coalesce bursts of discovery events into a single sweep.
    setTimeout(() => {
      prepareSweepScheduled = false;
      prepareAllCourses(reason).catch((error) =>
        diagEvent("prepare-sweep-failed", { reason, error: String(error?.message || error) }));
    }, 0);
  }

  async function prepareAllCourses(reason = "") {
    if (prepareSweepRunning) { prepareSweepRerun = true; return; }
    prepareSweepRunning = true;
    try {
      do {
        prepareSweepRerun = false;
        const records = studentCourseRecords();
        if (!records.length) return;
        const activeKey = activeCourseKey();
        const active = records.find((record) => exactCourseKey(record) === activeKey);
        const rest = records.filter((record) => record !== active);
        refreshPrepIndicators();
        // The active course gets its own lane for a head start; the remaining
        // courses compile two at a time so the sidebar stays responsive and we
        // never burst too many requests at Blackboard or the local backend.
        const lanes = [];
        if (active) lanes.push(prepareOneCourse(active, reason));
        lanes.push(BBCourseWork.mapLimit(rest, 2, (record) => prepareOneCourse(record, reason)));
        await Promise.all(lanes);
      } while (prepareSweepRerun);
    } finally {
      prepareSweepRunning = false;
      refreshPrepIndicators();
    }
  }

  async function prepareOneCourse(record, reason = "") {
    const key = exactCourseKey(record);
    if (!key) return;
    try {
      // Auto create/link/persist the Blackboard→AI Lookup Chat mapping.
      await ensureCourseMapping(record);
    } catch (error) {
      diagEvent("prepare-course-mapping-failed", { course: record.displayName, reason, error: String(error?.message || error) });
      return;
    }
    let probe = state.courseProbeResults.get(key);
    if (!probe || !Array.isArray(probe.outline)) {
      try { probe = await probeCourseData(record, false, { silent: true, fast: true }); }
      catch (error) { diagEvent("prepare-course-probe-failed", { course: record.displayName, reason, error: String(error?.message || error) }); }
    }
    const outline = probe?.outline || state.courseProbeResults.get(key)?.outline || [];
    refreshPrepIndicators();
    if (outline.length) {
      await startActiveCoursePreparation(record, outline, { partial: probe?.partial === true });
    } else {
      // A course with no discoverable materials is "ready" (nothing to compile)
      // rather than stuck compiling forever.
      const status = activePreparationStatus.get(key);
      if (!status || status.phase === "discovering") {
        setActivePreparationStatus(record, { phase: "complete", total: 0, ready: 0, failed: 0 });
      }
    }
    refreshPrepIndicators();
  }

  // Consumer-facing preparation copy. Deliberately hides every technical
  // detail (chunks, embeddings, OCR, parsers, course/db ids, model names):
  // the student only needs to know whether a class is ready.
  function prepLabel(status) {
    const phase = status?.phase;
    if (!phase || phase === "discovering") return "Compiling files…";
    if (phase === "running") {
      const total = status.total || 0;
      const done = Math.min(status.ready || 0, total || (status.ready || 0));
      return total ? `Compiling files… ${done} / ${total}` : "Compiling files…";
    }
    if (phase === "complete") return "Ready to go";
    if (phase === "partial") {
      return status.failed ? `Ready — ${status.failed} file${status.failed === 1 ? "" : "s"} couldn’t be prepared` : "Ready to go";
    }
    if (phase === "failed") return "Couldn’t finish preparing your course";
    return "Compiling files…";
  }

  function prepPhaseTone(status) {
    const phase = status?.phase;
    if (phase === "complete") return "ready";
    if (phase === "partial") return status.failed ? "attention" : "ready";
    if (phase === "failed") return "failed";
    return "compiling";
  }

  // Push the current preparation state into the live UI without a full
  // re-render: the per-class status lines, the class-list rows, and the global
  // "Preparing your courses…" banner.
  function refreshPrepIndicators() {
    for (const node of document.querySelectorAll("[data-bbx-prep-course]")) {
      const key = node.dataset.bbxPrepCourse;
      const status = activePreparationStatus.get(key);
      node.textContent = prepLabel(status);
      node.dataset.tone = prepPhaseTone(status);
    }
    const banner = document.getElementById("bbx-prep-banner");
    if (banner) {
      const records = studentCourseRecords();
      // Statuses live in memory, so every fresh load legitimately begins with
      // "Preparing your courses…" and clears once every class is ready.
      const anyBusy = records.length > 0 && records.some((record) => {
        const phase = activePreparationStatus.get(exactCourseKey(record))?.phase;
        return !phase || phase === "discovering" || phase === "running";
      });
      banner.textContent = anyBusy ? "Preparing your courses…" : "";
      banner.hidden = !anyBusy;
    }
  }

  // Minimal, safe Markdown → DOM. Every piece of model text is written with
  // textContent and only http(s) links become anchors, so nothing the model
  // returns is ever interpreted as HTML. Covers what AI Lookup Chat answers
  // actually use: headings, bold/italic, inline code, fenced code, ordered and
  // unordered lists, blockquotes, links and paragraphs.
  // Light-weight math prettifier: no full TeX engine (the sidebar can't ship
  // one), but it strips \( \) / \[ \] delimiters and turns the common textbook
  // forms — subscripts, superscripts, and a handful of operators/Greek — into
  // real Unicode, so "\( p_{1}x_{1} \le m \)" reads as "p₁x₁ ≤ m" instead of raw
  // TeX. Anything it can't map is shown plainly rather than as backslash noise.
  const _SUB = { "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    a: "ₐ", e: "ₑ", i: "ᵢ", j: "ⱼ", o: "ₒ", x: "ₓ", n: "ₙ", t: "ₜ" };
  const _SUP = { "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾", n: "ⁿ", i: "ⁱ" };
  const _MATH_SYM = { "\\le": "≤", "\\leq": "≤", "\\ge": "≥", "\\geq": "≥", "\\ne": "≠",
    "\\neq": "≠", "\\times": "×", "\\cdot": "·", "\\div": "÷", "\\pm": "±", "\\to": "→",
    "\\Rightarrow": "⇒", "\\rightarrow": "→", "\\leftarrow": "←", "\\approx": "≈",
    "\\sum": "∑", "\\int": "∫", "\\infty": "∞", "\\partial": "∂", "\\nabla": "∇",
    "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ", "\\epsilon": "ε",
    "\\theta": "θ", "\\lambda": "λ", "\\mu": "μ", "\\pi": "π", "\\rho": "ρ",
    "\\sigma": "σ", "\\tau": "τ", "\\phi": "φ", "\\omega": "ω", "\\Delta": "Δ",
    "\\Sigma": "Σ", "\\Omega": "Ω", "\\geqslant": "≥", "\\leqslant": "≤" };

  function _mapScript(body, table) {
    let out = "";
    for (const ch of body) {
      if (!(ch in table)) return null;
      out += table[ch];
    }
    return out;
  }

  function formatMath(raw) {
    let s = String(raw || "");
    s = s.replace(/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "($1)/($2)");
    // Subscripts/superscripts, braced or single-char.
    s = s.replace(/([_^])\{([^{}]*)\}|([_^])([A-Za-z0-9])/g, (m, b1, body, b2, ch) => {
      const kind = b1 || b2;
      const text = body != null ? body : ch;
      const mapped = _mapScript(text, kind === "_" ? _SUB : _SUP);
      return mapped != null ? mapped : (kind === "_" ? `_${text}` : `^${text}`);
    });
    for (const [cmd, sym] of Object.entries(_MATH_SYM)) s = s.split(cmd).join(sym);
    s = s.replace(/\\left|\\right|\\,|\\;|\\!|\\quad|\\qquad/g, " ");
    s = s.replace(/\\[A-Za-z]+/g, (c) => c.slice(1)); // drop unknown commands, keep the word
    s = s.replace(/[{}]/g, "");
    return s.replace(/\s{2,}/g, " ").trim();
  }

  // Real math typesetting via KaTeX → MathML, which Chrome renders natively
  // (no CSS/fonts needed). Falls back to the Unicode approximation if KaTeX
  // isn't loaded or the expression won't parse.
  function renderTex(tex, display) {
    const span = document.createElement("span");
    span.className = "bbx-math";
    const katex = self.katex;
    if (katex) {
      try {
        span.innerHTML = katex.renderToString(tex, {
          output: "mathml", throwOnError: false, displayMode: !!display,
        });
        return span;
      } catch (_) { /* fall back below */ }
    }
    span.textContent = formatMath(tex);
    return span;
  }

  function renderInlineMarkdown(parent, text, ctx) {
    // <n> is a citation sentinel injected by the citation context
    // (see makeCitationContext); it renders as a numbered, clickable reference.
    const re = /(\d+)|(\\\([^\n]*?\\\)|\\\[[^\n]*?\\\])|(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*|_[^_\n]+_)|(\[[^\]]+\]\([^)\s]+\))/g;
    let last = 0;
    let match;
    while ((match = re.exec(text))) {
      if (match.index > last) parent.append(text.slice(last, match.index));
      const token = match[0];
      if (token.charCodeAt(0) === 0xe000) {
        const n = Number(token.slice(1, -1));
        parent.append(ctx ? ctx.renderMarker(n) : document.createTextNode(`[${n}]`));
      } else if (token.startsWith("\\(") || token.startsWith("\\[")) {
        parent.append(renderTex(token.slice(2, -2), token.startsWith("\\[")));
      } else if (token.startsWith("`")) {
        const code = document.createElement("code");
        code.textContent = token.slice(1, -1);
        parent.append(code);
      } else if (token.startsWith("**")) {
        const strong = document.createElement("strong");
        strong.textContent = token.slice(2, -2);
        parent.append(strong);
      } else if (token.startsWith("[")) {
        const parts = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
        if (parts && /^https?:\/\//i.test(parts[2])) {
          const link = document.createElement("a");
          link.href = parts[2];
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = parts[1];
          parent.append(link);
        } else {
          parent.append(parts ? parts[1] : token);
        }
      } else {
        const em = document.createElement("em");
        em.textContent = token.replace(/^[*_]|[*_]$/g, "");
        parent.append(em);
      }
      last = match.index + token.length;
    }
    if (last < text.length) parent.append(text.slice(last));
  }

  function renderMarkdownInto(container, markdown, ctx) {
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    const isBreak = (line) => !line.trim() || /^```/.test(line) || /^#{1,6}\s/.test(line) ||
      /^>\s?/.test(line) || /^\s*[-*+]\s+/.test(line) || /^\s*\d+\.\s+/.test(line);

    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }

      const fence = line.match(/^```/);
      if (fence) {
        const buffer = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { buffer.push(lines[i]); i++; }
        i++; // closing fence
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = buffer.join("\n");
        pre.append(code);
        container.append(pre);
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        // Keep headings within the small panel's type scale.
        const level = Math.min(6, heading[1].length + 2);
        const node = document.createElement("h" + level);
        renderInlineMarkdown(node, heading[2].trim(), ctx);
        container.append(node);
        i++;
        continue;
      }

      if (/^>\s?/.test(line)) {
        const buffer = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { buffer.push(lines[i].replace(/^>\s?/, "")); i++; }
        const quote = document.createElement("blockquote");
        renderInlineMarkdown(quote, buffer.join(" "), ctx);
        container.append(quote);
        continue;
      }

      const ordered = /^\s*\d+\.\s+/.test(line);
      if (ordered || /^\s*[-*+]\s+/.test(line)) {
        const pattern = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*+]\s+/;
        const list = document.createElement(ordered ? "ol" : "ul");
        while (i < lines.length && pattern.test(lines[i])) {
          const item = document.createElement("li");
          renderInlineMarkdown(item, lines[i].replace(pattern, ""), ctx);
          list.append(item);
          i++;
        }
        container.append(list);
        continue;
      }

      const buffer = [line];
      i++;
      while (i < lines.length && !isBreak(lines[i])) { buffer.push(lines[i]); i++; }
      const paragraph = document.createElement("p");
      renderInlineMarkdown(paragraph, buffer.join(" "), ctx);
      container.append(paragraph);
    }
  }

  // --- Blackboard-linked, numbered citations ----------------------------
  //
  // A grounded answer arrives with in-text labels like "[econ304_bbx_ab…, p. 1]"
  // (the internal AI Lookup Chat course id) and a citations list. Neither is
  // useful to a student. We map each cited source back to the actual Blackboard
  // content page — matching on the item id the material was ingested under, and
  // falling back to its title — then rewrite the labels as compact, clickable,
  // Wikipedia-style numbers ([1], [2], …) that open the file in Blackboard.

  async function _sha256Hex(text) {
    const bytes = new TextEncoder().encode(String(text));
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function _normTitle(value) {
    return cleanText(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  }

  // {byItemHash: Map(hash16 -> {url,title}), byTitle: Map(normTitle -> {url,title})}.
  // hash16 mirrors the backend's source id: sha256(itemId)[:16] (see
  // api/integrations/bbplus.py safe_material_filename + ingest _source_id).
  async function buildBlackboardSourceMap(courseKey) {
    const map = { byItemHash: new Map(), byTitle: new Map() };
    const probe = state.courseProbeResults.get(courseKey) || state.courseOutlineCache.get(courseKey);
    const outline = probe?.outline;
    if (!Array.isArray(outline) || !outline.length) return map;
    const leaves = [];
    walkOutlineLeaves(outline, (item) => leaves.push(item));
    for (const item of leaves) {
      const url = absUrl(firstText(item.url, item.downloadUrl));
      if (!url) continue;
      const title = cleanText(firstText(item.title));
      const entry = { url, title };
      try {
        const hash = await _sha256Hex(stableItemId(item));
        map.byItemHash.set(hash.slice(0, 16), entry);
      } catch (_) {}
      if (title) map.byTitle.set(_normTitle(title), entry);
    }
    return map;
  }

  function blackboardEntryForCitation(citation, sourceTitle, bbMap) {
    const hashMatch = String(citation.source_id || "").match(/bbplus_([0-9a-f]{16})/i);
    if (hashMatch && bbMap.byItemHash.has(hashMatch[1].toLowerCase())) {
      return bbMap.byItemHash.get(hashMatch[1].toLowerCase());
    }
    const title = _normTitle(sourceTitle || citation.chapter_title || "");
    if (title && bbMap.byTitle.has(title)) return bbMap.byTitle.get(title);
    return null;
  }

  // The backend already numbers citations by source: answer.citations[i] is the
  // source cited in text as [i+1]. Resolve each to its Blackboard page and make
  // the in-text [n] markers clickable superscript links.
  function makeCitationContext(citations, target, bbMap, blackboardCourseId) {
    const sourceTitleOf = (c) =>
      target?.sources?.find((s) => s.source_id === c.source_id)?.title || "";

    const urlOf = (c) => {
      const entry = blackboardEntryForCitation(c, sourceTitleOf(c), bbMap);
      if (entry?.url) return { url: entry.url, where: "Blackboard" };
      // Fall back to the locally-served copy if the Blackboard page is unknown.
      if (c.source_id) {
        return {
          url: `${COURSE_COPILOT_ORIGIN}/api/integrations/bbplus/course-mappings/` +
            `${encodeURIComponent(blackboardCourseId)}/sources/${encodeURIComponent(c.source_id)}/file`,
          where: "stored copy",
        };
      }
      return { url: "", where: "" };
    };

    const refs = citations.map((c, i) => {
      const link = urlOf(c);
      return {
        n: i + 1, source_id: c.source_id,
        title: sourceTitleOf(c) || c.chapter_title || c.source_id || "Course material",
        url: link.url, where: link.where,
      };
    });
    const max = refs.length;

    return {
      // Rewrite a text's labels into numbering sentinels. Returns the new text.
      tokenize(text) {
        if (!max || !text) return text || "";
        return text.replace(/\[(\d+)\]/g, (m, digits) => {
          const n = Number(digits);
          if (n < 1 || n > max) return m;
          return `${n}`;
        });
      },
      // A [n] superscript link to the source (opens in Blackboard).
      renderMarker(n) {
        const ref = refs[n - 1];
        const sup = document.createElement("sup");
        sup.className = "bbx-cite";
        if (ref?.url) {
          const a = document.createElement("a");
          a.href = ref.url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.textContent = `[${n}]`;
          if (ref.title) a.title = ref.title;
          sup.append(a);
        } else {
          sup.textContent = `[${n}]`;
        }
        return sup;
      },
      refs,
    };
  }

  function makeCourseCopilotPanel(record) {
    const section = document.createElement("section");
    section.className = "bbx-copilot";
    section.setAttribute("aria-labelledby", "bbx-copilot-title");

    const heading = document.createElement("h3");
    heading.id = "bbx-copilot-title";
    heading.textContent = "AI Lookup Chat";
    const intro = document.createElement("p");
    intro.className = "bbx-copilot-intro";
    intro.textContent = "Ask questions about this class and its Blackboard materials.";
    const status = document.createElement("div");
    status.className = "bbx-copilot-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const actions = document.createElement("div");
    actions.className = "bbx-copilot-actions";
    const askArea = document.createElement("div");
    askArea.className = "bbx-copilot-ask";
    const resultArea = document.createElement("div");
    resultArea.className = "bbx-copilot-result";
    section.append(heading, intro, status, actions, askArea, resultArea);

    const blackboardCourseId = firstText(record.id);
    const courseName = cleanText(record.displayName) || blackboardCourseId;
    const setStatus = (message, kind = "") => {
      status.textContent = message;
      status.dataset.kind = kind;
    };
    const button = (label, className, onClick) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = className;
      el.textContent = label;
      el.addEventListener("click", onClick);
      return el;
    };
    const send = async (type, fields = {}) => {
      const reply = await chrome.runtime.sendMessage({ type, ...fields });
      if (!reply?.ok) throw new Error(reply?.error || "AI Lookup Chat could not complete that request.");
      return reply;
    };

    let apiState = null;
    let mappedCourse = null;
    let refreshing = false;
    const courseKey = exactCourseKey(record);

    const renderMappedTools = () => {
      actions.replaceChildren();
      askArea.replaceChildren();
      if (!mappedCourse) return;

      const target = apiState?.courses?.find((course) => course.course_id === mappedCourse.course_id);

      const form = document.createElement("form");
      form.className = "bbx-copilot-form";
      const questionLabel = document.createElement("label");
      questionLabel.textContent = "Ask about this course";
      const question = document.createElement("textarea");
      question.rows = 3;
      question.maxLength = 12000;
      question.placeholder = "What should I understand about this week’s material?";
      questionLabel.append(question);
      const options = document.createElement("div");
      options.className = "bbx-copilot-options";
      const depthLabel = document.createElement("label");
      depthLabel.textContent = "Answer depth";
      const depth = document.createElement("select");
      depth.add(new Option("Concise", "concise"));
      depth.add(new Option("In depth", "in_depth"));
      depthLabel.append(depth);
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "bbx-copilot-primary";
      submit.textContent = "Ask";
      options.append(depthLabel, submit);
      form.append(questionLabel, options);
      askArea.append(form);

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const prompt = question.value.trim();
        if (!prompt) {
          setStatus("Enter a question first.", "error");
          question.focus();
          return;
        }
        activePreparationQueries.set(courseKey, prompt);
        submit.disabled = true;
        resultArea.replaceChildren();
        setStatus("Searching this course’s materials and preparing an answer…", "loading");
        try {
          const answer = await send("BBX_CP_ASK", {
            blackboardCourseId, question: prompt, depth: depth.value
          });
          await renderAnswer(answer, target);
          setStatus(answer.refused
            ? "The indexed course materials did not contain enough evidence to answer."
            : `Answered from ${answer.backend || "AI Lookup Chat"}.`, answer.refused ? "warning" : "success");
        } catch (error) {
          setStatus(error?.message || String(error), "error");
        } finally {
          submit.disabled = false;
        }
      });
    };

    const renderAnswer = async (answer, target) => {
      resultArea.replaceChildren();

      const citations = Array.isArray(answer.citations) ? answer.citations : [];
      // Resolve each cited source to its Blackboard page, then set up numbered,
      // clickable references shared across the answer and explanation.
      const bbMap = citations.length
        ? await buildBlackboardSourceMap(courseKey).catch(() => ({ byItemHash: new Map(), byTitle: new Map() }))
        : { byItemHash: new Map(), byTitle: new Map() };
      const ctx = citations.length
        ? makeCitationContext(citations, target, bbMap, blackboardCourseId)
        : null;

      const answerCard = document.createElement("article");
      answerCard.className = "bbx-copilot-card";
      const answerHeading = document.createElement("h4");
      answerHeading.textContent = "Answer";
      const answerText = document.createElement("div");
      answerText.className = "bbx-copilot-answer-text bbx-md";
      const answerBody = ctx ? ctx.tokenize(answer.answer || "") : (answer.answer || "No answer was returned.");
      renderMarkdownInto(answerText, answerBody || "No answer was returned.", ctx);
      answerCard.append(answerHeading, answerText);
      if (answer.explanation) {
        const explanationHeading = document.createElement("h4");
        explanationHeading.textContent = "Explanation";
        const explanation = document.createElement("div");
        explanation.className = "bbx-copilot-answer-text bbx-md";
        renderMarkdownInto(explanation, ctx ? ctx.tokenize(answer.explanation) : answer.explanation, ctx);
        answerCard.append(explanationHeading, explanation);
      }
      resultArea.append(answerCard);

      if (!ctx || !ctx.refs.length) return;

      const sourceList = document.createElement("ol");
      sourceList.className = "bbx-copilot-sources";
      const sourceHeading = document.createElement("h4");
      sourceHeading.className = "bbx-copilot-sources-title";
      sourceHeading.textContent = "Sources";
      resultArea.append(sourceHeading, sourceList);
      for (const ref of ctx.refs) {
        const item = document.createElement("li");
        item.className = "bbx-copilot-source";
        item.value = ref.n;
        const name = document.createElement(ref.url ? "a" : "strong");
        name.textContent = ref.title;
        if (ref.url) {
          name.className = "bbx-copilot-source-link";
          name.href = ref.url;
          name.target = "_blank";
          name.rel = "noopener noreferrer";
          if (ref.where && ref.where !== "Blackboard") name.title = `Opens the ${ref.where}`;
        }
        item.append(name);
        sourceList.append(item);
      }
    };

    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      actions.replaceChildren();
      askArea.replaceChildren();
      setStatus("Connecting to AI Lookup Chat…", "loading");
      try {
        const linked = await ensureCourseMapping(record);
        apiState = linked.apiState;
        mappedCourse = linked.mapping;
        renderMappedTools();
        setStatus("This class is connected to AI Lookup Chat.", "success");
        const probe = state.courseProbeResults.get(courseKey);
        if (probe?.outline) {
          startActiveCoursePreparation(record, probe.outline, { partial: probe.partial === true });
          const prep = activePreparationStatus.get(courseKey);
          if (prep?.phase === "failed") setStatus(prep.message || "Materials could not be prepared. Retry when ready.", "error");
        }
      } catch (error) {
        setStatus(`${error?.message || error} Use the B+ extension menu to connect AI Lookup Chat, then retry.`, "error");
        actions.append(button("Retry connection", "bbx-copilot-secondary", () => refresh()));
      } finally {
        refreshing = false;
      }
    };

    refresh();
    return section;
  }

  function renderStudentCourseDetail(container, record) {
    const top = document.createElement("div");
    top.className = "bbx-student-detail-top";

    const back = document.createElement("button");
    back.type = "button";
    back.className = "bbx-student-back";
    back.textContent = "← Classes";
    back.addEventListener("click", () => {
      state.studentSelectedCourse = "";
      save();
      render();
    });

    const name = document.createElement("div");
    name.className = "bbx-student-course-name";
    name.textContent = record.displayName;

    top.append(back, name);
    container.append(top);

    container.append(makeCourseCopilotPanel(record));

    const key = exactCourseKey(record);
    const status = state.courseProbeStatus.get(key) || "";
    const probe = state.courseProbeResults.get(key);

    const materialSummary = document.createElement("section");
    materialSummary.className = "bbx-student-materials";
    const materialHeading = document.createElement("strong");
    materialHeading.textContent = "Course materials";
    const count = document.createElement("span");
    count.className = "bbx-student-material-count";
    const materialStatus = document.createElement("span");
    materialStatus.className = "bbx-student-material-status";
    materialStatus.dataset.bbxMaterialCourse = key;
    if (probe?.outline) {
      const jobs = buildCourseIngestJobs(record, probe.outline);
      const processable = jobs.filter((item) =>
        item.kind === "fetch" || item.kind === "markup" ||
        (item.kind === "unresolved" && item.reason === "no-download-url" && item.parentId)
      );
      count.textContent = `${processable.length} ${processable.length === 1 ? "material" : "materials"}`;
    } else {
      count.textContent = status === "loading" ? "Finding materials…" : "Materials are not loaded yet";
    }
    materialStatus.dataset.tone = prepPhaseTone(activePreparationStatus.get(key));
    materialStatus.textContent = prepLabel(activePreparationStatus.get(key));
    const retryMaterials = document.createElement("button");
    retryMaterials.type = "button";
    retryMaterials.className = "bbx-copilot-secondary bbx-student-material-retry";
    retryMaterials.textContent = "Retry materials";
    retryMaterials.dataset.bbxRetryCourse = key;
    retryMaterials.hidden = !["failed", "partial"].includes(activePreparationStatus.get(key)?.phase);
    retryMaterials.addEventListener("click", () => {
      startActiveCoursePreparation(record, state.courseProbeResults.get(key)?.outline || [], { retry: true });
    });
    materialSummary.append(materialHeading, count, materialStatus, retryMaterials);
    container.append(materialSummary);

    if (!probe) {
      if (status !== "loading") {
        setTimeout(() => probeActiveCourse(record, false), 0);
      }
      return;
    }

    if (!probe.outline?.length) {
      const empty = document.createElement("div");
      empty.className = "bbx-student-empty";
      empty.textContent = "No course content found yet.";
      container.append(empty);
      return;
    }
  }

  function renderStudent() {
    const termBar = document.getElementById("bbx-term-bar");
    const summary = document.getElementById("bbx-summary");
    const body = document.getElementById("bbx-body");
    if (!body || !summary) return;

    termBar?.replaceChildren();
    summary.replaceChildren();

    const records = studentCourseRecords();

    const selected = records.find(
      (record) => exactCourseKey(record) === state.studentSelectedCourse
    );

    const panel = document.createElement("div");
    panel.className = "bbx-student-panel";

    if (selected) {
      renderStudentCourseDetail(panel, selected);
    } else {
      const prepBanner = document.createElement("div");
      prepBanner.id = "bbx-prep-banner";
      prepBanner.className = "bbx-prep-banner";
      prepBanner.setAttribute("role", "status");
      prepBanner.setAttribute("aria-live", "polite");
      prepBanner.hidden = true;
      panel.append(prepBanner);

      const list = document.createElement("div");
      list.className = "bbx-student-course-list";

      if (!records.length) {
        const empty = document.createElement("div");
        empty.className = "bbx-student-empty";
        empty.textContent = "No courses found for this term.";
        list.append(empty);
      } else {
        for (const record of records) {
          list.append(makeStudentCourseButton(record));
        }
      }

      panel.append(list);
    }

    body.replaceChildren(panel);
    refreshPrepIndicators();
  }

  function renderDiagnostic() {
    const termBar = document.getElementById("bbx-term-bar");
    const summary = document.getElementById("bbx-summary");
    const body = document.getElementById("bbx-body");
    if (!summary || !body) return;

    const exactCourses = exactCourseRecordsFromNetwork();

    if (termBar) {
      const banner = document.createElement("div");
      banner.className = "bbx-diag-notice";
      banner.textContent =
        "SCHEMA-DRIVEN BUILD · Courses = body.results[*].course with both displayName and term.name.";
      termBar.replaceChildren(banner);
    }

    summary.replaceChildren(
      makeStat("Raw JSON", state.diagnostics.network.length),
      makeStat("Courses", exactCourses.length),
      makeStat("Terms", availableExactTerms(exactCourses).length)
    );

    const tabs = document.createElement("div");
    tabs.className = "bbx-tabs";

    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "bbx-tab-button bbx-refresh-button";
    refresh.textContent = "Refresh View";
    refresh.title = "Show data captured since this view was last rendered";
    refresh.addEventListener("click", () => render());

    tabs.append(
      makeTabButton("courses", "Courses"),
      makeTabButton("courseData", "Course Data"),
      makeTabButton("raw", "Raw Blackboard JSON"),
      makeTabButton("page", "Current Page"),
      refresh
    );

    const panel = document.createElement("div");
    panel.className = "bbx-tab-panel";

    if (state.diagnosticTab === "courseData") {
      renderCourseDataTab(panel, exactCourses);
    } else if (state.diagnosticTab === "raw") {
      renderRawTab(panel);
    } else if (state.diagnosticTab === "page") {
      renderPageTab(panel);
    } else {
      renderCoursesTab(panel, exactCourses);
    }

    body.replaceChildren(tabs, panel);
  }

  function render() {
    try {
      const tools = document.getElementById("bbx-header-tools");
      if (tools) tools.hidden = state.uiMode !== "debug";
      if (state.uiMode === "debug") renderDiagnostic();
      else renderStudent();
    } catch (error) {
      console.error("[B+ render]", error);
      const body = document.getElementById("bbx-body");
      if (body) {
        const pre = document.createElement("pre");
        pre.className = "bbx-render-crash";
        pre.textContent =
          "B+ render error:\n" +
          String(error?.stack || error?.message || error) +
          "\n\nRaw network data:\n" +
          prettyJson(state.diagnostics?.network || []);
        body.replaceChildren(pre);
      }
    }
  }

  async function start() {
    await restore();

    const onReady = () => {
      ensureUi();
      scanDom();
      setTimeout(() => refreshCourseListFromKnownEndpoints(), 250);
      // Once Blackboard exposes the course list (from its normal course-list
      // traffic), the automatic lifecycle discovers, maps, syncs and compiles
      // every current course — active course first — in the background. The
      // sweep is idempotent, so triggering it here and again on navigation or
      // as new course traffic arrives never duplicates work.
      schedulePrepareAllCourses("startup");

      const observer = new MutationObserver(scheduleScan);
      observer.observe(document.documentElement, { childList: true, subtree: true });
      window.addEventListener("popstate", () => {
        scheduleScan();
        setTimeout(() => refreshCourseListFromKnownEndpoints(), 200);
      });
      window.addEventListener("hashchange", () => {
        scheduleScan();
        setTimeout(() => refreshCourseListFromKnownEndpoints(), 200);
      });
      setInterval(scanDom, 15_000);
    };

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", onReady, { once: true });
    } else {
      onReady();
    }
  }

  start();
})();

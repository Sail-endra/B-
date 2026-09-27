/* Course Copilot — a reading instrument.
 *
 * The interface decision that matters: citations are margin sidenotes, not
 * chips. A superscript marker stays in the line; the note floats into the
 * margin beside the sentence that cites it, carrying course, chapter and page.
 * Below 1024px the margin collapses and the note expands inline — CSS does
 * that, not JavaScript, so it survives reflow and zoom.
 *
 * No spinner: loading is skeleton text set in the real measure and the real
 * type, so the wait looks like the content arriving.
 *
 * API contracts are untouched. This file only changes presentation.
 */

const $ = (id) => document.getElementById(id);

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

// KaTeX only visits text nodes created by the existing safe DOM renderers.
function renderMath(container) {
  if (typeof window.renderMathInElement !== "function") return;
  try {
    window.renderMathInElement(container, {
      delimiters: [
        { left: "\\[", right: "\\]", display: true },
        { left: "$$", right: "$$", display: true },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false, trust: false, strict: "ignore",
    });
  } catch (error) {
    console.warn("Math rendering skipped an invalid expression.", error);
  }
}

/* Course ids are lowercase slugs now ("econ303"), so this is case-insensitive.
   The chapter group is optional: a journal article has pages but no chapter. */
const CITE_RE = /\[([A-Za-z0-9_-]{2,20}),\s*(?:Ch\s*(\d+),\s*)?(pp?\.\s*[\d–-]+)\]/g;

let COURSES = [];
let voicePrefs = { voice_consent: false, voice_only_mode: false, auto_submit_voice: true,
  selected_voice_id: "", speech_speed: 1, voice_api_configured: false };
let pendingVoiceStart = false;
let mediaRecorder = null;
let mediaStream = null;
let mediaChunks = [];
let recordingTimer = null;
let recordingStartedAt = 0;
let activeAudio = null;
let activePlayback = null;
const spokenAudioCache = new Map();
const spokenAudioPending = new Map();

async function json(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 1000)}`);
  return res.json();
}

/* ── views ─────────────────────────────────────────────────────────────── */

document.querySelectorAll(".views button").forEach((tab) => {
  tab.addEventListener("click", () => {
    if (activeAudio) stopActiveAudio("Playback stopped because you changed sections.");
    document.querySelectorAll(".views button").forEach((t) =>
      t.setAttribute("aria-selected", String(t === tab)));
    $("view-ask").hidden = tab.dataset.view !== "ask";
    $("view-dash").hidden = tab.dataset.view !== "dash";
    $("view-practice").hidden = tab.dataset.view !== "practice";
    $("view-grades").hidden = tab.dataset.view !== "grades";
    if (tab.dataset.view === "dash") loadDashboard();
    if (tab.dataset.view === "practice") loadPractice();
    if (tab.dataset.view === "grades") loadGradePredictor();
  });
});

$("sheet-open").addEventListener("click", async () => {
  $("sheet").hidden = false;
  await loadCourses();
  await loadFirstPendingReview();
});
$("sheet-close").addEventListener("click", () => { $("sheet").hidden = true; });
$("course-delete-open").addEventListener("click", openCourseDeleteDialog);
$("course-delete-close").addEventListener("click", () => $("course-delete-dialog").close());
$("course-delete-cancel").addEventListener("click", () => $("course-delete-dialog").close());
$("course-delete-all").addEventListener("change", () => {
  courseDeletionSelected = $("course-delete-all").checked
    ? new Set(courseDeletionPreview.map((course) => course.course_id)) : new Set();
  renderCourseDeleteSelection();
});
$("course-delete-phrase").addEventListener("input", updateCourseDeleteConfirmation);
$("course-delete-confirm").addEventListener("click", submitCourseDeletions);
$("ai-preferences").addEventListener("click", () => { $("consent-banner").hidden = false; });

/* Light and dark are both first-class; a reading tool gets used in both. */
const themeButton = $("theme-toggle");
function applyTheme(mode) {
  document.documentElement.setAttribute("data-theme", mode);
  themeButton.textContent = mode === "dark" ? "Light" : "Dark";
  try { localStorage.setItem("cc-theme", mode); } catch { /* private window */ }
}

/* ── grade predictor: reviewed syllabus rules + deterministic score tools ─ */
let gradeData = null;
let gradeSyllabusData = null;
let gradeWhatIf = {};
let gradeCourseId = "";

$("grades-course").addEventListener("change", loadGradePredictor);
$("grade-add-category").addEventListener("click", () => {
  gradeData.pending.schema.components.push({name: "New category", weight: 0, aggregation: "equal", items: [{name: "Assessment", weight_within_category: null}], drop_lowest: 0});
  renderGradeReview(gradeData.pending);
});
$("grade-confirm").addEventListener("click", confirmGradeRules);
$("grade-edit-structure").addEventListener("click", () => {
  if (!gradeData?.confirmed) return;
  gradeData.pending = {
    schema: JSON.parse(JSON.stringify(gradeData.confirmed)),
    id: gradeData.pending?.id || null, status: "pending",
  };
  renderGradeReview(gradeData.pending);
  $("grade-review").hidden = false; $("grade-main").hidden = true;
  $("grade-review").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("grade-syllabus-upload").addEventListener("click", () => $("grade-syllabus-file").click());
$("grade-syllabus-analyze").addEventListener("click", () => analyzeGradeSyllabus(false));
$("grade-syllabus-reanalyze").addEventListener("click", () => analyzeGradeSyllabus(true));
$("grade-syllabus-retry").addEventListener("click", loadGradePredictor);
$("grade-syllabus-use").addEventListener("click", async () => {
  try {
    await json(`/api/courses/${encodeURIComponent(gradeCourseId)}/syllabus/select`, {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({source_id: $("grade-syllabus-choice").value}),
    });
    await loadGradePredictor();
  } catch (error) { $("grade-syllabus-info").textContent = `Could not select syllabus: ${error.message}`; }
});
$("grade-syllabus-file").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file || !gradeCourseId) return;
  const button = $("grade-syllabus-upload");
  button.disabled = true; button.textContent = "Uploading and analyzing…";
  $("grade-syllabus-info").textContent = "Uploading the syllabus and generating a grading proposal…";
  try {
    const body = new FormData(); body.append("file", file);
    await json(`/api/courses/${encodeURIComponent(gradeCourseId)}/syllabus`, {method:"POST", body});
    await loadGradePredictor();
  } catch (error) {
    $("grade-syllabus-info").textContent = `Upload or analysis failed: ${error.message}. You can retry.`;
  } finally {
    button.disabled = false; button.textContent = "Upload Syllabus"; event.target.value = "";
  }
});

async function analyzeGradeSyllabus(force) {
  const button = force ? $("grade-syllabus-reanalyze") : $("grade-syllabus-analyze");
  if (force && !window.confirm("Re-analyze the complete syllabus? The new grading structure will be a proposal for review; saved grades will remain.")) return;
  button.disabled = true;
  $("grade-syllabus-info").textContent = "Analyzing the complete syllabus for grading rules…";
  try {
    await json(`/api/courses/${encodeURIComponent(gradeCourseId)}/syllabus/analyze`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({source_id: gradeSyllabusData?.selected_source_id || undefined, force}),
    });
    await loadGradePredictor();
  } catch (error) {
    $("grade-syllabus-info").textContent = `Analysis failed: ${error.message}. You can retry.`;
    $("grade-syllabus-analyze").hidden = false;
  } finally { button.disabled = false; }
}

async function loadGradePredictor() {
  const select = $("grades-course");
  const previous = select.value;
  if (gradeCourseId && gradeCourseId !== previous) gradeWhatIf = {};
  select.textContent = "";
  for (const course of COURSES) {
    const option = el("option", null, course.code);
    option.value = course.course_id;
    select.append(option);
  }
  if (!COURSES.length) {
    $("grades-status").textContent = "Create a course to set up grade prediction.";
    $("grade-syllabus-info").textContent = "No course is selected.";
    $("grade-syllabus-upload").disabled = true;
    $("grade-review").hidden = true; $("grade-main").hidden = true;
    return;
  }
  $("grade-syllabus-upload").disabled = false;
  select.value = COURSES.some((course) => course.course_id === previous) ? previous : COURSES[0].course_id;
  gradeCourseId = select.value;
  $("grades-status").textContent = "Loading this course’s syllabus and saved grades…";
  try {
    gradeSyllabusData = await json(`/api/courses/${encodeURIComponent(select.value)}/syllabus`);
    const choice = $("grade-syllabus-choice"); choice.textContent = "";
    for (const item of gradeSyllabusData.candidates || []) {
      const option = el("option", null, item.file_name); option.value = item.source_id; choice.append(option);
    }
    choice.hidden = (gradeSyllabusData.candidates || []).length < 2;
    $("grade-syllabus-use").hidden = !gradeSyllabusData.ambiguous;
    $("grade-syllabus-analyze").hidden = true;
    $("grade-syllabus-reanalyze").hidden = !gradeSyllabusData.selected;
    $("grade-syllabus-retry").hidden = true;
    const selected = gradeSyllabusData.selected;
    if (!gradeSyllabusData.exists) {
      $("grade-syllabus-info").textContent = "No syllabus found for this course. Upload one to generate your grading structure.";
    } else if (gradeSyllabusData.ambiguous) {
      $("grade-syllabus-info").textContent = "More than one syllabus is associated with this course. Select the one to use.";
    } else {
      $("grade-syllabus-info").textContent = `Syllabus detected: ${selected.file_name}`;
      if (selected.status === "not_analyzed") {
        $("grade-syllabus-info").textContent += " · analyzing grading structure…";
        try {
          await json(`/api/courses/${encodeURIComponent(select.value)}/syllabus/analyze`, {
            method:"POST", headers:{"Content-Type":"application/json"},
            body:JSON.stringify({source_id:selected.source_id, force:false}),
          });
          return await loadGradePredictor();
        } catch (error) {
          $("grade-syllabus-info").textContent = `Syllabus detected: ${selected.file_name}. Analysis failed: ${error.message}. Retry when ready.`;
          $("grade-syllabus-analyze").hidden = false;
        }
      }
      if (selected.status === "pending") $("grade-syllabus-info").textContent += " · grading proposal ready for review";
      if (selected.status === "analyzed") $("grade-syllabus-info").textContent += " · grading structure ready";
      if (selected.status === "failed") {
        $("grade-syllabus-info").textContent += " · previous analysis failed; retry below";
        $("grade-syllabus-analyze").hidden = false;
      }
    }
    gradeData = await json(`/api/courses/${encodeURIComponent(select.value)}/grades`);
    $("grade-review").hidden = !gradeData.pending || gradeData.pending.status !== "pending";
    $("grade-main").hidden = !gradeData.confirmed;
    $("grades-status").textContent = gradeData.confirmed
      ? "Confirmed grade structure and saved scores. Re-analysis creates a proposal for review; saved grades stay separate."
      : (gradeData.pending ? "Review the extracted grading structure, then confirm to create grade fields." : "Grade fields appear after a grading structure is confirmed.");
    if (gradeData.pending) renderGradeReview(gradeData.pending);
    if (gradeData.confirmed) renderGradeFields();
  } catch (error) {
    $("grades-status").textContent = `Could not load grade predictor: ${error.message}`;
    $("grade-syllabus-info").textContent = "Could not load syllabus information. Retry by changing courses or reopening Grade Predictor.";
    $("grade-syllabus-retry").hidden = false;
    $("grade-review").hidden = true; $("grade-main").hidden = true;
  }
}

function renderGradeReview(pending) {
  const diff = $("grade-diff"); diff.textContent = "";
  const oldComponents = gradeData.confirmed?.components || [];
  if (oldComponents.length) {
    const oldById = new Map(oldComponents.map((component) => [component.id, component]));
    const messages = [];
    for (const next of pending.schema.components) {
      const old = oldById.get(next.id);
      if (!old) messages.push(`Added category: ${next.name}`);
      else {
        oldById.delete(next.id);
        if (Number(old.weight) !== Number(next.weight)) messages.push(`${next.name}: weight ${(old.weight * 100).toFixed(1)}% → ${(next.weight * 100).toFixed(1)}%`);
        if (old.items.map((item) => item.name).join(", ") !== next.items.map((item) => item.name).join(", ")) messages.push(`${next.name}: assessment list changed`);
        if (old.drop_lowest !== next.drop_lowest) messages.push(`${next.name}: dropped-score rule changed`);
      }
    }
    for (const old of oldById.values()) messages.push(`Removed category: ${old.name}`);
    diff.append(el("p", "note", messages.length ? `Compared with confirmed rules: ${messages.join(" · ")}. Existing actual scores are retained; only matching assessment IDs continue to contribute.` : "No category, weight, or assessment name changes detected. Existing actual scores remain saved."));
  }
  const box = $("grade-review-fields"); box.textContent = "";
  pending.schema.components.forEach((component, ci) => {
    const section = el("section", "apparatus");
    const heading = el("h4", "label", component.name || "Category");
    const name = el("input", "field"); name.value = component.name; name.setAttribute("aria-label", "Category name");
    name.addEventListener("input", () => { component.name = name.value; heading.textContent = name.value; });
    const weight = el("input", "field"); weight.type = "number"; weight.min = "0"; weight.max = "100"; weight.step = "0.1";
    weight.value = (Number(component.weight || 0) * 100).toString(); weight.setAttribute("aria-label", `${component.name} course weight percent`);
    weight.addEventListener("input", () => { component.weight = Number(weight.value) / 100; });
    section.append(heading, el("label", "fine", "Category name"), name,
      el("label", "fine", "Course weight (%)"), weight);
    // Dropping only makes sense when a category has more than one assessment.
    // For a single exam there is nothing to drop, so the controls are omitted and
    // any stray drop rule is cleared (otherwise it fails validation on confirm).
    if (component.items.length > 1) {
      const drop = el("input", "field"); drop.type = "number"; drop.min = "0"; drop.max = String(component.items.length - 1); drop.step = "1"; drop.value = String(component.drop_lowest || 0);
      drop.setAttribute("aria-label", `Lowest scores dropped in ${component.name}`);
      drop.addEventListener("input", () => { component.drop_lowest = Number(drop.value); });
      section.append(el("label", "fine", "Lowest scores dropped"), drop);
      const highDrop = el("input", "field"); highDrop.type = "number"; highDrop.min = "0"; highDrop.max = String(component.items.length - 1); highDrop.step = "1"; highDrop.value = String(component.drop_highest || 0);
      highDrop.setAttribute("aria-label", `Highest scores dropped in ${component.name}`);
      highDrop.addEventListener("input", () => { component.drop_highest = Number(highDrop.value); });
      section.append(el("label", "fine", "Highest scores dropped"), highDrop);
    } else {
      component.drop_lowest = 0; component.drop_highest = 0;
    }
    const aggregation = el("select", "field"); aggregation.setAttribute("aria-label", `${component.name} averaging method`);
    for (const [value, label] of [["equal", "Equal average"], ["weighted", "Weighted assessments"]]) { const option = el("option", null, label); option.value = value; aggregation.append(option); }
    aggregation.value = component.aggregation || "equal";
    aggregation.addEventListener("change", () => { component.aggregation = aggregation.value; component.items.forEach((item) => { item.weight_within_category = aggregation.value === "weighted" ? 1 / component.items.length : null; }); renderGradeReview(pending); });
    section.append(el("label", "fine", "How this category is averaged"), aggregation);
    for (const [ii, item] of component.items.entries()) {
      const line = el("div", "stack");
      const field = el("input", "field"); field.value = item.name; field.setAttribute("aria-label", `${component.name} assessment ${ii + 1} name`);
      field.addEventListener("input", () => { item.name = field.value; });
      const remove = el("button", "plain", "Remove"); remove.type = "button";
      remove.addEventListener("click", () => { component.items.splice(ii, 1); component.drop_lowest = Math.min(component.drop_lowest, Math.max(0, component.items.length - 1)); renderGradeReview(pending); });
      line.append(field);
      if (component.aggregation === "weighted") {
        const share = el("input", "field"); share.type = "number"; share.min = "0"; share.max = "100"; share.step = "0.1";
        share.value = item.weight_within_category == null ? "" : String(Number(item.weight_within_category) * 100);
        share.setAttribute("aria-label", `${item.name} category weight percent`);
        share.addEventListener("input", () => { item.weight_within_category = share.value === "" ? null : Number(share.value) / 100; });
        line.append(share);
      }
      line.append(remove); section.append(line);
    }
    const add = el("button", "plain", "Add assessment"); add.type = "button";
    add.addEventListener("click", () => { const count = component.items.length + 1; component.items.push({name: `Assessment ${count}`, weight_within_category: component.aggregation === "weighted" ? 1 / count : null}); if (component.aggregation === "weighted") component.items.slice(0, -1).forEach((item) => { item.weight_within_category = 1 / count; }); renderGradeReview(pending); });
    const removeCategory = el("button", "plain", "Remove category"); removeCategory.type = "button";
    removeCategory.addEventListener("click", () => { pending.schema.components.splice(ci, 1); renderGradeReview(pending); });
    section.append(add, removeCategory); box.append(section);
  });
  const uncertainty = $("grade-uncertainties"); uncertainty.textContent = "";
  for (const rule of pending.schema.special_rules || []) uncertainty.append(el("p", "note", `Syllabus rule: ${rule}`));
  for (const item of pending.schema.extra_credit || []) {
    const row = el("div", "stack");
    const name = el("input", "field"); name.value = item.name; name.setAttribute("aria-label", "Extra-credit item name");
    name.addEventListener("input", () => { item.name = name.value; });
    const points = el("input", "field"); points.type = "number"; points.min = "0.1"; points.max = "100"; points.step = "0.1";
    points.placeholder = "Maximum course points"; points.value = item.maximum_percentage_points ?? "";
    points.setAttribute("aria-label", `Maximum course percentage points for ${item.name}`);
    points.addEventListener("input", () => { item.maximum_percentage_points = points.value === "" ? null : Number(points.value); });
    row.append(name, points); uncertainty.append(row);
    uncertainty.append(el("p", "fine", item.description || "Extra credit is separate from the normal course weights."));
  }
  for (const rule of pending.schema.replacement_rules || []) uncertainty.append(el("p", "note", `Replacement: ${rule.source_item_name} can replace the lowest assessment in ${rule.target_category_name}${rule.replace_if_higher ? " if higher" : ""}.`));
  for (const note of pending.schema.uncertainties || []) uncertainty.append(el("p", "note", `Review: ${note}`));
}

async function confirmGradeRules() {
  const pending = gradeData?.pending;
  if (!pending) return;
  try {
    await json(`/api/courses/${encodeURIComponent($("grades-course").value)}/grades/rules`, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({schema: pending.schema, extraction_id: pending.id}),
    });
    gradeWhatIf = {};
    await loadGradePredictor();
  } catch (error) { $("grades-status").textContent = `Please review the grade structure: ${error.message}`; }
}

function renderGradeFields() {
  const schema = gradeData.confirmed;
  const box = $("grade-fields");
  box.textContent = "";
  for (const component of schema.components) {
    const section = el("section", "grade-cat");
    const head = el("div", "grade-cat-head");
    head.append(el("span", "grade-cat-name", component.name));
    head.append(el("span", "grade-cat-weight", `${trimPct(component.weight * 100)}% of grade`));
    section.append(head);
    if (component.drop_lowest || component.drop_highest) {
      const parts = [];
      if (component.drop_lowest) parts.push(`lowest ${component.drop_lowest}`);
      if (component.drop_highest) parts.push(`highest ${component.drop_highest}`);
      section.append(el("p", "grade-drop", `Drops ${parts.join(" and ")} once every score is in.`));
    }
    for (const item of component.items) section.append(scoreRow(item.id, item.name));
    box.append(section);
  }
  if (schema.extra_credit?.length) {
    const section = el("section", "grade-cat");
    const head = el("div", "grade-cat-head");
    head.append(el("span", "grade-cat-name", "Extra credit"));
    section.append(head);
    for (const item of schema.extra_credit) {
      const note = item.maximum_percentage_points == null ? "points need review" : `up to ${item.maximum_percentage_points} pts`;
      section.append(scoreRow(item.id, `${item.name} · ${note}`));
    }
    box.append(section);
  }
  refreshGradeResults();
}

function trimPct(n) { return Number(Number(n).toFixed(1)).toString(); }

function scoreRow(id, label) {
  const row = el("div", "grade-row");
  const name = el("label", "grade-row-name", label);
  name.htmlFor = `score-${id}`;
  const wrap = el("div", "grade-input-wrap");
  const input = el("input", "grade-input");
  input.id = `score-${id}`; input.type = "number"; input.min = "0"; input.max = "100"; input.step = "0.1";
  input.inputMode = "decimal"; input.placeholder = "—";
  input.value = gradeData.scores[id] ?? "";
  input.setAttribute("aria-label", `${label} score in percent`);
  const pct = el("span", "grade-pct", "%");
  const status = el("span", "grade-save");
  input.addEventListener("change", async () => {
    const raw = input.value.trim();
    const score = raw === "" ? null : Number(raw);
    if (score !== null && (!Number.isFinite(score) || score < 0 || score > 100)) {
      input.value = gradeData.scores[id] ?? ""; return;
    }
    status.textContent = "saving…"; status.className = "grade-save";
    try {
      const result = await json(`/api/courses/${encodeURIComponent($("grades-course").value)}/grades/${encodeURIComponent(id)}`,
        { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ score }) });
      gradeData.scores = result.scores; gradeData.calculation = result.calculation;
      status.textContent = "saved"; status.className = "grade-save ok";
      row.dataset.filled = String(score !== null);
      refreshGradeResults();
    } catch (error) { status.textContent = "not saved"; status.className = "grade-save err"; }
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); input.blur(); } });
  row.dataset.filled = String(gradeData.scores[id] != null);
  wrap.append(input, pct);
  row.append(name, wrap, status);
  return row;
}

async function refreshGradeResults() {
  const schema = gradeData.confirmed;
  const ladderBox = $("grade-ladder");
  const note = $("grade-needed-note");
  // One call returns both the current standing and the per-letter ladder, so the
  // "grade so far" is always fresh (the initial GET may not include it).
  let result;
  try {
    result = await json(`/api/courses/${encodeURIComponent($("grades-course").value)}/grades/calculate`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
    gradeData.calculation = result.calculation;
  } catch (error) {
    renderGradeStanding();
    ladderBox.textContent = "";
    ladderBox.append(el("p", "note", `Could not calculate: ${error.message}`));
    return;
  }
  renderGradeStanding();
  const blanks = [];
  for (const c of schema.components) for (const it of c.items) if (gradeData.scores[it.id] == null) blanks.push(it.name);
  if (!blanks.length) {
    note.textContent = "Every assessment has a score — this is your final grade.";
    ladderBox.textContent = "";
    return;
  }
  const one = blanks.length === 1;
  note.textContent = one
    ? `You have one assessment left — ${blanks[0]}. Here is the score it needs for each grade.`
    : `You have ${blanks.length} assessments left. Here is the average across them needed for each grade.`;
  renderGradeLadder(result.ladder || [], one);
}

function renderGradeStanding() {
  const r = gradeData.calculation || {};
  const box = $("grade-summary"); box.textContent = "";
  box.append(el("div", "grade-now-label", "Grade so far"));
  const big = el("div", "grade-now");
  big.append(el("span", "grade-now-val", r.current_percent == null ? "—" : `${r.current_percent.toFixed(1)}%`));
  if (r.current_letter) big.append(el("span", "grade-now-letter", r.current_letter));
  box.append(big);
  const done = Math.round((r.completed_weight || 0) * 100);
  box.append(el("p", "grade-now-sub", `${done}% of the course is graded · ${100 - done}% still to come`));
  if (r.unallocated_weight > 0.001)
    box.append(el("p", "note", `${(r.unallocated_weight * 100).toFixed(1)}% of the grade is not covered by the confirmed categories — adjust the structure if this looks wrong.`));
}

function renderGradeLadder(ladder, singleField) {
  const box = $("grade-ladder"); box.textContent = "";
  if (!ladder.length) {
    box.append(el("p", "note", "This syllabus has no letter-grade thresholds. Use “Adjust grading structure” to add them."));
    return;
  }
  const table = el("table", "grade-ladder-table");
  const thead = el("thead"); const hr = el("tr");
  hr.append(el("th", null, "Grade"), el("th", "num", singleField ? "Score needed" : "Average needed"));
  thead.append(hr); table.append(thead);
  const tbody = el("tbody");
  for (const row of ladder) {
    const tr = el("tr", `grade-ladder-row grade-ladder-${row.status}`);
    const g = el("td", "grade-ladder-letter");
    g.append(el("span", "gl-letter", row.letter));
    g.append(el("span", "gl-thr", `≥ ${row.minimum}%`));
    tr.append(g);
    const need = el("td", "grade-ladder-need num");
    if (row.status === "guaranteed") { need.append(el("span", "gl-need locked", "Locked in ✓")); }
    else if (row.status === "impossible") { need.append(el("span", "gl-need out", "Out of reach")); need.append(el("span", "gl-sub", `max ${fmtNum(row.maximum_possible)}%`)); }
    else if (row.status === "achieved") { need.append(el("span", "gl-need locked", "Achieved ✓")); }
    else if (row.required_average != null) { need.append(el("span", "gl-need val", `${row.required_average.toFixed(1)}%`)); }
    else { need.append(el("span", "gl-need", row.detail || "—")); }
    tr.append(need);
    tbody.append(tr);
  }
  table.append(tbody); box.append(table);
}

function fmtNum(n) { return n == null ? "—" : n.toFixed(1); }

themeButton.addEventListener("click", () => {
  const now = document.documentElement.getAttribute("data-theme");
  applyTheme(now === "dark" ? "light" : "dark");
});
try {
  const saved = localStorage.getItem("cc-theme");
  if (saved) applyTheme(saved);
} catch { /* ignore */ }

// Depth is a per-user setting that persists; the "go deeper" control escalates
// one answer without changing the default.
const depthSelect = $("depth");
try {
  const d = localStorage.getItem("cc-depth");
  if (d) depthSelect.value = d;
} catch { /* ignore */ }
depthSelect.addEventListener("change", () => {
  try { localStorage.setItem("cc-depth", depthSelect.value); } catch { /* ignore */ }
  saveConsent({ depth: depthSelect.value }).catch(() => {});
});

/* ── boot ──────────────────────────────────────────────────────────────── */

async function boot() {
  try {
    const health = await json("/api/health");
    const b = health.backends;
    const bar = $("colophon");
    bar.textContent = "";
    for (const part of [
      `${health.chunks} chunks`, `embed ${b.embeddings}`, `llm ${b.llm}`,
      `rerank ${b.reranker}`, `refuse < ${health.refusal_threshold}`,
    ]) bar.append(el("span", null, part));
  } catch {
    $("colophon").textContent = "backend unreachable";
  }
  await loadCourses();
  await loadConsent();
  if (!COURSES.length) $("sheet").hidden = false;
}

async function loadConsent() {
  try {
    const prefs = await json("/api/consent");
    $("consent-copy").textContent = prefs.copy;
    $("consent-current").textContent = `Current backend: ${prefs.backend}. ` +
      (prefs.llm_consent ? "Free-tier Gemini consent is on." : "Free-tier consent is off.");
    if (prefs.depth) {
      depthSelect.value = prefs.depth;
      try { localStorage.setItem("cc-depth", prefs.depth); } catch { /* ignore */ }
    }
    voicePrefs = { ...voicePrefs, ...prefs,
      voice_api_configured: Boolean(prefs.voice_api_configured) };
    syncVoiceSettings();
    if (voicePrefs.voice_consent && voicePrefs.voice_api_configured) await loadVoiceChoices();
    // First launch pauses before cohort data can be entered. The choice is
    // stored server-side per user; the header button reopens these settings.
    if (!prefs.consent_decided && !prefs.has_own_gemini_key && !prefs.has_own_anthropic_key)
      $("consent-banner").hidden = false;
  } catch { /* the app remains usable as an extractive reader */ }
}

async function saveConsent(fields) {
  const result = await json("/api/consent", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  $("consent-current").textContent = `Current backend: ${result.backend}.`;
  return result;
}

function syncVoiceSettings() {
  const accepted = Boolean(voicePrefs.voice_consent);
  $("voice-consent-accept").hidden = accepted;
  $("voice-consent-revoke").hidden = !accepted;
  $("voice-only-toggle").checked = Boolean(voicePrefs.voice_only_mode);
  $("voice-only-toggle").disabled = !accepted || !voicePrefs.voice_available;
  $("voice-auto-submit").checked = Boolean(voicePrefs.auto_submit_voice);
  $("voice-auto-submit").disabled = !accepted;
  $("voice-choice").disabled = !accepted || !voicePrefs.voice_api_configured;
  $("voice-speed").disabled = !accepted || !voicePrefs.voice_api_configured;
  $("voice-speed").value = String(voicePrefs.speech_speed || 1);
  $("voice-consent-status").textContent = accepted
    ? "Voice consent is on. Recording starts only when you activate Ask by voice."
    : "Voice is off until you explicitly allow audio transcription and answer speech with ElevenLabs.";
  if (voicePrefs.voice_only_mode) {
    $("voice-only-panel").hidden = false;
    document.body.classList.add("voice-only");
  } else {
    $("voice-only-panel").hidden = true;
    document.body.classList.remove("voice-only");
  }
  if (!voicePrefs.voice_api_configured) {
    $("voice-settings-note").textContent = "ElevenLabs is not configured on this server. Typed questions and all other study features still work.";
  }
}

async function loadVoiceChoices() {
  const select = $("voice-choice");
  try {
    const data = await json("/api/voice/voices");
    const voices = data.voices || [];
    select.textContent = "";
    if (!voices.length) {
      select.append(el("option", null, "No voices available"));
      select.disabled = true;
      return;
    }
    for (const voice of voices) {
      const option = el("option", null, voice.name);
      option.value = voice.voice_id;
      select.append(option);
    }
    if (voices.some((voice) => voice.voice_id === voicePrefs.selected_voice_id)) {
      select.value = voicePrefs.selected_voice_id;
    } else {
      voicePrefs.selected_voice_id = voices[0].voice_id;
      select.value = voicePrefs.selected_voice_id;
      await saveVoicePreferences({ selected_voice_id: voicePrefs.selected_voice_id });
    }
    select.disabled = false;
    $("voice-settings-note").textContent = "Voice list loaded from ElevenLabs.";
  } catch (err) {
    $("voice-settings-note").textContent = `Could not load ElevenLabs voices: ${friendlyVoiceError(err)}. You can still type and use the configured default voice.`;
  }
}

async function saveVoicePreferences(fields) {
  const result = await json("/api/voice/preferences", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  voicePrefs = { ...voicePrefs, ...result };
  syncVoiceSettings();
  return result;
}

function friendlyVoiceError(error) {
  let message = String(error?.message || error || "");
  const detail = message.match(/^\d+\s+(\{.*\})$/s);
  let structured = null;
  if (detail) {
    try {
      const parsed = JSON.parse(detail[1]);
      structured = parsed.detail && typeof parsed.detail === "object" ? parsed.detail : null;
      message = typeof parsed.detail === "string" ? parsed.detail : message;
    } catch { /* keep the human-readable response */ }
  }
  const requestId = structured?.request_id ? ` (request ID ${structured.request_id})` : "";
  if (structured?.code === "ELEVENLABS_KEY_ID_USED_AS_KEY")
    return `${structured.message || "The ElevenLabs setting contains a key ID, not the API key secret."}${requestId}`;
  if (structured?.code === "ELEVENLABS_AUTHENTICATION_FAILED")
    return `ElevenLabs rejected the server API key or its permissions. Check the server configuration${requestId}`;
  if (structured?.code === "ELEVENLABS_RATE_LIMITED")
    return `ElevenLabs is rate-limiting requests. Please wait and try again${requestId}`;
  if (structured?.code === "ELEVENLABS_INSUFFICIENT_CREDITS")
    return `The ElevenLabs account has insufficient credits${requestId}`;
  if (structured?.code === "AUDIO_INVALID" || structured?.code === "AUDIO_FORMAT_UNSUPPORTED" ||
      structured?.code === "AUDIO_TOO_SHORT" || structured?.code === "AUDIO_TOO_LONG")
    return `${structured.message || "The recording could not be processed."}${requestId}`;
  if (structured?.message) return `${structured.message}${requestId}`;
  if (message.includes("403")) return "allow voice processing in AI & voice settings first";
  if (message.includes("413")) return "recording is over the 15 MB limit";
  if (message.includes("415")) return "this browser recording format is not supported";
  if (message.includes("429") || message.includes("503")) return "ElevenLabs is unavailable or rate-limited";
  if (message.includes("401") || message.includes("502")) return "ElevenLabs could not process the request";
  if (message.includes("504")) return "ElevenLabs timed out; try again";
  return message.replace(/^\d+\s*/, "") || "the voice request failed";
}

$("voice-consent-accept").addEventListener("click", async () => {
  try {
    await saveConsent({ voice_consent: true });
    voicePrefs.voice_consent = true;
    syncVoiceSettings();
    if (voicePrefs.voice_api_configured) await loadVoiceChoices();
    $("consent-banner").hidden = true;
    if (pendingVoiceStart) {
      pendingVoiceStart = false;
      await startVoiceRecording();
    }
  } catch (err) { $("voice-consent-status").textContent = `Could not save voice consent: ${friendlyVoiceError(err)}`; }
});

$("voice-consent-dismiss").addEventListener("click", () => {
  pendingVoiceStart = false;
  $("consent-banner").hidden = true;
  $("voice-status").textContent = "Voice consent was not changed. You can continue by typing your question.";
});

$("voice-consent-revoke").addEventListener("click", async () => {
  try {
    stopVoiceRecording();
    stopActiveAudio("Playback stopped because voice consent was revoked.");
    await saveConsent({ voice_consent: false });
    for (const url of spokenAudioCache.values()) URL.revokeObjectURL(url);
    spokenAudioCache.clear();
    voicePrefs.voice_consent = false;
    voicePrefs.voice_only_mode = false;
    syncVoiceSettings();
    $("voice-status").textContent = "Voice consent revoked. Typing remains available.";
  } catch (err) { $("voice-consent-status").textContent = `Could not revoke consent: ${friendlyVoiceError(err)}`; }
});

$("voice-only-toggle").addEventListener("change", async (event) => {
  if (event.target.checked && !voicePrefs.voice_consent) {
    event.target.checked = false;
    $("consent-banner").hidden = false;
    $("voice-consent-status").textContent = "Allow voice processing before enabling Voice Only Mode.";
    return;
  }
  try { await saveVoicePreferences({ voice_only_mode: event.target.checked }); }
  catch (err) { event.target.checked = !event.target.checked; $("voice-settings-note").textContent = friendlyVoiceError(err); }
  if (event.target.checked && voicePrefs.voice_only_mode)
    await speakText("Voice Only Mode is on. Activate Ask by voice to record a question. The microphone never listens in the background. Answers will be read aloud.", { status: $("voice-status"), auto: true });
});

$("voice-auto-submit").addEventListener("change", async (event) => {
  try { await saveVoicePreferences({ auto_submit_voice: event.target.checked }); }
  catch (err) { event.target.checked = !event.target.checked; $("voice-settings-note").textContent = friendlyVoiceError(err); }
});

$("voice-choice").addEventListener("change", async (event) => {
  try { await saveVoicePreferences({ selected_voice_id: event.target.value }); }
  catch (err) { $("voice-settings-note").textContent = friendlyVoiceError(err); }
});

$("voice-speed").addEventListener("change", async (event) => {
  try { await saveVoicePreferences({ speech_speed: Number(event.target.value) }); }
  catch (err) { $("voice-settings-note").textContent = friendlyVoiceError(err); }
});

$("voice-only-exit").addEventListener("click", async () => {
  try { await saveVoicePreferences({ voice_only_mode: false }); }
  catch (err) { $("voice-status").textContent = friendlyVoiceError(err); }
});

$("voice-record").addEventListener("click", async () => {
  if (mediaRecorder?.state === "recording") { stopVoiceRecording(); return; }
  if (!voicePrefs.voice_consent) {
    pendingVoiceStart = true;
    $("consent-banner").hidden = false;
    $("voice-consent-status").textContent = "Your recording will be sent to ElevenLabs for transcription. Allow voice processing to continue; recording will not start until you accept.";
    $("voice-consent-accept").focus();
    return;
  }
  await startVoiceRecording();
});

async function startVoiceRecording() {
  if (!voicePrefs.voice_api_configured) {
    $("voice-status").textContent = "Voice features are not configured on this server. You can continue by typing your question.";
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    $("voice-status").textContent = "This browser does not support audio recording. You can continue by typing.";
    return;
  }
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (error) {
    const denied = error?.name === "NotAllowedError" || error?.name === "PermissionDeniedError";
    $("voice-status").textContent = denied
      ? "Microphone access was denied. Allow it in browser settings to use voice input; you can still type."
      : "The microphone could not be started. Check that it is available, or type your question.";
    return;
  }

  const types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  const mimeType = types.find((type) => MediaRecorder.isTypeSupported?.(type));
  try { mediaRecorder = mimeType ? new MediaRecorder(mediaStream, { mimeType }) : new MediaRecorder(mediaStream); }
  catch {
    mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
    $("voice-status").textContent = "This browser cannot create a supported audio recording. You can still type.";
    return;
  }
  mediaChunks = [];
  mediaRecorder.addEventListener("dataavailable", (event) => {
    if (event.data?.size) mediaChunks.push(event.data);
  });
  mediaRecorder.addEventListener("error", () => {
    $("voice-status").textContent = "Recording failed. Please try again or type your question.";
    stopVoiceRecording();
  }, { once: true });
  mediaRecorder.addEventListener("stop", processVoiceRecording, { once: true });
  mediaRecorder.start();
  recordingStartedAt = Date.now();
  $("voice-record").textContent = "Stop recording";
  $("voice-record").setAttribute("aria-label", "Stop voice recording");
  $("voice-record").setAttribute("aria-pressed", "true");
  $("voice-status").textContent = "Listening. Activate Stop recording when you have finished speaking.";
  clearTimeout(recordingTimer);
  recordingTimer = setTimeout(() => {
    if (mediaRecorder?.state === "recording") {
      $("voice-status").textContent = "Recording reached the 60-second limit. Transcribing now.";
      stopVoiceRecording();
    }
  }, 60000);
}

function stopVoiceRecording() {
  clearTimeout(recordingTimer);
  if (mediaRecorder?.state === "recording") mediaRecorder.stop();
  else if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
  $("voice-record").textContent = "Ask by voice";
  $("voice-record").setAttribute("aria-label", "Start voice input");
  $("voice-record").setAttribute("aria-pressed", "false");
}

async function processVoiceRecording() {
  if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
  mediaStream = null;
  const stoppedRecorder = mediaRecorder;
  mediaRecorder = null;
  const type = mediaChunks.find((chunk) => chunk.type)?.type || stoppedRecorder?.mimeType || "application/octet-stream";
  const normalizedType = type.split(";", 1)[0].toLowerCase();
  const ext = ({ "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/aac": "aac" })[normalizedType];
  const blob = new Blob(mediaChunks, { type });
  const durationMs = Math.max(0, Date.now() - recordingStartedAt);
  mediaChunks = [];
  if (!blob.size || !ext) {
    $("voice-status").textContent = "No audio was captured. Try recording again.";
    return;
  }
  const filename = `question.${ext}`;
  console.info("Voice recording ready", {
    mime_type: type, size_bytes: blob.size, duration_ms: durationMs, filename,
  });
  $("voice-record").disabled = true;
  $("voice-status").textContent = "Transcribing your recording with ElevenLabs…";
  try {
    const form = new FormData();
    form.append("file", blob, filename);
    form.append("duration_ms", String(durationMs));
    const result = await json("/api/voice/transcribe", { method: "POST", body: form });
    $("question").value = result.text;
    if (voicePrefs.voice_only_mode && voicePrefs.auto_submit_voice) {
      $("voice-status").textContent = "Transcription complete. Sending it through the same grounded question flow…";
      $("askform").requestSubmit();
    } else {
      $("voice-status").textContent = "Transcription ready. Review or edit it in the question field, then select Ask.";
      $("question").focus();
      if (voicePrefs.voice_only_mode) await speakText(`I heard: ${result.text}`, { auto: true });
    }
  } catch (error) {
    $("voice-status").textContent = `Couldn't transcribe that recording: ${friendlyVoiceError(error)}. You can try again or type your question.`;
  } finally {
    $("voice-record").disabled = false;
  }
}

function stopActiveAudio(message = "Playback stopped.") {
  if (!activeAudio) return;
  activeAudio.pause();
  try { activeAudio.currentTime = 0; } catch { /* media not loaded */ }
  if (activePlayback?.status) activePlayback.status.textContent = message;
  setPlaybackControls(activePlayback?.controls, "idle");
  activeAudio = null;
  activePlayback = null;
}

function setPlaybackControls(controls, state) {
  if (!controls) return;
  controls.play.disabled = state === "generating" || state === "starting" || state === "playing";
  controls.pause.disabled = state !== "playing";
  controls.resume.disabled = state !== "paused";
  controls.stop.disabled = state !== "playing" && state !== "paused";
  controls.replay.disabled = state === "generating" || state === "starting";
}

function playbackControls(container, text, { auto = false, label = "Read aloud" } = {}) {
  container.textContent = "";
  container.hidden = false;
  container.classList.add("voice-playback");
  const status = el("span", "note");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const make = (title, action) => {
    const button = el("button", "plain", title);
    button.type = "button";
    button.onclick = action;
    container.append(button);
    return button;
  };
  const play = make(label, () => speakText(text, { status, controls: { play, pause, resume, stop, replay } }));
  const pause = make("Pause", () => {
    if (activePlayback?.status !== status || !activeAudio) return;
    activeAudio.pause(); status.textContent = "Playback paused.";
    setPlaybackControls(activePlayback.controls, "paused");
  });
  const resume = make("Resume", async () => {
    if (activePlayback?.status !== status || !activeAudio) return;
    try {
      await activeAudio.play(); status.textContent = "Reading response aloud.";
      setPlaybackControls(activePlayback.controls, "playing");
    } catch {
      status.textContent = "Playback could not resume. Select Read aloud to try again.";
      setPlaybackControls(activePlayback.controls, "paused");
    }
  });
  const stop = make("Stop", () => {
    if (activePlayback?.status === status) stopActiveAudio("Playback stopped.");
  });
  const replay = make("Replay", () => speakText(text, { status, controls: { play, pause, resume, stop, replay }, replay: true }));
  setPlaybackControls({ play, pause, resume, stop, replay }, "idle");
  container.append(status);
  if (auto) speakText(text, { status, controls: { play, pause, resume, stop, replay } });
  return container;
}

async function speakText(text, { status = $("voice-status"), controls = null, replay = false, auto = false } = {}) {
  const clean = String(text || "").trim();
  if (!clean) { status.textContent = "There is no answer text to read."; return; }
  if (!voicePrefs.voice_consent) {
    pendingVoiceStart = false;
    $("consent-banner").hidden = false;
    $("voice-consent-status").textContent = "The answer text will be sent to ElevenLabs to create audio. Allow voice processing before continuing.";
    $("voice-consent-accept").focus();
    status.textContent = "Allow voice processing in AI & voice settings before using Read aloud.";
    return;
  }
  if (!voicePrefs.voice_api_configured) {
    status.textContent = "Voice audio is unavailable because ElevenLabs is not configured on this server.";
    return;
  }
  const voiceId = voicePrefs.selected_voice_id || "configured-default";
  const key = `${voiceId}:${voicePrefs.speech_speed}:${clean}`;
  let url = spokenAudioCache.get(key);
  if (!url) {
    status.textContent = auto ? "Generating spoken answer…" : "Generating audio with ElevenLabs…";
    setPlaybackControls(controls, "generating");
    try {
      let pending = spokenAudioPending.get(key);
      if (!pending) {
        pending = (async () => {
        const response = await fetch("/api/voice/synthesize", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: clean }),
        });
        if (!response.ok) throw new Error(`${response.status} ${(await response.text()).slice(0, 180)}`);
        const blob = await response.blob();
        const generated = URL.createObjectURL(blob);
        spokenAudioCache.set(key, generated);
        while (spokenAudioCache.size > 8) {
          const oldest = spokenAudioCache.keys().next().value;
          const oldUrl = spokenAudioCache.get(oldest);
          spokenAudioCache.delete(oldest);
          if (oldUrl !== generated) URL.revokeObjectURL(oldUrl);
        }
        return generated;
        })();
        spokenAudioPending.set(key, pending);
      }
      url = await pending;
    } catch (error) {
      status.textContent = `Audio couldn't be generated: ${friendlyVoiceError(error)}. The answer remains available on screen.`;
      setPlaybackControls(controls, "idle");
      return;
    } finally {
      spokenAudioPending.delete(key);
    }
  }
  stopActiveAudio("Previous response stopped.");
  const audio = new Audio(url);
  activeAudio = audio;
  activePlayback = { status, controls };
  setPlaybackControls(controls, "starting");
  status.textContent = "Reading response aloud.";
  audio.addEventListener("ended", () => {
    if (activeAudio === audio) {
      status.textContent = "Playback complete. Activate Ask by voice when you are ready to speak again.";
      setPlaybackControls(activePlayback?.controls, "idle");
      activeAudio = null;
      activePlayback = null;
      if (voicePrefs.voice_only_mode) $("voice-record").focus();
    }
  }, { once: true });
  audio.addEventListener("error", () => {
    status.textContent = "The generated audio could not be played. Select Read aloud to try again.";
    if (activeAudio === audio) {
      setPlaybackControls(activePlayback?.controls, "idle");
      activeAudio = null;
      activePlayback = null;
    }
  }, { once: true });
  try {
    await audio.play();
    setPlaybackControls(controls, "playing");
  }
  catch {
    status.textContent = "Audio is ready. Your browser blocked automatic playback; select Resume to play it.";
    setPlaybackControls(controls, "paused");
  }
}

function addReadAloud(container, text, label = "Read aloud", auto = false) {
  const controls = el("div", "voice-playback");
  playbackControls(controls, text, { label, auto });
  container.append(controls);
  return controls;
}

window.addEventListener("beforeunload", () => {
  stopActiveAudio("Playback stopped.");
  for (const url of spokenAudioCache.values()) URL.revokeObjectURL(url);
});

$("consent-yes").addEventListener("click", async () => {
  try {
    const result = await saveConsent({ llm_consent: true, consent_decided: true });
    $("consent-banner").hidden = true;
    $("consent-current").textContent = `Free-tier Gemini enabled · ${result.backend}`;
  } catch (err) { $("consent-current").textContent = `Could not save choice: ${err}`; }
});

$("consent-key-save").addEventListener("click", async () => {
  const key = $("consent-key").value.trim();
  if (!key) { $("consent-current").textContent = "Paste your API key first."; return; }
  const provider = $("consent-provider").value;
  try {
    const fields = provider === "anthropic" ? { anthropic_api_key: key }
      : { gemini_api_key: key };
    const result = await saveConsent({ ...fields, consent_decided: true });
    $("consent-key").value = "";
    $("consent-banner").hidden = true;
    $("consent-current").textContent = `Your ${provider} key is saved on this machine · ${result.backend}`;
  } catch (err) { $("consent-current").textContent = `Could not save key: ${err}`; }
});

$("consent-no").addEventListener("click", async () => {
  try {
    await saveConsent({ llm_consent: false, consent_decided: true });
    $("consent-banner").hidden = true;
    $("consent-current").textContent = "Free-tier AI is off. Ask still provides cited textbook quotes.";
  } catch (err) { $("consent-current").textContent = `Could not save choice: ${err}`; }
});

async function loadCourses() {
  try { COURSES = (await json("/api/courses")).courses; }
  catch { COURSES = []; }

  const select = $("course");
  const previous = select.value;
  select.textContent = "";
  const first = el("option", null, COURSES.length ? "Choose a course" : "No courses yet");
  first.value = "";
  select.append(first);
  for (const c of COURSES) {
    const opt = el("option", null, `${c.code}${c.materials_loaded ? "" : " — no materials"}`);
    opt.value = c.course_id;
    opt.disabled = !c.materials_loaded;
    select.append(opt);
  }
  select.value = previous;
  renderCourseList();
  renderCourseNote();
}

$("course").addEventListener("change", renderCourseNote);

function currentCourse() {
  return COURSES.find((c) => c.course_id === $("course").value) || null;
}

function renderCourseNote() {
  const note = $("course-note");
  note.textContent = "";
  const course = currentCourse();
  if (!COURSES.length) {
    note.append("No materials yet. Open ");
    note.append(el("strong", null, "Courses & uploads"));
    note.append(", create a course, and add a syllabus and a textbook. " +
      "Answers come only from what you upload.");
    return;
  }
  if (!course) {
    note.append("Choose a course. Every answer is drawn from that course's " +
      "materials alone, and cited to the page.");
    return;
  }
  note.append(`${course.chunks.toLocaleString()} passages indexed. `);
  note.append(course.has_data_link
    ? "Real-world data is available for this course."
    : "No real-world data link — answers come from the readings alone.");
}

/* ── ask ───────────────────────────────────────────────────────────────── */

$("askform").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  const course = $("course").value || null;
  if (!question) return;
  if (!course) { renderCourseNote(); $("course").focus(); return; }

  await runAsk(question, course, depthSelect.value);
});

async function runAsk(question, course, depth) {
  stopActiveAudio("Previous response stopped because a new question was submitted.");
  $("ask-status").textContent = "Searching your course materials for an answer.";
  $("askform").setAttribute("aria-busy", "true");
  $("asksubmit").textContent = "Asking…";
  $("answer-voice-controls").hidden = true;
  $("explanation-voice-controls").hidden = true;
  $("asksubmit").disabled = true;
  $("explain-section").hidden = true;
  $("answer-section").hidden = false;
  $("passages-section").hidden = true;
  $("steps-section").hidden = true;
  $("data-section").hidden = true;
  skeleton($("answer"), [92, 78, 96, 64]);

  try {
    const result = await json("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, course, depth }),
    });
    renderAnswer(result);
    renderExplanation(result, question, course);
    renderSteps(result.steps, result.latency_ms);
    $("ask-status").textContent = result.refused
      ? "No supporting course passage was found. The answer explains the limitation."
      : "Answer ready. Sources and passages are available below.";
    const spoken = spokenAnswer(result);
    if (result.explained && result.explanation) {
      playbackControls($("explanation-voice-controls"), spoken, { label: "Read explanation aloud" });
    } else {
      playbackControls($("answer-voice-controls"), spoken, { label: "Read answer aloud" });
    }
    await Promise.allSettled([
      loadPassages(question, course),
      maybeLoadData(question, course),
    ]);
    if (voicePrefs.voice_only_mode) {
      const target = result.explained && result.explanation
        ? $("explanation-voice-controls") : $("answer-voice-controls");
      const status = target.querySelector('[role="status"]');
      const buttons = [...target.querySelectorAll("button")];
      speakText(spoken, { status, auto: true, controls: {
        play: buttons[0], pause: buttons[1], resume: buttons[2], stop: buttons[3], replay: buttons[4],
      } });
    }
  } catch (err) {
    $("answer").textContent = "";
    $("answer").append(el("p", null, `That request failed: ${err}`));
    $("ask-status").textContent = "The question could not be answered because the request failed.";
  } finally {
    $("asksubmit").disabled = false;
    $("asksubmit").textContent = "Ask";
    $("askform").removeAttribute("aria-busy");
  }
}

function spokenAnswer(result) {
  let text = result.explained && result.explanation ? result.explanation : result.answer;
  text = String(text || "").replace(CITE_RE, " ")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/[#*`>]+/g, "")
    .replace(/\s+/g, " ").trim();
  const chapters = [...new Set((result.citations || [])
    .map((citation) => citation.chapter_title || (citation.chapter_num ? `Chapter ${citation.chapter_num}` : ""))
    .filter(Boolean))].slice(0, 2);
  if (voicePrefs.voice_only_mode && chapters.length)
    text += ` This explanation is grounded in your course material from ${chapters.join(" and ")}.`;
  return text;
}

/* The explanation layer sits ABOVE the verbatim answer; the source stays
   visible and the sidenotes still point at the real text. */
function backendBadge(backend) {
  const generated = backend && !/^extractive/i.test(backend) && !backend.startsWith("—");
  const badge = el("span", "backend-badge " + (generated ? "gen" : "ext"),
    (generated ? "answered by " : "") + (backend || "—"));
  return badge;
}

function renderExplanation(result, question, course) {
  const box = $("explanation");
  const section = $("explain-section");
  const deeper = $("go-deeper");
  const label = $("explain-label");
  // Always surface which backend answered, so an extractive fallback can never
  // look like a generated explanation.
  if (!result.explained || !result.explanation) {
    // No generation: show the extractive answer alone, clearly labelled on it.
    section.hidden = true;
    const head = $("answer-section").querySelector(".label");
    head.textContent = "From the text  ";
    head.append(backendBadge(result.backend));
    return;
  }
  section.hidden = false;
  label.textContent =
    "Explanation · " + (result.depth === "in_depth" ? "in-depth" : "concise") + "  ";
  label.append(backendBadge(result.backend));
  renderAnswerWithCites(box, result.explanation);
  deeper.hidden = result.depth === "in_depth";
  deeper.onclick = () => runAsk(question, course, "in_depth");
}

/* Skeleton text in the real measure, leading and family. */
function skeleton(container, widths) {
  container.textContent = "";
  const block = el("div", "skeleton");
  for (const w of widths) {
    const line = el("span");
    line.style.width = `${w}%`;
    block.append(line, document.createTextNode(" "));
  }
  container.append(block);
}

/* The central rendering decision: markers in the line, notes in the margin.
   Shared by the verbatim answer and the explanation gloss, since both are cited
   prose over the same passages. */
function renderAnswer(result) {
  const box = $("answer");
  box.className = "reading" + (result.refused ? " refused" : "");
  renderAnswerWithCites(box, result.answer, result.citations || [], result.refused);
}

function renderAnswerWithCites(box, text, citations = [], refused = false) {
  box.textContent = "";
  if (!box.classList.contains("reading")) box.classList.add("reading");

  text = text || "";
  if (!text.trim()) {
    box.append(el("p", null, "No answer was produced."));
    return;
  }

  const byLabel = new Map();
  for (const c of citations) byLabel.set(c.label, c);

  const paragraph = el("p");
  const seen = new Map();          // label -> marker number, so repeats share one
  let cursor = 0;
  let counter = 0;

  for (const match of text.matchAll(CITE_RE)) {
    if (match.index > cursor) {
      paragraph.append(text.slice(cursor, match.index).replace(/\s+$/, " "));
    }
    const label = match[0];
    let number = seen.get(label);
    const firstUse = number === undefined;
    if (firstUse) { number = ++counter; seen.set(label, number); }

    const marker = el("sup", "sn-ref", String(number));
    marker.dataset.n = String(number);
    marker.tabIndex = 0;
    paragraph.append(marker);

    if (firstUse) {
      paragraph.append(buildSidenote(number, label, byLabel.get(label), match));
    }
    cursor = match.index + label.length;
  }
  if (cursor < text.length) paragraph.append(text.slice(cursor));
  box.append(paragraph);
  renderMath(paragraph);

  if (!counter && !refused) {
    box.append(el("p", "note", "This answer carries no citation, which should not happen — treat it with suspicion."));
  }
}

function buildSidenote(number, label, citation, match) {
  const note = el("span", "sidenote");
  note.dataset.n = String(number);
  note.append(el("span", "n", String(number)));

  const courseId = (citation?.course_id || match[1] || "").toUpperCase();
  const chapterTitle = citation?.chapter_title || "";
  const chapterNum = citation?.chapter_num ?? (match[2] ? Number(match[2]) : 0);
  const pages = match[3] || "";

  // Chapter title first — it is the part a reader recognises.
  note.append(chapterTitle || (chapterNum ? `Chapter ${chapterNum}` : "Reading"));

  const where = el("span", "where");
  where.append([courseId, chapterNum ? `ch. ${chapterNum}` : null, pages]
    .filter(Boolean).join("  ·  "));
  note.append(where);
  return note;
}

/* Hovering or focusing either half lights both. This replaces the old
   hover-to-highlight on citation chips and does the same job in print's idiom. */
function linkPair(active, n) {
  document.querySelectorAll(`.sn-ref[data-n="${n}"], .sidenote[data-n="${n}"]`)
    .forEach((node) => { node.dataset.active = String(active); });
}
for (const [on, off] of [["mouseover", "mouseout"], ["focusin", "focusout"]]) {
  document.addEventListener(on, (e) => {
    const t = e.target.closest?.(".sn-ref, .sidenote");
    if (t?.dataset.n) linkPair(true, t.dataset.n);
  });
  document.addEventListener(off, (e) => {
    const t = e.target.closest?.(".sn-ref, .sidenote");
    if (t?.dataset.n) linkPair(false, t.dataset.n);
  });
}

function renderSteps(steps, latency) {
  const list = $("steps");
  list.textContent = "";
  $("steps-section").hidden = false;
  for (const s of steps || []) {
    const li = el("li", s.kind === "error" ? "error" : null);
    li.append(el("span", "k", s.name || s.kind));
    li.append(el("span", "v", s.detail || ""));
    if (s.elapsed_ms) li.append(el("span", "ms", `${s.elapsed_ms} ms`));
    list.append(li);
  }
  const total = el("li");
  total.append(el("span", "k", "total"));
  total.append(el("span", "v", ""));
  total.append(el("span", "ms", `${latency} ms`));
  list.append(total);
}

async function loadPassages(question, course) {
  const result = await json("/api/passages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, course }),
  });
  const box = $("passages");
  box.textContent = "";
  $("passages-section").hidden = false;

  const passages = result.passages || [];
  if (!passages.length) {
    box.append(el("p", "empty",
      result.reason || "Nothing in this course's materials matches that question."));
    return;
  }
  for (const p of passages) {
    const wrap = el("div", "passage");
    wrap.dataset.chunk = p.chunk_id;
    const source = el("div", "source");
    source.append(el("span", null,
      [p.citation.label, p.chapter_title].filter(Boolean).join("  ·  ")));
    source.append(el("span", null, `score ${Number(p.score).toFixed(3)}`));
    wrap.append(source);
    wrap.append(el("div", "body", p.snippet));
    box.append(wrap);
    renderMath(wrap);
  }
}

/* ── real-world data: an Ask sidecar, only where the course earned it ──── */

async function maybeLoadData(question, course) {
  const meta = COURSES.find((c) => c.course_id === course);
  if (!meta || !meta.has_data_link) return;

  const result = await json("/api/data/evidence", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, course }),
  });
  const box = $("datacard");
  box.textContent = "";
  $("data-section").hidden = false;

  if (!result.found) {
    box.append(el("p", "note", result.detail || "No matching series."));
    return;
  }
  for (const hit of result.series || []) {
    const fig = el("div", "figure");
    const cap = el("div", "caption");
    cap.append(el("span", "sid", hit.series_id));
    cap.append(` ${hit.title}`);
    fig.append(cap);
    if (hit.points?.length) fig.append(sparkline(hit.points));
    fig.append(el("div", "caption",
      [hit.frequency, hit.units, hit.transform].filter(Boolean).join("  ·  ")));
    box.append(fig);
  }
  if (result.warning) box.append(el("p", "warning", result.warning));
  box.append(el("p", "warning quiet",
    "These are data, not course materials. The textbook is never the source of these numbers."));
}

function sparkline(points) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 300 48");
  svg.setAttribute("preserveAspectRatio", "none");
  const values = points.map((p) => p.value).filter(Number.isFinite);
  if (values.length < 2) return svg;
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const d = values.map((v, i) =>
    `${i ? "L" : "M"}${(i / (values.length - 1)) * 300},${46 - ((v - lo) / span) * 44}`
  ).join(" ");
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", d);
  path.setAttribute("class", "plot");
  svg.append(path);
  return svg;
}

/* ── schedule ──────────────────────────────────────────────────────────── */

async function loadDashboard() {
  // The month calendar + rings are the centrepiece; render them first.
  loadCalendar();

  let data;
  try { data = await json("/api/dashboard"); }
  catch (err) { return; }

  renderUndated(data.undated || [], data.undated_note || "");
  await Promise.allSettled([renderProgress(), renderAssessments()]);

  // Readiness
  const exam = $("exam");
  exam.textContent = "";
  if (!data.exam) {
    $("exam-head").textContent = "Exam readiness";
    exam.append(el("p", "empty",
      "No assessment on the schedule yet. Upload a syllabus and its dates will appear here."));
    return;
  }
  $("exam-head").textContent =
    `${data.exam.course_id} ${data.exam.title} · ${data.exam.date} · ${data.exam.days_away} days`;

  for (const ch of data.exam.chapters) {
    const row = el("div", "row");
    const chapter = el("div", "chapter");
    chapter.append(el("span", "num", `${ch.chapter}`));
    chapter.append(ch.label);
    if (ch.status === "none") chapter.append(el("span", "start", "  start here"));
    row.append(chapter);

    const gaugeWrap = el("div");
    const gauge = el("span", "gauge");
    const fill = el("i", ch.status);
    fill.style.width = `${Math.max(3, ch.derived * 100)}%`;
    gauge.append(fill);
    gaugeWrap.append(gauge);
    if (ch.manual !== null)
      gaugeWrap.append(el("div", "self", `self-reported ${Math.round(ch.manual * 100)}%`));
    row.append(gaugeWrap);

    row.append(el("div", "count",
      `${ch.questions_asked} questions · ${ch.ps_questions_hit} practice attempts (2× weight)`));
    exam.append(row);
  }
}

/* ── calendar: month grid, rings, day detail, manual events ────────────── */

const CAL_COLORS = ["#c2704a", "#6f8a63", "#7c6cae", "#9a854f", "#4f83a6", "#a3577f", "#5f9e8f"];
const WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const calState = { year: 0, month: 0, byDate: {}, courses: [], colorOf: {}, selected: null, editing: null };

function isoOf(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
function courseColor(cid) { return cid ? (calState.colorOf[cid] || "var(--ink-3)") : "var(--ink-3)"; }
// Personal events (no course) get a colour by their kind so the calendar stays legible.
const KIND_COLORS = { club: "#7c6cae", meeting: "#4f83a6", project: "#9a854f", research: "#6f8a63", job: "#a3577f", deadline: "#c2704a", exam: "#a35f52", custom: "#8a8072", class: "#6f8a63" };
function itemColor(it) { return it.course_id ? courseColor(it.course_id) : (KIND_COLORS[it.kind] || "var(--ink-3)"); }
// Scheduled commitments you attend rather than "complete": no check-off, not counted as work.
const ATTEND_KINDS = new Set(["class", "club", "job"]);

async function loadCalendar() {
  const now = new Date();
  if (!calState.year) { calState.year = now.getFullYear(); calState.month = now.getMonth(); }
  const first = new Date(calState.year, calState.month, 1);
  const gridStart = new Date(first); gridStart.setDate(1 - ((first.getDay() + 6) % 7));
  const gridEnd = new Date(gridStart); gridEnd.setDate(gridStart.getDate() + 41);
  let feed;
  try {
    feed = await json(`/api/calendar?start=${isoOf(gridStart)}&end=${isoOf(gridEnd)}`);
  } catch { $("cal-grid").textContent = ""; return; }
  calState.courses = feed.courses || [];
  calState.colorOf = {};
  calState.courses.forEach((c, i) => { calState.colorOf[c.course_id] = CAL_COLORS[i % CAL_COLORS.length]; });
  calState.byDate = {};
  for (const it of feed.items) (calState.byDate[it.date] ||= []).push(it);
  renderCalendarGrid(gridStart);
  renderCalendarLegend();
  populateEventCourseOptions();
  renderRings();
  if (calState.selected && calState.byDate[calState.selected] !== undefined) openDay(calState.selected);
}

function renderCalendarGrid(gridStart) {
  $("cal-month").textContent = new Date(calState.year, calState.month, 1)
    .toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const grid = $("cal-grid"); grid.textContent = "";
  const todayIso = isoOf(new Date());
  for (let i = 0; i < 42; i++) {
    const d = new Date(gridStart); d.setDate(gridStart.getDate() + i);
    const iso = isoOf(d);
    const inMonth = d.getMonth() === calState.month;
    const items = calState.byDate[iso] || [];
    const holiday = items.find((x) => x.kind === "holiday");
    const cell = el("button", "cal-cell");
    cell.type = "button";
    cell.setAttribute("role", "gridcell");
    if (!inMonth) cell.classList.add("out");
    if (iso === todayIso) cell.classList.add("today");
    if (holiday) cell.classList.add("holiday");
    if (iso === calState.selected) cell.classList.add("sel");
    cell.setAttribute("aria-label", `${d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}, ${items.length} item${items.length === 1 ? "" : "s"}`);
    const num = el("span", "cal-num", String(d.getDate()));
    cell.append(num);
    if (holiday) cell.append(el("span", "cal-holiday", holiday.title));
    const tasks = items.filter((x) => x.kind !== "holiday");
    const list = el("span", "cal-chips");
    for (const it of tasks.slice(0, 3)) {
      const chip = el("span", `cal-chip k-${it.kind}${it.done ? " done" : ""}`);
      chip.style.setProperty("--c", itemColor(it));
      chip.append(el("i", "cal-dot"));
      chip.append(el("span", "cal-chip-t", chipLabel(it)));
      list.append(chip);
    }
    if (tasks.length > 3) list.append(el("span", "cal-more", `+${tasks.length - 3} more`));
    cell.append(list);
    cell.addEventListener("click", () => openDay(iso));
    grid.append(cell);
  }
}

function chipLabel(it) {
  const t = (it.start_time ? `${it.start_time} ` : "") + cleanTitle(it.title);
  return t.length > 26 ? t.slice(0, 25) + "…" : t;
}
function cleanText(s) { return String(s || "").replace(/\s+/g, " ").trim(); }
/* Syllabus tables often prefix a row with its weekday+date marker ("R Sep 24 …");
   strip that so the calendar shows the topic, not the raw table cell. */
function cleanTitle(s) {
  return cleanText(s).replace(
    /^(?:M|T|W|R|F|Sa|Su|MW|TR|TTh|MWF|MTWRF)\s+[A-Z][a-z]{2,3}\.?\s+\d{1,2}\b[\s.:–-]*/,
    "");
}

function renderCalendarLegend() {
  const box = $("cal-legend"); box.textContent = "";
  for (const c of calState.courses) {
    const tag = el("span", "cal-leg");
    const dot = el("i", "cal-dot"); dot.style.setProperty("--c", courseColor(c.course_id));
    tag.append(dot, document.createTextNode(" " + c.code));
    box.append(tag);
  }
}

async function renderRings() {
  let p;
  try { p = await json("/api/calendar/progress"); } catch { return; }
  const box = $("cal-rings"); box.textContent = "";
  box.append(donut(p.today, "Today"));
  box.append(donut(p.week, "This week"));
  box.append(donut(p.semester, "This semester"));
}

function donut(stat, label) {
  const wrap = el("div", "ring");
  const pct = stat.pct || 0;
  const r = 34, C = 2 * Math.PI * r;
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 80 80"); svg.setAttribute("class", "ring-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${label}: ${stat.done} of ${stat.total} done, ${pct}%`);
  const bg = document.createElementNS(NS, "circle");
  bg.setAttribute("cx", "40"); bg.setAttribute("cy", "40"); bg.setAttribute("r", String(r));
  bg.setAttribute("class", "ring-bg");
  const arc = document.createElementNS(NS, "circle");
  arc.setAttribute("cx", "40"); arc.setAttribute("cy", "40"); arc.setAttribute("r", String(r));
  arc.setAttribute("class", "ring-arc");
  arc.setAttribute("stroke-dasharray", `${C * pct / 100} ${C}`);
  arc.setAttribute("transform", "rotate(-90 40 40)");
  const txt = document.createElementNS(NS, "text");
  txt.setAttribute("x", "40"); txt.setAttribute("y", "45"); txt.setAttribute("class", "ring-pct");
  txt.textContent = `${pct}%`;
  svg.append(bg, arc, txt);
  wrap.append(svg);
  wrap.append(el("div", "ring-label", label));
  wrap.append(el("div", "ring-sub", `${stat.done}/${stat.total} done`));
  return wrap;
}

function openDay(iso) {
  calState.selected = iso;
  document.querySelectorAll(".cal-cell.sel").forEach((c) => c.classList.remove("sel"));
  const panel = $("cal-day"); panel.hidden = false;
  const d = new Date(iso + "T00:00:00");
  $("cal-day-title").textContent = d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  const body = $("cal-day-body"); body.textContent = "";
  const items = (calState.byDate[iso] || []).slice();
  if (!items.length) {
    body.append(el("p", "note", "Nothing scheduled. Use “+ Add event” to add a class, deadline, or reminder."));
  }
  const add = el("button", "plain cal-day-add", "+ Add on this day");
  add.type = "button";
  add.addEventListener("click", () => openEventForm({ date: iso }));
  body.append(add);
  for (const it of items) body.append(dayItemRow(it));
  renderCalendarGrid(gridStartForState());
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function gridStartForState() {
  const first = new Date(calState.year, calState.month, 1);
  const gs = new Date(first); gs.setDate(1 - ((first.getDay() + 6) % 7));
  return gs;
}

function dayItemRow(it) {
  const row = el("div", `cal-item k-${it.kind}${it.done ? " done" : ""}`);
  row.style.setProperty("--c", itemColor(it));
  const left = el("div", "cal-item-main");
  const head = el("div", "cal-item-head");
  if (it.kind === "holiday") {
    head.append(el("span", "cal-item-badge holiday", "Holiday"));
  } else {
    const box = el("input", "cal-check"); box.type = "checkbox"; box.checked = !!it.done;
    box.setAttribute("aria-label", `Mark ${cleanText(it.title)} done`);
    const isTask = !ATTEND_KINDS.has(it.kind);
    if (!isTask) box.disabled = true, box.title = "Class meetings are not tasks";
    box.addEventListener("change", () => toggleItem(it, box.checked));
    head.append(box);
  }
  const title = el("span", "cal-item-title", it.kind === "holiday" ? cleanText(it.title) : cleanTitle(it.title));
  head.append(title);
  left.append(head);
  const meta = [];
  if (it.course_code) meta.push(it.course_code);
  if (it.kind && it.kind !== "concept" && it.kind !== "class") meta.push(prettyKind(it.kind));
  if (it.start_time) meta.push(it.end_time ? `${it.start_time}–${it.end_time}` : it.start_time);
  if (it.location) meta.push("📍 " + it.location);
  if (meta.length) left.append(el("div", "cal-item-meta", meta.join(" · ")));
  if (it.chapter_refs?.length) left.append(el("div", "cal-item-meta", "Ch " + it.chapter_refs.map((r) => String(r).split(":").pop()).join(", ")));
  if (it.readings?.length) left.append(el("div", "cal-item-meta", cleanText(it.readings.join("; ")).slice(0, 120)));
  row.append(left);
  if (it.source === "event" && it.editable) {
    const edit = el("button", "plain cal-edit", "Edit"); edit.type = "button";
    edit.addEventListener("click", () => openEventForm({ id: it.event_id }));
    row.append(edit);
  }
  return row;
}

function prettyKind(k) { return ({ deadline: "Deadline", exam: "Exam", project: "Project", research: "Research", club: "Club", meeting: "Meeting", job: "Job", homework: "Homework", problem_set: "Problem set", paper: "Paper", quiz: "Quiz", due: "Due", custom: "Event" })[k] || k; }

async function toggleItem(it, done) {
  try {
    await json("/api/calendar/toggle", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ item_id: it.id, done }) });
    it.done = done;
    renderRings();
    renderCalendarGrid(gridStartForState());
    openDay(calState.selected);
  } catch { /* revert on failure */ loadCalendar(); }
}

/* ── event add / edit / delete ─────────────────────────────────────────── */

function populateEventCourseOptions() {
  const sel = $("ev-course"); if (!sel) return;
  sel.textContent = "";
  const none = el("option", null, "No course (personal)"); none.value = ""; sel.append(none);
  for (const c of calState.courses) { const o = el("option", null, c.code); o.value = c.course_id; sel.append(o); }
}

function buildDayChips() {
  const box = $("ev-days"); if (!box || box.childElementCount) return;
  ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach((lbl, i) => {
    const b = el("button", "cal-day-chip"); b.type = "button"; b.textContent = lbl; b.dataset.code = WEEKDAY_CODES[i];
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => { const on = b.getAttribute("aria-pressed") === "true"; b.setAttribute("aria-pressed", String(!on)); });
    box.append(b);
  });
}

function openEventForm(preset = {}) {
  buildDayChips();
  const form = $("cal-form");
  calState.editing = preset.id || null;
  form.reset();
  document.querySelectorAll("#ev-days .cal-day-chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
  $("ev-recur").checked = false; $("ev-recur-opts").hidden = true;
  $("ev-delete").hidden = true; $("ev-status").textContent = "";
  if (preset.id) {
    const src = (Object.values(calState.byDate).flat()).find((x) => x.event_id === preset.id);
    $("cal-form-title").textContent = "Edit event";
    $("ev-delete").hidden = false;
    // Pull authoritative fields from a fresh GET is overkill; use the item we have.
    if (src) {
      $("ev-title").value = cleanText(src.title); $("ev-course").value = src.course_id || "";
      $("ev-kind").value = src.kind || "custom"; $("ev-date").value = src.date;
      $("ev-start").value = src.start_time || ""; $("ev-end").value = src.end_time || "";
      $("ev-loc").value = src.location || "";
      if (src.recurring) {
        $("ev-recur").checked = true; $("ev-recur-opts").hidden = false;
      }
    }
  } else {
    $("cal-form-title").textContent = "Add event";
    if (preset.date) $("ev-date").value = preset.date;
  }
  form.hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "center" });
  $("ev-title").focus();
}

function collectEvent() {
  const days = [...document.querySelectorAll("#ev-days .cal-day-chip")].filter((c) => c.getAttribute("aria-pressed") === "true").map((c) => c.dataset.code);
  const recurring = $("ev-recur").checked;
  return {
    title: $("ev-title").value.trim(), course_id: $("ev-course").value,
    kind: $("ev-kind").value, date: $("ev-date").value || null,
    start_time: $("ev-start").value, end_time: $("ev-end").value,
    location: $("ev-loc").value,
    recurrence: recurring ? "weekly" : "none",
    recur_days: recurring ? days.join(",") : "",
    recur_until: recurring ? ($("ev-until").value || null) : null,
  };
}

/* wire calendar controls (elements are static in index.html) */
if ($("cal-prev")) {
  $("cal-prev").addEventListener("click", () => { shiftMonth(-1); });
  $("cal-next").addEventListener("click", () => { shiftMonth(1); });
  $("cal-today").addEventListener("click", () => { const n = new Date(); calState.year = n.getFullYear(); calState.month = n.getMonth(); calState.selected = isoOf(n); loadCalendar(); });
  $("cal-add").addEventListener("click", () => openEventForm({}));
  $("cal-day-close").addEventListener("click", () => { $("cal-day").hidden = true; calState.selected = null; renderCalendarGrid(gridStartForState()); });
  $("cal-form-cancel").addEventListener("click", () => { $("cal-form").hidden = true; });
  $("ev-recur").addEventListener("change", () => { $("ev-recur-opts").hidden = !$("ev-recur").checked; buildDayChips(); });
  $("cal-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = collectEvent();
    if (!body.title) { $("ev-status").textContent = "A title is required."; return; }
    if (body.recurrence === "weekly" && !body.recur_days) { $("ev-status").textContent = "Pick at least one weekday to repeat on."; return; }
    if (body.recurrence === "none" && !body.date) { $("ev-status").textContent = "Pick a date."; return; }
    $("ev-status").textContent = "Saving…";
    try {
      const url = calState.editing ? `/api/calendar/events/${calState.editing}` : "/api/calendar/events";
      await json(url, { method: calState.editing ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      $("cal-form").hidden = true; await loadCalendar();
    } catch (err) { $("ev-status").textContent = `Could not save: ${err.message}`; }
  });
  $("ev-delete").addEventListener("click", async () => {
    if (!calState.editing) return;
    $("ev-status").textContent = "Deleting…";
    try { await json(`/api/calendar/events/${calState.editing}`, { method: "DELETE" }); $("cal-form").hidden = true; await loadCalendar(); }
    catch (err) { $("ev-status").textContent = `Could not delete: ${err.message}`; }
  });
}

function shiftMonth(delta) {
  let m = calState.month + delta, y = calState.year;
  if (m < 0) { m = 11; y--; } else if (m > 11) { m = 0; y++; }
  calState.month = m; calState.year = y; loadCalendar();
}

/* ── progress: two bars on one axis ────────────────────────────────────── */

async function renderProgress() {
  let data;
  try { data = await json("/api/progress-report"); } catch { return; }
  const box = $("progress");
  box.textContent = "";

  const bars = (p) => {
    const wrap = el("div");
    wrap.append(pbar("Class pace", p.pace, p.meetings_to_date, p.meetings_total, "meetings", "pace"));
    wrap.append(pbar("Your progress", p.progress, p.topics_engaged, p.topics_to_date, "topics engaged", "mine"));
    if (p.behind > 0) {
      const b = el("div", "pbehind", `${p.behind} ${p.behind === 1 ? "topic" : "topics"} behind`);
      wrap.append(b);
    }
    return wrap;
  };

  $("progress-scope").textContent = "· all courses";
  box.append(bars(data.rollup));
  for (const c of data.courses) {
    const cc = el("div", "pcourse");
    cc.append(el("div", "pcode", c.code));
    cc.append(bars(c));
    box.append(cc);
  }
}

function pbar(label, frac, num, denom, unit, cls) {
  const row = el("div", `pbar ${cls}`);
  row.append(el("div", "plabel", label));
  const track = el("div", "track");
  const fill = el("i");
  fill.style.width = `${Math.round((frac || 0) * 100)}%`;
  track.append(fill);
  row.append(track);
  const num_ = el("div", "pnum");
  num_.append(`${Math.round((frac || 0) * 100)}%  `);
  num_.append(el("span", "sub", `${num}/${denom} ${unit}`));
  row.append(num_);
  return row;
}

/* ── undated outline rows ──────────────────────────────────────────────── */

function renderUndated(rows, note) {
  const section = $("undated-section");
  section.hidden = rows.length === 0;
  $("undated-note").textContent = note;
  const body = $("undated");
  body.textContent = "";
  for (const r of rows) {
    const tr = el("tr");
    tr.append(el("td", "day", r.course_id));
    const t = el("td");
    t.append(el("span", "topic", r.topic));
    tr.append(t);
    tr.append(el("td", null, r.link_title || (r.chapter_refs[0] ? "linked" : "—")));
    body.append(tr);
  }
}

/* ── due banner: anything within 7 days, dismissible per item ──────────── */

let dismissed = new Set();
try { dismissed = new Set(JSON.parse(localStorage.getItem("cc-dismissed") || "[]")); } catch {}

function renderBanner(items) {
  const banner = $("due-banner");
  const soon = items.filter((a) => a.due_date && !a.overdue_done && a.status !== "done"
    && daysUntil(a.due_date) >= 0 && daysUntil(a.due_date) <= 7 && !dismissed.has(a.id));
  banner.textContent = "";
  banner.hidden = soon.length === 0;
  if (!soon.length) return;
  banner.append(el("div", "bhead", "Due within 7 days"));
  for (const a of soon) {
    const row = el("div", "bitem");
    const d = daysUntil(a.due_date);
    row.append(el("span", "when", d === 0 ? "today" : d === 1 ? "1 day" : `${d} days`));
    row.append(`${a.kind_label}: ${a.title} (${a.course_id.toUpperCase()})`);
    const x = el("button", "x", "dismiss");
    x.onclick = () => {
      dismissed.add(a.id);
      try { localStorage.setItem("cc-dismissed", JSON.stringify([...dismissed])); } catch {}
      renderBanner(items);
    };
    row.append(x);
    banner.append(row);
  }
}

function daysUntil(iso) {
  const d = new Date(iso + "T00:00:00");
  return Math.round((d - new Date(new Date().toDateString())) / 86400000);
}

/* ── assessments: grouped by week, overdue first, status + manual edit ─── */

async function renderAssessments() {
  let items;
  try { items = (await json("/api/assessments")).assessments; } catch { return; }
  renderBanner(items);

  const box = $("assessments");
  box.textContent = "";

  const overdue = items.filter((a) => a.overdue);
  const rest = items.filter((a) => !a.overdue);
  if (overdue.length) {
    const g = el("div", "weekgroup");
    g.append(el("div", "wk", "Overdue"));
    overdue.forEach((a) => g.append(arow(a, items)));
    box.append(g);
  }

  // group the rest by ISO week of due date; undated last
  const groups = new Map();
  for (const a of rest) {
    const key = a.due_date ? weekLabel(a.due_date) : "No date";
    (groups.get(key) || groups.set(key, []).get(key)).push(a);
  }
  for (const [label, rows] of groups) {
    const g = el("div", "weekgroup");
    g.append(el("div", "wk", label));
    rows.forEach((a) => g.append(arow(a, items)));
    box.append(g);
  }
  if (!items.length) box.append(el("p", "empty", "No assessments yet. Add one, or upload a syllabus."));

  // calendar subscribe link
  try {
    const sub = await json("/api/calendar/subscribe");
    $("cal-link").href = sub.path;
    $("cal-link").title = sub.how;
  } catch {}
}

function weekLabel(iso) {
  const d = new Date(iso + "T00:00:00");
  const mon = new Date(d); mon.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return "Week of " + mon.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function arow(a, all) {
  const row = el("div", "arow" + (a.overdue ? " overdue" : "") + (a.status === "done" ? " done" : ""));
  row.append(el("div", "atype", a.kind_label));
  const title = el("div", "atitle");
  title.append(a.title);
  if (a.user_entered) title.append(el("span", "you", "you"));
  row.append(title);
  const due = el("div", "adue");
  due.append(a.due_date ? new Date(a.due_date + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "no date");
  if (a.weight) due.append(el("span", "aweight", `  ${Math.round(a.weight * 100)}%`));
  row.append(due);

  const sel = el("select", "status");
  for (const [v, t] of [["not_started", "not started"], ["in_progress", "in progress"], ["done", "done"]]) {
    const o = el("option", null, t); o.value = v; if (a.status === v) o.selected = true;
    sel.append(o);
  }
  sel.onchange = async () => {
    await json(`/api/assessments/${a.id}/status`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: sel.value }),
    });
    renderAssessments();
  };
  row.append(sel);
  return row;
}

// manual add
$("assess-add").addEventListener("click", () => {
  const f = $("assess-form");
  f.hidden = !f.hidden;
  if (f.hidden) return;
  f.textContent = "";
  const course = el("select"); course.id = "af-course";
  for (const c of COURSES) { const o = el("option", null, c.code); o.value = c.course_id; course.append(o); }
  const kind = el("select"); kind.id = "af-kind";
  for (const k of ["homework","problem_set","paper","exam","quiz","project","reading_response","presentation"])
    { const o = el("option", null, k.replace("_"," ")); o.value = k; kind.append(o); }
  const title = el("input"); title.id = "af-title"; title.placeholder = "Title"; title.className = "full";
  const due = el("input"); due.id = "af-due"; due.type = "date";
  const weight = el("input"); weight.id = "af-weight"; weight.type = "number"; weight.step = "0.05"; weight.placeholder = "weight 0-1";
  const save = el("button", "plain", "Save"); save.type = "button"; save.className = "full";
  save.onclick = async () => {
    await json("/api/assessments", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        course_id: course.value, kind: kind.value, title: title.value,
        due_date: due.value || null, weight: parseFloat(weight.value) || 0,
      }),
    });
    f.hidden = true;
    renderAssessments();
  };
  f.append(course, kind, title, due, weight, save);
});

/* ── courses, uploads, review ──────────────────────────────────────────── */

let courseDeletionPreview = [];
let courseDeletionScopes = new Map();
let courseDeletionSelected = new Set();

async function openCourseDeleteDialog() {
  const dialog = $("course-delete-dialog");
  $("course-delete-status").textContent = "Loading courses and deletion counts…";
  $("course-delete-list").textContent = "";
  $("course-delete-phrase").value = "";
  $("course-delete-all").checked = false;
  $("course-delete-confirm").disabled = true;
  $("course-delete-cache-note").textContent = "";
  if (!dialog.open) dialog.showModal();
  try {
    const preview = await json("/api/course-deletions/preview");
    courseDeletionPreview = preview.courses || [];
    courseDeletionScopes = new Map(courseDeletionPreview.map((course) => [course.course_id, "entire_course"]));
    courseDeletionSelected = new Set();
    $("course-delete-cache-note").textContent = preview.reupload_cache
      ? "Re-uploading a previously processed textbook is usually faster because its parsed content is cached by file hash."
      : "Re-uploading removed materials requires parsing and indexing them again. A large textbook may take several minutes.";
    renderCourseDeleteSelection();
    $("course-delete-status").textContent = courseDeletionPreview.length ? "" : "There are no courses to delete.";
  } catch (error) {
    $("course-delete-status").textContent = `Could not load deletion counts: ${error.message}`;
  }
}

function selectedDeletionCourses() {
  const selected = new Set(Array.from(document.querySelectorAll(".course-delete-check:checked"), (input) => input.value));
  return courseDeletionPreview.filter((course) => selected.has(course.course_id));
}

function appendDeletionCountLines(box, entries) {
  const labels = {
    files:"uploaded files", sources:"file records", chunks:"chunks and embeddings",
    chapters:"chapters", schedule_rows:"schedule rows", assessments:"assessments",
    calendar_events:"syllabus calendar events", grade_records:"grade records",
    practice_problems:"practice problems", generated_variants:"generated variants",
    attempts:"practice attempts", engagement_records:"engagement records",
    manual_progress:"manual progress records", conversations:"conversations",
    messages:"conversation messages", artifacts:"conversation artifacts",
    ingest_jobs:"upload jobs", agent_runs:"AI activity records", parents:"source text records",
    study_plans:"study plans", task_status:"completed-item flags",
  };
  for (const [key, value] of Object.entries(entries || {})) {
    if (!value) continue;
    box.append(el("p", "fine", `${value} ${labels[key] || key.replaceAll("_", " ")}`));
  }
  if (!Object.values(entries || {}).some(Boolean)) box.append(el("p", "fine", "Nothing in this scope is currently stored."));
}

function renderCourseDeleteSelection() {
  const list = $("course-delete-list"); list.textContent = "";
  for (const course of courseDeletionPreview) {
    const row = el("div", "course-delete-course");
    const head = el("div", "course-delete-course-head");
    const check = el("input"); check.type = "checkbox"; check.className = "course-delete-check";
    check.value = course.course_id; check.checked = courseDeletionSelected.has(course.course_id);
    check.setAttribute("aria-label", `Select ${course.code}`);
    const label = el("label", null, `${course.code} · ${course.title}`);
    label.htmlFor = `delete-course-${course.course_id}`; check.id = label.htmlFor;
    check.addEventListener("change", () => {
      if (check.checked) courseDeletionSelected.add(course.course_id);
      else courseDeletionSelected.delete(course.course_id);
      $("course-delete-all").checked = courseDeletionPreview.length > 0 &&
        courseDeletionSelected.size === courseDeletionPreview.length;
      renderCourseDeleteSelection();
    });
    const select = el("select"); select.className = "field";
    select.setAttribute("aria-label", `Deletion scope for ${course.code}`);
    const scopes = [["entire_course","Entire course"],["materials","Materials only"],
      ["syllabus","Syllabus and schedule only"],["assessment_uploads","Assessment uploads and practice"]];
    for (const [value, title] of scopes) { const option = el("option", null, title); option.value = value; select.append(option); }
    select.value = courseDeletionScopes.get(course.course_id) || "entire_course";
    select.addEventListener("change", () => { courseDeletionScopes.set(course.course_id, select.value); renderCourseDeleteSelection(); });
    head.append(check, label, select); row.append(head);
    const counts = el("div", "course-delete-counts");
    const scopeCounts = course.counts[select.value] || {};
    appendDeletionCountLines(counts, scopeCounts);
    row.append(counts); list.append(row);
  }
  $("course-delete-all").checked = courseDeletionPreview.length > 0 &&
    courseDeletionSelected.size === courseDeletionPreview.length;
  updateCourseDeleteConfirmation();
}

function deletionConfirmationPhrase(selected, selectAll) {
  if (selectAll && selected.length === courseDeletionPreview.length) return "DELETE ALL";
  if (selected.length === 1) return selected[0].code;
  return `DELETE ${selected.map((course) => course.code).join(", ")}`;
}

function updateCourseDeleteConfirmation() {
  const selected = selectedDeletionCourses();
  const selectAll = $("course-delete-all").checked;
  const phrase = deletionConfirmationPhrase(selected, selectAll);
  $("course-delete-confirmation").textContent = selected.length
    ? `To confirm ${selected.length} selected course${selected.length === 1 ? "" : "s"}, type: ${phrase}`
    : "Select one or more courses to continue.";
  $("course-delete-confirm").disabled = !selected.length ||
    $("course-delete-phrase").value.trim().toLocaleLowerCase() !== phrase.toLocaleLowerCase();
}

async function submitCourseDeletions() {
  const selectedCourses = selectedDeletionCourses();
  const selectAll = $("course-delete-all").checked;
  const phrase = deletionConfirmationPhrase(selectedCourses, selectAll);
  if (!selectedCourses.length || $("course-delete-phrase").value.trim().toLocaleLowerCase() !== phrase.toLocaleLowerCase()) return;
  const button = $("course-delete-confirm"); button.disabled = true; button.textContent = "Deleting…";
  $("course-delete-status").textContent = "Removing the selected records and files…";
  try {
    const result = await json("/api/course-deletions", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({selected:selectedCourses.map((course) => ({course_id:course.course_id,
        scope:courseDeletionScopes.get(course.course_id) || "entire_course"})),
        confirmation:phrase,select_all:selectAll})});
    await loadCourses();
    await openCourseDeleteDialog();
    if (result.failed?.length) {
      const course = courseDeletionPreview.find((row) => row.course_id === result.failed[0].course_id);
      const name = course?.code || result.failed[0].course_id;
      $("course-delete-status").textContent = result.deleted?.length
        ? `Earlier selected course data was deleted. The deletion for ${name} failed and that course's database changes were rolled back.`
        : `Deletion for ${name} failed. Its database changes were rolled back.`;
    } else {
      $("course-delete-status").textContent = "Selected course data was deleted.";
    }
    if (result.file_cleanup_pending) {
      $("course-delete-status").textContent += " One or more uploaded files could not be removed; contact support before re-uploading.";
    }
  } catch (error) {
    $("course-delete-status").textContent = `Deletion request failed: ${error.message}`;
  } finally { button.textContent = "Delete selected data"; updateCourseDeleteConfirmation(); }
}

$("newcourse").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = new FormData();
  body.append("code", $("nc-code").value.trim());
  body.append("title", $("nc-title").value.trim());
  await json("/api/courses", { method: "POST", body });
  $("nc-code").value = ""; $("nc-title").value = "";
  await loadCourses();
});

function renderCourseList() {
  const list = $("courselist");
  list.textContent = "";
  if (!COURSES.length) {
    list.append(el("p", "empty",
      "No courses yet. Create one above, then upload its syllabus and readings. " +
      "Nothing is preloaded — the tool knows only what you give it."));
    return;
  }
  for (const course of COURSES) {
    const row = el("div", "courserow");
    const head = el("div", "head2");
    head.append(el("span", "code", course.code));
    head.append(el("span", "name", course.title));
    head.append(el("span", "meta",
      `${course.chunks.toLocaleString()} passages · ${course.has_data_link ? "data link" : "no data link"}`));
    row.append(head);

    const actions = el("div", "stack");
    actions.style.marginTop = "10px";
    for (const [labelText, endpoint, multiple] of [
      ["Add materials", "materials", true], ["Add syllabus", "syllabus", false],
    ]) {
      const input = el("input");
      input.type = "file"; input.multiple = multiple; input.hidden = true;
      input.accept = ".pdf,.docx,.md,.txt";
      const button = el("button", "plain", labelText);
      button.type = "button";
      button.addEventListener("click", () => input.click());
      input.addEventListener("change", async () => {
        if (!input.files.length) return;
        button.textContent = "Working…"; button.disabled = true;
        const body = new FormData();
        for (const file of input.files) body.append(multiple ? "files" : "file", file);
        try {
          const result = await json(
            `/api/courses/${course.course_id}/${endpoint}`, { method: "POST", body });
          if (endpoint === "materials" && result.job_id) {
            // Background job: the user is not blocked. Poll and show real
            // per-stage progress until every document finishes.
            button.textContent = labelText; button.disabled = false;
            await pollIngestJob(row, result.job_id, course.course_id);
          } else {
            renderIngest(row, result);
            await loadCourses();
            if (endpoint === "syllabus") loadReview(course.course_id);
            button.textContent = labelText; button.disabled = false;
          }
        } catch (err) {
          const fail = el("div", "filestat failed");
          fail.append(el("span", "st", "failed"));
          fail.append(el("span", "name", String(err).slice(0, 200)));
          row.append(fail);
          button.textContent = labelText; button.disabled = false;
        }
      });
      actions.append(button, input);
    }
    row.append(actions);
    list.append(row);
  }
}

/* Per-file outcome, plainly. The API already reports which file failed and
   why; this surfaces it rather than collapsing it into one error. */
function renderIngest(row, result) {
  const box = el("div");
  box.style.marginTop = "12px";
  for (const file of result.files || []) {
    const line = el("div", `filestat ${file.status}`);
    line.append(el("span", "st", file.status));
    line.append(el("span", "name", file.filename));
    if (file.status === "ok") {
      line.append(el("span", "why",
        `${file.chunks} passages, ${file.chapters} chapters` +
        (file.ocr_pages ? `, ${file.ocr_pages} pages read by OCR` : "")));
    } else if (file.detail) {
      line.append(el("span", "why", file.detail));
    }
    box.append(line);
  }
  if (result.rows !== undefined) {
    const line = el("div", "filestat ok");
    line.append(el("span", "st", "syllabus"));
    line.append(el("span", "name",
      `${result.rows} rows · ${result.linked} linked to chapters · ${result.needs_review} need review`));
    box.append(line);
  }
  if (result.grade_extraction) {
    const extraction = result.grade_extraction;
    const line = el("div", `filestat ${extraction.status === "pending_review" ? "ok" : "failed"}`);
    line.append(el("span", "st", "grade rules"));
    line.append(el("span", "name", extraction.status === "pending_review"
      ? `${extraction.component_count} categories extracted · open Grade Predictor to review and confirm`
      : extraction.message || "Grading structure could not be extracted"));
    box.append(line);
  }
  row.append(box);
}

/* Background ingest: poll the job and show real per-stage progress -- "OCR, page
   340 of 770" -- not an indeterminate spinner. Ask enables per course as each
   document finishes (loadCourses runs on every done/failed transition). */
const STAGE_LABEL = {
  queued: "queued", reading: "reading file", extracting: "extracting text",
  ocr_warn: "mostly scanned — this will take a while", ocr: "running OCR",
  chunking: "splitting into passages", embedding: "embedding", indexing: "saving",
  done: "done", failed: "failed", reused: "unchanged (reused)",
};

async function pollIngestJob(row, jobId, courseId) {
  let box = row.querySelector(".ingestjob");
  if (!box) { box = el("div", "ingestjob"); box.style.marginTop = "12px"; row.append(box); }
  const seenDone = new Set();
  for (;;) {
    let job;
    try { job = await json(`/api/jobs/${jobId}`); }
    catch { break; }
    renderJobProgress(box, job);
    // Enable Ask as soon as any document finishes, not only at the very end.
    for (const [i, f] of (job.files || []).entries()) {
      if ((f.stage === "done" || f.stage === "reused") && !seenDone.has(i)) {
        seenDone.add(i); loadCourses();
      }
    }
    if (job.status === "done" || job.status === "failed") { loadCourses(); break; }
    await new Promise((r) => setTimeout(r, 900));
  }
}

function renderJobProgress(box, job) {
  box.textContent = "";
  for (const f of job.files || []) {
    const done = f.stage === "done" || f.stage === "reused";
    const failed = f.stage === "failed";
    const line = el("div", `filestat ${done ? "ok" : failed ? "failed" : "working"}`);
    line.append(el("span", "st", done ? "ok" : failed ? "failed" : STAGE_LABEL[f.stage] || f.stage));
    line.append(el("span", "name", f.filename));

    if (!done && !failed && f.pages_total > 0 &&
        (f.stage === "extracting" || f.stage === "ocr" || f.stage === "embedding")) {
      const unit = f.stage === "embedding" ? "passage" : "page";
      line.append(el("span", "why", `${unit} ${f.pages_done} of ${f.pages_total}`));
      const bar = el("div", "pbar");
      const fill = el("div", "pfill");
      fill.style.width = `${Math.round(100 * f.pages_done / f.pages_total)}%`;
      bar.append(fill); line.append(bar);
    } else if (done && f.chunks) {
      line.append(el("span", "why", `${f.chunks} passages` + (f.detail ? ` · ${f.detail}` : "")));
    } else if (f.detail) {
      line.append(el("span", "why", f.detail));
    } else if (!done && !failed) {
      line.append(el("span", "why", STAGE_LABEL[f.stage] || f.stage));
    }
    box.append(line);
  }
}

let reviewCourse = null;

async function loadFirstPendingReview() {
  $("review-section").hidden = true;
  for (const course of COURSES) {
    try {
      const data = await json(`/api/courses/${course.course_id}/review`);
      if (data.flagged > 0) { await loadReview(course.course_id); return; }
    } catch { /* no schedule yet */ }
  }
}

async function loadReview(courseId) {
  reviewCourse = courseId;
  let data;
  try { data = await json(`/api/courses/${courseId}/review`); } catch { return; }

  $("review-section").hidden = data.flagged === 0;
  $("review-summary").textContent =
    `${data.flagged} of ${data.total} rows need a look. Rows both extraction passes ` +
    `agreed on, and that linked confidently to a chapter, are not shown.`;

  const box = $("reviewrows");
  box.textContent = "";
  for (const row of data.rows) {
    const wrap = el("div", "reviewrow");
    wrap.dataset.rowId = row.id;

    const when = el("div");
    when.append(el("div", "when", row.date));
    when.append(el("div", "kind", row.kind));
    wrap.append(when);

    const middle = el("div");
    const topic = el("input", "field");
    topic.type = "text"; topic.value = row.topic; topic.dataset.field = "topic";
    middle.append(topic);
    middle.append(el("div", "why", row.link_score > 0
      ? `matched “${row.link_title || "—"}” at ${row.link_score} (${row.link_method})`
      : `extraction confidence: ${row.confidence}`));
    wrap.append(middle);

    const select = el("select");
    select.dataset.field = "chapter";
    const none = el("option", null, "leave unlinked");
    none.value = "";
    select.append(none);
    for (const chapter of data.chapters) {
      const opt = el("option", null, `${chapter.chapter_num} · ${chapter.title}`);
      opt.value = chapter.chapter_ref;
      if (row.chapter_refs.includes(chapter.chapter_ref)) opt.selected = true;
      select.append(opt);
    }
    wrap.append(select);
    box.append(wrap);
  }
}

$("review-save").addEventListener("click", async () => {
  if (!reviewCourse) return;
  const fixes = [...document.querySelectorAll(".reviewrow")].map((row) => ({
    row_id: row.dataset.rowId,
    topic: row.querySelector('[data-field="topic"]').value,
    chapter_refs: [row.querySelector('[data-field="chapter"]').value].filter(Boolean),
    resolved: true,
  }));
  const result = await json(`/api/courses/${reviewCourse}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fixes),
  });
  $("review-summary").textContent =
    `Saved ${result.applied} corrections. ${result.remaining} rows still flagged.`;
  if (!result.remaining) $("review-section").hidden = true;
});

/* ── practice: isolated assignments, deliberate help, verified test sets ─ */

let practiceMode = "practice";
let practiceItems = [];
let practiceIndex = 0;
let practiceHelpLevel = 0;
let practiceMarked = false;
let testItems = [];

$("practice-course").addEventListener("change", loadPractice);
for (const button of document.querySelectorAll("[data-mode]")) {
  button.addEventListener("click", () => {
    practiceMode = button.dataset.mode;
    document.querySelectorAll("[data-mode]").forEach((b) =>
      b.setAttribute("aria-selected", String(b === button)));
    loadPractice();
  });
}

$("practice-files").addEventListener("change", uploadPracticeFiles);

async function loadPractice() {
  const select = $("practice-course");
  const previous = select.value;
  select.textContent = "";
  for (const c of COURSES) {
    const option = el("option", null, c.code);
    option.value = c.course_id;
    select.append(option);
  }
  if (COURSES.length) select.value = COURSES.some((c) => c.course_id === previous)
    ? previous : COURSES[0].course_id;
  const course = select.value;
  const body = $("practice-body");
  body.textContent = "";
  if (!course) {
    $("practice-note").textContent = "Create a course before uploading assignments.";
    return;
  }
  if (practiceMode === "test") {
    await loadPracticeTest(course);
    return;
  }
  try {
    const data = await json(`/api/practice/problems?course=${encodeURIComponent(course)}`);
    practiceItems = data.problems || [];
    const missed = practiceItems.filter((p) => (p.attempts || []).some((a) => !a.correct)).length;
    $("practice-note").textContent = `${practiceItems.length} problems · ${missed} missed problems surfaced first. ` +
      "Assignments are stored separately from textbook search.";
    if (!practiceItems.length) {
      body.append(el("p", "empty", "No assignment problems yet. Upload a worksheet, homework set, or past exam above."));
      return;
    }
    practiceIndex = Math.min(practiceIndex, practiceItems.length - 1);
    renderPracticeProblem();
  } catch (err) {
    $("practice-note").textContent = `Could not load practice problems: ${err}`;
  }
}

function renderPracticeProblem() {
  const body = $("practice-body");
  body.textContent = "";
  practiceHelpLevel = 0;
  practiceMarked = false;
  const problem = practiceItems[practiceIndex];
  if (!problem) return;

  const card = el("article", "practice-card");
  const meta = el("div", "practice-meta");
  meta.append(el("span", "pill", problem.origin === "uploaded" ? "Uploaded assignment" : "Generated variant"));
  if (problem.number) meta.append(el("span", "pill", `Problem ${problem.number}`));
  if (problem.verified) meta.append(el("span", "pill verified", "Sympy verified"));
  else if (problem.origin === "generated") meta.append(el("span", "pill unverified", "Unverified"));
  if (problem.needs_review) meta.append(el("span", "pill unverified", "Check this parse"));
  card.append(meta);
  card.append(el("p", "practice-topic", problem.topic || problem.type.replaceAll("_", " ")));
  card.append(el("p", "practice-prompt", problem.prompt));
  addReadAloud(card, `Problem ${problem.number || practiceIndex + 1}. ${problem.prompt}`,
    "Hear problem", voicePrefs.voice_only_mode);
  if (problem.chapter_ref) card.append(el("p", "note", `Linked to ${chapterName(problem.chapter_ref)}.`));
  else card.append(el("p", "note",
    "No confident chapter link yet. You can still practice this problem; it will not change chapter readiness."));
  if (problem.needs_review) card.append(el("p", "warning", "This problem was extracted with low confidence. Review the uploaded assignment before relying on it."));
  if (problem.origin === "generated" && !problem.verified)
    card.append(el("p", "warning", "This variant has not been independently checked. Do not treat its answer as verified."));

  const response = el("textarea", "field practice-response");
  response.rows = 3;
  response.placeholder = "Work it out here, or on paper. Your answer stays on this device until you submit it.";
  response.setAttribute("aria-label", "Your answer");
  card.append(response);

  const controls = el("div", "practice-controls");
  const tryButton = el("button", "plain", "I tried it");
  tryButton.type = "button";
  tryButton.onclick = () => {
    if (practiceMarked) return;
    const judgment = el("div", "practice-judgment");
    judgment.append(el("span", "note", "How did it go?"));
    for (const [label, correct] of [["I solved it", true], ["Not yet", false]]) {
      const mark = el("button", "plain", label);
      mark.type = "button";
      mark.onclick = async () => {
        try {
          await json("/api/practice/attempt", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ problem_id: problem.id, correct, help_level: practiceHelpLevel }),
          });
          practiceMarked = true;
          const feedback = correct ? "Attempt saved as correct." : "Saved. This problem will appear early next time.";
          judgment.textContent = feedback;
          addReadAloud(judgment, feedback, "Hear feedback", voicePrefs.voice_only_mode);
          loadDashboardIfVisible();
          await loadPractice();
        } catch (err) { judgment.textContent = `Could not save attempt: ${err}`; }
      };
      judgment.append(mark);
    }
    controls.append(judgment);
    tryButton.disabled = true;
  };
  controls.append(tryButton);

  const helpBox = el("div", "practice-help");
  const helpButton = el("button", "plain", "Show hint");
  helpButton.type = "button";
  helpButton.onclick = async () => {
    const next = practiceHelpLevel + 1;
    const max = Number(problem.max_help_level || (problem.origin === "uploaded" ? 2 : 3));
    if (next > max) return;
    helpButton.disabled = true;
    try {
      const help = await json("/api/practice/help", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ problem_id: problem.id, level: next }),
      });
      practiceHelpLevel = next;
      const section = el("section", "help-level");
      section.append(el("h3", "label", `Level ${next} · ${help.kind}`));
      section.append(el("p", "practice-prompt", help.text));
      if (help.note) section.append(el("p", "note", help.note));
      addReadAloud(section, `${help.kind}. ${help.text}${help.note ? ` ${help.note}` : ""}`,
        `Hear ${help.kind}`, voicePrefs.voice_only_mode);
      helpBox.append(section);
      renderMath(section);
      if (next < max) helpButton.textContent = next === 1 ? "Show method" : "Show full solution";
      else helpButton.hidden = true;
      if (help.blocked) helpButton.hidden = true;
    } catch (err) {
      helpBox.append(el("p", "warning", `Could not load help: ${err}`));
    } finally { helpButton.disabled = false; }
  };
  controls.append(helpButton);
  card.append(controls, helpBox);

  const nav = el("div", "practice-controls");
  nav.append(el("span", "note", `Problem ${practiceIndex + 1} of ${practiceItems.length}`));
  const next = el("button", "plain", practiceIndex + 1 < practiceItems.length ? "Next problem →" : "Back to first problem");
  next.type = "button";
  next.onclick = () => {
    practiceIndex = (practiceIndex + 1) % practiceItems.length;
    renderPracticeProblem();
  };
  nav.append(next);
  card.append(nav);
  body.append(card);
  renderMath(card);
}

function chapterName(ref) {
  const parts = ref.split(":");
  return /^\d+$/.test(parts.at(-1)) ? `Chapter ${parts.at(-1)}` : "the linked chapter";
}

async function loadPracticeTest(course) {
  const body = $("practice-body");
  try {
    const data = await json(`/api/practice/test?course=${encodeURIComponent(course)}&n=8`);
    testItems = data.problems || [];
    const instructions = data.weighted_by_exam
      ? "Test set is weighted toward chapters on your next exam. No hints during the test."
      : "Test set uses verified generated variants. No hints during the test.";
    $("practice-note").textContent = instructions;
    addReadAloud($("practice-note"), instructions, "Hear test instructions", voicePrefs.voice_only_mode);
    if (!testItems.length) {
      body.append(el("p", "empty", "No verified generated variants yet. Upload an assignment in Practice mode first."));
      return;
    }
    const form = el("form", "test-set");
    const fields = new Map();
    testItems.forEach((problem, index) => {
      const card = el("section", "practice-card");
      card.append(el("div", "practice-meta", `Question ${index + 1} · ${chapterName(problem.chapter_ref)}`));
      card.append(el("p", "practice-prompt", problem.prompt));
      addReadAloud(card, `Question ${index + 1}. ${problem.prompt}`, "Hear question");
      const input = el("textarea", "field practice-response");
      input.rows = 2;
      input.required = true;
      input.placeholder = "Use answer labels if shown, e.g. x1 = 25, x2 = 8";
      input.setAttribute("aria-label", `Answer for question ${index + 1}`);
      fields.set(problem.id, input);
      card.append(input);
      form.append(card);
    });
    const finish = el("button", "plain", "Finish test and score");
    finish.type = "submit";
    form.append(finish);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      finish.disabled = true;
      try {
        const answers = Object.fromEntries([...fields].map(([id, input]) => [id, input.value]));
        const result = await json("/api/practice/test/grade", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answers }),
        });
        renderTestReview(result);
        loadDashboardIfVisible();
      } catch (err) {
        body.prepend(el("p", "warning", `Could not score the test: ${err}`));
        finish.disabled = false;
      }
    });
    body.append(form);
    renderMath(form);
  } catch (err) { body.append(el("p", "warning", `Could not build a test set: ${err}`)); }
}

function renderTestReview(result) {
  const body = $("practice-body");
  body.textContent = "";
  body.append(el("h3", "practice-score", `Your score: ${result.score}/${result.total} · ${result.percent}%`));
  addReadAloud(body, `Your score is ${result.score} out of ${result.total}, or ${result.percent} percent.`,
    "Hear score", voicePrefs.voice_only_mode);
  body.append(el("p", "note", "Review each problem and its checked answer. Practice attempts count twice as much as an Ask question in chapter engagement."));
  for (const item of result.results || []) {
    const card = el("article", "practice-card");
    card.append(el("div", `practice-meta ${item.correct ? "verified" : "unverified"}`,
      item.correct ? "Correct" : "Review this one"));
    card.append(el("p", "practice-prompt", item.prompt));
    card.append(el("p", "answer-key", `Checked answer: ${item.answer}`));
    if (item.solution_steps) card.append(el("p", "note", item.solution_steps));
    addReadAloud(card,
      `${item.correct ? "Correct." : "Review this one."} The checked answer is ${item.answer}.${item.solution_steps ? ` ${item.solution_steps}` : ""}`,
      "Hear feedback");
    body.append(card);
    renderMath(card);
  }
  const again = el("button", "plain", "Build another test");
  again.type = "button";
  again.onclick = loadPractice;
  body.append(again);
}

async function uploadPracticeFiles() {
  const input = $("practice-files");
  const course = $("practice-course").value;
  const files = [...input.files];
  input.value = "";
  if (!files.length) return;
  if (!course) { $("practice-upload-status").textContent = "Choose a course first."; return; }
  const status = $("practice-upload-status");
  status.textContent = "Uploading assignment files…";
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  try {
    const started = await json(`/api/courses/${encodeURIComponent(course)}/assessments`,
      { method: "POST", body: form });
    let job;
    do {
      await new Promise((resolve) => setTimeout(resolve, 800));
      job = await json(`/api/jobs/${started.job_id}`);
      status.textContent = (job.files || []).map((f) =>
        `${f.filename}: ${f.stage}${f.detail ? ` · ${f.detail}` : ""}`).join("\n");
    } while (!["done", "failed"].includes(job.status));
    await loadPractice();
  } catch (err) { status.textContent = `Upload failed: ${err}`; }
}

function loadDashboardIfVisible() {
  if (!$("view-dash").hidden) loadDashboard();
}

boot();

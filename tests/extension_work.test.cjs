const test = require("node:test");
const assert = require("node:assert/strict");
const { mapLimit, createSemaphore, queryRelevance, matchCourse } = require("../extension/lib/work.js");

test("mapLimit preserves result order and never exceeds its worker bound", async () => {
  let active = 0;
  let peak = 0;
  const result = await mapLimit([0, 1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 2));
    active -= 1;
    return value * 2;
  });
  assert.deepEqual(result, [0, 2, 4, 6, 8, 10]);
  assert.equal(peak, 2);
});

test("createSemaphore bounds independent parser work across callers", async () => {
  const withSlot = createSemaphore(2);
  let active = 0;
  let peak = 0;
  await Promise.all(Array.from({ length: 7 }, (_, index) => withSlot(async () => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, index % 2 ? 3 : 1));
    active -= 1;
  })));
  assert.equal(peak, 2);
  assert.equal(active, 0);
});

test("matchCourse requires a unique course and respects the term", () => {
  const courses = [
    { course_id: "a", code: "ECON 304", title: "Macro", term: "Fall 2026" },
    { course_id: "b", code: "ECON 304", title: "Macro", term: "Spring 2026" },
  ];
  assert.equal(matchCourse({ courseCode: "econ-304", termName: "Fall 2026" }, courses), courses[0]);
  assert.equal(matchCourse({ courseCode: "ECON 304" }, courses), null);
  assert.equal(matchCourse({ displayName: "Macro", termName: "Fall 2026" }, courses), courses[0]);
});

test("queryRelevance ranks materials whose titles match the question", () => {
  assert.ok(queryRelevance("Explain consumer choice", "Chapter 4: Consumer Choice") >
    queryRelevance("Explain consumer choice", "Lecture 2: Market Equilibrium"));
});

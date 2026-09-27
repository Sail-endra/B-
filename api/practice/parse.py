"""Parse an uploaded assignment into structured problems.

Same discipline as syllabus extraction: the model returns structured problems
with a confidence, and a low-confidence parse is flagged for review rather than
guessed at. Types match what a student actually uploads: numeric, symbolic
derivation, proof, short answer, multiple choice, code, essay prompt.

The provided solution is captured when the file includes one -- it is the best
signal for what a correct answer looks like, and it is what a generated variant's
worked solution should resemble.
"""

from __future__ import annotations

from typing import Any

PROBLEM_TYPES = (
    "numeric", "symbolic", "symbolic_derivation", "proof", "short_answer",
    "multiple_choice", "code", "essay", "essay_prompt",
)

_TYPE_ALIASES = {
    "symbolic derivation": "symbolic_derivation",
    "essay prompt": "essay_prompt",
    "short answer": "short_answer",
    "multiple choice": "multiple_choice",
}

_PROMPT = """You are extracting individual problems from a student's uploaded
assignment (a problem set, worksheet, or past exam). Return every distinct
problem or sub-problem as its own object.

For each problem give:
- "number": its label as printed ("3", "3(b)", "II.4"), or "" if unlabelled.
- "prompt": the full problem statement, verbatim enough to solve. Include given
  values and what is asked.
- "type": one of numeric, symbolic_derivation, proof, short_answer,
  multiple_choice, code, essay_prompt.
- "topic": the method it tests, in a few words ("utility maximization",
  "price elasticity", "proof by induction").
- "difficulty": easy, standard, or hard.
- "given_solution": the provided answer/solution if the file includes one for
  this problem, else "".
- "confidence": 0.0-1.0, how sure you are this is a real, complete problem (low
  for fragments, headers, or garbled OCR text).

Return JSON only: {{"problems": [ ... ]}}. If the text contains no problems
(it is prose, a syllabus, or notes), return {{"problems": []}}.

ASSIGNMENT TEXT:
{text}"""

# Below this the parse is flagged for review rather than trusted.
_CONFIDENCE_FLOOR = 0.55


def parse_problems(text: str, llm: Any, max_chars: int = 14000) -> list[dict]:
    """Return a list of parsed problem dicts. Empty when no model is available
    (parsing is inherently generative) or nothing parses."""
    if not getattr(llm, "available", False) or not text.strip():
        return []
    try:
        data = llm.json(_PROMPT.format(text=text[:max_chars]), max_tokens=3000)
    except Exception:  # noqa: BLE001 - a parse failure must not crash ingest
        return []
    out: list[dict] = []
    for raw in data.get("problems", []) or []:
        prompt = str(raw.get("prompt", "")).strip()
        if not prompt:
            continue
        ptype = str(raw.get("type", "short_answer")).strip().lower()
        ptype = _TYPE_ALIASES.get(ptype, ptype.replace("-", "_"))
        if ptype not in PROBLEM_TYPES:
            ptype = "short_answer"
        try:
            conf = float(raw.get("confidence", 0.5))
        except Exception:  # noqa: BLE001
            conf = 0.5
        out.append({
            "number": str(raw.get("number", "")).strip()[:12],
            "prompt": prompt,
            "type": ptype,
            "topic": str(raw.get("topic", "")).strip()[:80],
            "difficulty": str(raw.get("difficulty", "standard")).strip().lower(),
            "given_solution": str(raw.get("given_solution", "")).strip(),
            "confidence": conf,
            "needs_review": conf < _CONFIDENCE_FLOOR,
        })
    return out

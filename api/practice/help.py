"""Escalating help for a practice problem, with the integrity line enforced here.

Three levels, each a separate deliberate request:
  1 hint     -- points at the concept, cited to the chapter. No steps.
  2 method   -- the steps, without the arithmetic / final number.
  3 solution -- the full worked solution WITH the final answer.

The integrity line is structural, not a prompt instruction: `solution` (level 3)
is unreachable for a problem whose `origin` is 'uploaded'. Working an assigned
problem is not studying; working a generated variant is. A generated variant's
answer is the sympy-verified one, so its solution is trustworthy.
"""

from __future__ import annotations

import re
from typing import Any

from ..math_text import canonicalize_math

HINT, METHOD, SOLUTION = 1, 2, 3


def max_level(origin: str) -> int:
    """Uploaded originals top out at the method; only generated variants reach the
    full solution."""
    return METHOD if origin == "uploaded" else SOLUTION


def _chapter_label(chapter_ref: str) -> str:
    # course:source:chapter -> "Chapter N"
    parts = (chapter_ref or "").split(":")
    return f"Chapter {parts[-1]}" if parts and parts[-1].isdigit() else "the relevant chapter"


_HINT_PROMPT = """A student is stuck on this problem and wants only a HINT -- one
or two sentences that point at the METHOD or concept to use, and why. Do NOT give
any step, formula result, or number that solves it. End by citing the chapter to
review, like "(see {chapter})".

Problem: {prompt}
Topic: {topic}"""

_METHOD_PROMPT = """Give the METHOD for this problem: the ordered steps to solve
it, WITHOUT substituting or evaluating the problem's numeric values, WITHOUT
doing arithmetic, and WITHOUT stating the final answer or whether the claim is
true. Use symbolic variables and steps only. The student will do the computation.

Problem: {prompt}
Topic: {topic}"""


def help_for(problem, level: int, llm: Any) -> dict[str, Any]:
    """Return {level, kind, text, blocked}. Enforces the integrity gate."""
    def math_result(result: dict[str, Any]) -> dict[str, Any]:
        for key in ("text", "note"):
            if key in result:
                result[key] = canonicalize_math(result[key])
        return result

    origin = problem.origin
    if level > max_level(origin):
        return math_result({
            "level": level, "blocked": True,
            "kind": "solution",
            "text": ("This is a problem from your own assignment, so Course Copilot "
                     "gives hints and method only — never the final answer. Working "
                     "the assigned problem yourself is the point. Try a generated "
                     "variant to see a full worked solution."),
        })

    chapter = _chapter_label(problem.chapter_ref)

    # Generated variants carry a checked answer and full solution. Level 2 still
    # gets a method-only response; the checked arithmetic is reserved for the
    # student's deliberate level-3 request.
    if origin == "generated":
        if level == SOLUTION:
            steps = problem.solution_steps or "(worked steps)"
            ans = f"\n\nAnswer: {problem.answer}" if problem.answer else ""
            verified = " ✓ verified by sympy" if problem.verified else " (unverified)"
            citation = f"\n\nReview {chapter}."
            return math_result({"level": level, "blocked": False, "kind": "solution",
                    "text": steps + ans + citation, "verified": problem.verified,
                    "cited": chapter, "note": verified})
    # Otherwise generate hint / method on demand (grounded to the chapter).
    if not getattr(llm, "available", False):
        if level == HINT:
            text = f"Review {chapter} on {problem.topic or 'this topic'}, then try again."
        else:
            text = (f"For {problem.topic or 'this problem'}, identify the relevant "
                    "relationship, write it using the problem's variables, and apply "
                    "the condition asked about. Keep the expression symbolic; do not "
                    "substitute values yet.")
        return math_result({"level": level, "blocked": False,
                "kind": "hint" if level == HINT else "method",
                "text": text, "cited": chapter, "degraded": True})
    tmpl = _HINT_PROMPT if level == HINT else _METHOD_PROMPT
    try:
        text = llm.text(tmpl.format(prompt=problem.prompt[:1200], topic=problem.topic,
                                    chapter=chapter), max_tokens=300).strip()
        if level == METHOD:
            text = _method_without_arithmetic(text)
    except Exception:  # noqa: BLE001
        text = f"Review {chapter} on {problem.topic or 'this topic'}."
    # Do not trust a model-generated chapter number: append the actual linked
    # chapter from the stored problem, after removing any conflicting citation.
    text = re.sub(r"\s*\(see\s+Chapter\s+\d+\)\s*", " ", text, flags=re.I).strip()
    text = (text + f" (see {chapter})").strip()
    return math_result({"level": level, "blocked": False,
            "kind": "hint" if level == HINT else "method", "text": text,
            "cited": chapter})


def _method_without_arithmetic(text: str) -> str:
    """Drop accidental worked calculations or conclusions from level-2 prose."""
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if re.search(r"\d+(?:\.\d+)?\s*(?:\*|×|\/|\+|−|-)\s*\d+|=\s*-?\d", sentence):
            continue
        if re.search(r"\b(?:the answer is|conclude that|therefore.*\b(?:true|false|equal))\b",
                     sentence, flags=re.I):
            continue
        if sentence.strip():
            kept.append(sentence.strip())
    return " ".join(kept)

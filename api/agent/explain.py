"""Explanation layer: turn retrieved passages into an explanation, not a quote.

Two depths, both cited, both bounded to the passages:

  concise   -- 3-5 sentences: what the concept is and why it matters.
  in_depth  -- concept, mechanism, the notation as the textbook states it, and a
               worked example only if one appears in the passages.

Hard rules enforced by the prompt AND by the fallback:
  * The verbatim passages stay visible; this is a layer on top, never a
    replacement. The caller still renders the passages and the sidenotes point at
    the real text.
  * No fact, number, definition or example that is not in the retrieved passages.
    In-depth means more thorough, not more outside knowledge.
  * If the passages cannot support a fuller explanation, say so and give concise.
  * Runs only after retrieval succeeds. With no model, the caller keeps the
    existing extractive answer; explanation is additive, never a precondition.
"""

from __future__ import annotations

import re
from typing import Sequence

Depth = str  # "concise" | "in_depth"

_RULES = (
    "Rules you must follow exactly:\n"
    "- Use ONLY the numbered passages below. Do not add any fact, number, "
    "definition, or example that is not in them.\n"
    "- After every claim, cite the passage it came from with its bracket label, "
    "e.g. [ECON303, Ch 2, p. 58], copied verbatim from the passage header. Put "
    "each citation in its OWN brackets; never combine two page references in one "
    "bracket like [ECON303, Ch 2, p. 54, ECON303, Ch 2, p. 56].\n"
    "- If the passages do not support a fuller explanation, say so briefly and "
    "give the concise version instead of padding.\n"
    "- Plain language. Do not invent notation; use the textbook's if it appears.\n"
    "- Preserve mathematics exactly, using standard TeX inside \\( ... \\) for inline math "
    "and \\[ ... \\] for displayed equations. Use braces for subscripts, superscripts, "
    "fractions, and roots (for example \\(p_{1}x_{1}+p_{2}x_{2}\\le m\\)). Never leave "
    "a TeX command outside math delimiters.\n"
)

_CONCISE = (
    "Explain the concept the student asked about in THREE to FIVE sentences: what "
    "it is and why it matters. Every claim cited.\n\n" + _RULES
)

_IN_DEPTH = (
    "Give a fuller walkthrough: the concept, the mechanism behind it, the notation "
    "exactly as the passages state it, and a worked example ONLY if one appears in "
    "the passages. Every claim cited. Do not exceed what the passages support.\n\n"
    + _RULES
)

SYSTEM = (
    "You are a study assistant that explains a student's own course materials. You "
    "never introduce outside facts; you make the passages clearer, and you cite "
    "every claim to the passage it came from."
)

_CITATION_BLOCK = re.compile(r"\[([^\]]+)\]")
_CITATION_LABEL = re.compile(
    r"([A-Za-z0-9_-]{2,20},\s*(?:Ch\s*\d+,\s*)?pp?\.\s*\d+(?:[–-]\d+)?)"
)


def normalize_citations(text: str) -> str:
    """Split model-combined page references into the UI's atomic citation form.

    Some providers ignore the instruction to put each page reference in its own
    bracket. The reading UI can then fail to attach the reference to a margin
    note, so normalize only bracket groups that contain multiple valid labels.
    """
    def split(match: re.Match[str]) -> str:
        labels = _CITATION_LABEL.findall(match.group(1))
        return " ".join(f"[{label}]" for label in labels) if len(labels) > 1 else match.group(0)

    return _CITATION_BLOCK.sub(split, text)


def build_prompt(question: str, passages: Sequence[dict], depth: Depth) -> str:
    """passages: dicts with 'citation' (label) and 'snippet'/'text'."""
    instruction = _IN_DEPTH if depth == "in_depth" else _CONCISE
    blocks = []
    for i, p in enumerate(passages, 1):
        label = p.get("citation") or p.get("label") or ""
        body = p.get("snippet") or p.get("text") or ""
        blocks.append(f"[Passage {i}] header {label}\n{body}")
    joined = "\n\n".join(blocks)
    return (
        f"{instruction}\n\nStudent's question: {question}\n\n"
        f"Passages (the only source you may use):\n\n{joined}"
    )


def explain(question: str, passages: Sequence[dict], depth: Depth, llm) -> tuple[str, bool]:
    """Return (explanation_text, generated).

    generated is False when no model is available -- the caller then keeps the
    extractive answer. This function never fabricates: with no model it returns
    ("", False) and the existing grounded-extractive path stands.
    """
    if not getattr(llm, "available", False):
        return "", False
    prompt = build_prompt(question, passages, depth)
    max_tokens = 900 if depth == "in_depth" else 400
    try:
        text = llm.text(prompt, system=SYSTEM, max_tokens=max_tokens)
    except Exception:  # noqa: BLE001 - explanation is additive; never break the answer
        return "", False
    text = normalize_citations(text.strip())
    return text, bool(text)

"""Question-intent gating.

Calibrating refusal on relevance alone has a ceiling, and the eval harness found
it: a cluster of out-of-corpus questions score as high as genuine ones because
their *topic* is squarely in the corpus even though their *answer* is not.

    "Who is the current chair of the Federal Reserve?"   0.335
    "Translate the Phillips curve equation into French."  0.319
    "Write the solution to problem set 4 question 3."     0.311

A retriever cannot separate these, and it is not a threshold problem: the
passage it returns really is about the Federal Reserve. What differs is what the
question asks the system to *do* with the corpus. So intent is scored separately
and composed with relevance, rather than pushing the threshold up and refusing
genuine questions to catch them.

Four outcomes:

  ANSWERABLE      -- normal retrieval question
  NEEDS_LIVE_DATA -- asks for a current value. The corpus is static and there are
                     no data tools, so this refuses; kept as a distinct outcome
                     because the *reason* shown to the student differs
  OUT_OF_SCOPE    -- asks for something the materials cannot supply at all
                     (a translation, a proof, a grade, someone's phone number)
  QUANTITATIVE_REQUEST
                  -- asks for a trend, correlation or share. Refused for a course
                     with no data link; allowed through for one that has it, where
                     the data tools can actually answer

Keyword gates rather than a model call: they are cheap, deterministic, and
inspectable, and every pattern here is justified by a question in the eval set.
Precision matters far more than coverage -- a false OUT_OF_SCOPE refuses a real
question, so the patterns are deliberately narrow.
"""

from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    ANSWERABLE = "answerable"
    NEEDS_LIVE_DATA = "needs_live_data"
    OUT_OF_SCOPE = "out_of_scope"
    # Asks for a statistic, trend or relationship over data. Answerable only for
    # a course whose corpus earned a data link; for everything else the materials
    # simply do not contain a series to compute over. Resolved by the pipeline,
    # which is the layer that knows the course's capability.
    QUANTITATIVE_REQUEST = "quantitative_request"


# Asks for a value as of now. The corpus is static, so only a data feed can serve
# these -- and only for a course that has one.
_TEMPORAL = re.compile(
    r"\b(current|currently|today|todays|right now|at present|latest|most recent|"
    r"this (?:week|month|year)|last (?:week|month|quarter|year))\b",
    re.I,
)

# Asks the system to perform a task rather than report what the materials say.
_TASK_NOT_LOOKUP = re.compile(
    r"\b(translate|prove that|write (?:my|the solution|an essay)|solve problem set|"
    r"do my homework|recommend a|what should i buy|install)\b",
    re.I,
)

# Asks about the student's own record or course administration, which the
# materials do not contain.
_ADMINISTRATIVE = re.compile(
    r"\b(my grade|what grade did i|office (?:phone|number|hours)|"
    r"will be on the (?:final|exam)|professor'?s? (?:phone|email|number))\b",
    re.I,
)

# A named person holding an office now -- distinct from a concept in the text.
_WHO_IS_NOW = re.compile(r"\bwho is the (?:current|present)\b", re.I)

# Asks for a number computed over a series: a trend, a correlation, a share.
# Matched narrowly -- "the correlation between X and Y" is quantitative, while
# "correlation does not imply causation" is a concept a textbook may well cover.
_QUANTITATIVE = re.compile(
    r"\b(?:the\s+)?(?:trend|trends)\s+in\b"
    r"|\bcorrelation\s+between\b"
    r"|\brate\s+of\s+(?:growth|change)\s+(?:of|in)\b"
    r"|\bgrowth\s+rate\s+of\b"
    r"|\bwhat\s+percentage\s+of\b"
    r"|\bstatistically\s+significant\b"
    r"|\b(?:plot|chart|graph)\s+(?:the|a)\b"
    r"|\bshow\s+me\s+the\s+(?:trend|change|growth|distribution)\b",
    re.I,
)


def classify(question: str) -> tuple[Intent, str]:
    """Return the intent and a short human-readable reason."""
    text = question or ""

    if (m := _ADMINISTRATIVE.search(text)) is not None:
        return Intent.OUT_OF_SCOPE, (
            f"asks about course administration or personal records ({m.group(0)!r}), "
            "which the course materials do not contain"
        )
    if (m := _TASK_NOT_LOOKUP.search(text)) is not None:
        return Intent.OUT_OF_SCOPE, (
            f"asks the system to perform a task ({m.group(0)!r}) rather than report "
            "what the materials say"
        )
    if _WHO_IS_NOW.search(text) is not None:
        return Intent.OUT_OF_SCOPE, (
            "asks who currently holds an office, which is a fact about the present "
            "rather than about the course materials"
        )
    if (m := _TEMPORAL.search(text)) is not None:
        return Intent.NEEDS_LIVE_DATA, (
            f"asks for a value as of now ({m.group(0)!r}); the materials are static"
        )
    if (m := _QUANTITATIVE.search(text)) is not None:
        return Intent.QUANTITATIVE_REQUEST, (
            f"asks for a statistic computed over data ({m.group(0)!r}), which the "
            "course materials do not contain"
        )
    return Intent.ANSWERABLE, ""

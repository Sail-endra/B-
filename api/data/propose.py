"""Propose FRED search queries for a concept -- the model writes the query, never
an id.

The gap this closes: "budget constraint" is not itself a FRED series, but the
concept is *about* how income and prices bound a consumer, which real series --
disposable personal income, the CPI -- do measure. A keyword like "budget
constraint real world data" finds none of them; translating the concept into the
indicators that illustrate it is world knowledge, which is exactly what the model
supplies. The output is always plain-language SEARCH TERMS; the search-first
guarantee (`SeriesRegistry`) still forbids the model from ever naming an id.

This is a per-concept gate as much as a query builder: when no aggregate series
meaningfully illustrates the concept, the model returns an empty list and no chart
is rendered, which is the correct answer for most micro concepts.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

_PROMPT = """A student is studying the economics concept below. Identify the real
economic QUANTITIES the concept is defined in terms of (for example: prices,
income, output, employment, interest rates), and propose up to TWO published
indicators that measure those quantities -- the kind of thing you would type into
a search box at FRED (Federal Reserve Economic Data).

Worked reasoning: a "budget constraint" is defined by prices and income, so its
illustrating series are a price index and an income series (e.g. "consumer price
index", "disposable personal income") -- even though "budget constraint" is not
itself a series.

Rules:
- Output plain-language SEARCH TERMS a person would type, NEVER a series id.
- If the concept is purely about individual preferences or abstract theory with no
  measurable aggregate quantity (an indifference curve, a utility function), return
  an empty list. An empty list is a correct, common answer; do not force a match.

Return JSON only: {{"queries": ["<term>", "<term>"]}}

Concept: {concept}"""

# A proposal that looks like an id (all-caps alnum token) is rejected: the model
# must not smuggle an identifier through the query field.
_LOOKS_LIKE_ID = re.compile(r"^[A-Z0-9]{3,20}$")


def propose_series_queries(
    concept: str, llm: Any, passages: Sequence[str] = ()
) -> list[str]:
    """Return up to two plain-language FRED search queries, or [] if none map.

    Falls back to [] (no chart) when no model is available, rather than guessing a
    keyword that would surface an unrelated series.
    """
    concept = (concept or "").strip()
    if not concept or not getattr(llm, "available", False):
        return []
    try:
        data = llm.json(_PROMPT.format(concept=concept), max_tokens=120)
    except Exception:  # noqa: BLE001 - a data card must never break the answer
        return []
    out: list[str] = []
    for q in data.get("queries", []) or []:
        q = str(q).strip()
        if q and not _LOOKS_LIKE_ID.match(q) and q.lower() not in {c.lower() for c in out}:
            out.append(q)
    return out[:2]

"""Decompose a compound question into weighted sub-queries.

A single embedding of "what is the budget constraint? how does it play into the
real world? give me an example and explain it" averages four intents into a
vector that is close to none of them -- and, worse, the rare framing words ("real
world") carry high IDF and pull sparse retrieval toward whichever chapter happens
to use that phrase, so the chapter literally titled *Budget Constraint* loses to
*Asymmetric Information*. That is the failure this module exists to prevent.

The fix is to retrieve the *concept* on its own, retrieve the application and the
example separately, and fuse the rankings -- weighting the concept above the
framing, because the topic is what the student wants and the rest is how they
asked.

Two decomposers, measured against each other in the eval:

  heuristic -- split on question marks and connectives, lift the concept noun
               phrase by stripping interrogative framing. Zero cost, deterministic.
  llm       -- one cheap JSON call. More robust on oddly-phrased questions.

The heuristic ships by default; the eval reports both so the choice is evidenced
rather than assumed. `RESULTS.md` carries the comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Sub-intent kinds. "example" and "application" matter beyond retrieval: the
# answer layer (explain) uses them to decide whether a worked example is required,
# and the data layer uses "application" as one signal that a real-world series
# might be relevant.
CONCEPT = "concept"
APPLICATION = "application"
EXAMPLE = "example"
PART = "part"  # an additional distinct sub-question

# The concept sub-query is what the student actually wants answered; framing and
# application sub-queries are how they asked. Weight the concept above them so a
# chapter-title match on the concept is not out-voted by a rare framing word.
CONCEPT_WEIGHT = 2.0
SECONDARY_WEIGHT = 1.0

# Split on sentence boundaries, and on "and"/comma ONLY when a new interrogative
# or imperative clause follows. This keeps noun lists intact ("taxes, subsidies
# and rationing" is one clause) while separating genuine sub-questions ("...and
# give me an example", "..., how does it apply").
_CLAUSE_SPLIT = re.compile(
    r"[?;\n]+"
    r"|\s+and\s+(?=give|explain|show|tell|describe|list|walk|define|how\b|why\b|what\b|when\b|where\b)"
    r"|,\s+(?=how|why|what|give|explain|define|show|tell)\b",
    re.I,
)

# Interrogative / imperative framing stripped to leave the bare concept.
_LEAD_FRAMING = re.compile(
    r"^\s*(?:"
    r"what(?:'s| is| are| does| do)?|"
    r"who(?:'s| is| are)?|"
    r"how (?:does|do|is|are|can|would|might)?|"
    r"why (?:does|do|is|are)?|"
    r"when (?:does|do|is|are)?|"
    r"where (?:does|do|is|are)?|"
    r"define|defines|explain|describe|discuss|summari[sz]e|"
    r"tell me about|give me|show me|walk me through|can you|please|"
    r"i want to know about|i'd like to know about"
    r")\b[\s:,]*",
    re.I,
)
_LEAD_ARTICLE = re.compile(r"^\s*(?:the|a|an|this|that|these|those|its|it|it's)\b\s*", re.I)
# "an example of", "real-world examples of", "an instance of" -> the concept follows.
_LEAD_EXAMPLE_OF = re.compile(
    r"^\s*(?:real[\s-]?world\s+)?(?:examples?|instances?)\s+of\s+", re.I
)
_TRAIL_FILLER = re.compile(
    r"\s*(?:"
    r"and explain it(?: to me)?|and explain|to me|for me|please|"
    r"in (?:simple|plain) (?:terms|english)|"
    r"work|works|mean|means|actually mean|really mean"
    r")\s*$",
    re.I,
)

_WANTS_EXAMPLE = re.compile(
    r"\b(example|examples|instance|for instance|e\.g\.|such as|illustrat\w*|concrete)\b", re.I
)
_WANTS_APPLICATION = re.compile(
    r"\b(real[\s-]?world|in practice|in the real world|applicat\w*|apply|applies|"
    r"applied|used in|use case|matter\w* in|play\w* (?:in|into))\b",
    re.I,
)


@dataclass
class SubQuery:
    text: str
    weight: float
    kind: str


@dataclass
class Decomposition:
    original: str
    concept: str
    subqueries: list[SubQuery] = field(default_factory=list)
    wants_example: bool = False
    wants_application: bool = False
    method: str = "single"

    @property
    def is_compound(self) -> bool:
        return self.method != "single" and len(self.subqueries) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "concept": self.concept,
            "method": self.method,
            "wants_example": self.wants_example,
            "wants_application": self.wants_application,
            "subqueries": [
                {"text": s.text, "weight": s.weight, "kind": s.kind}
                for s in self.subqueries
            ],
        }


def _clean_clause(clause: str) -> str:
    text = clause.strip().strip(".!,: ")
    prev = None
    # Strip leading framing then a leading article, repeatedly, so "how does the"
    # collapses to nothing and leaves the concept.
    while prev != text:
        prev = text
        text = _LEAD_FRAMING.sub("", text, count=1)
        text = _LEAD_ARTICLE.sub("", text, count=1)
        text = _LEAD_EXAMPLE_OF.sub("", text, count=1)
    text = _TRAIL_FILLER.sub("", text).strip().strip(".!,: ")
    return text


def _extract_concept(question: str, clauses: list[str]) -> str:
    """The concept is the bare noun phrase the student is asking about.

    Prefer the first clause that survives framing-stripping as a short phrase
    (2-6 content words); fall back to the whole question stripped of framing.
    """
    candidates = []
    for i, clause in enumerate(clauses):
        cleaned = _clean_clause(clause)
        # Reject clauses that are only pronoun/framing residue ("it", "it play
        # into", empty).
        if not cleaned or len(cleaned) < 3:
            continue
        # An example/application clause describes HOW, not WHAT, so it is not the
        # concept -- unless it is the opening clause and nothing better precedes it
        # ("give me an example of the budget constraint").
        if _WANTS_EXAMPLE.search(clause) or _WANTS_APPLICATION.search(clause):
            if i != 0 or candidates:
                continue
        words = cleaned.split()
        if 1 <= len(words) <= 8:
            candidates.append(cleaned)
    if candidates:
        return candidates[0]
    # Fallback: strip framing off the whole question.
    whole = _clean_clause(question)
    return whole or question.strip()


def decompose_heuristic(question: str) -> Decomposition:
    q = (question or "").strip()
    if not q:
        return Decomposition(original=q, concept="", subqueries=[], method="single")

    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(q) if c and c.strip()]
    wants_example = bool(_WANTS_EXAMPLE.search(q))
    wants_application = bool(_WANTS_APPLICATION.search(q))
    concept = _extract_concept(q, clauses or [q])

    # Not compound: one clause, no extra intents. Keep the original query intact
    # so simple lookups are unaffected.
    extra_parts = [
        _clean_clause(c)
        for c in clauses[1:]
        if _clean_clause(c)
        and not _WANTS_EXAMPLE.search(c)
        and not _WANTS_APPLICATION.search(c)
        and _clean_clause(c).lower() != concept.lower()
    ]
    if len(clauses) <= 1 and not wants_example and not wants_application and not extra_parts:
        return Decomposition(
            original=q,
            concept=concept,
            subqueries=[SubQuery(q, CONCEPT_WEIGHT, CONCEPT)],
            method="single",
        )

    subs: list[SubQuery] = [SubQuery(concept, CONCEPT_WEIGHT, CONCEPT)]
    if wants_application:
        subs.append(SubQuery(f"{concept} real world application", SECONDARY_WEIGHT, APPLICATION))
    if wants_example:
        subs.append(SubQuery(f"{concept} example", SECONDARY_WEIGHT, EXAMPLE))
    for part in extra_parts:
        subs.append(SubQuery(part, SECONDARY_WEIGHT, PART))

    return Decomposition(
        original=q,
        concept=concept,
        subqueries=subs,
        wants_example=wants_example,
        wants_application=wants_application,
        method="heuristic",
    )


_LLM_PROMPT = """Break this student question into retrieval sub-queries.

Return JSON only:
{{"concept": "<the single core topic, 1-5 words, no framing words>",
  "wants_example": <true/false>,
  "wants_application": <true/false>,
  "extra_parts": ["<any additional distinct sub-question topics, concept-only>"]}}

Rules:
- "concept" is the noun phrase the student wants explained, stripped of "what is",
  "how does", articles, and framing. For "what is the budget constraint? how does
  it play into the real world?" the concept is "budget constraint".
- wants_example: does the student ask for an example or illustration?
- wants_application: do they ask how it applies / works in the real world?

QUESTION: {question}"""


def decompose_llm(question: str, llm: Any) -> Optional[Decomposition]:
    """LLM decomposition. Returns None (caller falls back to heuristic) when no
    model is available or the call fails -- decomposition must never break search."""
    if not getattr(llm, "available", False):
        return None
    try:
        data = llm.json(_LLM_PROMPT.format(question=question), max_tokens=250)
    except Exception:  # noqa: BLE001 - never let decomposition break retrieval
        return None
    concept = str(data.get("concept", "")).strip()
    if not concept:
        return None
    wants_example = bool(data.get("wants_example"))
    wants_application = bool(data.get("wants_application"))
    extra = [str(p).strip() for p in data.get("extra_parts", []) if str(p).strip()]

    subs = [SubQuery(concept, CONCEPT_WEIGHT, CONCEPT)]
    if wants_application:
        subs.append(SubQuery(f"{concept} real world application", SECONDARY_WEIGHT, APPLICATION))
    if wants_example:
        subs.append(SubQuery(f"{concept} example", SECONDARY_WEIGHT, EXAMPLE))
    for part in extra:
        if part.lower() != concept.lower():
            subs.append(SubQuery(part, SECONDARY_WEIGHT, PART))

    method = "llm" if (len(subs) > 1) else "single"
    return Decomposition(
        original=question.strip(),
        concept=concept,
        subqueries=subs,
        wants_example=wants_example,
        wants_application=wants_application,
        method=method,
    )


def decompose(question: str, llm: Any = None, prefer_llm: bool = False) -> Decomposition:
    """Decompose `question`. Heuristic by default; LLM when asked and available.

    The heuristic ships because the eval shows it matches the LLM on the benchmark
    at zero cost and zero latency (see RESULTS.md).
    """
    if prefer_llm and llm is not None:
        result = decompose_llm(question, llm)
        if result is not None:
            return result
    return decompose_heuristic(question)

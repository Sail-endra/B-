"""Derive per-course capabilities from the corpus, never from a course code.

The only capability today is `has_data_link`: does this course study things that
real-world economic time series measure? Economics, finance and econometrics do.
Literature, archaeology and computer organization do not.

Course codes cannot answer this. "ECON 303" is intermediate micro at one school
and econometrics at another; plenty of quantitative economics is taught under
codes like "PUBP", "PPOL" or "BUAD". So the flag is derived from what the
uploaded corpus and syllabus actually say.

Two backends, as everywhere else. With a model configured, one cheap
classification call. Without one, a lexical score over two marker vocabularies,
normalised by corpus size so a long book cannot win on length alone. The decision
and its reason are stored on the course, so a wrong call is visible and
correctable rather than mysterious.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

# Vocabulary that indicates a subject measured by published economic statistics.
_DATA_MARKERS = {
    "inflation", "unemployment", "gdp", "gross domestic product", "interest rate",
    "monetary policy", "fiscal policy", "price index", "consumer price",
    "exchange rate", "recession", "business cycle", "econometric", "regression",
    "time series", "elasticity", "demand curve", "supply curve", "equilibrium price",
    "marginal cost", "marginal revenue", "consumer surplus", "market equilibrium",
    "utility maximization", "budget constraint", "opportunity cost", "monopoly",
    "oligopoly", "labour supply", "labor supply", "capital market", "asset pricing",
    "portfolio", "interest", "wage", "productivity", "output per worker",
}

# Vocabulary that indicates a subject no economic series measures.
_NON_DATA_MARKERS = {
    "narrator", "protagonist", "stanza", "metaphor", "prose", "novel", "poem",
    "rhetoric", "close reading", "literary", "author", "chapter of the novel",
    "excavation", "stratigraphy", "artefact", "artifact", "archaeological",
    "ethnography", "kinship", "fieldwork", "positionality", "colonial",
    "register", "cache", "instruction set", "assembly", "compiler", "pipeline",
    "operating system", "byte", "pointer", "processor", "memory hierarchy",
    "algorithm", "data structure", "runtime complexity",
}

# Ratio above which the corpus is judged to have a data link. Set from the
# benchmark corpora: Varian scores far above it, the novel and the archaeology
# readings far below, with a wide gap rather than a knife edge.
_THRESHOLD = 1.6
# A corpus smaller than this cannot be classified confidently either way.
_MIN_SAMPLE_CHARS = 4000

CLASSIFY_PROMPT = """Does this course study subjects that published real-world economic
statistics measure -- things like prices, output, employment, interest rates or
markets?

Answer true for economics, finance, econometrics, and quantitative policy courses.
Answer false for literature, history, archaeology, anthropology, computer science
and similar, even when they use numbers.

Course: {code} {title}

Sample of the course materials:
{sample}

Reply JSON only: {{"has_data_link": true, "reason": "<12 words>"}}"""


@dataclass
class Capability:
    has_data_link: bool
    reason: str
    method: str
    score: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "has_data_link": self.has_data_link,
            "reason": self.reason,
            "method": self.method,
            "score": round(self.score, 3),
        }


def _count_markers(text: str, markers: set[str]) -> int:
    lowered = text.lower()
    total = 0
    for marker in markers:
        if " " in marker:
            total += lowered.count(marker)
        else:
            total += len(re.findall(rf"\b{re.escape(marker)}\b", lowered))
    return total


def classify_lexically(sample: str) -> Capability:
    data_hits = _count_markers(sample, _DATA_MARKERS)
    other_hits = _count_markers(sample, _NON_DATA_MARKERS)

    if len(sample) < _MIN_SAMPLE_CHARS:
        return Capability(
            has_data_link=False,
            reason="too little text to classify; defaulting to no data link",
            method="lexical/insufficient",
            score=0.0,
        )

    # +1 smoothing so a corpus with zero non-data markers does not divide by zero.
    ratio = (data_hits + 1) / (other_hits + 1)
    has_link = ratio >= _THRESHOLD
    return Capability(
        has_data_link=has_link,
        reason=(
            f"{data_hits} economic-data markers vs {other_hits} humanities/CS markers "
            f"(ratio {ratio:.2f}, threshold {_THRESHOLD})"
        ),
        method="lexical",
        score=ratio,
    )


def detect(
    code: str,
    title: str,
    corpus_sample: Sequence[str],
    syllabus_text: str = "",
    allow_gemini: bool = False,
) -> Capability:
    """Classify one course. `corpus_sample` is a spread of chunk texts."""
    sample = "\n".join(corpus_sample)[:12000]
    combined = (syllabus_text[:3000] + "\n" + sample).strip()

    from ..llm import get_llm

    llm = get_llm(allow_gemini=allow_gemini)
    if llm.available and combined:
        try:
            data = llm.json(
                CLASSIFY_PROMPT.format(code=code, title=title, sample=combined[:8000]),
                max_tokens=200,
            )
            return Capability(
                has_data_link=bool(data.get("has_data_link")),
                reason=str(data.get("reason", ""))[:120],
                method="llm",
            )
        except Exception:  # noqa: BLE001 - fall through to the lexical path
            pass

    return classify_lexically(combined)

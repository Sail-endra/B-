"""Groundedness: what fraction of asserted claims are supported by a cited passage.

An answer is decomposed into atomic claims and each is checked against the text of
the chunks the answer cited. The headline number is the share of claims supported.

Two backends, as everywhere else. With a model configured, an LLM judge does the
decomposition and the entailment check. Without one, a lexical judge checks
whether each sentence's content terms are covered by the cited passages. The
lexical judge is strictly weaker -- it cannot catch a claim that reuses the
passage's vocabulary while inverting its meaning -- and that limitation is stated
in the report rather than hidden, because a groundedness number from a weak judge
is exactly the kind of metric that invites over-claiming.

    python -m eval.run_grounding
    python -m eval.run_grounding --limit 20
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from api.agent.loop import NOT_IN_MATERIALS, Agent
from api.config import settings
from api.llm import get_llm
from api.store import get_store

from .dataset import BENCHMARK_USER, EvalQuestion, load_questions

_SENT = re.compile(r"(?<=[.!?])\s+")
_CITE = re.compile(r"\[[A-Z]{4}\d{3},[^\]]*\]")
_WORD = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")
_STOP = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "is", "are",
    "was", "were", "be", "been", "it", "its", "this", "that", "these", "those", "as",
    "by", "with", "from", "which", "not", "but", "than", "then", "so", "only", "also",
}

JUDGE_PROMPT = """You are checking whether an answer is grounded in its sources.

Break the ANSWER into atomic factual claims. For each, decide whether the SOURCES
support it. A claim is supported only if the sources state it or directly entail
it -- not if the sources merely discuss the same topic.

Ignore pure hedging or framing sentences ("this is covered in chapter 3").

Return JSON only:
{{"claims": [{{"claim": "<short>", "supported": true, "why": "<12 words>"}}]}}

QUESTION: {question}

SOURCES:
{sources}

ANSWER:
{answer}
"""


@dataclass
class GroundingResult:
    question_id: str
    n_claims: int
    n_supported: int
    unsupported: list[str] = field(default_factory=list)
    refused: bool = False
    has_citations: bool = False

    @property
    def rate(self) -> float:
        return self.n_supported / self.n_claims if self.n_claims else 1.0


def _terms(text: str) -> set[str]:
    return {t for t in _WORD.findall((text or "").lower()) if t not in _STOP and len(t) > 2}


def lexical_judge(answer: str, sources: Sequence[str]) -> tuple[int, int, list[str]]:
    """Coverage-based fallback judge.

    A sentence counts as supported when at least 80% of its content terms appear
    in the cited passages. This catches fabricated specifics -- names, numbers,
    entities that are simply absent -- which is the dominant failure mode for a
    bare model. It cannot catch a correctly-worded inversion, and the report says
    so.
    """
    source_terms: set[str] = set()
    for source in sources:
        source_terms |= _terms(source)

    supported, total, unsupported = 0, 0, []
    for sentence in _SENT.split(_CITE.sub("", answer or "")):
        sentence = sentence.strip()
        if len(sentence) < 25:
            continue
        claim_terms = _terms(sentence)
        if not claim_terms:
            continue
        total += 1
        covered = len(claim_terms & source_terms) / len(claim_terms)
        if covered >= 0.8:
            supported += 1
        else:
            unsupported.append(sentence[:140])
    return total, supported, unsupported


def llm_judge(question: str, answer: str, sources: Sequence[str]) -> tuple[int, int, list[str]]:
    llm = get_llm()
    prompt = JUDGE_PROMPT.format(
        question=question,
        sources="\n\n---\n\n".join(s[:1500] for s in sources) or "(none)",
        answer=answer,
    )
    data = llm.json(prompt, max_tokens=1600)
    claims = data.get("claims", [])
    supported = [c for c in claims if c.get("supported")]
    unsupported = [str(c.get("claim", ""))[:140] for c in claims if not c.get("supported")]
    return len(claims), len(supported), unsupported


def evaluate(
    questions: Sequence[EvalQuestion], limit: Optional[int] = None
) -> tuple[list[GroundingResult], dict[str, object]]:
    store = get_store()
    llm = get_llm()
    # Benchmark answerer injected explicitly (not the privacy-gated product path).
    agent = Agent(user_id=BENCHMARK_USER, llm=llm)
    judge_name = "llm-judge" if llm.available else "lexical-judge"

    targets = [q for q in questions if q.in_corpus][:limit]
    results: list[GroundingResult] = []

    for question in targets:
        answer = agent.ask(question.q, question.course)
        if answer.refused or answer.answer.startswith(NOT_IN_MATERIALS):
            results.append(
                GroundingResult(question.id, 0, 0, refused=True, has_citations=False)
            )
            continue

        cited = store.get_chunks([c.chunk_id for c in answer.citations], with_parents=False)
        sources = [c.text for c in cited]
        if llm.available:
            try:
                total, supported, unsupported = llm_judge(question.q, answer.answer, sources)
            except Exception:  # noqa: BLE001 - degrade rather than abort a sweep
                total, supported, unsupported = lexical_judge(answer.answer, sources)
        else:
            total, supported, unsupported = lexical_judge(answer.answer, sources)

        results.append(
            GroundingResult(
                question_id=question.id,
                n_claims=total,
                n_supported=supported,
                unsupported=unsupported,
                has_citations=bool(answer.citations),
            )
        )

    answered = [r for r in results if not r.refused]
    total_claims = sum(r.n_claims for r in answered)
    total_supported = sum(r.n_supported for r in answered)

    summary: dict[str, object] = {
        "judge": judge_name,
        "n_questions": len(results),
        "n_answered": len(answered),
        "n_refused": len(results) - len(answered),
        "total_claims": total_claims,
        "supported_claims": total_supported,
        "grounded_rate": round(total_supported / total_claims, 4) if total_claims else 0.0,
        "answers_with_citations": round(
            sum(1 for r in answered if r.has_citations) / len(answered), 4
        )
        if answered
        else 0.0,
        "backends": settings.describe_backends(),
    }
    if judge_name == "lexical-judge":
        summary["judge_caveat"] = (
            "Lexical coverage judge: detects fabricated specifics, but cannot detect "
            "a claim that reuses source vocabulary while inverting its meaning. Treat "
            "this as an upper bound and re-run with a model configured."
        )
    if not llm.available:
        # Without a model the answerer is purely extractive -- it copies sentences
        # out of the retrieved passages. A grounded rate of 1.0 under that answerer
        # is therefore structurally guaranteed and measures nothing. Saying so is
        # the difference between a metric and a decoration.
        summary["tautology_warning"] = (
            "NO GENERATIVE MODEL CONFIGURED. The offline answerer is extractive: it "
            "quotes retrieved sentences verbatim, so it cannot emit an ungrounded "
            "claim by construction. A grounded rate of 1.000 here is a property of "
            "the answerer, NOT evidence about grounding. This number only becomes "
            "meaningful with ANTHROPIC_API_KEY set."
        )
    return results, summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Groundedness evaluation")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--json")
    args = parser.parse_args(argv)

    results, summary = evaluate(load_questions(), args.limit)

    print(f"judge: {summary['judge']}")
    print(f"questions: {summary['n_questions']}  answered: {summary['n_answered']}  "
          f"refused: {summary['n_refused']}")
    print(f"claims: {summary['supported_claims']}/{summary['total_claims']} supported")
    print(f"GROUNDED RATE: {summary['grounded_rate']:.3f}")
    print(f"answers carrying citations: {summary['answers_with_citations']:.3f}")
    if "tautology_warning" in summary:
        print(f"\n!! {summary['tautology_warning']}")
    if "judge_caveat" in summary:
        print(f"\ncaveat: {summary['judge_caveat']}")

    worst = sorted((r for r in results if r.unsupported), key=lambda r: r.rate)[:6]
    if worst:
        print("\nleast-grounded answers:")
        for result in worst:
            print(f"  {result.question_id} rate={result.rate:.2f}")
            for claim in result.unsupported[:2]:
                print(f"    unsupported: {claim}")

    if args.json:
        from pathlib import Path

        Path(args.json).write_text(
            json.dumps(
                {"summary": summary, "results": [r.__dict__ for r in results]}, indent=2
            )
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

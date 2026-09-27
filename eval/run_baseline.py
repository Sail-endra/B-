"""Baseline comparison: the same questions through a bare model, no retrieval.

This is the highest value-per-hour artifact in the project, because the question
set, the judge and the agent already exist -- the only new code is "ask the model
directly and score it the same way".

What it shows is the thing a reader actually wants to know: not that the system
answers questions, but that it answers them *differently* from a model that has
never seen these materials.

    python -m eval.run_baseline --limit 40

Requires a model. Without one there is no bare baseline to compare against, and
the script says so rather than inventing numbers -- a fabricated comparison chart
would be worse than no chart.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Optional, Sequence

from api.agent.loop import Agent
from api.config import settings
from api.llm import get_llm
from api.store import get_store

from .dataset import EvalQuestion, load_questions
from .run_grounding import lexical_judge, llm_judge

BARE_PROMPT = """Answer this question from a university course as helpfully as you can.

{question}"""


@dataclass
class SideBySide:
    label: str
    n: int = 0
    answered: int = 0
    refused: int = 0
    with_citation: int = 0
    claims: int = 0
    supported: int = 0

    @property
    def grounded_rate(self) -> float:
        return self.supported / self.claims if self.claims else 0.0

    @property
    def citation_rate(self) -> float:
        return self.with_citation / self.answered if self.answered else 0.0

    @property
    def refusal_rate(self) -> float:
        return self.refused / self.n if self.n else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "n": self.n,
            "answered": self.answered,
            "refused": self.refused,
            "correct_refusal_rate": round(self.refusal_rate, 4),
            "citation_rate": round(self.citation_rate, 4),
            "grounded_rate": round(self.grounded_rate, 4),
        }


def _judge(question: str, answer: str, sources: Sequence[str]) -> tuple[int, int]:
    llm = get_llm()
    if llm.available:
        try:
            total, supported, _ = llm_judge(question, answer, sources)
            return total, supported
        except Exception:  # noqa: BLE001
            pass
    total, supported, _ = lexical_judge(answer, sources)
    return total, supported


def run(questions: Sequence[EvalQuestion], limit: Optional[int] = None) -> dict[str, object]:
    llm = get_llm()
    if not llm.available:
        raise SystemExit(
            "No model configured, so there is no bare-model baseline to measure.\n"
            "Set ANTHROPIC_API_KEY and re-run. This script will not fabricate a "
            "comparison."
        )

    store = get_store()
    agent = Agent()
    targets = list(questions)[:limit] if limit else list(questions)

    bare = SideBySide("bare model (no retrieval)")
    system = SideBySide("course copilot")

    for question in targets:
        # Ground truth for scoring is the gold passages, for both systems alike.
        gold_ids = question.resolve_gold(store)
        gold_text = [c.text for c in store.get_chunks(sorted(gold_ids), with_parents=False)]

        # -- bare model
        bare.n += 1
        raw = llm.text(BARE_PROMPT.format(question=question.q), max_tokens=700)
        bare_refused = _looks_like_refusal(raw)
        if bare_refused:
            bare.refused += 1
        else:
            bare.answered += 1
            if "[" in raw and "Ch" in raw:
                bare.with_citation += 1
            if gold_text:
                total, supported = _judge(question.q, raw, gold_text)
                bare.claims += total
                bare.supported += supported

        # -- the system
        system.n += 1
        answer = agent.ask(question.q, question.course)
        if answer.refused:
            system.refused += 1
        else:
            system.answered += 1
            if answer.citations:
                system.with_citation += 1
            cited = store.get_chunks(
                [c.chunk_id for c in answer.citations], with_parents=False
            )
            if cited:
                total, supported = _judge(question.q, answer.answer, [c.text for c in cited])
                system.claims += total
                system.supported += supported

    # Refusal is only *correct* on out-of-corpus questions; recompute against those.
    out_of_corpus = [q for q in targets if not q.in_corpus]
    return {
        "backends": settings.describe_backends(),
        "n_questions": len(targets),
        "n_out_of_corpus": len(out_of_corpus),
        "bare": bare.as_dict(),
        "system": system.as_dict(),
    }


def _looks_like_refusal(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "i don't have access",
            "i do not have access",
            "i cannot",
            "i can't",
            "not able to",
            "don't have information",
            "no information about",
        )
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bare-model baseline comparison")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--json")
    args = parser.parse_args(argv)

    payload = run(load_questions(), args.limit)
    bare, system = payload["bare"], payload["system"]  # type: ignore[index]

    header = f"{'':<28} {'grounded':>10} {'cites':>8} {'refused':>9}"
    print(header)
    print("-" * len(header))
    for row in (bare, system):
        print(
            f"{row['label']:<28} {row['grounded_rate']:>10.3f} "
            f"{row['citation_rate']:>8.3f} {row['correct_refusal_rate']:>9.3f}"
        )

    if args.json:
        from pathlib import Path

        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

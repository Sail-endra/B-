"""Groundedness of the *explanation layer*, measured per depth.

`run_grounding` measures the verbatim extractive answer -- which, being copied out
of the passages, is grounded by construction. This harness instead measures the
generated gloss the explanation layer adds on top, and it does so for the two
depths SEPARATELY, because the whole point of the depth control is that a fuller
walkthrough (`in_depth`) has more room to drift past what the passages support
than a 3-5 sentence summary (`concise`) does. Reporting a single blended number
would hide exactly the tradeoff the control exists to expose.

Both the explanation and the judge run on the same model. Gemini is enabled here
because this is the benchmark user -- the only place the free tier is allowed to
see text (see `get_llm`'s docstring). It never runs on cohort or product data.

    python -m eval.run_explanation_grounding
    python -m eval.run_explanation_grounding --limit 10 --json out.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Optional, Sequence

from api.agent.loop import NOT_IN_MATERIALS, Agent
from api.config import settings
from api.llm import get_llm, reset_llm

from .dataset import BENCHMARK_USER, EvalQuestion, load_questions
from .run_grounding import lexical_judge, llm_judge

DEPTHS = ("concise", "in_depth")


@dataclass
class DepthResult:
    question_id: str
    depth: str
    n_claims: int
    n_supported: int
    unsupported: list[str] = field(default_factory=list)
    refused: bool = False
    fell_back: bool = False  # in_depth that the layer downgraded to concise
    judged_by: str = ""  # "llm" | "lexical-fallback" -- never let a fallback hide

    @property
    def rate(self) -> float:
        return self.n_supported / self.n_claims if self.n_claims else 1.0


def _summarize(depth: str, results: Sequence[DepthResult], judge: str) -> dict:
    answered = [r for r in results if not r.refused]
    claims = sum(r.n_claims for r in answered)
    supported = sum(r.n_supported for r in answered)
    n_lexical = sum(1 for r in answered if r.judged_by == "lexical-fallback")
    return {
        "depth": depth,
        "judge": judge,
        "n_questions": len(results),
        "n_answered": len(answered),
        "n_refused": len(results) - len(answered),
        "n_fell_back_to_concise": sum(1 for r in answered if r.fell_back),
        "n_lexical_fallback": n_lexical,  # >0 means the number is not purely llm-judged
        "total_claims": claims,
        "supported_claims": supported,
        "grounded_rate": round(supported / claims, 4) if claims else 0.0,
    }


def evaluate(
    questions: Sequence[EvalQuestion], limit: Optional[int] = None
) -> tuple[dict[str, list[DepthResult]], dict]:
    # Prime the LLM singleton with the Gemini-enabled answerer, so both the agent
    # and the judge use it. Benchmark-only path.
    reset_llm()
    llm = get_llm(allow_gemini=True)
    if not llm.available:
        raise SystemExit(
            "No generative model available. Set GEMINI_API_KEY (and do not set "
            "COPILOT_OFFLINE) so the explanation layer actually generates."
        )
    judge_name = "llm-judge"

    # Inject the benchmark answerer explicitly. The product path is gated by the
    # privacy boundary; the benchmark is not, so it passes its Gemini llm directly.
    agent = Agent(user_id=BENCHMARK_USER, llm=llm)
    targets = [q for q in questions if q.in_corpus][:limit]

    by_depth: dict[str, list[DepthResult]] = {d: [] for d in DEPTHS}

    for depth in DEPTHS:
        for question in targets:
            answer = agent.ask(question.q, question.course, depth=depth)

            # No grounded gloss to score: retrieval refused, or the layer produced
            # nothing (no passages strong enough to explain from).
            if (
                answer.refused
                or answer.answer.startswith(NOT_IN_MATERIALS)
                or not answer.explanation.strip()
            ):
                by_depth[depth].append(
                    DepthResult(question.id, depth, 0, 0, refused=True)
                )
                continue

            # Judge against the SAME passages the explanation was allowed to use --
            # every retrieved passage explain() saw, not the narrower subset the
            # extractive answer happened to cite. Judging against the citation
            # subset systematically undercounts: a claim grounded in a
            # retrieved-but-uncited passage would be scored as fabricated. Mirror
            # explain.build_prompt's body selection exactly (snippet, then text).
            passages = getattr(agent, "_last_passages", [])
            sources = [
                (p.get("snippet") or p.get("text") or "") for p in passages
            ]
            # The explanation is bounded to the passages; score IT, not the
            # extractive answer.
            try:
                total, supported, unsupported = llm_judge(
                    question.q, answer.explanation, sources
                )
                judged_by = "llm"
            except Exception as exc:  # noqa: BLE001 - degrade, but record it loudly
                total, supported, unsupported = lexical_judge(
                    answer.explanation, sources
                )
                judged_by = "lexical-fallback"
                print(f"  ! {question.id}/{depth}: llm judge failed "
                      f"({type(exc).__name__}: {str(exc)[:60]}); used lexical")

            fell_back = depth == "in_depth" and not answer.explained
            by_depth[depth].append(
                DepthResult(
                    question_id=question.id,
                    depth=depth,
                    n_claims=total,
                    n_supported=supported,
                    unsupported=unsupported,
                    fell_back=fell_back,
                    judged_by=judged_by,
                )
            )

    summary = {
        "backends": settings.describe_backends(),
        "per_depth": {d: _summarize(d, by_depth[d], judge_name) for d in DEPTHS},
    }
    return by_depth, summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Explanation-layer groundedness by depth")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--json")
    args = parser.parse_args(argv)

    by_depth, summary = evaluate(load_questions(), args.limit)

    print(f"backends: {summary['backends']}\n")
    for depth in DEPTHS:
        s = summary["per_depth"][depth]
        print(f"[{depth}]")
        print(f"  answered {s['n_answered']}/{s['n_questions']}  refused {s['n_refused']}"
              f"  fell-back-to-concise {s['n_fell_back_to_concise']}")
        print(f"  claims {s['supported_claims']}/{s['total_claims']} supported")
        print(f"  GROUNDED RATE: {s['grounded_rate']:.3f}\n")

    c, d = summary["per_depth"]["concise"], summary["per_depth"]["in_depth"]
    delta = d["grounded_rate"] - c["grounded_rate"]
    print(f"in_depth - concise = {delta:+.3f}  "
          f"({'in_depth worse' if delta < 0 else 'in_depth not worse'})")

    if args.json:
        from pathlib import Path

        Path(args.json).write_text(
            json.dumps(
                {
                    "summary": summary,
                    "results": {
                        d: [r.__dict__ for r in by_depth[d]] for d in DEPTHS
                    },
                },
                indent=2,
            )
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

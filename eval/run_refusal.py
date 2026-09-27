"""Refusal calibration and measurement.

Sweeps the reranker-score threshold and picks the F1-maximising value rather than
guessing one. Both precision and recall are reported, always: a system that
refuses everything scores perfect precision and is useless, so recall is what
keeps the number honest.

    python -m eval.run_refusal              # measure at the configured threshold
    python -m eval.run_refusal --calibrate  # sweep and report the best threshold

A note on what this can and cannot separate. Questions like "what is the current
federal funds rate" are out of corpus, but their *topic* is squarely in it, so a
relevance score cannot distinguish them -- the retrieved passage genuinely is
about the federal funds rate. Those questions are tagged `refuse_or_data_tool`
and are scored separately: routing them to the data tool is the correct outcome,
and that is the agent's job, not the retriever's. Folding them into the refusal
metric would understate precision for a failure the retriever cannot fix.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Optional, Sequence

from api.config import settings
from api.retrieval.pipeline import PipelineConfig, RetrievalPipeline
from api.store import get_store

from .dataset import BENCHMARK_USER, EvalQuestion, load_questions

# The operating point is chosen by F-beta with beta=2, NOT F1.
#
# F1 treats a wrong answer and a wrong refusal as equally bad. For this product
# they are not. An unnecessary refusal costs a user about thirty seconds and they
# rephrase. An ungrounded answer breaks the single claim the project makes --
# "only from your materials" -- and the user has no way to detect it, because a
# confident wrong answer looks exactly like a right one.
#
# beta=2 weights recall beta^2 = 4x precision, i.e. it encodes "one ungrounded
# answer is about as costly as four unnecessary refusals". That ratio is a
# judgement, not a measurement, and it is stated here so it can be argued with
# rather than buried in an F1 call.
REFUSAL_BETA = 2.0


@dataclass
class RefusalScore:
    threshold: float
    tp: int = 0  # correctly refused an out-of-corpus question
    fp: int = 0  # refused an in-corpus question (over-refusal)
    fn: int = 0  # answered an out-of-corpus question (hallucination risk)
    tn: int = 0  # correctly answered an in-corpus question

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        return self.f_beta(1.0)

    @property
    def f2(self) -> float:
        return self.f_beta(REFUSAL_BETA)

    def f_beta(self, beta: float) -> float:
        p, r = self.precision, self.recall
        if not (p + r):
            return 0.0
        b2 = beta * beta
        return (1 + b2) * p * r / (b2 * p + r)

    @property
    def answer_rate(self) -> float:
        """Share of in-corpus questions that were actually answered."""
        total = self.tn + self.fp
        return self.tn / total if total else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "threshold": round(self.threshold, 3),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "f2": round(self.f2, 4),
            "answer_rate_in_corpus": round(self.answer_rate, 4),
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
        }


def collect_confidences(
    pipeline: RetrievalPipeline, questions: Sequence[EvalQuestion]
) -> list[tuple[EvalQuestion, float, bool]]:
    """One retrieval pass; thresholds are swept over the cached scores afterwards,
    so calibration costs a single pass rather than one per candidate threshold.

    The third element is the intent gate's verdict, captured separately so the
    sweep can compose the two signals the way the pipeline does. Folding it into
    the score would make the threshold uninterpretable.
    """
    out = []
    for question in questions:
        # Compositional questions are included. They were excluded at first, and
        # that gap hid a real bug: long questions scored near zero and were
        # falsely refused while retrieving the correct passage at rank 1.
        result = pipeline.search(question.q, course_id=question.course)
        # Read the PIPELINE's own gate verdict rather than re-deriving it.
        #
        # Re-deriving it here caused two separate measurement bugs: first the
        # harness modelled only OUT_OF_SCOPE while the pipeline also refused
        # NEEDS_LIVE_DATA, and then the corpus-aware checks (quantitative request
        # on a course with no data link, chapter beyond the corpus) could not be
        # expressed here at all. A harness that models a subset of the system it
        # measures reports fiction; there is now one source of truth.
        out.append((question, result.confidence, result.gate_refused))
    return out


def score_at(
    confidences: Sequence[tuple[EvalQuestion, float, bool]],
    threshold: float,
    include_data_tool_cases: bool = False,
    use_intent_gate: bool = True,
) -> RefusalScore:
    score = RefusalScore(threshold=threshold)
    for question, confidence, gated in confidences:
        if question.may_use_data_tool and not include_data_tool_cases:
            continue
        refused = confidence < threshold or (use_intent_gate and gated)
        should_refuse = not question.in_corpus
        if should_refuse and refused:
            score.tp += 1
        elif should_refuse and not refused:
            score.fn += 1
        elif not should_refuse and refused:
            score.fp += 1
        else:
            score.tn += 1
    return score


def calibrate(
    confidences: Sequence[tuple[EvalQuestion, float, bool]],
    lo: float = 0.02,
    hi: float = 0.95,
    steps: int = 94,
) -> tuple[RefusalScore, list[RefusalScore]]:
    sweep = []
    for i in range(steps + 1):
        threshold = lo + (hi - lo) * i / steps
        sweep.append(score_at(confidences, threshold))
    # Maximise F-beta, not F1 -- see REFUSAL_BETA.
    best = max(sweep, key=lambda s: (s.f_beta(REFUSAL_BETA), s.answer_rate))
    return best, sweep


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Refusal calibration")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--json", help="write results here")
    args = parser.parse_args(argv)

    store = get_store()
    questions = load_questions()
    pipeline = RetrievalPipeline(store, BENCHMARK_USER)

    confidences = collect_confidences(pipeline, questions)
    payload: dict[str, object] = {"backends": settings.describe_backends()}

    if args.calibrate:
        best, sweep = calibrate(confidences)
        print(f"threshold sweep (F{REFUSAL_BETA:.0f}-maximising row marked *)\n")
        print(f"{'thresh':>7} {'prec':>7} {'recall':>7} {'F1':>7} "
              f"{'F' + format(REFUSAL_BETA, '.0f'):>7} {'answered':>9}")
        print("-" * 50)
        for score in sweep[::3]:
            mark = "*" if abs(score.threshold - best.threshold) < 1e-9 else " "
            print(
                f"{score.threshold:>7.3f} {score.precision:>7.3f} {score.recall:>7.3f} "
                f"{score.f1:>7.3f} {score.f2:>7.3f} {score.answer_rate:>9.3f} {mark}"
            )
        f1_best = max(sweep, key=lambda s: s.f1)
        print(
            f"\nF{REFUSAL_BETA:.0f}-optimal threshold {best.threshold:.3f}: "
            f"precision {best.precision:.3f}, recall {best.recall:.3f}, "
            f"F1 {best.f1:.3f}, F{REFUSAL_BETA:.0f} {best.f2:.3f}"
        )
        print(
            f"(F1-optimal would be {f1_best.threshold:.3f}: recall "
            f"{f1_best.recall:.3f}, answered {f1_best.answer_rate:.3f} -- "
            "shown only for contrast; F1 is the wrong objective here)"
        )
        print(f"in-corpus questions still answered: {best.answer_rate:.3f}")
        print(f"\nSet RetrievalConfig.refusal_threshold = {best.threshold:.3f}")
        payload["best"] = best.as_dict()
        payload["sweep"] = [s.as_dict() for s in sweep]
    else:
        current = score_at(confidences, settings.retrieval.refusal_threshold)
        print(f"at the configured threshold {current.threshold:.3f}:")
        print(f"  refusal precision  {current.precision:.3f}")
        print(f"  refusal recall     {current.recall:.3f}")
        print(f"  F1                 {current.f1:.3f}")
        print(f"  in-corpus answered {current.answer_rate:.3f}")
        print(f"  tp={current.tp} fp={current.fp} fn={current.fn} tn={current.tn}")
        payload["current"] = current.as_dict()

    # Report the data-tool cases separately rather than hiding them.
    data_cases = [(q, c) for q, c, _ in confidences if q.may_use_data_tool]
    if data_cases:
        threshold = (
            payload.get("best", {}).get("threshold")  # type: ignore[union-attr]
            if args.calibrate
            else settings.retrieval.refusal_threshold
        )
        would_refuse = sum(1 for _, c in data_cases if c < float(threshold))
        print(
            f"\nlive-data questions ({len(data_cases)}): {would_refuse} would be refused "
            f"on relevance alone; the rest must be routed to fetch_series by the agent."
        )
        payload["data_tool_cases"] = {
            "n": len(data_cases),
            "would_refuse_on_relevance": would_refuse,
        }

    if args.json:
        from pathlib import Path

        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

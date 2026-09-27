"""Retrieval metrics and the ablation table.

    python -m eval.run_retrieval            # full pipeline, per-slice breakdown
    python -m eval.run_retrieval --ablate   # every configuration, the table
    python -m eval.run_retrieval --json out.json

Metrics are reported separately for the `generated` and `natural` slices, and
that split is the point. Generated questions were written by looking at their own
gold chunk, so they share its vocabulary and inflate every lexical signal --
which is exactly the signal the BM25 ablation row exists to measure. Reporting
one blended number would make the RRF improvement look larger than it is. The
`natural` slice is the honest number.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from api.config import settings
from api.retrieval.pipeline import PipelineConfig, RetrievalPipeline
from api.store import get_store

from .dataset import BENCHMARK_USER, EvalQuestion, load_questions, summarise, validate


@dataclass
class Metrics:
    n: int = 0
    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    recall_at_5: float = 0.0
    recall_at_10: float = 0.0
    mrr: float = 0.0
    ndcg_at_10: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "n": self.n,
            "recall@1": round(self.recall_at_1, 4),
            "recall@3": round(self.recall_at_3, 4),
            "recall@5": round(self.recall_at_5, 4),
            "recall@10": round(self.recall_at_10, 4),
            "mrr": round(self.mrr, 4),
            "ndcg@10": round(self.ndcg_at_10, 4),
        }


@dataclass
class RunResult:
    label: str
    overall: Metrics
    by_slice: dict[str, Metrics] = field(default_factory=dict)
    elapsed_s: float = 0.0
    misses: list[tuple[str, str]] = field(default_factory=list)


def _recall_at(retrieved: Sequence[str], gold: set[str], k: int) -> float:
    """Hit-rate style recall: did any gold chunk appear in the top k?

    Several chunks commonly express the same fact, so requiring all of them would
    penalise a correct retrieval. `gold` is the set of chunks covering the gold
    page range and finding any one of them means the answer was surfaced.
    """
    if not gold:
        return 0.0
    return 1.0 if any(cid in gold for cid in retrieved[:k]) else 0.0


def _reciprocal_rank(retrieved: Sequence[str], gold: set[str]) -> float:
    for i, cid in enumerate(retrieved):
        if cid in gold:
            return 1.0 / (i + 1)
    return 0.0


def _ndcg_at(retrieved: Sequence[str], gold: set[str], k: int = 10) -> float:
    if not gold:
        return 0.0
    dcg = sum(
        1.0 / math.log2(i + 2) for i, cid in enumerate(retrieved[:k]) if cid in gold
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return dcg / ideal if ideal else 0.0


def evaluate(
    pipeline: RetrievalPipeline,
    questions: Sequence[EvalQuestion],
    label: str,
    top_k: int = 10,
) -> RunResult:
    store = pipeline.store
    started = time.time()

    buckets: dict[str, list[tuple[float, ...]]] = {}
    misses: list[tuple[str, str]] = []
    scored = 0
    totals = [0.0] * 6

    for question in questions:
        if not question.in_corpus or not question.gold:
            continue
        gold = question.resolve_gold(store)
        if not gold:
            continue
        result = pipeline.search(question.q, course_id=question.course, top_k=top_k)
        retrieved = [s.chunk.id for s in result.chunks]

        row = (
            _recall_at(retrieved, gold, 1),
            _recall_at(retrieved, gold, 3),
            _recall_at(retrieved, gold, 5),
            _recall_at(retrieved, gold, 10),
            _reciprocal_rank(retrieved, gold),
            _ndcg_at(retrieved, gold, 10),
        )
        buckets.setdefault(question.slice, []).append(row)
        for i, v in enumerate(row):
            totals[i] += v
        scored += 1
        if row[3] == 0.0:
            misses.append((question.id, question.q))

    def to_metrics(rows: list[tuple[float, ...]]) -> Metrics:
        if not rows:
            return Metrics()
        n = len(rows)
        col = [sum(r[i] for r in rows) / n for i in range(6)]
        return Metrics(n, col[0], col[1], col[2], col[3], col[4], col[5])

    overall = (
        Metrics(scored, *[t / scored for t in totals]) if scored else Metrics()
    )
    return RunResult(
        label=label,
        overall=overall,
        by_slice={k: to_metrics(v) for k, v in sorted(buckets.items())},
        elapsed_s=time.time() - started,
        misses=misses,
    )


# The ablation ladder. Each row adds one stage to the row above it, which is what
# makes the resulting table a record of work already done rather than extra work.
# Every stage from the row above it, off by default so each row isolates one
# addition. The last two rows are the compound-query work; the final row is the
# shipped configuration.
_BASE = dict(use_dense=True, use_sparse=True, use_rerank=True, use_parent=True, rerank_mode="rank")
ABLATION_LADDER: list[tuple[str, PipelineConfig]] = [
    ("dense only", PipelineConfig(use_dense=True, use_sparse=False, use_rerank=False, use_parent=False, decompose=False)),
    ("sparse only (BM25)", PipelineConfig(use_dense=False, use_sparse=True, use_rerank=False, use_parent=False, decompose=False)),
    ("+ BM25 (RRF)", PipelineConfig(use_dense=True, use_sparse=True, use_rerank=False, use_parent=False, decompose=False)),
    ("+ rerank (reorder)", PipelineConfig(use_dense=True, use_sparse=True, use_rerank=True, use_parent=False, rerank_mode="rank", decompose=False)),
    ("+ rerank (confidence)", PipelineConfig(use_dense=True, use_sparse=True, use_rerank=True, use_parent=False, rerank_mode="confidence", decompose=False)),
    ("+ parent-child", PipelineConfig(**_BASE, decompose=False)),
    ("+ decompose", PipelineConfig(**_BASE, decompose=True, chapter_boost_weight=0.0)),
    ("+ title-boost 0.30 [shipped]", PipelineConfig(**_BASE, decompose=True, chapter_boost_weight=0.30)),
]


def run_ablation(
    pipeline: RetrievalPipeline, questions: Sequence[EvalQuestion]
) -> list[RunResult]:
    results = []
    for label, config in ABLATION_LADDER:
        results.append(evaluate(pipeline.with_config(config), questions, label))
    return results


def format_table(results: Sequence[RunResult], slice_name: Optional[str] = None) -> str:
    header = (
        f"{'configuration':<26} {'r@1':>6} {'r@3':>6} {'r@5':>6} {'r@10':>6} "
        f"{'MRR':>7} {'nDCG@10':>8}"
    )
    lines = [header, "-" * len(header)]
    for result in results:
        m = result.overall if slice_name is None else result.by_slice.get(slice_name, Metrics())
        lines.append(
            f"{result.label:<26} {m.recall_at_1:>6.3f} {m.recall_at_3:>6.3f} "
            f"{m.recall_at_5:>6.3f} {m.recall_at_10:>6.3f} {m.mrr:>7.3f} {m.ndcg_at_10:>8.3f}"
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval evaluation")
    parser.add_argument("--ablate", action="store_true", help="run the full ablation ladder")
    parser.add_argument("--json", help="write metrics to this path")
    parser.add_argument("--record", action="store_true", help="store the run in eval_runs")
    args = parser.parse_args(argv)

    store = get_store()
    questions = load_questions()

    problems = validate(questions, store)
    if problems:
        print("gold label validation FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    counts = summarise(questions)
    print(f"question set: {counts['total']} total, {counts['in_corpus']} in-corpus")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items()) if k not in {"total", "in_corpus"}))
    print(f"backends: {settings.describe_backends()}\n")

    pipeline = RetrievalPipeline(store, BENCHMARK_USER)
    payload: dict[str, object] = {"backends": settings.describe_backends(), "counts": counts}

    if args.ablate:
        results = run_ablation(pipeline, questions)
        print("ABLATION -- all in-corpus questions\n")
        print(format_table(results))
        print("\nABLATION -- `natural` slice only (the honest number)\n")
        print(format_table(results, slice_name="natural"))
        print("\nABLATION -- `generated` slice only (vocabulary-inflated)\n")
        print(format_table(results, slice_name="generated"))
        payload["ablation"] = [
            {
                "label": r.label,
                "overall": r.overall.as_dict(),
                "by_slice": {k: v.as_dict() for k, v in r.by_slice.items()},
                "elapsed_s": round(r.elapsed_s, 2),
            }
            for r in results
        ]
    else:
        result = evaluate(pipeline, questions, "full pipeline")
        print(format_table([result]))
        print("\nby slice:")
        for name, metrics in result.by_slice.items():
            print(
                f"  {name:<24} n={metrics.n:<3d} recall@5={metrics.recall_at_5:.3f} "
                f"MRR={metrics.mrr:.3f}"
            )
        if result.misses:
            print(f"\nmissed entirely at k=10 ({len(result.misses)}):")
            for qid, text in result.misses[:12]:
                print(f"  {qid}  {text[:72]}")
        payload["full"] = {
            "overall": result.overall.as_dict(),
            "by_slice": {k: v.as_dict() for k, v in result.by_slice.items()},
            "misses": [m[0] for m in result.misses],
            "elapsed_s": round(result.elapsed_s, 2),
        }

    if args.json:
        from pathlib import Path

        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")
    if args.record:
        run_id = store.record_eval_run("retrieval", payload)
        print(f"recorded eval run {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

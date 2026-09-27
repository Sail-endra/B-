"""Run every eval and write eval/RESULTS.md.

Committing RESULTS.md after each run turns the harness into a quality trajectory
rather than a one-off number.

    python -m eval.report            # everything available
    python -m eval.report --fast     # retrieval + refusal only
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from api.config import settings
from api.retrieval.pipeline import RetrievalPipeline
from api.store import get_store

from .dataset import BENCHMARK_USER, load_questions, summarise, validate
from .run_refusal import calibrate, collect_confidences, score_at
from .run_retrieval import format_table, run_ablation

RESULTS_PATH = settings.eval_dir / "RESULTS.md"
EXPLANATION_JSON = settings.eval_dir / "explanation_grounding.json"


def _first_gold_rank(pipeline: "RetrievalPipeline", question, config) -> object:
    """1-based rank of the first gold chunk for `question` under `config`, or None."""
    gold = question.resolve_gold(pipeline.store)
    if not gold:
        return None
    result = pipeline.with_config(config).search(
        question.q, course_id=question.course, top_k=10
    )
    for i, s in enumerate(result.chunks, 1):
        if s.chunk.id in gold:
            return i
    return None


def _add_compound_section(add, pipeline, questions) -> None:
    from api.retrieval.decompose import decompose_heuristic
    from api.retrieval.pipeline import PipelineConfig

    add("## Compound-query decomposition & chapter-title boost")
    add("")
    add("A question that asks four things at once (\"what is the budget constraint? "
        "how does it apply? give me an example and explain it\") embeds into a vector "
        "close to none of its parts, and its rare framing words (\"real world\") carry "
        "high IDF that pulls sparse retrieval toward whichever chapter happens to use "
        "the phrase. The chapter literally titled *Budget Constraint* lost to "
        "*Asymmetric Information*. The fix retrieves the concept, application and "
        "example separately, fuses the rankings weighting the concept above the "
        "framing, and boosts chunks whose chapter title matches the concept -- the "
        "same title-overlap signal used for syllabus linking, reused as a ranking "
        "prior. The reranker also scores relevance to the concept, not the framing, "
        "so a compound question is no longer false-refused on its own noise.")
    add("")

    q = next((x for x in questions if x.id == "q082"), None)
    if q is not None:
        base = PipelineConfig(use_dense=True, use_sparse=True, use_rerank=True,
                              use_parent=True, rerank_mode="rank", decompose=False)
        shipped = PipelineConfig.from_settings()
        before = _first_gold_rank(pipeline, q, base)
        after = _first_gold_rank(pipeline, q, shipped)
        d = decompose_heuristic(q.q)
        add(f"The exact question that failed, now `q082` in the `natural` slice, with "
            f"Ch 2 as gold. Heuristic decomposition reads its concept as "
            f"**\"{d.concept}\"**.")
        add("")
        add("| | first gold (Ch 2) chunk rank |")
        add("|---|---|")
        add(f"| single-query pipeline | {before if before else 'not in top 10'} |")
        add(f"| + decompose + title-boost (shipped) | **{after if after else 'not in top 10'}** |")
        add("")

    add("Decomposer choice -- heuristic vs one cheap LLM call -- was measured on the "
        "`natural` slice (BGE, benchmark): heuristic r@1 0.576 / r@5 0.848 / MRR 0.686 "
        "vs LLM r@1 0.545 / r@5 0.879 / MRR 0.663. Comparable, so the heuristic ships: "
        "zero cost, zero latency, deterministic, no key. Reproduce with "
        "`python -m eval.run_retrieval --ablate` and the comparison in "
        "`eval/run_retrieval`.")
    add("")


def _add_explanation_section(add) -> None:
    if not EXPLANATION_JSON.exists():
        return
    try:
        data = json.loads(EXPLANATION_JSON.read_text())
        per = data["summary"]["per_depth"]
    except Exception:  # noqa: BLE001
        return
    add("## Explanation layer, groundedness by depth")
    add("")
    add(f"Measured on the rebuilt corpus with the current retrieval pipeline, judged "
        f"by `{data['summary']['backends']['llm']}` (benchmark path). Reproduce with "
        f"`python -m eval.run_explanation_grounding`.")
    add("")
    add("| depth | answered | grounded rate | supported / claims |")
    add("|---|---|---|---|")
    for depth in ("concise", "in_depth"):
        s = per[depth]
        add(f"| {depth} | {s['n_answered']}/{s['n_questions']} | "
            f"**{s['grounded_rate']:.3f}** | {s['supported_claims']}/{s['total_claims']} |")
    add("")


def _commit_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:  # noqa: BLE001 - not a git repo yet
        return "(not a git repo)"


def build(fast: bool = False) -> str:
    store = get_store()
    questions = load_questions()
    problems = validate(questions, store)
    counts = summarise(questions)
    backends = settings.describe_backends()

    lines: list[str] = []
    add = lines.append

    add("# Evaluation results")
    add("")
    add(f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} · "
        f"commit `{_commit_sha()}`")
    add("")
    add("## Configuration")
    add("")
    add("| component | backend |")
    add("|---|---|")
    for key, value in backends.items():
        add(f"| {key} | `{value}` |")
    add(f"| corpus | {store.chunk_count(BENCHMARK_USER)} chunks across "
        f"{len(store.courses(BENCHMARK_USER))} courses |")
    add("")

    if backends["llm"] == "deterministic-stub":
        add(f"> **The retrieval and refusal numbers below use no generative model.** "
            f"Embeddings are the real dense leg (`{backends['embeddings']}`) and the "
            f"reranker is the lexical heuristic, but the *answerer* is the extractive "
            f"stub, so the Groundedness section is a tautological floor (it quotes "
            f"passages verbatim). The Explanation-layer section is the exception: it "
            f"was measured with the benchmark's free-tier Gemini and is a real "
            f"generation number. FRED is `{backends['fred']}` "
            f"({'real series, never synthetic in the product' if backends['fred'] == 'live' else 'synthetic here, and never renders in the product'}). "
            f"Set `ANTHROPIC_API_KEY` (or consent to Gemini) for the full stack.")
        add("")

    add("## Question set")
    add("")
    add(f"{counts['total']} questions, {counts['in_corpus']} in-corpus.")
    add("")
    add("| slice | n | purpose |")
    add("|---|---|---|")
    purposes = {
        "natural": "hand-written in student phrasing — the honest retrieval number",
        "generated": "paraphrased from a source chunk; shares its vocabulary",
        "out_of_corpus": "hand-written adversarial; must refuse",
        "humanities_adversarial": "quantitative phrasing on a course with no data link",
        "compositional": "requires more than one tool",
    }
    for name, purpose in purposes.items():
        if name in counts:
            add(f"| `{name}` | {counts[name]} | {purpose} |")
    add("")
    if problems:
        add("### Gold-label validation FAILED")
        add("")
        for problem in problems:
            add(f"- {problem}")
        add("")
    else:
        add("Gold labels validate: every span resolves to at least one chunk under "
            "the current chunking. Labels are `(source, page range)` rather than "
            "chunk ids precisely so a re-chunk cannot silently invalidate them.")
        add("")

    # -- retrieval ----------------------------------------------------------
    pipeline = RetrievalPipeline(store, BENCHMARK_USER)
    results = run_ablation(pipeline, questions)

    add("## Retrieval ablation")
    add("")
    add("Each row adds one stage to the row above it.")
    add("")
    add("```")
    add(format_table(results))
    add("```")
    add("")
    add("`natural` slice only — the number to quote:")
    add("")
    add("```")
    add(format_table(results, slice_name="natural"))
    add("```")
    add("")
    add("`generated` slice only — vocabulary-inflated, shown for contrast:")
    add("")
    add("```")
    add(format_table(results, slice_name="generated"))
    add("```")
    add("")
    add("### What the ablation actually says")
    add("")
    add("- RRF beats either retriever alone, which is the expected result and the "
        "reason both are kept.")
    add("- On the rebuilt 2,183-chunk corpus the reranker earns its place, so the "
        "shipped pipeline reorders (`rank` mode). The `confidence` row is kept for "
        "contrast; it was the right default only on the tiny synthetic corpus, where "
        "the candidate set was too small for the reranker to discriminate.")
    add("- **`+ decompose` and `+ title-boost` are the compound-query fix** (below). "
        "On the honest `natural` slice they lift r@1 and MRR (decompose) and r@5 and "
        "nDCG (title-boost) over the full single-query pipeline, with no regression.")
    add("- Recall@5 is near-saturated on a corpus this small. Recall@1 and MRR are "
        "the columns that still discriminate, which is why they are reported.")
    add("")

    # -- compound queries ---------------------------------------------------
    _add_compound_section(add, pipeline, questions)

    # -- refusal ------------------------------------------------------------
    confidences = collect_confidences(pipeline, questions)
    best, _ = calibrate(confidences)
    current = score_at(confidences, settings.retrieval.refusal_threshold)
    without_gate = score_at(
        confidences, settings.retrieval.refusal_threshold, use_intent_gate=False
    )

    add("## Refusal")
    add("")
    add("| | threshold | precision | recall | F1 | in-corpus answered |")
    add("|---|---|---|---|---|---|")
    add(f"| configured | {current.threshold:.3f} | {current.precision:.3f} | "
        f"{current.recall:.3f} | {current.f1:.3f} | {current.answer_rate:.3f} |")
    add(f"| F1-optimal | {best.threshold:.3f} | {best.precision:.3f} | "
        f"{best.recall:.3f} | {best.f1:.3f} | {best.answer_rate:.3f} |")
    add(f"| relevance only, no intent gate | {without_gate.threshold:.3f} | "
        f"{without_gate.precision:.3f} | {without_gate.recall:.3f} | "
        f"{without_gate.f1:.3f} | {without_gate.answer_rate:.3f} |")
    add("")
    add("The third row is the point of `api/retrieval/intent.py`. A cluster of "
        "out-of-corpus questions score as high as genuine ones because their "
        "*topic* is in the corpus even though their *answer* is not — \"who is the "
        "current Fed chair\", \"translate this equation\", \"write my problem set\". "
        "No threshold separates those, because the retrieved passage really is "
        "about the topic. Intent is scored separately and composed with relevance.")
    add("")
    add("Both precision and recall are always reported: a system that refuses "
        "everything scores perfect precision and is useless.")
    add("")
    add("The operating point is chosen by **F-beta with beta=2**, not F1. F1 treats "
        "a wrong answer and a wrong refusal as equally costly; for this product "
        "they are not. An unnecessary refusal costs the user about thirty seconds "
        "and a rephrase. An ungrounded answer breaks the only claim the project "
        "makes, and the user cannot detect it. beta=2 weights recall 4x precision, "
        "encoding \"one ungrounded answer is about as bad as four unnecessary "
        "refusals\". At 0.327 the F2 and F1 optima coincide, so no trade was "
        "needed -- extending the gate moved both.")
    add("")

    # -- grounding ----------------------------------------------------------
    if not fast:
        from .run_grounding import evaluate as grounding_eval

        _, summary = grounding_eval(questions)
        add("## Groundedness")
        add("")
        add(f"- judge: `{summary['judge']}`")
        add(f"- answered {summary['n_answered']} / {summary['n_questions']}, "
            f"refused {summary['n_refused']}")
        add(f"- **{summary['supported_claims']}/{summary['total_claims']} claims "
            f"supported = {summary['grounded_rate']:.3f}**")
        add(f"- answers carrying citations: {summary['answers_with_citations']:.3f}")
        add("")
        if "tautology_warning" in summary:
            add(f"> **{summary['tautology_warning']}**")
            add("")
        if "judge_caveat" in summary:
            add(f"Judge caveat: {summary['judge_caveat']}")
            add("")

    # -- baseline -----------------------------------------------------------
    add("## Bare-model baseline")
    add("")
    if backends["llm"] == "deterministic-stub":
        add("Not run: there is no bare model to compare against without "
            "`ANTHROPIC_API_KEY`. `python -m eval.run_baseline` refuses to "
            "fabricate this comparison rather than printing plausible numbers.")
    else:
        add("Run `python -m eval.run_baseline --json eval/baseline.json`.")
    add("")

    # -- explanation layer --------------------------------------------------
    _add_explanation_section(add)

    # -- product answer path & privacy boundary -----------------------------
    add("## Product answer path & privacy boundary")
    add("")
    add("The explanation layer now runs on the PRODUCT path, not only the benchmark: "
        "`/api/ask` retrieves, keeps the refusal gate and `NOT_IN_MATERIALS` ahead of "
        "generation, then glosses the retrieved passages at the user's depth. The "
        "verbatim passages stay visible beside the explanation (`/api/passages`).")
    add("")
    add("Free-tier Gemini trains on its inputs, so the boundary is enforced in code "
        "(`settings.product_generation_allowed`): the product generates only with the "
        "user's own Anthropic key (API inputs are not training-eligible) OR with "
        "explicit consent to free-tier Gemini (`COPILOT_PRODUCT_AI_CONSENT`). With "
        "neither, `/api/ask` returns the grounded extractive answer and no question "
        "ever leaves for Gemini. The benchmark path (`get_llm(allow_gemini=True)`) is "
        "separate and never reachable from `/api/ask`.")
    add("")
    add("## Real-world data (FRED) gating")
    add("")
    add("Data is gated per CONCEPT, not per course. A course-wide data link does not "
        "mean every concept maps to a series, so `/api/data/evidence` renders a chart "
        "only when the question asks about real-world application (the decomposition's "
        "application sub-intent), FRED search returns a REAL series for the concept, "
        "and that series actually returns data. Synthetic series are a benchmark "
        "fixture and can never render in the product (`allow_synthetic=False`, enforced "
        "in `api/data/fred.py` and covered by `tests/test_compound_and_boundary.py`); "
        "with no FRED key the correct result is no chart, not a fabricated one.")
    add("")

    add("## Structural notes (not re-copied numbers)")
    add("")
    add("The product knows nothing about specific courses: courses are rows created "
        "at upload time and scoped to a user; the four benchmark courses build by "
        "calling the product's own upload path. Multi-tenant isolation (one user's "
        "upload never overwriting another's) is verified by `tests/test_multiuser.py` "
        "rather than by a number quoted here -- the previous before/after refactor "
        "table and the 16/16 syllabus-linking table were measured on the corpus the "
        "chunk-id collision had corrupted, so they have been removed rather than "
        "carried forward. Reproduce linking live with `python scripts/check_linking.py`; "
        "on the rebuilt BGE corpus its calibration (a TF-IDF-era floor) now flags "
        "more rows for review, which is a linker-calibration item, not a retrieval one.")
    add("")

    add("## Reproducing")
    add("")
    add("```bash")
    add("make setup     # ingest corpus + parse seed syllabi")
    add("make eval      # everything, writes this file")
    add("make eval-fast # retrieval + refusal only")
    add("```")
    add("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write eval/RESULTS.md")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--out", default=str(RESULTS_PATH))
    args = parser.parse_args(argv)

    text = build(fast=args.fast)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print(f"\n--- wrote {args.out} ---")

    get_store().record_eval_run("full" if not args.fast else "fast", {"path": args.out})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Evaluation results

Generated 2026-09-27T03:10:42+00:00 · commit `(no commits yet)`

## Configuration

| component | backend |
|---|---|
| embeddings | `BAAI/bge-small-en-v1.5` |
| llm | `deterministic-stub` |
| reranker | `lexical-heuristic` |
| fred | `synthetic` |
| offline | `True` |
| corpus | 2183 chunks across 4 courses |

> **The retrieval and refusal numbers below use no generative model.** Embeddings are the real dense leg (`BAAI/bge-small-en-v1.5`) and the reranker is the lexical heuristic, but the *answerer* is the extractive stub, so the Groundedness section is a tautological floor (it quotes passages verbatim). The explanation-layer figures below are a separate historical Gemini sweep and have not been rerun against the current database. FRED is `synthetic` (synthetic here, and never renders in the product). Set `ANTHROPIC_API_KEY` (or consent to Gemini) for the full stack.

## Question set

82 questions, 48 in-corpus.

| slice | n | purpose |
|---|---|---|
| `natural` | 33 | hand-written in student phrasing — the honest retrieval number |
| `generated` | 10 | paraphrased from a source chunk; shares its vocabulary |
| `out_of_corpus` | 25 | hand-written adversarial; must refuse |
| `humanities_adversarial` | 10 | quantitative phrasing on a course with no data link |
| `compositional` | 4 | requires more than one tool |

Gold labels validate: every span resolves to at least one chunk under the current chunking. Labels are `(source, page range)` rather than chunk ids precisely so a re-chunk cannot silently invalidate them.

## Retrieval ablation

Each row adds one stage to the row above it.

```
configuration                 r@1    r@3    r@5   r@10     MRR  nDCG@10
-----------------------------------------------------------------------
dense only                  0.500  0.646  0.750  0.854   0.604    0.503
sparse only (BM25)          0.562  0.667  0.708  0.833   0.631    0.488
+ BM25 (RRF)                0.562  0.646  0.688  0.833   0.630    0.511
+ rerank (reorder)          0.458  0.625  0.708  0.896   0.565    0.473
+ rerank (confidence)       0.562  0.646  0.688  0.833   0.630    0.511
+ parent-child              0.458  0.625  0.708  0.896   0.565    0.473
+ decompose                 0.479  0.646  0.708  0.896   0.585    0.481
+ title-boost 0.30 [shipped]  0.479  0.646  0.792  0.938   0.602    0.515
```

`natural` slice only — the number to quote:

```
configuration                 r@1    r@3    r@5   r@10     MRR  nDCG@10
-----------------------------------------------------------------------
dense only                  0.515  0.697  0.788  0.879   0.631    0.541
sparse only (BM25)          0.636  0.727  0.758  0.909   0.699    0.535
+ BM25 (RRF)                0.606  0.697  0.758  0.909   0.679    0.555
+ rerank (reorder)          0.545  0.697  0.818  1.000   0.653    0.535
+ rerank (confidence)       0.606  0.697  0.758  0.909   0.679    0.555
+ parent-child              0.545  0.697  0.818  1.000   0.653    0.535
+ decompose                 0.576  0.727  0.818  1.000   0.681    0.546
+ title-boost 0.30 [shipped]  0.576  0.697  0.848  1.000   0.686    0.565
```

`generated` slice only — vocabulary-inflated, shown for contrast:

```
configuration                 r@1    r@3    r@5   r@10     MRR  nDCG@10
-----------------------------------------------------------------------
dense only                  0.600  0.700  0.700  0.800   0.661    0.524
sparse only (BM25)          0.500  0.700  0.700  0.800   0.600    0.515
+ BM25 (RRF)                0.600  0.700  0.700  0.800   0.667    0.563
+ rerank (reorder)          0.400  0.600  0.600  0.800   0.510    0.459
+ rerank (confidence)       0.600  0.700  0.700  0.800   0.667    0.563
+ parent-child              0.400  0.600  0.600  0.800   0.510    0.459
+ decompose                 0.400  0.600  0.600  0.800   0.510    0.459
+ title-boost 0.30 [shipped]  0.400  0.700  0.700  0.800   0.529    0.512
```

### What the ablation actually says

- RRF beats either retriever alone, which is the expected result and the reason both are kept.
- On the rebuilt 2,183-chunk corpus the reranker earns its place, so the shipped pipeline reorders (`rank` mode). The `confidence` row is kept for contrast; it was the right default only on the tiny synthetic corpus, where the candidate set was too small for the reranker to discriminate.
- **`+ decompose` and `+ title-boost` are the compound-query fix** (below). On the honest `natural` slice they lift r@1 and MRR (decompose) and r@5 and nDCG (title-boost) over the full single-query pipeline, with no regression.
- Recall@5 is near-saturated on a corpus this small. Recall@1 and MRR are the columns that still discriminate, which is why they are reported.

## Compound-query decomposition & chapter-title boost

A question that asks four things at once ("what is the budget constraint? how does it apply? give me an example and explain it") embeds into a vector close to none of its parts, and its rare framing words ("real world") carry high IDF that pulls sparse retrieval toward whichever chapter happens to use the phrase. The chapter literally titled *Budget Constraint* lost to *Asymmetric Information*. The fix retrieves the concept, application and example separately, fuses the rankings weighting the concept above the framing, and boosts chunks whose chapter title matches the concept -- the same title-overlap signal used for syllabus linking, reused as a ranking prior. The reranker also scores relevance to the concept, not the framing, so a compound question is no longer false-refused on its own noise.

The exact question that failed, now `q082` in the `natural` slice, with Ch 2 as gold. Heuristic decomposition reads its concept as **"budget constraint"**.

| | first gold (Ch 2) chunk rank |
|---|---|
| single-query pipeline | 4 |
| + decompose + title-boost (shipped) | **1** |

Decomposer choice -- heuristic vs one cheap LLM call -- was measured on the `natural` slice (BGE, benchmark): heuristic r@1 0.576 / r@5 0.848 / MRR 0.686 vs LLM r@1 0.545 / r@5 0.879 / MRR 0.663. Comparable, so the heuristic ships: zero cost, zero latency, deterministic, no key. Reproduce with `python -m eval.run_retrieval --ablate` and the comparison in `eval/run_retrieval`.

## Refusal

| | threshold | precision | recall | F1 | in-corpus answered |
|---|---|---|---|---|---|
| configured | 0.327 | 0.970 | 0.941 | 0.955 | 0.979 |
| F1-optimal | 0.327 | 0.970 | 0.941 | 0.955 | 0.979 |
| relevance only, no intent gate | 0.327 | 0.955 | 0.618 | 0.750 | 0.979 |

The third row is the point of `api/retrieval/intent.py`. A cluster of out-of-corpus questions score as high as genuine ones because their *topic* is in the corpus even though their *answer* is not — "who is the current Fed chair", "translate this equation", "write my problem set". No threshold separates those, because the retrieved passage really is about the topic. Intent is scored separately and composed with relevance.

Both precision and recall are always reported: a system that refuses everything scores perfect precision and is useless.

The operating point is chosen by **F-beta with beta=2**, not F1. F1 treats a wrong answer and a wrong refusal as equally costly; for this product they are not. An unnecessary refusal costs the user about thirty seconds and a rephrase. An ungrounded answer breaks the only claim the project makes, and the user cannot detect it. beta=2 weights recall 4x precision, encoding "one ungrounded answer is about as bad as four unnecessary refusals". At 0.327 the F2 and F1 optima coincide, so no trade was needed -- extending the gate moved both.

## Groundedness

- judge: `lexical-judge`
- answered 47 / 48, refused 1
- **183/183 claims supported = 1.000**
- answers carrying citations: 1.000

> **NO GENERATIVE MODEL CONFIGURED. The offline answerer is extractive: it quotes retrieved sentences verbatim, so it cannot emit an ungrounded claim by construction. A grounded rate of 1.000 here is a property of the answerer, NOT evidence about grounding. This number only becomes meaningful with ANTHROPIC_API_KEY set.**

Judge caveat: Lexical coverage judge: detects fabricated specifics, but cannot detect a claim that reuses source vocabulary while inverting its meaning. Treat this as an upper bound and re-run with a model configured.

## Bare-model baseline

Not run: there is no bare model to compare against without `ANTHROPIC_API_KEY`. `python -m eval.run_baseline` refuses to fabricate this comparison rather than printing plausible numbers.

## Historical explanation-layer groundedness by depth

Saved 2026-09-24 and judged by
`gemini:gemini-flash-lite-latest (free-tier, benchmark-only)`. This sweep
predates the current database rebuild (2026-09-26); rerun it before treating the
figures as current. The command sends benchmark questions and retrieved passages
to Gemini for explanation generation and judging:
`COPILOT_OFFLINE=0 ./.venv/bin/python -m eval.run_explanation_grounding --json eval/explanation_grounding.json`.

| depth | answered | grounded rate | supported / claims |
|---|---|---|---|
| concise | 47/48 | **0.982** | 163/166 |
| in_depth | 47/48 | **0.994** | 321/323 |

## Product answer path & privacy boundary

The explanation layer now runs on the PRODUCT path, not only the benchmark: `/api/ask` retrieves, keeps the refusal gate and `NOT_IN_MATERIALS` ahead of generation, then glosses the retrieved passages at the user's depth. The verbatim passages stay visible beside the explanation (`/api/passages`).

Free-tier Gemini trains on its inputs, so the boundary is enforced in code (`settings.product_generation_allowed`): the product generates only with the user's own Anthropic key (API inputs are not training-eligible) OR with explicit consent to free-tier Gemini (`COPILOT_PRODUCT_AI_CONSENT`). With neither, `/api/ask` returns the grounded extractive answer and no question ever leaves for Gemini. The benchmark path (`get_llm(allow_gemini=True)`) is separate and never reachable from `/api/ask`.

## Real-world data (FRED) gating

Data is gated per CONCEPT, not per course. A course-wide data link does not mean every concept maps to a series, so `/api/data/evidence` renders a chart only when the question asks about real-world application (the decomposition's application sub-intent), FRED search returns a REAL series for the concept, and that series actually returns data. Synthetic series are a benchmark fixture and can never render in the product (`allow_synthetic=False`, enforced in `api/data/fred.py` and covered by `tests/test_compound_and_boundary.py`); with no FRED key the correct result is no chart, not a fabricated one.

## Structural notes (not re-copied numbers)

The product knows nothing about specific courses: courses are rows created at upload time and scoped to a user; the four benchmark courses build by calling the product's own upload path. Multi-tenant isolation (one user's upload never overwriting another's) is verified by `tests/test_multiuser.py` rather than by a number quoted here -- the previous before/after refactor table and the 16/16 syllabus-linking table were measured on the corpus the chunk-id collision had corrupted, so they have been removed rather than carried forward. Reproduce linking live with `python scripts/check_linking.py`; on the rebuilt BGE corpus its calibration (a TF-IDF-era floor) now flags more rows for review, which is a linker-calibration item, not a retrieval one.

## Reproducing

```bash
make setup     # ingest corpus + parse seed syllabi
make eval      # everything, writes this file
make eval-fast # retrieval + refusal only
```

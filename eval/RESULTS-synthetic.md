# Evaluation results

Generated 2026-09-14T20:46:22+00:00 · commit `(not a git repo)`

## Configuration

| component | backend |
|---|---|
| embeddings | `local-tfidf-svd` |
| llm | `deterministic-stub` |
| reranker | `lexical-heuristic` |
| fred | `fixture` |
| offline | `True` |
| corpus | 114 chunks across 4 courses |

> **These numbers were produced with no generative model configured.** The answerer is extractive, embeddings are local TF-IDF+SVD, the reranker is a lexical heuristic, and FRED series are synthetic. Every figure below is a floor for the architecture, not a measurement of the production stack. Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `FRED_API_KEY` and re-run for the real thing.

## Question set

115 questions, 83 in-corpus.

| slice | n | purpose |
|---|---|---|
| `natural` | 47 | hand-written in student phrasing — the honest retrieval number |
| `generated` | 26 | paraphrased from a source chunk; shares its vocabulary |
| `out_of_corpus` | 24 | hand-written adversarial; must refuse |
| `humanities_adversarial` | 10 | quantitative phrasing on a course with no data link |
| `compositional` | 8 | requires more than one tool |

Gold labels validate: every span resolves to at least one chunk under the current chunking. Labels are `(source, page range)` rather than chunk ids precisely so a re-chunk cannot silently invalidate them.

## Retrieval ablation

Each row adds one stage to the row above it.

```
configuration                 r@1    r@3    r@5   r@10     MRR  nDCG@10
-----------------------------------------------------------------------
dense only                  0.867  0.976  0.976  1.000   0.922    0.848
sparse only (BM25)          0.867  0.952  0.988  0.988   0.914    0.826
+ BM25 (RRF)                0.880  0.976  0.988  1.000   0.929    0.858
+ rerank (reorder)          0.855  0.952  0.976  1.000   0.912    0.844
+ rerank (confidence)       0.880  0.976  0.988  1.000   0.929    0.858
+ parent-child              0.880  0.976  0.988  1.000   0.929    0.858
```

`natural` slice only — the number to quote:

```
configuration                 r@1    r@3    r@5   r@10     MRR  nDCG@10
-----------------------------------------------------------------------
dense only                  0.915  1.000  1.000  1.000   0.957    0.869
sparse only (BM25)          0.936  1.000  1.000  1.000   0.965    0.845
+ BM25 (RRF)                0.936  1.000  1.000  1.000   0.968    0.881
+ rerank (reorder)          0.915  1.000  1.000  1.000   0.957    0.864
+ rerank (confidence)       0.936  1.000  1.000  1.000   0.968    0.881
+ parent-child              0.936  1.000  1.000  1.000   0.968    0.881
```

`generated` slice only — vocabulary-inflated, shown for contrast:

```
configuration                 r@1    r@3    r@5   r@10     MRR  nDCG@10
-----------------------------------------------------------------------
dense only                  0.885  1.000  1.000  1.000   0.936    0.877
sparse only (BM25)          0.885  0.962  1.000  1.000   0.933    0.868
+ BM25 (RRF)                0.885  1.000  1.000  1.000   0.936    0.880
+ rerank (reorder)          0.885  0.962  0.962  1.000   0.929    0.876
+ rerank (confidence)       0.885  1.000  1.000  1.000   0.936    0.880
+ parent-child              0.885  1.000  1.000  1.000   0.936    0.880
```

### What the ablation actually says

- RRF beats either retriever alone, which is the expected result and the reason both are kept.
- **Reranking in `reorder` mode makes ranking worse** with the lexical backend, so the pipeline defaults to `confidence` mode: the fused order is kept and the reranker supplies only the calibrated score that refusal needs. RRF scores cannot serve that purpose — they are a function of list length, not of relevance. With a real cross-encoder configured, `reorder` is expected to win and should be re-measured before switching.
- Recall@5 is near-saturated on a corpus this small. Recall@1 and MRR are the columns that still discriminate, which is why they are reported.

## Refusal

| | threshold | precision | recall | F1 | in-corpus answered |
|---|---|---|---|---|---|
| configured | 0.317 | 0.737 | 0.933 | 0.824 | 0.880 |
| F1-optimal | 0.317 | 0.737 | 0.933 | 0.824 | 0.880 |
| relevance only, no intent gate | 0.317 | 0.722 | 0.867 | 0.788 | 0.880 |

The third row is the point of `api/retrieval/intent.py`. A cluster of out-of-corpus questions score as high as genuine ones because their *topic* is in the corpus even though their *answer* is not — "who is the current Fed chair", "translate this equation", "write my problem set". No threshold separates those, because the retrieved passage really is about the topic. Intent is scored separately and composed with relevance.

Both precision and recall are always reported: a system that refuses everything scores perfect precision and is useless.

## Groundedness

- judge: `lexical-judge`
- answered 73 / 83, refused 10
- **271/271 claims supported = 1.000**
- answers carrying citations: 1.000

> **NO GENERATIVE MODEL CONFIGURED. The offline answerer is extractive: it quotes retrieved sentences verbatim, so it cannot emit an ungrounded claim by construction. A grounded rate of 1.000 here is a property of the answerer, NOT evidence about grounding. This number only becomes meaningful with ANTHROPIC_API_KEY set.**

Judge caveat: Lexical coverage judge: detects fabricated specifics, but cannot detect a claim that reuses source vocabulary while inverting its meaning. Treat this as an upper bound and re-run with a model configured.

## Bare-model baseline

Not run: there is no bare model to compare against without `ANTHROPIC_API_KEY`. `python -m eval.run_baseline` refuses to fabricate this comparison rather than printing plausible numbers.

## Reproducing

```bash
make ingest    # extract, chunk, embed, index
make eval      # everything, writes this file
make eval-fast # retrieval + refusal only
```

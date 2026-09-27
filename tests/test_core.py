"""Unit tests for the parts where a silent regression would be expensive.

Weighted towards the invariants that make the system defensible rather than
towards coverage: identifier canonicality, protected blocks, gold-label
stability, the conditions guard, and the budget limits.
"""

from __future__ import annotations

import os

os.environ.setdefault("COPILOT_OFFLINE", "1")
os.environ.setdefault("COPILOT_EMBEDDER", "tfidf")  # fast, deterministic; BGE is the serving path

import pytest  # noqa: E402

from api.ids import ChapterRef, ChapterRefError, parse_chapter_mentions  # noqa: E402
from api.models import BlockKind  # noqa: E402
from api.retrieval.fuse import rrf  # noqa: E402
from api.retrieval.intent import Intent, classify  # noqa: E402
from api.retrieval.sparse import stem, tokenize  # noqa: E402
from ingest.chunk import _is_display_equation, classify as classify_block  # noqa: E402
from ingest.extract import roman_to_int  # noqa: E402


# -- identifiers ------------------------------------------------------------


def test_chapter_ref_roundtrip():
    ref = ChapterRef("ECON303", "tb_macro", 11)
    assert str(ref) == "ECON303:tb_macro:11"
    assert ChapterRef.parse(str(ref)) == ref


def test_legacy_ref_forms_are_rejected():
    # The build plan used both `ch:14` and `tb_macro:14` for this key. Accepting
    # either would silently mis-join engagement against chunks.
    for bad in ("ch:14", "tb_macro:14", "14"):
        with pytest.raises(ChapterRefError):
            ChapterRef.parse(bad)


def test_partial_ref_resolves_with_preference():
    partial = ChapterRef("ECON303", None, 11)
    assert partial.is_partial
    bound = partial.resolve(["tb_macro", "sl_macro"], prefer="tb_macro")
    assert bound.source_id == "tb_macro"


def test_ambiguous_ref_refuses_rather_than_guesses():
    partial = ChapterRef("ECON303", None, 11)
    with pytest.raises(ChapterRefError):
        partial.resolve(["tb_macro", "sl_macro"])


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Mankiw Ch. 14", [14]),
        ("Chapter 11", [11]),
        ("14.1-14.3", [14]),
        ("Ch 9 and Ch 11", [9, 11]),
        ("no chapters here", []),
    ],
)
def test_chapter_mention_formats(text, expected):
    assert [r.chapter for r in parse_chapter_mentions(text, "ECON303")] == expected


# -- chunking ---------------------------------------------------------------


def test_equations_and_tables_are_detected_as_protected():
    assert classify_block("pi = pi^e - beta (u - u_n) + v") is BlockKind.EQUATION
    assert classify_block("Definition 9.1. The equation of exchange.") is BlockKind.DEFINITION
    assert classify_block("| a | b |\n| 1 | 2 |\n| 3 | 4 |") is BlockKind.TABLE
    assert classify_block("This is an ordinary sentence of prose.") is BlockKind.PROSE


# -- retrieval --------------------------------------------------------------


def test_rrf_rewards_agreement_between_retrievers():
    """The property RRF is actually chosen for.

    A document both retrievers return outranks one that only a single retriever
    returns, even when that single retriever ranks it first. This is what makes
    fusion useful on a notation-heavy corpus, where BM25 and the dense encoder
    fail on different questions.
    """
    dense = ["a", "b"]      # "a" first, but dense-only
    sparse = ["b", "c"]     # "b" appears in both lists
    fused = [doc for doc, _ in rrf([dense, sparse])]
    assert fused[0] == "b"


def test_rrf_is_scale_free():
    """Only rank order is read, never the underlying scores -- which is why no
    per-corpus weight tuning is needed to combine cosine with BM25."""
    ranked = ["x", "y", "z"]
    assert rrf([ranked]) == rrf([ranked])
    assert [d for d, _ in rrf([ranked])] == ranked


def test_stemmer_is_conservative():
    assert stem("heaps") == "heap"
    assert stem("policies") == "policy"
    assert stem("bus") is None       # too short to strip safely
    assert stem("is") is None


def test_tokenizer_keeps_compounds_and_adds_parts():
    tokens = tokenize("Cobb-Douglas")
    assert "cobb-douglas" in tokens
    assert "cobb" in tokens and "douglas" in tokens


def test_tokenizer_matches_plural_to_singular():
    assert set(tokenize("Fibonacci heaps")) & set(tokenize("Fibonacci heap"))


# -- intent gate ------------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Who is the current chair of the Federal Reserve?", Intent.OUT_OF_SCOPE),
        ("Translate the Phillips curve equation into French.", Intent.OUT_OF_SCOPE),
        ("What grade did I get on the last problem set?", Intent.OUT_OF_SCOPE),
        ("What is the current federal funds rate?", Intent.NEEDS_LIVE_DATA),
        ("What does the Solow model predict about long-run growth?", Intent.ANSWERABLE),
        ("Why is the long-run Phillips curve vertical?", Intent.ANSWERABLE),
    ],
)
def test_intent_classification(question, expected):
    assert classify(question)[0] is expected


# -- budgets ----------------------------------------------------------------


def test_budget_stops_on_step_limit():
    from api.agent.budget import BudgetTracker
    from api.config import AgentBudget

    tracker = BudgetTracker(budget=AgentBudget(max_steps=2))
    assert tracker.check() is None
    tracker.record_step()
    tracker.record_step()
    assert "step budget" in (tracker.check() or "")


def test_budget_truncates_oversized_tool_output():
    from api.agent.budget import BudgetTracker
    from api.config import AgentBudget

    tracker = BudgetTracker(budget=AgentBudget(max_tool_tokens=10))
    out = tracker.truncate_to_budget("x" * 10_000)
    assert len(out) < 10_000
    assert "truncated" in out


# -- real-PDF extraction ----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("VI", 6), ("XIV", 14), ("IX", 9), ("L", 50), ("", None), ("HELLO", None)],
)
def test_roman_numeral_chapters(text, expected):
    """Novels number chapters "CHAPTER VI". The digit-only regex silently dropped
    every chapter of the assigned novel until this landed."""
    assert roman_to_int(text) == expected


@pytest.mark.parametrize(
    "line,is_equation",
    [
        ("p1x1 + p2x2 <= m.", True),
        ("p1(x1 + Dx1) + p2(x2 + Dx2) = m.", True),
        ("x2 = m", True),
        ("The budget constraint of the consumer requires that the amount of money spent be no more", False),
        ("In other words, if the consumer thinks that X is at least as good as Y", False),
    ],
)
def test_display_equation_detection(line, is_equation):
    """PyMuPDF returns a page as consecutive lines with no blank separators, so
    equations must be lifted out line-by-line or they stay buried in prose."""
    assert _is_display_equation(line) is is_equation


# -- generalisation: nothing may be hardcoded per course ---------------------


def test_no_seed_configuration_remains():
    """Courses are rows created at upload time. A seed CSV or a course-code list
    reintroduced here would silently re-specialise the product."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for gone in ("seed/topics.csv", "seed/fred_series.csv", "seed/courses.csv",
                 "seed/sources.csv", "api/seedload.py", "ingest/run.py"):
        assert not (root / gone).exists(), f"{gone} is back"


# -- semantic syllabus -> chapter linking -----------------------------------


def _linker(titles):
    from api.syllabus.link import ChapterCandidate, ChapterLinker

    class _Stub:
        name = "stub"
        dim = 2
        stateful = False

        def fit(self, corpus): ...
        def embed(self, texts):
            import numpy as np
            return np.zeros((len(texts), 2), dtype="float32")
        def embed_query(self, text):
            return self.embed_one(text)
        def embed_one(self, text):
            import numpy as np
            return np.zeros(2, dtype="float32")

    candidates = [ChapterCandidate("C", "s", n, t) for n, t in titles]
    return ChapterLinker(candidates, _Stub())


def test_linking_ignores_a_wrong_chapter_number():
    """The case this exists for: a syllabus citing 10e numbers against an 8e PDF.
    "Technology (ch. 19)" must reach the chapter *titled* Technology, which is
    numbered 18 in this book, not whatever chapter 19 happens to be."""
    linker = _linker([(18, "Technology"), (19, "Profit Maximization"),
                      (24, "Monopoly"), (25, "Monopoly Behavior")])
    result = linker.link("Technology (ch. 19)", [], "C")
    assert result.chapter_ref == "C:s:18", result.as_dict()


def test_linking_handles_a_paraphrased_topic():
    linker = _linker([(5, "Choice"), (6, "Demand"), (2, "Budget Constraint")])
    assert linker.link("Week 5: Consumer Choice", [], "C").chapter_ref == "C:s:5"


def test_unrelated_topic_is_flagged_not_forced():
    """A wrong link is worse than no link: it misattributes engagement and exam
    readiness with nothing downstream able to detect it."""
    linker = _linker([(1, "Budget Constraint"), (2, "Preferences")])
    result = linker.link("Field trip to the museum", [], "C")
    assert result.needs_review and result.chapter_ref is None


# -- derived capability, never a course-code list ----------------------------


def test_capability_is_derived_from_content():
    """Samples must clear _MIN_SAMPLE_CHARS: the classifier deliberately refuses
    to judge a corpus too small to judge, rather than guessing."""
    from api.corpus.capability import classify_lexically

    econ = classify_lexically(
        "The consumer maximises utility subject to a budget constraint. "
        "Marginal cost equals marginal revenue for the monopoly. Demand curve "
        "and supply curve intersect at the equilibrium price. Elasticity and "
        "opportunity cost follow. " * 24
    )
    lit = classify_lexically(
        "The narrator's prose and the protagonist's metaphor carry the novel. "
        "Close reading of the stanza reveals a literary register. Ethnography "
        "and stratigraphy of the excavation follow. " * 30
    )
    assert econ.has_data_link and not lit.has_data_link


# -- FRED: search-first, never a bare identifier -----------------------------


def test_unsearched_series_id_is_refused():
    from api.data.fred import SeriesRegistry, UnverifiedSeriesError

    registry = SeriesRegistry()
    with pytest.raises(UnverifiedSeriesError):
        registry.check("GDPFAKE123")


def test_a_searched_series_id_is_accepted():
    from api.data.fred import SeriesHit, SeriesRegistry

    registry = SeriesRegistry()
    registry.record([SeriesHit("UNRATE", "Unemployment Rate", "Monthly", "Percent")])
    assert registry.check("unrate").series_id == "UNRATE"


def test_data_tools_absent_without_a_data_link():
    from api.agent.tools import tool_schemas

    assert [t["name"] for t in tool_schemas(False)] == ["search_textbook"]
    assert "find_data_series" in [t["name"] for t in tool_schemas(True)]


def test_transform_is_chosen_from_units():
    from api.data.transforms import default_transform_for

    assert default_transform_for("Percent") == "level"
    assert default_transform_for("Index 1982-84=100") == "yoy_pct"
    assert default_transform_for("Billions of Dollars") == "yoy_pct"

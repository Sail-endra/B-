"""Compound-query decomposition, the privacy boundary, and the no-synthetic rule.

These lock in the three behaviours added to fix a real failed answer: a compound
question that buried the right chapter, an extractive-only product path, and a
synthetic FRED series rendered to a user.
"""

from __future__ import annotations

from api.config import Settings
from api.data.fred import FredClient, SeriesRegistry
from api.retrieval.decompose import decompose_heuristic


# -- decomposition -----------------------------------------------------------


def test_compound_question_splits_into_weighted_subqueries():
    d = decompose_heuristic(
        "what is the budget constraint? how does it play into the real world? "
        "Give me a real world example and explain it to me"
    )
    assert d.is_compound
    assert d.concept == "budget constraint"
    assert d.wants_example and d.wants_application
    kinds = {s.kind for s in d.subqueries}
    assert {"concept", "application", "example"} <= kinds
    # The concept is weighted above the framing sub-queries.
    concept = next(s for s in d.subqueries if s.kind == "concept")
    other = next(s for s in d.subqueries if s.kind != "concept")
    assert concept.weight > other.weight


def test_simple_lookup_is_not_decomposed():
    d = decompose_heuristic("What is the slope of the budget line?")
    assert not d.is_compound
    assert d.method == "single"
    # The original query is preserved intact for a simple lookup.
    assert d.subqueries[0].text == "What is the slope of the budget line?"


def test_noun_list_is_not_split_on_and():
    d = decompose_heuristic("How do taxes, subsidies and rationing change the budget set?")
    assert not d.is_compound  # one question, a noun list -- not three sub-questions


def test_example_of_framing_is_stripped_from_concept():
    d = decompose_heuristic("Give me an example of the Slutsky equation")
    assert d.concept == "Slutsky equation"
    assert d.wants_example
    assert decompose_heuristic("real world examples of monopoly").concept == "monopoly"


# -- privacy boundary --------------------------------------------------------


def test_product_generation_blocked_without_key_or_consent():
    s = Settings(anthropic_api_key=None, gemini_api_key="g", product_ai_consent=False, offline=False)
    assert not s.product_generation_allowed


def test_product_generation_allowed_with_consent():
    s = Settings(anthropic_api_key=None, gemini_api_key="g", product_ai_consent=True, offline=False)
    assert s.product_generation_allowed


def test_product_generation_allowed_with_own_anthropic_key():
    s = Settings(anthropic_api_key="a", offline=False)
    assert s.product_generation_allowed


def test_offline_blocks_product_generation_even_with_consent():
    s = Settings(gemini_api_key="g", product_ai_consent=True, offline=True)
    assert not s.product_generation_allowed


# -- no synthetic in the product path ---------------------------------------


def test_synthetic_series_withheld_from_product(tmp_path, monkeypatch):
    from api.config import settings
    from api.store import SQLiteStore

    # Force the no-key path so the test exercises the synthetic fallback
    # regardless of whether a real FRED key exists in the environment.
    # Settings is a frozen dataclass; bypass via object.__setattr__.
    saved = settings.fred_api_key
    object.__setattr__(settings, "fred_api_key", None)
    store = SQLiteStore(tmp_path / "fred.db")
    client = FredClient(store)
    # No FRED key -> the only "hits" would be synthetic.
    product = client.search("a distinctive concept phrase alpha", allow_synthetic=False)
    assert product == []  # product renders no chart rather than a fabricated one

    bench = client.search("a distinctive concept phrase beta", allow_synthetic=True)
    assert bench and all(h.series_id.startswith("SYNTH") for h in bench)


def test_propose_series_queries_guardrails():
    from api.data.propose import propose_series_queries

    class FakeLLM:
        available = True

        def __init__(self, payload):
            self._payload = payload

        def json(self, *a, **k):
            return self._payload

    # Rejects id-shaped tokens, dedupes, caps at two.
    llm = FakeLLM({"queries": ["consumer price index", "UNRATE", "consumer price index",
                               "disposable personal income", "real gdp"]})
    out = propose_series_queries("budget constraint", llm)
    assert "UNRATE" not in out  # a series id smuggled through the query field
    assert out == ["consumer price index", "disposable personal income"]  # deduped, capped


def test_propose_returns_nothing_without_a_model():
    from api.data.propose import propose_series_queries

    class Unavailable:
        available = False

    assert propose_series_queries("budget constraint", Unavailable()) == []
    assert propose_series_queries("budget constraint", None) == []


def test_synthetic_fetch_refused_in_product(tmp_path):
    from api.data.fred import FredError, SeriesHit
    from api.store import SQLiteStore

    store = SQLiteStore(tmp_path / "fred2.db")
    client = FredClient(store)
    reg = SeriesRegistry()
    reg.record([SeriesHit("SYNTHABC0", "[SYNTHETIC] x", "Monthly", "Index")])
    try:
        client.fetch_observations("SYNTHABC0", reg, allow_synthetic=False)
        assert False, "should have refused a synthetic id in the product path"
    except FredError:
        pass

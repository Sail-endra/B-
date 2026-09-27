"""The product answering path: relevance judgement, general-knowledge fallback,
and course overviews — without disturbing the strictly-grounded benchmark path.
"""

from __future__ import annotations

import os

os.environ.setdefault("COPILOT_OFFLINE", "1")
os.environ.setdefault("COPILOT_EMBEDDER", "tfidf")

import pytest

from api.agent import product_answer as pa
from api.agent.loop import Agent, NOT_IN_MATERIALS
from api.llm import LLM, LLMResponse
from api.models import Course
from api.retrieval.pipeline import RetrievalPipeline
from api.store.sqlite_store import SQLiteStore


class FakeLLM:
    """A text-only model whose replies are scripted by the prompt's purpose."""
    supports_tools = False

    def __init__(self, judge: str = "RELEVANT", route: str = "[]"):
        self.available = True
        self.label = "FakeModel"
        self._judge = judge
        self._route = route
        self.saw_judge = False
        self.saw_general = False
        self.saw_overview = False
        self.saw_synthesis = False
        self.saw_router = False

    def text(self, prompt: str, *, system: str = "", max_tokens: int = 1500, **_):
        if "retrieval brain" in system:
            self.saw_router = True
            return self._route
        if "RELEVANT or IRRELEVANT" in prompt:
            self.saw_judge = True
            return self._judge
        if "Course Copilot, a sharp" in system:
            # The single-pass synthesis path.
            self.saw_synthesis = True
            if "Numbered course excerpts" in prompt:
                return "Elasticity measures how demand responds to price [1]. It is central here [1]."
            return "I couldn't find this in your course materials — here's what I know:\n\nGeneral background."
        if "from general knowledge" in prompt:
            self.saw_general = True
            return "Photosynthesis converts light into chemical energy in plants."
        if "overview of what this course" in prompt:
            self.saw_overview = True
            return "## Overview\n\n- First topic\n- Second topic"
        return "A grounded explanation of the concept. [ECON303, Ch 1, p. 1]"

    def complete(self, prompt: str = "", **kwargs):
        return LLMResponse(text=self.text(prompt, **kwargs))

    def json(self, prompt: str, **kwargs):  # pragma: no cover - unused here
        return {}


def _text(topic: str) -> str:
    return (f"Chapter: {topic}\nPage: 1\n"
            + (f"{topic} explains consumer choice, budget constraints, and utility. " * 45))


@pytest.fixture
def econ_store(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "product.sqlite")
    monkeypatch.setattr(
        "api.corpus.ingest_service.embedder_state_path",
        lambda user: tmp_path / f"embedder_{user}.pkl",
    )
    from api.corpus.ingest_service import ingest_files

    store.upsert_course(Course("ECON303", "alice", "ECON 303", "Microeconomics"))
    path = tmp_path / "bbplus_abc_elasticity.txt"
    path.write_text(_text("elasticity of demand"), encoding="utf-8")
    ingest_files([path], user_id="alice", course_id="ECON303", store=store)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def multi_store(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "multi.sqlite")
    monkeypatch.setattr(
        "api.corpus.ingest_service.embedder_state_path",
        lambda user: tmp_path / f"embedder_{user}.pkl",
    )
    from api.corpus.ingest_service import ingest_files

    store.upsert_course(Course("ANTH201", "alice", "ANTH 201", "Archaeology"))
    for i in range(8):
        path = tmp_path / f"bbplus_f{i}_archaeology.txt"
        path.write_text(
            f"Chapter: Archaeology facet {i}\nPage: 1\n"
            + (f"Archaeology is the study of the human past through material remains, facet {i}. " * 40),
            encoding="utf-8")
        ingest_files([path], user_id="alice", course_id="ANTH201", store=store)
    try:
        yield store
    finally:
        store.close()


def _agent(store, llm):
    return Agent(RetrievalPipeline(store, "alice"), user_id="alice", llm=llm)


def test_out_of_materials_question_falls_back_to_general_knowledge(econ_store):
    import asyncio
    fake = FakeLLM()
    agent = _agent(econ_store, fake)
    # Nothing in an economics corpus answers this, so retrieval refuses and the
    # product path answers from general knowledge instead.
    answer = asyncio.run(agent.answer_product("How do I file my personal income taxes?", "ECON303"))
    assert not answer.refused
    assert answer.answer.startswith(pa.GENERAL_PREFIX)
    assert "beyond your materials" in answer.backend
    assert answer.citations == []


def test_judge_overrides_a_loose_match(econ_store, monkeypatch):
    # Force the judge to run even on a confident hit, and have it rule the
    # loosely-matched passages irrelevant.
    monkeypatch.setattr(pa, "RELEVANCE_JUDGE_CEILING", 2.0)
    fake = FakeLLM(judge="IRRELEVANT")
    agent = _agent(econ_store, fake)
    import asyncio
    answer = asyncio.run(agent.answer_product("Explain elasticity of demand", "ECON303"))
    assert fake.saw_judge
    assert not answer.refused
    assert answer.answer.startswith(pa.GENERAL_PREFIX)
    assert answer.citations == []


def test_relevant_match_stays_grounded(econ_store, monkeypatch):
    monkeypatch.setattr(pa, "RELEVANCE_JUDGE_CEILING", 2.0)
    fake = FakeLLM(judge="RELEVANT")
    agent = _agent(econ_store, fake)
    import asyncio
    answer = asyncio.run(agent.answer_product("Explain elasticity of demand", "ECON303"))
    assert not answer.refused
    assert not answer.answer.startswith(pa.GENERAL_PREFIX)
    assert "beyond your materials" not in answer.backend
    assert answer.citations, "a grounded answer must carry citations"


def test_overview_question_uses_course_structure(econ_store):
    fake = FakeLLM()
    agent = _agent(econ_store, fake)
    import asyncio
    answer = asyncio.run(agent.answer_product("What do I need to know about this course?", "ECON303"))
    assert fake.saw_overview
    assert not answer.refused
    assert "course overview" in answer.backend
    assert answer.answer.startswith("## Overview")


def test_no_model_still_refuses_out_of_materials(econ_store):
    # With no model available, there is no general fallback: the honest grounded
    # refusal stands rather than fabricating an answer.
    import asyncio
    agent = _agent(econ_store, LLM())  # offline stub: available is False
    answer = asyncio.run(agent.answer_product("How do I file my personal income taxes?", "ECON303"))
    assert answer.refused
    assert answer.answer.strip().startswith(NOT_IN_MATERIALS)


def test_benchmark_ask_path_is_unchanged_and_still_refuses(econ_store):
    # The eval-measured path must not gain a general fallback.
    fake = FakeLLM()
    agent = _agent(econ_store, fake)
    answer = agent.ask("How do I file my personal income taxes?", "ECON303")
    assert answer.refused
    assert answer.answer.strip().startswith(NOT_IN_MATERIALS)


def test_bbplus_ask_endpoint_wires_the_product_fallback(tmp_path, monkeypatch):
    """The HTTP ask endpoint routes through answer_product, so an out-of-materials
    question comes back as a labelled general answer rather than a refusal."""
    from fastapi.testclient import TestClient
    from api import main

    store = SQLiteStore(tmp_path / "api.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "alice")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr("api.agent.loop.resolve_product_llm", lambda prefs: FakeLLM())
    main.invalidate_pipeline()
    client = TestClient(main.app)
    try:
        created = client.post(
            "/api/integrations/bbplus/course-mappings/bb-econ/create",
            json={"code": "ECON 303", "title": "Micro", "term": ""},
        )
        assert created.status_code == 200, created.text
        main.invalidate_pipeline()  # an empty course has no corpus to retrieve from
        reply = client.post(
            "/api/integrations/bbplus/course-mappings/bb-econ/ask",
            json={"question": "Explain photosynthesis in plants", "depth": "concise"},
        )
        assert reply.status_code == 200, reply.text
        body = reply.json()
        # numbered synthesis path: no citations for an out-of-materials question,
        # answered from general knowledge, not refused.
        assert body["refused"] is False
        assert body["citations"] == []
        assert "general knowledge" in body["backend"]
    finally:
        client.close()
        store.close()
        main.invalidate_pipeline()


def test_numbered_synthesis_cites_only_used_sources(econ_store):
    import asyncio
    fake = FakeLLM()
    agent = _agent(econ_store, fake)
    answer = asyncio.run(agent.answer_product("Explain elasticity of demand", "ECON303", numbered=True))
    assert fake.saw_synthesis
    assert not answer.refused
    assert "[1]" in answer.answer
    # No ugly internal-id labels leak through.
    assert "econ303" not in answer.answer.lower()
    assert len(answer.citations) == 1
    assert "from your materials" in answer.backend


def test_numbered_synthesis_falls_back_to_general_with_no_citations(econ_store):
    import asyncio
    fake = FakeLLM()
    agent = _agent(econ_store, fake)
    answer = asyncio.run(agent.answer_product("How do I file my personal income taxes?", "ECON303", numbered=True))
    assert fake.saw_synthesis
    assert not answer.refused
    assert answer.citations == []
    assert "general knowledge" in answer.backend


def test_finalize_citations_renumbers_and_drops_invalid():
    text = "A [2] then [2] again, and [5] plus [1]."
    out, order = pa.finalize_citations(text, ["s0", "s1", "s2"])
    assert order == ["s1", "s0"]          # first-seen order
    assert "[1]" in out and "[2]" in out  # renumbered 1..M
    assert "[5]" not in out                # invalid dropped
    assert out.count("[1]") == 2           # repeats of the same source share a number


def test_sidebar_widens_source_pool_beyond_the_strict_top_k(multi_store, monkeypatch):
    """The old grounded path fed only ~6 chunks (often from 1-2 files); the sidebar
    synthesis must surface more distinct files so a relevant one is not crowded out."""
    import asyncio

    captured = {}

    def spy(question, sources, course_code, depth, llm):
        captured["sources"] = sources
        # Cite two different offered sources.
        return "Archaeology studies the human past [1][3]."

    monkeypatch.setattr(pa, "synthesize", spy)
    agent = _agent(multi_store, FakeLLM())
    answer = asyncio.run(agent.answer_product("What is archaeology?", "ANTH201", numbered=True))

    # More than the strict top-6 distinct files reach the model...
    assert len(captured["sources"]) >= 7
    # ...each carries real (non-empty, deduped) context...
    assert all(s["snippets"] for s in captured["sources"])
    # ...and only the two the model actually cited become references.
    assert len(answer.citations) == 2


@pytest.fixture
def router_store(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "router.sqlite")
    monkeypatch.setattr(
        "api.corpus.ingest_service.embedder_state_path",
        lambda user: tmp_path / f"embedder_{user}.pkl",
    )
    from api.corpus.ingest_service import ingest_files

    store.upsert_course(Course("ANTH201", "alice", "ANTH 201", "Archaeology"))
    # A syllabus whose text never mentions "archaeology" — embeddings won't rank
    # it for a concept question, but its title tells a human (and the router).
    syl = tmp_path / "bbplus_syllabus.txt"
    syl.write_text("Chapter: Syllabus\nPage: 1\n"
                   + ("Course policies. Attendance is required. Late work loses ten percent per day. "
                      "Grades come from two exams and weekly checks. Office hours are Tuesdays. " * 20),
                   encoding="utf-8")
    ingest_files([syl], user_id="alice", course_id="ANTH201", store=store)
    # rename its title to look like a syllabus (ingest titles from filename)
    src = store.sources("alice", "ANTH201")[0]
    src.title = "Syllabus ANTH 201.pdf"
    store.upsert_source(src)
    for i in range(2):
        r = tmp_path / f"bbplus_reading_{i}.txt"
        r.write_text(f"Chapter: Reading {i}\nPage: 1\n"
                     + ("Archaeology is the study of the human past through material remains. " * 40),
                     encoding="utf-8")
        ingest_files([r], user_id="alice", course_id="ANTH201", store=store)
    try:
        yield store
    finally:
        store.close()


def test_router_surfaces_a_file_that_embeddings_miss(router_store, monkeypatch):
    """The syllabus never lexically matches a concept query, so embedding retrieval
    skips it; the LLM router picks it by title and it reaches the model."""
    import asyncio

    # Router picks whichever catalogue entry looks like a syllabus.
    def pick_syllabus(question, catalog, llm, **_):
        return [c["n"] for c in catalog if "syllabus" in (c["title"] or "").lower()]

    captured = {}

    def spy(question, sources, course_code, depth, llm):
        captured["titles"] = [s["title"] for s in sources]
        return "See the policy [1]."

    monkeypatch.setattr(pa, "route_files", pick_syllabus)
    monkeypatch.setattr(pa, "synthesize", spy)
    agent = _agent(router_store, FakeLLM())
    asyncio.run(agent.answer_product("what is the late work policy?", "ANTH201", numbered=True))
    assert any("syllabus" in (t or "").lower() for t in captured["titles"]), captured.get("titles")


def test_router_parses_model_output_and_ignores_invalid_numbers():
    catalog = [{"n": 1, "title": "A"}, {"n": 2, "title": "B"}, {"n": 3, "title": "C"}]

    class R:
        available = True
        def text(self, prompt, *, system="", **_):
            return "Sure! The relevant files are [3, 1, 99]."

    assert pa.route_files("q", catalog, R()) == [3, 1]


def test_clean_snippet_strips_layout_noise():
    raw = "[Slide 1]\nArchaeology studies the human past.\n[Figure: Slide 3 image]\nProvenience matters [Figure: x] here.\n[Page 3]"
    cleaned = pa.clean_snippet(raw)
    assert "Slide" not in cleaned and "Figure" not in cleaned and "Page 3" not in cleaned
    assert "Archaeology studies the human past." in cleaned
    assert "Provenience matters here." in cleaned

"""Regression coverage for the assessment/Practice boundary and integrity line."""

from __future__ import annotations

from types import SimpleNamespace

from api.practice import generate, parse, service, verify
from api.practice.help import help_for
from api.store import SQLiteStore


def test_variant_solver_rederives_budget_constraint_with_sympy():
    ok, answer = verify.verify(
        "cobb_douglas_utility_max",
        {"a": 0.5, "b": 0.5, "m": 100, "p1": 2, "p2": 5},
        {"x1": 25, "x2": 10},
    )
    assert ok
    assert answer == {"x1": 25.0, "x2": 10.0}

    bad, _ = verify.verify(
        "cobb_douglas_utility_max",
        {"a": 0.5, "b": 0.5, "m": 100, "p1": 2, "p2": 5},
        {"x1": 25},
    )
    assert not bad  # every independently derived result must be checked


def test_uploaded_solution_is_structurally_blocked():
    original = SimpleNamespace(
        origin="uploaded", chapter_ref="econ303:varian:5", topic="budget constraints",
        prompt="Find the optimal bundle", solution_steps="", answer="",
    )
    result = help_for(original, 3, SimpleNamespace(available=False))
    assert result["blocked"] is True
    assert "never the final answer" in result["text"]


def test_level_two_strips_calculation_and_uses_linked_chapter():
    class Model:
        available = True

        def text(self, *args, **kwargs):
            return "Multiply the bundles. 20 * 6 = 120. Therefore the claim is true."

    generated = SimpleNamespace(
        origin="generated", chapter_ref="econ303:varian:4", topic="indifference curves",
        prompt="Compare two bundles", solution_steps="20 * 6 = 120; answer true",
        answer="true", verified=True,
    )
    result = help_for(generated, 2, Model())
    assert "120" not in result["text"]
    assert "true" not in result["text"].lower()
    assert "Chapter 4" in result["text"]


def test_practice_chapter_linker_ignores_generic_word_overlap():
    store = SimpleNamespace(chapters=lambda *a: [{
        "source_id": "varian", "chapter_num": 21, "title": "Cost Curves",
    }])
    link = service._chapter_linker(store, "u", "econ303")
    assert link("indifference curves", "Compare bundles on an indifference curve") == ""


def test_assessment_import_does_not_index_homework_or_recurse_variants(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "practice.db")
    store.upsert_course_stub("u", "econ303", "ECON 303", "Microeconomics")
    source_file = tmp_path / "hw.pdf"
    source_file.write_bytes(b"placeholder")

    class Segment:
        text = "Problem 1. Maximize utility subject to a budget constraint."

    monkeypatch.setattr(service, "extract", lambda *a, **k: SimpleNamespace(
        segments=[Segment()], page_count=1))
    monkeypatch.setattr(parse, "parse_problems", lambda *a, **k: [{
        "number": "1", "prompt": "Maximize utility.", "type": "numeric",
        "topic": "utility maximization", "difficulty": "standard",
        "given_solution": "", "needs_review": False,
    }])
    generation_calls = []

    def variants(*a, **k):
        generation_calls.append(1)
        return [{"prompt": "A verified variant", "type": "numeric", "answer": "x1 = 1",
                 "solution_steps": "Steps", "verified": True,
                 "verify_method": "sympy:test"}]

    monkeypatch.setattr(generate, "generate_variants", variants)
    report = service.ingest_assessment(
        source_file, user_id="u", course_id="econ303", store=store,
        llm=SimpleNamespace(available=True),
    )

    assert report.parsed == 1
    assert report.variants_kept == 1
    assert report.variants_generated == 1
    assert len(generation_calls) == 1
    assert {p.origin for p in store.problems("u", "econ303")} == {"uploaded", "generated"}
    assert store.chunk_count_for_course("u", "econ303") == 0

from pathlib import Path
from types import SimpleNamespace

import pytest

from api.math_text import canonicalize_math, math_to_speech
from ingest.chunk import _split_into_blocks, segments_to_blocks
from ingest.extract import Segment, _repair_stacked_fractions
from api.models import AgentAnswer, BlockKind, Chunk, Problem, ScoredChunk
from api.practice.help import help_for


def test_plain_equations_are_canonicalized_without_rewriting_prose():
    text = "Budget: p1x1 + p2x2 ≤ m.\nThe price is $50 today."
    result = canonicalize_math(text)
    assert r"\[Budget" not in result
    assert r"\(p_{1}x_{1} + p_{2}x_{2} \le m.\)" in result
    assert "$50" in result


def test_explicit_inline_and_display_math_are_not_double_wrapped():
    text = r"Elasticity is \(\epsilon = \frac{dQ}{dP}\)." + "\n" + r"\[x_2 = \alpha^2\]"
    result = canonicalize_math(text)
    assert result.count(r"\(") == 1
    assert result.count(r"\[") == 1
    assert r"\frac{dQ}{dP}" in result
    assert canonicalize_math("Let $x$ be income; price is $50.").startswith(r"Let \(x\)")


@pytest.mark.parametrize(
    ("tex", "expected"),
    [
        (r"\[\frac{p_1x_1}{p_2}\]", "p sub one x sub one divided by p sub two"),
        (r"\(\alpha^2 + \beta_1\)", "alpha squared plus beta sub one"),
        (r"\[x \leq 3\]", "x less than or equal to 3"),
        (r"\[\sqrt{x} + \sum_{i=1}^{n} i + \int_0^1 f(x) dx\]",
         "square root of x plus the sum from i equals 1 to n of i plus the integral from zero to one of f x dx"),
        (r"\[\lim_{x\to 0} \frac{1}{1-\alpha}\]",
         "the limit as x approaches 0 1 divided by 1 minus alpha"),
        (r"\[\begin{aligned}x&=1\\y&=2\end{aligned}\]",
         "x equals 1; next, y equals 2"),
        (r"\[\epsilon = \frac{\%\Delta Q}{\%\Delta P}\]",
         "epsilon equals percent capital delta Q divided by percent capital delta P"),
        (r"\[P_xX + P_yY = I\]", "P sub x X plus P sub y Y equals I"),
        ("It costs $50 today.", "It costs $50 today."),
    ],
)
def test_speech_converts_math_and_preserves_prose(tex, expected):
    speech = " ".join(math_to_speech(tex).split())
    assert expected in speech
    assert "\\frac" not in speech and "{" not in speech


def test_malformed_tex_does_not_send_tex_punctuation_to_speech():
    speech = math_to_speech(r"An unfinished formula \frac{a}")
    assert "\\" not in speech and "{" not in speech and "}" not in speech


def test_fraction_equation_remains_one_protected_chunk():
    source = "Intro.\nx2 = \\frac{m}{p2}\n−\\frac{p1}{p2}\nx1.\n(2.4)\nMore prose."
    blocks = _split_into_blocks(source)
    equations = [text for text, kind in blocks if kind is BlockKind.EQUATION]
    assert len(equations) == 1
    normalized = segments_to_blocks([Segment(source, page=1)])[1].text
    assert r"\frac{m}{p_{2}}" in normalized
    assert r"\frac{p_{1}}{p_{2}}" in normalized


def test_real_textbook_geometry_restores_stacked_fractions():
    pdf = Path("materials/econ303/tb_varian.pdf")
    if not pdf.exists():
        pytest.skip("bundled Varian PDF is unavailable")
    import pymupdf

    page = pymupdf.open(pdf)[47]
    result = _repair_stacked_fractions(page, page.get_text("text"))
    assert r"\frac{m}{p2}" in result
    assert r"\frac{p1}{p2}" in result
    blocks = segments_to_blocks([Segment(result, page=48)])
    equation = next(block.text for block in blocks if block.kind is BlockKind.EQUATION and r"\frac{m}" in block.text)
    assert r"\frac{m}{p_{2}}" in equation
    assert r"\frac{p_{1}}{p_{2}}" in equation


def test_geometry_repair_handles_function_expressions_in_fractions():
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((80, 100), "r = F'(T)")
    top = page.get_text("dict")["blocks"][0]["lines"][0]["bbox"]
    width = pymupdf.get_text_length("F(T)", fontname="helv", fontsize=11)
    page.insert_text((top[2] - width, 114), "F(T)")
    raw = page.get_text("text")
    repaired = _repair_stacked_fractions(page, raw)
    assert r"\frac{F'(T)}{F(T)}" in repaired
    doc.close()


def test_end_to_end_economics_equation_through_retrieval_answer_practice_and_tts():
    canonical = r"\[x_2 = \frac{m}{p_2} - \frac{p_1}{p_2}x_1\]"
    chunk = Chunk(
        id="budget-equation", user_id="test", source_id="varian", course_id="econ303",
        chapter_num=2, chapter_title="Budget Constraint", section="Budget set",
        page_start=48, page_end=48, text=canonical, parent_text=canonical,
        kind=BlockKind.EQUATION,
    )
    retrieved = ScoredChunk(chunk, 0.9).to_dict()
    assert retrieved["text"] == canonicalize_math(canonical)
    answer = AgentAnswer(
        question="What is the consumer's budget constraint?",
        answer=f"Rearranged, the budget line is {canonical}", citations=[], steps=[], refused=False,
        explanation=f"The intercept is the available income divided by price: {canonical}",
        explained=True,
    ).to_dict()
    assert r"\frac{m}{p_{2}}" in answer["answer"]
    problem = Problem(
        id="variant", user_id="test", course_id="econ303", prompt=f"Solve using {canonical}",
        origin="generated", answer=r"x_1=2", solution_steps=f"Rearrange: {canonical}",
    )
    practice = help_for(problem, 3, SimpleNamespace(available=False))
    assert r"\frac{m}{p_{2}}" in practice["text"]
    speech = math_to_speech(answer["answer"])
    assert "equals m divided by p sub two minus p sub one divided by p sub two x sub one" in speech
    assert r"\frac" not in speech and "{" not in speech and "}" not in speech

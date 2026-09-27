"""Product answering strategy: relevance judgement + graceful fallback.

The benchmark path (`Agent.ask`/`ask_async`) is strictly grounded: it answers
only from the course corpus and refuses otherwise. That is exactly what the eval
harness measures, so it must not change.

The *product* wants something friendlier. When a student's own materials do not
actually answer a question, the old grounded path either forced a weak,
loosely-matched answer or refused with an apology. This module adds, for the
product path only:

  * a relevance judge over the retrieved passages, so a loose keyword match is
    recognised as *not* an answer;
  * a general-knowledge fallback (clearly labelled as NOT from the course) when
    the materials do not cover the question;
  * an overview path for meta questions like "what do I need to know about this
    course?", which no passage lookup answers well — it synthesises from the
    course's real structure (its topics and graded work).

None of this touches retrieval, the refusal gate, chunking, or generation on the
benchmark; it is reached only through `Agent.answer_product`.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence

# Retrieval above this reranker confidence is trusted without a relevance call,
# so a strong, obvious hit adds no latency. Loose matches (which is exactly the
# failure being fixed) sit lower and get judged.
RELEVANCE_JUDGE_CEILING = 0.60

# Guarantees the fallback is always marked, even if the model forgets to.
GENERAL_PREFIX = "*Not found in your course materials — answering from general knowledge:*\n\n"

_OVERVIEW_PATTERNS = [
    r"\bwhat.*\b(need|have|should)\b.*\bknow\b",          # what do I need to know
    r"\bwhat('?s| is| are)\b.*\b(this )?(course|class)\b.*\b(about|cover)",
    r"\bwhat.*\b(course|class)\b.*\babout\b",
    r"\b(overview|orientation|summary|syllabus|study guide|road ?map)\b.*\b(course|class)\b",
    r"\b(course|class)\b.*\b(overview|summary|study guide)\b",
    r"\bwhat.*\b(cover|topics|units|main (ideas|themes))\b",
    r"\bwhat.*\b(should|do) i (focus|study|prioriti[sz]e)\b",
    r"\bwhere.*\bstart\b",
    r"\btell me about\b.*\b(this )?(course|class)\b",
]
_OVERVIEW_RE = re.compile("|".join(_OVERVIEW_PATTERNS), re.I)


def is_overview_question(question: str) -> bool:
    """A meta/orientation question that a passage lookup answers badly."""
    return bool(_OVERVIEW_RE.search(question or ""))


# -- relevance judge --------------------------------------------------------

_JUDGE_SYSTEM = (
    "You decide whether retrieved course passages actually contain the information "
    "needed to answer a student's question. A passage that merely shares some "
    "words with the question but does not address it is NOT relevant. Reply with "
    "exactly one word: RELEVANT or IRRELEVANT."
)


def judge_relevance(question: str, passages: Sequence[dict], llm: Any) -> Optional[bool]:
    """True/False, or None when the judgement could not be made.

    None means "no opinion" — the caller then keeps the retrieval gate's verdict,
    so a judge failure can never turn a good grounded answer into a fallback.
    """
    if not getattr(llm, "available", False) or not passages:
        return None
    blocks = []
    for i, passage in enumerate(passages[:5], 1):
        text = (passage.get("snippet") or passage.get("text") or "")[:600]
        blocks.append(f"[Passage {i}]\n{text}")
    prompt = (
        f"Question: {question}\n\n"
        f"Retrieved passages:\n\n" + "\n\n".join(blocks) +
        "\n\nDo these passages contain the information needed to answer the "
        "question? Reply with exactly one word: RELEVANT or IRRELEVANT."
    )
    try:
        verdict = llm.text(prompt, system=_JUDGE_SYSTEM, max_tokens=8).strip().lower()
    except Exception:  # noqa: BLE001 - the judge is advisory; never break the answer
        return None
    # Order matters: "irrelevant" contains "relevant" as a substring.
    if "irrelevant" in verdict:
        return False
    if "relevant" in verdict:
        return True
    return None


# -- general-knowledge fallback --------------------------------------------

_GENERAL_SYSTEM = (
    "You are a helpful study assistant. The student's own course materials do not "
    "cover their question, so you are answering from general knowledge. Be "
    "accurate and clear, and do not claim the course says anything."
)


def general_answer(question: str, course_code: str, depth: str, llm: Any) -> str:
    """A clearly-labelled general-knowledge answer, in Markdown."""
    if not getattr(llm, "available", False):
        return ""
    context = f"The student is taking {course_code}. " if course_code else ""
    prompt = (
        f"{context}Their course materials do not contain an answer to the question "
        f"below. Answer it yourself, accurately and helpfully, from general "
        f"knowledge. Use Markdown (short paragraphs, lists where useful). Do not "
        f"pretend the course materials say this.\n\nQuestion: {question}"
    )
    max_tokens = 900 if depth == "in_depth" else 450
    try:
        text = llm.text(prompt, system=_GENERAL_SYSTEM, max_tokens=max_tokens).strip()
    except Exception:  # noqa: BLE001
        return ""
    if not text:
        return ""
    # Strip a leading note the model may have added itself, then prepend our own,
    # so the label is present exactly once and always.
    text = re.sub(r"^\s*\*?_?not (found|in|available).*?\n+", "", text, flags=re.I)
    return GENERAL_PREFIX + text


# -- course overview --------------------------------------------------------

# -- file routing (LLM picks which files are relevant) ---------------------
#
# Embedding + rerank retrieval matches PASSAGES, and it reliably buries files
# whose relevance a human reads off the title: a terse slide deck loses to a
# dense reading, and a syllabus never lexically matches "what's the late
# policy". So before synthesising we show the model the whole file catalogue
# (title + type + a one-line synopsis) and let it pick the files likely to hold
# the answer. This is the "let the AI sort the files out" idea, done as a cheap
# routing step rather than dumping every file's full text into one prompt.

_ROUTER_SYSTEM = (
    "You are the retrieval brain of a study assistant. Given a student's question "
    "and a numbered catalogue of their course files, pick the files most likely to "
    "contain the answer. Reason from titles, types and synopses:\n"
    "- a SYLLABUS answers policy / schedule / grading / logistics / 'what do we do "
    "in this course' questions;\n"
    "- slides and readings answer concept questions;\n"
    "- assignment/problem/solution files answer questions about that assignment, and "
    "rarely a general concept question;\n"
    "- prefer a few strong matches over many weak ones.\n"
    "Reply with ONLY a JSON array of file numbers, most relevant first, e.g. [3,1]. "
    "If nothing in the catalogue fits, reply []."
)

_INT_LIST = re.compile(r"-?\d+")


def route_files(question: str, catalog: Sequence[dict], llm: Any, limit: int = 8) -> list[int]:
    """Return catalogue numbers the model judges relevant (possibly empty)."""
    if not getattr(llm, "available", False) or not catalog:
        return []
    lines = [
        f'[{c["n"]}] {c.get("title") or "Untitled"} ({c.get("type") or "file"}) — {c.get("synopsis") or ""}'
        for c in catalog
    ]
    prompt = (f"Question: {question}\n\nCourse files:\n" + "\n".join(lines) +
              "\n\nReturn the JSON array of the most relevant file numbers.")
    try:
        raw = llm.text(prompt, system=_ROUTER_SYSTEM, max_tokens=60)
    except Exception:  # noqa: BLE001 - routing is advisory; retrieval still ran
        return []
    bracket = raw[raw.find("["): raw.rfind("]") + 1] if "[" in raw and "]" in raw else raw
    seen: list[int] = []
    valid = {c["n"] for c in catalog}
    for token in _INT_LIST.findall(bracket):
        n = int(token)
        if n in valid and n not in seen:
            seen.append(n)
        if len(seen) >= limit:
            break
    return seen


# -- grounded synthesis (numbered, Blackboard-linkable citations) ----------

_SYNTHESIS_SYSTEM = (
    "You are Course Copilot, a sharp and genuinely helpful study assistant. A "
    "student asked a question about their course, and you are given numbered "
    "excerpts pulled from that course's own materials. Write a clear, confident, "
    "well-structured answer in Markdown.\n\n"
    "How to use the excerpts:\n"
    "- If they contain information relevant to the question, SYNTHESISE a complete "
    "answer in your own words — explain it, connect the pieces, do not merely "
    "quote and do not hedge or apologise when you actually have relevant material.\n"
    "- Cite each claim you take from an excerpt with its number in square brackets, "
    "like [1] or [2]. Cite the excerpt the claim came from; never invent a number, "
    "and do not cite an excerpt you did not use.\n"
    "- If the excerpts do NOT actually address the question, do not force it. Begin "
    "with exactly this line:\n"
    "  I couldn't find this in your course materials — here's what I know:\n"
    "  then answer from your own general knowledge, accurately, with NO [n] "
    "citations. You may also blend: use the materials where they help (cited) and "
    "general knowledge where they don't.\n\n"
    "Presentation rules:\n"
    "- Ignore layout noise in the excerpts — slide/page numbers, '[Figure: ...]', "
    "headers, keyword lists, stray brackets or symbols. NEVER copy that noise into "
    "your answer.\n"
    "- Render mathematics in proper LaTeX: \\( ... \\) inline and \\[ ... \\] for "
    "display. Never leave a bare TeX command outside math delimiters, and never put "
    "a [n] citation inside math.\n"
    "- Prose and lists only. Do not restate the question. Do not describe your own "
    "process."
)


_NOISE_LINE = re.compile(r"^\s*(\[(slide|page|figure)[^\]]*\]|slide\s*\d+|page\s*\d+)\s*$", re.I)
_INLINE_NOISE = re.compile(r"\[(?:figure|image|slide|page)[^\]]*\]", re.I)


def clean_snippet(text: str, cap: int = 1500) -> str:
    """Strip the worst layout debris from an excerpt before the model reads it.

    The model is also told to ignore such noise; removing it here keeps the
    prompt tighter and the model less likely to echo it.
    """
    lines = []
    for line in str(text or "").splitlines():
        if _NOISE_LINE.match(line):
            continue
        lines.append(_INLINE_NOISE.sub("", line))
    cleaned = " ".join(" ".join(lines).split())
    return cleaned[:cap]


def _format_sources(sources: Sequence[dict]) -> str:
    blocks = []
    for source in sources:
        title = source.get("title") or "Course material"
        course = source.get("course") or ""
        header = f'[{source["n"]}] "{title}"' + (f" ({course})" if course else "")
        body = "\n".join(s for s in source.get("snippets", []) if s)[:2400]
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks)


def synthesize(question: str, sources: Sequence[dict], course_code: str,
               depth: str, llm: Any) -> str:
    """One grounded-or-general answer with numbered [n] citations, clean Markdown.

    `sources` items: {n, title, course, snippets:[...]}. An empty list is valid —
    the model then answers from general knowledge with the standard lead-in.
    """
    if not getattr(llm, "available", False):
        return ""
    depth_note = (
        "Give a thorough, well-structured explanation (with a worked example if the "
        "excerpts contain one)." if depth == "in_depth"
        else "Keep it concise — a few short paragraphs or bullets."
    )
    context = f"The student is taking {course_code}. " if course_code else ""
    if sources:
        material = "Numbered course excerpts:\n\n" + _format_sources(sources)
    else:
        material = ("No course excerpts were retrieved for this question, so answer "
                    "from general knowledge using the lead-in described in your "
                    "instructions.")
    prompt = (
        f"{context}{depth_note}\n\nStudent's question: {question}\n\n{material}"
    )
    max_tokens = 1100 if depth == "in_depth" else 600
    try:
        return llm.text(prompt, system=_SYNTHESIS_SYSTEM, max_tokens=max_tokens).strip()
    except Exception:  # noqa: BLE001
        return ""


_CITE_MARKER = re.compile(r"\[(\d+)\]")


def finalize_citations(text: str, source_ids_by_number: Sequence[str]) -> tuple[str, list[str]]:
    """Rewrite the model's [n] markers to a clean, gap-free 1..M numbering over the
    sources it actually cited, in order of first appearance.

    Returns (rewritten_text, cited_source_ids_in_display_order). A marker that
    points past the sources we supplied is dropped rather than shown wrong.
    """
    order: list[str] = []

    def repl(match: re.Match[str]) -> str:
        k = int(match.group(1))
        if k < 1 or k > len(source_ids_by_number):
            return ""
        source_id = source_ids_by_number[k - 1]
        if source_id not in order:
            order.append(source_id)
        return f"[{order.index(source_id) + 1}]"

    rewritten = _CITE_MARKER.sub(repl, text)
    # Tidy any doubled spaces a dropped marker may have left.
    rewritten = re.sub(r"[ \t]{2,}", " ", rewritten).replace(" .", ".").replace(" ,", ",")
    return rewritten, order


_OVERVIEW_SYSTEM = (
    "You are a study assistant giving a student a clear, honest orientation to a "
    "course. Base the overview on the actual topics and graded work provided. Do "
    "not invent specifics that are not listed; if the information is thin, say so "
    "and suggest checking the syllabus."
)


def overview_answer(question: str, course_code: str, topics: Sequence[str],
                    assessments: Sequence[str], depth: str, llm: Any) -> str:
    """Synthesise a course orientation from its real structure."""
    if not getattr(llm, "available", False):
        return ""
    topic_block = "\n".join(f"- {t}" for t in topics[:60]) or "(no topics recorded yet)"
    assess_block = "\n".join(f"- {a}" for a in assessments[:40]) or "(no graded work recorded yet)"
    prompt = (
        f"The student asked: {question}\n\n"
        f"Course: {course_code or 'this course'}\n\n"
        f"Topics/chapters the course actually contains:\n{topic_block}\n\n"
        f"Graded work / assessments:\n{assess_block}\n\n"
        "Give a concise, well-structured overview of what this course is about and "
        "what the student should focus on. Use Markdown with short sections or "
        "bullet points. Ground it in the topics and assessments above."
    )
    max_tokens = 1000 if depth == "in_depth" else 550
    try:
        text = llm.text(prompt, system=_OVERVIEW_SYSTEM, max_tokens=max_tokens).strip()
    except Exception:  # noqa: BLE001
        return ""
    return text

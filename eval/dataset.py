"""Eval question set loading and gold-label resolution.

Gold labels are stored as (source_id, page range) and resolved to chunk ids at
eval time. This is the single most important decision in the harness. Had gold
been stored as chunk ids, every change to chunk size, the parent-child split or
the equation detector would silently invalidate questions -- and it would
fail quietly, appearing as a mysterious recall drop rather than an error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from api.config import settings
from api.store import SQLiteStore

QUESTIONS_PATH = settings.eval_dir / "benchmark" / "questions.jsonl"
# The benchmark runs against its own user so a developer's real uploads and
# the labelled corpus can never contaminate each other.
BENCHMARK_USER = "benchmark"

# Slices, and what each one is for.
SLICES = {
    "generated": "paraphrased from a source chunk; shares vocabulary with it",
    "natural": "hand-written in student phrasing; the honest retrieval number",
    "out_of_corpus": "hand-written adversarial; must refuse",
    "humanities_adversarial": "quantitative phrasing on a course with no data link",
    "compositional": "requires more than one tool",
}


@dataclass
class GoldSpan:
    source: str
    page_start: int
    page_end: int


@dataclass
class EvalQuestion:
    id: str
    q: str
    course: Optional[str]
    in_corpus: bool
    slice: str
    gold: list[GoldSpan] = field(default_factory=list)
    gold_points: list[str] = field(default_factory=list)
    expect: str = ""
    note: str = ""

    @property
    def expects_tools(self) -> list[str]:
        if self.expect.startswith("tools:"):
            return [t.strip() for t in self.expect[len("tools:") :].split(",") if t.strip()]
        return []

    @property
    def forbids_data_tool(self) -> bool:
        return self.expect == "no_data_tool"

    @property
    def may_use_data_tool(self) -> bool:
        return self.expect == "refuse_or_data_tool"

    def resolve_gold(self, store: SQLiteStore, user_id: str = BENCHMARK_USER) -> set[str]:
        """Page ranges -> chunk ids, against whatever chunking is current, scoped
        to the benchmark user so another user's identically-named source cannot
        leak in."""
        out: set[str] = set()
        for span in self.gold:
            out.update(store.resolve_pages_to_chunks(
                span.source, span.page_start, span.page_end, user_id=user_id))
        return out


def load_questions(path: Optional[Path] = None) -> list[EvalQuestion]:
    path = path or QUESTIONS_PATH
    questions: list[EvalQuestion] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        raw: dict[str, Any] = json.loads(line)
        gold = [
            GoldSpan(g["source"], int(g["pages"][0]), int(g["pages"][1]))
            for g in raw.get("gold", [])
        ]
        questions.append(
            EvalQuestion(
                id=raw["id"],
                q=raw["q"],
                course=raw.get("course"),
                in_corpus=bool(raw.get("in_corpus", False)),
                slice=raw.get("slice", "generated"),
                gold=gold,
                gold_points=raw.get("gold_points", []),
                expect=raw.get("expect", ""),
                note=raw.get("note", ""),
            )
        )
    return questions


def validate(questions: list[EvalQuestion], store: SQLiteStore) -> list[str]:
    """Catch the failure mode this design exists to prevent: a gold span that no
    longer resolves to any chunk. Run before every eval so a broken label is a
    loud error rather than a silent recall drop."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for question in questions:
        if question.id in seen_ids:
            problems.append(f"{question.id}: duplicate id")
        seen_ids.add(question.id)
        if question.in_corpus and not question.gold:
            problems.append(f"{question.id}: in_corpus but no gold spans")
        for span in question.gold:
            resolved = store.resolve_pages_to_chunks(
                span.source, span.page_start, span.page_end, user_id=BENCHMARK_USER)
            if not resolved:
                problems.append(
                    f"{question.id}: gold span {span.source} pp.{span.page_start}-{span.page_end} "
                    "resolves to no chunks (corpus changed, or the page range is wrong)"
                )
    return problems


def summarise(questions: list[EvalQuestion]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for question in questions:
        counts[question.slice] = counts.get(question.slice, 0) + 1
    counts["total"] = len(questions)
    counts["in_corpus"] = sum(1 for q in questions if q.in_corpus)
    return counts

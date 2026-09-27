"""Agent tools: definitions and handlers.

`search_textbook` is always available. The two data tools are offered only when
the course's derived `has_data_link` capability is true, so the generalised
no-data-link guard is enforced by *absence* rather than by refusal: a literature
question cannot reach for a time series because no such tool is in its schema.

The data path is search-first by construction. `find_data_series` takes a concept
and returns real FRED ids; `fetch_data_series` refuses any id that did not come
back from a search in this conversation. That replaces the hand-curated series
catalogue, which could not survive generalisation, while keeping the protection
it provided against invented identifiers.

Tool results are summaries with citations; the model may only assert what a
citation supports, and FRED numbers are labelled as data, never attributed to the
course materials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

import re

from ..data.fred import FredError, SeriesRegistry, UnverifiedSeriesError, get_fred
from ..data.transforms import default_transform_for
from ..models import Citation
from ..retrieval.pipeline import RetrievalPipeline

DATA_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "find_data_series",
        "description": (
            "Search FRED for real-world economic data series matching a CONCEPT. "
            "Pass a plain-language query such as 'unemployment rate' or 'consumer "
            "price index'. NEVER pass a series id. Returns real ids you may then "
            "fetch. If nothing usable comes back, say so -- do not guess an id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Concept, not an id"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_data_series",
        "description": (
            "Fetch observations for a series id returned by find_data_series. "
            "Ids not produced by a search in this conversation are refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "series_id": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["series_id"],
        },
    },
]

BASE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_textbook",
        "description": (
            "Search the student's own course materials. This is the ONLY source for "
            "claims about what a course says. Returns ranked passages with citations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for"},
                "course": {"type": "string", "description": "The course to search, as given by the caller"},
                "chapter": {"type": "integer", "description": "Restrict to one chapter"},
            },
            "required": ["query"],
        },
    },
]


def tool_schemas(has_data_link: bool) -> list[dict[str, Any]]:
    """The data tools exist for a course only when its corpus earned them.

    This is the generalised form of the old `no_data_link` guard: rather than
    offering the tool and refusing the call, the tool is not offered at all for a
    course whose capability flag is false, so a literature question cannot reach
    for a time series however quantitatively it is phrased.
    """
    return BASE_TOOL_SCHEMAS + (DATA_TOOL_SCHEMAS if has_data_link else [])


# Back-compat for callers that want everything.
TOOL_SCHEMAS = BASE_TOOL_SCHEMAS


@dataclass
class ToolResult:
    ok: bool
    payload: dict[str, Any]
    citations: list[Citation]
    summary: str

    def to_json(self) -> str:
        return json.dumps(self.payload, default=str)


class ToolBox:
    """Handlers, bound to one user's corpus."""

    def __init__(
        self,
        pipeline: RetrievalPipeline,
        course_hint: Optional[str] = None,
        has_data_link: bool = False,
        allow_synthetic: bool = False,
    ):
        self.pipeline = pipeline
        self.course_hint = course_hint
        self.has_data_link = has_data_link
        # Synthetic FRED series are a benchmark fixture. The product path leaves
        # this False so a fabricated series can never be searched or fetched.
        self.allow_synthetic = allow_synthetic
        # Per-conversation: a series id verified for one question must not be
        # usable in the next without being searched for again.
        self.registry = SeriesRegistry()

    @property
    def handlers(self) -> dict[str, Callable[[dict[str, Any]], ToolResult]]:
        base = {"search_textbook": self.search_textbook}
        if self.has_data_link:
            base["find_data_series"] = self.find_data_series
            base["fetch_data_series"] = self.fetch_data_series
        return base

    def _no_data_link(self, tool: str) -> ToolResult:
        return ToolResult(
            False,
            {
                "error": "no_data_link",
                "detail": (
                    f"{self.course_hint or 'this course'} has no real-world data link. "
                    "Answer from the course materials alone; do not cite or fetch "
                    "time series."
                ),
            },
            [],
            f"{tool} refused: course has no data link",
        )

    def find_data_series(self, args: dict[str, Any]) -> ToolResult:
        if not self.has_data_link:
            return self._no_data_link("find_data_series")
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(False, {"error": "query is required"}, [], "empty query")
        # A model that passes an id instead of a concept is corrected, not obeyed.
        if re.fullmatch(r"[A-Z0-9]{3,20}", query):
            return ToolResult(
                False,
                {"error": "looks_like_an_id",
                 "detail": f"{query!r} looks like a series id. Pass a concept such as "
                           "'unemployment rate' instead."},
                [], "rejected an id passed as a query",
            )
        try:
            hits = get_fred().search(query, allow_synthetic=self.allow_synthetic)
        except FredError as exc:
            return ToolResult(False, {"error": str(exc)}, [], "search failed")
        if not hits:
            return ToolResult(
                False,
                {"found": False,
                 "detail": f"FRED returned no series for {query!r}. Say so rather than "
                           "guessing an identifier."},
                [], "no series found",
            )
        self.registry.record(hits)
        payload: dict[str, Any] = {"found": True, "series": [h.as_dict() for h in hits]}
        if any(h.series_id.startswith("SYNTH") for h in hits):
            payload["WARNING"] = (
                "SYNTHETIC results -- no FRED key configured. These ids are not real "
                "and must not be presented as findings about the world."
            )
        return ToolResult(True, payload, [], f"{len(hits)} candidate series")

    def fetch_data_series(self, args: dict[str, Any]) -> ToolResult:
        if not self.has_data_link:
            return self._no_data_link("fetch_data_series")
        series_id = str(args.get("series_id", "")).strip()
        try:
            hit = self.registry.check(series_id)
            transform = default_transform_for(hit.units, hit.title)
            series = get_fred().fetch_observations(
                series_id, self.registry,
                start=args.get("start") or "1960-01-01", end=args.get("end"),
                transform=transform, allow_synthetic=self.allow_synthetic,
            )
        except UnverifiedSeriesError as exc:
            return ToolResult(False, {"error": "unverified_series", "detail": str(exc)},
                              [], "refused an unsearched id")
        except FredError as exc:
            return ToolResult(False, {"error": str(exc)}, [], "fetch failed")

        payload: dict[str, Any] = {
            "summary": series.summary(),
            "points": series.points(),
            "source": "FRED",
            "attribution_rule": (
                "These numbers are FRED data. Label them as data and cite the series "
                "id. Never attribute a data value to the course materials."
            ),
        }
        if series.synthetic:
            payload["WARNING"] = (
                "SYNTHETIC data -- not a finding about the world."
            )
        return ToolResult(True, payload, [], f"{series.series_id} ({series.transform})")

    def search_textbook(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(False, {"error": "query is required"}, [], "empty query")
        course = args.get("course") or self.course_hint
        chapter = args.get("chapter")
        result = self.pipeline.search(
            query, course_id=course, chapter=int(chapter) if chapter else None
        )
        if result.refused or not result.chunks:
            return ToolResult(
                False,
                {
                    "found": False,
                    "reason": result.reason or "nothing relevant in the loaded materials",
                    "confidence": round(result.confidence, 3),
                },
                [],
                "no relevant passages",
            )

        # `snippet` is the child chunk, whose page range the citation actually
        # describes. `context` is the surrounding parent, which may span several
        # pages -- so a sentence quoted from it cannot carry the child's citation.
        # Keeping them separate is what stops an answer attributing a sentence to
        # the wrong page.
        passages = [
            {
                "citation": s.chunk.citation().label(),
                "course": s.chunk.course_id,
                "chapter": s.chunk.chapter_num,
                "chapter_title": s.chunk.chapter_title,
                "snippet": s.chunk.text,
                "context": (s.chunk.parent_text or s.chunk.text)[:1800],
            }
            for s in result.chunks
        ]
        return ToolResult(
            True,
            {"found": True, "confidence": round(result.confidence, 3), "passages": passages},
            [s.chunk.citation() for s in result.chunks],
            f"{len(passages)} passages from {result.chunks[0].chunk.course_id}",
        )

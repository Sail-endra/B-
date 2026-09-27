"""Canonical identifier formats.

The build plan specified chapter references two different ways -- `ch:14` in the
onboarding section and `tb_macro:14` in the dashboard section -- for a string that
is the join key between `syllabus_schedule`, `engagement` and `chunks`. Neither
form disambiguates a course that has both a textbook and slides with a chapter 14.

One canonical form, defined here, used everywhere:

    {course_id}:{source_id}:{chapter_num}

A reference may be *partial* during syllabus extraction, when the syllabus says
"Ch. 14" without telling us which source it means. Partial refs carry a `None`
source and are resolved against the course's sources at commit time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# "Mankiw Ch. 14", "Chapter 14", "14.1-14.3", "ch:14", "pp. 287-310"
_CHAPTER_PATTERNS = [
    re.compile(r"\bch(?:apter|apt|\.)?\s*:?\s*(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})\.\d{1,2}\s*[-–]\s*\d{1,2}\.\d{1,2}\b"),
    re.compile(r"\b(\d{1,2})\.\d{1,2}\b"),
]

_SECTION_RANGE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\s*[-–]\s*(?:\d{1,2}\.)?(\d{1,2})\b")


class ChapterRefError(ValueError):
    """Raised when a chapter reference cannot be parsed or resolved."""


@dataclass(frozen=True)
class ChapterRef:
    """A canonical reference to one chapter of one source within one course."""

    course_id: str
    source_id: Optional[str]
    chapter: int

    def __post_init__(self) -> None:
        if not self.course_id:
            raise ChapterRefError("course_id is required")
        if self.chapter <= 0:
            raise ChapterRefError(f"chapter must be positive, got {self.chapter}")

    @property
    def is_partial(self) -> bool:
        """True when the source is unknown -- e.g. a syllabus that says only "Ch. 14"."""
        return self.source_id is None

    def __str__(self) -> str:
        return f"{self.course_id}:{self.source_id or '*'}:{self.chapter}"

    @classmethod
    def parse(cls, raw: str) -> "ChapterRef":
        parts = raw.strip().split(":")
        if len(parts) != 3:
            raise ChapterRefError(
                f"expected 'course:source:chapter', got {raw!r}. "
                "Legacy 'ch:14' and 'tb_macro:14' forms are not accepted."
            )
        course_id, source_id, chapter = parts
        try:
            chapter_num = int(chapter)
        except ValueError as exc:
            raise ChapterRefError(f"chapter must be an integer in {raw!r}") from exc
        return cls(course_id, None if source_id == "*" else source_id, chapter_num)

    def resolve(
        self,
        candidate_source_ids: Iterable[str],
        prefer: Optional[str] = None,
    ) -> "ChapterRef":
        """Bind a partial ref to a concrete source.

        A syllabus that says "Ch. 14" does not say which source it means, and a
        course commonly has a textbook plus slides that both number a chapter 14.
        `prefer` names the course's primary source -- normally the textbook -- and
        wins when it is among the candidates.

        Without a preference, ambiguity is an error rather than a guess: a wrong
        binding silently corrupts every engagement and readiness number downstream,
        and a loud failure at ingest time is far cheaper than a quiet one on the
        dashboard.
        """
        if not self.is_partial:
            return self
        candidates = list(candidate_source_ids)
        if not candidates:
            raise ChapterRefError(f"no sources available to resolve {self}")
        if prefer and prefer in candidates:
            return ChapterRef(self.course_id, prefer, self.chapter)
        if len(candidates) > 1:
            raise ChapterRefError(
                f"{self} is ambiguous across sources {candidates}; "
                "set a primary source for the course or name it in the syllabus mapping"
            )
        return ChapterRef(self.course_id, candidates[0], self.chapter)


def parse_chapter_mentions(text: str, course_id: str) -> list[ChapterRef]:
    """Pull every chapter mention out of a free-text syllabus reading cell.

    Returns *partial* refs -- the syllabus almost never names the source.
    Deduplicated, ordered by first appearance.
    """
    seen: dict[int, None] = {}
    for pattern in _CHAPTER_PATTERNS:
        for match in pattern.finditer(text or ""):
            num = int(match.group(1))
            if 0 < num < 100:
                seen.setdefault(num, None)
    return [ChapterRef(course_id, None, n) for n in seen]


def parse_section_range(text: str) -> Optional[tuple[int, int, int]]:
    """Parse "14.1-14.3" into (chapter, first_section, last_section)."""
    match = _SECTION_RANGE.search(text or "")
    if not match:
        return None
    chapter, first, last = (int(g) for g in match.groups())
    return chapter, first, last


def user_tag(user_id: str) -> str:
    """Short, stable, id-safe tag for a user, to namespace chunk/parent ids.

    Chunk ids were `{source_id}_{ordinal}`, which collide across users who upload
    the same file to the same course code -- and INSERT OR REPLACE then silently
    reassigned one user's chunks to the other. Namespacing the id by user closes
    that. A hash (not the raw id) keeps ids id-safe whatever the user id contains.
    """
    import hashlib
    return "u" + hashlib.sha1(user_id.encode()).hexdigest()[:8]


def chunk_id(user_id: str, source_id: str, ordinal: int) -> str:
    """Deterministic, USER-NAMESPACED chunk id. Stable for a given
    (user, source, ordinal); never use as an eval gold label -- label by page
    range instead, which survives re-chunking."""
    return f"{user_tag(user_id)}_{source_id}_{ordinal:04d}"


def parent_id(user_id: str, source_id: str, ordinal: int) -> str:
    return f"{user_tag(user_id)}_{source_id}_p{ordinal:04d}"

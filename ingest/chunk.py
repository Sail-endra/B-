"""Chunking.

Three properties the plan asks for, implemented here:

1. **Protected blocks.** Tables, display equations and numbered definitions are
   detected and never split. A half-equation retrieves as noise and reads to a
   student as a bug.
2. **Parent-child.** ~400-token children are indexed for precision; each carries a
   pointer to a ~1200-token parent that is what actually gets shown, so the answer
   has surrounding context the match itself did not need.
3. **Contextual prefix.** `[COURSE / Chapter n: Title / Section]` is prepended to
   the *embedded* text only (see `Chunk.embed_text`), never the displayed text.

Token counts are word-count approximations. A real tokenizer changes the numbers
by ~25% and changes none of the behaviour, so the dependency is not worth it here;
the constant is centralised in `_token_estimate` if that ever stops being true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from api.ids import chunk_id, parent_id
from api.math_text import canonicalize_math
from api.models import BlockKind, Chunk

from .extract import ExtractedDoc, Segment

# A display equation: a line dominated by math operators / Greek, or a LaTeX block.
_EQUATION = re.compile(
    r"^\s*(?:\$\$.*?\$\$|\\\[.*?\\\]|[A-Za-z]\s*[=<>]\s*[^.]{0,120})\s*$", re.S
)
_EQUATION_HINT = re.compile(r"[=×·∑∫∂α-ωΑ-Ω]|\\frac|\\sum|\^|_\{")
# A markdown/pipe table or a run of tab/multi-space aligned columns.
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$|^\s*\S+(?:\t| {3,})\S+")
# "Definition 3.1", "DEFINITION:", "Theorem 2."
_DEFINITION = re.compile(r"^\s*(definition|theorem|lemma|proposition|corollary)\b", re.I)

_PARA_SPLIT = re.compile(r"\n\s*\n")


def _token_estimate(text: str) -> int:
    """Words * 1.3 approximates BPE tokens closely enough for budgeting."""
    return int(len(text.split()) * 1.3) + 1


@dataclass
class Block:
    """A unit of text that may or may not be splittable."""

    text: str
    kind: BlockKind
    page: int
    chapter_num: int
    chapter_title: str
    section: str

    @property
    def protected(self) -> bool:
        return self.kind is not BlockKind.PROSE

    @property
    def tokens(self) -> int:
        return _token_estimate(self.text)


def classify(text: str) -> BlockKind:
    stripped = text.strip()
    if not stripped:
        return BlockKind.PROSE
    if _DEFINITION.match(stripped):
        return BlockKind.DEFINITION
    lines = [l for l in stripped.splitlines() if l.strip()]
    if lines and sum(bool(_TABLE_LINE.match(l)) for l in lines) >= max(2, len(lines) * 0.6):
        return BlockKind.TABLE
    if len(lines) <= 3 and _EQUATION_HINT.search(stripped) and len(stripped) < 400:
        if _EQUATION.match(stripped) or sum(c in "=<>+-*/^" for c in stripped) >= 2:
            return BlockKind.EQUATION
    return BlockKind.PROSE


# A display equation as PDF text extraction actually delivers it: a short line
# carrying a relation, few words, often followed by a bare equation number.
_EQ_NUMBER = re.compile(r"^\(\d{1,2}\.\d{1,3}\)$|^\(\d{1,3}\)$")
_RELATION = re.compile(r"[=≤≥<>∑∫≡≠→]|\\frac|\\sum")


def _is_display_equation(line: str) -> bool:
    stripped = line.strip()
    if not (0 < len(stripped) < 120):
        return False
    if not _RELATION.search(stripped):
        return False
    words = [w for w in re.split(r"\s+", stripped) if w]
    if len(words) > 14:
        return False
    # Mostly-prose lines that merely contain "=" are not display equations.
    wordy = sum(1 for w in words if len(w) > 3 and w.isalpha())
    return wordy <= max(2, len(words) // 3)


def _split_into_blocks(text: str) -> list[tuple[str, BlockKind]]:
    """Break one page of extracted text into blocks.

    Blank-line paragraph splitting is not enough on real PDFs: PyMuPDF returns a
    page as consecutive lines with no blank separators, so the whole page becomes
    one block and every display equation ends up buried inside prose -- where the
    classifier never sees it, and where the prose splitter is free to cut it in
    half. On the synthetic markdown corpus this never showed up, because that
    corpus was written with blank lines between paragraphs.

    So equations are lifted out line-by-line and become their own protected
    blocks, taking any trailing equation number with them.
    """
    out: list[tuple[str, BlockKind]] = []
    for para in _PARA_SPLIT.split(text):
        para = para.strip()
        if not para:
            continue
        lines = para.splitlines()
        if len(lines) < 2:
            out.append((para, classify(para)))
            continue

        buffer: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if _is_display_equation(line):
                if buffer:
                    body = "\n".join(buffer).strip()
                    if body:
                        out.append((body, classify(body)))
                    buffer = []
                equation = [line.strip()]
                # Fractions often occupy multiple extracted baselines. Keep
                # adjacent short mathematical rows with their equation.
                while index + 1 < len(lines):
                    following = lines[index + 1].strip()
                    if _EQ_NUMBER.match(following):
                        break
                    if _is_display_equation(following) or (
                        len(following) <= 32
                        and re.fullmatch(r"[A-Za-z0-9_{}\\.^+−*/=<>≤≥()\s-]+", following)
                        and re.search(r"[A-Za-z0-9}]", following)
                    ):
                        equation.append(following)
                        index += 1
                    else:
                        break
                # Absorb a following bare equation number, e.g. "(2.1)".
                if index + 1 < len(lines) and _EQ_NUMBER.match(lines[index + 1].strip()):
                    equation.append(lines[index + 1].strip())
                    index += 1
                out.append((" ".join(equation), BlockKind.EQUATION))
            else:
                buffer.append(line)
            index += 1
        body = "\n".join(buffer).strip()
        if body:
            out.append((body, classify(body)))
    return out


def segments_to_blocks(segments: Sequence[Segment]) -> list[Block]:
    blocks: list[Block] = []
    for seg in segments:
        for body, kind in _split_into_blocks(seg.text):
            if kind is BlockKind.EQUATION:
                body = canonicalize_math(" ".join(body.splitlines()))
            blocks.append(
                Block(
                    text=body,
                    kind=kind,
                    page=seg.page,
                    chapter_num=seg.chapter_num,
                    chapter_title=seg.chapter_title,
                    section=seg.section,
                )
            )
    return blocks


def _split_prose(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Sentence-aware split of a single oversized prose block."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        tokens = _token_estimate(sentence)
        if current and current_tokens + tokens > max_tokens:
            out.append(" ".join(current))
            # Carry a sentence or two of overlap so a boundary never orphans a claim.
            carry: list[str] = []
            carried = 0
            for prev in reversed(current):
                prev_tokens = _token_estimate(prev)
                if carried + prev_tokens > overlap_tokens:
                    break
                carry.insert(0, prev)
                carried += prev_tokens
            current, current_tokens = carry, carried
        current.append(sentence)
        current_tokens += tokens
    if current:
        out.append(" ".join(current))
    return [c for c in out if c.strip()]


@dataclass
class ChunkingResult:
    chunks: list[Chunk]
    parents: list[tuple[str, str, str, str]]  # (id, user_id, source_id, text)
    protected_kept_whole: int


def chunk_document(
    doc: ExtractedDoc,
    *,
    course_id: str,
    user_id: str,
    child_tokens: int = 400,
    parent_tokens: int = 1200,
    child_overlap: int = 64,
) -> ChunkingResult:
    blocks = segments_to_blocks(doc.segments)
    chunks: list[Chunk] = []
    parents: list[tuple[str, str, str, str]] = []
    protected_kept = 0

    # Group blocks into parents first, then split each parent into children. This
    # ordering is what makes every child's parent contiguous and coherent.
    parent_groups: list[list[Block]] = []
    current: list[Block] = []
    current_tokens = 0
    current_chapter: Optional[int] = None

    for block in blocks:
        # Never let a parent straddle a chapter boundary.
        if current and block.chapter_num != current_chapter:
            parent_groups.append(current)
            current, current_tokens = [], 0
        if current and current_tokens + block.tokens > parent_tokens:
            parent_groups.append(current)
            current, current_tokens = [], 0
        current.append(block)
        current_tokens += block.tokens
        current_chapter = block.chapter_num
    if current:
        parent_groups.append(current)

    ordinal = 0
    for group_index, group in enumerate(parent_groups):
        parent_id_ = parent_id(user_id, doc.source_id, group_index)
        parent_text = "\n\n".join(b.text for b in group)
        parents.append((parent_id_, user_id, doc.source_id, parent_text))

        # Pack consecutive blocks into children up to child_tokens, splitting only
        # oversized PROSE and never splitting a protected block.
        #
        # Emitting one child per block was wrong: a display equation like "x2 = m"
        # became a standalone three-token chunk with no surrounding argument. It
        # can never be a useful retrieval result on its own, and thousands of them
        # drag the median chunk down to 16 tokens and distort IDF across the whole
        # index. "Never split an equation" means it stays atomic, not that it gets
        # isolated from the prose that explains it.
        packed: list[tuple[list[str], Block]] = []
        buf: list[str] = []
        buf_tokens = 0
        anchor: Optional[Block] = None

        def emit() -> None:
            nonlocal buf, buf_tokens, anchor
            if buf and anchor is not None:
                packed.append((list(buf), anchor))
            buf, buf_tokens, anchor = [], 0, None

        for block in group:
            if block.protected:
                protected_kept += 1
            pieces = (
                [block.text]
                if block.protected or block.tokens <= child_tokens
                else _split_prose(block.text, child_tokens, child_overlap)
            )
            for piece in pieces:
                piece_tokens = _token_estimate(piece)
                if buf and buf_tokens + piece_tokens > child_tokens:
                    emit()
                if anchor is None:
                    anchor = block
                buf.append(piece)
                buf_tokens += piece_tokens
        emit()

        for parts, block in packed:
            for piece in ["\n".join(parts)]:
                chunks.append(
                    Chunk(
                        id=chunk_id(user_id, doc.source_id, ordinal),
                        user_id=user_id,
                        source_id=doc.source_id,
                        course_id=course_id,
                        chapter_num=block.chapter_num,
                        chapter_title=block.chapter_title,
                        section=block.section,
                        page_start=block.page,
                        page_end=block.page,
                        text=piece,
                        parent_id=parent_id_,
                        parent_text=parent_text,
                        kind=block.kind,
                        token_count=_token_estimate(piece),
                    )
                )
                ordinal += 1

    return ChunkingResult(chunks=chunks, parents=parents, protected_kept_whole=protected_kept)

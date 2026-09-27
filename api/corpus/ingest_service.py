"""Ingest uploaded materials for one user's course.

Replaces the seed-driven `ingest/run.py`. Nothing is configured ahead of time:
a course row is created because someone uploaded a file, its sources are whatever
they uploaded, and its capabilities are derived from the content.

Per file the pipeline is: extract (OCR-ing image-only pages) -> chunk -> embed ->
index, then chapter titles are embedded for semantic syllabus linking and the
course's data-link capability is derived from a sample of the corpus.

Every file gets a status. A document that could not be read is reported by name
with a reason and never contributes empty chunks.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

# on_progress(file_index, **patch): merge these fields into the job's per-file
# progress record. Patch keys: stage, pages_done, pages_total, chunks, status,
# detail. A no-op default keeps the synchronous callers unchanged.
ProgressFn = Optional[Callable[..., None]]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

from ..config import settings
from ..embed import Embedder, get_embedder
from ..models import Chunk, Course, Source, SourceType
from ..store import SQLiteStore, get_store
from ingest.chunk import chunk_document
from ingest.extract import ExtractionError, extract

def embedder_state_path(user_id: str) -> Path:
    """Per user. The local embedder is corpus-fitted, so one global state file
    means any user's ingest silently moves every other user's vectors into a
    different space.

    The user id becomes part of a filename, so it is sanitised *and* hashed:
    sanitising alone lets ".." address the parent directory, and lets two
    distinct ids ("a/b" and "a_b") collapse onto one file and silently share an
    embedder. The hash guarantees uniqueness; the readable prefix keeps the
    directory debuggable.
    """
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
    readable = re.sub(r"[^A-Za-z0-9-]", "_", user_id)[:32].strip("_.") or "user"
    return settings.db_path.parent / f"embedder_{readable}_{digest}.pkl"


@dataclass
class FileReport:
    filename: str
    source_id: str
    status: str            # ok | failed | unsupported
    detail: str = ""
    chunks: int = 0
    pages: int = 0
    chapters: int = 0
    ocr_pages: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "source_id": self.source_id,
            "status": self.status,
            "detail": self.detail,
            "chunks": self.chunks,
            "pages": self.pages,
            "chapters": self.chapters,
            "ocr_pages": self.ocr_pages,
        }


@dataclass
class IngestReport:
    course_id: str
    files: list[FileReport] = field(default_factory=list)
    total_chunks: int = 0
    has_data_link: bool = False
    data_link_reason: str = ""
    data_link_method: str = ""
    embedder: str = ""

    @property
    def failed(self) -> list[FileReport]:
        return [f for f in self.files if f.status != "ok"]

    def as_dict(self) -> dict[str, object]:
        return {
            "course_id": self.course_id,
            "files": [f.as_dict() for f in self.files],
            "total_chunks": self.total_chunks,
            "failed": [f.as_dict() for f in self.failed],
            "has_data_link": self.has_data_link,
            "data_link_reason": self.data_link_reason,
            "data_link_method": self.data_link_method,
            "embedder": self.embedder,
        }


# Reader errors a stranger will actually hit, in words they can act on.
_FAILURE_HINTS = (
    ("cannot open empty file", "the file is empty (0 bytes) -- the upload may have "
                               "been interrupted"),
    ("failed to open file", "the file is not a readable PDF. It may be corrupt, "
                            "truncated, or renamed from another format"),
    ("no objects found", "the PDF structure is damaged and cannot be opened"),
    ("password", "the file is password-protected. Remove the password and re-upload"),
    ("encrypted", "the file is encrypted. Remove the protection and re-upload"),
    ("unsupported file type", "that file type is not supported. Upload PDF, DOCX, "
                              "Markdown or plain text"),
)


def _user_message(exc: Exception, filename: str) -> str:
    """Plain-language reason, with no server paths in it.

    The raw exception names an absolute path inside the server's upload
    directory, which tells the user nothing and discloses layout.
    """
    raw = str(exc).lower()
    for needle, message in _FAILURE_HINTS:
        if needle in raw:
            return message
    # Unknown failure: keep the exception type, drop anything path-shaped.
    cleaned = re.sub(r"[\'\"]?(?:/[^\s'\"]+)+[\'\"]?", "the file", str(exc))
    return f"{type(exc).__name__}: {cleaned[:160]}"


def slugify(text: str, fallback: str = "course") -> str:
    slug = re.sub(r"[^a-z0-9]+", "", (text or "").lower())[:16]
    return slug or fallback


def _source_id(course_id: str, filename: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "_", Path(filename).stem.lower()).strip("_")[:28]
    return f"{course_id.lower()}_{stem or uuid.uuid4().hex[:6]}"


def _guess_type(filename: str) -> SourceType:
    lowered = filename.lower()
    if "bbplus" in lowered:
        return SourceType.BBPLUS
    if any(k in lowered for k in ("slide", "lecture", "deck", "ppt")):
        return SourceType.SLIDES
    if any(k in lowered for k in ("note", "handout")):
        return SourceType.NOTES
    if any(k in lowered for k in ("text", "book", "edition", "principles", "intro")):
        return SourceType.TEXTBOOK
    return SourceType.READINGS


def _fit_embedder(store: SQLiteStore, user_id: str, new_texts: Sequence[str]) -> Embedder:
    """Fit the embedder on this user's corpus, if it is a stateful one.

    A stateful (TF-IDF+SVD) embedder is corpus-fitted, so adding a source
    invalidates the old vector space and every chunk must be re-embedded together.
    A stateless embedder (BGE, OpenAI) maps into a fixed pretrained space with no
    fitting -- which is the whole reason for the switch: no per-user state file,
    and nothing to keep in sync, so the multi-tenancy bug class is gone.
    """
    embedder = get_embedder()
    if not embedder.stateful:
        return embedder
    existing = [c.embed_text for c in store.all_chunks(user_id)]
    corpus = list(new_texts) + existing
    embedder.fit(corpus or list(new_texts))
    embedder.save(embedder_state_path(user_id))
    return embedder  # stateful path only


def _chapter_rows_for(result, source_id: str, course_id: str) -> list[tuple]:
    """Chapter records for semantic syllabus linking. The embedded text is the
    title plus the chapter's opening prose: a bare title like "Choice" is too short
    to match a syllabus row like "Week 5: Consumer Choice"."""
    seen: dict[int, dict] = {}
    for chunk in result.chunks:
        if not chunk.chapter_num:
            continue
        entry = seen.setdefault(
            chunk.chapter_num,
            {"title": chunk.chapter_title, "start": chunk.page_start,
             "end": chunk.page_end, "text": []},
        )
        entry["end"] = max(entry["end"], chunk.page_end)
        entry["start"] = min(entry["start"], chunk.page_start)
        if len(" ".join(entry["text"])) < 900:
            entry["text"].append(chunk.text)
    rows = []
    for number, entry in sorted(seen.items()):
        blob = f"{entry['title']}. " + " ".join(entry["text"])[:900]
        rows.append((source_id, course_id, number, entry["title"],
                     entry["start"], entry["end"], blob))
    return rows


def ingest_files(
    paths: Sequence[Path],
    *,
    user_id: str,
    course_id: str,
    code: str = "",
    title: str = "",
    term: str = "",
    syllabus_text: str = "",
    store: Optional[SQLiteStore] = None,
    on_progress: ProgressFn = None,
    on_file_done: Optional[Callable[[], None]] = None,
) -> IngestReport:
    store = store or get_store()
    report = IngestReport(course_id=course_id)

    def report_progress(i: int, **patch) -> None:
        if on_progress:
            on_progress(i, **patch)

    course = store.course(user_id, course_id) or Course(
        course_id=course_id, user_id=user_id, code=code or course_id,
        title=title or course_id, term=term,
    )
    if code:
        course.code = code
    if title:
        course.title = title
    store.upsert_course(course)

    embedder = get_embedder()
    report.embedder = embedder.name
    # A stateless embedder (BGE, the product default) maps into a fixed space, so
    # each document can embed and persist independently -- which is what lets the
    # Ask tab enable per document as each finishes. A stateful (TF-IDF) embedder
    # must be fit on the whole corpus together, so those chunks are accumulated and
    # embedded in one pass at the end.
    per_file = not embedder.stateful

    pending: list[Chunk] = []          # for the stateful batch path
    pending_parents: list[tuple] = []
    pending_chapter_rows: list[tuple] = []
    corpus_sample: list[str] = []

    for i, path in enumerate(paths):
        filename = path.name
        source_id = _source_id(course_id, filename)
        report_progress(i, stage="reading", pages_done=0, pages_total=0)
        source = Source(
            source_id=source_id, user_id=user_id, course_id=course_id,
            title=Path(filename).stem.replace("_", " ")[:80],
            type=_guess_type(filename), file=str(path),
        )

        # Cache: an identical re-upload to the same course is already ingested.
        try:
            file_hash = _sha256(path)
        except Exception:  # noqa: BLE001
            file_hash = ""
        source.file_hash = file_hash
        if file_hash and store.source_with_hash(user_id, course_id, file_hash):
            report.files.append(FileReport(filename, source_id, "ok",
                                           detail="unchanged since last upload (reused)"))
            report_progress(i, stage="reused", status="ok",
                            detail="unchanged since last upload (reused)")
            continue

        def page_cb(stage, done, total, _i=i):
            report_progress(_i, stage=stage, pages_done=done, pages_total=total)

        try:
            doc = extract(path, source_id, on_page=page_cb)
        except ExtractionError as exc:
            detail = _user_message(exc, filename)
            source.status, source.status_detail = "unsupported", detail
            store.upsert_source(source)
            report.files.append(FileReport(filename, source_id, "unsupported", detail))
            report_progress(i, stage="failed", status="unsupported", detail=detail)
            continue
        except Exception as exc:  # noqa: BLE001
            # Any reader failure is this file's problem, not the upload's. A
            # corrupt or truncated PDF previously raised out of here, returned a
            # bare HTTP 500, and took every other file in the same batch with it.
            detail = _user_message(exc, filename)
            source.status, source.status_detail = "failed", detail
            store.upsert_source(source)
            report.files.append(FileReport(filename, source_id, "failed", detail))
            report_progress(i, stage="failed", status="failed", detail=detail)
            continue

        source.pages = doc.page_count
        source.ocr_pages = doc.ocr_pages

        if doc.failed or not doc.segments:
            source.status = "failed"
            source.status_detail = doc.failure_reason or "no readable text"
            store.upsert_source(source)
            report.files.append(
                FileReport(filename, source_id, "failed", source.status_detail,
                           pages=doc.page_count, ocr_pages=doc.ocr_pages)
            )
            report_progress(i, stage="failed", status="failed",
                            detail=source.status_detail)
            continue

        report_progress(i, stage="chunking", pages_done=doc.page_count,
                        pages_total=doc.page_count)
        result = chunk_document(
            doc, course_id=course_id, user_id=user_id,
            child_tokens=settings.retrieval.child_tokens,
            parent_tokens=settings.retrieval.parent_tokens,
            child_overlap=settings.retrieval.child_overlap,
        )
        store.upsert_source(source)
        store.clear_source(user_id, source_id)
        chapter_rows = _chapter_rows_for(result, source_id, course_id)
        fr = FileReport(filename, source_id, "ok", chunks=len(result.chunks),
                        pages=doc.page_count, chapters=len(chapter_rows),
                        ocr_pages=doc.ocr_pages)
        for warning in doc.warnings:
            if warning.startswith("OCR"):
                fr.detail = warning
        report.files.append(fr)
        corpus_sample.extend(c.text for c in result.chunks[:: max(1, len(result.chunks) // 20)][:20])

        if per_file:
            # Embed in sub-batches so the dominant stage shows a real, moving bar
            # ("passage 640 of 1239") instead of sitting on one label for ~20s.
            texts = [c.embed_text for c in result.chunks]
            total_c = len(texts)
            batch = 128
            report_progress(i, stage="embedding", pages_done=0, pages_total=total_c,
                            chunks=total_c)
            for s in range(0, total_c, batch):
                vecs = embedder.embed(texts[s:s + batch])
                for chunk, vector in zip(result.chunks[s:s + batch], vecs):
                    chunk.embedding = vector.tolist()
                report_progress(i, stage="embedding",
                                pages_done=min(s + batch, total_c),
                                pages_total=total_c, chunks=total_c)
            report_progress(i, stage="indexing", chunks=len(result.chunks))
            store.insert_parents(result.parents)
            store.insert_chunks(result.chunks)
            if chapter_rows:
                blobs = [r[6] for r in chapter_rows]
                cvecs = embedder.embed(blobs)
                store.replace_chapters(user_id, source_id, [
                    (r[1], r[2], r[3], r[4], r[5], v) for r, v in zip(chapter_rows, cvecs)
                ])
            report.total_chunks += len(result.chunks)
            report_progress(i, stage="done", status="ok", chunks=len(result.chunks))
            # This document is now searchable; let the caller enable Ask for it.
            if on_file_done:
                on_file_done()
        else:
            pending.extend(result.chunks)
            pending_parents.extend(result.parents)
            pending_chapter_rows.extend(chapter_rows)

    # Stateful embedder: one batch fit + embed over everything, then persist.
    if pending:
        embedder = _fit_embedder(store, user_id, [c.embed_text for c in pending])
        vectors = embedder.embed([c.embed_text for c in pending])
        for chunk, vector in zip(pending, vectors):
            chunk.embedding = vector.tolist()
        store.insert_parents(pending_parents)
        store.insert_chunks(pending)
        report.total_chunks += len(pending)
        _reembed_existing(store, user_id, embedder, {c.id for c in pending})
        if pending_chapter_rows:
            blobs = [r[6] for r in pending_chapter_rows]
            cvecs = embedder.embed(blobs)
            by_source: dict[str, list[tuple]] = {}
            for r, v in zip(pending_chapter_rows, cvecs):
                by_source.setdefault(r[0], []).append((r[1], r[2], r[3], r[4], r[5], v))
            for src, rows in by_source.items():
                store.replace_chapters(user_id, src, rows)
        if on_file_done:
            on_file_done()

    if report.total_chunks == 0:
        return report

    # Capability, derived from the corpus rather than the course code.
    from .capability import detect

    capability = detect(course.code, course.title, corpus_sample[:40], syllabus_text)
    course.has_data_link = capability.has_data_link
    course.data_link_reason = f"{capability.reason} [{capability.method}]"
    store.upsert_course(course)
    report.has_data_link = capability.has_data_link
    report.data_link_reason = capability.reason
    report.data_link_method = capability.method
    return report


def _reembed_existing(
    store: SQLiteStore, user_id: str, embedder: Embedder, skip: set[str]
) -> None:
    stale = [c for c in store.all_chunks(user_id) if c.id not in skip]
    if not stale:
        return
    vectors = embedder.embed([c.embed_text for c in stale])
    for chunk, vector in zip(stale, vectors):
        chunk.embedding = vector.tolist()
    store.insert_chunks(stale)

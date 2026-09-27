"""Instrument every ingest stage on a real PDF and print a table sorted by time.

    COPILOT_EMBEDDER=bge python scripts/profile_ingest.py materials/econ303/tb_varian.pdf

No guessing: this times the actual functions the pipeline calls, so the profile
decides what gets optimised. It writes to a throwaway temp DB so it never touches
the real corpus.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path

TIMES: dict[str, float] = {}
COUNTS: dict[str, str] = {}


@contextmanager
def stage(name: str):
    t0 = time.perf_counter()
    yield
    TIMES[name] = TIMES.get(name, 0.0) + (time.perf_counter() - t0)


def main(argv):
    path = Path(argv[1] if len(argv) > 1 else "materials/econ303/tb_varian.pdf")
    print(f"profiling ingest of {path} ...\n")

    import pymupdf

    from ingest.ocr import needs_ocr, ocr_pages
    from ingest.extract import _outline_structure, extract
    from ingest.chunk import chunk_document
    from api.embed import get_embedder

    # A. open
    with stage("pdf_open"):
        pdf = pymupdf.open(str(path))
        n = pdf.page_count
    COUNTS["pdf_open"] = f"{n} pages"

    # B. per-page text extraction
    with stage("text_extract (per-page get_text)"):
        page_text = [pdf.load_page(i).get_text("text") or "" for i in range(n)]

    # C. OCR detection
    with stage("ocr_detect (needs_ocr per page)"):
        needs = [i for i in range(n) if needs_ocr(page_text[i])]
    COUNTS["ocr_detect (needs_ocr per page)"] = f"{len(needs)} pages flagged"

    # D. OCR itself
    with stage("ocr_run"):
        ocr = ocr_pages(pdf, needs) if needs else None
    COUNTS["ocr_run"] = f"{ocr.succeeded}/{ocr.attempted} pages OCR'd" if ocr else "0 pages"

    # E. outline / chapter detection
    with stage("chapter_detect (outline)"):
        toc_ch, toc_sec, labels = _outline_structure(pdf)
    COUNTS["chapter_detect (outline)"] = f"{len(toc_ch)} chapters"
    pdf.close()

    # F. full extract() (real) -> segment building = full - (open+text+detect+ocr+outline)
    with stage("extract_full (real call)"):
        doc = extract(path, "profile_src")
    granular = sum(TIMES[k] for k in
                   ("pdf_open", "text_extract (per-page get_text)",
                    "ocr_detect (needs_ocr per page)", "ocr_run",
                    "chapter_detect (outline)"))
    TIMES["block/segment assembly"] = max(0.0, TIMES["extract_full (real call)"] - granular)
    COUNTS["block/segment assembly"] = f"{len(doc.segments)} segments"
    del TIMES["extract_full (real call)"]  # folded into its parts

    # G. chunk + protected-block/equation detection
    with stage("chunk_assembly (+equation/protected)"):
        result = chunk_document(doc, course_id="PROF", user_id="profuser",
                                child_tokens=400, parent_tokens=1200, child_overlap=64)
    COUNTS["chunk_assembly (+equation/protected)"] = f"{len(result.chunks)} chunks, {len(result.parents)} parents"

    # H. embedding (chunks)
    embedder = get_embedder()
    with stage("embed_chunks"):
        vecs = embedder.embed([c.embed_text for c in result.chunks])
    COUNTS["embed_chunks"] = f"{len(vecs)} vectors, {embedder.name}"

    # I. second embedding pass (chapter blobs)
    seen = {}
    for c in result.chunks:
        if c.chapter_num:
            seen.setdefault(c.chapter_num, []).append(c.text)
    blobs = [f"ch. " + " ".join(v)[:900] for v in seen.values()]
    with stage("embed_chapters (2nd pass)"):
        if blobs:
            embedder.embed(blobs)
    COUNTS["embed_chapters (2nd pass)"] = f"{len(blobs)} chapter blobs"

    # J. BM25 index build
    from api.retrieval.sparse import SparseIndex
    with stage("bm25_build"):
        SparseIndex(result.chunks)

    # K. database writes
    import tempfile
    from api.store.sqlite_store import SQLiteStore
    for c, v in zip(result.chunks, vecs):
        c.embedding = v.tolist()
    tmp = Path(tempfile.mkdtemp()) / "prof.db"
    st = SQLiteStore(tmp)
    with stage("db_writes"):
        st.insert_parents(result.parents)
        st.insert_chunks(result.chunks)
    COUNTS["db_writes"] = f"{len(result.chunks)} chunks + {len(result.parents)} parents"

    total = sum(TIMES.values())
    print(f"{'stage':<38} {'seconds':>9} {'% total':>8}   detail")
    print("-" * 90)
    for name, secs in sorted(TIMES.items(), key=lambda kv: -kv[1]):
        print(f"{name:<38} {secs:>9.2f} {100*secs/total:>7.1f}%   {COUNTS.get(name,'')}")
    print("-" * 90)
    print(f"{'TOTAL':<38} {total:>9.2f} {100.0:>7.1f}%")


if __name__ == "__main__":
    main(sys.argv)

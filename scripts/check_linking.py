"""Compare semantic syllabus->chapter linking against number matching.

The case this exists to measure: the ECON 303 syllabus cites Varian **10th
edition** chapter numbers while the PDF on disk is the **8th edition**. Chapter
numbers therefore disagree with the book; chapter titles do not.

    python -m scripts.check_linking --course ECON303

Reports, per syllabus row, what each method linked to, so a wrong semantic link
is visible rather than hidden behind an aggregate.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from api.config import ROOT
from api.store import get_store
from api.syllabus.commit import build_linker
from api.syllabus.extract import double_extract, read_syllabus
from api.syllabus.link import ChapterCandidate, number_only_link

BENCHMARK_USER = "benchmark"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Semantic vs number-only linking")
    parser.add_argument("--course", default="ECON303")
    parser.add_argument("--syllabus", help="path to the syllabus file")
    parser.add_argument("--user", default=BENCHMARK_USER)
    args = parser.parse_args(argv)

    store = get_store()
    path = Path(args.syllabus) if args.syllabus else None
    if path is None:
        folder = ROOT / "materials" / args.course.lower()
        path = next((p for p in folder.iterdir() if p.name.startswith("_syllabus")), None)
    if path is None or not path.exists():
        print(f"no syllabus found for {args.course}")
        return 1

    rows = store.chapters(args.user, args.course)
    if not rows:
        print(f"no chapters indexed for {args.course}; run `make benchmark` first")
        return 1
    candidates = [
        ChapterCandidate(r["course_id"], r["source_id"], r["chapter_num"], r["title"])
        for r in rows
    ]
    linker = build_linker(store, args.user, args.course)
    if linker is None:
        print("could not build a linker (no fitted embedder?)")
        return 1

    text = read_syllabus(path)
    extraction = double_extract(text, args.course, primary_source=None).extraction
    meetings = extraction.meetings

    # Many syllabi -- including this one -- give an undated "Course outline"
    # rather than a dated table. The offline heuristic extractor keys on dates
    # and finds nothing in them, so linking is tested against the outline rows
    # directly. This is a limitation of the OFFLINE extractor, not of linking;
    # the LLM double-extraction path reads these rows.
    if len([m for m in meetings if len(m.topic) > 12]) < 3:
        import re as _re
        from api.models import Meeting as _Meeting
        from datetime import date as _date

        rows = _re.findall(r"^\s*(\d{1,2})[.)]\s+(.+)$", text, _re.M)
        outline = []
        for _, body in rows:
            body = " ".join(body.split())
            if len(body) > 4:
                outline.append(_Meeting(date=_date(2026, 1, 1), topic=body, readings=[]))
        if len(outline) >= 3:
            print(f"(using {len(outline)} undated outline rows; "
                  f"the dated extractor found only {len(meetings)})\n")
            meetings = outline
    print(f"syllabus: {path.name}   rows: {len(meetings)}   chapters indexed: {len(candidates)}\n")

    header = f"{'date':<11} {'topic':<34} {'semantic':<26} {'number-only':<16}"
    print(header)
    print("-" * len(header))

    sem_linked = num_linked = agree = flagged = 0
    for meeting in meetings:
        semantic = linker.link(meeting.topic, meeting.readings, args.course)
        numeric = number_only_link(meeting.topic, meeting.readings, candidates, args.course)

        sem_text = (
            f"ch{semantic.chapter_ref.split(':')[-1]} {semantic.chapter_title[:16]} "
            f"{semantic.score:.2f}"
            if semantic.chapter_ref
            else f"FLAG ({semantic.score:.2f})"
        )
        num_text = (
            f"ch{numeric.chapter_ref.split(':')[-1]} {numeric.chapter_title[:10]}"
            if numeric.chapter_ref
            else "miss"
        )
        print(f"{meeting.date.isoformat():<11} {meeting.topic[:34]:<34} {sem_text:<26} {num_text:<16}")

        sem_linked += bool(semantic.chapter_ref)
        num_linked += bool(numeric.chapter_ref)
        flagged += semantic.needs_review
        if semantic.chapter_ref and numeric.chapter_ref:
            agree += semantic.chapter_ref == numeric.chapter_ref

    total = len(meetings) or 1
    print(
        f"\nsemantic linked   {sem_linked}/{total} ({sem_linked / total:.0%})"
        f"   flagged for review {flagged}"
    )
    print(f"number-only linked {num_linked}/{total} ({num_linked / total:.0%})")
    print(f"both linked and agreed on the chapter: {agree}")
    print(
        "\nNote: agreement is not accuracy. The syllabus cites 10e numbers against an "
        "8e PDF, so where the two methods disagree the *number* is the one expected "
        "to be wrong. Read the rows above rather than the totals."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

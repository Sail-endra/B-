"""Build the benchmark corpus by driving the product's own upload path.

The benchmark is deliberately not a special ingest mode. It calls
`ingest_files` exactly as the upload API does, so a change that breaks real
uploads breaks the benchmark too. The only thing that makes it a benchmark is
the fixed manifest and the hand-labelled question set beside it.

    python -m eval.benchmark.build            # build into the benchmark user
    python -m eval.benchmark.build --no-ocr   # skip OCR (much faster)

Files are referenced in place under `materials/`, never copied here: they are
purchased textbooks and licensed readings, and committing them would be
redistribution. A machine without them cannot run the benchmark, which the
script says plainly rather than silently producing an empty corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from api.config import ROOT
from api.corpus.ingest_service import ingest_files
from api.store import get_store

MANIFEST = Path(__file__).parent / "manifest.json"
BENCHMARK_USER = "benchmark"
_EXTS = {".pdf", ".docx", ".md", ".markdown", ".txt"}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the benchmark corpus")
    parser.add_argument("--no-ocr", action="store_true", help="skip OCR of scanned pages")
    parser.add_argument("--course", help="build only this course_id")
    args = parser.parse_args(argv)

    if args.no_ocr:
        import ingest.ocr as ocr_module

        ocr_module._engine_error = "OCR disabled for this run (--no-ocr)"

    manifest = json.loads(MANIFEST.read_text())
    store = get_store()
    user_id = manifest.get("user_id", BENCHMARK_USER)

    missing: list[str] = []
    totals = {"chunks": 0, "files": 0, "failed": 0}

    for course in manifest["courses"]:
        if args.course and course["course_id"] != args.course:
            continue
        directory = ROOT / course["dir"]
        if not directory.exists():
            missing.append(f"{course['course_id']}: {course['dir']} not found")
            continue

        paths = sorted(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in _EXTS and not p.name.startswith("_")
        )
        syllabus = next(
            (p for p in directory.iterdir() if p.name.startswith("_syllabus")), None
        )
        syllabus_text = ""
        if syllabus is not None:
            from api.syllabus.extract import read_syllabus

            try:
                syllabus_text = read_syllabus(syllabus)
            except Exception:  # noqa: BLE001 - a bad syllabus must not stop ingest
                syllabus_text = ""

        if not paths:
            missing.append(f"{course['course_id']}: no ingestable files in {course['dir']}")
            store.upsert_course_stub(
                user_id, course["course_id"], course["code"], course["title"]
            )
            continue

        print(f"\n== {course['course_id']} ({len(paths)} files) ==")
        report = ingest_files(
            paths,
            user_id=user_id,
            course_id=course["course_id"],
            code=course["code"],
            title=course["title"],
            syllabus_text=syllabus_text,
            store=store,
        )
        for file_report in report.files:
            flag = "  " if file_report.status == "ok" else "!!"
            print(
                f" {flag} {file_report.filename[:44]:46s} {file_report.status:11s} "
                f"{file_report.chunks:5d} chunks  {file_report.chapters:3d} ch"
                + (f"  ocr={file_report.ocr_pages}" if file_report.ocr_pages else "")
            )
            if file_report.status != "ok":
                print(f"      -> {file_report.detail[:110]}")
        expected = course.get("expect_data_link")
        agree = "" if expected is None else (
            "  [MATCHES manifest]" if report.has_data_link == expected
            else f"  [DISAGREES with manifest, expected {expected}]"
        )
        print(
            f"    data_link={report.has_data_link} via {report.data_link_method}{agree}\n"
            f"    reason: {report.data_link_reason[:110]}"
        )
        totals["chunks"] += report.total_chunks
        totals["files"] += len(report.files)
        totals["failed"] += len(report.failed)

    print(
        f"\nbenchmark: {totals['chunks']} chunks from {totals['files']} files "
        f"({totals['failed']} failed)"
    )
    if missing:
        print("\nMISSING INPUTS -- the benchmark is incomplete:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\nThe benchmark references the repo owner's own materials, which are not "
            "committed. Point eval/benchmark/manifest.json at a corpus you have.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

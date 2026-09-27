"""Document extraction.

Produces a flat list of `Segment`s -- text carrying its chapter, section and page
provenance. Three input paths:

  * PDF  (PyMuPDF), with a heuristic heading detector and an OCR-empty guard
  * DOCX (python-docx), using heading styles
  * MD / TXT, using explicit provenance comments -- the format the seed corpus uses

The page number matters more than it looks: eval gold labels are (source, page
range), so provenance is what keeps the question set valid across re-chunking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from .ocr import needs_ocr, ocr_pages

# on_page(stage, done, total): progress callback for the background job UI.
# stage is "extracting" (reading the text layer) or "ocr" (recognising pages).
ProgressFn = Optional[Callable[[str, int, int], None]]

# <!-- chapter: 11 | title: Aggregate Supply -->
_MD_CHAPTER = re.compile(r"<!--\s*chapter:\s*(\d+)\s*\|\s*title:\s*(.+?)\s*-->", re.I)
_MD_SECTION = re.compile(r"<!--\s*section:\s*(.+?)\s*-->", re.I)
_MD_PAGE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->", re.I)

# "Chapter 11", "CHAPTER 11 Aggregate Supply", "CHAPTER VI"
_PDF_CHAPTER = re.compile(r"^\s*chapter\s+(\d{1,2})\b[:.\s]*(.*)$", re.I)
_PDF_CHAPTER_ROMAN = re.compile(r"^\s*chapter\s+([IVXLC]{1,7})\b[:.\s]*(.*)$", re.I)
_PDF_SECTION = re.compile(r"^\s*(\d{1,2}\.\d{1,2})\s+(.+)$")

# Outline entry that names a chapter, e.g. "Chapter 1 The Market".
_TOC_CHAPTER = re.compile(r"^\s*chapter\s+(\d{1,2}|[IVXLC]{1,7})\b[:.\s]*(.*)$", re.I)
# Outline entry that is really a page label, e.g. "p. 168" in a JSTOR scan.
_TOC_PAGE_LABEL = re.compile(r"^\s*p+\.?\s*(\d{1,5})\s*$", re.I)

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def looks_like_chapter_heading(remainder: str) -> bool:
    """Does the text after "Chapter N" read as a title rather than a sentence?

    Prose refers to chapters constantly. The Voss preface contains the line
    "chapter 7 emphasizes the symbolic and cultural importance of El Presidio's",
    which the bare regex accepted -- inventing a chapter whose "title" was a
    fragment of somebody else's sentence, and making it the only chapter the
    whole ANTH course could offer the syllabus linker.

    A heading is short and begins like a title. A sentence continuation begins
    with a lowercase verb.
    """
    remainder = (remainder or "").strip()
    if not remainder:
        return True                      # "CHAPTER VI" on its own line
    if len(remainder) > 60:
        return False
    first = remainder[0]
    return first.isupper() or first.isdigit()


def roman_to_int(text: str) -> Optional[int]:
    """Novels number chapters in Roman numerals; "CHAPTER VI" must not be dropped."""
    text = (text or "").strip().upper()
    if not text or any(c not in _ROMAN for c in text):
        return None
    total, prev = 0, 0
    for char in reversed(text):
        value = _ROMAN[char]
        total = total - value if value < prev else total + value
        prev = max(prev, value)
    return total or None


@dataclass
class Segment:
    text: str
    page: int
    chapter_num: int = 0
    chapter_title: str = ""
    section: str = ""


@dataclass
class ExtractedDoc:
    source_id: str
    path: Path
    segments: list[Segment] = field(default_factory=list)
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)
    ocr_pages: int = 0
    ocr_unavailable: bool = False
    failed: bool = False
    failure_reason: str = ""

    @property
    def chapters(self) -> set[int]:
        return {s.chapter_num for s in self.segments if s.chapter_num}


class ExtractionError(RuntimeError):
    pass


def extract(path: Path, source_id: str, on_page: ProgressFn = None) -> ExtractedDoc:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path, source_id, on_page)
    if suffix == ".docx":
        return _extract_docx(path, source_id)
    if suffix in {".md", ".markdown", ".txt"}:
        return _extract_markdown(path, source_id)
    raise ExtractionError(f"unsupported file type {suffix!r} for {path}")


# ---------------------------------------------------------------------------


def _extract_markdown(path: Path, source_id: str) -> ExtractedDoc:
    doc = ExtractedDoc(source_id=source_id, path=path)
    chapter_num, chapter_title, section, page = 0, "", "", 1
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if body:
            doc.segments.append(
                Segment(body, page, chapter_num, chapter_title, section)
            )
        buffer = []

    for line in path.read_text(encoding="utf-8").splitlines():
        if (m := _MD_CHAPTER.search(line)) is not None:
            flush()
            chapter_num, chapter_title = int(m.group(1)), m.group(2).strip()
            section = ""
            continue
        if (m := _MD_SECTION.search(line)) is not None:
            flush()
            section = m.group(1).strip()
            continue
        if (m := _MD_PAGE.search(line)) is not None:
            flush()
            page = int(m.group(1))
            continue
        buffer.append(line)
    flush()

    doc.page_count = max((s.page for s in doc.segments), default=0)
    if not doc.segments:
        doc.warnings.append("no segments extracted")
    return doc


def _extract_docx(path: Path, source_id: str) -> ExtractedDoc:
    import docx

    doc = ExtractedDoc(source_id=source_id, path=path)
    document = docx.Document(str(path))
    chapter_num, chapter_title, section = 0, "", ""
    buffer: list[str] = []
    # DOCX has no page concept without rendering; approximate ~500 words/page so
    # gold page ranges stay meaningful and monotonic.
    words = 0

    def page_of() -> int:
        return words // 500 + 1

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if body:
            doc.segments.append(Segment(body, page_of(), chapter_num, chapter_title, section))
        buffer = []

    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower()
        if "heading 1" in style or (m := _PDF_CHAPTER.match(text)):
            flush()
            m = _PDF_CHAPTER.match(text)
            if m:
                chapter_num = int(m.group(1))
                chapter_title = m.group(2).strip() or chapter_title
            else:
                chapter_title = text
            section = ""
            continue
        if "heading 2" in style or _PDF_SECTION.match(text):
            flush()
            section = text
            continue
        buffer.append(text)
        words += len(text.split())
    flush()

    doc.page_count = page_of()
    if not doc.segments:
        doc.warnings.append("no segments extracted from docx")
    return doc


def _outline_structure(pdf) -> tuple[dict[int, tuple[int, str]], dict[int, str], dict[int, int]]:
    """Read chapter/section structure from the PDF outline.

    Far more reliable than matching headings in page text, which on a real
    textbook mostly finds *running headers*. Varian's pages begin with a
    section title in caps and a page number; nothing on a content page says
    "Chapter 14". Regex over that produces chapter numbers carried over from a
    handful of accidental matches -- silently wrong citations on every page.

    Returns (page -> (chapter_num, chapter_title), page -> section,
             pdf_page -> printed_page).

    The third map handles journal scans whose outline is a list of page labels
    ("p. 168", "p. 169"): it lets a citation quote the page number actually
    printed on the article rather than the PDF's index.
    """
    chapters: dict[int, tuple[int, str]] = {}
    sections: dict[int, str] = {}
    page_labels: dict[int, int] = {}
    try:
        toc = pdf.get_toc() or []
    except Exception:  # noqa: BLE001 - a malformed outline must not fail ingest
        return chapters, sections, page_labels

    for level, title, page in toc:
        if page is None or page < 1:
            continue
        if (m := _TOC_PAGE_LABEL.match(title or "")) is not None:
            page_labels[page] = int(m.group(1))
            continue
        if (m := _TOC_CHAPTER.match(title or "")) is not None:
            raw = m.group(1)
            number = int(raw) if raw.isdigit() else roman_to_int(raw)
            if number:
                chapters[page] = (number, m.group(2).strip() or title.strip())
            continue
        if level >= 2:
            sections[page] = (title or "").strip()
    return chapters, sections, page_labels


def _looks_like_running_header(line: str, page_no: int) -> bool:
    """A running header is a short all-caps section title, or a bare page number.

    Both appear at the top of every page of a typeset book and carry no content;
    left in, they pollute every chunk's text and the BM25 index with the same
    repeated tokens.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.isdigit() and len(stripped) <= 4:
        return True
    letters = [c for c in stripped if c.isalpha()]
    return (
        len(stripped) < 60
        and len(letters) >= 3
        and all(c.isupper() for c in letters)
    )


def _extract_pdf(path: Path, source_id: str, on_page: ProgressFn = None) -> ExtractedDoc:
    import pymupdf

    doc = ExtractedDoc(source_id=source_id, path=path)
    pdf = pymupdf.open(str(path))
    doc.page_count = pdf.page_count
    total = pdf.page_count

    toc_chapters, toc_sections, page_labels = _outline_structure(pdf)
    use_outline = bool(toc_chapters)
    if use_outline:
        doc.warnings.append(
            f"chapter structure taken from the PDF outline ({len(toc_chapters)} chapters)"
        )

    chapter_num, chapter_title, section = 0, "", ""
    empty_pages = 0

    # Pass 1: read the embedded text layer and find pages that have none.
    page_text: list[str] = []
    needs: list[int] = []
    for page_index in range(pdf.page_count):
        page = pdf.load_page(page_index)
        raw = page.get_text("text") or ""
        raw = _repair_stacked_fractions(page, raw)
        page_text.append(raw)
        if needs_ocr(raw):
            needs.append(page_index)
        # Report every ~2% so the UI shows "extracting, page X of Y" without
        # flooding the job store with writes.
        if on_page and (page_index % max(1, total // 50) == 0 or page_index == total - 1):
            on_page("extracting", page_index + 1, total)

    # More than a quarter of pages need OCR: tell the user up front that this
    # document will be slow, with an estimate, rather than silently grinding.
    if on_page and total and len(needs) / total > 0.25:
        on_page("ocr_warn", len(needs), total)

    # Pass 2: OCR only the image-only pages. A text-native PDF skips this
    # entirely; a scan pays ~1.7s a page once, at upload.
    if needs:
        ocr = ocr_pages(pdf, needs, on_page=on_page)
        doc.ocr_pages = ocr.succeeded
        if not ocr.available:
            # Not fatal on its own. A 806-page text-native textbook with four
            # image-only plates must not be reported as a failed upload because
            # the OCR engine was missing -- only a document with no readable text
            # at all has actually failed, and that is decided after extraction.
            doc.warnings.append(
                f"{len(needs)} image-only page(s) skipped: {ocr.error}"
            )
            doc.ocr_unavailable = True
        else:
            for index, text in ocr.pages.items():
                page_text[index] = text
            doc.warnings.append(
                f"OCR: {ocr.succeeded}/{ocr.attempted} image-only pages recovered "
                f"in {ocr.seconds:.0f}s"
            )
            if ocr.failed:
                doc.warnings.append(
                    f"OCR could not read {ocr.failed} page(s)"
                    + (f": {ocr.error}" if ocr.error else "")
                )

    for page_index in range(pdf.page_count):
        raw = page_text[page_index]
        page_no = page_index + 1

        # Outline entries are keyed on 1-based PDF page numbers.
        if page_no in toc_chapters:
            chapter_num, chapter_title = toc_chapters[page_no]
            section = ""
        if page_no in toc_sections:
            section = toc_sections[page_no]
        printed_page = page_labels.get(page_no, page_no)

        if not raw.strip():
            empty_pages += 1
            continue

        buffer: list[str] = []

        def flush() -> None:
            nonlocal buffer
            body = "\n".join(buffer).strip()
            if body:
                doc.segments.append(
                    Segment(body, printed_page, chapter_num, chapter_title, section)
                )
            buffer = []

        for line_no, line in enumerate(raw.splitlines()):
            stripped = line.strip()
            if not stripped:
                buffer.append("")
                continue
            # Only the first couple of lines can be a running header.
            if line_no < 2 and _looks_like_running_header(stripped, page_no):
                continue
            if not use_outline and len(stripped) < 90:
                if (m := _PDF_CHAPTER.match(stripped)) is not None:
                    if not looks_like_chapter_heading(m.group(2)):
                        buffer.append(stripped)
                        continue
                    flush()
                    chapter_num = int(m.group(1))
                    chapter_title = m.group(2).strip() or chapter_title
                    section = ""
                    continue
                if (m := _PDF_CHAPTER_ROMAN.match(stripped)) is not None:
                    if not looks_like_chapter_heading(m.group(2)):
                        buffer.append(stripped)
                        continue
                    if (number := roman_to_int(m.group(1))) is not None:
                        flush()
                        chapter_num = number
                        chapter_title = m.group(2).strip() or f"Chapter {m.group(1).upper()}"
                        section = ""
                        continue
                if (m := _PDF_SECTION.match(stripped)) is not None:
                    flush()
                    section = stripped
                    continue
            buffer.append(stripped)
        flush()

    pdf.close()

    if doc.page_count and empty_pages / doc.page_count > 0.5:
        doc.warnings.append(
            f"{empty_pages}/{doc.page_count} pages still produced no text after OCR"
        )
    if not doc.segments:
        # Reported, not raised: one unreadable file must not abort a whole upload,
        # and the user needs to be told which file and why.
        doc.failed = True
        doc.failure_reason = doc.failure_reason or (
            "image-only document and the OCR engine is unavailable; install "
            "rapidocr-onnxruntime or re-run without --no-ocr"
            if doc.ocr_unavailable
            else "no extractable text, and OCR recovered nothing. The file may be "
            "a photograph, encrypted, or blank."
        )
    return doc


def _repair_stacked_fractions(page, raw: str) -> str:
    """Use PDF glyph geometry to restore simple stacked fractions.

    PyMuPDF's plain-text view emits the numerator and denominator on separate
    lines. When short equation-like rows are vertically stacked and horizontally
    aligned, join them as a TeX fraction before chunking and storage.
    """
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:  # noqa: BLE001 - keep ordinary extraction available
        return raw
    lines = []
    for block in blocks:
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if text:
                x0, y0, x1, y1 = line["bbox"]
                lines.append((text, x1, (y0 + y1) / 2))
    text_lines = raw.splitlines()
    replacements = []
    for i, (top, x_top, y_top) in enumerate(lines[:-1]):
        for bottom, x_bottom, y_bottom in lines[i + 1:]:
            if not (5 <= y_bottom - y_top <= 25):
                if y_bottom - y_top > 25:
                    break
                continue
            if abs(x_top - x_bottom) > 10:
                continue
            denominator = bottom.rstrip(".,;:")
            if len(denominator) > 48 or not re.fullmatch(r"[A-Za-z0-9().'′^{}]+", denominator):
                continue
            if "=" in top:
                numerator_text = top.rsplit("=", 1)[1].strip()
            else:
                numerator = re.search(r"([A-Za-z0-9]+)\s*$", top)
                numerator_text = numerator.group(1) if numerator else ""
            if not numerator_text or not re.search(r"[=+−-]", top):
                continue
            replacements.append((numerator_text, denominator))
            break
    for numerator, denominator in replacements:
        pattern = re.compile(rf"(?<![A-Za-z0-9])({re.escape(numerator)})\s*\n\s*{re.escape(denominator)}(?=[\s.,;:]|$)")
        raw, _ = pattern.subn(lambda _: f"\\frac{{{numerator}}}{{{denominator}}}", raw, count=1)
    return raw


def iter_corpus(corpus_dir: Path) -> Iterator[Path]:
    for path in sorted(corpus_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in {
            ".pdf",
            ".docx",
            ".md",
            ".markdown",
            ".txt",
        }:
            yield path

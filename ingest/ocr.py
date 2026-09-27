"""OCR for image-only PDF pages.

A required pipeline stage, not an optional preprocessing step a user is told to
run themselves. Telling a stranger to `brew install ocrmypdf` is the same as
telling them the upload failed, so the engine here is `rapidocr-onnxruntime`:
pure Python, ONNX models bundled in the wheel, no system binary, no Homebrew.

Three rules:

  * Never silently ingest an empty page. A scanned document that could not be
    read is reported by filename with a reason, and its source row is marked
    `failed` so the upload UI can show it.
  * OCR only what needs it. Text is extracted first, always; only a page whose
    extracted text is essentially empty (< `_EMPTY_PAGE_CHARS`) is rendered and
    recognised. Running OCR over a text-native PDF wastes minutes.
  * A scan-heavy document is OCR'd across a process pool, not one page at a time.
    OCR is CPU-bound and single-threaded per call, so a 400-page scan that took
    ~11 min sequentially finishes in roughly (cores) times less wall time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

# Below this many characters, a page is treated as image-only.
_EMPTY_PAGE_CHARS = 40
# Rendering DPI. 180 is enough for body text and roughly halves the time of 300.
_DPI = 180
# Use a process pool once at least this many pages need OCR; below it the pool's
# per-worker engine-load cost (~1.5s each) outweighs the parallelism.
_POOL_THRESHOLD = 24
# Leave one core for the main thread / OS.
_WORKERS = max(1, (os.cpu_count() or 2) - 1)


@dataclass
class OCRResult:
    available: bool
    attempted: int = 0
    succeeded: int = 0
    pages: dict[int, str] = field(default_factory=dict)
    error: str = ""
    seconds: float = 0.0

    @property
    def failed(self) -> int:
        return self.attempted - self.succeeded


_engine = None
_engine_error: Optional[str] = None


def _thread_count() -> int:
    return max(1, os.cpu_count() or 1)


def _build_engine():
    """Construct a RapidOCR engine, pinning ONNX to all cores.

    rapidocr-onnxruntime defaults its ONNX sessions to a single intra-op thread,
    which leaves most of the CPU idle during recognition. Passing the thread count
    for each of the three models (det/cls/rec) uses the whole machine. The kwargs
    are wrapped because their names have changed across rapidocr versions."""
    from rapidocr_onnxruntime import RapidOCR

    n = _thread_count()
    os.environ.setdefault("OMP_NUM_THREADS", str(n))
    for kwargs in (
        {"intra_op_num_threads": n},  # newer rapidocr
        {"det_use_num_threads": n, "cls_use_num_threads": n, "rec_use_num_threads": n},
        {},  # last resort: default threads, still correct, just slower
    ):
        try:
            return RapidOCR(**kwargs)
        except TypeError:
            continue
    return RapidOCR()


def _get_engine():
    """Load once. Import is deferred because the ONNX runtime costs ~1s and most
    documents never need it."""
    global _engine, _engine_error
    if _engine is not None or _engine_error is not None:
        return _engine
    try:
        _engine = _build_engine()
    except Exception as exc:  # noqa: BLE001 - reported, never raised into ingest
        _engine_error = (
            f"OCR engine unavailable: {exc}. Install with "
            "`pip install rapidocr-onnxruntime` (no system packages required)."
        )
    return _engine


def ocr_available() -> tuple[bool, str]:
    engine = _get_engine()
    return (engine is not None), (_engine_error or "")


def needs_ocr(page_text: Optional[str]) -> bool:
    return len((page_text or "").strip()) < _EMPTY_PAGE_CHARS


def _page_to_array(pdf, index: int):
    """Render a PDF page to an HxWx3 uint8 array."""
    import numpy as np

    pixmap = pdf.load_page(index).get_pixmap(dpi=_DPI)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )
    if pixmap.n == 4:      # RGBA -> RGB
        image = image[:, :, :3]
    elif pixmap.n == 1:    # greyscale -> RGB
        image = np.repeat(image, 3, axis=2)
    return np.ascontiguousarray(image)


def _recognise(image) -> str:
    """OCR one image with this process's engine (loaded once per worker)."""
    engine = _get_engine()
    if engine is None:
        return ""
    lines, _ = engine(image)
    return "\n".join(entry[1] for entry in (lines or []))


def _worker(payload):
    """Process-pool worker: (index, ndarray) -> (index, text). Each worker loads
    its own engine lazily on first call and reuses it for the rest of its pages."""
    index, image = payload
    try:
        return index, _recognise(image)
    except Exception:  # noqa: BLE001 - a bad page must not kill the pool
        return index, ""


def ocr_pages(
    pdf, page_indices: list[int], max_pages: int = 400,
    on_page: Optional[Callable[[str, int, int], None]] = None,
) -> OCRResult:
    """OCR the given 0-based page indices of an open PyMuPDF document.

    Sequential for a handful of pages; across a process pool once the count
    crosses `_POOL_THRESHOLD`. `on_page("ocr", done, total)` reports progress."""
    import time

    if not page_indices:
        return OCRResult(available=True)

    engine = _get_engine()
    if engine is None:
        return OCRResult(available=False, attempted=len(page_indices),
                         error=_engine_error or "")

    todo = page_indices[:max_pages]
    total = len(todo)
    result = OCRResult(available=True)
    started = time.time()

    def keep(index: int, text: str) -> None:
        result.attempted += 1
        if len(text.strip()) >= _EMPTY_PAGE_CHARS:
            result.pages[index] = text
            result.succeeded += 1

    if total < _POOL_THRESHOLD:
        for done, index in enumerate(todo, 1):
            try:
                keep(index, _recognise(_page_to_array(pdf, index)))
            except Exception as exc:  # noqa: BLE001
                result.attempted += 1
                if not result.error:
                    result.error = f"page {index + 1}: {exc}"
            if on_page:
                on_page("ocr", done, total)
    else:
        # Render in the main process (PyMuPDF is fast, releases little GIL), OCR
        # in workers. Rendered arrays are picklable; the PDF object is not.
        from concurrent.futures import ProcessPoolExecutor

        images = []
        for index in todo:
            try:
                images.append((index, _page_to_array(pdf, index)))
            except Exception as exc:  # noqa: BLE001
                result.attempted += 1
                if not result.error:
                    result.error = f"page {index + 1}: {exc}"
        done = 0
        try:
            with ProcessPoolExecutor(max_workers=_WORKERS) as pool:
                for index, text in pool.map(_worker, images):
                    keep(index, text)
                    done += 1
                    if on_page:
                        on_page("ocr", done, total)
        except Exception as exc:  # noqa: BLE001 - fall back to sequential
            for index, image in images[done:]:
                keep(index, _recognise(image))
                done += 1
                if on_page:
                    on_page("ocr", done, total)
            if not result.error:
                result.error = f"process pool degraded to sequential: {exc}"

    if len(page_indices) > max_pages:
        result.error = (
            f"{len(page_indices)} pages needed OCR; stopped after {max_pages}. "
            "Split the document or raise max_pages."
        )
    result.seconds = time.time() - started
    return result

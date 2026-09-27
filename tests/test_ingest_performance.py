from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_bge_embedder_is_reused_for_the_same_model(monkeypatch):
    import api.config
    import api.embed
    import api.embed.bge_embed as bge_module

    created = []

    class FakeBge:
        def __init__(self, model_name):
            self.name = model_name
            created.append(model_name)

    monkeypatch.setattr(api.config, "settings", SimpleNamespace(
        embedder_mode="bge", bge_model="test/model", has_openai=False
    ))
    monkeypatch.setattr(bge_module, "BgeEmbedder", FakeBge)
    api.embed._clear_embedder_cache()
    try:
        first = api.embed.get_embedder()
        second = api.embed.get_embedder()
        assert first is second
        assert created == ["test/model"]
    finally:
        api.embed._clear_embedder_cache()


def test_native_text_pdf_does_not_invoke_ocr(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    import ingest.extract as extraction

    path = tmp_path / "native.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((40, 50), "A native text page with more than forty characters for OCR fallback testing.")
    document.save(path)
    document.close()

    def unexpected_ocr(*_args, **_kwargs):
        raise AssertionError("OCR must not run for a native-text PDF page")

    monkeypatch.setattr(extraction, "ocr_pages", unexpected_ocr)
    result = extraction.extract(path, "native")
    assert result.segments
    assert result.ocr_pages == 0
    assert result.ocr_seconds == 0


def test_scanned_pdf_falls_back_to_ocr_and_keeps_recovered_text(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    import ingest.extract as extraction

    path = tmp_path / "scanned.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()
    calls = []

    def recover_page(pdf, indices, on_page=None):
        calls.append(indices)
        return SimpleNamespace(
            available=True, attempted=len(indices), succeeded=len(indices),
            pages={0: "Recovered scanned page text with enough characters for indexing."},
            seconds=0.25, failed=0, error="",
        )

    monkeypatch.setattr(extraction, "ocr_pages", recover_page)
    result = extraction.extract(path, "scanned")
    assert calls == [[0]]
    assert result.ocr_pages == 1
    assert result.ocr_seconds == 0.25
    assert "Recovered scanned page text" in result.segments[0].text

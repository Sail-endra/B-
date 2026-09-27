"""Server-side guarantees the automatic BB Plus setup lifecycle relies on.

The extension drives discover → map → sync → compile automatically and re-runs
that sweep on every Blackboard/sidebar load. These tests pin the invariants that
make repeated, unattended runs safe:

  * creating + mapping a Blackboard course is idempotent (no duplicate courses
    or mappings across reloads);
  * re-syncing unchanged materials does not duplicate sources or chunks;
  * two mapped courses stay isolated at question time.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("COPILOT_OFFLINE", "1")
os.environ.setdefault("COPILOT_EMBEDDER", "tfidf")

import pytest
from fastapi.testclient import TestClient

from api import main
from api.store.sqlite_store import SQLiteStore


def _doc(item_id: str, topic: str, course_id: str = "") -> dict:
    """A Blackboard document whose serialized text is long enough to retrieve."""
    return {
        "item_id": item_id,
        "course_id": course_id,
        "title": f"{topic} reading",
        "source_type": "html",
        "blocks": [
            {"type": "heading", "level": 2, "text": topic},
            {"type": "paragraph", "page": 1,
             "text": (f"{topic} is the central idea of this lesson. " * 40)},
        ],
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "autosetup.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "alice")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(
        "api.corpus.ingest_service.embedder_state_path",
        lambda user: tmp_path / f"embedder_{user}.pkl",
    )
    api_client = TestClient(main.app)
    try:
        yield api_client
    finally:
        api_client.close()
        store.close()


def _create_and_map(client: TestClient, bb_id: str, code: str, term: str = "Fall 2026") -> str:
    response = client.post(
        f"/api/integrations/bbplus/course-mappings/{bb_id}/create",
        json={"code": code, "title": f"{code} course", "term": term},
    )
    assert response.status_code == 200, response.text
    return response.json()["course_id"]


def _sync_and_wait(client: TestClient, bb_id: str, documents: list[dict]) -> dict:
    response = client.post(
        f"/api/integrations/bbplus/course-mappings/{bb_id}/materials",
        json={"documents": documents},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    if body.get("job_id"):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{body['job_id']}").json()
            if job["status"] in {"done", "failed"}:
                assert job["status"] == "done", job
                break
            time.sleep(0.02)
    return body


def test_create_and_map_is_idempotent_across_reloads(client):
    first = _create_and_map(client, "bb-econ-304", "ECON 304")
    # A reload re-runs the sweep and calls create/map again for the same course.
    second = _create_and_map(client, "bb-econ-304", "ECON 304")
    assert first == second

    state = client.get("/api/integrations/bbplus/state").json()
    matching_courses = [c for c in state["courses"] if c["course_id"] == first]
    mappings = [m for m in state["mappings"] if m["blackboard_course_id"] == "bb-econ-304"]
    assert len(matching_courses) == 1
    assert len(mappings) == 1
    assert mappings[0]["course_id"] == first


def test_repeated_material_sync_does_not_duplicate_sources_or_chunks(client):
    course_id = _create_and_map(client, "bb-econ-304", "ECON 304")
    _sync_and_wait(client, "bb-econ-304", [_doc("item-1", "elasticity of demand")])

    state = client.get("/api/integrations/bbplus/state").json()
    course = next(c for c in state["courses"] if c["course_id"] == course_id)
    chunks_before = course["chunks"]
    assert chunks_before > 0
    assert len(course["sources"]) == 1

    # Re-open Blackboard: the identical material is synced again.
    _sync_and_wait(client, "bb-econ-304", [_doc("item-1", "elasticity of demand")])
    state = client.get("/api/integrations/bbplus/state").json()
    course = next(c for c in state["courses"] if c["course_id"] == course_id)
    assert course["chunks"] == chunks_before
    assert len(course["sources"]) == 1


def test_two_mapped_courses_stay_isolated_at_question_time(client):
    econ = _create_and_map(client, "bb-econ-304", "ECON 304")
    bio = _create_and_map(client, "bb-bio-201", "BIO 201")
    _sync_and_wait(client, "bb-econ-304", [_doc("econ-1", "elasticity of demand")])
    _sync_and_wait(client, "bb-bio-201", [_doc("bio-1", "photosynthesis in plants")])

    state = client.get("/api/integrations/bbplus/state").json()
    bio_sources = {
        s["source_id"]
        for c in state["courses"] if c["course_id"] == bio
        for s in c["sources"]
    }
    assert bio_sources

    # Asking ECON its own topic must never cite the biology course's sources.
    answer = client.post(
        "/api/integrations/bbplus/course-mappings/bb-econ-304/ask",
        json={"question": "Explain elasticity of demand", "depth": "concise"},
    ).json()
    cited = {c.get("source_id") for c in answer.get("citations", [])}
    assert cited.isdisjoint(bio_sources)
    assert "photosynthesis" not in (answer.get("answer") or "").lower()

    # Asking ECON about the biology topic cannot pull biology sources across.
    cross = client.post(
        "/api/integrations/bbplus/course-mappings/bb-econ-304/ask",
        json={"question": "Describe photosynthesis in plants", "depth": "concise"},
    ).json()
    cross_cited = {c.get("source_id") for c in cross.get("citations", [])}
    assert cross_cited.isdisjoint(bio_sources)


def test_cited_source_file_is_served_and_scoped_to_its_course(client):
    econ = _create_and_map(client, "bb-econ-304", "ECON 304")
    _create_and_map(client, "bb-bio-201", "BIO 201")
    _sync_and_wait(client, "bb-econ-304", [_doc("econ-1", "elasticity of demand")])

    state = client.get("/api/integrations/bbplus/state").json()
    econ_source = next(
        s["source_id"] for c in state["courses"] if c["course_id"] == econ for s in c["sources"]
    )

    # The citation's link resolves to the stored file, served inline.
    ok = client.get(f"/api/integrations/bbplus/course-mappings/bb-econ-304/sources/{econ_source}/file")
    assert ok.status_code == 200, ok.text
    assert "inline" in ok.headers.get("content-disposition", "")
    assert "elasticity" in ok.text.lower()

    # A source cannot be opened through a different Blackboard course's mapping.
    leaked = client.get(f"/api/integrations/bbplus/course-mappings/bb-bio-201/sources/{econ_source}/file")
    assert leaked.status_code == 404


def test_file_endpoint_extracts_pdf_server_side(client):
    """A raw PDF sent to the file endpoint is extracted by the backend (PyMuPDF),
    ingested into the course, and becomes retrievable — the path that also does
    OCR for scanned/handwritten pages when a tesseract engine is present."""
    import base64
    import pymupdf

    _create_and_map(client, "bb-anth-201", "ANTH 201")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72),
                     "What is archaeology? Archaeology is the study of the human past "
                     "through material remains and their context. " * 6)
    payload = base64.b64encode(doc.tobytes()).decode()

    reply = client.post(
        "/api/integrations/bbplus/course-mappings/bb-anth-201/materials/files",
        json={"files": [{"item_id": "slides-week2", "title": "Week 2 Slides",
                         "filename": "Week 2 Slides.pdf", "content_base64": payload}]},
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    job_id = body.get("job_id")
    if job_id:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"done", "failed"}:
                assert job["status"] == "done", job
                break
            time.sleep(0.02)

    state = client.get("/api/integrations/bbplus/state").json()
    course = next(c for c in state["courses"] if c["course_id"].startswith("anth"))
    assert course["chunks"] > 0
    assert any("slides" in (s["title"] or "").lower() or s["source_id"].startswith("anth")
               for s in course["sources"])
    # A non-PDF/DOCX is skipped by this endpoint (it uses the blocks path).
    bad = client.post(
        "/api/integrations/bbplus/course-mappings/bb-anth-201/materials/files",
        json={"files": [{"item_id": "x", "title": "x", "filename": "notes.txt", "content_base64": "YWJj"}]},
    )
    assert bad.status_code == 200
    assert bad.json()["job_id"] == "" and bad.json()["skipped"]


def test_source_id_embeds_item_hash_so_the_extension_can_link_to_blackboard():
    """The sidebar maps a citation back to its Blackboard page by recomputing
    sha256(itemId)[:16] and matching it in the source id. Pin that derivation so
    the two sides cannot drift apart."""
    import hashlib
    from api.corpus.ingest_service import _source_id
    from api.integrations.bbplus import safe_material_filename

    item_id = "_9040_1::document::week-3-elasticity"
    expected = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:16]
    filename = safe_material_filename(item_id, "Week 3 — Elasticity")
    source_id = _source_id("econ304_bbx_ab12cd34", filename)
    assert f"bbplus_{expected}" in source_id

from __future__ import annotations

import os
import time

os.environ.setdefault("COPILOT_OFFLINE", "1")
os.environ.setdefault("COPILOT_EMBEDDER", "tfidf")

from fastapi.testclient import TestClient

from api import main
from api.corpus.ingest_service import _guess_type, ingest_files
from api.models import SourceType
from api.retrieval.pipeline import RetrievalPipeline
from api.store.sqlite_store import SQLiteStore


def _text(topic: str) -> str:
    return (
        f"Chapter: {topic}\nPage: 1\n"
        + (f"{topic} explains consumer choice, budget constraints, and utility. " * 45)
    )


def test_bbplus_filename_detection_does_not_change_regular_material_types():
    assert _guess_type("bbplus_a12bc34d_elasticity.txt") is SourceType.BBPLUS
    assert _guess_type("textbook_varian.md") is SourceType.TEXTBOOK
    assert _guess_type("lecture_slides.txt") is SourceType.SLIDES


def test_bbplus_ingests_chunks_retrieves_and_reuses_identical_upload(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "bbplus.sqlite")
    monkeypatch.setattr(
        "api.corpus.ingest_service.embedder_state_path",
        lambda user: tmp_path / f"embedder_{user}.pkl",
    )
    try:
        path = tmp_path / "bbplus_a12bc34d_elasticity.txt"
        path.write_text(_text("elasticity of demand"), encoding="utf-8")
        first = ingest_files([path], user_id="alice", course_id="ECON303", store=store)
        assert first.files[0].status == "ok"
        assert first.total_chunks > 0
        source = store.sources("alice", "ECON303")[0]
        assert source.type is SourceType.BBPLUS

        result = RetrievalPipeline(store, "alice").search(
            "Explain elasticity of demand", course_id="ECON303"
        )
        assert result.chunks
        assert any("elasticity of demand" in c.chunk.text.lower() for c in result.chunks)
        assert all(
            c.chunk.user_id == "alice" and c.chunk.course_id == "ECON303"
            for c in result.chunks
        )

        count_before = store.chunk_count_for_course("alice", "ECON303")
        def unexpected_embedder_load():
            raise AssertionError("unchanged sync should not initialize an embedding model")
        monkeypatch.setattr("api.corpus.ingest_service.get_embedder", unexpected_embedder_load)
        second = ingest_files([path], user_id="alice", course_id="ECON303", store=store)
        assert second.files[0].status == "ok"
        assert "reused" in second.files[0].detail.lower()
        assert store.chunk_count_for_course("alice", "ECON303") == count_before
    finally:
        store.close()


def test_bbplus_materials_cleanup_preserves_other_user_and_handles_regular_textbook(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "isolation.sqlite")
    monkeypatch.setattr(
        "api.corpus.ingest_service.embedder_state_path",
        lambda user: tmp_path / f"embedder_{user}.pkl",
    )
    try:
        bbplus = tmp_path / "bbplus_3a5f_elasticity.txt"
        bbplus.write_text(_text("elasticity"), encoding="utf-8")
        textbook = tmp_path / "textbook_choice.txt"
        textbook.write_text(_text("consumer choice"), encoding="utf-8")
        ingest_files([bbplus, textbook], user_id="alice", course_id="ECON303", store=store)
        ingest_files([bbplus], user_id="bob", course_id="ECON303", store=store)
        store.set_bbplus_course_mapping("alice", "blackboard-econ303", "Micro", "ECON303")
        assert {s.type for s in store.sources("alice", "ECON303")} == {
            SourceType.BBPLUS, SourceType.TEXTBOOK
        }
        alice_before = store.chunk_count_for_course("alice", "ECON303")
        bob_before = store.chunk_count_for_course("bob", "ECON303")
        assert store.course_deletion_counts("alice", "ECON303")["materials"]["sources"] == 2

        store.delete_course_scope("alice", "ECON303", "materials")
        assert alice_before > 0 and store.chunk_count_for_course("alice", "ECON303") == 0
        assert store.chunk_count_for_course("bob", "ECON303") == bob_before > 0
        assert store.bbplus_course_mapping("alice", "blackboard-econ303")["course_id"] == "ECON303"
        assert len(store.sources("bob", "ECON303")) == 1
    finally:
        store.close()


def test_materials_upload_api_returns_pollable_job_and_scopes_job_to_user(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "api.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "alice")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(
        "api.corpus.ingest_service.embedder_state_path",
        lambda user: tmp_path / f"embedder_{user}.pkl",
    )
    client = TestClient(main.app)
    try:
        response = client.post(
            "/api/courses/ECON303/materials",
            files=[("files", ("bbplus_90ab_elasticity.txt", _text("elasticity"), "text/plain"))],
        )
        assert response.status_code == 200
        body = response.json()
        assert body["course_id"] == "ECON303"
        assert body["files"] == ["bbplus_90ab_elasticity.txt"]
        assert body["job_id"]

        deadline = time.monotonic() + 10
        job = None
        while time.monotonic() < deadline:
            job_response = client.get(f"/api/jobs/{body['job_id']}")
            assert job_response.status_code == 200
            job = job_response.json()
            if job["status"] in {"done", "failed"}:
                break
            time.sleep(0.02)
        assert job and job["status"] == "done"
        assert store.sources("alice", "ECON303")[0].type is SourceType.BBPLUS

        store.create_job("bob-job", "bob", "ECON303", [])
        assert client.get("/api/jobs/bob-job").status_code == 404
    finally:
        client.close()
        store.close()

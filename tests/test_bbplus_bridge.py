from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from api import main
from api.integrations.bbplus import safe_material_filename, serialize_document
from api.models import SourceType
from api.store.sqlite_store import SQLiteStore


def _blocks(topic: str) -> list[dict]:
    return [
        {"type": "heading", "level": 2, "text": topic, "page": 49},
        {"type": "paragraph", "text": (f"{topic} explains consumer choice and the budget line. " * 50), "page": 49},
        {"type": "math", "latex": "p_x x + p_y y = m", "page": 50},
        {"type": "table", "rows": [["Item", "Value"], ["Income", "100"]]},
    ]


def _wait_for_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"done", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError("BB Plus material ingest job did not finish")


def test_bbplus_serializer_preserves_structure_and_locations():
    text = serialize_document("Demand", _blocks("Demand theory"))
    assert "## Demand theory" in text
    assert "[Page 49]" in text
    assert "$$p_x x + p_y y = m$$" in text
    assert "Item | Value" in text
    assert safe_material_filename("item/with?unsafe:id", "Demand / Chapter 2") == safe_material_filename(
        "item/with?unsafe:id", "Renamed chapter"
    )
    assert safe_material_filename("item-a", "Demand") != safe_material_filename("item-b", "Demand")


def test_create_and_map_uses_blackboard_identity_to_avoid_same_code_collision(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "course-ids.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "alice")
    client = TestClient(main.app)
    payload = {"code": "ECON 303", "title": "Microeconomics", "term": "Fall"}
    try:
        first = client.post("/api/integrations/bbplus/course-mappings/bb-id-one/create", json=payload)
        second = client.post("/api/integrations/bbplus/course-mappings/bb-id-two/create", json=payload)
        repeated = client.post("/api/integrations/bbplus/course-mappings/bb-id-one/create", json=payload)
        assert first.status_code == second.status_code == repeated.status_code == 200
        first_id = first.json()["course_id"]
        assert first_id == repeated.json()["course_id"]
        assert first_id != second.json()["course_id"]
        assert len(store.courses("alice")) == 2
    finally:
        client.close()
        store.close()


def test_bbplus_unreadable_documents_are_rejected():
    with pytest.raises(ValueError, match="no readable text"):
        serialize_document("Scanned file", [{"type": "unparsed", "reason": "no parser"}])


def test_bbplus_mapping_sync_ingest_poll_retrieval_and_idempotency(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "bridge.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "alice")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(
        "api.corpus.ingest_service.embedder_state_path",
        lambda user: tmp_path / f"embedder_{user}.pkl",
    )
    client = TestClient(main.app)
    document = {"item_id": "bb-item-41", "course_id": "bb-course-9", "title": "Budget Line",
                "source_type": "pdf", "blocks": _blocks("slope of the budget line")}
    same_content_different_item = {**document, "item_id": "bb-item-42"}
    documents = [document, same_content_different_item]
    try:
        created = client.post("/api/integrations/bbplus/course-mappings/bb-course-9/create", json={
            "code": "ECON303", "title": "Intermediate Microeconomics", "term": "Fall"
        })
        assert created.status_code == 200
        course_id = created.json()["course_id"]
        mapped = client.put("/api/integrations/bbplus/course-mappings/bb-course-9", json={
            "course_id": course_id, "course_name": "Intermediate Microeconomics", "user_id": "bob"
        })
        assert mapped.status_code == 200

        first = client.post(
            "/api/integrations/bbplus/course-mappings/bb-course-9/materials",
            json={"documents": documents},
        )
        assert first.status_code == 200, first.text
        first_job = _wait_for_job(client, first.json()["job_id"])
        assert first_job["status"] == "done"
        assert len(store.sources("alice", course_id)) == 2
        assert all(source.type is SourceType.BBPLUS for source in store.sources("alice", course_id))
        first_chunks = store.chunk_count_for_course("alice", course_id)
        assert first_chunks > 0

        from api.retrieval.pipeline import RetrievalPipeline
        result = RetrievalPipeline(store, "alice").search(
            "What is the slope of the budget line?", course_id=course_id
        )
        assert result.chunks
        assert any("slope of the budget line" in hit.chunk.text.lower() for hit in result.chunks)
        assert all(hit.chunk.user_id == "alice" and hit.chunk.course_id == course_id for hit in result.chunks)

        second = client.post(
            "/api/integrations/bbplus/course-mappings/bb-course-9/materials",
            json={"documents": documents},
        )
        assert second.status_code == 200
        second_job = _wait_for_job(client, second.json()["job_id"])
        assert second_job["status"] == "done"
        assert store.chunk_count_for_course("alice", course_id) == first_chunks
        assert len(store.sources("alice", course_id)) == 2

        old_sources = {source.source_id for source in store.sources("alice", course_id)}
        changed = {**document, "title": "Updated Budget Line",
                   "blocks": _blocks("updated slope of the budget line")}
        third = client.post(
            "/api/integrations/bbplus/course-mappings/bb-course-9/materials",
            json={"documents": [changed, same_content_different_item]},
        )
        assert third.status_code == 200
        assert _wait_for_job(client, third.json()["job_id"])["status"] == "done"
        updated_sources = store.sources("alice", course_id)
        assert {source.source_id for source in updated_sources} == old_sources
        assert any(source.title == "Updated Budget Line" for source in updated_sources)
    finally:
        client.close()
        store.close()


def test_bbplus_mapping_and_materials_are_scoped_by_user(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "users.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "alice")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    client = TestClient(main.app)
    try:
        store.upsert_course_stub("alice", "econ303", "ECON303", "Alice course")
        store.upsert_course_stub("bob", "econ303", "ECON303", "Bob course")
        assert client.put("/api/integrations/bbplus/course-mappings/colliding-id", json={
            "course_id": "econ303", "course_name": "Blackboard course"
        }).status_code == 200
        assert store.bbplus_course_mapping("alice", "colliding-id")["course_id"] == "econ303"
        assert store.bbplus_course_mapping("bob", "colliding-id") is None

        assert client.put("/api/integrations/bbplus/course-mappings/not-owned", json={
            "course_id": "bob-only-course", "course_name": "Other user's course"
        }).status_code == 404
        assert client.post(
            "/api/integrations/bbplus/course-mappings/colliding-id/materials",
            json={"documents": [{"item_id": "x", "course_id": "different-course",
                                 "title": "Wrong course", "blocks": _blocks("not shared")}]},
        ).status_code == 409
        store.set_bbplus_course_mapping("bob", "colliding-id", "Bob's Blackboard course", "econ303")
        store.delete_course_scope("alice", "econ303", "entire_course")
        assert store.bbplus_course_mapping("alice", "colliding-id") is None
        assert store.bbplus_course_mapping("bob", "colliding-id")["course_id"] == "econ303"
        assert store.course("bob", "econ303") is not None
    finally:
        client.close()
        store.close()


def test_bbplus_ingest_failure_is_pollable_and_has_a_safe_reason(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "failed-job.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "alice")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    store.upsert_course_stub("alice", "econ303", "ECON303", "Micro")
    store.set_bbplus_course_mapping("alice", "bb-course", "Micro", "econ303")

    def fail_ingest(*args, **kwargs):
        raise RuntimeError("internal failure detail must not be exposed")

    monkeypatch.setattr(main, "ingest_files", fail_ingest)
    client = TestClient(main.app)
    try:
        response = client.post(
            "/api/integrations/bbplus/course-mappings/bb-course/materials",
            json={"documents": [{"item_id": "item", "course_id": "bb-course",
                                 "title": "Readable", "blocks": _blocks("Readable material")}]},
        )
        assert response.status_code == 200
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["status"] == "failed"
        assert job["files"][0]["status"] == "failed"
        assert "Retry the sync" in job["files"][0]["detail"]
        assert "internal failure detail" not in job["files"][0]["detail"]
    finally:
        client.close()
        store.close()


def test_benchmark_user_is_blocked_from_bbplus_product_routes(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "benchmark.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "benchmark")
    client = TestClient(main.app)
    try:
        store.upsert_course_stub("benchmark", "fixture", "FIXTURE", "Immutable fixture")
        before = store.chunk_count("benchmark")
        assert client.get("/api/integrations/bbplus/state").status_code == 403
        assert client.post("/api/integrations/bbplus/course-mappings/id/create", json={
            "code": "NEW"
        }).status_code == 403
        assert client.put("/api/integrations/bbplus/course-mappings/id", json={
            "course_id": "fixture", "course_name": "Fixture"
        }).status_code == 403
        assert store.chunk_count("benchmark") == before
        assert store.bbplus_course_mappings("benchmark") == []
    finally:
        client.close()
        store.close()


def test_browser_api_restricts_extension_origin_and_rejects_cross_site_writes():
    client = TestClient(main.app)
    extension_origin = f"chrome-extension://{'a' * 32}"
    try:
        allowed = client.options("/api/integrations/bbplus/state", headers={
            "Origin": extension_origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        })
        assert allowed.status_code == 200
        assert allowed.headers.get("access-control-allow-origin") == extension_origin

        denied_preflight = client.options("/api/integrations/bbplus/state", headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "GET",
        })
        assert "access-control-allow-origin" not in denied_preflight.headers

        denied_write = client.post("/api/integrations/bbplus/courses", json={"code": "EVIL"}, headers={
            "Origin": "https://untrusted.example",
        })
        assert denied_write.status_code == 403
    finally:
        client.close()

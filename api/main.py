"""FastAPI application.

Two tabs' worth of API plus onboarding. Agent steps stream over SSE, because
watching the retrieval actually happen makes the wait feel like work rather than
lag, and it is the cheapest legibility win in the product.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import re
import shutil
import threading
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent.loop import Agent
from .llm import resolve_product_llm
from .grade_predictor import GRADE_SCHEMA, PROMPT as GRADE_PROMPT, GradeSchemaError, calculate as calculate_grade, grade_ladder, solve_item_target, solve_target, validate_schema
from .config import ROOT, settings
from .calendar_feed import build_ics
from .dashboard.progress import progress_report
from .dashboard.readiness import (
    chapter_readiness,
    exam_readiness,
    today_view,
    week_view,
)
from .retrieval.pipeline import RetrievalPipeline
from .corpus.ingest_service import ingest_files, slugify
from .corpus.ingest_service import _source_id
from .integrations.bbplus import safe_material_filename, serialize_document
from .models import Source, SourceType
from .store import get_store
from .syllabus.commit import commit as commit_syllabus
from .syllabus.commit import build_linker
from .syllabus.extract import double_extract, read_syllabus
from .voice.service import ElevenLabsService, VoiceServiceError

app = FastAPI(title="Course Copilot", version="0.1.0")
logger = logging.getLogger(__name__)
voice_service = ElevenLabsService(settings)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8471", "http://localhost:8471"],
    allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
    allow_methods=["*"],
    allow_headers=["content-type", "accept"],
)


@app.middleware("http")
async def reject_cross_site_mutations(request, call_next):
    """Block browser CSRF-style writes while allowing the local UI and extension."""
    origin = request.headers.get("origin")
    if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
        local_origins = {"http://127.0.0.1:8471", "http://localhost:8471"}
        extension_origin = re.fullmatch(r"chrome-extension://[a-p]{32}", origin)
        if origin not in local_origins and not extension_origin:
            return JSONResponse(status_code=403, content={"detail": "Cross-site writes are not allowed."})
    return await call_next(request)

WEB_DIR = ROOT / "web"
UPLOAD_DIR = ROOT / "data" / "uploads"


@app.on_event("startup")
def _warm_embedder() -> None:
    """Load the embedding model in the background at boot so the first course
    compilation (and first question) doesn't pay the one-time ~7s cold start."""
    if settings.offline:
        return

    def warm() -> None:
        try:
            from .embed import get_embedder
            get_embedder().embed(["warm up"])
            logger.info("Embedding model warmed.")
        except Exception:  # noqa: BLE001 - warming is best-effort
            logger.debug("Embedder warm-up skipped.", exc_info=True)

    threading.Thread(target=warm, name="embed-warm", daemon=True).start()

# One pipeline per user. Multi-tenant now, so a single global would leak one
# user's corpus into another's answers.
_pipelines: dict[str, RetrievalPipeline] = {}
# One course-ingest worker at a time bounds CPU/RAM use and prevents several
# simultaneous uploads from competing to load/encode with the shared BGE model.
_material_ingest_semaphore = threading.BoundedSemaphore(1)


def current_user() -> str:
    """Single user until auth lands. Every query already scopes by this value, so
    adding real auth means changing this function and nothing else."""
    return settings.default_user_id


def pipeline(user_id: Optional[str] = None) -> RetrievalPipeline:
    user_id = user_id or current_user()
    if user_id not in _pipelines:
        _pipelines[user_id] = RetrievalPipeline(get_store(), user_id)
    return _pipelines[user_id]


def invalidate_pipeline(user_id: Optional[str] = None) -> None:
    """Called after any ingest: the corpus and the local vector space changed."""
    if user_id is None:
        _pipelines.clear()
    else:
        _pipelines.pop(user_id, None)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AskRequest(BaseModel):
    question: str
    course: Optional[str] = None
    depth: str = "concise"        # "concise" | "in_depth"


class ProgressRequest(BaseModel):
    chapter_ref: str
    value: float


class VoicePreferencesRequest(BaseModel):
    voice_only_mode: Optional[bool] = None
    auto_submit_voice: Optional[bool] = None
    selected_voice_id: Optional[str] = None
    speech_speed: Optional[float] = None


class VoiceSynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class GradeRulesRequest(BaseModel):
    grade_schema: dict[str, Any] = Field(alias="schema")
    extraction_id: Optional[str] = None


class GradeScoreRequest(BaseModel):
    score: Optional[float] = Field(default=None, ge=0, le=100)


class GradeCalculateRequest(BaseModel):
    hypothetical: dict[str, float] = {}
    target_percent: Optional[float] = Field(default=None, ge=0, le=100)
    target_letter: Optional[str] = None
    target_item_id: Optional[str] = None
    unfilled_assumption: str = "same_score"


class GradeSyllabusSelectionRequest(BaseModel):
    source_id: str


class GradeSyllabusAnalyzeRequest(BaseModel):
    source_id: Optional[str] = None
    force: bool = False


class CourseDeletionItem(BaseModel):
    course_id: str
    scope: str


class CourseDeletionRequest(BaseModel):
    selected: list[CourseDeletionItem]
    confirmation: str
    select_all: bool = False


class BBPlusCourseRequest(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    title: str = Field(default="", max_length=300)
    term: str = Field(default="", max_length=120)


class BBPlusMappingRequest(BaseModel):
    course_id: str = Field(min_length=1, max_length=120)
    course_name: str = Field(default="", max_length=300)


class BBPlusDocument(BaseModel):
    item_id: str = Field(min_length=1, max_length=300)
    course_id: str = Field(default="", max_length=300)
    title: str = Field(default="Blackboard material", max_length=500)
    source_type: str = Field(default="unknown", max_length=40)
    blocks: list[dict[str, Any]] = Field(default_factory=list, max_length=20000)


class BBPlusSyncRequest(BaseModel):
    documents: list[BBPlusDocument] = Field(min_length=1, max_length=100)


class BBPlusFileItem(BaseModel):
    item_id: str = Field(min_length=1, max_length=300)
    title: str = Field(default="Blackboard material", max_length=500)
    filename: str = Field(default="", max_length=500)
    content_base64: str = Field(min_length=1, max_length=90_000_000)


class BBPlusFilesRequest(BaseModel):
    files: list[BBPlusFileItem] = Field(min_length=1, max_length=12)


class BBPlusAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    depth: str = "concise"


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    store = get_store()
    user = current_user()
    return {
        "ok": True,
        "chunks": store.chunk_count(user),
        "courses": [c.code for c in store.courses(user)],
        "backends": settings.describe_backends(),
        "refusal_threshold": settings.retrieval.refusal_threshold,
    }


@app.get("/api/courses")
def courses() -> dict[str, Any]:
    store = get_store()
    user = current_user()
    out = []
    for course in store.courses(user):
        chunks = store.chunk_count_for_course(user, course.course_id)
        sources = store.sources(user, course.course_id)
        out.append(
            {
                "course_id": course.course_id,
                "code": course.code,
                "title": course.title,
                "term": course.term,
                "has_data_link": course.has_data_link,
                "data_link_reason": course.data_link_reason,
                "chunks": chunks,
                "materials_loaded": chunks > 0,
                "sources": [
                    {"source_id": x.source_id, "title": x.title, "status": x.status,
                     "detail": x.status_detail, "pages": x.pages, "ocr_pages": x.ocr_pages}
                    for x in sources
                ],
            }
        )
    return {"courses": out}


def _require_non_benchmark_product_user() -> str:
    user = current_user()
    if user == "benchmark":
        raise HTTPException(403, detail={"code": "benchmark_read_only", "message": "The benchmark corpus is not available to product integrations."})
    return user


@app.get("/api/integrations/bbplus/state")
def bbplus_state() -> dict[str, Any]:
    user = _require_non_benchmark_product_user()
    store = get_store()
    return {
        "courses": [
            {"course_id": c.course_id, "code": c.code, "title": c.title, "term": c.term,
             "chunks": store.chunk_count_for_course(user, c.course_id),
             "sources": [{"source_id": s.source_id, "title": s.title, "pages": s.pages,
                         "status": s.status} for s in store.sources(user, c.course_id)]}
            for c in store.courses(user)
        ],
        "mappings": store.bbplus_course_mappings(user),
    }


@app.post("/api/integrations/bbplus/course-mappings/{blackboard_course_id}/create")
def create_and_map_bbplus_course(blackboard_course_id: str,
                                 request: BBPlusCourseRequest) -> dict[str, Any]:
    user = _require_non_benchmark_product_user()
    store = get_store()
    external_hash = hashlib.sha256(blackboard_course_id.encode("utf-8")).hexdigest()[:8]
    course_id = f"{slugify(request.code)}_bbx_{external_hash}"
    title = request.title.strip() or request.code.strip()
    store.upsert_course_stub(user, course_id, request.code.strip(), title)
    course = store.course(user, course_id)
    if course:
        course.term = request.term.strip()
        store.upsert_course(course)
    try:
        store.set_bbplus_course_mapping(user, blackboard_course_id,
                                        request.code.strip() or title, course_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, detail={"code": "mapping_failed", "message": str(exc)}) from exc
    return {"course_id": course_id, "code": request.code.strip(), "title": title,
            "term": request.term.strip(), "blackboard_course_id": blackboard_course_id}


@app.put("/api/integrations/bbplus/course-mappings/{blackboard_course_id}")
def set_bbplus_mapping(blackboard_course_id: str, request: BBPlusMappingRequest) -> dict[str, Any]:
    user = _require_non_benchmark_product_user()
    store = get_store()
    try:
        store.set_bbplus_course_mapping(user, blackboard_course_id,
                                        request.course_name.strip(), request.course_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"code": "course_not_found", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_mapping", "message": str(exc)}) from exc
    return {"blackboard_course_id": blackboard_course_id,
            "blackboard_course_name": request.course_name.strip(), "course_id": request.course_id}


@app.post("/api/integrations/bbplus/course-mappings/{blackboard_course_id}/materials")
def sync_bbplus_materials(blackboard_course_id: str, request: BBPlusSyncRequest) -> dict[str, Any]:
    user = _require_non_benchmark_product_user()
    store = get_store()
    mapping = store.bbplus_course_mapping(user, blackboard_course_id)
    if mapping is None:
        raise HTTPException(409, detail={"code": "course_mapping_required", "message": "Map this Blackboard course to a Course Copilot course first."})
    if store.course(user, mapping["course_id"]) is None:
        raise HTTPException(409, detail={"code": "mapped_course_missing", "message": "The mapped Course Copilot course no longer exists. Choose a course again."})

    prepared: list[tuple[str, str, str]] = []
    skipped: list[dict[str, str]] = []
    total_chars = 0
    # Validate every course identifier before writing any upload file. This is
    # a hard isolation boundary; unreadable individual documents, however,
    # should not discard other good documents in the same incremental batch.
    if any(document.course_id and document.course_id != blackboard_course_id
           for document in request.documents):
        raise HTTPException(409, detail={"code": "course_mismatch", "message": "A document belongs to a different Blackboard course."})
    for document in request.documents:
        try:
            content = serialize_document(document.title, document.blocks)
            if total_chars + len(content) > 24_000_000:
                skipped.append({"item_id": document.item_id, "title": document.title,
                                "reason": "This batch exceeds the 24 MB text limit."})
                continue
            total_chars += len(content)
            prepared.append((safe_material_filename(document.item_id, document.title),
                             content, document.item_id))
        except ValueError as exc:
            skipped.append({"item_id": document.item_id, "title": document.title,
                            "reason": str(exc)[:200]})
    if not prepared:
        return {"course_id": mapping["course_id"], "job_id": "", "files": [],
                "skipped": skipped}

    destination = UPLOAD_DIR / user / mapping["course_id"]
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    file_item_ids: dict[str, str] = {}
    for filename, content, item_id in prepared:
        target = destination / filename
        target.write_text(content, encoding="utf-8")
        paths.append(target)
        file_item_ids[filename] = item_id
    return {**_start_material_ingest(paths, mapping["course_id"], user, store,
                                    file_item_ids=file_item_ids),
            "skipped": skipped}


@app.post("/api/integrations/bbplus/course-mappings/{blackboard_course_id}/materials/files")
def ingest_bbplus_files(blackboard_course_id: str, request: BBPlusFilesRequest) -> dict[str, Any]:
    """Ingest a batch of raw Blackboard files (PDF/DOCX) through the full
    server-side extractor — PyMuPDF plus OCR for image-only/handwritten pages —
    instead of the extension's text-layer-only parser. Batched into ONE ingest
    job so the (one-at-a-time) embedder runs over all of them in one pass rather
    than paying per-file overhead. Idempotent by content hash.
    """
    import base64
    import binascii

    user = _require_non_benchmark_product_user()
    store = get_store()
    mapping = store.bbplus_course_mapping(user, blackboard_course_id)
    if mapping is None or store.course(user, mapping["course_id"]) is None:
        raise HTTPException(409, detail={"code": "course_mapping_required",
                                         "message": "Map this Blackboard course to a Course Copilot course first."})

    destination = UPLOAD_DIR / user / mapping["course_id"]
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    file_item_ids: dict[str, str] = {}
    file_titles: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    for item in request.files:
        ext = Path(item.filename or "").suffix.lower()
        if ext not in {".pdf", ".docx"}:
            skipped.append({"item_id": item.item_id, "reason": "not a PDF or DOCX"})
            continue
        try:
            raw = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError):
            skipped.append({"item_id": item.item_id, "reason": "invalid base64"})
            continue
        if not raw or len(raw) > 60_000_000:
            skipped.append({"item_id": item.item_id, "reason": "empty or too large"})
            continue
        filename = safe_material_filename(item.item_id, item.title, ext)
        (destination / filename).write_bytes(raw)
        paths.append(destination / filename)
        file_item_ids[filename] = item.item_id
        file_titles[filename] = item.title

    if not paths:
        return {"course_id": mapping["course_id"], "job_id": "", "files": [], "skipped": skipped}
    return {**_start_material_ingest(paths, mapping["course_id"], user, store,
                                     file_item_ids=file_item_ids, file_titles=file_titles),
            "skipped": skipped}


@app.get("/api/integrations/bbplus/course-mappings/{blackboard_course_id}/sources/{source_id}/file")
def bbplus_source_file(blackboard_course_id: str, source_id: str) -> FileResponse:
    """Serve the stored file a citation came from, so the sidebar can link to it.

    Scoped hard by user and by the Blackboard course's own mapping: a citation
    can only ever open a source that belongs to the class it was answered from.
    """
    user = _require_non_benchmark_product_user()
    store = get_store()
    mapping = store.bbplus_course_mapping(user, blackboard_course_id)
    if mapping is None:
        raise HTTPException(404, detail={"code": "course_mapping_required",
                                         "message": "This Blackboard class is not linked to Course Copilot."})
    source = next((s for s in store.sources(user, mapping["course_id"])
                   if s.source_id == source_id), None)
    if source is None:
        raise HTTPException(404, detail={"code": "source_not_found",
                                         "message": "That cited material is no longer stored."})
    path = Path(source.file)
    if not path.is_file():
        raise HTTPException(404, detail={"code": "file_missing",
                                         "message": "The stored material file is missing."})
    media_type = mimetypes.guess_type(path.name)[0] or "text/plain"
    display_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.title or path.name).strip("_") or path.name
    if not Path(display_name).suffix:
        display_name += path.suffix
    return FileResponse(str(path), media_type=media_type, filename=display_name,
                        content_disposition_type="inline")


@app.post("/api/integrations/bbplus/course-mappings/{blackboard_course_id}/ask")
async def ask_bbplus_course(blackboard_course_id: str, request: BBPlusAskRequest) -> dict[str, Any]:
    user = _require_non_benchmark_product_user()
    if request.depth not in {"concise", "in_depth"}:
        raise HTTPException(400, detail={"code": "invalid_depth", "message": "Depth must be concise or in_depth."})
    mapping = get_store().bbplus_course_mapping(user, blackboard_course_id)
    if mapping is None or get_store().course(user, mapping["course_id"]) is None:
        raise HTTPException(409, detail={"code": "course_mapping_required", "message": "Map this Blackboard course to a Course Copilot course first."})
    if not request.question.strip():
        raise HTTPException(400, detail={"code": "empty_question", "message": "Enter a question first."})
    agent = Agent(pipeline(), user_id=user)
    answer = await agent.answer_product(request.question, mapping["course_id"],
                                        depth=request.depth, numbered=True)
    return answer.to_dict()


# ---------------------------------------------------------------------------
# Tab 1 -- Ask
# ---------------------------------------------------------------------------


@app.post("/api/ask")
async def ask(request: AskRequest) -> dict[str, Any]:
    if not request.question.strip():
        raise HTTPException(400, "question is required")
    agent = Agent(pipeline(), user_id=current_user())
    answer = await agent.answer_product(request.question, request.course, depth=request.depth)
    return answer.to_dict()


@app.post("/api/ask/stream")
async def ask_stream(request: AskRequest) -> StreamingResponse:
    """Server-sent events: one frame per agent step, then the answer.

    Evidence cards arrive independently and each fails alone -- a chart that
    errors must not take the passages down with it.
    """
    if not request.question.strip():
        raise HTTPException(400, "question is required")

    async def events():
        queue: asyncio.Queue = asyncio.Queue()

        async def run() -> None:
            agent = Agent(pipeline(), user_id=current_user())
            try:
                answer = await agent.ask_async(request.question, request.course)
                # Replay the steps in order, then the final answer.
                for step in answer.steps:
                    await queue.put(("step", asdict(step)))
                await queue.put(("answer", answer.to_dict()))
            except Exception as exc:  # noqa: BLE001 - render as a card-level error
                await queue.put(("error", {"detail": str(exc)}))
            finally:
                await queue.put((None, None))

        task = asyncio.create_task(run())
        yield f"event: open\ndata: {json.dumps({'question': request.question})}\n\n"
        while True:
            kind, payload = await queue.get()
            if kind is None:
                break
            yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"
        await task
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/passages")
def passages(request: AskRequest) -> dict[str, Any]:
    """Retrieval only, no agent.

    The evidence card calls this rather than reading the answer's citations, so
    the passages still render when answer generation fails.
    """
    result = pipeline().search(request.question, course_id=request.course)
    return result.to_dict()


CONSENT_COPY = (
    "To explain in its own words (not just quote passages) and to generate practice "
    "problems, Course Copilot needs to send your question and the retrieved textbook "
    "passages to an AI model.\n\n"
    "Default is OFF: with no AI, you still get grounded answers built from your "
    "materials, cited to the page — just quotes, not explanations.\n\n"
    "If you turn this on, your questions and the relevant passages are sent to "
    "Google's free-tier Gemini. Free-tier inputs may be used to improve Google's "
    "models. No other data — no account details, no other courses — is sent.\n\n"
    "Alternatives that need no consent: paste your own Anthropic or Gemini API key "
    "(your key's inputs are not used for training), or run a local model with Ollama "
    "(nothing leaves your machine)."
)


@app.get("/api/consent")
def get_consent() -> dict[str, Any]:
    prefs = get_store().get_prefs(current_user())
    return {
        "llm_consent": prefs["llm_consent"],
        "consent_decided": prefs["consent_decided"],
        "depth": prefs["depth"],
        "voice_consent": prefs["voice_consent"],
        "voice_only_mode": prefs["voice_only_mode"],
        "auto_submit_voice": prefs["auto_submit_voice"],
        "selected_voice_id": prefs["selected_voice_id"] or settings.elevenlabs_voice_id,
        "speech_speed": prefs["speech_speed"],
        "voice_api_configured": voice_service.has_key,
        "voice_available": bool(voice_service.has_key and (
            prefs["selected_voice_id"] or settings.elevenlabs_voice_id
        )),
        "has_own_gemini_key": bool(prefs["gemini_api_key"]),
        "has_own_anthropic_key": bool(prefs["anthropic_api_key"]),
        "backend": resolve_product_llm(prefs).label,
        "copy": CONSENT_COPY,
    }


class ConsentRequest(BaseModel):
    llm_consent: Optional[bool] = None
    consent_decided: Optional[bool] = None
    gemini_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    depth: Optional[str] = None
    voice_consent: Optional[bool] = None


@app.post("/api/consent")
def set_consent(req: ConsentRequest) -> dict[str, Any]:
    """Persist the per-user privacy choice. Enforced in code: the answerer reads
    these prefs, so nothing reaches a training-eligible free model without opt-in."""
    user = current_user()
    store = get_store()
    fields: dict[str, Any] = {}
    if req.llm_consent is not None:
        fields["llm_consent"] = req.llm_consent
    if req.consent_decided is not None:
        fields["consent_decided"] = req.consent_decided
    if req.gemini_api_key is not None:
        fields["gemini_api_key"] = req.gemini_api_key.strip()
    if req.anthropic_api_key is not None:
        fields["anthropic_api_key"] = req.anthropic_api_key.strip()
    if req.depth is not None:
        if req.depth not in {"concise", "in_depth"}:
            raise HTTPException(400, "depth must be concise or in_depth")
        fields["depth"] = req.depth
    if req.voice_consent is not None:
        fields["voice_consent"] = req.voice_consent
        if not req.voice_consent:
            fields["voice_only_mode"] = False
    store.set_prefs(user, **fields)
    prefs = store.get_prefs(user)
    return {"llm_consent": prefs["llm_consent"], "consent_decided": prefs["consent_decided"],
            "depth": prefs["depth"], "voice_consent": prefs["voice_consent"],
            "backend": resolve_product_llm(prefs).label}


VOICE_MAX_BYTES = 15 * 1024 * 1024
VOICE_MIME_TYPES = {
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".mp4",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/aac": ".aac", "audio/m4a": ".m4a",
}


def _valid_audio_signature(content_type: str, data: bytes) -> bool:
    if content_type == "audio/webm":
        return data.startswith(b"\x1aE\xdf\xa3")
    if content_type == "audio/ogg":
        return data.startswith(b"OggS")
    if content_type in {"audio/mp4", "audio/m4a"}:
        return len(data) >= 8 and data[4:8] == b"ftyp"
    if content_type in {"audio/wav", "audio/x-wav"}:
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if content_type in {"audio/mpeg", "audio/mp3"}:
        return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if content_type == "audio/aac":
        return len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0
    return False


def _require_voice_consent() -> dict[str, Any]:
    prefs = get_store().get_prefs(current_user())
    if not prefs["voice_consent"]:
        raise HTTPException(403, "Voice processing consent is required before sending audio or answer text to ElevenLabs.")
    return prefs


def _voice_http_exception(exc: VoiceServiceError) -> HTTPException:
    """Return a safe, structured error; provider messages and credentials stay private."""
    detail: dict[str, str] = {"code": exc.code, "message": exc.detail}
    if exc.request_id:
        detail["request_id"] = exc.request_id
    return HTTPException(exc.status_code, detail)


@app.post("/api/voice/transcribe")
async def voice_transcribe(
    file: UploadFile = File(...), duration_ms: int | None = Form(None),
) -> dict[str, str]:
    """Forward one explicitly recorded clip; bytes are never saved by the app."""
    _require_voice_consent()
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in VOICE_MIME_TYPES:
        await file.close()
        raise HTTPException(415, "Use a supported recording format: WebM, Ogg, MP4, MP3, AAC, or WAV.")
    try:
        audio = await file.read(VOICE_MAX_BYTES + 1)
    finally:
        await file.close()
    if len(audio) > VOICE_MAX_BYTES:
        raise HTTPException(413, "Recording is too large. Keep it under 15 MB and try again.")
    if not audio:
        raise HTTPException(400, "The recording is empty. Record a question and try again.")
    if not _valid_audio_signature(content_type, audio):
        raise HTTPException(415, "The uploaded file does not match its audio format. Record a new clip and try again.")
    filename = Path(file.filename or "recording").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".webm", ".ogg", ".mp4", ".mp3", ".wav", ".aac", ".m4a"}:
        filename = f"recording{VOICE_MIME_TYPES[content_type]}"
    logger.info(
        "Voice upload received mime_type=%s size_bytes=%s duration_ms=%s filename=%s",
        content_type, len(audio), duration_ms, filename,
    )
    try:
        text = await voice_service.transcribe(audio, filename, content_type)
    except VoiceServiceError as exc:
        raise _voice_http_exception(exc) from exc
    return {"text": text, "provider": "elevenlabs"}


@app.post("/api/voice/synthesize")
async def voice_synthesize(request: VoiceSynthesizeRequest) -> Response:
    prefs = _require_voice_consent()
    text = request.text.strip()
    if not text:
        raise HTTPException(400, "Text to read is required.")
    voice_id = prefs["selected_voice_id"] or settings.elevenlabs_voice_id
    try:
        audio = await voice_service.synthesize(text, voice_id, float(prefs["speech_speed"]))
    except VoiceServiceError as exc:
        raise _voice_http_exception(exc) from exc
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/voice/voices")
async def voice_list() -> dict[str, Any]:
    _require_voice_consent()
    try:
        return {"voices": await voice_service.voices()}
    except VoiceServiceError as exc:
        raise _voice_http_exception(exc) from exc


@app.post("/api/voice/preferences")
async def voice_preferences(request: VoicePreferencesRequest) -> dict[str, Any]:
    prefs = get_store().get_prefs(current_user())
    fields: dict[str, Any] = {}
    if request.voice_only_mode is not None:
        if request.voice_only_mode and not prefs["voice_consent"]:
            raise HTTPException(403, "Accept voice processing consent before enabling Voice Only Mode.")
        if request.voice_only_mode and not (
            voice_service.has_key and (request.selected_voice_id or prefs["selected_voice_id"]
                                       or settings.elevenlabs_voice_id)
        ):
            raise HTTPException(503, "Configure an ElevenLabs key and voice before enabling Voice Only Mode.")
        fields["voice_only_mode"] = request.voice_only_mode
    if request.auto_submit_voice is not None:
        fields["auto_submit_voice"] = request.auto_submit_voice
    if request.speech_speed is not None:
        if request.speech_speed not in {0.8, 1.0, 1.2}:
            raise HTTPException(400, "speech_speed must be 0.8, 1.0, or 1.2")
        fields["speech_speed"] = request.speech_speed
    if request.selected_voice_id is not None:
        _require_voice_consent()
        if not voice_service.has_key:
            raise HTTPException(503, "ElevenLabs is not configured on this server.")
        try:
            available = await voice_service.voices()
        except VoiceServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        if request.selected_voice_id not in {voice["voice_id"] for voice in available}:
            raise HTTPException(400, "Choose a voice from the available ElevenLabs voice list.")
        fields["selected_voice_id"] = request.selected_voice_id
    get_store().set_prefs(current_user(), **fields)
    updated = get_store().get_prefs(current_user())
    return {"voice_consent": updated["voice_consent"],
            "voice_only_mode": updated["voice_only_mode"],
            "auto_submit_voice": updated["auto_submit_voice"],
            "selected_voice_id": updated["selected_voice_id"] or settings.elevenlabs_voice_id,
            "speech_speed": updated["speech_speed"],
            "voice_available": bool(voice_service.has_key and (
                updated["selected_voice_id"] or settings.elevenlabs_voice_id
            ))}



@app.post("/api/data/evidence")
def data_evidence(request: AskRequest) -> dict[str, Any]:
    """FRED evidence for one question -- an Ask card, never a tab.

    Gated per CONCEPT, not per course. A course-wide data link (ECON has one) does
    not mean every concept in it maps to a series: most micro concepts have no
    aggregate time series at all. So a chart appears only when all of these hold:

      1. the course has a data link,
      2. the question actually asks how the concept applies in the real world
         (the decomposition's application sub-intent), and
      3. FRED search returns a REAL series for the concept that returns data.

    Synthetic series are never rendered here (`allow_synthetic=False`); with no
    FRED key, step 3 finds nothing and the correct result is no chart. The concept
    -- not the whole question -- is what is searched; no id is ever written by hand.
    """
    user = current_user()
    store = get_store()
    course = store.course(user, request.course) if request.course else None
    if course is None or not course.has_data_link:
        return {"found": False,
                "detail": "this course has no real-world data link",
                "query": ""}

    from .agent.tools import ToolBox
    from .data.propose import propose_series_queries
    from .llm import resolve_product_llm
    from .retrieval.decompose import decompose_heuristic

    decomp = decompose_heuristic(request.question)
    no_chart = {"found": False, "query": decomp.concept}
    # Per-concept gate 1: only reach for data when the question asks about
    # real-world application. "What is the budget constraint?" alone gets no chart.
    if not decomp.wants_application:
        return {**no_chart, "detail": "no real-world-data intent detected for this concept"}
    # No live source -> no chart. Synthetic is never rendered in the product, so
    # without a FRED key the honest result is nothing, not a fabricated series.
    if not settings.has_fred:
        return {**no_chart, "detail": "no FRED key configured; no real data to show"}

    # Per-concept gate 2: the model proposes the SEARCH QUERIES that map this
    # concept to real indicators (budget constraint -> disposable income, CPI). An
    # empty proposal means no series illustrates the concept -> no chart.
    queries = propose_series_queries(
        decomp.concept, resolve_product_llm(get_store().get_prefs(current_user())))
    if not queries:
        return {**no_chart, "detail": "no real-world series maps to this concept"}

    toolbox = ToolBox(pipeline(user), course_hint=request.course,
                      has_data_link=True, allow_synthetic=False)
    series_out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for q in queries:
        if len(series_out) >= 2:
            break
        found = toolbox.find_data_series({"query": q})
        if not found.ok:
            continue
        # One series per proposal, so distinct concepts (e.g. prices AND income)
        # each get a slot instead of one concept filling both.
        for hit in found.payload["series"]:
            if hit["series_id"] in seen_ids:
                continue
            fetched = toolbox.fetch_data_series({"series_id": hit["series_id"]})
            if not fetched.ok:
                # A series that does not actually return data is not evidence; drop
                # it rather than render an empty or errored plot.
                continue
            seen_ids.add(hit["series_id"])
            entry = dict(hit)
            entry["points"] = fetched.payload["points"]
            entry["transform"] = fetched.payload["summary"]["transform_meaning"]
            entry["proposed_for"] = q
            series_out.append(entry)
            break  # move to the next proposal query

    if not series_out:
        return {**no_chart, "detail": "no FRED series maps to this concept"}
    return {"found": True, "query": decomp.concept, "series": series_out}


# ---------------------------------------------------------------------------
# Tab 2 -- Dashboard
# ---------------------------------------------------------------------------


@app.get("/api/dashboard")
def dashboard(today: Optional[str] = None) -> dict[str, Any]:
    store = get_store()
    user = current_user()
    when = date.fromisoformat(today) if today else date.today()
    exam = exam_readiness(store, user, when)
    undated = store.schedule_undated(user)
    return {
        "today": today_view(store, user, when),
        "week": week_view(store, user, when),
        "exam": exam.to_dict() if exam else None,
        # Undated 'course outline' rows render in their own section, never dropped.
        "undated": [
            {"course_id": r["course_id"], "topic": r["topic"],
             "chapter_refs": r["chapter_refs"], "readings": r["readings"],
             "kind": r["kind"], "link_title": r["link_title"]}
            for r in undated
        ],
        "undated_note": (
            "These come from an undated course outline. Anything date-dependent "
            "above is computed over dated rows only."
        ) if undated else "",
    }


# ---------------------------------------------------------------------------
# Calendar: unified month view, manual events, holidays, check-off progress
# ---------------------------------------------------------------------------


class EventInput(BaseModel):
    id: Optional[str] = None
    course_id: str = ""
    title: str
    kind: str = "custom"
    date: Optional[str] = None
    start_time: str = ""
    end_time: str = ""
    location: str = ""
    notes: str = ""
    recurrence: str = "none"          # none | weekly
    recur_days: str = ""              # 'MO,WE,FR'
    recur_until: Optional[str] = None


class ToggleInput(BaseModel):
    item_id: str
    done: bool


@app.get("/api/calendar")
def calendar_feed(start: str, end: str) -> dict[str, Any]:
    """Every dated item -- syllabus concepts, assessments, user events, holidays --
    between start and end (ISO dates), normalised and check-off aware."""
    from .calendar_view import calendar_items

    store, user = get_store(), current_user()
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(422, "start and end must be ISO dates (YYYY-MM-DD)")
    if (e - s).days > 400:
        raise HTTPException(422, "range too large")
    items = calendar_items(store, user, s, e)
    return {"items": items, "courses": [
        {"course_id": c.course_id, "code": c.code} for c in store.courses(user)]}


@app.get("/api/calendar/progress")
def calendar_progress(today: Optional[str] = None) -> dict[str, Any]:
    from .calendar_view import progress

    when = date.fromisoformat(today) if today else date.today()
    return progress(get_store(), current_user(), when)


@app.post("/api/calendar/events")
def create_event(req: EventInput) -> dict[str, Any]:
    store, user = get_store(), current_user()
    if not req.title.strip():
        raise HTTPException(422, "an event needs a title")
    if req.recurrence == "weekly" and not req.recur_days:
        raise HTTPException(422, "a weekly event needs at least one weekday")
    event = req.model_dump()
    event["user_id"] = user
    event_id = store.upsert_event(event)
    return {"ok": True, "id": event_id}


@app.put("/api/calendar/events/{event_id}")
def update_event(event_id: str, req: EventInput) -> dict[str, Any]:
    store, user = get_store(), current_user()
    if store.event(user, event_id) is None:
        raise HTTPException(404, "no such event")
    event = req.model_dump()
    event["id"] = event_id
    event["user_id"] = user
    store.upsert_event(event)
    return {"ok": True, "id": event_id}


@app.delete("/api/calendar/events/{event_id}")
def remove_event(event_id: str) -> dict[str, Any]:
    if not get_store().delete_event(current_user(), event_id):
        raise HTTPException(404, "no such event")
    return {"ok": True}


@app.post("/api/courses/{course_id}/schedule/extract")
def extract_course_schedule(course_id: str) -> dict[str, Any]:
    """Pull class meeting times, locations, and exam/deadline dates out of the
    course's syllabus into calendar events, so the calendar shows this course's
    classes and exams even when the syllabus has no day-by-day table."""
    from pathlib import Path as _Path

    from .syllabus.extract import read_syllabus
    from .syllabus.meetings import events_from_meetings, extract_meetings

    store, user = get_store(), current_user()
    if store.course(user, course_id) is None:
        raise HTTPException(404, "course not found")
    syll = next((s for s in store.sources(user, course_id)
                 if s.type == SourceType.SYLLABUS or "syllabus" in s.source_id.lower()), None)
    if syll is None or not _Path(syll.file).exists():
        raise HTTPException(404, "no syllabus file found for this course; upload one first")

    llm = resolve_product_llm(store.get_prefs(user))
    if not getattr(llm, "available", False):
        raise HTTPException(503, "AI is required to read a syllabus; turn on AI or add a key in AI & voice settings")

    text = read_syllabus(_Path(syll.file))
    extracted = extract_meetings(text, llm)
    if extracted is None:
        raise HTTPException(502, "could not read the syllabus")
    events = events_from_meetings(extracted, course_id)
    store.delete_events_from_syllabus(user, course_id)
    for ev in events:
        ev["user_id"] = user
        store.upsert_event(ev)
    return {"ok": True, "course_id": course_id,
            "meetings": len(extracted["meetings"]), "key_dates": len(extracted["key_dates"]),
            "term": [extracted.get("term_start"), extracted.get("term_end")]}


@app.post("/api/calendar/toggle")
def toggle_task(req: ToggleInput) -> dict[str, Any]:
    """Check a calendar item off. Assessments update their own status so the
    Assessments list stays in sync; everything else uses the generic task flag."""
    store, user = get_store(), current_user()
    if req.item_id.startswith("assess:"):
        from .models import AssessmentStatus

        a = store.assessment(user, req.item_id.split(":", 1)[1])
        if a is None:
            raise HTTPException(404, "no such assessment")
        a.status = AssessmentStatus.DONE if req.done else AssessmentStatus.NOT_STARTED
        store.upsert_assessment(a)
    else:
        store.set_task_done(user, req.item_id, req.done)
    return {"ok": True, "item_id": req.item_id, "done": req.done}


@app.get("/api/engagement")
def engagement() -> dict[str, Any]:
    store = get_store()
    user = current_user()
    refs = sorted(
        {
            f"{c.course_id}:{c.source_id}:{c.chapter_num}"
            for c in store.all_chunks(user)
        }
    )
    return {"chapters": [c.to_dict() for c in chapter_readiness(store, user, refs)]}


@app.post("/api/progress")
def set_progress(request: ProgressRequest) -> dict[str, Any]:
    """Manual override. Stored separately from the derived signal and labelled
    self-reported -- it never overwrites what the engagement log says."""
    get_store().set_manual_progress(
        current_user(), request.chapter_ref, request.value
    )
    return {"ok": True, "chapter_ref": request.chapter_ref, "self_reported": request.value}


@app.get("/api/runs")
def runs() -> dict[str, Any]:
    """Agent run log -- the raw material for the usage study."""
    rows = get_store().agent_runs(current_user())
    answered = [r for r in rows if not r["refused"]]
    return {
        "n": len(rows),
        "answered": len(answered),
        "refused": len(rows) - len(answered),
        "median_latency_ms": sorted(r["latency_ms"] for r in rows)[len(rows) // 2]
        if rows
        else 0,
        "runs": rows[:100],
    }


# ---------------------------------------------------------------------------
# Progress, assessments, calendar
# ---------------------------------------------------------------------------


@app.get("/api/progress-report")
def progress_report_endpoint(today: Optional[str] = None) -> dict[str, Any]:
    """Two bars on one axis: class pace (date-driven) vs your engagement."""
    when = date.fromisoformat(today) if today else date.today()
    return progress_report(get_store(), current_user(), when).to_dict()


class AssessmentInput(BaseModel):
    course_id: str
    kind: str = "homework"
    title: str = ""
    due_date: Optional[str] = None      # ISO date or null
    weight: float = 0.0
    chapter_refs: list[str] = []
    status: str = "not_started"


class StatusInput(BaseModel):
    status: str


@app.get("/api/assessments")
def list_assessments(course_id: Optional[str] = None) -> dict[str, Any]:
    from datetime import date as _date

    store = get_store()
    items = store.assessments(current_user(), course_id)
    today = _date.today().isoformat()
    out = []
    for a in items:
        d = a.to_dict()
        d["overdue"] = bool(
            a.due_date and a.due_date.isoformat() < today
            and a.status.value != "done"
        )
        out.append(d)
    return {"assessments": out}


@app.post("/api/assessments")
def add_assessment(item: AssessmentInput) -> dict[str, Any]:
    """Manual add. Flagged user-entered -- extraction is never perfect and a
    student must be able to fix or add without re-uploading."""
    import uuid as _uuid
    from datetime import date as _date

    from .models import AssessmentKind, AssessmentRecord, AssessmentStatus

    record = AssessmentRecord(
        id=_uuid.uuid4().hex,
        user_id=current_user(),
        course_id=item.course_id,
        kind=AssessmentKind(item.kind),
        title=item.title,
        due_date=_date.fromisoformat(item.due_date) if item.due_date else None,
        weight=item.weight,
        chapter_refs=item.chapter_refs,
        status=AssessmentStatus(item.status),
        user_entered=True,
        source="manual",
    )
    get_store().upsert_assessment(record)
    return {"ok": True, "id": record.id}


@app.put("/api/assessments/{assessment_id}")
def edit_assessment(assessment_id: str, item: AssessmentInput) -> dict[str, Any]:
    import uuid as _uuid  # noqa: F401
    from datetime import date as _date

    from .models import AssessmentKind, AssessmentRecord, AssessmentStatus

    store = get_store()
    existing = store.assessment(current_user(), assessment_id)
    if existing is None:
        raise HTTPException(404, "no such assessment")
    record = AssessmentRecord(
        id=assessment_id, user_id=current_user(), course_id=item.course_id,
        kind=AssessmentKind(item.kind), title=item.title,
        due_date=_date.fromisoformat(item.due_date) if item.due_date else None,
        weight=item.weight, chapter_refs=item.chapter_refs,
        status=AssessmentStatus(item.status),
        user_entered=True,                # an edit makes it user-owned
        source="manual" if existing.source == "manual" else "edited",
    )
    store.upsert_assessment(record)
    return {"ok": True, "id": assessment_id}


@app.post("/api/assessments/{assessment_id}/status")
def set_assessment_status(assessment_id: str, body: StatusInput) -> dict[str, Any]:
    from .models import AssessmentStatus

    store = get_store()
    a = store.assessment(current_user(), assessment_id)
    if a is None:
        raise HTTPException(404, "no such assessment")
    a.status = AssessmentStatus(body.status)
    store.upsert_assessment(a)
    return {"ok": True, "status": a.status.value}


@app.delete("/api/assessments/{assessment_id}")
def delete_assessment(assessment_id: str) -> dict[str, Any]:
    n = get_store().delete_assessment(current_user(), assessment_id)
    if not n:
        raise HTTPException(404, "no such assessment")
    return {"ok": True}


@app.get("/api/calendar/subscribe")
def calendar_subscribe() -> dict[str, Any]:
    """Hand the student a stable feed URL to paste into Google/Apple Calendar."""
    token = get_store().feed_token(current_user())
    return {
        "token": token,
        "path": f"/calendar/{token}.ics",
        "how": "Add by URL in Google Calendar (Other calendars -> From URL) or "
               "Apple Calendar (File -> New Calendar Subscription).",
    }


@app.get("/calendar/{token}.ics")
def calendar_feed(token: str) -> Response:
    """Public, read-only, token-gated .ics. Regenerated on every read so calendar
    clients pick up schedule edits on their next refresh."""
    store = get_store()
    user = store.user_for_token(token)
    if user is None:
        raise HTTPException(404, "unknown feed")
    ics = build_ics(store.assessments(user))
    return Response(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="course-copilot.ics"'},
    )


# ---------------------------------------------------------------------------
# Onboarding: upload materials, extract a syllabus, review the flagged rows
# ---------------------------------------------------------------------------

_SYLLABUS_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
_SYLLABUS_NAME = re.compile(r"syllabus|syll|course[ _-]*outline", re.I)


def _grade_syllabus_candidates(user: str, course_id: str) -> list[dict[str, Any]]:
    """Resolve syllabus candidates from the existing course source records and
    the same course upload folder used by both upload flows. Legacy syllabus
    uploads predate source registration, so the folder scan is intentionally
    course-scoped and non-recursive."""
    store = get_store()
    course = store.course(user, course_id)
    if course is None:
        raise HTTPException(404, "course not found")
    root = (UPLOAD_DIR / user / course_id).resolve()
    records: dict[Path, dict[str, Any]] = {}
    for source in store.sources(user, course_id):
        try:
            path = Path(source.file).resolve(strict=True)
            path.relative_to(root)
        except (OSError, ValueError):
            continue
        if path.is_file() and path.suffix.lower() in _SYLLABUS_EXTENSIONS:
            named_or_marked = source.type == SourceType.SYLLABUS or bool(_SYLLABUS_NAME.search(path.name))
            try:
                # Generic, unmarked materials are content-sniffed only when
                # modest in size; named/explicit syllabi can use the full limit.
                if path.stat().st_size > 2 * 1024 * 1024 and not named_or_marked:
                    continue
            except OSError:
                continue
            records[path] = {"source_id": source.source_id, "name": path.name,
                             "explicit": source.type == SourceType.SYLLABUS}
    if root.exists():
        for path in root.iterdir():
            if path.is_file() and path.suffix.lower() in _SYLLABUS_EXTENSIONS and _SYLLABUS_NAME.search(path.name):
                records.setdefault(path.resolve(), {"source_id": _source_id(course_id, path.name),
                    "name": path.name, "explicit": False})

    code_match = re.search(r"([A-Z]{2,8})\s*[- ]?(\d{1,4})", course.code.upper())
    expected_subject, expected_number = code_match.groups() if code_match else ("", "")
    title = course.title.casefold()
    title_alias = "computer science" if "computer science" in title else ""
    is_calculus = "calculus" in title or expected_subject.startswith("CALC")
    academic_prefixes = {"CS", "CSC", "CSCI", "ECON", "MATH", "MAT", "CALC",
                         "ENG", "ENGL", "ART", "BIO", "CHEM", "PHYS", "PSY",
                         "STAT", "HIST", "POLI", "BUS", "ACCT"}
    candidates: list[dict[str, Any]] = []
    for path, meta in records.items():
        try:
            if path.stat().st_size > 15 * 1024 * 1024:
                continue
            text = read_syllabus(path)
        except Exception:
            continue
        clean = text.strip()
        if not clean:
            continue
        opening = clean[:12000]
        syllabus_mention = bool(re.search(r"\bsyllabus\b", opening, re.I))
        syllabus_structure = bool(re.search(
            r"\b(course description|course objectives|grading policy|grading scale|assessment weights|course outline|course requirements)\b",
            opening, re.I))
        has_syllabus_content = syllabus_mention and syllabus_structure
        if not (meta["explicit"] or _SYLLABUS_NAME.search(path.name) or has_syllabus_content):
            continue
        if expected_subject and expected_number:
            identifiers = re.findall(r"\b([A-Z]{2,8})\s*[- ]?(\d{1,4})\b", clean[:6000].upper())
            subject_codes = [(subject, number) for subject, number in identifiers
                             if subject == expected_subject or
                             (title_alias and subject in {"CS", "CSC", "CSCI"}) or
                             (is_calculus and subject in {"MATH", "MAT", "CALC"})]
            if subject_codes and not (is_calculus and any(
                    subject in {"MATH", "MAT", "CALC"} for subject, _number in subject_codes)) \
                    and (expected_subject, expected_number) not in subject_codes:
                # A same-subject conflicting number (e.g. CSCI 243 in CSCI 303),
                # or a clearly different subject in a course whose code is exact
                # (e.g. CSCI 243 in ECON 303), is not this course's syllabus.
                continue
            academic_codes = [(subject, number) for subject, number in identifiers
                              if subject in academic_prefixes]
            # A one-digit course code like CALC 2 often maps to a catalog code
            # such as MATH 112. In that case retain course-folder and filename
            # association. For full catalog numbers, reject clear conflicting
            # syllabus codes unless the document carries that same number.
            if (not subject_codes and len(expected_number) >= 2 and academic_codes
                    and expected_number not in {number for _subject, number in academic_codes}):
                continue
        text_hash = hashlib.sha256(clean.encode()).hexdigest()
        candidates.append({"source_id": meta["source_id"], "file_name": meta["name"],
                           "hash": text_hash, "text": clean,
                           "explicit": meta["explicit"]})

    # Identical files copied into the legacy folder are one choice, not ambiguity.
    deduped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        existing = deduped.get(candidate["hash"])
        if existing is None or (candidate["explicit"] and not existing["explicit"]):
            deduped[candidate["hash"]] = candidate
    return sorted(deduped.values(), key=lambda item: (not item["explicit"], item["file_name"].casefold()))


def _grade_llm(store, user: str):
    prefs = store.get_prefs(user)
    if settings.offline:
        return None
    if prefs.get("gemini_api_key"):
        from .gemini import GeminiClient
        return GeminiClient(prefs["gemini_api_key"])
    if prefs.get("llm_consent") and settings.gemini_api_key:
        from .gemini import GeminiClient
        return GeminiClient(settings.gemini_api_key)
    return None


def _grade_syllabus_publication(candidate: dict[str, Any], extraction: Optional[dict[str, Any]],
                                confirmed_hash: str) -> dict[str, Any]:
    return {"source_id": candidate["source_id"], "file_name": candidate["file_name"],
            "status": "analyzed" if confirmed_hash == candidate["hash"] else
                      (extraction["status"] if extraction else "not_analyzed"),
            "schema": extraction["schema"] if extraction else None,
            "extraction_id": extraction["id"] if extraction else None,
            "hash": candidate["hash"]}


def _analyze_grade_syllabus(user: str, course_id: str, candidate: dict[str, Any], force: bool = False) -> dict[str, Any]:
    store = get_store()
    store.save_grade_syllabus_selection(user, course_id, candidate["source_id"],
                                        candidate["file_name"], candidate["hash"])
    cached = store.grade_extraction_for_syllabus(user, course_id, candidate["source_id"], candidate["hash"])
    if cached:
        try:
            validate_schema(cached["schema"])
        except GradeSchemaError:
            cached = None
    if cached and not force:
        return {"status": "cached", "extraction": cached}
    grade_llm = _grade_llm(store, user)
    if grade_llm is None:
        if settings.offline:
            raise HTTPException(503, "Gemini is disabled because the server is in offline mode. Restart Course Copilot with COPILOT_OFFLINE=0.")
        raise HTTPException(503, "Gemini is unavailable. Enable the existing AI setting or add your Gemini API key.")
    # The entire extracted document is sent, including text distributed across sections.
    raw = grade_llm.complete(GRADE_PROMPT + candidate["text"] + "\nFULL SYLLABUS END",
                             max_tokens=8000, temperature=0,
                             response_schema=GRADE_SCHEMA, cache=False)
    proposed = validate_schema(json.loads(raw))
    extraction_id = store.save_grade_extraction(user, course_id, candidate["hash"],
        proposed, grade_llm.backend, candidate["source_id"], candidate["file_name"])
    return {"status": "pending_review", "extraction": {
        "id": extraction_id, "schema": proposed, "backend": grade_llm.backend,
        "status": "pending", "syllabus_hash": candidate["hash"],
        "source_id": candidate["source_id"], "file_name": candidate["file_name"]}}


def _save_uploads(files: list[UploadFile], folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for upload in files:
        target = folder / (upload.filename or uuid.uuid4().hex)
        target.write_bytes(upload.file.read())
        saved.append(target)
    return saved


@app.post("/api/courses")
async def create_course(code: str = Form(...), title: str = Form("")) -> dict[str, Any]:
    """A course exists because someone created it. Nothing is seeded."""
    user = current_user()
    course_id = slugify(code) or uuid.uuid4().hex[:8]
    store = get_store()
    store.upsert_course_stub(user, course_id, code, title or code)
    return {"course_id": course_id, "code": code, "title": title or code}


def _start_material_ingest(
    paths: list[Path], course_id: str, user: str, store,
    file_item_ids: Optional[dict[str, str]] = None,
    file_titles: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Create one ordinary Course Copilot ingest job for any trusted adapter."""
    course = store.course(user, course_id)
    job_id = uuid.uuid4().hex
    file_records = [
        {"filename": p.name, "item_id": (file_item_ids or {}).get(p.name, ""),
         "stage": "queued", "pages_done": 0, "pages_total": 0,
         "chunks": 0, "status": "", "detail": ""}
        for p in paths
    ]
    store.create_job(job_id, user, course_id, file_records)

    def worker() -> None:
        def on_progress(i: int, **patch) -> None:
            job = store.get_job(job_id, user)
            if not job or i >= len(job["files"]):
                return
            job["files"][i].update({k: v for k, v in patch.items() if v is not None})
            store.update_job(job_id, status="running", files=job["files"])

        def on_file_done() -> None:
            # A finished document is searchable now -- drop the cached pipeline so
            # the next Ask sees it, enabling the course mid-batch.
            invalidate_pipeline(user)

        try:
            with _material_ingest_semaphore:
                store.update_job(job_id, status="running")
                ingest_files(
                    paths, user_id=user, course_id=course_id,
                    code=course.code if course else course_id,
                    title=course.title if course else course_id,
                    store=store, on_progress=on_progress, on_file_done=on_file_done,
                    file_titles=file_titles,
                )
                store.update_job(job_id, status="done")
        except Exception as exc:  # noqa: BLE001 - surface, never crash the server
            job = store.get_job(job_id, user) or {"files": file_records}
            logger.error("Material ingest job failed (%s).", type(exc).__name__)
            failed_files = job["files"]
            for item in failed_files:
                if item.get("status") not in {"failed", "unsupported"}:
                    item.update(stage="failed", status="failed",
                                detail="Indexing could not finish. Retry the sync; see the local server log if it happens again.")
            store.update_job(job_id, status="failed", files=failed_files)
        finally:
            invalidate_pipeline(user)

    threading.Thread(target=worker, name=f"ingest-{job_id[:8]}", daemon=True).start()
    return {"job_id": job_id, "course_id": course_id,
            "files": [f["filename"] for f in file_records]}


@app.post("/api/courses/{course_id}/materials")
async def upload_materials(course_id: str, files: list[UploadFile]) -> dict[str, Any]:
    """Upload textbooks/readings through the canonical asynchronous ingest path."""
    user = current_user()
    if not files:
        raise HTTPException(400, "no files uploaded")
    paths = _save_uploads(files, UPLOAD_DIR / user / course_id)
    return _start_material_ingest(paths, course_id, user, get_store())


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    """Poll the progress of a background ingest job."""
    job = get_store().get_job(job_id, current_user())
    if job is None:
        raise HTTPException(404, "no such job")
    return job


# ---------------------------------------------------------------------------
# Practice: assessments upload, parsing, verified variants, escalating help
# ---------------------------------------------------------------------------


@app.post("/api/courses/{course_id}/assessments")
async def upload_assessments(course_id: str, files: list[UploadFile]) -> dict[str, Any]:
    """Upload problem sets / worksheets / past exams. These are parsed into
    practice problems and NEVER added to the retrieval corpus, so a homework can
    never be cited as if it were the textbook. Runs in the background like a
    material upload; poll /api/jobs/{id}."""
    user = current_user()
    if get_store().course(user, course_id) is None:
        raise HTTPException(404, "no such course")
    if not files:
        raise HTTPException(400, "no files uploaded")
    paths = _save_uploads(files, UPLOAD_DIR / user / course_id / "assessments")
    store = get_store()
    llm = resolve_product_llm(store.get_prefs(user))

    job_id = uuid.uuid4().hex
    records = [{"filename": p.name, "stage": "queued", "pages_done": 0,
                "pages_total": 0, "chunks": 0, "status": "", "detail": ""} for p in paths]
    store.create_job(job_id, user, course_id, records)

    def worker() -> None:
        from .practice.service import ingest_assessment

        def on_progress(i: int, **patch) -> None:
            job = store.get_job(job_id, user)
            if not job or i >= len(job["files"]):
                return
            job["files"][i].update({k: v for k, v in patch.items() if v is not None})
            store.update_job(job_id, status="running", files=job["files"])

        store.update_job(job_id, status="running")
        try:
            for idx, path in enumerate(paths):
                rep = ingest_assessment(
                    path, user_id=user, course_id=course_id, store=store, llm=llm,
                    on_progress=lambda _i=idx, **p: on_progress(idx, **p))
                job = store.get_job(job_id, user)
                if job and idx < len(job["files"]):
                    job["files"][idx]["detail"] = (
                        f"{rep.parsed} parsed · {rep.needs_review} need review · "
                        f"{rep.variants_kept} verified variants · "
                        f"{rep.variants_discarded}/{rep.variants_generated} discarded "
                        f"({int(rep.discard_rate*100)}%) · {rep.unverified} unverified")
                    store.update_job(job_id, files=job["files"])
            store.update_job(job_id, status="done")
        except Exception as exc:  # noqa: BLE001
            store.update_job(job_id, status="failed")
        finally:
            invalidate_pipeline(user)

    threading.Thread(target=worker, name=f"assess-{job_id[:8]}", daemon=True).start()
    return {"job_id": job_id, "course_id": course_id,
            "files": [f["filename"] for f in records]}


@app.get("/api/practice/problems")
def practice_problems(course: Optional[str] = None) -> dict[str, Any]:
    """Problems for Practice mode, solutions withheld. Includes each problem's
    attempt history so the UI can resurface missed ones."""
    user = current_user()
    store = get_store()
    problems = store.problems(user, course)
    attempts = store.attempts(user)
    by_problem: dict[str, list] = {}
    for a in attempts:
        by_problem.setdefault(a["problem_id"], []).append(a)
    out = []
    for p in problems:
        d = p.public(with_solution=False)
        d["max_help_level"] = 2 if p.origin == "uploaded" else 3
        d["attempts"] = by_problem.get(p.id, [])
        out.append(d)
    # Put missed work first so it naturally resurfaces at the next session.
    out.sort(key=lambda p: (
        0 if any(not a["correct"] for a in p["attempts"]) else 1,
        -sum(not a["correct"] for a in p["attempts"]),
        0 if p["origin"] == "uploaded" else 1,
        p["number"], p["id"],
    ))
    return {"problems": out, "count": len(out)}


class HelpRequest(BaseModel):
    problem_id: str
    level: int


@app.post("/api/practice/help")
def practice_help(req: HelpRequest) -> dict[str, Any]:
    """Escalating help. The integrity gate lives in practice.help: level 3
    (full solution) is unreachable for an uploaded original."""
    user = current_user()
    store = get_store()
    problem = store.problem(user, req.problem_id)
    if problem is None:
        raise HTTPException(404, "no such problem")
    from .practice.help import help_for

    llm = resolve_product_llm(store.get_prefs(user))
    return help_for(problem, max(1, min(3, req.level)), llm)


class AttemptRequest(BaseModel):
    problem_id: str
    correct: bool
    help_level: int = 0


@app.post("/api/practice/attempt")
def practice_attempt(req: AttemptRequest) -> dict[str, Any]:
    """Record an attempt and credit engagement. A worked problem counts toward
    the chapter's engagement at the problem-set weight (2x a question), never as
    mastery."""
    user = current_user()
    store = get_store()
    problem = store.problem(user, req.problem_id)
    if problem is None:
        raise HTTPException(404, "no such problem")
    store.record_attempt(user, problem.id, problem.chapter_ref, req.correct,
                         max(0, min(3, req.help_level)))
    if problem.chapter_ref:
        store.bump_engagement(user, problem.chapter_ref, ps_hits=1)
    return {"ok": True}


@app.get("/api/practice/test")
def practice_test(course: Optional[str] = None, n: int = 8) -> dict[str, Any]:
    """Assemble a Test-yourself set: verified generated problems weighted toward
    the chapters an upcoming exam covers. No hints; scored at the end."""
    user = current_user()
    store = get_store()
    generated = [p for p in store.problems(user, course, origin="generated") if p.verified]
    # Weight by chapters the next exam covers, if any.
    exam_chapters: set[str] = set()
    for a in store.assessments(user, course) if hasattr(store, "assessments") else []:
        if getattr(a, "kind", None) and getattr(a.kind, "is_exam", False):
            exam_chapters.update(a.chapter_refs or [])
    def weight(p) -> tuple:
        return (0 if p.chapter_ref in exam_chapters else 1, p.id)
    generated.sort(key=weight)
    chosen = generated[:max(1, n)]
    return {"problems": [p.public(with_solution=False) for p in chosen],
            "count": len(chosen),
            "weighted_by_exam": bool(exam_chapters)}


class TestGradeRequest(BaseModel):
    answers: dict[str, str]


@app.post("/api/practice/test/grade")
def grade_practice_test(req: TestGradeRequest) -> dict[str, Any]:
    """Grade an ended Test-yourself set, then release verified answer keys.

    Only owned, generated, sympy-verified problems can enter this endpoint. The
    browser withholds the answer fields until the student submits the whole set.
    """
    user = current_user()
    store = get_store()
    results = []
    for problem_id, entered in req.answers.items():
        problem = store.problem(user, problem_id)
        if problem is None or problem.origin != "generated" or not problem.verified:
            raise HTTPException(400, "test contains a problem without a verified key")
        expected = {k: float(v) for k, v in re.findall(
            r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)",
            problem.answer, flags=re.I)}
        given = {k: float(v) for k, v in re.findall(
            r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)",
            entered or "", flags=re.I)}
        correct = bool(expected) and expected.keys() <= given.keys() and all(
            math.isclose(given[k], value, rel_tol=1e-3, abs_tol=1e-5)
            for k, value in expected.items())
        store.record_attempt(user, problem.id, problem.chapter_ref, correct, 0)
        if problem.chapter_ref:
            store.bump_engagement(user, problem.chapter_ref, ps_hits=1)
        results.append({
            "id": problem.id, "prompt": problem.public()["prompt"],
            "answer": problem.public(with_solution=True).get("answer", ""),
            "solution_steps": problem.public(with_solution=True).get("solution_steps", ""),
            "correct": correct,
            "chapter_ref": problem.chapter_ref,
        })
    score = sum(r["correct"] for r in results)
    return {"score": score, "total": len(results),
            "percent": round(100 * score / len(results)) if results else 0,
            "results": results}


@app.get("/api/courses/{course_id}/syllabus")
def get_grade_syllabus(course_id: str) -> dict[str, Any]:
    user, store = current_user(), get_store()
    candidates = _grade_syllabus_candidates(user, course_id)
    data = store.grade_predictor(user, course_id)
    options = []
    for candidate in candidates:
        extraction = store.grade_extraction_for_syllabus(
            user, course_id, candidate["source_id"], candidate["hash"])
        options.append(_grade_syllabus_publication(candidate, extraction,
            data.get("confirmed_syllabus_hash", "") if data.get("confirmed_source_id") == candidate["source_id"] else ""))
    selection = store.grade_syllabus_selection(user, course_id)
    selected_id = selection["source_id"] if selection and any(
        row["source_id"] == selection["source_id"] for row in options) else ""
    if not selected_id and len(options) == 1:
        selected_id = options[0]["source_id"]
        chosen = candidates[0]
        store.save_grade_syllabus_selection(user, course_id, chosen["source_id"],
                                            chosen["file_name"], chosen["hash"])
    chosen = next((row for row in options if row["source_id"] == selected_id), None)
    return {"exists": bool(options), "candidates": [
        {k: row[k] for k in ("source_id", "file_name", "status", "extraction_id")}
        for row in options], "selected_source_id": selected_id,
        "selected": chosen, "ambiguous": len(options) > 1 and not selected_id,
        "confirmed": data["confirmed"] if chosen and chosen["status"] == "analyzed" else None}


@app.post("/api/courses/{course_id}/syllabus/select")
def select_grade_syllabus(course_id: str, req: GradeSyllabusSelectionRequest) -> dict[str, Any]:
    user = current_user()
    candidate = next((item for item in _grade_syllabus_candidates(user, course_id)
                      if item["source_id"] == req.source_id), None)
    if candidate is None:
        raise HTTPException(404, "syllabus not found for this course")
    get_store().save_grade_syllabus_selection(user, course_id, candidate["source_id"],
                                              candidate["file_name"], candidate["hash"])
    return {"ok": True, "source_id": candidate["source_id"]}


@app.post("/api/courses/{course_id}/syllabus/analyze")
def analyze_grade_syllabus(course_id: str, req: GradeSyllabusAnalyzeRequest) -> dict[str, Any]:
    user, store = current_user(), get_store()
    candidates = _grade_syllabus_candidates(user, course_id)
    selected = req.source_id or (store.grade_syllabus_selection(user, course_id) or {}).get("source_id")
    if not selected and len(candidates) == 1:
        selected = candidates[0]["source_id"]
    candidate = next((item for item in candidates if item["source_id"] == selected), None)
    if candidate is None:
        if candidates:
            raise HTTPException(409, "Select which syllabus to use for this course.")
        raise HTTPException(404, "no syllabus found for this course")
    result = _analyze_grade_syllabus(user, course_id, candidate, force=req.force)
    return {"status": result["status"], "extraction": result["extraction"]}


@app.post("/api/courses/{course_id}/syllabus")
async def upload_syllabus(
    course_id: str, file: UploadFile, commit_now: bool = True
) -> dict[str, Any]:
    """Double-extract, link rows to chapters semantically, and commit.

    The response carries everything the review screen needs: only the rows that
    are flagged, each with why.
    """
    user = current_user()
    paths = _save_uploads([file], UPLOAD_DIR / user / course_id)
    text = read_syllabus(paths[0])
    if not text.strip():
        raise HTTPException(422, "no text could be extracted from that syllabus")

    store = get_store()
    course = store.course(user, course_id)
    if course is None:
        raise HTTPException(404, "course not found")
    upload_path = paths[0].resolve()
    syllabus_source_id = _source_id(course_id, upload_path.name)
    store.upsert_source(Source(syllabus_source_id, user, course_id,
        upload_path.stem.replace("_", " ")[:80], SourceType.SYLLABUS,
        str(upload_path), file_hash=hashlib.sha256(upload_path.read_bytes()).hexdigest()))

    diffed = double_extract(text, course_id, primary_source=None)
    if commit_now:
        linker = build_linker(store, user, course_id)
        summary = commit_syllabus(store, diffed.extraction, user, linker=linker)
        summary["extraction_backend"] = diffed.backend
        summary["extraction_conflicts"] = len(diffed.conflicts)
        summary["agreement_rate"] = round(diffed.agreement_rate, 3)
    else:
        summary = {"course_id": course_id, "backend": diffed.backend,
                   "conflicts": len(diffed.conflicts)}
    # Grade extraction is a separate proposal and never changes confirmed rules
    # or actual grades. The complete extracted text goes through the shared path.
    try:
        candidate = {"source_id": syllabus_source_id, "file_name": upload_path.name,
                     "hash": hashlib.sha256(text.strip().encode()).hexdigest(), "text": text.strip()}
        result = _analyze_grade_syllabus(user, course_id, candidate)
        extraction = result["extraction"]
        if result["status"] == "cached":
            summary["grade_extraction"] = {"status": "already_analyzed", "id": extraction["id"]}
        else:
            proposed = extraction["schema"]
            summary["grade_extraction"] = {"status": "pending_review", "id": extraction["id"],
                "backend": extraction["backend"], "uncertainties": proposed["uncertainties"],
                "component_count": len(proposed["components"])}
    except HTTPException as exc:
        if exc.status_code == 503:
            summary["grade_extraction"] = {"status": "unavailable", "message":
                exc.detail}
        else:
            raise
    except Exception as exc:  # malformed JSON, provider failure, or schema rejection
        logger.warning("Grade extraction could not be completed: %s", str(exc)[:300])
        summary["grade_extraction"] = {"status": "failed", "message":
            "We couldn't confidently determine the grading structure from this syllabus. Please retry or review the syllabus text."}

    # Calendar: class meeting times/locations + exam/deadline dates -> events, so
    # the Schedule tab populates automatically on upload alongside the grade
    # predictor. Consent-gated (owner's own material); skipped, never fatal, when
    # no model is available.
    if commit_now:
        try:
            from .syllabus.meetings import events_from_meetings, extract_meetings

            mtg_llm = resolve_product_llm(store.get_prefs(user))
            extracted = extract_meetings(text, mtg_llm) if getattr(mtg_llm, "available", False) else None
            if extracted:
                store.delete_events_from_syllabus(user, course_id)
                for ev in events_from_meetings(extracted, course_id):
                    ev["user_id"] = user
                    store.upsert_event(ev)
                summary["calendar_extraction"] = {"status": "ok",
                    "meetings": len(extracted["meetings"]), "key_dates": len(extracted["key_dates"])}
            else:
                summary["calendar_extraction"] = {"status": "skipped",
                    "message": "Turn on AI to auto-add class times and exam dates."}
        except Exception as exc:  # noqa: BLE001 - never fail the upload over this
            logger.warning("Meeting extraction failed: %s", str(exc)[:200])
            summary["calendar_extraction"] = {"status": "failed"}

    return summary


@app.get("/api/courses/{course_id}/grades")
def get_grade_predictor(course_id: str) -> dict[str, Any]:
    user = current_user()
    store = get_store()
    if store.course(user, course_id) is None:
        raise HTTPException(404, "course not found")
    data = store.grade_predictor(user, course_id)
    selection = store.grade_syllabus_selection(user, course_id)
    if selection:
        current = next((item for item in _grade_syllabus_candidates(user, course_id)
                        if item["source_id"] == selection["source_id"]), None)
        matching = (store.grade_extraction_for_syllabus(user, course_id,
                    current["source_id"], current["hash"]) if current else None)
        data["pending"] = matching if matching and matching["status"] == "pending" else None
    if data["confirmed"]:
        data["calculation"] = calculate_grade(data["confirmed"], data["scores"])
    return data


@app.put("/api/courses/{course_id}/grades/rules")
def confirm_grade_predictor(course_id: str, req: GradeRulesRequest) -> dict[str, Any]:
    store, user = get_store(), current_user()
    if store.course(user, course_id) is None:
        raise HTTPException(404, "course not found")
    try:
        schema = validate_schema(req.grade_schema)
    except GradeSchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    store.confirm_grade_rules(user, course_id, schema, req.extraction_id)
    return {"ok": True, "schema": schema}


@app.put("/api/courses/{course_id}/grades/{item_id}")
def put_grade(course_id: str, item_id: str, req: GradeScoreRequest) -> dict[str, Any]:
    store, user = get_store(), current_user()
    data = store.grade_predictor(user, course_id)
    schema = data["confirmed"]
    allowed = ({item["id"] for component in (schema or {}).get("components", []) for item in component["items"]} |
               {item["id"] for item in (schema or {}).get("extra_credit", [])})
    if item_id not in allowed:
        raise HTTPException(404, "grade field not found")
    store.set_grade_score(user, course_id, item_id, req.score)
    updated = store.grade_predictor(user, course_id)
    return {"scores": updated["scores"], "calculation": calculate_grade(schema, updated["scores"])}


@app.post("/api/courses/{course_id}/grades/calculate")
def post_grade_calculation(course_id: str, req: GradeCalculateRequest) -> dict[str, Any]:
    store, user = get_store(), current_user()
    if store.course(user, course_id) is None:
        raise HTTPException(404, "course not found")
    data = store.grade_predictor(user, course_id)
    schema = data["confirmed"]
    if not schema:
        raise HTTPException(409, "Confirm the grading structure first.")
    known = ({item["id"] for component in schema["components"] for item in component["items"]} |
             {item["id"] for item in schema.get("extra_credit", [])})
    if not set(req.hypothetical) <= known:
        raise HTTPException(422, "A hypothetical score does not match a course grade field.")
    if any(not math.isfinite(value) or value < 0 or value > 100 for value in req.hypothetical.values()):
        raise HTTPException(422, "Hypothetical scores must be between 0 and 100.")
    if req.target_item_id and req.target_item_id not in known:
        raise HTTPException(422, "Select a grade field from this course.")
    calculation = calculate_grade(schema, data["scores"], req.hypothetical)
    target = req.target_percent
    if req.target_letter:
        row = next((grade for grade in schema.get("grading_scale", {}).get("letter_grades", [])
                    if grade["letter"].casefold() == req.target_letter.casefold()), None)
        if row is None:
            raise HTTPException(422, "That letter grade is not in the syllabus scale.")
        target = row["minimum"]
    target_result = solve_target(schema, data["scores"], target, req.hypothetical) if target is not None else None
    field_targets = None
    if req.target_item_id:
        field_targets = []
        for grade in schema.get("grading_scale", {}).get("letter_grades", []):
            try:
                result = solve_item_target(schema, data["scores"], req.target_item_id,
                    grade["minimum"], req.hypothetical, req.unfilled_assumption)
            except GradeSchemaError as exc:
                raise HTTPException(422, str(exc)) from exc
            field_targets.append({"letter": grade["letter"], "minimum": grade["minimum"], **result})
    # The whole "what do I need for each letter grade" picture, across the empty
    # fields, in one place -- the headline of the grade predictor.
    ladder = grade_ladder(schema, data["scores"], req.hypothetical)
    return {"calculation": calculation, "target": target_result,
            "field_targets": field_targets, "ladder": ladder}


@app.get("/api/courses/{course_id}/review")
def review_rows(course_id: str) -> dict[str, Any]:
    """Only the flagged rows, pre-filled. Both kinds of uncertainty land here:
    extraction disagreement, and low-confidence semantic chapter links."""
    user = current_user()
    store = get_store()
    rows = [r for r in store.schedule(user, course_id=course_id) if r["needs_review"]]
    chapters = store.chapters(user, course_id)
    return {
        "course_id": course_id,
        "flagged": len(rows),
        "total": len(store.schedule(user, course_id=course_id)),
        "rows": [
            {
                "id": r["id"], "date": r["date"], "kind": r["kind"], "topic": r["topic"],
                "readings": r["readings"], "chapter_refs": r["chapter_refs"],
                "confidence": r["confidence"], "link_score": round(r["link_score"], 3),
                "link_method": r["link_method"], "link_title": r["link_title"],
            }
            for r in rows
        ],
        "chapters": [
            {"chapter_ref": f"{c['course_id']}:{c['source_id']}:{c['chapter_num']}",
             "chapter_num": c["chapter_num"], "title": c["title"]}
            for c in chapters
        ],
    }


class ReviewFix(BaseModel):
    row_id: str
    chapter_refs: list[str] = []
    topic: Optional[str] = None
    resolved: bool = True


@app.post("/api/courses/{course_id}/review")
def apply_review(course_id: str, fixes: list[ReviewFix]) -> dict[str, Any]:
    user = current_user()
    store = get_store()
    applied = 0
    for fix in fixes:
        applied += store.update_schedule_row(
            user, fix.row_id, chapter_refs=fix.chapter_refs,
            topic=fix.topic, needs_review=not fix.resolved,
        )
    return {"applied": applied, "remaining": len(
        [r for r in store.schedule(user, course_id=course_id) if r["needs_review"]]
    )}


@app.delete("/api/courses/{course_id}")
def delete_course(course_id: str) -> dict[str, Any]:
    user = current_user()
    if user == "benchmark":
        raise HTTPException(403, "The benchmark corpus is protected from product deletion.")
    store = get_store()
    try:
        store.delete_course_scope(user, course_id, "entire_course")
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "course not found") from exc
    file_cleanup_pending = _remove_course_uploads(user, course_id, "entire_course", [])
    invalidate_pipeline(user)
    return {"deleted": course_id, "file_cleanup_pending": file_cleanup_pending}


_DELETE_SCOPES = {"materials", "syllabus", "assessment_uploads", "entire_course"}
_DELETE_NAME = re.compile(r"syllabus|syll|course[ _-]*outline", re.I)


def _course_upload_root(user: str, course_id: str) -> Path:
    user_root = (UPLOAD_DIR / user).resolve()
    root = (user_root / course_id).resolve()
    try:
        root.relative_to(user_root)
    except ValueError as exc:
        raise HTTPException(400, "Invalid course storage location.") from exc
    return root


def _course_scope_files(user: str, course_id: str, scope: str) -> list[Path]:
    root = _course_upload_root(user, course_id)
    if not root.exists():
        return []
    store = get_store()
    sources = store.sources(user, course_id)
    selected: set[Path] = set()
    if scope == "entire_course":
        raw = (path for path in root.rglob("*") if path.is_file())
    else:
        allowed = {
            "materials": {"textbook", "slides", "notes"},
            "syllabus": {"syllabus"},
            "assessment_uploads": {"assessment"},
        }[scope]
        raw_paths = [Path(source.file) for source in sources if source.type.value in allowed]
        if scope == "syllabus":
            raw_paths.extend(path for path in root.iterdir()
                             if path.is_file() and _DELETE_NAME.search(path.name))
        if scope == "assessment_uploads":
            assessment_dir = root / "assessments"
            if assessment_dir.exists():
                raw_paths.extend(path for path in assessment_dir.iterdir() if path.is_file())
        raw = iter(raw_paths)
    for path in raw:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            selected.add(resolved)
    return sorted(selected)


def _remove_course_uploads(user: str, course_id: str, scope: str,
                           committed_paths: list[str]) -> bool:
    root = _course_upload_root(user, course_id)
    if scope == "entire_course":
        try:
            if root.exists():
                shutil.rmtree(root)
            return False
        except OSError:
            return True
    paths = set(_course_scope_files(user, course_id, scope))
    # Store returns registered paths captured before the database transaction.
    for raw_path in committed_paths:
        try:
            path = Path(raw_path).resolve(strict=True)
            path.relative_to(root)
            paths.add(path)
        except (OSError, ValueError):
            continue
    failed = False
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed = True
    return failed


@app.get("/api/course-deletions/preview")
def course_deletion_preview() -> dict[str, Any]:
    user, store = current_user(), get_store()
    if user == "benchmark":
        raise HTTPException(403, "The benchmark corpus is protected from product deletion.")
    rows = []
    for course in store.courses(user):
        counts = store.course_deletion_counts(user, course.course_id)
        counts["materials"]["files"] = len(_course_scope_files(user, course.course_id, "materials"))
        counts["syllabus"]["files"] = len(_course_scope_files(user, course.course_id, "syllabus"))
        counts["assessment_uploads"]["files"] = len(_course_scope_files(user, course.course_id, "assessment_uploads"))
        counts["entire_course"]["files"] = len(_course_scope_files(user, course.course_id, "entire_course"))
        rows.append({"course_id": course.course_id, "code": course.code,
                     "title": course.title, "counts": counts})
    # The hash only detects duplicates while source/chunk rows still exist; the
    # extracted content is not retained after those rows are deleted.
    return {"courses": rows, "reupload_cache": False}


@app.post("/api/course-deletions")
def delete_selected_courses(req: CourseDeletionRequest) -> dict[str, Any]:
    user, store = current_user(), get_store()
    if user == "benchmark":
        raise HTTPException(403, "The benchmark corpus is protected from product deletion.")
    if not req.selected:
        raise HTTPException(422, "Select at least one course to delete.")
    ids = [item.course_id for item in req.selected]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "A course can only appear once in a deletion request.")
    if any(item.scope not in _DELETE_SCOPES for item in req.selected):
        raise HTTPException(422, "Choose a valid deletion scope for each course.")
    available = {course.course_id: course for course in store.courses(user)}
    if any(course_id not in available for course_id in ids):
        raise HTTPException(404, "One or more selected courses were not found.")
    selected_all = req.select_all and set(ids) == set(available)
    if req.select_all and not selected_all:
        raise HTTPException(422, "Select every listed course before using DELETE ALL.")
    if selected_all:
        expected = "DELETE ALL"
    elif len(ids) == 1:
        expected = available[ids[0]].code
    else:
        expected = "DELETE " + ", ".join(available[course_id].code for course_id in ids)
    if req.confirmation.strip().casefold() != expected.casefold():
        raise HTTPException(422, f"Type {expected} exactly to confirm.")

    removed = []
    failed = []
    file_cleanup_pending = False
    for item in req.selected:
        try:
            paths = _course_scope_files(user, item.course_id, item.scope)
            result = store.delete_course_scope(user, item.course_id, item.scope)
            file_cleanup_pending |= _remove_course_uploads(
                user, item.course_id, item.scope,
                result.get("files", []) + [str(path) for path in paths])
            removed.append({"course_id": item.course_id, "scope": item.scope})
        except Exception:
            # Earlier selected courses may already have committed their own
            # transaction. Report the per-course outcome rather than implying
            # the whole batch was atomic or hiding which course rolled back.
            failed.append({"course_id": item.course_id, "scope": item.scope})
            break
    invalidate_pipeline(user)
    return {"deleted": removed, "failed": failed,
            "file_cleanup_pending": file_cleanup_pending}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(WEB_DIR / "index.html"))

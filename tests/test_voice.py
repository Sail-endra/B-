from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from api import main
from api.config import settings
from api.store.sqlite_store import SQLiteStore
from api.voice.service import ElevenLabsService, VoiceServiceError


def run(awaitable):
    return asyncio.run(awaitable)


def test_transcription_uses_elevenlabs_multipart_and_hides_key():
    seen = {}

    async def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"text": "Explain marginal utility."})

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret", elevenlabs_stt_model="scribe_v2"),
        httpx.MockTransport(handler),
    )
    transcript = run(service.transcribe(b"webm-bytes", "question.webm", "audio/webm"))
    request = seen["request"]
    assert transcript == "Explain marginal utility."
    assert request.url.path == "/v1/speech-to-text"
    assert request.headers["xi-api-key"] == "server-secret"
    assert b"model_id\"\r\n\r\nscribe_v2" in request.content
    assert b"question.webm" in request.content and b"webm-bytes" in request.content


def test_synthesis_uses_configured_voice_speed_and_returns_audio():
    seen = {}

    async def handler(request):
        seen["request"] = request
        return httpx.Response(200, content=b"fake-mp3", headers={"content-type": "audio/mpeg"})

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret", elevenlabs_tts_model="eleven_multilingual_v2"),
        httpx.MockTransport(handler),
    )
    audio = run(service.synthesize("A grounded answer.", "voice-123", 0.8))
    request = seen["request"]
    assert audio == b"fake-mp3"
    assert request.url.path == "/v1/text-to-speech/voice-123"
    assert request.url.params["output_format"] == "mp3_44100_128"
    assert request.headers["xi-api-key"] == "server-secret"
    assert request.read() == b'{"text":"A grounded answer.","model_id":"eleven_multilingual_v2","voice_settings":{"speed":0.8}}'


def test_synthesis_speaks_math_naturally_before_sending_to_provider():
    seen = {}

    async def handler(request):
        seen["payload"] = request.read()
        return httpx.Response(200, content=b"fake-mp3")

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret"), httpx.MockTransport(handler)
    )
    run(service.synthesize(r"The elasticity is \(\epsilon = \frac{dQ}{dP}\).", "voice", 1.0))
    assert b"\\frac" not in seen["payload"]
    assert b"epsilon equals dQ divided by dP" in seen["payload"]


def test_missing_elevenlabs_key_fails_gracefully():
    service = ElevenLabsService(replace(settings, elevenlabs_api_key=None))
    with pytest.raises(VoiceServiceError) as error:
        run(service.transcribe(b"audio", "q.webm", "audio/webm"))
    assert error.value.status_code == 503
    assert error.value.code == "ELEVENLABS_NOT_CONFIGURED"
    assert "not configured" in error.value.detail


def test_key_id_used_as_api_key_has_specific_safe_diagnostic():
    async def handler(request):
        return httpx.Response(400, json={"detail": {
            "type": "authentication_error", "code": "invalid_api_key",
            "status": "api_key_id_used_as_api_key",
            "message": "provider text must not be forwarded", "request_id": "req-safe-123",
            "param": "api_key",
        }})

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret"), httpx.MockTransport(handler)
    )
    with pytest.raises(VoiceServiceError) as error:
        run(service.transcribe(b"audio", "q.webm", "audio/webm"))
    assert error.value.status_code == 502
    assert error.value.code == "ELEVENLABS_KEY_ID_USED_AS_KEY"
    assert error.value.request_id == "req-safe-123"
    assert "key ID, not an API key secret" in error.value.detail
    assert "provider text" not in error.value.detail
    assert "server-secret" not in error.value.detail


@pytest.mark.parametrize("provider_code,status,expected_status,expected_code", [
    ("invalid_audio", 400, 422, "AUDIO_INVALID"),
    ("invalid_audio_format", 400, 415, "AUDIO_FORMAT_UNSUPPORTED"),
    ("audio_too_short", 400, 422, "AUDIO_TOO_SHORT"),
    ("audio_too_long", 400, 413, "AUDIO_TOO_LONG"),
    ("insufficient_credits", 402, 503, "ELEVENLABS_INSUFFICIENT_CREDITS"),
])
def test_structured_provider_errors_map_to_safe_categories(provider_code, status, expected_status, expected_code):
    async def handler(request):
        return httpx.Response(status, json={"detail": {
            "type": "invalid_request", "code": provider_code,
            "message": "sensitive provider text", "request_id": "req-123",
        }})

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret"), httpx.MockTransport(handler)
    )
    with pytest.raises(VoiceServiceError) as error:
        run(service.transcribe(b"audio", "q.webm", "audio/webm"))
    assert error.value.status_code == expected_status
    assert error.value.code == expected_code
    assert error.value.request_id == "req-123"
    assert "sensitive provider text" not in error.value.detail


@pytest.mark.parametrize("status,expected", [(401, 502), (429, 503), (503, 502)])
def test_elevenlabs_http_errors_are_sanitized(status, expected):
    async def handler(request):
        return httpx.Response(status, text="secret provider diagnostic")

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret"), httpx.MockTransport(handler)
    )
    with pytest.raises(VoiceServiceError) as error:
        run(service.transcribe(b"audio", "q.webm", "audio/webm"))
    assert error.value.status_code == expected
    assert "secret" not in error.value.detail


def test_elevenlabs_timeout_is_a_friendly_gateway_timeout():
    async def handler(request):
        raise httpx.ReadTimeout("provider timeout", request=request)

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret"), httpx.MockTransport(handler)
    )
    with pytest.raises(VoiceServiceError) as error:
        run(service.transcribe(b"audio", "q.webm", "audio/webm"))
    assert error.value.status_code == 504


def test_tts_provider_failure_is_friendly_and_voice_list_is_sanitized():
    async def failing(request):
        return httpx.Response(429, text="account-specific provider details")

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret"), httpx.MockTransport(failing)
    )
    with pytest.raises(VoiceServiceError) as error:
        run(service.synthesize("An answer", "voice-id", 1.0))
    assert error.value.status_code == 503
    assert "rate-limiting" in error.value.detail
    assert "account-specific" not in error.value.detail

    async def voices(request):
        return httpx.Response(200, json={"voices": [
            {"voice_id": "safe-id", "name": "Reader", "preview_url": "private-url", "sharing": {"x": 1}},
            {"name": "Malformed"},
        ]})

    service = ElevenLabsService(
        replace(settings, elevenlabs_api_key="server-secret"), httpx.MockTransport(voices)
    )
    assert run(service.voices()) == [{"voice_id": "safe-id", "name": "Reader"}]


@pytest.fixture
def voice_store(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "voice.sqlite")
    monkeypatch.setattr(main, "get_store", lambda: store)
    yield store
    store.close()


class FakeVoiceService:
    has_key = True

    async def transcribe(self, audio, filename, content_type):
        self.transcribed = (audio, filename, content_type)
        return "Explain marginal utility."

    async def synthesize(self, text, voice_id, speed):
        self.synthesized = (text, voice_id, speed)
        return b"fake-audio"

    async def voices(self):
        return [{"voice_id": "valid-voice", "name": "Study voice"}]


def uploaded(data=b"recording", content_type="audio/webm", filename="clip.webm"):
    return UploadFile(
        filename=filename, file=io.BytesIO(data),
        headers=Headers({"content-type": content_type}),
    )


def test_transcription_requires_voice_consent_and_makes_no_provider_call(voice_store, monkeypatch):
    fake = FakeVoiceService()
    monkeypatch.setattr(main, "voice_service", fake)
    with pytest.raises(HTTPException) as error:
        run(main.voice_transcribe(uploaded()))
    assert error.value.status_code == 403
    assert not hasattr(fake, "transcribed")


def test_transcription_validates_format_size_and_returns_text(voice_store, monkeypatch):
    voice_store.set_prefs("local-user", voice_consent=True)
    fake = FakeVoiceService()
    monkeypatch.setattr(main, "voice_service", fake)
    result = run(main.voice_transcribe(uploaded(data=b"\x1aE\xdf\xa3recording")))
    assert result == {"text": "Explain marginal utility.", "provider": "elevenlabs"}
    assert fake.transcribed == (b"\x1aE\xdf\xa3recording", "clip.webm", "audio/webm")

    with pytest.raises(HTTPException) as unsupported:
        run(main.voice_transcribe(uploaded(content_type="application/octet-stream")))
    assert unsupported.value.status_code == 415

    with pytest.raises(HTTPException) as mismatched:
        run(main.voice_transcribe(uploaded(data=b"not audio")))
    assert mismatched.value.status_code == 415

    with pytest.raises(HTTPException) as oversized:
        run(main.voice_transcribe(uploaded(data=b"a" * (main.VOICE_MAX_BYTES + 1))))
    assert oversized.value.status_code == 413


def test_transcription_returns_structured_provider_diagnostic(voice_store, monkeypatch):
    voice_store.set_prefs("local-user", voice_consent=True)

    class InvalidKeyVoiceService(FakeVoiceService):
        async def transcribe(self, audio, filename, content_type):
            raise VoiceServiceError(
                502, "Configured value is a key ID, not a secret.",
                "ELEVENLABS_KEY_ID_USED_AS_KEY", "req-123",
            )

    monkeypatch.setattr(main, "voice_service", InvalidKeyVoiceService())
    with pytest.raises(HTTPException) as error:
        run(main.voice_transcribe(uploaded(data=b"\x1aE\xdf\xa3recording"), duration_ms=1200))
    assert error.value.status_code == 502
    assert error.value.detail == {
        "code": "ELEVENLABS_KEY_ID_USED_AS_KEY",
        "message": "Configured value is a key ID, not a secret.",
        "request_id": "req-123",
    }


def test_synthesis_requires_consent_and_uses_saved_voice_settings(voice_store, monkeypatch):
    fake = FakeVoiceService()
    monkeypatch.setattr(main, "voice_service", fake)
    payload = main.VoiceSynthesizeRequest(text="A concise answer.")
    with pytest.raises(HTTPException) as error:
        run(main.voice_synthesize(payload))
    assert error.value.status_code == 403

    voice_store.set_prefs("local-user", voice_consent=True, selected_voice_id="valid-voice", speech_speed=1.2)
    response = run(main.voice_synthesize(payload))
    assert response.body == b"fake-audio"
    assert response.media_type == "audio/mpeg"
    assert fake.synthesized == ("A concise answer.", "valid-voice", 1.2)


def test_voice_preference_persists_and_rejects_unknown_voice(voice_store, monkeypatch):
    voice_store.set_prefs("local-user", voice_consent=True)
    fake = FakeVoiceService()
    monkeypatch.setattr(main, "voice_service", fake)
    result = run(main.voice_preferences(main.VoicePreferencesRequest(
        voice_only_mode=True, auto_submit_voice=False, selected_voice_id="valid-voice", speech_speed=0.8
    )))
    assert result["voice_only_mode"] is True
    assert result["auto_submit_voice"] is False
    assert result["selected_voice_id"] == "valid-voice"
    assert result["speech_speed"] == 0.8
    assert voice_store.get_prefs("local-user")["voice_only_mode"] is True

    with pytest.raises(HTTPException) as error:
        run(main.voice_preferences(main.VoicePreferencesRequest(selected_voice_id="arbitrary")))
    assert error.value.status_code == 400


def test_revoking_voice_consent_disables_voice_only_mode(voice_store):
    voice_store.set_prefs("local-user", voice_consent=True, voice_only_mode=True)
    result = main.set_consent(main.ConsentRequest(voice_consent=False))
    assert result["voice_consent"] is False
    assert voice_store.get_prefs("local-user")["voice_only_mode"] is False


def test_voice_frontend_is_accessible_and_reuses_ask_pipeline():
    root = Path(__file__).parents[1]
    html = (root / "web/index.html").read_text()
    script = (root / "web/js/app.js").read_text()
    assert 'id="voice-record"' in html and 'aria-label="Start voice input"' in html
    assert 'aria-live="polite"' in html and 'id="voice-only-exit"' in html
    assert 'form.append("file", blob' in script
    assert "MediaRecorder.isTypeSupported" in script
    assert "size_bytes: blob.size" in script and "duration_ms: durationMs" in script
    assert 'form.append("duration_ms", String(durationMs))' in script
    assert '"ELEVENLABS_KEY_ID_USED_AS_KEY"' in script
    assert '$("askform").requestSubmit()' in script
    assert 'json("/api/ask"' in script
    assert "window.speechSynthesis" not in script
    assert "ELEVENLABS_API_KEY" not in script

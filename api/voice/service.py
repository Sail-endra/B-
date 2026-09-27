"""Small ElevenLabs client. Audio is forwarded in memory and never persisted."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from ..math_text import math_to_speech
from ..config import Settings, settings

API_BASE = "https://api.elevenlabs.io"
logger = logging.getLogger(__name__)


@dataclass
class VoiceServiceError(Exception):
    status_code: int
    detail: str
    code: str = "VOICE_PROVIDER_ERROR"
    request_id: str | None = None


class ElevenLabsService:
    def __init__(self, config: Settings = settings, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.config.elevenlabs_api_key and self.config.elevenlabs_voice_id)

    @property
    def has_key(self) -> bool:
        return bool(self.config.elevenlabs_api_key)

    def _require_key(self) -> str:
        key = self.config.elevenlabs_api_key
        if not key:
            raise VoiceServiceError(
                503, "Voice features are unavailable: ElevenLabs is not configured.",
                "ELEVENLABS_NOT_CONFIGURED",
            )
        return key

    @staticmethod
    def _provider_error(
        response: httpx.Response,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None]:
        """Extract only safe identifiers from ElevenLabs' structured error."""
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        detail = payload.get("detail", payload) if isinstance(payload, dict) else {}
        if isinstance(detail, list):
            detail = detail[0] if detail else {}
        detail = detail if isinstance(detail, dict) else {}
        error = detail.get("error") if isinstance(detail.get("error"), dict) else detail
        meta = error.get("meta") if isinstance(error.get("meta"), dict) else {}
        def safe_token(value: Any) -> str | None:
            token = re.sub(r"[^A-Za-z0-9_.-]", "", str(value or ""))[:120]
            return token or None

        return (
            safe_token(error.get("type")),
            safe_token(error.get("code")),
            safe_token(error.get("status")),
            safe_token(error.get("request_id") or meta.get("request_id") or response.headers.get("request-id")),
            safe_token(error.get("param")),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        key = self._require_key()
        headers = dict(kwargs.pop("headers", {}))
        headers["xi-api-key"] = key
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(40.0, connect=8.0), transport=self.transport
            ) as client:
                response = await client.request(method, API_BASE + path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise VoiceServiceError(504, "ElevenLabs took too long to respond. Please try again.", "ELEVENLABS_TIMEOUT") from exc
        except httpx.RequestError as exc:
            raise VoiceServiceError(502, "ElevenLabs could not be reached. You can still type normally.", "ELEVENLABS_UNREACHABLE") from exc

        if response.status_code >= 400:
            provider_type, provider_code, provider_status, request_id, provider_param = self._provider_error(response)
            logger.warning(
                "ElevenLabs request failed status=%s type=%s code=%s provider_status=%s param=%s request_id=%s",
                response.status_code, provider_type, provider_code, provider_status, provider_param, request_id,
            )
            safe_code = (provider_code or "").lower()
            legacy_status = (provider_status or "").lower()
            if safe_code == "invalid_api_key" and legacy_status == "api_key_id_used_as_api_key":
                raise VoiceServiceError(
                    502,
                    "The configured ElevenLabs value is a key ID, not an API key secret. Replace ELEVENLABS_API_KEY with the secret shown when you create or rotate an API key.",
                    "ELEVENLABS_KEY_ID_USED_AS_KEY", request_id,
                )
            if response.status_code in {401, 403} or safe_code in {"invalid_api_key", "authorization_error"}:
                raise VoiceServiceError(
                    502, "ElevenLabs rejected the server API key or its permissions. Check the server configuration.",
                    "ELEVENLABS_AUTHENTICATION_FAILED", request_id,
                )
            if response.status_code == 429 or safe_code == "rate_limit_exceeded":
                raise VoiceServiceError(
                    503, "ElevenLabs is rate-limiting requests. Please wait and try again.",
                    "ELEVENLABS_RATE_LIMITED", request_id,
                )
            if response.status_code == 402 or safe_code == "insufficient_credits":
                raise VoiceServiceError(
                    503, "The ElevenLabs account has insufficient credits for this request.",
                    "ELEVENLABS_INSUFFICIENT_CREDITS", request_id,
                )
            audio_errors = {
                "invalid_audio": (422, "AUDIO_INVALID", "ElevenLabs could not read this recording. Record a new clip and try again."),
                "invalid_audio_format": (415, "AUDIO_FORMAT_UNSUPPORTED", "ElevenLabs does not support this recording format. Try another browser or type your question."),
                "audio_too_short": (422, "AUDIO_TOO_SHORT", "The recording is too short to transcribe. Speak for a little longer and try again."),
                "audio_too_long": (413, "AUDIO_TOO_LONG", "The recording is too long. Record a shorter clip and try again."),
            }
            if safe_code in audio_errors:
                status, code, message = audio_errors[safe_code]
                raise VoiceServiceError(status, message, code, request_id)
            if response.status_code >= 500:
                raise VoiceServiceError(
                    502, "ElevenLabs is temporarily unavailable. Please try again.",
                    "ELEVENLABS_TEMPORARY_FAILURE", request_id,
                )
            raise VoiceServiceError(
                502, "ElevenLabs could not process this voice request.",
                "ELEVENLABS_REQUEST_REJECTED", request_id,
            )
        return response

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        if not audio:
            raise VoiceServiceError(400, "The recording is empty. Record a question and try again.", "AUDIO_EMPTY")
        response = await self._request(
            "POST", "/v1/speech-to-text",
            data={"model_id": self.config.elevenlabs_stt_model},
            files={"file": (filename, audio, content_type)},
        )
        try:
            text = str(response.json().get("text", "")).strip()
        except (ValueError, AttributeError) as exc:
            raise VoiceServiceError(502, "ElevenLabs returned an unreadable transcription. Please try again.", "TRANSCRIPTION_RESPONSE_INVALID") from exc
        if not text:
            raise VoiceServiceError(422, "No speech was detected. Try recording a little more clearly.", "NO_SPEECH_DETECTED")
        return text

    async def synthesize(self, text: str, voice_id: str, speed: float) -> bytes:
        if not self.config.elevenlabs_api_key:
            self._require_key()
        if not voice_id:
            raise VoiceServiceError(503, "Choose a default ElevenLabs voice in the server configuration.", "ELEVENLABS_VOICE_NOT_CONFIGURED")
        response = await self._request(
            "POST", f"/v1/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            json={"text": math_to_speech(text), "model_id": self.config.elevenlabs_tts_model,
                  "voice_settings": {"speed": speed}},
        )
        if not response.content:
            raise VoiceServiceError(502, "ElevenLabs returned empty audio. Please try again.", "TTS_RESPONSE_EMPTY")
        return response.content

    async def voices(self) -> list[dict[str, str]]:
        response = await self._request(
            "GET", "/v2/voices", params={"page_size": 100, "include_total_count": "false"}
        )
        try:
            rows = response.json().get("voices", [])
        except (ValueError, AttributeError) as exc:
            raise VoiceServiceError(502, "ElevenLabs returned an unreadable voice list.", "VOICE_LIST_RESPONSE_INVALID") from exc
        return [
            {"voice_id": str(row["voice_id"]), "name": str(row.get("name") or "Voice")}
            for row in rows if isinstance(row, dict) and row.get("voice_id")
        ]

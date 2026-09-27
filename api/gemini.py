"""Free-tier Gemini client (Google AI Studio) — benchmark path only.

Google's free tier uses submitted inputs to improve their models, so this client
must never see cohort or product-user data. It is wired only into the benchmark
(`get_llm(allow_gemini=True)`), never into `/api/ask`, until the privacy boundary
is built.

Two hard rules from the brief, implemented here:
  * Exponential backoff on HTTP 429, starting at 1s (the free daily quota is real).
  * Every response cached to disk, keyed by a hash of (model, prompt, system), so
    re-runs are free and deterministic.

Do NOT enable billing on the key: enabling it deletes the free tier and every
call becomes billable from the first token.

The model string is read from GEMINI_MODEL. AI Studio's current free Flash-Lite
id changes over time and must be confirmed there rather than assumed; the default
below is a placeholder the operator should verify.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .config import ROOT, settings

_CACHE_DIR = ROOT / "data" / "gemini_cache"
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_MAX_RETRIES = 6
_BACKOFF_START = 1.0


class GeminiError(RuntimeError):
    pass


class GeminiClient:
    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model or settings.gemini_model
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def backend(self) -> str:
        return f"gemini:{self.model}"

    def _cache_path(self, key: str) -> Path:
        return _CACHE_DIR / f"{key}.json"

    def _key(self, prompt: str, system: str, max_tokens: int, temperature: float,
             response_schema: Optional[dict[str, Any]] = None) -> str:
        h = hashlib.sha256()
        h.update(f"{self.model}\x00{temperature}\x00{max_tokens}\x00{system}\x00{prompt}\x00".encode())
        if response_schema:
            h.update(json.dumps(response_schema, sort_keys=True).encode())
        return h.hexdigest()

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 1500,
        temperature: float = 0.0,
        response_schema: Optional[dict[str, Any]] = None,
        cache: bool = True,
        **_: Any,
    ) -> str:
        key = self._key(prompt, system, max_tokens, temperature, response_schema)
        cached = self._cache_path(key) if cache else None
        if cached and cached.exists():
            return json.loads(cached.read_text())["text"]

        text = self._call(prompt, system, max_tokens, temperature, response_schema)
        if cached:
            cached.write_text(json.dumps({"text": text}))
        return text

    def _call(self, prompt: str, system: str, max_tokens: int, temperature: float,
              response_schema: Optional[dict[str, Any]] = None) -> str:
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if response_schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseSchema"] = response_schema
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        url = _ENDPOINT.format(model=self.model)
        data = json.dumps(body).encode()
        backoff = _BACKOFF_START

        for attempt in range(_MAX_RETRIES):
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode())
                return _extract_text(payload)
            except urllib.error.HTTPError as exc:
                # Exponential backoff on rate limit; surface anything else.
                if exc.code == 429 and attempt < _MAX_RETRIES - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                detail = exc.read().decode(errors="replace")[:200]
                raise GeminiError(f"Gemini HTTP {exc.code}: {detail}") from exc
            except Exception as exc:  # noqa: BLE001
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise GeminiError(f"Gemini call failed: {exc}") from exc
        raise GeminiError("Gemini call failed after retries")


def _extract_text(payload: dict[str, Any]) -> str:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError):
        # A safety block or empty candidate: return empty so callers fall back.
        return ""

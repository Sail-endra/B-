"""Claude client wrapper, plus a deterministic offline stub.

The stub is not a mock that returns canned strings. It performs genuinely
extractive answering over the retrieved passages: it selects the sentences that
best cover the question and returns them with their citations. That keeps the
entire system -- agent loop, groundedness judge, refusal calibration -- runnable
and *measurable* with no key, and it is honest about what it is: a floor, not a
model. Every eval report records which backend produced it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .config import settings

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _extract_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if (m := _JSON_BLOCK.search(raw)) is not None:
        raw = m.group(1).strip()
    start = raw.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in response: {raw[:200]!r}")
    # Decode the FIRST complete object from `start` and ignore anything after it.
    # Free-tier models sometimes append prose, a second object, or a repeated
    # answer; spanning first-"{" to last-"}" then trips json.loads with "Extra
    # data". raw_decode stops at the first object's close instead.
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError:
        # Last resort: the widest brace span (handles a single object with a
        # trailing stray brace fragment before the real close).
        end = raw.rfind("}")
        if end <= start:
            raise
        obj = json.loads(raw[start : end + 1])
    return obj


class LLM:
    """Thin wrapper. Every call has a timeout and one retry; a typed failure
    propagates so the caller can render a card-level error rather than a stack."""

    supports_tools = True

    def __init__(self, api_key: Optional[str] = None, label: Optional[str] = None) -> None:
        key = api_key or settings.anthropic_api_key
        self.available = bool(key) and not settings.offline
        self._label = label
        self._client = None
        if self.available:
            from anthropic import Anthropic

            kwargs: dict[str, Any] = {"api_key": key}
            if settings.anthropic_base_url:
                kwargs["base_url"] = settings.anthropic_base_url
            self._client = Anthropic(**kwargs)

    @property
    def backend(self) -> str:
        return settings.answer_model if self.available else "deterministic-stub"

    @property
    def label(self) -> str:
        return self._label or (f"Claude ({settings.answer_model})" if self.available
                               else "Extractive (no AI)")

    # -- core ------------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model: Optional[str] = None,
        max_tokens: int = 1500,
        tools: Optional[Sequence[dict[str, Any]]] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        if not self.available:
            return LLMResponse(text="", stop_reason="unavailable")

        payload: dict[str, Any] = {
            "model": model or settings.answer_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages or [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = list(tools)

        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = self._client.messages.create(**payload)
                text_parts, calls = [], []
                for block in resp.content:
                    if block.type == "text":
                        text_parts.append(block.text)
                    elif block.type == "tool_use":
                        calls.append(ToolCall(block.id, block.name, dict(block.input)))
                return LLMResponse(
                    text="".join(text_parts),
                    tool_calls=calls,
                    stop_reason=resp.stop_reason or "end_turn",
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - retried, then surfaced
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM call failed after 2 attempts: {last}")

    def text(self, prompt: str, **kwargs: Any) -> str:
        return self.complete(prompt, **kwargs).text

    def json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("model", settings.cheap_model)
        raw = self.complete(prompt, **kwargs).text
        if not raw:
            raise RuntimeError("empty response from LLM")
        return _extract_json(raw)


# ---------------------------------------------------------------------------
# Offline extractive answering
# ---------------------------------------------------------------------------

_SENT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")
_STOP = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "is",
    "are", "was", "were", "be", "it", "its", "this", "that", "these", "those",
    "as", "by", "with", "from", "what", "which", "does", "do", "how", "why",
    "when", "about", "according", "explain", "describe",
}


def _terms(text: str) -> set[str]:
    return {t for t in _WORD.findall((text or "").lower()) if t not in _STOP and len(t) > 1}


def extractive_answer(question: str, passages: Sequence[tuple[str, str]], max_sentences: int = 4):
    """Select the sentences that best cover the question.

    `passages` is a sequence of (citation_label, text) **in rank order**. Returns
    (answer, used_labels). Greedy maximum-coverage: each pick is the sentence
    adding the most uncovered question terms, which avoids returning four
    paraphrases of the same sentence.

    Two things keep the selection from drifting off-topic:

    * **Rank decay.** A sentence from a lower-ranked passage must be clearly
      better to be chosen. Without it, a passage ranked 4th can contribute a
      sentence that happens to share a common word -- an ethnography passage
      about "practice" landing in an answer about Fibonacci heaps in practice.
    * **Course locking.** Once the first sentence is chosen, later sentences must
      come from the same course. Splicing two courses into one answer is never
      right, and the citations make the splice look deliberate.
    """
    q_terms = _terms(question)
    if not q_terms:
        return "", []

    candidates: list[tuple[str, str, set[str], float]] = []
    for rank, (label, text) in enumerate(passages):
        weight = 1.0 / (1.0 + 0.6 * rank)
        for sentence in _SENT.split(text or ""):
            sentence = sentence.strip()
            if 20 < len(sentence) < 400:
                candidates.append((label, sentence, _terms(sentence), weight))
    if not candidates:
        return "", []

    def course_of(label: str) -> str:
        return label[1:].split(",", 1)[0].strip() if label.startswith("[") else label

    chosen: list[tuple[str, str]] = []
    covered: set[str] = set()
    locked_course: Optional[str] = None

    for _ in range(max_sentences):
        best, best_gain = None, 0.0
        for label, sentence, terms, weight in candidates:
            if locked_course is not None and course_of(label) != locked_course:
                continue
            if any(sentence == s for _, s in chosen):
                continue
            overlap = terms & q_terms
            gain = (len(overlap - covered) + 0.25 * len(overlap)) * weight
            if gain > best_gain:
                best, best_gain = (label, sentence, terms), gain
        if best is None or best_gain <= 0:
            break
        chosen.append((best[0], best[1]))
        covered |= best[2] & q_terms
        if locked_course is None:
            locked_course = course_of(best[0])

    if not chosen:
        return "", []
    answer = " ".join(f"{sentence} {label}" for label, sentence in chosen)
    return answer, list(dict.fromkeys(label for label, _ in chosen))


class GeminiLLM:
    supports_tools = False
    """Adapter exposing the LLM surface (complete/text/json) over free-tier
    Gemini. Text-only: no tool-use, so the agent loop treats it as a plain
    answerer. Benchmark path only -- never wired into product requests, because
    free-tier inputs are used to train Google's models.
    """

    def __init__(self, api_key: Optional[str] = None, label: Optional[str] = None) -> None:
        from .gemini import GeminiClient

        self._client = GeminiClient(api_key or settings.gemini_api_key)
        self.available = True
        self._label = label

    @property
    def backend(self) -> str:
        return self._client.backend

    @property
    def label(self) -> str:
        return self._label or f"gemini:{settings.gemini_model} (free-tier, benchmark-only)"

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 1500,
                 temperature: float = 0.0, tools=None, messages=None,
                 model=None, **_: Any) -> LLMResponse:
        # Flatten a messages transcript to a single prompt; Gemini here is a
        # plain answerer, not a tool-runner.
        if messages:
            prompt = "\n\n".join(
                m["content"] if isinstance(m.get("content"), str) else str(m.get("content"))
                for m in messages
            )
        text = self._client.complete(prompt, system=system, max_tokens=max_tokens,
                                     temperature=temperature)
        return LLMResponse(text=text, stop_reason="end_turn")

    def text(self, prompt: str, **kwargs: Any) -> str:
        return self.complete(prompt, **kwargs).text

    def json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        raw = self.complete(prompt, **kwargs).text
        if not raw:
            raise RuntimeError("empty response from Gemini")
        return _extract_json(raw)


_llm: Optional[Any] = None


def get_llm(allow_gemini: bool = False) -> Any:
    """The answerer. Anthropic when keyed; else free-tier Gemini ONLY when the
    caller is the benchmark (`allow_gemini=True`) and a key is set; else the
    deterministic stub. The product path never passes allow_gemini, so cohort and
    product data never reach Gemini's free tier."""
    global _llm
    if _llm is not None:
        return _llm
    if settings.has_anthropic:
        _llm = LLM()
    elif allow_gemini and settings.gemini_api_key and not settings.offline:
        _llm = GeminiLLM()
    else:
        _llm = LLM()   # deterministic stub (available == False)
    return _llm


class OllamaLLM:
    """Local model via Ollama (http://localhost:11434). Fully private -- nothing
    leaves the machine -- so it needs no consent. Text-only (no tool loop)."""

    supports_tools = False

    def __init__(self, model: str, label: Optional[str] = None) -> None:
        self.model = model
        self.available = True
        self._label = label

    @property
    def label(self) -> str:
        return self._label or f"Ollama {self.model} (local, private)"

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 1500,
                 temperature: float = 0.0, tools=None, messages=None, **_: Any) -> LLMResponse:
        import json as _json
        import urllib.request

        if messages:
            prompt = "\n\n".join(
                m["content"] if isinstance(m.get("content"), str) else str(m.get("content"))
                for m in messages)
        body = _json.dumps({
            "model": self.model, "prompt": prompt, "system": system, "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }).encode()
        req = urllib.request.Request(
            f"{settings.ollama_url}/api/generate", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = _json.loads(r.read().decode())
        return LLMResponse(text=data.get("response", ""), stop_reason="end_turn")

    def text(self, prompt: str, **kwargs: Any) -> str:
        return self.complete(prompt, **kwargs).text

    def json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        raw = self.complete(prompt, **kwargs).text
        if not raw:
            raise RuntimeError("empty response from Ollama")
        return _extract_json(raw)


def resolve_product_llm(prefs: Optional[dict[str, Any]] = None) -> Any:
    """The PRODUCT answerer for ONE user, chosen from that user's preferences.

    Priority, each enforced in code so no data reaches a training-eligible free
    model without opt-in:
      1. the user's OWN key (Anthropic or Gemini) -- API inputs are not used for
         training, so this needs no consent;
      2. a local Ollama model, if configured -- fully private;
      3. the server's Anthropic key, if the operator set one;
      4. free-tier Gemini ONLY if the user set `llm_consent`;
      5. otherwise the extractive stub -- grounded quotes, no generation, nothing
         sent anywhere.
    """
    prefs = prefs or {}
    if settings.offline:
        return LLM(label="Extractive (offline)")
    if prefs.get("anthropic_api_key"):
        return LLM(api_key=prefs["anthropic_api_key"], label="Claude (your key)")
    if prefs.get("gemini_api_key"):
        return GeminiLLM(api_key=prefs["gemini_api_key"], label="Gemini (your key)")
    if settings.ollama_model and _ollama_reachable():
        return OllamaLLM(settings.ollama_model, label=f"Ollama {settings.ollama_model} (local)")
    if settings.has_anthropic:
        return LLM(label=f"Claude ({settings.answer_model})")
    if prefs.get("llm_consent") and settings.gemini_api_key:
        return GeminiLLM(label=f"Gemini {settings.gemini_model} (free-tier, consented)")
    return LLM(label="Extractive (no AI)")  # unavailable stub


def _ollama_reachable() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{settings.ollama_url}/api/tags", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def get_product_llm() -> Any:
    """Back-compat no-user resolver (data-evidence path). Uses the global consent
    flag; the per-user path is `resolve_product_llm(prefs)`."""
    return resolve_product_llm(
        {"llm_consent": settings.product_ai_consent} if settings.product_generation_allowed
        else {})


def reset_llm() -> None:
    global _llm
    _llm = None

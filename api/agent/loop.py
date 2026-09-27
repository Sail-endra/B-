"""The agent loop.

Single-shot retrieval answers lookup questions. A loop answers compositional ones,
which is the whole difference between "define the Phillips curve" and "does the
data still support what chapter 11 claims".

Independent tool calls in one turn still run concurrently via `asyncio.gather`.
With the data tools removed there is currently one tool, so that path is mostly
dormant -- it is kept because it costs ten lines and re-earns its value the moment
a second tool returns.

Grounding binds throughout: the model may reason with tool output, but may only
assert what a citation supports. When retrieval finds nothing, the answer is the
NOT_IN_MATERIALS sentinel, not a paraphrase of general knowledge.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..config import settings
from ..llm import extractive_answer, get_llm, resolve_product_llm
from ..models import AgentAnswer, AgentStep, Citation
from ..retrieval.pipeline import RetrievalPipeline
from ..store import get_store
from .budget import BudgetTracker
from .tools import ToolBox, ToolResult, tool_schemas

NOT_IN_MATERIALS = "NOT_IN_MATERIALS"

SYSTEM_PROMPT = f"""You are a study assistant that answers strictly from the student's own course materials.

Rules, in priority order:

1. Every factual claim about a course must be supported by a passage returned by
   `search_textbook`, and must carry that passage's citation label in the form
   [COURSE, Ch N, p. X]. If the materials do not support a claim, do not make it.

2. If the materials do not contain the answer, reply with exactly {NOT_IN_MATERIALS}
   followed by one sentence saying what is missing. Do not answer from general
   knowledge. Do not guess. A refusal is a correct answer.

3. When a claim in the materials is conditional -- "in the short run", "holding
   other things equal", "for a competitive market" -- state the conditions. A
   conditional claim reported without them is a different, stronger claim than the
   one the materials made.

4. When the data tools are available, they are search-first: pass a CONCEPT to
   `find_data_series`, never a series id from memory, then fetch an id it
   returned. Numbers from those tools are data -- label them as data and cite the
   series id. Never attribute a data value to the course materials.

5. If the data tools are not in your toolset, this course has no real-world data
   link. Answer from the materials alone and do not mention time series.

Be concise. Prefer the student's own wording from the passages over paraphrase."""


class Agent:
    def __init__(
        self,
        pipeline: Optional[RetrievalPipeline] = None,
        user_id: str = settings.default_user_id,
        llm: Any = None,
    ):
        self.store = get_store()
        self.pipeline = pipeline or RetrievalPipeline(self.store, user_id)
        self.user_id = user_id
        # The answerer. Injected explicitly by the benchmark (which may use Gemini);
        # for the product it defaults to get_product_llm(), which enforces the
        # privacy boundary. None here means "resolve the product LLM at ask time".
        self._llm = llm
        # Passages from the last retrieval, so the explanation layer can gloss the
        # real source text rather than re-retrieving.
        self._last_passages: list[dict] = []

    # -- public ------------------------------------------------------------

    def ask(self, question: str, course: Optional[str] = None,
            depth: str = "concise") -> AgentAnswer:
        return asyncio.run(self.ask_async(question, course, depth=depth))

    async def ask_async(self, question: str, course: Optional[str] = None,
                        depth: str = "concise") -> AgentAnswer:
        started = time.monotonic()
        # Do not let a later question inherit the previous question's passages.
        self._last_passages = []
        tracker = BudgetTracker()
        course_row = self.store.course(self.user_id, course) if course else None
        has_data_link = bool(course_row and course_row.has_data_link)
        toolbox = ToolBox(self.pipeline, course_hint=course, has_data_link=has_data_link)
        steps: list[AgentStep] = []
        citations: list[Citation] = []

        llm = (self._llm if self._llm is not None
               else resolve_product_llm(self.store.get_prefs(self.user_id)))
        tool_model_answered = False
        if llm.available and getattr(llm, "supports_tools", False):
            # Tool-using model (Claude): retrieve via the agent loop.
            answer, truncated = await self._run_llm_loop(
                question, llm, toolbox, tracker, steps, citations, has_data_link
            )
            tool_model_answered = True
        else:
            # No model, or a text-only model (Gemini): retrieve deterministically
            # here, then -- if a model is available -- the explanation layer below
            # glosses the retrieved passages. This keeps generation grounded: the
            # model never answers before retrieval and only sees the passages.
            answer, truncated = self._run_offline(question, toolbox, tracker, steps, citations)

        # Explanation layer, on top of the verbatim answer. Only generates when a
        # model is available and retrieval actually succeeded; otherwise the
        # grounded extractive answer stands unchanged. Never runs on a refusal.
        explanation, explained = "", False
        refused = answer.strip().startswith(NOT_IN_MATERIALS) or not answer.strip()
        if not refused and llm.available and self._last_passages:
            from .explain import explain as _explain

            explanation, explained = _explain(question, self._last_passages, depth, llm)

        latency_ms = int((time.monotonic() - started) * 1000)

        self._log(question, course, tracker, refused, latency_ms, citations)
        self._record_engagement(citations)

        # The backend label reflects what actually produced the shown content, so
        # an extractive fallback can never look like a generated answer.
        if refused:
            backend = "— (refused before generation)"
        elif tool_model_answered or explained:
            backend = getattr(llm, "label", "model")
        else:
            backend = "Extractive (no AI — grounded quotes)"

        return AgentAnswer(
            question=question,
            answer=answer,
            citations=_dedupe(citations),
            steps=steps,
            refused=refused,
            latency_ms=latency_ms,
            truncated=truncated,
            explanation=explanation,
            depth=depth,
            explained=explained,
            backend=backend,
        )

    # -- LLM path -----------------------------------------------------------

    async def _run_llm_loop(
        self,
        question: str,
        llm: Any,
        toolbox: ToolBox,
        tracker: BudgetTracker,
        steps: list[AgentStep],
        citations: list[Citation],
        has_data_link: bool = False,
    ) -> tuple[str, bool]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        final_text = ""
        truncated = False

        while True:
            if (stop := tracker.check()) is not None:
                truncated = True
                steps.append(AgentStep(len(steps), "error", "budget", stop))
                break

            step_started = time.monotonic()
            response = await asyncio.to_thread(
                llm.complete,
                "",
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tool_schemas(has_data_link),
                max_tokens=1600,
            )
            tracker.record_step()

            if response.text:
                final_text = response.text
                steps.append(
                    AgentStep(
                        len(steps),
                        "text",
                        "answer",
                        response.text[:200],
                        elapsed_ms=int((time.monotonic() - step_started) * 1000),
                    )
                )
            if not response.wants_tools:
                break

            # Independent calls in one turn run concurrently.
            assistant_content: list[dict[str, Any]] = []
            if response.text:
                assistant_content.append({"type": "text", "text": response.text})
            for call in response.tool_calls:
                assistant_content.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                )
            messages.append({"role": "assistant", "content": assistant_content})

            for call in response.tool_calls:
                steps.append(
                    AgentStep(len(steps), "tool_call", call.name, _describe(call.arguments))
                )

            results = await asyncio.gather(
                *[
                    asyncio.to_thread(self._dispatch, toolbox, call.name, call.arguments)
                    for call in response.tool_calls
                ]
            )

            tool_content: list[dict[str, Any]] = []
            for call, result in zip(response.tool_calls, results):
                if call.name == "search_textbook" and result.ok:
                    self._last_passages = result.payload.get("passages", [])
                body = tracker.truncate_to_budget(result.to_json())
                tracker.record_tool(call.name, len(body))
                citations.extend(result.citations)
                steps.append(
                    AgentStep(
                        len(steps),
                        "tool_result",
                        call.name,
                        result.summary,
                        elapsed_ms=int((time.monotonic() - step_started) * 1000),
                    )
                )
                tool_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": body,
                        "is_error": not result.ok,
                    }
                )
            messages.append({"role": "user", "content": tool_content})

        return final_text, truncated

    # -- offline path --------------------------------------------------------

    def _run_offline(
        self,
        question: str,
        toolbox: ToolBox,
        tracker: BudgetTracker,
        steps: list[AgentStep],
        citations: list[Citation],
    ) -> tuple[str, bool]:
        """Deterministic policy used when no model is configured.

        Genuinely extractive rather than canned: it retrieves, and composes an
        answer from the sentences that best cover the question, with citations.
        That keeps groundedness and refusal measurable with no key -- it is a floor
        for those metrics, and is reported as such.
        """
        steps.append(AgentStep(len(steps), "tool_call", "search_textbook", question[:80]))
        result = toolbox.search_textbook({"query": question})
        tracker.record_step()
        tracker.record_tool("search_textbook", len(result.to_json()))
        steps.append(AgentStep(len(steps), "tool_result", "search_textbook", result.summary))

        if not result.ok:
            return (
                f"{NOT_IN_MATERIALS} Nothing in the loaded course materials covers this "
                "question.",
                False,
            )

        citations.extend(result.citations)
        self._last_passages = result.payload.get("passages", [])
        # Extract from the child snippet, not the parent context: the citation
        # describes the child's page range, so quoting the parent would attribute
        # a sentence to a page it did not come from.
        passages = [
            (p["citation"], p["snippet"]) for p in result.payload.get("passages", [])
        ]
        answer, used = extractive_answer(question, passages)
        if not answer:
            return (
                f"{NOT_IN_MATERIALS} Passages were retrieved but none of them "
                "addresses the question directly.",
                False,
            )
        keep = set(used)
        citations[:] = [c for c in citations if c.label() in keep] or citations
        return answer, False

    # -- plumbing ------------------------------------------------------------

    @staticmethod
    def _dispatch(toolbox: ToolBox, name: str, args: dict[str, Any]) -> ToolResult:
        handler = toolbox.handlers.get(name)
        if handler is None:
            return ToolResult(False, {"error": f"unknown tool {name!r}"}, [], "unknown tool")
        try:
            return handler(args)
        except Exception as exc:  # noqa: BLE001 - a tool failure is a card-level error
            return ToolResult(False, {"error": str(exc)}, [], f"{name} failed: {exc}")

    def _log(
        self,
        question: str,
        course: Optional[str],
        tracker: BudgetTracker,
        refused: bool,
        latency_ms: int,
        citations: list[Citation],
    ) -> None:
        self.store.log_agent_run(
            user_id=self.user_id,
            question=question,
            course_id=course,
            steps=tracker.steps,
            tools=tracker.tools_used,
            refused=refused,
            latency_ms=latency_ms,
            citations=[c.chunk_id for c in citations],
        )

    def _record_engagement(self, citations: list[Citation]) -> None:
        """Engagement is derived from what was actually asked about, never clicked.
        One increment per chapter per question, not per citation, so a question
        that happens to match six passages in one chapter does not count six times.
        """
        seen: set[str] = set()
        for citation in citations:
            ref = f"{citation.course_id}:{citation.source_id}:{citation.chapter_num}"
            if ref in seen:
                continue
            seen.add(ref)
            self.store.bump_engagement(self.user_id, ref, questions=1)


def _describe(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, "", []))[:120]


def _dedupe(citations: list[Citation]) -> list[Citation]:
    seen, out = set(), []
    for citation in citations:
        if citation.chunk_id in seen:
            continue
        seen.add(citation.chunk_id)
        out.append(citation)
    return out

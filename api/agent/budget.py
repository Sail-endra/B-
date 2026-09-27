"""Loop budgets.

An unbounded agent loop is how you get a 90-second response and a $4 bill per
question. Three limits, enforced from the first commit rather than added after
the first surprise:

  * step count   -- how many times the model may call tools
  * tool tokens  -- total size of tool results fed back into context
  * wall clock   -- hard timeout

The timeout renders whatever the agent had rather than a blank error. A partial
answer with the steps shown is more useful to a student than a spinner that gives
up, and it makes the failure legible instead of mysterious.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import AgentBudget, settings


class BudgetExceeded(Exception):
    def __init__(self, kind: str, detail: str):
        self.kind = kind
        super().__init__(detail)


@dataclass
class BudgetTracker:
    budget: AgentBudget = field(default_factory=lambda: settings.budget)
    steps: int = 0
    tool_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)
    tools_used: list[str] = field(default_factory=list)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.budget.hard_timeout_s - self.elapsed_s)

    def check(self) -> Optional[str]:
        """Return a reason string when the loop must stop, else None."""
        if self.steps >= self.budget.max_steps:
            return f"step budget reached ({self.budget.max_steps} steps)"
        if self.tool_tokens >= self.budget.max_tool_tokens:
            return f"tool-output budget reached ({self.budget.max_tool_tokens} tokens)"
        if self.elapsed_s >= self.budget.hard_timeout_s:
            return f"timed out after {self.budget.hard_timeout_s:.0f}s"
        return None

    def record_step(self) -> None:
        self.steps += 1

    def record_tool(self, name: str, payload_chars: int) -> None:
        self.tools_used.append(name)
        # ~4 chars per token is close enough for a budget guard.
        self.tool_tokens += payload_chars // 4

    def truncate_to_budget(self, text: str) -> str:
        """Clip a tool result so one oversized payload cannot consume the budget."""
        remaining_tokens = max(0, self.budget.max_tool_tokens - self.tool_tokens)
        limit = remaining_tokens * 4
        if limit <= 0:
            return "[tool output omitted: token budget exhausted]"
        if len(text) <= limit:
            return text
        return text[:limit] + "\n[...truncated to fit the tool-output budget]"

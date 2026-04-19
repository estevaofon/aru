"""Session management: state, persistence, and plan tracking."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from aru.providers import MODEL_ALIASES, get_model_display, resolve_model_ref

# Default model reference (provider/model format)
DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"

# Pricing per million tokens (USD). Cache read/write have separate rates.
# Format: {model_id_prefix: (input, output, cache_read, cache_write)}
# Prices as of 2026-04. Models not listed fall back to "default".
MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Anthropic  (input, output, cache_read=10%, cache_write=125%)
    "claude-sonnet-4-5":    (3.00,  15.00,  0.30,   3.75),
    "claude-sonnet-4-6":    (3.00,  15.00,  0.30,   3.75),
    "claude-opus-4":        (15.00, 75.00,  1.50,  18.75),
    "claude-opus-4-5":      (5.00,  25.00,  0.50,   6.25),
    "claude-opus-4-6":      (5.00,  25.00,  0.50,   6.25),
    "claude-haiku-3-5":     (0.80,   4.00,  0.08,   1.00),
    "claude-haiku-4-5":     (1.00,   5.00,  0.10,   1.25),
    # OpenAI
    "gpt-4o":               (2.50,  10.00,  1.25,   2.50),
    "gpt-4o-mini":          (0.15,   0.60,  0.075,  0.15),
    "gpt-4.1":              (2.00,   8.00,  0.50,   2.00),
    "gpt-4.1-mini":         (0.40,   1.60,  0.10,   0.40),
    "gpt-4.1-nano":         (0.10,   0.40,  0.025,  0.10),
    "o3":                   (2.00,   8.00,  0.50,   2.00),
    "o3-mini":              (1.10,   4.40,  0.275,  1.10),
    "o4-mini":              (1.10,   4.40,  0.275,  1.10),
    # Qwen / DashScope (<=256K tier, explicit cache: creation=125%, hit=10%)
    "qwen3-plus":           (0.50,   3.00,  0.05,   0.625),
    "qwen3.6-plus":         (0.50,   3.00,  0.05,   0.625),
    "qwen-plus":            (0.50,   3.00,  0.05,   0.625),
    "qwen-max":             (2.00,   6.00,  0.20,   2.50),
    "qwen-turbo":           (0.30,   0.60,  0.03,   0.375),
    "qwen3-coder-plus":     (0.50,   3.00,  0.05,   0.625),
    # DeepSeek
    "deepseek-chat":        (0.27,   1.10,  0.07,   0.27),
    "deepseek-reasoner":    (0.55,   2.19,  0.14,   0.55),
    # Google Gemini (via OpenRouter)
    "gemini-2.5-pro":       (1.25,  10.00,  0.315,  1.25),
    "gemini-2.5-flash":     (0.15,   0.60,  0.0375, 0.15),
    # Groq (free tier / very cheap)
    "llama-3.3-70b":        (0.59,   0.79,  0.0,    0.0),
    "llama-3.1":            (0.05,   0.08,  0.0,    0.0),
    # Fallback
    "default":              (3.00,  15.00,  0.30,   3.75),
}

SESSIONS_DIR = os.path.join(".aru", "sessions")


class InvokedSkill:
    """Record of a skill that was invoked in this session.

    Held in `session.invoked_skills` so the content survives compaction —
    mirrors claude-code's `STATE.invokedSkills`. The core re-injects the
    stored body (or the skill's shorter `reminder`) inside
    `<system-reminder>` after a compaction would otherwise drop it.

    Attributes:
        name: The skill's dir/slash name (e.g. "brainstorming").
        content: The SKILL.md body — what the model saw on the initial
            injection. Stored verbatim so post-compact restoration is
            identical to what the model read the first time.
        source_path: Absolute path to the SKILL.md file (for reference).
        invoked_at: Unix timestamp (seconds) — used to sort by recency
            when multiple skills were invoked and the preservation
            budget must drop some.
        agent_id: Scope identifier — which agent invoked the skill. None
            means the primary (top-level) agent. Subagents have a
            unique id set by `fork_ctx()`. Mirrors claude-code's
            `agentId` on `InvokedSkillInfo` so a subagent's invoked
            skills don't bleed into the parent's compaction.
    """

    def __init__(self, name: str, content: str, source_path: str = "",
                 invoked_at: float | None = None, agent_id: str | None = None):
        self.name = name
        self.content = content
        self.source_path = source_path
        self.invoked_at = time.time() if invoked_at is None else invoked_at
        self.agent_id = agent_id

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "content": self.content,
            "source_path": self.source_path,
            "invoked_at": self.invoked_at,
            "agent_id": self.agent_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InvokedSkill":
        raw_agent = data.get("agent_id")
        return cls(
            name=str(data.get("name", "")),
            content=str(data.get("content", "")),
            source_path=str(data.get("source_path", "")),
            invoked_at=float(data.get("invoked_at") or time.time()),
            agent_id=str(raw_agent) if raw_agent else None,
        )


@dataclass
class SubagentTrace:
    """Structured record of one sub-agent invocation.

    Populated by `delegate_task._execute_with_streaming` as events flow in.
    Stored on the session's `subagent_traces` list so the `/subagents` and
    `/subagent <id>` slash commands can render the invocation tree.

    `parent_id` links sub-agents spawned from another sub-agent (nested
    delegations) — mirrors claude-code's Perfetto agent hierarchy
    (runAgent.ts:355-359 registers `parentId` per agent).

    Fields like `tool_calls` are truncated previews (args/result capped) so
    long sessions don't accumulate gigabytes of trace data. Full tool
    outputs remain in the Agno message history.
    """

    task_id: str
    parent_id: str | None
    agent_name: str
    task: str  # truncated to 200 chars at write time
    started_at: float
    ended_at: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    # status: running while the agent is emitting events; completed on
    # normal exit; cancelled when abort_event fired; error on exception.
    status: Literal["running", "completed", "cancelled", "error"] = "running"
    result: str = ""  # truncated to 500 chars — full content lives in messages

    @property
    def duration(self) -> float:
        """Wall-clock duration in seconds (0 if still running)."""
        if self.ended_at is None:
            return 0.0
        return max(0.0, self.ended_at - self.started_at)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "parent_id": self.parent_id,
            "agent_name": self.agent_name,
            "task": self.task,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tool_calls": self.tool_calls,
            "status": self.status,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubagentTrace":
        return cls(
            task_id=str(data.get("task_id", "")),
            parent_id=data.get("parent_id"),
            agent_name=str(data.get("agent_name", "")),
            task=str(data.get("task", "")),
            started_at=float(data.get("started_at") or 0.0),
            ended_at=data.get("ended_at"),
            tokens_in=int(data.get("tokens_in") or 0),
            tokens_out=int(data.get("tokens_out") or 0),
            tool_calls=list(data.get("tool_calls") or []),
            status=data.get("status", "running"),
            result=str(data.get("result", "")),
        )


class PlanStep:
    """A single step in a structured plan."""

    def __init__(self, index: int, description: str, subtasks: list[str] | None = None):
        self.index = index
        self.description = description
        self.subtasks: list[str] = subtasks or []
        self.status: str = "pending"  # pending | in_progress | completed | failed

    @property
    def checkbox(self) -> str:
        if self.status == "completed":
            return "[bold green]\\[x][/bold green]"
        elif self.status == "in_progress":
            return "[bold yellow]\\[~][/bold yellow]"
        elif self.status == "failed":
            return "[bold red]\\[!][/bold red]"
        return "[dim]\\[ ][/dim]"

    @property
    def full_description(self) -> str:
        """Description with subtask list for executor prompt."""
        if not self.subtasks:
            return self.description
        subtask_lines = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(self.subtasks))
        return f"{self.description}\n\nSubtasks:\n{subtask_lines}"

    def __str__(self) -> str:
        return f"Step {self.index}: {self.description}"

    def to_dict(self) -> dict:
        return {"index": self.index, "description": self.description, "subtasks": self.subtasks, "status": self.status}

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        step = cls(data["index"], data["description"], data.get("subtasks", []))
        step.status = data.get("status", "pending")
        return step


def parse_plan_steps(plan_text: str) -> list[PlanStep]:
    """Extract structured steps from a plan markdown output.

    Matches step lines like:
    - [ ] Step 1: Do something
    - [ ] 1. Do something

    And subtask lines indented below each step:
      1. Write backend/models.py
      2. Edit backend/main.py — add router
    """
    steps = []
    lines = plan_text.split("\n")

    # Patterns
    checkbox_pattern = re.compile(r"^\s*-\s*\[[ x]\]\s*(.+)$")
    subtask_pattern = re.compile(r"^\s+\d+[.:]\s*(.+)$")

    current_step_desc = None
    current_subtasks: list[str] = []
    step_index = 0

    def _flush_step():
        nonlocal current_step_desc, current_subtasks, step_index
        if current_step_desc is not None:
            step_index += 1
            cleaned = re.sub(r"^(?:step\s*)?\d+[.:]\s*", "", current_step_desc, flags=re.IGNORECASE).strip()
            steps.append(PlanStep(step_index, cleaned or current_step_desc.strip(), current_subtasks))
            current_subtasks = []
            current_step_desc = None

    for line in lines:
        checkbox_match = checkbox_pattern.match(line)
        subtask_match = subtask_pattern.match(line)

        if checkbox_match:
            _flush_step()
            current_step_desc = checkbox_match.group(1)
        elif subtask_match and current_step_desc is not None:
            current_subtasks.append(subtask_match.group(1).strip())

    _flush_step()

    if steps:
        return steps

    # Fallback: numbered items without checkboxes
    numbered_pattern = re.compile(r"^\s*(?:step\s*)?\d+[.:]\s*(.+)$", re.IGNORECASE)
    for line in lines:
        match = numbered_pattern.match(line)
        if match:
            desc = match.group(1)
            cleaned = re.sub(r"^(?:step\s*)?\d+[.:]\s*", "", desc, flags=re.IGNORECASE).strip()
            steps.append(PlanStep(len(steps) + 1, cleaned or desc.strip()))

    return steps if len(steps) >= 2 else []


class Session:
    """Holds shared state across the conversation."""

    # Approximate chars-per-token ratio for estimation (conservative)
    _CHARS_PER_TOKEN = 3.5

    def __init__(self, session_id: str | None = None):
        self.session_id: str = session_id or _generate_session_id()
        # History is a list of block-shaped items:
        # {"role": "user" | "assistant" | "tool", "content": list[Block]}
        # See aru.history_blocks for block schema and helpers.
        self.history: list[dict] = []
        self.current_plan: str | None = None
        self.plan_task: str | None = None
        self.plan_steps: list[PlanStep] = []
        # Plan mode — when True, mutating tools (edit/write/bash/delegate_task)
        # are blocked by the tool wrapper's gate. Set by enter_plan_mode,
        # cleared by exit_plan_mode approval, persists across turns.
        self.plan_mode: bool = False
        # Currently active skill name per agent scope. Keyed by agent_id;
        # None-key is the primary (top-level) agent. Set by invoke_skill and
        # by the CLI slash-command dispatcher. Consulted by the tool wrapper
        # in agent_factory to enforce `disallowed_tools`. Single slot per
        # agent scope: invoking a new skill replaces the previous one for
        # that agent. Keying by agent_id means a subagent does not inherit
        # its parent's active skill — claude-code parity (STATE.invokedSkills
        # composite keying, state.ts:1516).
        self.active_skills: dict[str | None, str] = {}
        # All skills invoked in this session, keyed by "<agent_id>:<name>"
        # (empty agent_id for primary). Grows as skills are invoked; never
        # shrinks during a session. Used by compaction to preserve skill
        # bodies via `<system-reminder>` attachment. Keying includes agent_id
        # so a subagent's invocations stay out of the parent's compaction
        # (claude-code parity — state.ts:1516).
        self.invoked_skills: dict[str, InvokedSkill] = {}
        # Feedback from the last rejected plan (auto-approval flow or
        # exit_plan_mode). Injected into the next turn's plan reminder so
        # the agent sees the user's critique and revises. Cleared once
        # consumed by the reminder.
        self._plan_rejection_feedback: str | None = None
        # Transient flag set by runner when a turn ends with pending plan steps;
        # surfaced as a warning in the next turn's plan reminder, then cleared.
        self._pending_plan_warning: bool = False
        # Monotonic plan generation — bumped whenever the plan is replaced or
        # cleared. update_plan_step captures this and only its rendering loop
        # consults it; lets the runner tell stale renders apart from live ones.
        self._plan_generation: int = 0
        # Set by update_plan_step / set_plan / clear_plan whenever plan state
        # changes and a render should happen. Runner flushes this once per
        # tool batch so multiple mutations in one batch produce one panel.
        self._plan_render_pending: bool = False
        self.model_ref: str = DEFAULT_MODEL  # provider/model format
        self.cwd: str = os.getcwd()
        self.created_at: str = datetime.now().isoformat(timespec="milliseconds")
        self.updated_at: str = self.created_at
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_cache_write_tokens: int = 0
        self.api_calls: int = 0
        # Per-call metrics: last API call's context window (set by cache_patch)
        self.last_input_tokens: int = 0
        self.last_output_tokens: int = 0
        self.last_cache_read: int = 0
        self.last_cache_write: int = 0
        # Context cache — invalidated on file mutations
        self._cached_tree: str | None = None
        self._cached_git_status: str | None = None
        self._context_dirty: bool = True
        # Tree depth for env context (configurable via aru.json "tree_depth")
        self._tree_max_depth: int = 2
        # Token budget (0 = unlimited)
        self.token_budget: int = 0
        # Per-session reasoning effort override. When set, takes precedence
        # over the provider/model's `reasoning` config. Values:
        #   None             → use config (default)
        #   "low"/"medium"/"high"/"max" → force that effort level
        #   "off"            → disable thinking entirely
        # Set via /reasoning slash command; persists across `aru resume`.
        self.reasoning_override: str | None = None
        # Structured sub-agent trace log, populated by delegate_task. Each
        # entry records one delegation (task, duration, tokens, tool calls,
        # result preview). Consumed by `/subagents` / `/subagent <id>` slash
        # commands for observability. Not persisted in `to_dict()` by default
        # to keep session JSON small — persistence to a separate subagents/
        # subdir is feature #G.
        self.subagent_traces: list[SubagentTrace] = []
        # Background sub-agent results waiting to be surfaced to the primary
        # agent on its next turn. `delegate_task(run_in_background=True)`
        # dispatches the sub-agent in an asyncio task and appends the result
        # here when it finishes. The REPL drains this list before each
        # prompt so the model sees a `<task-notification>` message.
        # Mirrors claude-code's LocalAgentTask async notification pattern
        # (LocalAgentTask.tsx:466-500).
        self.pending_notifications: list[dict] = []

    @property
    def model_id(self) -> str:
        """Resolve to the actual model ID for the API."""
        from aru.providers import _get_actual_model_id, get_provider
        provider_key, model_name = resolve_model_ref(self.model_ref)
        provider = get_provider(provider_key)
        if provider:
            return _get_actual_model_id(provider, model_name)
        return model_name

    @property
    def model_display(self) -> str:
        return get_model_display(self.model_ref)

    @property
    def title(self) -> str:
        """Generate a short title from the first user message or plan task."""
        from aru.history_blocks import item_text
        if self.plan_task:
            return self.plan_task[:60]
        for msg in self.history:
            if msg["role"] == "user":
                text = item_text(msg)[:60]
                return text.split("\n")[0] if text else "(empty session)"
        return "(empty session)"

    def set_plan(self, task: str, plan_content: str):
        """Store a plan and parse its steps."""
        self.current_plan = plan_content
        self.plan_task = task
        self.plan_steps = parse_plan_steps(plan_content)
        self._plan_generation += 1
        self._plan_render_pending = True

    def clear_plan(self):
        """Clear the active plan."""
        had_plan = bool(self.plan_steps) or self.current_plan is not None
        self.current_plan = None
        self.plan_task = None
        self.plan_steps = []
        self._pending_plan_warning = False
        if had_plan:
            self._plan_generation += 1
        # Clearing alone doesn't need a render — the replacement set_plan
        # (or end-of-turn) will handle it. But mark pending so an explicit
        # clear without a replacement still flushes any stale queued state.
        self._plan_render_pending = False

    # --- Active skill helpers (scoped by agent_id) ----------------------

    @staticmethod
    def _invoked_key(agent_id: str | None, name: str) -> str:
        """Composite key for invoked_skills (empty agent_id = primary)."""
        return f"{agent_id or ''}:{name}"

    @property
    def active_skill(self) -> str | None:
        """Backward-compat alias: the primary agent's active skill.

        Prefer `get_active_skill(agent_id)` / `set_active_skill(...)` in new
        code. This property shadows the pre-C3 single-slot attribute so
        callers and tests that read `session.active_skill` keep working and
        always refer to the top-level agent's slot.
        """
        return self.active_skills.get(None)

    @active_skill.setter
    def active_skill(self, value: str | None) -> None:
        self.set_active_skill(None, value)

    def get_active_skill(self, agent_id: str | None = None) -> str | None:
        """Return the currently active skill for a given agent scope.

        `agent_id=None` is the primary (top-level) agent. Subagents should
        pass their own `ctx.agent_id`.
        """
        return self.active_skills.get(agent_id)

    def set_active_skill(self, agent_id: str | None, name: str | None) -> None:
        """Set (or clear, when name is None) the active skill for a scope."""
        if name:
            self.active_skills[agent_id] = name
        else:
            self.active_skills.pop(agent_id, None)

    def get_invoked_skills_for_agent(
        self, agent_id: str | None = None
    ) -> dict[str, "InvokedSkill"]:
        """Return invoked-skill records belonging to a specific agent scope.

        Filters the flat `invoked_skills` dict so compaction at the primary
        scope doesn't replay subagent-invoked skills, and vice versa.
        """
        return {
            k: v for k, v in self.invoked_skills.items() if v.agent_id == agent_id
        }

    def record_invoked_skill(self, name: str, content: str, source_path: str = "",
                             agent_id: str | None = None) -> None:
        """Register a skill invocation so its body survives compaction.

        Called by the CLI slash-command dispatcher and by the `invoke_skill`
        tool. Re-invoking the same skill (in the same agent scope) refreshes
        `invoked_at` and overwrites the stored content (useful if the
        SKILL.md was edited mid-session).
        """
        if not name:
            return
        key = self._invoked_key(agent_id, name)
        self.invoked_skills[key] = InvokedSkill(
            name=name,
            content=content,
            source_path=source_path,
            agent_id=agent_id,
        )

    def track_tokens(self, metrics):
        """Accumulate token usage from a RunCompletedEvent.metrics."""
        if metrics is None:
            return
        self.total_input_tokens += getattr(metrics, "input_tokens", 0) or 0
        self.total_output_tokens += getattr(metrics, "output_tokens", 0) or 0
        self.total_cache_read_tokens += getattr(metrics, "cache_read_tokens", 0) or 0
        self.total_cache_write_tokens += getattr(metrics, "cache_write_tokens", 0) or 0
        self.api_calls += 1
        # Capture last API call's context window (set by cache_patch)
        try:
            from aru.cache_patch import get_last_call_metrics
            self.last_input_tokens, self.last_output_tokens, self.last_cache_read, self.last_cache_write = get_last_call_metrics()
        except ImportError:
            self.last_input_tokens = getattr(metrics, "input_tokens", 0) or 0
            self.last_output_tokens = getattr(metrics, "output_tokens", 0) or 0
            self.last_cache_read = 0
            self.last_cache_write = 0

    def _get_pricing(self) -> tuple[float, float, float, float]:
        """Get per-million-token pricing for the current model."""
        model_id = self.model_id
        # Try exact match, then prefix match, then fallback
        for prefix, pricing in MODEL_PRICING.items():
            if prefix == "default":
                continue
            if model_id.startswith(prefix):
                return pricing
        return MODEL_PRICING["default"]

    @property
    def estimated_cost(self) -> float:
        """Estimate cumulative cost in USD based on token usage and model pricing.

        For input tokens, subtracts cache_read (charged at cache rate) and
        cache_write (charged at write rate) from the base input count.
        """
        price_in, price_out, price_cache_read, price_cache_write = self._get_pricing()
        # Non-cached input = total input - cache_read - cache_write
        base_input = max(0, self.total_input_tokens - self.total_cache_read_tokens - self.total_cache_write_tokens)
        cost = (
            base_input * price_in / 1_000_000
            + self.total_output_tokens * price_out / 1_000_000
            + self.total_cache_read_tokens * price_cache_read / 1_000_000
            + self.total_cache_write_tokens * price_cache_write / 1_000_000
        )
        return cost

    @property
    def token_summary(self) -> str:
        """One-line summary shown after each response: context window + cost."""
        if self.last_input_tokens <= 0 and self.total_input_tokens == 0:
            return ""
        cost = self.estimated_cost
        cost_str = f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"
        if self.last_input_tokens > 0:
            ctx_total = self.last_input_tokens + self.last_output_tokens + self.last_cache_read + self.last_cache_write
            parts = [f"in: {self.last_input_tokens:,}", f"out: {self.last_output_tokens:,}"]
            if self.last_cache_read > 0:
                parts.append(f"cache_read: {self.last_cache_read:,}")
            if self.last_cache_write > 0:
                parts.append(f"cache_write: {self.last_cache_write:,}")
            return f"context: {ctx_total:,} ({' / '.join(parts)}) | cost: {cost_str}"
        # Fallback when per-call metrics aren't available
        total = self.total_input_tokens + self.total_output_tokens
        return f"tokens: {total:,} | cost: {cost_str}"

    @property
    def cost_summary(self) -> str:
        """Detailed cost breakdown for /cost command."""
        total = self.total_input_tokens + self.total_output_tokens
        if total == 0:
            return "No token usage yet."
        cost = self.estimated_cost
        cost_str = f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"
        lines = [
            f"Session cost: {cost_str}",
            f"",
            f"Cumulative tokens:",
            f"  input:       {self.total_input_tokens:,}",
            f"  output:      {self.total_output_tokens:,}",
        ]
        if self.total_cache_read_tokens > 0:
            lines.append(f"  cache_read:  {self.total_cache_read_tokens:,}")
        if self.total_cache_write_tokens > 0:
            lines.append(f"  cache_write: {self.total_cache_write_tokens:,}")
        lines.append(f"  total:       {total:,}")
        lines.append(f"  api calls:   {self.api_calls}")
        if self.last_input_tokens > 0:
            ctx_total = self.last_input_tokens + self.last_output_tokens + self.last_cache_read + self.last_cache_write
            lines.append(f"")
            lines.append(f"Last context window: {ctx_total:,}")
            lines.append(f"  input:       {self.last_input_tokens:,}")
            lines.append(f"  output:      {self.last_output_tokens:,}")
            if self.last_cache_read > 0:
                lines.append(f"  cache_read:  {self.last_cache_read:,}")
            if self.last_cache_write > 0:
                lines.append(f"  cache_write: {self.last_cache_write:,}")
        if self.token_budget > 0:
            pct = int(total / self.token_budget * 100)
            lines.append(f"")
            lines.append(f"Budget: {pct}% used")

        # Micro-compaction stats: shown when the pre-API-call prune actually
        # fired. Useful for understanding whether the budget threshold is set
        # right — if results_cleared stays at 0 across long sessions, the
        # protect window is generous enough that prune never trips.
        try:
            from aru.cache_patch import get_microcompact_stats
            mc = get_microcompact_stats()
            if mc["results_cleared"] > 0 or mc.get("overflow_recoveries", 0) > 0:
                lines.append(f"")
                lines.append(f"Micro-compaction (process-wide):")
                lines.append(f"  invocations:     {mc['invocations']:,}")
                lines.append(f"  clear passes:    {mc['clear_passes']:,}")
                lines.append(f"  results cleared: {mc['results_cleared']:,}")
                if mc.get("overflow_recoveries", 0) > 0:
                    lines.append(f"  overflow saves:  {mc['overflow_recoveries']:,}")
        except Exception:
            pass
        return "\n".join(lines)

    def invalidate_context_cache(self):
        """Mark cached tree/git status as stale. Call after file mutations."""
        self._context_dirty = True

    def get_cached_tree(self, cwd: str) -> str | None:
        """Return cached directory tree, regenerating if dirty."""
        if self._context_dirty or self._cached_tree is None:
            self._refresh_context_cache(cwd)
        return self._cached_tree

    def get_cached_git_status(self, cwd: str) -> str | None:
        """Return cached git status, regenerating if dirty."""
        if self._context_dirty or self._cached_git_status is None:
            self._refresh_context_cache(cwd)
        return self._cached_git_status

    def _refresh_context_cache(self, cwd: str):
        """Regenerate tree and git status caches."""
        try:
            from aru.tools.codebase import get_project_tree
            self._cached_tree = get_project_tree(cwd, max_depth=self._tree_max_depth) or None
        except Exception:
            self._cached_tree = None
        try:
            self._cached_git_status = subprocess.run(
                ["git", "status", "-s"], capture_output=True, text=True, cwd=cwd, timeout=2
            ).stdout.strip() or None
        except Exception:
            self._cached_git_status = None
        self._context_dirty = False

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Fast approximate token count based on character length."""
        return int(len(text) / Session._CHARS_PER_TOKEN)

    def check_budget_warning(self) -> str | None:
        """Return a warning string if token usage is approaching the budget."""
        if self.token_budget <= 0:
            return None
        total = self.total_input_tokens + self.total_output_tokens
        pct = total / self.token_budget * 100
        if pct >= 95:
            return f"[bold red]Token budget nearly exhausted ({pct:.0f}%)[/bold red]"
        if pct >= 80:
            return f"[yellow]Token budget at {pct:.0f}%[/yellow]"
        return None

    def undo_last_turn(self) -> int:
        """Remove the last complete turn (user message + assistant/tool responses).

        Pops backward from the end of history until the last user message
        (inclusive) is removed. Returns the number of messages removed.
        """
        if not self.history:
            return 0
        removed = 0
        # Pop from the end until we've removed one user message
        while self.history:
            msg = self.history.pop()
            removed += 1
            if msg["role"] == "user":
                break
        self.updated_at = datetime.now().isoformat(timespec="milliseconds")
        return removed

    def add_message(self, role: str, content):
        """Append a message to history.

        `content` may be a string (auto-wrapped as a single text block) or
        already a list of block dicts (used by the runner to persist
        structured assistant/tool_result turns).
        """
        from aru.history_blocks import coerce_content
        blocks = coerce_content(content)
        # Skip empty messages entirely — they only add noise and break
        # role alternation assertions.
        if not blocks:
            return
        self.history.append({"role": role, "content": blocks})
        # Hard cap as safety net — structured pruning/compaction in
        # aru/context.py handles the normal case; this only fires if
        # something bypasses them. Set high enough that long sessions
        # (which now accumulate more messages because prune is
        # non-destructive for text and compact rarely fires) don't hit
        # this destructive path routinely.
        if len(self.history) > 300:
            self.history = self.history[-300:]

    def add_structured_message(self, role: str, blocks: list[dict]):
        """Explicitly add a message with pre-built content blocks.

        Thin wrapper over `add_message` for call sites that want to make
        it obvious they are producing structured content.
        """
        self.add_message(role, blocks)

    def compact_history(self, max_tokens: int) -> int:
        """Remove oldest messages until the estimated token total is below max_tokens.

        Args:
            max_tokens: Target token ceiling for the conversation history.

        Returns:
            Number of messages removed.
        """
        from aru.history_blocks import item_char_len

        def _total_tokens() -> int:
            return sum(int(item_char_len(m) / self._CHARS_PER_TOKEN) for m in self.history)

        removed = 0
        while self.history and _total_tokens() > max_tokens:
            self.history.pop(0)
            removed += 1

        if removed:
            self.updated_at = datetime.now().isoformat(timespec="milliseconds")

        return removed

    def to_dict(self) -> dict:
        # active_skills serialized with empty-string for the primary (None)
        # agent_id so the JSON stays well-formed. Same convention used by
        # the invoked_skills composite key.
        return {
            "session_id": self.session_id,
            "history": self.history,
            "current_plan": self.current_plan,
            "plan_task": self.plan_task,
            "plan_steps": [s.to_dict() for s in self.plan_steps],
            "plan_mode": self.plan_mode,
            "active_skills": {
                ("" if k is None else str(k)): v for k, v in self.active_skills.items()
            },
            "invoked_skills": {k: v.to_dict() for k, v in self.invoked_skills.items()},
            "model_ref": self.model_ref,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reasoning_override": self.reasoning_override,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        from aru.history_blocks import coerce_history
        session = cls(session_id=data["session_id"])
        # Backward compat: old sessions stored content as strings; coerce to
        # block-shaped form on load so downstream code can assume blocks.
        session.history = coerce_history(data.get("history", []))
        session.current_plan = data.get("current_plan")
        session.plan_task = data.get("plan_task")
        session.plan_steps = [PlanStep.from_dict(s) for s in data.get("plan_steps", [])]
        session.plan_mode = bool(data.get("plan_mode", False))

        # active_skills: prefer the scoped dict, fall back to the legacy
        # single-slot "active_skill" string. Legacy values migrate to the
        # primary (None) slot.
        scoped = data.get("active_skills")
        if isinstance(scoped, dict):
            session.active_skills = {
                (None if k == "" else str(k)): str(v)
                for k, v in scoped.items()
                if v
            }
        else:
            legacy_active = data.get("active_skill")
            if legacy_active:
                session.active_skills = {None: str(legacy_active)}

        invoked = data.get("invoked_skills") or {}
        if isinstance(invoked, dict):
            parsed: dict[str, InvokedSkill] = {}
            for k, v in invoked.items():
                if not isinstance(v, dict):
                    continue
                skill = InvokedSkill.from_dict(v)
                # Legacy sessions keyed by bare skill name with no agent_id
                # stored on the record. Treat them as primary-scope so they
                # continue to show up for the primary agent's compaction.
                if ":" not in str(k):
                    key = cls._invoked_key(None, skill.name)
                    parsed[key] = skill
                else:
                    parsed[str(k)] = skill
            session.invoked_skills = parsed
        # Support both new "model_ref" and legacy "model_key" for backward compat
        model_ref = data.get("model_ref")
        if not model_ref:
            legacy_key = data.get("model_key", "sonnet")
            model_ref = MODEL_ALIASES.get(legacy_key, DEFAULT_MODEL)
        session.model_ref = model_ref
        session.cwd = data.get("cwd", os.getcwd())
        session.created_at = data.get("created_at", "")
        session.updated_at = data.get("updated_at", "")
        override = data.get("reasoning_override")
        session.reasoning_override = str(override) if override else None
        return session

    def get_context_summary(self) -> str:
        """Build compact context string from active plan status."""
        parts = []
        if self.current_plan:
            parts.append(f"## Active Plan\nTask: {self.plan_task}\n\n{self.render_plan_progress()}")
        return "\n\n".join(parts)

    def render_plan_progress(self) -> str:
        """Render the plan steps with checkbox status for display."""
        if not self.plan_steps:
            return ""
        lines = []
        completed = sum(1 for s in self.plan_steps if s.status == "completed")
        total = len(self.plan_steps)
        lines.append(f"[bold]Plan Progress ({completed}/{total}):[/bold]")
        for step in self.plan_steps:
            style = ""
            if step.status == "completed":
                style = "green"
            elif step.status == "in_progress":
                style = "yellow"
            elif step.status == "failed":
                style = "red"
            desc = f"[{style}]{step.description}[/{style}]" if style else step.description
            lines.append(f"  {step.checkbox} {desc}")
        return "\n".join(lines)

    def render_compact_progress(self, current_index: int) -> str:
        """Render a token-efficient progress view for LLM context."""
        if not self.plan_steps:
            return ""
        completed = sum(1 for s in self.plan_steps if s.status == "completed")
        total = len(self.plan_steps)
        lines = [f"Progress: {completed}/{total} steps done."]
        for step in self.plan_steps:
            if step.status == "completed":
                lines.append(f"  [x] Step {step.index} (done)")
            elif step.index == current_index:
                lines.append(f"  [~] Step {step.index}: {step.description} << CURRENT")
            else:
                lines.append(f"  [ ] Step {step.index}: {step.description}")
        return "\n".join(lines)


def _generate_session_id() -> str:
    """Generate a short, unique session ID like 'a3f7b2'."""
    raw = f"{time.time()}-{os.getpid()}-{random.randint(0, 999999)}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


class SessionStore:
    """Persist and load sessions from .aru/sessions/."""

    def __init__(self, base_dir: str | None = None):
        self.base_dir = base_dir or os.path.join(os.getcwd(), SESSIONS_DIR)
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"{session_id}.json")

    def save(self, session: Session):
        """Save session state to disk."""
        session.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._path(session.session_id), "w", encoding="utf-8") as f:
            json.dump(session.to_dict(), f, indent=2, ensure_ascii=False)

    def load(self, session_id: str) -> Session | None:
        """Load a session by ID (full or prefix match)."""
        path = self._path(session_id)
        if os.path.isfile(path):
            return self._read(path)

        for filename in os.listdir(self.base_dir):
            if filename.startswith(session_id) and filename.endswith(".json"):
                return self._read(os.path.join(self.base_dir, filename))

        return None

    def _read(self, path: str) -> Session | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Session.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError):
            return None

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List recent sessions, newest first."""
        sessions = []
        if not os.path.isdir(self.base_dir):
            return sessions

        for filename in os.listdir(self.base_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self.base_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "session_id": data["session_id"],
                    "title": data.get("plan_task") or self._first_user_msg(data),
                    "model": data.get("model_ref", data.get("model_key", "?")),
                    "messages": len(data.get("history", [])),
                    "updated_at": data.get("updated_at", ""),
                    "cwd": data.get("cwd", ""),
                })
            except (json.JSONDecodeError, OSError, KeyError):
                continue

        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions[:limit]

    def _first_user_msg(self, data: dict) -> str:
        from aru.history_blocks import item_text
        for msg in data.get("history", []):
            if msg["role"] == "user":
                text = item_text(msg)[:60]
                return text.split("\n")[0] if text else "(empty session)"
        return "(empty session)"

    def load_last(self) -> Session | None:
        """Load the most recently updated session."""
        sessions = self.list_sessions(limit=1)
        if sessions:
            return self.load(sessions[0]["session_id"])
        return None

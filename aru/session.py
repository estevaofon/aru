"""Session management: state, persistence, and plan tracking."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import time
from datetime import datetime

from aru.providers import MODEL_ALIASES, get_model_display, resolve_model_ref

# Default model reference (provider/model format)
DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"

SESSIONS_DIR = os.path.join(".aru", "sessions")


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
    # History summarization threshold: summarize oldest messages when history exceeds this
    _HISTORY_SUMMARIZE_THRESHOLD = 20
    _HISTORY_SUMMARIZE_COUNT = 6  # number of oldest messages to condense

    def __init__(self, session_id: str | None = None):
        self.session_id: str = session_id or _generate_session_id()
        self.history: list[dict[str, str]] = []
        self.current_plan: str | None = None
        self.plan_task: str | None = None
        self.plan_steps: list[PlanStep] = []
        self.model_ref: str = DEFAULT_MODEL  # provider/model format
        self.cwd: str = os.getcwd()
        self.created_at: str = datetime.now().isoformat(timespec="milliseconds")
        self.updated_at: str = self.created_at
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_cache_write_tokens: int = 0
        self.api_calls: int = 0
        # Context cache — invalidated on file mutations
        self._cached_tree: str | None = None
        self._cached_git_status: str | None = None
        self._context_dirty: bool = True
        # Token budget (0 = unlimited)
        self.token_budget: int = 0

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
        if self.plan_task:
            return self.plan_task[:60]
        for msg in self.history:
            if msg["role"] == "user":
                text = msg["content"][:60]
                return text.split("\n")[0]
        return "(empty session)"

    def set_plan(self, task: str, plan_content: str):
        """Store a plan and parse its steps."""
        self.current_plan = plan_content
        self.plan_task = task
        self.plan_steps = parse_plan_steps(plan_content)

    def clear_plan(self):
        """Clear the active plan."""
        self.current_plan = None
        self.plan_task = None
        self.plan_steps = []

    def track_tokens(self, metrics):
        """Accumulate token usage from a RunCompletedEvent.metrics."""
        if metrics is None:
            return
        self.total_input_tokens += getattr(metrics, "input_tokens", 0) or 0
        self.total_output_tokens += getattr(metrics, "output_tokens", 0) or 0
        self.total_cache_read_tokens += getattr(metrics, "cache_read_tokens", 0) or 0
        self.total_cache_write_tokens += getattr(metrics, "cache_write_tokens", 0) or 0
        self.api_calls += 1

    @property
    def token_summary(self) -> str:
        total = self.total_input_tokens + self.total_output_tokens
        if total == 0:
            return ""
        metrics_str = f"in: {self.total_input_tokens:,} / out: {self.total_output_tokens:,}"
        if self.total_cache_read_tokens > 0:
            metrics_str += f" / cached: {self.total_cache_read_tokens:,}"
        summary = f"tokens: {total:,} ({metrics_str}) | calls: {self.api_calls}"
        if self.token_budget > 0:
            pct = int(total / self.token_budget * 100)
            summary += f" | budget: {pct}%"
        return summary

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
            self._cached_tree = get_project_tree(cwd, max_depth=3) or None
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

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})
        # Summarize oldest messages instead of hard-truncating
        if len(self.history) > self._HISTORY_SUMMARIZE_THRESHOLD:
            self._summarize_old_messages()
        # Hard cap as safety net
        if len(self.history) > 30:
            self.history = self.history[-30:]

    def _summarize_old_messages(self):
        """Condense the oldest messages into a single summary message.

        Preserves [Tools] and [Plan] sections so the model knows what actions
        were taken even after summarization.
        """
        n = self._HISTORY_SUMMARIZE_COUNT
        old = self.history[:n]
        rest = self.history[n:]
        summary_parts = []
        for msg in old:
            role = msg["role"]
            content = msg["content"]
            # Extract [Tools] section before truncating
            tools_section = ""
            tools_idx = content.find("\n[Tools]\n")
            if tools_idx != -1:
                tools_section = content[tools_idx:]
            # Truncate the main text but keep tools metadata
            text = content[:300] if tools_idx == -1 else content[:tools_idx][:300]
            if len(content) > 300:
                text += "..."
            if tools_section:
                text += tools_section
            summary_parts.append(f"[{role}]: {text}")
        summary = "[Conversation summary of earlier messages]\n" + "\n".join(summary_parts)
        self.history = [{"role": "user", "content": summary}] + rest
        self.updated_at = datetime.now().isoformat(timespec="milliseconds")

    def compact_history(self, max_tokens: int) -> int:
        """Remove oldest messages until the estimated token total is below max_tokens.

        Args:
            max_tokens: Target token ceiling for the conversation history.

        Returns:
            Number of messages removed.
        """
        def _total_tokens() -> int:
            return sum(self.estimate_tokens(m["content"]) for m in self.history)

        removed = 0
        while self.history and _total_tokens() > max_tokens:
            self.history.pop(0)
            removed += 1

        if removed:
            self.updated_at = datetime.now().isoformat(timespec="milliseconds")

        return removed

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "history": self.history,
            "current_plan": self.current_plan,
            "plan_task": self.plan_task,
            "plan_steps": [s.to_dict() for s in self.plan_steps],
            "model_ref": self.model_ref,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        session = cls(session_id=data["session_id"])
        session.history = data.get("history", [])
        session.current_plan = data.get("current_plan")
        session.plan_task = data.get("plan_task")
        session.plan_steps = [PlanStep.from_dict(s) for s in data.get("plan_steps", [])]
        # Support both new "model_ref" and legacy "model_key" for backward compat
        model_ref = data.get("model_ref")
        if not model_ref:
            legacy_key = data.get("model_key", "sonnet")
            model_ref = MODEL_ALIASES.get(legacy_key, DEFAULT_MODEL)
        session.model_ref = model_ref
        session.cwd = data.get("cwd", os.getcwd())
        session.created_at = data.get("created_at", "")
        session.updated_at = data.get("updated_at", "")
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
        for msg in data.get("history", []):
            if msg["role"] == "user":
                return msg["content"][:60].split("\n")[0]
        return "(empty session)"

    def load_last(self) -> Session | None:
        """Load the most recently updated session."""
        sessions = self.list_sessions(limit=1)
        if sessions:
            return self.load(sessions[0]["session_id"])
        return None

"""Granular permission system for Aru tools.

Resolves each tool action to one of three outcomes: allow, ask, deny.
Supports per-tool rules with fnmatch patterns (file paths for read/edit/write,
command strings for bash, URLs for web_fetch, etc.).

Configuration in aru.json:
    "permission": {
        "*": "ask",
        "read": "allow",
        "edit": {"*": "ask", "*.env": "deny"},
        "bash": {"*": "ask", "git *": "allow", "rm -rf *": "deny"}
    }

Rule precedence: last-match-wins (place catch-all "*" first, specific rules after).
"""

from __future__ import annotations

import fnmatch
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator, Literal

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from aru.runtime import get_ctx
from aru.select import select_option

PermissionAction = Literal["allow", "ask", "deny"]

VALID_ACTIONS: set[str] = {"allow", "ask", "deny"}


# ---------------------------------------------------------------------------
# Typed permission errors (OpenCode parity)
# ---------------------------------------------------------------------------
#
# Three distinct outcomes that all collapse to "deny the tool call" in the
# bool-returning `check_permission`, but carry different meaning:
#
#   PermissionDenied   — a rule said no (config, plan mode, skill).
#                        Not user-visible; the agent sees the blocker string.
#   PermissionRejected — the user pressed "No" on the prompt. No feedback.
#   PermissionCorrected — the user pressed "No" and typed guidance for the
#                        agent. The `feedback` field carries what to say.
#
# Mirrors opencode/permission/index.ts:83-103 `DeniedError/RejectedError/
# CorrectedError`. Defined here so future wrappers can switch from the
# bool + `last_rejection_feedback` singleton pattern to per-call exceptions
# (race-free under concurrent tool calls).


class PermissionDenied(Exception):
    """A permission rule denied the call (config, plan mode, skill, etc.)."""

    def __init__(self, category: str, subject: str, pattern: str = "*") -> None:
        self.category = category
        self.subject = subject
        self.pattern = pattern
        super().__init__(f"permission denied for {category}: {subject} (rule: {pattern})")


class PermissionRejected(Exception):
    """User pressed No on the interactive permission prompt, no feedback."""

    def __init__(self, category: str, subject: str) -> None:
        self.category = category
        self.subject = subject
        super().__init__(f"user rejected {category}: {subject}")


class PermissionCorrected(Exception):
    """User pressed No on the prompt and typed guidance for the agent.

    `feedback` carries the free-text message the agent should consider
    before retrying with a different approach.
    """

    def __init__(self, category: str, subject: str, feedback: str) -> None:
        self.category = category
        self.subject = subject
        self.feedback = feedback
        super().__init__(
            f"user rejected {category}: {subject} with feedback: {feedback}"
        )


PermissionError_ = PermissionDenied  # alias for type hints in unions


@dataclass
class PermissionRule:
    pattern: str
    action: PermissionAction


@dataclass
class PermissionConfig:
    default: PermissionAction = "ask"
    categories: dict[str, list[PermissionRule]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Unified rule shape (OpenCode parity)
# ---------------------------------------------------------------------------
#
# Flat Ruleset model. A single `Rule` shape for every source: user config,
# category defaults, sensitive-file defaults, safe-bash defaults, plan-mode
# and skill-disallowed gates, session "always" approvals. Every gate
# produces `Ruleset`; a single `evaluate()` composes them. Mirrors
# opencode/permission/index.ts:133 (`Permission.evaluate`).
#
# Coexists with `PermissionConfig`/`PermissionRule` during the migration —
# `parse_permission_config` and `resolve_permission` still produce/consume
# the legacy shape. `from_config`/`evaluate` are the v2 entry points used
# by subsequent phases.


@dataclass(frozen=True)
class Rule:
    """Single uniform rule: who (permission), what (pattern), how (action).

    `permission` and `pattern` are both matched via fnmatch — wildcards
    work in either field. `Rule("edit*", "*", "ask")` asks on any permission
    starting with "edit"; `Rule("*", "*", "deny")` is a universal catch-all.
    """
    permission: str
    pattern: str
    action: PermissionAction


Ruleset = list[Rule]


# ---------------------------------------------------------------------------
# Canonical permission names (OpenCode parity)
# ---------------------------------------------------------------------------
#
# The tool name seen by the LLM (e.g. `delegate_task`, `web_fetch`) differs
# from the canonical permission name used in config and rules
# (`task`, `webfetch`). Mirrors OpenCode's `EDIT_TOOLS = ["edit", "write",
# "apply_patch", "multiedit"]` convention where multiple tool names share
# one permission. Enables:
#   1. aru.json shape portable with opencode.json
#   2. Grouping: one rule `{"edit": "ask"}` covers edit_file and edit_files
#   3. Wildcard at the permission level: `{"edit*": "allow"}` is expressible

TOOL_PERMISSION_NAMES: dict[str, str] = {
    # Edit family — all mutations to existing files
    "edit_file": "edit",
    "edit_files": "edit",
    # Write family — all creations
    "write_file": "write",
    "write_files": "write",
    # Read family
    "read_file": "read",
    "read_files": "read",
    # Search family
    "glob_search": "glob",
    "grep_search": "grep",
    "list_directory": "list",
    # Shell
    "bash": "bash",
    "run_command": "bash",
    # Web — OpenCode uses single-word permission names
    "web_fetch": "webfetch",
    "web_search": "websearch",
    # Subagent delegation — OpenCode permission is "task"
    "delegate_task": "task",
    # Skill invocation
    "invoke_skill": "skill",
}


def canonical_permission(tool_name: str) -> str:
    """Map a tool name to its canonical permission name.

    Tools without an explicit mapping use their own name as the permission
    (common case for custom tools). Callers pass the canonical name to
    `evaluate()` so rules in `aru.json` keyed by canonical names apply
    uniformly regardless of which specific tool in a family was invoked.
    """
    return TOOL_PERMISSION_NAMES.get(tool_name, tool_name)


# ---------------------------------------------------------------------------
# Pattern expansion (OpenCode parity)
# ---------------------------------------------------------------------------

def _expand_pattern(pattern: str) -> str:
    """Expand `~/` and `$HOME/` prefixes to the user's home directory.

    Mirrors opencode/permission/index.ts:271-277. Applied at parse time
    (inside `from_config`) so rules see absolute paths. Non-home prefixes
    pass through unchanged; this keeps glob patterns like `*.env` intact.
    """
    if not pattern:
        return pattern
    home = os.path.expanduser("~")
    if pattern.startswith("~/"):
        return home + pattern[1:]
    if pattern == "~":
        return home
    if pattern.startswith("$HOME/"):
        return home + pattern[5:]
    if pattern == "$HOME":
        return home
    return pattern


def _wildcard_match(subject: str, pattern: str) -> bool:
    """OpenCode-parity wildcard. Pure fnmatch — no path basename fallback.

    Callers that need basename matching (file-path rules) normalize the
    subject before calling `evaluate` and pass candidate strings explicitly,
    keeping this function shape-agnostic.
    """
    if pattern == "*" or pattern == subject:
        return True
    return fnmatch.fnmatch(subject, pattern)


def evaluate(permission: str, pattern: str, *rulesets: Ruleset) -> Rule:
    """Last-match-wins across all rulesets flattened in order.

    Returns the matching `Rule` so callers can inspect which source fired
    (used by the multi-reason BLOCKED message helper). If nothing matches,
    returns a synthetic `Rule(permission, "*", "ask")` — same default as
    OpenCode's `evaluate()`.
    """
    for ruleset in reversed(rulesets):
        for rule in reversed(ruleset):
            if _wildcard_match(permission, rule.permission) and _wildcard_match(pattern, rule.pattern):
                return rule
    return Rule(permission, "*", "ask")


def disabled(tools: list[str] | tuple[str, ...], *rulesets: Ruleset) -> set[str]:
    """Tools disabled by a universal deny rule — safe to exclude from the
    toolset exposed to the model.

    A tool is "disabled" when the last rule matching its canonical permission
    has `pattern="*"` and `action="deny"`. Subject-specific rules (e.g.
    `{"bash": {"rm -rf *": "deny"}}`) do NOT disable the tool — they only
    deny specific calls, and the model may still usefully call the tool
    for other subjects.

    Mirrors opencode/permission/index.ts:299. Used by the tool-registry
    composer at session start: a disabled tool is omitted from the toolset,
    saving tokens and preventing the model from attempting calls that
    would always fail.
    """
    result: set[str] = set()
    rules = [r for rs in rulesets for r in rs]
    for tool in tools:
        permission = canonical_permission(tool)
        last_match: Rule | None = None
        for rule in rules:
            if _wildcard_match(permission, rule.permission):
                last_match = rule
        if last_match and last_match.pattern == "*" and last_match.action == "deny":
            result.add(tool)
    return result


def from_config(raw: Any) -> Ruleset:
    """Parse aru.json/opencode.json-style permission config into a Ruleset.

    Accepts every shape the OpenCode schema accepts:

        "allow"                                       -> [Rule("*", "*", "allow")]
        {"*": "ask"}                                  -> [Rule("*", "*", "ask")]
        {"read": "allow"}                             -> [Rule("read", "*", "allow")]
        {"bash": {"*": "ask", "git *": "allow"}}      -> [Rule("bash", "*", "ask"),
                                                          Rule("bash", "git *", "allow")]
        {"task": {"explorer": "allow"}}               -> [Rule("task", "explorer", "allow")]

    Key order in the input dict is preserved in the output ruleset —
    `evaluate` iterates in reverse and returns the last match, so the
    written order is the resolution order (top = weakest, bottom = strongest).
    """
    if raw is None or raw == {}:
        return []
    if isinstance(raw, str):
        return [Rule("*", "*", _validate_action(raw))]
    if not isinstance(raw, dict):
        return []

    ruleset: Ruleset = []
    for key, value in raw.items():
        if isinstance(value, str):
            ruleset.append(Rule(key, "*", _validate_action(value)))
        elif isinstance(value, dict):
            for pattern, action in value.items():
                ruleset.append(Rule(key, _expand_pattern(pattern), _validate_action(action)))
    return ruleset


# ---------------------------------------------------------------------------
# Hardcoded defaults
# ---------------------------------------------------------------------------

CATEGORY_DEFAULTS: dict[str, PermissionAction] = {
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "edit": "ask",
    "write": "ask",
    "bash": "ask",
    # Canonical permission names (OpenCode parity). Tool names like
    # `web_search`, `web_fetch`, `delegate_task` get canonicalised at
    # resolve time via `canonical_permission` so legacy callers (and old
    # tests) continue to work — they look up the same rule by a different
    # name.
    "websearch": "allow",
    "webfetch": "allow",
    "task": "allow",
    "skill": "allow",
}

SAFE_COMMAND_PREFIXES = (
    # File/directory inspection
    "ls", "dir", "find", "tree", "cat", "head", "tail", "less", "more", "wc",
    "file", "stat", "du", "df",
    # Search
    "grep", "rg", "ag", "ack",
    # Git read-only
    "git status", "git log", "git diff", "git show", "git branch", "git tag",
    "git remote", "git stash list", "git blame", "git shortlog",
    # System info / navigation
    "cd", "echo", "pwd", "whoami", "which", "where", "type", "env", "printenv",
    "uname", "hostname", "ps", "top", "free", "uptime",
    # Language versions
    "python --version", "python3 --version", "node --version", "npm --version",
    "cargo --version", "go version", "java --version", "uv --version",
    # Sort/filter (typically piped)
    "sort", "uniq", "cut", "tr", "awk", "sed -n", "jq",
)

# Default rules for sensitive files (prepended before user rules)
_SENSITIVE_FILE_RULES: list[PermissionRule] = [
    PermissionRule("*.env", "deny"),
    PermissionRule("*.env.*", "deny"),
    PermissionRule("*.env.example", "allow"),
]

# Convert SAFE_COMMAND_PREFIXES to PermissionRules
_SAFE_BASH_RULES: list[PermissionRule] = []
for _prefix in SAFE_COMMAND_PREFIXES:
    _SAFE_BASH_RULES.append(PermissionRule(_prefix, "allow"))
    _SAFE_BASH_RULES.append(PermissionRule(f"{_prefix} *", "allow"))


# ---------------------------------------------------------------------------
# Thin wrappers over RuntimeContext (preserve public API for callers)
# ---------------------------------------------------------------------------

def set_config(config: PermissionConfig) -> None:
    get_ctx().perm_config = config


def set_skip_permissions(value: bool) -> None:
    get_ctx().skip_permissions = value


def get_skip_permissions() -> bool:
    return get_ctx().skip_permissions


def reset_session() -> None:
    """Reset session-level permission state (call between conversations)."""
    ctx = get_ctx()
    ctx.session_allowed.clear()
    ctx.last_rejection_feedback = ""


# Modes the user can cycle between with shift+tab in the REPL.
_MODE_CYCLE: tuple[str, ...] = ("default", "acceptEdits", "yolo")

MODE_LABELS: dict[str, str] = {
    "default": "manually accept edits",
    "acceptEdits": "auto-accept edits",
    "yolo": "DANGEROUSLY skip all permissions",
}


def get_permission_mode() -> str:
    return get_ctx().permission_mode


def set_permission_mode(mode: str) -> str:
    ctx = get_ctx()
    if mode not in _MODE_CYCLE:
        mode = "default"
    ctx.permission_mode = mode
    ctx.skip_permissions = (mode == "yolo")
    return mode


def cycle_permission_mode() -> str:
    """Advance to the next mode and return it."""
    ctx = get_ctx()
    try:
        idx = _MODE_CYCLE.index(ctx.permission_mode)
    except ValueError:
        idx = 0
    next_mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
    ctx.permission_mode = next_mode
    ctx.skip_permissions = (next_mode == "yolo")
    return next_mode


def consume_rejection_feedback() -> str:
    """Return and clear the most recent user-supplied rejection feedback."""
    ctx = get_ctx()
    fb = ctx.last_rejection_feedback
    ctx.last_rejection_feedback = ""
    return fb


def merge_configs(base: PermissionConfig, overlay: PermissionConfig) -> PermissionConfig:
    """Merge overlay onto base. Overlay categories fully replace base categories.

    Categories not in overlay are inherited from base.
    """
    merged_categories = dict(base.categories)
    for cat, rules in overlay.categories.items():
        merged_categories[cat] = rules
    return PermissionConfig(default=base.default, categories=merged_categories)


@contextmanager
def permission_scope(overlay_raw: dict[str, Any] | None) -> Generator[None, None, None]:
    """Temporarily overlay agent permissions on the global config.

    While inside the scope, the merged config is active. When the scope exits,
    the previous config is restored. Supports nesting (agent -> subagent).

    Each scope gets its own fresh "always" session memory, so agent approvals
    don't leak to the global scope or other agents.
    """
    if not overlay_raw:
        yield
        return

    ctx = get_ctx()
    ctx.config_stack.append(ctx.perm_config)
    ctx.session_stack.append(ctx.session_allowed)
    ctx.session_allowed = set()

    overlay = parse_permission_config(overlay_raw)
    ctx.perm_config = merge_configs(ctx.perm_config, overlay)
    try:
        yield
    finally:
        ctx.perm_config = ctx.config_stack.pop()
        ctx.session_allowed = ctx.session_stack.pop()


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def parse_permission_config(raw: Any) -> PermissionConfig:
    """Parse permission config from aru.json into a PermissionConfig.

    Supports:
        "allow"                              -> everything allowed
        {"*": "ask", "read": "allow", ...}   -> per-category with string shorthand
        {"bash": {"*": "ask", "git *": "allow"}} -> per-category with pattern rules
    """
    if raw is None or raw == {}:
        return PermissionConfig()

    if isinstance(raw, str):
        action = _validate_action(raw)
        return PermissionConfig(default=action)

    if not isinstance(raw, dict):
        return PermissionConfig()

    default: PermissionAction = "ask"
    categories: dict[str, list[PermissionRule]] = {}

    for key, value in raw.items():
        if key == "*":
            default = _validate_action(value)
            continue

        if isinstance(value, str):
            # Shorthand: "read": "allow" -> single catch-all rule
            categories[key] = [PermissionRule("*", _validate_action(value))]
        elif isinstance(value, dict):
            # Pattern rules: {"*": "ask", "git *": "allow"}
            rules: list[PermissionRule] = []
            for pattern, action in value.items():
                rules.append(PermissionRule(pattern, _validate_action(action)))
            categories[key] = rules

    return PermissionConfig(default=default, categories=categories)


def _validate_action(value: Any) -> PermissionAction:
    if isinstance(value, str) and value in VALID_ACTIONS:
        return value  # type: ignore[return-value]
    return "ask"


# ---------------------------------------------------------------------------
# Shell command splitting (moved from codebase.py)
# ---------------------------------------------------------------------------

def _shell_split(command: str, separators: tuple[str, ...]) -> list[str] | None:
    """Split command by shell operators, respecting quotes.

    Returns list of parts if any separator found, None otherwise.
    """
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    chars = command
    while i < len(chars):
        c = chars[i]
        if c == "'" and not in_double:
            in_single = not in_single
            current.append(c)
        elif c == '"' and not in_single:
            in_double = not in_double
            current.append(c)
        elif not in_single and not in_double:
            matched = False
            for sep in separators:
                if chars[i:i+len(sep)] == sep:
                    parts.append("".join(current).strip())
                    current = []
                    i += len(sep)
                    matched = True
                    break
            if matched:
                continue
            current.append(c)
        else:
            current.append(c)
        i += 1
    if parts:  # at least one separator was found
        parts.append("".join(current).strip())
        return [p for p in parts if p]
    return None


# ---------------------------------------------------------------------------
# Permission resolution
# ---------------------------------------------------------------------------

def _build_rules(category: str) -> list[PermissionRule]:
    """Build the full rule list for a category: hardcoded defaults + user config.

    Order matters: defaults first, user rules after (last-match-wins).
    """
    rules: list[PermissionRule] = []

    # Add hardcoded defaults
    if category == "bash":
        rules.extend(_SAFE_BASH_RULES)
    elif category in ("read", "edit", "write"):
        rules.extend(_SENSITIVE_FILE_RULES)

    # Add user-configured rules
    ctx = get_ctx()
    if category in ctx.perm_config.categories:
        rules.extend(ctx.perm_config.categories[category])

    return rules


def _match_rule(pattern: str, subject: str) -> bool:
    """Check if a subject matches a rule pattern.

    For bash commands, uses prefix matching (like SAFE_COMMAND_PREFIXES did).
    For file paths, uses fnmatch on the basename and full path.
    """
    if pattern == "*":
        return True
    # Try fnmatch on full subject
    if fnmatch.fnmatch(subject, pattern):
        return True
    # For file paths, also try matching against basename only
    if os.sep in subject or "/" in subject:
        basename = os.path.basename(subject)
        if fnmatch.fnmatch(basename, pattern):
            return True
    return False


def _normalize_cmd(cmd: str) -> str:
    """Normalize a command for matching: forward slashes, strip leading ./"""
    cmd = cmd.replace("\\", "/")
    if cmd.startswith("./"):
        cmd = cmd[2:]
    return cmd


def _match_bash_rule(pattern: str, command: str) -> bool:
    """Match a bash command against a rule pattern.

    Supports both prefix matching (for SAFE_COMMAND_PREFIXES compatibility)
    and fnmatch glob patterns. Normalizes slashes and ./ prefix for Windows.
    """
    if pattern == "*":
        return True
    cmd = _normalize_cmd(command.strip())
    pat = _normalize_cmd(pattern)
    # Exact match
    if cmd == pat:
        return True
    # Prefix match: "git status" matches "git status --short"
    if cmd.startswith(pat + " "):
        return True
    # fnmatch glob: "git *" matches "git status"
    if fnmatch.fnmatch(cmd, pat):
        return True
    return False


def _resolve_bash_compound(command: str) -> tuple[PermissionAction, str]:
    """Resolve permission for a potentially compound bash command.

    Splits on &&, ;, | and returns the most restrictive result.
    """
    cmd = command.strip()

    # Split on chained operators
    parts = _shell_split(cmd, ("&&", ";"))
    if parts:
        return _most_restrictive([_resolve_bash_single(p) for p in parts])

    # Split on pipes
    parts = _shell_split(cmd, ("|",))
    if parts:
        return _most_restrictive([_resolve_bash_single(p) for p in parts])

    return _resolve_bash_single(cmd)


def _resolve_bash_single(command: str) -> tuple[PermissionAction, str]:
    """Resolve permission for a single (non-compound) bash command."""
    rules = _build_rules("bash")
    result: PermissionAction = CATEGORY_DEFAULTS.get("bash", get_ctx().perm_config.default)
    matched_pattern = "*"

    for rule in rules:
        if _match_bash_rule(rule.pattern, command):
            result = rule.action
            matched_pattern = rule.pattern

    return result, matched_pattern


def _most_restrictive(
    results: list[tuple[PermissionAction, str]],
) -> tuple[PermissionAction, str]:
    """Return the most restrictive result from a list. deny > ask > allow."""
    priority = {"deny": 2, "ask": 1, "allow": 0}
    worst = results[0]
    for r in results[1:]:
        if priority[r[0]] > priority[worst[0]]:
            worst = r
    return worst


# Mapping from permission category (what resolve_permission takes) to the
# tool_name used by the unified tool-policy gate (what evaluate_tool_policy
# takes). The permission system asks about *categories* (edit, write, bash),
# while the tool-policy layer reasons about tool *names* (edit_file, bash,
# ...). This mapping lets resolve_permission consult the tool-policy layer
# consistently so that, e.g., a bash check in plan mode denies at the
# permission level too — not only at the wrapper level.
_CATEGORY_TO_REPRESENTATIVE_TOOL: dict[str, str] = {
    "edit": "edit_file",
    "write": "write_file",
    "bash": "bash",
    "delegate_task": "delegate_task",
}


def resolve_permission(
    category: str, subject: str = ""
) -> tuple[PermissionAction, str]:
    """Resolve permission for a tool action.

    Returns (action, matched_pattern).

    Algorithm:
    1. If skip_permissions -> ("allow", "*")
    2. Consult unified tool-policy gate (plan_mode / skill disallowed).
       If policy denies this category's representative tool, return
       ("deny", "tool-policy"). This is how claude-code / opencode fold
       mode-based gates into the same decision function that handles
       user rules, instead of stacking independent short-circuits.
    3. Check session_allowed for matching (category, pattern)
       -> ("allow", pattern)
    4. For bash: handle compound commands, then walk rules
    5. For others: walk rules (defaults + user config), last-match-wins
    6. Fallback: category default, then global default
    """
    ctx = get_ctx()
    if ctx.skip_permissions:
        return ("allow", "*")

    # Unified tool-policy gate — shared with the agent_factory wrapper so
    # both paths agree. A tool denied by plan_mode / skill rules is denied
    # here too; the wrapper renders the combined message for the model,
    # and this call returns a plain "deny" for the user-prompt codepath.
    rep_tool = _CATEGORY_TO_REPRESENTATIVE_TOOL.get(category)
    if rep_tool:
        from aru.tool_policy import evaluate_tool_policy
        decision = evaluate_tool_policy(rep_tool)
        if not decision.allowed:
            return ("deny", "tool-policy")

    # Canonical permission name (OpenCode parity). Tool names map to their
    # canonical permission (edit_file → "edit", delegate_task → "task",
    # web_fetch → "webfetch"); canonical names pass through unchanged. The
    # user's aru.json keys by the canonical name, so this mapping is what
    # makes `{"task": {"explorer": "allow"}}` work for `delegate_task` and
    # `{"edit": "ask"}` cover both edit_file and edit_files.
    canonical = canonical_permission(category)

    # "Accept edits" mode auto-allows edit/write categories for the session.
    if ctx.permission_mode == "acceptEdits" and canonical in ("edit", "write"):
        return ("allow", "*")

    # Check session memory (also keyed by canonical name so "always allow"
    # granted for one tool in a family covers the whole family).
    for cat, pattern in ctx.session_allowed:
        if cat == canonical and _match_rule(pattern, subject):
            return ("allow", pattern)

    # Bash has special compound command handling
    if canonical == "bash":
        return _resolve_bash_compound(subject)

    # All other categories
    rules = _build_rules(canonical)
    result: PermissionAction = CATEGORY_DEFAULTS.get(canonical, ctx.perm_config.default)
    matched_pattern = "*"

    for rule in rules:
        if _match_rule(rule.pattern, subject):
            result = rule.action
            matched_pattern = rule.pattern

    return result, matched_pattern


# ---------------------------------------------------------------------------
# Permission gate (user-facing prompt)
# ---------------------------------------------------------------------------

def _fire_permission_hook(mgr, category: str, subject: str) -> bool | None:
    """Fire permission.ask hook through all plugin handlers.

    Supports both sync and async handlers. Returns True/False if a handler
    sets event.data["allow"], or None if no handler overrode the decision.

    Async handlers dispatched in a worker thread carry a copied
    contextvars.Context so plugin code can still call `get_ctx()` and
    other contextvar-backed helpers — without the copy, the new
    `asyncio.run` loop would see an empty context and break handlers
    that rely on the runtime.
    """
    import asyncio
    import contextvars
    from aru.plugins.hooks import HookEvent

    evt = HookEvent(hook="permission.ask", data={"category": category, "subject": subject})

    for hooks_obj in mgr._hooks:
        for handler in hooks_obj.get_handlers("permission.ask"):
            try:
                if asyncio.iscoroutinefunction(handler):
                    # Async handler — run via the event loop
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop is not None:
                        # A loop is running in this thread; we cannot call
                        # run_until_complete. Dispatch to a worker thread
                        # with the current contextvars snapshot so the
                        # handler sees the same RuntimeContext.
                        import concurrent.futures
                        snapshot = contextvars.copy_context()
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            pool.submit(snapshot.run, asyncio.run, handler(evt)).result(timeout=5)
                    else:
                        asyncio.run(handler(evt))
                else:
                    handler(evt)
            except Exception:
                continue  # skip broken handlers

            if "allow" in evt.data:
                return bool(evt.data["allow"])

    return None  # no handler overrode


def _resolve_many(category: str, subjects: list[str]) -> list[tuple[PermissionAction, str]]:
    """Resolve permission for each subject independently.

    Returns list of (action, pattern) — one per input subject, in the same
    order. Callers combine via any-deny-wins / any-ask-prompts semantics.
    """
    return [resolve_permission(category, s) for s in subjects]


def check_permission(
    category: str,
    subject: str | list[str],
    display_details: str | Text | Group,
) -> bool:
    """Check permission and prompt user if needed.

    `subject` may be a single string (legacy path) or a list of subjects
    (multi-pattern check for batch tools like `write_files`/`edit_files`).
    For the list form: any deny → return False; all allow → return True;
    any ask → one prompt covering the whole batch. Mirrors OpenCode's
    `ask({patterns: string[]})` atomic semantics — one tool call, one
    decision, covering every affected pattern.

    Returns True if allowed, False if denied.
    """
    # Normalise to list; track whether caller used the legacy single-subject
    # form for title rendering later.
    if isinstance(subject, list):
        subjects = list(subject) if subject else [""]
        display_subject = ", ".join(s for s in subjects if s) or ""
    else:
        subjects = [subject]
        display_subject = subject

    results = _resolve_many(category, subjects)

    # any-deny — stop immediately, do not prompt. Honor the most restrictive
    # decision across the batch so batch tools can't smuggle a denied path
    # through by mixing it with allowed ones.
    if any(action == "deny" for action, _ in results):
        return False
    if all(action == "allow" for action, _ in results):
        return True

    # At least one subject is "ask" — prompt once, covering the whole batch.
    # Fire permission.ask hook — plugins can override the decision.
    # check_permission runs in a sync context (called from tool threads),
    # so we fire sync handlers directly and async handlers via the event loop.
    ctx = get_ctx()
    mgr = getattr(ctx, "plugin_manager", None)
    if mgr is not None and getattr(mgr, "loaded", False):
        try:
            override = _fire_permission_hook(mgr, category, display_subject)
            if override is not None:
                return override
        except Exception:
            pass  # never let plugin errors block permissions

    # action == "ask" -> prompt user
    with ctx.permission_lock:
        # Re-check after acquiring lock (another thread may have resolved it)
        results2 = _resolve_many(category, subjects)
        if any(action == "deny" for action, _ in results2):
            return False
        if all(action == "allow" for action, _ in results2):
            return True

        # Pause Live and flush already-streamed content
        if ctx.live:
            ctx.live.stop()
        if ctx.display:
            ctx.display.flush()

        title = f"{category}: {display_subject}" if display_subject else category
        ctx.console.print()
        ctx.console.print(Panel(
            display_details,
            title=f"[bold yellow]{title}[/bold yellow]",
            border_style="yellow",
            expand=False,
        ))

        is_edit = category in ("edit", "write")
        if is_edit:
            options = [
                "Yes",
                "Yes, and auto-accept edits (shift+tab)",
                "No, and tell Aru what to do differently",
            ]
            reject_index = 2  # "No" option
        else:
            options = [
                "Yes",
                "No, and tell Aru what to do differently",
            ]
            reject_index = 1

        # Arrow-key menu — pauses stdin during render, returns the chosen
        # index (or reject_index on cancel so Esc/Ctrl+C behaves like "No").
        choice = select_option(
            options,
            title="Choose an option (↑↓ to move, Enter to confirm):",
            default=0,
            cancel_value=reject_index,
        )

        if choice == 0:
            allowed = True
        elif is_edit and choice == 1:
            ctx.permission_mode = "acceptEdits"
            ctx.console.print(
                "[dim]Auto-accept edits enabled for this session (shift+tab to toggle).[/dim]"
            )
            allowed = True
        else:
            # Rejection path — optionally collect feedback for the model.
            # Catch BaseException so tests and Ctrl+C during feedback don't crash.
            try:
                feedback = ctx.console.input(
                    "[bold yellow]Tell Aru what to do differently (enter to skip):[/bold yellow] "
                ).strip()
            except BaseException:
                feedback = ""
            if feedback:
                ctx.last_rejection_feedback = feedback
            allowed = False

        # Resume Live display
        if ctx.live:
            ctx.live.start()
            ctx.live._live_render._shape = None

        return allowed

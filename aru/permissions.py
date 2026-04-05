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
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator, Literal

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

PermissionAction = Literal["allow", "ask", "deny"]

VALID_ACTIONS: set[str] = {"allow", "ask", "deny"}


@dataclass
class PermissionRule:
    pattern: str
    action: PermissionAction


@dataclass
class PermissionConfig:
    default: PermissionAction = "ask"
    categories: dict[str, list[PermissionRule]] = field(default_factory=dict)


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
    "web_search": "allow",
    "web_fetch": "allow",
    "delegate_task": "allow",
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
# Module-level state
# ---------------------------------------------------------------------------

_config: PermissionConfig = PermissionConfig()
_session_allowed: set[tuple[str, str]] = set()  # (category, pattern) approved via "always"
_skip_permissions: bool = False
_permission_lock = threading.Lock()
_live = None
_display = None
_console = Console()


# ---------------------------------------------------------------------------
# Setters
# ---------------------------------------------------------------------------

def set_config(config: PermissionConfig):
    global _config
    _config = config


def set_skip_permissions(value: bool):
    global _skip_permissions
    _skip_permissions = value


def get_skip_permissions() -> bool:
    return _skip_permissions


def set_live(live):
    global _live
    _live = live


def set_display(display):
    global _display
    _display = display


def set_console(console: Console):
    global _console
    _console = console


def reset_session():
    """Reset session-level permission state (call between conversations)."""
    _session_allowed.clear()


# ---------------------------------------------------------------------------
# Agent-level permission scoping
# ---------------------------------------------------------------------------

_config_stack: list[PermissionConfig] = []
_session_stack: list[set[tuple[str, str]]] = []


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
    global _config, _session_allowed
    if not overlay_raw:
        yield
        return

    _config_stack.append(_config)
    _session_stack.append(_session_allowed)
    _session_allowed = set()

    overlay = parse_permission_config(overlay_raw)
    _config = merge_configs(_config, overlay)
    try:
        yield
    finally:
        _config = _config_stack.pop()
        _session_allowed = _session_stack.pop()


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
    if category in _config.categories:
        rules.extend(_config.categories[category])

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
    result: PermissionAction = CATEGORY_DEFAULTS.get("bash", _config.default)
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


def resolve_permission(
    category: str, subject: str = ""
) -> tuple[PermissionAction, str]:
    """Resolve permission for a tool action.

    Returns (action, matched_pattern).

    Algorithm:
    1. If skip_permissions -> ("allow", "*")
    2. Check session_allowed for matching (category, pattern) -> ("allow", pattern)
    3. For bash: handle compound commands, then walk rules
    4. For others: walk rules (defaults + user config), last-match-wins
    5. Fallback: category default, then global default
    """
    if _skip_permissions:
        return ("allow", "*")

    # Check session memory
    for cat, pattern in _session_allowed:
        if cat == category and _match_rule(pattern, subject):
            return ("allow", pattern)

    # Bash has special compound command handling
    if category == "bash":
        return _resolve_bash_compound(subject)

    # All other categories
    rules = _build_rules(category)
    result: PermissionAction = CATEGORY_DEFAULTS.get(category, _config.default)
    matched_pattern = "*"

    for rule in rules:
        if _match_rule(rule.pattern, subject):
            result = rule.action
            matched_pattern = rule.pattern

    return result, matched_pattern


# ---------------------------------------------------------------------------
# Permission gate (user-facing prompt)
# ---------------------------------------------------------------------------

def check_permission(
    category: str,
    subject: str,
    display_details: str | Text | Group,
) -> bool:
    """Check permission and prompt user if needed.

    Returns True if allowed, False if denied.
    """
    action, matched_pattern = resolve_permission(category, subject)

    if action == "allow":
        return True
    if action == "deny":
        return False

    # action == "ask" -> prompt user
    with _permission_lock:
        # Re-check after acquiring lock (another thread may have resolved it)
        action2, pattern2 = resolve_permission(category, subject)
        if action2 == "allow":
            return True
        if action2 == "deny":
            return False

        # Pause Live and flush already-streamed content
        if _live:
            _live.stop()
        if _display:
            _display.flush()

        title = f"{category}: {subject}" if subject else category
        _console.print()
        _console.print(Panel(
            display_details,
            title=f"[bold yellow]{title}[/bold yellow]",
            border_style="yellow",
            expand=False,
        ))
        try:
            answer = _console.input(
                "[bold yellow]Allow? (y)es once / (a)lways / (n)o:[/bold yellow] "
            ).strip().lower()
            if answer in ("a", "always", "all"):
                _session_allowed.add((category, matched_pattern))
                allowed = True
            else:
                allowed = answer in ("y", "yes", "s", "sim")
        except (EOFError, KeyboardInterrupt):
            allowed = False

        # Resume Live display
        if _live:
            _live.start()
            _live._live_render._shape = None

        return allowed

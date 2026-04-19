"""Unified tool-policy evaluation.

Single decision point for whether a tool call may proceed. Plan-mode
and active-skill `disallowed_tools` are expressed as `Ruleset`s and
composed through the same `evaluate()` function used by user/config
rules. Reason collection wraps `evaluate()` per source so the combined
BLOCKED message can name each gate that fired — one coherent block
instead of two contradictory BLOCKED strings in sequence.

Opencode parity: `plan_mode_rules()` and `skill_rules()` return
`Ruleset` — identical shape to user config rules. `evaluate_tool_policy`
is a thin wrapper that attributes each deny to its source.
Claude-code parity: `PolicyDecision.reasons` is a tagged list so
diagnostics name which rule fired — analogous to
`PermissionDecision.decisionReason` in claude-code/permissions.ts.

Contract:
    evaluate_tool_policy(tool_name) -> PolicyDecision
        .allowed: True when the call may proceed.
        .reasons: ordered list of PolicyReason objects when denied.
        .message: LLM-facing explanation combining all reasons into one
                  coherent block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from aru.permissions import Rule, Ruleset, evaluate

# Tool names whose execution is blocked while a plan-mode session is
# awaiting user approval. Read-only tools (read/glob/grep/list/...) are
# deliberately NOT here — the planning agent needs them to research and
# write the plan. Mirrors claude-code's mode-based tool filtering.
PLAN_MODE_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "edit_file",
    "edit_files",
    "write_file",
    "write_files",
    "bash",
    "delegate_task",
})

# Tools that MUST remain callable regardless of any other policy: without
# them the agent has no way to change mode or reach permission categories.
# Matches claude-code's `EnterPlanMode.isEnabled()` / `ExitPlanMode` pair
# invariant — "plan mode is not a trap the model can enter but never leave".
ALWAYS_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "exit_plan_mode",
})


PolicyReasonType = Literal["plan_mode", "skill_disallowed"]


@dataclass(frozen=True)
class PolicyReason:
    """A single reason a tool call was denied.

    `type` is the tag — downstream code may branch on it (logging,
    analytics). `message` is the human-readable fragment that goes into
    the combined BLOCKED string.
    """
    type: PolicyReasonType
    message: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of a tool-policy evaluation.

    `allowed=True` means every gate agreed to let the call pass.
    `allowed=False` means at least one gate denied; `reasons` lists them
    in evaluation order and `message` is the single string that should
    be surfaced to the LLM. When multiple gates fire, the message
    explains all of them so the agent sees one coherent instruction
    rather than two contradictory BLOCKED replies in a row.
    """
    allowed: bool
    reasons: tuple[PolicyReason, ...] = ()
    message: str = ""


def _plan_mode_reason(tool_name: str) -> PolicyReason:
    return PolicyReason(
        type="plan_mode",
        message=(
            f"plan mode is active — mutating/executing tool `{tool_name}` "
            "is blocked until the plan is approved. Finish writing the "
            "plan as your next assistant message and call "
            "exit_plan_mode(plan=<full plan text>) to request approval."
        ),
        detail={"tool_name": tool_name},
    )


def _skill_disallowed_reason(tool_name: str, skill_name: str) -> PolicyReason:
    return PolicyReason(
        type="skill_disallowed",
        message=(
            f"tool `{tool_name}` is disallowed by the active skill "
            f"`{skill_name}`. Read the skill's SKILL.md for the prescribed "
            "alternative (commonly: write the output to a `.md` file via "
            "`write_file` instead of using in-session state)."
        ),
        detail={"tool_name": tool_name, "skill": skill_name},
    )


def _render_message(tool_name: str, reasons: tuple[PolicyReason, ...]) -> str:
    """Combine one or more reasons into the single BLOCKED string sent to
    the LLM. Format mirrors the original single-reason messages so agents
    already trained on them keep parsing correctly, but when multiple
    reasons fire they appear as a numbered list under one BLOCKED header
    with a single 'Do NOT retry' instruction at the end."""
    if len(reasons) == 1:
        return (
            f"BLOCKED: {reasons[0].message} "
            f"Do NOT retry `{tool_name}`."
        )
    bullets = "\n".join(f"  {i + 1}. {r.message}" for i, r in enumerate(reasons))
    return (
        f"BLOCKED: tool `{tool_name}` is denied by multiple rules:\n"
        f"{bullets}\n"
        f"Address all of the above before retrying. Do NOT retry "
        f"`{tool_name}` until each blocker is cleared."
    )


def plan_mode_rules(session) -> Ruleset:
    """Ruleset expressing plan-mode denials. Empty when plan mode is off.

    Every mutating tool in `PLAN_MODE_BLOCKED_TOOLS` becomes a `deny`
    rule — composed alongside user rules via `evaluate()`. Read-only
    tools are deliberately absent so the planning agent can research.
    """
    if session is None or not getattr(session, "plan_mode", False):
        return []
    return [Rule(tool, "*", "deny") for tool in PLAN_MODE_BLOCKED_TOOLS]


def _active_skill_name(session, agent_id) -> str | None:
    """Resolve the current scope's active skill.

    `session.get_active_skill(agent_id)` is the authoritative form when
    present (supports per-subagent scope via C3). Falls back to the
    module-level `active_skill` attribute for older session shapes.
    """
    if session is None:
        return None
    getter = getattr(session, "get_active_skill", None)
    if callable(getter):
        return getter(agent_id)
    return getattr(session, "active_skill", None)


def skill_rules(session, config, agent_id) -> Ruleset:
    """Ruleset derived from the active skill's `disallowed_tools` list.

    Returns `[]` when no skill is active or no tools are disallowed.
    Otherwise produces one `deny` rule per tool so the decision path
    matches plan mode and user rules.
    """
    if session is None or config is None:
        return []
    active = _active_skill_name(session, agent_id)
    if not active:
        return []
    skills = getattr(config, "skills", None) or {}
    skill_obj = skills.get(active)
    if skill_obj is None:
        return []
    disallowed = getattr(skill_obj, "disallowed_tools", None) or []
    return [Rule(tool, "*", "deny") for tool in disallowed]


def evaluate_tool_policy(tool_name: str) -> PolicyDecision:
    """Evaluate whether `tool_name` may be called in the current context.

    Each gate is evaluated against its own `Ruleset` via the same
    `evaluate()` function that resolves user rules — unifying the
    decision primitive. When multiple gates fire, each contributes a
    `PolicyReason` so the combined message names every blocker.

    When no `RuntimeContext` is installed (e.g. unit tests that construct
    the wrapper directly without `init_ctx`), returns `allowed=True`.
    """
    # Always-allowed tools bypass all policy — exit_plan_mode must never
    # be denied or plan mode becomes a trap.
    if tool_name in ALWAYS_ALLOWED_TOOLS:
        return PolicyDecision(allowed=True)

    try:
        from aru.runtime import get_ctx
        ctx = get_ctx()
    except LookupError:
        return PolicyDecision(allowed=True)

    session = getattr(ctx, "session", None)
    config = getattr(ctx, "config", None)
    agent_id = getattr(ctx, "agent_id", None)

    reasons: list[PolicyReason] = []

    if evaluate(tool_name, "*", plan_mode_rules(session)).action == "deny":
        reasons.append(_plan_mode_reason(tool_name))

    skill_rs = skill_rules(session, config, agent_id)
    if evaluate(tool_name, "*", skill_rs).action == "deny":
        active = _active_skill_name(session, agent_id)
        reasons.append(_skill_disallowed_reason(tool_name, active))

    if not reasons:
        return PolicyDecision(allowed=True)

    return PolicyDecision(
        allowed=False,
        reasons=tuple(reasons),
        message=_render_message(tool_name, tuple(reasons)),
    )

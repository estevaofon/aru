"""Unified tool-policy evaluation.

Single decision point for whether a tool call may proceed. Consolidates
gates that were previously scattered across the agent_factory wrapper
(plan mode, active-skill disallowed_tools). The same function is called
by the tool wrapper *and* by per-tool permission checks inside
`resolve_permission`, so both paths see a consistent answer and one
coherent message.

Opencode parity: mirrors the single-function `Permission.evaluate(...)`
pattern (permission/index.ts:133) where multiple rule sources are
composed into one decision instead of independent short-circuit gates.
Claude-code parity: `PolicyDecision` carries a tagged `reason` so
diagnostics name which rule fired — analogous to `PermissionDecision`'s
`decisionReason` in claude-code/permissions.ts.

Contract:
    evaluate_tool_policy(tool_name) -> PolicyDecision
        .allowed: True when the call may proceed.
        .reasons: ordered list of PolicyReason objects when denied.
        .message: LLM-facing explanation combining all reasons into one
                  coherent block (never two contradictory BLOCKED strings
                  in a row on the wire).

This module reads the current `RuntimeContext`, `Session`, and
`AgentConfig` via `aru.runtime.get_ctx()` — callers don't pass them
explicitly so the surface stays small and the same function can be
invoked from the wrapper and from the permission layer without wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

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


def evaluate_tool_policy(tool_name: str) -> PolicyDecision:
    """Evaluate whether `tool_name` may be called in the current context.

    Inspects the active `RuntimeContext`, its `Session`, and `AgentConfig`
    and returns a single decision. When no `RuntimeContext` is installed
    (e.g. unit tests that construct the wrapper directly without
    `init_ctx`), returns `allowed=True` — the ctx-less environment is
    equivalent to "no policy configured", matching the wrapper's prior
    defensive behavior.
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

    # Plan-mode gate. Reads from session.plan_mode (authoritative flag).
    # Only mutating tools in PLAN_MODE_BLOCKED_TOOLS are affected; the
    # planning agent still needs read/search tools freely.
    if (
        session is not None
        and getattr(session, "plan_mode", False)
        and tool_name in PLAN_MODE_BLOCKED_TOOLS
    ):
        reasons.append(_plan_mode_reason(tool_name))

    # Active-skill gate. Each agent scope carries its own active_skill slot
    # (C3) — a subagent with its own ctx.agent_id does not inherit the
    # parent's. The gate consults that scope's skill and its
    # `disallowed_tools` frontmatter.
    if session is not None and config is not None:
        getter = getattr(session, "get_active_skill", None)
        if callable(getter):
            active = getter(agent_id)
        else:
            active = getattr(session, "active_skill", None)
        skills = getattr(config, "skills", None) or {}
        skill_obj = skills.get(active) if active else None
        disallowed = getattr(skill_obj, "disallowed_tools", None) or []
        if tool_name in disallowed:
            reasons.append(_skill_disallowed_reason(tool_name, active))

    if not reasons:
        return PolicyDecision(allowed=True)

    return PolicyDecision(
        allowed=False,
        reasons=tuple(reasons),
        message=_render_message(tool_name, tuple(reasons)),
    )

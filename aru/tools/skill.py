"""Skill invocation tool.

Enables the agent to dynamically load another skill's SKILL.md content into
the next turn's context. This is the primary mechanism for multi-skill
workflows (e.g. superpowers: brainstorming -> writing-plans -> executing-plans)
where the agent itself drives the transition rather than requiring the user
to re-type slash commands.

Flow:
  Agent calls: invoke_skill("writing-plans", "docs/aru/specs/x.md")
    |
    v
  Tool looks up skill in ctx.config.skills
    |
    v
  Renders SKILL.md body via render_skill_template (applies $ARGUMENTS / $1 / $2)
    |
    v
  Returns framed content as tool_result. Agno includes it in the next LLM
  call's context as a natural tool_result message.

Scope:
  - Included in ALL_TOOLS / GENERAL_TOOLS / EXECUTOR_TOOLS for primary agents.
  - Excluded from _DEFAULT_SUBAGENT_TOOLS / PLANNER_TOOLS / EXPLORER_TOOLS.
  - Excluded from _PLAN_MODE_BLOCKED_TOOLS (loads text only; mutating tools
    stay blocked independently).
"""

from __future__ import annotations

from typing import Any

from aru.runtime import get_ctx


_BASE_DOC = """Load another skill's instructions into your next turn's context.

Use this when the current skill's terminal state is reached and the user has
approved moving to the next workflow phase. The tool fetches the target
SKILL.md and returns its full body as the result of this tool call. The
agent (you) then follows those instructions on the next turn.

Prefer `invoke_skill` over improvising from memory. Skill bodies contain
gates, checklists, and red-flags tables that do NOT live in your running
context unless you load them explicitly.

Args:
    name: The skill name, with or without leading slash. E.g. "writing-plans" or "/writing-plans".
    arguments: Optional argument string. Substituted into the SKILL.md via $ARGUMENTS, $1, $2 (agentskills.io style).
"""


def invoke_skill(name: str, arguments: str = "") -> str:
    """Load another skill's instructions into the next turn's context."""
    ctx = get_ctx()
    config = getattr(ctx, "config", None)
    if config is None:
        return (
            "Error: no skill config available in runtime context. "
            "This usually means Aru was started in a mode that did not load config. "
            "Report this to the user; continue without calling invoke_skill."
        )

    # Normalise the name: strip leading slash, whitespace
    cleaned = (name or "").strip().lstrip("/")
    if not cleaned:
        return "Error: invoke_skill requires a non-empty `name`. Example: invoke_skill(name=\"writing-plans\")."

    skills = getattr(config, "skills", None) or {}
    skill = skills.get(cleaned)

    if skill is None:
        available = sorted(
            k for k, s in skills.items()
            if not getattr(s, "disable_model_invocation", False)
        )
        listing = ", ".join(f"`{n}`" for n in available) if available else "(none)"
        return (
            f"Error: skill not found: {cleaned!r}. "
            f"Available skills: {listing}. "
            "Check for typos; skill names use hyphens and do not start with a slash in the `name` argument."
        )

    if getattr(skill, "disable_model_invocation", False):
        return (
            f"Error: skill {cleaned!r} is marked disable_model_invocation=true and cannot be loaded via invoke_skill. "
            "Ask the user to type the slash command instead."
        )

    # Reset the task_store so the new skill's Entering gate can call
    # create_task_list without being refused ("Task list already created").
    # Any checklist from the previous skill is, by design, stale when we
    # cross a skill boundary — this is documented in the framing below.
    try:
        ctx.task_store.reset()
    except Exception:
        # Defensive: if task_store is missing for any reason, continue —
        # the skill content will still be returned correctly.
        pass

    # Mark this skill as active so the tool wrapper can enforce the skill's
    # disallowed_tools list. Scoped by ctx.agent_id — a subagent invoking a
    # skill does NOT change the primary agent's active-skill slot, and
    # vice versa. Mirrors claude-code's composite `${agentId}:${skillName}`
    # keying (state.ts:1516).
    session = getattr(ctx, "session", None)
    agent_id = getattr(ctx, "agent_id", None)
    if session is not None:
        session.set_active_skill(agent_id, cleaned)

    # Render the skill body with argument substitution (reuse existing helper)
    from aru.config import render_skill_template
    rendered = render_skill_template(skill.content or "", arguments or "")

    # Record so the skill body survives compaction — mirror of claude-code's
    # addInvokedSkill. We store the rendered content (post-argument substitution)
    # so post-compact restoration matches what the model initially read.
    if session is not None:
        session.record_invoked_skill(
            cleaned, rendered, getattr(skill, "source_path", "") or "",
            agent_id=agent_id,
        )

    args_display = arguments.strip() if arguments and arguments.strip() else "(none)"
    framed = (
        f"[Skill loaded: /{cleaned}]\n"
        f"[Arguments: {args_display}]\n"
        f"\n"
        f"<SKILL_CONTENT>\n"
        f"{rendered}\n"
        f"</SKILL_CONTENT>\n"
        f"\n"
        f"You have now loaded /{cleaned}. Follow its instructions precisely for this and subsequent "
        f"turns until the skill's terminal state is reached.\n\n"
        f"The task_store has been RESET as part of the skill transition — any prior checklist is gone. "
        f"If the skill has an \"Entering This Skill\" gate or a checklist template, call create_task_list "
        f"now with the NEW skill's items (not the previous skill's). create_task_list will succeed."
    )
    return framed


invoke_skill.__doc__ = _BASE_DOC


def _update_invoke_skill_docstring(skills: dict[str, Any] | None) -> None:
    """Enumerate available skills at the bottom of invoke_skill's docstring.

    Mirrors the pattern in `aru.tools.delegate._update_delegate_task_docstring`.
    Called from cli.py after config is loaded so the LLM-facing schema for this
    tool lists the skills actually present on disk.

    Args:
        skills: Dict mapping skill name to a Skill dataclass (from `aru.config`).
            Pass `config.skills`. May be None or empty; in that case only the
            base doc is kept.
    """
    doc = _BASE_DOC
    if skills:
        # Only list skills the model is allowed to invoke
        visible = [
            (name, s) for name, s in skills.items()
            if not getattr(s, "disable_model_invocation", False)
        ]
        if visible:
            doc += "\n\nAvailable skills (pass the name without the leading slash):\n"
            for name, s in sorted(visible, key=lambda kv: kv[0]):
                desc = (getattr(s, "description", "") or "").strip()
                # Truncate each skill description to keep the schema compact
                if len(desc) > 140:
                    desc = desc[:137] + "..."
                doc += f'  - name="{name}": {desc}\n'
    invoke_skill.__doc__ = doc

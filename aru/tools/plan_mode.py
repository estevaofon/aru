"""Plan-mode tools — agent-invokable entry and exit for Aru's planning flow.

Plan mode is a **session-level flag**, not a nested agent run. Entering it
only flips `session.plan_mode = True`; the build agent continues in the
same loop, writes the plan as its next assistant message, and calls
`exit_plan_mode(plan=...)` to request user approval. While the flag is on,
`aru.agent_factory._PLAN_MODE_BLOCKED_TOOLS` blocks edit/write/bash/
delegate_task at the tool wrapper, so the agent cannot accidentally
execute side effects mid-plan.

This design replaces an earlier implementation that invoked the planner
agent via a nested `runner.prompt(...)` call. That created a second `Live`
render context on top of the outer turn's Live, and collided with any
concurrent permission prompt — producing a deadlock whenever the build
agent dispatched `enter_plan_mode` in the same parallel tool batch as a
`bash` or `edit_file`. The flag-flip design eliminates the nested runner
entirely, matching how Claude Code's EnterPlanMode tool works.

The `/plan <task>` slash command is separate — it still runs the planner
agent directly from `aru/commands.py`, because that path is user-initiated
(no outer `Live` to collide with) and benefits from the planner's
specialized read-only tool set and instructions.
"""

from __future__ import annotations

import asyncio
import sys

from rich.panel import Panel

from aru.runtime import get_ctx


def _prompt_plan_approval(plan_steps: list, n_steps: int) -> tuple[bool, str]:
    """Show the plan panel and ask the user to approve execution.

    Returns (approved, feedback). If the user types free text instead of y/n,
    it is treated as feedback that the agent should use to adjust course.
    Non-interactive sessions (no TTY) auto-approve.
    """
    # Auto-approve in non-interactive sessions — there's nobody to answer.
    if not sys.stdin.isatty():
        return True, ""

    from aru.tools.tasklist import _render_plan_steps

    ctx = get_ctx()

    if ctx.live:
        ctx.live.stop()
    if ctx.display:
        try:
            ctx.display.flush()
        except Exception:
            pass

    # Build the approval panel as a single renderable so it survives the
    # REPL/TUI split. The TUI modal renders this inside the ChoiceModal;
    # the REPL ReplUI prints it above the menu.
    from rich.console import Group
    details_parts: list = []
    if plan_steps:
        details_parts.append(_render_plan_steps(plan_steps))
    details_parts.append(Panel(
        f"Proposed plan with [bold]{n_steps}[/bold] step(s). Approve execution?",
        title="[bold cyan]Plan approval[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))
    details = Group(*details_parts)

    from aru.permissions import _resolve_ui
    ui = _resolve_ui(ctx)
    options = [
        "Approve and execute",
        "Reject (revise with feedback)",
        "Reject (no feedback)",
    ]

    try:
        choice = ui.ask_choice(
            options,
            title="Plan approval:",
            default=0,
            cancel_value=2,  # bare reject on Esc/Ctrl+C
            details=details,
        )
    finally:
        if ctx.live:
            try:
                ctx.live.start()
                ctx.live._live_render._shape = None
            except Exception:
                pass

    if choice == 0:
        return True, ""
    if choice == 1:
        # Collect free-text feedback for the model to revise against.
        try:
            feedback = ui.ask_text(
                "[bold cyan]Tell Aru what to revise:[/bold cyan] ",
                default="",
            ).strip()
        except BaseException:
            feedback = ""
        return False, feedback
    return False, ""


async def enter_plan_mode() -> str:
    """Enter plan mode — a read-only session flag that blocks mutating tools.

    Call this as your FIRST action when the user asks you to "plan",
    "planeje", "think through", "propose", or when you're about to make
    3+ coordinated changes across files. After calling, write a structured
    plan as your next assistant message, then call
    `exit_plan_mode(plan=<full plan text>)` to request user approval.

    While plan mode is active, these tools return a BLOCKED error and must
    not be retried: edit_file(s), write_file(s), bash, delegate_task.

    Read-only tools (read_file, glob_search, grep_search, list_directory,
    web_search, web_fetch, rank_files) remain usable so you can research
    before writing the plan.

    IMPORTANT: plan mode is a PRE-EXECUTION gate. Do NOT call it after you
    have already made changes in this turn. If you already edited files,
    describe what you did as text; do not retroactively "plan" completed work.

    Returns instructions for what to do next.
    """
    ctx = get_ctx()
    session = ctx.session
    if session is None:
        return "Error: enter_plan_mode requires an active session."

    if session.plan_mode:
        return (
            "Already in plan mode. Finish writing the plan as your next "
            "assistant message, then call exit_plan_mode(plan=<full plan text>)."
        )

    # Any stale plan_steps from a previous plan should not leak into this
    # new planning session — the agent hasn't produced the new plan yet.
    # clear_plan() does NOT touch plan_mode, so order doesn't matter here.
    session.clear_plan()
    session.plan_mode = True

    return (
        "Plan mode active. Mutating tools (edit_file, write_file, bash, "
        "delegate_task) are now blocked. Research with read-only tools if "
        "needed, then write a structured plan as your next assistant "
        "message in this format:\n\n"
        "## Goal\n<one-line goal>\n\n"
        "## Steps\n1. <action>\n2. <action>\n3. <action>\n\n"
        "## Files\n- path/to/file1\n- path/to/file2\n\n"
        "When the plan is ready, call exit_plan_mode(plan=<full plan text>) "
        "to request user approval. Do NOT execute any step until approved."
    )


async def exit_plan_mode(plan: str) -> str:
    """Exit plan mode and request user approval to execute the plan.

    Call this AFTER writing the full plan as an assistant message. The user
    is shown the plan and asked to approve. On approval, plan mode clears
    and mutating tools become usable again — execute the steps in order,
    calling update_plan_step(index, 'completed') after each. On rejection,
    plan mode stays active so you can revise and call exit_plan_mode again
    with the updated plan.

    Args:
        plan: The full plan text. Shown to the user in the approval panel
            and parsed into structured steps for progress tracking.
    """
    ctx = get_ctx()
    session = ctx.session
    if session is None:
        return "Error: exit_plan_mode requires an active session."
    if not session.plan_mode:
        return (
            "Error: not in plan mode. Call enter_plan_mode() first if the "
            "user asked for a plan. Otherwise proceed normally."
        )

    plan_text = (plan or "").strip()
    if not plan_text:
        return (
            "Error: exit_plan_mode requires a non-empty plan. Write the plan "
            "first, then call exit_plan_mode(plan=<full plan text>)."
        )

    # Parse steps for progress tracking + render. `set_plan` populates
    # session.plan_steps from the plan text.
    task_label = plan_text.split("\n", 1)[0][:80] if plan_text else "plan"
    session.set_plan(task=task_label, plan_content=plan_text)
    n_steps = len(session.plan_steps)

    # ``exit_plan_mode`` is an async tool, so ``agent_factory`` awaits it
    # directly on the event-loop thread (no ``_thread_tool`` worker hop like
    # sync tools get). ``_prompt_plan_approval`` is synchronous and in TUI
    # mode invokes ``TuiUI.ask_choice`` → ``App.call_from_thread``, which
    # raises "must run in a different thread from the app" when called from
    # the loop thread it targets. Hop to a worker thread first so the modal
    # dispatch path matches the sync-tool and runner-auto-exit paths
    # (``runner.py`` does the same). ``asyncio.to_thread`` copies the
    # contextvars snapshot, so ``get_ctx()`` inside still resolves.
    approved, feedback = await asyncio.to_thread(
        _prompt_plan_approval, session.plan_steps, n_steps
    )

    # The approval prompt already rendered the plan panel inline, so suppress
    # the runner's coalesced end-of-batch render to avoid a duplicate.
    session._plan_render_pending = False

    if approved:
        session.plan_mode = False
        if n_steps > 0:
            return (
                f"User approved the plan ({n_steps} step(s)). Plan mode is "
                f"now OFF — mutating tools are unlocked. Execute the steps "
                f"in order and call update_plan_step(index, 'completed') "
                f"after each.\n\n--- PLAN ---\n{plan_text}"
            )
        return (
            f"User approved. Plan mode is now OFF — mutating tools are "
            f"unlocked. Execute the work described:\n\n--- PLAN ---\n{plan_text}"
        )

    # Rejection keeps plan mode ON so the agent can revise without re-entering.
    # clear_plan() wipes plan_steps but does not touch plan_mode.
    session.clear_plan()
    if feedback:
        return (
            f"User rejected the plan with this feedback:\n\n  {feedback}\n\n"
            f"You are STILL in plan mode. Revise the plan based on the "
            f"feedback, write the updated plan as your next message, and "
            f"call exit_plan_mode(plan=<revised plan>) again. Do NOT execute."
        )
    return (
        "User rejected the plan. You are still in plan mode. Ask the user "
        "what they would like changed, revise the plan, and call "
        "exit_plan_mode(plan=<revised plan>) again. Do NOT execute anything."
    )

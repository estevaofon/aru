"""Agent execution: catalog-driven entry point, streaming, plan reminder injection."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from aru.display import (
    StatusBar,
    StreamingDisplay,
    _format_tool_label,
    console,
)


# Categories of tools that modify files (for highlighting in history)
_MUTATION_TOOLS = {"write_file", "edit_file", "bash"}

# Max-tokens recovery loop — port of Claude Code's two-tier strategy. When the
# provider returns stop_reason="max_tokens", inject a meta user message telling
# the model to resume mid-thought and re-run up to N times. After N failures
# the truncated turn is persisted as-is and the user is told explicitly.
_MAX_TOKENS_RECOVERY_ATTEMPTS = 3
_MAX_TOKENS_RECOVERY_PROMPT = (
    "Output token limit hit. Resume directly — no apology, no recap of what "
    "you were doing. Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)


def _prepare_recovery_input(
    *,
    agent,
    prior_history,
    user_message: str,
    assistant_blocks: list[dict],
    tool_result_msgs: list[dict],
    pending_tool_uses: dict[str, dict],
    accumulated_text: str,
    flush_pending_text,
    images,
):
    """Build the next-attempt input after a max_tokens truncation.

    The assistant was in the middle of something when the provider capped
    the response. To resume safely we:

    1. Flush any text streamed since the last tool_use into a text block.
    2. Drop orphaned tool_use blocks (no matching tool_result yet) — the
       Anthropic API rejects a request whose trailing assistant message has
       a tool_use without a tool_result on the following user turn, and we
       can't synthesize a plausible result.
    3. Replay the conversation the agent just saw (prior history + current
       user message), plus the truncated assistant turn we have so far,
       plus the open tool_result round (if any), and append the meta
       recovery user message at the very end.

    Returns a list[Message] suitable for agent.arun(...).
    """
    from agno.models.message import Message
    from aru.history_blocks import to_agno_messages

    # Step 1: flush any text accumulated since the last tool_use into the
    # assistant block stream so retry sees it.
    flush_pending_text(accumulated_text)

    # Step 2: strip open tool_use blocks (no tool_result landed). Without
    # this the API rejects the next request. Best-effort removal since the
    # block ref in `pending_tool_uses` may have been mutated.
    for tid in list(pending_tool_uses.keys()):
        block = pending_tool_uses.pop(tid)
        try:
            assistant_blocks.remove(block)
        except ValueError:
            pass

    # Step 3: rebuild the message list. `prior_history` is the Agno
    # Message list originally passed to agent.arun() — it already contains
    # the current user turn as its last entry when history exists. When
    # prior_history is empty (first turn of a session) the original input
    # was a bare string, so we construct the user message ourselves.
    messages = list(prior_history)  # copy so we don't mutate caller state
    if not messages:
        from agno.models.message import Message as _Msg
        messages.append(_Msg(role="user", content=user_message, images=images or None))

    structured = []
    if assistant_blocks:
        structured.append({"role": "assistant", "content": list(assistant_blocks)})
    for tr in tool_result_msgs:
        # Only forward *closed* rounds: open ones mean we truncated before
        # the tool batch finished, which we can't safely resume.
        if not tr.get("_open", True):
            structured.append({"role": "tool", "content": tr["content"]})

    if structured:
        messages.extend(to_agno_messages(structured))

    messages.append(
        Message(role="user", content=_MAX_TOKENS_RECOVERY_PROMPT, images=images or None)
    )
    return messages

_PLAN_STEP_ICONS = {
    "completed": "\u2713",
    "in_progress": "~",
    "failed": "\u2717",
    "skipped": "\u00b7",
}


def _build_plan_reminder(session) -> str | None:
    """Build a system-reminder block listing pending/completed plan steps.

    Mirrors the Claude Code TodoWrite reminder pattern: state lives in the
    session, the model sees the current snapshot every turn and updates it
    via update_plan_step. Returns None when no plan is active.

    Also surfaces plan-mode status: if `session.plan_mode` is True, emits
    a standalone reminder even when no steps are parsed yet (e.g. the agent
    entered plan mode but hasn't written the plan). Keeps the gate visible
    across turns so the agent can't forget it's in plan mode.
    """
    if session is None:
        return None
    steps = getattr(session, "plan_steps", None)
    plan_mode = bool(getattr(session, "plan_mode", False))
    if not steps:
        if plan_mode:
            parts = [
                "<system-reminder>",
                "PLAN MODE ACTIVE — mutating tools (edit_file, write_file, "
                "bash, delegate_task) are BLOCKED until the user approves "
                "a plan. Write the plan as your next assistant message — "
                "it will be shown to the user for approval at the end of "
                "this turn. Do NOT retry blocked tools.",
            ]
            feedback = _consume_plan_rejection_feedback(session)
            if feedback:
                parts.append(
                    f"The user REJECTED your previous plan with this "
                    f"feedback: {feedback}\nRevise the plan accordingly."
                )
            parts.append("</system-reminder>")
            return "\n".join(parts)
        return None

    # Auto-retire plans that have nothing left to execute. Leaving a fully-
    # terminal plan in the reminder makes the agent re-surface it on the next
    # turn — it may even call update_plan_step on old steps, re-rendering the
    # stale panel and confusing the user who already moved on to a new task.
    if all(s.status in ("completed", "skipped", "failed") for s in steps):
        session.clear_plan()
        return None

    pending = sum(1 for s in steps if s.status == "pending")
    done = sum(1 for s in steps if s.status == "completed")
    header = (
        "PLAN MODE ACTIVE (mutating tools BLOCKED until exit_plan_mode approval) - "
        if plan_mode else "PLAN ACTIVE - "
    )
    lines = [
        "<system-reminder>",
        f"{header}{len(steps)} steps total ({done} completed, {pending} pending):",
    ]
    for s in steps:
        icon = _PLAN_STEP_ICONS.get(s.status, "\u25cb")
        lines.append(f"{icon} {s.index}. {s.description}")
    lines.append(
        "Execute steps in order. For each: optionally call create_task_list to break "
        "the step into subtasks, do the work, then call update_plan_step(index, "
        "'completed') to mark progress. Do NOT skip steps silently."
    )
    if getattr(session, "_pending_plan_warning", False):
        lines.append(
            "WARNING: the previous turn ended with steps still pending. Continue "
            "execution or mark unfinished steps as 'skipped' explicitly."
        )
    feedback = _consume_plan_rejection_feedback(session)
    if feedback:
        lines.append(
            f"The user REJECTED your previous plan with this feedback: "
            f"{feedback}\nRevise the plan accordingly."
        )
    lines.append("</system-reminder>")
    return "\n".join(lines)


def _consume_plan_rejection_feedback(session) -> str | None:
    """Read-and-clear plan rejection feedback stored on the session.

    The auto plan-approval path at turn end stores the user's revision
    feedback on `session._plan_rejection_feedback`. The plan reminder
    consumes it on the next turn and clears it, so the agent sees the
    critique exactly once.
    """
    feedback = getattr(session, "_plan_rejection_feedback", None)
    if feedback:
        session._plan_rejection_feedback = None
    return feedback


def _extract_assistant_text(assistant_blocks: list[dict]) -> str:
    """Concatenate the text blocks from a list of structured assistant blocks.

    Used by the auto plan-approval path at turn end to recover the plan
    the agent wrote as regular text. tool_use and other non-text blocks
    are ignored — we only care about what the model said.
    """
    parts: list[str] = []
    for block in assistant_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


async def _fire_plugin_hook(event_name: str, data: dict) -> dict:
    """Fire a plugin hook if the plugin manager is available. Returns (mutated) data."""
    try:
        from aru.runtime import get_ctx
        ctx = get_ctx()
        mgr = ctx.plugin_manager
        if mgr is not None and mgr.loaded:
            event = await mgr.fire(event_name, data)
            return event.data
    except (LookupError, AttributeError):
        pass
    return data


async def _publish_event(event_type: str, data: dict | None = None) -> None:
    """Publish an event to the plugin event bus (fire-and-forget)."""
    try:
        from aru.runtime import get_ctx
        ctx = get_ctx()
        mgr = ctx.plugin_manager
        if mgr is not None and mgr.loaded:
            await mgr.publish(event_type, data or {})
    except (LookupError, AttributeError):
        pass


async def _fire_chat_message_hook(message: str, session=None) -> str:
    """Fire chat.message hook — plugins can modify the user message."""
    data = await _fire_plugin_hook("chat.message", {
        "message": message,
        "session_id": getattr(session, "id", None),
    })
    return data.get("message", message)


async def _fire_chat_messages_transform_hook(messages: list, session=None) -> list:
    """Fire chat.messages.transform hook — plugins can modify message history."""
    data = await _fire_plugin_hook("chat.messages.transform", {
        "messages": messages,
        "session_id": getattr(session, "id", None),
    })
    return data.get("messages", messages)


def build_env_context(session, cwd: str | None = None) -> str:
    """Build environment context string (cwd, git status) for system prompt.

    This context goes into agent instructions (system prompt) so it's cached
    by the provider between turns. Tree is omitted — the model uses
    glob_search/list_directory on demand instead of paying upfront tokens.
    """
    cwd = cwd or os.getcwd()
    parts = [f"The current working directory is: {cwd}"]

    if session:
        git_status = session.get_cached_git_status(cwd)
        if git_status:
            parts.append(f"Git status:\n{git_status}")

    return "\n\n".join(parts)


@dataclass
class AgentRunResult:
    """Result from run_agent_capture.

    When a `session` is passed to `run_agent_capture`, the runner persists
    the assistant turn(s) + tool_result message(s) to `session.history`
    directly as block-shaped content (see aru.history_blocks). Callers
    only need `content` (final text) for display and `stalled` for
    retry logic. `tool_calls` is kept as a display-only list of labels.
    """
    content: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    stalled: bool = False


@dataclass
class PromptInput:
    """Input for runner.prompt — single entry point for native agent execution.

    `agent_name` selects an entry from aru.agents.catalog.AGENTS. Custom
    agents (defined via .agents/agents/*.md) do not flow through prompt() —
    they continue using create_custom_agent_instance + run_agent_capture.
    """
    session: object  # Session — typed as object to avoid circular import
    message: str
    agent_name: str = "build"
    model_ref: str | None = None
    extra_instructions: str = ""
    lightweight: bool = False
    images: list | None = None


async def prompt(input: PromptInput) -> AgentRunResult:
    """Single entry point for native agent execution.

    Builds the agent from the catalog spec, then delegates to run_agent_capture.
    Equivalent to OpenCode's SessionPrompt.prompt() and Claude Code's queryLoop.
    """
    from aru.agent_factory import create_agent_from_spec
    from aru.agents.catalog import AGENTS

    if input.agent_name not in AGENTS:
        raise KeyError(f"Unknown native agent: {input.agent_name!r}. "
                       f"Known: {sorted(AGENTS.keys())}")

    agent = await create_agent_from_spec(
        AGENTS[input.agent_name],
        session=input.session,
        model_ref=input.model_ref,
        extra_instructions=input.extra_instructions,
    )
    return await run_agent_capture(
        agent, input.message, session=input.session,
        lightweight=input.lightweight, images=input.images,
    )


async def run_agent_capture(agent, message: str, session=None, lightweight: bool = False,
                           images: list | None = None) -> AgentRunResult:
    """Run agent with async streaming display and parallel tool execution.

    Args:
        agent: The Agno agent to run.
        message: The user message/prompt.
        session: Optional session for history and context.
        lightweight: If True, skip tree/git/plan context and history (for executor steps).
        images: Optional list of agno.media.Image objects to attach.

    Returns:
        AgentRunResult with text output and list of tool call labels.
    """
    from agno.models.message import Message
    from agno.run.agent import (
        RunContentEvent,
        RunOutput,
        ToolCallCompletedEvent,
        ToolCallStartedEvent,
    )
    from aru.history_blocks import (
        text_block, tool_use_block, tool_result_block, to_agno_messages,
    )

    console.print()
    final_content = None
    collected_tool_calls: list[str] = []
    _stalled = False

    # Snapshot the parent's live/display BEFORE the try block so the except
    # clauses can always restore them — a nested run_agent_capture (e.g. the
    # build agent calling enter_plan_mode) must not clobber the outer Live
    # handle, otherwise downstream permission prompts hang.
    try:
        from aru.runtime import get_ctx as _get_ctx_outer
        _outer_ctx = _get_ctx_outer()
        _parent_live = getattr(_outer_ctx, "live", None)
        _parent_display = getattr(_outer_ctx, "display", None)
    except (LookupError, AttributeError):
        _parent_live = None
        _parent_display = None

    # Structured capture: the stream loop appends to these in event order.
    # On stream completion we persist them to session.history as blocks.
    # `assistant_blocks` holds interleaved text + tool_use blocks for the
    # assistant turn; `tool_result_msgs` holds per-round tool result
    # messages (one tool-role message per round of tool calls).
    assistant_blocks: list[dict] = []
    tool_result_msgs: list[dict] = []  # list of {"role": "tool", "content": [tool_result_blocks]}
    pending_tool_uses: dict[str, dict] = {}  # tool_call_id → tool_use block
    _flushed_text_len: int = 0  # chars of `accumulated` already added to assistant_blocks

    def _flush_pending_text(accumulated_text: str):
        """Move any unflushed accumulated text into a text block."""
        nonlocal _flushed_text_len
        new_text = accumulated_text[_flushed_text_len:]
        if new_text.strip():
            assistant_blocks.append(text_block(new_text))
        _flushed_text_len = len(accumulated_text)

    try:
        from aru.runtime import get_ctx

        status = StatusBar(interval=3.0)
        display = StreamingDisplay(status)
        tracker = display.tool_tracker

        # Build message — environment context (tree/git/cwd) is now in the
        # system prompt (agent instructions) so it's cacheable across turns.
        # Plan reminder + budget warnings are prepended here.
        msg_parts = []

        if session and not lightweight:
            reminder = _build_plan_reminder(session)
            if reminder:
                msg_parts.append(reminder)

            warning = session.check_budget_warning()
            if warning:
                console.print(warning)

        if msg_parts:
            prefix = "\n\n".join(msg_parts)
            run_message = f"{prefix}\n\n{message}"
        else:
            run_message = message

        # Clear stale pending-plan warning — once we surface it, it's consumed.
        if session is not None and getattr(session, "_pending_plan_warning", False):
            session._pending_plan_warning = False

        # Hook: chat.message — let plugins intercept/modify user message
        run_message = await _fire_chat_message_hook(run_message, session)

        # Event: turn.start (Tier 2 #3) — plugin-visible turn boundary
        await _publish_event("turn.start", {
            "user_message": run_message,
            "session_id": getattr(session, "id", None),
        })

        # Event: message.user
        await _publish_event("message.user", {
            "message": run_message,
            "session_id": getattr(session, "id", None),
        })

        # Build conversation history as real messages for the LLM.
        # At turn start we only do reversible pruning — destructive compaction
        # is reserved for the post-turn reactive path (below) which fires when
        # real token count threatens context overflow.
        from aru.context import prune_history, should_compact, compact_conversation, would_prune
        if session and session.history and not lightweight:
            if would_prune(session.history, model_id=session.model_id):
                from rich.status import Status
                with Status("[dim]Pruning context...[/dim]", console=console, spinner="dots"):
                    session.history = prune_history(session.history, model_id=session.model_id)
                console.print("[dim]Context pruned.[/dim]")

        history_messages: list[Message] = []
        if session and session.history and not lightweight:
            prior_history = session.history[:-1]
            history_messages = to_agno_messages(prior_history)

        # Hook: chat.messages.transform — let plugins modify history before LLM
        if history_messages:
            history_messages = await _fire_chat_messages_transform_hook(history_messages, session)

        # Combine: history messages + current enriched message
        if history_messages:
            history_messages.append(Message(role="user", content=run_message, images=images or None))
            agent_input = history_messages
        else:
            agent_input = run_message

        run_output = None
        with Live(display, console=console, refresh_per_second=10) as live:
            ctx = get_ctx()
            ctx.live = live
            ctx.display = display
            accumulated = ""
            _stall_counter = 0
            _stalled = False
            _STALL_LIMIT = 20
            arun_kwargs = dict(stream=True, stream_events=True, yield_run_output=True)
            if isinstance(agent_input, str) and images:
                arun_kwargs["images"] = images

            # Max-tokens recovery loop. A single run may cycle through
            # `agent.arun()` multiple times if the provider truncates at
            # max_tokens — we inject a meta user message asking the model
            # to resume and re-stream into the *same* assistant_blocks /
            # accumulated buffers so the persisted turn reads as one
            # continuous message. Mirrors Claude Code's query.ts loop.
            from aru.cache_patch import get_last_stop_reason, reset_last_stop_reason
            current_input = agent_input
            recovery_attempts_left = _MAX_TOKENS_RECOVERY_ATTEMPTS
            while True:
                reset_last_stop_reason()
                async for event in agent.arun(current_input, **arun_kwargs):
                    if isinstance(event, RunOutput):
                        run_output = event
                        break

                    if isinstance(event, ToolCallStartedEvent):
                        _stall_counter = 0
                        if hasattr(event, "tool") and event.tool:
                            tool_name = event.tool.tool_name or "tool"
                            tool_args = event.tool.tool_args or None
                            tool_id = getattr(event.tool, "tool_call_id", None) or tool_name
                        else:
                            tool_name = getattr(event, "tool_name", "tool")
                            tool_args = getattr(event, "tool_args", None)
                            tool_id = getattr(event, "tool_call_id", None) or tool_name
                        label = _format_tool_label(tool_name, tool_args)
                        collected_tool_calls.append(label)
                        # Structured capture: flush any text streamed so far into
                        # a text block, then append the tool_use block. This
                        # preserves the order text → tool call → more text.
                        _flush_pending_text(accumulated)
                        assistant_blocks.append(
                            tool_use_block(tool_id, tool_name, tool_args if isinstance(tool_args, dict) else {})
                        )
                        pending_tool_uses[tool_id] = assistant_blocks[-1]
                        if accumulated[display._flushed_len:]:
                            display.content = None
                            live.stop()
                            display.flush()
                            live.start()
                            live._live_render._shape = None
                        tracker.start(tool_id, label)
                        status.set_text(f"{label}...")
                        live.update(display)
                        # Event: tool.called
                        await _publish_event("tool.called", {
                            "tool_name": tool_name, "tool_id": tool_id,
                            "args": tool_args if isinstance(tool_args, dict) else {},
                        })

                    elif isinstance(event, ToolCallCompletedEvent):
                        _stall_counter = 0
                        if hasattr(event, "tool") and event.tool:
                            tool_id = getattr(event.tool, "tool_call_id", None) or getattr(event.tool, "tool_name", "tool")
                            tool_result_text = getattr(event.tool, "result", None)
                        else:
                            tool_id = getattr(event, "tool_call_id", None) or getattr(event, "tool_name", "tool")
                            tool_result_text = getattr(event, "content", None)

                        # Structured capture: bundle the tool_result into the
                        # most recent tool-role message (same "round" of tool
                        # calls) so they become a single user-side follow-up
                        # in Anthropic's wire format.
                        if tool_id in pending_tool_uses:
                            result_str = str(tool_result_text) if tool_result_text is not None else ""
                            tr_block = tool_result_block(tool_id, result_str)
                            if tool_result_msgs and tool_result_msgs[-1]["_open"]:
                                tool_result_msgs[-1]["content"].append(tr_block)
                            else:
                                tool_result_msgs.append({
                                    "role": "tool",
                                    "content": [tr_block],
                                    "_open": True,
                                })
                            pending_tool_uses.pop(tool_id, None)

                        # Event: tool.completed
                        await _publish_event("tool.completed", {
                            "tool_id": tool_id,
                            "result_length": len(str(tool_result_text)) if tool_result_text else 0,
                        })
                        result = tracker.complete(tool_id)
                        for label, duration in tracker.pop_completed():
                            dur_str = f" {duration:.1f}s" if duration >= 0.5 else ""
                            live.console.print(Text.assemble(
                                ("  ", ""),
                                ("\u2713 ", "bold green"),
                                (label, "dim"),
                                (dur_str, "dim cyan"),
                            ))
                        if not tracker.active_labels:
                            status.resume_cycling()
                            # Close the current tool_result round — any further
                            # tool calls start a new round.
                            if tool_result_msgs and tool_result_msgs[-1]["_open"]:
                                tool_result_msgs[-1]["_open"] = False
                            # Flush coalesced plan-panel render. Multiple
                            # update_plan_step calls in the same batch (and any
                            # enter_plan_mode that replaces the plan mid-batch)
                            # collapse into a single panel showing final state.
                            try:
                                from aru.tools.tasklist import flush_plan_render
                                flush_plan_render(session)
                            except Exception:
                                pass
                        live.update(display)

                    elif isinstance(event, RunContentEvent):
                        _stall_counter = 0
                        if hasattr(event, "content") and event.content:
                            accumulated += event.content
                            unflushed = accumulated[display._flushed_len:]

                            if unflushed.count("\n") > 15:
                                break_point = unflushed.rfind("\n\n")
                                if break_point == -1:
                                    break_point = unflushed.rfind("\n")

                                if break_point != -1:
                                    chunk = unflushed[:break_point + 1]
                                    if chunk.count("```") % 2 == 0:
                                        display.content = None
                                        live.stop()
                                        console.print(Markdown(chunk))
                                        display._flushed_len += len(chunk)
                                        live.start()
                                        live._live_render._shape = None

                            display.set_content(accumulated)
                            live.update(display)

                    else:
                        _stall_counter += 1
                        if _stall_counter >= _STALL_LIMIT:
                            _stalled = True
                            live.console.print(
                                "[yellow]Agent stalled (tool call limit likely reached). "
                                "Moving on.[/yellow]"
                            )
                            break

                # Stream for this attempt finished. If we weren't truncated
                # at the output cap, or we've exhausted recovery attempts,
                # stop here. Otherwise build a recovery input and loop.
                if get_last_stop_reason() != "max_tokens":
                    break
                if _stalled:
                    break
                if recovery_attempts_left <= 0:
                    live.console.print(
                        f"[yellow]Output still truncated after "
                        f"{_MAX_TOKENS_RECOVERY_ATTEMPTS} recovery attempts. "
                        f"Persisting the turn as-is.[/yellow]"
                    )
                    break
                current_input = _prepare_recovery_input(
                    agent=agent,
                    prior_history=history_messages,
                    user_message=run_message,
                    assistant_blocks=assistant_blocks,
                    tool_result_msgs=tool_result_msgs,
                    pending_tool_uses=pending_tool_uses,
                    accumulated_text=accumulated,
                    flush_pending_text=_flush_pending_text,
                    images=images,
                )
                recovery_attempts_left -= 1
                attempt_no = _MAX_TOKENS_RECOVERY_ATTEMPTS - recovery_attempts_left
                live.console.print(
                    f"[dim]Output truncated at cap — resuming "
                    f"({attempt_no}/{_MAX_TOKENS_RECOVERY_ATTEMPTS})...[/dim]"
                )
                # Clear run_output so the next pass repopulates it.
                run_output = None

            # Clear live content before the Live context exits so its final
            # render doesn't duplicate text that we print explicitly below.
            display.content = None

        # Restore the parent's live/display (or None if this was the outermost call).
        ctx.live = _parent_live
        ctx.display = _parent_display

        # Flush any trailing text into a final text block, then persist the
        # assistant turn + tool_result messages to session.history as
        # structured blocks. This is how tool results survive across turns.
        _flush_pending_text(accumulated)
        if session and not lightweight:
            if assistant_blocks:
                session.add_structured_message("assistant", assistant_blocks)
            for tr_msg in tool_result_msgs:
                session.add_structured_message(tr_msg["role"], tr_msg["content"])

        # Auto-exit plan mode at turn end. If the agent called
        # `enter_plan_mode` and then wrote the plan as assistant text but
        # *forgot* to call `exit_plan_mode`, the flag would stay on forever
        # and the user would never see the approval panel. Surface the
        # approval flow automatically here using the assistant's text as
        # the plan. If the agent DID call exit_plan_mode, plan_mode is
        # already False and this block is a no-op.
        if session and not lightweight and getattr(session, "plan_mode", False):
            plan_text = _extract_assistant_text(assistant_blocks).strip()
            if plan_text:
                try:
                    from aru.tools.plan_mode import _prompt_plan_approval
                    task_label = plan_text.split("\n", 1)[0][:80]
                    session.set_plan(task=task_label, plan_content=plan_text)
                    approved, feedback = _prompt_plan_approval(
                        session.plan_steps, len(session.plan_steps)
                    )
                    session._plan_render_pending = False
                    if approved:
                        session.plan_mode = False
                        session._plan_rejection_feedback = None
                        console.print(
                            "[green]Plan approved. Send your next instruction "
                            "to execute it, or say 'go' to proceed.[/green]"
                        )
                    else:
                        # Keep plan_mode on so the agent revises next turn.
                        session.clear_plan()
                        session.plan_mode = True  # clear_plan() doesn't touch it
                        if feedback:
                            session._plan_rejection_feedback = feedback
                            console.print(
                                f"[yellow]Plan rejected. Feedback queued for "
                                f"next turn:[/yellow] [dim]{feedback}[/dim]"
                            )
                        else:
                            session._plan_rejection_feedback = (
                                "User rejected the plan without specific feedback. "
                                "Ask the user what they would like changed."
                            )
                            console.print(
                                "[yellow]Plan rejected. Tell Aru what to "
                                "revise.[/yellow]"
                            )
                except Exception:
                    # Never let an approval-flow glitch crash the turn —
                    # the plan text is already in history, the user can
                    # interact normally on the next turn.
                    pass

        if run_output and session and hasattr(run_output, "metrics"):
            session.track_tokens(run_output.metrics)

            # Reactive compaction: runs with a visible spinner so the user
            # sees progress instead of a frozen screen.
            #
            # IMPORTANT: the compaction trigger must reflect the *per-call*
            # context window (what the next API request would occupy), NOT
            # the cumulative input across all API calls in this turn.
            # Agno's `RunMetrics.input_tokens` is cumulative (it does
            # `metrics.input_tokens += input_tokens` on every call), so
            # using it here causes compaction to fire on multi-tool turns
            # even when the actual per-call window is comfortably small.
            # `session.track_tokens` above already populated `last_*` via
            # `cache_patch.get_last_call_metrics`, which gives us the real
            # last-call window — the same metric shown in the status bar.
            last_call_window = (
                session.last_input_tokens
                + session.last_output_tokens
                + session.last_cache_read
                + session.last_cache_write
            )
            if should_compact(last_call_window, session.model_id):
                from rich.status import Status
                with Status("[dim]Compacting context...[/dim]", console=console, spinner="dots"):
                    try:
                        session.history = prune_history(session.history, model_id=session.model_id)
                        # Only the primary agent's invoked skills get replayed
                        # after compaction here — subagents have their own
                        # compaction flow and carry their own agent_id.
                        session.history = await compact_conversation(
                            session.history, session.model_ref, session.plan_task,
                            model_id=session.model_id,
                            invoked_skills=session.get_invoked_skills_for_agent(None),
                        )
                        console.print("[dim]Context compacted to save tokens.[/dim]")
                    except Exception:
                        pass

        final_content = accumulated or final_content

        # Event: message.assistant
        await _publish_event("message.assistant", {
            "content": final_content,
            "tool_calls": collected_tool_calls,
            "session_id": getattr(session, "id", None),
        })

        # Event: turn.end (Tier 2 #3) — end-of-turn hook consumed by the
        # auto-memory extractor and metric/audit plugins. Fires even when
        # the assistant's reply is empty so plugins can still count turns.
        _turn_tokens_in = 0
        _turn_tokens_out = 0
        if session is not None:
            _turn_tokens_in = getattr(session, "last_input_tokens", 0) or 0
            _turn_tokens_out = getattr(session, "last_output_tokens", 0) or 0
        await _publish_event("turn.end", {
            "assistant_message": final_content or "",
            "tool_calls": collected_tool_calls,
            "tokens_in": _turn_tokens_in,
            "tokens_out": _turn_tokens_out,
            "session_id": getattr(session, "id", None),
        })

        # Tier 2 #4: auto-memory extraction (opt-in, fire-and-forget).
        try:
            from aru.memory.extractor import schedule_extraction_task
            from aru.runtime import get_ctx as _get_ctx
            _cfg = getattr(_get_ctx(), "config", None)
            _cfg_memory = getattr(_cfg, "memory", None) or {}
            _project_root = getattr(session, "project_root", None) or os.getcwd()
            schedule_extraction_task(
                project_root=_project_root,
                user_msg=run_message or "",
                assistant_msg=final_content or "",
                config_memory=_cfg_memory,
                turn_tokens=_turn_tokens_in + _turn_tokens_out,
            )
        except Exception:
            pass  # extractor guards internally; swallow any unexpected raise

        remaining = (final_content or "")[display._flushed_len:]
        if remaining:
            console.print(Markdown(remaining))

    except (KeyboardInterrupt, asyncio.CancelledError):
        ctx = get_ctx()
        ctx.live = _parent_live
        ctx.display = _parent_display
        console.print("\n[yellow]Interrupted.[/yellow]")
    except Exception as e:
        ctx = get_ctx()
        ctx.live = _parent_live
        ctx.display = _parent_display
        from rich.markup import escape
        console.print(f"[red]Error: {escape(str(e))}[/red]")

    # Final guard: if a plan is active and the agent ended its turn with
    # pending steps (without stalling), mark the session so the next turn's
    # reminder includes a warning. The model can then decide to continue or
    # mark unfinished steps as 'skipped' explicitly.
    if session is not None and not lightweight and not _stalled:
        steps = getattr(session, "plan_steps", None)
        if steps and any(s.status == "pending" for s in steps):
            session._pending_plan_warning = True

    console.print()
    return AgentRunResult(content=final_content, tool_calls=collected_tool_calls, stalled=_stalled)

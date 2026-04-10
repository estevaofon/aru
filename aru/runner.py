"""Agent execution: streaming display orchestration, plan step execution."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field

from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from aru.commands import ask_yes_no
from aru.display import (
    StatusBar,
    StreamingDisplay,
    _format_tool_label,
    console,
)
from aru.permissions import get_skip_permissions


# Categories of tools that modify files (for highlighting in history)
_MUTATION_TOOLS = {"write_file", "edit_file", "bash"}


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
        # Only plan progress and budget warnings are added here.
        msg_parts = []

        if session and not lightweight:
            if session.current_plan:
                msg_parts.append(f"## Active Plan\nTask: {session.plan_task}\n\n{session.render_plan_progress()}")

            warning = session.check_budget_warning()
            if warning:
                console.print(warning)

        if msg_parts:
            prefix = "\n\n".join(msg_parts)
            run_message = f"{prefix}\n\n---\n\n{message}"
        else:
            run_message = message

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
            async for event in agent.arun(agent_input, **arun_kwargs):
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

            # Clear live content before the Live context exits so its final
            # render doesn't duplicate text that we print explicitly below.
            display.content = None

        ctx.live = None
        ctx.display = None

        # Flush any trailing text into a final text block, then persist the
        # assistant turn + tool_result messages to session.history as
        # structured blocks. This is how tool results survive across turns.
        _flush_pending_text(accumulated)
        if session and not lightweight:
            if assistant_blocks:
                session.add_structured_message("assistant", assistant_blocks)
            for tr_msg in tool_result_msgs:
                session.add_structured_message(tr_msg["role"], tr_msg["content"])

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
                        session.history = await compact_conversation(
                            session.history, session.model_ref, session.plan_task,
                            model_id=session.model_id,
                        )
                        console.print("[dim]Context compacted to save tokens.[/dim]")
                    except Exception:
                        pass

        final_content = accumulated or final_content
        remaining = (final_content or "")[display._flushed_len:]
        if remaining:
            console.print(Markdown(remaining))

    except (KeyboardInterrupt, asyncio.CancelledError):
        ctx = get_ctx()
        ctx.live = None
        ctx.display = None
        console.print("\n[yellow]Interrupted.[/yellow]")
    except Exception as e:
        ctx = get_ctx()
        ctx.live = None
        ctx.display = None
        from rich.markup import escape
        console.print(f"[red]Error: {escape(str(e))}[/red]")

    console.print()
    return AgentRunResult(content=final_content, tool_calls=collected_tool_calls, stalled=_stalled)


def _extract_plan_file_paths(plan_text: str) -> list[str]:
    """Extract file paths mentioned in plan steps (e.g., 'in `aru/cli.py`')."""
    matches = re.findall(r"`([^`]+\.\w{1,5})`", plan_text or "")
    seen = set()
    paths = []
    for m in matches:
        norm = os.path.normpath(m)
        if norm not in seen and os.path.isfile(norm):
            seen.add(norm)
            paths.append(norm)
    return paths


def _build_file_context(file_paths: list[str], max_total: int = 20_000) -> str:
    """Read files and build a context string, respecting a total char budget."""
    if not file_paths:
        return ""
    parts = []
    total = 0
    for path in file_paths:
        try:
            content = open(path, "r", encoding="utf-8").read()
            if total + len(content) > max_total:
                continue
            total += len(content)
            parts.append(f"### `{path}`\n```\n{content}\n```")
        except Exception:
            continue
    if not parts:
        return ""
    return "## Pre-loaded file contents (do NOT re-read these files)\n\n" + "\n\n".join(parts)


async def execute_plan_steps(session, executor_factory) -> str | None:
    """Execute plan steps one by one with live progress tracking."""
    plan_files = _extract_plan_file_paths(session.current_plan)
    file_context = _build_file_context(plan_files)

    if not session.plan_steps:
        executor = executor_factory()
        exec_prompt = (
            f"Execute the following plan step by step.\n\n"
            f"## Task\n{session.plan_task}\n\n"
            f"## Plan\n{session.current_plan}"
        )
        run_result = await run_agent_capture(executor, exec_prompt, session, lightweight=True)
        content = run_result.content or ""
        if run_result.tool_calls:
            tools_section = "\n".join(f"  - {t}" for t in run_result.tool_calls)
            content = f"{content}\n\n[Tools]\n{tools_section}" if content else tools_section
        return content or None

    all_results = []
    completed_context = ""

    for step in session.plan_steps:
        console.print()
        console.print(Panel(
            Text.from_markup(session.render_plan_progress()),
            title="[bold]Plan Progress[/bold]",
            border_style="blue",
            padding=(0, 1),
        ))
        console.print()

        step.status = "in_progress"
        console.print(f"[bold yellow]>>> Step {step.index}:[/bold yellow] {step.description}")

        compact_progress = session.render_compact_progress(step.index)
        step_prompt_parts = [
            f"## Task: {session.plan_task}\n",
            f"## Current Step ({step.index}/{len(session.plan_steps)})\n{step.description}\n",
            f"## Progress\n{compact_progress}\n",
            "IMPORTANT: Just execute this step. Do NOT repeat completed steps or summarize.",
        ]
        if file_context:
            step_prompt_parts.insert(1, file_context)
        step_prompt = "\n".join(step_prompt_parts)

        from aru.tools.tasklist import reset_task_store
        reset_task_store()

        executor = executor_factory()
        try:
            run_result = await run_agent_capture(executor, step_prompt, session, lightweight=True)
            content = run_result.content

            if run_result.stalled:
                from aru.tools.tasklist import get_task_store
                store = get_task_store()
                all_tasks = store.get_all()
                done = [t for t in all_tasks if t["status"] == "completed"]
                pending = [t for t in all_tasks if t["status"] not in ("completed", "failed")]

                console.print(f"\n[yellow]Step {step.index} stalled (tool call limit reached).[/yellow]")
                if done:
                    console.print(f"  [green]Completed:[/green] {len(done)}/{len(all_tasks)} subtasks")
                if pending:
                    console.print(f"  [yellow]Pending:[/yellow]")
                    for t in pending:
                        console.print(f"    - {t.get('description', t.get('id', '?'))}")

                if get_skip_permissions():
                    step.status = "failed"
                    continue

                console.print("\n[bold]Options:[/bold]")
                console.print("  [cyan](r)[/cyan] Retry step with additional instructions")
                console.print("  [cyan](s)[/cyan] Skip to next step")
                console.print("  [cyan](a)[/cyan] Abort plan execution")
                try:
                    choice = console.input("[bold yellow]Choice (r/s/a):[/bold yellow] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = "a"

                if choice in ("r", "retry"):
                    try:
                        extra = console.input("[bold cyan]Additional instructions:[/bold cyan] ").strip()
                    except (EOFError, KeyboardInterrupt):
                        extra = ""
                    if extra:
                        step.status = "in_progress"
                        reset_task_store()
                        retry_prompt = step_prompt + f"\n\n## Additional Instructions\n{extra}"
                        executor = executor_factory()
                        run_result = await run_agent_capture(executor, retry_prompt, session, lightweight=True)
                        content = run_result.content
                    else:
                        step.status = "failed"
                        continue
                elif choice in ("s", "skip"):
                    step.status = "failed"
                    continue
                else:
                    step.status = "failed"
                    break

            from aru.tools.tasklist import get_task_store
            store = get_task_store()
            all_tasks = store.get_all()
            tasks_completed = sum(1 for t in all_tasks if t["status"] == "completed")
            tasks_failed = sum(1 for t in all_tasks if t["status"] == "failed")
            tasks_total = len(all_tasks)
            tasks_all_done = tasks_total > 0 and (tasks_completed + tasks_failed == tasks_total)

            step_failed = False
            if tasks_all_done:
                if tasks_failed > 0 and tasks_completed == 0:
                    step_failed = True
            elif content:
                step_failed = (
                    content.startswith("Error")
                    or "Error from OpenAI API" in content
                    or "Error in Agent run" in content
                )

            if step_failed:
                step.status = "failed"
                fail_msg = content[:200] if content else f"{tasks_failed}/{tasks_total} subtasks failed"
                console.print(f"\n[red]Step {step.index} failed: {fail_msg}[/red]")
                if not get_skip_permissions() and not ask_yes_no("Continue with remaining steps?"):
                    break
            elif content or tasks_all_done:
                step.status = "completed"
                summary = content or f"All {tasks_completed} subtasks completed."
                step_text = f"### Step {step.index}: {step.description}\n{summary}"
                if run_result.tool_calls:
                    tools_str = ", ".join(run_result.tool_calls)
                    step_text += f"\nTools: {tools_str}"
                all_results.append(step_text)
                completed_context += f"\n- Step {step.index} ({step.description}): Done"
            else:
                step.status = "completed"
                completed_context += f"\n- Step {step.index} ({step.description}): Done (no output)"
        except (KeyboardInterrupt, asyncio.CancelledError):
            step.status = "failed"
            console.print(f"\n[yellow]Step {step.index} interrupted.[/yellow]")
            if not get_skip_permissions() and not ask_yes_no("Continue with remaining steps?"):
                break
        except Exception as e:
            step.status = "failed"
            console.print(f"\n[red]Step {step.index} failed: {e}[/red]")
            if not get_skip_permissions() and not ask_yes_no("Continue with remaining steps?"):
                break

    console.print()
    console.print(Panel(
        Text.from_markup(session.render_plan_progress()),
        title="[bold]Plan Complete[/bold]",
        border_style="green" if all(s.status == "completed" for s in session.plan_steps) else "yellow",
        padding=(0, 1),
    ))

    return "\n\n".join(all_results) if all_results else None

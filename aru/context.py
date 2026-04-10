"""Context management for token optimization.

Mirrors opencode's two-layer approach:

1. **Prune** (routine, lossy only on tool outputs): walks old tool_result
   blocks and replaces their content with a placeholder. User/assistant
   text is NEVER touched — it survives verbatim until real overflow.
   This is the steady-state memory mechanism. Matches cache_patch.py's
   strategy at the Agno message layer.

2. **Compact** (rare, lossy full summary): triggers only when the per-call
   context window actually approaches the model's limit. Runs a
   compaction agent that produces a structured summary (Goal / Instructions
   / Discoveries / Accomplished / File contents / Relevant files) and
   marks the resulting assistant message with `summary: True` so
   subsequent prunes stop at that checkpoint.

There is also a `truncate_output` layer used by individual tools to cap
their own output size before it ever reaches history.
"""

from __future__ import annotations

# ── Constants ──────────────────────────────────────────────────────

# Pruning: minimum chars that must be freeable to justify a prune pass.
# Matches opencode's PRUNE_MINIMUM = 20_000 tokens (~80K chars @ 4 chars/token).
PRUNE_MINIMUM_CHARS = 80_000  # ~20K tokens
# Placeholder that replaces cleared tool_result content. Matches
# cache_patch.py's _PRUNED_PLACEHOLDER so both layers produce identical
# text when a tool output is cleared.
CLEARED_TOOL_RESULT = "[Old tool result cleared]"
# Minimum number of recent user turns always protected (regardless of char budget)
PRUNE_PROTECT_TURNS = 2
# Tool result markers that should never be pruned (critical context)
PRUNE_PROTECTED_MARKERS = {"[SubAgent-", "delegate_task"}
# Tool names whose outputs should never be pruned (like opencode's PRUNE_PROTECTED_TOOLS)
# These are checked as substrings in message content (tool results include the tool name)
PRUNE_PROTECTED_TOOLS = {"delegate_task"}

# Truncation: universal limits for any tool output
TRUNCATE_MAX_LINES = 300
TRUNCATE_MAX_BYTES = 15 * 1024  # 15 KB
TRUNCATE_KEEP_START = 150  # lines to keep from the start
TRUNCATE_KEEP_END = 60  # lines to keep from the end
TRUNCATE_MAX_LINE_LENGTH = 1500  # chars per individual line (prevents minified files)
# Directory for saving full truncated outputs (like opencode pattern)
TRUNCATE_SAVE_DIR = ".aru/truncated"

# Compaction: chars of recent conversation preserved verbatim post-compact.
#
# Separate from the prune protect window (160K) because they measure
# different things:
#   - Prune protect: "how much tool_result content stays intact"
#   - Compact recent: "how much full-message history stays verbatim after
#     the summary replaces the older portion"
#
# Set to 80K chars (~20K tokens) — half the prune window. Rationale:
# with the compactor now running on the main model (not a small one),
# summaries are faithful enough that we don't need 40K of recent overlap
# as a safety net. 20K still covers 3-6 recent turns verbatim, which
# mirrors the "last few exchanges" a human would re-read to resume work.
# Going to zero would match opencode exactly but requires the reactive
# overflow replay flow we haven't implemented yet.
COMPACT_RECENT_CHARS = 80_000

# Compaction: trigger when per-call input tokens approach real overflow.
# Matches opencode's philosophy: only fire near the model's actual context
# limit, not routinely. Routine context reduction is handled by prune_history
# (lossy only on tool outputs), so compaction is reserved for genuine
# overflow — where the next API call would otherwise exceed the model's
# input limit minus the reserved buffer.
#
# Opencode fires at `count >= limit.input - reserved` (overflow.ts:22) —
# no extra ratio. We mirror that here. The sole safety margin is
# COMPACTION_BUFFER_TOKENS, which is 30K (vs opencode's 20K) to give a bit
# more headroom for output + tool definitions + estimation noise, since
# we don't yet have a reactive overflow handler to catch the edge case.
COMPACTION_BUFFER_TOKENS = 30_000
# Default model context limits (input tokens)
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    # Anthropic
    "claude-sonnet-4-5-20250929": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4.1-nano": 1_000_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    # Qwen (AlibabaCloud)
    "qwen3-plus": 128_000,
    "qwen3.6-plus": 128_000,
    "qwen-plus": 128_000,
    "qwen-max": 128_000,
    "qwen-turbo": 128_000,
    "qwen3-coder-plus": 128_000,
    # DeepSeek
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    # Meta Llama (common Ollama/Groq)
    "llama3.1": 128_000,
    "llama-3.1-70b-versatile": 128_000,
    "llama-3.3-70b-versatile": 128_000,
    "llama4-scout": 512_000,
    # Google Gemini (OpenRouter)
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    # Fallback
    "default": 128_000,
}

COMPACTION_TEMPLATE = """\
Summarize this conversation into the EXACT sections below. Be concise but complete — \
this summary replaces the full conversation history. Output ONLY these sections:

## Goal
What the user is trying to accomplish (1-2 sentences).

## Instructions
Important instructions or preferences the user stated (bullet list). \
If none, write "None stated."

## Discoveries
Notable things learned about the codebase, bugs, or architecture (bullet list). \
If none, write "None."

## Accomplished
What was done so far — be specific about files created/changed and functions added/modified. \
List what is in progress and what remains (bullet list).

## File contents (key excerpts)
For each file whose contents were shown in the conversation (via @mention or tool reads), \
preserve the most important excerpts verbatim: function/class signatures, critical \
constants, bug-related lines. Format as ```path\\n<excerpt>\\n```. \
If no file contents were shown, write "None."

## Relevant files / directories
Structured list of file paths relevant to continuing the work (one per line)."""


# ── Layer 1: Pruning ──────────────────────────────────────────────

def _tool_result_content_len(msg: dict) -> int:
    """Sum of content length of all non-cleared tool_result blocks in a message.

    Mirrors opencode's prune walk, which accumulates only
    `Token.estimate(part.state.output)` for `ToolPart`s (compaction.ts:119).
    Text blocks and tool_use args are ignored — they are not the thing
    being freed. This means pruning only "consumes budget" for real tool
    output, so text-heavy conversations with few tool calls never trip
    the prune path.

    Already-cleared tool_results (content == CLEARED_TOOL_RESULT) are
    skipped so a second pass doesn't double-count them.
    """
    from aru.history_blocks import is_tool_result
    total = 0
    for block in msg.get("content", []):
        if is_tool_result(block):
            content = block.get("content")
            if content == CLEARED_TOOL_RESULT:
                continue
            if content is None:
                continue
            # tool_result content can be a string or a list of blocks —
            # stringify to get a char count that roughly tracks tokens.
            total += len(str(content))
    return total


def _get_prune_protect_chars(model_id: str = "default") -> int:
    """Chars of recent history that must NEVER be pruned.

    Flat value across all models, mirroring opencode's fixed
    `PRUNE_PROTECT = 40_000` tokens (compaction.ts:36). At ~4 chars/token
    that's 160K chars of tool-result content kept intact in the recent
    window. Older tool_result blocks beyond this budget are eligible for
    the lossy clear pass in `prune_history`.

    Why flat (not scaled by model): opencode validated this in production
    on contexts from 128K to 1M — scaling by ratio adds complexity without
    improving behavior, and protecting too much in 1M-context models can
    actually hurt prompt caching by keeping rarely-touched tail content warm.

    The `model_id` parameter is retained for signature compatibility with
    older call sites; it has no effect on the returned value.
    """
    del model_id  # unused — kept for signature compatibility
    return 160_000


def prune_history(
    history: list[dict], model_id: str = "default"
) -> list[dict]:
    """Reduce history token footprint by clearing old tool result content.

    Operates on block-shaped history (see `aru.history_blocks`). Matches
    opencode's approach: the ONLY lossy operation is replacing the
    content of old `tool_result` blocks with a short placeholder. Text
    blocks (user and assistant), `tool_use` blocks, and block structure
    are always preserved — so the original ask survives verbatim until
    real overflow forces a full compaction.

    **Budget semantics** (opencode parity): the walk backward accumulates
    **only tool_result content chars**, not whole-message chars. Text
    blocks and tool_use args don't consume the protection budget, because
    they aren't what prune can free. Consequences:
      - Text-heavy conversations with few tool calls never trigger prune.
      - Prune only fires when there is >= `protect_chars + PRUNE_MINIMUM_CHARS`
        of tool_result content total — mirroring opencode's
        `total > PRUNE_PROTECT + PRUNE_MINIMUM`.
      - The "is it worth pruning?" dry-run check from opencode
        (`pruned > PRUNE_MINIMUM`) is implicit: we cannot enter the loop
        without enough prunable content, and once in the loop any walk
        past `protect_chars` is guaranteed to be freeing real bytes.

    Protection layers (applied on top of the budget walk):
    1. Turn-based: last `PRUNE_PROTECT_TURNS` user turns always kept
       intact, plus the assistant response right after each. Index 0
       (the original user ask) is also always protected.
    2. Budget-based: tool_result content within the 160K protect window
       (~40K tokens, matching opencode) is kept.
    3. Content-based: messages whose stringified content contains any
       `PRUNE_PROTECTED_MARKERS` or `PRUNE_PROTECTED_TOOLS` never prune.
    4. Summary checkpoint: walking backward stops at any message marked
       `summary: True` (a previous compaction's assistant output).
       Everything before a summary was already consolidated and must
       not be re-processed.

    Returns a new list (does not mutate the input).
    """
    from aru.history_blocks import (
        coerce_history_item, item_text, is_tool_result,
    )

    if len(history) <= 2:
        return [coerce_history_item(m) for m in history]

    protect_chars = _get_prune_protect_chars(model_id)
    result = [coerce_history_item(m) for m in history]

    # Entry gate mirrors opencode: only proceed if total tool output
    # exceeds protect + minimum. Text length is irrelevant.
    total_tool_chars = sum(_tool_result_content_len(msg) for msg in result)
    if total_tool_chars < protect_chars + PRUNE_MINIMUM_CHARS:
        return result

    # Identify indices of last N user turns (always protected) and index 0
    # (the original user ask, protected defensively so the anchor never
    # evaporates even if future edits change the budget calculus).
    turn_protected: set[int] = set()
    user_turns_seen = 0
    for i in range(len(result) - 1, -1, -1):
        if result[i]["role"] == "user":
            user_turns_seen += 1
            if user_turns_seen <= PRUNE_PROTECT_TURNS:
                turn_protected.add(i)
                if i + 1 < len(result):
                    turn_protected.add(i + 1)
    if result and result[0]["role"] == "user":
        turn_protected.add(0)
        if len(result) > 1:
            turn_protected.add(1)

    # Walk backward accumulating ONLY tool_result content chars into the
    # protection budget. Messages with no tool_result (pure text, or just
    # tool_use) consume zero budget and are skipped without pruning.
    protected = 0

    for i in range(len(result) - 1, -1, -1):
        msg = result[i]

        # Stop at the previous compaction summary marker — everything
        # before it was already consolidated into the summary.
        if msg.get("summary"):
            break

        tool_chars = _tool_result_content_len(msg)

        # No prunable content here — nothing to clear, nothing to count.
        if tool_chars == 0:
            continue

        if i in turn_protected:
            protected += tool_chars
            continue

        if protected + tool_chars <= protect_chars:
            protected += tool_chars
            continue

        # Outside protection window — check content-based protection
        text_view = item_text(msg)
        if (any(marker in text_view for marker in PRUNE_PROTECTED_MARKERS)
                or any(tool in text_view for tool in PRUNE_PROTECTED_TOOLS)):
            protected += tool_chars
            continue

        # Clear any tool_result payloads in this message. Leave every
        # other block (text, tool_use, thinking, etc.) untouched.
        new_blocks: list[dict] = []
        for block in msg["content"]:
            if is_tool_result(block) and block.get("content") != CLEARED_TOOL_RESULT:
                new_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": CLEARED_TOOL_RESULT,
                })
            else:
                new_blocks.append(block)

        result[i] = {"role": msg["role"], "content": new_blocks}

    return result


# ── Layer 2: Truncation ───────────────────────────────────────────

def _truncate_long_lines(lines: list[str]) -> list[str]:
    """Truncate individual lines that exceed MAX_LINE_LENGTH.

    Prevents minified JS/CSS or log lines from consuming massive tokens.
    """
    result = []
    for line in lines:
        if len(line) > TRUNCATE_MAX_LINE_LENGTH:
            result.append(
                line[:TRUNCATE_MAX_LINE_LENGTH]
                + f"... (line truncated to {TRUNCATE_MAX_LINE_LENGTH} chars)\n"
            )
        else:
            result.append(line)
    return result


def _save_truncated_output(text: str) -> str | None:
    """Save full truncated output to disk and return the file path.

    Returns None if saving fails (non-fatal — hint will omit path).
    """
    import os
    import time

    save_dir = os.path.join(os.getcwd(), TRUNCATE_SAVE_DIR)
    try:
        os.makedirs(save_dir, exist_ok=True)
        filename = f"output_{int(time.time() * 1000)}.txt"
        filepath = os.path.join(save_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)
        return filepath
    except OSError:
        return None


def _build_truncation_hint(
    source_file: str = "",
    source_tool: str = "",
    lines_shown: int = 0,
    saved_path: str | None = None,
) -> str:
    """Build a context-aware truncation hint.

    When output was saved to disk, points to the saved file.
    When the source file is known, provides a direct read_file reference.
    """
    parts = ["[Truncated."]

    if saved_path:
        parts.append(f" Full output saved to: {saved_path}")
        parts.append(" Use grep_search or read_file with start_line/end_line to inspect.")
    elif source_file:
        next_line = lines_shown + 1 if lines_shown else 1
        parts.append(f' read_file("{source_file}", start_line={next_line}) for more.')
    else:
        parts.append(" Use grep_search to find specific content.")

    parts.append("]")
    return " ".join(parts)


def truncate_output(
    text: str,
    source_file: str = "",
    source_tool: str = "",
) -> str:
    """Universal truncation for tool outputs.

    Caps output at TRUNCATE_MAX_BYTES / TRUNCATE_MAX_LINES, keeping the
    start and end with a middle marker showing what was cut.
    Also truncates individual lines exceeding TRUNCATE_MAX_LINE_LENGTH.

    Args:
        text: The output text to truncate.
        source_file: Optional file path that produced this output (for targeted hints).
        source_tool: Optional tool name (e.g. "bash", "grep") for hint context.
    """
    if not text:
        return text

    # Check byte size
    byte_len = len(text.encode("utf-8", errors="replace"))
    lines = text.splitlines(keepends=True)
    line_count = len(lines)

    # Truncate individual long lines first
    lines = _truncate_long_lines(lines)

    if byte_len <= TRUNCATE_MAX_BYTES and line_count <= TRUNCATE_MAX_LINES:
        return "".join(lines)

    # Save full output to disk before truncating (like OpenCode)
    saved_path = _save_truncated_output(text)

    # Truncate by lines
    if line_count > TRUNCATE_MAX_LINES:
        head = lines[:TRUNCATE_KEEP_START]
        omitted = line_count - TRUNCATE_KEEP_START
        hint = _build_truncation_hint(source_file, source_tool, TRUNCATE_KEEP_START, saved_path)
        return (
            "".join(head)
            + f"\n\n[... {omitted:,} lines omitted ({line_count:,} total)]\n"
            + hint + "\n"
        )

    # Truncate by bytes (lines fit but total bytes too large)
    kept_lines: list[str] = []
    total = 0
    for line in lines:
        line_bytes = len(line.encode("utf-8", errors="replace"))
        if total + line_bytes > TRUNCATE_MAX_BYTES:
            break
        kept_lines.append(line)
        total += line_bytes

    remaining = line_count - len(kept_lines)
    hint = _build_truncation_hint(source_file, source_tool, len(kept_lines), saved_path)
    return (
        "".join(kept_lines)
        + f"\n\n[... truncated at ~{TRUNCATE_MAX_BYTES // 1024}KB — "
        f"{remaining:,} more lines]\n"
        + hint + "\n"
    )


# ── Layer 3: Compaction ───────────────────────────────────────────

def estimate_history_tokens(history: list[dict]) -> int:
    """Estimate token count from conversation history chars (~3.5 chars/token).

    Works on both flat-text legacy history and block-shaped history.
    """
    from aru.history_blocks import item_char_len
    total_chars = sum(item_char_len(msg) for msg in history)
    return int(total_chars / 3.5)


def should_compact(
    history_or_tokens: int | list[dict[str, str]],
    model_id: str = "default",
) -> bool:
    """Check if the conversation should be compacted.

    Fires when the per-call context window reaches real overflow:
    `tokens >= limit - COMPACTION_BUFFER_TOKENS`.

    Matches opencode's `isOverflow` in overflow.ts:22 — `count >= usable`,
    no extra ratio. Routine context reduction is handled by `prune_history`
    (lossy only on tool outputs), so compaction is reserved for genuine
    overflow where the next API call would otherwise exceed the model's
    input limit minus the reserved buffer.

    Accepts either an estimated token count (int) or the history list.
    """
    if isinstance(history_or_tokens, list):
        tokens = estimate_history_tokens(history_or_tokens)
    else:
        tokens = history_or_tokens

    limit = MODEL_CONTEXT_LIMITS.get(model_id, MODEL_CONTEXT_LIMITS["default"])
    usable = limit - COMPACTION_BUFFER_TOKENS
    return tokens >= usable


def would_prune(history: list[dict], model_id: str = "default") -> bool:
    """Check if prune_history would discard content from this history.

    Uses the same entry gate as `prune_history`: total tool_result
    content must exceed the protection window + minimum prunable
    threshold. Text and tool_use args are not counted — only real
    prunable output. Mirrors opencode's logic.
    """
    if len(history) <= 2:
        return False
    total_tool_chars = sum(_tool_result_content_len(msg) for msg in history)
    protect_chars = _get_prune_protect_chars(model_id)
    return total_tool_chars >= protect_chars + PRUNE_MINIMUM_CHARS


def _split_history(history: list[dict], model_id: str = "default") -> tuple[list[dict], list[dict]]:
    """Split history into old (to summarize) and recent (to keep intact).

    Uses `COMPACT_RECENT_CHARS` (80K chars ≈ 20K tokens) as the "recent"
    budget — half of the prune protect window. Rationale: the compactor
    now runs on the main model and produces high-fidelity summaries, so
    we don't need 40K of recent overlap as a safety net. 20K covers 3-6
    recent turns verbatim, which is enough to absorb the gap between
    the last summarized state and the next turn.

    Defensively, the first user turn (index 0) is always pulled into
    `recent` so the original ask survives literal even through a full
    compaction — the compactor extracts it into the `## Goal` section
    of the summary, but keeping it in recent too means the agent can
    quote it verbatim afterward.

    The `model_id` parameter is retained for signature compatibility;
    the recent budget is a flat value not scaled by model context.
    """
    del model_id  # unused — recent budget is flat across models
    from aru.history_blocks import item_char_len
    protected = 0
    split_idx = len(history)
    for i in range(len(history) - 1, -1, -1):
        msg_len = item_char_len(history[i])
        if protected + msg_len <= COMPACT_RECENT_CHARS:
            protected += msg_len
            split_idx = i
        else:
            break

    # Defensive: force the first user turn into `recent` even if the
    # protect budget would have sent it to `old`. The original ask is
    # the session anchor and must stay literal.
    if split_idx > 0 and history and history[0].get("role") == "user":
        return history[1:split_idx], [history[0]] + history[split_idx:]
    return history[:split_idx], history[split_idx:]


def build_compaction_prompt(
    history: list[dict],
    plan_task: str | None = None,
    model_id: str = "default",
) -> str:
    """Build the prompt sent to the compaction agent.

    Only includes OLD messages (outside the protection window) for
    summarization. Recent messages are kept intact by apply_compaction.
    """
    from aru.history_blocks import item_text
    old_msgs, _ = _split_history(history, model_id)

    parts = [COMPACTION_TEMPLATE, "\n\n---\n\n## Conversation to summarize:\n"]

    if plan_task:
        parts.append(f"**Active task:** {plan_task}\n\n")

    import re as _re
    _code_block_re = _re.compile(r"(```[\s\S]*?```)")
    _CODE_BLOCK_LIMIT = 3_000
    _MSG_LIMIT = 8_000

    def _truncate_code_block(match: _re.Match) -> str:
        block = match.group(1)
        if len(block) <= _CODE_BLOCK_LIMIT:
            return block
        # Preserve the opening fence line and truncate the body
        fence_end = block.find("\n")
        if fence_end == -1:
            return block[:_CODE_BLOCK_LIMIT]
        opener = block[:fence_end + 1]
        body_budget = _CODE_BLOCK_LIMIT - len(opener) - 4  # room for closing ```
        body = block[fence_end + 1:-3] if block.endswith("```") else block[fence_end + 1:]
        truncated_body = body[:body_budget]
        return (
            f"{opener}{truncated_body}\n"
            f"... [code block truncated to {_CODE_BLOCK_LIMIT} chars]\n```"
        )

    for msg in old_msgs:
        role = msg["role"].upper()
        # Project block content into text for the compactor. item_text handles
        # tool_use/tool_result blocks with bracketed placeholders.
        content = item_text(msg)
        # Truncate large code blocks instead of removing them wholesale — the
        # compactor still needs enough file content to produce useful excerpts.
        content = _code_block_re.sub(_truncate_code_block, content)
        # Cap individual messages (more generous than before to fit excerpts)
        if len(content) > _MSG_LIMIT:
            content = content[:_MSG_LIMIT] + f"... [{len(content) - _MSG_LIMIT} chars truncated]"
        parts.append(f"**{role}:** {content}\n\n")

    return "".join(parts)



def apply_compaction(
    history: list[dict], summary: str, model_id: str = "default"
) -> list[dict]:
    """Replace OLD messages with a summary, keep RECENT messages intact.

    The summary is emitted as a synthetic user→assistant exchange so that
    role alternation stays natural:
        [user: "Please summarize..."]
        [assistant: "<summary>", summary=True]
        + recent messages as-is

    The assistant summary is marked with `summary: True` as a checkpoint.
    `prune_history` walks backward and stops at this marker, so content
    already consolidated into the summary is never re-processed. Mirrors
    opencode's `msg.info.summary` flag (see message-v2.ts:914).
    """
    from aru.history_blocks import text_block, coerce_history_item
    _, recent = _split_history(history, model_id)

    compacted: list[dict] = [
        {
            "role": "user",
            "content": [text_block("Please summarize the prior conversation so we can continue.")],
        },
        {
            "role": "assistant",
            "content": [text_block(f"Prior conversation summary:\n\n{summary}")],
            "summary": True,
        },
    ]
    compacted.extend(coerce_history_item(m) for m in recent)

    return compacted


async def compact_conversation(
    history: list[dict[str, str]],
    model_ref: str,
    plan_task: str | None = None,
    model_id: str = "default",
) -> list[dict[str, str]]:
    """Run the compaction agent to summarize and replace history.

    Uses the **same model** as the main session (`model_ref`), not a
    cheaper small model. Rationale:

    - Compaction is rare (only on real overflow, ~0-2× per long session).
    - The summary is the *only* persistent record of pre-window history.
    - A weaker compactor risks dropping subtle decisions that the main
      model would have caught — and once dropped, they cannot be recovered
      mid-session.
    - The marginal cost (Sonnet: ~$0.20-0.40 per session; Opus: a few
      dollars) is justified by the fidelity gain on a non-recoverable
      step.

    Falls back to a mechanical summary if the agent call fails.
    """
    from aru.providers import create_model

    prompt = build_compaction_prompt(history, plan_task, model_id=model_id)

    try:
        from agno.agent import Agent

        compactor = Agent(
            name="Compactor",
            model=create_model(model_ref, max_tokens=4096),
            instructions=(
                "You summarize coding conversations concisely. Output ONLY the requested sections, no preamble. "
                "Preserve: user goals, explicit instructions/preferences, file paths with line numbers, "
                "function/class names that were modified, what remains to be done. "
                "For the '## File contents (key excerpts)' section, use your judgment: "
                "if a file was central to the work (being debugged, actively edited, or referenced "
                "in a decision), include the critical lines verbatim; if a file was only briefly "
                "read for context, just list the path. Do not mechanically copy everything. "
                "Drop: greetings, reasoning chains, redundant tool output, transient status messages."
            ),
            markdown=True,
        )

        result = await compactor.arun(prompt, stream=False)
        summary = result.content if result and result.content else ""

        if not summary:
            # Fallback: simple mechanical summary
            summary = _fallback_summary(history, plan_task)

        return apply_compaction(history, summary, model_id=model_id)

    except Exception:
        # Fallback if agent fails
        summary = _fallback_summary(history, plan_task)
        return apply_compaction(history, summary, model_id=model_id)


def _fallback_summary(history: list[dict], plan_task: str | None = None) -> str:
    """Mechanical summary when the compaction agent is unavailable.

    Operates on block-shaped history. Uses `item_text` to project each
    message's content blocks into a string for regex extraction and
    excerpting.
    """
    from aru.history_blocks import item_text

    parts = []
    if plan_task:
        parts.append(f"**Task:** {plan_task}")

    msg_count = len(history)
    user_msgs = sum(1 for m in history if m["role"] == "user")
    parts.append(f"**Conversation:** {msg_count} messages ({user_msgs} from user)")

    # Extract file paths mentioned
    import re
    all_text = " ".join(item_text(m) for m in history)
    files = set(re.findall(r'[\w./\\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|md|json|yaml|yml|toml)', all_text))
    if files:
        parts.append(f"**Files referenced:** {', '.join(sorted(files)[:20])}")

    # Keep last 3 messages as brief excerpts
    parts.append("\n**Recent context:**")
    for msg in history[-3:]:
        role = msg["role"]
        text = item_text(msg)
        if len(text) > 300:
            text = text[:300] + "..."
        parts.append(f"- [{role}]: {text}")

    return "\n".join(parts)


def format_context_block(content: str, label: str = "Context", include_timestamp: bool = True) -> str:
    """Format a context block with separator and optional timestamp.
    
    Args:
        content: The content to include in the block.
        label: Label for the context block.
        include_timestamp: Whether to include timestamp in the separator.
    
    Returns:
        Formatted context block with separators and timestamp.
    """
    from datetime import datetime
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if include_timestamp:
        separator = f"-- {label} ({timestamp}) --"
    else:
        separator = f"-- {label} --"
    
    return f"{separator}\n{content}\n{separator}"

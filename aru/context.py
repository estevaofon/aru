"""Context management for token optimization.

Implements three layers of token reduction:
1. Pruning — evict old tool/assistant outputs from history
2. Truncation — universal cap on tool output size
3. Compaction — summarize entire conversation when approaching context limits
"""

from __future__ import annotations

# ── Constants ──────────────────────────────────────────────────────

# Pruning: minimum chars that must be freeable to justify a prune pass
PRUNE_MINIMUM_CHARS = 12_000  # ~3.5K tokens
# Placeholder that replaces evicted content
PRUNED_PLACEHOLDER = "[cleared]"
# User messages larger than this threshold are truncated when outside protection window
PRUNE_USER_MSG_THRESHOLD = 2_000  # ~570 tokens
# How many chars to keep from the start of a pruned user message
PRUNE_USER_MSG_KEEP = 500  # ~140 tokens
# Minimum number of recent user turns always protected (regardless of char budget)
PRUNE_PROTECT_TURNS = 2
# Tool result markers that should never be pruned (critical context)
PRUNE_PROTECTED_MARKERS = {"[SubAgent-", "delegate_task"}
# Tool names whose outputs should never be pruned (like OpenCode's PRUNE_PROTECTED_TOOLS)
# These are checked as substrings in message content (tool results include the tool name)
PRUNE_PROTECTED_TOOLS = {"delegate_task"}

# Truncation: universal limits for any tool output
TRUNCATE_MAX_LINES = 300
TRUNCATE_MAX_BYTES = 15 * 1024  # 15 KB
TRUNCATE_KEEP_START = 150  # lines to keep from the start
TRUNCATE_KEEP_END = 60  # lines to keep from the end
TRUNCATE_MAX_LINE_LENGTH = 1500  # chars per individual line (prevents minified files)
# Directory for saving full truncated outputs (like OpenCode pattern)
TRUNCATE_SAVE_DIR = ".aru/truncated"

# Compaction: trigger when per-run input tokens exceed this fraction of model limit
COMPACTION_THRESHOLD_RATIO = 0.70
# Compaction: target post-compaction size as fraction of model context limit
COMPACTION_TARGET_RATIO = 0.15
# Compaction: also trigger after this many user turns (regardless of token count)
COMPACTION_MAX_TURNS = 15
# Compaction: reserve buffer for the compaction process itself (like OpenCode's 20K)
COMPACTION_BUFFER_TOKENS = 20_000
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

def _get_prune_protect_chars(model_id: str = "default") -> int:
    """Scale protection window based on model context size.

    Returns the number of chars worth of recent history that should NEVER
    be pruned. The remaining history beyond this window is eligible for
    reversible pruning.

    Sizing rationale: the target is a steady-state per-call context
    window of ~20K tokens (what the user sees in the status bar), which
    means protected history should be ~17K tokens = ~60K chars. This
    floor is applied to every model; larger models get more protection
    scaled at ~7% of their context, capped at 200K chars (~57K tokens)
    to avoid protecting too much in 1M-context models where the extra
    history hurts prompt caching.
    """
    limit = MODEL_CONTEXT_LIMITS.get(model_id, MODEL_CONTEXT_LIMITS["default"])
    # ~4 chars per token, protect ~7% of context as the ratio ceiling
    ratio_based = int(limit * 0.07 * 4)
    # Floor of 60K chars (~17K tokens) keeps the user-visible context
    # window around 20K tokens steady-state after system + cache + output
    # overheads. Applies to any model where 7% would be smaller.
    return max(60_000, min(ratio_based, 200_000))


def prune_history(
    history: list[dict], model_id: str = "default"
) -> list[dict]:
    """Reduce history token footprint by dropping old content blocks.

    Operates on block-shaped history (see `aru.history_blocks`). The
    algorithm walks backward accumulating a char budget, and for any
    message that falls outside the protection window:

    - `text` blocks on assistant messages → replaced with `[cleared]`
      text block.
    - Large `text` blocks on user messages → truncated to first N chars.
    - `tool_use` blocks → dropped **together with** their matching
      `tool_result` block in the subsequent tool/user message. Dropping
      them atomically is required: Anthropic's API rejects orphans with
      `400: tool_use_id not found`.
    - `tool_result` blocks → dropped only when their paired `tool_use`
      is also dropped.

    Protection layers:
    1. Turn-based: last `PRUNE_PROTECT_TURNS` user turns always kept
       intact, along with the assistant response right after each.
    2. Char-based: recent content within the protection window is kept.
    3. Content-based: messages whose stringified content contains any
       `PRUNE_PROTECTED_MARKERS` or `PRUNE_PROTECTED_TOOLS` never prune.

    Returns a new list (does not mutate the input).
    """
    from aru.history_blocks import (
        coerce_history_item, item_char_len, item_text,
        is_text, is_tool_use, is_tool_result, text_block,
    )

    if len(history) <= 2:
        return [coerce_history_item(m) for m in history]

    protect_chars = _get_prune_protect_chars(model_id)
    result = [coerce_history_item(m) for m in history]

    total_chars = sum(item_char_len(msg) for msg in result)
    if total_chars < protect_chars + PRUNE_MINIMUM_CHARS:
        return result

    # Identify indices of last N user turns (always protected)
    turn_protected: set[int] = set()
    user_turns_seen = 0
    for i in range(len(result) - 1, -1, -1):
        if result[i]["role"] == "user":
            user_turns_seen += 1
            if user_turns_seen <= PRUNE_PROTECT_TURNS:
                turn_protected.add(i)
                if i + 1 < len(result):
                    turn_protected.add(i + 1)

    # Build a map of tool_use_id → (assistant_idx, user_idx) so we can
    # drop both halves of a pair atomically. The user_idx points to the
    # next message(s) after the assistant carrying the matching tool_result.
    tool_pair_loc: dict[str, tuple[int, int]] = {}
    for i, msg in enumerate(result):
        if msg["role"] != "assistant":
            continue
        for block in msg["content"]:
            if not is_tool_use(block):
                continue
            tu_id = block.get("id")
            if not tu_id:
                continue
            # Look forward for the matching tool_result (usually i+1)
            for j in range(i + 1, min(i + 3, len(result))):
                for rb in result[j]["content"]:
                    if is_tool_result(rb) and rb.get("tool_use_id") == tu_id:
                        tool_pair_loc[tu_id] = (i, j)
                        break
                if tu_id in tool_pair_loc:
                    break

    # Walk backward, protecting recent content
    protected = 0
    dropped_tool_use_ids: set[str] = set()

    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        msg_len = item_char_len(msg)

        if i in turn_protected:
            protected += msg_len
            continue

        if protected + msg_len <= protect_chars:
            protected += msg_len
            continue

        # Outside protection window — check content-based protection
        text_view = item_text(msg)
        if (any(marker in text_view for marker in PRUNE_PROTECTED_MARKERS)
                or any(tool in text_view for tool in PRUNE_PROTECTED_TOOLS)):
            protected += msg_len
            continue

        # Prune this message's blocks
        new_blocks: list[dict] = []
        for block in msg["content"]:
            if is_text(block):
                if msg["role"] == "assistant":
                    # Replace with a single placeholder (only if not already)
                    if not new_blocks or new_blocks[-1].get("text") != PRUNED_PLACEHOLDER:
                        new_blocks.append(text_block(PRUNED_PLACEHOLDER))
                elif msg["role"] == "user":
                    text = block.get("text", "")
                    if len(text) > PRUNE_USER_MSG_THRESHOLD:
                        truncated = (
                            text[:PRUNE_USER_MSG_KEEP]
                            + f"\n\n[... {len(text) - PRUNE_USER_MSG_KEEP:,} "
                              "chars pruned to save context ...]"
                        )
                        new_blocks.append(text_block(truncated))
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)
            elif is_tool_use(block):
                # Drop the tool_use entirely and mark its id for paired removal
                tu_id = block.get("id")
                if tu_id:
                    dropped_tool_use_ids.add(tu_id)
                # Do NOT add to new_blocks
            elif is_tool_result(block):
                # Drop only if its paired tool_use is also being dropped
                tu_id = block.get("tool_use_id")
                if tu_id in dropped_tool_use_ids:
                    pass  # drop
                else:
                    new_blocks.append(block)
            else:
                new_blocks.append(block)

        result[i] = {"role": msg["role"], "content": new_blocks}

    # Second pass: any tool_result blocks in user messages whose tool_use
    # was dropped on a previous pass (covers case where user msg was
    # inside protection but its paired assistant was outside).
    if dropped_tool_use_ids:
        for idx, msg in enumerate(result):
            if not msg["content"]:
                continue
            filtered = [
                b for b in msg["content"]
                if not (is_tool_result(b) and b.get("tool_use_id") in dropped_tool_use_ids)
            ]
            if len(filtered) != len(msg["content"]):
                result[idx] = {"role": msg["role"], "content": filtered}

    # Drop any messages that ended up with zero blocks (valid but useless)
    result = [m for m in result if m["content"]]

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

    Triggers on EITHER condition:
    1. Token-based: tokens >= usable_context * threshold_ratio
    2. Turn-based: user turns >= COMPACTION_MAX_TURNS (prevents slow token creep)

    Accepts either an estimated token count (int) or the history list.
    """
    if isinstance(history_or_tokens, list):
        history = history_or_tokens
        tokens = estimate_history_tokens(history)
        # Turn-based trigger: count user messages
        user_turns = sum(1 for m in history if m["role"] == "user")
        if user_turns >= COMPACTION_MAX_TURNS:
            return True
    else:
        tokens = history_or_tokens

    limit = MODEL_CONTEXT_LIMITS.get(model_id, MODEL_CONTEXT_LIMITS["default"])
    usable = limit - COMPACTION_BUFFER_TOKENS
    threshold = int(usable * COMPACTION_THRESHOLD_RATIO)
    return tokens >= threshold


def would_prune(history: list[dict], model_id: str = "default") -> bool:
    """Check if prune_history would discard content from this history.

    Uses the exact same criteria as prune_history: total chars exceed
    the protection window + minimum prunable threshold.
    """
    from aru.history_blocks import item_char_len
    if len(history) <= 2:
        return False
    total_chars = sum(item_char_len(msg) for msg in history)
    protect_chars = _get_prune_protect_chars(model_id)
    return total_chars >= protect_chars + PRUNE_MINIMUM_CHARS


def _split_history(history: list[dict], model_id: str = "default") -> tuple[list[dict], list[dict]]:
    """Split history into old (to summarize) and recent (to keep intact).

    Uses the same protection window as pruning.
    """
    from aru.history_blocks import item_char_len
    protect_chars = _get_prune_protect_chars(model_id)
    protected = 0
    split_idx = len(history)
    for i in range(len(history) - 1, -1, -1):
        msg_len = item_char_len(history[i])
        if protected + msg_len <= protect_chars:
            protected += msg_len
            split_idx = i
        else:
            break
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
        [assistant: "<summary>"]
        + recent messages as-is

    This shape avoids the `[user, user, ...]` sequence that previously
    biased the model toward describing actions rather than emitting
    structured tool calls.
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

    Uses a small/fast model for the summarization to minimize cost.
    Falls back to simple truncation if the agent call fails.
    """
    from aru.runtime import get_ctx
    from aru.providers import create_model

    prompt = build_compaction_prompt(history, plan_task, model_id=model_id)

    try:
        from agno.agent import Agent

        small_ref = get_ctx().small_model_ref
        compactor = Agent(
            name="Compactor",
            model=create_model(small_ref, max_tokens=4096),
            instructions=(
                "You summarize coding conversations concisely. Output ONLY the requested sections, no preamble. "
                "Preserve: user goals, explicit instructions/preferences, file paths with line numbers, "
                "function/class names that were modified, what remains to be done, AND verbatim excerpts "
                "from any file contents shown in the conversation (signatures, critical constants, "
                "bug-related lines) under the '## File contents (key excerpts)' section. "
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

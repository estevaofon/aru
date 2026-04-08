"""Context management for token optimization.

Implements three layers of token reduction:
1. Pruning — evict old tool/assistant outputs from history
2. Truncation — universal cap on tool output size
3. Compaction — summarize entire conversation when approaching context limits
"""

from __future__ import annotations

# ── Constants ──────────────────────────────────────────────────────

# Pruning: minimum chars that must be freeable to justify a prune pass
PRUNE_MINIMUM_CHARS = 8_000  # ~2K tokens (was 12K — prune sooner)
# Placeholder that replaces evicted content
PRUNED_PLACEHOLDER = "[cleared]"
# User messages larger than this threshold are truncated when outside protection window
PRUNE_USER_MSG_THRESHOLD = 1_200  # ~340 tokens (was 2K — catch file contents earlier)
# How many chars to keep from the start of a pruned user message
PRUNE_USER_MSG_KEEP = 300  # ~85 tokens (was 500 — enough for the request intent)
# Minimum number of recent user turns always protected (regardless of char budget)
PRUNE_PROTECT_TURNS = 1  # was 2 — only protect the very last turn
# Tool result markers that should never be pruned (critical context)
PRUNE_PROTECTED_MARKERS = {"[SubAgent-", "delegate_task"}
# Tool names whose outputs should never be pruned (like OpenCode's PRUNE_PROTECTED_TOOLS)
# These are checked as substrings in message content (tool results include the tool name)
PRUNE_PROTECTED_TOOLS = {"delegate_task"}

# Truncation: universal limits for any tool output
TRUNCATE_MAX_LINES = 200  # was 300 — tighter to save context
TRUNCATE_MAX_BYTES = 10 * 1024  # 10 KB (was 15KB — save full to disk instead)
TRUNCATE_KEEP_START = 150  # lines to keep from the start
TRUNCATE_KEEP_END = 30  # lines to keep from the end (was 60)
TRUNCATE_MAX_LINE_LENGTH = 1500  # chars per individual line (prevents minified files)
# Directory for saving full truncated outputs (like OpenCode pattern)
TRUNCATE_SAVE_DIR = ".aru/truncated"

# Compaction: trigger when per-run input tokens exceed this fraction of model limit
COMPACTION_THRESHOLD_RATIO = 0.50  # was 0.70 — compact much earlier to stay lean
# Compaction: target post-compaction size as fraction of model context limit
COMPACTION_TARGET_RATIO = 0.10  # was 0.15 — more aggressive compaction target
# Compaction: also trigger after this many user turns (regardless of token count)
COMPACTION_MAX_TURNS = 8
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

## Relevant files / directories
Structured list of file paths relevant to continuing the work (one per line)."""


# ── Layer 1: Pruning ──────────────────────────────────────────────

def _get_prune_protect_chars(model_id: str = "default") -> int:
    """Scale protection window based on model context size.

    Larger models get more protection; smaller models prune more aggressively
    to prevent context overflow. Returns ~7% of the model's context in chars.
    """
    limit = MODEL_CONTEXT_LIMITS.get(model_id, MODEL_CONTEXT_LIMITS["default"])
    # ~4 chars per token, protect ~5% of context (was 7% — tighter budget)
    protect = int(limit * 0.05 * 4)
    # Clamp between 10K (minimum usable) and 40K (diminishing returns)
    return max(10_000, min(protect, 40_000))


def prune_history(
    history: list[dict[str, str]], model_id: str = "default"
) -> list[dict[str, str]]:
    """Replace old messages with a short placeholder to reduce tokens.

    Walks backward through history, protecting the most recent content
    (scaled to the model's context size). Older messages beyond that
    budget are pruned:
    - Assistant messages: replaced entirely with placeholder (unless protected)
    - User messages over PRUNE_USER_MSG_THRESHOLD: truncated to first N chars

    Protection layers:
    1. Turn-based: last PRUNE_PROTECT_TURNS user turns always kept
    2. Char-based: recent content within the protection window
    3. Content-based: messages containing PRUNE_PROTECTED_MARKERS never pruned

    Returns a new list (does not mutate the input).
    """
    if len(history) <= 2:
        return list(history)

    protect_chars = _get_prune_protect_chars(model_id)

    # Calculate total prunable chars (both roles)
    total_chars = sum(len(msg["content"]) for msg in history)

    # Not enough to prune
    if total_chars < protect_chars + PRUNE_MINIMUM_CHARS:
        return list(history)

    # Identify indices of last N user turns (always protected)
    turn_protected: set[int] = set()
    user_turns_seen = 0
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "user":
            user_turns_seen += 1
            if user_turns_seen <= PRUNE_PROTECT_TURNS:
                turn_protected.add(i)
                # Also protect the assistant response right after this user turn
                if i + 1 < len(history):
                    turn_protected.add(i + 1)

    # Walk backward, protecting recent content
    result = list(history)
    protected = 0

    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        msg_len = len(msg["content"])

        # Turn-based protection: never prune last N user turns
        if i in turn_protected:
            protected += msg_len
            continue

        if protected + msg_len <= protect_chars:
            # Still within protection window
            protected += msg_len
        else:
            # Check protected markers and tool names before pruning
            content = msg["content"]
            if (any(marker in content for marker in PRUNE_PROTECTED_MARKERS)
                    or any(tool in content for tool in PRUNE_PROTECTED_TOOLS)):
                protected += msg_len
                continue

            # Beyond protection window — prune
            if msg["role"] == "assistant":
                if msg["content"] != PRUNED_PLACEHOLDER:
                    result[i] = {"role": "assistant", "content": PRUNED_PLACEHOLDER}
            elif msg["role"] == "user" and msg_len > PRUNE_USER_MSG_THRESHOLD:
                truncated = msg["content"][:PRUNE_USER_MSG_KEEP] + \
                    f"\n\n[... {msg_len - PRUNE_USER_MSG_KEEP:,} chars pruned to save context ...]"
                result[i] = {"role": "user", "content": truncated}

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

def estimate_history_tokens(history: list[dict[str, str]]) -> int:
    """Estimate token count from conversation history chars (~3.5 chars/token)."""
    total_chars = sum(len(msg["content"]) for msg in history)
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


def would_prune(history: list[dict[str, str]], model_id: str = "default") -> bool:
    """Check if prune_history would discard content from this history.

    Uses the exact same criteria as prune_history: total chars exceed
    the protection window + minimum prunable threshold.
    """
    if len(history) <= 2:
        return False
    total_chars = sum(len(msg["content"]) for msg in history)
    protect_chars = _get_prune_protect_chars(model_id)
    return total_chars >= protect_chars + PRUNE_MINIMUM_CHARS


def _split_history(history: list[dict[str, str]], model_id: str = "default") -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split history into old (to summarize) and recent (to keep intact).

    Uses the same protection window as pruning.
    """
    protect_chars = _get_prune_protect_chars(model_id)
    protected = 0
    split_idx = len(history)
    for i in range(len(history) - 1, -1, -1):
        msg_len = len(history[i]["content"])
        if protected + msg_len <= protect_chars:
            protected += msg_len
            split_idx = i
        else:
            break
    return history[:split_idx], history[split_idx:]


def build_compaction_prompt(
    history: list[dict[str, str]],
    plan_task: str | None = None,
    model_id: str = "default",
) -> str:
    """Build the prompt sent to the compaction agent.

    Only includes OLD messages (outside the protection window) for
    summarization. Recent messages are kept intact by apply_compaction.
    """
    old_msgs, _ = _split_history(history, model_id)

    parts = [COMPACTION_TEMPLATE, "\n\n---\n\n## Conversation to summarize:\n"]

    if plan_task:
        parts.append(f"**Active task:** {plan_task}\n\n")

    import re as _re
    _code_block_re = _re.compile(r"```[\s\S]*?```")

    for msg in old_msgs:
        role = msg["role"].upper()
        content = msg["content"]
        # Strip large code blocks — compactor only needs to know what was done, not raw code
        content = _code_block_re.sub("[code block removed]", content)
        # Cap individual messages in the compaction input to avoid blowing up
        if len(content) > 2000:
            content = content[:2000] + f"... [{len(content) - 2000} chars truncated]"
        parts.append(f"**{role}:** {content}\n\n")

    return "".join(parts)



def apply_compaction(
    history: list[dict[str, str]], summary: str, model_id: str = "default"
) -> list[dict[str, str]]:
    """Replace OLD messages with a summary, keep RECENT messages intact.

    Uses the same protection window as pruning: recent messages within
    the window are preserved as-is, older messages are replaced by a
    compaction summary. Replays the last user message to maintain continuity.
    """
    _, recent = _split_history(history, model_id)

    compacted = [
        {"role": "user", "content": f"[Conversation compacted]\n\n{summary}"}
    ]
    compacted.extend(recent)

    # Replay: ensure the last message is from the user so the LLM continues naturally
    if not compacted or compacted[-1]["role"] != "user":
        # Find last user message in original history for replay
        last_user = None
        for msg in reversed(history):
            if msg["role"] == "user":
                last_user = msg["content"]
                break
        if last_user:
            # Truncate replayed message to avoid re-bloating context
            replay = last_user[:1000] if len(last_user) > 1000 else last_user
            compacted.append({"role": "user", "content": replay})
        else:
            compacted.append({"role": "user", "content": "Continue if you have next steps, or stop and ask for clarification."})

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
            model=create_model(small_ref, max_tokens=2048),
            instructions=(
                "You summarize coding conversations concisely. Output ONLY the requested sections, no preamble. "
                "Preserve: user goals, explicit instructions/preferences, file paths with line numbers, "
                "function/class names that were modified, and what remains to be done. "
                "Drop: raw code blocks, tool output details, greetings, reasoning."
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


def _fallback_summary(history: list[dict[str, str]], plan_task: str | None = None) -> str:
    """Mechanical summary when the compaction agent is unavailable."""
    parts = []
    if plan_task:
        parts.append(f"**Task:** {plan_task}")

    msg_count = len(history)
    user_msgs = sum(1 for m in history if m["role"] == "user")
    parts.append(f"**Conversation:** {msg_count} messages ({user_msgs} from user)")

    # Extract file paths mentioned
    import re
    all_text = " ".join(m["content"] for m in history)
    files = set(re.findall(r'[\w./\\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|md|json|yaml|yml|toml)', all_text))
    if files:
        parts.append(f"**Files referenced:** {', '.join(sorted(files)[:20])}")

    # Keep last 3 messages as brief excerpts
    parts.append("\n**Recent context:**")
    for msg in history[-3:]:
        role = msg["role"]
        text = msg["content"][:300]
        if len(msg["content"]) > 300:
            text += "..."
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

"""Context management for token optimization.

Implements three layers of token reduction:
1. Pruning — evict old tool/assistant outputs from history
2. Truncation — universal cap on tool output size
3. Compaction — summarize entire conversation when approaching context limits
"""

from __future__ import annotations

# ── Constants ──────────────────────────────────────────────────────

# Pruning: minimum chars that must be freeable to justify a prune pass
PRUNE_MINIMUM_CHARS = 20_000  # ~5.7K tokens
# Placeholder that replaces evicted content
PRUNED_PLACEHOLDER = "[previous output cleared to save context]"
# User messages larger than this threshold are truncated when outside protection window
PRUNE_USER_MSG_THRESHOLD = 2_000  # ~570 tokens — catches @file mentions
# How many chars to keep from the start of a pruned user message
PRUNE_USER_MSG_KEEP = 500  # ~140 tokens — enough to understand the request

# Truncation: universal limits for any tool output
TRUNCATE_MAX_LINES = 500
TRUNCATE_MAX_BYTES = 20 * 1024  # 20 KB
TRUNCATE_KEEP_START = 350  # lines to keep from the start
TRUNCATE_KEEP_END = 100  # lines to keep from the end

# Compaction: trigger when per-run input tokens exceed this fraction of model limit
COMPACTION_THRESHOLD_RATIO = 0.85
# Compaction: target post-compaction size as fraction of model context limit
COMPACTION_TARGET_RATIO = 0.15
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
Summarize this conversation concisely. Preserve:
1. **Goal**: What the user wants to accomplish
2. **Key decisions**: Important choices made during the conversation
3. **Discoveries**: What was learned about the codebase or problem
4. **Accomplished**: What has been done so far (be specific about files changed)
5. **Relevant files**: File paths that are important for continuing the work
6. **Next steps**: What remains to be done

Be concise but complete. This summary replaces the full conversation history."""


# ── Layer 1: Pruning ──────────────────────────────────────────────

def _get_prune_protect_chars(model_id: str = "default") -> int:
    """Scale protection window based on model context size.

    Larger models get more protection; smaller models prune more aggressively
    to delay compaction. Returns ~10% of the model's context in chars (~3.5 chars/token).
    """
    limit = MODEL_CONTEXT_LIMITS.get(model_id, MODEL_CONTEXT_LIMITS["default"])
    # ~3.5 chars per token, protect ~10% of context
    protect = int(limit * 0.10 * 3.5)
    # Clamp between 20K (minimum usable) and 80K (diminishing returns)
    return max(20_000, min(protect, 80_000))


def prune_history(
    history: list[dict[str, str]], model_id: str = "default"
) -> list[dict[str, str]]:
    """Replace old messages with a short placeholder to reduce tokens.

    Walks backward through history, protecting the most recent content
    (scaled to the model's context size). Older messages beyond that
    budget are pruned:
    - Assistant messages: replaced entirely with placeholder
    - User messages over PRUNE_USER_MSG_THRESHOLD: truncated to first N chars

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

    # Walk backward, protecting recent content
    result = list(history)
    protected = 0

    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        msg_len = len(msg["content"])

        if protected + msg_len <= protect_chars:
            # Still within protection window
            protected += msg_len
        else:
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

def truncate_output(text: str) -> str:
    """Universal truncation for tool outputs.

    Caps output at TRUNCATE_MAX_BYTES / TRUNCATE_MAX_LINES, keeping the
    start and end with a middle marker showing what was cut.
    """
    if not text:
        return text

    # Check byte size
    byte_len = len(text.encode("utf-8", errors="replace"))
    lines = text.splitlines(keepends=True)
    line_count = len(lines)

    if byte_len <= TRUNCATE_MAX_BYTES and line_count <= TRUNCATE_MAX_LINES:
        return text

    # Truncate by lines
    if line_count > TRUNCATE_MAX_LINES:
        head = lines[:TRUNCATE_KEEP_START]
        tail = lines[-TRUNCATE_KEEP_END:]
        omitted = line_count - TRUNCATE_KEEP_START - TRUNCATE_KEEP_END
        return (
            "".join(head)
            + f"\n\n[... {omitted:,} lines omitted ({line_count:,} total) — "
            f"use offset/limit or a more specific query ...]\n\n"
            + "".join(tail)
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
    return (
        "".join(kept_lines)
        + f"\n\n[... truncated at ~{TRUNCATE_MAX_BYTES // 1024}KB — "
        f"{remaining:,} more lines — use offset/limit to read further ...]\n"
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
    """Check if the conversation should be compacted (reactive, post-run).

    Accepts either an estimated token count (int) or the history list
    (from which tokens are estimated via char count).
    """
    if isinstance(history_or_tokens, list):
        tokens = estimate_history_tokens(history_or_tokens)
    else:
        tokens = history_or_tokens
    limit = MODEL_CONTEXT_LIMITS.get(model_id, MODEL_CONTEXT_LIMITS["default"])
    threshold = int(limit * COMPACTION_THRESHOLD_RATIO)
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

    for msg in old_msgs:
        role = msg["role"].upper()
        content = msg["content"]
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
    compaction summary. This preserves the natural conversation flow.
    """
    _, recent = _split_history(history, model_id)

    compacted = [
        {"role": "user", "content": f"[Conversation compacted]\n\n{summary}"}
    ]
    compacted.extend(recent)

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
            instructions="You summarize conversations concisely. Output ONLY the summary, no preamble.",
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

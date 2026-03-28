"""Context management for token optimization.

Implements three layers of token reduction:
1. Pruning — evict old tool/assistant outputs from history
2. Truncation — universal cap on tool output size
3. Compaction — summarize entire conversation when approaching context limits
"""

from __future__ import annotations

# ── Constants ──────────────────────────────────────────────────────

# Pruning: protect the most recent N chars of assistant content from eviction
PRUNE_PROTECT_CHARS = 50_000  # ~14K tokens
# Pruning: minimum chars that must be freeable to justify a prune pass
PRUNE_MINIMUM_CHARS = 20_000  # ~5.7K tokens
# Placeholder that replaces evicted content
PRUNED_PLACEHOLDER = "[previous output cleared to save context]"

# Truncation: universal limits for any tool output
TRUNCATE_MAX_LINES = 500
TRUNCATE_MAX_BYTES = 20 * 1024  # 20 KB
TRUNCATE_KEEP_START = 350  # lines to keep from the start
TRUNCATE_KEEP_END = 100  # lines to keep from the end

# Compaction: trigger when cumulative input tokens exceed this fraction of model limit
COMPACTION_THRESHOLD_RATIO = 0.50
# Default model context limits (input tokens)
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-sonnet-4-5-20250929": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "default": 200_000,
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

def prune_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Replace old assistant messages with a short placeholder to reduce tokens.

    Walks backward through history, protecting the most recent assistant
    content (up to PRUNE_PROTECT_CHARS). Older assistant messages beyond
    that budget are replaced with a compact placeholder.

    Returns a new list (does not mutate the input).
    """
    if len(history) <= 2:
        return list(history)

    # Calculate total assistant chars
    total_assistant_chars = sum(
        len(msg["content"]) for msg in history if msg["role"] == "assistant"
    )

    # Not enough to prune
    if total_assistant_chars < PRUNE_PROTECT_CHARS + PRUNE_MINIMUM_CHARS:
        return list(history)

    # Walk backward, protecting recent content
    result = list(history)
    protected = 0

    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        if msg["role"] != "assistant":
            continue

        msg_len = len(msg["content"])
        if protected + msg_len <= PRUNE_PROTECT_CHARS:
            # Still within protection window
            protected += msg_len
        else:
            # Beyond protection window — prune this message
            if msg["content"] != PRUNED_PLACEHOLDER:
                result[i] = {"role": "assistant", "content": PRUNED_PLACEHOLDER}

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

def should_compact(total_input_tokens: int, model_id: str = "default") -> bool:
    """Check if the conversation should be compacted based on token usage."""
    limit = MODEL_CONTEXT_LIMITS.get(model_id, MODEL_CONTEXT_LIMITS["default"])
    threshold = int(limit * COMPACTION_THRESHOLD_RATIO)
    return total_input_tokens >= threshold


def build_compaction_prompt(history: list[dict[str, str]], plan_task: str | None = None) -> str:
    """Build the prompt sent to the compaction agent to summarize the conversation."""
    parts = [COMPACTION_TEMPLATE, "\n\n---\n\n## Conversation to summarize:\n"]

    if plan_task:
        parts.append(f"**Active task:** {plan_task}\n\n")

    for msg in history:
        role = msg["role"].upper()
        content = msg["content"]
        # Cap individual messages in the compaction input to avoid blowing up
        if len(content) > 2000:
            content = content[:2000] + f"... [{len(content) - 2000} chars truncated]"
        parts.append(f"**{role}:** {content}\n\n")

    return "".join(parts)


def apply_compaction(history: list[dict[str, str]], summary: str) -> list[dict[str, str]]:
    """Replace history with a compaction summary + the most recent exchange."""
    compacted = [
        {"role": "user", "content": f"[Conversation compacted]\n\n{summary}"}
    ]
    # Keep the last user message and last assistant message for continuity
    last_user = None
    last_assistant = None
    for msg in reversed(history):
        if msg["role"] == "user" and last_user is None:
            last_user = msg
        elif msg["role"] == "assistant" and last_assistant is None:
            last_assistant = msg
        if last_user and last_assistant:
            break

    if last_assistant:
        compacted.append(last_assistant)
    if last_user and last_user != compacted[0]:
        compacted.append(last_user)

    return compacted


async def compact_conversation(
    history: list[dict[str, str]],
    model_ref: str,
    plan_task: str | None = None,
) -> list[dict[str, str]]:
    """Run the compaction agent to summarize and replace history.

    Uses a small/fast model for the summarization to minimize cost.
    Falls back to simple truncation if the agent call fails.
    """
    from aru.tools.codebase import _get_small_model_ref
    from aru.providers import create_model

    prompt = build_compaction_prompt(history, plan_task)

    try:
        from agno.agent import Agent

        small_ref = _get_small_model_ref()
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

        return apply_compaction(history, summary)

    except Exception:
        # Fallback if agent fails
        summary = _fallback_summary(history, plan_task)
        return apply_compaction(history, summary)


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

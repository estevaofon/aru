# Context Management — OpenCode Reference Parameters

The pruning/compaction system mirrors opencode's two-layer design. Below are the **canonical OpenCode-aligned values** for all tunable parameters. If values are changed for economy, these are the targets to revert to.

## Parameter Table

| File | Parameter | OpenCode Value | Purpose |
|------|-----------|---------------|---------|
| `context.py` | `PRUNE_MINIMUM_CHARS` | `80_000` (~20K tokens) | Min freeable chars to justify a prune pass |
| `context.py` | `_get_prune_protect_chars()` | `160_000` (~40K tokens, flat) | Recent tool_result content never pruned |
| `context.py` | `COMPACT_RECENT_CHARS` | `160_000` (~40K tokens) | Recent history kept verbatim after compaction |
| `context.py` | `COMPACTION_BUFFER_TOKENS` | `30_000` (opencode uses 20K) | Reserved headroom before overflow compaction fires |
| `cache_patch.py` | `_PRUNE_PROTECT_CHARS` | `160_000` (~40K tokens) | Agno-layer mirror of context.py protect window |
| `cache_patch.py` | `_PRUNE_MINIMUM_CHARS` | `80_000` (~20K tokens) | Agno-layer mirror of context.py minimum |
| `tools/codebase.py` | `read_file` `max_size` | `12_000` (12KB) | Default truncation per read_file call |
| `tools/codebase.py` | `_READ_HARD_CAP` | `40_000` (~11K tokens) | Absolute ceiling per tool result chunk |
| `tools/codebase.py` | `grep_search` `context_lines` | `10` | Lines of context around grep matches |
| `context.py` | `TRUNCATE_MAX_LINES` | `300` | Max lines in any tool output |
| `context.py` | `TRUNCATE_MAX_BYTES` | `15_360` (15KB) | Max bytes in any tool output |
| `context.py` | `TRUNCATE_KEEP_START` | `150` | Lines kept from start when truncating |
| `context.py` | `TRUNCATE_KEEP_END` | `60` | Lines kept from end when truncating |

## Design Invariants

These are the key rules from opencode that must hold regardless of parameter tuning:

- **Prune is lossy ONLY on `tool_result` content** — text blocks, tool_use blocks, and user messages are never touched. They survive verbatim until real overflow forces a full compaction.
- **Prune budget counts only `tool_result` chars**, not full message chars. Text-heavy conversations with few tool calls never trigger prune.
- **Compact fires only at real overflow** (`tokens >= limit - buffer`), no ratio trigger, no turn-based trigger. Routine context reduction is handled by prune.
- **`summary: True` marker** on the compaction assistant message acts as a prune stop-point. Walking backward in prune_history stops at this checkpoint.
- **Protect window is flat across all models** — no scaling by context size. OpenCode validated this in production on contexts from 128K to 1M.

## OpenCode Source References

- Prune protect / minimum: `session/compaction.ts:36` (`PRUNE_PROTECT = 40_000` tokens, `PRUNE_MINIMUM = 20_000` tokens)
- Overflow trigger: `session/overflow.ts:22` (`count >= limit.input - reserved`)
- Summary flag: `message-v2.ts:914` (`msg.info.summary`)
- Budget walk: `compaction.ts:119` (`Token.estimate(part.state.output)` — only tool output counts)

## Accepted Drifts from OpenCode

1. **Compaction prompt uses text projection, not modelMessages** — Agno idiomatic trade-off.
2. **No reactive overflow replay** — `COMPACTION_BUFFER_TOKENS = 30K` (vs opencode's 20K) is the hedge.
3. **`delegate_task` protected tools may be counted in budget** while opencode's `skill` protected tools are not — more conservative, acceptable.

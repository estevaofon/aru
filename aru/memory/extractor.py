"""Async extraction of durable memories from completed turns.

Subscribes to the ``turn.end`` hook (Tier 2 #3). Spawns a small-model
sub-agent with a tight prompt that returns a JSON list of candidate
memories. Writes each accepted candidate via :mod:`aru.memory.store`.

Trigger rules (opt-in):
- ``config.memory.auto_extract`` must be truthy.
- Skip when the turn's combined user+assistant token estimate is below
  ``min_turn_tokens`` (default 500). Trivial turns ("ok", "thanks") don't
  merit an extraction API call.
- Bypass the threshold when the user message explicitly asks to remember
  (regex on ``remember|lembra|salva|save``) — respects direct instruction.

Execution is fire-and-forget via ``asyncio.create_task``; failures are
logged via the plugin error ring buffer (Tier 1 #2) and never block the
next user turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from aru.memory.loader import load_memory_index
from aru.memory.store import MemoryEntry, VALID_MEMORY_TYPES, write_memory

logger = logging.getLogger("aru.memory")

_REMEMBER_HINTS = re.compile(r"\b(remember|lembr[ae]|salv[ae]|save to memory|save memory)\b", re.IGNORECASE)

DEFAULT_EXTRACT_MODEL = "anthropic/claude-haiku-4-5"
DEFAULT_MIN_TOKENS = 500

_EXTRACT_PROMPT = """\
You are a memory curator. Read the turn below and identify 0-3 facts \
worth remembering across future sessions in this project.

ONLY extract:
- **user** — user's persistent preferences or workflow rules ("use pytest", \
  "always type hints")
- **feedback** — corrections the user gave ("don't mock the DB, got burned")
- **project** — project-level state / decisions / deadlines / incidents
- **reference** — pointers to external systems (dashboards, tickets, docs)

Do NOT extract:
- Code patterns visible in the codebase (derivable from reading files)
- Ephemeral conversation state
- Anything already present in the current memory index below

Current memory index (skip duplicates):
{memory_index}

Turn:
=== user ===
{user_msg}
=== assistant ===
{assistant_msg}

Respond with a JSON array (no prose, no markdown fences) of objects:
[{{"name": "...", "description": "...", "type": "user|feedback|project|reference", "body": "..."}}]

If nothing is worth saving, return []. Keep name under 60 chars, \
description under 100, body under 400.
"""


def should_trigger(config_memory: dict[str, Any], user_msg: str,
                   turn_tokens: int) -> tuple[bool, str]:
    """Decide whether to run extraction. Returns (run?, reason)."""
    if not isinstance(config_memory, dict) or not config_memory.get("auto_extract"):
        return False, "auto_extract disabled"
    min_tokens = int(config_memory.get("min_turn_tokens", DEFAULT_MIN_TOKENS))
    if _REMEMBER_HINTS.search(user_msg or ""):
        return True, "explicit remember keyword"
    if turn_tokens < min_tokens:
        return False, f"turn too small ({turn_tokens} < {min_tokens})"
    return True, "threshold met"


async def extract_from_turn(
    project_root: str,
    user_msg: str,
    assistant_msg: str,
    *,
    model_ref: str | None = None,
    base: str | None = None,
) -> list[MemoryEntry]:
    """Run the extraction sub-agent. Returns entries already persisted to disk."""
    model_ref = model_ref or DEFAULT_EXTRACT_MODEL
    memory_index = load_memory_index(project_root, base=base) or "(empty)"

    prompt = _EXTRACT_PROMPT.format(
        memory_index=memory_index,
        user_msg=(user_msg or "")[:4000],
        assistant_msg=(assistant_msg or "")[:4000],
    )

    try:
        content = await _run_extractor_agent(prompt, model_ref)
    except Exception as exc:
        logger.warning("memory extractor agent failed: %s", exc)
        return []

    candidates = _parse_json_array(content)
    if not candidates:
        return []

    written: list[MemoryEntry] = []
    for cand in candidates[:3]:  # hard cap per turn
        entry = _candidate_to_entry(cand)
        if entry is None:
            continue
        try:
            persisted = write_memory(project_root, entry, base=base)
            written.append(persisted)
        except Exception as exc:
            logger.warning("failed to persist memory %r: %s", entry.name, exc)
    return written


async def _run_extractor_agent(prompt: str, model_ref: str) -> str:
    """Run a tools-less Agno agent with the extraction prompt and return its text."""
    from agno.agent import Agent

    from aru.providers import create_model

    agent = Agent(
        name="MemoryExtractor",
        model=create_model(model_ref, max_tokens=1024),
        tools=[],
        instructions="You curate durable memories. Output only the requested JSON.",
        markdown=False,
    )
    result = await agent.arun(prompt, stream=False)
    return (result.content or "") if result else ""


def _parse_json_array(content: str) -> list[dict]:
    content = (content or "").strip()
    # Tolerate stray prose: pull out the first [...] block if present
    if not content.startswith("["):
        import re as _re
        m = _re.search(r"\[.*\]", content, _re.DOTALL)
        if not m:
            return []
        content = m.group(0)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _candidate_to_entry(cand: dict) -> MemoryEntry | None:
    name = str(cand.get("name", "")).strip()
    description = str(cand.get("description", "")).strip()
    mtype = str(cand.get("type", "")).strip().lower()
    body = str(cand.get("body", "")).strip()
    if not name or not body or mtype not in VALID_MEMORY_TYPES:
        return None
    return MemoryEntry(
        name=name[:60],
        description=description[:100],
        type=mtype,
        body=body[:400],
    )


def schedule_extraction_task(
    project_root: str,
    user_msg: str,
    assistant_msg: str,
    config_memory: dict[str, Any],
    turn_tokens: int,
) -> asyncio.Task | None:
    """Fire-and-forget wrapper: only schedules if ``should_trigger`` is true."""
    ok, reason = should_trigger(config_memory, user_msg, turn_tokens)
    if not ok:
        logger.debug("memory extract skipped: %s", reason)
        return None
    model_ref = config_memory.get("model_ref") or DEFAULT_EXTRACT_MODEL
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("no running loop; skipping memory extract")
        return None
    return loop.create_task(
        extract_from_turn(project_root, user_msg, assistant_msg, model_ref=model_ref)
    )

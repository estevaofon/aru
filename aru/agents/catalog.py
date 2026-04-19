"""Native agent catalog — single source of truth for built-in agent specs.

Each AgentSpec describes a runtime-parameterized agent: prompt role, tool list,
mode (primary/subagent), and model sizing. The factory in agent_factory.py
consumes specs and builds Agno Agent instances. The runner in runner.py looks
up specs by name when handling runner.prompt(PromptInput).

Custom agents (defined via .agents/agents/*.md) follow a separate path through
create_custom_agent_instance and are NOT listed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal


@dataclass(frozen=True)
class AgentSpec:
    """Static description of a native agent.

    The tools_factory is a lazy callable so module load order does not force
    aru.tools.codebase to be imported before this module.

    `max_tokens=None` means "use the model's full cap" (see providers.py).
    An explicit int caps the agent below that ceiling — providers.py always
    clamps the final value to min(requested, model_cap) so specs can never
    ask for more than the model supports.

    `description` is the LLM-facing summary rendered into `delegate_task`'s
    docstring. Only subagent specs need a meaningful description (primary
    agents are never picked via `agent_name`). Keep it short (1-3 sentences)
    and directive — the model uses it to decide when this agent fits.

    `extra_instructions` is appended to the base role instructions when the
    agent is built. Use it for agent-specific policy ("you are read-only,
    never call write tools") that shouldn't leak into other roles.
    """

    name: str                            # display name passed to Agno
    role: str                            # key into build_instructions(role, ...)
    mode: Literal["primary", "subagent"]
    tools_factory: Callable[[], list]    # lazy resolver — invoked at agent creation
    max_tokens: int | None
    small_model: bool = False            # if True, factory uses ctx.small_model_ref
    use_reasoning: bool = True           # False skips thinking params (e.g. explorer)
    description: str = ""                # LLM-facing summary for `delegate_task` docstring
    extra_instructions: str = ""         # appended to base role instructions on build


def _build_tools() -> list:
    from aru.tools.registry import GENERAL_TOOLS
    return GENERAL_TOOLS


def _plan_tools() -> list:
    from aru.tools.registry import PLANNER_TOOLS
    return PLANNER_TOOLS


def _exec_tools() -> list:
    from aru.tools.registry import EXECUTOR_TOOLS
    return EXECUTOR_TOOLS


def _explore_tools() -> list:
    from aru.tools.registry import EXPLORER_TOOLS
    return EXPLORER_TOOLS


AGENTS: dict[str, AgentSpec] = {
    # Primary agents default to the model's full output cap (clamped by
    # providers.create_model). Subagents keep a tight budget so a runaway
    # explorer can't blow through the whole turn.
    "build": AgentSpec(
        name="Aru",
        role="general",
        mode="primary",
        tools_factory=_build_tools,
        max_tokens=None,
    ),
    "plan": AgentSpec(
        name="Planner",
        role="planner",
        mode="primary",
        tools_factory=_plan_tools,
        max_tokens=4096,
    ),
    "executor": AgentSpec(
        name="Executor",
        role="executor",
        mode="primary",
        tools_factory=_exec_tools,
        max_tokens=None,
    ),
    "explorer": AgentSpec(
        name="Explorer",
        role="explorer",
        mode="subagent",
        tools_factory=_explore_tools,
        max_tokens=8192,
        small_model=True,
        use_reasoning=False,  # fast read-only subagent — no thinking overhead
        description=(
            "Fast read-only codebase exploration agent. Use for searching "
            "files, finding patterns, reading code, and understanding "
            "structure. Specify thoroughness in the task text: \"quick\" "
            "(basic searches), \"medium\" (moderate exploration), or "
            "\"very thorough\" (comprehensive analysis)."
        ),
    ),
    "verification": AgentSpec(
        name="Verifier",
        role="verifier",
        mode="subagent",
        tools_factory=_explore_tools,  # read-only
        max_tokens=4096,
        small_model=True,
        use_reasoning=False,
        description=(
            "Double-check a recent batch of edits for correctness. Reads "
            "changed files, searches for call sites, reports inconsistencies "
            "and missing follow-up edits. Read-only — never edits. Use after "
            "non-trivial multi-file edits to catch issues before the user sees them."
        ),
    ),
    "reviewer": AgentSpec(
        name="Reviewer",
        role="reviewer",
        mode="subagent",
        tools_factory=_explore_tools,  # read-only
        max_tokens=4096,
        small_model=True,
        use_reasoning=False,
        description=(
            "Code review against naming, error handling, test coverage, and "
            "security heuristics. Read-only; produces bulleted findings with "
            "file:line refs and severity tags. Use when you want a second "
            "pair of eyes before finalising changes."
        ),
    ),
    "guide": AgentSpec(
        name="Guide",
        role="guide",
        mode="subagent",
        tools_factory=_explore_tools,  # read-only
        max_tokens=4096,
        small_model=True,
        use_reasoning=False,
        description=(
            "Answer questions about using Aru itself — slash commands, "
            "permission config, skills, plugins, tool catalog. Reads "
            "AGENTS.md and docs/ to ground answers. Use when the user's "
            "question is about Aru's features, not their own codebase."
        ),
    ),
}

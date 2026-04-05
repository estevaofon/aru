"""Configuration loader for AGENTS.md, commands, and skills.

Supports:
- AGENTS.md: Project-level agent instructions (appended to system prompt)
- .agents/commands/*.md: Custom slash commands (filename = command name)
- skills/<name>/SKILL.md: agentskills.io-compatible skills with YAML frontmatter
  Searched in: ~/.agents/, ~/.claude/, .agents/, .claude/ (last wins)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CustomCommand:
    """A custom command defined in .agents/commands/."""
    name: str
    description: str
    template: str
    source_path: str


@dataclass
class Skill:
    """A skill following the agentskills.io standard (<name>/SKILL.md)."""
    name: str
    description: str
    content: str
    source_path: str
    allowed_tools: list[str] = field(default_factory=list)
    disable_model_invocation: bool = False
    user_invocable: bool = True
    argument_hint: str = ""


@dataclass
class CustomAgent:
    """A custom agent defined in .agents/agents/<name>.md."""
    name: str
    description: str
    system_prompt: str
    source_path: str
    model: str | None = None
    tools: list[str] | dict[str, bool] = field(default_factory=list)
    max_turns: int | None = None
    mode: str = "primary"  # "primary" | "subagent"
    permission: dict[str, Any] | None = None


MAX_README_CHARS = 2000  # Reduced from 8000 to save ~1.7K tokens per request


@dataclass
class AgentConfig:
    """Loaded configuration from AGENTS.md, README.md, and .agents/ directory."""
    readme_md: str = ""
    agents_md: str = ""
    commands: dict[str, CustomCommand] = field(default_factory=dict)
    skills: dict[str, Skill] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    default_model: str | None = None
    model_aliases: dict[str, str] = field(default_factory=dict)
    custom_agents: dict[str, CustomAgent] = field(default_factory=dict)
    plan_reviewer: bool = True

    @property
    def has_instructions(self) -> bool:
        return bool(self.agents_md) or bool(self.skills)

    def get_extra_instructions(self, active_skills: list[str] | None = None, lightweight: bool = False) -> str:
        """Build extra instructions from README.md, AGENTS.md, and active skills.

        Args:
            active_skills: List of skill names to include.
            lightweight: If True, skip README.md and skill catalog to save tokens.
        """
        parts = []
        if self.readme_md and not lightweight:
            parts.append(f"## Project Overview (README.md)\n\n{self.readme_md}")
        if self.agents_md:
            parts.append(f"## Project Instructions (AGENTS.md)\n\n{self.agents_md}")
        if active_skills:
            for name in active_skills:
                if name in self.skills:
                    skill = self.skills[name]
                    parts.append(f"## Skill: {skill.name}\n\n{skill.content}")

        # Include skill catalog for model awareness (names + descriptions only)
        invocable = {k: v for k, v in self.skills.items() if v.user_invocable}
        if invocable and not lightweight:
            lines = ["## Available Skills\n"]
            lines.append("The user can invoke these skills with `/skill-name <args>`. You may suggest relevant skills.\n")
            for name, skill in invocable.items():
                hint = f" {skill.argument_hint}" if skill.argument_hint else ""
                lines.append(f"- `/{name}{hint}`: {skill.description}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown file.

    Uses PyYAML for robust parsing of nested structures.
    Returns (metadata_dict, body_content).
    """
    if not content.startswith("---"):
        return {}, content

    # Find closing --- delimiter (after the opening ---)
    lines = content.split("\n")
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx < 0:
        return {}, content

    yaml_block = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])

    try:
        import yaml
        metadata = yaml.safe_load(yaml_block)
    except Exception:
        metadata = None

    if not isinstance(metadata, dict):
        metadata = {}

    return metadata, body.strip()


def _parse_skill_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Interpret frontmatter values into typed Skill fields."""
    result: dict[str, Any] = {}
    result["name"] = str(metadata.get("name", ""))
    result["description"] = str(metadata.get("description", ""))
    hint = metadata.get("argument-hint", "")
    # YAML parses [text] as a list — convert back to string for display
    if isinstance(hint, list):
        hint = "[" + ", ".join(str(x) for x in hint) + "]"
    result["argument_hint"] = str(hint)

    ui = metadata.get("user-invocable", True)
    result["user_invocable"] = ui if isinstance(ui, bool) else str(ui).lower() != "false"

    dmi = metadata.get("disable-model-invocation", False)
    result["disable_model_invocation"] = dmi if isinstance(dmi, bool) else str(dmi).lower() == "true"

    tools_raw = metadata.get("allowed-tools", "")
    if isinstance(tools_raw, list):
        result["allowed_tools"] = [str(t).strip() for t in tools_raw]
    elif tools_raw:
        result["allowed_tools"] = [t.strip() for t in str(tools_raw).split(",") if t.strip()]
    else:
        result["allowed_tools"] = []

    return result


def _parse_agent_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Interpret frontmatter values into typed CustomAgent fields."""
    result: dict[str, Any] = {}
    result["name"] = str(metadata.get("name", ""))
    result["description"] = str(metadata.get("description", ""))
    result["model"] = metadata.get("model", None) or None
    mode_val = metadata.get("mode", "primary")
    result["mode"] = str(mode_val).lower() if mode_val else "primary"

    max_turns_raw = metadata.get("max_turns") or metadata.get("max-turns")
    if isinstance(max_turns_raw, int):
        result["max_turns"] = max_turns_raw
    elif max_turns_raw and str(max_turns_raw).strip().isdigit():
        result["max_turns"] = int(str(max_turns_raw).strip())
    else:
        result["max_turns"] = None

    tools_raw = metadata.get("tools")
    if isinstance(tools_raw, dict):
        result["tools"] = tools_raw
    elif isinstance(tools_raw, list):
        result["tools"] = [str(t).strip() for t in tools_raw]
    elif tools_raw:
        tools_str = str(tools_raw).strip()
        if tools_str.startswith("{"):
            try:
                result["tools"] = json.loads(tools_str)
            except json.JSONDecodeError:
                result["tools"] = []
        else:
            result["tools"] = [t.strip() for t in tools_str.split(",") if t.strip()]
    else:
        result["tools"] = []

    perm_raw = metadata.get("permission", None)
    result["permission"] = perm_raw if isinstance(perm_raw, dict) else None

    return result


def _load_commands(agents_dir: Path) -> dict[str, CustomCommand]:
    """Load custom commands from .agents/commands/."""
    commands_dir = agents_dir / "commands"
    commands: dict[str, CustomCommand] = {}

    if not commands_dir.is_dir():
        return commands

    for filepath in sorted(commands_dir.iterdir()):
        if filepath.suffix != ".md":
            continue

        name = filepath.stem
        try:
            content = filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        metadata, body = _parse_frontmatter(content)
        description = metadata.get("description", f"Custom command: {name}")

        commands[name] = CustomCommand(
            name=name,
            description=description,
            template=body,
            source_path=str(filepath),
        )

    return commands


def _discover_skills(search_roots: list[Path]) -> dict[str, Skill]:
    """Discover skills from multiple root directories (agentskills.io format).

    Each root is expected to contain a skills/ subdirectory with skill directories:
        skills/<name>/SKILL.md

    Later roots override earlier ones (project-local wins over global).
    """
    skills: dict[str, Skill] = {}

    for root in search_roots:
        skills_dir = root / "skills"
        if not skills_dir.is_dir():
            continue

        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue

            try:
                content = skill_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            raw_meta, body = _parse_frontmatter(content)
            meta = _parse_skill_metadata(raw_meta)

            dir_name = entry.name
            skill_name = meta["name"] or dir_name
            description = meta["description"] or f"Skill: {dir_name}"

            skills[dir_name] = Skill(
                name=skill_name,
                description=description,
                content=body,
                source_path=str(skill_file),
                allowed_tools=meta["allowed_tools"],
                disable_model_invocation=meta["disable_model_invocation"],
                user_invocable=meta["user_invocable"],
                argument_hint=meta["argument_hint"],
            )

    return skills


def _discover_agents(search_roots: list[Path]) -> dict[str, CustomAgent]:
    """Discover custom agents from agents/<name>.md files.

    Later roots override earlier ones (project-local wins over global).
    """
    agents: dict[str, CustomAgent] = {}

    for root in search_roots:
        agents_dir = root / "agents"
        if not agents_dir.is_dir():
            continue

        for filepath in sorted(agents_dir.iterdir()):
            if filepath.suffix != ".md":
                continue

            try:
                content = filepath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            raw_meta, body = _parse_frontmatter(content)
            meta = _parse_agent_metadata(raw_meta)

            dir_name = filepath.stem
            agent_name = meta["name"] or dir_name
            description = meta["description"] or f"Custom agent: {dir_name}"

            if not body.strip():
                continue

            agents[dir_name] = CustomAgent(
                name=agent_name,
                description=description,
                system_prompt=body,
                source_path=str(filepath),
                model=meta["model"],
                tools=meta["tools"],
                max_turns=meta["max_turns"],
                mode=meta["mode"],
                permission=meta.get("permission"),
            )

    return agents


def load_config(cwd: str | None = None) -> AgentConfig:
    """Load agent configuration from AGENTS.md and .agents/ directory.

    Searches the current working directory for:
    - AGENTS.md: Project-level instructions
    - .agents/commands/*.md: Custom slash commands
    - .agents/skills/*.md: Custom skills/personas

    Args:
        cwd: Working directory to search in. Defaults to os.getcwd().

    Returns:
        AgentConfig with all loaded configuration.
    """
    root = Path(cwd or os.getcwd())
    config = AgentConfig()

    # Load README.md first — gives the agent project context upfront
    for readme_name in ("README.md", "readme.md", "Readme.md"):
        readme_path = root / readme_name
        if readme_path.is_file():
            try:
                content = readme_path.read_text(encoding="utf-8").strip()
                config.readme_md = content[:MAX_README_CHARS]
            except (OSError, UnicodeDecodeError):
                pass
            break

    # Load AGENTS.md
    agents_md_path = root / "AGENTS.md"
    if agents_md_path.is_file():
        try:
            config.agents_md = agents_md_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            pass

    # Load commands from .agents/commands/
    agents_dir = root / ".agents"
    if agents_dir.is_dir():
        config.commands = _load_commands(agents_dir)

    # Discover skills from multiple roots (agentskills.io convention)
    # Order: global paths first, project-local last (local overrides global)
    home = Path.home()
    skill_roots: list[Path] = []
    for dirname in (".agents", ".claude"):
        global_dir = home / dirname
        if global_dir.is_dir():
            skill_roots.append(global_dir)
    for dirname in (".agents", ".claude"):
        local_dir = root / dirname
        if local_dir.is_dir():
            skill_roots.append(local_dir)
    config.skills = _discover_skills(skill_roots)
    config.custom_agents = _discover_agents(skill_roots)

    # Load opencode-style config (aru.json or .aru/config.json)
    config_paths = [root / "aru.json", root / ".aru" / "config.json"]
    for config_path in config_paths:
        if config_path.is_file():
            try:
                content = config_path.read_text(encoding="utf-8")
                data = json.loads(content)
                if isinstance(data, dict):
                    if "permission" in data:
                        config.permissions = data["permission"]
                    # Load provider configuration
                    if "providers" in data:
                        from aru.providers import load_providers_from_config
                        load_providers_from_config(data)
                    # Store default model and aliases for CLI
                    if "default_model" in data:
                        config.default_model = data["default_model"]
                    if "model_aliases" in data and isinstance(data["model_aliases"], dict):
                        config.model_aliases = data["model_aliases"]
                    if "plan_reviewer" in data:
                        config.plan_reviewer = bool(data["plan_reviewer"])
                    # Agent-level permission overrides from aru.json
                    if "agent" in data and isinstance(data["agent"], dict):
                        for agent_name, agent_data in data["agent"].items():
                            if agent_name in config.custom_agents and isinstance(agent_data, dict):
                                if "permission" in agent_data:
                                    config.custom_agents[agent_name].permission = agent_data["permission"]
                break
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass

    return config


def render_command_template(template: str, user_input: str) -> str:
    """Render a command template with user input.

    Replaces $INPUT with the user's arguments.
    Also supports $SELECTION (empty if not provided) for future use.
    """
    result = template.replace("$INPUT", user_input)
    result = result.replace("$SELECTION", "")
    return result


def render_skill_template(content: str, arguments: str) -> str:
    """Render a skill template with argument substitution (agentskills.io).

    Supports:
    - $ARGUMENTS: Full argument string
    - $ARGUMENTS[N]: Nth argument (0-indexed)
    - $1, $2, ...: Nth argument (1-indexed, shell-style)

    Also prepends an explicit argument context block so the agent cannot
    miss or misread the user-supplied value.
    """
    parts = arguments.split() if arguments else []

    def _replace_indexed(m: re.Match) -> str:
        idx = int(m.group(1))
        return parts[idx] if idx < len(parts) else ""

    # Replace $ARGUMENTS[N] first (before $ARGUMENTS to avoid partial match)
    result = re.sub(r'\$ARGUMENTS\[(\d+)\]', _replace_indexed, content)

    def _replace_positional(m: re.Match) -> str:
        idx = int(m.group(1)) - 1
        return parts[idx] if 0 <= idx < len(parts) else ""

    # Replace $1, $2, ... (1-indexed)
    result = re.sub(r'\$(\d+)', _replace_positional, result)

    # Replace $ARGUMENTS last
    result = result.replace("$ARGUMENTS", arguments)

    # Prepend an explicit context block so the agent cannot miss the argument
    if arguments and arguments.strip():
        header = f"> **Skill argument:** `{arguments.strip()}`\n> Use this value exactly where the skill instructions reference the argument.\n\n"
        result = header + result

    return result

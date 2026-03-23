"""Configuration loader for AGENTS.md and .agents/ directory.

Supports:
- AGENTS.md: Project-level agent instructions (appended to system prompt)
- .agents/commands/*.md: Custom slash commands (filename = command name)
- .agents/skills/*.md: Custom skills/personas (loaded as additional instructions)

Follows the Gemini .agents convention for cross-platform compatibility.
"""

import json
import os
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
    """A skill defined in .agents/skills/."""
    name: str
    description: str
    content: str
    source_path: str


MAX_README_CHARS = 8000


@dataclass
class AgentConfig:
    """Loaded configuration from AGENTS.md, README.md, and .agents/ directory."""
    readme_md: str = ""
    agents_md: str = ""
    commands: dict[str, CustomCommand] = field(default_factory=dict)
    skills: dict[str, Skill] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)

    @property
    def has_instructions(self) -> bool:
        return bool(self.agents_md) or bool(self.skills)

    def get_extra_instructions(self, active_skills: list[str] | None = None) -> str:
        """Build extra instructions from README.md, AGENTS.md, and active skills.

        Order matters — README.md comes first so the agent has project context
        before any custom instructions or skills.
        """
        parts = []
        if self.readme_md:
            parts.append(f"## Project Overview (README.md)\n\n{self.readme_md}")
        if self.agents_md:
            parts.append(f"## Project Instructions (AGENTS.md)\n\n{self.agents_md}")
        if active_skills:
            for name in active_skills:
                if name in self.skills:
                    skill = self.skills[name]
                    parts.append(f"## Skill: {skill.name}\n\n{skill.content}")
        elif self.skills:
            # If no specific skills requested, load all skills
            for skill in self.skills.values():
                parts.append(f"## Skill: {skill.name}\n\n{skill.content}")
        return "\n\n".join(parts)


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse YAML-like frontmatter from a markdown file.

    Returns (metadata_dict, body_content).
    """
    metadata: dict[str, str] = {}
    body = content

    if content.startswith("---"):
        lines = content.split("\n")
        end_idx = -1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx > 0:
            for line in lines[1:end_idx]:
                if ":" in line:
                    key, _, value = line.partition(":")
                    metadata[key.strip()] = value.strip()
            body = "\n".join(lines[end_idx + 1:]).strip()

    return metadata, body


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


def _load_skills(agents_dir: Path) -> dict[str, Skill]:
    """Load skills from .agents/skills/."""
    skills_dir = agents_dir / "skills"
    skills: dict[str, Skill] = {}

    if not skills_dir.is_dir():
        return skills

    for filepath in sorted(skills_dir.iterdir()):
        if filepath.suffix != ".md":
            continue

        name = filepath.stem
        try:
            content = filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        metadata, body = _parse_frontmatter(content)
        description = metadata.get("description", f"Skill: {name}")

        skills[name] = Skill(
            name=name,
            description=description,
            content=body,
            source_path=str(filepath),
        )

    return skills


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

    # Load .agents/ directory
    agents_dir = root / ".agents"
    if agents_dir.is_dir():
        config.commands = _load_commands(agents_dir)
        config.skills = _load_skills(agents_dir)

    # Load opencode-style config (arc.json or .arc/config.json)
    config_paths = [root / "arc.json", root / ".arc" / "config.json"]
    for config_path in config_paths:
        if config_path.is_file():
            try:
                content = config_path.read_text(encoding="utf-8")
                data = json.loads(content)
                if isinstance(data, dict) and "permission" in data:
                    config.permissions = data["permission"]
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

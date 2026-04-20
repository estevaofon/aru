"""Configuration loader for AGENTS.md, commands, and skills.

Supports:
- AGENTS.md: Project-level agent instructions (appended to system prompt)
- .agents/commands/*.md: Custom slash commands (filename = command name)
- skills/<name>/SKILL.md: agentskills.io-compatible skills with YAML frontmatter
  Searched in: ~/.agents/, ~/.claude/, .agents/, .claude/ (last wins)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("aru.config")


@dataclass
class CustomCommand:
    """A custom command defined in .agents/commands/."""
    name: str
    description: str
    template: str
    source_path: str
    agent: str | None = None
    model: str | None = None


@dataclass
class Skill:
    """A skill following the agentskills.io standard (<name>/SKILL.md)."""
    name: str
    description: str
    content: str
    source_path: str
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    disable_model_invocation: bool = False
    user_invocable: bool = True
    argument_hint: str = ""
    # Short (~1-2 sentences) reminder used by the core to reinforce the
    # skill's critical gates during compaction. Not re-injected per turn —
    # it only appears wrapped in `<system-reminder>` when a compaction
    # would otherwise drop the skill body from history. When absent, the
    # core derives a default from `description`.
    reminder: str = ""


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
MAX_RULE_FILE_SIZE = 10_000  # 10KB per rule file
URL_FETCH_TIMEOUT = 5  # seconds
MAX_TOTAL_RULES_SIZE = 50_000  # 50KB combined cap

# Module-level URL cache (session-scoped, persists for process lifetime)
_url_cache: dict[str, str | None] = {}


def _resolve_instructions(entries: list[str], root: Path) -> str:
    """Resolve instruction entries (local files, glob patterns, URLs) into combined text.

    Each entry is classified as:
    - URL: starts with http:// or https://
    - Glob: contains *, ?, or [
    - File path: everything else (resolved relative to root)
    """
    from aru.tools.gitignore import is_ignored

    parts: list[str] = []
    total_size = 0

    def _add_content(source: str, content: str) -> None:
        nonlocal total_size
        if not content.strip():
            return
        truncated = content[:MAX_RULE_FILE_SIZE]
        if total_size + len(truncated) > MAX_TOTAL_RULES_SIZE:
            remaining = MAX_TOTAL_RULES_SIZE - total_size
            if remaining <= 0:
                logger.warning("Total rules size cap reached, skipping: %s", source)
                return
            truncated = truncated[:remaining]
            logger.warning("Total rules size cap reached, truncating: %s", source)
        parts.append(f"## Rules: {source}\n\n{truncated}")
        total_size += len(truncated)

    def _read_file(filepath: Path, source_label: str) -> None:
        try:
            content = filepath.read_text(encoding="utf-8")
            _add_content(source_label, content)
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Failed to read instruction file %s: %s", filepath, exc)

    for entry in entries:
        if entry.startswith("http://") or entry.startswith("https://"):
            # Remote URL
            if entry in _url_cache:
                cached = _url_cache[entry]
                if cached is not None:
                    _add_content(entry, cached)
                continue
            try:
                import httpx
                with httpx.Client(timeout=URL_FETCH_TIMEOUT, follow_redirects=True) as client:
                    resp = client.get(entry)
                    resp.raise_for_status()
                    text = resp.text
                _url_cache[entry] = text
                _add_content(entry, text)
            except Exception as exc:
                _url_cache[entry] = None
                logger.warning("Failed to fetch instruction URL %s: %s", entry, exc)

        elif any(c in entry for c in ("*", "?", "[")):
            # Glob pattern
            matched = sorted(root.glob(entry))
            for filepath in matched:
                if not filepath.is_file():
                    continue
                try:
                    rel = filepath.relative_to(root)
                except ValueError:
                    continue
                if is_ignored(str(rel), str(root)):
                    continue
                _read_file(filepath, str(rel))

        else:
            # Local file path
            filepath = root / entry
            if filepath.is_file():
                _read_file(filepath, entry)
            else:
                logger.warning("Instruction file not found: %s", filepath)

    return "\n\n".join(parts)


@dataclass
class AgentConfig:
    """Loaded configuration from AGENTS.md, README.md, and .agents/ directory."""
    readme_md: str = ""
    agents_md: str = ""
    rules_instructions: str = ""
    commands: dict[str, CustomCommand] = field(default_factory=dict)
    skills: dict[str, Skill] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    default_model: str | None = None
    model_aliases: dict[str, str] = field(default_factory=dict)
    custom_agents: dict[str, CustomAgent] = field(default_factory=dict)
    plan_reviewer: bool = True
    tree_depth: int = 2  # max depth for directory tree in system prompt
    disabled_tools: list[str] = field(default_factory=list)  # tools to skip loading
    plugin_specs: list = field(default_factory=list)  # plugin specs from aru.json
    # Auto-memory extraction (Tier 2 #4). Opt-in — off by default to avoid
    # invisible token spend in new projects.
    memory: dict[str, Any] = field(default_factory=dict)
    # LSP server per language (Tier 2 #5). Keys are language ids (python,
    # typescript, rust, go, ...); values are {"command": "...", "args": [...], "env": {...}}.
    # Empty ⇒ LSP tools report "not configured" without spawning anything.
    lsp: dict[str, Any] = field(default_factory=dict)
    # Formatter config per language (Tier 3 #1). Same shape as `lsp`, plus
    # an `enabled` top-level boolean to flip auto-format on/off in aggregate.
    format: dict[str, Any] = field(default_factory=dict)

    @property
    def has_instructions(self) -> bool:
        return bool(self.agents_md) or bool(self.skills) or bool(self.rules_instructions)

    def get_extra_instructions(self, active_skills: list[str] | None = None, lightweight: bool = False) -> str:
        """Build extra instructions from README.md, AGENTS.md, and active skills.

        Args:
            active_skills: List of skill names to include.
            lightweight: If True, skip README.md and skill catalog to save tokens.
        """
        parts = []
        # README.md is no longer included by default — it's written for humans
        # (badges, install instructions, contributing guides) and wastes ~2K tokens
        # per turn. AGENTS.md is the proper place for model-facing context.
        if self.agents_md:
            parts.append(f"## Project Instructions (AGENTS.md)\n\n{self.agents_md}")
        if self.rules_instructions:
            parts.append(self.rules_instructions)
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

        # MCP tool catalog is NOT included in the system prompt to save ~1-1.5K
        # tokens per turn. The model discovers available tools on-demand when it
        # calls use_mcp_tool — the gateway returns the catalog on first use.

        # Auto-memory index (Tier 2 #4). Always injected when a MEMORY.md
        # exists for the current project — the file only populates if the
        # user opted into memory.auto_extract, so a clean project stays silent.
        if not lightweight:
            try:
                from aru.memory.loader import memory_section_for_prompt
                import os
                section = memory_section_for_prompt(os.getcwd())
                if section:
                    parts.append(section.strip())
            except Exception:  # pragma: no cover — memory module failure mustn't break prompts
                pass

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

    disallowed_raw = metadata.get("disallowed-tools", "")
    if isinstance(disallowed_raw, list):
        result["disallowed_tools"] = [str(t).strip() for t in disallowed_raw]
    elif disallowed_raw:
        result["disallowed_tools"] = [t.strip() for t in str(disallowed_raw).split(",") if t.strip()]
    else:
        result["disallowed_tools"] = []

    reminder_raw = metadata.get("reminder", "")
    result["reminder"] = str(reminder_raw).strip() if reminder_raw else ""

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
            agent=metadata.get("agent") or None,
            model=metadata.get("model") or None,
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
                disallowed_tools=meta["disallowed_tools"],
                disable_model_invocation=meta["disable_model_invocation"],
                user_invocable=meta["user_invocable"],
                argument_hint=meta["argument_hint"],
                reminder=meta["reminder"],
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


def _load_json_file(path: Path) -> dict | None:
    """Read and parse a JSON file, returning None on any error."""
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override values win for scalars;
    dicts are merged recursively; lists are replaced (not concatenated)."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_config_data(config: AgentConfig, data: dict, root: Path) -> None:
    """Apply a merged config dict to an AgentConfig object."""
    if "permission" in data:
        config.permissions = data["permission"]
    if "providers" in data:
        from aru.providers import load_providers_from_config
        load_providers_from_config(data)
    if "default_model" in data:
        config.default_model = data["default_model"]
    if "model_aliases" in data and isinstance(data["model_aliases"], dict):
        config.model_aliases = data["model_aliases"]
    if "plan_reviewer" in data:
        config.plan_reviewer = bool(data["plan_reviewer"])
    if "tree_depth" in data:
        td = data["tree_depth"]
        if isinstance(td, int) and 0 <= td <= 5:
            config.tree_depth = td
    if "plugins" in data and isinstance(data["plugins"], list):
        config.plugin_specs = data["plugins"]
    if "tools" in data and isinstance(data["tools"], dict):
        tools_cfg = data["tools"]
        if "disabled" in tools_cfg and isinstance(tools_cfg["disabled"], list):
            config.disabled_tools = [str(t) for t in tools_cfg["disabled"]]
    if "memory" in data and isinstance(data["memory"], dict):
        config.memory = data["memory"]
    if "lsp" in data and isinstance(data["lsp"], dict):
        config.lsp = data["lsp"]
    if "format" in data and isinstance(data["format"], dict):
        config.format = data["format"]
    if "instructions" in data and isinstance(data["instructions"], list):
        entries = [str(e) for e in data["instructions"] if isinstance(e, str)]
        config.rules_instructions = _resolve_instructions(entries, root)
    if "agent" in data and isinstance(data["agent"], dict):
        for agent_name, agent_data in data["agent"].items():
            if agent_name in config.custom_agents and isinstance(agent_data, dict):
                if "permission" in agent_data:
                    config.custom_agents[agent_name].permission = agent_data["permission"]


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
    # Order: cached plugins first (lowest priority), then global, then project-local
    # (local overrides global overrides cache — lets users shadow plugin skills).
    home = Path.home()
    skill_roots: list[Path] = []
    try:
        from aru.plugin_cache import get_cached_plugin_roots
        skill_roots.extend(get_cached_plugin_roots())
    except Exception as exc:  # defensive: never fail config load over cache
        logger.warning("Failed to load cached plugin roots: %s", exc)
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

    # Load config: global (~/.aru/config.json) first, then project-level on top.
    # Project values override global values via deep merge.
    home = Path.home()
    global_config_paths = [home / ".aru" / "aru.json", home / ".aru" / "config.json"]
    project_config_paths = [root / "aru.json", root / ".aru" / "config.json"]

    merged_data: dict = {}
    for config_path in global_config_paths:
        data = _load_json_file(config_path)
        if data is not None:
            merged_data = data
            break

    for config_path in project_config_paths:
        data = _load_json_file(config_path)
        if data is not None:
            merged_data = _deep_merge(merged_data, data)
            break

    if merged_data:
        _apply_config_data(config, merged_data, root)

    return config


def render_template_arguments(
    content: str, arguments: str, *, context_label: str = "Argument",
) -> str:
    """Render a template with $ARGUMENTS / $1 / $2 substitution.

    Supports:
    - $ARGUMENTS: Full argument string
    - $ARGUMENTS[N]: Nth argument (0-indexed)
    - $1, $2, ...: Nth argument (1-indexed, shell-style)

    Also prepends an explicit context block so the agent cannot
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
        header = f"> **{context_label}:** `{arguments.strip()}`\n> Use this value exactly where the instructions reference the argument.\n\n"
        result = header + result

    return result


def render_command_template(template: str, user_input: str) -> str:
    """Render a command template with OpenCode-style argument substitution."""
    return render_template_arguments(template, user_input, context_label="Command argument")


def render_skill_template(content: str, arguments: str) -> str:
    """Render a skill template with argument substitution (agentskills.io)."""
    return render_template_arguments(content, arguments, context_label="Skill argument")

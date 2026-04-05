"""Unit tests for aru.config module."""

import pytest
from pathlib import Path
from aru.config import (
    AgentConfig,
    CustomAgent,
    CustomCommand,
    Skill,
    _discover_agents,
    _parse_agent_metadata,
    _parse_frontmatter,
    _parse_skill_metadata,
    render_command_template,
    render_skill_template,
    load_config,
    MAX_README_CHARS,
)


class TestDataClasses:
    """Test dataclass structures."""

    def test_custom_command_creation(self):
        cmd = CustomCommand(
            name="test",
            description="Test command",
            template="Do $INPUT",
            source_path="/path/to/test.md",
        )
        assert cmd.name == "test"
        assert cmd.description == "Test command"
        assert cmd.template == "Do $INPUT"
        assert cmd.source_path == "/path/to/test.md"

    def test_skill_creation(self):
        skill = Skill(
            name="review",
            description="Code review skill",
            content="Review the code carefully",
            source_path="/path/to/review/SKILL.md",
        )
        assert skill.name == "review"
        assert skill.description == "Code review skill"
        assert skill.content == "Review the code carefully"
        assert skill.source_path == "/path/to/review/SKILL.md"
        # Defaults for new agentskills.io fields
        assert skill.allowed_tools == []
        assert skill.disable_model_invocation is False
        assert skill.user_invocable is True
        assert skill.argument_hint == ""

    def test_skill_creation_with_extended_fields(self):
        skill = Skill(
            name="review",
            description="Code review skill",
            content="Review the code",
            source_path="/path/to/review/SKILL.md",
            allowed_tools=["Read", "Grep"],
            disable_model_invocation=True,
            user_invocable=False,
            argument_hint="[file-path]",
        )
        assert skill.allowed_tools == ["Read", "Grep"]
        assert skill.disable_model_invocation is True
        assert skill.user_invocable is False
        assert skill.argument_hint == "[file-path]"

    def test_agent_config_defaults(self):
        config = AgentConfig()
        assert config.readme_md == ""
        assert config.agents_md == ""
        assert config.commands == {}
        assert config.skills == {}
        assert not config.has_instructions

    def test_agent_config_has_instructions_with_agents_md(self):
        config = AgentConfig(agents_md="Some instructions")
        assert config.has_instructions

    def test_agent_config_has_instructions_with_skills(self):
        skill = Skill("test", "Test skill", "Content", "/path")
        config = AgentConfig(skills={"test": skill})
        assert config.has_instructions

    def test_agent_config_get_extra_instructions_empty(self):
        config = AgentConfig()
        result = config.get_extra_instructions()
        assert result == ""

    def test_agent_config_get_extra_instructions_readme_only(self):
        config = AgentConfig(readme_md="# My Project\n\nDescription")
        result = config.get_extra_instructions()
        assert "## Project Overview (README.md)" in result
        assert "# My Project" in result

    def test_agent_config_get_extra_instructions_agents_md_only(self):
        config = AgentConfig(agents_md="Follow these rules")
        result = config.get_extra_instructions()
        assert "## Project Instructions (AGENTS.md)" in result
        assert "Follow these rules" in result

    def test_agent_config_get_extra_instructions_no_skills_by_default(self):
        """Skill content is only loaded when explicitly requested (token optimization)."""
        skill1 = Skill("review", "Review", "Review code", "/path1")
        skill2 = Skill("test", "Test", "Write tests", "/path2")
        config = AgentConfig(skills={"review": skill1, "test": skill2})
        result = config.get_extra_instructions()
        assert "## Skill: review" not in result
        assert "## Skill: test" not in result
        # But skill catalog IS included for model awareness
        assert "## Available Skills" in result
        assert "/review" in result
        assert "/test" in result

    def test_agent_config_get_extra_instructions_specific_skills(self):
        skill1 = Skill("review", "Review", "Review code", "/path1")
        skill2 = Skill("test", "Test", "Write tests", "/path2")
        config = AgentConfig(skills={"review": skill1, "test": skill2})
        result = config.get_extra_instructions(active_skills=["review"])
        assert "## Skill: review" in result
        assert "Review code" in result
        assert "## Skill: test" not in result

    def test_agent_config_get_extra_instructions_order(self):
        config = AgentConfig(
            readme_md="README content",
            agents_md="AGENTS content",
            skills={"test": Skill("test", "Test", "Skill content", "/path")}
        )
        # Skills only loaded when explicitly requested
        result = config.get_extra_instructions(active_skills=["test"])
        # Check order: README -> AGENTS.md -> Skills
        readme_pos = result.find("README content")
        agents_pos = result.find("AGENTS content")
        skill_pos = result.find("Skill content")
        assert readme_pos < agents_pos < skill_pos


class TestParseFrontmatter:
    """Test frontmatter parsing."""

    def test_no_frontmatter(self):
        content = "Just plain text\nNo metadata here"
        metadata, body = _parse_frontmatter(content)
        assert metadata == {}
        assert body == content

    def test_empty_frontmatter(self):
        content = "---\n---\nBody content"
        metadata, body = _parse_frontmatter(content)
        assert metadata == {}
        assert body == "Body content"

    def test_simple_frontmatter(self):
        content = "---\ndescription: Test command\nauthor: John\n---\nBody text"
        metadata, body = _parse_frontmatter(content)
        assert metadata == {"description": "Test command", "author": "John"}
        assert body == "Body text"

    def test_frontmatter_with_colons_in_value(self):
        content = "---\nurl: https://example.com:8080\n---\nContent"
        metadata, body = _parse_frontmatter(content)
        assert metadata["url"] == "https://example.com:8080"
        assert body == "Content"

    def test_frontmatter_with_empty_lines(self):
        content = "---\ndescription: Test\n\n---\n\nBody with newlines"
        metadata, body = _parse_frontmatter(content)
        assert metadata == {"description": "Test"}
        assert body == "Body with newlines"

    def test_frontmatter_strips_whitespace(self):
        content = "---\n  description:   Test command   \n---\n  Body  "
        metadata, body = _parse_frontmatter(content)
        assert metadata["description"] == "Test command"
        assert body == "Body"

    def test_no_closing_frontmatter_delimiter(self):
        content = "---\ndescription: Test\nBody content"
        metadata, body = _parse_frontmatter(content)
        # Should not parse frontmatter if no closing delimiter
        assert metadata == {}
        assert body == content

    def test_frontmatter_invalid_yaml_returns_empty(self):
        content = "---\ndescription: Test\nInvalid line\nkey: value\n---\nBody"
        metadata, body = _parse_frontmatter(content)
        # Invalid YAML (bare line without colon) → graceful fallback to empty dict
        assert metadata == {}
        assert body == "Body"


class TestRenderCommandTemplate:
    """Test command template rendering."""

    def test_render_with_input(self):
        template = "Execute $INPUT in the codebase"
        result = render_command_template(template, "refactoring")
        assert result == "Execute refactoring in the codebase"

    def test_render_multiple_input_placeholders(self):
        template = "Run $INPUT and verify $INPUT works"
        result = render_command_template(template, "tests")
        assert result == "Run tests and verify tests works"

    def test_render_removes_selection(self):
        template = "Process $INPUT and $SELECTION"
        result = render_command_template(template, "data")
        assert result == "Process data and "

    def test_render_no_placeholders(self):
        template = "Just plain text"
        result = render_command_template(template, "input")
        assert result == "Just plain text"

    def test_render_empty_input(self):
        template = "Do $INPUT now"
        result = render_command_template(template, "")
        assert result == "Do  now"

    def test_render_with_special_characters(self):
        template = "Run $INPUT"
        result = render_command_template(template, "pytest -v --cov")
        assert result == "Run pytest -v --cov"


class TestLoadConfig:
    """Test configuration loading."""

    def test_load_config_empty_directory(self, tmp_path):
        config = load_config(str(tmp_path))
        assert config.readme_md == ""
        assert config.agents_md == ""
        assert config.commands == {}
        assert config.skills == {}

    def test_load_config_with_readme(self, tmp_path):
        readme_path = tmp_path / "README.md"
        readme_path.write_text("# Test Project\n\nDescription")
        
        config = load_config(str(tmp_path))
        assert "# Test Project" in config.readme_md
        assert "Description" in config.readme_md

    def test_load_config_readme_case_insensitive(self, tmp_path):
        readme_path = tmp_path / "readme.md"
        readme_path.write_text("Content")
        
        config = load_config(str(tmp_path))
        assert config.readme_md == "Content"

    def test_load_config_readme_truncation(self, tmp_path):
        readme_path = tmp_path / "README.md"
        long_content = "x" * (MAX_README_CHARS + 1000)
        readme_path.write_text(long_content)
        
        config = load_config(str(tmp_path))
        assert len(config.readme_md) == MAX_README_CHARS

    def test_load_config_with_agents_md(self, tmp_path):
        agents_path = tmp_path / "AGENTS.md"
        agents_path.write_text("Custom instructions")
        
        config = load_config(str(tmp_path))
        assert config.agents_md == "Custom instructions"

    def test_load_config_with_commands(self, tmp_path):
        commands_dir = tmp_path / ".agents" / "commands"
        commands_dir.mkdir(parents=True)
        
        cmd_file = commands_dir / "deploy.md"
        cmd_file.write_text("---\ndescription: Deploy the app\n---\nDeploy $INPUT")
        
        config = load_config(str(tmp_path))
        assert "deploy" in config.commands
        cmd = config.commands["deploy"]
        assert cmd.name == "deploy"
        assert cmd.description == "Deploy the app"
        assert cmd.template == "Deploy $INPUT"

    def test_load_config_command_without_frontmatter(self, tmp_path):
        commands_dir = tmp_path / ".agents" / "commands"
        commands_dir.mkdir(parents=True)
        
        cmd_file = commands_dir / "test.md"
        cmd_file.write_text("Run tests")
        
        config = load_config(str(tmp_path))
        assert "test" in config.commands
        cmd = config.commands["test"]
        assert cmd.description == "Custom command: test"
        assert cmd.template == "Run tests"

    def test_load_config_ignores_non_md_commands(self, tmp_path):
        commands_dir = tmp_path / ".agents" / "commands"
        commands_dir.mkdir(parents=True)
        
        (commands_dir / "test.md").write_text("Valid")
        (commands_dir / "test.txt").write_text("Invalid")
        (commands_dir / "test.py").write_text("Invalid")
        
        config = load_config(str(tmp_path))
        assert len(config.commands) == 1
        assert "test" in config.commands

    def test_load_config_with_skills(self, tmp_path):
        skill_dir = tmp_path / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("---\ndescription: Code review\n---\nReview carefully")

        config = load_config(str(tmp_path))
        assert "review" in config.skills
        skill = config.skills["review"]
        assert skill.name == "review"
        assert skill.description == "Code review"
        assert skill.content == "Review carefully"

    def test_load_config_skill_without_frontmatter(self, tmp_path):
        skill_dir = tmp_path / ".agents" / "skills" / "optimize"
        skill_dir.mkdir(parents=True)

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("Optimize the code")

        config = load_config(str(tmp_path))
        assert "optimize" in config.skills
        skill = config.skills["optimize"]
        assert skill.description == "Skill: optimize"
        assert skill.content == "Optimize the code"

    def test_load_config_ignores_non_skill_dirs(self, tmp_path):
        skills_dir = tmp_path / ".agents" / "skills"
        skills_dir.mkdir(parents=True)

        # Valid: directory with SKILL.md
        valid_dir = skills_dir / "valid"
        valid_dir.mkdir()
        (valid_dir / "SKILL.md").write_text("Content")

        # Invalid: flat file (not a directory)
        (skills_dir / "invalid.md").write_text("Content")

        # Invalid: directory without SKILL.md
        no_skill = skills_dir / "no-skill"
        no_skill.mkdir()
        (no_skill / "README.md").write_text("Not a skill")

        config = load_config(str(tmp_path))
        assert len(config.skills) == 1
        assert "valid" in config.skills

    def test_load_config_multiple_commands_sorted(self, tmp_path):
        commands_dir = tmp_path / ".agents" / "commands"
        commands_dir.mkdir(parents=True)
        
        (commands_dir / "zebra.md").write_text("Zebra")
        (commands_dir / "alpha.md").write_text("Alpha")
        (commands_dir / "beta.md").write_text("Beta")
        
        config = load_config(str(tmp_path))
        # Should be sorted alphabetically
        keys = list(config.commands.keys())
        assert keys == ["alpha", "beta", "zebra"]

    def test_load_config_multiple_skills_sorted(self, tmp_path):
        skills_dir = tmp_path / ".agents" / "skills"
        skills_dir.mkdir(parents=True)

        for name in ("z", "a", "m"):
            d = skills_dir / name
            d.mkdir()
            (d / "SKILL.md").write_text(name.upper())

        config = load_config(str(tmp_path))
        keys = list(config.skills.keys())
        assert keys == ["a", "m", "z"]

    def test_load_config_handles_unicode_errors(self, tmp_path):
        # Create a file with invalid UTF-8 (binary file)
        readme_path = tmp_path / "README.md"
        readme_path.write_bytes(b"\x80\x81\x82")
        
        # Should not raise, just skip the file
        config = load_config(str(tmp_path))
        assert config.readme_md == ""

    def test_load_config_no_agents_directory(self, tmp_path):
        # Create README but no .agents directory
        (tmp_path / "README.md").write_text("Content")
        
        config = load_config(str(tmp_path))
        assert config.readme_md == "Content"
        assert config.commands == {}
        assert config.skills == {}

    def test_load_config_full_integration(self, tmp_path):
        # Create complete configuration
        (tmp_path / "README.md").write_text("# Project")
        (tmp_path / "AGENTS.md").write_text("Instructions")

        commands_dir = tmp_path / ".agents" / "commands"
        commands_dir.mkdir(parents=True)
        (commands_dir / "test.md").write_text("---\ndescription: Test\n---\nRun $INPUT")

        skill_dir = tmp_path / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\ndescription: Review\n---\nReview code")

        config = load_config(str(tmp_path))
        assert config.readme_md == "# Project"
        assert config.agents_md == "Instructions"
        assert len(config.commands) == 1
        assert len(config.skills) == 1
        assert config.has_instructions

    def test_load_config_skill_with_extended_frontmatter(self, tmp_path):
        skill_dir = tmp_path / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)

        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: code-review\n"
            "description: Deep code review\n"
            "allowed-tools: Read, Grep, Glob\n"
            "user-invocable: true\n"
            "disable-model-invocation: true\n"
            "argument-hint: [file-path]\n"
            "---\n"
            "Review $ARGUMENTS carefully"
        )

        config = load_config(str(tmp_path))
        skill = config.skills["review"]
        assert skill.name == "code-review"
        assert skill.description == "Deep code review"
        assert skill.allowed_tools == ["Read", "Grep", "Glob"]
        assert skill.user_invocable is True
        assert skill.disable_model_invocation is True
        assert skill.argument_hint == "[file-path]"

    def test_load_config_claude_skills_dir(self, tmp_path):
        """Skills in .claude/skills/ should also be discovered."""
        skill_dir = tmp_path / ".claude" / "skills" / "lint"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\ndescription: Lint code\n---\nLint it")

        config = load_config(str(tmp_path))
        assert "lint" in config.skills
        assert config.skills["lint"].description == "Lint code"

    def test_load_config_local_overrides_global(self, tmp_path, monkeypatch):
        """Project-local skills override global user skills."""
        # Simulate home directory
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        # Global skill
        global_skill = fake_home / ".agents" / "skills" / "review"
        global_skill.mkdir(parents=True)
        (global_skill / "SKILL.md").write_text("---\ndescription: Global review\n---\nGlobal")

        # Project-local skill (same name, should override)
        project = tmp_path / "project"
        project.mkdir()
        local_skill = project / ".agents" / "skills" / "review"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text("---\ndescription: Local review\n---\nLocal")

        config = load_config(str(project))
        assert config.skills["review"].description == "Local review"
        assert config.skills["review"].content == "Local"


class TestParseSkillMetadata:
    """Test _parse_skill_metadata helper."""

    def test_empty_metadata(self):
        result = _parse_skill_metadata({})
        assert result["name"] == ""
        assert result["description"] == ""
        assert result["argument_hint"] == ""
        assert result["user_invocable"] is True
        assert result["disable_model_invocation"] is False
        assert result["allowed_tools"] == []

    def test_boolean_fields(self):
        result = _parse_skill_metadata({
            "disable-model-invocation": "true",
            "user-invocable": "false",
        })
        assert result["disable_model_invocation"] is True
        assert result["user_invocable"] is False

    def test_boolean_case_insensitive(self):
        result = _parse_skill_metadata({
            "disable-model-invocation": "True",
            "user-invocable": "FALSE",
        })
        assert result["disable_model_invocation"] is True
        assert result["user_invocable"] is False

    def test_allowed_tools_comma_separated(self):
        result = _parse_skill_metadata({"allowed-tools": "Read, Edit, Grep"})
        assert result["allowed_tools"] == ["Read", "Edit", "Grep"]

    def test_allowed_tools_single(self):
        result = _parse_skill_metadata({"allowed-tools": "Read"})
        assert result["allowed_tools"] == ["Read"]

    def test_allowed_tools_empty(self):
        result = _parse_skill_metadata({"allowed-tools": ""})
        assert result["allowed_tools"] == []

    def test_name_override(self):
        result = _parse_skill_metadata({"name": "my-custom-name"})
        assert result["name"] == "my-custom-name"


class TestRenderSkillTemplate:
    """Test render_skill_template with argument substitution."""

    _HEADER = "> **Skill argument:** `{arg}`\n> Use this value exactly where the skill instructions reference the argument.\n\n"

    def test_arguments_substitution(self):
        result = render_skill_template("Review $ARGUMENTS carefully", "src/main.py")
        assert result == self._HEADER.format(arg="src/main.py") + "Review src/main.py carefully"

    def test_indexed_arguments(self):
        result = render_skill_template("File: $ARGUMENTS[0], Line: $ARGUMENTS[1]", "main.py 42")
        assert result == self._HEADER.format(arg="main.py 42") + "File: main.py, Line: 42"

    def test_positional_arguments(self):
        result = render_skill_template("File: $1, Line: $2", "main.py 42")
        assert result == self._HEADER.format(arg="main.py 42") + "File: main.py, Line: 42"

    def test_empty_arguments(self):
        result = render_skill_template("Review $ARGUMENTS", "")
        assert result == "Review "

    def test_missing_indexed_argument(self):
        result = render_skill_template("$ARGUMENTS[0] and $ARGUMENTS[5]", "only-one")
        assert result == self._HEADER.format(arg="only-one") + "only-one and "

    def test_missing_positional_argument(self):
        result = render_skill_template("$1 and $3", "first second")
        assert result == self._HEADER.format(arg="first second") + "first and "

    def test_no_placeholders(self):
        result = render_skill_template("Plain content", "args")
        assert result == self._HEADER.format(arg="args") + "Plain content"

    def test_all_substitution_types(self):
        result = render_skill_template(
            "Full: $ARGUMENTS, First: $ARGUMENTS[0], Second: $2",
            "foo bar"
        )
        assert result == self._HEADER.format(arg="foo bar") + "Full: foo bar, First: foo, Second: bar"

    def test_header_not_added_for_empty_arguments(self):
        result = render_skill_template("No args here", "")
        assert result == "No args here"
        assert "Skill argument" not in result

    def test_header_not_added_for_whitespace_arguments(self):
        result = render_skill_template("No args here", "   ")
        assert result == "No args here"
        assert "Skill argument" not in result

    def test_header_contains_exact_argument_value(self):
        result = render_skill_template("Base: $ARGUMENTS", "develop")
        assert "> **Skill argument:** `develop`" in result
        assert result.startswith("> **Skill argument:**")


class TestCustomAgent:
    """Test CustomAgent dataclass."""

    def test_custom_agent_creation(self):
        agent = CustomAgent(
            name="reviewer",
            description="Review code",
            system_prompt="You are a code reviewer.",
            source_path="/path/to/reviewer.md",
        )
        assert agent.name == "reviewer"
        assert agent.description == "Review code"
        assert agent.system_prompt == "You are a code reviewer."
        assert agent.model is None
        assert agent.tools == []
        assert agent.max_turns is None
        assert agent.mode == "primary"

    def test_custom_agent_with_all_fields(self):
        agent = CustomAgent(
            name="debugger",
            description="Debug issues",
            system_prompt="You are a debugger.",
            source_path="/path",
            model="anthropic/claude-sonnet-4-5",
            tools=["read_file", "bash"],
            max_turns=15,
            mode="subagent",
        )
        assert agent.model == "anthropic/claude-sonnet-4-5"
        assert agent.tools == ["read_file", "bash"]
        assert agent.max_turns == 15
        assert agent.mode == "subagent"


class TestParseAgentMetadata:
    """Test _parse_agent_metadata helper."""

    def test_empty_metadata(self):
        result = _parse_agent_metadata({})
        assert result["name"] == ""
        assert result["description"] == ""
        assert result["model"] is None
        assert result["mode"] == "primary"
        assert result["max_turns"] is None
        assert result["tools"] == []

    def test_full_metadata(self):
        result = _parse_agent_metadata({
            "name": "reviewer",
            "description": "Review code",
            "model": "anthropic/claude-sonnet-4-5",
            "mode": "subagent",
            "max_turns": "15",
            "tools": "read_file, bash, grep_search",
        })
        assert result["name"] == "reviewer"
        assert result["description"] == "Review code"
        assert result["model"] == "anthropic/claude-sonnet-4-5"
        assert result["mode"] == "subagent"
        assert result["max_turns"] == 15
        assert result["tools"] == ["read_file", "bash", "grep_search"]

    def test_tools_as_json_dict(self):
        result = _parse_agent_metadata({
            "tools": '{"bash": false, "write_file": true}',
        })
        assert result["tools"] == {"bash": False, "write_file": True}

    def test_tools_empty(self):
        result = _parse_agent_metadata({"tools": ""})
        assert result["tools"] == []

    def test_tools_invalid_json(self):
        result = _parse_agent_metadata({"tools": "{invalid"})
        assert result["tools"] == []

    def test_max_turns_non_numeric(self):
        result = _parse_agent_metadata({"max_turns": "abc"})
        assert result["max_turns"] is None

    def test_max_turns_hyphenated(self):
        result = _parse_agent_metadata({"max-turns": "20"})
        assert result["max_turns"] == 20

    def test_mode_case_insensitive(self):
        result = _parse_agent_metadata({"mode": "SUBAGENT"})
        assert result["mode"] == "subagent"


class TestDiscoverAgents:
    """Test _discover_agents function."""

    def test_discover_agents_empty(self, tmp_path):
        agents = _discover_agents([tmp_path])
        assert agents == {}

    def test_discover_agents_no_agents_dir(self, tmp_path):
        (tmp_path / "skills").mkdir()
        agents = _discover_agents([tmp_path])
        assert agents == {}

    def test_discover_agents_single(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "reviewer.md").write_text(
            "---\nname: Code Reviewer\ndescription: Review code\n---\nYou are a reviewer."
        )
        agents = _discover_agents([tmp_path])
        assert "reviewer" in agents
        assert agents["reviewer"].name == "Code Reviewer"
        assert agents["reviewer"].description == "Review code"
        assert agents["reviewer"].system_prompt == "You are a reviewer."

    def test_discover_agents_override(self, tmp_path):
        global_root = tmp_path / "global"
        local_root = tmp_path / "local"
        for root in (global_root, local_root):
            d = root / "agents"
            d.mkdir(parents=True)

        (global_root / "agents" / "reviewer.md").write_text(
            "---\ndescription: Global reviewer\n---\nGlobal prompt"
        )
        (local_root / "agents" / "reviewer.md").write_text(
            "---\ndescription: Local reviewer\n---\nLocal prompt"
        )

        agents = _discover_agents([global_root, local_root])
        assert agents["reviewer"].description == "Local reviewer"
        assert agents["reviewer"].system_prompt == "Local prompt"

    def test_discover_agents_ignores_non_md(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "valid.md").write_text("---\ndescription: Valid\n---\nPrompt")
        (agents_dir / "invalid.txt").write_text("Not an agent")
        (agents_dir / "also_invalid.py").write_text("Not an agent")

        agents = _discover_agents([tmp_path])
        assert len(agents) == 1
        assert "valid" in agents

    def test_discover_agents_skips_empty_body(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "empty.md").write_text("---\ndescription: No body\n---\n")

        agents = _discover_agents([tmp_path])
        assert agents == {}

    def test_discover_agents_with_tools(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "reader.md").write_text(
            "---\nname: Reader\ndescription: Read only\ntools: read_file, grep_search\nmode: subagent\n---\nRead stuff."
        )

        agents = _discover_agents([tmp_path])
        assert agents["reader"].tools == ["read_file", "grep_search"]
        assert agents["reader"].mode == "subagent"

    def test_load_config_with_custom_agents(self, tmp_path):
        agents_dir = tmp_path / ".agents" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "reviewer.md").write_text(
            "---\nname: Reviewer\ndescription: Review code\nmodel: anthropic/claude-sonnet-4-5\n---\nReview code."
        )

        config = load_config(str(tmp_path))
        assert "reviewer" in config.custom_agents
        assert config.custom_agents["reviewer"].model == "anthropic/claude-sonnet-4-5"


class TestAgentPermissions:
    """Tests for agent-level permission parsing."""

    def test_parse_agent_permission_from_frontmatter(self, tmp_path):
        agents_dir = tmp_path / ".agents" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "safe.md").write_text(
            "---\nname: Safe Agent\ndescription: Read-only\npermission:\n  edit: deny\n  bash: deny\n---\nYou are safe."
        )
        agents = _discover_agents([tmp_path / ".agents"])
        assert "safe" in agents
        assert agents["safe"].permission == {"edit": "deny", "bash": "deny"}

    def test_parse_agent_permission_nested_bash(self, tmp_path):
        agents_dir = tmp_path / ".agents" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "builder.md").write_text(
            "---\nname: Builder\ndescription: Build things\npermission:\n  bash:\n    git *: allow\n    npm *: allow\n---\nBuild stuff."
        )
        agents = _discover_agents([tmp_path / ".agents"])
        assert agents["builder"].permission == {
            "bash": {"git *": "allow", "npm *": "allow"},
        }

    def test_agent_no_permission_is_none(self, tmp_path):
        agents_dir = tmp_path / ".agents" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "basic.md").write_text(
            "---\nname: Basic\ndescription: Basic agent\n---\nDo stuff."
        )
        agents = _discover_agents([tmp_path / ".agents"])
        assert agents["basic"].permission is None

    def test_aru_json_agent_permission_override(self, tmp_path):
        # Create agent file
        agents_dir = tmp_path / ".agents" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "worker.md").write_text(
            "---\nname: Worker\ndescription: Worker agent\npermission:\n  edit: allow\n---\nWork."
        )
        # Create aru.json with agent override
        import json
        (tmp_path / "aru.json").write_text(json.dumps({
            "agent": {
                "worker": {
                    "permission": {"edit": "deny"}
                }
            }
        }))
        config = load_config(str(tmp_path))
        # aru.json override should win
        assert config.custom_agents["worker"].permission == {"edit": "deny"}
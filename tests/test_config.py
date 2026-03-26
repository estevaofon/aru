"""Unit tests for aru.config module."""

import pytest
from pathlib import Path
from aru.config import (
    AgentConfig,
    CustomCommand,
    Skill,
    _parse_frontmatter,
    render_command_template,
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
            source_path="/path/to/review.md",
        )
        assert skill.name == "review"
        assert skill.description == "Code review skill"
        assert skill.content == "Review the code carefully"
        assert skill.source_path == "/path/to/review.md"

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
        """Skills are only loaded when explicitly requested (token optimization)."""
        skill1 = Skill("review", "Review", "Review code", "/path1")
        skill2 = Skill("test", "Test", "Write tests", "/path2")
        config = AgentConfig(skills={"review": skill1, "test": skill2})
        result = config.get_extra_instructions()
        assert "## Skill: review" not in result
        assert "## Skill: test" not in result

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

    def test_frontmatter_line_without_colon(self):
        content = "---\ndescription: Test\nInvalid line\nkey: value\n---\nBody"
        metadata, body = _parse_frontmatter(content)
        # Should skip lines without colons
        assert "description" in metadata
        assert "key" in metadata
        assert "Invalid line" not in metadata
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
        skills_dir = tmp_path / ".agents" / "skills"
        skills_dir.mkdir(parents=True)
        
        skill_file = skills_dir / "review.md"
        skill_file.write_text("---\ndescription: Code review\n---\nReview carefully")
        
        config = load_config(str(tmp_path))
        assert "review" in config.skills
        skill = config.skills["review"]
        assert skill.name == "review"
        assert skill.description == "Code review"
        assert skill.content == "Review carefully"

    def test_load_config_skill_without_frontmatter(self, tmp_path):
        skills_dir = tmp_path / ".agents" / "skills"
        skills_dir.mkdir(parents=True)
        
        skill_file = skills_dir / "optimize.md"
        skill_file.write_text("Optimize the code")
        
        config = load_config(str(tmp_path))
        assert "optimize" in config.skills
        skill = config.skills["optimize"]
        assert skill.description == "Skill: optimize"
        assert skill.content == "Optimize the code"

    def test_load_config_ignores_non_md_skills(self, tmp_path):
        skills_dir = tmp_path / ".agents" / "skills"
        skills_dir.mkdir(parents=True)
        
        (skills_dir / "valid.md").write_text("Content")
        (skills_dir / "invalid.txt").write_text("Content")
        
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
        
        (skills_dir / "z.md").write_text("Z")
        (skills_dir / "a.md").write_text("A")
        (skills_dir / "m.md").write_text("M")
        
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
        
        skills_dir = tmp_path / ".agents" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "review.md").write_text("---\ndescription: Review\n---\nReview code")
        
        config = load_config(str(tmp_path))
        assert config.readme_md == "# Project"
        assert config.agents_md == "Instructions"
        assert len(config.commands) == 1
        assert len(config.skills) == 1
        assert config.has_instructions
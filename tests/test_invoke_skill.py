"""Tests for the `invoke_skill` tool (aru/tools/skill.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aru.config import AgentConfig, Skill
from aru.runtime import get_ctx
from aru.tools.skill import (
    _BASE_DOC,
    _update_invoke_skill_docstring,
    invoke_skill,
)


def _make_skill(
    name: str,
    body: str = "Default body",
    *,
    description: str = "Test skill",
    disable_model_invocation: bool = False,
    user_invocable: bool = True,
) -> Skill:
    return Skill(
        name=name,
        description=description,
        content=body,
        source_path=f"/fake/{name}/SKILL.md",
        disable_model_invocation=disable_model_invocation,
        user_invocable=user_invocable,
    )


def _set_config_with_skills(skills: dict[str, Skill]) -> AgentConfig:
    """Inject skills into the current runtime context's config."""
    cfg = AgentConfig()
    cfg.skills = skills
    get_ctx().config = cfg
    return cfg


class TestInvokeSkillHappyPath:
    def test_loads_content_for_valid_skill(self):
        _set_config_with_skills({
            "writing-plans": _make_skill(
                "writing-plans",
                body="# Writing Plans\n\nOpen with a checklist.",
                description="Use when converting a spec into a plan.",
            )
        })

        result = invoke_skill(name="writing-plans")

        assert "[Skill loaded: /writing-plans]" in result
        assert "<SKILL_CONTENT>" in result
        assert "# Writing Plans" in result
        assert "Open with a checklist." in result
        assert "</SKILL_CONTENT>" in result
        # Framing tells the agent the task_store was reset and it should call
        # create_task_list fresh for the new skill's checklist.
        assert "task_store has been RESET" in result
        assert "create_task_list" in result

    def test_substitutes_arguments(self):
        _set_config_with_skills({
            "writing-plans": _make_skill(
                "writing-plans",
                body="Spec at: $ARGUMENTS",
            )
        })

        result = invoke_skill(
            name="writing-plans",
            arguments="docs/aru/specs/foo.md",
        )

        assert "Spec at: docs/aru/specs/foo.md" in result
        assert "[Arguments: docs/aru/specs/foo.md]" in result

    def test_accepts_leading_slash(self):
        _set_config_with_skills({
            "writing-plans": _make_skill("writing-plans", body="body"),
        })

        result_with_slash = invoke_skill(name="/writing-plans")
        result_without_slash = invoke_skill(name="writing-plans")

        assert "[Skill loaded: /writing-plans]" in result_with_slash
        # Both paths should produce equivalent framing
        assert result_with_slash.replace("\n", "") != ""
        assert result_without_slash.replace("\n", "") != ""

    def test_no_arguments_renders_none_placeholder(self):
        _set_config_with_skills({
            "x": _make_skill("x", body="body"),
        })

        result = invoke_skill(name="x")

        assert "[Arguments: (none)]" in result


class TestInvokeSkillErrors:
    def test_empty_name_returns_error(self):
        _set_config_with_skills({})

        result = invoke_skill(name="")

        assert result.startswith("Error: invoke_skill requires")

    def test_not_found_lists_available_skills(self):
        _set_config_with_skills({
            "writing-plans": _make_skill("writing-plans", body="b"),
            "executing-plans": _make_skill("executing-plans", body="b"),
        })

        result = invoke_skill(name="brainstorming")

        assert "Error: skill not found" in result
        assert "'brainstorming'" in result
        assert "`writing-plans`" in result
        assert "`executing-plans`" in result

    def test_disabled_for_model_refused(self):
        _set_config_with_skills({
            "restricted": _make_skill(
                "restricted",
                body="secret",
                disable_model_invocation=True,
            ),
        })

        result = invoke_skill(name="restricted")

        assert "Error: skill 'restricted' is marked disable_model_invocation" in result
        assert "secret" not in result  # body must NOT leak


class TestInvokeSkillActiveSkill:
    def test_sets_active_skill_on_success(self):
        from aru.session import Session

        session = Session()
        get_ctx().session = session
        assert session.active_skill is None

        _set_config_with_skills({
            "writing-plans": _make_skill("writing-plans", body="body"),
        })

        result = invoke_skill(name="writing-plans")
        assert "[Skill loaded: /writing-plans]" in result
        assert session.active_skill == "writing-plans"

    def test_replaces_previously_active_skill(self):
        from aru.session import Session

        session = Session()
        session.active_skill = "brainstorming"
        get_ctx().session = session

        _set_config_with_skills({
            "writing-plans": _make_skill("writing-plans", body="body"),
        })

        invoke_skill(name="writing-plans")
        assert session.active_skill == "writing-plans"

    def test_leaves_active_skill_unchanged_on_error(self):
        from aru.session import Session

        session = Session()
        session.active_skill = "brainstorming"
        get_ctx().session = session

        _set_config_with_skills({
            "writing-plans": _make_skill("writing-plans", body="body"),
        })

        result = invoke_skill(name="nonexistent")
        assert "Error: skill not found" in result
        assert session.active_skill == "brainstorming"

    def test_disabled_skill_hidden_from_not_found_listing(self):
        _set_config_with_skills({
            "public": _make_skill("public", body="b"),
            "hidden": _make_skill(
                "hidden",
                body="b",
                disable_model_invocation=True,
            ),
        })

        result = invoke_skill(name="does-not-exist")

        assert "`public`" in result
        assert "`hidden`" not in result

    def test_no_config_graceful_error(self):
        get_ctx().config = None

        result = invoke_skill(name="anything")

        assert "Error: no skill config available" in result


class TestTaskStoreReset:
    """Regression: invoke_skill must clear the task_store so the loaded
    skill's Entering gate can call create_task_list without being refused."""

    def test_reset_clears_existing_task_list(self):
        _set_config_with_skills({
            "next-skill": _make_skill("next-skill", body="body"),
        })
        # Seed a prior task list (as if the previous skill had one)
        ctx = get_ctx()
        ctx.task_store.create(["old task 1", "old task 2", "old task 3"])
        assert ctx.task_store.is_created

        invoke_skill(name="next-skill")

        assert not ctx.task_store.is_created
        assert ctx.task_store.get_all() == []

    def test_reset_framing_mentions_fresh_create_task_list(self):
        _set_config_with_skills({
            "x": _make_skill("x", body="b"),
        })
        get_ctx().task_store.create(["a", "b"])

        result = invoke_skill(name="x")

        assert "task_store has been RESET" in result
        assert "create_task_list" in result

    def test_error_paths_do_not_reset_task_store(self):
        """If invoke_skill returns an error (skill not found, disabled, etc.),
        the task_store should NOT be reset — otherwise a bad invocation destroys
        the current phase's checklist."""
        _set_config_with_skills({
            "real": _make_skill("real", body="b"),
        })
        ctx = get_ctx()
        ctx.task_store.create(["preserve me"])

        # Not found → error, store should remain
        invoke_skill(name="does-not-exist")
        assert ctx.task_store.is_created
        assert ctx.task_store.get_all()[0]["description"] == "preserve me"

        # Disabled skill → error, store should remain
        _set_config_with_skills({
            "hidden": _make_skill("hidden", body="b", disable_model_invocation=True),
            "real": _make_skill("real", body="b"),
        })
        ctx.task_store.create(["preserve me 2"])
        invoke_skill(name="hidden")
        assert ctx.task_store.is_created
        assert ctx.task_store.get_all()[0]["description"] == "preserve me 2"


class TestDocstringUpdate:
    def test_base_docstring_present(self):
        _update_invoke_skill_docstring(None)
        assert "Load another skill's instructions" in (invoke_skill.__doc__ or "")

    def test_docstring_lists_available_skills(self):
        skills = {
            "alpha": _make_skill("alpha", description="First skill"),
            "beta": _make_skill("beta", description="Second skill"),
        }

        _update_invoke_skill_docstring(skills)
        doc = invoke_skill.__doc__ or ""

        assert "Available skills" in doc
        assert 'name="alpha"' in doc
        assert 'name="beta"' in doc
        assert "First skill" in doc
        assert "Second skill" in doc

    def test_docstring_skips_disable_model_invocation(self):
        skills = {
            "visible": _make_skill("visible", description="shown"),
            "hidden": _make_skill(
                "hidden",
                description="should not appear",
                disable_model_invocation=True,
            ),
        }

        _update_invoke_skill_docstring(skills)
        doc = invoke_skill.__doc__ or ""

        assert 'name="visible"' in doc
        assert 'name="hidden"' not in doc
        assert "should not appear" not in doc

    def test_docstring_truncates_long_descriptions(self):
        long_desc = "x" * 300
        skills = {
            "big": _make_skill("big", description=long_desc),
        }

        _update_invoke_skill_docstring(skills)
        doc = invoke_skill.__doc__ or ""

        # Description should have been truncated with "..."
        assert "..." in doc
        assert "x" * 300 not in doc

    def test_empty_skills_keeps_base_only(self):
        _update_invoke_skill_docstring({})
        doc = invoke_skill.__doc__ or ""

        assert doc == _BASE_DOC


class TestRegistryIntegration:
    def test_invoke_skill_in_registry(self):
        from aru.tools.registry import TOOL_REGISTRY, ALL_TOOLS, GENERAL_TOOLS

        assert "invoke_skill" in TOOL_REGISTRY
        assert TOOL_REGISTRY["invoke_skill"] is invoke_skill
        assert invoke_skill in ALL_TOOLS
        assert invoke_skill in GENERAL_TOOLS

    def test_invoke_skill_excluded_from_subagent_tools(self):
        # Import after registry is constructed so _DEFAULT_SUBAGENT_TOOLS is populated
        from aru.tools.delegate import _DEFAULT_SUBAGENT_TOOLS
        import aru.tools.registry  # noqa: F401 — ensures _DEFAULT_SUBAGENT_TOOLS was set

        assert invoke_skill not in _DEFAULT_SUBAGENT_TOOLS

    def test_invoke_skill_excluded_from_planner_and_explorer(self):
        from aru.tools.registry import PLANNER_TOOLS, EXPLORER_TOOLS

        assert invoke_skill not in PLANNER_TOOLS
        assert invoke_skill not in EXPLORER_TOOLS

    def test_invoke_skill_not_blocked_in_plan_mode(self):
        from aru.agent_factory import _PLAN_MODE_BLOCKED_TOOLS

        assert "invoke_skill" not in _PLAN_MODE_BLOCKED_TOOLS


# Reset docstring state between test classes so they don't leak
@pytest.fixture(autouse=True)
def _restore_docstring():
    original = invoke_skill.__doc__
    yield
    invoke_skill.__doc__ = original

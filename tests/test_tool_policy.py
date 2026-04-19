"""Tests for the unified tool-policy gate.

Covers:
- Plan mode alone denies mutating tools.
- Active skill alone denies listed tools.
- Both together produce ONE combined BLOCKED message (scenario 1).
- exit_plan_mode is always allowed (scenario 2 defense-in-depth).
- Scope: subagent ctx.agent_id does not see primary's active skill (C3
  is enforced through the policy too).
- resolve_permission consults the same policy (unification).
"""
from __future__ import annotations

from aru.config import AgentConfig, Skill
from aru.runtime import get_ctx, init_ctx
from aru.session import Session
from aru.tool_policy import evaluate_tool_policy


def _setup(plan_mode: bool = False, skill_name: str | None = None,
           disallowed: list[str] | None = None,
           agent_id: str | None = None) -> None:
    session = Session()
    session.plan_mode = plan_mode
    if skill_name:
        session.set_active_skill(None, skill_name)
    cfg = AgentConfig()
    if skill_name:
        cfg.skills = {
            skill_name: Skill(
                name=skill_name,
                description="",
                content="",
                source_path="/fake",
                disallowed_tools=disallowed or [],
            ),
        }
    ctx = init_ctx()
    ctx.session = session
    ctx.config = cfg
    ctx.agent_id = agent_id


def test_allows_when_no_gates_active():
    _setup()
    assert evaluate_tool_policy("bash").allowed is True


def test_plan_mode_denies_mutating_tool():
    _setup(plan_mode=True)
    d = evaluate_tool_policy("bash")
    assert d.allowed is False
    assert d.reasons[0].type == "plan_mode"
    assert "BLOCKED" in d.message
    assert "plan mode" in d.message.lower()


def test_plan_mode_does_not_deny_readonly_tool():
    _setup(plan_mode=True)
    assert evaluate_tool_policy("read_file").allowed is True


def test_skill_disallowed_denies_listed_tool():
    _setup(skill_name="writing-plans", disallowed=["enter_plan_mode"])
    d = evaluate_tool_policy("enter_plan_mode")
    assert d.allowed is False
    assert d.reasons[0].type == "skill_disallowed"
    assert "writing-plans" in d.message


def test_exit_plan_mode_is_always_allowed_even_with_skill_deny():
    _setup(skill_name="writing-plans", disallowed=["exit_plan_mode"])
    assert evaluate_tool_policy("exit_plan_mode").allowed is True


def test_exit_plan_mode_allowed_in_plan_mode():
    """Sanity: the escape-hatch tool is reachable from within plan mode."""
    _setup(plan_mode=True)
    assert evaluate_tool_policy("exit_plan_mode").allowed is True


def test_combined_denial_emits_single_message_with_all_reasons():
    """Scenario 1 fix: when BOTH plan mode and an active skill deny the
    same tool, the agent must see ONE coherent BLOCKED message listing
    every reason — not two contradictory ones fired one after the other."""
    _setup(
        plan_mode=True,
        skill_name="writing-plans",
        disallowed=["bash"],
    )
    d = evaluate_tool_policy("bash")
    assert d.allowed is False
    kinds = {r.type for r in d.reasons}
    assert kinds == {"plan_mode", "skill_disallowed"}
    # Single BLOCKED header, both reasons rendered as a list
    assert d.message.count("BLOCKED") == 1
    assert "plan mode" in d.message.lower()
    assert "writing-plans" in d.message
    # Exactly one closing instruction
    assert d.message.count("Do NOT retry") == 1


def test_subagent_scope_does_not_see_primary_active_skill():
    """C3 + C5 parity: with ctx.agent_id set, the policy looks at the
    subagent's slot (empty) not the parent's."""
    _setup(
        skill_name="writing-plans",
        disallowed=["bash"],
        agent_id="subagent-x",
    )
    # The skill is registered in session.active_skills[None] (primary),
    # but the subagent asks for its own scope (agent_id=subagent-x) → no skill
    assert evaluate_tool_policy("bash").allowed is True


def test_resolve_permission_consults_policy():
    """Unification: resolve_permission must reach the same 'deny' conclusion
    as the tool-policy gate when the category's representative tool is
    blocked (e.g. bash in plan mode)."""
    _setup(plan_mode=True)
    from aru.permissions import resolve_permission
    action, pattern = resolve_permission("bash", "ls -la")
    assert action == "deny"
    assert pattern == "tool-policy"


def test_resolve_permission_allows_when_policy_allows():
    """Control case: without plan mode or a skill deny, resolve_permission
    falls back to its normal rules (user prompt in "ask" mode etc.)."""
    _setup()
    from aru.permissions import resolve_permission
    action, _ = resolve_permission("bash", "ls -la")
    # default behavior for bash is "ask" (non-allowlisted commands)
    assert action in ("ask", "allow")


def test_no_ctx_returns_allow():
    """Defensive: when no RuntimeContext is installed (unit tests that
    drive the wrapper directly), the policy must not crash — it treats
    the absence as 'no policy configured'."""
    # Install a ctx, then explicitly clear its session/config to simulate
    # partial setup similar to what older test harnesses do.
    ctx = init_ctx()
    ctx.session = None
    ctx.config = None
    assert evaluate_tool_policy("bash").allowed is True


# ---------------------------------------------------------------------------
# plan_mode_rules / skill_rules — Ruleset-producing helpers (Fase 2)
# ---------------------------------------------------------------------------


def test_plan_mode_rules_empty_when_plan_mode_off():
    from aru.tool_policy import plan_mode_rules
    session = Session()
    session.plan_mode = False
    assert plan_mode_rules(session) == []


def test_plan_mode_rules_denies_all_blocked_tools():
    from aru.tool_policy import PLAN_MODE_BLOCKED_TOOLS, plan_mode_rules
    session = Session()
    session.plan_mode = True
    rules = plan_mode_rules(session)
    denied = {r.permission for r in rules if r.action == "deny" and r.pattern == "*"}
    assert denied == set(PLAN_MODE_BLOCKED_TOOLS)


def test_plan_mode_rules_none_session():
    from aru.tool_policy import plan_mode_rules
    assert plan_mode_rules(None) == []


def test_skill_rules_empty_without_active_skill():
    from aru.tool_policy import skill_rules
    session = Session()
    cfg = AgentConfig()
    assert skill_rules(session, cfg, None) == []


def test_skill_rules_denies_disallowed_tools():
    from aru.tool_policy import skill_rules
    session = Session()
    session.set_active_skill(None, "writing-plans")
    cfg = AgentConfig()
    cfg.skills = {
        "writing-plans": Skill(
            name="writing-plans",
            description="",
            content="",
            source_path="/fake",
            disallowed_tools=["bash", "enter_plan_mode"],
        ),
    }
    rules = skill_rules(session, cfg, None)
    assert {r.permission for r in rules} == {"bash", "enter_plan_mode"}
    assert all(r.action == "deny" and r.pattern == "*" for r in rules)


def test_skill_rules_scoped_by_agent_id():
    """Subagent with its own agent_id sees the empty slot, not the parent's."""
    from aru.tool_policy import skill_rules
    session = Session()
    session.set_active_skill(None, "writing-plans")  # primary scope
    cfg = AgentConfig()
    cfg.skills = {
        "writing-plans": Skill(
            name="writing-plans",
            description="",
            content="",
            source_path="/fake",
            disallowed_tools=["bash"],
        ),
    }
    # Subagent scope — no skill active
    assert skill_rules(session, cfg, "subagent-x") == []


def test_tool_policy_composes_through_evaluate():
    """Fase 2 invariant: evaluate_tool_policy uses the same `evaluate()`
    function that resolves user rules. The rulesets are just more sources
    of Rule, not a separate machine."""
    from aru.permissions import evaluate
    from aru.tool_policy import plan_mode_rules

    session = Session()
    session.plan_mode = True
    # Direct composition — what evaluate_tool_policy does internally
    rule = evaluate("bash", "*", plan_mode_rules(session))
    assert rule.action == "deny"
    # Read-only tool: no deny rule exists in the ruleset
    rule2 = evaluate("read_file", "*", plan_mode_rules(session))
    assert rule2.action == "ask"  # synthetic default, not denied

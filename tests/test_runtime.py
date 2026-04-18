"""Tests for RuntimeContext and fork_ctx scope isolation."""
from __future__ import annotations

from aru.runtime import fork_ctx, init_ctx, get_ctx, set_ctx


class TestAgentId:
    def test_primary_ctx_has_no_agent_id(self):
        ctx = init_ctx()
        assert ctx.agent_id is None

    def test_fork_assigns_unique_agent_id(self):
        init_ctx()
        forked = fork_ctx()
        assert forked.agent_id is not None
        assert forked.agent_id.startswith("subagent-")

    def test_two_forks_get_different_ids(self):
        init_ctx()
        a = fork_ctx()
        b = fork_ctx()
        assert a.agent_id != b.agent_id

    def test_fork_does_not_mutate_parent_agent_id(self):
        """fork_ctx() increments the parent's counter but must NOT change
        the parent's agent_id — the parent is still the same scope."""
        parent = init_ctx()
        assert parent.agent_id is None
        _ = fork_ctx()
        assert parent.agent_id is None

    def test_nested_fork_still_yields_unique_id(self):
        """A fork-of-a-fork gets its own unique id (not inheriting the
        parent's). Scope isolation must hold across nesting."""
        init_ctx()
        child = fork_ctx()
        set_ctx(child)
        grandchild = fork_ctx()
        assert grandchild.agent_id != child.agent_id
        assert grandchild.agent_id is not None

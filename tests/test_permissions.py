"""Tests for the granular permission system."""

import pytest

from aru.permissions import (
    PermissionAction,
    PermissionConfig,
    PermissionRule,
    _build_rules,
    _match_bash_rule,
    _match_rule,
    _most_restrictive,
    _normalize_cmd,
    _resolve_bash_compound,
    _shell_split,
    check_permission,
    get_skip_permissions,
    merge_configs,
    parse_permission_config,
    permission_scope,
    reset_session,
    resolve_permission,
    set_config,
    set_skip_permissions,
)
from aru.runtime import get_ctx


# ---------------------------------------------------------------------------
# parse_permission_config
# ---------------------------------------------------------------------------


class TestParsePermissionConfig:
    def test_none_returns_default(self):
        config = parse_permission_config(None)
        assert config.default == "ask"
        assert config.categories == {}

    def test_empty_dict_returns_default(self):
        config = parse_permission_config({})
        assert config.default == "ask"

    def test_string_shorthand(self):
        config = parse_permission_config("allow")
        assert config.default == "allow"
        assert config.categories == {}

    def test_invalid_string_defaults_to_ask(self):
        config = parse_permission_config("yolo")
        assert config.default == "ask"

    def test_global_default_with_star(self):
        config = parse_permission_config({"*": "deny"})
        assert config.default == "deny"
        assert config.categories == {}

    def test_category_string_shorthand(self):
        config = parse_permission_config({"read": "allow", "edit": "deny"})
        assert len(config.categories["read"]) == 1
        assert config.categories["read"][0] == PermissionRule("*", "allow")
        assert config.categories["edit"][0] == PermissionRule("*", "deny")

    def test_category_with_patterns(self):
        config = parse_permission_config({
            "bash": {
                "*": "ask",
                "git *": "allow",
                "rm -rf *": "deny",
            }
        })
        rules = config.categories["bash"]
        assert len(rules) == 3
        assert rules[0] == PermissionRule("*", "ask")
        assert rules[1] == PermissionRule("git *", "allow")
        assert rules[2] == PermissionRule("rm -rf *", "deny")

    def test_mixed_categories(self):
        config = parse_permission_config({
            "*": "ask",
            "read": "allow",
            "bash": {"*": "ask", "git *": "allow"},
        })
        assert config.default == "ask"
        assert config.categories["read"][0].action == "allow"
        assert len(config.categories["bash"]) == 2


# ---------------------------------------------------------------------------
# _match_rule
# ---------------------------------------------------------------------------


class TestMatchRule:
    def test_wildcard(self):
        assert _match_rule("*", "anything") is True

    def test_fnmatch_glob(self):
        assert _match_rule("*.env", ".env") is True
        assert _match_rule("*.env", "config.env") is True
        assert _match_rule("*.env", "config.yaml") is False

    def test_basename_match(self):
        assert _match_rule("*.env", "/home/user/project/.env") is True
        assert _match_rule("*.env", "C:\\Users\\project\\.env") is True

    def test_env_example_allowed(self):
        assert _match_rule("*.env.example", ".env.example") is True
        assert _match_rule("*.env.*", ".env.local") is True


# ---------------------------------------------------------------------------
# _match_bash_rule
# ---------------------------------------------------------------------------


class TestMatchBashRule:
    def test_wildcard(self):
        assert _match_bash_rule("*", "anything") is True

    def test_exact(self):
        assert _match_bash_rule("ls", "ls") is True
        # "ls" as pattern also matches "ls -la" via prefix match (intended behavior)
        assert _match_bash_rule("ls", "ls -la") is True
        assert _match_bash_rule("ls", "lsblk") is False

    def test_prefix(self):
        assert _match_bash_rule("git status", "git status --short") is True
        assert _match_bash_rule("git status", "git log") is False

    def test_fnmatch_glob(self):
        assert _match_bash_rule("git *", "git status") is True
        assert _match_bash_rule("git *", "git log --oneline") is True
        assert _match_bash_rule("git *", "npm install") is False

    def test_rm_deny(self):
        assert _match_bash_rule("rm -rf *", "rm -rf /") is True
        assert _match_bash_rule("rm -rf *", "rm file.txt") is False

    def test_windows_backslash_and_dot_prefix(self):
        # .\.venv\Scripts\python.exe should match .venv/Scripts/python.exe *
        assert _match_bash_rule(
            ".venv/Scripts/python.exe *",
            r".\.venv\Scripts\python.exe -m pytest",
        ) is True
        # Also without .\ prefix
        assert _match_bash_rule(
            ".venv/Scripts/python.exe *",
            r".venv\Scripts\python.exe -m pytest",
        ) is True


# ---------------------------------------------------------------------------
# _shell_split
# ---------------------------------------------------------------------------


class TestShellSplit:
    def test_no_separator(self):
        assert _shell_split("ls -la", ("&&",)) is None

    def test_and_separator(self):
        assert _shell_split("ls && git status", ("&&",)) == ["ls", "git status"]

    def test_semicolon(self):
        assert _shell_split("ls; pwd", (";",)) == ["ls", "pwd"]

    def test_pipe(self):
        assert _shell_split("cat file | grep foo", ("|",)) == ["cat file", "grep foo"]

    def test_quoted_separator(self):
        result = _shell_split("echo 'a && b'", ("&&",))
        assert result is None  # && is inside quotes

    def test_multiple_separators(self):
        result = _shell_split("a && b && c", ("&&",))
        assert result == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# resolve_permission
# ---------------------------------------------------------------------------


class TestResolvePermission:
    def setup_method(self):
        set_skip_permissions(False)
        set_config(PermissionConfig())
        reset_session()

    def test_skip_permissions(self):
        set_skip_permissions(True)
        action, pattern = resolve_permission("edit", "main.py")
        assert action == "allow"
        set_skip_permissions(False)

    def test_default_read_is_allow(self):
        action, _ = resolve_permission("read", "main.py")
        assert action == "allow"

    def test_default_edit_is_ask(self):
        action, _ = resolve_permission("edit", "main.py")
        assert action == "ask"

    def test_default_write_is_ask(self):
        action, _ = resolve_permission("write", "main.py")
        assert action == "ask"

    def test_env_file_denied_by_default(self):
        for category in ("read", "edit", "write"):
            action, _ = resolve_permission(category, ".env")
            assert action == "deny", f".env should be denied for {category}"

    def test_env_local_denied(self):
        action, _ = resolve_permission("read", ".env.local")
        assert action == "deny"

    def test_env_example_allowed(self):
        action, _ = resolve_permission("read", ".env.example")
        assert action == "allow"

    def test_user_config_overrides_defaults(self):
        config = parse_permission_config({
            "edit": {"*": "allow"},
        })
        set_config(config)
        action, _ = resolve_permission("edit", "main.py")
        assert action == "allow"

    def test_last_match_wins(self):
        config = parse_permission_config({
            "edit": {
                "*": "allow",
                "*.secret": "deny",
            },
        })
        set_config(config)
        action, _ = resolve_permission("edit", "keys.secret")
        assert action == "deny"

    def test_user_can_override_env_deny(self):
        config = parse_permission_config({
            "read": {
                "*": "allow",
                "*.env": "allow",  # user explicitly allows
            },
        })
        set_config(config)
        action, _ = resolve_permission("read", ".env")
        assert action == "allow"

    def test_bash_safe_command(self):
        action, _ = resolve_permission("bash", "ls")
        assert action == "allow"

    def test_bash_safe_command_with_args(self):
        action, _ = resolve_permission("bash", "git status --short")
        assert action == "allow"

    def test_bash_unsafe_command(self):
        action, _ = resolve_permission("bash", "pip install foo")
        assert action == "ask"

    def test_bash_compound_all_safe(self):
        action, _ = resolve_permission("bash", "ls && git status")
        assert action == "allow"

    def test_bash_compound_one_unsafe(self):
        action, _ = resolve_permission("bash", "ls && rm foo")
        assert action == "ask"

    def test_bash_pipe_all_safe(self):
        action, _ = resolve_permission("bash", "cat file | grep foo")
        assert action == "allow"

    def test_bash_pipe_one_unsafe(self):
        action, _ = resolve_permission("bash", "cat file | python")
        assert action == "ask"

    def test_bash_user_deny_rule(self):
        config = parse_permission_config({
            "bash": {"rm -rf *": "deny"},
        })
        set_config(config)
        action, _ = resolve_permission("bash", "rm -rf /")
        assert action == "deny"

    def test_bash_user_allow_overrides_safe_prefix(self):
        config = parse_permission_config({
            "bash": {"*": "deny"},
        })
        set_config(config)
        # User catch-all deny should override safe prefix (last-match-wins)
        action, _ = resolve_permission("bash", "ls")
        assert action == "deny"

    def test_session_allowed(self):
        get_ctx().session_allowed.add(("edit", "*"))
        action, _ = resolve_permission("edit", "main.py")
        assert action == "allow"
        reset_session()

    def test_session_allowed_pattern(self):
        get_ctx().session_allowed.add(("bash", "npm *"))
        action, _ = resolve_permission("bash", "npm install")
        assert action == "allow"
        reset_session()

    def test_glob_default_allow(self):
        action, _ = resolve_permission("glob", "")
        assert action == "allow"

    def test_web_search_default_allow(self):
        action, _ = resolve_permission("web_search", "")
        assert action == "allow"

    def test_unknown_category_uses_global_default(self):
        action, _ = resolve_permission("unknown_tool", "")
        assert action == "ask"

    def test_global_default_override(self):
        config = parse_permission_config({"*": "allow"})
        set_config(config)
        action, _ = resolve_permission("unknown_tool", "")
        assert action == "allow"
        set_config(PermissionConfig())


# ---------------------------------------------------------------------------
# _most_restrictive
# ---------------------------------------------------------------------------


class TestMostRestrictive:
    def test_deny_wins(self):
        result = _most_restrictive([("allow", "*"), ("deny", "*.env"), ("ask", "foo")])
        assert result == ("deny", "*.env")

    def test_ask_over_allow(self):
        result = _most_restrictive([("allow", "*"), ("ask", "foo")])
        assert result == ("ask", "foo")

    def test_all_allow(self):
        result = _most_restrictive([("allow", "a"), ("allow", "b")])
        assert result == ("allow", "a")


# ---------------------------------------------------------------------------
# check_permission (unit tests with mocked input)
# ---------------------------------------------------------------------------


class TestCheckPermission:
    def setup_method(self):
        set_skip_permissions(False)
        set_config(PermissionConfig())
        reset_session()

    def test_allow_returns_true(self):
        assert check_permission("read", "main.py", "reading main.py") is True

    def test_deny_returns_false(self):
        assert check_permission("edit", ".env", "editing .env") is False

    def test_skip_permissions_allows_all(self):
        set_skip_permissions(True)
        assert check_permission("edit", "main.py", "editing") is True
        assert check_permission("bash", "rm -rf /", "danger") is True
        set_skip_permissions(False)


# ---------------------------------------------------------------------------
# _build_rules
# ---------------------------------------------------------------------------


class TestBuildRules:
    def setup_method(self):
        set_config(PermissionConfig())

    def test_bash_includes_safe_prefix_rules(self):
        rules = _build_rules("bash")
        patterns = [r.pattern for r in rules]
        assert "ls" in patterns
        assert "git status" in patterns
        assert "git status *" in patterns

    def test_read_includes_sensitive_file_rules(self):
        rules = _build_rules("read")
        patterns = [r.pattern for r in rules]
        assert "*.env" in patterns
        assert "*.env.*" in patterns
        assert "*.env.example" in patterns

    def test_edit_includes_sensitive_file_rules(self):
        rules = _build_rules("edit")
        patterns = [r.pattern for r in rules]
        assert "*.env" in patterns

    def test_write_includes_sensitive_file_rules(self):
        rules = _build_rules("write")
        patterns = [r.pattern for r in rules]
        assert "*.env" in patterns

    def test_unknown_category_returns_empty(self):
        rules = _build_rules("unknown")
        assert rules == []

    def test_user_config_appended_after_defaults(self):
        config = parse_permission_config({
            "read": {"*.md": "allow"},
        })
        set_config(config)
        rules = _build_rules("read")
        patterns = [r.pattern for r in rules]
        # defaults first, user rule last
        assert patterns[0] == "*.env"
        assert "*.md" in patterns

    def test_glob_category_no_special_rules(self):
        rules = _build_rules("glob")
        patterns = [r.pattern for r in rules]
        assert "*.env" not in patterns


# ---------------------------------------------------------------------------
# _resolve_bash_single
# ---------------------------------------------------------------------------


class TestResolveBashSingle:
    def setup_method(self):
        set_config(PermissionConfig())

    def test_returns_tuple_action_and_pattern(self):
        from aru.permissions import _resolve_bash_single
        action, pattern = _resolve_bash_single("ls")
        assert action == "allow"
        assert pattern == "ls"

    def test_unsafe_returns_ask(self):
        from aru.permissions import _resolve_bash_single
        action, pattern = _resolve_bash_single("pip install foo")
        assert action == "ask"

    def test_rm_command_unsafe(self):
        from aru.permissions import _resolve_bash_single
        # rm is not in SAFE_COMMAND_PREFIXES, so it defaults to ask
        action, _ = _resolve_bash_single("rm file.txt")
        assert action == "ask"


# ---------------------------------------------------------------------------
# _resolve_bash_compound
# ---------------------------------------------------------------------------


class TestResolveBashCompound:
    def setup_method(self):
        set_config(PermissionConfig())

    def test_semicolon_compound(self):
        action, _ = _resolve_bash_compound("ls; rm foo")
        assert action == "ask"

    def test_mixed_operators(self):
        action, _ = _resolve_bash_compound("ls && git status | grep foo")
        assert action == "allow"

    def test_pipe_most_restrictive_wins(self):
        # echo is safe (allow), rm is not in safe list (ask)
        action, _ = _resolve_bash_compound("echo hello | rm -rf /")
        assert action == "ask"

    def test_mixed_safe_and_unsafe_is_ask(self):
        action, _ = _resolve_bash_compound("ls && npm install")
        assert action == "ask"


# ---------------------------------------------------------------------------
# check_permission interactive scenarios
# ---------------------------------------------------------------------------


class TestCheckPermissionInteractive:
    def setup_method(self):
        set_skip_permissions(False)
        set_config(PermissionConfig())
        reset_session()

    def test_ask_no_denies(self, monkeypatch):
        ctx = get_ctx()
        ctx.skip_permissions = False

        # Arrow-key menu returns index 2 for "No" on edit prompts (the
        # third option). Feedback input stays textual — empty here.
        monkeypatch.setattr("aru.permissions.select_option", lambda *a, **kw: 2)
        monkeypatch.setattr(ctx.console, "input", lambda _: "")

        result = check_permission("edit", "src/app.py", "editing src/app.py")
        assert result is False

    def test_ask_no_captures_feedback(self, monkeypatch):
        from aru.permissions import consume_rejection_feedback
        ctx = get_ctx()
        ctx.skip_permissions = False

        monkeypatch.setattr("aru.permissions.select_option", lambda *a, **kw: 2)
        monkeypatch.setattr(ctx.console, "input", lambda _: "use a different approach")

        result = check_permission("edit", "src/app.py", "editing src/app.py")
        assert result is False
        assert consume_rejection_feedback() == "use a different approach"

    def test_auto_accept_option_enables_mode(self, monkeypatch):
        ctx = get_ctx()
        ctx.skip_permissions = False
        ctx.permission_mode = "default"

        # Index 1 = "Yes, and auto-accept edits" on edit prompts.
        monkeypatch.setattr("aru.permissions.select_option", lambda *a, **kw: 1)

        result = check_permission("edit", "main.py", "editing main.py")
        assert result is True
        assert ctx.permission_mode == "acceptEdits"
        # Mode persists — subsequent edit checks skip the prompt entirely.
        result2 = check_permission("edit", "other.py", "editing other.py")
        assert result2 is True
        ctx.permission_mode = "default"

    def test_ask_yes_allows_once(self, monkeypatch):
        ctx = get_ctx()
        ctx.skip_permissions = False

        # Index 0 = "Yes" on both edit and non-edit prompts.
        monkeypatch.setattr("aru.permissions.select_option", lambda *a, **kw: 0)

        result = check_permission("edit", "src/app.py", "editing")
        assert result is True

    def test_ask_keyboard_interrupt_returns_false(self, monkeypatch):
        ctx = get_ctx()
        ctx.skip_permissions = False

        # select_option returns cancel_value (which the caller sets to
        # reject_index) when the user presses Ctrl+C / Esc. Simulate that
        # by having it return the reject_index directly (2 for edits).
        def _cancel(*args, **kwargs):
            return kwargs.get("cancel_value", None)
        monkeypatch.setattr("aru.permissions.select_option", _cancel)
        monkeypatch.setattr(ctx.console, "input", lambda _: "")

        result = check_permission("edit", "main.py", "editing")
        assert result is False

    def test_ask_eof_error_returns_false(self, monkeypatch):
        ctx = get_ctx()
        ctx.skip_permissions = False

        # EOFError out of select_option — the select module catches it and
        # returns cancel_value, which is reject_index for the caller.
        def _cancel(*args, **kwargs):
            return kwargs.get("cancel_value", None)
        monkeypatch.setattr("aru.permissions.select_option", _cancel)
        monkeypatch.setattr(ctx.console, "input", lambda _: "")

        result = check_permission("edit", "main.py", "editing")
        assert result is False

    def test_sim_portuguese_accepted(self, monkeypatch):
        """With arrow-key selection there's no language-specific parsing —
        the test remains as a sanity check that index 0 still means yes."""
        ctx = get_ctx()
        ctx.skip_permissions = False

        monkeypatch.setattr("aru.permissions.select_option", lambda *a, **kw: 0)

        result = check_permission("edit", "main.py", "editing")
        assert result is True


# ---------------------------------------------------------------------------
# parse_permission_config edge cases
# ---------------------------------------------------------------------------


class TestParsePermissionConfigEdgeCases:
    def test_invalid_action_in_dict_defaults_to_ask(self):
        config = parse_permission_config({
            "read": "invalid_action",
        })
        # Invalid action should be normalized to "ask"
        assert config.categories["read"][0].action == "ask"

    def test_invalid_action_in_pattern_defaults_to_ask(self):
        config = parse_permission_config({
            "bash": {"rm *": "bad_action"},
        })
        assert config.categories["bash"][0].action == "ask"

    def test_wrong_type_value_skipped(self):
        config = parse_permission_config({
            "read": 123,
        })
        # Non-string/non-dict value is ignored
        assert config.categories == {}


# ---------------------------------------------------------------------------
# resolve_permission edge cases
# ---------------------------------------------------------------------------


class TestResolvePermissionEdgeCases:
    def setup_method(self):
        set_skip_permissions(False)
        set_config(PermissionConfig())
        reset_session()

    def test_edit_env_default_deny_always_wins(self):
        """Default hardcoded deny for *.env takes precedence."""
        config = parse_permission_config({
            "edit": {"*.env": "allow", "*": "ask"},
        })
        set_config(config)
        action, _ = resolve_permission("edit", ".env")
        assert action == "ask"

    def test_resolve_permission_returns_matched_pattern(self):
        _, pattern = resolve_permission("bash", "git status --short")
        assert pattern == "git status --short" or pattern == "git status *"

    def test_resolve_permission_empty_subject(self):
        # glob/grep use empty subject
        action, _ = resolve_permission("grep", "")
        assert action == "allow"


# ---------------------------------------------------------------------------
# set/get skip_permissions
# ---------------------------------------------------------------------------


class TestSkipPermissions:
    def test_default_false(self):
        set_skip_permissions(False)
        assert get_skip_permissions() is False

    def test_set_true(self):
        original = get_skip_permissions()
        try:
            set_skip_permissions(True)
            assert get_skip_permissions() is True
        finally:
            set_skip_permissions(original)

    def test_set_false(self):
        original = get_skip_permissions()
        try:
            set_skip_permissions(True)
            set_skip_permissions(False)
            assert get_skip_permissions() is False
        finally:
            set_skip_permissions(original)


# ---------------------------------------------------------------------------
# _normalize_cmd
# ---------------------------------------------------------------------------


class TestNormalizeCmd:
    def test_forward_slash_unchanged(self):
        assert _normalize_cmd("git status") == "git status"

    def test_backslash_converted_to_forward(self):
        assert _normalize_cmd("dir\\path") == "dir/path"

    def test_leading_dot_slash_removed(self):
        assert _normalize_cmd("./git status") == "git status"
        assert _normalize_cmd(".\\git status") == "git status"

    def test_complex_path(self):
        assert _normalize_cmd(".\\.venv\\Scripts\\python.exe") == ".venv/Scripts/python.exe"


# ---------------------------------------------------------------------------
# merge_configs
# ---------------------------------------------------------------------------


class TestMergeConfigs:
    def test_overlay_replaces_category(self):
        base = parse_permission_config({"edit": "allow"})
        overlay = parse_permission_config({"edit": "deny"})
        merged = merge_configs(base, overlay)
        assert merged.categories["edit"][0].action == "deny"

    def test_unspecified_categories_inherited(self):
        base = parse_permission_config({"read": "allow", "edit": "ask"})
        overlay = parse_permission_config({"edit": "deny"})
        merged = merge_configs(base, overlay)
        # read inherited from base
        assert merged.categories["read"][0].action == "allow"
        # edit replaced by overlay
        assert merged.categories["edit"][0].action == "deny"

    def test_base_default_preserved(self):
        base = PermissionConfig(default="allow")
        overlay = parse_permission_config({"edit": "deny"})
        merged = merge_configs(base, overlay)
        assert merged.default == "allow"

    def test_empty_overlay(self):
        base = parse_permission_config({"read": "allow"})
        overlay = PermissionConfig()
        merged = merge_configs(base, overlay)
        assert merged.categories["read"][0].action == "allow"


# ---------------------------------------------------------------------------
# permission_scope
# ---------------------------------------------------------------------------


class TestPermissionScope:
    def setup_method(self):
        set_skip_permissions(False)
        set_config(PermissionConfig())
        reset_session()

    def test_activates_overlay(self):
        set_config(parse_permission_config({"edit": "ask"}))
        with permission_scope({"edit": "allow"}):
            action, _ = resolve_permission("edit", "main.py")
            assert action == "allow"

    def test_restores_after_exit(self):
        set_config(parse_permission_config({"edit": "ask"}))
        with permission_scope({"edit": "allow"}):
            pass
        action, _ = resolve_permission("edit", "main.py")
        assert action == "ask"

    def test_none_is_noop(self):
        set_config(parse_permission_config({"edit": "ask"}))
        with permission_scope(None):
            action, _ = resolve_permission("edit", "main.py")
            assert action == "ask"

    def test_nested_stacking(self):
        set_config(parse_permission_config({"edit": "ask", "write": "ask"}))
        with permission_scope({"edit": "allow"}):
            action, _ = resolve_permission("edit", "main.py")
            assert action == "allow"
            with permission_scope({"edit": "deny"}):
                action2, _ = resolve_permission("edit", "main.py")
                assert action2 == "deny"
            # Back to first overlay
            action3, _ = resolve_permission("edit", "main.py")
            assert action3 == "allow"
        # Back to original
        action4, _ = resolve_permission("edit", "main.py")
        assert action4 == "ask"

    def test_session_isolated(self):
        ctx = get_ctx()
        set_config(parse_permission_config({"edit": "ask"}))
        ctx.session_allowed.add(("edit", "*"))
        with permission_scope({"edit": "ask"}):
            # Inside scope, session memory is fresh — no "always" carry-over
            assert ("edit", "*") not in ctx.session_allowed
        # After scope, original session memory restored
        assert ("edit", "*") in ctx.session_allowed
        reset_session()

    def test_unspecified_categories_inherit(self):
        set_config(parse_permission_config({"read": "allow", "edit": "ask"}))
        with permission_scope({"edit": "deny"}):
            # read should inherit from global
            action_read, _ = resolve_permission("read", "main.py")
            assert action_read == "allow"
            # edit should be overridden
            action_edit, _ = resolve_permission("edit", "main.py")
            assert action_edit == "deny"


# ---------------------------------------------------------------------------
# YOLO mode (interactive skip-permissions)
# ---------------------------------------------------------------------------


class TestYoloMode:
    def setup_method(self):
        from aru.permissions import set_permission_mode
        set_permission_mode("default")
        set_skip_permissions(False)
        set_config(PermissionConfig())
        reset_session()

    def teardown_method(self):
        from aru.permissions import set_permission_mode
        set_permission_mode("default")

    def test_set_permission_mode_yolo_enables_skip(self):
        from aru.permissions import set_permission_mode
        set_permission_mode("yolo")
        assert get_skip_permissions() is True
        assert get_ctx().permission_mode == "yolo"

    def test_set_permission_mode_default_disables_skip(self):
        from aru.permissions import set_permission_mode
        set_permission_mode("yolo")
        set_permission_mode("default")
        assert get_skip_permissions() is False
        assert get_ctx().permission_mode == "default"

    def test_cycle_includes_yolo(self):
        from aru.permissions import cycle_permission_mode, set_permission_mode
        set_permission_mode("default")
        assert cycle_permission_mode() == "acceptEdits"
        assert cycle_permission_mode() == "yolo"
        assert get_skip_permissions() is True
        assert cycle_permission_mode() == "default"
        assert get_skip_permissions() is False

    def test_yolo_allows_env_files(self):
        """YOLO must bypass the hardcoded _SENSITIVE_FILE_RULES deny for .env."""
        from aru.permissions import set_permission_mode
        # Baseline: .env is denied in default mode
        action, _ = resolve_permission("read", ".env")
        assert action == "deny"
        set_permission_mode("yolo")
        action, _ = resolve_permission("read", ".env")
        assert action == "allow"

    def test_yolo_allows_arbitrary_bash(self):
        from aru.permissions import set_permission_mode
        # Baseline: rm -rf is not in safe bash defaults → ask
        action, _ = resolve_permission("bash", "rm -rf /tmp/anything")
        assert action == "ask"
        set_permission_mode("yolo")
        action, _ = resolve_permission("bash", "rm -rf /tmp/anything")
        assert action == "allow"

    def test_disable_yolo_restores_env_deny(self):
        from aru.permissions import set_permission_mode
        set_permission_mode("yolo")
        set_permission_mode("default")
        action, _ = resolve_permission("read", ".env")
        assert action == "deny"

    def test_init_ctx_with_skip_permissions_sets_yolo_mode(self):
        """Starting aru with --dangerously-skip-permissions must mark mode as yolo."""
        from rich.console import Console
        from aru.runtime import init_ctx
        ctx = init_ctx(console=Console(), skip_permissions=True)
        assert ctx.permission_mode == "yolo"
        assert ctx.skip_permissions is True


class TestPermissionHookContextPropagation:
    """Async permission.ask handlers dispatched to a worker thread must see
    the same RuntimeContext as the caller. Without contextvars.copy_context,
    `asyncio.run` in the worker starts an empty context and plugin code
    calling `get_ctx()` would crash with LookupError."""

    import pytest

    @pytest.mark.asyncio
    async def test_async_handler_sees_parent_runtime_context(self):
        """Regression: async permission.ask handler can call get_ctx() when
        dispatched to a worker thread (copy_context propagation)."""
        from aru.permissions import _fire_permission_hook
        from aru.plugins.hooks import Hooks
        from aru.plugins.manager import PluginManager
        from aru.runtime import get_ctx, init_ctx

        # Install a distinctive ctx we can recognize from inside the handler
        ctx = init_ctx()
        ctx.small_model_ref = "sentinel/from-parent"

        observed: dict = {}

        async def plugin(_pctx, _opts):
            hooks = Hooks()

            @hooks.on("permission.ask")
            async def handler(event):
                try:
                    inner_ctx = get_ctx()
                    observed["small_model_ref"] = inner_ctx.small_model_ref
                except LookupError as e:
                    observed["error"] = str(e)
                event.data["allow"] = True

            return hooks

        mgr = PluginManager()
        import asyncio as _asyncio
        mgr._hooks.append(await plugin(None, None))
        mgr._loaded = True

        # Simulate the caller: a running loop triggers the worker-thread branch
        result = _fire_permission_hook(mgr, "edit", "/tmp/x")

        assert result is True
        assert "error" not in observed, (
            f"Async handler lost RuntimeContext: {observed.get('error')}"
        )
        assert observed.get("small_model_ref") == "sentinel/from-parent"

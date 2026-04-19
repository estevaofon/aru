"""Tests for the granular permission system."""

import pytest

from aru.permissions import (
    PermissionAction,
    PermissionConfig,
    PermissionRule,
    Rule,
    Ruleset,
    TOOL_PERMISSION_NAMES,
    _build_rules,
    _expand_pattern,
    _match_bash_rule,
    _match_rule,
    _most_restrictive,
    _normalize_cmd,
    _resolve_bash_compound,
    _shell_split,
    _wildcard_match,
    canonical_permission,
    check_permission,
    evaluate,
    from_config,
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


# ---------------------------------------------------------------------------
# Unified rule shape (OpenCode parity) — Rule / Ruleset / evaluate / from_config
# ---------------------------------------------------------------------------


class TestWildcardMatch:
    def test_star_matches_anything(self):
        assert _wildcard_match("edit", "*") is True
        assert _wildcard_match("", "*") is True

    def test_exact_match(self):
        assert _wildcard_match("edit", "edit") is True
        assert _wildcard_match("edit", "write") is False

    def test_glob_suffix(self):
        assert _wildcard_match("edit_file", "edit*") is True
        assert _wildcard_match("edit_files", "edit*") is True
        assert _wildcard_match("write_file", "edit*") is False

    def test_pattern_with_space(self):
        # bash-style patterns: "git *" should match "git status"
        assert _wildcard_match("git status", "git *") is True
        assert _wildcard_match("npm install", "git *") is False


class TestEvaluate:
    def test_no_rules_defaults_to_ask(self):
        rule = evaluate("edit", "main.py")
        assert rule.action == "ask"
        assert rule.permission == "edit"
        assert rule.pattern == "*"

    def test_single_ruleset_matching_rule(self):
        rs: Ruleset = [Rule("edit", "*", "allow")]
        rule = evaluate("edit", "main.py", rs)
        assert rule.action == "allow"
        assert rule.pattern == "*"

    def test_last_match_wins_within_ruleset(self):
        rs: Ruleset = [
            Rule("edit", "*", "allow"),
            Rule("edit", "*.secret", "deny"),
        ]
        rule = evaluate("edit", "keys.secret", rs)
        assert rule.action == "deny"

    def test_later_ruleset_overrides_earlier(self):
        # plan_mode ruleset after user ruleset → plan_mode wins when both match
        user_rules: Ruleset = [Rule("bash", "*", "allow")]
        plan_rules: Ruleset = [Rule("bash", "*", "deny")]
        rule = evaluate("bash", "ls", user_rules, plan_rules)
        assert rule.action == "deny"

    def test_wildcard_permission(self):
        rs: Ruleset = [Rule("edit*", "*", "ask")]
        rule = evaluate("edit_file", "main.py", rs)
        assert rule.action == "ask"
        rule2 = evaluate("edit_files", "main.py", rs)
        assert rule2.action == "ask"

    def test_universal_catchall(self):
        rs: Ruleset = [Rule("*", "*", "deny")]
        assert evaluate("anything", "else", rs).action == "deny"

    def test_non_matching_rule_ignored(self):
        rs: Ruleset = [Rule("edit", "*.env", "deny")]
        rule = evaluate("edit", "main.py", rs)
        # no match → default ask, not the deny rule
        assert rule.action == "ask"


class TestFromConfig:
    def test_none_returns_empty(self):
        assert from_config(None) == []
        assert from_config({}) == []

    def test_string_shorthand(self):
        rs = from_config("allow")
        assert rs == [Rule("*", "*", "allow")]

    def test_invalid_string_becomes_ask(self):
        rs = from_config("yolo")
        assert rs == [Rule("*", "*", "ask")]

    def test_key_string_value(self):
        rs = from_config({"read": "allow"})
        assert rs == [Rule("read", "*", "allow")]

    def test_key_dict_value_expands(self):
        rs = from_config({"bash": {"*": "ask", "git *": "allow"}})
        assert rs == [
            Rule("bash", "*", "ask"),
            Rule("bash", "git *", "allow"),
        ]

    def test_star_top_level_becomes_universal_rule(self):
        # OpenCode shape: {"*": "ask"} -> Rule("*", "*", "ask")
        rs = from_config({"*": "ask"})
        assert rs == [Rule("*", "*", "ask")]

    def test_opencode_task_shape(self):
        # Per-subagent permission (e.g. subagent_type as pattern)
        rs = from_config({
            "task": {"explorer": "allow", "custom_dangerous": "ask"},
        })
        assert rs == [
            Rule("task", "explorer", "allow"),
            Rule("task", "custom_dangerous", "ask"),
        ]

    def test_mixed_shape(self):
        rs = from_config({
            "*": "ask",
            "read": "allow",
            "bash": {"*": "ask", "git *": "allow"},
        })
        assert rs == [
            Rule("*", "*", "ask"),
            Rule("read", "*", "allow"),
            Rule("bash", "*", "ask"),
            Rule("bash", "git *", "allow"),
        ]

    def test_key_order_preserved(self):
        # Resolution relies on last-match-wins — input order = priority order
        rs = from_config({"edit": {"*": "allow", "*.env": "deny"}})
        assert [r.pattern for r in rs] == ["*", "*.env"]

    def test_invalid_action_becomes_ask(self):
        rs = from_config({"read": "bogus"})
        assert rs == [Rule("read", "*", "ask")]

    def test_non_dict_non_string_returns_empty(self):
        assert from_config(42) == []
        assert from_config([1, 2, 3]) == []


class TestEvaluateIntegration:
    """End-to-end: from_config -> evaluate, mirroring how future resolve_permission
    will compose rulesets once tool_policy is absorbed (Fase 2)."""

    def test_opencode_style_edit_rules(self):
        rs = from_config({"edit": {"*": "ask", "*.env": "deny"}})
        assert evaluate("edit", "main.py", rs).action == "ask"
        assert evaluate("edit", ".env", rs).action == "deny"

    def test_plan_mode_as_ruleset_overrides_user(self):
        """Preview of Fase 2: plan_mode gate is expressed as a Ruleset passed
        after user rules. Last match wins, so plan_mode denies mutating tools
        even when user has allowed them globally."""
        user = from_config({"bash": "allow"})
        plan_mode = [Rule("bash", "*", "deny")]
        rule = evaluate("bash", "ls", user, plan_mode)
        assert rule.action == "deny"

    def test_session_approved_as_ruleset_unlocks(self):
        """Preview of Fase 2: session 'always' approvals are a Ruleset
        appended after user/defaults. Persists allow for the session."""
        defaults = from_config({"edit": "ask"})
        session_approved = [Rule("edit", "*", "allow")]
        rule = evaluate("edit", "main.py", defaults, session_approved)
        assert rule.action == "allow"


# ---------------------------------------------------------------------------
# Canonical permission names (Fase 3)
# ---------------------------------------------------------------------------


class TestCanonicalPermission:
    def test_edit_family_maps_to_edit(self):
        assert canonical_permission("edit_file") == "edit"
        assert canonical_permission("edit_files") == "edit"

    def test_write_family_maps_to_write(self):
        assert canonical_permission("write_file") == "write"
        assert canonical_permission("write_files") == "write"

    def test_read_family_maps_to_read(self):
        assert canonical_permission("read_file") == "read"
        assert canonical_permission("read_files") == "read"

    def test_delegate_task_maps_to_task(self):
        """OpenCode parity: permission name is `task`, not `delegate_task`."""
        assert canonical_permission("delegate_task") == "task"

    def test_web_tools_single_word(self):
        """OpenCode uses `webfetch`/`websearch` without underscore."""
        assert canonical_permission("web_fetch") == "webfetch"
        assert canonical_permission("web_search") == "websearch"

    def test_bash_unchanged(self):
        assert canonical_permission("bash") == "bash"
        assert canonical_permission("run_command") == "bash"

    def test_unknown_tool_uses_own_name(self):
        """Custom tools without an explicit mapping become their own permission."""
        assert canonical_permission("my_custom_tool") == "my_custom_tool"

    def test_table_covers_all_core_tools(self):
        """Sanity: the mapping table should cover every tool exposed by registry.
        This test is informational — it lists the tools expected to have
        canonical mappings. Failing means a new tool was added without an
        entry."""
        expected_in_table = {
            "edit_file", "edit_files",
            "write_file", "write_files",
            "read_file", "read_files",
            "glob_search", "grep_search", "list_directory",
            "bash", "run_command",
            "web_fetch", "web_search",
            "delegate_task", "invoke_skill",
        }
        assert expected_in_table <= set(TOOL_PERMISSION_NAMES.keys())


class TestExpandPattern:
    def test_home_prefix_expanded(self):
        home = os.path.expanduser("~")
        assert _expand_pattern("~/project/.env") == home + "/project/.env"

    def test_tilde_alone(self):
        assert _expand_pattern("~") == os.path.expanduser("~")

    def test_dollar_home_prefix(self):
        home = os.path.expanduser("~")
        assert _expand_pattern("$HOME/logs") == home + "/logs"

    def test_dollar_home_alone(self):
        assert _expand_pattern("$HOME") == os.path.expanduser("~")

    def test_glob_unchanged(self):
        assert _expand_pattern("*.env") == "*.env"

    def test_absolute_path_unchanged(self):
        assert _expand_pattern("/etc/hosts") == "/etc/hosts"

    def test_empty_unchanged(self):
        assert _expand_pattern("") == ""


class TestFromConfigExpansion:
    """from_config must apply _expand_pattern so user rules with `~/`
    resolve correctly at parse time (OpenCode parity)."""

    def test_tilde_in_pattern_expanded(self):
        home = os.path.expanduser("~")
        rs = from_config({"read": {"~/secrets/*": "deny"}})
        assert rs == [Rule("read", home + "/secrets/*", "deny")]

    def test_dollar_home_in_pattern_expanded(self):
        home = os.path.expanduser("~")
        rs = from_config({"edit": {"$HOME/.ssh/*": "deny"}})
        assert rs == [Rule("edit", home + "/.ssh/*", "deny")]

    def test_non_home_pattern_unchanged(self):
        rs = from_config({"edit": {"*.env": "deny"}})
        assert rs == [Rule("edit", "*.env", "deny")]


class TestOpenCodeConfigPortability:
    """End-to-end: an OpenCode-style permission config block should parse
    to a valid Ruleset. Mirrors the shape users would copy from an
    opencode.json into their aru.json."""

    def test_task_permission_per_subagent(self):
        # Convention (same as OpenCode): catch-all first, specifics after.
        # last-match-wins picks the most-specific rule placed later.
        rs = from_config({
            "task": {"*": "ask", "explorer": "allow", "custom_dangerous": "ask"},
        })
        assert evaluate("task", "explorer", rs).action == "allow"
        assert evaluate("task", "custom_dangerous", rs).action == "ask"
        assert evaluate("task", "unknown_subagent", rs).action == "ask"

    def test_mixed_opencode_permissions(self):
        rs = from_config({
            "edit": {"*": "ask", "~/.ssh/*": "deny"},
            "bash": {"*": "ask", "git *": "allow"},
            "webfetch": "allow",
            "websearch": "allow",
            "task": {"*": "ask", "explorer": "allow"},
        })
        home = os.path.expanduser("~")
        # Edit with home expansion
        assert evaluate("edit", home + "/.ssh/config", rs).action == "deny"
        assert evaluate("edit", "main.py", rs).action == "ask"
        # Bash
        assert evaluate("bash", "git status", rs).action == "allow"
        assert evaluate("bash", "pip install x", rs).action == "ask"
        # Web
        assert evaluate("webfetch", "https://example.com", rs).action == "allow"
        # Task
        assert evaluate("task", "explorer", rs).action == "allow"


# Needed for os.path.expanduser calls in the Fase 3 tests above
import os  # noqa: E402


# ---------------------------------------------------------------------------
# Multi-pattern check_permission (Fase 4)
# ---------------------------------------------------------------------------


class TestCheckPermissionMultiPattern:
    """Batch tools (write_files/edit_files) pass a list of paths. Resolution
    is atomic: any deny → deny whole batch; all allow → pass; any ask → one
    prompt. Mirrors OpenCode's `ask({patterns: []})` semantics."""

    def setup_method(self):
        set_skip_permissions(False)
        set_config(PermissionConfig())
        reset_session()

    def test_list_all_allow_returns_true(self):
        # read defaults to allow; passing multiple readable paths should pass
        assert check_permission("read", ["a.md", "b.md", "c.md"], "reading batch") is True

    def test_list_any_deny_returns_false(self):
        # .env is denied by default; mixing it in a batch denies the whole call
        assert check_permission("read", ["a.md", ".env", "c.md"], "reading batch") is False

    def test_list_all_deny_returns_false(self):
        assert check_permission("read", [".env", ".env.local"], "reading") is False

    def test_empty_list_treated_as_empty_subject(self):
        # read of empty subject defaults to allow (category default)
        assert check_permission("read", [], "nothing") is True

    def test_single_path_list_equivalent_to_string(self):
        # write of main.py → ask → with monkeypatched select returning "Yes"
        # via separate test. Here we verify the deny path: .env write should
        # deny for both forms.
        assert check_permission("write", ".env", "x") is False
        assert check_permission("write", [".env"], "x") is False

    def test_list_any_ask_prompts_once(self, monkeypatch):
        ctx = get_ctx()
        ctx.skip_permissions = False
        call_count = {"n": 0}

        def _fake_select(*args, **kwargs):
            call_count["n"] += 1
            return 0  # "Yes"

        monkeypatch.setattr("aru.permissions.select_option", _fake_select)

        # Two "ask" subjects (write category defaults to ask) → single prompt
        result = check_permission("write", ["a.py", "b.py", "c.py"], "batch write")
        assert result is True
        assert call_count["n"] == 1  # prompted exactly once, not 3 times

    def test_list_deny_skips_prompt(self, monkeypatch):
        """If any subject denies, prompt must not fire — the user shouldn't
        be asked to approve a batch that includes a hard-deny file."""
        ctx = get_ctx()
        ctx.skip_permissions = False
        prompted = {"fired": False}

        def _should_not_fire(*args, **kwargs):
            prompted["fired"] = True
            return 0

        monkeypatch.setattr("aru.permissions.select_option", _should_not_fire)

        result = check_permission("write", ["a.py", ".env"], "batch")
        assert result is False
        assert prompted["fired"] is False


# ---------------------------------------------------------------------------
# Typed permission errors (Fase 5)
# ---------------------------------------------------------------------------


class TestTypedPermissionErrors:
    def test_permission_denied_carries_context(self):
        from aru.permissions import PermissionDenied
        err = PermissionDenied("edit", ".env", "*.env")
        assert err.category == "edit"
        assert err.subject == ".env"
        assert err.pattern == "*.env"
        assert "edit" in str(err) and ".env" in str(err)

    def test_permission_rejected_has_no_feedback(self):
        from aru.permissions import PermissionRejected
        err = PermissionRejected("bash", "rm -rf /")
        assert err.category == "bash"
        assert err.subject == "rm -rf /"
        assert not hasattr(err, "feedback")

    def test_permission_corrected_carries_feedback(self):
        from aru.permissions import PermissionCorrected
        err = PermissionCorrected("edit", "main.py", "use edit_files for batching")
        assert err.category == "edit"
        assert err.subject == "main.py"
        assert err.feedback == "use edit_files for batching"
        assert "feedback" in str(err)

    def test_all_inherit_from_exception(self):
        from aru.permissions import (
            PermissionCorrected,
            PermissionDenied,
            PermissionRejected,
        )
        assert issubclass(PermissionDenied, Exception)
        assert issubclass(PermissionRejected, Exception)
        assert issubclass(PermissionCorrected, Exception)

    def test_errors_are_distinct_types(self):
        """Callers must be able to branch on type — opencode parity
        (index.ts:83-103 uses three separate tagged error classes)."""
        from aru.permissions import (
            PermissionCorrected,
            PermissionDenied,
            PermissionRejected,
        )
        assert PermissionDenied is not PermissionRejected
        assert PermissionRejected is not PermissionCorrected
        assert PermissionDenied is not PermissionCorrected


# ---------------------------------------------------------------------------
# disabled(tools, ruleset) helper (Fase 6)
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_universal_deny_disables_tool(self):
        from aru.permissions import disabled
        rs = from_config({"bash": "deny"})
        assert disabled(["bash"], rs) == {"bash"}

    def test_subject_specific_deny_does_not_disable(self):
        """{"bash": {"rm -rf *": "deny"}} is a specific call deny, not a
        universal tool disable. The tool stays in the toolset."""
        from aru.permissions import disabled
        rs = from_config({"bash": {"rm -rf *": "deny"}})
        assert disabled(["bash"], rs) == set()

    def test_canonical_name_covers_tool_family(self):
        """{"edit": "deny"} disables both edit_file and edit_files, because
        canonical_permission maps both tools to "edit"."""
        from aru.permissions import disabled
        rs = from_config({"edit": "deny"})
        assert disabled(["edit_file", "edit_files"], rs) == {"edit_file", "edit_files"}

    def test_task_permission_disables_delegate_task(self):
        """OpenCode-style {"task": "deny"} disables the delegate_task tool."""
        from aru.permissions import disabled
        rs = from_config({"task": "deny"})
        assert disabled(["delegate_task"], rs) == {"delegate_task"}

    def test_later_allow_unblocks(self):
        """A later rule can override an earlier universal deny."""
        from aru.permissions import disabled
        # Order matters — later rule wins
        rs = [Rule("bash", "*", "deny"), Rule("bash", "*", "allow")]
        assert disabled(["bash"], rs) == set()

    def test_last_match_across_rulesets(self):
        """disabled considers all rulesets flattened in order — identical
        to evaluate()'s composition."""
        from aru.permissions import disabled
        user = from_config({"bash": "deny"})
        # Later ruleset overrides
        override = [Rule("bash", "*", "allow")]
        assert disabled(["bash"], user, override) == set()

    def test_unknown_tool_passes_through(self):
        """A tool with no matching rule is not disabled."""
        from aru.permissions import disabled
        rs = from_config({"bash": "deny"})
        assert disabled(["my_custom_tool"], rs) == set()

    def test_empty_ruleset_disables_nothing(self):
        from aru.permissions import disabled
        assert disabled(["edit_file", "bash", "delegate_task"]) == set()

    def test_multiple_tools_mixed_states(self):
        """Combine canonical mapping + some denies + some allows."""
        from aru.permissions import disabled
        rs = from_config({
            "edit": "deny",       # disables edit_file, edit_files
            "task": "deny",       # disables delegate_task
            "bash": "ask",        # does NOT disable (ask ≠ deny)
            "read": "allow",      # does NOT disable
        })
        tools = ["edit_file", "edit_files", "bash", "delegate_task", "read_file"]
        assert disabled(tools, rs) == {"edit_file", "edit_files", "delegate_task"}

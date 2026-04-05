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
    _resolve_bash_compound,
    _session_allowed,
    _shell_split,
    check_permission,
    get_skip_permissions,
    parse_permission_config,
    reset_session,
    resolve_permission,
    set_config,
    set_skip_permissions,
)


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
        _session_allowed.add(("edit", "*"))
        action, _ = resolve_permission("edit", "main.py")
        assert action == "allow"
        reset_session()

    def test_session_allowed_pattern(self):
        _session_allowed.add(("bash", "npm *"))
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
        set_skip_permissions(True)
        try:
            set_skip_permissions(False)
            assert get_skip_permissions() is False
        finally:
            set_skip_permissions(False)

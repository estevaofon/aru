"""Tests for the recent-models MRU (``aru.state``) and its integration
with ``/connect`` model selection and ``/connect logout``.

Verifies the OpenCode-parity behaviour the user asked for: after a
``/connect`` (or any explicit model switch), the next launch of aru
auto-loads that model instead of falling back to the built-in default.
"""

from __future__ import annotations

import json
import time

import pytest

from aru import auth, commands, providers, state
from aru.session import Session


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Temp credential + state file + provider-registry restore."""
    monkeypatch.setattr(auth, "auth_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "state.json")
    snapshot = {
        k: (p.api_key, p.base_url, p.default_model, p.codex_oauth)
        for k, p in providers._providers.items()
    }
    original = set(providers._providers.keys())
    yield
    for k in list(providers._providers.keys()):
        if k not in original:
            del providers._providers[k]
    for k, (api_key, base_url, default_model, codex) in snapshot.items():
        p = providers._providers.get(k)
        if p is not None:
            p.api_key = api_key
            p.base_url = base_url
            p.default_model = default_model
            p.codex_oauth = codex


class _StubUI:
    """Minimal UIAdapter — copy of test_connect_command.StubUI."""

    def __init__(self, *, choices=None, texts=None):
        self._choices = list(choices or [])
        self._texts = list(texts or [])
        self.prints: list[str] = []
        self.notifications: list[tuple[str, str]] = []

    def ask_choice(self, options, *, title=None, default=0, cancel_value=None, details=None):
        return self._choices.pop(0)

    def ask_text(self, prompt, *, default="", multiline=False, password=False):
        return self._texts.pop(0)

    def confirm(self, prompt, default=False):
        return True

    def print(self, renderable):
        self.prints.append(str(renderable))

    def notify(self, message, severity="info"):
        self.notifications.append((severity, str(message)))


def _use_ui(monkeypatch, ui):
    monkeypatch.setattr(commands, "_resolve_connect_ui", lambda: ui)


# ── pure state.py behaviour ──────────────────────────────────────────────


def test_recent_list_starts_empty():
    assert state.get_recent_models() == []
    assert state.get_last_model() is None


def test_record_model_is_mru_dedup_capped():
    state.record_model("anthropic/claude-sonnet-4-5")
    state.record_model("openai/gpt-5.4")
    state.record_model("anthropic/claude-sonnet-4-5")  # bumps to the front
    assert state.get_recent_models() == [
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-5.4",
    ]


def test_record_model_caps_at_max_recent():
    # MAX_RECENT defaults to 10; record 12 and confirm the older two fall off.
    for i in range(12):
        state.record_model(f"openai/m-{i}")
    recent = state.get_recent_models()
    assert len(recent) == state.MAX_RECENT
    # Most recent is the last inserted.
    assert recent[0] == "openai/m-11"
    # The two oldest got evicted.
    assert "openai/m-0" not in recent
    assert "openai/m-1" not in recent


def test_record_model_ignores_empty_or_nonstring():
    state.record_model("anthropic/claude-sonnet-4-5")
    state.record_model("")
    state.record_model("   ")
    state.record_model(None)  # type: ignore[arg-type]
    assert state.get_recent_models() == ["anthropic/claude-sonnet-4-5"]


def test_get_last_model_skips_unknown_provider():
    # Prepend a stale entry whose provider is no longer registered.
    state.record_model("anthropic/claude-sonnet-4-5")
    state.record_model("ghostprov/some-model")
    # We just wrote MRU = ["ghostprov/some-model", "anthropic/..."]. The first
    # is unknown → fall through to the anthropic entry.
    assert state.get_last_model() == "anthropic/claude-sonnet-4-5"


def test_get_last_model_returns_none_when_all_unknown():
    state.record_model("ghostprov-1/a")
    state.record_model("ghostprov-2/b")
    assert state.get_last_model() is None


def test_forget_provider_drops_all_entries_for_provider():
    state.record_model("openai/gpt-5.4")
    state.record_model("openai/gpt-4o")
    state.record_model("anthropic/claude-sonnet-4-5")
    state.forget_provider("openai")
    assert state.get_recent_models() == ["anthropic/claude-sonnet-4-5"]


def test_forget_model_drops_a_single_entry():
    state.record_model("a/1")
    state.record_model("a/2")
    state.forget_model("a/1")
    assert state.get_recent_models() == ["a/2"]


def test_state_file_is_resilient_to_garbage():
    # Manually corrupt the file; load_state / get_recent_models should not raise.
    state.state_path().parent.mkdir(parents=True, exist_ok=True)
    state.state_path().write_text("{ not json", encoding="utf-8")
    assert state.load_state() == {}
    assert state.get_recent_models() == []


def test_state_file_written_with_pretty_indent():
    """Sanity: the JSON on disk is human-inspectable, not a one-liner."""
    state.record_model("openai/gpt-5.4")
    raw = state.state_path().read_text(encoding="utf-8")
    # indent=2 → at least one newline.
    assert "\n" in raw
    assert "recent_models" in json.loads(raw)


# ── /connect → record_model integration ─────────────────────────────────


def test_connect_via_menu_records_chosen_model(monkeypatch):
    """A successful /connect run should bump the model into recent_models."""
    models = commands._list_provider_models(providers.get_provider("anthropic"))
    ui = _StubUI(choices=[0], texts=["sk-abc-12345"])  # anthropic preselect → model idx 0
    _use_ui(monkeypatch, ui)
    session = Session()

    commands.handle_connect_command("anthropic", session=session)

    assert state.get_recent_models() == [f"anthropic/{models[0]}"]
    assert state.get_last_model() == f"anthropic/{models[0]}"


def test_connect_logout_clears_provider_from_recent(monkeypatch):
    """Disconnecting a provider must also drop it from the MRU list."""
    auth.set_credential("openai", {"type": "api", "key": "sk-x"})
    state.record_model("openai/gpt-5.4")
    state.record_model("anthropic/claude-sonnet-4-5")
    ui = _StubUI()
    _use_ui(monkeypatch, ui)

    commands.handle_connect_command("logout openai", session=None)

    assert state.get_recent_models() == ["anthropic/claude-sonnet-4-5"]


def test_connect_oauth_records_chosen_model(monkeypatch):
    """The Codex (OAuth) branch should record the picked GPT-5 model too."""
    from aru import codex_oauth

    # Stub the OAuth flow so we don't open a browser or hit the network.
    rec: dict = {}

    class _FakeFlow:
        authorize_url = "https://auth.openai.com/oauth/authorize?fake=1"
        pkce = codex_oauth.PkceCodes(verifier="v", challenge="c")
        state = "s"

    monkeypatch.setattr(codex_oauth, "start_codex_oauth_flow", lambda: _FakeFlow())
    monkeypatch.setattr(
        codex_oauth,
        "await_codex_callback",
        lambda flow, timeout=300: {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_in": 3600,
            "id_token": None,
        },
    )
    monkeypatch.setattr(codex_oauth, "stop_codex_oauth_flow", lambda flow: None)
    monkeypatch.setattr("webbrowser.open", lambda url, new=0, **kw: rec.setdefault("opened", url))

    # provider menu: openai (idx 1); OAuth/API menu: 0 (OAuth); model menu: 0
    ui = _StubUI(choices=[1, 0, 0])
    _use_ui(monkeypatch, ui)

    commands.handle_connect_command("", session=Session())

    recent = state.get_recent_models()
    assert recent, "OAuth /connect should have recorded a model"
    assert recent[0].startswith("openai/gpt-5")


def test_connect_custom_provider_records_typed_default_model(monkeypatch):
    other_index = len(commands._provider_menu())
    ui = _StubUI(
        choices=[other_index],
        texts=[
            "myprov",
            "https://api.example.com/v1",
            "My Provider",
            "my-model",
            "sk-custom-9999",
        ],
    )
    _use_ui(monkeypatch, ui)

    commands.handle_connect_command("", session=Session())
    assert state.get_last_model() == "myprov/my-model"

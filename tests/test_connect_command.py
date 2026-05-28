"""Tests for the ``/connect`` handler (``aru.commands.handle_connect_command``).

The handler is UI-agnostic — it talks only through ``ctx.ui``. We swap in a
scripted ``StubUI`` so the full flow (provider menu → key entry → model
selection → store, plus ``list`` / ``logout``) can be exercised without a
terminal or Textual app.
"""

from __future__ import annotations

import pytest

from aru import auth, commands, providers
from aru.session import Session


class StubUI:
    """Scripted UIAdapter: pops queued answers, records output."""

    def __init__(self, *, choices=None, texts=None, confirms=None):
        self._choices = list(choices or [])
        self._texts = list(texts or [])
        self._confirms = list(confirms or [])
        self.prints: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.password_prompts: list[str] = []
        self.choice_options: list[list[str]] = []  # options shown per ask_choice

    def ask_choice(self, options, *, title=None, default=0, cancel_value=None, details=None):
        self.choice_options.append(list(options))
        return self._choices.pop(0)

    def ask_text(self, prompt, *, default="", multiline=False, password=False):
        if password:
            self.password_prompts.append(prompt)
        return self._texts.pop(0)

    def confirm(self, prompt, default=False):
        return self._confirms.pop(0)

    def print(self, renderable):
        self.prints.append(str(renderable))

    def notify(self, message, severity="info"):
        self.notifications.append((severity, str(message)))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Temp credential file + provider-registry restore (see test_auth_store)."""
    monkeypatch.setattr(auth, "auth_path", lambda: tmp_path / "auth.json")
    snapshot = {
        k: (p.api_key, p.base_url, p.default_model)
        for k, p in providers._providers.items()
    }
    original_keys = set(providers._providers.keys())
    yield
    for k in list(providers._providers.keys()):
        if k not in original_keys:
            del providers._providers[k]
    for k, (api_key, base_url, default_model) in snapshot.items():
        p = providers._providers.get(k)
        if p is not None:
            p.api_key, p.base_url, p.default_model = api_key, base_url, default_model


def _use_ui(monkeypatch, ui):
    monkeypatch.setattr(commands, "_resolve_connect_ui", lambda: ui)


def _anthropic_models():
    return commands._list_provider_models(providers.get_provider("anthropic"))


# ── login ────────────────────────────────────────────────────────────────


def test_connect_via_menu_stores_key_then_keeps_model(monkeypatch):
    # Provider menu index 0 == anthropic; Esc (None) on the model menu keeps
    # the current model.
    ui = StubUI(choices=[0, None], texts=["sk-test-123456"])
    _use_ui(monkeypatch, ui)
    session = Session()
    original_ref = session.model_ref

    result = commands.handle_connect_command("", session=session)

    assert result is None  # model unchanged
    assert session.model_ref == original_ref
    assert auth.get_credential("anthropic") == {"type": "api", "key": "sk-test-123456"}
    assert providers.get_provider("anthropic").api_key == "sk-test-123456"
    assert ui.password_prompts  # key requested masked


def test_connect_selects_model_from_menu(monkeypatch):
    # preselect anthropic → key → pick the first model from the menu.
    models = _anthropic_models()
    ui = StubUI(choices=[0], texts=["sk-abc-12345"])
    _use_ui(monkeypatch, ui)
    session = Session()

    result = commands.handle_connect_command("anthropic", session=session)

    expected = f"anthropic/{models[0]}"
    assert result == expected
    assert session.model_ref == expected
    # The menu actually offered the provider's models + a manual escape hatch.
    offered = ui.choice_options[0]
    assert models[0] in offered
    assert commands._CUSTOM_MODEL_LABEL in offered


def test_connect_model_menu_manual_entry(monkeypatch):
    # Choosing the "manual id" escape hatch prompts for a free-text model id.
    models = _anthropic_models()
    custom_idx = len(models)
    ui = StubUI(choices=[custom_idx], texts=["sk-x", "claude-some-future-model"])
    _use_ui(monkeypatch, ui)
    session = Session()

    result = commands.handle_connect_command("anthropic", session=session)

    assert result == "anthropic/claude-some-future-model"
    assert session.model_ref == "anthropic/claude-some-future-model"


def test_connect_empty_key_cancels(monkeypatch):
    # /connect openai now opens an OAuth-vs-key picker first (idx 1 == API
    # key). Then the empty key string cancels the flow.
    ui = StubUI(choices=[1], texts=[""])
    _use_ui(monkeypatch, ui)

    result = commands.handle_connect_command("openai", session=Session())

    assert result is None
    assert auth.get_credential("openai") is None
    assert any(sev == "warn" for sev, _ in ui.notifications)


def test_connect_unknown_provider_preselect_warns(monkeypatch):
    ui = StubUI()
    _use_ui(monkeypatch, ui)

    result = commands.handle_connect_command("not-a-provider", session=Session())

    assert result is None
    assert any(sev == "warn" for sev, _ in ui.notifications)


def test_connect_keyless_provider_stores_base_url_and_model(monkeypatch):
    # Ollama has no api_key_env → base URL instead of a key, then a free-text
    # model id (no static model registry).
    ui = StubUI(texts=["http://localhost:11434", "llama3.1"])
    _use_ui(monkeypatch, ui)
    session = Session()

    commands.handle_connect_command("ollama", session=session)

    assert auth.get_credential("ollama") == {
        "type": "local",
        "base_url": "http://localhost:11434",
    }
    assert session.model_ref == "ollama/llama3.1"
    assert not ui.password_prompts  # never asked for a key


def test_connect_custom_provider_uses_typed_model(monkeypatch):
    # "Other" is the last provider-menu entry; the typed default model becomes
    # the session model without re-prompting.
    other_index = len(commands._provider_menu())
    ui = StubUI(
        choices=[other_index],
        texts=[
            "myprov",                          # provider id
            "https://api.example.com/v1",      # base url
            "My Provider",                     # display name
            "my-model",                        # default model
            "sk-custom-9999",                  # api key
        ],
    )
    _use_ui(monkeypatch, ui)
    session = Session()

    result = commands.handle_connect_command("", session=session)

    cred = auth.get_credential("myprov")
    assert cred["key"] == "sk-custom-9999"
    assert cred["base_url"] == "https://api.example.com/v1"
    assert cred["provider_type"] == "openai"
    p = providers.get_provider("myprov")
    assert p is not None and p.api_key == "sk-custom-9999"
    assert result == "myprov/my-model"
    assert session.model_ref == "myprov/my-model"


def test_connect_custom_rejects_bad_id(monkeypatch):
    ui = StubUI(choices=[len(commands._provider_menu())], texts=["Bad ID!"])
    _use_ui(monkeypatch, ui)

    commands.handle_connect_command("", session=Session())

    assert any(sev == "error" for sev, _ in ui.notifications)


def test_connect_menu_cancel(monkeypatch):
    ui = StubUI(choices=[None])  # Esc on the provider menu
    _use_ui(monkeypatch, ui)

    result = commands.handle_connect_command("", session=Session())

    assert result is None
    assert auth.load_auth() == {}


# ── list / logout ─────────────────────────────────────────────────────────


def test_connect_list_shows_stored_and_masks(monkeypatch):
    auth.set_credential("anthropic", {"type": "api", "key": "sk-supersecret-key"})
    ui = StubUI()
    _use_ui(monkeypatch, ui)

    commands.handle_connect_command("list", session=None)

    out = "\n".join(ui.prints)
    assert "anthropic" in out
    assert "sk-supersecret-key" not in out  # raw key never printed


def test_connect_logout_with_arg_removes(monkeypatch):
    auth.set_credential("anthropic", {"type": "api", "key": "sk-x"})
    providers.apply_stored_credentials()
    ui = StubUI()
    _use_ui(monkeypatch, ui)

    commands.handle_connect_command("logout anthropic", session=None)

    assert auth.get_credential("anthropic") is None
    assert providers.get_provider("anthropic").api_key is None


def test_connect_logout_interactive_pick(monkeypatch):
    auth.set_credential("openai", {"type": "api", "key": "sk-o"})
    ui = StubUI(choices=[0])
    _use_ui(monkeypatch, ui)

    commands.handle_connect_command("logout", session=None)

    assert auth.get_credential("openai") is None


def test_connect_logout_nothing_stored(monkeypatch):
    ui = StubUI()
    _use_ui(monkeypatch, ui)

    commands.handle_connect_command("logout", session=None)

    assert any(sev == "warn" for sev, _ in ui.notifications)

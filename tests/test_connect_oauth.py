"""Tests for the ChatGPT (Codex) OAuth path of ``/connect``.

The ``/connect`` handler offers a 2-way menu when the OpenAI provider is
picked — "ChatGPT Pro/Plus (browser sign-in)" vs "API key". This file
covers the new OAuth branch end to end with the local PKCE flow stubbed
out, plus the providers-side consumption (``codex_oauth`` flag, GPT-5
model filtering, Codex Responses model creation).
"""

from __future__ import annotations

import time
import urllib.parse

import pytest

from aru import auth, codex_oauth, commands, providers
from aru.session import Session


# ── shared stub UI (same shape as test_connect_command.StubUI) ──────────


class StubUI:
    def __init__(self, *, choices=None, texts=None):
        self._choices = list(choices or [])
        self._texts = list(texts or [])
        self.prints: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.choice_options: list[list[str]] = []
        self.choice_titles: list[str] = []

    def ask_choice(self, options, *, title=None, default=0, cancel_value=None, details=None):
        self.choice_options.append(list(options))
        self.choice_titles.append(title or "")
        return self._choices.pop(0)

    def ask_text(self, prompt, *, default="", multiline=False, password=False):
        return self._texts.pop(0)

    def confirm(self, prompt, default=False):
        return True

    def print(self, renderable):
        self.prints.append(str(renderable))

    def notify(self, message, severity="info"):
        self.notifications.append((severity, str(message)))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Temp credential file + provider-registry restore."""
    monkeypatch.setattr(auth, "auth_path", lambda: tmp_path / "auth.json")
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


def _use_ui(monkeypatch, ui):
    monkeypatch.setattr(commands, "_resolve_connect_ui", lambda: ui)


# ── apply_stored_credentials for {"type": "oauth"} ───────────────────────


def test_apply_stored_credentials_sets_codex_oauth_flag():
    auth.set_credential(
        "openai",
        {
            "type": "oauth",
            "refresh": "rt",
            "access": "at",
            "expires": int(time.time() * 1000) + 3_600_000,
            "accountId": "acc-1",
        },
    )
    providers.apply_stored_credentials()
    p = providers.get_provider("openai")
    assert p.codex_oauth is True
    # OAuth path must NOT bleed a stale api_key back into the registry.
    assert p.api_key is None
    # Default model should be one the Codex backend accepts.
    assert p.default_model.startswith("gpt-5")


def test_apply_stored_credentials_api_supersedes_oauth():
    """Switching from OAuth → API key clears the codex_oauth flag."""
    p = providers.get_provider("openai")
    p.codex_oauth = True

    auth.set_credential("openai", {"type": "api", "key": "sk-typed"})
    providers.apply_stored_credentials()
    assert p.codex_oauth is False
    assert p.api_key == "sk-typed"


def test_forget_credential_clears_oauth_flag():
    p = providers.get_provider("openai")
    p.codex_oauth = True
    p.api_key = None

    providers.forget_credential("openai")
    assert p.codex_oauth is False


# ── /connect → ChatGPT OAuth happy path ─────────────────────────────────


def _stub_start(monkeypatch, tokens=None, raise_on_callback=None):
    """Replace start_codex_oauth_flow + await_codex_callback with stubs.

    Returns the recorder dict so tests can assert which URL would have
    been opened and that we ran exactly one callback wait.
    """
    rec: dict = {"opened_urls": [], "starts": 0, "awaits": 0, "stops": 0}

    class FakeFlow:
        authorize_url = "https://auth.openai.com/oauth/authorize?fake=1"
        pkce = codex_oauth.PkceCodes(verifier="v", challenge="c")
        state = "fake-state"

    def fake_start():
        rec["starts"] += 1
        return FakeFlow()

    def fake_await(flow, timeout=300):
        rec["awaits"] += 1
        if raise_on_callback is not None:
            raise raise_on_callback
        return tokens or {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_in": 3600,
            "id_token": None,
        }

    def fake_stop(flow):
        rec["stops"] += 1

    def fake_open(url, new=0, **_kw):
        rec["opened_urls"].append(url)
        return True

    monkeypatch.setattr(codex_oauth, "start_codex_oauth_flow", fake_start)
    monkeypatch.setattr(codex_oauth, "await_codex_callback", fake_await)
    monkeypatch.setattr(codex_oauth, "stop_codex_oauth_flow", fake_stop)
    monkeypatch.setattr("webbrowser.open", fake_open)
    return rec


def test_connect_openai_oauth_stores_tokens_and_picks_model(monkeypatch):
    """Pick OpenAI → ChatGPT OAuth → finish browser flow → pick first model."""
    rec = _stub_start(
        monkeypatch,
        tokens={
            "access_token": "ATok",
            "refresh_token": "RTok",
            "expires_in": 3600,
            "id_token": None,
        },
    )
    # provider menu: openai (idx 1); OAuth-vs-key menu: 0 (OAuth); model menu: 0
    ui = StubUI(choices=[1, 0, 0])
    _use_ui(monkeypatch, ui)

    session = Session()
    result = commands.handle_connect_command("", session=session)

    # Browser was prompted with the authorize URL exactly once.
    assert rec["starts"] == 1
    assert rec["awaits"] == 1
    assert "auth.openai.com" in rec["opened_urls"][0]

    cred = auth.get_credential("openai")
    assert cred["type"] == "oauth"
    assert cred["access"] == "ATok"
    assert cred["refresh"] == "RTok"
    assert isinstance(cred["expires"], int)
    assert providers.get_provider("openai").codex_oauth is True

    # Session moved to a Codex-supported model.
    assert result is not None
    assert result.startswith("openai/gpt-5")
    assert session.model_ref == result


def test_connect_openai_oauth_filters_picker_to_gpt5(monkeypatch):
    """The model picker shown after OAuth must hide gpt-4o / o3 / etc."""
    _stub_start(monkeypatch)
    # provider=openai, OAuth, then model idx=0
    ui = StubUI(choices=[1, 0, 0])
    _use_ui(monkeypatch, ui)
    commands.handle_connect_command("", session=Session())

    # First choice_options is the provider menu; second is the OAuth menu;
    # third is the model picker. Validate the model picker contents.
    model_options = ui.choice_options[-1]
    # Strip the trailing "manual id" escape hatch before checking.
    model_names = [o for o in model_options if o != commands._CUSTOM_MODEL_LABEL]
    assert model_names, "Codex OAuth picker should list the GPT-5 models"
    assert all(name.startswith("gpt-5") for name in model_names)


def test_connect_openai_oauth_cancel_menu_does_nothing(monkeypatch):
    rec = _stub_start(monkeypatch)
    # provider=openai, then cancel on the OAuth-vs-API menu
    ui = StubUI(choices=[1, None])
    _use_ui(monkeypatch, ui)

    result = commands.handle_connect_command("", session=Session())

    assert result is None
    assert rec["starts"] == 0  # never attempted the flow
    assert auth.get_credential("openai") is None


def test_connect_openai_oauth_timeout_does_not_persist(monkeypatch):
    _stub_start(
        monkeypatch,
        raise_on_callback=TimeoutError("no callback received within 1s"),
    )
    ui = StubUI(choices=[1, 0])
    _use_ui(monkeypatch, ui)

    result = commands.handle_connect_command("", session=Session())

    assert result is None
    assert auth.get_credential("openai") is None
    assert any(sev == "warn" for sev, _ in ui.notifications)


def test_connect_openai_apikey_branch_still_works(monkeypatch):
    """Picking 'API key' after OpenAI keeps the legacy path intact."""
    _stub_start(monkeypatch)
    ui = StubUI(
        # provider=openai, OAuth/API menu: 1=API key, model menu: 0
        choices=[1, 1, 0],
        texts=["sk-typed-99999"],
    )
    _use_ui(monkeypatch, ui)

    result = commands.handle_connect_command("", session=Session())

    cred = auth.get_credential("openai")
    assert cred == {"type": "api", "key": "sk-typed-99999"}
    assert providers.get_provider("openai").codex_oauth is False
    assert providers.get_provider("openai").api_key == "sk-typed-99999"
    # Falls through to the regular model picker (all openai models visible).
    assert result is not None and result.startswith("openai/")


def test_connect_list_marks_oauth_credential(monkeypatch):
    auth.set_credential(
        "openai",
        {
            "type": "oauth",
            "refresh": "rt",
            "access": "at",
            "expires": int(time.time() * 1000) + 60_000,
            "accountId": "acc-pretty",
        },
    )
    ui = StubUI()
    _use_ui(monkeypatch, ui)
    commands.handle_connect_command("list", session=None)

    out = "\n".join(ui.prints)
    assert "[oauth]" in out
    assert "acc-pretty" in out
    # Tokens are never printed.
    assert "rt" not in out.split()
    assert "at" not in out.split()


# ── _list_provider_models filter ────────────────────────────────────────


def test_list_provider_models_filters_when_codex_oauth():
    p = providers.get_provider("openai")
    p.codex_oauth = True
    try:
        names = commands._list_provider_models(p)
        assert names, "expected at least one GPT-5 model"
        for n in names:
            mid = p.models[n].get("id", n)
            assert mid.startswith("gpt-5"), f"non-Codex model leaked: {n}"
    finally:
        p.codex_oauth = False


def test_list_provider_models_keeps_full_set_when_api_key():
    p = providers.get_provider("openai")
    names = commands._list_provider_models(p)
    # Both legacy (gpt-4o) and Codex (gpt-5.4) families visible on API-key path.
    has_legacy = any(p.models[n].get("id", n).startswith("gpt-4") for n in names)
    has_gpt5 = any(p.models[n].get("id", n).startswith("gpt-5") for n in names)
    assert has_legacy and has_gpt5


# ── Codex provider model construction ───────────────────────────────────


def test_codex_responses_model_is_built_with_codex_endpoint(monkeypatch):
    """``providers.create_model`` should route oauth-typed openai through
    the Codex Responses endpoint, with the dummy API key and the auth-
    bearing http_client."""
    p = providers.get_provider("openai")
    p.codex_oauth = True
    try:
        model = providers.create_model("openai/gpt-5.4")
        assert model.base_url == codex_oauth.CODEX_API_BASE
        assert model.api_key == codex_oauth.OAUTH_DUMMY_KEY
        # default_headers should brand the request and have the http_client
        # carry our CodexAuth so refreshes are transparent.
        assert model.default_headers["originator"] == "aru"
        assert model.http_client is not None
        # Class name confirms we went through Responses, not Chat Completions.
        assert "Responses" in type(model).__name__
        # And specifically our Codex subclass, which strips system messages.
        assert type(model).__name__ == "CodexOpenAIResponses"
    finally:
        p.codex_oauth = False


def test_codex_model_lifts_system_into_instructions(monkeypatch):
    """The Codex backend requires `instructions=…` and a system-free input.

    Walk through the subclass's two hooks (`_format_messages`,
    `get_request_params`) and verify the system prompt is lifted out of
    `input` and surfaced as the top-level `instructions` field — that's
    what fixes the ``400 "Instructions are required"`` rejection.
    """
    from agno.models.message import Message

    p = providers.get_provider("openai")
    p.codex_oauth = True
    try:
        model = providers.create_model("openai/gpt-5.4")
        msgs = [
            Message(role="system", content="You are aru."),
            Message(role="system", content="Be terse."),
            Message(role="user", content="Hello"),
        ]

        # System messages must be stripped from `input` (the Codex backend
        # rejects them when both `instructions` and a system message exist).
        formatted_input = model._format_messages(msgs)
        roles = [m.get("role") for m in formatted_input if isinstance(m, dict)]
        assert "system" not in roles
        assert "developer" not in roles  # OpenAI rewrites; Codex would reject

        # And the lifted text becomes the top-level instructions field.
        params = model.get_request_params(messages=msgs)
        assert "instructions" in params
        assert "You are aru." in params["instructions"]
        assert "Be terse." in params["instructions"]
    finally:
        p.codex_oauth = False


def test_codex_model_omits_instructions_when_no_system(monkeypatch):
    """No system messages → no `instructions` key (no point sending empty)."""
    from agno.models.message import Message

    p = providers.get_provider("openai")
    p.codex_oauth = True
    try:
        model = providers.create_model("openai/gpt-5.4")
        msgs = [Message(role="user", content="Hello")]
        params = model.get_request_params(messages=msgs)
        assert "instructions" not in params
    finally:
        p.codex_oauth = False

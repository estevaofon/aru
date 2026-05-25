"""Tests for the credential store (``aru.auth``) and its consumption in
``aru.providers`` (``apply_stored_credentials`` / ``_resolve_api_key`` /
``forget_credential``) that back the ``/connect`` command.
"""

from __future__ import annotations

import json

import pytest

from aru import auth, providers


@pytest.fixture(autouse=True)
def _isolated_auth(tmp_path, monkeypatch):
    """Point the credential store at a temp file and restore the provider
    registry's mutable fields after each test (the registry is a module
    global shared with the BUILTIN_PROVIDERS objects)."""
    monkeypatch.setattr(auth, "auth_path", lambda: tmp_path / "auth.json")

    snapshot = {
        k: (p.api_key, p.base_url, p.default_model)
        for k, p in providers._providers.items()
    }
    original_keys = set(providers._providers.keys())
    yield
    # Restore mutated fields and drop any provider added during the test.
    for k in list(providers._providers.keys()):
        if k not in original_keys:
            del providers._providers[k]
    for k, (api_key, base_url, default_model) in snapshot.items():
        p = providers._providers.get(k)
        if p is not None:
            p.api_key = api_key
            p.base_url = base_url
            p.default_model = default_model


# ── auth.py storage ──────────────────────────────────────────────────────


def test_set_get_remove_roundtrip():
    assert auth.get_credential("anthropic") is None
    auth.set_credential("anthropic", {"type": "api", "key": "sk-abc"})
    assert auth.get_credential("anthropic") == {"type": "api", "key": "sk-abc"}
    assert auth.remove_credential("anthropic") is True
    assert auth.get_credential("anthropic") is None
    # Removing a missing key reports False.
    assert auth.remove_credential("nope") is False


def test_load_auth_ignores_garbage(tmp_path):
    path = auth.auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    assert auth.load_auth() == {}


def test_load_auth_drops_non_dict_entries():
    path = auth.auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"good": {"type": "api", "key": "k"}, "bad": "oops"}),
        encoding="utf-8",
    )
    data = auth.load_auth()
    assert "good" in data
    assert "bad" not in data


def test_set_credential_persists_to_disk():
    auth.set_credential("openai", {"type": "api", "key": "sk-openai"})
    on_disk = json.loads(auth.auth_path().read_text(encoding="utf-8"))
    assert on_disk["openai"]["key"] == "sk-openai"


# ── providers consumption ────────────────────────────────────────────────


def test_resolve_api_key_prefers_stored_over_env(monkeypatch):
    monkeypatch.setenv("MY_ENV_KEY", "from-env")
    p = providers.ProviderConfig(name="X", api_key_env="MY_ENV_KEY", api_key="from-store")
    assert providers._resolve_api_key(p) == "from-store"


def test_resolve_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("MY_ENV_KEY", "from-env")
    p = providers.ProviderConfig(name="X", api_key_env="MY_ENV_KEY")
    assert providers._resolve_api_key(p) == "from-env"


def test_resolve_api_key_none_when_unset(monkeypatch):
    monkeypatch.delenv("MY_ENV_KEY", raising=False)
    p = providers.ProviderConfig(name="X", api_key_env="MY_ENV_KEY")
    assert providers._resolve_api_key(p) is None


def test_apply_stored_credentials_sets_builtin_key():
    auth.set_credential("anthropic", {"type": "api", "key": "sk-stored"})
    providers.apply_stored_credentials()
    assert providers.get_provider("anthropic").api_key == "sk-stored"


def test_apply_stored_credentials_registers_custom_provider():
    auth.set_credential(
        "myprov",
        {
            "type": "api",
            "key": "sk-custom",
            "base_url": "https://api.example.com/v1",
            "name": "My Provider",
            "provider_type": "openai",
            "default_model": "my-model",
        },
    )
    providers.apply_stored_credentials()
    p = providers.get_provider("myprov")
    assert p is not None
    assert p.api_key == "sk-custom"
    assert p.base_url == "https://api.example.com/v1"
    assert p.name == "My Provider"
    assert p.default_model == "my-model"
    assert p.options.get("_provider_type") == "openai"


def test_apply_stored_credentials_local_provider_sets_base_url():
    auth.set_credential("ollama", {"type": "local", "base_url": "http://box:11434"})
    providers.apply_stored_credentials()
    assert providers.get_provider("ollama").base_url == "http://box:11434"


def test_apply_stored_credentials_missing_file_is_noop():
    # No file written — must not raise and must not invent providers.
    providers.apply_stored_credentials()


def test_forget_credential_clears_in_memory_key():
    auth.set_credential("anthropic", {"type": "api", "key": "sk-stored"})
    providers.apply_stored_credentials()
    assert providers.get_provider("anthropic").api_key == "sk-stored"
    providers.forget_credential("anthropic")
    assert providers.get_provider("anthropic").api_key is None

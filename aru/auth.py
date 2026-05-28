"""Credential store for provider API keys (OpenCode-parity ``auth.json``).

``/connect`` writes here so users no longer have to hand-edit ``aru.json``
to wire up a provider. Mirrors OpenCode's ``auth.json``: a flat
``{ "<provider>": <info> }`` map persisted under the user's home, written
with ``0600`` permissions so the key isn't world-readable.

Schema (``info``) — a tagged union on ``type`` so future auth methods
(OAuth, well-known) can slot in beside the current API-key path:

    {"type": "api",   "key": "sk-...",            # built-in provider
     "base_url": "...", "name": "...",            # extra fields for a
     "provider_type": "openai", "default_model": "...",  # custom provider
     "context_limit": 128000}
    {"type": "local", "base_url": "http://..."}   # keyless (e.g. Ollama)
    {"type": "oauth", "refresh": "...",            # ChatGPT (Codex) — wired
     "access": "...", "expires": 1735689600000,   # by /connect → "ChatGPT
     "accountId": "acc-..."}                      # Pro/Plus (browser)"

Consumption lives in :func:`aru.providers.apply_stored_credentials`, which
layers these onto the in-memory provider registry at startup (and again
right after ``/connect``) so a stored key takes precedence over the
provider's ``api_key_env``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("aru.auth")


def auth_path() -> Path:
    """Absolute path to the credential file (``~/.aru/auth.json``)."""
    return Path.home() / ".aru" / "auth.json"


def load_auth() -> dict[str, dict[str, Any]]:
    """Return the full credential map, or ``{}`` when missing/unreadable."""
    path = auth_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Drop any malformed (non-dict) entries defensively.
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def get_credential(provider_key: str) -> dict[str, Any] | None:
    """Return the stored credential for ``provider_key`` or ``None``."""
    return load_auth().get(provider_key)


def _write_auth(data: dict[str, dict[str, Any]]) -> None:
    """Write the credential map atomically-ish with ``0600`` perms."""
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Lock down before the rename so the secret is never briefly group/other
    # readable. chmod is a partial no-op on Windows but harmless.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def set_credential(provider_key: str, info: dict[str, Any]) -> None:
    """Store (or replace) the credential for ``provider_key``."""
    data = load_auth()
    data[provider_key] = info
    _write_auth(data)


def remove_credential(provider_key: str) -> bool:
    """Delete the credential for ``provider_key``. Returns ``True`` if removed."""
    data = load_auth()
    if provider_key not in data:
        return False
    del data[provider_key]
    _write_auth(data)
    return True

"""Tests for ``aru.codex_oauth`` — the ChatGPT (Codex) PKCE OAuth flow.

Covers the parts that are pure (PKCE generation, JWT claim parsing, account
id extraction) plus the end-to-end ``start_codex_oauth_flow`` →
``await_codex_callback`` handshake against a fake OAuth server.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aru import codex_oauth


# ── helpers ──────────────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_jwt(payload: dict) -> str:
    header = _b64url(json.dumps({"alg": "none"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{header}.{body}.sig"


# ── PKCE + URL ───────────────────────────────────────────────────────────


def test_generate_pkce_lengths_and_alphabet():
    pkce = codex_oauth.generate_pkce()
    # Spec: verifier between 43 and 128 chars; we generate exactly 43.
    assert len(pkce.verifier) == 43
    # Challenge is base64url(sha256(verifier)) without padding — 43 chars.
    assert len(pkce.challenge) == 43
    assert "=" not in pkce.challenge
    assert "+" not in pkce.challenge
    assert "/" not in pkce.challenge


def test_generate_pkce_is_unique():
    a = codex_oauth.generate_pkce()
    b = codex_oauth.generate_pkce()
    assert a.verifier != b.verifier
    assert a.challenge != b.challenge


def test_build_authorize_url_carries_pkce_state_and_originator():
    pkce = codex_oauth.PkceCodes(verifier="v" * 43, challenge="c" * 43)
    url = codex_oauth.build_authorize_url(pkce, state="state-x")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["code_challenge"] == ["c" * 43]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["state"] == ["state-x"]
    assert qs["client_id"] == [codex_oauth.CLIENT_ID]
    assert qs["redirect_uri"] == [codex_oauth.OAUTH_REDIRECT_URI]
    # Brand the request as aru so OpenAI's logs can identify us.
    assert qs["originator"] == ["aru"]


# ── JWT parsing ──────────────────────────────────────────────────────────


def test_parse_jwt_claims_valid():
    payload = {"email": "test@example.com", "chatgpt_account_id": "acc-123"}
    jwt = _make_jwt(payload)
    assert codex_oauth.parse_jwt_claims(jwt) == payload


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "invalid",
        "only.two",
        "a.b.c.d",
        "a.!!!.b",
    ],
)
def test_parse_jwt_claims_bad_inputs_return_none(bad):
    assert codex_oauth.parse_jwt_claims(bad) is None


def test_parse_jwt_claims_non_json_body_returns_none():
    header = _b64url(b"{}")
    body = _b64url(b"not json")
    assert codex_oauth.parse_jwt_claims(f"{header}.{body}.sig") is None


# ── account id extraction (mirrors opencode codex.test.ts) ──────────────


def test_extract_account_id_from_claims_root():
    assert (
        codex_oauth.extract_account_id_from_claims({"chatgpt_account_id": "acc-root"})
        == "acc-root"
    )


def test_extract_account_id_from_claims_nested():
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "acc-nested"}}
    assert codex_oauth.extract_account_id_from_claims(claims) == "acc-nested"


def test_extract_account_id_prefers_root_over_nested():
    claims = {
        "chatgpt_account_id": "acc-root",
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-nested"},
    }
    assert codex_oauth.extract_account_id_from_claims(claims) == "acc-root"


def test_extract_account_id_from_organizations_fallback():
    claims = {"organizations": [{"id": "org-1"}, {"id": "org-2"}]}
    assert codex_oauth.extract_account_id_from_claims(claims) == "org-1"


def test_extract_account_id_returns_none_when_missing():
    assert codex_oauth.extract_account_id_from_claims({"email": "x@y.com"}) is None


def test_extract_account_id_prefers_id_token_over_access():
    id_jwt = _make_jwt({"chatgpt_account_id": "from-id-token"})
    access_jwt = _make_jwt({"chatgpt_account_id": "from-access-token"})
    assert (
        codex_oauth.extract_account_id(
            {"id_token": id_jwt, "access_token": access_jwt, "refresh_token": "rt"}
        )
        == "from-id-token"
    )


def test_extract_account_id_falls_back_to_access_token():
    id_jwt = _make_jwt({"email": "x@y.com"})
    access_jwt = _make_jwt(
        {"https://api.openai.com/auth": {"chatgpt_account_id": "from-access"}}
    )
    assert (
        codex_oauth.extract_account_id(
            {"id_token": id_jwt, "access_token": access_jwt, "refresh_token": "rt"}
        )
        == "from-access"
    )


def test_extract_account_id_returns_none_when_no_tokens_carry_id():
    jwt = _make_jwt({"email": "x@y.com"})
    assert (
        codex_oauth.extract_account_id(
            {"id_token": jwt, "access_token": jwt, "refresh_token": "rt"}
        )
        is None
    )


# ── End-to-end OAuth handshake (against a fake server) ──────────────────


class _FakeIssuer:
    """Tiny stand-in for ``auth.openai.com`` running on an ephemeral port.

    Only implements ``/oauth/token`` with the form the code expects. The
    test injects this server's origin in place of the real issuer so
    :func:`exchange_code_for_tokens` and :func:`refresh_codex_tokens` round
    trip without touching the network.
    """

    def __init__(self):
        self.exchange_calls: list[dict[str, str]] = []
        self.refresh_calls: list[dict[str, str]] = []
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def _handler(this):  # noqa: N805 — outer instance for closures
        class H(BaseHTTPRequestHandler):
            def log_message(self, *_a, **_k):  # silence
                return

            def do_POST(self):  # noqa: N802
                if self.path != "/oauth/token":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("ascii")
                form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
                grant = form.get("grant_type", "")
                if grant == "authorization_code":
                    this.exchange_calls.append(form)
                    payload = {
                        "id_token": _make_jwt({"chatgpt_account_id": "acc-from-id"}),
                        "access_token": "access-1",
                        "refresh_token": "refresh-1",
                        "expires_in": 3600,
                    }
                elif grant == "refresh_token":
                    this.refresh_calls.append(form)
                    payload = {
                        "id_token": _make_jwt({"chatgpt_account_id": "acc-from-id"}),
                        "access_token": "access-2",
                        "refresh_token": "refresh-2",
                        "expires_in": 3600,
                    }
                else:
                    self.send_error(400)
                    return
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return H


@pytest.fixture
def fake_issuer(monkeypatch):
    issuer = _FakeIssuer()
    monkeypatch.setattr(codex_oauth, "ISSUER", issuer.origin)
    try:
        yield issuer
    finally:
        issuer.stop()


def test_exchange_code_for_tokens_round_trip(fake_issuer):
    pkce = codex_oauth.generate_pkce()
    tokens = codex_oauth.exchange_code_for_tokens("the-code", pkce)
    assert tokens["access_token"] == "access-1"
    assert tokens["refresh_token"] == "refresh-1"
    assert tokens["expires_in"] == 3600
    # Server saw the right grant_type + code_verifier round-tripped.
    sent = fake_issuer.exchange_calls[-1]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert sent["code_verifier"] == pkce.verifier
    assert sent["redirect_uri"] == codex_oauth.OAUTH_REDIRECT_URI
    assert sent["client_id"] == codex_oauth.CLIENT_ID


def test_refresh_codex_tokens_round_trip(fake_issuer):
    tokens = codex_oauth.refresh_codex_tokens("refresh-old")
    assert tokens["access_token"] == "access-2"
    sent = fake_issuer.refresh_calls[-1]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "refresh-old"
    assert sent["client_id"] == codex_oauth.CLIENT_ID


def test_full_oauth_flow_with_local_callback(fake_issuer, monkeypatch):
    """Drive ``start_codex_oauth_flow`` → simulate browser → expect tokens."""
    flow = codex_oauth.start_codex_oauth_flow()
    try:
        # Sanity: the authorize URL embeds our state and PKCE challenge.
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(flow.authorize_url).query)
        assert qs["state"] == [flow.state]
        assert qs["code_challenge"] == [flow.pkce.challenge]

        # Simulate the browser hitting our local callback. Use a background
        # thread so await_codex_callback can block on the main thread.
        callback_url = (
            f"http://127.0.0.1:{codex_oauth.OAUTH_PORT}/auth/callback?"
            + urllib.parse.urlencode({"code": "the-code", "state": flow.state})
        )

        def hit_callback():
            time.sleep(0.05)  # let await_codex_callback enter the wait
            urllib.request.urlopen(callback_url, timeout=5).read()

        threading.Thread(target=hit_callback, daemon=True).start()

        tokens = codex_oauth.await_codex_callback(flow, timeout=5)
        assert tokens["access_token"] == "access-1"
        assert codex_oauth.extract_account_id(tokens) == "acc-from-id"
    finally:
        # Idempotent — await_codex_callback already shut it down.
        codex_oauth.stop_codex_oauth_flow(flow)


def test_oauth_flow_rejects_csrf_state(fake_issuer):
    flow = codex_oauth.start_codex_oauth_flow()
    try:
        callback_url = (
            f"http://127.0.0.1:{codex_oauth.OAUTH_PORT}/auth/callback?"
            + urllib.parse.urlencode({"code": "the-code", "state": "WRONG"})
        )

        def hit_callback():
            time.sleep(0.05)
            try:
                urllib.request.urlopen(callback_url, timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=hit_callback, daemon=True).start()

        with pytest.raises(RuntimeError, match="state"):
            codex_oauth.await_codex_callback(flow, timeout=5)
    finally:
        codex_oauth.stop_codex_oauth_flow(flow)


# ── CodexAuth (httpx auth) + refresh on demand ───────────────────────────


@pytest.fixture
def isolated_auth(tmp_path, monkeypatch):
    from aru import auth, providers

    monkeypatch.setattr(auth, "auth_path", lambda: tmp_path / "auth.json")
    snapshot = {
        k: (p.api_key, p.base_url, p.default_model, p.codex_oauth)
        for k, p in providers._providers.items()
    }
    yield
    for k, (api_key, base_url, default_model, codex) in snapshot.items():
        p = providers._providers.get(k)
        if p is not None:
            p.api_key = api_key
            p.base_url = base_url
            p.default_model = default_model
            p.codex_oauth = codex


def _fresh_creds(account_id="acc-x", expires_in_ms=3600_000):
    return {
        "type": "oauth",
        "refresh": "refresh-current",
        "access": "access-current",
        "expires": int(time.time() * 1000) + expires_in_ms,
        "accountId": account_id,
    }


class _FakeRequest:
    """Stub matching the bits of an httpx.Request that CodexAuth touches."""

    def __init__(self, initial_auth=None):
        self.headers: dict[str, str] = {}
        if initial_auth is not None:
            self.headers["Authorization"] = initial_auth


def test_codex_auth_writes_bearer_and_account_id(isolated_auth, fake_issuer):
    from aru import auth

    auth.set_credential("openai", _fresh_creds(account_id="acc-x"))

    request = _FakeRequest(initial_auth="Bearer dummy-stripped")
    list(codex_oauth.CodexAuth().auth_flow(request))

    assert request.headers["Authorization"] == "Bearer access-current"
    assert request.headers["ChatGPT-Account-Id"] == "acc-x"
    assert request.headers["originator"] == "aru"


def test_codex_auth_refreshes_when_expired(isolated_auth, fake_issuer):
    from aru import auth

    # Already-expired credential.
    auth.set_credential(
        "openai",
        {
            "type": "oauth",
            "refresh": "refresh-old",
            "access": "access-old",
            "expires": int(time.time() * 1000) - 10_000,
            "accountId": "acc-prev",
        },
    )

    request = _FakeRequest()
    list(codex_oauth.CodexAuth().auth_flow(request))

    # Picked up refreshed tokens from the fake issuer.
    assert request.headers["Authorization"] == "Bearer access-2"
    # Persisted to disk so the next process inherits the new state.
    stored = auth.get_credential("openai")
    assert stored["access"] == "access-2"
    assert stored["refresh"] == "refresh-2"


def test_codex_auth_raises_without_credential(isolated_auth):
    request = _FakeRequest()
    with pytest.raises(RuntimeError, match="No Codex OAuth"):
        list(codex_oauth.CodexAuth().auth_flow(request))

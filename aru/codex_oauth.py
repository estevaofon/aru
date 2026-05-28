"""ChatGPT (Codex) OAuth for OpenAI provider — Plus/Pro browser login.

Mirrors OpenCode's ``packages/opencode/src/plugin/openai/codex.ts`` so users
who have an active ChatGPT Plus / Pro subscription can sign in once via the
browser and route requests through their plan instead of pay-as-you-go API
credits. The flow is the standard OAuth 2.0 PKCE Authorization Code grant
against ``auth.openai.com`` with the Codex CLI's client_id.

Three-stage handshake:

1. ``start_codex_oauth_flow()`` — spin up a tiny localhost callback server on
   port 1455, generate a PKCE verifier/challenge, build the authorize URL
   and return it. The caller opens the URL in the user's browser
   (``webbrowser.open``) and awaits the callback.
2. ``await_codex_callback(flow)`` — block until the user completes the
   browser consent screen. The local server picks up the ``?code=…&state=…``
   query, exchanges the code for ``{access, refresh, id_token, expires_in}``
   at ``/oauth/token`` and extracts the ChatGPT account id from the JWT
   claims (``chatgpt_account_id`` or fallback locations).
3. ``refresh_codex_tokens(refresh_token)`` — swap a long-lived ``refresh``
   token for a new ``access`` token when the cached one expires (handled
   transparently by :class:`CodexAuth` on every request).

Persistence lives in ``~/.aru/auth.json`` under provider id ``openai`` with
``{"type": "oauth", "refresh", "access", "expires", "accountId"}`` —
provider creation in :mod:`aru.providers` detects this and swaps an Agno
``OpenAIResponses`` model pointed at the Codex endpoint into the registry.

This module is import-light by design: the local HTTP server and browser
launcher are only touched when ``start_codex_oauth_flow`` is invoked, so
ordinary startup paths (which only ever need :func:`refresh_codex_tokens`
and :class:`CodexAuth`) don't pay for them.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("aru.codex_oauth")

# Codex CLI's OAuth app id. Stable; published in OpenAI's Codex CLI source.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_API_ENDPOINT = f"{CODEX_API_BASE}/responses"
OAUTH_PORT = 1455
OAUTH_REDIRECT_URI = f"http://localhost:{OAUTH_PORT}/auth/callback"
CALLBACK_TIMEOUT_SECONDS = 5 * 60


# ---------------------------------------------------------------------------
# PKCE + URL building
# ---------------------------------------------------------------------------

_PKCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class PkceCodes:
    verifier: str
    challenge: str


def generate_pkce() -> PkceCodes:
    """Generate a (verifier, challenge) PKCE pair using SHA-256.

    Matches the OpenCode TS implementation: 43-byte verifier drawn from a
    URL-safe alphabet, S256 challenge.
    """
    verifier = "".join(
        _PKCE_ALPHABET[b % len(_PKCE_ALPHABET)] for b in secrets.token_bytes(43)
    )
    challenge = _base64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkceCodes(verifier=verifier, challenge=challenge)


def build_authorize_url(pkce: PkceCodes, state: str) -> str:
    """Build the OpenAI ``/oauth/authorize`` URL that opens in the browser."""
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": "openid profile email offline_access",
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "aru",
    }
    return f"{ISSUER}/oauth/authorize?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# JWT claim parsing (account id extraction)
# ---------------------------------------------------------------------------

def parse_jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode the middle (claims) segment of a JWT without signature checks.

    Returns ``None`` for malformed tokens. Signature verification is
    deliberately skipped — we trust the token here because it came directly
    from the OAuth endpoint over TLS and we only read it to surface a
    convenience field (``chatgpt_account_id``).
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        # base64url padding restoration
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(body.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    return claims


def extract_account_id_from_claims(claims: dict[str, Any]) -> str | None:
    """Pull the ChatGPT account id out of JWT claims.

    Search order mirrors OpenCode/Codex CLI:
      1. ``chatgpt_account_id`` at the root
      2. ``["https://api.openai.com/auth"].chatgpt_account_id`` (nested)
      3. ``organizations[0].id`` (org-mode fallback)
    """
    if not isinstance(claims, dict):
        return None
    root = claims.get("chatgpt_account_id")
    if isinstance(root, str) and root:
        return root
    nested = claims.get("https://api.openai.com/auth")
    if isinstance(nested, dict):
        nested_id = nested.get("chatgpt_account_id")
        if isinstance(nested_id, str) and nested_id:
            return nested_id
    orgs = claims.get("organizations")
    if isinstance(orgs, list) and orgs:
        first = orgs[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id:
                return org_id
    return None


def extract_account_id(tokens: dict[str, Any]) -> str | None:
    """Find the account id in the OAuth token response — prefer id_token."""
    id_token = tokens.get("id_token")
    if isinstance(id_token, str) and id_token:
        claims = parse_jwt_claims(id_token)
        if claims:
            acc = extract_account_id_from_claims(claims)
            if acc:
                return acc
    access = tokens.get("access_token")
    if isinstance(access, str) and access:
        claims = parse_jwt_claims(access)
        if claims:
            return extract_account_id_from_claims(claims)
    return None


# ---------------------------------------------------------------------------
# Token endpoints
# ---------------------------------------------------------------------------

def _post_form(url: str, data: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    """Tiny POST helper — used for both the code exchange and refresh.

    Avoids pulling in ``httpx``/``requests`` so this module can be imported
    standalone (e.g. in tests / a future CLI subcommand) without ordering
    issues.
    """
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "aru-codex-oauth",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"OAuth request to {url} failed: HTTP {e.code} {detail[:200]}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OAuth request to {url} failed: {e.reason}") from e
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RuntimeError(f"OAuth response from {url} was not JSON") from e


def exchange_code_for_tokens(code: str, pkce: PkceCodes) -> dict[str, Any]:
    """Trade an authorization code for ``{access_token, refresh_token, …}``."""
    return _post_form(
        f"{ISSUER}/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": pkce.verifier,
        },
    )


def refresh_codex_tokens(refresh_token: str) -> dict[str, Any]:
    """Swap a refresh token for a fresh access token."""
    return _post_form(
        f"{ISSUER}/oauth/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
    )


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

_HTML_SUCCESS = """<!doctype html>
<html><head><title>aru — Codex login successful</title>
<style>body{font-family:system-ui,sans-serif;display:flex;justify-content:center;
align-items:center;height:100vh;margin:0;background:#131010;color:#f1ecec}
.c{text-align:center;padding:2rem}h1{margin-bottom:1rem}p{color:#b7b1b1}
</style></head><body><div class="c"><h1>You're signed in</h1>
<p>You can close this tab and return to aru.</p></div>
<script>setTimeout(()=>window.close(),1500)</script></body></html>"""


def _html_error(msg: str) -> str:
    safe = (msg or "Unknown error").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<!doctype html><html><head><title>aru — Codex login failed</title>'
        '<style>body{font-family:system-ui,sans-serif;display:flex;'
        'justify-content:center;align-items:center;height:100vh;margin:0;'
        'background:#131010;color:#f1ecec}.c{text-align:center;padding:2rem}'
        'h1{color:#fc533a;margin-bottom:1rem}p{color:#b7b1b1}'
        '.e{color:#ff917b;font-family:monospace;margin-top:1rem;padding:1rem;'
        'background:#3c140d;border-radius:.5rem}</style></head><body>'
        '<div class="c"><h1>Login failed</h1>'
        '<p>Something went wrong during authorization.</p>'
        f'<div class="e">{safe}</div></div></body></html>'
    )


@dataclass
class CodexAuthFlow:
    """Handle returned from :func:`start_codex_oauth_flow`.

    Carries everything the caller needs: the URL to open in the browser, the
    PKCE verifier (kept for the eventual code exchange), the CSRF ``state``,
    and the synchronisation primitives the local HTTP server uses to deliver
    the result back to :func:`await_codex_callback`.
    """
    authorize_url: str
    pkce: PkceCodes
    state: str
    _server: socketserver.TCPServer
    _thread: threading.Thread
    _result_event: threading.Event = field(default_factory=threading.Event)
    _result: dict[str, Any] | None = None
    _error: BaseException | None = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler: accept ``/auth/callback`` then trigger shutdown."""
    # Filled in by start_codex_oauth_flow before the server starts serving.
    flow: "CodexAuthFlow | None" = None

    # Silence the default request log — we have our own logger.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("oauth callback: " + format, *args)

    def _send_html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        flow = self.flow
        if flow is None:
            self._send_html(404, _html_error("No active OAuth flow"))
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth/callback":
            self._send_html(404, "Not found")
            return

        query = urllib.parse.parse_qs(parsed.query)
        error = query.get("error", [None])[0]
        error_desc = query.get("error_description", [None])[0]
        if error:
            msg = error_desc or error
            flow._error = RuntimeError(f"OAuth error: {msg}")
            self._send_html(200, _html_error(msg))
            flow._result_event.set()
            return

        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        if not code:
            flow._error = RuntimeError("Missing authorization code")
            self._send_html(400, _html_error("Missing authorization code"))
            flow._result_event.set()
            return
        if state != flow.state:
            flow._error = RuntimeError("Invalid state — potential CSRF")
            self._send_html(400, _html_error("Invalid state — potential CSRF"))
            flow._result_event.set()
            return

        try:
            tokens = exchange_code_for_tokens(code, flow.pkce)
            flow._result = tokens
            self._send_html(200, _HTML_SUCCESS)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            flow._error = exc
            self._send_html(500, _html_error(str(exc)))
        flow._result_event.set()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_codex_oauth_flow() -> CodexAuthFlow:
    """Bootstrap the OAuth flow and return the URL to open in the browser.

    Spins up a tiny localhost server on port 1455 that will receive the
    ``/auth/callback`` redirect, computes the PKCE pair and state, builds the
    authorize URL and returns the :class:`CodexAuthFlow` handle. Caller is
    responsible for opening ``flow.authorize_url`` (e.g. via
    ``webbrowser.open``) and then awaiting the result via
    :func:`await_codex_callback`.

    Raises ``OSError`` (typically ``EADDRINUSE``) when port 1455 is already
    occupied. The port is fixed because the OAuth app's redirect URI list is
    server-side and hardcoded.
    """
    pkce = generate_pkce()
    state = _base64url_encode(secrets.token_bytes(32))
    authorize_url = build_authorize_url(pkce, state)

    server = _ThreadingHTTPServer(("127.0.0.1", OAUTH_PORT), _CallbackHandler)
    thread = threading.Thread(
        target=server.serve_forever, name="aru-codex-oauth", daemon=True
    )
    flow = CodexAuthFlow(
        authorize_url=authorize_url,
        pkce=pkce,
        state=state,
        _server=server,
        _thread=thread,
    )
    # Stash the live flow on the handler class so the per-request handler can
    # see it without us needing to subclass per-flow.
    _CallbackHandler.flow = flow
    thread.start()
    logger.info("codex oauth server listening on port %d", OAUTH_PORT)
    return flow


def await_codex_callback(
    flow: CodexAuthFlow, timeout: float = CALLBACK_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Block until the user finishes the browser handshake and return tokens.

    Always tears down the local server (regardless of success/failure) before
    returning. Raises ``TimeoutError`` if the user takes longer than
    ``timeout`` seconds, or the underlying error if the exchange failed.
    """
    try:
        got = flow._result_event.wait(timeout=timeout)
        if not got:
            raise TimeoutError(
                "Codex OAuth: no callback received within "
                f"{timeout:.0f}s — login cancelled."
            )
        if flow._error is not None:
            raise flow._error
        if flow._result is None:
            raise RuntimeError("Codex OAuth: empty token response")
        return flow._result
    finally:
        stop_codex_oauth_flow(flow)


def stop_codex_oauth_flow(flow: CodexAuthFlow) -> None:
    """Tear down the local callback server. Safe to call multiple times."""
    try:
        flow._server.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        flow._server.server_close()
    except Exception:  # noqa: BLE001
        pass
    if _CallbackHandler.flow is flow:
        _CallbackHandler.flow = None


# ---------------------------------------------------------------------------
# httpx Auth — injects Bearer + account-id and refreshes on the fly
# ---------------------------------------------------------------------------

# Sentinel api_key handed to the OpenAI SDK so it doesn't raise on a missing
# key. The actual ``Authorization`` header is written by :class:`CodexAuth`
# below — the SDK's value is stripped before the request goes out.
OAUTH_DUMMY_KEY = "aru-codex-oauth-dummy"

# Refresh margin: refresh slightly before the token actually expires so we
# never race the API check.
_REFRESH_MARGIN_MS = 60_000


def _load_codex_credential() -> dict[str, Any]:
    from aru import auth as auth_mod

    creds = auth_mod.get_credential("openai")
    if not creds or creds.get("type") != "oauth":
        raise RuntimeError(
            "No Codex OAuth credential — run /connect and pick "
            "'ChatGPT Pro/Plus (browser)' first."
        )
    return creds


def _save_codex_credential(creds: dict[str, Any]) -> None:
    from aru import auth as auth_mod

    auth_mod.set_credential("openai", creds)


def _refresh_if_needed(creds: dict[str, Any]) -> dict[str, Any]:
    expires = creds.get("expires", 0)
    now_ms = int(time.time() * 1000)
    if isinstance(expires, (int, float)) and now_ms < int(expires) - _REFRESH_MARGIN_MS:
        return creds
    refresh = creds.get("refresh")
    if not isinstance(refresh, str) or not refresh:
        raise RuntimeError("Codex credential has no refresh token; re-run /connect.")
    logger.info("refreshing Codex access token")
    tokens = refresh_codex_tokens(refresh)
    new_creds: dict[str, Any] = {
        "type": "oauth",
        "refresh": tokens.get("refresh_token") or refresh,
        "access": tokens["access_token"],
        "expires": now_ms + int(tokens.get("expires_in", 3600)) * 1000,
    }
    account_id = extract_account_id(tokens) or creds.get("accountId")
    if account_id:
        new_creds["accountId"] = account_id
    _save_codex_credential(new_creds)
    return new_creds


def get_codex_access_token() -> tuple[str, str | None]:
    """Return ``(access_token, account_id)`` for an OAuth credential.

    Refreshes transparently when the cached token is about to expire.
    Public so the CLI can sanity-check connectivity (``/connect`` post-flow
    summary, future ``aru auth status`` etc.) without going through httpx.
    """
    creds = _refresh_if_needed(_load_codex_credential())
    return creds["access"], creds.get("accountId")


class CodexAuth(httpx.Auth):
    """``httpx.Auth`` subclass that injects Codex headers per request.

    Used by :mod:`aru.providers` when the openai credential is of type
    ``oauth``. Responsibilities:

    * strip whatever ``Authorization`` header the OpenAI SDK set (it uses
      the dummy key),
    * write a fresh ``Authorization: Bearer <access>`` (refreshing the token
      on the fly when needed),
    * add ``ChatGPT-Account-Id`` and ``originator: aru`` so the request
      looks like a legitimate ChatGPT-CLI call.

    The refresh path goes through :func:`refresh_codex_tokens` synchronously
    via ``urllib`` — it's rare (~once per hour) so the blocking call is
    fine, and httpx will run the sync flow from a thread for async clients
    automatically (see the base class ``async_auth_flow`` implementation).
    """

    # Inherits requires_request_body / requires_response_body = False from
    # the base class — we never need the body to compute the header.

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _apply(self, request: httpx.Request) -> None:
        # httpx headers are case-insensitive but request construction can
        # leave duplicates if both casings got set upstream — wipe both.
        for key in ("Authorization", "authorization"):
            try:
                del request.headers[key]
            except KeyError:
                pass

        with self._lock:
            access, account_id = get_codex_access_token()
        request.headers["Authorization"] = f"Bearer {access}"
        if account_id:
            request.headers["ChatGPT-Account-Id"] = account_id
        request.headers.setdefault("originator", "aru")
        # OpenAI's Codex backend tags requests with this for retry dedup;
        # keep it stable per process.
        request.headers.setdefault("session_id", _PROCESS_SESSION_ID)

    def auth_flow(self, request: httpx.Request):
        self._apply(request)
        yield request


# Process-wide session id — Codex tags requests with this so its servers can
# de-dup retries. Doesn't need to map to anything meaningful for us.
_PROCESS_SESSION_ID = _base64url_encode(secrets.token_bytes(16))


__all__ = [
    "CODEX_API_BASE",
    "CODEX_API_ENDPOINT",
    "CLIENT_ID",
    "ISSUER",
    "OAUTH_PORT",
    "OAUTH_REDIRECT_URI",
    "OAUTH_DUMMY_KEY",
    "CodexAuth",
    "CodexAuthFlow",
    "PkceCodes",
    "await_codex_callback",
    "build_authorize_url",
    "exchange_code_for_tokens",
    "extract_account_id",
    "extract_account_id_from_claims",
    "generate_pkce",
    "get_codex_access_token",
    "parse_jwt_claims",
    "refresh_codex_tokens",
    "start_codex_oauth_flow",
    "stop_codex_oauth_flow",
]

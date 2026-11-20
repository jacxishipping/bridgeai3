"""Generic OIDC login for the control plane (dormant unless configured).

Configure with environment variables:
  AI_BRIDGE_OIDC_ISSUER        e.g. https://accounts.google.com
  AI_BRIDGE_OIDC_CLIENT_ID
  AI_BRIDGE_OIDC_CLIENT_SECRET
  AI_BRIDGE_OIDC_REDIRECT_URI e.g. https://your-domain/api/auth/oidc/callback
  AI_BRIDGE_OIDC_DASHBOARD_URL e.g. https://your-domain/ (login landing page)

Flow: dashboard -> GET /api/auth/oidc/login -> provider -> callback ->
ID-token verified against the provider JWKS (RS256, iss/aud/exp/nonce) ->
local user matched by verified email or OIDC subject ->
session token -> redirect to the dashboard with ``?oidc_code=`` (a
single-use, 60 s exchange code) -> dashboard POSTs the code to
``/api/auth/oidc/exchange`` and gets the session token back in a JSON
body. The token itself never appears in a URL: the old
``#session_token=`` fragment redirect was removed in Wave 4 (F-05-15).

Only SHA-256 hashes of session tokens are stored. No provider secrets are
logged. Without all four variables set, OIDC is disabled and the login
route returns 400.

Wave 3A (F-05-15): the ``#session_token=`` fragment redirect is
removed — a fragment survives in browser history and is visible to
any script on the page. The replacement is a one-time exchange code:
:func:`mint_oidc_exchange_code` stores a single-use, 60 s code (the
session token itself, in the same DB that already holds the session
hash, deleted on first read) and :func:`redeem_oidc_exchange_code`
swaps it for the token over a POST body, so the token never appears in
a URL at all. Wave 4 wired the server side (``oidc_callback`` mints
the code, ``POST /api/auth/oidc/exchange`` redeems it); the dashboard
half (``consumeIdentityRedirect`` in ``apps/dashboard/src/api.ts``)
is a sibling-track change — see ``analysis/wave4-track-a.md`` for the
exact contract.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import secrets
import threading
import time

import httpx

log = logging.getLogger("ai_bridge.oidc")

_DISCOVERY_CACHE: dict = {"at": 0.0, "doc": None}
_JWKS_CACHE: dict = {"at": 0.0, "uri": "", "jwks": None}
_JWKS_TTL_SEC = 300.0
_PENDING: dict[str, dict] = {}

# Durable OIDC pending-login store (Wave 2A / DIST-04, TST-15).
# When a DB connection is bound via bind_oidc_store(), pending logins live
# in the shared `settings` table (key "oidc.pending.<state>") so a callback
# landing on a different worker than the login still finds its state.
# With no connection bound, the in-memory dict below is used (dev/test).
_PENDING_TTL_SEC = 600
_PENDING_KEY_PREFIX = "oidc.pending."
_PENDING_STORE_CONN = None
_store_lock = threading.RLock()

# One-time OIDC exchange codes (Wave 3A / F-05-15). Same store binding as
# the pending-login store (bind_oidc_store); same in-memory fallback when
# unbound. A code is a random 256-bit value; only its SHA-256 is used as
# the settings key, and the row is deleted on first read (single-use).
_EXCHANGE_TTL_SEC = 60
_EXCHANGE_KEY_PREFIX = "oidc.exchange."
_EXCHANGE: dict[str, tuple[str, float]] = {}  # code-hash -> (token, issued_at)


def bind_oidc_store(conn) -> None:
    """Bind (or, with None, unbind) the shared DB connection for pending logins.

    The parent wires this in create_app() with the plane connection. The
    store is optional: without it, new_login()/pop_pending() behave exactly
    as before, using process memory.
    """
    global _PENDING_STORE_CONN
    with _store_lock:
        _PENDING_STORE_CONN = conn


def _prune_store_locked(conn, now: float) -> None:
    """Delete expired/malformed pending rows. Caller must hold _store_lock."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (_PENDING_KEY_PREFIX + "%",),
        ).fetchall()
    except Exception:
        return
    for row in rows:
        key = row[0]
        raw = row[1]
        try:
            at = float(json.loads(str(raw)).get("at") or 0)
        except Exception:
            at = 0.0
        if now - at > _PENDING_TTL_SEC:
            try:
                conn.execute("DELETE FROM settings WHERE key=?", (key,))
            except Exception:
                pass


def _store_pending(state: str, record: dict) -> None:
    """Persist one pending login (durable store when bound, else memory)."""
    conn = _PENDING_STORE_CONN
    if conn is None:
        _PENDING[state] = record
        # Prune stale attempts.
        for key in [k for k, v in _PENDING.items() if time.time() - v["at"] > _PENDING_TTL_SEC]:
            _PENDING.pop(key, None)
        return
    try:
        payload = {"nonce": record["nonce"], "at": record["at"]}
        if record.get("code_verifier"):
            payload["code_verifier"] = record["code_verifier"]
        with _store_lock:
            _prune_store_locked(conn, time.time())
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    _PENDING_KEY_PREFIX + state,
                    json.dumps(payload),
                ),
            )
            conn.commit()
    except Exception:
        log.warning("oidc: durable pending store failed; falling back to memory", exc_info=True)
        _PENDING[state] = record


def _atomic_delete_setting(conn, key: str) -> str | None:
    """Atomically delete ``key`` from settings and return its value (cross-worker safe).

    Uses single-statement ``DELETE FROM settings WHERE key=? RETURNING value``
    (supported on SQLite 3.35+ and PostgreSQL), ensuring two concurrent workers
    cannot both redeem the same OIDC state or one-time exchange code.
    """
    row = conn.execute("DELETE FROM settings WHERE key=? RETURNING value", (key,)).fetchone()
    if row is None:
        return None
    return str(row["value"] if hasattr(row, "keys") else row[0])


def _pop_pending_store(state: str) -> dict | None:
    """Atomically consume one pending login; None when absent/expired."""
    conn = _PENDING_STORE_CONN
    if conn is None:
        return _PENDING.pop(state, None)
    try:
        with _store_lock:
            raw = _atomic_delete_setting(conn, _PENDING_KEY_PREFIX + state)
            _prune_store_locked(conn, time.time())
            conn.commit()
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict) or "nonce" not in data or "at" not in data:
            return None
        out: dict = {"nonce": data["nonce"], "at": float(data["at"])}
        if data.get("code_verifier"):
            out["code_verifier"] = str(data["code_verifier"])
        return out
    except Exception:
        log.warning("oidc: durable pending pop failed; falling back to memory", exc_info=True)
        return _PENDING.pop(state, None)


def oidc_config() -> dict | None:
    issuer = os.environ.get("AI_BRIDGE_OIDC_ISSUER", "").rstrip("/")
    client_id = os.environ.get("AI_BRIDGE_OIDC_CLIENT_ID", "")
    client_secret = os.environ.get("AI_BRIDGE_OIDC_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("AI_BRIDGE_OIDC_REDIRECT_URI", "")
    if not (issuer and client_id and client_secret and redirect_uri):
        return None
    return {
        "issuer": issuer,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "dashboard_url": os.environ.get("AI_BRIDGE_OIDC_DASHBOARD_URL", "/"),
    }


def discovery() -> dict:
    cfg = oidc_config()
    assert cfg is not None
    now = time.time()
    if _DISCOVERY_CACHE["doc"] and now - _DISCOVERY_CACHE["at"] < 3600:
        return _DISCOVERY_CACHE["doc"]
    url = cfg["issuer"] + "/.well-known/openid-configuration"
    with httpx.Client(timeout=15) as client:
        resp = client.get(url)
        resp.raise_for_status()
        doc = resp.json()
    _DISCOVERY_CACHE["at"] = now
    _DISCOVERY_CACHE["doc"] = doc
    return doc


def new_login() -> tuple[str, str, str]:
    """Return (authorize_url, state, nonce) for a fresh PKCE-protected login attempt."""
    import hashlib

    cfg = oidc_config()
    assert cfg is not None
    doc = discovery()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    _store_pending(state, {"nonce": nonce, "at": time.time(), "code_verifier": code_verifier})
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    from urllib.parse import urlencode

    return doc["authorization_endpoint"] + "?" + urlencode(params), state, nonce


def _fetch_jwks(jwks_uri: str, *, force_refresh: bool = False) -> dict:
    now = time.time()
    if not force_refresh and _JWKS_CACHE["jwks"] and _JWKS_CACHE["uri"] == jwks_uri and (now - _JWKS_CACHE["at"] < _JWKS_TTL_SEC):
        return _JWKS_CACHE["jwks"]
    with httpx.Client(timeout=15) as client:
        jwks_resp = client.get(jwks_uri)
        jwks_resp.raise_for_status()
        jwks = jwks_resp.json()
    _JWKS_CACHE["at"] = now
    _JWKS_CACHE["uri"] = jwks_uri
    _JWKS_CACHE["jwks"] = jwks
    return jwks


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4))


def verify_id_token(id_token: str, expected_nonce: str) -> dict:
    """Verify an RS256 ID token against the provider JWKS. Returns claims."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    cfg = oidc_config()
    assert cfg is not None
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed ID token")
    header_b64, payload_b64, sig_b64 = parts
    header = json.loads(_b64url(header_b64.encode()))
    claims = json.loads(_b64url(payload_b64.encode()))
    if header.get("alg") != "RS256":
        raise ValueError("unsupported ID token alg")
    doc = discovery()
    jwks = _fetch_jwks(doc["jwks_uri"])
    key = None
    for candidate in jwks.get("keys", []):
        if candidate.get("kid") == header.get("kid") and candidate.get("kty") == "RSA":
            key = candidate
            break
    if key is None:
        # Provider may have rotated keys; refresh JWKS once on cache miss.
        jwks = _fetch_jwks(doc["jwks_uri"], force_refresh=True)
        for candidate in jwks.get("keys", []):
            if candidate.get("kid") == header.get("kid") and candidate.get("kty") == "RSA":
                key = candidate
                break
    if key is None:
        raise ValueError("signing key not found")
    numbers = rsa.RSAPublicNumbers(
        int.from_bytes(_b64url(key["e"].encode()), "big"),
        int.from_bytes(_b64url(key["n"].encode()), "big"),
    )
    public_key = numbers.public_key()
    signing_input = (header_b64 + "." + payload_b64).encode()
    try:
        public_key.verify(
            _b64url(sig_b64.encode()),
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature:
        raise ValueError("bad ID token signature")
    now = time.time()
    if claims.get("iss") != cfg["issuer"]:
        raise ValueError("bad issuer")
    aud = claims.get("aud")
    aud_ok = (cfg["client_id"] in aud) if isinstance(aud, list) else (aud == cfg["client_id"])
    if not aud_ok:
        raise ValueError("bad audience")
    if claims.get("exp", 0) < now:
        raise ValueError("ID token expired")
    if claims.get("nonce") != expected_nonce:
        raise ValueError("bad nonce")
    return claims


def pop_pending(state: str) -> dict | None:
    record = _pop_pending_store(state)
    if record is None or time.time() - record.get("at", 0) > _PENDING_TTL_SEC:
        return None
    return record


def _exchange_code_hash(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.encode()).hexdigest()


def mint_oidc_exchange_code(session_token: str, *, ttl_s: int = _EXCHANGE_TTL_SEC) -> str:
    """Mint a one-time code redeemable for ``session_token`` (F-05-15).

    Returns the code (hand it to the browser ONCE, e.g. as a query param
    on the dashboard redirect — the code, not the token, is what travels
    in the URL). The code is single-use with a 60 s TTL; the stored row
    is deleted on first read, expired rows are pruned on mint/redeem.
    Uses the bound OIDC store when one is bound (cross-worker safe),
    else process memory (dev/test, same fallback as pending logins).

    Residual, stated plainly: the session token value rests in the
    settings table for at most 60 s until redeemed — the same database
    that already holds password hashes and session-token hashes. That is
    strictly better than the fragment it replaces (which persists in
    browser history indefinitely and is readable by page scripts), and
    matches how OAuth authorization codes are stored server-side.
    """
    if not session_token:
        raise ValueError("session_token is required")
    code = secrets.token_urlsafe(32)
    code_hash = _exchange_code_hash(code)
    now = time.time()
    conn = _PENDING_STORE_CONN
    if conn is None:
        _EXCHANGE[code_hash] = (session_token, now)
        for key in [k for k, (_, at) in _EXCHANGE.items() if now - at > _EXCHANGE_TTL_SEC]:
            _EXCHANGE.pop(key, None)
        return code
    try:
        with _store_lock:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE ?",
                (_EXCHANGE_KEY_PREFIX + "%",),
            ).fetchall()
            for row in rows:
                try:
                    at = float(json.loads(str(row[1])).get("at") or 0)
                except Exception:
                    at = 0.0
                if now - at > _EXCHANGE_TTL_SEC:
                    with contextlib.suppress(Exception):
                        conn.execute("DELETE FROM settings WHERE key=?", (row[0],))
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    _EXCHANGE_KEY_PREFIX + code_hash,
                    json.dumps({"token": session_token, "at": now}),
                ),
            )
            conn.commit()
    except Exception:
        log.warning("oidc: durable exchange store failed; falling back to memory", exc_info=True)
        _EXCHANGE[code_hash] = (session_token, now)
    return code


def redeem_oidc_exchange_code(code: str) -> str | None:
    """Swap a one-time exchange code for its session token, or None.

    Single-use: the row is deleted before the token is returned, so a
    replayed code finds nothing. Expired codes (older than 60 s) and
    unknown codes return None. The caller (POST /api/auth/oidc/exchange)
    returns the token in a JSON body — never in a URL.
    """
    if not code:
        return None
    code_hash = _exchange_code_hash(code)
    now = time.time()
    conn = _PENDING_STORE_CONN
    if conn is None:
        token, at = _EXCHANGE.pop(code_hash, (None, 0.0))
        if token is None or now - at > _EXCHANGE_TTL_SEC:
            return None
        return token
    try:
        with _store_lock:
            raw = _atomic_delete_setting(conn, _EXCHANGE_KEY_PREFIX + code_hash)
            conn.commit()
        if raw is None:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or "token" not in data or "at" not in data:
            return None
        if now - float(data["at"]) > _EXCHANGE_TTL_SEC:
            return None
        return str(data["token"])
    except Exception:
        log.warning("oidc: durable exchange redeem failed", exc_info=True)
        token, at = _EXCHANGE.pop(code_hash, (None, 0.0))
        if token is None or now - at > _EXCHANGE_TTL_SEC:
            return None
        return token

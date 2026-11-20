"""Tests for OAuth gateway device binding.

The gateway's DB path is env-overridable (AI_BRIDGE_OAUTH_DB) so tests
run against a throwaway SQLite file instead of /etc/ai-bridge-oauth.
"""
import base64
import hashlib
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB = Path(tempfile.mkdtemp(prefix="aibridge-oauth-binding-")) / "ai-bridge-test-oauth.db"
os.environ["AI_BRIDGE_OAUTH_DB"] = str(DB)
os.environ["AI_BRIDGE_ADMIN_TOKEN"] = "test-admin"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deployment" / "oauth"))

import gateway  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _fresh_client():
    gateway.DB_PATH = DB
    if DB.exists():
        try:
            DB.unlink()
        except OSError:
            pass
    gateway.init_db()
    return TestClient(gateway.app, raise_server_exceptions=False)


def _register(client):
    r = client.post("/oauth/register", json={"redirect_uris": ["https://example.com/cb"],
                                       "client_name": "test-app"})
    assert r.status_code == 200, r.text
    return r.json()["client_id"]


def _pkce():
    """A valid (verifier, S256 challenge) pair for the authorize flow."""
    verifier = "test-verifier-0123456789abcdef"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _approve(client, client_id, device_token="", verifier=None, challenge=None):
    if verifier is None:
        verifier, challenge = _pkce()
    return client.post(
        "/oauth/authorize",
        data={"client_id": client_id, "redirect_uri": "https://example.com/cb",
              "scope": "", "state": "s", "code_challenge": challenge,
              "code_challenge_method": "S256",
              "admin_token": "test-admin", "device_token": device_token,
              "action": "approve"},
        follow_redirects=False,
    )


def _binding(client_id):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT device_id FROM clients WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def test_approve_binds_device_and_verify_returns_it(monkeypatch):
    client = _fresh_client()
    cid = _register(client)
    monkeypatch.setattr(gateway, "resolve_device_token",
                        lambda t: "dev-123" if t == "good-token" else None)
    r = _approve(client, cid, device_token="good-token")
    assert r.status_code == 302, r.text
    assert "code=" in r.headers["location"]
    assert _binding(cid) == "dev-123"

    now = int(time.time())
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO tokens (token_hash, kind, client_id, scope, created_at, expires_at, revoked)"
                 " VALUES (?, 'access', ?, '', ?, ?, 0)",
                 (gateway.digest("tok-1"), cid, now, now + 3600))
    conn.commit()
    conn.close()
    r = client.get("/internal/verify", headers={"Authorization": "Bearer tok-1"})
    assert r.status_code == 200
    assert r.json()["device_id"] == "dev-123"


def test_approve_with_admin_token_only_succeeds():
    client = _fresh_client()
    cid = _register(client)
    r = _approve(client, cid, device_token="")
    assert r.status_code == 302, r.text
    assert "code=" in r.headers["location"]
    assert _binding(cid) in ("", None)

    now = int(time.time())
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO tokens (token_hash, kind, client_id, scope, created_at, expires_at, revoked)"
                 " VALUES (?, 'access', ?, '', ?, ?, 0)",
                 (gateway.digest("tok-unbound"), cid, now, now + 3600))
    conn.commit()
    conn.close()
    r = client.get("/internal/verify", headers={"Authorization": "Bearer tok-unbound"})
    assert r.status_code == 200
    assert r.json()["device_id"] is None


def test_invalid_admin_token_rejected():
    client = _fresh_client()
    cid = _register(client)
    r = client.post(
        "/oauth/authorize",
        data={"client_id": cid, "redirect_uri": "https://example.com/cb",
              "admin_token": "wrong-token", "action": "approve"},
    )
    assert r.status_code == 401
    assert "Invalid administrator token" in r.text
    assert _binding(cid) in ("", None)


def test_approve_with_optional_device_token(monkeypatch):
    client = _fresh_client()
    cid = _register(client)
    monkeypatch.setattr(gateway, "resolve_device_token",
                        lambda t: "dev-123" if t == "good-token" else None)
    r = _approve(client, cid, device_token="good-token")
    assert r.status_code == 302
    assert _binding(cid) == "dev-123"


def test_admin_token_verify_is_unbound():
    client = _fresh_client()
    r = client.get("/internal/verify", headers={"Authorization": "Bearer test-admin"})
    assert r.status_code == 200
    assert r.json()["device_id"] is None


# ---------------------------------------------------------------- F-06-12 (Wave 5A): PKCE is mandatory

def test_authorize_post_without_pkce_rejected():
    client = _fresh_client()
    cid = _register(client)
    r = client.post(
        "/oauth/authorize",
        data={"client_id": cid, "redirect_uri": "https://example.com/cb",
              "scope": "", "state": "s", "code_challenge": "",
              "code_challenge_method": "",
              "admin_token": "test-admin", "action": "approve"},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert "error=invalid_request" in r.headers["location"]
    assert "code=" not in r.headers["location"]


def test_authorize_get_without_pkce_rejected():
    client = _fresh_client()
    cid = _register(client)
    r = client.get(
        "/oauth/authorize",
        params={"client_id": cid, "redirect_uri": "https://example.com/cb",
                "response_type": "code", "state": "s"},
    )
    assert r.status_code == 400
    assert "PKCE" in r.text


def test_token_exchange_requires_correct_pkce_verifier():
    client = _fresh_client()
    cid = _register(client)
    verifier, challenge = _pkce()
    r = _approve(client, cid, verifier=verifier, challenge=challenge)
    assert r.status_code == 302, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]

    # Wrong verifier -> invalid_grant, and the code is single-use (consumed).
    bad = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://example.com/cb", "client_id": cid,
        "code_verifier": "wrong-verifier",
    })
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_grant"

    # Fresh code + correct verifier -> tokens.
    verifier2, challenge2 = _pkce()
    r = _approve(client, cid, verifier=verifier2, challenge=challenge2)
    code2 = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    ok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code2,
        "redirect_uri": "https://example.com/cb", "client_id": cid,
        "code_verifier": verifier2,
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["access_token"]
    assert ok.json()["refresh_token"]


def test_token_exchange_without_verifier_rejected():
    client = _fresh_client()
    cid = _register(client)
    verifier, challenge = _pkce()
    r = _approve(client, cid, verifier=verifier, challenge=challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://example.com/cb", "client_id": cid,
        "code_verifier": "",
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_pkce_ok_rejects_missing_challenge_or_verifier():
    assert gateway.pkce_ok("anything", "", "S256") is False
    assert gateway.pkce_ok("", "some-challenge", "S256") is False
    assert gateway.pkce_ok("v", "c", "plain") is False


def test_register_rejects_ip_literal_userinfo_and_fragment_redirects():
    client = _fresh_client()
    for bad_uri in (
        "https://203.0.113.7/cb",          # non-loopback IP literal
        "https://user:pass@example.com/cb",  # userinfo smuggling
        "https://example.com/cb#frag",      # fragment leaks the code
        "http://example.com/cb",            # plaintext, non-loopback
    ):
        r = client.post("/oauth/register", json={"redirect_uris": [bad_uri]})
        assert r.status_code == 400, bad_uri
        assert r.json()["error"] == "invalid_redirect_uri"
    # Loopback http stays allowed (RFC 8252 §7.3); normal https still works.
    for good_uri in ("http://127.0.0.1:8080/cb", "https://example.com/cb"):
        r = client.post("/oauth/register", json={"redirect_uris": [good_uri]})
        assert r.status_code == 200, good_uri

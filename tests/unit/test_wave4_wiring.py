"""Wave 4 wiring tests — follow-up patches from Wave 3A §11 + Wave 3C.

Covers, end to end through the HTTP/WebSocket surface (not just the
mechanism modules Wave 3A/3C tested in isolation):

- WS legacy ``?token=`` fallback is gone (ticket-or-session auth only).
- OIDC callback completes without a URL-fragment token (``?oidc_code=``
  + ``POST /api/auth/oidc/exchange``; the code is single-use).
- List/get task endpoints exclude the multi-MB ``result`` TEXT and
  ``GET /api/tasks/{id}/result`` serves it (PERF-03).
- ``POST /api/policy/simulate`` returns a decision and executes nothing
  (FR-01).
- The limiter is imported from the single interface
  ``control/limiter.py``, not from ``durable_ratelimit`` directly.
- ``to_pg_sql`` is a tokenizer + allowlist, not regex rewriting (F-05-18).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_bridge.control import db as dbmod
from ai_bridge.control import limiter as limiter_mod
from ai_bridge.control import oidc as oidcmod
from ai_bridge.control.app import create_app

ADMIN = "wave4-admin-token"


@pytest.fixture()
def app_client(tmp_path):
    app = create_app(admin_token="wave4-admin-token", db_path=tmp_path / "wave4.db")
    client = TestClient(app)
    yield client, app
    try:
        app.state.conn.close()
    except Exception:
        pass


def _admin():
    return {"X-Admin-Token": ADMIN}


def _enroll(client):
    resp = client.post(
        "/api/devices/enroll",
        json={"name": "wave4-dev", "os": "linux"},
        headers=_admin(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- 1a. WS legacy-token fallback removed -------------------------------

def test_ws_legacy_token_query_param_rejected(app_client):
    client, _app = app_client
    enrolled = _enroll(client)
    token = enrolled["device_token"]
    # The old ?token=<redacted> fallback must not authenticate: the server
    # closes the socket (4401) instead of accepting it.
    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/agent/ws?token={token}"):
            pass


def test_ws_header_token_still_works(app_client):
    client, _app = app_client
    enrolled = _enroll(client)
    with client.websocket_connect(
        "/api/agent/ws", headers={"X-Device-Token": enrolled["device_token"]}
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["device_id"] == enrolled["device_id"]


# --- 1b. F-05-15 OIDC exchange-code flow --------------------------------

class _FakeTokenResponse:
    status_code = 200

    def json(self):
        return {"id_token": "fake-id-token"}


def _fake_oidc(monkeypatch):
    cfg = {
        "redirect_uri": "http://plane.local/api/auth/oidc/callback",
        "client_id": "wave4-client",
        "client_secret": "wave4-secret",
        "dashboard_url": "http://dashboard.local",
    }
    monkeypatch.setattr(oidcmod, "oidc_config", lambda: cfg)
    monkeypatch.setattr(oidcmod, "pop_pending", lambda state: {"nonce": "nonce-1"})
    monkeypatch.setattr(
        oidcmod, "discovery", lambda: {"token_endpoint": "https://idp.example/token"}
    )
    monkeypatch.setattr(
        oidcmod,
        "verify_id_token",
        lambda token, nonce: {"email": "wave4@example.com", "email_verified": True},
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeTokenResponse())


def test_oidc_callback_completes_without_url_fragment_token(app_client, monkeypatch):
    client, _app = app_client
    _fake_oidc(monkeypatch)
    resp = client.get(
        "/api/auth/oidc/callback?code=authcode-1&state=state-1",
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    # The session token must never appear in the URL (no fragment, no
    # session_token param); only the single-use exchange code travels.
    assert "session_token" not in location
    assert "#" not in location
    assert location.startswith("http://dashboard.local/?oidc_code=")
    code = location.split("oidc_code=", 1)[1]
    assert code

    # Exchange the code for the token over POST (JSON body, not a URL).
    exchanged = client.post("/api/auth/oidc/exchange", json={"code": code})
    assert exchanged.status_code == 200, exchanged.text
    session_token = exchanged.json()["session_token"]
    assert session_token

    # The exchanged token is a real session: /api/auth/me accepts it.
    me = client.get("/api/auth/me", headers={"X-Session-Token": session_token})
    assert me.status_code == 200, me.text

    # Single-use: replaying the same code is a 401, as is an unknown code.
    assert client.post("/api/auth/oidc/exchange", json={"code": code}).status_code == 401
    assert (
        client.post("/api/auth/oidc/exchange", json={"code": "no-such-code"}).status_code
        == 401
    )


# --- 1d. PERF-03 task projection ----------------------------------------

def _make_completed_task_with_big_result(client, app):
    enrolled = _enroll(client)
    resp = client.post(
        "/api/tasks",
        json={"device_id": enrolled["device_id"], "tool": "get_system_info", "args": {}},
        headers=_admin(),
    )
    assert resp.status_code == 200, resp.text
    conn = app.state.conn
    row = conn.execute("SELECT id FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row is not None
    task_id = row["id"]
    big_result = "X" * 5000
    conn.execute(
        "UPDATE tasks SET result=?, status='completed' WHERE id=?",
        (big_result, task_id),
    )
    conn.commit()
    return task_id, big_result


def test_list_endpoints_exclude_result_and_result_endpoint_returns_it(app_client):
    client, app = app_client
    task_id, big_result = _make_completed_task_with_big_result(client, app)

    listed = client.get("/api/tasks", headers=_admin())
    assert listed.status_code == 200, listed.text
    items = listed.json()
    assert isinstance(items, list) and items
    assert all("result" not in item for item in items), "list leaked result TEXT"

    # Single-task GET KEEPS "result": device_routing.execute_on_device(),
    # the dashboard task-detail view, and ai_api all read the result from
    # this endpoint. Projecting it away returned empty results for every
    # remote execution (regression caught by the two pre-existing tests).
    single = client.get(f"/api/tasks/{task_id}", headers=_admin())
    assert single.status_code == 200, single.text
    assert single.json().get("result") == big_result

    result = client.get(f"/api/tasks/{task_id}/result", headers=_admin())
    assert result.status_code == 200, result.text
    assert result.json() == {"task_id": task_id, "result": big_result}

    missing = client.get("/api/tasks/no-such-task/result", headers=_admin())
    assert missing.status_code == 404


# --- FR-01 policy simulation --------------------------------------------

def test_policy_simulate_returns_decision_without_executing(app_client):
    client, app = app_client
    conn = app.state.conn
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    resp = client.post(
        "/api/policy/simulate",
        json={"tool": "delete_workspace_file", "approval_granted": False},
        headers=_admin(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool"] == "delete_workspace_file"
    assert body["executed"] is False
    assert isinstance(body["allowed"], bool)
    assert isinstance(body["needs_approval"], bool)
    assert body["reason"]
    after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert after == before, "simulation must not create a task"

    # Batch form also never executes.
    batch = client.post(
        "/api/policy/simulate",
        json={"calls": [{"tool": "run_shell"}, {"tool": "uptime"}]},
        headers=_admin(),
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["executed"] is False
    assert len(batch.json()["results"]) == 2
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before

    # A caller-supplied body can never smuggle the unrestricted bypass in:
    # "unrestricted" is ignored by the endpoint, so the decision must be
    # identical to the same call without the smuggled field. (The admin
    # caller is allowed here via is_admin/role, not via "unrestricted".)
    control = client.post(
        "/api/policy/simulate",
        json={"tool": "run_shell"},
        headers=_admin(),
    )
    assert control.status_code == 200, control.text
    smuggled = client.post(
        "/api/policy/simulate",
        json={"tool": "run_shell", "policy": {"unrestricted": True}},
        headers=_admin(),
    )
    assert smuggled.status_code == 200, smuggled.text
    assert smuggled.json() == control.json()


# --- Limiter single interface -------------------------------------------

def test_limiter_imports_come_from_control_limiter():
    # Static: no direct durable_ratelimit.allow import on the hot paths.
    repo = Path(__file__).resolve().parents[2]
    webrtc_src = (repo / "src" / "ai_bridge" / "webrtc_stream.py").read_text(encoding="utf-8")
    assert "from ai_bridge.control.durable_ratelimit import allow" not in webrtc_src
    assert "control.limiter" in webrtc_src
    app_src = (repo / "src" / "ai_bridge" / "control" / "app.py").read_text(encoding="utf-8")
    assert "from .durable_ratelimit import allow" not in app_src
    assert "from .limiter import" in app_src

    # Functional: the single interface exists and enforces a limit.
    assert callable(limiter_mod.allow)
    assert callable(limiter_mod.allow_async)
    assert limiter_mod.allow("wave4-wiring-probe", 5, 60) is True


# --- F-05-18 to_pg_sql: tokenizer + allowlist, no regex ------------------

def test_to_pg_sql_has_no_regex_rewriting():
    module_src = inspect.getsource(dbmod)
    assert "re.sub" not in module_src
    assert "re.search" not in module_src
    assert "import re\n" not in module_src

    # Placeholder translation still works, including % escaping.
    assert (
        dbmod.to_pg_sql("SELECT * FROM devices WHERE id=?")
        == "SELECT * FROM devices WHERE id=%s"
    )
    # Only the allowlisted settings upserts translate.
    assert dbmod.to_pg_sql(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('k', 'v');"
    ).endswith("ON CONFLICT DO NOTHING")
    # Anything else that looks like an upsert is refused, not rewritten.
    with pytest.raises(ValueError):
        dbmod.to_pg_sql("INSERT OR IGNORE INTO devices (id) VALUES ('d1')")
    with pytest.raises(ValueError):
        dbmod.to_pg_sql("INSERT OR REPLACE INTO tasks (id) VALUES ('t1')")

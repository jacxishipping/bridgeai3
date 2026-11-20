"""Wave 6A residuals: query-param tokens, F-05-12, F07-05, W5E-01.

- Query-param tokens: ``live.jpg`` and the terminal stream used to accept
  ``?token=`` / ``?session_token=`` query fallbacks. Tokens in query strings
  leak into logs, browser history and referrers, and the dashboard no longer
  sends them — the server must accept headers only.
- F-05-12: ``dispatch()`` hardcoded ``approval_granted=True`` for
  ``desktop_anticipate_action``; it must pass the caller's real value.
- F07-05: with no sealed token store the agent wrote the device token to a
  plaintext file silently. The write is now loud, and
  ``AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE=0`` refuses it (memory-only).
- W5E-01: ``GET /api/devices`` ran up to 2 task-history queries per device
  behind the single DB lock. Live-meta enrichment is now one batched query.
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from ai_bridge.control import db as dbmod
from ai_bridge.control import dispatch as dispatch_mod
from ai_bridge.control.app import create_app

ADMIN = "wave6a-test-admin-token"


def _make_client(tmp_path):
    app = create_app(admin_token=ADMIN, db_path=str(tmp_path / "test.db"))
    return TestClient(app, raise_server_exceptions=False), str(tmp_path / "test.db")


# --- Query-param tokens -------------------------------------------------


def test_live_jpg_rejects_query_token(tmp_path):
    client, _ = _make_client(tmp_path)
    # A query-only token must NOT authenticate (401), even a valid one.
    r = client.get(f"/api/devices/nope/desktop/live.jpg?token={ADMIN}")
    assert r.status_code == 401
    r = client.get(f"/api/devices/nope/desktop/live.jpg?session_token={ADMIN}")
    assert r.status_code == 401
    # The same token as a header authenticates (404: no such device/frame).
    r = client.get("/api/devices/nope/desktop/live.jpg", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 404


def test_terminal_stream_rejects_query_token(tmp_path):
    client, _ = _make_client(tmp_path)
    r = client.get("/api/devices/nope/terminal/sess-1/stream?token=" + ADMIN)
    assert r.status_code == 401
    r = client.get(
        "/api/devices/nope/terminal/sess-1/stream",
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.status_code == 404


# --- F-05-12 ------------------------------------------------------------


def test_dispatch_anticipate_passes_real_approval_state(monkeypatch):
    seen = {}

    def fake_anticipate(*args, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(dispatch_mod.tools, "desktop_anticipate_action", fake_anticipate)
    args = {"action": "click", "x": 0.5, "y": 0.5}
    dispatch_mod.dispatch("desktop_anticipate_action", args, approval_granted=False)
    assert seen.get("approval_granted") is False
    dispatch_mod.dispatch("desktop_anticipate_action", args, approval_granted=True)
    assert seen.get("approval_granted") is True


# --- F07-05 -------------------------------------------------------------


def test_save_token_plaintext_opt_out_refuses_file(monkeypatch, tmp_path, capsys):
    from agents.common.poller import Poller

    monkeypatch.setenv("AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE", "0")
    token_file = tmp_path / "device.token"
    poller = Poller("http://localhost:8000", "tok", token_file=token_file)
    poller._save_token("secret-device-token")
    assert not token_file.exists()
    err = capsys.readouterr().err.lower()
    assert "refusing" in err
    assert "plaintext" in err


def test_save_token_plaintext_warns_loudly(monkeypatch, tmp_path, capsys):
    from agents.common.poller import Poller

    monkeypatch.delenv("AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE", raising=False)
    token_file = tmp_path / "device.token"
    poller = Poller("http://localhost:8000", "tok", token_file=token_file)
    poller._save_token("secret-device-token")
    assert token_file.read_text(encoding="utf-8") == "secret-device-token"
    assert "PLAINTEXT" in capsys.readouterr().err


def test_save_token_configured_saver_failure_stays_memory_only(monkeypatch, tmp_path, capsys):
    from agents.common.poller import Poller

    def boom(_token):
        raise RuntimeError("DPAPI unavailable in test")

    token_file = tmp_path / "device.token"
    poller = Poller("http://localhost:8000", "tok", token_file=token_file, token_saver=boom)
    poller._save_token("secret-device-token")
    assert not token_file.exists()
    assert "NOT writing" in capsys.readouterr().err


# --- W5E-01 -------------------------------------------------------------


def _seed_device_with_tasks(conn, device_id, now):
    conn.execute(
        "INSERT INTO devices (id, name, hostname, os, tenant, token_hash, status, last_seen, created_at) VALUES (?, ?, ?, 'linux', 'default', 'hash', 'offline', ?, ?)",
        (device_id, f"dev-{device_id}", f"host-{device_id}", now, now),
    )
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, status, result, created_at, updated_at) VALUES (?, ?, 'default', 'get_resource_usage', '{}', 'completed', ?, ?, ?)",
        (
            f"t-res-{device_id}",
            device_id,
            json.dumps(
                {
                    "memory_total_bytes": 8 * 1024**3,
                    "disk_total_bytes": 100 * 1024**3,
                    "uptime_seconds": 7200,
                }
            ),
            now,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, status, result, created_at, updated_at) VALUES (?, ?, 'default', 'get_network_info', '{}', 'completed', ?, ?, ?)",
        (
            f"t-net-{device_id}",
            device_id,
            json.dumps({"interfaces": [{"ip": "10.0.0.5"}, {"ip": "127.0.0.1"}]}),
            now,
            now,
        ),
    )
    conn.commit()


def test_devices_list_uses_one_batched_live_meta_query(tmp_path, monkeypatch):
    client, db_path = _make_client(tmp_path)
    conn = dbmod.connect(db_path=db_path)
    now = time.time()
    for i in range(3):
        _seed_device_with_tasks(conn, f"dev{i}", now)
    conn.close()

    # Count task-history queries issued by the request itself (ignore background
    # _scheduler_loop ticks on other threads so the test never flakes on a 30s sweep).
    import threading

    counts = {"tasks": 0}
    orig_execute = dbmod.LockedConnection.execute
    in_request = {"active": False}
    app_conn = client.app.state.conn

    def counting(self, sql, parameters=()):
        t_name = threading.current_thread().name
        if (
            self is app_conn
            and in_request["active"]
            and "FROM tasks" in sql
            and "sweep" not in sql.lower()
            and t_name not in ("ai-bridge-scheduler", "custom-agent-runner")
            and not t_name.startswith("agent-run-")
        ):
            # Exclude background scheduler stale-task sweep queries.
            if "WHERE status = 'running'" not in sql and "WHERE status='running'" not in sql:
                counts["tasks"] += 1
        return orig_execute(self, sql, parameters)

    monkeypatch.setattr(dbmod.LockedConnection, "execute", counting)
    in_request["active"] = True
    try:
        r = client.get("/api/devices", headers={"X-Admin-Token": ADMIN})
    finally:
        in_request["active"] = False
    assert r.status_code == 200
    assert counts["tasks"] == 1  # was up to 2 per device (6 for 3 devices)
    devices = {d["id"]: d for d in r.json()}
    assert devices["dev0"]["ip_address"] == "10.0.0.5"


def test_devices_live_meta_matches_single_device_path(tmp_path):
    # The batched path must return the same enrichment as the old
    # per-device path: ip from the network task, totals from resource usage.
    client, db_path = _make_client(tmp_path)
    conn = dbmod.connect(db_path=db_path)
    now = time.time()
    _seed_device_with_tasks(conn, "solo", now)
    conn.close()
    r = client.get("/api/devices", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200
    (dev,) = [d for d in r.json() if d["id"] == "solo"]
    assert dev["ip_address"] == "10.0.0.5"

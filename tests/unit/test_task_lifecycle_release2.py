"""Release 2 — Task lifecycle tests (progress heartbeats, retries, backoff, DLQ, failure reasons, retention)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai_bridge.control import db as dbmod
from ai_bridge.control import orchestration
from ai_bridge.control.app import create_app
from ai_bridge.policy import is_tool_idempotent

ADMIN = "r2-lifecycle-admin-token"


def _admin():
    return {"X-Admin-Token": ADMIN}


def test_tool_idempotency_classification():
    assert is_tool_idempotent("get_system_info") is True
    assert is_tool_idempotent("list_processes") is True
    assert is_tool_idempotent("take_screenshot") is True
    assert is_tool_idempotent("run_shell") is False
    assert is_tool_idempotent("delete_workspace_file") is False
    assert is_tool_idempotent("mouse_click") is False
    assert is_tool_idempotent("interact_and_view_screen") is False
    assert is_tool_idempotent("unknown_tool") is False


def test_classify_failure_reasons():
    assert orchestration.classify_failure("cancelled", "")[0] == "cancelled"
    assert orchestration.classify_failure("timed_out", "")[0] == "timeout"
    assert orchestration.classify_failure("failed", json.dumps({"error": "503 temporarily unavailable"}))[0] == "transient"
    assert orchestration.classify_failure("failed", json.dumps({"error": "Tool denied by policy"}))[0] == "policy"
    assert orchestration.classify_failure("failed", json.dumps({"error": "Invalid JSON syntax"}))[0] == "permanent"


def test_progress_persists_pct_message_and_extends_lease(tmp_path):
    app = create_app(admin_token=ADMIN, db_path=tmp_path / "prog.db")
    client = TestClient(app)
    enr = client.post("/api/devices/enroll", json={"name": "p-box", "os": "linux"}, headers=_admin()).json()
    dev_id, dev_tok = enr["device_id"], enr["device_token"]

    t = client.post("/api/tasks", json={"device_id": dev_id, "tool": "get_system_info", "args": {}}, headers=_admin()).json()
    tid = t["task_id"]
    claimed = client.get("/api/agent/tasks/next", params={"wait": 0}, headers={"X-Device-Token": dev_tok}).json()["task"]
    assert claimed["id"] == tid

    p = client.post(
        f"/api/agent/tasks/{tid}/progress",
        json={"task_id": tid, "epoch": claimed["epoch"], "progress_pct": 42.5, "status_message": "Collecting diagnostics"},
        headers={"X-Device-Token": dev_tok},
    )
    assert p.status_code == 200
    assert p.json()["progress_pct"] == 42.5

    detail = client.get(f"/api/tasks/{tid}", headers=_admin()).json()
    assert detail["progress_pct"] == 42.5
    assert detail["status_message"] == "Collecting diagnostics"
    assert detail["attempt"] == 1
    assert detail["started_at"] is not None
    assert detail["lease_expires_at"] is not None

    events = client.get(f"/api/tasks/{tid}/events", headers=_admin()).json()["events"]
    ev_names = [e["event"] for e in events]
    assert "created" in ev_names
    assert "running" in ev_names
    assert "progress" in ev_names


def test_transient_retry_backoff_and_dead_letter_replay(tmp_path):
    app = create_app(admin_token=ADMIN, db_path=tmp_path / "dlq.db")
    client = TestClient(app)
    enr = client.post("/api/devices/enroll", json={"name": "r-box", "os": "linux"}, headers=_admin()).json()
    dev_id, dev_tok = enr["device_id"], enr["device_token"]

    # Create idempotent task with max_attempts=2
    t = client.post(
        "/api/tasks",
        json={"device_id": dev_id, "tool": "get_system_info", "args": {}, "max_attempts": 2},
        headers=_admin(),
    ).json()
    tid = t["task_id"]

    # Attempt 1 claimed and fails transiently -> automatically scheduled for retry with backoff
    c1 = client.get("/api/agent/tasks/next", params={"wait": 0}, headers={"X-Device-Token": dev_tok}).json()["task"]
    assert c1["id"] == tid
    res1 = client.post(
        f"/api/agent/tasks/{tid}/result",
        json={"status": "failed", "epoch": c1["epoch"], "result": json.dumps({"error": "network timeout 503"})},
        headers={"X-Device-Token": dev_tok},
    )
    assert res1.status_code == 200
    mid = client.get(f"/api/tasks/{tid}", headers=_admin()).json()
    assert mid["status"] == "queued"
    assert mid["failure_class"] == "transient"
    assert mid["next_attempt_at"] is not None

    # Before backoff expires, next_task does NOT return the delayed retry
    early = client.get("/api/agent/tasks/next", params={"wait": 0}, headers={"X-Device-Token": dev_tok}).json()
    assert early["task"] is None

    # Advance next_attempt_at into the past so attempt 2 is claimable
    app.state.conn.execute("UPDATE tasks SET next_attempt_at=? WHERE id=?", (dbmod.now() - 1.0, tid))
    app.state.conn.commit()

    c2 = client.get("/api/agent/tasks/next", params={"wait": 0}, headers={"X-Device-Token": dev_tok}).json()["task"]
    assert c2["id"] == tid
    assert c2["epoch"] > c1["epoch"]

    # Attempt 2 fails transiently -> retries exhausted -> failed + dead-lettered!
    res2 = client.post(
        f"/api/agent/tasks/{tid}/result",
        json={"status": "failed", "epoch": c2["epoch"], "result": json.dumps({"error": "connection reset"})},
        headers={"X-Device-Token": dev_tok},
    )
    assert res2.status_code == 200
    done = client.get(f"/api/tasks/{tid}", headers=_admin()).json()
    assert done["status"] == "failed"
    assert done["dead_lettered"] == 1
    assert done["failure_class"] == "transient"

    # List DLQ, replay it, and verify a new task is queued and DLQ is marked replayed
    dlq_list = client.get("/api/tasks/dead-letter", headers=_admin()).json()
    assert dlq_list["count"] == 1
    dlq_id = dlq_list["items"][0]["id"]
    assert dlq_list["items"][0]["task_id"] == tid

    replayed = client.post(f"/api/tasks/dead-letter/{dlq_id}/replay", headers=_admin())
    assert replayed.status_code == 200
    new_tid = replayed.json()["task_id"]
    assert new_tid != tid

    # Second replay returns 409; pending DLQ list is now empty
    assert client.post(f"/api/tasks/dead-letter/{dlq_id}/replay", headers=_admin()).status_code == 409
    assert client.get("/api/tasks/dead-letter", headers=_admin()).json()["count"] == 0

    # Admin can purge the replayed DLQ entry
    assert client.delete(f"/api/tasks/dead-letter/{dlq_id}", headers=_admin()).status_code == 200


def test_stale_lease_requeues_idempotent_with_budget_and_dead_letters_exhausted(tmp_path):
    conn = dbmod.connect(db_path=tmp_path / "stale.db")
    old = dbmod.now() - 2000
    # Task 1: idempotent with retry budget left -> requeued with backoff
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, tier, status, created_at, updated_at, attempt, max_attempts, lease_expires_at)"
        " VALUES ('t-retry','d1','default','get_system_info','{}',1,'running',?,?,1,3,?)",
        (old, old, old + 60),
    )
    # Task 2: non-idempotent even with max_attempts=3 -> NOT auto-requeued, timed_out + dead_lettered
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, tier, status, created_at, updated_at, attempt, max_attempts, lease_expires_at)"
        " VALUES ('t-mut','d1','default','run_shell','{}',3,'running',?,?,1,3,?)",
        (old, old, old + 60),
    )
    conn.commit()

    stats = orchestration.sweep_stale_running_tasks(conn, lease_sec=900)
    assert stats["requeued"] == 1
    assert stats["timed_out"] == 1
    assert stats["dead_lettered"] == 1

    r1 = conn.execute("SELECT status, next_attempt_at, failure_class FROM tasks WHERE id='t-retry'").fetchone()
    assert r1["status"] == "queued"
    assert r1["next_attempt_at"] is not None
    assert r1["failure_class"] == "agent_crash"

    r2 = conn.execute("SELECT status, dead_lettered, failure_class FROM tasks WHERE id='t-mut'").fetchone()
    assert r2["status"] == "timed_out"
    assert r2["dead_lettered"] == 1
    assert r2["failure_class"] == "timeout"


def test_retention_sweep_from_env_prunes_sessions_tickets_events_tasks(tmp_path, monkeypatch):
    conn = dbmod.connect(db_path=tmp_path / "ret.db")
    old = dbmod.now() - 100 * 86400
    conn.execute(
        "INSERT INTO users (id, username, password_hash, salt, role, tenant, created_at) VALUES ('u1','alice','h','s','viewer','default',?)",
        (old,),
    )
    conn.execute(
        "INSERT INTO sessions (token_hash, username, role, tenant, expires_at, created_at) VALUES ('s-old','alice','viewer','default',?,?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO stream_tickets (ticket_hash, actor, tenant, device_id, session_id, expires_at, used, created_at) VALUES ('tk-old','alice','default','d1','s1',?,0,?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, tier, status, created_at, updated_at) VALUES ('t-old','d1','default','get_system_info','{}',1,'completed',?,?)",
        (old, old),
    )
    conn.commit()

    monkeypatch.setenv("AI_BRIDGE_RETENTION_TASKS_DAYS", "30")
    monkeypatch.setenv("AI_BRIDGE_RETENTION_TASK_EVENTS_DAYS", "14")
    counts = dbmod.retention_sweep_from_env(conn)
    assert counts["sessions"] == 1
    assert counts["stream_tickets"] == 1
    assert counts["tasks"] == 1

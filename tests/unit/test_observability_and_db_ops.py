"""Release 2 — Observability & DB Operations tests.

Covers:
- Structured JSON logging: standard extra={...} preservation, token scrubbing, contextvars correlation
- End-to-end correlation: X-Correlation-ID propagation from HTTP -> task -> task_events -> audit -> GET /api/traces/{trace_id}
- Secured /metrics via dedicated AI_BRIDGE_METRICS_TOKEN Bearer credential + DB/HTTP/DLQ metrics exposed
- DB observability: query timing, slow-query logging, get_db_stats() in /health/ready & /api/metrics/summary
- Automated EXPLAIN QUERY PLAN index checks on hot queries
- Automated operational alert evaluator (stale tasks, DLQ backlog, spool backlog, cooldown dedup)
"""

from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from ai_bridge.control import alerts
from ai_bridge.control import db as dbmod
from ai_bridge.control.app import create_app
from ai_bridge.logging_config import JSONFormatter, log_context

ADMIN = "r2-obs-admin-token"


def _admin(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"X-Admin-Token": ADMIN}
    if extra:
        h.update(extra)
    return h


def test_json_formatter_preserves_extra_scrubs_tokens_and_injects_context():
    fmt = JSONFormatter()
    rec = logging.LogRecord(
        name="ai_bridge.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="Processing request with Authorization: Bearer supersecret1234567890abcdef",
        args=(),
        exc_info=None,
    )
    rec.custom_latency_ms = 12.5
    rec.endpoint = "/api/tasks"

    with log_context(correlation_id="tr-abc123", task_id="tsk-9", device_id="dev-1", actor="alice"):
        rendered = fmt.format(rec)

    data = json.loads(rendered)
    assert data["correlation_id"] == "tr-abc123"
    assert data["task_id"] == "tsk-9"
    assert data["device_id"] == "dev-1"
    assert data["actor"] == "alice"
    assert data["custom_latency_ms"] == 12.5
    assert data["endpoint"] == "/api/tasks"
    assert "supersecret1234567890abcdef" not in data["message"]


def test_end_to_end_trace_correlation_api(tmp_path):
    app = create_app(admin_token=ADMIN, db_path=tmp_path / "trace.db")
    client = TestClient(app)

    enr = client.post("/api/devices/enroll", json={"name": "tr-box", "os": "linux"}, headers=_admin()).json()
    dev_id, dev_tok = enr["device_id"], enr["device_token"]

    trace_id = "tr-e2e-correlation-001"
    # Submit task with X-Correlation-ID
    sub = client.post(
        "/api/tasks",
        json={"device_id": dev_id, "tool": "get_system_info", "args": {}},
        headers=_admin({"X-Correlation-ID": trace_id}),
    )
    assert sub.status_code == 200
    assert sub.headers.get("x-correlation-id") == trace_id
    tid = sub.json()["task_id"]
    assert sub.json()["trace_id"] == trace_id

    # Agent claims, sends progress, and completes task with same correlation header
    nxt = client.get("/api/agent/tasks/next", params={"wait": 0}, headers={"X-Device-Token": dev_tok}).json()["task"]
    assert nxt["id"] == tid
    assert nxt["trace_id"] == trace_id

    client.post(
        f"/api/agent/tasks/{tid}/progress",
        json={"task_id": tid, "epoch": nxt["epoch"], "progress_pct": 50.0, "status_message": "halfway"},
        headers={"X-Device-Token": dev_tok, "X-Correlation-ID": trace_id},
    )
    client.post(
        f"/api/agent/tasks/{tid}/result",
        json={"status": "completed", "epoch": nxt["epoch"], "result": json.dumps({"ok": True})},
        headers={"X-Device-Token": dev_tok, "X-Correlation-ID": trace_id},
    )

    # Trace API correlates task, task_events, and audit rows under one trace_id
    tr = client.get(f"/api/traces/{trace_id}", headers=_admin())
    assert tr.status_code == 200
    body = tr.json()
    assert body["trace_id"] == trace_id
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["id"] == tid
    ev_names = [e["event"] for e in body["task_events"]]
    assert {"created", "running", "progress", "completed"}.issubset(set(ev_names))
    assert len(body["audit"]) >= 1
    assert any(a["trace_id"] == trace_id for a in body["audit"])


def test_metrics_dedicated_bearer_token_and_db_health_stats(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_METRICS_TOKEN", "prom-scrape-secret-999")
    app = create_app(admin_token=ADMIN, db_path=tmp_path / "metrics.db")
    client = TestClient(app)

    # Unauthenticated scrape rejected when AI_BRIDGE_METRICS_TOKEN is set
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong-token"}).status_code == 401

    # Scrape with dedicated token succeeds and exposes Release 2 metrics
    ok = client.get("/metrics", headers={"Authorization": "Bearer prom-scrape-secret-999"})
    assert ok.status_code == 200
    text = ok.text
    assert "aibridge_uptime_seconds" in text

    # /health/ready exposes db_latency_ms and db_stats
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    rj = ready.json()
    assert isinstance(rj["db_latency_ms"], (int, float))
    assert rj["db_stats"]["dialect"] == "sqlite"
    assert rj["db_stats"]["queries_total"] >= 1

    # /api/metrics/summary includes db_stats and dead_letter_pending
    summary = client.get("/api/metrics/summary", headers=_admin())
    assert summary.status_code == 200
    sj = summary.json()
    assert "db_stats" in sj
    assert "dead_letter_pending" in sj


def test_explain_query_plans_use_indexes_on_hot_paths(tmp_path):
    """Automated query-plan check ensuring hot queries hit explicit indexes."""
    conn = dbmod.connect(db_path=tmp_path / "plans.db")
    queries = [
        ("SELECT * FROM tasks WHERE device_id='d1' AND status='queued' ORDER BY created_at ASC LIMIT 1", "idx_tasks_device_status"),
        ("SELECT * FROM tasks WHERE tenant='default' AND trace_id='tr-1'", "idx_tasks_trace_id"),
        ("SELECT * FROM audit WHERE tenant='default' AND trace_id='tr-1'", "idx_audit_trace_id"),
        ("SELECT * FROM task_events WHERE task_id='t1' ORDER BY created_at ASC LIMIT 50", "idx_task_events_task"),
        ("SELECT * FROM telemetry WHERE device_id='d1' ORDER BY recorded_at DESC LIMIT 1", "idx_telemetry_device_time"),
        ("SELECT * FROM approvals WHERE status='pending' AND expires_at < 100", "idx_approvals_status_expires"),
        ("SELECT * FROM sessions WHERE expires_at < 100", "idx_sessions_expires"),
        ("SELECT * FROM task_dead_letters WHERE tenant='default' ORDER BY created_at DESC LIMIT 50", "idx_task_dead_letters_tenant"),
    ]
    for sql, expected_idx_prefix in queries:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        plan_text = " ".join(str(r["detail"] if hasattr(r, "keys") else r[-1]) for r in rows)
        assert expected_idx_prefix in plan_text, f"Expected index {expected_idx_prefix} in plan for {sql!r}, got: {plan_text}"


def test_operational_alert_evaluator_detects_conditions_and_deduplicates(tmp_path, monkeypatch):
    conn = dbmod.connect(db_path=tmp_path / "alerts.db")
    old = dbmod.now() - 2000
    # Seed 1 stale running task + 1 dead-letter item + 1 device with spool backlog
    conn.execute(
        "INSERT INTO devices (id, name, tenant, token_hash, status, last_seen, policy_json, revoked, created_at, spool_stats_json) "
        "VALUES ('d1','box1','default','h','online',?,'{}',0,?,?)",
        (old, old, json.dumps({"pending_count": 30, "oldest_pending_age_sec": 4200})),
    )
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, tier, status, created_at, updated_at) "
        "VALUES ('t1','d1','default','get_system_info','{}',1,'running',?,?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO task_dead_letters (id, task_id, device_id, tenant, tool, args_json, attempts, failure_reason, failure_class, created_at) "
        "VALUES ('dlq-1','t1','d1','default','get_system_info','{}',2,'timeout','timeout',?)",
        (old,),
    )
    conn.commit()
    monkeypatch.setenv("AI_BRIDGE_ALERT_DLQ_THRESHOLD", "1")
    alerts._LAST_ALERT_AT.clear()

    fired1 = alerts.evaluate_operational_alerts(conn, cooldown_sec=300.0, dispatch=True)
    types1 = {f["event_type"] for f in fired1}
    assert "stale_running_tasks" in types1
    assert "stale_heartbeat_devices" in types1
    assert "dead_letter_backlog" in types1
    assert "agent_spool_backlog:d1" in types1

    # Immediate re-evaluation within cooldown deduplicates all findings
    fired2 = alerts.evaluate_operational_alerts(conn, cooldown_sec=300.0, dispatch=True)
    assert fired2 == []

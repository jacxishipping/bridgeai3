"""Release 2 — Agent resilience, spool visibility, drift detection, and rollout rings tests."""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from agents.common.poller import ControlPlaneHTTPError, Poller
from agents.common.spool import DLQ_REASON_PLANE_REJECTED, TaskSpooler
from ai_bridge.control.app import create_app
from ai_bridge.policy import TOOL_TIERS, Policy, RiskTier, decide
from ai_bridge.tools.fleet import rollback_fleet_agent

ADMIN = "r2-resilience-admin-token"


def _admin():
    return {"X-Admin-Token": ADMIN}


def test_spooler_epoch_trace_stats_and_terminal_rejection(tmp_path):
    spool = TaskSpooler(tmp_path / "spool.db")
    spool.spool("t-ok", "completed", '{"ok":true}', epoch=2, trace_id="tr-1")
    spool.spool("t-stale", "completed", '{"ok":true}', epoch=1, trace_id="tr-2")

    st = spool.stats()
    assert st["pending_count"] == 2
    assert st["dead_letter_count"] == 0
    assert st["total_bytes"] > 0
    assert st["oldest_pending_age_sec"] >= 0.0

    seen = []

    def sender(tid, status, result, epoch, trace_id):
        seen.append((tid, epoch, trace_id))
        if tid == "t-stale":
            return "terminal_rejected"
        return True

    flushed = spool.flush(sender)
    assert flushed == 1
    assert ("t-ok", 2, "tr-1") in seen
    assert ("t-stale", 1, "tr-2") in seen

    st2 = spool.stats()
    assert st2["pending_count"] == 0
    assert st2["dead_letter_count"] == 1
    dlq = spool.get_dead_letter()
    assert dlq[0]["task_id"] == "t-stale"
    assert dlq[0]["reason"] == DLQ_REASON_PLANE_REJECTED


def test_poller_heartbeat_reports_spool_poll_state_and_drift_in_fleet_health(tmp_path, monkeypatch):
    app = create_app(admin_token=ADMIN, db_path=tmp_path / "fleet.db")
    client = TestClient(app)
    enr = client.post("/api/devices/enroll", json={"name": "edge-1", "os": "linux"}, headers=_admin()).json()
    dev_id, dev_tok = enr["device_id"], enr["device_token"]

    # Simulate heartbeat with Release 2 resilience fields
    hb = client.post(
        "/api/agent/heartbeat",
        json={
            "hostname": "edge-1",
            "os": "Linux 6.8",
            "agent_version": "0.2.9",
            "agent_commit": "abcdef1",
            "policy_hash": "deadbeef00001111",
            "poll_state": {"ws_connected": False, "poll_mode": "http_fallback", "backoff_attempts": 2, "reconnect_count": 3},
            "spool_stats": {"pending_count": 4, "oldest_pending_age_sec": 120.5, "total_bytes": 1024, "dead_letter_count": 1},
        },
        headers={"X-Device-Token": dev_tok},
    )
    assert hb.status_code == 200

    health = client.get("/api/fleet/health", headers=_admin()).json()
    assert health["spool_pending_total"] == 4
    assert health["spool_max_age_sec"] == 120.5
    assert health["version_drift_count"] == 1
    assert health["policy_drift_count"] == 1
    dev = next(d for d in health["devices"] if d["id"] == dev_id)
    assert dev["version_drift"] is True
    assert dev["policy_drift"] is True
    assert dev["poll_state"]["poll_mode"] == "http_fallback"
    assert dev["spool_stats"]["pending_count"] == 4
    assert dev["update_ring"] == "general"

    # Fleet version no longer reports hardcoded 1.30.0
    ver = client.get("/api/fleet/version", headers=_admin()).json()
    assert ver["control_plane_version"] != "1.30.0"


def test_poller_try_post_result_marks_409_as_terminal_rejected(tmp_path):
    poller = Poller("http://127.0.0.1:9999", "tok-123", token_file=tmp_path / "device.token")

    def fake_req(method, path, payload=None, timeout=15):
        assert payload["epoch"] == 7
        assert poller._current_task_trace_id == "tr-99"
        raise ControlPlaneHTTPError(409, path, "Stale result: epoch mismatch")

    poller._req = fake_req  # type: ignore[method-assign]
    assert poller._try_post_result("tsk-1", "completed", "{}", epoch=7, trace_id="tr-99") == "terminal_rejected"


def test_staged_update_rollout_rings_pause_health_gate_and_rollback(tmp_path):
    app = create_app(admin_token=ADMIN, db_path=tmp_path / "rollouts.db")
    client = TestClient(app)

    d_canary = client.post("/api/devices/enroll", json={"name": "box-canary", "os": "linux"}, headers=_admin()).json()["device_id"]
    d_early = client.post("/api/devices/enroll", json={"name": "box-early", "os": "linux"}, headers=_admin()).json()["device_id"]

    # Assign rings
    assert client.put(f"/api/devices/{d_canary}/ring", json={"ring": "canary"}, headers=_admin()).status_code == 200
    assert client.put(f"/api/devices/{d_early}/ring", json={"ring": "early"}, headers=_admin()).status_code == 200
    assert client.put(f"/api/devices/{d_early}/ring", json={"ring": "bogus"}, headers=_admin()).status_code == 400

    # Create staged rollout -> dispatches canary ring immediately
    created = client.post(
        "/api/fleet/rollouts",
        json={"name": "v0.3.1 rollout", "target_version": "0.3.1", "rings": ["canary", "early"], "max_failure_rate": 0.25},
        headers=_admin(),
    )
    assert created.status_code == 200
    rol = created.json()
    rid = rol["id"]
    assert rol["current_ring"] == "canary"
    assert rol["status"] == "active"
    assert len(rol["dispatched"]["canary"]) == 1
    canary_task_id = rol["dispatched"]["canary"][0]["task_id"]
    assert canary_task_id

    # Pause and resume
    assert client.post(f"/api/fleet/rollouts/{rid}/pause", headers=_admin()).json()["status"] == "paused"
    assert client.post(f"/api/fleet/rollouts/{rid}/advance", headers=_admin()).status_code == 409
    assert client.post(f"/api/fleet/rollouts/{rid}/resume", headers=_admin()).json()["status"] == "active"

    # Simulate canary failure -> advance auto-pauses due to health gate!
    app.state.conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (canary_task_id,))
    app.state.conn.commit()
    adv_fail = client.post(f"/api/fleet/rollouts/{rid}/advance", headers=_admin()).json()
    assert adv_fail["health_gate_failed"] is True
    assert adv_fail["status"] == "paused"

    # Resume, mark canary completed, advance -> moves to early ring
    client.post(f"/api/fleet/rollouts/{rid}/resume", headers=_admin())
    app.state.conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (canary_task_id,))
    app.state.conn.commit()
    adv_ok = client.post(f"/api/fleet/rollouts/{rid}/advance", headers=_admin()).json()
    assert adv_ok["health_gate_failed"] is False
    assert adv_ok["current_ring"] == "early"
    assert len(adv_ok["dispatched"]["early"]) == 1

    # Rollback -> dispatches rollback_fleet_agent to both touched devices
    rb = client.post(f"/api/fleet/rollouts/{rid}/rollback", headers=_admin()).json()
    assert rb["status"] == "rolled_back"
    rb_devs = {e["device_id"] for e in rb["dispatched"]["rollback"]}
    assert rb_devs == {d_canary, d_early}


def test_rollback_fleet_agent_policy_and_git_checkout(tmp_path):
    # Policy check: rollback_fleet_agent is Tier 3 (SENSITIVE) — requires approval by default
    p_default = Policy()
    assert TOOL_TIERS["rollback_fleet_agent"] == RiskTier.SENSITIVE
    dec = decide(p_default, "rollback_fleet_agent", approval_granted=False)
    assert dec.needs_approval is True

    # Create a real temporary git repo mimicking an ai-bridge checkout
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "ai-bridge"\nversion = "0.3.0"\n', encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@example.test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=repo, check=True, capture_output=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    (repo / "marker.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=repo, check=True, capture_output=True)

    rb_meta = tmp_path / "update-rollback.json"
    rb_meta.write_text(json.dumps({"prev_commit": c1}), encoding="utf-8")

    # Without approval, policy refuses
    with pytest.raises(PermissionError):
        rollback_fleet_agent(approval_granted=False, policy=p_default, repo_dir=repo, rollback_file=rb_meta)

    # With explicit approval, checks out prev_commit cleanly
    res = rollback_fleet_agent(approval_granted=True, policy=p_default, repo_dir=repo, rollback_file=rb_meta)
    assert res["ok"] is True
    assert res["rolled_back_to"] == c1
    head_now = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert head_now == c1

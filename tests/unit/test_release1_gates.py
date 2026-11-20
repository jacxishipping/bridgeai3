"""Release 1 verification, policy-hardening, and release-gate unit tests.

Covers:
  - Policy tier hardening from the Release 1 security review (`write_pc_file`
    Tier 4 / HIGH_RISK, `list_directory` Tier 3 / SENSITIVE, full-PC and
    window-automation tools tiered in both `TOOL_TIERS` and `TOOL_REGISTRY`).
  - Database restore drill (`scripts/backup_restore.py::drill_sqlite`):
    migrations count, core tables, audit hash-chain continuity, app boot,
    and RTO/RPO evidence recording.
  - macOS launchd plist rendering (`scripts/macos_launchd_e2e.py::render_launchd_plist`).
  - WebRTC agent-side view-only, takeover, and disconnect held-key release
    (`scripts/webrtc_browser_e2e.py::_agent_enforcement_checks`).
  - Dashboard smoke check (`deployment/smoke_check.py`) alignment with v3
    dashboard HTML (`apps/dashboard/index.html`).
"""
from __future__ import annotations

import json
import plistlib
from pathlib import Path

from fastapi.testclient import TestClient

from ai_bridge import policy, tool_registry
from ai_bridge.control.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_full_pc_and_window_tools_policy_tiers_and_registry():
    # Arbitrary PC write must be Tier 4 (HIGH_RISK) and require approval even when Tier 3 is on.
    assert policy.TOOL_TIERS["write_pc_file"] == policy.RiskTier.HIGH_RISK
    assert policy.TOOL_TIERS["run_powershell"] == policy.RiskTier.HIGH_RISK
    assert policy.TOOL_TIERS["kill_process"] == policy.RiskTier.HIGH_RISK

    # Arbitrary directory listing & file reads outside workspace must be at least Tier 3 (SENSITIVE).
    assert policy.TOOL_TIERS["list_directory"] == policy.RiskTier.SENSITIVE
    assert policy.TOOL_TIERS["read_pc_file"] == policy.RiskTier.SENSITIVE

    # Window/UI mutating tools must be Tier 3 (SENSITIVE); read-only active window is Tier 1.
    for tool_name in (
        "focus_window",
        "maximize_window",
        "minimize_window",
        "restore_window",
        "close_window",
        "move_resize_window",
        "invoke_element",
        "set_element_value",
        "focus_element",
    ):
        assert policy.TOOL_TIERS[tool_name] == policy.RiskTier.SENSITIVE
        entry = tool_registry.get_tool(tool_name)
        assert entry is not None and entry.requires_approval is True

    assert policy.TOOL_TIERS["get_active_window"] == policy.RiskTier.READ_ONLY

    # Default policy denies unattended Tier 3/4 tools (needs approval).
    default_pol = policy.Policy()
    d_list = policy.decide(default_pol, "list_directory", approval_granted=False)
    assert d_list.allowed is False and d_list.needs_approval is True

    tier3_pol = policy.Policy(allow_writes=True, allow_tier3=True, max_tier_unattended=3)
    d_write = policy.decide(tier3_pol, "write_pc_file", approval_granted=False)
    assert d_write.allowed is False and d_write.needs_approval is True


def test_sqlite_restore_drill_verifies_migrations_audit_chain_and_rto(tmp_path):
    from scripts.backup_restore import drill_sqlite, expected_migration_count

    db_path = tmp_path / "source.db"
    app = create_app(admin_token="drill-adm", db_path=db_path)
    try:
        client = TestClient(app)
        r = client.post(
            "/api/devices/enroll",
            json={"name": "drill-node", "os": "Linux"},
            headers={"X-Admin-Token": "drill-adm"},
        )
        assert r.status_code == 200
        dev_id = r.json()["device_id"]
        r2 = client.post(
            "/api/tasks",
            json={"device_id": dev_id, "tool": "get_system_info", "args": {}},
            headers={"X-Admin-Token": "drill-adm"},
        )
        assert r2.status_code == 200
    finally:
        app.state.conn.close()

    evidence_path = tmp_path / "evidence.json"
    report = drill_sqlite(db_path, tmp_path / "work", evidence_out=evidence_path)
    assert report["ok"] is True
    assert report["migrations_applied"] == expected_migration_count()
    assert report["migrations_ok"] is True
    assert report["audit_chain_valid"] is True
    assert report["table_counts"]["devices"] == 1
    assert report["table_counts"]["tasks"] == 1
    assert report["table_counts"]["audit"] >= 1
    assert report["app_boot"]["ok"] is True
    assert report["rto_seconds"] >= 0.0
    assert report["rpo_seconds"] >= 0.0
    saved = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert saved["ok"] is True


def test_macos_launchd_plist_rendering(tmp_path):
    from scripts.macos_launchd_e2e import render_launchd_plist

    template = REPO_ROOT / "deployment" / "macos" / "com.aibridge.agent.plist"
    raw = render_launchd_plist(
        template,
        python_exe="/usr/bin/python3",
        repo_root=REPO_ROOT,
        control_url="https://plane.example.test",
        device_token="tok-test-123",
        log_dir=tmp_path,
    )
    parsed = plistlib.loads(raw)
    assert parsed["Label"] == "com.aibridge.agent.e2e"
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["EnvironmentVariables"]["AI_BRIDGE_DEVICE_TOKEN"] == "tok-test-123"
    assert "--control-url" in parsed["ProgramArguments"]
    assert "https://plane.example.test" in parsed["ProgramArguments"]
    assert parsed["StandardOutPath"].endswith("ai-bridge-agent.out")


def test_webrtc_agent_enforcement_checks():
    from scripts.webrtc_browser_e2e import _agent_enforcement_checks

    res = _agent_enforcement_checks()
    assert res["ok"] is True
    assert res["view_only_blocked"] is True
    assert res["takeover_blocked"] is True
    assert res["ping_and_release_ok"] is True


def test_smoke_check_matches_v3_dashboard_index_html():
    index_html = (REPO_ROOT / "apps" / "dashboard" / "index.html").read_bytes()
    assert b'<div id="root">' in index_html
    assert b"AI Bridge" in index_html
    smoke_src = (REPO_ROOT / "deployment" / "smoke_check.py").read_text(encoding="utf-8")
    assert "brand-mark.svg" not in smoke_src


def test_postgres_pitr_drill_wal_scripts_generation(tmp_path):
    from scripts.postgres_pitr_drill import _write_wal_scripts

    archive_dir = tmp_path / "wal"
    archive_cmd, restore_cmd = _write_wal_scripts(tmp_path, archive_dir)
    assert "wal_archive.py" in archive_cmd and "%p" in archive_cmd and "%f" in archive_cmd
    assert "wal_restore.py" in restore_cmd and "%f" in restore_cmd and "%p" in restore_cmd
    assert (tmp_path / "wal_archive.py").is_file()
    assert (tmp_path / "wal_restore.py").is_file()


def test_poller_run_forever_marks_revoked_when_tasks_next_raises_revoked(monkeypatch):
    from agents.common.poller import DeviceRevokedError, Poller

    p = Poller("http://127.0.0.1:9", "revoked-tok")
    monkeypatch.setattr(p, "start_side_channel", lambda: None)
    monkeypatch.setattr(p, "start_heartbeat_loop", lambda: None)
    p._heartbeat_loop_running = True
    monkeypatch.setattr(
        p,
        "_req",
        lambda method, path, body=None, timeout=None: (_ for _ in ()).throw(
            DeviceRevokedError(401, path, "Unknown or revoked device token.")
        ),
    )
    out = p.run_forever()
    assert out["revoked"] is True
    assert p.revoked is True
    assert "revoked" in (out.get("revoked_reason") or "")


def test_independent_review_p0_ai_execute_local_approval_gate(tmp_path, monkeypatch):
    """P0 regression: unapproved approval_id must return 409 (not fall through),
    and omitted approval_id must cleanly insert a pending approval row."""
    monkeypatch.setenv("AI_BRIDGE_ALLOW_LOCAL_EXECUTION", "1")
    app = create_app(admin_token="adm-p0", db_path=tmp_path / "p0.db")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"X-Admin-Token": "adm-p0"}
        # 1. Omitting approval_id for Tier-3 local tool creates a pending approval cleanly
        r1 = client.post(
            "/api/ai/execute",
            json={"tool": "run_shell", "args": {"command": "echo hi"}, "device": "local"},
            headers=headers,
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert body1["status"] == "needs_approval"
        ap_id = body1["approval_id"]
        assert ap_id

        # 2. Supplying that STILL-PENDING approval_id must return 409 denied (not execute!)
        r2 = client.post(
            "/api/ai/execute",
            json={"tool": "run_shell", "args": {"command": "echo hi"}, "device": "local", "approval_id": ap_id},
            headers=headers,
        )
        assert r2.status_code == 409, r2.text
    finally:
        app.state.conn.close()


def test_independent_review_p0_tenant_isolation_frames_and_approvals(tmp_path):
    """P0/P1 regression: fleet desktop frames and approval delete must enforce tenant isolation."""
    app = create_app(admin_token="adm-ten", db_path=tmp_path / "ten.db")
    try:
        client = TestClient(app, raise_server_exceptions=False)
        adm = {"X-Admin-Token": "adm-ten"}
        # Create tenant-a and tenant-b admins + enroll one device each
        client.post("/api/auth/users", json={"username": "adma", "password": "Password123!", "role": "admin", "tenant": "tenant-a"}, headers=adm)
        client.post("/api/auth/users", json={"username": "admb", "password": "Password123!", "role": "admin", "tenant": "tenant-b"}, headers=adm)
        tok_a = client.post("/api/auth/login", json={"username": "adma", "password": "Password123!"}).json()["session_token"]
        tok_b = client.post("/api/auth/login", json={"username": "admb", "password": "Password123!"}).json()["session_token"]
        csrf_a = client.get("/api/auth/csrf-token", headers={"X-Session-Token": tok_a}).json()["csrf_token"]
        csrf_b = client.get("/api/auth/csrf-token", headers={"X-Session-Token": tok_b}).json()["csrf_token"]

        dev_b = client.post(
            "/api/devices/enroll",
            json={"name": "dev-b", "os": "Windows", "tenant": "tenant-b"},
            headers={"X-Session-Token": tok_b, "X-CSRF-Token": csrf_b},
        ).json()["device_id"]

        # Tenant A must NOT see Tenant B's device in /api/fleet/desktop/frames
        frames_a = client.get("/api/fleet/desktop/frames", headers={"X-Session-Token": tok_a})
        assert frames_a.status_code == 200
        assert all(d["device_id"] != dev_b for d in frames_a.json()["devices"])

        # Tenant A must NOT delete Tenant B's approval
        t3_b = client.post(
            "/api/tasks",
            json={"device_id": dev_b, "tool": "run_shell", "args": {"command": "echo b"}},
            headers={"X-Session-Token": tok_b, "X-CSRF-Token": csrf_b},
        ).json()
        del_cross = client.delete(
            f"/api/approvals/{t3_b['approval_id']}",
            headers={"X-Session-Token": tok_a, "X-CSRF-Token": csrf_a},
        )
        assert del_cross.status_code in (403, 404)

        # Spoofed X-Admin-Token alongside X-Session-Token must NOT bypass CSRF (403)
        spoofed = client.post(
            "/api/tasks",
            json={"device_id": dev_b, "tool": "get_system_info", "args": {}},
            headers={"X-Session-Token": tok_a, "X-Admin-Token": "fake-spoof"},
        )
        assert spoofed.status_code == 403
    finally:
        app.state.conn.close()


def test_independent_review_p1_drill_postgres_refuses_same_db():
    import pytest

    from scripts.backup_restore import _strip_dsn_password, drill_postgres

    with pytest.raises(ValueError, match="different database"):
        drill_postgres(
            "postgresql://u:secret@127.0.0.1:5432/live_db",
            "postgresql://u:secret@127.0.0.1:5432/live_db",
        )
    safe, env = _strip_dsn_password("postgresql://u:s3cr3t@db.example:5432/app")
    assert "s3cr3t" not in safe
    assert env["PGPASSWORD"] == "s3cr3t"

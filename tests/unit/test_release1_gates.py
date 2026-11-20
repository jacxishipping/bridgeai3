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

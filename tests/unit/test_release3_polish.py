"""Release 3 polish verification: pagination, docs generators, and dashboard truthfulness contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from ai_bridge.control.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "release3-admin-token"


def _make_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=str(tmp_path / "r3.db"), admin_token=TOKEN)
    return TestClient(app)


def test_devices_server_side_pagination_and_filters(tmp_path: Path):
    client = _make_client(tmp_path)
    headers = {"X-Admin-Token": TOKEN}

    enrolled = []
    specs = [
        ("win-alpha", "WIN-A", "Windows 11 Pro", "prod-win", ["finance", "primary"]),
        ("win-beta", "WIN-B", "Windows Server 2022", "prod-win", ["edge"]),
        ("linux-gamma", "LNX-G", "Ubuntu 24.04 LTS", "prod-linux", ["db"]),
        ("mac-delta", "MAC-D", "macOS Sequoia", "dev-mac", ["laptop"]),
    ]
    for name, host, os_name, group, tags in specs:
        r = client.post("/api/devices/enroll", headers=headers, json={"name": name, "hostname": host, "os": os_name})
        assert r.status_code == 200
        dev_id = r.json()["device_id"]
        enrolled.append((dev_id, name, group))
        m = client.put(f"/api/devices/{dev_id}/metadata", headers=headers, json={"group": group, "tags": tags})
        assert m.status_code == 200

    # Revoke one device to verify status filter
    rev_id = enrolled[1][0]
    assert client.post(f"/api/devices/{rev_id}/revoke", headers=headers).status_code == 200

    # Full list includes X-Total-Count=4
    all_resp = client.get("/api/devices", headers=headers)
    assert all_resp.status_code == 200
    assert all_resp.headers.get("X-Total-Count") == "4"
    assert len(all_resp.json()) == 4

    # Server-side pagination: limit=2, offset=0 and offset=2
    page1 = client.get("/api/devices?limit=2&offset=0", headers=headers)
    page2 = client.get("/api/devices?limit=2&offset=2", headers=headers)
    assert page1.status_code == 200 and page2.status_code == 200
    assert page1.headers.get("X-Total-Count") == "4"
    assert page2.headers.get("X-Total-Count") == "4"
    assert len(page1.json()) == 2
    assert len(page2.json()) == 2
    assert {d["id"] for d in page1.json()}.isdisjoint({d["id"] for d in page2.json()})

    # Filter by group
    grp = client.get("/api/devices?group=prod-win", headers=headers)
    assert grp.status_code == 200
    assert grp.headers.get("X-Total-Count") == "2"
    assert all(d["group"] == "prod-win" for d in grp.json())

    # Filter by OS
    os_r = client.get("/api/devices?os=ubuntu", headers=headers)
    assert os_r.status_code == 200
    assert os_r.headers.get("X-Total-Count") == "1"
    assert os_r.json()[0]["name"] == "linux-gamma"

    # Filter by status=revoked
    rev_r = client.get("/api/devices?status=revoked", headers=headers)
    assert rev_r.status_code == 200
    assert rev_r.headers.get("X-Total-Count") == "1"
    assert rev_r.json()[0]["id"] == rev_id

    # Free-text query matches tags too
    q_r = client.get("/api/devices?q=finance", headers=headers)
    assert q_r.status_code == 200
    assert q_r.headers.get("X-Total-Count") == "1"
    assert q_r.json()[0]["name"] == "win-alpha"


def test_tasks_server_side_pagination_and_status_filter(tmp_path: Path):
    client = _make_client(tmp_path)
    headers = {"X-Admin-Token": TOKEN}

    dev = client.post("/api/devices/enroll", headers=headers, json={"name": "node-1", "hostname": "N1", "os": "Linux"}).json()
    dev_id = dev["device_id"]

    task_ids = []
    for _ in range(4):
        res = client.post("/api/tasks", headers=headers, json={"device_id": dev_id, "tool": "get_system_info", "args": {}})
        assert res.status_code == 200
        task_ids.append(res.json()["task_id"])

    # Cancel one task so we have mixed statuses
    assert client.post(f"/api/tasks/{task_ids[0]}/cancel", headers=headers).status_code == 200

    p1 = client.get("/api/tasks?limit=2&offset=0", headers=headers)
    p2 = client.get("/api/tasks?limit=2&offset=2", headers=headers)
    assert p1.status_code == 200 and p2.status_code == 200
    assert p1.headers.get("X-Total-Count") == "4"
    assert p2.headers.get("X-Total-Count") == "4"
    assert len(p1.json()) == 2
    assert len(p2.json()) == 2
    assert {t["id"] for t in p1.json()}.isdisjoint({t["id"] for t in p2.json()})

    queued = client.get("/api/tasks?status=queued", headers=headers)
    cancelled = client.get("/api/tasks?status=cancelled", headers=headers)
    assert queued.headers.get("X-Total-Count") == "3"
    assert cancelled.headers.get("X-Total-Count") == "1"


def test_cors_exposes_total_count_and_correlation_id(tmp_path: Path):
    client = _make_client(tmp_path)
    resp = client.get(
        "/api/devices",
        headers={"X-Admin-Token": TOKEN, "Origin": "http://localhost:5173"},
    )
    assert resp.status_code == 200
    exposed = resp.headers.get("access-control-expose-headers", "")
    assert "X-Total-Count" in exposed
    assert "X-Correlation-ID" in exposed


def test_environment_reference_generator_is_current():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "generate_env_reference.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    env_doc = (REPO_ROOT / "docs" / "environment-reference.md").read_text(encoding="utf-8")
    assert "AI_BRIDGE_ADMIN_TOKEN" in env_doc
    assert "DATABASE_URL" in env_doc


def test_canonical_docs_exist():
    for rel in (
        "docs/support-matrix.md",
        "docs/runbooks.md",
        "docs/environment-reference.md",
        "docs/api-reference.md",
        "docs/fleet-limits.md",
    ):
        p = REPO_ROOT / rel
        assert p.is_file() and p.stat().st_size > 200, f"Missing or empty doc: {rel}"


def test_dashboard_truthfulness_and_accessibility_contracts():
    ui_src = (REPO_ROOT / "apps" / "dashboard" / "src" / "components" / "ui.tsx").read_text(encoding="utf-8")
    assert "[4, 6, 5, 8, 7, 9, 6]" not in ui_src, "Sparkline must never render fabricated fallback numbers"
    assert "export function StateBanner" in ui_src
    assert 'role="dialog"' in ui_src and 'aria-modal="true"' in ui_src

    overview_src = (REPO_ROOT / "apps" / "dashboard" / "src" / "views" / "OverviewView.tsx").read_text(encoding="utf-8")
    assert "avgCpu : 14" not in overview_src and "avgMem : 42" not in overview_src

    desktop_src = (REPO_ROOT / "apps" / "dashboard" / "src" / "views" / "DesktopView.tsx").read_text(encoding="utf-8")
    assert "controlArmed && !controlBlockedReason" in desktop_src
    assert 'e.key === "Escape"' in desktop_src
    assert "View-Only Session" in desktop_src

    security_src = (REPO_ROOT / "apps" / "dashboard" / "src" / "views" / "SecurityView.tsx").read_text(encoding="utf-8")
    assert "Pre-Publish Review" in security_src
    assert "Blast Radius" in security_src
    assert "Pre-Publish Simulation Results" in security_src

    i18n_src = (REPO_ROOT / "apps" / "dashboard" / "src" / "i18n.ts").read_text(encoding="utf-8")
    assert 'dir = locale === "ar" ? "rtl" : "ltr"' in i18n_src

    css_src = (REPO_ROOT / "apps" / "dashboard" / "src" / "styles" / "theme.css").read_text(encoding="utf-8")
    assert ":focus-visible" in css_src
    assert '[dir="rtl"]' in css_src

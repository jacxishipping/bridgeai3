#!/usr/bin/env python3
"""Release 1 Windows Installer & Agent End-to-End Verification Harness.

Exercises the real Windows installer (`deployment/windows/installer_gui.py --silent`),
DPAPI token sealing, restart/reboot survival (fresh subprocess reading the sealed
config), safe Tier-1 task execution, Tier-3 approval flow, desktop capability &
view-only session check, token rotation, device revocation, and clean uninstall
against a local control plane.

Runs inside an isolated temporary profile (`USERPROFILE` / `LOCALAPPDATA`) so it
never modifies the host user's `~/.ai-bridge` config, shortcuts, or autostart.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


def _start_plane(admin_token: str, db_path: Path) -> tuple[str, Any, Any, threading.Thread]:
    from uvicorn import Config, Server

    from ai_bridge.control.app import create_app

    os.environ.setdefault("AI_BRIDGE_CSRF_SECRET", secrets.token_hex(32))
    os.environ["AI_BRIDGE_DATA_DIR"] = str(db_path.parent)
    app = create_app(admin_token=admin_token, db_path=db_path)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = Server(Config(app=app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("Local control plane failed to start")
    return f"http://127.0.0.1:{port}", app, server, thread


def _run_agent_once_from_sealed_config(
    control_url: str,
    isolated_env: dict[str, str],
    *,
    expect_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Simulate a post-reboot fresh agent process loading its DPAPI-sealed token."""
    code = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})",
            f"sys.path.insert(0, {str(REPO_ROOT / 'agents')!r})",
            f"sys.path.insert(0, {str(REPO_ROOT / 'agents' / 'windows')!r})",
            "import app_runtime",
            "from common.poller import ControlPlaneHTTPError, DeviceRevokedError, DeviceTokenExpiredError, Poller",
            "tok = app_runtime.load_token()",
            "assert tok, 'DPAPI-sealed token missing after restart'",
            f"p = Poller({control_url!r}, tok)",
            "try:",
            "    res = p.run_once()",
            "    print(res)",
            "    sys.exit(2 if p.revoked else 0)",
            "except (DeviceRevokedError, DeviceTokenExpiredError):",
            "    sys.exit(2)",
            "except ControlPlaneHTTPError as exc:",
            "    sys.exit(2 if getattr(exc, 'status_code', 0) == 401 else 1)",
        ]
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        env=isolated_env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def run_windows_e2e(out_path: Path) -> dict[str, Any]:
    if platform.system() != "Windows":
        raise RuntimeError(f"Windows installer E2E requires Windows (got {platform.system()})")

    import httpx

    started = time.time()
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    admin_token = f"win-e2e-{secrets.token_hex(12)}"
    admin_headers = {"X-Admin-Token": admin_token}
    installer = REPO_ROOT / "deployment" / "windows" / "installer_gui.py"

    with tempfile.TemporaryDirectory(prefix="aibridge-win-e2e-") as td:
        root = Path(td)
        fake_home = root / "home"
        fake_local = fake_home / "AppData" / "Local"
        install_dir = fake_local / "AIBridge"
        fake_local.mkdir(parents=True, exist_ok=True)
        db_path = root / "control.db"

        isolated_env = dict(os.environ)
        isolated_env["USERPROFILE"] = str(fake_home)
        isolated_env["HOME"] = str(fake_home)
        isolated_env["LOCALAPPDATA"] = str(fake_local)
        isolated_env["APPDATA"] = str(fake_home / "AppData" / "Roaming")
        isolated_env["PYTHONUNBUFFERED"] = "1"

        base_url, app, server, thread = _start_plane(admin_token, db_path)
        try:
            with httpx.Client(timeout=20.0) as client:
                # 1. Enroll Windows device
                r_enr = client.post(
                    f"{base_url}/api/devices/enroll",
                    json={"name": "win-e2e-host", "os": "Windows"},
                    headers=admin_headers,
                )
                r_enr.raise_for_status()
                reg = r_enr.json()
                device_id = reg["device_id"]
                device_token = reg["device_token"]
                record("enroll", True, f"device_id={device_id}")

                # 2. Silent install into isolated profile + DPAPI sealing
                inst = subprocess.run(
                    [
                        sys.executable,
                        str(installer),
                        "--silent",
                        "--url",
                        base_url,
                        "--token",
                        device_token,
                        "--install-dir",
                        str(install_dir),
                        "--no-packages",
                        "--no-shortcuts",
                        "--no-autostart",
                        "--no-agent",
                        "--no-launch",
                    ],
                    env=isolated_env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                cfg_file = fake_home / ".ai-bridge" / "agent_gui.json"
                tok_file = fake_home / ".ai-bridge" / "device.token"
                cfg_data = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
                tok_raw = tok_file.read_bytes() if tok_file.exists() else b""
                token_not_in_json = device_token not in json.dumps(cfg_data)
                token_sealed = bool(tok_raw) and (device_token.encode("utf-8") not in tok_raw)
                safe_defaults = (
                    cfg_data.get("allow_tier3") is False
                    and int(cfg_data.get("max_tier_unattended", 99)) == 2
                )
                record(
                    "silent_install_and_dpapi_seal",
                    inst.returncode == 0 and cfg_file.exists() and tok_file.exists() and token_not_in_json and token_sealed and safe_defaults,
                    f"rc={inst.returncode}, cfg={cfg_file.exists()}, tok={tok_file.exists()}, "
                    f"sealed={token_sealed}, no_json_secret={token_not_in_json}, safe_defaults={safe_defaults}",
                )

                # 3. Post-restart safe Tier-1 task via sealed token
                r_t1 = client.post(
                    f"{base_url}/api/tasks",
                    json={"device_id": device_id, "tool": "get_system_info", "args": {}},
                    headers=admin_headers,
                )
                t1_id = r_t1.json()["task_id"]
                p1 = _run_agent_once_from_sealed_config(base_url, isolated_env)
                r_t1_get = client.get(f"{base_url}/api/tasks/{t1_id}", headers=admin_headers)
                t1_status = r_t1_get.json().get("status")
                record(
                    "restart_survival_and_safe_task",
                    p1.returncode == 0 and t1_status == "completed",
                    f"agent_rc={p1.returncode}, task_status={t1_status}",
                )

                # 4. Tier-3 approved task flow
                r_t3 = client.post(
                    f"{base_url}/api/tasks",
                    json={"device_id": device_id, "tool": "run_shell", "args": {"command": "echo win-e2e-ok"}},
                    headers=admin_headers,
                )
                t3_body = r_t3.json()
                t3_id = t3_body["task_id"]
                app_id = t3_body["approval_id"]
                # Agent poll before approval must NOT execute the task
                p_pre = _run_agent_once_from_sealed_config(base_url, isolated_env)
                st_before = client.get(f"{base_url}/api/tasks/{t3_id}", headers=admin_headers).json().get("status")
                # Approve and poll again
                r_app = client.post(
                    f"{base_url}/api/approvals/{app_id}/decide",
                    json={"decision": "approved", "reason": "Windows E2E verification"},
                    headers=admin_headers,
                )
                p_post = _run_agent_once_from_sealed_config(base_url, isolated_env)
                st_after = client.get(f"{base_url}/api/tasks/{t3_id}", headers=admin_headers).json().get("status")
                record(
                    "approved_tier3_task",
                    p_pre.returncode == 0
                    and st_before == "needs_approval"
                    and r_app.status_code == 200
                    and p_post.returncode == 0
                    and st_after in ("completed", "failed"),
                    f"before={st_before}, approve={r_app.status_code}, after={st_after}",
                )

                # 5. Desktop capability status & view-only v1 session check
                from ai_bridge import tools

                dstat = tools.get_desktop_status()
                r_sess = client.post(
                    f"{base_url}/api/v1/sessions",
                    json={"device_id": device_id, "mode": "view_only", "fps": 15, "scale": 0.5},
                    headers=admin_headers,
                )
                sess_ok = r_sess.status_code == 200 and r_sess.json().get("mode") == "view_only"
                if sess_ok:
                    sid = r_sess.json().get("session_id") or r_sess.json().get("id")
                    client.post(f"{base_url}/api/v1/sessions/{sid}/terminate", json={}, headers=admin_headers)
                record(
                    "desktop_status_and_view_only_session",
                    isinstance(dstat, dict) and sess_ok,
                    f"desktop_keys={sorted(dstat.keys())[:6]}, session_status={r_sess.status_code}",
                )

                # 6. Token rotation & revocation
                r_rot = client.post(f"{base_url}/api/agent/rotate", headers={"X-Device-Token": device_token})
                new_token = r_rot.json().get("device_token", "") if r_rot.status_code == 200 else ""
                old_401 = client.get(f"{base_url}/api/agent/tasks/next?wait=0", headers={"X-Device-Token": device_token}).status_code == 401
                # Agent still holds old sealed token -> fresh subprocess must detect revocation/401 (exit 2)
                p_old = _run_agent_once_from_sealed_config(base_url, isolated_env)
                new_200 = bool(new_token) and client.get(
                    f"{base_url}/api/agent/tasks/next?wait=0", headers={"X-Device-Token": new_token}
                ).status_code == 200
                r_rev = client.post(f"{base_url}/api/devices/{device_id}/revoke", headers=admin_headers)
                rev_401 = r_rev.status_code == 200 and client.get(
                    f"{base_url}/api/agent/tasks/next?wait=0", headers={"X-Device-Token": new_token}
                ).status_code == 401
                record(
                    "token_rotation_and_revocation",
                    bool(new_token) and old_401 and p_old.returncode == 2 and new_200 and rev_401,
                    f"rotate={r_rot.status_code}, old_401={old_401}, stale_agent_rc={p_old.returncode}, "
                    f"new_200={new_200}, rev_401={rev_401}",
                )

                # 7. Clean uninstall + purge
                uninst = subprocess.run(
                    [sys.executable, str(installer), "--uninstall", "--purge"],
                    env=isolated_env,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                purged = not cfg_file.exists() and not tok_file.exists()
                record(
                    "clean_uninstall",
                    uninst.returncode == 0 and purged,
                    f"rc={uninst.returncode}, cfg_removed={not cfg_file.exists()}, tok_removed={not tok_file.exists()}",
                )
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            try:
                app.state.conn.close()
            except Exception:
                pass

    all_ok = all(c["ok"] for c in checks.values())
    report = {
        "gate": "release-1-windows-installer-e2e",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "duration_seconds": round(time.time() - started, 2),
        "all_passed": all_ok,
        "passed_count": sum(1 for c in checks.values() if c["ok"]),
        "total_count": len(checks),
        "checks": checks,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".run/windows_installer_e2e_evidence.json"))
    args = parser.parse_args(argv)
    report = run_windows_e2e(args.out)
    print(json.dumps({"wrote": str(args.out), "all_passed": report["all_passed"], "passed": f"{report['passed_count']}/{report['total_count']}"}, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

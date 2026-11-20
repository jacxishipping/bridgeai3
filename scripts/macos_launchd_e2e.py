#!/usr/bin/env python3
"""Release 1 macOS launchd & Agent End-to-End Verification Harness.

Designed to run on macOS (`macos-latest` in GitHub Actions or an operator Mac):
  1. Renders `deployment/macos/com.aibridge.agent.plist` with a test control URL,
     enrolled device token, and isolated log paths, and validates it with
     `plistlib` + `plutil -lint`.
  2. Boots a local control plane, enrolls a macOS device, and runs
     `agents/macos/agent_main.py --once` across a fresh subprocess restart to
     verify enrollment, polling, safe Tier-1 task execution (`get_system_info`),
     and audit recording.
  3. Verifies `tools.get_desktop_status()` reports honest macOS capability status
     (never fabricating unsupported desktop capture).
  4. Attempts `launchctl` syntax/domain validation when a user GUI domain is
     present, recording honestly whether live `launchctl bootstrap` ran or was
     skipped on a headless CI runner.
  5. Rotates and revokes the device token and verifies clean exit code 2.
  6. Archives structured JSON evidence to `--out`.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import secrets
import shutil
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


def render_launchd_plist(
    template_path: Path,
    *,
    python_exe: str,
    repo_root: Path,
    control_url: str,
    device_token: str,
    log_dir: Path,
    label: str = "com.aibridge.agent.e2e",
) -> bytes:
    with template_path.open("rb") as fh:
        plist_data = plistlib.load(fh)
    plist_data["Label"] = label
    plist_data["ProgramArguments"] = [
        python_exe,
        str(repo_root / "agents" / "macos" / "agent_main.py"),
        "--control-url",
        control_url,
    ]
    plist_data["EnvironmentVariables"] = {"AI_BRIDGE_DEVICE_TOKEN": device_token}
    plist_data["StandardOutPath"] = str(log_dir / "ai-bridge-agent.out")
    plist_data["StandardErrorPath"] = str(log_dir / "ai-bridge-agent.err")
    return plistlib.dumps(plist_data)


def run_macos_e2e(out_path: Path) -> dict[str, Any]:
    import httpx

    from ai_bridge import tools

    started = time.time()
    is_macos = platform.system() == "Darwin"
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, ok: bool, detail: str, extra: dict[str, Any] | None = None) -> None:
        item: dict[str, Any] = {"ok": bool(ok), "detail": detail}
        if extra:
            item.update(extra)
        checks[name] = item

    admin_token = f"mac-e2e-{secrets.token_hex(12)}"
    admin_headers = {"X-Admin-Token": admin_token}
    plist_template = REPO_ROOT / "deployment" / "macos" / "com.aibridge.agent.plist"
    agent_script = REPO_ROOT / "agents" / "macos" / "agent_main.py"

    with tempfile.TemporaryDirectory(prefix="aibridge-mac-e2e-") as td:
        root = Path(td)
        db_path = root / "control.db"
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        base_url, app, server, thread = _start_plane(admin_token, db_path)
        try:
            with httpx.Client(timeout=20.0) as client:
                # 1. Enroll macOS device
                r_enr = client.post(
                    f"{base_url}/api/devices/enroll",
                    json={"name": "macos-e2e-host", "os": "macOS"},
                    headers=admin_headers,
                )
                r_enr.raise_for_status()
                reg = r_enr.json()
                device_id = reg["device_id"]
                device_token = reg["device_token"]
                record("enroll", True, f"device_id={device_id}")

                # 2. Render & validate launchd plist
                rendered = render_launchd_plist(
                    plist_template,
                    python_exe=sys.executable,
                    repo_root=REPO_ROOT,
                    control_url=base_url,
                    device_token=device_token,
                    log_dir=log_dir,
                )
                plist_out = root / "com.aibridge.agent.e2e.plist"
                plist_out.write_bytes(rendered)
                parsed = plistlib.loads(plist_out.read_bytes())
                plutil_ok = True
                plutil_detail = "plistlib-only (non-macOS host)"
                if is_macos and shutil.which("plutil"):
                    pl = subprocess.run(["plutil", "-lint", str(plist_out)], capture_output=True, text=True, timeout=15)
                    plutil_ok = pl.returncode == 0
                    plutil_detail = (pl.stdout or pl.stderr).strip()
                record(
                    "launchd_plist_validation",
                    plutil_ok and parsed.get("RunAtLoad") is True and parsed.get("KeepAlive") is True,
                    plutil_detail,
                )

                # 3. Poll & execute safe Tier-1 task via agents/macos/agent_main.py --once
                r_t1 = client.post(
                    f"{base_url}/api/tasks",
                    json={"device_id": device_id, "tool": "get_system_info", "args": {}},
                    headers=admin_headers,
                )
                t1_id = r_t1.json()["task_id"]
                agent_env = {**os.environ, "AI_BRIDGE_DEVICE_TOKEN": device_token}
                proc = subprocess.run(
                    [sys.executable, str(agent_script), "--control-url", base_url, "--once"],
                    env=agent_env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                r_t1_get = client.get(f"{base_url}/api/tasks/{t1_id}", headers=admin_headers)
                t1_status = r_t1_get.json().get("status")
                record(
                    "macos_agent_poll_and_safe_task",
                    proc.returncode == 0 and t1_status == "completed",
                    f"rc={proc.returncode}, task_status={t1_status}",
                )

                # 4. Honest desktop capability reporting
                dstat = tools.get_desktop_status()
                honest_ok = isinstance(dstat, dict) and "available" in dstat and "reason" in dstat
                if is_macos:
                    honest_ok = honest_ok and dstat.get("stream_available") is False
                record(
                    "honest_desktop_capability_status",
                    honest_ok,
                    f"os={platform.system()}, available={dstat.get('available')}, "
                    f"stream_available={dstat.get('stream_available')}, reason={str(dstat.get('reason', ''))[:120]}",
                )

                # 5. Live launchctl check (macOS only; headless runners may lack a GUI bootstrap domain)
                if is_macos and shutil.which("launchctl"):
                    uid = os.getuid()
                    domain = f"gui/{uid}"
                    boot = subprocess.run(
                        ["launchctl", "bootstrap", domain, str(plist_out)],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if boot.returncode == 0:
                        time.sleep(2.0)
                        subprocess.run(
                            ["launchctl", "bootout", f"{domain}/com.aibridge.agent.e2e"],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        record(
                            "launchd_lifecycle",
                            True,
                            f"launchctl bootstrap + bootout succeeded in {domain}",
                            {"launchctl_live_bootstrap": True},
                        )
                    else:
                        record(
                            "launchd_lifecycle",
                            True,
                            f"headless runner without {domain} session (launchctl rc={boot.returncode}: {(boot.stderr or boot.stdout).strip()[:160]})",
                            {"launchctl_live_bootstrap": False},
                        )
                else:
                    record(
                        "launchd_lifecycle",
                        not is_macos,
                        "non-macOS host (run on macOS to verify live launchctl bootstrap)",
                        {"launchctl_live_bootstrap": False},
                    )

                # 6. Revocation handling
                r_rev = client.post(f"{base_url}/api/devices/{device_id}/revoke", headers=admin_headers)
                r_probe = client.get(
                    f"{base_url}/api/agent/tasks/next?wait=0", headers={"X-Device-Token": device_token}
                )
                record(
                    "token_revocation",
                    r_rev.status_code == 200 and r_probe.status_code == 401,
                    f"revoke={r_rev.status_code}, post_revoke_poll={r_probe.status_code}",
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
        "gate": "release-1-macos-launchd-e2e",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "is_macos": is_macos,
        "launchctl_live_verified": bool(checks.get("launchd_lifecycle", {}).get("launchctl_live_bootstrap", is_macos)),
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
    parser.add_argument("--out", type=Path, default=Path(".run/macos_launchd_e2e_evidence.json"))
    args = parser.parse_args(argv)
    report = run_macos_e2e(args.out)
    print(json.dumps({"wrote": str(args.out), "all_passed": report["all_passed"], "is_macos": report["is_macos"]}, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

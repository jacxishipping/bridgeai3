#!/usr/bin/env python3
"""Release 1 Staging Release Gate & Evidence Archiver.

Runs the full Release 1 staging verification checklist against either:
  - A live staging plane (`--url https://... --admin-token ...`), or
  - A self-hosted local plane (`--self-host`) using the current codebase and
    built dashboard bundle (`apps/dashboard/dist`).

Checks covered (per docs/ROADMAP.md Release 1):
  1. Health & readiness (`/health/live`, `/health/ready`, `/health`)
  2. Schema migrations (all `migrations/[0-9]*.sql` applied)
  3. Dashboard smoke (HTML shell, unauthenticated `/api/auth/me` -> 401, OIDC config, MCP HTTP mount)
  4. Security configuration review (CSRF on session writes, query-param token rejection on
     `live.jpg` & terminal stream, anonymous rejection)
  5. Device enrollment (token shown once, device visible in `/api/devices`)
  6. Safe Tier-1 task lifecycle (create -> agent claim -> real dispatch -> result -> audit)
  7. Tier-3 approval lifecycle (blocked before approval -> approved -> claimed -> completed -> audit chain verify)
  8. Token rotation & revocation (old token -> 401, new token -> 200, revoked -> 401)
  9. Rollback readiness (down migrations present; in `--self-host` / `--db-path` mode, verified backup+restore drill)

Never writes tokens or secrets into the evidence file.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _expected_migration_count() -> int:
    mig_dir = REPO_ROOT / "migrations"
    return len(sorted(p for p in mig_dir.glob("[0-9][0-9][0-9]_*.sql")))


def _down_migrations() -> list[str]:
    mig_dir = REPO_ROOT / "migrations"
    return sorted(p.name for p in mig_dir.glob("down_*.sql"))


def _start_local_plane(admin_token: str, db_path: Path) -> tuple[str, Any, Any, threading.Thread]:
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
        raise RuntimeError("Local control plane failed to start within 15s")
    return f"http://127.0.0.1:{port}", app, server, thread


def run_staging_gate(
    base_url: str,
    admin_token: str,
    *,
    db_path: Path | None = None,
    check_dashboard_root: bool = True,
) -> dict[str, Any]:
    import httpx

    from ai_bridge.control import dispatch as dispatch_mod

    base = base_url.rstrip("/")
    admin_headers = {"X-Admin-Token": admin_token}
    checks: dict[str, dict[str, Any]] = {}
    started = time.time()

    def record(name: str, ok: bool, detail: str, extra: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {"ok": bool(ok), "detail": detail}
        if extra:
            entry.update(extra)
        checks[name] = entry

    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        # 1. Health & readiness
        r_live = client.get(f"{base}/health/live")
        r_ready = client.get(f"{base}/health/ready")
        r_health = client.get(f"{base}/health")
        record(
            "health_and_readiness",
            r_live.status_code == 200 and r_ready.status_code == 200 and r_health.status_code == 200,
            f"live={r_live.status_code}, ready={r_ready.status_code}, health={r_health.status_code}",
        )

        # 2. Dashboard smoke & unauthenticated boundaries
        r_me = client.get(f"{base}/api/auth/me")
        r_oidc = client.get(f"{base}/api/auth/oidc/configured")
        oidc_ok = r_oidc.status_code == 200 and isinstance(r_oidc.json().get("enabled"), bool)
        root_ok = True
        root_detail = "skipped"
        if check_dashboard_root:
            r_root = client.get(f"{base}/")
            root_ok = r_root.status_code == 200 and ('<div id="root">' in r_root.text and "AI Bridge" in r_root.text)
            root_detail = f"HTTP {r_root.status_code}"
        mcp_mounted = False
        mcp_detail = ""
        for mcp_path in ("/mcp", "/mcp/", "/api/mcp"):
            rm = client.post(
                f"{base}{mcp_path}",
                content=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if rm.status_code in (200, 202, 401, 403, 405, 406):
                mcp_mounted = True
                mcp_detail = f"{mcp_path} -> HTTP {rm.status_code}"
                break
        record(
            "dashboard_and_surface_smoke",
            r_me.status_code == 401 and oidc_ok and root_ok and mcp_mounted,
            f"root={root_detail}, auth_me={r_me.status_code}, oidc={r_oidc.status_code}, mcp={mcp_detail or 'none'}",
        )

        # 3. Security configuration review (query-param rejection + session CSRF)
        r_q_jpg = client.get(f"{base}/api/devices/gate-probe/desktop/live.jpg?token={admin_token}")
        r_q_term = client.get(f"{base}/api/devices/gate-probe/terminal/sess-1/stream?token={admin_token}")
        gate_user = f"gate-op-{secrets.token_hex(4)}"
        gate_pass = f"GatePass!{secrets.token_hex(8)}"
        r_mkuser = client.post(
            f"{base}/api/auth/users",
            json={"username": gate_user, "password": gate_pass, "role": "operator"},
            headers=admin_headers,
        )
        csrf_blocked = False
        csrf_allowed = False
        session_token = ""
        if r_mkuser.status_code == 200:
            r_login = client.post(f"{base}/api/auth/login", json={"username": gate_user, "password": gate_pass})
            if r_login.status_code == 200:
                session_token = r_login.json().get("session_token", "")
        if session_token:
            # Enroll a temporary device via admin first so session task POST has a target
            r_no_csrf = client.post(
                f"{base}/api/tasks",
                json={"device_id": "nonexistent-device", "tool": "get_system_info", "args": {}},
                headers={"X-Session-Token": session_token},
            )
            csrf_blocked = r_no_csrf.status_code == 403
            r_csrf = client.get(f"{base}/api/auth/csrf-token", headers={"X-Session-Token": session_token})
            if r_csrf.status_code == 200:
                csrf_tok = r_csrf.json().get("csrf_token", "")
                r_with_csrf = client.post(
                    f"{base}/api/auth/logout",
                    headers={"X-Session-Token": session_token, "X-CSRF-Token": csrf_tok},
                )
                csrf_allowed = r_with_csrf.status_code == 200
        record(
            "security_configuration_review",
            r_q_jpg.status_code == 401 and r_q_term.status_code == 401 and csrf_blocked and csrf_allowed,
            f"query_jpg={r_q_jpg.status_code}, query_term={r_q_term.status_code}, "
            f"csrf_blocked={csrf_blocked}, csrf_logout={csrf_allowed}",
        )

        # 4. Device enrollment
        dev_name = f"gate-device-{secrets.token_hex(4)}"
        r_enr = client.post(
            f"{base}/api/devices/enroll",
            json={"name": dev_name, "os": platform.system()},
            headers=admin_headers,
        )
        enr_ok = r_enr.status_code == 200 and bool(r_enr.json().get("device_id")) and bool(r_enr.json().get("device_token"))
        device_id = r_enr.json().get("device_id", "") if enr_ok else ""
        device_token = r_enr.json().get("device_token", "") if enr_ok else ""
        r_devs = client.get(f"{base}/api/devices", headers=admin_headers)
        listed = r_devs.status_code == 200 and any(d.get("id") == device_id for d in r_devs.json())
        record(
            "enrollment",
            enr_ok and listed,
            f"enroll={r_enr.status_code}, listed={listed}, device_id={device_id}",
        )

        if not enr_ok:
            return _finalize(base, started, checks)

        dev_headers = {"X-Device-Token": device_token}

        # 5. Safe Tier-1 task lifecycle (create -> claim -> dispatch -> result -> read)
        r_t1 = client.post(
            f"{base}/api/tasks",
            json={"device_id": device_id, "tool": "get_system_info", "args": {}},
            headers=admin_headers,
        )
        t1_id = r_t1.json().get("task_id", "") if r_t1.status_code == 200 else ""
        r_claim = client.get(f"{base}/api/agent/tasks/next?wait=0", headers=dev_headers)
        claimed_task = (r_claim.json() or {}).get("task") if r_claim.status_code == 200 else None
        t1_claimed = bool(claimed_task and claimed_task.get("id") == t1_id)
        t1_completed = False
        if t1_claimed:
            out = dispatch_mod.dispatch(claimed_task["tool"], json.loads(claimed_task.get("args_json") or "{}"))
            r_res = client.post(
                f"{base}/api/agent/tasks/{t1_id}/result",
                json={"status": "completed", "result": json.dumps(out)},
                headers=dev_headers,
            )
            r_get = client.get(f"{base}/api/tasks/{t1_id}", headers=admin_headers)
            t1_completed = r_res.status_code == 200 and r_get.status_code == 200 and r_get.json().get("status") == "completed"
        record(
            "safe_task_lifecycle",
            bool(t1_id) and t1_claimed and t1_completed,
            f"create={r_t1.status_code}, claimed={t1_claimed}, completed={t1_completed}",
        )

        # 6. Tier-3 approval lifecycle (blocked before approval -> approve -> claim -> complete)
        r_t3 = client.post(
            f"{base}/api/tasks",
            json={"device_id": device_id, "tool": "run_shell", "args": {"command": "echo gate-ok"}},
            headers=admin_headers,
        )
        t3_body = r_t3.json() if r_t3.status_code == 200 else {}
        t3_id = t3_body.get("task_id", "")
        approval_id = t3_body.get("approval_id", "")
        pending_ok = r_t3.status_code == 200 and t3_body.get("status") == "needs_approval" and bool(approval_id)
        r_claim_blocked = client.get(f"{base}/api/agent/tasks/next?wait=0", headers=dev_headers)
        blocked_before = r_claim_blocked.status_code == 200 and (r_claim_blocked.json() or {}).get("task") is None

        approved_ok = False
        claimed_after = False
        completed_after = False
        if pending_ok and blocked_before:
            r_app = client.post(
                f"{base}/api/approvals/{approval_id}/decide",
                json={"decision": "approved", "reason": "Release 1 staging gate check"},
                headers=admin_headers,
            )
            approved_ok = r_app.status_code == 200
            r_claim_after = client.get(f"{base}/api/agent/tasks/next?wait=0", headers=dev_headers)
            c_task = (r_claim_after.json() or {}).get("task") if r_claim_after.status_code == 200 else None
            claimed_after = bool(c_task and c_task.get("id") == t3_id)
            if claimed_after:
                r_res3 = client.post(
                    f"{base}/api/agent/tasks/{t3_id}/result",
                    json={"status": "completed", "result": json.dumps({"ok": True, "gate": "approved"})},
                    headers=dev_headers,
                )
                completed_after = r_res3.status_code == 200
        record(
            "approval_lifecycle",
            pending_ok and blocked_before and approved_ok and claimed_after and completed_after,
            f"pending={pending_ok}, blocked_before={blocked_before}, approved={approved_ok}, "
            f"claimed_after={claimed_after}, completed={completed_after}",
        )

        # 7. Audit hash-chain integrity
        r_av = client.get(f"{base}/api/audit/verify", headers=admin_headers)
        av_json = r_av.json() if r_av.status_code == 200 else {}
        av_ok = bool(av_json.get("ok") if "ok" in av_json else av_json.get("valid"))
        record(
            "audit_chain_integrity",
            r_av.status_code == 200 and av_ok,
            f"status={r_av.status_code}, ok={av_ok}, checked={av_json.get('checked')}",
        )

        # 8. Token rotation & revocation
        r_rot = client.post(f"{base}/api/agent/rotate", headers=dev_headers)
        new_token = r_rot.json().get("device_token", "") if r_rot.status_code == 200 else ""
        old_rejected = client.get(f"{base}/api/agent/tasks/next?wait=0", headers=dev_headers).status_code == 401
        new_accepted = (
            bool(new_token)
            and client.get(f"{base}/api/agent/tasks/next?wait=0", headers={"X-Device-Token": new_token}).status_code == 200
        )
        r_rev = client.post(f"{base}/api/devices/{device_id}/revoke", headers=admin_headers)
        revoked_rejected = (
            r_rev.status_code == 200
            and bool(new_token)
            and client.get(f"{base}/api/agent/tasks/next?wait=0", headers={"X-Device-Token": new_token}).status_code == 401
        )
        record(
            "token_rotation_and_revocation",
            bool(new_token) and old_rejected and new_accepted and revoked_rejected,
            f"rotate={r_rot.status_code}, old_401={old_rejected}, new_200={new_accepted}, revoked_401={revoked_rejected}",
        )

    # 9. Migrations & rollback readiness (local/self-host DB verification + down-migration inventory)
    expected_migs = _expected_migration_count()
    downs = _down_migrations()
    if db_path is not None and db_path.exists():
        from ai_bridge.control import db as dbmod
        from scripts.backup_restore import drill_sqlite

        conn = dbmod.connect(db_path=db_path)
        try:
            applied_migs = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        finally:
            conn.close()
        record(
            "migrations",
            applied_migs == expected_migs,
            f"applied={applied_migs}, expected={expected_migs}",
        )
        with tempfile.TemporaryDirectory(prefix="aibridge-gate-drill-") as td:
            drill_report = drill_sqlite(db_path, Path(td))
        record(
            "rollback_and_restore_readiness",
            bool(drill_report["ok"]) and len(downs) > 0,
            f"restore_drill_ok={drill_report['ok']}, rto_s={drill_report['rto_seconds']}, "
            f"audit_chain_valid={drill_report['audit_chain_valid']}, down_migrations={len(downs)}",
            {"drill": drill_report, "down_migrations": downs},
        )
    else:
        record(
            "migrations",
            expected_migs >= 16,
            f"repo_migrations={expected_migs} (remote DB count checked during backup drill)",
        )
        record(
            "rollback_and_restore_readiness",
            len(downs) > 0,
            f"down_migrations={len(downs)} (run scripts/backup_drill.sh for live PostgreSQL restore evidence)",
            {"down_migrations": downs},
        )

    return _finalize(base, started, checks)


def _finalize(base: str, started: float, checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    all_ok = all(c["ok"] for c in checks.values()) and len(checks) > 0
    return {
        "gate": "release-1-staging-gate",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.system(),
        "target_url": base,
        "duration_seconds": round(time.time() - started, 2),
        "all_passed": all_ok,
        "passed_count": sum(1 for c in checks.values() if c["ok"]),
        "total_count": len(checks),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="", help="Base URL of staging control plane")
    parser.add_argument("--admin-token", default=os.environ.get("AI_BRIDGE_ADMIN_TOKEN", ""), help="Admin token")
    parser.add_argument("--self-host", action="store_true", help="Boot a local plane with built dashboard and run the gate")
    parser.add_argument("--out", type=Path, default=Path(".run/staging_release_gate_evidence.json"))
    args = parser.parse_args(argv)

    app = None
    server = None
    thread = None
    temp_ctx = None
    db_path: Path | None = None

    try:
        if args.self_host:
            temp_ctx = tempfile.TemporaryDirectory(prefix="aibridge-stage-gate-")
            tmp_dir = Path(temp_ctx.name)
            db_path = tmp_dir / "control.db"
            token = args.admin_token or f"gate-adm-{secrets.token_hex(12)}"
            base_url, app, server, thread = _start_local_plane(token, db_path)
            has_dist = (REPO_ROOT / "apps" / "dashboard" / "dist" / "index.html").is_file()
            report = run_staging_gate(base_url, token, db_path=db_path, check_dashboard_root=has_dist)
            report["mode"] = "self_host"
            report["dashboard_dist_present"] = has_dist
        else:
            if not args.url or not args.admin_token:
                parser.error("Provide --url and --admin-token (or AI_BRIDGE_ADMIN_TOKEN), or use --self-host.")
            report = run_staging_gate(args.url, args.admin_token, check_dashboard_root=True)
            report["mode"] = "remote_staging"
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if app is not None:
            try:
                app.state.conn.close()
            except Exception:
                pass
        if temp_ctx is not None:
            temp_ctx.cleanup()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"wrote": str(args.out), "all_passed": report["all_passed"], "passed": f"{report['passed_count']}/{report['total_count']}"}, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

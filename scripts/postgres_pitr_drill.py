#!/usr/bin/env python3
"""Release 1 Live PostgreSQL Backup, Restore, Parity & PITR Drill.

Boots an isolated local PostgreSQL instance (`initdb` + `pg_ctl` on a free
localhost port — no system service or elevated privileges required) and proves:
  1. Schema parity (`scripts/check_schema_parity.py --postgres ...`)
  2. Clean Postgres control-plane boot (`scripts/test_postgres_boot.py`)
  3. Logical backup/restore drill (`pg_dump -Fc` -> `pg_restore` into isolated DB
     -> 16 migrations -> users/devices/tasks/approvals/sessions/audit counts ->
     audit hash-chain continuity -> FastAPI boot + `/health/ready` -> RTO/RPO)
  4. Physical Point-in-Time Recovery (PITR) drill:
     - `pg_basebackup` + `pg_verifybackup`
     - Post-basebackup writes (proving WAL replay, not just basebackup state)
     - Named restore point `aibridge_pre_incident` + `pg_switch_wal()`
     - Simulated post-incident data corruption/deletion + `pg_switch_wal()`
     - Isolated restore with `recovery.signal` + `restore_command` +
       `recovery_target_name = 'aibridge_pre_incident'` + `promote`
     - Control plane boot against the PITR-recovered instance verifying 100%
       pre-incident continuity, 0 post-incident contamination, valid audit
       hash-chain, and recorded RTO/RPO.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.backup_restore import (  # noqa: E402
    _boot_plane_smoke,
    _verify_restored_connection,
    drill_postgres,
    find_pg_bin,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run_checked(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed (rc={proc.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{proc.stdout[-1200:]}\nSTDERR:\n{proc.stderr[-1200:]}"
        )
    return proc


def _write_wal_scripts(work_dir: Path, wal_archive: Path) -> tuple[str, str]:
    """Create tiny portable Python helper scripts for archive_command / restore_command."""
    archiver = work_dir / "wal_archive.py"
    archiver.write_text(
        "import shutil, sys\n"
        "from pathlib import Path\n"
        f"dst = Path({str(wal_archive)!r}) / sys.argv[2]\n"
        "dst.parent.mkdir(parents=True, exist_ok=True)\n"
        "if not dst.exists():\n"
        "    shutil.copy2(sys.argv[1], dst)\n",
        encoding="utf-8",
    )
    restorer = work_dir / "wal_restore.py"
    restorer.write_text(
        "import shutil, sys\n"
        "from pathlib import Path\n"
        f"src = Path({str(wal_archive)!r}) / sys.argv[1]\n"
        "if not src.is_file():\n"
        "    sys.exit(1)\n"
        "shutil.copy2(src, sys.argv[2])\n",
        encoding="utf-8",
    )
    py = Path(sys.executable).as_posix()
    archive_cmd = f'"{py}" "{archiver.as_posix()}" "%p" "%f"'
    restore_cmd = f'"{py}" "{restorer.as_posix()}" "%f" "%p"'
    return archive_cmd, restore_cmd


def _start_pg_cluster(pg_ctl: str, pgdata: Path, log_file: Path, port: int) -> None:
    # Redirect pg_ctl stdout/stderr to a file rather than a PIPE: on Windows,
    # the detached postgres.exe child inherits PIPE handles and blocks
    # subprocess.run(capture_output=True) until timeout.
    ctl_log = log_file.with_suffix(".ctl.log")
    with ctl_log.open("wb") as out_fh:
        proc = subprocess.run(
            [
                pg_ctl,
                "-D",
                str(pgdata),
                "-l",
                str(log_file),
                "-w",
                "-o",
                f"-h 127.0.0.1 -p {port}",
                "start",
            ],
            stdin=subprocess.DEVNULL,
            stdout=out_fh,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
    if proc.returncode != 0:
        ctl_txt = ctl_log.read_text(encoding="utf-8", errors="replace") if ctl_log.exists() else ""
        srv_txt = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        raise RuntimeError(f"pg_ctl start failed (rc={proc.returncode}):\n{ctl_txt}\n{srv_txt[-1200:]}")


def _stop_pg_cluster(pg_ctl: str, pgdata: Path) -> None:
    if pgdata.exists():
        subprocess.run(
            [pg_ctl, "-D", str(pgdata), "-m", "fast", "stop"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def _seed_plane_state(dsn: str, admin_token: str, data_dir: Path) -> dict[str, Any]:
    """Boot the control plane against ``dsn`` and create representative operational data."""
    from fastapi.testclient import TestClient

    from ai_bridge.control.app import create_app

    os.environ["AI_BRIDGE_CSRF_SECRET"] = "pg-pitr-drill-csrf-secret"
    os.environ["AI_BRIDGE_DATA_DIR"] = str(data_dir)
    app = create_app(admin_token=admin_token, database_url=dsn)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"X-Admin-Token": admin_token}

        # User + login session
        r_u = client.post(
            "/api/auth/users",
            json={"username": "drill-op", "password": "DrillPassword123!", "role": "operator"},
            headers=headers,
        )
        assert r_u.status_code == 200, r_u.text
        r_l = client.post("/api/auth/login", json={"username": "drill-op", "password": "DrillPassword123!"})
        assert r_l.status_code == 200, r_l.text

        # Device 1 + completed safe task + approved Tier-3 task
        r_d1 = client.post("/api/devices/enroll", json={"name": "pg-drill-node-1", "os": "Linux"}, headers=headers)
        assert r_d1.status_code == 200, r_d1.text
        dev1_id = r_d1.json()["device_id"]
        dev1_tok = r_d1.json()["device_token"]

        r_t1 = client.post(
            "/api/tasks",
            json={"device_id": dev1_id, "tool": "get_system_info", "args": {}},
            headers=headers,
        )
        assert r_t1.status_code == 200, r_t1.text
        t1_id = r_t1.json()["task_id"]
        client.get("/api/agent/tasks/next?wait=0", headers={"X-Device-Token": dev1_tok})
        client.post(
            f"/api/agent/tasks/{t1_id}/result",
            json={"status": "completed", "result": json.dumps({"ok": True, "drill": "pre-basebackup"})},
            headers={"X-Device-Token": dev1_tok},
        )

        # Tier-3 approval record
        r_t3 = client.post(
            "/api/tasks",
            json={"device_id": dev1_id, "tool": "run_shell", "args": {"command": "echo drill"}},
            headers=headers,
        )
        assert r_t3.status_code == 200, r_t3.text
        app_id = r_t3.json()["approval_id"]
        r_dec = client.post(
            f"/api/approvals/{app_id}/decide",
            json={"decision": "approved", "reason": "PITR drill baseline"},
            headers=headers,
        )
        assert r_dec.status_code == 200, r_dec.text

        return {"device1_id": dev1_id, "task1_id": t1_id, "approval_id": app_id}
    finally:
        try:
            app.state.conn.close()
        except Exception:
            pass


def run_live_postgres_drills(out_path: Path) -> dict[str, Any]:
    import psycopg
    from fastapi.testclient import TestClient

    from ai_bridge.control import db as dbmod
    from ai_bridge.control.app import create_app

    required_bins = ("initdb", "pg_ctl", "pg_basebackup", "pg_verifybackup", "pg_dump", "pg_restore", "psql", "createdb", "dropdb")
    bins: dict[str, str] = {}
    missing = []
    for name in required_bins:
        path = find_pg_bin(name)
        if path is None:
            missing.append(name)
        else:
            bins[name] = path
    if missing:
        raise RuntimeError(f"Missing PostgreSQL binaries: {', '.join(missing)}")

    pg_version = _run_checked([bins["psql"], "--version"]).stdout.strip()
    started = time.time()
    admin_token = f"pg-pitr-{secrets.token_hex(12)}"
    results: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="aibridge-pg-pitr-") as td:
        root = Path(td)
        pgdata_primary = root / "pgdata-primary"
        pgdata_pitr = root / "pgdata-pitr"
        basebackup_dir = root / "basebackup"
        wal_archive = root / "wal-archive"
        wal_archive.mkdir(parents=True, exist_ok=True)
        plane_data = root / "plane-data"
        plane_data.mkdir(parents=True, exist_ok=True)

        primary_port = _free_port()
        pitr_port = _free_port()
        while pitr_port == primary_port:
            pitr_port = _free_port()

        archive_cmd, restore_cmd = _write_wal_scripts(root, wal_archive)

        # Initialize primary cluster with checksums & WAL archiving
        _run_checked([bins["initdb"], "-D", str(pgdata_primary), "-U", "ai_bridge", "-A", "trust", "-E", "UTF8", "--data-checksums"])
        with (pgdata_primary / "postgresql.conf").open("a", encoding="utf-8") as conf:
            conf.write(
                "\n"
                "listen_addresses = '127.0.0.1'\n"
                "wal_level = replica\n"
                "archive_mode = on\n"
                f"archive_command = '{archive_cmd}'\n"
                "max_wal_senders = 5\n"
                "checkpoint_timeout = 5min\n"
            )
        # Allow local replication for pg_basebackup
        with (pgdata_primary / "pg_hba.conf").open("a", encoding="utf-8") as hba:
            hba.write("\nhost replication ai_bridge 127.0.0.1/32 trust\n")

        _start_pg_cluster(bins["pg_ctl"], pgdata_primary, root / "primary.log", primary_port)
        try:
            base_dsn = f"postgresql://ai_bridge@127.0.0.1:{primary_port}/postgres"
            live_db = "ai_bridge_live"
            live_dsn = f"postgresql://ai_bridge@127.0.0.1:{primary_port}/{live_db}"
            logical_restore_dsn = f"postgresql://ai_bridge@127.0.0.1:{primary_port}/ai_bridge_logical_drill"

            # 1. Schema parity against live PostgreSQL
            parity_proc = _run_checked(
                [sys.executable, str(REPO_ROOT / "scripts" / "check_schema_parity.py"), "--postgres", base_dsn]
            )
            results["schema_parity"] = {
                "ok": "PG PARITY OK" in parity_proc.stdout,
                "output": parity_proc.stdout.strip().splitlines()[-1] if parity_proc.stdout.strip() else "",
            }

            # 2. Control-plane boot test against live PostgreSQL
            boot_proc = _run_checked(
                [sys.executable, str(REPO_ROOT / "scripts" / "test_postgres_boot.py"), "--base-dsn", base_dsn]
            )
            results["postgres_boot"] = {
                "ok": "POSTGRES BOOT OK" in boot_proc.stdout,
                "output": boot_proc.stdout.strip().splitlines()[-1] if boot_proc.stdout.strip() else "",
            }

            # 3. Create & seed live operational database
            with psycopg.connect(base_dsn, autocommit=True) as conn:
                conn.execute(f'CREATE DATABASE "{live_db}"')
            _seed_plane_state(live_dsn, admin_token, plane_data)

            # 3a. Logical pg_dump / pg_restore drill
            logical_report = drill_postgres(live_dsn, logical_restore_dsn, fmt="c")
            results["logical_pg_dump_restore_drill"] = logical_report

            # 4. Physical PITR drill (pg_basebackup + WAL archiving + restore point)
            t_bb_start = time.perf_counter()
            _run_checked(
                [
                    bins["pg_basebackup"],
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(primary_port),
                    "-U",
                    "ai_bridge",
                    "-D",
                    str(basebackup_dir),
                    "-Fp",
                    "-X",
                    "stream",
                    "-c",
                    "fast",
                ]
            )
            bb_seconds = time.perf_counter() - t_bb_start
            verify_bb = _run_checked([bins["pg_verifybackup"], str(basebackup_dir)])
            bb_verified = "backup successfully verified" in (verify_bb.stdout + verify_bb.stderr).lower()

            # 4b. Post-basebackup writes (must be recovered via archived WAL!)
            app = create_app(admin_token=admin_token, database_url=live_dsn)
            try:
                client = TestClient(app, raise_server_exceptions=False)
                headers = {"X-Admin-Token": admin_token}
                r_d2 = client.post(
                    "/api/devices/enroll",
                    json={"name": "pg-pitr-post-basebackup-node", "os": "Windows"},
                    headers=headers,
                )
                assert r_d2.status_code == 200, r_d2.text
                post_bb_dev_id = r_d2.json()["device_id"]
                r_t2 = client.post(
                    "/api/tasks",
                    json={"device_id": post_bb_dev_id, "tool": "get_system_info", "args": {}},
                    headers=headers,
                )
                assert r_t2.status_code == 200, r_t2.text
                post_bb_task_id = r_t2.json()["task_id"]
            finally:
                try:
                    app.state.conn.close()
                except Exception:
                    pass

            pre_incident_ts = time.time()
            # Record expected pre-incident state and create named restore point
            conn_chk = dbmod.connect(database_url=live_dsn)
            try:
                pre_incident_state = _verify_restored_connection(conn_chk)
            finally:
                conn_chk.close()

            with psycopg.connect(live_dsn, autocommit=True) as conn:
                rp_lsn = conn.execute("SELECT pg_create_restore_point('aibridge_pre_incident')::text").fetchone()[0]
                conn.execute("SELECT pg_switch_wal()")
            restore_point_ts = time.time()
            pitr_rpo_seconds = max(0.0, restore_point_ts - pre_incident_ts)

            # Wait briefly for archive_command to flush the switched WAL segment
            time.sleep(1.5)

            # 4c. Simulate post-restore-point incident (must NOT appear after PITR!)
            with psycopg.connect(live_dsn, autocommit=True) as conn:
                conn.execute("DELETE FROM tasks WHERE id = %s", (post_bb_task_id,))
                conn.execute(
                    "INSERT INTO devices (id, name, hostname, os, tenant, token_hash, status, last_seen, created_at) "
                    "VALUES ('incident-corrupt-dev', 'incident-corrupt', 'bad', 'Linux', 'default', 'badhash', 'offline', 0, 0)"
                )
                conn.execute("SELECT pg_switch_wal()")
            time.sleep(1.5)

            # 4d. Restore basebackup into isolated pgdata_pitr and recover to named restore point
            t_pitr_rto_start = time.perf_counter()
            shutil.copytree(basebackup_dir, pgdata_pitr)
            (pgdata_pitr / "recovery.signal").write_text("", encoding="utf-8")
            with (pgdata_pitr / "postgresql.auto.conf").open("a", encoding="utf-8") as aconf:
                aconf.write(
                    "\n"
                    f"restore_command = '{restore_cmd}'\n"
                    "recovery_target_name = 'aibridge_pre_incident'\n"
                    "recovery_target_action = 'promote'\n"
                    "archive_mode = off\n"
                )

            _start_pg_cluster(bins["pg_ctl"], pgdata_pitr, root / "pitr.log", pitr_port)
            try:
                pitr_dsn = f"postgresql://ai_bridge@127.0.0.1:{pitr_port}/{live_db}"
                # Wait for recovery promotion
                promoted = False
                for _ in range(60):
                    try:
                        with psycopg.connect(pitr_dsn, autocommit=True) as conn:
                            in_rec = conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]
                            if not in_rec:
                                promoted = True
                                break
                    except Exception:
                        pass
                    time.sleep(0.25)
                if not promoted:
                    raise RuntimeError("PITR cluster did not promote within 15s")

                conn_pitr = dbmod.connect(database_url=pitr_dsn)
                try:
                    pitr_verify = _verify_restored_connection(conn_pitr)
                    recovered_post_bb_dev = (
                        conn_pitr.execute("SELECT COUNT(*) FROM devices WHERE id = ?", (post_bb_dev_id,)).fetchone()[0]
                        == 1
                    )
                    recovered_post_bb_task = (
                        conn_pitr.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (post_bb_task_id,)).fetchone()[0]
                        == 1
                    )
                    excluded_incident_dev = (
                        conn_pitr.execute("SELECT COUNT(*) FROM devices WHERE id = 'incident-corrupt-dev'").fetchone()[0]
                        == 0
                    )
                finally:
                    conn_pitr.close()

                pitr_boot = _boot_plane_smoke(database_url=pitr_dsn)
                pitr_rto_seconds = time.perf_counter() - t_pitr_rto_start

                wal_files_archived = len([p for p in wal_archive.iterdir() if p.is_file()])
                pitr_ok = bool(
                    bb_verified
                    and promoted
                    and pitr_verify["migrations_ok"]
                    and pitr_verify["audit_chain_valid"]
                    and pitr_verify["table_counts"] == pre_incident_state["table_counts"]
                    and recovered_post_bb_dev
                    and recovered_post_bb_task
                    and excluded_incident_dev
                    and pitr_boot["ok"]
                )
                results["physical_pitr_drill"] = {
                    "ok": pitr_ok,
                    "basebackup_verified": bb_verified,
                    "basebackup_seconds": round(bb_seconds, 3),
                    "wal_files_archived": wal_files_archived,
                    "restore_point_name": "aibridge_pre_incident",
                    "restore_point_lsn": rp_lsn,
                    "promoted": promoted,
                    "rto_seconds": round(pitr_rto_seconds, 3),
                    "rpo_seconds": round(pitr_rpo_seconds, 3),
                    "pre_incident_counts": pre_incident_state["table_counts"],
                    "recovered_counts": pitr_verify["table_counts"],
                    "post_basebackup_device_recovered": recovered_post_bb_dev,
                    "post_basebackup_task_recovered": recovered_post_bb_task,
                    "post_incident_corruption_excluded": excluded_incident_dev,
                    "migrations_applied": pitr_verify["migrations_applied"],
                    "migrations_expected": pitr_verify["migrations_expected"],
                    "audit_chain": pitr_verify["audit_chain"],
                    "app_boot": pitr_boot,
                }
            finally:
                _stop_pg_cluster(bins["pg_ctl"], pgdata_pitr)
        finally:
            _stop_pg_cluster(bins["pg_ctl"], pgdata_primary)

    all_ok = all(bool(v.get("ok")) for v in results.values())
    report = {
        "gate": "release-1-postgres-restore-and-pitr-drill",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "postgres_version": pg_version,
        "duration_seconds": round(time.time() - started, 2),
        "all_passed": all_ok,
        "results": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".run/postgres_pitr_drill_evidence.json"))
    args = parser.parse_args(argv)
    report = run_live_postgres_drills(args.out)
    print(
        json.dumps(
            {
                "wrote": str(args.out),
                "postgres_version": report["postgres_version"],
                "all_passed": report["all_passed"],
                "logical_rto_s": report["results"]["logical_pg_dump_restore_drill"]["rto_seconds"],
                "pitr_rto_s": report["results"]["physical_pitr_drill"]["rto_seconds"],
                "pitr_rpo_s": report["results"]["physical_pitr_drill"]["rpo_seconds"],
            },
            indent=2,
        )
    )
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

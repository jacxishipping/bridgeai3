"""Boot the control plane against a real PostgreSQL database (Gate 16).

Creates a scratch database, runs the app's own connect path (migrations),
boots the FastAPI app against it, and exercises the core flows end to
end: health, device enroll/list, task create/read, user create/login,
CSRF mint + session task create, audit rows. Proves the plane boots on
Postgres and the schema the migrations build there actually serves the
app — not just that the migrations run.

Usage:
    python3 scripts/test_postgres_boot.py --base-dsn \
        postgresql://ai_bridge:ai_bridge_pw@localhost:5432/postgres \
        [--db ai_bridge_boot] [--keep]

--base-dsn must point at an existing database the caller can CREATE
DATABASE from. The scratch database is dropped at the end unless --keep.
Exit 0 on PASS, 1 on any failure (assertion traceback).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("AI_BRIDGE_CSRF_SECRET", "postgres-boot-test-secret")


def _db_dsn(base_dsn: str, dbname: str) -> str:
    from urllib.parse import urlparse, urlunparse

    u = urlparse(base_dsn)
    return urlunparse(u._replace(path="/" + dbname))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dsn", required=True)
    parser.add_argument("--db", default="ai_bridge_boot")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    import psycopg
    from fastapi.testclient import TestClient

    from ai_bridge.control import db as dbmod
    from ai_bridge.control import migrations as migmod
    from ai_bridge.control.app import create_app

    tmp = Path(tempfile.mkdtemp(prefix="aibridge-pg-boot-"))
    os.environ["AI_BRIDGE_DATA_DIR"] = str(tmp)
    boot_dsn = _db_dsn(args.base_dsn, args.db)

    with psycopg.connect(args.base_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{args.db}"')
            cur.execute(f'CREATE DATABASE "{args.db}"')
    app = None
    try:
        conn = dbmod.connect(database_url=boot_dsn)
        try:
            versions = [r[0] for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()]
        finally:
            conn.close()
        expected_versions = sorted(p.name.split("_", 1)[0] for p in migmod.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
        assert versions == expected_versions, f"expected migrations {expected_versions}, got {versions}"
        print(f"migrations applied on Postgres: {len(versions)}")

        app = create_app(admin_token="test", database_url=boot_dsn)
        client = TestClient(app, raise_server_exceptions=False)
        admin = {"X-Admin-Token": "test"}

        r = client.get("/health")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"
        print("health: 200 ok")
        r = client.get("/health/ready")
        assert r.status_code == 200, r.text
        print("health/ready: 200")

        r = client.post("/api/devices/enroll", json={"name": "pg-boot-test"}, headers=admin)
        assert r.status_code == 200, r.text
        device_id = r.json()["device_id"]
        print(f"enroll: 200 device {device_id}")

        r = client.get("/api/devices", headers=admin)
        assert r.status_code == 200, r.text
        assert any(d["id"] == device_id for d in r.json()), "enrolled device missing"
        print("list devices: enrolled device present")

        r = client.post("/api/tasks", json={"device_id": device_id, "tool": "get_system_info", "args": {}}, headers=admin)
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]
        assert r.json()["status"] == "queued"
        r = client.get(f"/api/tasks/{task_id}", headers=admin)
        assert r.status_code == 200 and r.json()["status"] == "queued", r.text
        print(f"task create/read: queued {task_id}")

        r = client.post("/api/auth/users", json={"username": "pgop", "password": "pw123456", "role": "operator"}, headers=admin)
        assert r.status_code == 200, r.text
        r = client.post("/api/auth/login", json={"username": "pgop", "password": "pw123456"})
        assert r.status_code == 200, r.text
        session_token = r.json()["session_token"]
        r = client.get("/api/auth/csrf-token", headers={"X-Session-Token": session_token})
        assert r.status_code == 200, r.text
        csrf = r.json()["csrf_token"]
        r = client.post("/api/tasks", json={"device_id": device_id, "tool": "get_system_info", "args": {}},
                        headers={"X-Session-Token": session_token, "X-CSRF-Token": csrf})
        assert r.status_code == 200, r.text
        print("login + CSRF mint + session task create: 200")

        r = client.get("/api/audit", headers=admin)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) >= 1, "no audit rows on Postgres"
        print(f"audit: {len(rows)} row(s)")

        print("POSTGRES BOOT OK")
        return 0
    finally:
        # The TestClient leaves the app's database connection open; DROP
        # DATABASE refuses while any session is attached ("database is
        # being accessed by other users"). Close the app's connection,
        # then terminate any stray backends on the scratch DB before
        # dropping it. (Gate 16: without this the script exits 1 even
        # though every boot check passed.)
        if app is not None:
            try:
                app.state.conn.close()
            except Exception:
                pass
        if not args.keep:
            with psycopg.connect(args.base_dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (args.db,),
                    )
                    cur.execute(f'DROP DATABASE IF EXISTS "{args.db}"')


if __name__ == "__main__":
    raise SystemExit(main())

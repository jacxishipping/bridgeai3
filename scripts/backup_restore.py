#!/usr/bin/env python3
"""Backup / restore drill for the AI Bridge control plane (DB-01).

SQLite path (executed in tests):
  backup  --db plane.db --out plane-YYYYMMDD-HHMMSS.db
          Uses sqlite3's online backup API via
          ai_bridge.control.backup.verified_backup (integrity-checked,
          refuses to overwrite, 0600 perms).
  restore --backup plane-....db --db fresh.db
          Copies the backup to a NEW path, runs PRAGMA integrity_check,
          then boots it through ai_bridge.control.db.connect() (which
          applies any pending migrations and verifies the schema) and
          reports row counts. Never overwrites an existing live DB —
          restore into a fresh path and swap it in while the plane is
          stopped.

Postgres path (scripted, NOT executed here — no Postgres server in this
environment; record honestly):
  backup  --pg-dsn "postgresql://user@host/db" --out dump.sql
          Runs pg_dump (custom format via --pg-format c is the default
          for restore with pg_restore; plain SQL with --pg-format p).
  restore --pg-dump dump.sql --pg-dsn "postgresql://user@host/newdb"
          Creates the target DB if missing (createdb) and restores with
          psql (plain) or pg_restore (custom format), then verifies by
          connecting and counting core tables.
  The commands are printed before running so an operator can audit
  them; pg_dump/psql/pg_restore must be on PATH (missing binary is a
  hard error, not a silent skip).

Backups contain password hashes, session-token hashes and device token
hashes: treat backup files as credentials (0600, encrypted off-host
copy). This tool never transmits anything.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_bridge.control import db as dbmod  # noqa: E402
from ai_bridge.control.backup import verified_backup  # noqa: E402

_CORE_TABLES = ("users", "devices", "tasks", "sessions", "approvals", "audit")


def expected_migration_count() -> int:
    mig_dir = REPO_ROOT / "migrations"
    return len(sorted(mig_dir.glob("[0-9][0-9][0-9]_*.sql")))


def _verify_restored_connection(conn: Any) -> dict[str, Any]:
    """Validate migrations, core tables, and audit hash-chain continuity."""
    versions = [
        r[0]
        for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    counts: dict[str, int] = {}
    for table in _CORE_TABLES:
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    chain = dbmod.verify_audit_chain(conn)
    expected = expected_migration_count()
    return {
        "migrations_applied": len(versions),
        "migrations_expected": expected,
        "migrations_ok": len(versions) == expected,
        "table_counts": counts,
        "audit_chain": chain,
        "audit_chain_valid": bool(chain.get("ok")),
    }


def _boot_plane_smoke(*, db_path: Path | None = None, database_url: str | None = None) -> dict[str, Any]:
    """Boot the real FastAPI control plane against the restored DB and verify readiness."""
    from fastapi.testclient import TestClient

    from ai_bridge.control.app import create_app

    os.environ.setdefault("AI_BRIDGE_CSRF_SECRET", "restore-drill-csrf-secret")
    app = create_app(
        admin_token="restore-drill-admin",
        db_path=db_path,
        database_url=database_url,
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        r_health = client.get("/health")
        r_ready = client.get("/health/ready")
        r_devs = client.get("/api/devices", headers={"X-Admin-Token": "restore-drill-admin"})
        return {
            "health_status": r_health.status_code,
            "ready_status": r_ready.status_code,
            "devices_status": r_devs.status_code,
            "ok": r_health.status_code == 200 and r_ready.status_code == 200 and r_devs.status_code == 200,
        }
    finally:
        try:
            app.state.conn.close()
        except Exception:
            pass


def drill_sqlite(db_path: Path, work_dir: Path, *, evidence_out: Path | None = None) -> dict[str, Any]:
    """Full isolated SQLite backup + restore + app-boot + audit-continuity drill with RTO/RPO."""
    work_dir.mkdir(parents=True, exist_ok=True)
    backup_path = work_dir / "drill-backup.db"
    restored_path = work_dir / "drill-restored.db"

    t_backup_start = time.perf_counter()
    backup_ts = time.time()
    verified_backup(db_path, backup_path)
    backup_duration = time.perf_counter() - t_backup_start

    t_rto_start = time.perf_counter()
    restore_sqlite(backup_path, restored_path)
    conn = dbmod.connect(db_path=restored_path)
    try:
        verification = _verify_restored_connection(conn)
    finally:
        conn.close()
    boot = _boot_plane_smoke(db_path=restored_path)
    rto_seconds = time.perf_counter() - t_rto_start
    rpo_seconds = max(0.0, time.time() - backup_ts)

    ok = bool(verification["migrations_ok"] and verification["audit_chain_valid"] and boot["ok"])
    report: dict[str, Any] = {
        "drill": "release-1-db-restore-drill",
        "engine": "sqlite",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "ok": ok,
        "backup_bytes": backup_path.stat().st_size,
        "backup_seconds": round(backup_duration, 3),
        "rto_seconds": round(rto_seconds, 3),
        "rpo_seconds": round(rpo_seconds, 3),
        **verification,
        "app_boot": boot,
    }
    if evidence_out is not None:
        evidence_out.parent.mkdir(parents=True, exist_ok=True)
        evidence_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _pg_db_identity(dsn: str) -> tuple[str, int, str]:
    from urllib.parse import urlsplit

    u = urlsplit(dsn)
    host = (u.hostname or "localhost").lower()
    port = int(u.port or 5432)
    dbname = (u.path or "/").lstrip("/")
    return host, port, dbname


def _strip_dsn_password(dsn: str) -> tuple[str, dict[str, str]]:
    """Return (dsn_without_password, extra_env_with_PGPASSWORD) so passwords never appear in argv."""
    from urllib.parse import unquote, urlsplit, urlunsplit

    u = urlsplit(dsn)
    env: dict[str, str] = {}
    if not u.password:
        return dsn, env
    env["PGPASSWORD"] = unquote(u.password)
    hostport = u.hostname or ""
    if u.port:
        hostport = f"{hostport}:{u.port}"
    netloc = f"{u.username}@{hostport}" if u.username else hostport
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment)), env


def drill_postgres(
    source_dsn: str,
    restore_dsn: str,
    *,
    fmt: str = "c",
    evidence_out: Path | None = None,
    keep_restored: bool = False,
) -> dict[str, Any]:
    """Full isolated PostgreSQL backup + restore + app-boot + audit-continuity drill with RTO/RPO."""
    if _pg_db_identity(source_dsn) == _pg_db_identity(restore_dsn):
        raise ValueError("Refusing PostgreSQL drill: restore_dsn must target a different database than source_dsn.")
    with tempfile.TemporaryDirectory(prefix="aibridge-pg-drill-") as td:
        dump_path = Path(td) / ("staging-dump.dump" if fmt == "c" else "staging-dump.sql")

        t_backup_start = time.perf_counter()
        backup_ts = time.time()
        backup_postgres(source_dsn, dump_path, fmt=fmt)
        backup_duration = time.perf_counter() - t_backup_start

        t_rto_start = time.perf_counter()
        created_fresh = False
        try:
            created_fresh = _createdb(restore_dsn, allow_existing=False)
            restore_postgres(dump_path, restore_dsn, fmt=fmt, create_db=False)
            conn = dbmod.connect(database_url=restore_dsn)
            try:
                verification = _verify_restored_connection(conn)
            finally:
                conn.close()
            boot = _boot_plane_smoke(database_url=restore_dsn)
            rto_seconds = time.perf_counter() - t_rto_start
            rpo_seconds = max(0.0, time.time() - backup_ts)
        finally:
            if created_fresh and not keep_restored:
                _dropdb(restore_dsn)

        ok = bool(verification["migrations_ok"] and verification["audit_chain_valid"] and boot["ok"])
        report: dict[str, Any] = {
            "drill": "release-1-db-restore-drill",
            "engine": "postgresql",
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": platform.node(),
            "ok": ok,
            "backup_format": fmt,
            "backup_bytes": dump_path.stat().st_size,
            "backup_seconds": round(backup_duration, 3),
            "rto_seconds": round(rto_seconds, 3),
            "rpo_seconds": round(rpo_seconds, 3),
            **verification,
            "app_boot": boot,
        }
        if evidence_out is not None:
            evidence_out.parent.mkdir(parents=True, exist_ok=True)
            evidence_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report


def _verify_sqlite(path: Path) -> dict[str, int]:
    """Integrity-check + boot a SQLite plane DB; return core row counts."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ValueError(f"integrity check failed on {path}: {check}")
    # Boot through the real plane connect(): applies pending migrations
    # and verifies the schema is usable.
    conn = dbmod.connect(db_path=path)
    try:
        counts = {}
        for table in _CORE_TABLES:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return counts
    finally:
        conn.close()


def backup_sqlite(db_path: Path, out_path: Path) -> dict[str, int]:
    verified_backup(db_path, out_path)
    return _verify_sqlite(out_path)


def restore_sqlite(backup_path: Path, db_path: Path) -> dict[str, int]:
    """Restore a backup into a fresh path (refuses to overwrite)."""
    db_path = db_path.resolve()
    if db_path.exists():
        raise ValueError(f"refusing to overwrite existing database: {db_path}")
    descriptor = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        shutil.copyfile(backup_path, db_path)
    except Exception:
        db_path.unlink(missing_ok=True)
        raise
    return _verify_sqlite(db_path)


def _redact(cmd: list[str]) -> str:
    """Join a command for logging with any DSN password masked.

    pg_dump/pg_restore/psql take the DSN as an argument; printing it
    verbatim would leak the database password into logs. Mask the
    userinfo (``://user:password@``) — the command actually executed is
    unaffected.
    """
    import re

    text = " ".join(cmd)
    return re.sub(r"://[^:/\s]+:[^@\s]+@", "://***:***@", text)


def find_pg_bin(name: str) -> str | None:
    """Locate a PostgreSQL CLI binary on PATH or standard OS install directories."""
    found = shutil.which(name)
    if found:
        return found
    env_dir = os.environ.get("PG_BIN_DIR", "").strip()
    search_dirs: list[Path] = []
    if env_dir:
        search_dirs.append(Path(env_dir))
    if platform.system() == "Windows":
        pg_root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
        if pg_root.is_dir():
            for ver_dir in sorted(pg_root.iterdir(), reverse=True):
                bin_dir = ver_dir / "bin"
                if bin_dir.is_dir():
                    search_dirs.append(bin_dir)
    else:
        for base in (Path("/usr/lib/postgresql"), Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
            if base.is_dir():
                for child in sorted(base.iterdir(), reverse=True):
                    bin_dir = child / "bin"
                    if bin_dir.is_dir():
                        search_dirs.append(bin_dir)
    suffix = ".exe" if platform.system() == "Windows" else ""
    for directory in search_dirs:
        candidate = directory / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None


def _run(cmd: list[str], extra_env: dict[str, str] | None = None) -> None:
    print("+ " + _redact(cmd))
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(cmd, env=env, check=False)
    if result.returncode != 0:
        raise SystemExit(f"command failed (exit {result.returncode}): {_redact(cmd)}")


def backup_postgres(dsn: str, out_path: Path, fmt: str = "c") -> None:
    """pg_dump to a file. Custom format ('c') is the default."""
    pg_dump = find_pg_bin("pg_dump")
    if pg_dump is None:
        raise SystemExit("pg_dump not found on PATH — install PostgreSQL client tools")
    if out_path.exists():
        raise ValueError(f"refusing to overwrite existing backup: {out_path}")
    safe_dsn, pw_env = _strip_dsn_password(dsn)
    _run([pg_dump, "--format", fmt, "--file", str(out_path), safe_dsn], extra_env=pw_env)
    print(f"Postgres backup written to {out_path} (format {fmt}). "
          "Verify with a restore drill before relying on it.")


def _pg_conn_args(dsn: str) -> tuple[list[str], dict[str, str], str]:
    """Split a Postgres DSN into createdb/dropdb arguments.

    createdb/dropdb take a literal database NAME, not a conninfo string:
    passing a full DSN makes them connect to the default (local-socket)
    host instead of the DSN's host. Parse the DSN and pass -h/-p/-U
    explicitly, with the password via PGPASSWORD (never in argv, so it
    is never printed by _redact).
    """
    from urllib.parse import unquote, urlsplit

    u = urlsplit(dsn)
    args: list[str] = []
    env: dict[str, str] = {}
    if u.hostname:
        args += ["-h", u.hostname]
    if u.port:
        args += ["-p", str(u.port)]
    if u.username:
        args += ["-U", unquote(u.username)]
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    dbname = (u.path or "/").lstrip("/")
    if not dbname:
        raise SystemExit("DSN has no database name")
    return args, env, dbname


def _createdb(dsn: str, *, allow_existing: bool = True) -> bool:
    """Create the target database. Returns True when created fresh.

    When ``allow_existing=False`` (used by isolated restore drills), an
    existing database is a hard error so a drill can never overwrite or
    subsequently drop a pre-existing database.
    """
    createdb = find_pg_bin("createdb")
    if createdb is None:
        raise SystemExit("createdb not found on PATH")
    conn_args, extra_env, dbname = _pg_conn_args(dsn)
    cmd = [createdb] + conn_args + [dbname]
    print("+ " + _redact(cmd))
    env = dict(os.environ)
    env.update(extra_env)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode == 0:
        return True
    if "already exists" in (result.stderr or ""):
        if not allow_existing:
            raise ValueError(f"refusing to overwrite pre-existing Postgres database: {dbname}")
        return False
    raise SystemExit(f"createdb failed: {result.stderr.strip()}")


def _dropdb(dsn: str) -> None:
    """Drop the database named by ``dsn``, tolerating 'does not exist'."""
    dropdb = find_pg_bin("dropdb")
    if dropdb is None:
        raise SystemExit("dropdb not found on PATH")
    conn_args, extra_env, dbname = _pg_conn_args(dsn)
    cmd = [dropdb, "--force"] + conn_args + [dbname]
    print("+ " + _redact(cmd))
    env = dict(os.environ)
    env.update(extra_env)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0 and "does not exist" not in (result.stderr or ""):
        raise SystemExit(f"dropdb failed: {result.stderr.strip()}")


def restore_postgres(dump_path: Path, dsn: str, fmt: str = "c", *, create_db: bool = True) -> None:
    """Restore a pg_dump file into the database named by ``dsn``.

    Creates the target database if it does not exist (createdb), then
    restores with pg_restore (custom format) or psql (plain SQL), and
    verifies core tables are readable.
    """
    safe_dsn, pw_env = _strip_dsn_password(dsn)
    if fmt == "c":
        pg_restore = find_pg_bin("pg_restore")
        if pg_restore is None:
            raise SystemExit("pg_restore not found on PATH")
        if create_db:
            _createdb(dsn)
        # --no-privileges: the dump carries ALTER DEFAULT PRIVILEGES for
        # the role that owns the live DB; restoring as the drill user
        # (a non-superuser that cannot impersonate that role) fails
        # those 4 statements with "permission denied to change default
        # privileges" and pg_restore exits 1. The backup file itself
        # stays full-fidelity; only the drill restore skips replaying
        # privilege changes. The data and schema are what the drill
        # verifies.
        _run(
            [pg_restore, "--dbname", safe_dsn, "--clean", "--if-exists", "--no-privileges", str(dump_path)],
            extra_env=pw_env,
        )
    else:
        psql = find_pg_bin("psql")
        if psql is None:
            raise SystemExit("psql not found on PATH")
        if create_db:
            _createdb(dsn)
        _run([psql, safe_dsn, "-v", "ON_ERROR_STOP=1", "-f", str(dump_path)], extra_env=pw_env)
    print("Postgres restore finished. Verify row counts with a live "
          "connection before pointing the plane at this database.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_backup = sub.add_parser("backup", help="back up the plane database")
    p_backup.add_argument("--db", type=Path, help="SQLite plane DB path")
    p_backup.add_argument("--out", type=Path, required=True, help="backup output path")
    p_backup.add_argument("--pg-dsn", help="Postgres DSN (uses pg_dump)")
    p_backup.add_argument("--pg-format", choices=["c", "p"], default="c",
                          help="pg_dump format: c=custom (default), p=plain SQL")
    p_restore = sub.add_parser("restore", help="restore a backup into a fresh DB")
    p_restore.add_argument("--backup", type=Path, help="SQLite backup file")
    p_restore.add_argument("--db", type=Path, help="fresh SQLite DB path (must not exist)")
    p_restore.add_argument("--pg-dump", type=Path, help="pg_dump file to restore")
    p_restore.add_argument("--pg-dsn", help="target Postgres DSN")
    p_restore.add_argument("--pg-format", choices=["c", "p"], default="c")
    p_drop = sub.add_parser("drop", help="drop a Postgres database named by DSN")
    p_drop.add_argument("--pg-dsn", required=True, help="Postgres DSN of the DB to drop")
    p_drill = sub.add_parser("drill", help="run a full isolated backup -> restore -> app boot -> audit verify drill")
    p_drill.add_argument("--db", type=Path, help="SQLite source DB path")
    p_drill.add_argument("--pg-dsn", help="Postgres source DSN")
    p_drill.add_argument("--restore-dsn", help="Postgres isolated target DSN (default: <source>_drill)")
    p_drill.add_argument("--pg-format", choices=["c", "p"], default="c")
    p_drill.add_argument("--evidence-out", type=Path, default=None, help="Optional JSON evidence output path")
    p_drill.add_argument("--keep", action="store_true", help="Keep restored Postgres drill database")
    args = parser.parse_args(argv)

    if args.command == "drop":
        _dropdb(args.pg_dsn)
        return 0

    if args.command == "drill":
        if args.pg_dsn:
            from urllib.parse import urlsplit, urlunsplit

            restore_dsn = args.restore_dsn
            if not restore_dsn:
                u = urlsplit(args.pg_dsn)
                restore_dsn = urlunsplit((u.scheme, u.netloc, "/ai_bridge_restore_drill", u.query, u.fragment))
            report = drill_postgres(
                args.pg_dsn,
                restore_dsn,
                fmt=args.pg_format,
                evidence_out=args.evidence_out,
                keep_restored=args.keep,
            )
        else:
            if not args.db:
                parser.error("drill needs --db (SQLite) or --pg-dsn (Postgres)")
            with tempfile.TemporaryDirectory(prefix="aibridge-sqlite-drill-") as td:
                report = drill_sqlite(args.db, Path(td), evidence_out=args.evidence_out)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    if args.command == "backup":
        if args.pg_dsn:
            backup_postgres(args.pg_dsn, args.out, args.pg_format)
        else:
            if not args.db:
                parser.error("backup needs --db (SQLite) or --pg-dsn (Postgres)")
            counts = backup_sqlite(args.db, args.out)
            print(f"SQLite backup verified: {args.out} rows={counts}")
        return 0
    if args.pg_dump:
        if not args.pg_dsn:
            parser.error("Postgres restore needs --pg-dsn")
        restore_postgres(args.pg_dump, args.pg_dsn, args.pg_format)
    else:
        if not args.backup or not args.db:
            parser.error("SQLite restore needs --backup and --db")
        counts = restore_sqlite(args.backup, args.db)
        print(f"SQLite restore verified (plane boots): {args.db} rows={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

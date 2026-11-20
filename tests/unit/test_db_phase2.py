"""Wave 3A Phase 2 — database tests.

Covers: migration runner rewrite (splitter, per-file atomicity, fail-fast,
down migrations), migration 014 (stream_tickets), migration 015 (FKs +
CHECKs + handoff/parent columns + missing indexes), DB-03 schema parity
script, retention jobs (DB-07), backup/restore drill (DB-01), and the
OIDC one-time exchange code (F-05-15 primitives).

Postgres paths are intentionally NOT executed here: no Postgres server
exists in this environment, so the Postgres side is scripted in
scripts/backup_restore.py and recorded as blocked in
analysis/wave3a-progress.md.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_bridge.control import db as dbmod
from ai_bridge.control import migrations, oidc

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


@pytest.fixture
def conn(tmp_path):
    c = dbmod.connect(db_path=tmp_path / "t.db")
    c.execute("PRAGMA foreign_keys=ON")
    yield c
    c.close()


def _table_columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_names(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def _seed_plane(conn) -> float:
    """Seed one row per core table; returns the seed timestamp."""
    now = dbmod.now()
    conn.execute(
        "INSERT INTO users VALUES (?,?,?,?,?,?,?,0)",
        ("u1", "alice", "h", "s", "operator", "default", now),
    )
    conn.execute(
        "INSERT INTO devices (id, name, hostname, os, tenant, token_hash, status,"
        " last_seen, policy_json, revoked, created_at, capabilities_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'[]')",
        ("d1", "srv", "srv", "linux", "default", "tok", "online", now, "{}", 0, now),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        ("th", "alice", "operator", "default", now + 99, now),
    )
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, tier, status,"
        " created_at, updated_at, epoch, handoff_id, parent_task_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("t1", "d1", "default", "get_system_info", "{}", 1, "queued", now, now, 0, "h1", None),
    )
    conn.execute(
        "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        ("a1", "t1", "run_shell", "r", "default", "pending", now, None, None, None),
    )
    conn.execute(
        "INSERT INTO task_events (id, task_id, event, detail_json, created_at) VALUES (?,?,?,?,?)",
        ("e1", "t1", "created", "{}", now),
    )
    conn.execute(
        "INSERT INTO telemetry (device_id, tenant, cpu_percent, memory_percent,"
        " disk_percent, recorded_at) VALUES (?,?,?,?,?,?)",
        ("d1", "default", 10.0, 20.0, 30.0, now),
    )
    conn.commit()
    return now


def _applied_versions(conn) -> set[str]:
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


# ---------------------------------------------------------------- 014/015
def test_boot_applies_014_and_015(conn):
    versions = _applied_versions(conn)
    assert "014" in versions and "015" in versions
    cols = _table_columns(conn, "tasks")
    assert "handoff_id" in cols and "parent_task_id" in cols
    assert "created_at" in _table_columns(conn, "task_submissions")
    for name in (
        "idx_tasks_handoff_id",
        "idx_tasks_parent_task",
        "idx_tasks_status_updated",
        "idx_stream_tickets_expires",
        "idx_devices_tenant_name",
        "idx_tasks_tenant_created",
    ):
        assert name in _index_names(conn), name


def test_stream_ticket_roundtrip_single_use_and_expiry(conn):
    ticket, ttl = dbmod.mint_stream_ticket(
        conn, actor="alice", tenant="default", device_id="d1", session_id="s1"
    )
    assert ttl == dbmod.STREAM_TICKET_TTL_S
    row = dbmod.redeem_stream_ticket(conn, ticket, device_id="d1", session_id="s1")
    assert row is not None and row["actor"] == "alice"
    # Single-use: replay fails.
    assert dbmod.redeem_stream_ticket(conn, ticket, device_id="d1", session_id="s1") is None
    # Wrong device / session fails without consuming.
    ticket2, _ = dbmod.mint_stream_ticket(
        conn, actor="alice", tenant="default", device_id="d1", session_id="s1"
    )
    assert dbmod.redeem_stream_ticket(conn, ticket2, device_id="other", session_id="s1") is None
    assert dbmod.redeem_stream_ticket(conn, ticket2, device_id="d1", session_id="other") is None
    assert dbmod.redeem_stream_ticket(conn, ticket2, device_id="d1", session_id="s1") is not None
    # Expired fails.
    ticket3, _ = dbmod.mint_stream_ticket(
        conn, actor="alice", tenant="default", device_id="d1", session_id="s1"
    )
    conn.execute(
        "UPDATE stream_tickets SET expires_at=? WHERE ticket_hash=?",
        (dbmod.now() - 1, dbmod.hash_token(ticket3)),
    )
    conn.commit()
    assert dbmod.redeem_stream_ticket(conn, ticket3, device_id="d1", session_id="s1") is None


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO tasks (id, device_id, tenant, tool, status, created_at, updated_at)"
        " VALUES ('tx','d1','default','x','bogus',1,1)",
        "INSERT INTO tasks (id, device_id, tenant, tool, tier, status, created_at, updated_at)"
        " VALUES ('tx','d1','default','x',9,'queued',1,1)",
        "INSERT INTO tasks (id, device_id, tenant, tool, tier, status, created_at, updated_at)"
        " VALUES ('tx','d1','default','x',0,'queued',1,1)",
        "INSERT INTO users VALUES ('u2','bob','h','s','superuser','default',1,0)",
        "INSERT INTO devices (id, name, tenant, token_hash, status, policy_json, revoked, created_at)"
        " VALUES ('d2','x','default','t','weird','{}',0,1)",
        "INSERT INTO approvals VALUES ('a2','t1','x','r','default','maybe',1,NULL,NULL,NULL,0)",
    ],
)
def test_015_check_constraints_reject_bad_enums(conn, sql):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)
        conn.commit()
    conn.rollback()


def test_015_foreign_keys_and_cascades(conn):
    _seed_plane(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO task_events (id, task_id, event, detail_json, created_at) VALUES ('e9','nope','created','{}',1)")
        conn.commit()
    conn.rollback()
    conn.execute("DELETE FROM tasks WHERE id='t1'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
    conn.execute("DELETE FROM devices WHERE id='d1'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 0
    conn.execute("DELETE FROM users WHERE id='u1'")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_devices_status_revoked_is_valid(conn):
    """Regression (Wave 3A fix): the revoke endpoint sets status='revoked'
    (app.py revoke), so the devices CHECK must include it — the first
    015 draft only allowed online/offline and broke device revocation."""
    now = dbmod.now()
    conn.execute(
        "INSERT INTO devices (id, name, tenant, token_hash, status, last_seen, created_at)"
        " VALUES ('d1','s','default','t','revoked',?,?)",
        (now, now),
    )
    conn.commit()
    assert conn.execute("SELECT status FROM devices WHERE id='d1'").fetchone()[0] == "revoked"


def test_approvals_fk_is_deferred_for_create_task_order(conn):
    """Regression (Wave 3A fix): task_service.create_task inserts the
    approval row BEFORE the task row in one transaction. The approvals
    FK is therefore DEFERRABLE INITIALLY DEFERRED: the approval-before-
    task insert must commit, while an approval whose task never appears
    must still fail at commit."""
    conn.execute(
        "INSERT INTO approvals (id, task_id, tool, reason, status, created_at)"
        " VALUES ('a1','t9','run_shell','r','pending',1)"
    )
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, status, created_at, updated_at)"
        " VALUES ('t9','d1','default','run_shell','needs_approval',1,1)"
    )
    conn.commit()  # must not raise
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    conn.execute(
        "INSERT INTO approvals (id, task_id, tool, reason, status, created_at)"
        " VALUES ('a2','ghost','run_shell','r','pending',1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.commit()
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1


def test_015_down_migration_preserves_rows_and_reapplies(conn):
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, tier, status,"
        " created_at, updated_at) VALUES ('t1','d1','default','get_system_info','{}',1,'queued',1,1)"
    )
    conn.commit()
    migrations.run_down_migration(conn, "015")
    assert "handoff_id" not in _table_columns(conn, "tasks")
    assert "parent_task_id" not in _table_columns(conn, "tasks")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='015'").fetchone()[0] == 0
    # CHECKs are gone again: a bogus status is accepted on the old schema.
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, status, created_at, updated_at)"
        " VALUES ('tx','d1','default','x','bogus',1,1)"
    )
    conn.commit()
    migrations.run_down_migration(conn, "014")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='stream_tickets'"
        ).fetchone()[0]
        == 0
    )
    # 015 correctly refuses to re-apply over data violating its CHECKs.
    # Per-file atomicity: the 014 file applies (and commits) before 015
    # fails and rolls back, so only 015 is left unapplied.
    with pytest.raises(Exception):
        migrations.run_migrations(conn, dialect="sqlite")
    versions = _applied_versions(conn)
    assert "014" in versions and "015" not in versions
    conn.execute("DELETE FROM tasks WHERE id='tx'")
    conn.commit()
    applied = migrations.run_migrations(conn, dialect="sqlite")
    assert applied == ["015"]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_minimal_pre013_db_applies_013_014_015_016(tmp_path):
    raw = sqlite3.connect(str(tmp_path / "min.db"))
    raw.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,"
        " password_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer',"
        " created_at REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0)"
    )
    raw.execute(
        "CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,"
        " role TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL)"
    )
    raw.execute(
        "CREATE TABLE approvals (id TEXT PRIMARY KEY, task_id TEXT, tool TEXT, reason TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, decided_by TEXT,"
        " expires_at REAL, decided_at REAL, policy_version INTEGER NOT NULL DEFAULT 0)"
    )
    raw.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, device_id TEXT, tenant TEXT, tool TEXT,"
        " args_json TEXT, tier INTEGER, status TEXT, approval_id TEXT, created_at REAL,"
        " updated_at REAL, result TEXT)"
    )
    raw.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at REAL NOT NULL)"
    )
    for v in ("001", "002", "003", "004", "005", "006", "007", "008", "009", "010", "011", "012"):
        raw.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (v, f"{v}_x.sql", 1.0))
    raw.execute(
        "INSERT INTO users VALUES (?,?,?,?,?,?,0)", ("u1", "alice", "hash", "salt", "operator", 1.0)
    )
    raw.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,1,'queued',NULL,?, ?, '')",
        ("t1", "d1", "default", "get_system_info", "{}", 1.0, 1.0),
    )
    raw.commit()
    applied = migrations.run_migrations(raw)
    # 016 (Wave 6A, Gate 16) and 017 (Release 2) also apply cleanly on minimal DBs.
    assert applied == ["013", "014", "015", "016", "017"]
    assert raw.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    raw.close()


# ------------------------------------------------------- runner: splitter
def test_splitter_handles_strings_comments_triggers_dollar_quotes():
    stmts = migrations._split_statements(
        "INSERT INTO t VALUES ('a;b');\n"
        "-- @only: postgres\nALTER TABLE t ADD CONSTRAINT c CHECK (x IN ('a;b'));\n"
        "CREATE TRIGGER trg BEGIN SELECT RAISE(ABORT, 'x;y'); END;\n"
        "CREATE OR REPLACE FUNCTION f() RETURNS void AS $$ BEGIN RAISE NOTICE 'a;b'; END; $$"
        " LANGUAGE plpgsql;"
    )
    assert len(stmts) == 4
    assert len([s for s in stmts if "INSERT" in s["sql"]]) == 1
    assert stmts[1]["only"] == "postgres"
    assert "RAISE NOTICE 'a;b'" in stmts[3]["sql"]
    assert "SELECT RAISE(ABORT, 'x;y')" in stmts[2]["sql"]


def test_migration_atomicity_and_fail_fast(tmp_path):
    bad_dir = tmp_path / "mig_bad"
    bad_dir.mkdir()
    (bad_dir / "001_ok.sql").write_text("CREATE TABLE IF NOT EXISTS a (id TEXT PRIMARY KEY);\n")
    (bad_dir / "002_bad.sql").write_text(
        "CREATE TABLE IF NOT EXISTS b (id TEXT PRIMARY KEY);\n"
        "INSERT INTO b VALUES ('x;y');\n"
        "THIS IS NOT SQL;\n"
    )
    raw = sqlite3.connect(str(tmp_path / "atomic.db"))
    with pytest.raises(Exception):
        migrations.run_migrations(raw, migrations_dir=bad_dir)
    assert raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='b'").fetchone()[0] == 0
    assert (
        raw.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='002'").fetchone()[0]
        == 0
    )
    assert raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='a'").fetchone()[0] == 1
    with pytest.raises(FileNotFoundError):
        migrations.run_migrations(raw, migrations_dir=tmp_path / "nope")
    raw.close()


# ------------------------------------------------------- PERF-03 / DB-07
def test_list_projection_excludes_result(conn):
    now = dbmod.now()
    conn.execute(
        "INSERT INTO tasks (id, device_id, tenant, tool, args_json, tier, status,"
        " created_at, updated_at, result) VALUES ('t1','d1','default','x','{}',1,'completed',?,?,?)",
        (now, now, "R" * 5000),
    )
    conn.commit()
    rows = dbmod.list_tasks_projected(conn, tenant="default")
    assert len(rows) == 1
    assert "result" not in rows[0]
    assert rows[0]["id"] == "t1" and rows[0]["status"] == "completed"
    assert dbmod.get_task_result(conn, "t1") == "R" * 5000
    assert dbmod.get_task_result(conn, "missing") is None


def test_retention_jobs(conn):
    now = dbmod.now()
    conn.execute(
        "INSERT INTO task_submissions (actor, request_key, request_hash, task_id, created_at)"
        " VALUES ('alice','old','h','t1',?)",
        (now - 90000,),
    )
    conn.execute(
        "INSERT INTO task_submissions (actor, request_key, request_hash, task_id, created_at)"
        " VALUES ('alice','new','h','t2',?)",
        (now,),
    )
    conn.commit()
    assert dbmod.prune_task_submissions(conn, ttl_s=86400) == 1
    assert conn.execute("SELECT COUNT(*) FROM task_submissions").fetchone()[0] == 1
    # Audit pruning is WORM-gated: refused without a successful export.
    conn.execute(
        "INSERT INTO audit (ts, tenant, actor, device_id, tool, args_json, decision, result)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (now - 100000, "default", "alice", "d1", "x", "{}", "allow", "ok"),
    )
    conn.commit()
    with pytest.raises(ValueError):
        dbmod.retention_prune_audit(conn, older_than_s=1000, worm_export_ok=False)
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    assert dbmod.retention_prune_audit(conn, older_than_s=1000, worm_export_ok=True) == 1
    # Device cascade: tasks have no FK (virtual 'local' device), so the
    # helper must clean them explicitly.
    _seed_plane(conn)
    counts = dbmod.delete_device_cascade(conn, "d1")
    assert counts["tasks"] == 1 and counts["telemetry"] == 1
    assert conn.execute("SELECT COUNT(*) FROM devices WHERE id='d1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE device_id='d1'").fetchone()[0] == 0


def test_retention_sweep_counts(conn):
    now = dbmod.now()
    conn.execute(
        "INSERT INTO task_submissions (actor, request_key, request_hash, task_id, created_at)"
        " VALUES ('alice','old','h','t1',?)",
        (now - 90000,),
    )
    conn.commit()
    result = dbmod.retention_sweep(conn)
    assert result["task_submissions"] == 1
    assert result["audit"] == 0  # audit untouched without WORM export


# ------------------------------------------------------- DB-01 / DB-03
def test_backup_restore_drill(tmp_path):
    src = tmp_path / "plane.db"
    c = dbmod.connect(db_path=src)
    _seed_plane(c)
    c.close()
    backup = tmp_path / "plane-backup.db"
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT / "src")}
    r = subprocess.run(
        [sys.executable, "scripts/backup_restore.py", "backup", "--db", str(src),
         "--out", str(backup)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert backup.exists()
    restored = tmp_path / "fresh.db"
    r = subprocess.run(
        [sys.executable, "scripts/backup_restore.py", "restore", "--backup", str(backup),
         "--db", str(restored)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "rows=" in r.stdout and "'tasks': 1" in r.stdout
    # Restoring over an existing DB is refused.
    r = subprocess.run(
        [sys.executable, "scripts/backup_restore.py", "restore", "--backup", str(backup),
         "--db", str(restored)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert r.returncode != 0
    # The restored DB boots through the real plane connect() and holds data.
    c2 = dbmod.connect(db_path=restored)
    assert c2.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert c2.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    c2.close()


def test_schema_parity_script_exits_zero():
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT / "src")}
    r = subprocess.run(
        [sys.executable, "scripts/check_schema_parity.py"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PARITY OK" in r.stdout


# ------------------------------------------------------- F-05-15 primitives
@pytest.fixture
def oidc_store(conn):
    oidc.bind_oidc_store(conn)
    yield conn
    oidc.bind_oidc_store(None)
    oidc._EXCHANGE.clear()


def test_oidc_exchange_code_roundtrip_single_use(oidc_store):
    code = oidc.mint_oidc_exchange_code("tok-abc")
    assert code and code != "tok-abc"
    assert oidc.redeem_oidc_exchange_code(code) == "tok-abc"
    # Single-use: replay finds nothing.
    assert oidc.redeem_oidc_exchange_code(code) is None
    assert oidc.redeem_oidc_exchange_code("not-a-code") is None
    # The code is stored hash-only: no row contains the token value.
    rows = oidc_store.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'oidc.exchange.%'"
    ).fetchall()
    assert rows == []
    code2 = oidc.mint_oidc_exchange_code("tok-def")
    rows = oidc_store.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'oidc.exchange.%'"
    ).fetchall()
    assert len(rows) == 1
    assert "tok-def" in rows[0][1]  # value held only until redeemed (60 s max)
    assert rows[0][0].startswith("oidc.exchange.")
    assert oidc.redeem_oidc_exchange_code(code2) == "tok-def"


def test_oidc_exchange_code_expires(oidc_store):
    code = oidc.mint_oidc_exchange_code("tok-exp")
    code_hash = oidc._exchange_code_hash(code)
    oidc_store.execute(
        "UPDATE settings SET value=? WHERE key=?",
        (json.dumps({"token": "tok-exp", "at": time.time() - 120}),
         "oidc.exchange." + code_hash),
    )
    oidc_store.commit()
    assert oidc.redeem_oidc_exchange_code(code) is None


def test_oidc_exchange_code_memory_fallback():
    oidc.bind_oidc_store(None)
    oidc._EXCHANGE.clear()
    code = oidc.mint_oidc_exchange_code("tok-mem")
    assert oidc.redeem_oidc_exchange_code(code) == "tok-mem"
    assert oidc.redeem_oidc_exchange_code(code) is None
    oidc._EXCHANGE.clear()

"""Gate 16 (Wave 6): schema-parity helpers that do not need PostgreSQL.

The cross-dialect comparison itself only runs in CI (postgres-boot job),
where a real PG 18 server exists. These tests pin the normalizers and
the SQLite structural snapshot so a typo there fails locally instead of
in CI: CHECK-expression normalization (Postgres's ``= ANY (ARRAY[...])``
rewriting vs SQLite's ``IN (...)``), type/default canonicalization, and
the structural snapshot's contents (5 CHECKs, 4 FKs, all tables present,
and schema-boot == migrations-boot structurally).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_parity():
    spec = importlib.util.spec_from_file_location(
        "check_schema_parity", REPO_ROOT / "scripts" / "check_schema_parity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_expr_normalization_pg_any_vs_sqlite_in():
    mod = _load_parity()
    pg = "(status)::text = ANY (ARRAY['queued'::text, 'running'::text, 'completed'::text])"
    sqlite = "status IN ('queued', 'running', 'completed')"
    assert mod._normalize_check_expr(pg) == mod._normalize_check_expr(sqlite)
    assert mod._normalize_check_expr(sqlite) == "STATUS IN('QUEUED','RUNNING','COMPLETED')"


def test_check_expr_normalization_tier_range():
    mod = _load_parity()
    pg = "((tier IS NULL OR (tier >= 1 AND tier <= 4)))"
    sqlite = "tier IS NULL OR (tier >= 1 AND tier <= 4)"
    assert mod._normalize_check_expr(pg) == mod._normalize_check_expr(sqlite)


def test_check_expr_normalization_tier_range_real_pg_rendering():
    """Gate 16 CI failure (run 44127496573): pg_get_constraintdef wraps
    EVERY AND/OR operand in parens, not just the whole expression —
    ``(TIER IS NULL)OR((TIER >= 1)AND(TIER <= 4))``. The old test used a
    guessed rendering and passed locally while CI failed. This pins the
    real rendering against the SQLite spelling."""
    mod = _load_parity()
    pg = "(TIER IS NULL)OR((TIER >= 1)AND(TIER <= 4))"
    sqlite = "TIER IS NULL OR (TIER >= 1 AND TIER <= 4)"
    assert mod._normalize_check_expr(pg) == mod._normalize_check_expr(sqlite)
    assert mod._normalize_check_expr(sqlite) == "TIER IS NULL OR TIER >= 1 AND TIER <= 4"


def test_check_expr_normalization_in_list_parens_kept():
    """The paren stripper must never eat an IN (...) list."""
    mod = _load_parity()
    assert mod._normalize_check_expr("status IN ('a', 'b')") == "STATUS IN('A','B')"
    # An OR-chain inside AND keeps its parens (they are load-bearing).
    assert mod._normalize_check_expr("a AND (b OR c)") == "A AND(B OR C)"


def test_canonical_default_epoch_now():
    """Gate 16: the two dialects spell 'epoch seconds now' differently."""
    mod = _load_parity()
    assert mod._canonical_default("strftime('%s','now')") == "epoch_now()"
    assert mod._canonical_default("strftime('%s', 'now')") == "epoch_now()"
    assert mod._canonical_default("EXTRACT(epoch FROM now())") == "epoch_now()"
    assert mod._canonical_default("extract(epoch from now())") == "epoch_now()"


def test_pg_index_cols_strip_direction():
    """Gate 16: pg_get_indexdef renders DESC; SQLite's index_info does
    not report direction, so the comparison must ignore it."""
    mod = _load_parity()
    assert mod._pg_index_cols(
        "CREATE INDEX idx ON public.agent_runs USING btree (agent_id, started_at DESC)"
    ) == "agent_id,started_at"
    assert mod._pg_index_cols(
        'CREATE UNIQUE INDEX idx ON public.t USING btree ("a", "b" ASC)'
    ) == "a,b"
    assert mod._pg_index_cols(None) == ""


def test_render_struct_column_order_insensitive():
    """Gate 16: PG appends tenant via ALTER TABLE (last), SQLite has it
    inline — column order must not count as drift."""
    mod = _load_parity()
    a = {"t": ["id|TEXT|1||1", "tenant|TEXT|1|'default'|0", "name|TEXT|1||0"]}
    b = {"t": ["id|TEXT|1||1", "name|TEXT|1||0", "tenant|TEXT|1|'default'|0"]}
    assert mod._render_struct(a, set(), set(), set(), set()) == mod._render_struct(
        b, set(), set(), set(), set()
    )


def test_migration_016_approvals_task_index(tmp_path):
    """Gate 16: migration 016 adds the idx_approvals_task_id index the
    PG path was missing (015's PG block only adds the FK constraint;
    PG does not auto-index FK columns)."""
    mod = _load_parity()
    db_b = tmp_path / "mig.db"
    applied = mod.build_migrations_db(db_b)
    assert "016" in applied, applied
    snap = mod.snapshot_struct_sqlite(db_b)
    assert any(
        i.startswith("idx_approvals_task_id|approvals|task_id|0")
        for i in snap["indexes"]
    ), snap["indexes"]


def test_canonical_types_and_defaults():
    mod = _load_parity()
    assert mod._canonical_type("DOUBLE PRECISION") == "REAL"
    assert mod._canonical_type("double precision") == "REAL"
    assert mod._canonical_type("integer") == "INTEGER"
    assert mod._canonical_type("character varying") == "TEXT"
    assert mod._canonical_type("text") == "TEXT"
    assert mod._canonical_default("'1'::integer") == "'1'"
    assert mod._canonical_default("''::text") == "''"
    assert mod._canonical_default("nextval('policies_id_seq'::regclass)") == ""
    assert mod._canonical_default(None) == ""
    assert mod._canonical_default(1) == "1"


def test_structural_snapshot_sqlite_contents(tmp_path):
    mod = _load_parity()
    db_b = tmp_path / "mig.db"
    mod.build_migrations_db(db_b)
    snap = mod.snapshot_struct_sqlite(db_b)
    tables = [ln[:-1] for ln in snap["tables"] if ln.endswith(":")]
    assert len(tables) == 26, tables  # 26 tables (including task_dead_letters + update_rollouts from migration 017)
    assert "tasks" in tables and "devices" in tables and "schema_migrations" in tables
    # The 5 enum CHECKs from migration 015 (names differ per dialect;
    # the comparison is by expression, so assert the expressions).
    assert len(snap["checks"]) == 5, snap["checks"]
    assert any("tasks|STATUS IN(" in c for c in snap["checks"])
    assert any("users|ROLE IN(" in c for c in snap["checks"])
    # The 4 FKs from migration 015.
    assert len(snap["fks"]) == 4, snap["fks"]
    assert any(f.startswith("approvals|task_id|tasks|id|CASCADE|NO ACTION") for f in snap["fks"])
    # Explicit indexes only (no PK autoindexes).
    assert all("sqlite_autoindex" not in i for i in snap["indexes"])
    assert any(i.startswith("idx_tasks_device_status|tasks|device_id,status|0") for i in snap["indexes"])


def test_structural_snapshot_schema_boot_equals_migrations_boot(tmp_path):
    """The structural snapshot must agree with the raw-SQL parity check:
    SCHEMA boot and migrations-only boot are the same schema."""
    mod = _load_parity()
    db_a = tmp_path / "schema.db"
    db_b = tmp_path / "mig.db"
    mod.build_schema_db(db_a)
    mod.build_migrations_db(db_b)
    assert mod.snapshot_struct_sqlite(db_a) == mod.snapshot_struct_sqlite(db_b)


def test_sqlite_parity_still_ok(monkeypatch, tmp_path):
    """The original SQLite-vs-SQLite raw comparison still passes."""
    import sys

    mod = _load_parity()
    monkeypatch.setattr(sys, "argv", ["check_schema_parity.py", "--keep", str(tmp_path)])
    assert mod.main() == 0
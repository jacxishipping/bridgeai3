"""Lightweight, zero-dependency database migration runner for AI Bridge.

Tracks applied migrations in the ``schema_migrations`` table and applies
pending versioned migrations in chronological order. Compatible with both
SQLite and PostgreSQL engines.

Wave 3A (DB-08 / F-05-16) rewrite:

* Statements are split by a real lexer, not ``str.split(";")`` — semicolons
  inside string literals, quoted identifiers, ``--``/``/* */`` comments and
  PostgreSQL dollar-quoted strings (``$$ ... $$``, ``$tag$ ... $tag$``) do
  not split. A migration file may therefore contain trigger bodies,
  ``RAISE()`` messages, or JSON defaults containing ``;``.
* Each migration file applies inside ONE transaction (explicit ``BEGIN
  IMMEDIATE`` on SQLite, the connection's ``transaction()`` on Postgres),
  so a failure mid-file rolls the whole file back instead of leaving a
  partially-applied schema behind. The ``schema_migrations`` row is
  written inside the same transaction.
* Fail fast: a failed migration raises (it already did — the old code's
  "logs and continues" reading in report 17 was stale; the partial-apply
  half of F-05-16 was the real defect and is fixed here). A missing
  migrations directory also raises instead of silently returning ``[]``:
  an installed deployment without its migrations is a broken deployment,
  not a clean database.
* Down migrations are supported: :func:`run_down_migration` executes
  ``down_<version>_*.sql`` in one transaction and removes the
  ``schema_migrations`` row.
* Per-statement directives (comment lines immediately preceding a
  statement):
  ``-- @only: postgres`` / ``-- @only: sqlite`` — run the next statement
  on that dialect only (lets one file carry both the Postgres
  ``ALTER TABLE ... ADD CONSTRAINT`` form and the SQLite table-rebuild
  form of the same change).
  ``-- @requires-table: <name>`` — skip the next statement when the
  table does not exist (lets a migration apply cleanly to the minimal
  pre-013-style test databases that pre-fill ``schema_migrations``
  without creating every table).
  File-level ``-- @foreign-keys: off`` — the runner disables SQLite
  foreign-key enforcement around the file (outside the transaction —
  the pragma is a no-op inside one) and restores it afterwards, so a
  table rebuild cannot cascade-delete child rows when the parent table
  is dropped and recreated.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("ai_bridge.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "migrations"

_ONLY_RE = re.compile(r"^\s*--\s*@only:\s*(postgres|postgresql|sqlite)\s*$", re.IGNORECASE)
_REQUIRES_TABLE_RE = re.compile(r"^\s*--\s*@requires-table:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", re.IGNORECASE)
_FOREIGN_KEYS_RE = re.compile(r"^\s*--\s*@foreign-keys:\s*off\s*$", re.IGNORECASE)


def ensure_migrations_table(conn: Any, dialect: str = "sqlite") -> None:
    """Ensure the schema_migrations tracking table exists."""
    if dialect == "sqlite":
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at REAL NOT NULL
            );
        """)
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at DOUBLE PRECISION NOT NULL
            );
        """)
    conn.commit()


def get_applied_versions(conn: Any) -> set[str]:
    """Retrieve all previously applied migration version identifiers."""
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC").fetchall()
    return {r["version"] if isinstance(r, dict) or hasattr(r, "keys") else r[0] for r in rows}


def _sqlite_has_column(conn: Any, table: str, col: str) -> bool:
    """Check if a table in SQLite already has the specified column."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cols = {r["name"] if hasattr(r, "keys") else r[1] for r in rows}
        return col.lower() in {c.lower() for c in cols}
    except Exception:
        return False


def _table_exists(conn: Any, table: str, dialect: str) -> bool:
    """True iff ``table`` exists (used by the @requires-table directive)."""
    try:
        if dialect in ("postgres", "postgresql"):
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name=?", (table,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _split_statements(sql_text: str) -> list[dict[str, Any]]:
    """Split migration SQL into statements without breaking on ``;`` in
    strings, comments, quoted identifiers, or dollar-quoted bodies.

    Returns a list of ``{"sql": str, "only": str | None,
    "requires_tables": list[str]}``. Directive comment lines
    (``-- @only: ...`` / ``-- @requires-table: ...``) attach to the next
    statement and are stripped from the emitted SQL; ordinary comments
    are preserved (harmless) except a leading directive block.
    """
    statements: list[dict[str, Any]] = []
    buf: list[str] = []
    pending_only: str | None = None
    pending_tables: list[str] = []
    i = 0
    n = len(sql_text)
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    dollar_tag: str | None = None
    trigger_depth = 0  # >0 while inside a CREATE TRIGGER ... BEGIN ... END body

    def _statement_head() -> str:
        return "".join(buf).lstrip()[:40].upper()

    def flush() -> None:
        nonlocal buf, pending_only, pending_tables, trigger_depth
        text = "".join(buf).strip()
        buf = []
        trigger_depth = 0
        if text:
            statements.append(
                {"sql": text, "only": pending_only, "requires_tables": list(pending_tables)}
            )
        pending_only = None
        pending_tables = []

    while i < n:
        ch = sql_text[i]
        nxt = sql_text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                buf.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if dollar_tag is not None:
            if sql_text.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(ch)
                i += 1
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":  # escaped quote
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(ch)
            if ch == '"':
                if nxt == '"':
                    buf.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        # Not inside any literal/comment.
        if ch == "-" and nxt == "-":
            # Line comment: check for a directive, else skip to newline.
            eol = sql_text.find("\n", i)
            line = sql_text[i : eol if eol != -1 else n]
            m_only = _ONLY_RE.match(line)
            m_table = _REQUIRES_TABLE_RE.match(line)
            if m_only is not None:
                pending_only = m_only.group(1).lower()
                if pending_only == "postgresql":
                    pending_only = "postgres"
            elif m_table is not None:
                pending_tables.append(m_table.group(1))
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql_text[i:])
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            if trigger_depth:
                buf.append(ch)
                i += 1
                continue
            buf.append(ch)
            flush()
            i += 1
            continue
        # CREATE TRIGGER bodies: BEGIN ...; END; — inner semicolons do not
        # terminate the statement; only the semicolon after the matching
        # END does (tracked above via trigger_depth).
        if trigger_depth == 0 and _statement_head().startswith("CREATE TRIGGER"):
            m_begin = re.match(r"BEGIN\b", sql_text[i:], re.IGNORECASE)
            if m_begin:
                trigger_depth = 1
                buf.append(sql_text[i : i + m_begin.end()])
                i += m_begin.end()
                continue
        elif trigger_depth > 0:
            m_begin = re.match(r"BEGIN\b", sql_text[i:], re.IGNORECASE)
            m_end = re.match(r"END\b", sql_text[i:], re.IGNORECASE)
            if m_begin:
                trigger_depth += 1
                buf.append(sql_text[i : i + m_begin.end()])
                i += m_begin.end()
                continue
            if m_end:
                trigger_depth -= 1
                buf.append(sql_text[i : i + m_end.end()])
                i += m_end.end()
                continue
        buf.append(ch)
        i += 1
    flush()
    return statements


def _adapt_and_execute_statement(conn: Any, stmt: str, dialect: str) -> None:
    """Execute a single SQL statement with dialect-specific adaptations."""
    stmt = stmt.strip()
    if not stmt:
        return

    if dialect == "sqlite":
        # Check for ALTER TABLE <table> ADD COLUMN [IF NOT EXISTS] <col>
        alter_match = re.search(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)", stmt, re.IGNORECASE)
        if alter_match:
            table_name = alter_match.group(1)
            col_name = alter_match.group(2)
            if _sqlite_has_column(conn, table_name, col_name):
                logger.debug("Column %s.%s already exists, skipping ADD COLUMN", table_name, col_name)
                return
            # Remove IF NOT EXISTS for SQLite syntax
            stmt = re.sub(r"\bADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\b", "ADD COLUMN", stmt, flags=re.IGNORECASE)

        # Type adaptations
        stmt = re.sub(r"\bSERIAL\s+PRIMARY\s+KEY\b", "INTEGER PRIMARY KEY AUTOINCREMENT", stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"\bDOUBLE\s+PRECISION\b", "REAL", stmt, flags=re.IGNORECASE)

    elif dialect in ("postgres", "postgresql"):
        stmt = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "SERIAL PRIMARY KEY", stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"\bREAL\b", "DOUBLE PRECISION", stmt, flags=re.IGNORECASE)

    conn.execute(stmt)


def _dialect_of(dialect: str) -> str:
    return "postgres" if dialect in ("postgres", "postgresql") else "sqlite"


@contextlib.contextmanager
def _file_transaction(conn: Any, dialect: str):
    """One transaction for one migration file.

    SQLite: explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK on the raw
    connection (the wrapper's ``transaction()`` relies on implicit BEGIN
    on first DML, which never fires for a file that starts with DDL —
    each statement would auto-commit and a mid-file failure would leave
    a partial schema). Postgres: the connection's ``transaction()``.
    """
    if _dialect_of(dialect) == "postgres":
        txn = getattr(conn, "transaction", None)
        if txn is not None:
            with txn():
                yield
            return
    raw = getattr(conn, "_conn", conn)
    lock = getattr(conn, "_lock", None)
    ctx = lock if lock is not None else contextlib.nullcontext()
    with ctx:
        began = False
        try:
            raw.execute("BEGIN IMMEDIATE")
            began = True
        except Exception as exc:
            # Already inside a transaction (caller owns it): run the
            # statements in the caller's transaction instead of failing.
            if "cannot start a transaction within a transaction" not in str(exc).lower():
                raise
        try:
            yield
            if began:
                raw.execute("COMMIT")
        except Exception:
            if began:
                with contextlib.suppress(Exception):
                    raw.execute("ROLLBACK")
            raise


def _apply_file(
    conn: Any,
    sql_text: str,
    dialect: str,
    *,
    record_version: str | None = None,
    record_name: str | None = None,
    delete_version: str | None = None,
) -> None:
    """Apply one migration file's statements in a single transaction.

    The optional ``schema_migrations`` record insert (up) or delete
    (down) runs inside the same transaction as the file's statements,
    so a failed file never leaves a half-applied schema behind.
    """
    dialect = _dialect_of(dialect)
    statements = _split_statements(sql_text)
    foreign_keys_off = bool(_FOREIGN_KEYS_RE.search(sql_text))
    saved_fk: Any = None
    if dialect == "sqlite" and foreign_keys_off:
        try:
            saved_fk = conn.execute("PRAGMA foreign_keys").fetchone()
            saved_fk = saved_fk[0] if saved_fk is not None else 0
            conn.execute("PRAGMA foreign_keys=OFF")
        except Exception:
            saved_fk = None
    try:
        with _file_transaction(conn, dialect):
            for item in statements:
                only = item["only"]
                if only is not None and only != dialect:
                    continue
                missing = [
                    t for t in item["requires_tables"] if not _table_exists(conn, t, dialect)
                ]
                if missing:
                    logger.debug(
                        "Skipping statement: table(s) %s do not exist", ",".join(missing)
                    )
                    continue
                _adapt_and_execute_statement(conn, item["sql"], dialect)
            if record_version is not None:
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (version) DO NOTHING",
                    (record_version, record_name or record_version, time.time()),
                )
            if delete_version is not None:
                conn.execute(
                    "DELETE FROM schema_migrations WHERE version=?", (delete_version,)
                )
    finally:
        if dialect == "sqlite" and foreign_keys_off and saved_fk is not None:
            with contextlib.suppress(Exception):
                conn.execute(f"PRAGMA foreign_keys={'ON' if saved_fk else 'OFF'}")


def run_migrations(conn: Any, dialect: str = "sqlite", migrations_dir: Path | None = None) -> list[str]:
    """Discover and apply all unapplied migration files in chronological order.

    Fail fast: a missing migrations directory raises FileNotFoundError
    (a deployment without its migrations is broken, not clean), and a
    failed file raises after rolling its whole transaction back — the
    runner never records a half-applied migration and never continues
    past a failure.
    """
    mdir = migrations_dir or MIGRATIONS_DIR
    if not mdir.exists():
        raise FileNotFoundError(
            f"Migrations directory not found at {mdir}; refusing to start "
            "with an unverified schema (DB-08). Restore the migrations/ "
            "directory from the release before starting the control plane."
        )

    pg_locked = False
    if dialect == "postgres":
        try:
            conn.execute("SELECT pg_advisory_lock(719283744)")
            pg_locked = True
        except Exception:
            pg_locked = False
    try:
        ensure_migrations_table(conn, dialect)
        applied = get_applied_versions(conn)

        sql_files = sorted([f for f in mdir.glob("*.sql") if re.match(r"^\d+_", f.name)])
        newly_applied: list[str] = []

        for fpath in sql_files:
            version = fpath.stem.split("_")[0]
            if version in applied:
                continue

            raw_sql = fpath.read_text(encoding="utf-8")
            logger.info("Applying database migration [%s] %s ...", version, fpath.name)

            try:
                _apply_file(conn, raw_sql, dialect, record_version=version, record_name=fpath.name)
                newly_applied.append(version)
                logger.info("Successfully applied migration [%s].", version)
            except Exception as exc:
                logger.error("Failed to apply migration [%s] %s: %s", version, fpath.name, exc)
                raise

        return newly_applied
    finally:
        if pg_locked:
            try:
                conn.execute("SELECT pg_advisory_unlock(719283744)")
            except Exception:
                pass


def run_down_migration(
    conn: Any,
    version: str,
    dialect: str = "sqlite",
    migrations_dir: Path | None = None,
) -> str:
    """Roll one migration back: execute ``down_<version>_*.sql`` in a
    single transaction and remove the ``schema_migrations`` row.

    Raises FileNotFoundError when no down file exists for the version, and
    re-raises any SQL failure after rolling back. The down file itself is
    expected to contain its own ``DELETE FROM schema_migrations`` (see
    ``down_013_tenant_isolation.sql``); the runner deletes the row as
    well, so a file without that line still rolls back cleanly.
    """
    mdir = migrations_dir or MIGRATIONS_DIR
    if not mdir.exists():
        raise FileNotFoundError(f"Migrations directory not found at {mdir}.")
    version = str(version).strip()
    candidates = sorted(mdir.glob(f"down_{version}_*.sql"))
    if not candidates:
        raise FileNotFoundError(
            f"No down migration for version {version!r} in {mdir} "
            "(expected down_<version>_*.sql)."
        )
    fpath = candidates[0]
    raw_sql = fpath.read_text(encoding="utf-8")
    logger.info("Rolling back database migration [%s] %s ...", version, fpath.name)
    _apply_file(conn, raw_sql, dialect, delete_version=version)
    logger.info("Rolled back migration [%s].", version)
    return version

"""Dual-Engine Database Storage (SQLite for dev/edge, PostgreSQL for production clustering).

Supports:
- Zero-dependency local SQLite with WAL mode for dev and single-node setups.
- Production PostgreSQL 16+ with connection management when DATABASE_URL is set.
- Automatic parameter translation (? -> %s) for transparent cross-engine queries.
- Startup schema migrations via migrations.run_migrations().
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..security import redact, redact_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, hostname TEXT, os TEXT,
  tenant TEXT NOT NULL DEFAULT 'default', token_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'offline', last_seen REAL,
  policy_json TEXT NOT NULL DEFAULT '{}', revoked INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, token_expires_at REAL, token_issued_at REAL,
  capabilities_json TEXT NOT NULL DEFAULT '[]', mtls_fingerprint TEXT,
  agent_version TEXT DEFAULT '', agent_commit TEXT DEFAULT '',
  reported_policy_hash TEXT DEFAULT '', update_ring TEXT NOT NULL DEFAULT 'general',
  update_state TEXT DEFAULT 'idle', poll_state_json TEXT NOT NULL DEFAULT '{}',
  spool_stats_json TEXT NOT NULL DEFAULT '{}',
  CHECK (status IN ('online', 'offline', 'revoked')));
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL, tenant TEXT NOT NULL DEFAULT 'default',
  tool TEXT NOT NULL, args_json TEXT NOT NULL DEFAULT '{}', tier INTEGER,
  status TEXT NOT NULL DEFAULT 'queued', result TEXT DEFAULT '',
  approval_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0, handoff_id TEXT, parent_task_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 1,
  next_attempt_at REAL, failure_reason TEXT DEFAULT '', failure_class TEXT DEFAULT '',
  lease_expires_at REAL, started_at REAL, progress_pct REAL,
  status_message TEXT DEFAULT '', dead_lettered INTEGER NOT NULL DEFAULT 0,
  trace_id TEXT DEFAULT '',
  CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'timed_out', 'needs_approval')),
  CHECK (tier IS NULL OR (tier >= 1 AND tier <= 4)));
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, task_id TEXT, tool TEXT, reason TEXT,
  tenant TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, decided_by TEXT,
  expires_at REAL, decided_at REAL, policy_version INTEGER NOT NULL DEFAULT 0,
  CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
  -- DEFERRABLE INITIALLY DEFERRED (Wave 3A fix): task_service.create_task
  -- inserts the approval row BEFORE the task row in one transaction, so
  -- the FK must be checked at commit, not at statement time. Orphan
  -- protection and ON DELETE CASCADE still hold at commit.
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, tenant TEXT,
  actor TEXT, device_id TEXT, tool TEXT, args_json TEXT, decision TEXT, result TEXT,
  prev_hash TEXT, entry_hash TEXT, trace_id TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  salt TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer',
  tenant TEXT NOT NULL DEFAULT 'default',
  created_at REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
  CHECK (role IN ('viewer', 'operator', 'admin')));
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, role TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default',
  expires_at REAL NOT NULL, created_at REAL NOT NULL,
  FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS stream_tickets (
  ticket_hash TEXT PRIMARY KEY, actor TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default', device_id TEXT NOT NULL,
  session_id TEXT NOT NULL, expires_at REAL NOT NULL,
  used INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL,
  policy_json TEXT NOT NULL, created_by TEXT, created_at REAL NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, tenant TEXT NOT NULL DEFAULT 'default',
  device_id TEXT, steps_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending',
  current_step INTEGER NOT NULL DEFAULT 0, total_steps INTEGER NOT NULL DEFAULT 0,
  results_json TEXT NOT NULL DEFAULT '[]', error TEXT DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL);
CREATE TABLE IF NOT EXISTS telemetry (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default', cpu_percent REAL,
  memory_percent REAL, disk_percent REAL, recorded_at REAL NOT NULL,
  FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS triggers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, tenant TEXT NOT NULL DEFAULT 'default',
  device_id TEXT, metric TEXT NOT NULL, operator TEXT NOT NULL DEFAULT '>',
  threshold REAL NOT NULL, action_type TEXT NOT NULL DEFAULT 'tool',
  action_target TEXT NOT NULL, action_args_json TEXT NOT NULL DEFAULT '{}',
  cooldown_seconds INTEGER NOT NULL DEFAULT 300, last_triggered_at REAL,
  enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, tenant TEXT NOT NULL DEFAULT 'default',
  device_id TEXT, interval_seconds INTEGER NOT NULL DEFAULT 300,
  tool TEXT NOT NULL, args_json TEXT NOT NULL DEFAULT '{}',
  last_run_at REAL, next_run_at REAL NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_devices_token_hash ON devices(token_hash);
CREATE INDEX IF NOT EXISTS idx_devices_tenant_name ON devices(tenant, name, id);
CREATE INDEX IF NOT EXISTS idx_tasks_device_status ON tasks(device_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON tasks(tenant, created_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_tasks_handoff_id ON tasks(handoff_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent_task ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_approvals_task_id ON approvals(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON audit(tenant, ts DESC);
CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status);
CREATE INDEX IF NOT EXISTS idx_workflows_tenant_ts ON workflows(tenant, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_device_time ON telemetry(device_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_triggers_tenant ON triggers(tenant, enabled);
CREATE INDEX IF NOT EXISTS idx_schedules_next_run ON schedules(enabled, next_run_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_policies_version ON policies(version);
CREATE TABLE IF NOT EXISTS task_submissions (
  actor TEXT NOT NULL, request_key TEXT NOT NULL,
  request_hash TEXT NOT NULL, task_id TEXT NOT NULL,
  created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
  PRIMARY KEY (actor, request_key));
CREATE TABLE IF NOT EXISTS custom_agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  instruction TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default',
  provider TEXT NOT NULL DEFAULT 'openai',
  model TEXT,
  base_url TEXT,
  device_ids_json TEXT NOT NULL DEFAULT '[]',
  interval_seconds INTEGER NOT NULL DEFAULT 300,
  enabled INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'idle',
  last_run_at REAL,
  next_run_at REAL NOT NULL,
  last_result TEXT DEFAULT '',
  last_error TEXT DEFAULT '',
  run_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_skip_reason TEXT DEFAULT '',
  memory TEXT DEFAULT '',
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  mcp_server_ids_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS mcp_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  transport TEXT NOT NULL DEFAULT 'http',
  auth_header TEXT DEFAULT '',
  auth_token TEXT DEFAULT '',
  headers_json TEXT NOT NULL DEFAULT '{}',
  tools_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'unknown',
  last_checked_at REAL,
  last_error TEXT DEFAULT '',
  tenant TEXT NOT NULL DEFAULT 'default',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  auth_type TEXT NOT NULL DEFAULT 'bearer',
  oauth_client_id TEXT DEFAULT '',
  oauth_client_secret TEXT DEFAULT '',
  oauth_auth_url TEXT DEFAULT '',
  oauth_token_url TEXT DEFAULT '',
  oauth_scopes TEXT DEFAULT '',
  oauth_refresh_token TEXT DEFAULT '',
  oauth_token_expires_at REAL);
CREATE TABLE IF NOT EXISTS mcp_oauth_states (
  state TEXT PRIMARY KEY,
  server_id TEXT NOT NULL,
  code_verifier TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_tenant ON mcp_servers(tenant, enabled);
CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'running',
  started_at REAL NOT NULL,
  finished_at REAL,
  duration_ms INTEGER,
  response TEXT DEFAULT '',
  steps_json TEXT NOT NULL DEFAULT '[]',
  error TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_custom_agents_due ON custom_agents(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_id, started_at DESC);
CREATE TABLE IF NOT EXISTS workflow_templates (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  steps_json TEXT NOT NULL DEFAULT '[]',
  tenant TEXT NOT NULL DEFAULT 'default',
  created_by TEXT DEFAULT 'admin',
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS alert_deliveries (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT DEFAULT '',
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_ts ON alert_deliveries(created_at DESC);
CREATE TABLE IF NOT EXISTS device_metadata (
  device_id TEXT PRIMARY KEY,
  group_name TEXT NOT NULL DEFAULT '',
  tags_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS task_events (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  event TEXT NOT NULL,
  detail_json TEXT,
  created_at REAL NOT NULL,
  trace_id TEXT DEFAULT '',
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);
CREATE TABLE IF NOT EXISTS task_dead_letters (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default',
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT DEFAULT '',
  failure_class TEXT DEFAULT '',
  last_error TEXT DEFAULT '',
  trace_id TEXT DEFAULT '',
  created_at REAL NOT NULL,
  replayed_task_id TEXT,
  replayed_at REAL);
CREATE TABLE IF NOT EXISTS update_rollouts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default',
  target_version TEXT NOT NULL DEFAULT '',
  target_commit TEXT NOT NULL DEFAULT '',
  manifest_url TEXT DEFAULT '',
  current_ring TEXT NOT NULL DEFAULT 'canary',
  rings_json TEXT NOT NULL DEFAULT '["canary","early","general"]',
  status TEXT NOT NULL DEFAULT 'active',
  max_failure_rate REAL NOT NULL DEFAULT 0.25,
  dispatched_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT DEFAULT 'admin',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tasks_device_status_created ON tasks(device_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_device_tool_status_updated ON tasks(device_id, tool, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_trace_id ON tasks(trace_id);
CREATE INDEX IF NOT EXISTS idx_task_submissions_created ON task_submissions(created_at);
CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_approvals_status_expires ON approvals(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_audit_trace_id ON audit(trace_id);
CREATE INDEX IF NOT EXISTS idx_task_dead_letters_tenant ON task_dead_letters(tenant, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_update_rollouts_tenant ON update_rollouts(tenant, created_at DESC);
CREATE TABLE IF NOT EXISTS agent_memory_chunks (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default',
  chunk_index INTEGER NOT NULL DEFAULT 0,
  content TEXT NOT NULL,
  project_tag TEXT,
  task_tag TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_agent_memory_agent ON agent_memory_chunks(agent_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_agent_memory_tags ON agent_memory_chunks(agent_id, project_tag, task_tag);
"""


def _pg_segments(sql: str) -> tuple[list[tuple[str, int, int]], list[tuple[str, int, int]], bool]:
    """Tokenize ``sql`` into (kind, start, end) spans.

    Kinds: ``"code"``, ``"string"`` (single-quoted, ``''`` escape),
    ``"ident"`` (double-quoted or backtick-quoted, ``""`` escape),
    ``"comment"`` (``--`` to end of line, ``/* */`` non-nested, matching
    SQLite's reading of the input), ``"dollar"`` (``$tag$ ... $tag$``).

    Also returns the first five bare words seen in code (lowercased, with
    spans) for the upsert allowlist, and whether a top-level ``ON
    CONFLICT`` occurs in code. Words inside strings, identifiers,
    comments, or dollar-quoted bodies are never reported.
    """
    n = len(sql)
    segments: list[tuple[str, int, int]] = []
    head_words: list[tuple[str, int, int]] = []
    saw_on_conflict = False
    prev_word: str | None = None

    kind = "code"
    seg_start = 0
    i = 0
    comment_kind = ""  # "line" | "block" while kind == "comment"
    dollar_tag: str | None = None  # closing tag while kind == "dollar"

    def close(end: int, next_kind: str) -> None:
        nonlocal kind, seg_start
        if end > seg_start:
            segments.append((kind, seg_start, end))
        kind = next_kind
        seg_start = end

    def note_word(word: str, start: int, end: int) -> None:
        nonlocal prev_word, saw_on_conflict
        low = word.lower()
        if len(head_words) < 5:
            head_words.append((low, start, end))
        if prev_word == "on" and low == "conflict":
            saw_on_conflict = True
        prev_word = low

    while i < n:
        ch = sql[i]
        if kind == "code":
            if ch == "'":
                close(i, "string")
                i += 1
            elif ch == '"' or ch == "`":
                close(i, "ident")
                i += 1
            elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
                close(i, "comment")
                comment_kind = "line"
                i += 2
            elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
                close(i, "comment")
                comment_kind = "block"
                i += 2
            elif ch == "$":
                # Dollar-quoted body? $tag$ where tag starts with a letter
                # or underscore ($$ is the empty tag). $1 is NOT a tag.
                j = i + 1
                while j < n and (sql[j].isalnum() or sql[j] == "_"):
                    j += 1
                if j < n and sql[j] == "$" and (
                    j == i + 1 or sql[i + 1].isalpha() or sql[i + 1] == "_"
                ):
                    dollar_tag = sql[i : j + 1]
                    close(i, "dollar")
                    i = j + 1
                else:
                    i += 1
            elif ch.isalpha() or ch == "_":
                j = i + 1
                while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                    j += 1
                note_word(sql[i:j], i, j)
                i = j
            else:
                i += 1
        elif kind == "string":
            if sql.startswith("''", i):
                i += 2  # escaped quote, stays inside the literal
            elif ch == "'":
                close(i + 1, "code")
                i += 1
            else:
                i += 1
        elif kind == "ident":
            quote = sql[seg_start]
            if sql.startswith(quote * 2, i):
                i += 2  # escaped quote, stays inside the identifier
            elif ch == quote:
                close(i + 1, "code")
                i += 1
            else:
                i += 1
        elif kind == "comment":
            if comment_kind == "line":
                if ch == "\n":
                    close(i, "code")
                else:
                    i += 1
            else:  # block comment: first */ ends it (SQLite reading)
                if sql.startswith("*/", i):
                    close(i + 2, "code")
                    i += 2
                else:
                    i += 1
        else:  # dollar-quoted body: only its own closing tag ends it
            assert dollar_tag is not None
            k = sql.find(dollar_tag, i)
            if k == -1:
                i = n  # unterminated: runs to end of input; invalid SQL
            else:
                close(k + len(dollar_tag), "code")
                i = k + len(dollar_tag)
    if n > seg_start:
        segments.append((kind, seg_start, n))
    return segments, head_words, saw_on_conflict


def _render_pg(sql: str, segments: list[tuple[str, int, int]]) -> str:
    """Render tokenized ``sql`` for psycopg3.

    Code segments: ``?`` -> ``%s`` (placeholders) and ``%`` -> ``%%``.
    Every other segment (string, identifier, comment, dollar-quoted
    body): ``%`` -> ``%%`` only — a ``?`` there is literal text, never a
    placeholder. The ``%`` escaping is required everywhere because
    psycopg3's placeholder scan is quote-blind: it regex-scans the raw
    query, so a bare ``%`` inside a literal or a comment is a
    ProgrammingError, and ``%%`` collapses back to ``%`` even inside a
    literal (verified against psycopg 3.3.9 ``_split_query``).
    """
    parts: list[str] = []
    for seg_kind, start, end in segments:
        text = sql[start:end]
        if seg_kind == "code":
            # Escape % FIRST: the %s introduced below must not be escaped.
            parts.append(text.replace("%", "%%").replace("?", "%s"))
        else:
            parts.append(text.replace("%", "%%"))
    return "".join(parts)


#: SQLite upsert heads allowlisted for PostgreSQL translation, keyed by
#: (or_kind, table) -> ON CONFLICT clause appended. Only ``settings``
#: occurs in-tree (migrations/011_orchestration_plane_state.sql); any
#: other table is refused with ValueError instead of being rewritten.
_PG_UPSERT_ALLOWLIST = {
    ("ignore", "settings"): "ON CONFLICT DO NOTHING",
    ("replace", "settings"): "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
}


def to_pg_sql(sql: str) -> str:
    """Translate SQLite-dialect SQL to PostgreSQL (F-05-18, tokenizer-based).

    Replaces the old regex-based translation. The old code rewrote ANY
    ``INSERT OR REPLACE/IGNORE INTO ...`` anywhere in the text — including
    inside string literals — and converted ``?`` to ``%s`` with a
    quote-tracker blind to ``--``/``/* */`` comments and dollar-quoted
    bodies, so a ``?`` in a comment became a phantom placeholder and
    psycopg then demanded a parameter that does not exist.

    This version only translates what the allowlist permits:

    * ``?`` -> ``%s`` in code only (never inside a literal, quoted
      identifier, comment, or dollar-quoted body).
    * ``%`` -> ``%%`` everywhere (required by psycopg3's quote-blind
      placeholder scan; ``%%`` collapses back to ``%`` even inside a
      literal, so a literal ``%s`` survives as text instead of
      swallowing a parameter).
    * SQLite upserts: only the exact statement head ``INSERT OR
      IGNORE/REPLACE INTO settings`` is translated (see
      :data:`_PG_UPSERT_ALLOWLIST`). Any other ``INSERT OR ...`` head is
      refused with ValueError — the Postgres equivalent needs an explicit
      ON CONFLICT clause naming the constraint, which no rewrite can
      know. A statement that already contains a top-level ON CONFLICT is
      refused the same way instead of appending a second clause.

    Removal was considered and rejected: the Postgres query path
    (:meth:`PostgresConnection.execute`) serves hundreds of
    SQLite-dialect call sites using ``?`` placeholders, and psycopg3
    only accepts ``%s`` — dropping the translator would mean rewriting
    every call site. ``PostgresConnection.executescript`` (no in-tree
    callers; migrations go through ``conn.execute`` per statement) uses
    the same translation.
    """
    segments, head, saw_conflict = _pg_segments(sql)
    if (
        len(head) >= 4
        and head[0][0] == "insert"
        and head[1][0] == "or"
        and head[2][0] in ("ignore", "replace")
        and head[3][0] == "into"
    ):
        or_kind = head[2][0]
        if len(head) < 5:
            raise ValueError(
                f"to_pg_sql: SQLite-only 'INSERT OR {or_kind.upper()} INTO ...' "
                "has no table name after INTO; refusing to rewrite. Use an "
                "explicit ON CONFLICT clause instead."
            )
        table = head[4][0]
        clause = _PG_UPSERT_ALLOWLIST.get((or_kind, table))
        if clause is None:
            raise ValueError(
                f"to_pg_sql: SQLite-only 'INSERT OR {or_kind.upper()} INTO "
                f"{table}' is not allowlisted for PostgreSQL (only the "
                "settings table is); rewrite the statement with an explicit "
                "ON CONFLICT clause."
            )
        if saw_conflict:
            raise ValueError(
                "to_pg_sql: 'INSERT OR ... INTO settings' statement already "
                "contains a top-level ON CONFLICT; refusing to append a "
                "second clause."
            )
        # Rewrite the head on the ORIGINAL string (the recorded offsets
        # are valid there), then translate the result.
        _, or_start, _ = head[1]
        _, into_start, _ = head[3]
        _, _, table_end = head[4]
        rewritten = sql[:or_start] + sql[into_start:table_end] + sql[table_end:]
        out = _render_pg(rewritten, _pg_segments(rewritten)[0])
        # A trailing ";" can only be the statement terminator here (a
        # statement cannot end inside a literal), so strip it before
        # appending the clause.
        out = out.rstrip()
        if out.endswith(";"):
            out = out[:-1].rstrip()
        return out + " " + clause
    return _render_pg(sql, segments)


class RowDict(dict):
    """Dictionary supporting both column-name (row['id']) and integer (row[0]) access."""

    def __init__(self, data: dict, keys: list[str]) -> None:
        super().__init__(data)
        self._keys = keys

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, int):
            return super().__getitem__(self._keys[item])
        return super().__getitem__(item)


class _LockedResult:
    """Materialized query result so no raw database cursor is read outside lock."""

    def __init__(self, rows: list[Any], rowcount: int, lastrowid: int | None) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


_DB_LOGGER = None


def _db_log():
    global _DB_LOGGER
    if _DB_LOGGER is None:
        import logging

        _DB_LOGGER = logging.getLogger("ai_bridge.db")
    return _DB_LOGGER


def _sql_op(sql: str) -> str:
    head = (sql or "").lstrip().split(None, 1)
    if not head:
        return "other"
    word = head[0].upper()
    return word if word in ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "PRAGMA") else "other"


def _slow_query_threshold_ms() -> float:
    try:
        return max(1.0, float(os.environ.get("AI_BRIDGE_SLOW_QUERY_MS", "250") or "250"))
    except ValueError:
        return 250.0


def _observe_query(dialect: str, sql: str, elapsed_s: float, *, ok: bool, stats: dict[str, Any]) -> None:
    op = _sql_op(sql)
    stats["queries_total"] = int(stats.get("queries_total", 0)) + 1
    if not ok:
        stats["query_errors"] = int(stats.get("query_errors", 0)) + 1
    elapsed_ms = elapsed_s * 1000.0
    stats["last_query_ms"] = round(elapsed_ms, 3)
    if elapsed_ms >= _slow_query_threshold_ms():
        stats["slow_queries"] = int(stats.get("slow_queries", 0)) + 1
        _db_log().warning(
            "Slow DB query detected",
            extra={
                "dialect": dialect,
                "op": op,
                "duration_ms": round(elapsed_ms, 2),
                "sql_preview": " ".join((sql or "").split())[:160],
            },
        )
    try:
        from . import metrics as metricsmod

        metricsmod.record_db_query(dialect, op, elapsed_s, ok=ok)
    except Exception:
        pass


class LockedConnection:
    """Thread-safe wrapper around sqlite3.Connection for multi-threaded FastAPI routes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self.dialect = "sqlite"
        self._stats: dict[str, Any] = {
            "dialect": "sqlite",
            "pooled": False,
            "pool_size": 1,
            "pool_available": 1,
            "pool_waiting": 0,
            "queries_total": 0,
            "query_errors": 0,
            "slow_queries": 0,
            "last_query_ms": 0.0,
        }

    def get_db_stats(self) -> dict[str, Any]:
        return dict(self._stats)

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield self
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, parameters: tuple | list = ()) -> _LockedResult:
        t0 = time.perf_counter()
        ok = True
        try:
            with self._lock:
                cur = self._conn.execute(sql, parameters)
                rows = cur.fetchall()
                return _LockedResult(rows, cur.rowcount, cur.lastrowid)
        except Exception:
            ok = False
            raise
        finally:
            _observe_query("sqlite", sql, time.perf_counter() - t0, ok=ok, stats=self._stats)

    def executescript(self, sql_script: str):
        with self._lock:
            return self._conn.executescript(sql_script)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class PostgresConnection:
    """Thread-safe connection wrapper for PostgreSQL with optional psycopg_pool pooling."""

    def __init__(self, dsn: str):
        import psycopg

        self.dsn = dsn
        self._lock = threading.RLock()
        self._tx_local = threading.local()
        self.dialect = "postgres"
        self._pool = None
        self._conn = None
        self._stats: dict[str, Any] = {
            "dialect": "postgres",
            "pooled": False,
            "pool_size": 1,
            "pool_available": 1,
            "pool_waiting": 0,
            "queries_total": 0,
            "query_errors": 0,
            "slow_queries": 0,
            "last_query_ms": 0.0,
        }
        stmt_ms = max(1000, int(os.environ.get("AI_BRIDGE_DB_STATEMENT_TIMEOUT_MS", "30000") or "30000"))
        lock_ms = max(500, int(os.environ.get("AI_BRIDGE_DB_LOCK_TIMEOUT_MS", "5000") or "5000"))
        self._pg_options = f"-c statement_timeout={stmt_ms} -c lock_timeout={lock_ms}"

        use_pool = os.environ.get("AI_BRIDGE_DB_POOL_ENABLED", "1").strip().lower() in ("1", "true", "yes")
        if use_pool:
            try:
                from psycopg_pool import ConnectionPool

                min_sz = max(1, int(os.environ.get("AI_BRIDGE_DB_POOL_MIN", "2") or "2"))
                max_sz = max(min_sz, int(os.environ.get("AI_BRIDGE_DB_POOL_MAX", "10") or "10"))
                timeout_s = max(1.0, float(os.environ.get("AI_BRIDGE_DB_POOL_TIMEOUT", "15") or "15"))
                self._pool = ConnectionPool(
                    conninfo=dsn,
                    min_size=min_sz,
                    max_size=max_sz,
                    timeout=timeout_s,
                    kwargs={"autocommit": False, "options": self._pg_options},
                    open=True,
                )
                self._stats["pooled"] = True
                self._stats["pool_max"] = max_sz
            except Exception:
                self._pool = None
        if self._pool is None:
            self._conn = psycopg.connect(dsn, autocommit=False, options=self._pg_options)

    def get_db_stats(self) -> dict[str, Any]:
        out = dict(self._stats)
        if self._pool is not None:
            try:
                ps = self._pool.get_stats()
                out["pool_size"] = int(ps.get("pool_size", 0))
                out["pool_available"] = int(ps.get("pool_available", 0))
                out["pool_waiting"] = int(ps.get("requests_waiting", 0))
                out["pool_utilization"] = round(
                    (out["pool_size"] - out["pool_available"]) / max(1, out.get("pool_max", out["pool_size"] or 1)),
                    3,
                )
            except Exception:
                pass
        return out

    @contextlib.contextmanager
    def transaction(self):
        if self._pool is not None:
            active = getattr(self._tx_local, "conn", None)
            if active is not None:
                # Re-entrant nested transaction block on same thread.
                yield self
                return
            with self._pool.connection() as pg_conn:
                self._tx_local.conn = pg_conn
                try:
                    yield self
                    pg_conn.commit()
                except Exception:
                    with contextlib.suppress(Exception):
                        pg_conn.rollback()
                    raise
                finally:
                    self._tx_local.conn = None
            return
        with self._lock:
            try:
                yield self
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _ensure_conn(self) -> None:
        if self._conn is None or getattr(self._conn, "closed", False) or getattr(self._conn, "broken", False):
            import psycopg

            self._conn = psycopg.connect(self.dsn, autocommit=False, options=self._pg_options)

    def _run_on_conn(self, pg_conn: Any, pg_sql: str, parameters: tuple | list) -> _LockedResult:
        cur = pg_conn.cursor()
        cur.execute(pg_sql, parameters)
        if cur.description:
            cols = [desc[0] for desc in cur.description]
            raw_rows = cur.fetchall()
            rows = [RowDict(dict(zip(cols, r)), cols) for r in raw_rows]
        else:
            rows = []
        return _LockedResult(rows, cur.rowcount, getattr(cur, "lastrowid", None))

    def execute(self, sql: str, parameters: tuple | list = ()) -> _LockedResult:
        t0 = time.perf_counter()
        ok = True
        pg_sql = to_pg_sql(sql)
        try:
            if self._pool is not None:
                tx_conn = getattr(self._tx_local, "conn", None)
                if tx_conn is not None:
                    return self._run_on_conn(tx_conn, pg_sql, parameters)
                with self._pool.connection() as pg_conn:
                    try:
                        res = self._run_on_conn(pg_conn, pg_sql, parameters)
                        pg_conn.commit()
                        return res
                    except Exception:
                        with contextlib.suppress(Exception):
                            pg_conn.rollback()
                        raise
            with self._lock:
                self._ensure_conn()
                try:
                    return self._run_on_conn(self._conn, pg_sql, parameters)
                except Exception:
                    with contextlib.suppress(Exception):
                        self._conn.rollback()
                    raise
        except Exception:
            ok = False
            raise
        finally:
            _observe_query("postgres", sql, time.perf_counter() - t0, ok=ok, stats=self._stats)

    def executescript(self, sql_script: str):
        from .migrations import _split_statements

        stmts = [item["sql"].strip() for item in _split_statements(sql_script) if item["sql"].strip()]
        if self._pool is not None:
            with self._pool.connection() as pg_conn:
                cur = pg_conn.cursor()
                try:
                    for stmt in stmts:
                        cur.execute(to_pg_sql(stmt))
                    pg_conn.commit()
                except Exception:
                    with contextlib.suppress(Exception):
                        pg_conn.rollback()
                    raise
            return
        with self._lock:
            self._ensure_conn()
            cur = self._conn.cursor()
            try:
                for stmt in stmts:
                    cur.execute(to_pg_sql(stmt))
                self._conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.rollback()
                raise

    def commit(self) -> None:
        if self._pool is not None:
            tx_conn = getattr(self._tx_local, "conn", None)
            if tx_conn is not None:
                tx_conn.commit()
            return
        with self._lock:
            if self._conn is not None:
                self._conn.commit()

    def rollback(self) -> None:
        if self._pool is not None:
            tx_conn = getattr(self._tx_local, "conn", None)
            if tx_conn is not None:
                tx_conn.rollback()
            return
        with self._lock:
            if self._conn is not None:
                self._conn.rollback()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
            return
        with self._lock:
            if self._conn is not None:
                self._conn.close()


def connect(db_path: Path | None = None, database_url: str | None = None) -> LockedConnection | PostgresConnection:
    """Connect to the database, auto-detecting PostgreSQL vs SQLite, and run migrations."""
    db_url = database_url or os.environ.get("DATABASE_URL")

    # 1. PostgreSQL Path
    if db_url and (db_url.startswith("postgres://") or db_url.startswith("postgresql://")):
        conn = PostgresConnection(db_url)
        from . import migrations
        migrations.run_migrations(conn, dialect="postgres")
        return conn

    # 2. SQLite Path
    if db_path is None:
        data_dir = Path(os.environ.get("AI_BRIDGE_DATA_DIR", str(Path.home() / ".ai-bridge")))
        db_path = data_dir / "control.db"

    if isinstance(db_path, str):
        db_path = Path(db_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15.0)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=5000")
    # Wave 3A (DB-02): enforce the foreign keys migration 015 adds. Without
    # this pragma SQLite parses FK clauses but never enforces them.
    raw.execute("PRAGMA foreign_keys=ON")
    raw.executescript(SCHEMA)

    locked_conn = LockedConnection(raw)
    from . import migrations
    migrations.run_migrations(locked_conn, dialect="sqlite")
    return locked_conn


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def now() -> float:
    return time.time()


def _ensure_audit_hash_columns(conn: Any) -> None:
    dialect = getattr(conn, "dialect", "sqlite")
    try:
        if dialect == "postgres":
            cols = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'audit'"
                ).fetchall()
            }
        else:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(audit)").fetchall()}
    except Exception:
        cols = set()
    for col in ("entry_hash", "prev_hash"):
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE audit ADD COLUMN {col} TEXT")
            except Exception:
                if dialect == "postgres":
                    with contextlib.suppress(Exception):
                        conn.rollback()


def _audit_payload(prev_hash: str, ts: float, actor: Any, device_id: Any,
                   tool: Any, args_json: str, decision: Any) -> str:
    """Hash payload for the current (DB-05) audit scheme.

    Deliberately computed WITHOUT the row id so the complete row — including
    ``entry_hash`` — can be inserted in a single statement inside one
    transaction, with no post-insert UPDATE.
    """
    return f"{prev_hash}|{ts}|{actor}|{device_id}|{tool}|{args_json}|{decision}"


def _audit_payload_legacy(prev_hash: str, row_id: Any, ts: float, actor: Any,
                         device_id: Any, tool: Any, args_json: str,
                         decision: Any) -> str:
    """Hash payload for the pre-DB-05 scheme, which included the row id.

    Only used by :func:`verify_audit_chain` to recognise rows written before
    the atomic rewrite; never used for new writes.
    """
    return f"{prev_hash}|{row_id}|{ts}|{actor}|{device_id}|{tool}|{args_json}|{decision}"


def _audit_entry_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_get(row: Any, name: str, index: int) -> Any:
    """Read a column from a sqlite3.Row, RowDict, dict, or plain tuple row."""
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        try:
            return row[index]
        except (KeyError, TypeError, IndexError):
            return None


def verify_audit_chain(conn: Any) -> dict[str, Any]:
    """Verify the audit hash chain in ``id`` order, recomputing every hash.

    Rows written by the pre-DB-05 scheme (payload included the row id) are
    recognised as ``legacy`` and counted, not reported as broken. A row whose
    ``prev_hash`` does not match the previous row's ``entry_hash``, whose
    ``entry_hash`` is missing, or whose hash matches neither scheme is the
    first break: verification stops there.

    Returns ``{"ok": bool, "checked": int, "broken_at": int | None,
    "legacy_rows": int}`` where ``checked`` is the number of rows examined
    (up to and including the broken row, or all rows when intact).
    """
    rows = conn.execute(
        "SELECT id, ts, actor, device_id, tool, args_json, decision, prev_hash, entry_hash "
        "FROM audit ORDER BY id ASC"
    ).fetchall()
    expected_prev = "0" * 64
    checked = 0
    legacy_rows = 0
    broken_at: int | None = None
    for row in rows:
        row_id = _row_get(row, "id", 0)
        checked += 1
        prev_hash = _row_get(row, "prev_hash", 7)
        entry_hash = _row_get(row, "entry_hash", 8)
        ts = _row_get(row, "ts", 1)
        actor = _row_get(row, "actor", 2)
        device_id = _row_get(row, "device_id", 3)
        tool = _row_get(row, "tool", 4)
        args_json = _row_get(row, "args_json", 5)
        decision = _row_get(row, "decision", 6)
        if prev_hash != expected_prev or not entry_hash:
            broken_at = row_id
            break
        new_hash = _audit_entry_hash(
            _audit_payload(prev_hash, ts, actor, device_id, tool, args_json, decision)
        )
        if entry_hash == new_hash:
            expected_prev = entry_hash
            continue
        legacy_hash = _audit_entry_hash(
            _audit_payload_legacy(prev_hash, row_id, ts, actor, device_id, tool, args_json, decision)
        )
        if entry_hash == legacy_hash:
            legacy_rows += 1
            expected_prev = entry_hash
            continue
        broken_at = row_id
        break
    return {"ok": broken_at is None, "checked": checked, "broken_at": broken_at, "legacy_rows": legacy_rows}


def audit(conn: Any, *, tenant: str, actor: str, device_id: str,
          tool: str, args: dict, decision: str, result: str = "",
          trace_id: str | None = None) -> None:
    safe_args = redact_obj(json.loads(json.dumps(args or {}, default=str)))
    safe_result = redact(result or "")[:2000]
    args_json = json.dumps(safe_args, default=str)
    ts = now()
    if trace_id is None:
        try:
            from ..logging_config import get_correlation_id

            trace_id = get_correlation_id() or ""
        except Exception:
            trace_id = ""
    resolved_trace = str(trace_id or "")[:128]
    _ensure_audit_hash_columns(conn)
    # DB-05: read the chain tip, compute the hash, and INSERT the complete
    # row inside ONE transaction, so concurrent writers cannot interleave a
    # tip read / insert / hash UPDATE and fork the chain. The hash is
    # computed without the row id, so no post-insert UPDATE is needed.
    prev_hash = "0" * 64
    entry_hash = ""
    row_id: Any = None
    with conn.transaction():
        try:
            tip = conn.execute(
                "SELECT entry_hash FROM audit WHERE entry_hash IS NOT NULL AND entry_hash != '' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if tip and tip["entry_hash"]:
                prev_hash = str(tip["entry_hash"])
        except Exception:
            prev_hash = "0" * 64
        entry_hash = _audit_entry_hash(
            _audit_payload(prev_hash, ts, actor, device_id, tool, args_json, decision)
        )
        try:
            cur = conn.execute(
                "INSERT INTO audit (ts, tenant, actor, device_id, tool, args_json, decision, result, prev_hash, entry_hash, trace_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, tenant, actor, device_id, tool, args_json, decision, safe_result, prev_hash, entry_hash, resolved_trace),
            )
        except Exception:
            cur = conn.execute(
                "INSERT INTO audit (ts, tenant, actor, device_id, tool, args_json, decision, result, prev_hash, entry_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, tenant, actor, device_id, tool, args_json, decision, safe_result, prev_hash, entry_hash),
            )
        row_id = getattr(cur, "lastrowid", None)
    # WORM/SIEM emit happens only after the transaction has committed.
    try:
        from . import worm_siem

        if worm_siem.worm_siem_url():
            worm_siem.emit_audit_event(
                {
                    "id": row_id,
                    "ts": ts,
                    "tenant": tenant,
                    "actor": actor,
                    "device_id": device_id,
                    "tool": tool,
                    "decision": decision,
                    "prev_hash": prev_hash,
                    "entry_hash": entry_hash,
                    "trace_id": resolved_trace,
                }
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Explicit mcp_servers loaders (Wave 2 fix-up: never SELECT * FROM
# mcp_servers). Columns are named explicitly so a future schema change —
# especially a new secret-bearing column — cannot silently leak into the
# dicts handed to the MCP dispatchers in ai_api.py / custom_agents.py;
# new columns must be opted into MCP_SERVER_COLUMNS deliberately.
# ---------------------------------------------------------------------------
MCP_SERVER_COLUMNS = (
    "id, name, url, transport, auth_type, auth_header, auth_token, "
    "oauth_client_id, oauth_client_secret, oauth_auth_url, oauth_token_url, "
    "oauth_scopes, oauth_refresh_token, oauth_token_expires_at, "
    "headers_json, tools_json, enabled, status, last_checked_at, "
    "last_error, tenant, created_at, updated_at"
)


def list_enabled_mcp_servers(conn: Any, tenant: str) -> list[dict[str, Any]]:
    """Enabled external MCP servers for one tenant, as plain dicts."""
    rows = conn.execute(
        f"SELECT {MCP_SERVER_COLUMNS} FROM mcp_servers WHERE tenant=? AND enabled=1",
        (tenant,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_mcp_servers_by_ids(conn: Any, server_ids: list[str]) -> list[dict[str, Any]]:
    """Enabled external MCP servers for the given ids, as plain dicts."""
    ids = [str(i) for i in (server_ids or []) if str(i)]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT {MCP_SERVER_COLUMNS} FROM mcp_servers WHERE id IN ({placeholders}) AND enabled=1",
        tuple(ids),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Single-use stream tickets (D-08 follow-up).
#
# Browsers cannot set headers on a WebSocket handshake, so the dashboard
# used to put its long-lived session token in the WS URL query string.
# A stream ticket is instead a short-lived (60 s), single-use credential
# minted by an authenticated caller and bound to one actor + tenant +
# device + WebRTC session. Only the SHA-256 hash is stored; the ticket
# itself is returned once at mint time and never logged.
# ---------------------------------------------------------------------------
STREAM_TICKET_TTL_S = 60


def ensure_stream_tickets_table(conn: Any) -> None:
    """Create the stream_tickets table on databases predating the SCHEMA entry
    (e.g. PostgreSQL, whose schema comes from migrations, not SCHEMA)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stream_tickets ("
        "ticket_hash TEXT PRIMARY KEY, actor TEXT NOT NULL, "
        "tenant TEXT NOT NULL DEFAULT 'default', device_id TEXT NOT NULL, "
        "session_id TEXT NOT NULL, expires_at REAL NOT NULL, "
        "used INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
    )
    try:
        conn.commit()
    except Exception:
        pass


def mint_stream_ticket(
    conn: Any, *, actor: str, tenant: str, device_id: str, session_id: str,
    ttl_s: int = STREAM_TICKET_TTL_S,
) -> tuple[str, int]:
    """Mint a ticket; returns (ticket, ttl_s). Callers must return the
    ticket to the authenticated minter exactly once and never log it."""
    ensure_stream_tickets_table(conn)
    now_ts = now()
    with conn.transaction():
        conn.execute("DELETE FROM stream_tickets WHERE expires_at < ?", (now_ts,))
        ticket = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO stream_tickets "
            "(ticket_hash, actor, tenant, device_id, session_id, expires_at, used, created_at) "
            "VALUES (?,?,?,?,?,?,0,?)",
            (hash_token(ticket), actor, tenant, device_id, session_id, now_ts + ttl_s, now_ts),
        )
    return ticket, ttl_s


def redeem_stream_ticket(
    conn: Any, ticket: str, *, device_id: str, session_id: str,
) -> dict[str, Any] | None:
    """Atomically consume a ticket. Returns the ticket row on success, else
    None. A ticket redeems only once, only before expiry, and only for the
    device + session it was minted for; a wrong-device / wrong-session /
    expired / replayed presentation returns None without consuming it."""
    if not ticket:
        return None
    ensure_stream_tickets_table(conn)
    now_ts = now()
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM stream_tickets WHERE ticket_hash=? AND used=0",
            (hash_token(ticket),),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("expires_at", 0) < now_ts:
            return None
        if str(d.get("device_id") or "") != str(device_id) or str(d.get("session_id") or "") != str(session_id):
            return None
        cur = conn.execute(
            "UPDATE stream_tickets SET used=1 WHERE ticket_hash=? AND used=0",
            (hash_token(ticket),),
        )
        if not cur.rowcount:
            return None
    return d


# ---------------------------------------------------------------------------
# Wave 3A helpers: projected lists, retention, idempotency TTL, cascades.
# ---------------------------------------------------------------------------

# PERF-03: every tasks column EXCEPT the (up to 16 MB) ``result`` TEXT.
# List endpoints must select this list, never ``SELECT *``; full results
# come from :func:`get_task_result` only. app.py wiring is a parent
# follow-up — these helpers are the mechanism, with tests below.
TASK_LIST_COLUMNS = (
    "id, device_id, tenant, tool, args_json, tier, status, approval_id, "
    "created_at, updated_at, epoch, handoff_id, parent_task_id, "
    "attempt, max_attempts, next_attempt_at, failure_reason, failure_class, "
    "lease_expires_at, started_at, progress_pct, status_message, dead_lettered, trace_id"
)


def list_tasks_projected(
    conn: Any,
    *,
    tenant: str,
    device_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List tasks for one tenant WITHOUT the ``result`` column (PERF-03).

    Returns plain dicts with no ``"result"`` key, so a 200-row list can
    no longer return multi-MB screenshot payloads.
    """
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    sql = f"SELECT {TASK_LIST_COLUMNS} FROM tasks WHERE tenant=?"
    params: list[Any] = [tenant]
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, tuple(params)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        assert "result" not in d
        out.append(d)
    return out


def get_task_result(conn: Any, task_id: str) -> str | None:
    """Full ``result`` payload for one task (PERF-03 companion).

    Returns None when the task does not exist; ``""`` when it has no
    result yet. Callers must check the caller's tenant/device scope
    before calling this — it does no authorization itself.
    """
    row = conn.execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    return row["result"] if hasattr(row, "keys") else row[0]


def maybe_prune_telemetry(
    conn: Any, device_id: str, *, keep: int = 500, slack: int = 100
) -> int:
    """Amortized telemetry prune (PERF-05).

    The old heartbeat path pruned on EVERY heartbeat
    (``DELETE ... WHERE id NOT IN (SELECT ... LIMIT 500)``). This version
    first counts (one cheap indexed COUNT) and only prunes when the
    device is more than ``slack`` rows over ``keep`` — so a steady-state
    heartbeat pays a COUNT, not a DELETE, and pruning runs at most once
    per ``slack`` heartbeats. Returns the number of rows deleted.
    """
    count = conn.execute(
        "SELECT COUNT(*) FROM telemetry WHERE device_id=?", (device_id,)
    ).fetchone()
    total = count[0] if count is not None else 0
    if total <= keep + slack:
        return 0
    cur = conn.execute(
        "DELETE FROM telemetry WHERE device_id=? AND id NOT IN "
        "(SELECT id FROM telemetry WHERE device_id=? ORDER BY recorded_at DESC LIMIT ?)",
        (device_id, device_id, keep),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def prune_task_submissions(conn: Any, *, ttl_s: float = 86400.0) -> int:
    """Delete idempotency records older than ``ttl_s`` (DIST-09).

    ``task_submissions`` had no TTL: a replayed Idempotency-Key would
    return the original task forever, and the table grew without bound.
    Default TTL is 24 h — long enough to cover client retries, short
    enough to bound the table. Returns the number of rows deleted.
    """
    cutoff = now() - ttl_s
    cur = conn.execute(
        "DELETE FROM task_submissions WHERE created_at < ?", (cutoff,)
    )
    conn.commit()
    return int(cur.rowcount or 0)


def prune_task_events(conn: Any, *, older_than_s: float) -> int:
    """Delete ``task_events`` rows older than ``older_than_s`` seconds."""
    cutoff = now() - older_than_s
    cur = conn.execute("DELETE FROM task_events WHERE created_at < ?", (cutoff,))
    conn.commit()
    return int(cur.rowcount or 0)


def prune_terminal_tasks(conn: Any, *, older_than_s: float) -> int:
    """Delete terminal (completed/failed/cancelled/timed_out) tasks older
    than ``older_than_s`` seconds.

    Child rows (approvals, task_events) are removed by the migration-015
    ON DELETE CASCADE foreign keys; audit rows are never touched here
    (see :func:`retention_prune_audit`). Returns the number of tasks
    deleted.
    """
    cutoff = now() - older_than_s
    cur = conn.execute(
        "DELETE FROM tasks WHERE status IN ('completed', 'failed', 'cancelled', 'timed_out') "
        "AND updated_at < ?",
        (cutoff,),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def retention_prune_audit(
    conn: Any, *, older_than_s: float, worm_export_ok: bool
) -> int:
    """Delete audit rows older than ``older_than_s`` seconds — ONLY when
    the WORM/SIEM export of those rows succeeded (SEC-05 / Wave 2A).

    Raises ValueError (and deletes nothing) when ``worm_export_ok`` is
    False: audit history must be archive-then-delete, never silently
    pruned. Returns the number of rows deleted.
    """
    if not worm_export_ok:
        raise ValueError(
            "Audit retention refused: the WORM/SIEM export for this range "
            "has not succeeded (SEC-05). Archive first, then delete."
        )
    cutoff = now() - older_than_s
    cur = conn.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))
    conn.commit()
    return int(cur.rowcount or 0)


def prune_expired_sessions(conn: Any) -> int:
    """Delete expired local RBAC session rows."""
    cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
    conn.commit()
    return int(cur.rowcount or 0)


def prune_expired_stream_tickets(conn: Any) -> int:
    """Delete expired or consumed WebRTC stream tickets."""
    cur = conn.execute("DELETE FROM stream_tickets WHERE expires_at < ? OR used = 1", (now(),))
    conn.commit()
    return int(cur.rowcount or 0)


def retention_sweep(
    conn: Any,
    *,
    telemetry_keep: int = 500,
    task_events_older_than_s: float | None = None,
    terminal_tasks_older_than_s: float | None = None,
    audit_older_than_s: float | None = None,
    worm_export_ok: bool = False,
    idempotency_ttl_s: float = 86400.0,
) -> dict[str, int]:
    """Run all retention jobs (DB-07) and return per-job deleted counts.

    Audit pruning only runs when ``audit_older_than_s`` is given AND
    ``worm_export_ok`` is True; otherwise it is skipped (count 0) and
    the refusal is the caller's responsibility to surface — use
    :func:`retention_prune_audit` directly when an explicit refusal is
    wanted. Telemetry is pruned per device via
    :func:`maybe_prune_telemetry` (amortized, PERF-05).
    """
    counts: dict[str, int] = {
        "telemetry": 0,
        "task_events": 0,
        "tasks": 0,
        "audit": 0,
        "task_submissions": 0,
        "sessions": 0,
        "stream_tickets": 0,
        "ratelimits": 0,
    }
    device_rows = conn.execute("SELECT DISTINCT device_id FROM telemetry").fetchall()
    for r in device_rows:
        dev = r["device_id"] if hasattr(r, "keys") else r[0]
        counts["telemetry"] += maybe_prune_telemetry(conn, dev, keep=telemetry_keep)
    if task_events_older_than_s is not None:
        counts["task_events"] = prune_task_events(conn, older_than_s=task_events_older_than_s)
    if terminal_tasks_older_than_s is not None:
        counts["tasks"] = prune_terminal_tasks(conn, older_than_s=terminal_tasks_older_than_s)
    if audit_older_than_s is not None and worm_export_ok:
        counts["audit"] = retention_prune_audit(
            conn, older_than_s=audit_older_than_s, worm_export_ok=True
        )
    counts["task_submissions"] = prune_task_submissions(conn, ttl_s=idempotency_ttl_s)
    try:
        counts["sessions"] = prune_expired_sessions(conn)
    except Exception:
        pass
    try:
        counts["stream_tickets"] = prune_expired_stream_tickets(conn)
    except Exception:
        pass
    try:
        from . import durable_ratelimit as _drl

        counts["ratelimits"] = _drl.prune_expired_db_ratelimits(conn)
    except Exception:
        pass
    return counts


def retention_sweep_from_env(conn: Any) -> dict[str, int]:
    """Run :func:`retention_sweep` with operator-configured environment TTLs.

    Defaults:
    - ``AI_BRIDGE_RETENTION_TELEMETRY_KEEP``: 500 rows/device
    - ``AI_BRIDGE_RETENTION_TASK_EVENTS_DAYS``: 30 days
    - ``AI_BRIDGE_RETENTION_TASKS_DAYS``: 90 days
    - ``AI_BRIDGE_RETENTION_IDEMPOTENCY_HOURS``: 24 hours
    """
    keep = max(50, int(os.environ.get("AI_BRIDGE_RETENTION_TELEMETRY_KEEP", "500") or "500"))
    events_days = float(os.environ.get("AI_BRIDGE_RETENTION_TASK_EVENTS_DAYS", "30") or "30")
    tasks_days = float(os.environ.get("AI_BRIDGE_RETENTION_TASKS_DAYS", "90") or "90")
    idem_hours = float(os.environ.get("AI_BRIDGE_RETENTION_IDEMPOTENCY_HOURS", "24") or "24")
    return retention_sweep(
        conn,
        telemetry_keep=keep,
        task_events_older_than_s=events_days * 86400.0 if events_days > 0 else None,
        terminal_tasks_older_than_s=tasks_days * 86400.0 if tasks_days > 0 else None,
        idempotency_ttl_s=max(300.0, idem_hours * 3600.0),
    )


def delete_device_cascade(conn: Any, device_id: str) -> dict[str, int]:
    """Delete a device and explicitly clean its children (DB-02).

    ``tasks.device_id`` deliberately has no foreign key (``'local'`` is
    a virtual device with no ``devices`` row), so device deletion cannot
    rely on CASCADE for tasks: this helper deletes the device's tasks
    first — their approvals/task_events follow via the migration-015
    ON DELETE CASCADE keys — then its telemetry, then the device row,
    all in one transaction. Returns per-table deleted counts.
    """
    with conn.transaction():
        tasks = conn.execute(
            "DELETE FROM tasks WHERE device_id=?", (device_id,)
        ).rowcount or 0
        telemetry = conn.execute(
            "DELETE FROM telemetry WHERE device_id=?", (device_id,)
        ).rowcount or 0
        devices = conn.execute(
            "DELETE FROM devices WHERE id=?", (device_id,)
        ).rowcount or 0
    return {"tasks": int(tasks), "telemetry": int(telemetry), "devices": int(devices)}

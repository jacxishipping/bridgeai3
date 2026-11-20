"""Single audit writer for MCP tool calls (R-P0-06, Wave 1C).

Every MCP tool invocation — allowed and denied — is recorded through
this module, from the wrapper gate in server.py, so no tool can opt
out of auditing. Two sinks, in priority order:

1. Control-plane DB hash chain (``control.db.audit``) when a ``db_path``
   is given (explicit argument or ``AI_BRIDGE_MCP_AUDIT_DB`` env var).
   The control-plane import is done lazily inside the write so this
   module never creates an import cycle.
2. Append-only JSONL file with ``fsync`` per record when no DB is
   available (default ``~/.ai-bridge/mcp-audit.jsonl``, overridable
   with ``AI_BRIDGE_MCP_AUDIT_FILE``). If the DB write fails, the
   record falls back to JSONL rather than being silently dropped.

Secrets are redacted from arguments and result summaries before any
write (``security.redact_obj`` / ``security.redact``). Arguments and
result summaries are truncated so one call cannot flood the audit
store: at most 64 KiB of redacted argument JSON and 2,000 chars of
result summary per record; binary blobs (base64 image data) are
omitted entirely.

Failure policy (documented tradeoff): a failed audit write is
reported on stderr and the call returns ``None``; the gate that called
it still enforces its allow/deny decision. Audit unavailability must
not become a denial-of-service on tool execution, but the failure is
loud, and DB-05 (atomic chain) remains Wave 2 work.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .security import redact, redact_obj

MAX_ARGS_BYTES = 64_000
MAX_RESULT_CHARS = 2_000
_MAX_STRING_VALUE = 4_096
_BINARY_KEYS = frozenset({"base64_data", "image_base64", "data", "image_data"})

_JSONL_LOCK = threading.Lock()
_JSONL_PATH_OVERRIDE: Path | None = None


def set_jsonl_path(path: str | Path | None) -> None:
    """Override the JSONL audit file (used by tests). Pass None to clear."""
    global _JSONL_PATH_OVERRIDE
    _JSONL_PATH_OVERRIDE = Path(path) if path is not None else None


def _resolve_jsonl_path() -> Path:
    if _JSONL_PATH_OVERRIDE is not None:
        return _JSONL_PATH_OVERRIDE
    env_path = os.environ.get("AI_BRIDGE_MCP_AUDIT_FILE", "").strip()
    if env_path:
        return Path(env_path)
    return Path.home() / ".ai-bridge" / "mcp-audit.jsonl"


def _resolve_db_path(db_path: str | Path | None) -> Path | None:
    if db_path is not None:
        return Path(db_path)
    env_path = os.environ.get("AI_BRIDGE_MCP_AUDIT_DB", "").strip()
    if env_path:
        return Path(env_path)
    return None


def _truncate_args(value: Any) -> Any:
    """Recursively truncate long strings and drop binary blobs from args."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if str(k) in _BINARY_KEYS and isinstance(v, str) and len(v) > 256:
                out[k] = "<binary omitted>"
            else:
                out[k] = _truncate_args(v)
        return out
    if isinstance(value, list):
        return [_truncate_args(item) for item in value]
    if isinstance(value, str) and len(value) > _MAX_STRING_VALUE:
        return value[:_MAX_STRING_VALUE] + f"... [truncated {len(value) - _MAX_STRING_VALUE} chars]"
    return value


def _safe_args(args: dict | None) -> dict:
    if not args:
        return {}
    try:
        plain = json.loads(json.dumps(args, default=str))
    except Exception:
        plain = {"<unserializable>": str(type(args))}
    cleaned = redact_obj(_truncate_args(plain))
    encoded = json.dumps(cleaned, default=str).encode("utf-8", "replace")
    if len(encoded) > MAX_ARGS_BYTES:
        return {"<truncated>": f"arguments exceeded {MAX_ARGS_BYTES} bytes after redaction"}
    return cleaned


def _safe_result(result_summary: str | None) -> str:
    if not result_summary:
        return ""
    text = str(result_summary)
    # Never log image payloads.
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "... [truncated]"
    return redact(text)


def _write_jsonl(event: dict[str, Any]) -> None:
    path = _resolve_jsonl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, default=str) + "\n"
    with _JSONL_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


def _write_db_conn(event: dict[str, Any], conn: Any) -> None:
    """Write through the control-plane DB hash chain on a caller-owned
    connection (Gate 9, Wave 6). The caller owns the transaction: the
    audit row commits with the caller's own work (``control.db.audit``
    runs its insert inside ``conn.transaction()``), and the caller's
    tenant is preserved instead of the writer's default. The writer
    never commits or closes the caller's connection."""
    from .control import db as control_db

    control_db.audit(
        conn,
        tenant=event.get("tenant", "default"),
        actor=event.get("actor", "unknown"),
        device_id=event.get("device_id") or "local",
        tool=event.get("tool", event.get("action", "unknown")),
        args=event.get("arguments") or event.get("details") or {},
        decision=event.get("decision", "recorded"),
        result=event.get("result", ""),
        trace_id=event.get("trace_id") or None,
    )


def _write_db(event: dict[str, Any], db_path: Path) -> None:
    """Write through the control-plane DB hash chain (lazy import)."""
    from .control import db as control_db

    conn = control_db.connect(db_path)
    try:
        _write_db_conn(event, conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _write(event: dict[str, Any], db_path: str | Path | None) -> dict[str, Any] | None:
    resolved_db = _resolve_db_path(db_path)
    if resolved_db is not None:
        try:
            _write_db(event, resolved_db)
            return event
        except Exception as exc:  # fall back to JSONL, never drop silently
            print(f"[ai-bridge] audit DB write failed ({exc}); falling back to JSONL",
                  file=sys.stderr)
    try:
        _write_jsonl(event)
        return event
    except Exception as exc:
        print(f"[ai-bridge] audit write failed (DB and JSONL): {exc}", file=sys.stderr)
        return None


def record_tool_call(*, actor: str, session_id: str, tool: str,
                     tier: Any, device_id: str | None = None,
                     decision: str, args: dict | None = None,
                     result_summary: str | None = None,
                     db_path: str | Path | None = None) -> dict[str, Any] | None:
    """Record one MCP tool invocation. Returns the event dict, or None
    if every sink failed (failure is also reported on stderr)."""
    tier_label: str
    try:
        tier_label = str(int(tier))
    except (TypeError, ValueError):
        tier_label = str(tier)
    event: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "mcp_tool_call",
        "actor": actor,
        "session_id": session_id,
        "tool": tool,
        "tier": tier_label,
        "device_id": device_id or "local",
        "decision": decision,
        "arguments": _safe_args(args),
        "result": _safe_result(result_summary),
    }
    return _write(event, db_path)


def write_audit_event(*, actor: str, action: str,
                      device_id: str | None = None,
                      details: dict | None = None,
                      db_path: str | Path | None = None,
                      conn: Any = None,
                      tenant: str = "default",
                      decision: str = "recorded",
                      result: str | None = None) -> dict[str, Any] | None:
    """Record a generic audited admin/security event (e.g. an
    `unrestricted` policy grant). Same sinks and redaction as
    record_tool_call; the control-plane DB stores it as a tool-style
    audit row.

    Gate 9 (Wave 6): this is the single writer — every audit call site
    in the control plane routes through here. Pass the caller's live
    ``conn`` (and its ``tenant``) when the audit row must commit with
    the caller's own transaction on that connection; otherwise the
    writer opens its own connection from ``db_path`` (or
    ``AI_BRIDGE_MCP_AUDIT_DB``) and falls back to JSONL, as before.
    """
    event: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "audit_event",
        "actor": actor,
        "session_id": "",
        "tool": action,
        "action": action,
        "tier": "",
        "tenant": tenant,
        "device_id": device_id or "local",
        "decision": decision,
        "arguments": _safe_args(details),
        "details": _safe_args(details),
        "result": _safe_result(result),
    }
    if conn is not None:
        try:
            _write_db_conn(event, conn)
            return event
        except Exception as exc:  # fall back to JSONL, never drop silently
            print(f"[ai-bridge] audit DB write failed ({exc}); falling back to JSONL",
                  file=sys.stderr)
        try:
            _write_jsonl(event)
            return event
        except Exception as exc:
            print(f"[ai-bridge] audit write failed (DB and JSONL): {exc}", file=sys.stderr)
            return None
    return _write(event, db_path)

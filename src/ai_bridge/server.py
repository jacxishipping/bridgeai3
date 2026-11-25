"""MCP server: stdio (local clients) and Streamable HTTP (remote clients).

Usage:
  python -m ai_bridge.server                # stdio (default)
  python -m ai_bridge.server --http --port 8001   # Streamable HTTP
  AI_BRIDGE_HTTP=1 python -m ai_bridge.server

HTTP mode requires AI_BRIDGE_ADMIN_TOKEN (bearer) on every request.
There is no anonymous mode: the former anonymous-HTTP environment
variable bypass was removed in Wave 1C (R-P0-01). Local development
without a token must use stdio, or pass the explicit
--unsafe-allow-anon CLI flag (which refuses to run when
AI_BRIDGE_PROFILE=production).

Device-bound sessions: when the caller's bearer token belongs to an
OAuth client bound to a device (see the OAuth gateway's device token
field), or when the caller selects a target device via select_device(),
tools marked with @device_aware run on that device via control-plane
tasks instead of on the droplet. Unbound sessions keep the droplet-local
behavior.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import inspect
import json
import os
import platform
import secrets
import sys
import time
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import browser_tools, diagnostics, git_ops, tools, workspace
from .audit_writer import record_tool_call
from .device_routing import (
    BindingError,
    cancel_device_task,
    get_device_task,
    list_all_devices,
    resolve_binding,
    run_device_tool,
)
from .errors import ErrorCode, code_of_message, error_detail
from .execution import RUNNER
from .policy import TOOL_TIERS, Decision, RiskTier, decide, normalize_tool_name
from .security import read_workspace_text

try:
    # Wave 3B Track B: the tool registry is the single source of truth
    # for tool tiers. Until it lands, fall back to policy.TOOL_TIERS.
    from .tool_registry import get_tier as _registry_get_tier
except ImportError:  # pragma: no cover - registry not landed yet
    _registry_get_tier = None

# Local approval store: approvals are created via request_approval and
# granted only by approve_task (authenticated admin bearer session) or by
# the control-plane dashboard. AI text alone never grants approval.
# Records are scoped to the session that requested them (F-04-01,
# partial in Wave 1C — durable DB storage is Wave 2) and expire after
# _APPROVAL_TTL_S so a stale approval cannot be replayed later.
#
# F-04-01 (Wave 5A): when the control-plane DB is configured
# (AI_BRIDGE_MCP_AUDIT_DB, the same DB the audit writer uses),
# approvals live in that DB's settings table (control/approval_store)
# so they survive an MCP server restart and are visible to every
# process sharing the DB. Without a configured DB the in-process dict
# below remains the store (plain stdio use). The connection is cached
# for the life of the process, like the cached Redis clients.
_APPROVALS: dict[str, dict] = {}
_APPROVAL_TTL_S = 15 * 60
_APPROVAL_DB_CONN: Any | None = None
_APPROVAL_DB_PATH: str | None = None


def _approval_store_conn() -> Any | None:
    """Cached control-DB connection for durable approvals, or None.

    None means "no DB configured" (in-memory approvals) or "the DB
    could not be opened" (fall back to in-memory rather than breaking
    the approval flow; the failure is reported on stderr).
    """
    global _APPROVAL_DB_CONN, _APPROVAL_DB_PATH
    try:
        db_path = _audit_db_path()
    except Exception:
        return None
    if db_path is None:
        return None
    db_path = str(db_path)
    if _APPROVAL_DB_CONN is not None and _APPROVAL_DB_PATH == db_path:
        return _APPROVAL_DB_CONN
    if _APPROVAL_DB_CONN is not None:
        try:
            _APPROVAL_DB_CONN.close()
        except Exception:
            pass
        _APPROVAL_DB_CONN = None
        _APPROVAL_DB_PATH = None
    try:
        from .control import db as control_db

        _APPROVAL_DB_CONN = control_db.connect(db_path)
        _APPROVAL_DB_PATH = db_path
        return _APPROVAL_DB_CONN
    except Exception as exc:
        print(f"[ai-bridge] durable approval store unavailable ({exc}); "
              "using in-memory approvals", file=sys.stderr)
        _APPROVAL_DB_CONN = None
        _APPROVAL_DB_PATH = None
        return None


def _reset_approval_store_for_tests() -> None:
    global _APPROVAL_DB_CONN, _APPROVAL_DB_PATH
    try:
        if _APPROVAL_DB_CONN is not None:
            _APPROVAL_DB_CONN.close()
    except Exception:
        pass
    _APPROVAL_DB_CONN = None
    _APPROVAL_DB_PATH = None
    _APPROVALS.clear()

# R-P0-01 (Wave 1C): set ONLY by the explicit --unsafe-allow-anon CLI
# flag in main(). The former anonymous-HTTP env var no longer
# exists as an off-switch anywhere in this module.
_UNSAFE_ALLOW_ANON = False

# Per-session active device override set via select_device().
# Maps session key -> device_id (or "local" to force local execution).
_SELECTED_DEVICE: dict[str, str | None] = {}


def _peek_local_approval(tool_name: str) -> bool:
    """True if an approved, unexpired, same-session approval exists
    for tool_name. Peeks without consuming: the gate checks policy
    with this, the implementation wrapper consumes it."""
    key = _session_key()
    now = time.time()
    for rec in _APPROVALS.values():
        if rec.get("tool") != tool_name or rec.get("status") != "approved":
            continue
        if rec.get("session") is not None and rec["session"] != key:
            continue
        if now - float(rec.get("created_at", now)) > _APPROVAL_TTL_S:
            continue
        return True
    # F-04-01 (Wave 5A): same check against the durable store, when one
    # is configured. The in-memory dict is checked first so the common
    # path stays a dict lookup.
    try:
        conn = _approval_store_conn()
        if conn is not None:
            from .control import approval_store

            return approval_store.find_approved(
                conn, tool_name, key, _APPROVAL_TTL_S, now
            ) is not None
    except Exception:
        pass
    return False


def _consume_local_approval(tool_name: str) -> bool:
    """Consume one approved local approval record for tool_name, if any.
    Single-use, same-session, unexpired only."""
    key = _session_key()
    now = time.time()
    for rec in _APPROVALS.values():
        if rec.get("tool") != tool_name or rec.get("status") != "approved":
            continue
        if rec.get("session") is not None and rec["session"] != key:
            continue
        if now - float(rec.get("created_at", now)) > _APPROVAL_TTL_S:
            continue
        rec["status"] = "used"
        return True
    try:
        conn = _approval_store_conn()
        if conn is not None:
            from .control import approval_store

            return approval_store.consume_approved(
                conn, tool_name, key, _APPROVAL_TTL_S, now
            )
    except Exception:
        pass
    return False


def _require_host_tools_enabled(is_admin: bool = False) -> None:
    if is_admin:
        return
    if os.environ.get("AI_BRIDGE_DISABLE_HOST_TOOLS", "1") == "1":
        raise PermissionError("Unconfined host tools are disabled (AI_BRIDGE_DISABLE_HOST_TOOLS=1).")


_mcp_transport_security = None
if os.environ.get("AI_BRIDGE_MCP_ALLOWED_HOSTS"):
    _mcp_transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=os.environ.get("AI_BRIDGE_MCP_ALLOWED_HOSTS", "").split(","),
        allowed_origins=os.environ.get("AI_BRIDGE_MCP_ALLOWED_ORIGINS", "").split(",") if os.environ.get("AI_BRIDGE_MCP_ALLOWED_ORIGINS") else [],
    )
# R-P0-01 (Wave 1C): the former DNS-protection disable env var
# off-switch is deleted. With _mcp_transport_security left as None,
# FastMCP auto-enables DNS-rebinding protection for localhost
# (TransportSecuritySettings defaults enable_dns_rebinding_protection
# to True); there is no longer any way to disable it from the env.

# F-06-16 (Wave 7): `stateless_http=True` + `json_response=True` is the
# DEFAULT, and that default is deliberate — not an oversight:
#
# * The MCP HTTP app is embedded in the control plane
#   (`control/combined.py::create_combined_app`), which production runs
#   under gunicorn with 4 Uvicorn workers (`deployment/docker/
#   Dockerfile.prod`, `AI_BRIDGE_WORKERS=4`) behind a load balancer with
#   NO sticky sessions. Stateful Streamable HTTP sessions live in-process
#   in the SDK's `StreamableHTTPSessionManager`: a client whose next
#   request lands on a different worker gets an unknown `Mcp-Session-Id`
#   and its session is gone. Flipping the default to stateful would break
#   every remote client on the production deployment.
# * Resumability is a separate, larger gap: the SDK ships only the
#   `EventStore` ABC (`mcp/server/streamable_http.py`) — no Redis or
#   Postgres implementation. A shared, cross-worker event store would have
#   to be written before any session could survive a restart or a worker
#   change, even in stateful mode. Stateful mode below is therefore SSE
#   streaming + in-process sessions, NOT resumable sessions.
#
# What Wave 7 does instead: an explicit opt-in. Setting
# `AI_BRIDGE_MCP_STATEFUL_HTTP=1` builds the server with
# `stateless_http=False, json_response=False` (SSE streaming + server
# sessions). That is only safe for a SINGLE-PROCESS deployment — the
# standalone `python -m ai_bridge.server --http` (one uvicorn process) or
# a combined app run with one worker. If it is enabled while
# `AI_BRIDGE_WORKERS > 1`, startup prints a loud warning and keeps going
# (refusing to start would break existing single-worker containers that
# happen to export the variable); the operator must drop to one worker or
# add sticky sessions. The production default stays stateless, so this row
# stays OPEN for the deployed topology: closing it needs a shared
# EventStore plus sticky sessions or a single MCP worker, neither of which
# exists yet.
def _mcp_http_options() -> dict[str, bool]:
    """Stateless (default) vs stateful Streamable HTTP, from the env.

    `AI_BRIDGE_MCP_STATEFUL_HTTP=1` opts a single-process deployment
    into SSE streaming + server-managed sessions. Anything else (unset,
    `0`, `false`, ...) keeps the multi-worker-safe stateless default.
    Pure function of the environment, so tests can assert it without
    importing a second server instance.
    """
    stateful = os.environ.get("AI_BRIDGE_MCP_STATEFUL_HTTP", "").strip().lower() in (
        "1", "true", "yes",
    )
    return {"stateless_http": not stateful, "json_response": not stateful}


def _build_mcp_server() -> FastMCP:
    opts = _mcp_http_options()
    if not opts["stateless_http"]:
        try:
            workers = int(os.environ.get("AI_BRIDGE_WORKERS", "1") or "1")
        except ValueError:
            workers = 1
        if workers > 1:
            print(
                "[ai-bridge] WARNING: AI_BRIDGE_MCP_STATEFUL_HTTP=1 with "
                f"AI_BRIDGE_WORKERS={workers}: Streamable HTTP sessions are "
                "kept in-process, so a client whose next request lands on a "
                "different worker loses its session. Stateful MCP HTTP is "
                "only safe with a single worker (or sticky sessions).",
                file=sys.stderr,
            )
    return FastMCP(
        "ai-bridge",
        transport_security=_mcp_transport_security,
        **opts,
    )


mcp = _build_mcp_server()

VERIFY_URL = os.environ.get("AI_BRIDGE_GATEWAY_VERIFY_URL", "http://127.0.0.1:8002/internal/verify")
CONTROL_URL = os.environ.get("AI_BRIDGE_CONTROL_URL", "http://127.0.0.1:8000")


def _bearer_token() -> str | None:
    try:
        ctx = mcp.get_context()
        req = getattr(getattr(ctx, "request_context", None), "request", None)
    except Exception:
        return None
    if req is None:
        return None
    auth = (req.headers.get("authorization") or "")
    if not auth.startswith("Bearer "):
        return None
    return auth[7:].strip() or None


# F-06-02 (Wave 1C): anonymous stdio callers no longer share the
# session key "default". Each process invocation gets one random key
# (stable within the process so select_device/approvals keep working
# for that invocation, distinct across invocations).
_STDIO_SESSION_KEY: str | None = None


def _session_key() -> str:
    tok = _bearer_token()
    if tok:
        return hashlib.sha256(tok.encode()).hexdigest()[:16]
    global _STDIO_SESSION_KEY
    if _STDIO_SESSION_KEY is None:
        _STDIO_SESSION_KEY = "stdio-" + secrets.token_hex(8)
    return _STDIO_SESSION_KEY


def _oauth_bound_device() -> str | None:
    """Validate HTTP bearer token on every call; return locked device_id if OAuth-bound.

    F-04-02 / F-06-03 (Wave 1C): FAIL CLOSED. A verification failure
    (gateway down, network error, malformed response) used to be
    swallowed and treated as "unbound/local execution". It now denies
    the call with a logged PermissionError instead.
    """
    tok = _bearer_token()
    if not tok:
        return None
    admin_tok = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
    if admin_tok and hmac.compare_digest(tok, admin_tok):
        return None
    try:
        return resolve_binding(VERIFY_URL, tok)
    except Exception as exc:
        print(f"[ai-bridge] device binding verification failed; denying by default: {exc}",
              file=sys.stderr)
        raise PermissionError("Device binding verification failed; call denied.") from exc


def _session_device() -> str | None:
    """Device_id active for this MCP call, or None for unbound/local calls.

    OAuth gateway bindings are always enforced first if present.
    Unbound sessions check _SELECTED_DEVICE."""
    bound = _oauth_bound_device()
    if bound is not None:
        return bound
    key = _session_key()
    if key in _SELECTED_DEVICE:
        chosen = _SELECTED_DEVICE[key]
        return None if chosen in (None, "", "local") else chosen
    return None


# ---------------------------------------------------------------------------
# One wrapper gate (R-P1-05 / F-04-05 / F-06-05, Wave 1C).
#
# Every @mcp.tool wrapper is decorated with @_gated (applied between
# @device_aware and the function, so for device-routed calls the gate
# runs on the local execution path; routed calls are audited as
# "routed" in device_aware and authorised again by the agent/control
# plane). The gate:
#   1. validates arguments with pydantic models (F-06-10) — fail fast,
#      before any policy check or execution;
#   2. calls decide() (R-P0-05: every tool is tiered, unknown tools are
#      denied by default) with the peeked local approval and the
#      caller's admin status;
#   3. records EVERY invocation — allowed, denied, validation-failed,
#      or errored — through the single audit writer (R-P0-06).
# No tool can opt out: the audit call lives in the gate, not in the
# individual wrappers.
# ---------------------------------------------------------------------------

def _is_admin_caller() -> bool:
    """True when the current caller authenticated with the admin token."""
    admin_tok = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
    tok = _bearer_token()
    return bool(admin_tok and tok and hmac.compare_digest(tok, admin_tok))


def _actor_label() -> str:
    """Audit-safe actor label. Never contains a token value."""
    if _is_admin_caller():
        return "admin"
    tok = _bearer_token()
    if tok:
        return "bearer:" + hashlib.sha256(tok.encode()).hexdigest()[:8]
    return "stdio"


def _gate_tool(tool_name: str) -> Decision:
    """The single policy decision for one MCP tool call.

    An approved local approval record is itself admin authorisation
    for that tool: it could only have been approved via approve_task
    (admin bearer session) or the dashboard. The gate therefore treats a peeked
    approval as satisfying decide()'s admin requirement for Tier 4 —
    decide() itself is unchanged (approval without admin is still
    insufficient when called directly).
    """
    approved = _peek_local_approval(tool_name)
    is_admin = _is_admin_caller() or approved
    return decide(tools.default_policy(), tool_name,
                  approval_granted=approved, is_admin=is_admin)


def _tool_tier(tool_name: str) -> Any:
    """Tier label for audit records, from the tool registry when it is
    available, falling back to policy.TOOL_TIERS. server.py keeps no
    tier list of its own (F-06-14). The registry returns tier *names*
    ("HIGH_RISK"); they are normalized back to the RiskTier member so
    audit rows keep the numeric label they had before ("4")."""
    name = normalize_tool_name(tool_name)
    if _registry_get_tier is not None:
        try:
            tier = _registry_get_tier(name)
        except Exception:
            tier = None
        if tier is not None:
            try:
                return RiskTier[str(tier)]
            except (KeyError, AttributeError, TypeError):
                return tier
    return TOOL_TIERS.get(name, "?")


def _error_code_for(exc: BaseException) -> ErrorCode:
    """Taxonomy code for an exception raised by a tool implementation.

    A code already embedded as a ``[CODE]`` prefix wins, so a tool that
    raises a coded error is never re-coded; otherwise the type decides.
    """
    embedded = code_of_message(str(exc))
    if embedded is not None:
        return embedded
    if isinstance(exc, PermissionError):
        return ErrorCode.POLICY_DENIED
    if isinstance(exc, ValueError):
        return ErrorCode.VALIDATION_FAILED
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return ErrorCode.UPSTREAM_UNAVAILABLE
    return ErrorCode.INTERNAL_ERROR


def _summarize_result(result: Any) -> str:
    """Short, redacted, binary-free summary of a tool result for audit."""
    if result is None:
        return ""
    if isinstance(result, list):
        texts = []
        for item in result:
            if isinstance(item, dict):
                item = {k: v for k, v in item.items()
                        if k not in ("base64_data", "image_base64", "data")}
                texts.append(json.dumps(item, default=str)[:400])
            else:
                texts.append(str(item)[:400])
        return f"<{len(result)} items> " + "; ".join(texts)[:1800]
    if isinstance(result, dict):
        cleaned = {k: ("<binary omitted>" if k in ("base64_data", "image_base64", "data") and isinstance(v, str) and len(v) > 256 else v)
                   for k, v in result.items()}
        return json.dumps(cleaned, default=str)[:2000]
    return str(result)[:2000]


def _audit_db_path() -> str | None:
    return os.environ.get("AI_BRIDGE_MCP_AUDIT_DB") or None


# --- F-06-10: pydantic argument validation per tool family --------------
# Models declare only the fields they validate (extra="ignore"); the
# gate validates the bound call arguments against the model for the
# tool being invoked. Bounds are deliberately generous (multi-monitor
# coordinates, long text) — validation catches type errors and absurd
# values, it does not second-guess legitimate calls.

class _MouseClickArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    x: int = Field(ge=0, le=100000)
    y: int = Field(ge=0, le=100000)
    button: Literal["left", "right", "middle"] = "left"
    clicks: int = Field(default=1, ge=1, le=10)


class _MouseMoveArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    x: int = Field(ge=0, le=100000)
    y: int = Field(ge=0, le=100000)


class _MouseScrollArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    delta: int = Field(default=-120, ge=-100000, le=100000)
    x: int | None = Field(default=None, ge=0, le=100000)
    y: int | None = Field(default=None, ge=0, le=100000)


class _MouseDragArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start_x: int = Field(ge=0, le=100000)
    start_y: int = Field(ge=0, le=100000)
    end_x: int = Field(ge=0, le=100000)
    end_y: int = Field(ge=0, le=100000)


class _KeyboardPressArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str = Field(min_length=1, max_length=100)


class _DesktopKeyboardArgs(BaseModel):
    # desktop_keyboard may be called with text only (key defaults to "").
    model_config = ConfigDict(extra="ignore")
    text: str = Field(default="", max_length=100000)
    key: str = Field(default="", max_length=100)


class _KeyboardTypeArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(max_length=100000)


class _ScreenshotArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scale: float = Field(default=0.75, ge=0.2, le=1.0)
    quality: int = Field(default=75, ge=20, le=95)


class _ShellArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str = Field(min_length=1, max_length=100000)
    timeout: int = Field(default=0, ge=0, le=3600)


class _WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=10_000_000)


class _BrowserNavigateArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str = Field(min_length=1, max_length=8192)
    timeout: int = Field(default=30, ge=1, le=600)


class _SelectDeviceArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    device_id: str = Field(min_length=1, max_length=512)


class _HotkeyArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    combo: str = Field(min_length=1, max_length=100)


_TOOL_VALIDATORS: dict[str, type[BaseModel]] = {
    "mouse_click": _MouseClickArgs,
    "desktop_click": _MouseClickArgs,
    "mouse_move": _MouseMoveArgs,
    "desktop_mouse_move": _MouseMoveArgs,
    "mouse_scroll": _MouseScrollArgs,
    "desktop_scroll": _MouseScrollArgs,
    "mouse_drag": _MouseDragArgs,
    "keyboard_press": _KeyboardPressArgs,
    "desktop_keyboard": _DesktopKeyboardArgs,
    "keyboard_type": _KeyboardTypeArgs,
    "capture_desktop_frame": _ScreenshotArgs,
    "view_live_screen": _ScreenshotArgs,
    "interact_and_view_screen": _ScreenshotArgs,
    "run_shell": _ShellArgs,
    "desktop_execute_command": _ShellArgs,
    "write_file": _WriteFileArgs,
    "browser_navigate": _BrowserNavigateArgs,
    "browser_navigate_with_shot": _BrowserNavigateArgs,
    "select_device": _SelectDeviceArgs,
    "press_hotkey": _HotkeyArgs,
}


def _validate_tool_args(tool_name: str, bound_args: dict[str, Any]) -> None:
    """Validate bound call arguments for tool families with a model.
    Raises ValueError with a clear message on invalid input."""
    model = _TOOL_VALIDATORS.get(tool_name)
    if model is None:
        return
    try:
        model(**bound_args)
    except ValidationError as exc:
        raise ValueError(f"Invalid arguments for {tool_name}: {exc.errors(include_url=False)}") from exc


# Tools whose decide() is self-referential and therefore skipped by the
# gate (they are still validated and audited). approve_task is Tier 4,
# and decide() grants Tier 4 only when (approval_granted AND is_admin)
# — but an approval record for "approve_task" itself can never exist,
# because approvals are granted BY approve_task (for the target tool,
# e.g. delete_workspace_file). Running decide() on approve_task would
# therefore deny it for every caller, including admins: a deadlock.
# The exemption is safe because approve_task enforces its own
# authorisation inside the function: it requires an authenticated admin
# bearer session (_is_admin_caller()) and fails closed without one.
_GATE_DECIDE_EXEMPT = frozenset({"approve_task"})


# F-04-22 cache for the durable emergency-stop read (see
# _emergency_stop_active): last read time (monotonic) + value.
_ESTOP_CACHE: dict[str, Any] = {"at": 0.0, "active": False}
_ESTOP_CACHE_TTL_S = 1.0
_ESTOP_EXEMPT_TOOLS = frozenset({"request_approval", "approve_task"})


def _emergency_stop_active() -> bool:
    """True when the emergency stop is active (mirror or durable flag).

    F-04-22 (Wave 5A): the emergency stop was only a local mirror
    (plane_control_flags, synced by agent heartbeats) and a durable
    flag in the control-plane DB (control/plane_state.py) — neither
    was consulted by the MCP gate, so local shell/desktop tools kept
    executing while the stop was active. The gate checks both sources
    before executing any state-changing tool (tier >= 2). Read-only
    (tier 1) tools and the approval meta-tools are exempt: an admin
    must still be able to inspect state and grant approvals while the
    stop is active; executing on an approval stays blocked until the
    stop is cleared. The durable read is best-effort (1 s cache,
    read-only open of the control DB named by AI_BRIDGE_MCP_AUDIT_DB);
    a read failure falls back to the local mirror rather than
    blocking the gate.
    """
    try:
        from .plane_control_flags import is_emergency_stop as _mirror_active

        if _mirror_active():
            return True
    except Exception:
        pass
    now = time.monotonic()
    if now - float(_ESTOP_CACHE.get("at", 0.0)) < _ESTOP_CACHE_TTL_S:
        return bool(_ESTOP_CACHE.get("active", False))
    active = False
    try:
        db_path = _audit_db_path()
        if db_path is not None and os.path.exists(db_path):
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key='plane.emergency_stop'"
                ).fetchone()
                active = bool(row and str(row[0]) in ("1", "true", "True", "yes"))
            finally:
                conn.close()
    except Exception:
        active = False
    _ESTOP_CACHE["at"] = now
    _ESTOP_CACHE["active"] = active
    return active


def _reset_emergency_stop_cache_for_tests() -> None:
    _ESTOP_CACHE["at"] = 0.0
    _ESTOP_CACHE["active"] = False


def _gated(fn):
    """Uniform MCP wrapper gate: validate -> decide -> execute -> audit.

    Every error path reports a taxonomy code (errors.ErrorCode) instead
    of raw exception text: denials carry POLICY_DENIED (or
    APPROVAL_REQUIRED when decide() says an approval is missing),
    validation failures carry VALIDATION_FAILED, and tool exceptions are
    re-raised with their mapped code prefixed. The audit record carries
    the same code. Exception *types* are preserved (PermissionError /
    ValueError) so existing callers keep working.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        tool_name = fn.__name__
        actor = _actor_label()
        session_id = _session_key()
        device_id = kwargs.get("device") or kwargs.get("device_id")
        try:
            sig = inspect.signature(fn)
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            call_args: dict[str, Any] = dict(bound.arguments)
        except Exception:
            call_args = dict(kwargs)
        db_path = _audit_db_path()
        try:
            _validate_tool_args(tool_name, call_args)
        except ValueError as exc:
            coded = str(exc) if code_of_message(str(exc)) is not None else error_detail(
                ErrorCode.VALIDATION_FAILED, str(exc))
            record_tool_call(actor=actor, session_id=session_id, tool=tool_name,
                             tier=_tool_tier(tool_name),
                             device_id=device_id, decision="denied",
                             args=call_args, result_summary=coded,
                             db_path=db_path)
            raise ValueError(coded) from exc
        if tool_name in _GATE_DECIDE_EXEMPT:
            decision = Decision(True, reason="Approval meta-tool; authenticated by its admin bearer session (see _GATE_DECIDE_EXEMPT).")
        else:
            decision = _gate_tool(tool_name)
        if not decision.allowed:
            code = ErrorCode.APPROVAL_REQUIRED if decision.needs_approval else ErrorCode.POLICY_DENIED
            coded = error_detail(code, decision.reason)
            record_tool_call(actor=actor, session_id=session_id, tool=tool_name,
                             tier=_tool_tier(tool_name),
                             device_id=device_id, decision="denied",
                             args=call_args, result_summary=coded,
                             db_path=db_path)
            raise PermissionError(coded)
        # F-04-22: emergency stop blocks state-changing tools (tier >= 2)
        # even when policy would otherwise allow them. Read-only tools
        # and the approval meta-tools stay usable while the stop is on.
        # The tier is checked first so read-only calls never touch the
        # durable stop flag.
        if tool_name not in _ESTOP_EXEMPT_TOOLS:
            try:
                tier_no = int(_tool_tier(tool_name))  # RiskTier is an IntEnum
            except (TypeError, ValueError):
                tier_no = 0
            if tier_no >= 2 and _emergency_stop_active():
                coded = error_detail(
                    ErrorCode.POLICY_DENIED,
                    "Emergency stop is active: state-changing tools are blocked "
                    "until the emergency stop is cleared.",
                )
                record_tool_call(actor=actor, session_id=session_id, tool=tool_name,
                                 tier=_tool_tier(tool_name),
                                 device_id=device_id, decision="denied",
                                 args=call_args, result_summary=coded,
                                 db_path=db_path)
                raise PermissionError(coded)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            code = _error_code_for(exc)
            if code_of_message(str(exc)) is None:
                coded = error_detail(code, f"{type(exc).__name__}: {exc}")
                record_tool_call(actor=actor, session_id=session_id, tool=tool_name,
                                 tier=_tool_tier(tool_name),
                                 device_id=device_id, decision="allowed",
                                 args=call_args,
                                 result_summary=coded,
                                 db_path=db_path)
                try:
                    new_exc = type(exc)(coded)
                except Exception:
                    raise
                raise new_exc from exc
            record_tool_call(actor=actor, session_id=session_id, tool=tool_name,
                             tier=_tool_tier(tool_name),
                             device_id=device_id, decision="allowed",
                             args=call_args,
                             result_summary=str(exc),
                             db_path=db_path)
            raise
        record_tool_call(actor=actor, session_id=session_id, tool=tool_name,
                         tier=_tool_tier(tool_name),
                         device_id=device_id, decision="allowed",
                         args=call_args, result_summary=_summarize_result(result),
                         db_path=db_path)
        return result
    return wrapper


def _admin_token() -> str:
    token = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
    if not token:
        raise RuntimeError("AI_BRIDGE_ADMIN_TOKEN is not set; device-bound routing needs it.")
    return token


def _resolve_device_record(target: str | None) -> dict | None:
    """Resolve a target device identifier (id, friendly name, hostname)
    to the device record dict, or None if target is empty/'local'."""
    if not target:
        return None
    target_str = str(target).strip()
    if target_str.lower() in ("local", "host", "droplet"):
        return None
    admin = _admin_token()
    try:
        devices = list_all_devices(CONTROL_URL, admin)
    except Exception as exc:
        raise RuntimeError(f"Could not retrieve device fleet from control plane: {exc}")
    # 1. Exact ID match
    for d in devices:
        if d.get("id") == target_str:
            return d
    # 2. Case-insensitive exact name or hostname match
    low = target_str.lower()
    for d in devices:
        if (d.get("name") or "").lower() == low or (d.get("hostname") or "").lower() == low:
            return d
    # 3. ID prefix match
    matches = [d for d in devices if (d.get("id") or "").startswith(target_str)]
    if len(matches) == 1:
        return matches[0]

    available = [f"'{d.get('name') or 'unnamed'}' (id: {d.get('id')})" for d in devices]
    avail_str = ", ".join(available) if available else "No enrolled devices online"
    raise ValueError(f"Unknown device '{target_str}'. Enrolled devices: [{avail_str}], or use 'local'. Call list_devices() for full list.")


DESKTOP_ROUTING_TOOLS = {
    "get_desktop_status",
    "capture_desktop_screenshot",
    "take_screenshot",
    "capture_desktop_frame",
    "view_live_screen",
    "interact_and_view_screen",
    "launch_approved_application",
    "perform_approved_ui_action",
    "mouse_click",
    "mouse_move",
    "mouse_scroll",
    "mouse_drag",
    "keyboard_type",
    "keyboard_press",
    "list_desktop_windows",
    "focus_window",
    "get_screen_size",
    "ocr_screen",
    "click_text",
    "click_element",
    "list_ui_elements",
    "set_of_marks_screenshot",
    "wait_for_text",
    "verify_screen_contains",
    "press_hotkey",
    "get_clipboard",
    "set_clipboard",
    "desktop_get_screenshot",
    "desktop_get_window_screenshot",
    "desktop_get_status",
    "desktop_get_monitors",
    "desktop_clipboard_get",
    "desktop_clipboard_set",
    "desktop_mouse_move",
    "desktop_click",
    "desktop_keyboard",
    "desktop_scroll",
    "desktop_execute_command",
    "desktop_open_application",
    "desktop_get_windows",
    "desktop_wait",
    "desktop_verify_action",
    "desktop_a11y_status",
    "desktop_a11y_permissions",
    "desktop_a11y_snapshot",
    "desktop_a11y_find",
    "desktop_a11y_get_value",
    "desktop_a11y_list_surfaces",
    "desktop_a11y_list_apps",
    "desktop_a11y_act",
    "desktop_a11y_batch",
    "desktop_a11y_wait",
}


def _find_default_windows_device() -> str | None:
    """Find the primary active Windows desktop machine in the enrolled fleet."""
    try:
        admin = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
        if not admin:
            return None
        devices = list_all_devices(CONTROL_URL, admin)
        # 1. Prefer online Windows workstation
        for d in devices:
            os_str = str(d.get("os") or "").lower()
            if "win" in os_str and d.get("status") == "online":
                return d.get("id")
        # 2. Any online device with desktop automation enabled in policy
        for d in devices:
            if d.get("status") == "online":
                raw_p = d.get("policy_json") or "{}"
                try:
                    p = json.loads(raw_p) if isinstance(raw_p, str) else raw_p
                    if p.get("desktop_enabled"):
                        return d.get("id")
                except Exception:
                    pass
        # 3. Any enrolled Windows device
        for d in devices:
            os_str = str(d.get("os") or "").lower()
            if "win" in os_str:
                return d.get("id")
    except Exception:
        return None
    return None


def _resolve_target_device(target: str | None) -> str | None:
    rec = _resolve_device_record(target)
    return rec["id"] if rec else None


def device_aware(dispatch_name: str | None = None, *, bound: str = "route"):
    """Route a tool call to a target device (specified via device=... or session default).

    When a target device is active (or specified), tools with bound="route"
    run on that device via control-plane tasks and wait for the result.
    When targeting 'local' or when no device is selected, tools run
    locally on the MCP host / droplet.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                sig = inspect.signature(fn)
                bound_args = sig.bind(*args, **kwargs)
                bound_args.apply_defaults()
                call_args = dict(bound_args.arguments)
            except Exception:
                call_args = dict(kwargs)

            explicit_dev = call_args.pop("device", None)
            if explicit_dev is None:
                explicit_dev = call_args.pop("device_id", None)

            if explicit_dev is not None:
                device = _resolve_target_device(explicit_dev)
            else:
                device = _session_device()

            # Auto-route desktop GUI automation tools to active Windows device on headless hosts
            if device is None and (fn.__name__ in DESKTOP_ROUTING_TOOLS) and (platform.system() != "Windows"):
                win_dev = _find_default_windows_device()
                if win_dev is not None:
                    device = win_dev

            if device is None:
                kwargs.pop("device", None)
                kwargs.pop("device_id", None)
                return fn(*args, **kwargs)

            admin = _admin_token()
            if bound == "unavailable":
                # R-P0-06: routed-away local-only calls are audited too.
                record_tool_call(actor=_actor_label(), session_id=_session_key(),
                                 tool=fn.__name__,
                                 tier=_tool_tier(fn.__name__),
                                 device_id=device, decision="denied", args=call_args,
                                 result_summary="local-only tool called with a device target",
                                 db_path=_audit_db_path())
                raise ValueError(
                    f"Tool '{fn.__name__}' is only available on the local MCP host/droplet. "
                    f"To run on the local host, pass device='local' or call select_device('local')."
                )
            if bound == "route":
                # R-P0-06: device-routed calls skip the local gate (the
                # agent/control plane authorises them at dispatch — see
                # R-P0-10/R-P0-11, other waves) but are still audited
                # here as "routed" so no MCP invocation is invisible.
                try:
                    routed_result = run_device_tool(
                        CONTROL_URL, admin, device,
                        dispatch_name or fn.__name__, call_args
                    )
                except Exception as exc:
                    record_tool_call(actor=_actor_label(), session_id=_session_key(),
                                     tool=fn.__name__,
                                     tier=_tool_tier(fn.__name__),
                                     device_id=device, decision="routed", args=call_args,
                                     result_summary=f"error: {type(exc).__name__}: {exc}",
                                     db_path=_audit_db_path())
                    raise
                record_tool_call(actor=_actor_label(), session_id=_session_key(),
                                 tool=fn.__name__,
                                 tier=_tool_tier(fn.__name__),
                                 device_id=device, decision="routed", args=call_args,
                                 result_summary=_summarize_result(routed_result),
                                 db_path=_audit_db_path())
                return routed_result
            if bound == "control":
                name = fn.__name__
                control_result: Any = None
                handled = False
                if name in {"get_task_status", "get_task_result", "cancel_task"}:
                    tid = args[0] if args else kwargs["task_id"]
                    t_check = get_device_task(CONTROL_URL, admin, tid)
                    if name == "get_task_status":
                        control_result = {"task_id": t_check.get("id") or tid, "status": t_check.get("status")}
                    elif name == "get_task_result":
                        try:
                            out = json.loads(t_check.get("result") or "")
                        except Exception:
                            out = t_check.get("result") or ""
                        control_result = {"task_id": t_check.get("id") or tid, "status": t_check.get("status"), "output": out}
                    else:
                        t = cancel_device_task(CONTROL_URL, admin, tid)
                        control_result = {"task_id": t.get("task_id") or t.get("id") or tid, "cancelled": t.get("status") == "cancelled"}
                    handled = True
                elif name == "get_device_status":
                    d = _resolve_device_record(device)
                    if d is None:
                        raise ValueError(f"Unknown device: {device}")
                    control_result = {
                        "device_id": d.get("id"), "name": d.get("name"),
                        "hostname": d.get("hostname"), "os": d.get("os"),
                        "status": d.get("status"), "last_seen": d.get("last_seen"),
                    }
                    handled = True
                if handled:
                    record_tool_call(actor=_actor_label(), session_id=_session_key(),
                                     tool=fn.__name__,
                                     tier=_tool_tier(fn.__name__),
                                     device_id=device, decision="routed", args=call_args,
                                     result_summary=_summarize_result(control_result),
                                     db_path=_audit_db_path())
                    return control_result
            return fn(*args, **kwargs)
        return wrapper
    return deco

@mcp.tool()
@device_aware("get_system_info")
@_gated
def system_info(device: str | None = None) -> dict:
    """Return basic operating-system information for this machine or a fleet device.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return tools.get_system_info()

@mcp.tool()
@device_aware()
@_gated
def get_resource_usage(device: str | None = None) -> dict:
    """CPU, memory and disk usage for this machine or a fleet device.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return tools.get_resource_usage()

@mcp.tool()
@device_aware()
@_gated
def get_disk_usage(device: str | None = None) -> dict:
    """Disk usage for the root filesystem.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return diagnostics.disk_usage("/")

@mcp.tool()
@device_aware()
@_gated
def get_memory_usage(device: str | None = None) -> dict:
    """Memory usage for this machine or a fleet device.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return diagnostics.memory_usage()

@mcp.tool()
@device_aware()
@_gated
def list_workspace(relative_directory: str = ".", device: str | None = None) -> list[dict]:
    """List entries inside the configured workspace. No arbitrary filesystem access.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return workspace.list_workspace(relative_directory)

@mcp.tool()
@device_aware()
@_gated
def read_workspace_file(relative_path: str, device: str | None = None) -> str:
    """Read a UTF-8 text file inside the configured workspace (size-limited).
    device (optional): Target device name, hostname, ID, or 'local'."""
    return read_workspace_text(relative_path)

@mcp.tool()
@device_aware()
@_gated
def search_workspace(query: str, relative_directory: str = ".", device: str | None = None) -> list[dict]:
    """Search file contents inside the workspace (secret-named files excluded).
    device (optional): Target device name, hostname, ID, or 'local'."""
    return workspace.search_workspace(query, relative_directory)

@mcp.tool()
@device_aware()
@_gated
def create_workspace_file(relative_path: str, content: str, overwrite: bool = False, device: str | None = None) -> dict:
    """Create a file inside the workspace (Tier 2). Atomic write.
    device (optional): Target device name, hostname, ID, or 'local'."""
    tools._check("create_workspace_file")
    return workspace.create_workspace_file(relative_path, content, overwrite)

@mcp.tool()
@device_aware()
@_gated
def edit_workspace_file(relative_path: str, old_text: str, new_text: str, device: str | None = None) -> dict:
    """Edit a workspace file; a backup is taken first (Tier 3, opt-in).
    device (optional): Target device name, hostname, ID, or 'local'."""
    from .policy import decide
    from .tools import default_policy
    approved = _consume_local_approval("edit_workspace_file")
    d = decide(default_policy(), "edit_workspace_file", approval_granted=approved)
    if not d.allowed:
        raise PermissionError(d.reason + " Set AI_BRIDGE_ALLOW_TIER3=1 or use the approval queue.")
    return workspace.edit_workspace_file(relative_path, old_text, new_text)

@mcp.tool()
@device_aware()
@_gated
def move_workspace_file(source: str, destination: str, device: str | None = None) -> dict:
    """Move/rename a file inside the workspace (Tier 3, opt-in).
    device (optional): Target device name, hostname, ID, or 'local'."""
    from .policy import decide
    from .tools import default_policy
    approved = _consume_local_approval("move_workspace_file")
    d = decide(default_policy(), "move_workspace_file", approval_granted=approved)
    if not d.allowed:
        raise PermissionError(d.reason)
    return workspace.move_workspace_file(source, destination)

@mcp.tool()
@device_aware()
@_gated
def delete_workspace_file(relative_path: str, device: str | None = None) -> dict:
    """Delete a workspace file. Requires a prior approval (Tier 4).
    device (optional): Target device name, hostname, ID, or 'local'."""
    if not _consume_local_approval("delete_workspace_file"):
        raise PermissionError(error_detail(
            ErrorCode.APPROVAL_REQUIRED,
            "delete_workspace_file requires an approval: call request_approval, "
            "then approve_task as an authenticated admin, then retry."))
    return workspace.delete_workspace_file(relative_path)

@mcp.tool()
@device_aware()
@_gated
def get_service_status(service: str, device: str | None = None) -> dict:
    """Check whether a named service is active (allowlisted name only).
    device (optional): Target device name, hostname, ID, or 'local'."""
    return tools.get_service_status(service)

@mcp.tool()
@device_aware()
@_gated
def get_recent_logs(source: str = "system", lines: int = 50, device: str | None = None) -> str:
    """Recent system logs (journalctl on Linux, fixed Event Log query on Windows).
    device (optional): Target device name, hostname, ID, or 'local'."""
    return diagnostics.recent_logs(source, lines)

@mcp.tool()
@device_aware()
@_gated
def list_processes(limit: int = 50, device: str | None = None) -> list[dict]:
    """Top processes by CPU usage.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return diagnostics.list_processes(limit)

@mcp.tool()
@device_aware()
@_gated
def list_docker_containers(device: str | None = None) -> dict:
    """Docker containers, if Docker is installed.
    device (optional): Target device name, hostname, ID, or 'local'."""
    result = diagnostics.list_docker_containers()
    return result if isinstance(result, dict) else {"containers": result}

@mcp.tool()
@device_aware()
@_gated
def git_status(relative_directory: str = ".", device: str | None = None) -> str:
    """git status for a workspace directory.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return git_ops.git_status(relative_directory)

@mcp.tool()
@device_aware()
@_gated
def git_diff(relative_directory: str = ".", device: str | None = None) -> str:
    """git diff --stat for a workspace directory.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return git_ops.git_diff(relative_directory)

@mcp.tool()
@device_aware()
@_gated
def dev_workspace_plan(relative_directory: str = ".", max_depth: int = 3, device: str | None = None) -> dict:
    """Repo-aware plan: project tree plus git status and diff stat."""
    from . import dev_tools

    return dev_tools.dev_workspace_plan(relative_directory, max_depth=max_depth)

@mcp.tool()
@device_aware()
@_gated
def browser_session_info(device: str | None = None) -> dict:
    """Report whether a reusable local Playwright browser session is active."""
    from . import browser_tools

    return browser_tools.browser_session_info()

@mcp.tool()
@device_aware()
@_gated
def run_project_tests(test_command: str = "pytest", timeout: int = 120, device: str | None = None) -> dict:
    """Run an allowlisted test command (pytest / npm test / go test) with a timeout.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return tools.run_project_tests(test_command, timeout)

@mcp.tool()
@device_aware()
@_gated
def run_linter(timeout: int = 60, device: str | None = None) -> dict:
    """Run ruff check on the workspace, if ruff is installed.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return tools.run_linter(timeout)

@mcp.tool()
@device_aware()
@_gated
def create_backup(relative_directory: str = ".", device: str | None = None) -> dict:
    """Create a timestamped ZIP backup of a workspace directory.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return tools.create_backup(relative_directory)

@mcp.tool()
@device_aware()
@_gated
def deploy_application(project_directory: str = ".", target_directory: str = "deploy-staging", device: str | None = None) -> dict:
    """Staging deploy inside the workspace (backup first). Not a production deploy.
    device (optional): Target device name, hostname, ID, or 'local'."""
    approved = _consume_local_approval("deploy_application")
    return tools.deploy_application(project_directory, target_directory, approval_granted=approved)

@mcp.tool()
@device_aware(bound="unavailable")
@_gated
def submit_task(task_name: str, timeout: int = 30) -> dict:
    """Submit an allowlisted diagnostic/task to the bounded task runner."""
    tools._check("submit_task")
    if task_name in TOOL_TIERS:
        tools._check(task_name)
    r = RUNNER.submit(task_name, timeout=timeout, allowed_test_commands=tools.default_policy().allowed_test_commands)
    return {"task_id": r.task_id, "name": task_name, "status": r.status}

@mcp.tool()
@device_aware(bound="control")
@_gated
def get_task_status(task_id: str, device: str | None = None) -> dict:
    """Status of a submitted task on droplet or target device.
    device (optional): Target device name, hostname, ID, or 'local'."""
    r = RUNNER.get(task_id)
    if r is None:
        raise ValueError("Unknown task_id.")
    return {"task_id": r.task_id, "name": r.name, "status": r.status}

@mcp.tool()
@device_aware(bound="control")
@_gated
def get_task_result(task_id: str, device: str | None = None) -> dict:
    """Result/output of a submitted task on droplet or target device.
    device (optional): Target device name, hostname, ID, or 'local'."""
    r = RUNNER.get(task_id)
    if r is None:
        raise ValueError("Unknown task_id.")
    return {"task_id": r.task_id, "name": r.name, "status": r.status, "output": r.output}

@mcp.tool()
@device_aware(bound="control")
@_gated
def cancel_task(task_id: str, device: str | None = None) -> dict:
    """Cancel a running task on droplet or target device.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return {"task_id": task_id, "cancelled": RUNNER.cancel(task_id)}

@mcp.tool()
@device_aware(bound="unavailable")
@_gated
def request_approval(tool_name: str, reason: str) -> dict:
    """Create an approval request for a high-risk operation. It grants nothing by itself."""
    import uuid
    approval_id = uuid.uuid4().hex[:12]
    # F-04-01 (Wave 5A): scope the record to the requesting session
    # with a creation timestamp; _peek/_consume only honour
    # same-session, unexpired approvals. When the control-plane DB is
    # configured the record is stored durably (control/approval_store)
    # so it survives a restart; otherwise it lives in _APPROVALS.
    rec = {"tool": tool_name, "reason": reason, "status": "pending",
           "session": _session_key(), "created_at": time.time()}
    stored_durably = False
    try:
        conn = _approval_store_conn()
        if conn is not None:
            from .control import approval_store

            approval_store.create_approval(
                conn, approval_id, tool_name, reason, rec["session"],
                created_at=rec["created_at"],
            )
            stored_durably = True
    except Exception:
        stored_durably = False
    if not stored_durably:
        _APPROVALS[approval_id] = rec
    return {"approval_id": approval_id, "status": "pending",
            "note": "An administrator must approve this via the dashboard or approve_task (admin bearer session)."}

@mcp.tool()
@device_aware(bound="unavailable")
@_gated
def approve_task(approval_id: str) -> dict:
    """Approve a pending request.

    F-06-16: the admin token is never a tool argument. Authentication
    comes from the caller's session — the HTTP bearer token must equal
    AI_BRIDGE_ADMIN_TOKEN (_is_admin_caller()). Fails closed without
    an admin session, so a stdio caller cannot approve: approval from
    stdio must go through the dashboard.
    """
    if not _is_admin_caller():
        raise PermissionError(error_detail(
            ErrorCode.AUTH_REQUIRED,
            "approve_task requires an authenticated admin session (HTTP bearer token)."))
    # F-04-01 (Wave 5A): approve in the durable store when one is
    # configured, else in the in-memory dict. A record the dashboard
    # created in the DB is approved here too (same store, same id).
    try:
        conn = _approval_store_conn()
        if conn is not None:
            from .control import approval_store

            if approval_store.set_approval_status(conn, approval_id, "approved"):
                return {"approval_id": approval_id, "status": "approved"}
    except Exception:
        pass
    rec = _APPROVALS.get(approval_id)
    if rec is None:
        raise ValueError(error_detail(ErrorCode.APPROVAL_NOT_FOUND, f"Unknown approval_id: {approval_id}."))
    rec["status"] = "approved"
    return {"approval_id": approval_id, "status": "approved"}

@mcp.tool()
@device_aware()
@_gated
def get_desktop_status(device: str | None = None) -> dict:
    """Whether desktop automation is enabled for this machine or target device.
    device (optional): Target device name, hostname, ID, or 'local'."""
    return tools.get_desktop_status()

@mcp.tool()
@device_aware()
@_gated
def capture_desktop_screenshot(device: str | None = None) -> dict:
    """Screenshot, only when desktop automation is explicitly enabled.
    device (optional): Target device name, hostname, ID, or 'local'."""
    # F-06-06 (Wave 1C): pass the approval through so an approval can
    # actually unlock this Tier-3 tool.
    approved = _consume_local_approval("capture_desktop_screenshot")
    return tools.capture_desktop_screenshot(approval_granted=approved)


def _to_vision_content(res: dict) -> list:
    """Convert a frame/screenshot dict (or remote device task wrapper) into native MCP ImageContent + TextContent metadata."""
    if not isinstance(res, dict):
        return [TextContent(type="text", text=json.dumps(res))]
    inner = res.get("result") if isinstance(res.get("result"), dict) else res
    b64_val = inner.get("base64_data") or inner.get("image_base64") or res.get("base64_data") or res.get("image_base64")
    if not b64_val:
        return [TextContent(type="text", text=json.dumps(res))]
    b64 = str(b64_val)
    mime = str(inner.get("mime_type") or res.get("mime_type") or "image/jpeg")
    meta = {k: v for k, v in res.items() if k not in ("base64_data", "image_base64")}
    if isinstance(meta.get("result"), dict):
        meta["result"] = {k: v for k, v in meta["result"].items() if k not in ("base64_data", "image_base64")}
    return [
        ImageContent(type="image", data=b64, mimeType=mime),
        TextContent(type="text", text=json.dumps(meta)),
    ]


@mcp.tool()
@device_aware(dispatch_name="capture_desktop_frame")
@_gated
def capture_desktop_frame(scale: float = 0.75, quality: int = 70, device: str | None = None) -> dict:
    """Capture a fast scaled desktop frame optimized for real-time visual streaming.
    scale: Image scale factor between 0.2 and 1.0 (default 0.75).
    quality: JPEG compression quality between 20 and 95 (default 70).
    device (optional): Target machine or 'local'."""
    approved = _consume_local_approval("capture_desktop_screenshot")
    return tools.capture_desktop_frame(scale=scale, quality=quality, approval_granted=approved)


@mcp.tool()
@_gated
def view_live_screen(
    scale: float = 0.75,
    quality: int = 75,
    annotate: bool = False,
    max_marks: int = 40,
    device: str | None = None,
) -> list:
    """Stream/capture a live visual frame of the Windows desktop as a native MCP ImageContent block so ChatGPT, Claude, and Grok vision models can directly see the screen.
    scale: Image scale factor between 0.2 and 1.0 (default 0.75).
    quality: JPEG compression quality between 20 and 95 (default 75).
    annotate: When True, overlays numbered Set-of-Mark badges ([1], [2], [3]) on interactive UI controls and returns the marks table.
    max_marks: Maximum numbered UI badges when annotate=True (default 40).
    device (optional): Target Windows device name/ID, or automatically routes to the active Windows machine."""
    if annotate:
        res = set_of_marks_screenshot(max_marks=max_marks, device=device)
    else:
        res = capture_desktop_frame(scale=scale, quality=quality, device=device)
    return _to_vision_content(res)


@mcp.tool()
@_gated
def interact_and_view_screen(
    action: str,
    x: int | None = None,
    y: int | None = None,
    text: str | None = None,
    key: str | None = None,
    mark_id: str | None = None,
    button: str = "left",
    clicks: int = 1,
    delta: int = -120,
    settle_ms: int = 600,
    annotate: bool = False,
    scale: float = 0.75,
    quality: int = 75,
    device: str | None = None,
) -> list:
    """Execute a GUI action on the Windows desktop, wait briefly for the UI to settle, and immediately return the updated live screen as a native MCP ImageContent block in a single round-trip.
    action: 'click', 'double_click', 'right_click', 'move', 'scroll', 'type', 'press', 'hotkey', 'click_text', 'click_element', 'focus_window', 'launch_app', or 'none'.
    x, y: Pixel coordinates for mouse actions.
    text: Text to type (for 'type'), text on screen to click (for 'click_text'), window title (for 'focus_window'), or app name (for 'launch_app').
    key: Key or hotkey combo (e.g. 'enter', 'ctrl+l', 'alt+tab') for 'press' / 'hotkey'.
    mark_id: Numbered Set-of-Mark badge ID (e.g. '1', '2') for 'click_element'.
    settle_ms: Milliseconds to wait after the action before capturing the screen (default 600ms).
    annotate: When True, overlays numbered Set-of-Mark badges on the returned screenshot.
    device (optional): Target Windows device name/ID, or automatically routes to the active Windows machine."""
    import time as _time

    act = (action or "none").strip().lower()
    action_res: dict = {"action": act}

    # The model sees the previous frame, which may have been scaled for
    # streaming. Map those image coordinates back to physical screen pixels
    # before injecting input.
    try:
        _frame_scale = float(getattr(tools, "LAST_FRAME_SCALE", 1.0) or 1.0)
    except Exception:
        _frame_scale = 1.0
    if 0.05 < _frame_scale < 0.999:
        if x is not None:
            x = int(round(float(x) / _frame_scale))
        if y is not None:
            y = int(round(float(y) / _frame_scale))

    try:
        if act in ("click", "double_click", "right_click", "middle_click"):
            if x is None or y is None:
                raise ValueError(f"Action '{act}' requires x and y coordinates.")
            btn = "right" if act == "right_click" else ("middle" if act == "middle_click" else button)
            c_cnt = 2 if act == "double_click" else clicks
            action_res = mouse_click(x=int(x), y=int(y), button=btn, clicks=c_cnt, device=device)
        elif act == "move":
            if x is None or y is None:
                raise ValueError("Action 'move' requires x and y coordinates.")
            action_res = mouse_move(x=int(x), y=int(y), device=device)
        elif act == "scroll":
            action_res = mouse_scroll(delta=delta, x=x, y=y, device=device)
        elif act == "type":
            action_res = keyboard_type(text=text or "", device=device)
            if key:
                action_res["key_result"] = press_hotkey(combo=key, device=device)
        elif act in ("press", "hotkey"):
            action_res = press_hotkey(combo=key or text or "enter", device=device)
        elif act == "click_text":
            action_res = click_text(text=text or "", button=button, clicks=clicks, device=device)
        elif act == "click_element":
            action_res = click_element(mark_id=mark_id, name=text, button=button, clicks=clicks, device=device)
        elif act == "focus_window":
            action_res = focus_window(title=text or "", device=device)
        elif act == "launch_app":
            action_res = launch_approved_application(app_name=text or "", url=key, device=device)
        elif act in ("none", "wait", "view"):
            action_res = {"ok": True, "action": act}
        else:
            raise ValueError(f"Unsupported action: '{action}'.")
    except Exception as exc:
        action_res = {"ok": False, "action": act, "error": str(exc)}

    wait_s = max(0.0, min(float(settle_ms or 0), 5000.0)) / 1000.0
    if wait_s > 0:
        _time.sleep(wait_s)

    if annotate:
        frame = set_of_marks_screenshot(max_marks=40, device=device)
    else:
        frame = capture_desktop_frame(scale=scale, quality=quality, device=device)

    if isinstance(frame, dict):
        frame = {**frame, "action_executed": act, "action_result": action_res}
    return _to_vision_content(frame)


@mcp.tool()
@device_aware(dispatch_name="capture_desktop_screenshot")
@_gated
def take_screenshot(device: str | None = None) -> dict:
    """Capture a high-resolution screenshot of the Windows desktop.
    Returns JPEG base64 image data, width, height, and currently active foreground window title.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    # F-06-06 (Wave 1C): pass the approval through so an approval can
    # actually unlock this Tier-3 tool.
    approved = _consume_local_approval("take_screenshot")
    return tools.capture_desktop_screenshot(approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def mouse_click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    device: str | None = None,
) -> dict:
    """Click the mouse at (x, y) coordinates on the Windows GUI.
    button: 'left' (default), 'right', or 'middle'.
    clicks: 1 for single click, 2 for double click, 3 for triple click.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("mouse_click")
    return tools.mouse_click(x=x, y=y, button=button, clicks=clicks, approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def mouse_move(x: int, y: int, device: str | None = None) -> dict:
    """Move the mouse cursor to (x, y) coordinates on the Windows GUI.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("mouse_move")
    return tools.mouse_move(x=x, y=y, approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def mouse_scroll(
    delta: int = -120,
    x: int | None = None,
    y: int | None = None,
    device: str | None = None,
) -> dict:
    """Scroll the mouse wheel on the Windows GUI.
    delta: positive scrolls up (e.g. 120), negative scrolls down (e.g. -120).
    x, y (optional): Cursor coordinates before scrolling.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("mouse_scroll")
    return tools.mouse_scroll(delta=delta, x=x, y=y, approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def mouse_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    device: str | None = None,
) -> dict:
    """Click and drag the mouse from (start_x, start_y) to (end_x, end_y) on the Windows GUI.
    Useful for selecting text, dragging windows, slider controls, or drag-and-drop operations.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("mouse_drag")
    return tools.mouse_drag(start_x=start_x, start_y=start_y, end_x=end_x, end_y=end_y, approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def keyboard_type(text: str, device: str | None = None) -> dict:
    """Type unicode text into the currently active window or input control on the Windows GUI.
    Uses native Win32 Unicode injection for full compatibility across Chrome, Edge, VS Code, Terminal, etc.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("keyboard_type")
    return tools.keyboard_type(text=text, approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def keyboard_press(key: str, device: str | None = None) -> dict:
    """Press a key or key combination on the Windows GUI.
    Supported keys: 'enter', 'tab', 'esc', 'space', 'backspace', 'delete', 'win', 'up', 'down', 'left', 'right',
    'pageup', 'pagedown', 'home', 'end', 'f1'-'f12', or hotkeys like 'ctrl+c', 'ctrl+v', 'ctrl+a', 'alt+tab', 'alt+f4', 'win+r'.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("keyboard_press")
    return tools.keyboard_press(key=key, approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def list_desktop_windows(device: str | None = None) -> dict:
    """List all visible top-level windows on the desktop with titles, handles, and bounding rectangles [x, y, width, height].
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("list_desktop_windows")
    return tools.list_desktop_windows(approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def focus_window(title: str, device: str | None = None) -> dict:
    """Bring the window matching 'title' substring to the foreground and unminimize it if minimized.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("focus_window")
    return tools.focus_window(title=title, approval_granted=approved)

@mcp.tool()
@device_aware()
@_gated
def get_screen_size(device: str | None = None) -> dict:
    """Get the primary desktop screen width and height in pixels.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    return tools.get_screen_size()

@mcp.tool()
@device_aware()
@_gated
def launch_approved_application(app_name: str, url: str | None = None, device: str | None = None) -> dict:
    """Launch an application from the admin-approved list only.
    device (optional): Target device name, hostname, ID, or 'local'."""
    approved = _consume_local_approval("launch_approved_application")
    return tools.launch_approved_application(app_name, approval_granted=approved, url=url)

@mcp.tool()
@device_aware()
@_gated
def perform_approved_ui_action(
    action: str,
    x: int | None = None,
    y: int | None = None,
    text: str | None = None,
    key: str | None = None,
    title: str | None = None,
    delta: int = -120,
    device: str | None = None,
) -> dict:
    """Perform a structured desktop UI action (list_windows, focus_window,
    move_mouse, click, double_click, right_click, scroll, type_text,
    press_key/hotkey). Requires desktop_enabled and Tier-3 permission.
    device (optional): Target device name, hostname, ID, or 'local'."""
    approved = _consume_local_approval("perform_approved_ui_action")
    return tools.perform_approved_ui_action(
        action, x=x, y=y, text=text, key=key, title=title, delta=delta, approval_granted=approved
    )

@mcp.tool()
@device_aware(bound="control")
@_gated
def list_devices() -> list[dict]:
    """List all enrolled devices in the fleet and the local MCP host.
    Returns device IDs, friendly names, OS, and online status.
    GUI automation tools (mouse_click, mouse_move, mouse_scroll, mouse_drag, keyboard_type,
    keyboard_press, capture_desktop_screenshot, take_screenshot, list_desktop_windows, focus_window)
    automatically route to your active Windows machine if you are connected via cloud MCP.
    To execute other tools on any device, either:
      1. Pass device='<device_name_or_id>' to any tool call.
      2. Call select_device('<device_name_or_id>') to set the default target device for your session.
      3. Use device='local' to target the MCP host/droplet."""
    active = _session_device() or "local"
    out: list[dict] = []
    info = tools.get_system_info()
    out.append({
        "device_id": "local",
        "name": "local",
        "hostname": info.get("machine"),
        "system": info.get("system"),
        "os": info.get("system"),
        "status": "online",
        "workspace": info.get("workspace"),
        "active": active == "local",
        "routing_hint": "Pass device='local' or call select_device('local') to target the droplet host.",
    })
    token = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
    if token:
        try:
            for d in list_all_devices(CONTROL_URL, token):
                did = d.get("id")
                out.append({
                    "device_id": did,
                    "name": d.get("name"),
                    "hostname": d.get("hostname"),
                    "os": d.get("os"),
                    "status": d.get("status"),
                    "last_seen": d.get("last_seen"),
                    "active": did == active,
                    "routing_hint": f"Pass device='{d.get('name') or did}' to target this machine.",
                })
        except Exception:
            pass
    return out

@mcp.tool()
@_gated
def select_device(device_id: str) -> dict:
    """Switch the default target device for this MCP session by device_id,
    device name, hostname, or 'local' (to target the MCP host/droplet).
    All subsequent tool calls without an explicit device parameter will execute on this device."""
    target = (device_id or "").strip()
    key = _session_key()
    if len(_SELECTED_DEVICE) >= 1024 and key not in _SELECTED_DEVICE:
        _SELECTED_DEVICE.pop(next(iter(_SELECTED_DEVICE)), None)
    if not target or target.lower() in ("local", "host", "droplet"):
        _SELECTED_DEVICE[key] = "local"
        return {
            "selected_device": "local",
            "mode": "local",
            "note": "Tools will execute locally on the MCP host (droplet).",
        }
    d = _resolve_device_record(target)
    if not d:
        _SELECTED_DEVICE[key] = "local"
        return {
            "selected_device": "local",
            "mode": "local",
            "note": "Tools will execute locally on the MCP host (droplet).",
        }
    resolved = d["id"]
    _SELECTED_DEVICE[key] = resolved
    return {
        "selected_device": resolved,
        "name": d.get("name"),
        "hostname": d.get("hostname"),
        "os": d.get("os"),
        "status": d.get("status"),
        "mode": "remote",
        "note": f"Tools will now execute remotely on '{d.get('name') or resolved}' by default.",
    }

@mcp.tool()
@device_aware(bound="control")
@_gated
def get_device_status(device_id: str = "local") -> dict:
    """Status of a device ('local' for MCP host, or any enrolled device name or ID)."""
    if not device_id or device_id.strip().lower() in ("local", "host", "droplet"):
        return {"device_id": "local", "status": "online", **tools.get_resource_usage()}
    d = _resolve_device_record(device_id)
    if d is not None:
        return {
            "device_id": d.get("id"), "name": d.get("name"), "status": d.get("status"),
            "hostname": d.get("hostname"), "os": d.get("os"), "last_seen": d.get("last_seen"),
        }
    raise ValueError(f"Device '{device_id}' not found.")

@mcp.tool()
@device_aware(bound="unavailable")
@_gated
def run_diagnostic_task(task_name: str) -> str:
    """Run one predefined, read-only diagnostic task: uptime, disk_usage, memory_summary."""
    from .tasks import run_diagnostic
    return run_diagnostic(task_name)

@mcp.tool()
@device_aware()
@_gated
def run_shell(command: str, working_directory: str = "", timeout: int = 0, device: str | None = None) -> dict:
    """Run a command as an argv list (no shell interpretation) on any enrolled device or the local host.
    Pipes, redirection and && are not interpreted; pass the executable and its arguments.
    device (optional): Target device name, hostname, ID, or 'local'. Defaults to active device or local host.
    Tier-3 sensitive tool: executed on the target device via control-plane dispatch.
    timeout=0 (default) means standard execution without time limit. Output truncated per stream."""
    admin_tok = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
    bearer = _bearer_token()
    is_admin = bool(admin_tok and bearer and hmac.compare_digest(bearer, admin_tok))
    _require_host_tools_enabled(is_admin=is_admin)
    approved = _consume_local_approval("run_shell")
    return tools.run_shell(command, working_directory, timeout, approval_granted=approved, is_admin=is_admin)


@mcp.tool()
@device_aware()
@_gated
def read_file(path: str, max_chars: int = 100000, device: str | None = None) -> str:
    """Read any UTF-8 text file on this machine or target device by path (host tools opt-in required).
    device (optional): Target device name, hostname, ID, or 'local'."""
    admin_tok = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
    bearer = _bearer_token()
    is_admin = bool(admin_tok and bearer and hmac.compare_digest(bearer, admin_tok))
    _require_host_tools_enabled(is_admin=is_admin)
    return tools.read_file(path, max_chars, is_admin=is_admin)


@mcp.tool()
@device_aware()
@_gated
def write_file(path: str, content: str, overwrite: bool = False, device: str | None = None) -> dict:
    """Write content to any absolute path on this machine or target device (host tools opt-in required).
    device (optional): Target device name, hostname, ID, or 'local'.
    Refuses to overwrite an existing file unless overwrite=true."""
    admin_tok = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
    bearer = _bearer_token()
    is_admin = bool(admin_tok and bearer and hmac.compare_digest(bearer, admin_tok))
    _require_host_tools_enabled(is_admin=is_admin)
    # F-06-06 (Wave 1C): pass the approval through so an approval can
    # actually unlock this Tier-3 tool.
    approved = _consume_local_approval("write_file")
    return tools.write_file(path, content, overwrite, approval_granted=approved, is_admin=is_admin)


@mcp.tool()
@device_aware()
@_gated
def install_package(package_id: str, version: str | None = None, device: str | None = None) -> dict:
    """Install a software package on the target device (or local host).
    Supports Linux & WSL (apt-get on Ubuntu/Debian, dnf on Fedora/RHEL, pacman on Arch, apk on Alpine)
    and Windows (winget).
    device (optional): Target device name, hostname, ID, or 'local'.
    Tier-3 sensitive tool: requires approval in dashboard or policy opt-in (AI_BRIDGE_ALLOW_TIER3=1)."""
    approved = _consume_local_approval("install_package")
    return tools.install_package(package_id, version, approval_granted=approved)


@mcp.tool()
@device_aware("ocr_screen")
@_gated
def ocr_screen(device: str | None = None) -> dict:
    """Run native OCR on the current desktop display and return extracted text with pixel coordinates.
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("ocr_screen")
    return tools.ocr_screen(approval_granted=approved)


@mcp.tool()
@device_aware("click_text")
@_gated
def click_text(text: str, occurrence: int = 1, button: str = "left", clicks: int = 1, device: str | None = None) -> dict:
    """Find and click visible text on screen using native OCR + UI grounding (no pixel guessing needed).
    text: Text on screen to locate and click.
    occurrence: 1st, 2nd, etc. occurrence if text appears multiple times (default 1).
    button: 'left', 'right', 'middle'.
    clicks: 1 (single click) or 2 (double click).
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("click_text")
    return tools.click_text(text, occurrence=occurrence, button=button, clicks=clicks, approval_granted=approved)


@mcp.tool()
@device_aware("click_element")
@_gated
def click_element(mark_id: str | None = None, name: str | None = None, button: str = "left", clicks: int = 1, device: str | None = None) -> dict:
    """Click a labeled mark (from set_of_marks_screenshot) or element name.
    mark_id: Tag number (e.g. '1', '2') from set_of_marks_screenshot.
    name: Text or control name.
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("click_element")
    return tools.click_element(mark_id=mark_id, name=name, button=button, clicks=clicks, approval_granted=approved)


@mcp.tool()
@device_aware("list_ui_elements")
@_gated
def list_ui_elements(device: str | None = None) -> dict:
    """List visible interactive Windows controls (menus, buttons, input fields, child controls) with labels and bounds.
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("list_ui_elements")
    return tools.list_ui_elements(approval_granted=approved)


@mcp.tool()
@device_aware("set_of_marks_screenshot")
@_gated
def set_of_marks_screenshot(max_marks: int = 40, device: str | None = None) -> dict:
    """Capture a screenshot with numbered visual tag badges ([1], [2], [3]) overlaid over interactive elements.
    max_marks: Maximum elements to tag (default 40).
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("set_of_marks_screenshot")
    return tools.set_of_marks_screenshot(max_marks=max_marks, approval_granted=approved)


@mcp.tool()
@device_aware("wait_for_text")
@_gated
def wait_for_text(text: str, timeout_seconds: float = 15.0, poll_interval: float = 1.0, occurrence: int = 1, device: str | None = None) -> dict:
    """Poll screen with native OCR until specified text appears or timeout is reached.
    text: Text or label to wait for on screen.
    timeout_seconds: Maximum seconds to wait (default 15.0).
    poll_interval: Seconds between screen checks (default 1.0).
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("wait_for_text")
    return tools.wait_for_text(text, timeout_seconds=timeout_seconds, poll_interval=poll_interval, occurrence=occurrence, approval_granted=approved)


@mcp.tool()
@device_aware("verify_screen_contains")
@_gated
def verify_screen_contains(text: str, occurrence: int = 1, device: str | None = None) -> dict:
    """One-shot instant assertion: check if text is currently displayed on screen.
    text: Expected text on screen.
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("verify_screen_contains")
    return tools.verify_screen_contains(text, occurrence=occurrence, approval_granted=approved)


@mcp.tool()
@device_aware("press_hotkey")
@_gated
def press_hotkey(combo: str, device: str | None = None) -> dict:
    """Press a standard keyboard shortcut or hotkey combo on desktop (e.g. 'ctrl+s', 'alt+tab', 'enter').
    combo: Key combination like 'ctrl+s', 'ctrl+w', 'alt+f4', 'enter'.
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("press_hotkey")
    return tools.press_hotkey(combo, approval_granted=approved)


@mcp.tool()
@device_aware("get_clipboard")
@_gated
def get_clipboard(device: str | None = None) -> dict:
    """Read the current clipboard text from the Windows desktop or target fleet device.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    return tools.get_clipboard()


@mcp.tool()
@device_aware("set_clipboard")
@_gated
def set_clipboard(text: str, paste: bool = False, device: str | None = None) -> dict:
    """Copy text/code directly to the Windows desktop clipboard, optionally pasting it immediately (Ctrl+V) into the active window.
    text: Text content to place into the clipboard.
    paste: When True, automatically presses Ctrl+V after setting the clipboard so long code blocks or URLs paste instantaneously.
    device (optional): Target Windows device name/ID, or automatically routes to active Windows machine."""
    approved = _consume_local_approval("set_clipboard")
    return tools.set_clipboard(text, paste=paste, approval_granted=approved)


@mcp.tool()
@device_aware("execute_workflow")
@_gated
def execute_workflow(steps: list[dict], stop_on_failure: bool = True, max_retries: int = 1, device: str | None = None) -> dict:
    """Execute a structured plan of autonomous actions sequentially with verification and retry.
    steps: List of action dicts: [{'action': '...', 'args': {...}, 'name': '...'}].
    stop_on_failure: Stop subsequent steps if a step fails.
    max_retries: Retries per step upon failure.
    device (optional): Target device name (e.g. 'windows-pc') or 'local'."""
    approved = _consume_local_approval("execute_workflow")
    return tools.execute_workflow(steps, stop_on_failure=stop_on_failure, max_retries=max_retries, approval_granted=approved)


@mcp.tool()
@_gated
def broadcast_fleet_command(
    tool: str,
    args: dict = {},
    target_os: str | None = None,
    target_device_ids: list[str] | None = None,
) -> dict:
    """Broadcast an action or diagnostic tool across multiple fleet devices simultaneously.

    tool: Name of the tool to run across machines (e.g. 'get_system_info', 'run_shell').
    args: Arguments to supply to the tool (e.g. {'command': 'uptime'}).
    target_os (optional): Filter nodes by OS ('linux', 'windows', or omit for all).
    target_device_ids (optional): List of explicit device IDs to target.
    """
    approved = _consume_local_approval("broadcast_fleet_command")
    return tools.broadcast_fleet_command(
        tool,
        args=args,
        target_os=target_os,
        target_device_ids=target_device_ids,
        approval_granted=approved,
    )


# ---------------------------------------------------------------------------
# Canonical v1.0 MCP Computer-Use Toolset (Section 8)
# ---------------------------------------------------------------------------

@mcp.tool()
@device_aware("desktop_get_screenshot")
@_gated
def desktop_get_screenshot(device: str | None = None) -> dict:
    """Return a current desktop screenshot.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_get_screenshot")
    return tools.desktop_get_screenshot(approval_granted=approved)


@mcp.tool()
@device_aware("desktop_get_window_screenshot")
@_gated
def desktop_get_window_screenshot(hwnd: int | None = None, title: str = "", scale: float = 1.0, device: str | None = None) -> dict:
    """Return a screenshot of a specific application window by HWND or title.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_get_window_screenshot")
    return tools.desktop_get_window_screenshot(hwnd=hwnd, title=title, scale=scale, approval_granted=approved)


@mcp.tool()
@device_aware("desktop_get_monitors")
@_gated
def desktop_get_monitors(device: str | None = None) -> dict:
    """Return list of connected displays with dimensions, offsets, and primary status.
    device (optional): Target Windows device name/ID, or 'local'."""
    return tools.desktop_get_monitors()


@mcp.tool()
@device_aware("desktop_clipboard_get")
@_gated
def desktop_clipboard_get(device: str | None = None) -> dict:
    """Read the current system clipboard text.
    device (optional): Target Windows device name/ID, or 'local'."""
    return tools.desktop_clipboard_get()


@mcp.tool()
@device_aware("desktop_clipboard_set")
@_gated
def desktop_clipboard_set(text: str, paste: bool = False, device: str | None = None) -> dict:
    """Write text to the system clipboard and optionally trigger Ctrl+V paste.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_clipboard_set")
    return tools.desktop_clipboard_set(text=text, paste=paste, approval_granted=approved)



@mcp.tool()
@device_aware("desktop_get_status")
@_gated
def desktop_get_status(device: str | None = None) -> dict:
    """Return desktop and session status, monitors, and human takeover state.
    device (optional): Target Windows device name/ID, or 'local'."""
    return tools.desktop_get_status()


@mcp.tool()
@device_aware("desktop_mouse_move")
@_gated
def desktop_mouse_move(x: int, y: int, device: str | None = None) -> dict:
    """Move the mouse to approved coordinates.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_mouse_move")
    return tools.desktop_mouse_move(x=x, y=y, approval_granted=approved)


@mcp.tool()
@device_aware("desktop_click")
@_gated
def desktop_click(x: int, y: int, button: str = "left", clicks: int = 1, device: str | None = None) -> dict:
    """Click at approved coordinates.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_click")
    return tools.desktop_click(x=x, y=y, button=button, clicks=clicks, approval_granted=approved)


@mcp.tool()
@device_aware("desktop_keyboard")
@_gated
def desktop_keyboard(text: str = "", key: str = "", action: str = "type", device: str | None = None) -> dict:
    """Send approved keyboard input (text typing, key press, or hotkey shortcut).
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_keyboard")
    return tools.desktop_keyboard(text=text, key=key, action=action, approval_granted=approved)


@mcp.tool()
@device_aware("desktop_scroll")
@_gated
def desktop_scroll(delta: int = -120, x: int | None = None, y: int | None = None, device: str | None = None) -> dict:
    """Scroll the active desktop.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_scroll")
    return tools.desktop_scroll(delta=delta, x=x, y=y, approval_granted=approved)


@mcp.tool()
@device_aware("desktop_execute_command")
@_gated
def desktop_execute_command(command: str, working_directory: str = "", timeout: int = 60, device: str | None = None) -> dict:
    """Execute commands permitted by policy. Screen access does not automatically grant command execution.
    device (optional): Target Windows device name/ID, or 'local'."""
    admin_tok = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
    bearer = _bearer_token()
    is_admin = bool(admin_tok and bearer and hmac.compare_digest(bearer, admin_tok))
    _require_host_tools_enabled(is_admin=is_admin)
    approved = _consume_local_approval("desktop_execute_command")
    return tools.desktop_execute_command(
        command=command,
        working_directory=working_directory,
        timeout=timeout,
        approval_granted=approved,
        is_admin=is_admin,
    )


@mcp.tool()
@device_aware("desktop_open_application")
@_gated
def desktop_open_application(app_name: str, url: str | None = None, device: str | None = None) -> dict:
    """Launch an approved application.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_open_application")
    return tools.desktop_open_application(app_name=app_name, url=url, approval_granted=approved)


@mcp.tool()
@device_aware("desktop_get_windows")
@_gated
def desktop_get_windows(device: str | None = None) -> dict:
    """List visible application windows.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_get_windows")
    return tools.desktop_get_windows(approval_granted=approved)


@mcp.tool()
@device_aware("desktop_a11y_status")
@_gated
def desktop_a11y_status(device: str | None = None) -> dict:
    """Report Hybrid Desktop Accessibility Engine status (FFI/CLI/native backend, permissions, ref store).
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    return hybrid_desktop.describe_hybrid_a11y_status()


@mcp.tool()
@device_aware("desktop_a11y_permissions")
@_gated
def desktop_a11y_permissions(device: str | None = None) -> dict:
    """Check OS accessibility and screen-recording permissions with remediation guidance.
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    return hybrid_desktop.check_a11y_permissions()


@mcp.tool()
@device_aware("desktop_a11y_snapshot")
@_gated
def desktop_a11y_snapshot(
    app: str = "",
    window_title: str = "",
    window_id: str = "",
    root_ref: str = "",
    skeleton: bool = False,
    interactive_only: bool = False,
    compact: bool = True,
    max_depth: int = 10,
    max_elements: int = 80,
    monitor_index: int | None = None,
    session_id: str = "default",
    agent_id: str = "",
    device: str | None = None,
) -> dict:
    """Capture a structured accessibility tree snapshot with ephemeral @ref IDs (password fields redacted).
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    return hybrid_desktop.a11y_snapshot(
        app=app,
        window_title=window_title,
        window_id=window_id,
        root_ref=root_ref,
        skeleton=skeleton,
        interactive_only=interactive_only,
        compact=compact,
        max_depth=max_depth,
        max_elements=max_elements,
        monitor_index=monitor_index,
        session_id=session_id,
        agent_id=agent_id,
    )


@mcp.tool()
@device_aware("desktop_a11y_find")
@_gated
def desktop_a11y_find(
    query: str = "",
    role: str = "",
    app: str = "",
    window_title: str = "",
    max_results: int = 25,
    device: str | None = None,
) -> dict:
    """Find accessibility elements by query/role/app returning @ref IDs without dumping the entire tree.
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    return hybrid_desktop.a11y_find(
        query=query,
        role=role,
        app=app,
        window_title=window_title,
        max_results=max_results,
    )


@mcp.tool()
@device_aware("desktop_a11y_get_value")
@_gated
def desktop_a11y_get_value(ref: str, device: str | None = None) -> dict:
    """Read text/value/state of a snapshot @ref (password fields redacted; rejects stale refs with STALE_REF).
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    return hybrid_desktop.a11y_get_value(ref)


@mcp.tool()
@device_aware("desktop_a11y_list_surfaces")
@_gated
def desktop_a11y_list_surfaces(app: str = "", device: str | None = None) -> dict:
    """List active GUI surfaces (windows, dialogs, menus, context menus, popovers, sheets).
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    return hybrid_desktop.a11y_list_surfaces(app=app)


@mcp.tool()
@device_aware("desktop_a11y_list_apps")
@_gated
def desktop_a11y_list_apps(device: str | None = None) -> dict:
    """List running GUI applications exposing accessibility surfaces.
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    return hybrid_desktop.a11y_list_apps()


@mcp.tool()
@device_aware("desktop_a11y_act")
@_gated
def desktop_a11y_act(
    ref: str,
    action: str = "click",
    text: str = "",
    option: str = "",
    headed: bool = False,
    wait_for: str = "",
    wait_for_gone: str = "",
    wait_timeout_seconds: float = 10.0,
    session_id: str = "default",
    agent_id: str = "",
    fallback_to_visual: bool = True,
    fallback_text: str = "",
    device: str | None = None,
) -> dict:
    """Execute a governed accessibility action on a snapshot @ref with optional visual/OCR fallback.
    Actions: click, double_click, right_click, hover, type, fill, set_value, clear, check, uncheck,
    toggle, select, expand, collapse, scroll_to, focus, show_menu.
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    approved = _consume_local_approval("desktop_a11y_act")
    return hybrid_desktop.a11y_act(
        ref,
        action=action,
        text=text,
        option=option,
        headed=headed,
        wait_for=wait_for,
        wait_for_gone=wait_for_gone,
        wait_timeout_seconds=wait_timeout_seconds,
        session_id=session_id,
        agent_id=agent_id,
        fallback_to_visual=fallback_to_visual,
        fallback_text=fallback_text,
        approval_granted=approved,
    )


@mcp.tool()
@device_aware("desktop_a11y_batch")
@_gated
def desktop_a11y_batch(
    steps: list[dict],
    stop_on_error: bool = True,
    device: str | None = None,
) -> dict:
    """Execute up to 25 accessibility actions in one atomic policy-checked round-trip.
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    approved = _consume_local_approval("desktop_a11y_batch")
    return hybrid_desktop.a11y_batch(
        steps,
        stop_on_error=stop_on_error,
        approval_granted=approved,
    )


@mcp.tool()
@device_aware("desktop_a11y_wait")
@_gated
def desktop_a11y_wait(
    condition: str = "exists",
    query: str = "",
    role: str = "",
    app: str = "",
    window_title: str = "",
    timeout_seconds: float = 10.0,
    device: str | None = None,
) -> dict:
    """Wait until an accessibility element or window condition (exists, gone, enabled, window_open, window_closed) is met.
    device (optional): Target device name/ID, or 'local'."""
    from . import hybrid_desktop

    return hybrid_desktop.a11y_wait(
        condition=condition,
        query=query,
        role=role,
        app=app,
        window_title=window_title,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
@device_aware("desktop_wait")
@_gated
def desktop_wait(
    condition: str = "sleep",
    text: str = "",
    window_title: str = "",
    seconds: float = 1.0,
    timeout_seconds: float = 10.0,
    device: str | None = None,
) -> dict:
    """Wait for a condition ('sleep', 'text', or 'window') or action to complete.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_wait")
    return tools.desktop_wait(
        condition=condition,
        text=text,
        window_title=window_title,
        seconds=seconds,
        timeout_seconds=timeout_seconds,
        approval_granted=approved,
    )


@mcp.tool()
@device_aware("desktop_verify_action")
@_gated
def desktop_verify_action(
    expected_text: str = "",
    expected_window: str = "",
    device: str | None = None,
) -> dict:
    """Verify whether an action succeeded.
    device (optional): Target Windows device name/ID, or 'local'."""
    approved = _consume_local_approval("desktop_verify_action")
    return tools.desktop_verify_action(
        expected_text=expected_text,
        expected_window=expected_window,
        approval_granted=approved,
    )


# ---------------------------------------------------------------------------
# Web Browser Automation Tools (Playwright)
# ---------------------------------------------------------------------------

@mcp.tool()
@_gated
def browser_navigate(url: str, wait_until: str = "networkidle", timeout: int = 30) -> dict:
    """Navigate the browser to a URL. Returns current page URL and title.
    url: Target URL (must be http:// or https://).
    wait_until: When to consider navigation complete ('load', 'domcontentloaded', 'networkidle').
    timeout: Maximum seconds to wait for navigation."""
    approved = _consume_local_approval("browser_navigate")
    return browser_tools.browser_navigate(url, wait_until=wait_until, timeout=timeout, approval_granted=approved)


@mcp.tool()
@_gated
def browser_screenshot(full_page: bool = False) -> dict:
    """Capture a screenshot of the current browser page.
    full_page: When True, captures the entire scrollable page instead of just the viewport."""
    approved = _consume_local_approval("browser_screenshot")
    return browser_tools.browser_screenshot(full_page=full_page, approval_granted=approved)


@mcp.tool()
@_gated
def browser_click(selector: str, button: str = "left", clicks: int = 1) -> dict:
    """Click an element on the current page matching a CSS selector.
    selector: CSS selector (e.g. 'button#submit', '.nav-link').
    button: 'left', 'right', or 'middle'.
    clicks: Number of clicks (1 for single, 2 for double)."""
    approved = _consume_local_approval("browser_click")
    return browser_tools.browser_click(selector, button=button, clicks=clicks, approval_granted=approved)


@mcp.tool()
@_gated
def browser_type(selector: str, text: str, clear_first: bool = True, submit: bool = False) -> dict:
    """Type text into an input element matching a CSS selector.
    selector: CSS selector for the input/textarea element.
    text: Text to type.
    clear_first: Clear existing content before typing.
    submit: Press Enter after typing."""
    approved = _consume_local_approval("browser_type")
    return browser_tools.browser_type(selector, text, clear_first=clear_first, submit=submit, approval_granted=approved)


@mcp.tool()
@_gated
def browser_get_text(selector: str = "body") -> dict:
    """Extract visible text content from elements matching a CSS selector.
    selector: CSS selector (default 'body' for full page text)."""
    approved = _consume_local_approval("browser_get_text")
    return browser_tools.browser_get_text(selector, approval_granted=approved)


@mcp.tool()
@_gated
def browser_evaluate(script: str) -> dict:
    """Execute JavaScript in the current page context and return the result.
    script: JavaScript code to execute. Use 'return' to yield a value."""
    approved = _consume_local_approval("browser_evaluate")
    return browser_tools.browser_evaluate(script, approval_granted=approved)


@mcp.tool()
@_gated
def browser_get_url() -> dict:
    """Return the current page URL and title."""
    approved = _consume_local_approval("browser_get_url")
    return browser_tools.browser_get_url(approval_granted=approved)


@mcp.tool()
@_gated
def browser_go_back() -> dict:
    """Navigate back in browser history."""
    approved = _consume_local_approval("browser_go_back")
    return browser_tools.browser_go_back(approval_granted=approved)


@mcp.tool()
@_gated
def browser_go_forward() -> dict:
    """Navigate forward in browser history."""
    approved = _consume_local_approval("browser_go_forward")
    return browser_tools.browser_go_forward(approval_granted=approved)


@mcp.tool()
@_gated
def browser_scroll(delta_y: int = 0, delta_x: int = 0) -> dict:
    """Scroll the current page by delta pixels.
    delta_y: Vertical scroll amount (positive = down, negative = up).
    delta_x: Horizontal scroll amount (positive = right, negative = left)."""
    approved = _consume_local_approval("browser_scroll")
    return browser_tools.browser_scroll(delta_y=delta_y, delta_x=delta_x, approval_granted=approved)


@mcp.tool()
@_gated
def browser_close() -> dict:
    """Close the browser and release all resources."""
    approved = _consume_local_approval("browser_close")
    return browser_tools.browser_close(approval_granted=approved)


@mcp.tool()
@_gated
def browser_new_page(url: str | None = None) -> dict:
    """Open a new browser tab. Optionally navigate to a URL.
    url: Optional URL to navigate the new tab to."""
    approved = _consume_local_approval("browser_new_page")
    return browser_tools.browser_new_page(url=url, approval_granted=approved)


@mcp.tool()
@_gated
def browser_get_elements(selector: str, include_text: bool = True) -> dict:
    """List elements matching a CSS selector with tag names, counts, and optional text snippets.
    selector: CSS selector to query.
    include_text: Include visible text for each matched element."""
    approved = _consume_local_approval("browser_get_elements")
    return browser_tools.browser_get_elements(selector, include_text=include_text, approval_granted=approved)


@mcp.tool()
@_gated
def browser_wait_for_selector(selector: str, timeout: int = 10, state: str = "visible") -> dict:
    """Wait until an element matching the CSS selector appears (or disappears) on the page.
    selector: CSS selector to wait for.
    timeout: Maximum seconds to wait.
    state: 'visible', 'hidden', 'attached', or 'detached'."""
    approved = _consume_local_approval("browser_wait_for_selector")
    return browser_tools.browser_wait_for_selector(selector, timeout=timeout, state=state, approval_granted=approved)


@mcp.tool()
@_gated
def browser_select_option(selector: str, value: str | None = None, label: str | None = None) -> dict:
    """Select an option in a <select> dropdown by value or visible label.
    selector: CSS selector for the <select> element.
    value: Option value attribute to select.
    label: Visible text of the option to select."""
    approved = _consume_local_approval("browser_select_option")
    return browser_tools.browser_select_option(selector, value=value, label=label, approval_granted=approved)


@mcp.tool()
@_gated
def browser_download_file(url: str, filename: str | None = None) -> dict:
    """Download a file via the browser context to the workspace data directory.
    url: Direct URL of the file to download.
    filename: Optional filename override (defaults to URL basename)."""
    approved = _consume_local_approval("browser_download_file")
    return browser_tools.browser_download_file(url, filename=filename, approval_granted=approved)


@mcp.tool()
@_gated
def browser_set_viewport(
    device: str = "desktop",
    width: int | None = None,
    height: int | None = None,
    is_mobile: bool | None = None,
) -> dict:
    """Set desktop/mobile device preset or custom viewport for the browser session."""
    approved = _consume_local_approval("browser_set_viewport")
    return browser_tools.browser_set_viewport(
        device, width=width, height=height, is_mobile=is_mobile, approval_granted=approved
    )


@mcp.tool()
@_gated
def browser_expect(
    text: str | None = None,
    url_contains: str | None = None,
    title_contains: str | None = None,
    selector: str | None = None,
    screenshot_on_fail: bool = False,
) -> dict:
    """Assert text/URL/title/selector on the current page (website test helper)."""
    approved = _consume_local_approval("browser_expect")
    return browser_tools.browser_expect(
        text=text,
        url_contains=url_contains,
        title_contains=title_contains,
        selector=selector,
        screenshot_on_fail=screenshot_on_fail,
        approval_granted=approved,
    )


@mcp.tool()
@_gated
def browser_wait_for_url(url_contains: str, timeout: int = 15) -> dict:
    """Wait until the page URL contains the given substring."""
    approved = _consume_local_approval("browser_wait_for_url")
    return browser_tools.browser_wait_for_url(url_contains, timeout=timeout, approval_granted=approved)


@mcp.tool()
@_gated
def browser_diagnostics() -> dict:
    """Return recent console errors and failed network requests."""
    approved = _consume_local_approval("browser_diagnostics")
    return browser_tools.browser_diagnostics(approval_granted=approved)


@mcp.tool()
@_gated
def browser_compare_screenshot(
    baseline: str,
    threshold: float = 0.08,
    full_page: bool = False,
    update_baseline: bool = False,
) -> dict:
    """Compare the current page screenshot to a workspace visual baseline."""
    approved = _consume_local_approval("browser_compare_screenshot")
    return browser_tools.browser_compare_screenshot(
        baseline,
        threshold=threshold,
        full_page=full_page,
        update_baseline=update_baseline,
        approval_granted=approved,
    )


@mcp.tool()
@_gated
def browser_navigate_with_shot(url: str, wait_until: str = "networkidle", timeout: int = 30) -> dict:
    """Navigate and capture a viewport screenshot after load."""
    approved = _consume_local_approval("browser_navigate")
    return browser_tools.browser_navigate(
        url,
        wait_until=wait_until,
        timeout=timeout,
        screenshot_after_nav=True,
        approval_granted=approved,
    )


@mcp.tool()
@_gated
def firecrawl_scrape(url: str) -> dict:
    """Scrape a page to markdown via Firecrawl (requires FIRECRAWL API key)."""
    from .connectors import firecrawl

    return firecrawl.firecrawl_scrape({"url": url})


@mcp.tool()
@_gated
def firecrawl_search(query: str, limit: int = 5) -> dict:
    """Web search via Firecrawl."""
    from .connectors import firecrawl

    return firecrawl.firecrawl_search({"query": query, "limit": limit})


@mcp.tool()
@_gated
def firecrawl_map_site(url: str, search: str | None = None) -> dict:
    """Discover URLs on a site via Firecrawl map."""
    from .connectors import firecrawl

    args: dict = {"url": url}
    if search:
        args["search"] = search
    return firecrawl.firecrawl_map_site(args)


@mcp.tool()
@_gated
def firecrawl_crawl(url: str, limit: int = 10) -> dict:
    """Start a multi-page Firecrawl crawl job."""
    from .connectors import firecrawl

    return firecrawl.firecrawl_crawl({"url": url, "limit": limit})


@mcp.tool()
@_gated
def you_web_search(query: str, count: int = 5) -> dict:
    """Live web search via You.com."""
    from .connectors import you_search

    return you_search.you_web_search({"query": query, "count": count})


@mcp.tool()
@_gated
def you_fetch_contents(urls: list[str] | str) -> dict:
    """Fetch page contents via You.com for one or more URLs."""
    from .connectors import you_search

    return you_search.you_fetch_contents({"urls": urls})


class _BearerAuthMiddleware:
    """Enforce bearer token authentication and normalize Accept & path on Streamable HTTP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            # 1. Path normalization: route / and /mcp/ to /mcp so discovery probes to root succeed
            path = scope.get("path", "")
            if path in ("/", "", "/mcp/"):
                scope["path"] = "/mcp"

            # 2. Accept header normalization: FastMCP strictly requires application/json.
            # Clients like ChatGPT, curl, and requests send Accept: */* or omit it.
            # Normalizing ensures content negotiation never returns 406.
            raw_headers = list(scope.get("headers", []))
            normalized_headers = []
            has_accept = False
            for k, v in raw_headers:
                if k.lower() == b"accept":
                    has_accept = True
                    val_str = v.decode("latin1", errors="replace")
                    parts = [p.strip() for p in val_str.split(",") if p.strip()] if val_str else []
                    if "application/json" not in val_str:
                        parts.append("application/json")
                    if "text/event-stream" not in val_str:
                        parts.append("text/event-stream")
                    new_val = ", ".join(parts) if parts else "application/json, text/event-stream"
                    normalized_headers.append((k, new_val.encode("latin1")))
                else:
                    normalized_headers.append((k, v))
            if not has_accept:
                normalized_headers.append((b"accept", b"application/json, text/event-stream"))
            scope["headers"] = normalized_headers

        # R-P0-01 (Wave 1C): HTTP bearer auth is ALWAYS on. The former
        # former anonymous-HTTP env-var bypass is deleted; the
        # only escape hatch is the explicit --unsafe-allow-anon CLI
        # flag (module global _UNSAFE_ALLOW_ANON), which refuses to run
        # under AI_BRIDGE_PROFILE=production.
        if scope.get("type") != "http" or _UNSAFE_ALLOW_ANON:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
        authorized = False
        admin_tok = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "")
        if tok:
            if admin_tok and hmac.compare_digest(tok, admin_tok):
                authorized = True
            else:
                try:
                    resolve_binding(VERIFY_URL, tok)
                    authorized = True
                except BindingError:
                    authorized = False
        if not authorized:
            body = b'{"detail":"Missing or invalid bearer token"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def create_http_app():
    return _BearerAuthMiddleware(mcp.streamable_http_app())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ai-bridge")
    parser.add_argument("--http", action="store_true", help="Serve Streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_BRIDGE_MCP_PORT", "8001")))
    parser.add_argument("--host", default=os.environ.get("AI_BRIDGE_MCP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--unsafe-allow-anon", action="store_true",
        help="DANGER: allow unauthenticated HTTP MCP access (local dev only). "
             "Refuses to run when AI_BRIDGE_PROFILE=production. Prints a "
             "banner on every start. Never use outside local development.",
    )
    args = parser.parse_args(argv)
    if args.unsafe_allow_anon:
        if os.environ.get("AI_BRIDGE_PROFILE", "").strip().lower() == "production":
            raise SystemExit("--unsafe-allow-anon is not allowed when AI_BRIDGE_PROFILE=production.")
        global _UNSAFE_ALLOW_ANON
        _UNSAFE_ALLOW_ANON = True
        print("WARNING: --unsafe-allow-anon is set: HTTP MCP authentication is DISABLED. "
              "Local development only — never use in production.", file=sys.stderr)
    if args.http or os.environ.get("AI_BRIDGE_HTTP") == "1":
        import uvicorn
        app = create_http_app()
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        mcp.run(transport="stdio")

if __name__ == "__main__":
    main()

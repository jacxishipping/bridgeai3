"""Regression tests for Wave 1C P0 fixes: the MCP wrapper gate
(R-P1-05, R-P0-05, R-P0-06), the removed HTTP anon off-switch (R-P0-01),
fail-closed device binding (F-04-02), per-invocation stdio session
keys (F-06-02), approval pass-through (F-06-06) and argument
validation (F-06-10).

Audit output is directed at a tmp JSONL file via AI_BRIDGE_MCP_AUDIT_FILE
so tests never touch ~/.ai-bridge.
"""
from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_bridge import server as mcp_server

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_SRC = REPO_ROOT / "src" / "ai_bridge" / "server.py"


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_BRIDGE_MCP_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AI_BRIDGE_MCP_AUDIT_DB", raising=False)
    monkeypatch.delenv("AI_BRIDGE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("AI_BRIDGE_ALLOW_TIER3", raising=False)
    monkeypatch.delenv("AI_BRIDGE_DESKTOP_ENABLED", raising=False)
    monkeypatch.delenv("AI_BRIDGE_HTTP_ALLOW_ANON", raising=False)
    yield
    mcp_server._APPROVALS.clear()
    mcp_server._SELECTED_DEVICE.clear()


def _audit_rows(tmp_path: Path) -> list[dict]:
    log = tmp_path / "audit.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _grant_approval(tool_name: str, monkeypatch: pytest.MonkeyPatch):
    """Insert an approved, same-session approval record for tool_name."""
    rec_id = "test-approval-1"
    import time as _time
    mcp_server._APPROVALS[rec_id] = {
        "tool": tool_name,
        "reason": "test",
        "status": "approved",
        "session": mcp_server._session_key(),
        "created_at": _time.time(),
    }


# --- R-P0-05 / R-P1-05: tier + gate coverage -----------------------------

def test_every_mcp_tool_has_a_tier_entry():
    from ai_bridge.policy import TOOL_TIERS, normalize_tool_name

    tree = ast.parse(SERVER_SRC.read_text(encoding="utf-8"))
    tools = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                f = d.func if isinstance(d, ast.Call) else d
                if isinstance(f, ast.Attribute) and f.attr == "tool":
                    tools.append(node.name)
    assert len(tools) == 109
    missing = [t for t in tools
               if t not in TOOL_TIERS and normalize_tool_name(t) not in TOOL_TIERS]
    assert missing == []


def test_every_mcp_tool_wrapper_is_gated():
    tree = ast.parse(SERVER_SRC.read_text(encoding="utf-8"))
    ungated = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decs = []
            for d in node.decorator_list:
                f = d.func if isinstance(d, ast.Call) else d
                if isinstance(f, ast.Attribute):
                    decs.append(f.attr)
                elif isinstance(f, ast.Name):
                    decs.append(f.id)
            if "tool" in decs and "_gated" not in decs:
                ungated.append(node.name)
    assert ungated == []


# --- R-P0-01: no anonymous HTTP MCP -------------------------------------

def _run_middleware(scope: dict) -> int | None:
    """Drive _BearerAuthMiddleware with a fake ASGI app; return the
    HTTP status it sent (None if the request passed through)."""
    app = mcp_server.create_http_app()
    statuses: list[int] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def _run():
        try:
            await asyncio.wait_for(app(scope, receive, send), timeout=10)
        except Exception:
            # The inner FastMCP app may fail on the fake request; what
            # matters here is whether the middleware let it through
            # (no 401) or rejected it (401). A pass-through that then
            # errors inside FastMCP still proves auth did not reject it.
            if not statuses:
                raise

    asyncio.run(_run())
    return statuses[0] if statuses else None


def test_anon_http_mcp_returns_401_even_with_env_var_set(
    monkeypatch: pytest.MonkeyPatch,
):
    # The deleted off-switch must not resurrect auth bypass.
    monkeypatch.setenv("AI_BRIDGE_HTTP_ALLOW_ANON", "1")
    monkeypatch.delenv("AI_BRIDGE_ADMIN_TOKEN", raising=False)
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/mcp", "query_string": b"",
        "headers": [(b"accept", b"application/json")],
    }
    assert _run_middleware(scope) == 401


def test_http_mcp_with_valid_admin_token_passes_middleware(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AI_BRIDGE_ADMIN_TOKEN", "test-admin-token")
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/mcp", "query_string": b"",
        "headers": [(b"accept", b"application/json"),
                    (b"authorization", b"Bearer test-admin-token")],
    }
    assert _run_middleware(scope) != 401


def test_unsafe_allow_anon_flag_refused_in_production(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AI_BRIDGE_PROFILE", "production")
    with pytest.raises(SystemExit):
        mcp_server.main(["--http", "--unsafe-allow-anon"])


def test_unsafe_allow_anon_flag_sets_global_and_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
):
    import types

    monkeypatch.delenv("AI_BRIDGE_PROFILE", raising=False)
    seen_flag: list[bool] = []

    def _fake_run(*args, **kwargs):
        seen_flag.append(mcp_server._UNSAFE_ALLOW_ANON)

    fake_uvicorn = types.SimpleNamespace(run=_fake_run)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(mcp_server, "_UNSAFE_ALLOW_ANON", False)
    try:
        mcp_server.main(["--http", "--unsafe-allow-anon"])
    finally:
        monkeypatch.setattr(mcp_server, "_UNSAFE_ALLOW_ANON", False)
    assert seen_flag == [True], "uvicorn.run must see the flag enabled"
    assert "unsafe-allow-anon" in capsys.readouterr().err


# --- F-04-02: fail-closed device binding --------------------------------

def test_oauth_bound_device_fails_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "_bearer_token", lambda: "non-admin-token")
    monkeypatch.setenv("AI_BRIDGE_ADMIN_TOKEN", "admin-token")

    def _boom(url, tok):
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(mcp_server, "resolve_binding", _boom)
    with pytest.raises(PermissionError):
        mcp_server._oauth_bound_device()


def test_oauth_bound_device_admin_token_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(mcp_server, "_bearer_token", lambda: "admin-token")
    monkeypatch.setenv("AI_BRIDGE_ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(
        mcp_server, "resolve_binding",
        lambda url, tok: (_ for _ in ()).throw(RuntimeError("must not be called")),
    )
    assert mcp_server._oauth_bound_device() is None


# --- F-06-02: per-invocation stdio session keys --------------------------

def test_stdio_session_keys_are_distinct_across_invocations():
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from ai_bridge import server;"
        "print(server._session_key())"
    )
    keys = set()
    for _ in range(2):
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        keys.add(out.stdout.strip())
    assert len(keys) == 2
    assert "default" not in keys


def test_stdio_session_key_stable_within_process():
    assert mcp_server._session_key() == mcp_server._session_key()
    assert mcp_server._session_key() != "default"


# --- R-P1-05: gate denies, audits, and lets Tier 1 through -------------

def test_gate_denies_tier3_without_opt_in_and_audits(tmp_path: Path):
    with pytest.raises(PermissionError):
        mcp_server.mouse_click(x=10, y=10)
    rows = _audit_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["tool"] == "mouse_click"
    assert rows[0]["decision"] == "denied"


def test_gate_allows_tier1_and_audits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server.tools, "get_system_info",
                        lambda: {"system": "Linux", "machine": "test"})
    result = mcp_server.system_info()
    assert result["system"] == "Linux"
    rows = _audit_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["tool"] == "system_info"
    assert rows[0]["decision"] == "allowed"


def test_gate_denies_unknown_tool_name():
    # decide() itself denies unlisted tools; the gate must surface it.
    assert not mcp_server._gate_tool("run_any_command").allowed


# --- F-06-06: approval pass-through --------------------------------------

def test_write_file_passes_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AI_BRIDGE_ALLOW_TIER3", "1")
    monkeypatch.setenv("AI_BRIDGE_DISABLE_HOST_TOOLS", "0")
    _grant_approval("write_file", monkeypatch)
    seen: dict = {}

    def _fake_write(path, content, overwrite=False, **kwargs):
        seen.update(kwargs)
        return {"written": path}

    monkeypatch.setattr(mcp_server.tools, "write_file", _fake_write)
    out = mcp_server.write_file("x.txt", "hello")
    assert out == {"written": "x.txt"}
    assert seen.get("approval_granted") is True


def test_capture_screenshot_passes_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AI_BRIDGE_DESKTOP_ENABLED", "1")
    _grant_approval("capture_desktop_screenshot", monkeypatch)
    seen: dict = {}

    def _fake_cap(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_server.tools, "capture_desktop_screenshot", _fake_cap)
    assert mcp_server.capture_desktop_screenshot() == {"ok": True}
    assert seen.get("approval_granted") is True


def test_take_screenshot_passes_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AI_BRIDGE_DESKTOP_ENABLED", "1")
    _grant_approval("take_screenshot", monkeypatch)
    seen: dict = {}

    def _fake_cap(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_server.tools, "capture_desktop_screenshot", _fake_cap)
    assert mcp_server.take_screenshot() == {"ok": True}
    assert seen.get("approval_granted") is True


def test_tier4_delete_requires_approval_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # Without approval the gate denies (and audits the denial).
    with pytest.raises(PermissionError):
        mcp_server.delete_workspace_file("gone.txt")
    rows = _audit_rows(tmp_path)
    assert rows and rows[-1]["tool"] == "delete_workspace_file"
    assert rows[-1]["decision"] == "denied"

    # With a same-session approval the gate allows and the wrapper
    # consumes it (single-use).
    _grant_approval("delete_workspace_file", monkeypatch)
    monkeypatch.setattr(
        mcp_server.workspace, "delete_workspace_file",
        lambda p: {"deleted": p},
    )
    assert mcp_server.delete_workspace_file("gone.txt") == {"deleted": "gone.txt"}
    assert not mcp_server._peek_local_approval("delete_workspace_file")


# --- F-06-10: argument validation -----------------------------------------

def test_validation_rejects_bad_coordinates_and_does_not_execute(
    monkeypatch: pytest.MonkeyPatch,
):
    called: list = []
    monkeypatch.setattr(
        mcp_server.tools, "mouse_click",
        lambda *a, **k: called.append((a, k)) or {"ok": True},
    )
    with pytest.raises(ValueError):
        mcp_server.mouse_click(x=-5, y=10)
    assert called == []


def test_validation_rejects_bad_button():
    with pytest.raises(ValueError):
        mcp_server.mouse_click(x=10, y=10, button="sideways")


def test_validation_rejects_bad_screenshot_scale():
    with pytest.raises(ValueError):
        mcp_server.capture_desktop_frame(scale=5.0, quality=70)


# --- F-04-01 (partial): approvals are session-scoped ----------------------

def test_approval_from_another_session_is_not_honoured(
    monkeypatch: pytest.MonkeyPatch,
):
    import time as _time
    mcp_server._APPROVALS["other-session"] = {
        "tool": "edit_workspace_file",
        "reason": "test",
        "status": "approved",
        "session": "someone-else",
        "created_at": _time.time(),
    }
    assert not mcp_server._peek_local_approval("edit_workspace_file")
    assert not mcp_server._consume_local_approval("edit_workspace_file")

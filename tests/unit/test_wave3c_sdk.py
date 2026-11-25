"""Wave 3C Track C tests: MCP client, SDK, shell/terminal, browser isolation.

Covers F-06-15 (initialize/session/pagination), F-04-07/11/16 (shell
contract, SIGKILL escalation, bounded sessions), F-04-14 (fail-closed
browser sandbox), F-04-13 (per-session browser contexts + evaluate
allowlist), F-04-19/FR-07 (token hygiene, retries, idempotency),
FR-01/03/05 (policy simulation, task projection, demo flag), F-04-20
(signed-update gate), and F-04-17 (OCR script hardening).
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from ai_bridge import browser_isolation_os, browser_tools, terminal_manager, vision
from ai_bridge.client import (
    DEFAULT_CONTROL_URL,
    AIBridgeClient,
    AIBridgeError,
    project_task_summaries,
)
from ai_bridge.control import mcp_client as mcp_mod
from ai_bridge.policy import Policy
from ai_bridge.policy_simulation import (
    is_demo_mode,
    require_signed_update,
    simulate_batch,
    simulate_policy_decision,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self._headers = headers or {}

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def info(self):
        return dict(self._headers)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mcp_fake_factory(monkeypatch, mode="modern"):
    """Fake MCP server. mode=modern negotiates 2025-06-18 + session id
    and paginates tools/list; mode=legacy has no initialize support."""
    calls = []

    def fake_urlopen(req, timeout=12.0):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        calls.append(body.get("method"))
        method = body.get("method")
        params = body.get("params") or {}
        if method == "initialize":
            if mode == "legacy":
                return _FakeResp({"jsonrpc": "2.0", "id": 1, "result": {"content": []}})
            return _FakeResp(
                {"jsonrpc": "2.0", "id": 1, "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1"},
                }},
                headers={"Mcp-Session-Id": "sess-123"},
            )
        if method == "notifications/initialized":
            return _FakeResp({"jsonrpc": "2.0", "id": 1, "result": {}})
        if method == "tools/list":
            if params.get("cursor") == "c1":
                return _FakeResp({"jsonrpc": "2.0", "id": 1,
                                  "result": {"tools": [{"name": "b", "description": "B"}]}})
            if mode == "modern":
                return _FakeResp({"jsonrpc": "2.0", "id": 1,
                                  "result": {"tools": [{"name": "a", "description": "A"}],
                                             "nextCursor": "c1"}})
            return _FakeResp({"jsonrpc": "2.0", "id": 1,
                              "result": {"tools": [{"name": "ping", "description": "Ping"}]}})
        if method == "tools/call":
            return _FakeResp({"jsonrpc": "2.0", "id": 1,
                              "result": {"content": [{"type": "text", "text": "pong"}]}})
        raise AssertionError(f"unexpected MCP method: {method}")

    monkeypatch.setattr(mcp_mod.urllib.request, "urlopen", fake_urlopen)
    return calls


# ---------------------------------------------------------------------------
# F-06-15 — MCP client: initialize, session, pagination
# ---------------------------------------------------------------------------

def test_mcp_initialize_negotiates_version_and_session(monkeypatch):
    calls = _mcp_fake_factory(monkeypatch, mode="modern")
    client = mcp_mod.MCPClient("https://ext.example.com/mcp")
    info = client.initialize()
    assert info["protocolVersion"] == "2025-06-18"
    assert info["serverInfo"]["name"] == "fake"
    assert client.session_id == "sess-123"
    assert client.initialized is True
    assert "notifications/initialized" in calls


def test_mcp_list_all_tools_paginates(monkeypatch):
    _mcp_fake_factory(monkeypatch, mode="modern")
    client = mcp_mod.MCPClient("https://ext.example.com/mcp")
    client.ensure_initialized()
    tools = client.list_all_tools()
    assert [t["name"] for t in tools] == ["a", "b"]


def test_mcp_list_tools_single_page_cursor(monkeypatch):
    _mcp_fake_factory(monkeypatch, mode="modern")
    client = mcp_mod.MCPClient("https://ext.example.com/mcp")
    tools, cursor = client.list_tools()
    assert [t["name"] for t in tools] == ["a"]
    assert cursor == "c1"
    tools2, cursor2 = client.list_tools(cursor=cursor)
    assert [t["name"] for t in tools2] == ["b"]
    assert cursor2 is None


def test_mcp_legacy_server_without_initialize_still_works(monkeypatch):
    _mcp_fake_factory(monkeypatch, mode="legacy")
    ok, tools, msg = mcp_mod.fetch_mcp_tools("https://ext.example.com/mcp")
    assert ok is True
    assert [t["name"] for t in tools] == ["ping"]
    assert "protocol" not in msg  # legacy mode, no version claimed


def test_mcp_execute_tool_uses_session(monkeypatch):
    calls = _mcp_fake_factory(monkeypatch, mode="modern")
    res = mcp_mod.execute_mcp_tool("https://ext.example.com/mcp", "ping", {})
    assert res["ok"] is True
    assert res["result"] == "pong"
    assert calls[0] == "initialize"


def test_mcp_unsupported_protocol_version_rejected(monkeypatch):
    def fake_urlopen(req, timeout=12.0):
        return _FakeResp({"jsonrpc": "2.0", "id": 1,
                          "result": {"protocolVersion": "1999-01-01"}})

    monkeypatch.setattr(mcp_mod.urllib.request, "urlopen", fake_urlopen)
    client = mcp_mod.MCPClient("https://ext.example.com/mcp")
    with pytest.raises(mcp_mod.MCPClientError) as excinfo:
        client.initialize()
    assert excinfo.value.code == mcp_mod.MCPErrorCode.PROTOCOL


def test_mcp_error_taxonomy_codes():
    err = mcp_mod.MCPClientError("x", mcp_mod.MCPErrorCode.AUTH, status_code=401)
    assert err.code == mcp_mod.MCPErrorCode.AUTH
    assert err.status_code == 401


# ---------------------------------------------------------------------------
# FR-01 / FR-05 / F-04-20 — policy simulation, demo flag, signed-update gate
# ---------------------------------------------------------------------------

def test_simulate_tier4_needs_approval():
    sim = simulate_policy_decision("delete_workspace_file", Policy())
    assert sim["allowed"] is False
    assert sim["needs_approval"] is True
    assert sim["tier"] == 4
    assert sim["executed"] is False


def test_simulate_tier1_allowed_and_tier4_approved_by_admin():
    assert simulate_policy_decision("list_devices", Policy())["allowed"] is True
    sim = simulate_policy_decision(
        "delete_workspace_file", Policy(), approval_granted=True, is_admin=True)
    assert sim["allowed"] is True
    assert sim["rule"] == "tier4_approved"


def test_simulate_denied_tool_and_unknown_tool():
    denied = simulate_policy_decision(
        "run_shell", Policy(denied_tools=frozenset({"run_shell"})))
    assert denied["allowed"] is False
    unknown = simulate_policy_decision("no_such_tool_xyz", Policy())
    assert unknown["allowed"] is False
    assert unknown["tier"] is None


def test_simulate_batch_never_executes():
    results = simulate_batch([
        {"tool": "list_devices"},
        {"tool": "run_shell", "actor": "op-1", "device_id": "dev-1"},
        {"tool": ""},
    ])
    assert len(results) == 3
    assert results[0]["allowed"] is True
    assert results[1]["actor"] == "op-1"
    assert all(r["executed"] is False for r in results)


def test_demo_mode_flag(monkeypatch):
    monkeypatch.delenv("AI_BRIDGE_DEMO_MODE", raising=False)
    monkeypatch.delenv("AI_BRIDGE_DEMO", raising=False)
    assert is_demo_mode() == {"demo_mode": False, "source": "default", "explicit": False}
    monkeypatch.setenv("AI_BRIDGE_DEMO_MODE", "1")
    assert is_demo_mode()["demo_mode"] is True
    assert is_demo_mode()["source"] == "AI_BRIDGE_DEMO_MODE"


def test_require_signed_update_refuses_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("AI_BRIDGE_UPDATE_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("AI_BRIDGE_UPDATE_SIGNATURE_PATH", raising=False)
    monkeypatch.delenv("AI_BRIDGE_UPDATE_PUBLIC_KEY", raising=False)
    with pytest.raises(PermissionError):
        require_signed_update()


# ---------------------------------------------------------------------------
# FR-03 — task result projection
# ---------------------------------------------------------------------------

def test_project_task_summaries_strips_full_result():
    big = "x" * 5000
    out = project_task_summaries([
        {"id": "t1", "status": "completed", "device": "d1",
         "created_at": 1, "updated_at": 2, "result": big,
         "output": big, "stdout": big},
    ])
    assert len(out) == 1
    row = out[0]
    assert row["id"] == "t1"
    assert row["status"] == "completed"
    assert "result" not in row and "output" not in row and "stdout" not in row
    assert len(row["result_preview"]) <= 201 + len("… [truncated]")
    assert row["result_truncated"] is True
    assert big not in json.dumps(row)


# ---------------------------------------------------------------------------
# F-04-19 / FR-07 — SDK token hygiene, retries, idempotency
# ---------------------------------------------------------------------------

def test_sdk_repr_and_token_never_in_url(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _FakeResp({"ok": True, "devices": []})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sdk = AIBridgeClient(base_url="http://127.0.0.1:8000", token="tok-987654321")
    assert "tok-987654321" not in repr(sdk)
    sdk._request("GET", "/api/tasks", params={"token": "tok-987654321", "limit": 5})
    assert "tok-987654321" not in captured["url"]
    assert "token=" not in captured["url"]
    auth = {k.lower(): v for k, v in captured["headers"].items()}["authorization"]
    assert auth == "Bearer tok-987654321"


# P-12 (Wave 7) — one canonical default control URL.
# ---------------------------------------------------------------------------

def test_sdk_default_control_url_is_canonical_production_host(monkeypatch):
    """No env / no arg -> the production host, never silent localhost."""
    monkeypatch.delenv("AI_BRIDGE_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_CONTROL_URL", raising=False)
    assert DEFAULT_CONTROL_URL == "https://jxiptvplayer.app"
    sdk = AIBridgeClient(token="tok-987654321")
    assert sdk.base_url == "https://jxiptvplayer.app"


def test_sdk_localhost_is_explicit_dev_override_only(monkeypatch):
    """Localhost is reachable only as an explicit env/arg override."""
    monkeypatch.setenv("AI_BRIDGE_CONTROL_URL", "http://127.0.0.1:8000")
    sdk = AIBridgeClient(token="tok-987654321")
    assert sdk.base_url == "http://127.0.0.1:8000"
    monkeypatch.delenv("AI_BRIDGE_CONTROL_URL", raising=False)
    sdk = AIBridgeClient(base_url="http://127.0.0.1:8000", token="tok-987654321")
    assert sdk.base_url == "http://127.0.0.1:8000"


def test_sdk_error_redacts_token(monkeypatch):
    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(
            req.full_url, 500, "boom", {},
            io.BytesIO(b'{"detail": "failed for s3cr3t-token"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sdk = AIBridgeClient(base_url="http://127.0.0.1:8000", token="tok-987654321", max_retries=0)
    with pytest.raises(AIBridgeError) as excinfo:
        sdk._request("GET", "/api/tasks")
    assert "tok-987654321" not in str(excinfo.value)
    assert "tok-987654321" not in str(excinfo.value.details)
    assert excinfo.value.code == "server"


def test_sdk_retries_retryable_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=60):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.HTTPError(
                req.full_url, 503, "unavailable", {}, io.BytesIO(b"{}"))
        return _FakeResp({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("ai_bridge.client.time.sleep", lambda s: None)
    sdk = AIBridgeClient(base_url="http://127.0.0.1:8000", token="tok-987654321", max_retries=2)
    assert sdk._request("GET", "/api/tasks") == {"ok": True}
    assert attempts["n"] == 3


def test_sdk_does_not_retry_4xx_or_non_idempotent_post(monkeypatch):
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=60):
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 400, "bad", {}, io.BytesIO(b'{"detail": "bad"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sdk = AIBridgeClient(base_url="http://127.0.0.1:8000", token="tok-987654321", max_retries=2)
    with pytest.raises(AIBridgeError):
        sdk._request("GET", "/api/tasks")
    assert attempts["n"] == 1
    attempts["n"] = 0
    with pytest.raises(AIBridgeError):
        sdk._request("POST", "/api/ai/execute", payload={"tool": "x"})
    assert attempts["n"] == 1  # no idempotency key -> no retry of a write


def test_sdk_execute_sends_idempotency_key(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["headers"] = dict(req.headers)
        return _FakeResp({"ok": True, "result": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sdk = AIBridgeClient(base_url="http://127.0.0.1:8000", token="tok-987654321")
    sdk.execute("run_shell", device="d1", args={"command": "uptime"})
    lower_headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert lower_headers.get("idempotency-key")
    sdk.execute("run_shell", device="d1", args={"command": "uptime"},
                idempotency_key="fixed-key-1")
    lower_headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert lower_headers["idempotency-key"] == "fixed-key-1"


def test_sdk_token_file_loading(monkeypatch, tmp_path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("file-token-123\n")
    monkeypatch.setenv("AI_BRIDGE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("AI_BRIDGE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("AI_BRIDGE_TOKEN", raising=False)
    sdk = AIBridgeClient(base_url="http://127.0.0.1:8000")
    assert sdk.token == "file-token-123"
    monkeypatch.setenv("AI_BRIDGE_TOKEN_FILE", str(tmp_path / "missing.txt"))
    with pytest.raises(ValueError):
        AIBridgeClient(base_url="http://127.0.0.1:8000")


def test_sdk_error_code_taxonomy():
    assert AIBridgeError("x", status_code=401).code == "auth"
    assert AIBridgeError("x", status_code=404).code == "not_found"
    assert AIBridgeError("x", status_code=429).code == "rate_limited"
    assert AIBridgeError("x", status_code=500).code == "server"
    assert AIBridgeError("x").code == "transport"


# ---------------------------------------------------------------------------
# F-04-07 / F-04-11 — shell contract + SIGKILL escalation
# ---------------------------------------------------------------------------

def test_shell_contract_echo_and_clean_env(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    assert "AI_BRIDGE_ADMIN_TOKEN" not in terminal_manager.clean_shell_env(
        {"AI_BRIDGE_ADMIN_TOKEN": "leak"})
    env = terminal_manager.clean_shell_env()
    assert env.get("TERM") == "xterm-256color"
    res = terminal_manager.run_shell_command(
        terminal_manager.ShellSpec(argv=["echo", "hello"]))
    assert res["returncode"] == 0
    assert res["stdout"].strip() == "hello"
    assert res["timed_out"] is False
    with pytest.raises(ValueError):
        terminal_manager.run_shell_command(terminal_manager.ShellSpec(argv=[]))


def test_shell_contract_timeout_kills_sigterm_ignoring_process():
    # Child ignores SIGTERM: only the SIGKILL escalation can end it.
    code = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)")
    start = time.time()
    res = terminal_manager.run_shell_command(terminal_manager.ShellSpec(
        argv=[sys.executable, "-c", code], timeout=1.0, kill_grace_seconds=0.2))
    assert res["timed_out"] is True
    assert time.time() - start < 10


def test_terminate_process_group_sigkill():
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
        start_new_session=True)
    terminal_manager.terminate_process_group(proc, grace_seconds=0.2)
    assert proc.poll() is not None


# ---------------------------------------------------------------------------
# F-04-16 — bounded terminal sessions with TTL / eviction
# ---------------------------------------------------------------------------

def _remote_session(device_id):
    return terminal_manager.create_terminal_session(
        device_id=device_id,
        remote_exec=lambda *a: {"stdout": "", "stderr": "", "returncode": 0})


def test_terminal_sessions_bounded_and_evict_oldest(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_MAX_TERMINAL_SESSIONS", "2")
    monkeypatch.setenv("AI_BRIDGE_TERMINAL_SESSION_IDLE_TTL_SEC", "1800")
    s1, s2, s3 = _remote_session("d1"), _remote_session("d2"), _remote_session("d3")
    try:
        assert terminal_manager.get_terminal_session(s1.session_id) is None  # evicted
        assert terminal_manager.get_terminal_session(s2.session_id) is not None
        assert terminal_manager.get_terminal_session(s3.session_id) is not None
        assert len(terminal_manager.list_terminal_sessions()) == 2
    finally:
        for s in (s1, s2, s3):
            terminal_manager.close_terminal_session(s.session_id)


def test_terminal_sessions_idle_ttl_prune(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_TERMINAL_SESSION_IDLE_TTL_SEC", "60")
    s = _remote_session("d1")
    try:
        s.last_active = time.time() - 3600
        evicted = terminal_manager.prune_terminal_sessions()
        assert s.session_id in evicted
        assert terminal_manager.get_terminal_session(s.session_id) is None
        assert s.is_alive is False
    finally:
        terminal_manager.close_terminal_session(s.session_id)


# ---------------------------------------------------------------------------
# F-04-14 — browser isolation fails closed
# ---------------------------------------------------------------------------

def test_browser_isolation_reports_not_enforced_when_unrequested(monkeypatch):
    monkeypatch.delenv("AI_BRIDGE_BROWSER_NETNS", raising=False)
    monkeypatch.delenv("AI_BRIDGE_BROWSER_GVISOR", raising=False)
    monkeypatch.delenv("AI_BRIDGE_BROWSER_REQUIRE_SANDBOX", raising=False)
    info = browser_isolation_os.describe_os_browser_isolation()
    assert info["mode"] == "none"
    assert info["enforced"] is False
    assert info["seccomp"] == "none"  # no filter shipped; must not claim one


def test_browser_isolation_fails_closed_when_requested_but_missing(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_BROWSER_NETNS", "1")
    monkeypatch.delenv("AI_BRIDGE_ALLOW_BROWSER_NO_SANDBOX", raising=False)
    monkeypatch.setattr(browser_isolation_os.shutil, "which", lambda name: None)
    with pytest.raises(browser_isolation_os.BrowserIsolationError):
        browser_isolation_os.chromium_launch_wrapper_argv(["chromium", "--headless"])
    with pytest.raises(browser_isolation_os.BrowserIsolationError):
        browser_isolation_os.require_browser_sandbox()
    # Explicit opt-out restores pass-through.
    monkeypatch.setenv("AI_BRIDGE_ALLOW_BROWSER_NO_SANDBOX", "1")
    assert browser_isolation_os.chromium_launch_wrapper_argv(
        ["chromium", "--headless"]) == ["chromium", "--headless"]
    assert browser_isolation_os.require_browser_sandbox()["opted_out"] is True


def test_browser_isolation_require_sandbox_flag(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_BROWSER_REQUIRE_SANDBOX", "1")
    monkeypatch.delenv("AI_BRIDGE_BROWSER_NETNS", raising=False)
    monkeypatch.delenv("AI_BRIDGE_BROWSER_GVISOR", raising=False)
    monkeypatch.delenv("AI_BRIDGE_ALLOW_BROWSER_NO_SANDBOX", raising=False)
    with pytest.raises(browser_isolation_os.BrowserIsolationError):
        browser_isolation_os.require_browser_sandbox()


# ---------------------------------------------------------------------------
# F-04-13 — per-session browser contexts + evaluate allowlist
# ---------------------------------------------------------------------------

def test_evaluate_allowlist_matching(monkeypatch):
    monkeypatch.delenv("AI_BRIDGE_BROWSER_EVALUATE_ALLOWLIST", raising=False)
    assert browser_tools._evaluate_url_allowed("https://anything.example/") is True
    monkeypatch.setenv("AI_BRIDGE_BROWSER_EVALUATE_ALLOWLIST", "example.com")
    assert browser_tools._evaluate_url_allowed("https://example.com/x") is True
    assert browser_tools._evaluate_url_allowed("https://sub.example.com/x") is True
    assert browser_tools._evaluate_url_allowed("https://evil.com/") is False
    assert browser_tools._evaluate_url_allowed("https://example.com.evil.com/") is False


def test_browser_evaluate_rejects_disallowed_url(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_BROWSER_EVALUATE_ALLOWLIST", "example.com")
    monkeypatch.setattr(browser_tools, "_check", lambda *a, **k: None)

    class _FakePage:
        url = "https://evil.com/"
        def evaluate(self, script):
            raise AssertionError("must not evaluate on a disallowed URL")

    monkeypatch.setattr(browser_tools, "_ensure_browser", lambda **k: _FakePage())
    with pytest.raises(ValueError, match="not on the AI_BRIDGE_BROWSER_EVALUATE_ALLOWLIST"):
        browser_tools.browser_evaluate("1+1")


def test_browser_navigate_rejects_non_http_url(monkeypatch):
    monkeypatch.setattr(browser_tools, "_check", lambda *a, **k: None)
    called = {"n": 0}
    monkeypatch.setattr(browser_tools, "_ensure_browser",
                        lambda **k: (called.__setitem__("n", 1), None)[1])
    with pytest.raises(ValueError, match="http\\(s\\)"):
        browser_tools.browser_navigate("file:///etc/passwd")
    assert called["n"] == 0


def test_browser_sessions_do_not_share_state(monkeypatch):
    # Swap machinery only — no Playwright needed: distinct sentinels per key.
    saved = (browser_tools._ACTIVE_SESSION_KEY, dict(browser_tools._BROWSER_SESSIONS),
             browser_tools._page, browser_tools._device_profile)
    try:
        browser_tools.use_browser_session(session_id="aaa")
        browser_tools._page = object()
        page_a = browser_tools._page
        browser_tools.use_browser_session(session_id="bbb")
        assert browser_tools._page is None  # fresh session, nothing shared
        browser_tools._page = object()
        page_b = browser_tools._page
        assert page_b is not page_a
        browser_tools.use_browser_session(session_id="aaa")
        assert browser_tools._page is page_a
        browser_tools.use_browser_session(session_id="bbb")
        assert browser_tools._page is page_b
        keys = {s["session_key"] for s in browser_tools.list_browser_sessions()}
        assert "session:aaa" in keys and "session:bbb" in keys
    finally:
        browser_tools.close_all_browser_sessions()
        browser_tools._BROWSER_SESSIONS.clear()
        browser_tools._ACTIVE_SESSION_KEY = saved[0]
        browser_tools._page = saved[2]
        browser_tools._device_profile = saved[3]


def test_browser_close_all_sessions_is_safe():
    browser_tools.close_all_browser_sessions()
    assert browser_tools.list_browser_sessions() == []


# ---------------------------------------------------------------------------
# F-04-17 — OCR script hardening (verified absent in Wave 1; fixed here)
# ---------------------------------------------------------------------------

def test_ocr_script_path_is_per_invocation_random_and_0600(tmp_path):
    p1 = vision.get_ocr_script_path()
    p2 = vision.get_ocr_script_path()
    try:
        assert p1 != p2
        assert p1.read_text(encoding="utf-8") == vision._POWERSHELL_OCR_SCRIPT
        if os.name != "nt":
            mode = stat.S_IMODE(p1.stat().st_mode)
            assert mode == 0o600
        assert "ai_bridge_ocr.ps1" not in str(p1)  # no predictable shared path
    finally:
        p1.unlink(missing_ok=True)
        p2.unlink(missing_ok=True)


def test_ocr_no_bypass_in_invoke_command():
    import inspect

    src = inspect.getsource(vision.run_ocr)
    assert '"Bypass"' not in src
    assert '"RemoteSigned"' in src

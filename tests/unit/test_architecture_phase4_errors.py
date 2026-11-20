"""Wave 3B Track C (Phase 4): error taxonomy, secrets hygiene, MCP gate,
config validation, SSRF senders, and the workspace-routes rename.

Covers FR-06 / F-06-11 (errors.py), F-06-14 (MCP tool policy on the
execute endpoint + tool_registry tier wiring), F-06-16 (approve_task
session auth), F-04-09 (redaction), F-05-17 / SEC-11 (SSRF senders),
F-04-23 (cached/validated Settings), and F-04-12 (workspace shim).
"""
from __future__ import annotations

import base64
import inspect
import json
import re
import socket
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from ai_bridge import errors as errors_mod
from ai_bridge import server as mcp_server
from ai_bridge.config import get_settings, reset_for_tests
from ai_bridge.control import alerts, worm_siem
from ai_bridge.errors import BridgeError, ErrorCode, error_detail, error_response
from ai_bridge.policy import RiskTier
from ai_bridge.security import (
    is_secret_name,
    read_workspace_text,
    redact,
    redact_obj,
    validate_outbound_url,
)

ADMIN = "test-admin-token-track-c"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BRIDGE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AI_BRIDGE_MCP_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AI_BRIDGE_MCP_AUDIT_DB", raising=False)
    monkeypatch.delenv("AI_BRIDGE_ADMIN_TOKEN", raising=False)
    reset_for_tests()
    yield
    mcp_server._APPROVALS.clear()
    mcp_server._SELECTED_DEVICE.clear()
    reset_for_tests()


def _code_of(text: str) -> str | None:
    m = re.match(r"\[([A-Z_]+)\]", text or "")
    return m.group(1) if m else None


def _assert_coded(text: str) -> str:
    code = _code_of(text)
    assert code is not None, f"error text carries no taxonomy code: {text!r}"
    assert code in {c.value for c in ErrorCode}, f"unknown taxonomy code: {code}"
    return code


# ---------------------------------------------------------------------------
# 1. Error taxonomy (FR-06 / F-06-11)
# ---------------------------------------------------------------------------

def test_taxonomy_covers_all_required_codes():
    required = {
        "AUTH_REQUIRED": 401,
        "POLICY_DENIED": 403,
        "APPROVAL_REQUIRED": 403,
        "TENANT_FORBIDDEN": 403,
        "TASK_NOT_FOUND": 404,
        "DEVICE_NOT_FOUND": 404,
        "APPROVAL_NOT_FOUND": 404,
        "STALE_EPOCH": 409,
        "RATE_LIMITED": 429,
        "UPSTREAM_UNAVAILABLE": 502,
        "VALIDATION_FAILED": 400,
        "INTERNAL_ERROR": 500,
    }
    for code, status in required.items():
        info = errors_mod.ERROR_CATALOG[ErrorCode(code)]
        assert info.http_status == status, code
        assert info.message, code
    # Codes are stable strings on the wire.
    assert ErrorCode.POLICY_DENIED.value == "POLICY_DENIED"


def test_bridge_error_to_dict_and_response():
    err = BridgeError(ErrorCode.APPROVAL_NOT_FOUND, details={"approval_id": "abc"})
    assert err.to_dict() == {
        "code": "APPROVAL_NOT_FOUND",
        "message": "Approval not found.",
        "details": {"approval_id": "abc"},
    }
    assert err.http_status == 404
    body = error_response(ErrorCode.RATE_LIMITED)
    assert body["code"] == "RATE_LIMITED"
    assert body["message"] and body["details"] is None
    assert error_detail(ErrorCode.POLICY_DENIED, "nope").startswith("[POLICY_DENIED] nope")
    # Unknown codes degrade to INTERNAL_ERROR, never crash.
    assert errors_mod.error_info("NO_SUCH_CODE").code == ErrorCode.INTERNAL_ERROR


def test_gate_denial_carries_taxonomy_code():
    with pytest.raises(PermissionError) as excinfo:
        mcp_server.delete_workspace_file("x.txt")
    assert _assert_coded(str(excinfo.value)) == "APPROVAL_REQUIRED"


def test_gate_validation_failure_carries_taxonomy_code():
    with pytest.raises(ValueError) as excinfo:
        mcp_server.run_shell("")
    assert _assert_coded(str(excinfo.value)) == "VALIDATION_FAILED"


def test_gate_tool_error_carries_taxonomy_code():
    with pytest.raises(ValueError) as excinfo:
        mcp_server.get_task_status("no-such-task")
    assert _assert_coded(str(excinfo.value)) == "VALIDATION_FAILED"
    assert "Unknown task_id" in str(excinfo.value)


def test_gate_denied_and_error_audit_rows_carry_codes(tmp_path):
    with pytest.raises(PermissionError):
        mcp_server.delete_workspace_file("x.txt")
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines() if line.strip()]
    assert rows, "gate must audit denials"
    assert _assert_coded(rows[-1]["result"]) == "APPROVAL_REQUIRED"
    assert rows[-1]["decision"] == "denied"


# ---------------------------------------------------------------------------
# 2. Tool registry is the tier source (F-06-14, server.py side)
# ---------------------------------------------------------------------------

def test_tool_tier_comes_from_registry_and_matches_policy():
    assert mcp_server._tool_tier("delete_workspace_file") is RiskTier.HIGH_RISK
    assert mcp_server._tool_tier("system_info") is RiskTier.READ_ONLY
    assert mcp_server._tool_tier("definitely_not_a_tool") == "?"
    try:
        from ai_bridge.tool_registry import get_tier as registry_get_tier
    except ImportError:
        pytest.skip("tool_registry not landed yet (Track B)")
    assert registry_get_tier("delete_workspace_file") == "HIGH_RISK"


# ---------------------------------------------------------------------------
# 3. approve_task session auth (F-06-16)
# ---------------------------------------------------------------------------

def test_approve_task_signature_has_no_token_argument():
    params = list(inspect.signature(mcp_server.approve_task).parameters)
    assert params == ["approval_id"]
    # The gate exemption is still there, with the deadlock documented:
    # decide() Tier 4 needs (approval AND admin) and no approval can
    # ever exist for approve_task itself.
    assert "approve_task" in mcp_server._GATE_DECIDE_EXEMPT


def test_approve_task_fails_closed_without_admin_session():
    req = mcp_server.request_approval("delete_workspace_file", "cleanup")
    with pytest.raises(PermissionError) as excinfo:
        mcp_server.approve_task(req["approval_id"])
    assert _assert_coded(str(excinfo.value)) == "AUTH_REQUIRED"


def test_approve_task_unknown_id_reports_approval_not_found(monkeypatch):
    # With an admin bearer session the auth passes and the missing
    # approval is reported with the taxonomy code.
    monkeypatch.setenv("AI_BRIDGE_ADMIN_TOKEN", ADMIN)
    with monkeypatch.context() as m:
        m.setattr(mcp_server, "_bearer_token", lambda: ADMIN)
        with pytest.raises(ValueError) as excinfo:
            mcp_server.approve_task("no-such-approval")
    assert _assert_coded(str(excinfo.value)) == "APPROVAL_NOT_FOUND"


def test_approve_task_unknown_id_still_fails_closed_without_session():
    # Auth is checked before the approval lookup, so even an unknown
    # approval_id fails closed with AUTH_REQUIRED when there is no
    # admin session.
    with pytest.raises(PermissionError) as excinfo:
        mcp_server.approve_task("no-such-approval")
    assert _assert_coded(str(excinfo.value)) == "AUTH_REQUIRED"


# ---------------------------------------------------------------------------
# 4. Redaction (F-04-09)
# ---------------------------------------------------------------------------

def test_is_secret_name_inspects_every_component(tmp_path):
    ws = tmp_path
    assert is_secret_name(ws / "secrets" / "mykey.txt") is True
    assert is_secret_name(ws / "secrets" / "notes.txt") is True
    assert is_secret_name(ws / "sub" / "deep" / ".env") is True
    assert is_secret_name(ws / "sub" / ".env.local") is True
    assert is_secret_name(ws / "id_rsa") is True
    assert is_secret_name(ws / "keys" / "id_ed25519") is True
    assert is_secret_name(ws / "normal.txt") is False
    assert is_secret_name(ws / "docs" / "readme.md") is False


def test_is_secret_name_ignores_prefix_above_workspace(tmp_path, monkeypatch):
    # A workspace rooted under a "secret"-named parent dir must still
    # work: only components inside the workspace are inspected.
    parent = tmp_path / "my-secret-project"
    parent.mkdir()
    (parent / "normal.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("AI_BRIDGE_WORKSPACE", str(parent))
    reset_for_tests()
    assert is_secret_name(parent / "normal.txt") is False
    assert read_workspace_text("normal.txt") == "hello"


def test_read_workspace_text_refuses_secrets_directory(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "mykey.txt").write_text("x", encoding="utf-8")
    (tmp_path / "normal.txt").write_text("safe", encoding="utf-8")
    with pytest.raises(ValueError):
        read_workspace_text("secrets/mykey.txt")
    assert read_workspace_text("normal.txt") == "safe"


def test_redact_obj_nested_secrets_and_jwt():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    cleaned = redact_obj(
        {
            "args": [{"api_key": "zzz", "nested": {"token": "abc", "ok": 1}}],
            "note": f"bearer token {jwt} here",
            "plain": "version 1.2.3 is fine",
        }
    )
    assert cleaned["args"][0]["api_key"] == "***REDACTED***"
    assert cleaned["args"][0]["nested"]["token"] == "***REDACTED***"
    assert cleaned["args"][0]["nested"]["ok"] == 1
    assert jwt not in cleaned["note"] and "***REDACTED***" in cleaned["note"]
    assert cleaned["plain"] == "version 1.2.3 is fine"


def test_redact_jwt_in_free_text_but_not_version_numbers():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    assert "***REDACTED***" in redact(f"token={jwt}")
    assert redact("release 1.2.3 shipped") == "release 1.2.3 shipped"


def _jpeg_b64() -> str:
    return base64.b64encode(b"\xff\xd8\xff\xe0" + bytes(400)).decode()


def test_image_base64_real_image_preserved_but_junk_redacted():
    jpeg = _jpeg_b64()
    assert redact_obj({"image_base64": jpeg})["image_base64"] == jpeg
    assert redact_obj({"image_base64": "not-an-image!!"})["image_base64"] == "***REDACTED***"
    # Short base64-looking string is not plausible image data.
    assert redact_obj({"image_base64": "aGVsbG8gd29ybGQ="})["image_base64"] == "***REDACTED***"
    # Empty stays empty; mime_type is still preserved.
    assert redact_obj({"image_base64": "", "mime_type": "image/jpeg"}) == {
        "image_base64": "",
        "mime_type": "image/jpeg",
    }


# ---------------------------------------------------------------------------
# 5. SSRF senders (F-05-17 / SEC-11)
# ---------------------------------------------------------------------------

def test_validate_outbound_url_blocks_private_literal_ips():
    for bad in (
        "http://10.0.0.5/hook",
        "http://172.16.4.4/hook",
        "http://192.168.1.1/hook",
        "http://127.0.0.1:8080/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/hook",
        "http://100.64.0.9/hook",
    ):
        with pytest.raises(ValueError, match="blocked"):
            validate_outbound_url(bad)


def test_validate_outbound_url_rejects_bad_schemes_and_hosts():
    for bad in ("ftp://example.com/x", "http:///x", "https://user:pass@example.com/x", ""):
        with pytest.raises(ValueError):
            validate_outbound_url(bad)


def test_validate_outbound_url_allows_public_literal_ip():
    assert validate_outbound_url("https://93.184.216.34/hook") == "https://93.184.216.34/hook"


def _fake_getaddrinfo_private(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]


def _fake_getaddrinfo_public(host, port, *args, **kwargs):
    # Literal IP hosts resolve to themselves (like real getaddrinfo);
    # names resolve to a public IP.
    import ipaddress

    try:
        ipaddress.ip_address(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
    except ValueError:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class _FakeResp:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.is_success = 200 <= status_code < 300


class _FakeClient:
    def __init__(self, responses, calls):
        self._responses = list(responses)
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self._calls.append(url)
        return self._responses.pop(0)


def test_alert_sender_blocks_private_ip_without_connecting(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_private)
    calls: list[str] = []

    def _boom(*args, **kwargs):
        raise AssertionError("httpx.Client must not be constructed for a blocked URL")

    monkeypatch.setattr(alerts.httpx, "Client", _boom)
    assert alerts._send_http_json("https://hooks.example.com/services/x", {"a": 1}) is False
    assert calls == []


def test_alert_sender_posts_to_public_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_public)
    calls: list[str] = []
    monkeypatch.setattr(
        alerts.httpx, "Client",
        lambda *a, **kw: _FakeClient([_FakeResp(200)], calls),
    )
    assert alerts._send_http_json("https://hooks.example.com/services/x", {"a": 1}) is True
    assert calls == ["https://hooks.example.com/services/x"]


def test_alert_sender_redirect_to_private_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_public)
    calls: list[str] = []
    monkeypatch.setattr(
        alerts.httpx, "Client",
        lambda *a, **kw: _FakeClient(
            [_FakeResp(302, {"location": "http://10.9.9.9/steal"}), _FakeResp(200)], calls
        ),
    )
    assert alerts._send_http_json("https://hooks.example.com/services/x", {"a": 1}) is False
    assert calls == ["https://hooks.example.com/services/x"]


def test_worm_siem_blocks_private_ip_without_connecting(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORM_SIEM_URL", "https://siem.example/ingest")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_private)
    with patch("httpx.post") as post:
        out = worm_siem.emit_audit_event({"tool": "enroll_device", "decision": "allowed"})
    assert out["ok"] is False
    assert "SSRF validation" in out["error"]
    post.assert_not_called()


def test_worm_siem_posts_to_public_ip(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORM_SIEM_URL", "https://siem.example/ingest")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_public)

    class _Resp:
        status_code = 204
        headers = {}

    with patch("httpx.post", return_value=_Resp()) as post:
        out = worm_siem.emit_audit_event({"tool": "enroll_device", "decision": "allowed"})
    assert out["ok"] is True
    assert post.called


def test_worm_siem_redirect_to_private_is_blocked(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORM_SIEM_URL", "https://siem.example/ingest")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_public)
    first = _FakeResp(302, {"location": "http://127.0.0.1/steal"})
    with patch("httpx.post", return_value=first) as post:
        out = worm_siem.emit_audit_event({"tool": "x", "decision": "allowed"})
    assert out["ok"] is False
    assert "SSRF validation" in out["error"]
    assert post.call_count == 1


# ---------------------------------------------------------------------------
# 6. Config caching + validation (F-04-23)
# ---------------------------------------------------------------------------

def test_get_settings_is_cached_and_env_sensitive(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORKSPACE", "/tmp/ws-a")
    reset_for_tests()
    first = get_settings()
    assert get_settings() is first
    monkeypatch.setenv("AI_BRIDGE_WORKSPACE", "/tmp/ws-b")
    assert get_settings() is not first
    assert get_settings().workspace.as_posix() == "/tmp/ws-b"
    reset_for_tests()
    assert get_settings() is not first
    assert hasattr(get_settings, "cache_clear")


@pytest.mark.parametrize(
    "name,value",
    [
        ("AI_BRIDGE_MAX_READ_BYTES", "0"),
        ("AI_BRIDGE_MAX_READ_BYTES", "-5"),
        ("AI_BRIDGE_MAX_READ_BYTES", "not-a-number"),
        ("AI_BRIDGE_MAX_OUTPUT_BYTES", "0"),
        ("AI_BRIDGE_MAX_LIST", "0"),
        ("AI_BRIDGE_DEFAULT_TIMEOUT", "0"),
        ("AI_BRIDGE_DEFAULT_TIMEOUT", "3601"),
        ("AI_BRIDGE_MAX_TIMEOUT", "abc"),
        ("AI_BRIDGE_MAX_CONCURRENT", "0"),
        ("AI_BRIDGE_MAX_CONCURRENT", "65"),
        ("AI_BRIDGE_CONTROL_PORT", "0"),
        ("AI_BRIDGE_CONTROL_PORT", "65536"),
        ("AI_BRIDGE_CONTROL_PORT", "eighty"),
    ],
)
def test_get_settings_rejects_invalid_numbers(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    reset_for_tests()
    with pytest.raises(ValueError):
        get_settings()


def test_get_settings_no_expandvars_on_paths(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORKSPACE", "$HOME/somewhere")
    monkeypatch.setenv("AI_BRIDGE_DATA_DIR", "$HOME/.data")
    reset_for_tests()
    settings = get_settings()
    assert settings.workspace.as_posix() == "$HOME/somewhere"
    assert settings.data_dir.as_posix() == "$HOME/.data"
    monkeypatch.setenv("AI_BRIDGE_WORKSPACE", "~/ws")
    reset_for_tests()
    assert get_settings().workspace.as_posix().endswith("/ws")
    assert "$HOME" not in str(get_settings().workspace)


# ---------------------------------------------------------------------------
# 7. MCP execute endpoint policy gate (F-06-14, mcp_routes side)
# ---------------------------------------------------------------------------

@pytest.fixture
def _mcp_app(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from ai_bridge.control.app import create_app
    from ai_bridge.control.mcp_oauth import save_mcp_tool_policies

    monkeypatch.setenv("AI_BRIDGE_MCP_TOKEN_KEY", Fernet.generate_key().decode())
    app = create_app(db_path=tmp_path / "ctrl.db", admin_token=ADMIN)
    client = TestClient(app)
    # Create a server row via the API; the probe URL is unreachable so
    # the server lands "offline" but the row exists.
    r = client.post(
        "/api/mcp/servers",
        json={"name": "Local Test MCP", "url": "http://127.0.0.1:9/mcp", "auth_type": "none"},
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.status_code == 200, r.text
    srv = r.json()["server"]
    return client, app.state.conn, srv, save_mcp_tool_policies


def test_execute_denied_by_policy_returns_403_policy_denied(_mcp_app, monkeypatch):
    client, conn, srv, save_policies = _mcp_app
    save_policies(conn, {f"{srv['id']}:ping": "deny"})
    executed = []
    monkeypatch.setattr(
        "ai_bridge.control.mcp_routes.execute_mcp_tool",
        lambda **kw: executed.append(kw) or {"ok": True},
    )
    r = client.post(
        f"/api/mcp/servers/{srv['id']}/execute",
        json={"tool": "ping", "arguments": {}},
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.status_code == 403, r.text
    assert _assert_coded(r.json()["detail"]) == "POLICY_DENIED"
    assert executed == [], "denied tool must not execute"


def test_execute_require_approval_returns_403_with_approval_id(_mcp_app, monkeypatch):
    client, conn, srv, save_policies = _mcp_app
    save_policies(conn, {f"{srv['id']}:ping": "require_approval"})
    executed = []
    monkeypatch.setattr(
        "ai_bridge.control.mcp_routes.execute_mcp_tool",
        lambda **kw: executed.append(kw) or {"ok": True},
    )
    r = client.post(
        f"/api/mcp/servers/{srv['id']}/execute",
        json={"tool": "ping", "arguments": {}},
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.status_code == 403, r.text
    assert _assert_coded(r.json()["detail"]) == "APPROVAL_REQUIRED"
    assert "approval_id=" in r.json()["detail"]
    assert executed == [], "tool awaiting approval must not execute"


def test_execute_allowed_by_default_policy_runs(_mcp_app, monkeypatch):
    client, conn, srv, save_policies = _mcp_app
    monkeypatch.setattr(
        "ai_bridge.control.mcp_routes.execute_mcp_tool",
        lambda **kw: {"ok": True, "result": {"content": [{"type": "text", "text": "pong"}]}},
    )
    r = client.post(
        f"/api/mcp/servers/{srv['id']}/execute",
        json={"tool": "ping", "arguments": {}},
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_mcp_routes_errors_carry_taxonomy_codes(_mcp_app):
    client, conn, srv, save_policies = _mcp_app
    r = client.post(
        "/api/mcp/servers/no-such-server/execute",
        json={"tool": "ping", "arguments": {}},
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.status_code == 404
    assert _assert_coded(r.json()["detail"]) == "NOT_FOUND"
    r = client.post(
        "/api/mcp/servers",
        json={"name": "x", "url": "http://127.0.0.1:9/mcp", "auth_type": "bogus"},
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.status_code == 400
    assert _assert_coded(r.json()["detail"]) == "VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# 8. Workspace rename shim (F-04-12)
# ---------------------------------------------------------------------------

def test_workspace_shim_reexports_routes():
    from ai_bridge.control import workspace, workspace_routes

    assert workspace.install_workspace_routes is workspace_routes.install_workspace_routes
    assert workspace.device_summary is workspace_routes.device_summary

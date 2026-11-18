"""Wave 3B consolidation tests — cross-cutting Phase 4 checks.

This file aggregates the three Wave 3B tracks into one suite:

* Track A — control/app.py split into APIRouters (route snapshot identical).
* Track B — tools.py split into a tools/ package + one tool registry +
  one limiter.
* Track C — error taxonomy, structured redaction, SSRF validation,
  cached/validated Settings, MCP policy gate.

The per-track suites live in test_architecture_phase4_{routers,tools,
errors}.py; this module adds the cross-track invariants that only hold
when all three tracks are merged: one registry is the single source of
tiers for both server.py and mcp_routes.py, every error response carries
a taxonomy code, secrets are redacted in nested structures, and
private-IP webhook targets are blocked by the shared validator.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_architecture_phase4_routers import EXPECTED_ROUTES, _route_set  # noqa: E402
from test_architecture_phase4_tools import EXPECTED_TOOLS_PUBLIC_NAMES  # noqa: E402

import ai_bridge.tools as tools  # noqa: E402
from ai_bridge import tool_registry  # noqa: E402
from ai_bridge.config import get_settings  # noqa: E402
from ai_bridge.config import reset_for_tests as reset_settings
from ai_bridge.control.app import create_app  # noqa: E402
from ai_bridge.errors import BridgeError, ErrorCode, error_info, error_response  # noqa: E402
from ai_bridge.policy import TOOL_TIERS, normalize_tool_name  # noqa: E402
from ai_bridge.security import is_secret_name, redact, redact_obj, validate_outbound_url  # noqa: E402

# ---------------------------------------------------------------------------
# Track A: route snapshot identical after the APIRouter split
# ---------------------------------------------------------------------------

def test_phase4_route_snapshot_identical():
    """The refactored app exposes exactly the 19747b2 route set."""
    app = create_app()
    expected = set(EXPECTED_ROUTES)
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "apps" / "dashboard" / "dist").is_dir():
        expected.add(("GET", "/{full_path:path}"))
    if (repo_root / "apps" / "dashboard" / "dist" / "assets").is_dir():
        expected.add(("MOUNT", "/assets"))
    assert _route_set(app) == expected


def test_phase4_health_endpoint_still_responds():
    from fastapi.testclient import TestClient

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Track B: tools re-exports complete + one registry is single-source
# ---------------------------------------------------------------------------

def test_phase4_tools_reexports_complete():
    """`ai_bridge.tools` exposes exactly the pre-split public names."""
    actual = sorted(n for n in dir(tools) if not n.startswith("_"))
    assert actual == sorted(EXPECTED_TOOLS_PUBLIC_NAMES)


def test_phase4_registry_is_single_source_of_tiers():
    """Registry holds every TOOL_TIERS entry, with matching tiers."""
    assert len(tool_registry.TOOL_REGISTRY) == len(TOOL_TIERS)
    assert len(tool_registry.list_tools()) == len(TOOL_TIERS)
    for canonical, tier in TOOL_TIERS.items():
        entry = tool_registry.get_tool(canonical)
        assert entry is not None, canonical
        assert entry.tier == tier.name
        assert entry.requires_approval == (tier.name in {"SENSITIVE", "HIGH_RISK"})
        assert tool_registry.get_tier(canonical) == tier.name
    assert tool_registry.get_tool("definitely_not_a_tool") is None
    assert tool_registry.get_tier("definitely_not_a_tool") is None


def test_phase4_registry_normalizes_names_like_policy():
    assert tool_registry.get_tool("get_system_info") is not None
    # "system_info" is the MCP wrapper name for get_system_info.
    assert tool_registry.get_tier("system_info") == tool_registry.get_tier("get_system_info")


def test_phase4_server_gate_reads_registry_tiers():
    """server.py's tier lookup agrees with the registry (no second list)."""
    import ai_bridge.server as server_mod

    for name in ("run_shell", "read_file", "list_devices", "approve_task"):
        expected = tool_registry.get_tier(normalize_tool_name(name))
        got = server_mod._tool_tier(name)
        assert str(got) == str(expected) or getattr(got, "name", got) == expected, name


def test_phase4_limiter_single_interface():
    """The one limiter exposes the full interface; the shim re-exports it."""
    from ai_bridge.control import limiter, ratelimit

    for fn_name in ("allow", "allow_async", "rate_limit", "rate_limit_async",
                    "backend_name", "redis_required", "reset_for_tests"):
        assert hasattr(limiter, fn_name), fn_name
        assert hasattr(ratelimit, fn_name), fn_name
        assert getattr(ratelimit, fn_name) is getattr(limiter, fn_name), fn_name


# ---------------------------------------------------------------------------
# Track C: error taxonomy codes present on every error response
# ---------------------------------------------------------------------------

REQUIRED_CODES = {
    "AUTH_REQUIRED", "POLICY_DENIED", "APPROVAL_REQUIRED", "TENANT_FORBIDDEN",
    "TASK_NOT_FOUND", "DEVICE_NOT_FOUND", "APPROVAL_NOT_FOUND", "NOT_FOUND",
    "STALE_EPOCH", "RATE_LIMITED", "UPSTREAM_UNAVAILABLE", "VALIDATION_FAILED",
    "INTERNAL_ERROR",
}


def test_phase4_error_taxonomy_covers_required_codes():
    present = {code.name for code in ErrorCode}
    assert REQUIRED_CODES <= present
    for code in ErrorCode:
        resp = error_response(code)
        assert resp["code"] == code.name
        assert isinstance(resp["message"], str) and resp["message"]
        status = error_info(code).http_status
        assert isinstance(status, int) and 400 <= status < 600


def test_phase4_bridge_error_carries_taxonomy_code():
    err = BridgeError(ErrorCode.POLICY_DENIED, "denied by policy")
    payload = err.to_dict()
    assert payload["code"] == "POLICY_DENIED"
    assert err.http_status == 403
    assert "denied by policy" in payload["message"]
    assert str(err).startswith("[POLICY_DENIED]")


def test_phase4_gate_denial_carries_taxonomy_code():
    """A denied MCP tool call raises a coded error, not raw text.

    decide() itself returns plain reasons; the @_gated wrapper codes
    them (line ~509/515 in server.py). approve_task is the cleanest
    probe: without an admin bearer it is denied either by the gate
    (APPROVAL_REQUIRED, Tier 4 needs approval) or by its own session
    check (AUTH_REQUIRED) — both are taxonomy codes.
    """
    import ai_bridge.server as server_mod

    with pytest.raises(PermissionError, match=r"\[(APPROVAL_REQUIRED|AUTH_REQUIRED)\]"):
        server_mod.approve_task("no-such-approval-id")

    assert server_mod._error_code_for(PermissionError("denied")) is ErrorCode.POLICY_DENIED
    assert server_mod._error_code_for(ValueError("bad arg")) is ErrorCode.VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Track C: structured redaction (nested args/logs)
# ---------------------------------------------------------------------------

def test_phase4_redaction_nested_structures():
    nested = {
        "tool": "run_shell",
        "args": {
            "password": "hunter2",
            "items": [
                {"api_key": "sk-live-1234567890", "ok": "keep me"},
                {"deep": {"token": "tok-abcdefghij", "n": 1}},
            ],
        },
        "note": "password=hunter2 and token=tok-abcdefghij in free text",
    }
    cleaned = redact_obj(nested)
    assert cleaned["args"]["password"] == "***REDACTED***"
    assert cleaned["args"]["items"][0]["api_key"] == "***REDACTED***"
    assert cleaned["args"]["items"][0]["ok"] == "keep me"
    assert cleaned["args"]["items"][1]["deep"]["token"] == "***REDACTED***"
    assert "hunter2" not in cleaned["note"]
    assert "tok-abcdefghij" not in cleaned["note"]


def test_phase4_redaction_jwt_and_secret_paths():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    assert "eyJhbGciOi" not in redact(f"Authorization: Bearer {jwt}")
    assert is_secret_name(Path("secrets/mykey.txt"))
    assert is_secret_name(Path("config/.env"))
    assert is_secret_name(Path("keys/id_rsa"))
    assert not is_secret_name(Path("docs/readme.txt"))


# ---------------------------------------------------------------------------
# Tracks A + C: SSRF — private-IP webhook targets blocked before connecting
# ---------------------------------------------------------------------------

def _fake_getaddrinfo(public: bool):
    ip = "8.8.8.8" if public else "10.20.30.40"

    def _fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return _fake


def test_phase4_ssrf_private_ip_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(public=False))
    with pytest.raises(ValueError, match="blocked address"):
        validate_outbound_url("https://hooks.example.com/notify")


def test_phase4_ssrf_metadata_ip_blocked(monkeypatch):
    def _meta(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _meta)
    with pytest.raises(ValueError, match="blocked address"):
        validate_outbound_url("https://169.254.169.254/latest/meta-data/")


def test_phase4_ssrf_public_ip_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(public=True))
    assert validate_outbound_url("https://hooks.example.com/notify") == "https://hooks.example.com/notify"


def test_phase4_ssrf_rejects_non_http_and_embedded_credentials(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(public=True))
    with pytest.raises(ValueError):
        validate_outbound_url("ftp://hooks.example.com/x")
    with pytest.raises(ValueError):
        validate_outbound_url("https://user:pass@hooks.example.com/x")


# ---------------------------------------------------------------------------
# Track C: cached + validated Settings
# ---------------------------------------------------------------------------

def test_phase4_settings_cached_and_validated(monkeypatch):
    reset_settings()
    try:
        assert get_settings() is get_settings()
        monkeypatch.setenv("AI_BRIDGE_MAX_TIMEOUT", "not-a-number")
        reset_settings()
        with pytest.raises(ValueError):
            get_settings()
    finally:
        monkeypatch.delenv("AI_BRIDGE_MAX_TIMEOUT", raising=False)
        reset_settings()
        assert get_settings() is get_settings()

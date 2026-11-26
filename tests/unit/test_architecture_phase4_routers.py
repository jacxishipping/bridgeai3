"""Wave 3B Track A — control-plane router split: route-snapshot + SSRF tests.

Route snapshot: the (method, path) set below is the literal route set of the
committed tree at 19747b2, captured by building the app from a clean
``git worktree`` of that commit (``git worktree add /tmp/aib-19747b2 19747b2``)
and dumping ``app.routes``. The refactor must not add, drop, rename or
re-method any route. Two entries are environment-dependent and are added to
the expectation only when the dashboard build exists (exactly as
``create_app()`` does): ``GET /{full_path:path}`` (SPA catch-all, when
``apps/dashboard/dist`` exists) and the ``/assets`` static mount (when
``apps/dashboard/dist/assets`` exists).
"""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

import ai_bridge.control.app as appmod
from ai_bridge.control.app import (
    create_app,
    make_quick_sig,
    send_webhook_notification,
)

ADMIN_TOKEN = "wave3b-router-snapshot-token"

# Literal (method, path) route set of commit 19747b2 (220 entries, no
# dashboard dist in the worktree it was captured from).
EXPECTED_ROUTES = set(
    [
        ("DELETE", "/api/agents/{agent_id}"),
        ("DELETE", "/api/agents/{agent_id}/memory/chunks"),
        ("DELETE", "/api/approvals/{approval_id}"),
        ("DELETE", "/api/audit/{audit_id}"),
        ("DELETE", "/api/connections/clients/{client_id}"),
        ("DELETE", "/api/connectors/m365"),
        ("DELETE", "/api/devices/{device_id}"),
        ("DELETE", "/api/devices/{device_id}/desktop/webrtc/{session_id}"),
        ("DELETE", "/api/devices/{device_id}/policy"),
        ("DELETE", "/api/devices/{device_id}/terminal/{session_id}"),
        ("DELETE", "/api/mcp/servers/{server_id}"),
        ("DELETE", "/api/memory/snippets/{snippet_id}"),
        ("DELETE", "/api/policies/{version}"),
        ("DELETE", "/api/schedules/{schedule_id}"),
        ("DELETE", "/api/tasks"),
        ("DELETE", "/api/tasks/dead-letter/{dlq_id}"),
        ("DELETE", "/api/tasks/{task_id}"),
        ("DELETE", "/api/triggers/{trigger_id}"),
        ("DELETE", "/api/workflows/templates/{template_id}"),
        ("DELETE", "/api/workflows/{workflow_id}"),
        ("GET", "/api/activity/live"),
        ("GET", "/api/activity/stream"),
        ("GET", "/api/admin/backups"),
        ("GET", "/api/admin/backups/{filename}"),
        ("GET", "/api/agent/policy"),
        ("GET", "/api/agent/tasks/next"),
        ("GET", "/api/agent/tasks/{task_id}/execution-state"),
        ("GET", "/api/agent/webrtc/ice-servers"),
        ("GET", "/api/agent/webrtc/{session_id}"),
        ("GET", "/api/agent/whoami"),
        ("GET", "/api/agents"),
        ("GET", "/api/agents/{agent_id}/memory/chunks"),
        ("GET", "/api/agents/{agent_id}/runs"),
        ("GET", "/api/ai/devices"),
        ("GET", "/api/ai/tools"),
        ("GET", "/api/alerts/config"),
        ("GET", "/api/alerts/deliveries"),
        ("GET", "/api/approvals"),
        ("GET", "/api/approvals/quick/{approval_id}"),
        ("GET", "/api/audit"),
        ("GET", "/api/audit/export"),
        ("GET", "/api/audit/verify"),
        ("GET", "/api/auth/csrf-token"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/auth/oidc/callback"),
        ("GET", "/api/auth/oidc/configured"),
        ("GET", "/api/auth/oidc/login"),
        ("GET", "/api/auth/users"),
        ("GET", "/api/connections"),
        ("GET", "/api/connectors/m365/callback"),
        ("GET", "/api/connectors/m365/status"),
        ("GET", "/api/devices"),
        ("GET", "/api/devices/{device_id}/desktop/a11y/snapshot"),
        ("GET", "/api/devices/{device_id}/desktop/capabilities"),
        ("GET", "/api/devices/{device_id}/desktop/frame"),
        ("GET", "/api/devices/{device_id}/desktop/live.jpg"),
        ("GET", "/api/devices/{device_id}/desktop/monitors"),
        ("GET", "/api/devices/{device_id}/desktop/webrtc/{session_id}"),
        ("GET", "/api/devices/{device_id}/intelligence"),
        ("GET", "/api/devices/{device_id}/memory"),
        ("GET", "/api/devices/{device_id}/overview"),
        ("GET", "/api/devices/{device_id}/policy"),
        ("GET", "/api/devices/{device_id}/telemetry"),
        ("GET", "/api/devices/{device_id}/terminal/sessions"),
        ("GET", "/api/devices/{device_id}/terminal/{session_id}/stream"),
        ("GET", "/api/events/stream"),
        ("GET", "/api/features"),
        ("GET", "/api/fleet/analytics/trends"),
        ("GET", "/api/fleet/broadcast/{batch_id}"),
        ("GET", "/api/fleet/desktop-capabilities"),
        ("GET", "/api/fleet/desktop/frames"),
        ("GET", "/api/fleet/health"),
        ("GET", "/api/fleet/intelligence"),
        ("GET", "/api/fleet/inventory"),
        ("GET", "/api/fleet/notifications"),
        ("GET", "/api/fleet/rollouts"),
        ("GET", "/api/fleet/telemetry"),
        ("GET", "/api/fleet/version"),
        ("GET", "/api/incidents"),
        ("GET", "/api/logs"),
        ("GET", "/api/mcp/oauth/callback"),
        ("GET", "/api/mcp/servers"),
        ("GET", "/api/mcp/templates"),
        ("GET", "/api/mcp/tool-policies"),
        ("GET", "/api/memory/snippets"),
        ("GET", "/api/metrics/summary"),
        ("GET", "/api/policies"),
        ("GET", "/api/policies/export"),
        ("GET", "/api/policies/history"),
        ("GET", "/api/roles"),
        ("GET", "/api/schedules"),
        ("GET", "/api/settings/ai"),
        ("GET", "/api/settings/webhook"),
        ("GET", "/api/supervisor/handoffs/{handoff_id}"),
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/dead-letter"),
        ("GET", "/api/tasks/history"),
        ("GET", "/api/tasks/{task_id}"),
        ("GET", "/api/tasks/{task_id}/events"),
        ("GET", "/api/tasks/{task_id}/result"),
        ("GET", "/api/tenants"),
        ("GET", "/api/tenants/{tenant_id}/usage"),
        ("GET", "/api/traces/{trace_id}"),
        ("GET", "/api/triggers"),
        ("GET", "/api/v1/audit"),
        ("GET", "/api/v1/devices"),
        ("GET", "/api/v1/devices/{device_id}"),
        ("GET", "/api/v1/devices/{device_id}/files"),
        ("GET", "/api/v1/devices/{device_id}/files/download"),
        ("GET", "/api/v1/devices/{device_id}/monitors"),
        ("GET", "/api/v1/devices/{device_id}/status"),
        ("GET", "/api/v1/devices/{device_id}/ui-elements"),
        ("GET", "/api/v1/models"),
        ("GET", "/api/v1/policies"),
        ("GET", "/api/v1/sessions"),
        ("GET", "/api/v1/sessions/ice-servers"),
        ("GET", "/api/v1/sessions/{session_id}"),
        ("GET", "/api/v1/sessions/{session_id}/recording"),
        ("GET", "/api/v1/tasks"),
        ("GET", "/api/v1/tasks/{task_id}"),
        ("GET", "/api/workflows"),
        ("GET", "/api/workflows/curated"),
        ("GET", "/api/workflows/templates"),
        ("GET", "/api/workflows/{workflow_id}"),
        ("GET", "/api/workflows/{workflow_id}/stream"),
        ("GET", "/api/workflows/{workflow_id}/timeline"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/health"),
        ("GET", "/health/live"),
        ("GET", "/health/production-checklist"),
        ("GET", "/health/ready"),
        ("GET", "/install.ps1"),
        ("GET", "/install.sh"),
        ("GET", "/metrics"),
        ("GET", "/openapi.json"),
        ("GET", "/redoc"),
        ("GET", "/v1/models"),
        ("HEAD", "/docs"),
        ("HEAD", "/docs/oauth2-redirect"),
        ("HEAD", "/openapi.json"),
        ("HEAD", "/redoc"),
        ("PATCH", "/api/agents/{agent_id}"),
        ("PATCH", "/api/auth/users/{username}"),
        ("PATCH", "/api/mcp/servers/{server_id}"),
        ("PATCH", "/api/v1/sessions/{session_id}/mode"),
        ("PATCH", "/api/v1/sessions/{session_id}/quality"),
        ("POST", "/api/admin/backups"),
        ("POST", "/api/admin/backups/{filename}/restore"),
        ("POST", "/api/agent/heartbeat"),
        ("POST", "/api/agent/rotate"),
        ("POST", "/api/agent/tasks/{task_id}/progress"),
        ("POST", "/api/agent/tasks/{task_id}/result"),
        ("POST", "/api/agent/webrtc/{session_id}/candidates"),
        ("POST", "/api/agent/webrtc/{session_id}/error"),
        ("POST", "/api/agent/webrtc/{session_id}/offer"),
        ("POST", "/api/agents"),
        ("POST", "/api/agents/{agent_id}/ask"),
        ("POST", "/api/agents/{agent_id}/memory/chunks"),
        ("POST", "/api/agents/{agent_id}/run"),
        ("POST", "/api/ai/agent"),
        ("POST", "/api/ai/chat"),
        ("POST", "/api/ai/chat/stream"),
        ("POST", "/api/ai/execute"),
        ("POST", "/api/alerts/test"),
        ("POST", "/api/approvals/{approval_id}/decide"),
        ("POST", "/api/approvals/{approval_id}/replan"),
        ("POST", "/api/audit/retention"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/oidc/exchange"),
        ("POST", "/api/auth/users"),
        ("POST", "/api/connectors/m365/login"),
        ("POST", "/api/devices/enroll"),
        ("POST", "/api/devices/{device_id}/desktop/a11y/act"),
        ("POST", "/api/devices/{device_id}/desktop/input"),
        ("POST", "/api/devices/{device_id}/desktop/webrtc/session"),
        ("POST", "/api/devices/{device_id}/desktop/webrtc/{session_id}/answer"),
        ("POST", "/api/devices/{device_id}/desktop/webrtc/{session_id}/candidates"),
        ("POST", "/api/devices/{device_id}/memory/learn"),
        ("POST", "/api/devices/{device_id}/mtls-fingerprint"),
        ("POST", "/api/devices/{device_id}/refresh"),
        ("POST", "/api/devices/{device_id}/revoke"),
        ("POST", "/api/devices/{device_id}/terminal/sessions"),
        ("POST", "/api/devices/{device_id}/terminal/{session_id}/input"),
        ("POST", "/api/devices/{device_id}/terminal/{session_id}/resize"),
        ("POST", "/api/devices/{device_id}/update"),
        ("POST", "/api/emergency-stop"),
        ("POST", "/api/emergency-stop/clear"),
        ("POST", "/api/fleet/broadcast"),
        ("POST", "/api/fleet/rollouts"),
        ("POST", "/api/fleet/rollouts/{rollout_id}/advance"),
        ("POST", "/api/fleet/rollouts/{rollout_id}/pause"),
        ("POST", "/api/fleet/rollouts/{rollout_id}/resume"),
        ("POST", "/api/fleet/rollouts/{rollout_id}/rollback"),
        ("POST", "/api/fleet/update"),
        ("POST", "/api/incidents/{incident_id}/ack"),
        ("POST", "/api/mcp/oauth/discover"),
        ("POST", "/api/mcp/servers"),
        ("POST", "/api/mcp/servers/{server_id}/execute"),
        ("POST", "/api/mcp/servers/{server_id}/oauth/start"),
        ("POST", "/api/mcp/servers/{server_id}/sync"),
        ("POST", "/api/memory/snippets"),
        ("POST", "/api/policies"),
        ("POST", "/api/policies/import"),
        ("POST", "/api/policies/simulate"),
        ("POST", "/api/policy/simulate"),
        ("POST", "/api/roles/{role_id}/enable"),
        ("POST", "/api/schedules"),
        ("POST", "/api/schedules/{schedule_id}/run-now"),
        ("POST", "/api/schedules/{schedule_id}/toggle"),
        ("POST", "/api/settings/ai"),
        ("POST", "/api/settings/webhook"),
        ("POST", "/api/settings/webhook/test"),
        ("POST", "/api/stream-ticket"),
        ("POST", "/api/supervisor/handoff"),
        ("POST", "/api/supervisor/plan"),
        ("POST", "/api/supervisor/recover"),
        ("POST", "/api/tasks"),
        ("POST", "/api/tasks/dead-letter/{dlq_id}/replay"),
        ("POST", "/api/tasks/{task_id}/cancel"),
        ("POST", "/api/tasks/{task_id}/progress"),
        ("POST", "/api/tenants"),
        ("POST", "/api/triggers"),
        ("POST", "/api/triggers/{trigger_id}/test"),
        ("POST", "/api/triggers/{trigger_id}/toggle"),
        ("POST", "/api/v1/agents/{device_id}/heartbeat"),
        ("POST", "/api/v1/chat/completions"),
        ("POST", "/api/v1/devices/{device_id}/files/upload"),
        ("POST", "/api/v1/sessions"),
        ("POST", "/api/v1/sessions/{session_id}/answer"),
        ("POST", "/api/v1/sessions/{session_id}/clipboard"),
        ("POST", "/api/v1/sessions/{session_id}/failover"),
        ("POST", "/api/v1/sessions/{session_id}/ice"),
        ("POST", "/api/v1/sessions/{session_id}/mode"),
        ("POST", "/api/v1/sessions/{session_id}/offer"),
        ("POST", "/api/v1/sessions/{session_id}/recording/seal"),
        ("POST", "/api/v1/sessions/{session_id}/terminate"),
        ("POST", "/api/v1/tasks"),
        ("POST", "/api/workflows"),
        ("POST", "/api/workflows/templates"),
        ("POST", "/api/workflows/{workflow_id}/cancel"),
        ("POST", "/api/workflows/{workflow_id}/retry"),
        ("POST", "/api/workflows/{workflow_id}/schedule"),
        ("POST", "/v1/chat/completions"),
        ("PUT", "/api/agent/policy"),
        ("PUT", "/api/alerts/config"),
        ("PUT", "/api/approvals/{approval_id}"),
        ("PUT", "/api/audit/{audit_id}"),
        ("PUT", "/api/connections/clients/{client_id}"),
        ("PUT", "/api/devices/{device_id}"),
        ("PUT", "/api/devices/{device_id}/metadata"),
        ("PUT", "/api/devices/{device_id}/policy"),
        ("PUT", "/api/devices/{device_id}/ring"),
        ("PUT", "/api/mcp/tool-policies"),
        ("PUT", "/api/policies/{version}"),
        ("PUT", "/api/tasks/{task_id}"),
        ("PUT", "/api/tenants/{tenant_id}/quotas"),
        ("WS", "/api/agent/ws"),
        ("WS", "/api/v1/ws/sessions/{session_id}"),
    ]
)


def _route_set(app):
    out = set()

    def _collect(routes):
        for route in routes:
            # FastAPI >= 0.163 appends an _IncludedRouter bookkeeping entry
            # per include_router() call; it is not an endpoint itself, but
            # its original_router holds the included routes.
            original = getattr(route, "original_router", None)
            if original is not None:
                _collect(original.routes)
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            if isinstance(route, Mount):
                out.add(("MOUNT", path))
                continue
            methods = sorted(getattr(route, "methods", set()) or set())
            if not methods:
                out.add(("WS", path))
            else:
                for method in methods:
                    out.add((method, path))

    _collect(app.routes)
    return out


def _expected_routes():
    expected = set(EXPECTED_ROUTES)
    dist_dir = Path("/app/apps/dashboard/dist")
    if not dist_dir.exists():
        dist_dir = Path(__file__).resolve().parents[2] / "apps" / "dashboard" / "dist"
    if dist_dir.exists():
        expected.add(("GET", "/{full_path:path}"))
        if (dist_dir / "assets").exists():
            expected.add(("MOUNT", "/assets"))
    return expected


@pytest.fixture()
def client(tmp_path):
    app = create_app(admin_token=ADMIN_TOKEN, db_path=tmp_path / "control.db")
    return TestClient(app)


@pytest.fixture()
def app_obj(tmp_path):
    return create_app(admin_token=ADMIN_TOKEN, db_path=tmp_path / "control.db")


def _admin():
    return {"X-Admin-Token": ADMIN_TOKEN}


# --- Route snapshot (OV-02 / F-05-11) ---------------------------------------


def test_route_set_identical_to_committed_snapshot(app_obj):
    actual = _route_set(app_obj)
    expected = _expected_routes()
    assert actual == expected, f"route set changed by the router split;\nmissing (in 19747b2, gone now): {sorted(expected - actual)}\nadded (not in 19747b2): {sorted(actual - expected)}"


def test_route_count_matches_committed_snapshot(app_obj):
    assert len(_route_set(app_obj)) == len(_expected_routes())


def test_router_factories_exist_and_are_used():
    """The split is structural: one APIRouter factory per domain."""
    from ai_bridge.control.routers import (
        admin,
        agent,
        audit,
        auth,
        devices,
        health,
        tasks,
        webrtc,
        workflows,
    )
    from ai_bridge.control.routers._context import ControlContext

    for module, factory in [
        (health, "create_health_router"),
        (devices, "create_devices_router"),
        (tasks, "create_tasks_router"),
        (audit, "create_audit_router"),
        (workflows, "create_workflows_router"),
        (webrtc, "create_webrtc_router"),
        (admin, "create_admin_router"),
        (auth, "create_auth_router"),
        (agent, "create_agent_router"),
    ]:
        assert callable(getattr(module, factory)), f"{module.__name__} lacks {factory}"
    assert "conn" in ControlContext.__dataclass_fields__
    assert "admin_token" in ControlContext.__dataclass_fields__


def test_public_imports_still_work():
    assert callable(create_app)
    assert callable(make_quick_sig)
    assert callable(send_webhook_notification)


def test_rate_limit_docstring_matches_middleware():
    """F-05-11: the module docstring must not still claim 120/min.

    The middleware enforces 30/min per IP on auth endpoints, 1200/min per
    authenticated token and 300/min per unauthenticated IP (60 s windows).
    """
    assert "120 requests/minute" not in (appmod.__doc__ or "")
    assert "1200" in (appmod.__doc__ or "")
    assert "300" in (appmod.__doc__ or "")


# --- Smoke: one live endpoint per router ------------------------------------


def test_smoke_health(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/health/live").json()["status"] == "live"


def test_smoke_devices_tasks_audit(client):
    assert client.get("/api/devices", headers=_admin()).status_code == 200
    assert client.get("/api/tasks", headers=_admin()).status_code == 200
    assert client.get("/api/approvals", headers=_admin()).status_code == 200
    assert client.get("/api/audit", headers=_admin()).status_code == 200


def test_smoke_workflows_admin_auth(client):
    assert client.get("/api/workflows", headers=_admin()).status_code == 200
    assert client.get("/api/workflows/templates", headers=_admin()).status_code == 200
    assert client.get("/api/tenants", headers=_admin()).status_code == 200
    assert client.get("/api/alerts/config", headers=_admin()).status_code == 200
    assert client.get("/api/auth/me", headers=_admin()).status_code == 200


def test_smoke_webrtc_route_runs_handler(client):
    resp = client.post(
        "/api/devices/no-such-device/desktop/webrtc/session",
        json={"device_id": "no-such-device"},
        headers=_admin(),
    )
    # A missing route would be {"detail": "Not Found"}; the handler 404s
    # with its own detail, proving the router mounted and ran.
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Device not found."


def test_smoke_agent_route_runs_handler(client):
    resp = client.get("/api/agent/whoami")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Device token required."


# --- SSRF hardening of the app.py webhook sender (SEC-11 / F-05-17) --------

_EVENT = {
    "event": "approval_requested",
    "approval_id": "app-ssrf",
    "task_id": "tsk-ssrf",
    "device_id": "dev-01",
    "device_name": "Studio-Desktop",
    "tool": "delete_workspace_file",
    "tier": 4,
    "args": {"path": "test.txt"},
}

_PUBLIC_ADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
_PRIVATE_ADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", 443))]
_METADATA_ADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]


def _fake_getaddrinfo(mapping):
    def _fake(host, port, *args, **kwargs):
        if isinstance(mapping, Exception):
            raise mapping
        return mapping.get(host, _PUBLIC_ADDR)

    return _fake


def _mock_client(monkeypatch, handler):
    orig_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: orig_client(transport=transport, **kw))


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(status_code=200, json={"ok": True})


def test_ssrf_webhook_url_resolving_to_private_ip_rejected(monkeypatch):
    """A hostname that resolves to a private IP must never be contacted."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"evil.example": _PRIVATE_ADDR}))
    seen = []
    _mock_client(monkeypatch, lambda req: seen.append(str(req.url)) or _ok_handler(req))
    res = send_webhook_notification(
        "https://evil.example/hook",
        _EVENT,
        admin_token=ADMIN_TOKEN,
        public_base_url="https://jxiptvplayer.app",
    )
    assert res["ok"] is False
    assert "private" in res["error"].lower() or "blocked" in res["error"].lower()
    assert seen == []


def test_ssrf_webhook_url_resolving_to_metadata_ip_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"evil.example": _METADATA_ADDR}))
    seen = []
    _mock_client(monkeypatch, lambda req: seen.append(str(req.url)) or _ok_handler(req))
    res = send_webhook_notification(
        "https://evil.example/hook",
        _EVENT,
        admin_token=ADMIN_TOKEN,
        public_base_url="https://jxiptvplayer.app",
    )
    assert res["ok"] is False
    assert seen == []


def test_ssrf_webhook_literal_private_ip_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
    seen = []
    _mock_client(monkeypatch, lambda req: seen.append(str(req.url)) or _ok_handler(req))
    res = send_webhook_notification(
        "http://192.168.1.10/hook",
        _EVENT,
        admin_token=ADMIN_TOKEN,
        public_base_url="https://jxiptvplayer.app",
    )
    assert res["ok"] is False
    assert seen == []


def test_ssrf_webhook_dns_failure_rejected_by_sender(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(socket.gaierror("Name or service not known")))
    seen = []
    _mock_client(monkeypatch, lambda req: seen.append(str(req.url)) or _ok_handler(req))
    res = send_webhook_notification(
        "https://unresolvable.example/hook",
        _EVENT,
        admin_token=ADMIN_TOKEN,
        public_base_url="https://jxiptvplayer.app",
    )
    assert res["ok"] is False
    assert seen == []


def test_ssrf_webhook_redirect_to_private_ip_not_followed(monkeypatch):
    """A 302 to a private IP must not be followed (no 2nd request)."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(status_code=302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return _ok_handler(request)

    _mock_client(monkeypatch, handler)
    res = send_webhook_notification(
        "https://webhook.example.com/hook",
        _EVENT,
        admin_token=ADMIN_TOKEN,
        public_base_url="https://jxiptvplayer.app",
    )
    assert res["ok"] is False
    assert len(seen) == 1
    assert all("169.254" not in url for url in seen)


def test_ssrf_webhook_redirect_to_dns_rebinding_host_not_followed(monkeypatch):
    """A 302 to a host that resolves to a private IP must not be followed."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"evil.example": _PRIVATE_ADDR}),
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(status_code=302, headers={"location": "https://evil.example/steal"})
        return _ok_handler(request)

    _mock_client(monkeypatch, handler)
    res = send_webhook_notification(
        "https://webhook.example.com/hook",
        _EVENT,
        admin_token=ADMIN_TOKEN,
        public_base_url="https://jxiptvplayer.app",
    )
    assert res["ok"] is False
    assert len(seen) == 1


def test_ssrf_webhook_redirect_to_public_ip_followed(monkeypatch):
    """Positive control: a 302 to a public host is followed (max 3 hops)."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(status_code=302, headers={"location": "https://api.example.com/other"})
        return _ok_handler(request)

    _mock_client(monkeypatch, handler)
    res = send_webhook_notification(
        "https://webhook.example.com/hook",
        _EVENT,
        admin_token=ADMIN_TOKEN,
        public_base_url="https://jxiptvplayer.app",
    )
    assert res["ok"] is True
    assert len(seen) == 2


def test_ssrf_webhook_redirect_limit(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status_code=302, headers={"location": "https://webhook.example.com/again"})

    _mock_client(monkeypatch, handler)
    res = send_webhook_notification(
        "https://webhook.example.com/hook",
        _EVENT,
        admin_token=ADMIN_TOKEN,
        public_base_url="https://jxiptvplayer.app",
    )
    assert res["ok"] is False
    assert "redirect" in res["error"].lower()
    assert len(seen) == 4  # initial request + 3 followed redirects


def test_ssrf_webhook_config_rejects_resolved_private_ip(client, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"evil.example": _PRIVATE_ADDR}))
    resp = client.post(
        "/api/settings/webhook",
        json={"webhook_url": "https://evil.example/hook"},
        headers=_admin(),
    )
    assert resp.status_code == 400

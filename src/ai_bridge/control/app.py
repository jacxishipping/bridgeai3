"""Control-plane API (FastAPI, modular monolith).

Auth:
- Admin/dashboard: header X-Admin-Token == AI_BRIDGE_ADMIN_TOKEN (or the
  token passed to create_app() in tests). No admin token configured ->
  admin endpoints refuse (fail closed), except /health.
- Device/agent: header X-Device-Token, compared by SHA-256 hash.
  Devices connect OUTBOUND to this API; the plane never opens an
  inbound shell to a device, and no device port is exposed.
- Tenant isolation: the tenant is derived from the authenticated session
  server-side (R-P0-12); a caller-supplied tenant that differs is rejected.
  A device token only matches devices in its own tenant.

Endpoints: /health, /api/devices, /api/tasks, /api/approvals,
/api/audit, /api/emergency-stop, /api/settings/webhook, /api/agent/* (polling).
Rate limiting: 30 requests/minute per IP on auth endpoints; 1200/minute per
authenticated token and 300/minute per unauthenticated IP otherwise
(60-second windows; Redis/durable store when configured).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers

from .. import tools
from ..policy import TOOL_TIERS, Policy, normalize_tool_name
from ..security import redact_obj
from . import agent_channel
from . import alerts as alertsmod
from . import auth as authmod
from . import db as dbmod
from . import task_service as task_service_mod
from .csrf import csrf_middleware
from .dispatch import dispatch
from .ratelimit import rate_limit
from .secrets_backend import get_secret

MAX_RESULT_CHARS = 512_000
_DUMMY_SALT = "0" * 32
_DUMMY_HASH = "0" * 64


# SEC-11 / F-05-17 (Wave 3B): SSRF blocklist for webhook targets. Deliberately
# an explicit network list, NOT ip.is_private/is_global: in sandboxed/CI
# environments the resolver may answer public hostnames with 198.18.0.0/15
# (benchmarking) addresses, which ipaddress reports as private even though
# the range is not an internal-service range. The ranges below are the ones
# an SSRF attacker can actually abuse: loopback, RFC1918, link-local
# (incl. the 169.254.169.254 cloud-metadata address), CGNAT, multicast,
# reserved and documentation space, plus IPv6 ULA/Teredo/6to4 embeddings.
_BLOCKED_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",  # "this network" / unspecified source
        "10.0.0.0/8",  # RFC1918
        "100.64.0.0/10",  # CGNAT shared address space
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, incl. cloud metadata 169.254.169.254
        "172.16.0.0/12",  # RFC1918
        "192.0.0.0/24",  # IANA special-purpose
        "192.0.2.0/24",  # TEST-NET-1 documentation
        "192.31.196.0/24",  # AS112
        "192.168.0.0/16",  # RFC1918
        "198.51.100.0/24",  # TEST-NET-2 documentation
        "203.0.113.0/24",  # TEST-NET-3 documentation
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved (incl. 255.255.255.255)
    )
)
_BLOCKED_IPV6_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "::/128",  # unspecified
        "::1/128",  # loopback
        "100::/64",  # discard-only
        "2001::/32",  # Teredo (embeds an IPv4 address)
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (embeds an IPv4 address)
        "fc00::/7",  # unique-local
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if an IP must never be a webhook target (SSRF)."""
    if ip.version == 6:
        mapped = ip.ipv4_mapped
        if mapped is not None:
            # ::ffff:a.b.c.d — judge the embedded IPv4 address.
            return _is_blocked_ip(mapped)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    networks = _BLOCKED_IPV4_NETWORKS if ip.version == 4 else _BLOCKED_IPV6_NETWORKS
    return any(ip in net for net in networks)


def _resolved_webhook_ips(hostname: str, port: int) -> list[str]:
    """Resolve a webhook hostname; every answer is returned for checking.

    Raises HTTPException(400) when the name cannot be resolved at all.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError) as exc:
        raise HTTPException(status_code=400, detail="Webhook host could not be resolved.") from exc
    ips = [str(info[4][0]) for info in infos if info[4]]
    if not ips:
        raise HTTPException(status_code=400, detail="Webhook host could not be resolved.")
    return ips


def _validate_webhook_url(url: str, *, strict_dns: bool = False) -> None:
    """Validate a webhook URL against SSRF (SEC-11).

    Always checks scheme/host, the loopback/metadata hostname blocklist and
    literal blocked IPs. Hostnames are then resolved with DNS and *every*
    resolved address must pass the blocklist, so a name that rebinds to a
    private/loopback/link-local address is rejected after resolution.

    ``strict_dns`` is for the sender: when True an unresolvable host is
    rejected (fail closed — nothing is sent to an unverified target). At
    configuration time (``strict_dns=False``) an unresolvable host is
    accepted — the operator may configure a webhook before its DNS exists —
    because the sender re-validates strictly before every send.
    """
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise HTTPException(status_code=400, detail="webhook_url must be an http(s) URL.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise HTTPException(status_code=400, detail="webhook_url has an invalid port.") from None
    host = parsed.hostname.lower()
    if host in ("localhost", "127.0.0.1", "::1", "metadata", "metadata.google.internal") or host.endswith((".localhost", ".internal")):
        raise HTTPException(status_code=400, detail="Loopback and metadata webhooks are not allowed.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _is_blocked_ip(ip):
            raise HTTPException(status_code=400, detail="Private/loopback IP webhooks are not allowed.")
        return
    try:
        resolved = _resolved_webhook_ips(host, port)
    except HTTPException:
        if strict_dns:
            raise
        return
    for raw in resolved:
        try:
            resolved_ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if _is_blocked_ip(resolved_ip):
            raise HTTPException(status_code=400, detail="Webhook host resolves to a private/loopback/link-local address.")


def _valid_oauth_redirect_uri(uri: str) -> bool:
    try:
        p = urlparse(uri)
    except Exception:
        return False
    if p.fragment or not p.netloc or not p.hostname:
        return False
    if p.scheme == "https":
        return True
    if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"):
        return True
    return False


def _rate_limit(key: str, limit: int = 120, window: int = 60, conn: Any | None = None) -> None:
    """Sync rate-limit check (DIST-07). Kept for sync endpoints only
    (e.g. quick-approval HTML routes, which run outside a loop and
    reach Redis via the sync wrapper). Async callers must use
    :func:`_rate_limit_async` so the shared Redis limiter is enforced
    instead of the per-worker memory fallback."""
    if os.environ.get("REDIS_URL") or os.environ.get("AI_BRIDGE_REDIS_URL"):
        rate_limit(key, limit, window)()
        return
    # DIST-07 / limiter.py is the single rate-limit interface: the
    # durable implementation is reached through it, never imported
    # directly by callers.
    from .limiter import allow

    if not allow(key, limit, float(window), conn=conn):
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")


async def _rate_limit_async(key: str, limit: int = 120, window: int = 60, conn: Any | None = None) -> None:
    """Async-native rate-limit check (DIST-07).

    Awaits the shared limiter directly (``limiter.rate_limit_async`` /
    ``limiter.allow_async``), so async middleware enforces the shared
    Redis limit when Redis is configured instead of silently falling
    back to the per-worker memory limiter. Falls back to memory only
    when Redis is unconfigured or unreachable (PERF-02: never a
    per-request DB write on the Redis-configured path).
    """
    from .limiter import allow_async, rate_limit_async

    if os.environ.get("REDIS_URL") or os.environ.get("AI_BRIDGE_REDIS_URL"):
        await rate_limit_async(key, limit, window)
        return
    if not await allow_async(key, limit, float(window), conn=conn):
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")


def make_quick_sig(admin_token: str | None, approval_id: str, decision: str) -> str:
    from . import production_readiness as prodready

    secret = (admin_token or os.environ.get("AI_BRIDGE_ADMIN_TOKEN") or "").strip()
    if not secret:
        if prodready.is_production_profile():
            raise ValueError("Quick approval signature requires AI_BRIDGE_ADMIN_TOKEN (or admin_token) in production.")
        secret = "ai-bridge-secret"
    msg = f"{approval_id}:{decision}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def send_webhook_notification(
    webhook_url: str,
    event: dict,
    admin_token: str | None = None,
    public_base_url: str | None = None,
    timeout: int = 10,
) -> dict:
    """Send a formatted notification to Slack, Discord, Telegram Bot, or generic webhook URL with 1-click mobile approval links.

    SSRF (SEC-11): the target is DNS-resolved and every resolved address is
    checked against the private/loopback/link-local blocklist before any
    request is sent, and redirects are followed manually (max 3) with the
    same validation applied to each redirect target, so a redirect can never
    smuggle the request to an internal address.
    """
    url = (webhook_url or "").strip()
    if not url:
        return {"ok": False, "error": "No webhook URL configured."}
    try:
        _validate_webhook_url(url, strict_dns=True)
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail}

    safe_args = redact_obj(event.get("args") or {})
    args_json = json.dumps(safe_args, indent=2)
    tool = event.get("tool", "unknown_tool")
    tier = event.get("tier", "unknown")
    dev_name = event.get("device_name") or event.get("device_id") or "Device"
    app_id = event.get("approval_id") or ""
    task_id = event.get("task_id") or ""
    evt_type = event.get("event", "approval_requested")

    from . import production_readiness as prodready

    base_url = (public_base_url or prodready.env_public_base_url() or "https://jxiptvplayer.app").rstrip("/")

    approve_url = ""
    deny_url = ""
    if app_id:
        app_sig = make_quick_sig(admin_token, app_id, "approved")
        den_sig = make_quick_sig(admin_token, app_id, "denied")
        approve_url = f"{base_url}/api/approvals/quick/{app_id}?decision=approved&sig={app_sig}"
        deny_url = f"{base_url}/api/approvals/quick/{app_id}?decision=denied&sig={den_sig}"

    text = f"[AI Bridge Approval Required]\nTool: {tool} (Tier {tier})\nDevice: {dev_name}\nTask ID: {task_id}\nApproval ID: {app_id}\nArgs: {json.dumps(safe_args)[:300]}"
    payload = {
        "text": text,
        "content": text,
        "event": evt_type,
        "approval_id": app_id,
        "task_id": task_id,
        "device_id": event.get("device_id"),
        "device_name": dev_name,
        "tool": tool,
        "tier": tier,
        "args": safe_args,
        "quick_approve_url": approve_url,
        "quick_deny_url": deny_url,
    }

    parsed = urlparse(url)
    netloc = (parsed.netloc or "").lower()

    if "discord.com" in netloc:
        if evt_type == "approval_requested" and approve_url:
            payload["content"] = f"🚨 **[AI Bridge] Approval Required: `{tool}`**"
            payload["embeds"] = [
                {
                    "title": f"Tier-{tier} Operation Requested",
                    "description": f"**Device:** `{dev_name}`\n**Tool:** `{tool}`\n**Task ID:** `{task_id}`\n**Approval ID:** `{app_id}`\n\n**Arguments:**\n```{args_json[:500]}```",
                    "color": 15158332,
                    "fields": [
                        {"name": "Quick Approve (1-Click)", "value": f"[✅ Approve via Mobile]({approve_url})", "inline": True},
                        {"name": "Quick Deny (1-Click)", "value": f"[❌ Deny via Mobile]({deny_url})", "inline": True},
                    ],
                    "footer": {"text": "AI Bridge Security Control Plane • HMAC Signed"},
                }
            ]
        else:
            payload["content"] = f"ℹ️ **[AI Bridge Notification]** `{evt_type}` on `{dev_name}`\nTool: `{tool}`\nTask: `{task_id}`"
    elif "api.telegram.org" in netloc:
        qs = parse_qs(parsed.query)
        chat_id = qs.get("chat_id", [""])[0]
        if chat_id:
            payload["chat_id"] = chat_id
        text_lines = [
            "🚨 *[AI Bridge] Approval Required*",
            f"*Tool:* `{tool}` (Tier {tier})",
            f"*Device:* `{dev_name}`",
            f"*Task ID:* `{task_id}`",
            f"*Approval ID:* `{app_id}`",
            f"*Args:*\n`{args_json[:300]}`",
        ]
        payload["text"] = "\n".join(text_lines)
        payload["parse_mode"] = "Markdown"
        if approve_url and deny_url:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve (1-Click)", "url": approve_url},
                        {"text": "❌ Deny (1-Click)", "url": deny_url},
                    ]
                ]
            }
    elif "hooks.slack.com" in netloc:
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨 AI Bridge: Approval Required"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Tool:*\n{tool} (Tier {tier})"},
                    {"type": "mrkdwn", "text": f"*Device:*\n{dev_name}"},
                    {"type": "mrkdwn", "text": f"*Task ID:*\n{task_id}"},
                    {"type": "mrkdwn", "text": f"*Approval ID:*\n{app_id}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Arguments:*\n```{args_json[:400]}```"},
            },
        ]
        if approve_url and deny_url:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary", "url": approve_url},
                        {"type": "button", "text": {"type": "plain_text", "text": "❌ Deny"}, "style": "danger", "url": deny_url},
                    ],
                }
            )
        payload["blocks"] = blocks

    # SEC-11: automatic redirect following is disabled; redirects are
    # followed manually (max 3) and every redirect target is re-validated
    # with the same DNS + blocklist check as the initial URL, so a 302 to
    # a private/loopback/link-local address is never followed.
    max_redirects = 3
    current_url = url
    current_method = "POST"
    current_json: dict | None = payload
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for hop in range(max_redirects + 1):
                if current_method == "POST":
                    resp = client.post(current_url, json=current_json, headers={"User-Agent": "AI-Bridge-Control/0.3"})
                else:
                    resp = client.get(current_url, headers={"User-Agent": "AI-Bridge-Control/0.3"})
                if resp.status_code not in (301, 302, 303, 307, 308):
                    return {"ok": resp.is_success, "status": resp.status_code}
                location = resp.headers.get("location")
                if not location:
                    return {"ok": resp.is_success, "status": resp.status_code}
                if hop == max_redirects:
                    return {"ok": False, "status": resp.status_code, "error": "Webhook redirect limit exceeded (max 3 redirects)."}
                target = urljoin(current_url, location)
                try:
                    _validate_webhook_url(target, strict_dns=True)
                except HTTPException as exc:
                    return {"ok": False, "status": resp.status_code, "error": f"Webhook redirect target rejected: {exc.detail}"}
                if resp.status_code in (301, 302, 303):
                    current_method, current_json = "GET", None
                current_url = target
            return {"ok": False, "error": "Webhook redirect limit exceeded (max 3 redirects)."}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "status": exc.response.status_code, "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def send_webhook_notification_async(
    webhook_url: str,
    event: dict,
    admin_token: str | None = None,
    public_base_url: str | None = None,
    timeout: int = 10,
) -> dict:
    """Async version of send_webhook_notification using httpx.AsyncClient."""
    return await asyncio.to_thread(send_webhook_notification, webhook_url, event, admin_token, public_base_url, timeout)


class EnrollIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    hostname: str = ""
    os: str = ""
    tenant: str | None = None
    unrestricted: bool | None = None
    policy: dict | None = None


class TaskIn(BaseModel):
    device_id: str
    tool: str
    args: dict = {}
    tenant: str | None = None
    max_attempts: int = Field(default=1, ge=1, le=5)


class ApprovalDecision(BaseModel):
    decision: str  # approved | denied
    reason: str = ""


class TaskResultIn(BaseModel):
    status: str
    result: str = ""
    epoch: int | None = None  # execution epoch from next_task claim; stale results are rejected (DIST-05/F07-04)


class LoginIn(BaseModel):
    username: str
    password: str


class StreamTicketIn(BaseModel):
    device_id: str = ""
    session_id: str = ""


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    role: str = "viewer"
    tenant: str | None = None


class PolicyIn(BaseModel):
    allow_tier3: bool = False
    desktop_enabled: bool = False
    allowed_apps: list[str] = []
    allowed_services: list[str] = []
    note: str = ""


class WebhookIn(BaseModel):
    webhook_url: str = ""


class DevicePolicyIn(BaseModel):
    inherit_global: bool = True
    unrestricted: bool | None = None
    restricted: bool | None = None
    allow_writes: bool | None = None
    allow_tier3: bool | None = None
    desktop_enabled: bool | None = None
    allow_shell: bool | None = None
    allow_package_install: bool | None = None
    allow_file_deletion: bool | None = None
    allow_clipboard: bool | None = None
    require_local_approval: bool | None = None
    allow_full_pc_access: bool | None = None
    allow_unrestricted_apps: bool | None = None
    max_tier_unattended: int | None = None
    allowed_apps: list[str] | None = None
    allowed_commands: list[str] | None = None
    allowed_services: list[str] | None = None
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    note: str = ""


class DeviceUpdateIn(BaseModel):
    name: str | None = None
    hostname: str | None = None
    os: str | None = None
    revoked: bool | None = None
    policy: dict | None = None


class DeviceRefreshIn(BaseModel):
    tools: list[str] | None = None


class TaskUpdateIn(BaseModel):
    tool: str | None = None
    args: dict | None = None
    status: str | None = None


class ApprovalUpdateIn(BaseModel):
    reason: str | None = None
    status: str | None = None


class OAuthClientUpdateIn(BaseModel):
    client_name: str | None = None
    redirect_uris: str | None = None


class WorkflowIn(BaseModel):
    name: str = "Autonomous Workflow"
    device_id: str | None = None
    steps: list[dict] = []
    stop_on_failure: bool = True
    max_retries: int = 1
    require_step_approval: bool = False
    workflow_approval_id: str | None = None  # admin approval covering the whole workflow (R-P0-11)
    tenant: str | None = None


class WorkflowRetryIn(BaseModel):
    from_failed_step: bool = True


class WorkflowTemplateIn(BaseModel):
    name: str
    description: str = ""
    steps: list[dict] = []
    tenant: str | None = None


class WorkflowScheduleIn(BaseModel):
    name: str | None = None
    interval_seconds: int = 3600
    device_id: str | None = None


class AlertConfigIn(BaseModel):
    slack_webhook: str = ""
    discord_webhook: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    generic_webhook: str = ""


class TriggerIn(BaseModel):
    name: str
    device_id: str | None = None
    metric: str = "memory_percent"
    operator: str = ">"
    threshold: float = 85.0
    action_type: str = "tool"
    action_target: str = "get_system_info"
    action_args: dict = Field(default_factory=dict)
    cooldown_seconds: int = 300
    enabled: bool = True
    tenant: str | None = None


class ScheduleIn(BaseModel):
    name: str
    device_id: str | None = "local"
    interval_seconds: int = 300
    tool: str = "get_system_info"
    args: dict = Field(default_factory=dict)
    enabled: bool = True
    tenant: str | None = None


class FleetBroadcastIn(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    target_os: str | None = None
    target_device_ids: list[str] | None = None
    tenant: str | None = None


class SupervisorHandoffIn(BaseModel):
    device_id: str
    goal: str = ""
    tenant: str | None = None
    profile: str = "code"
    mode: str = "gated"


class SupervisorRecoverIn(BaseModel):
    role: str
    device_id: str | None = None


class DesktopInputIn(BaseModel):
    action: str = "click"
    x: float | None = None
    y: float | None = None
    normalized: bool = True
    button: str = "left"
    clicks: int = 1
    text: str | None = None
    key: str | None = None
    delta: int = -120
    screen_width: int = 1920
    screen_height: int = 1080


WORKFLOW_TEMPLATES = [
    {
        "id": "web_navigation_search",
        "name": "🌐 Web Navigation & OCR Verification",
        "description": "Opens browser, waits for load, navigates to target URL, and verifies header text.",
        "steps": [
            {"name": "Launch Browser", "action": "launch_approved_application", "args": {"app_name": "chrome"}},
            {"name": "Wait for Browser", "action": "wait_for_text", "args": {"text": "Google", "timeout_seconds": 10}},
            {"name": "Press Shortcut to Address Bar", "action": "press_hotkey", "args": {"combo": "ctrl+l"}},
            {"name": "Type Destination URL", "action": "keyboard_type", "args": {"text": "https://news.ycombinator.com\n"}},
            {"name": "Verify Site Loaded", "action": "wait_for_text", "args": {"text": "Hacker News", "timeout_seconds": 12}},
            {"name": "Capture Final Screenshot", "action": "take_screenshot", "args": {"quality": 80}},
        ],
    },
    {
        "id": "app_automation_doc",
        "name": "📝 App Launch & Document Generation",
        "description": "Opens Notepad, types formatted notes, triggers save shortcut, and verifies save dialog.",
        "steps": [
            {"name": "Open Notepad", "action": "launch_approved_application", "args": {"app_name": "notepad"}},
            {"name": "Wait for Notepad", "action": "wait_for_text", "args": {"text": "File", "timeout_seconds": 8}},
            {"name": "Type Document Content", "action": "keyboard_type", "args": {"text": "AI Bridge Autonomous Workflow Execution Report\nAll steps verified successfully.\n"}},
            {"name": "Trigger Save Hotkey", "action": "press_hotkey", "args": {"combo": "ctrl+s"}},
            {"name": "Verify Save Dialog", "action": "wait_for_text", "args": {"text": "Save", "timeout_seconds": 5}},
        ],
    },
    {
        "id": "fleet_health_diagnostics",
        "name": "🩺 Fleet Health & Telemetry Verification",
        "description": "Gathers system telemetry, lists open windows, checks desktop status, and confirms fleet health.",
        "steps": [
            {"name": "Check System Info", "action": "get_system_info", "args": {}},
            {"name": "Check Resource Usage", "action": "get_resource_usage", "args": {}},
            {"name": "List Active Desktop Windows", "action": "list_desktop_windows", "args": {}},
            {"name": "Enumerate UI Elements", "action": "list_ui_elements", "args": {}},
        ],
    },
]


def _render_quick_decision_html(
    title: str,
    badge_class: str,
    badge_icon: str,
    badge_text: str,
    heading: str,
    message: str,
    details: dict | None = None,
) -> str:
    details_rows = ""
    if details:
        for k, v in details.items():
            details_rows += f'<div class="row"><span class="label">{k}</span><span class="value">{v}</span></div>'
        details_html = f'<div class="details-card">{details_rows}</div>'
    else:
        details_html = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>{title} • AI Bridge</title>
  <style>
    :root {{
      --bg: #0f141c;
      --card-bg: rgba(26, 34, 46, 0.94);
      --card-border: rgba(255, 255, 255, 0.12);
      --text: #f3f6fa;
      --text-muted: #94a3b8;
      --accent: #0078d4;
      --success: #107c41;
      --success-bg: rgba(16, 124, 65, 0.18);
      --danger: #d83b01;
      --danger-bg: rgba(216, 59, 1, 0.18);
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", Roboto, sans-serif;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #f5f7fa;
        --card-bg: rgba(255, 255, 255, 0.96);
        --card-border: rgba(0, 0, 0, 0.1);
        --text: #1e293b;
        --text-muted: #64748b;
        --accent: #0067b8;
        --success: #107c41;
        --success-bg: #dff6dd;
        --danger: #c42b1c;
        --danger-bg: #fde7e9;
      }}
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: var(--font);
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px 16px;
      -webkit-font-smoothing: antialiased;
    }}
    .container {{
      max-width: 440px;
      width: 100%;
      background: var(--card-bg);
      backdrop-filter: blur(24px);
      -webkit-backdrop-filter: blur(24px);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      padding: 32px 24px;
      box-shadow: 0 16px 40px rgba(0,0,0,0.3);
      text-align: center;
      animation: popIn 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }}
    @keyframes popIn {{
      from {{ opacity: 0; transform: scale(0.96); }}
      to {{ opacity: 1; transform: scale(1); }}
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 13px;
      letter-spacing: 0.04em;
      margin-bottom: 20px;
    }}
    .badge.approved {{ background: var(--success-bg); color: var(--success); border: 1px solid var(--success); }}
    .badge.denied {{ background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger); }}
    .badge.info {{ background: rgba(0, 120, 212, 0.15); color: var(--accent); border: 1px solid var(--accent); }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 10px; letter-spacing: -0.02em; }}
    p.subtitle {{ color: var(--text-muted); font-size: 14px; line-height: 1.5; margin-bottom: 24px; }}
    p.subtitle b {{ color: var(--text); }}
    p.subtitle code {{ background: rgba(125,125,125,0.2); padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
    .details-card {{
      background: rgba(125, 125, 125, 0.08);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 24px;
      text-align: left;
      font-size: 13px;
    }}
    .row {{ display: flex; justify-content: space-between; align-items: center; padding: 7px 0; border-bottom: 1px solid var(--card-border); }}
    .row:last-child {{ border-bottom: none; }}
    .label {{ color: var(--text-muted); font-weight: 500; }}
    .value {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-weight: 600; word-break: break-all; }}
    .actions {{ display: flex; flex-direction: column; gap: 10px; }}
    .btn {{
      display: block;
      width: 100%;
      padding: 12px;
      border-radius: 10px;
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      text-align: center;
      transition: filter 0.15s ease;
    }}
    .btn-primary {{ background: var(--accent); color: #fff; }}
    .btn-primary:hover {{ filter: brightness(1.1); }}
    .footer-note {{ font-size: 11px; color: var(--text-muted); margin-top: 20px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="badge {badge_class}">
      <span>{badge_icon}</span>
      <span>{badge_text}</span>
    </div>
    <h1>{heading}</h1>
    <p class="subtitle">{message}</p>
    {details_html}
    <div class="actions">
      <a href="/" class="btn btn-primary">Open AI Bridge Dashboard</a>
    </div>
    <div class="footer-note">
      Cryptographically signed via HMAC-SHA256 • AI Bridge Control Plane
    </div>
  </div>
</body>
</html>
"""


def create_app(
    *,
    admin_token: str | None = None,
    db_path: Path | None = None,
    database_url: str | None = None,
    dashboard_dist_dir: str | Path | None = None,
) -> FastAPI:
    # Release 2: enable structured JSON logging automatically outside pytest
    # (so Gunicorn workers emit NDJSON logs with correlation IDs by default).
    if "pytest" not in __import__("sys").modules and os.environ.get("AI_BRIDGE_CONFIGURE_LOGGING", "1").strip().lower() not in ("0", "false", "no"):
        try:
            from ..logging_config import configure_logging

            configure_logging()
        except Exception:
            pass

    token = admin_token or get_secret("AI_BRIDGE_ADMIN_TOKEN") or os.environ.get("AI_BRIDGE_ADMIN_TOKEN")
    data_dir = Path(os.environ.get("AI_BRIDGE_DATA_DIR", str(Path.home() / ".ai-bridge")))
    db_path = db_path or (data_dir / "control.db")
    conn = dbmod.connect(db_path=db_path, database_url=database_url)
    from . import production_readiness as _prodready_boot

    _prodready_boot.enforce_multi_worker_redis(conn)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            loop = asyncio.get_running_loop()
            agent_channel.bind_event_loop(loop)
            agent_channel.start_redis_fanout(loop)
        except Exception:
            pass
        try:
            yield
        finally:
            _app.state._running = False
            task = getattr(agent_channel, "_fanout_task", None)
            if task is not None and not task.done():
                task.cancel()

    app = FastAPI(title="AI Bridge Control Plane", version="0.3.0", lifespan=_lifespan)
    # F-05-05 (Wave 2A): the legacy shared admin token is OFF by default.
    # It is accepted as an auth credential only when explicitly passed as
    # the admin_token argument (explicit configuration, e.g. tests) or
    # when the operator opts in via AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN=1.
    # The AI_BRIDGE_ADMIN_TOKEN env var alone no longer enables it;
    # session-based admin users are the supported admin path. The env
    # token still feeds the quick-approval HMAC (make_quick_sig).
    app.state.legacy_admin_enabled = bool(admin_token is not None or os.environ.get("AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN", "").strip().lower() in ("1", "true", "yes"))
    if app.state.legacy_admin_enabled and admin_token is None:
        print(
            "WARNING: legacy shared admin token enabled via AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN=1; prefer session-based admin users.",
            flush=True,
        )
    # R-P0-06 wiring (Wave 2A): point the MCP audit writer at the
    # control-plane DB by default so MCP tool calls land in the
    # hash-chained audit table. An operator-set AI_BRIDGE_MCP_AUDIT_DB
    # is never overridden (setdefault).
    app.state.db_path = db_path
    try:
        from . import oidc as _oidcmod

        _oidcmod.bind_oidc_store(conn)
    except Exception:
        pass
    try:
        from ..connectors import microsoft365 as _m365mod

        _m365mod.bind_pending_store(conn)
    except Exception:
        pass
    os.environ.setdefault("AI_BRIDGE_MCP_AUDIT_DB", str(db_path))
    # F-05-02 (Wave 2A): CSRF protection is ON by default for
    # session-authenticated mutating requests. Opt out explicitly with
    # AI_BRIDGE_CSRF_DISABLED=1 (not recommended). The shared secret
    # comes from the secrets backend (control/csrf.py).
    if os.environ.get("AI_BRIDGE_CSRF_DISABLED", "").strip().lower() not in ("1", "true", "yes"):
        # F-05-02 (Wave 6): fail fast at boot when the plane runs with
        # more than one worker and no shared CSRF secret is configured.
        # Without this, tokens minted on one worker silently fail on the
        # others (observed live in Wave 5E). Single-worker deployments
        # keep the per-process random fallback.
        from .csrf import resolve_csrf_secret

        resolve_csrf_secret()
        app.middleware("http")(csrf_middleware)
    # Browser dashboards call the API cross-origin; allow_credentials=True
    # requires an explicit origin list (not "*"). Extend via
    # AI_BRIDGE_CORS_ORIGINS / AI_BRIDGE_DASHBOARD_URL. F-05-04 (Wave 5A):
    # wildcard methods/headers with credentials are gone — the allowlist
    # below is the full set the dashboard and agents need. The bare-IP
    # origin (http://201.79.36.129) and the placeholder
    # https://your-aws-domain.com were removed: credentials must never
    # be sent to a plaintext bare-IP origin, and a placeholder origin
    # trusts a domain nobody owns. Named production origins below are
    # the deployed dashboard hosts; any other origin must be added
    # explicitly via AI_BRIDGE_CORS_ORIGINS.
    from . import production_readiness as _prodready_cors

    allowed_origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "https://jxiptvplayer.app",
        "https://ai-bridge-dashboard.onrender.com",
        _prodready_cors.env_public_base_url() or "",
        os.environ.get("AI_BRIDGE_DASHBOARD_URL", ""),
    ]
    for origin in os.environ.get("AI_BRIDGE_CORS_ORIGINS", "").split(","):
        allowed_origins.append(origin.strip())
    allowed_origins = [o for o in dict.fromkeys(allowed_origins) if o]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Admin-Token",
            "X-Session-Token",
            "X-Device-Token",
            "X-CSRF-Token",
            "X-Client-Cert-Fingerprint",
            "X-SSL-Client-Fingerprint",
            "X-Correlation-ID",
            "X-Request-ID",
        ],
        expose_headers=["X-Correlation-ID", "X-Total-Count"],
    )
    app.state.conn = conn
    app.state.admin_token = token
    app.state.emergency_stop = False
    from . import plane_state, role_catalog
    from .v1_routes import bind_control_connection, hydrate_human_takeover_from_db

    plane_state.sync_emergency_stop_to_app(conn, app.state)
    # SEC-06 (Wave 5A): re-encrypt any legacy plaintext MCP OAuth
    # secrets left over from before Fernet encryption landed. Best
    # effort and idempotent: a missing token key (or any failure) is
    # logged and never fails boot — the plaintext rows stay readable
    # until an operator runs scripts/migrate_mcp_secrets_to_fernet.py
    # with a key configured.
    try:
        from . import mcp_oauth as _mcp_oauth_boot

        _mig = _mcp_oauth_boot.migrate_plaintext_secrets(conn)
        if _mig["total"]:
            logging.getLogger("ai_bridge.control.app").info("migrated %d plaintext MCP OAuth secret(s) to Fernet", _mig["total"])
    except Exception as _mig_exc:
        logging.getLogger("ai_bridge.control.app").warning("plaintext MCP secret migration skipped: %s", _mig_exc)
    hydrate_human_takeover_from_db(conn)
    bind_control_connection(conn)
    role_catalog.seed_role_catalog(conn)
    task_cond = threading.Condition()
    app.state.task_cond = task_cond

    def notify_tasks(device_id: str | None = None, task_id: str | None = None, *, cancel: bool = False) -> None:
        with task_cond:
            task_cond.notify_all()
        if not device_id:
            return
        if cancel and task_id:
            agent_channel.notify_cancel(device_id, task_id)
        elif not cancel:
            agent_channel.notify_task_available(device_id, task_id)

    def get_webhook_url() -> str:
        row = conn.execute("SELECT value FROM settings WHERE key='webhook_url'").fetchone()
        if row is not None and row["value"] is not None:
            return str(row["value"]).strip()
        return os.environ.get("AI_BRIDGE_WEBHOOK_URL", "").strip()

    def get_public_base_url(req: Request | None = None) -> str:
        from . import production_readiness as prodready

        row = conn.execute("SELECT value FROM settings WHERE key='public_url'").fetchone()
        if row is not None and row["value"]:
            return str(row["value"]).strip().rstrip("/")
        env_url = prodready.env_public_base_url()
        if env_url:
            return env_url
        if req is not None:
            host = req.headers.get("x-forwarded-host") or req.headers.get("host")
            proto = req.headers.get("x-forwarded-proto") or req.url.scheme
            if host:
                return f"{proto}://{host}".rstrip("/")
        return "https://jxiptvplayer.app"

    def _auth_context_full(x_admin_token=None, x_session_token=None, authorization=None, admin_token=None):
        """Return (actor, role, tenant, is_legacy_admin).

        Session tokens outrank the legacy admin token. The tenant is the
        session user's tenant (R-P0-12); the legacy admin token is a
        global-admin credential and reports tenant "default" — callers
        using it may address any tenant explicitly via
        resolve_request_tenant. When admin_token is None the resolved
        control-plane token is used, matching require_role's behaviour.
        """
        bearer = None
        if authorization and authorization.strip().lower().startswith("bearer "):
            bearer = authorization.strip()[7:].strip()
        eff_session = x_session_token or bearer
        eff_admin = x_admin_token or bearer
        eff_admin_token = admin_token if admin_token is not None else token
        if eff_session:
            row = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (dbmod.hash_token(eff_session),)).fetchone()
            if row is not None and row["expires_at"] >= dbmod.now():
                user = conn.execute("SELECT * FROM users WHERE username=? AND disabled=0", (row["username"],)).fetchone()
                if user is not None:
                    return user["username"], user["role"], task_service_mod.user_tenant(conn, user["username"]), False
        if eff_admin_token and app.state.legacy_admin_enabled and eff_admin and hmac.compare_digest(eff_admin, eff_admin_token):
            return "admin-token", "admin", "default", True
        raise HTTPException(status_code=401, detail="Authentication required.")

    def auth_context(x_admin_token=None, x_session_token=None, authorization=None, admin_token=None):
        """Return (actor, role). Session tokens outrank the legacy admin token."""
        actor, role, _tenant, _is_legacy = _auth_context_full(x_admin_token, x_session_token, authorization, admin_token=admin_token)
        return actor, role

    def session_tenant_for_request(x_admin_token=None, x_session_token=None, authorization=None, admin_token=None):
        """(session tenant, is_legacy_admin) for the caller of this request."""
        _actor, _role, tenant, is_legacy = _auth_context_full(x_admin_token, x_session_token, authorization, admin_token=admin_token)
        return tenant, is_legacy

    def resolve_request_tenant(requested, x_admin_token=None, x_session_token=None, authorization=None, admin_token=None):
        """Effective tenant for a list/read/write (R-P0-12).

        Session users are strictly scoped to their own tenant; a
        caller-supplied tenant that differs is rejected (403). The legacy
        admin token may address any tenant explicitly.
        """
        tenant, is_legacy = session_tenant_for_request(x_admin_token, x_session_token, authorization, admin_token=admin_token)
        return task_service_mod.resolve_tenant(requested, session_tenant=tenant, is_legacy_admin=is_legacy)

    def _row_tenant(row):
        """Tenant of a DB row, tolerating rows without a tenant column."""
        if row is None:
            return "default"
        try:
            return str(dict(row).get("tenant") or "default")
        except Exception:
            return "default"

    def _require_row_tenant(row, x_admin_token=None, x_session_token=None, authorization=None, admin_token=None):
        """404 unless the row's tenant is visible to the caller (R-P0-12).

        Cross-tenant single-resource reads return 404 so the endpoint does
        not oracle the existence of another tenant's rows.
        """
        tenant, is_legacy = session_tenant_for_request(x_admin_token, x_session_token, authorization, admin_token=admin_token)
        if is_legacy:
            return
        if _row_tenant(row) != tenant:
            raise HTTPException(status_code=404, detail="Not found.")

    def _require_device_tenant(device_id: str, x_admin_token=None, x_session_token=None, authorization=None):
        """404 unless the device row's tenant is visible to the caller (R-P0-12).

        Skips the pseudo-device "local" (the control-plane host itself, which
        is not a tenant-scoped row; local execution is governed by the R-P0-03
        local-execution gate instead). A missing row is left to the handler's
        own not-found handling; an existing row in another tenant 404s.
        """
        if device_id == "local":
            return
        row = conn.execute("SELECT * FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
        if row is None:
            return
        _require_row_tenant(row, x_admin_token, x_session_token, authorization)

    def require_role(min_role: str, x_admin_token: str | None = None, x_session_token: str | None = None, authorization: str | None = None, admin_token: str | None = None):
        actor, role = auth_context(x_admin_token, x_session_token, authorization, admin_token=admin_token)
        if authmod.ROLES.get(role, 0) < authmod.ROLES[min_role]:
            raise HTTPException(status_code=403, detail=f"Role '{role}' cannot perform this action (requires {min_role}).")
        return actor

    def require_admin(x_admin_token: str | None = None, x_session_token: str | None = None, authorization: str | None = None, admin_token: str | None = None):
        return require_role("admin", x_admin_token, x_session_token, authorization, admin_token=admin_token)

    def get_current_policy() -> dict:
        row = conn.execute("SELECT policy_json FROM policies ORDER BY version DESC LIMIT 1").fetchone()
        if row is not None:
            import json as _json

            return _json.loads(row["policy_json"])
        return {
            "allow_tier3": os.environ.get("AI_BRIDGE_ALLOW_TIER3", "0") == "1",
            "desktop_enabled": os.environ.get("AI_BRIDGE_DESKTOP_ENABLED", "0") == "1",
            "allowed_apps": [],
            "allowed_services": [],
        }

    def get_effective_device_policy(device_row_or_id) -> dict:
        global_p = get_current_policy()
        row = device_row_or_id if not isinstance(device_row_or_id, str) else conn.execute("SELECT * FROM devices WHERE id=?", (device_row_or_id,)).fetchone()
        if row is None:
            return {**global_p, "has_custom_policy": False, "_source": "global", "denied_tools": []}
        raw_p = ""
        try:
            raw_p = row["policy_json"]
        except Exception:
            pass
        custom: dict = {}
        if raw_p:
            try:
                parsed_custom = json.loads(raw_p) if isinstance(raw_p, str) else raw_p
                if isinstance(parsed_custom, dict):
                    custom = {k: v for k, v in parsed_custom.items() if v is not None}
            except Exception:
                custom = {}

        is_unrestricted = bool(custom.get("unrestricted", False) or custom.get("allow_full_pc_access", False))
        if is_unrestricted:
            return {
                "inherit_global": False,
                "unrestricted": True,
                "unrestricted_granted_by": custom.get("unrestricted_granted_by"),
                "allow_writes": True,
                "allow_tier3": True,
                "desktop_enabled": True,
                "allow_shell": True,
                "allow_package_install": True,
                "allow_file_deletion": True,
                "allow_clipboard": bool(custom.get("allow_clipboard", True)),
                "require_local_approval": bool(custom.get("require_local_approval", False)),
                "allow_full_pc_access": True,
                "allow_unrestricted_apps": True,
                "max_tier_unattended": int(custom.get("max_tier_unattended") or 4),
                "allowed_apps": custom.get("allowed_apps") or ["*"],
                "allowed_commands": custom.get("allowed_commands") or [],
                "allowed_services": custom.get("allowed_services") or ["*"],
                "allowed_test_commands": ["*"],
                "denied_tools": custom.get("denied_tools") or [],
                "allowed_tools": custom.get("allowed_tools") or None,
                "has_custom_policy": True,
                "_source": "linux_unrestricted_server",
                "device_policy": custom,
                "note": custom.get("note") or "Unrestricted Linux Server Policy (No Policy Restrictions)",
            }

        if not custom:
            return {
                **global_p,
                "allow_shell": bool(global_p.get("allow_shell", False)),
                "allow_package_install": bool(global_p.get("allow_package_install", False)),
                "allow_file_deletion": bool(global_p.get("allow_file_deletion", False)),
                "allow_clipboard": bool(global_p.get("allow_clipboard", True)),
                "require_local_approval": bool(global_p.get("require_local_approval", False)),
                "allow_full_pc_access": False,
                "allow_unrestricted_apps": False,
                "allowed_commands": list(global_p.get("allowed_commands") or []),
                "has_custom_policy": False,
                "_source": "global",
                "denied_tools": [],
            }

        inherit = bool(custom.get("inherit_global", True))
        merged = (
            dict(global_p)
            if inherit
            else {
                "allow_writes": True,
                "allow_tier3": False,
                "desktop_enabled": False,
                "allow_shell": False,
                "allow_package_install": False,
                "allow_file_deletion": False,
                "allow_clipboard": True,
                "require_local_approval": False,
                "allow_full_pc_access": False,
                "allow_unrestricted_apps": False,
                "allowed_apps": [],
                "allowed_commands": [],
                "allowed_services": [],
                "allowed_test_commands": ["pytest", "npm test", "go test"],
                "max_tier_unattended": 2,
            }
        )
        merged.setdefault("allow_shell", bool(global_p.get("allow_shell", False)))
        merged.setdefault("allow_package_install", bool(global_p.get("allow_package_install", False)))
        merged.setdefault("allow_file_deletion", bool(global_p.get("allow_file_deletion", False)))
        merged.setdefault("allow_clipboard", True)
        merged.setdefault("require_local_approval", False)
        merged.setdefault("allow_full_pc_access", False)
        merged.setdefault("allow_unrestricted_apps", False)
        merged.setdefault("allowed_commands", [])
        for k in [
            "allow_writes",
            "allow_tier3",
            "desktop_enabled",
            "max_tier_unattended",
            "allow_file_deletion",
            "allow_shell",
            "allow_package_install",
            "allow_clipboard",
            "require_local_approval",
            "allow_full_pc_access",
            "allow_unrestricted_apps",
        ]:
            if k in custom and custom[k] is not None:
                merged[k] = custom[k]

        if custom.get("allowed_apps") is not None:
            merged["allowed_apps"] = custom["allowed_apps"]
        if custom.get("allowed_commands") is not None:
            merged["allowed_commands"] = custom["allowed_commands"]
        if custom.get("allowed_services") is not None:
            merged["allowed_services"] = custom["allowed_services"]

        denied = set(custom.get("denied_tools") or [])
        if custom.get("allow_shell") is False:
            denied.add("run_shell")
        if custom.get("allow_package_install") is False:
            denied.add("install_package")
        if custom.get("allow_file_deletion") is False:
            denied.add("delete_workspace_file")
        if custom.get("desktop_enabled") is False:
            denied.update(
                [
                    "capture_desktop_screenshot",
                    "take_screenshot",
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
                ]
            )
        if custom.get("allow_writes") is False:
            denied.update(["create_workspace_file", "edit_workspace_file", "move_workspace_file", "delete_workspace_file"])

        merged["denied_tools"] = sorted(list(denied))
        if custom.get("allowed_tools") is not None:
            merged["allowed_tools"] = custom["allowed_tools"]

        merged["has_custom_policy"] = True
        merged["_source"] = "device"
        merged["device_policy"] = custom
        return merged

    _POLICY_ESCALATION_FLAGS = (
        "unrestricted",
        "allow_writes",
        "allow_tier3",
        "desktop_enabled",
        "allow_shell",
        "allow_package_install",
        "allow_file_deletion",
    )

    def reject_policy_widening(effective: dict, proposed: dict) -> None:
        """A device may only narrow the policy an administrator gave it.

        The control plane sends this stored policy back down to the agent, so
        without this check a device token could set unrestricted=True (or
        raise max_tier_unattended to 4) and permanently escape the tier,
        approval and path-confinement checks.
        """
        current = effective or {}
        for key in _POLICY_ESCALATION_FLAGS:
            if proposed.get(key) is True and not current.get(key):
                raise HTTPException(status_code=403, detail=f"Device tokens cannot enable '{key}'; an administrator must.")
        ceiling = proposed.get("max_tier_unattended")
        if ceiling is not None and int(ceiling) > int(current.get("max_tier_unattended") or 0):
            raise HTTPException(status_code=403, detail="Device tokens cannot raise 'max_tier_unattended'; an administrator must.")
        for key in ("allowed_apps", "allowed_services", "allowed_tools"):
            wanted = proposed.get(key)
            if wanted is None:
                continue
            current_list = current.get(key)
            if current_list is None and key == "allowed_tools":
                continue  # previously unrestricted; any explicit list narrows it
            if not set(wanted) <= set(current_list or []):
                raise HTTPException(status_code=403, detail=f"Device tokens cannot add entries to '{key}'; an administrator must.")
        denied = proposed.get("denied_tools")
        if denied is not None and not set(current.get("denied_tools") or []) <= set(denied):
            raise HTTPException(status_code=403, detail="Device tokens cannot remove entries from 'denied_tools'.")

    device_ttl_days = int(os.environ.get("AI_BRIDGE_DEVICE_TOKEN_TTL_DAYS", "7"))

    def device_row(x_device_token: str | None):
        if not x_device_token:
            raise HTTPException(status_code=401, detail="Device token required.")
        h = dbmod.hash_token(x_device_token)
        # 401 split (Wave 4): unknown vs revoked are distinguishable so
        # the agent can tell a terminal revocation from a transient /
        # rotation-race unknown. The old combined "Unknown or revoked
        # device token." detail is gone. Agent side
        # (agents/common/poller.py classify_auth_failure): "revoked" in
        # the text -> terminal DeviceRevokedError; the unknown text has
        # no "revok"/"expir" marker -> "transient" (retried, and the
        # saved-token retry is exhausted first, as before).
        row = conn.execute("SELECT * FROM devices WHERE token_hash=?", (h,)).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Unknown device token.")
        if row["revoked"] or row["status"] == "revoked":
            raise HTTPException(status_code=401, detail="Device token revoked.")
        if row["token_expires_at"] is not None and row["token_expires_at"] < dbmod.now():
            raise HTTPException(status_code=401, detail="Device token expired; rotate it via /api/agent/rotate before it expires, or re-enroll.")
        return row

    def _require_agent_mtls(request, device_id, x_client_cert_fingerprint=None, x_ssl_client_fingerprint=None):
        """F-05-06: mTLS fingerprint check on agent routes.

        No-op unless AI_BRIDGE_DEVICE_MTLS=1; when enabled the device
        must present the pinned client-cert fingerprint (direct TLS or
        a trusted edge that strips/validates the proxy headers).
        """
        from . import device_mtls

        device_mtls.require_device_mtls(
            conn,
            device_id,
            device_mtls.extract_request_fingerprint(
                request,
                x_client_cert_fingerprint=x_client_cert_fingerprint,
                x_ssl_client_fingerprint=x_ssl_client_fingerprint,
            ),
        )

    def audit(actor: str, device_id: str, tool: str, args: dict, decision: str, result: str = "", tenant: str = "default"):
        # Gate 9 (Wave 6): the control-plane audit closure routes through
        # the single audit writer on the app's live connection — no direct
        # db.audit call sites remain outside audit_writer itself.
        from ..audit_writer import write_audit_event

        write_audit_event(
            actor=actor,
            action=tool,
            device_id=device_id,
            details=args,
            conn=conn,
            tenant=tenant,
            decision=decision,
            result=result,
        )

    from . import dispatch_audit as dispatch_auditmod

    def _record_dispatch_audit(tool: str, args: dict, _result: object, device_id: str) -> None:
        audit("dispatch", device_id or "", tool, args, "executed")

    dispatch_auditmod.set_dispatch_audit_callback(_record_dispatch_audit)

    from .workspace import install_workspace_routes

    install_workspace_routes(app, conn, require_role, audit)

    def _install_asset(relative: str) -> Path:
        """Resolve installer scripts from the repo (or packaged) tree."""
        here = Path(__file__).resolve()
        candidates = [
            here.parents[3] / relative,  # repo root when running from source
            here.parents[2] / relative,  # src/ layout fallback
            Path.cwd() / relative,
        ]
        for path in candidates:
            if path.is_file():
                return path
        raise HTTPException(status_code=404, detail=f"Installer not found: {relative}")

    app.state.device_live_meta = {}

    def _get_devices_live_meta(dev_ids: list) -> dict:
        """Live telemetry enrichment for many devices with ONE tasks query.

        Wave 6A (W5E-01): the old per-device ``_get_device_live_meta``
        ran up to 2 task-history queries per device behind the single DB
        lock, so ``GET /api/devices`` did ~2N queries and degraded to
        ~3.7 s/request at 20 concurrent VUs. This runs a single window-
        function query for the latest completed ``get_resource_usage`` /
        ``get_network_info`` result per (device, tool) across all ids that
        still need the data, then applies the same parsing as before.
        """
        metas: dict = {}
        need_resource: list = []
        need_network: list = []
        for dev_id in dev_ids:
            meta = dict(app.state.device_live_meta.get(dev_id) or {})
            metas[dev_id] = meta
            if not meta.get("memory_total_gb") or not meta.get("disk_total_gb"):
                need_resource.append(dev_id)
            if not meta.get("ip_address"):
                need_network.append(dev_id)
        wanted = sorted(set(need_resource) | set(need_network))
        if wanted:
            try:
                placeholders = ",".join("?" for _ in wanted)
                rows = conn.execute(
                    "SELECT device_id, tool, result FROM ("
                    "SELECT device_id, tool, result, "
                    "ROW_NUMBER() OVER (PARTITION BY device_id, tool "
                    "ORDER BY updated_at DESC) AS rn FROM tasks "
                    "WHERE device_id IN (" + placeholders + ") "
                    "AND tool IN ('get_resource_usage', 'get_network_info') "
                    "AND status='completed') WHERE rn = 1",
                    tuple(wanted),
                ).fetchall()
                for row in rows:
                    dev_id, tool, result = row["device_id"], row["tool"], row["result"]
                    meta = metas.get(dev_id)
                    if meta is None or not result:
                        continue
                    try:
                        data = json.loads(result)
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if tool == "get_resource_usage":
                        if data.get("memory_total_bytes") and not meta.get("memory_total_gb"):
                            meta["memory_total_gb"] = round(float(data["memory_total_bytes"]) / (1024**3), 1)
                        if data.get("disk_total_bytes") and not meta.get("disk_total_gb"):
                            meta["disk_total_gb"] = round(float(data["disk_total_bytes"]) / (1024**3), 1)
                        if data.get("uptime_seconds") and not meta.get("uptime_hours"):
                            meta["uptime_hours"] = round(float(data["uptime_seconds"]) / 3600.0, 1)
                    elif tool == "get_network_info":
                        if not meta.get("ip_address"):
                            for iface in data.get("interfaces") or []:
                                addr = iface.get("ip") or iface.get("address")
                                if addr and not str(addr).startswith("127."):
                                    meta["ip_address"] = str(addr)
                                    break
            except Exception:
                pass
        return metas

    def _get_device_live_meta(dev_id: str) -> dict:
        return _get_devices_live_meta([dev_id]).get(dev_id, {})

    def expire_pending_approvals() -> None:
        now = dbmod.now()
        rows = conn.execute("SELECT id, task_id FROM approvals WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?", (now,)).fetchall()
        for row in rows:
            conn.execute("UPDATE approvals SET status='expired', decided_by='system', decided_at=? WHERE id=? AND status='pending'", (now, row["id"]))
            if row["task_id"]:
                conn.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status='needs_approval'", (now, row["task_id"]))
            audit("system", "", "expire_approval", {"approval_id": row["id"]}, "expired")
        if rows:
            conn.commit()

    def approval_detail(row) -> dict:
        data = dict(row)
        if data.get("task_id"):
            task = conn.execute("SELECT device_id, tenant, tool, args_json, tier FROM tasks WHERE id=?", (data["task_id"],)).fetchone()
            if task:
                data.update({"device_id": task["device_id"], "tenant": task["tenant"], "tool": task["tool"], "args": json.loads(task["args_json"] or "{}"), "tier": task["tier"]})
                device = conn.execute("SELECT name FROM devices WHERE id=?", (task["device_id"],)).fetchone()
                data["device_name"] = device["name"] if device else None
        return data

    # --- Autonomous Workflows & Multi-Turn Plan Execution -----------------

    _PLANE_SUPERVISOR_TOOLS = frozenset(
        {
            "supervisor_enqueue_handoff",
            "supervisor_recover_role_tasks",
            "supervisor_delegate_role",
            "agent_run_role",
        }
    )

    def _execute_workflow_step_sync(
        action: str,
        args: dict,
        target_device_id: str | None,
        tenant: str,
        actor: str,
        *,
        workflow_approved: bool = False,
        step_approved: bool = False,
        workflow_approval_id: str | None = None,
        step_approval_id: str | None = None,
    ) -> dict:
        # R-P0-11: steps run with the WORKFLOW OWNER's policy, never admin.
        # Tier >=3 steps need a recorded approval (per-step, or a whole-
        # workflow approval by an admin) unless the owner's policy itself
        # opts into Tier 3. SEC-08: human takeover pauses autonomous steps.
        if plane_state.is_emergency_stop(conn, app.state):
            raise PermissionError("Emergency stop is active; workflow execution is halted.")
        action = normalize_tool_name(action)
        granted = bool(workflow_approved or step_approved)
        if target_device_id and target_device_id != "local" and plane_state.is_human_takeover_active(conn, target_device_id):
            raise PermissionError(f"Human takeover is active on device '{target_device_id}'; workflow steps are paused.")
        # Supervisor / role-orchestration tools must run on the control plane DB.
        if action in _PLANE_SUPERVISOR_TOOLS:
            from . import supervisor as _sup

            plane_args = dict(args or {})
            if target_device_id and not plane_args.get("device_id") and not plane_args.get("device"):
                plane_args["device_id"] = target_device_id
            if tenant and not plane_args.get("tenant"):
                plane_args["tenant"] = tenant
            tok = _sup.set_plane_conn(conn)
            try:
                if action == "agent_run_role":
                    return _sup.enqueue_agent_run_role(
                        conn,
                        device_id=str(plane_args.get("device_id") or target_device_id or "local"),
                        tenant=str(plane_args.get("tenant") or tenant or "default"),
                        role=str(plane_args.get("role") or ""),
                        prompt=str(plane_args.get("prompt") or ""),
                        parent_task_id=plane_args.get("parent_task_id") or plane_args.get("_parent_task_id"),
                        handoff_id=plane_args.get("handoff_id") or plane_args.get("_handoff_id"),
                        goal=plane_args.get("goal") or plane_args.get("_goal"),
                    )
                owner_policy = task_service_mod.policy_from_plane_dict(get_current_policy())
                return dispatch(action, plane_args, policy=owner_policy, approval_granted=granted, is_admin=granted)
            finally:
                _sup.reset_plane_conn(tok)
        if target_device_id and target_device_id != "local":
            dev_row = conn.execute(
                "SELECT * FROM devices WHERE id=? AND revoked=0 AND tenant=?",
                (target_device_id, tenant),
            ).fetchone()
            if dev_row:
                eff_pol = get_effective_device_policy(dev_row)
                if action in eff_pol.get("denied_tools", []):
                    raise PermissionError(f"Tool '{action}' is denied by policy for device '{dev_row['name']}'.")
                if eff_pol.get("allowed_tools") is not None and action not in eff_pol["allowed_tools"]:
                    raise PermissionError(f"Tool '{action}' is not in allowed_tools for device '{dev_row['name']}'.")
                # Route through the ONE task-creation service so the approval
                # gate, tenant scoping and takeover check apply here too. A
                # recorded step/workflow approval is presented, not re-asked.
                approval_id = step_approval_id if step_approved else (workflow_approval_id if workflow_approved else None)
                created = task_service_mod.create_task(
                    conn,
                    actor=actor,
                    role="operator",
                    tenant=tenant,
                    device_id=target_device_id,
                    tool=action,
                    args=args,
                    approval_id=approval_id,
                    human_initiated=False,
                    device_row=dev_row,
                    get_effective_device_policy=get_effective_device_policy,
                    get_current_policy=get_current_policy,
                    notify=notify_tasks,
                    audit_fn=audit,
                )
                if created["status"] == "needs_approval":
                    raise PermissionError(f"Workflow step '{action}' requires human approval (approval {created['approval_id']}); step not executed.")
                task_id = created["task_id"]
                deadline = time.time() + 45.0
                while time.time() < deadline:
                    time.sleep(0.3)
                    t_row = conn.execute("SELECT status, result FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if t_row and t_row["status"] in ("completed", "failed", "cancelled"):
                        if t_row["status"] == "failed":
                            raise RuntimeError(f"Remote device task failed: {t_row['result']}")
                        try:
                            return json.loads(t_row["result"])
                        except Exception:
                            return {"raw": t_row["result"]}
                raise TimeoutError(f"Timed out waiting for device {target_device_id} to execute {action}.")
        owner_policy = task_service_mod.policy_from_plane_dict(get_current_policy())
        tok = None
        try:
            from . import supervisor as _sup

            tok = _sup.set_plane_conn(conn)
            return dispatch(action, args, policy=owner_policy, approval_granted=granted, is_admin=granted)
        finally:
            if tok is not None:
                from . import supervisor as _sup

                _sup.reset_plane_conn(tok)

    def _run_workflow_worker(
        wf_id: str,
        target_dev: str | None,
        tenant: str,
        steps: list[dict],
        stop_on_failure: bool,
        max_retries: int,
        actor: str,
        require_step_approval: bool = False,
        initial_results: list[dict] | None = None,
        start_index: int = 0,
        workflow_approved: bool = False,
        workflow_approval_id: str | None = None,
    ):
        results = list(initial_results or [])
        overall_ok = True
        err_msg = ""

        for idx in range(start_index, len(steps)):
            step = steps[idx]
            check = conn.execute("SELECT status FROM workflows WHERE id=?", (wf_id,)).fetchone()
            if check and check["status"] == "cancelled":
                return

            step_name = step.get("name") or f"Step {idx + 1}"
            action = step.get("action") or step.get("tool") or ""
            args = dict(step.get("args") or {})
            verify_cfg = step.get("verify")
            guard_cfg = step.get("guard") if isinstance(step.get("guard"), dict) else None
            step_dev = str(step.get("device_id") or target_dev or "local").strip() or "local"
            retries_left = max(0, int(step.get("retries", max_retries)))

            tier_enum = TOOL_TIERS.get(action)
            # R-P0-11: Tier >=3 steps need per-step approval by default
            # (require_step_approval is now redundant — the default is
            # approval for Tier >=3); a whole-workflow admin approval
            # satisfies the requirement for every step.
            step_needs_approval = (not workflow_approved) and (bool(step.get("require_approval")) or (tier_enum is not None and tier_enum.value >= 3))
            approved = False
            step_approval_id = None
            if step_needs_approval:
                app_id = uuid.uuid4().hex[:12]
                now_ts = dbmod.now()
                conn.execute(
                    "INSERT INTO approvals (id, task_id, tool, reason, status, created_at, expires_at, policy_version, tenant) VALUES (?,?,?,?, 'pending', ?, ?, 0, ?)",
                    (app_id, None, action, f"Workflow {wf_id} step {idx + 1} ({step_name}) requires operator approval.", now_ts, now_ts + 600, tenant),
                )
                conn.execute("UPDATE workflows SET status='waiting_approval', current_step=?, updated_at=? WHERE id=?", (idx + 1, now_ts, wf_id))
                conn.commit()
                approved = False
                while True:
                    time.sleep(0.5)
                    wf_row = conn.execute("SELECT status FROM workflows WHERE id=?", (wf_id,)).fetchone()
                    if wf_row and wf_row["status"] == "cancelled":
                        return
                    ap_row = conn.execute("SELECT status, expires_at FROM approvals WHERE id=?", (app_id,)).fetchone()
                    if ap_row is None or ap_row["status"] in ("denied", "expired"):
                        break
                    if ap_row["expires_at"] is not None and ap_row["expires_at"] <= dbmod.now():
                        conn.execute("UPDATE approvals SET status='expired', decided_by='system', decided_at=? WHERE id=? AND status='pending'", (dbmod.now(), app_id))
                        conn.commit()
                        break
                    if ap_row["status"] == "approved":
                        approved = True
                        step_approval_id = app_id
                        break
                if not approved:
                    step_entry = {
                        "index": idx + 1,
                        "name": step_name,
                        "action": action,
                        "args": args,
                        "status": "failed",
                        "duration_seconds": 0.0,
                        "result": None,
                        "error": f"Step {idx + 1} approval denied or expired.",
                    }
                    results.append(step_entry)
                    conn.execute(
                        "UPDATE workflows SET status='failed', error=?, results_json=?, completed_at=?, updated_at=? WHERE id=?",
                        (step_entry["error"], json.dumps(results), dbmod.now(), dbmod.now(), wf_id),
                    )
                    conn.commit()
                    return
                conn.execute("UPDATE workflows SET status='running', updated_at=? WHERE id=?", (dbmod.now(), wf_id))
                conn.commit()

            step_entry = {
                "index": idx + 1,
                "name": step_name,
                "action": action,
                "args": args,
                "status": "running",
                "duration_seconds": 0.0,
                "result": None,
                "error": None,
            }

            conn.execute("UPDATE workflows SET current_step=?, updated_at=? WHERE id=?", (idx + 1, dbmod.now(), wf_id))
            conn.commit()

            t0 = time.time()
            success = False
            attempts_used = 0
            from . import context_execution as ctxexecmod
            from . import supervisor as _sup_bb

            # Resolve any {{blackboard.step_N.*}} bindings from upstream workflow steps
            bb_state = {"steps": {}, "shared": {}}
            for prev_entry in results:
                if isinstance(prev_entry, dict):
                    p_idx = int(prev_entry.get("index") or 0)
                    p_id = str(prev_entry.get("step_id") or f"step_{p_idx}")
                    p_out = prev_entry.get("result")
                    p_sum = str(p_out.get("summary") if isinstance(p_out, dict) else p_out or "")[:400]
                    bb_state["steps"][p_id] = {"output": p_out, "summary": p_sum, "status": prev_entry.get("status")}
                    bb_state["steps"][f"step_{p_idx}"] = bb_state["steps"][p_id]
            args = _sup_bb.resolve_blackboard_bindings(args, bb_state)
            step_entry["args"] = args
            step_entry["step_id"] = step.get("step_id") or f"step_{idx + 1}"
            step_entry["role"] = step.get("role") or ""
            step_entry["device_id"] = step_dev

            pre_ctx = ctxexecmod.capture_execution_context(
                conn,
                device_id=step_dev,
                tenant=tenant,
                live_meta=_get_device_live_meta(step_dev) if step_dev != "local" else {},
            )
            step_entry["pre_context"] = pre_ctx

            while retries_left >= 0 and not success:
                attempts_used += 1
                try:
                    if guard_cfg:
                        guard_res = ctxexecmod.evaluate_context_guard(
                            guard_cfg,
                            pre_ctx,
                            execute_tool_fn=_execute_workflow_step_sync,
                            device_id=step_dev if step_dev != "local" else target_dev,
                            tenant=tenant,
                            actor=actor,
                        )
                        step_entry["guard_evaluation"] = guard_res
                        if not guard_res.get("passed"):
                            raise RuntimeError("Context guard failed: " + "; ".join(guard_res.get("reasons") or []))

                    res = _execute_workflow_step_sync(
                        action,
                        args,
                        step_dev if step_dev != "local" else target_dev,
                        tenant,
                        actor,
                        workflow_approved=workflow_approved,
                        step_approved=approved,
                        workflow_approval_id=workflow_approval_id,
                        step_approval_id=step_approval_id,
                    )
                    step_entry["result"] = res

                    if verify_cfg and isinstance(verify_cfg, dict):
                        v_text = str(verify_cfg.get("text") or verify_cfg.get("expected_text") or "")
                        v_window = str(verify_cfg.get("window") or verify_cfg.get("expected_window") or "")
                        v_timeout = float(verify_cfg.get("timeout_seconds", verify_cfg.get("wait_seconds", 8.0)))
                        v_mode = str(verify_cfg.get("mode") or "verify").strip().lower()
                        if v_mode in {"visual", "screenshot", "browser_compare"}:
                            baseline = str(verify_cfg.get("baseline") or verify_cfg.get("name") or "").strip()
                            if not baseline:
                                raise RuntimeError("Visual verification requires verify.baseline filename.")
                            v_res = _execute_workflow_step_sync(
                                "browser_compare_screenshot",
                                {
                                    "baseline": baseline,
                                    "threshold": float(verify_cfg.get("threshold", 0.08)),
                                    "full_page": bool(verify_cfg.get("full_page", False)),
                                    "update_baseline": bool(verify_cfg.get("update_baseline", False)),
                                },
                                target_dev,
                                tenant,
                                actor,
                                workflow_approved=workflow_approved,
                                step_approved=approved,
                                workflow_approval_id=workflow_approval_id,
                                step_approval_id=step_approval_id,
                            )
                            step_entry["verification"] = v_res
                            if not v_res.get("matched", v_res.get("ok")):
                                raise RuntimeError(f"Step visual verification failed for baseline '{baseline}' (rmse={v_res.get('rmse')}).")
                        elif v_text or v_window:
                            if v_mode in {"wait", "poll", "wait_for_text"}:
                                if not v_text:
                                    raise RuntimeError("Step verification mode=wait requires text.")
                                v_res = _execute_workflow_step_sync(
                                    "wait_for_text",
                                    {"text": v_text, "timeout_seconds": v_timeout},
                                    target_dev,
                                    tenant,
                                    actor,
                                    workflow_approved=workflow_approved,
                                    step_approved=approved,
                                    workflow_approval_id=workflow_approval_id,
                                    step_approval_id=step_approval_id,
                                )
                                step_entry["verification"] = v_res
                                if not v_res.get("found"):
                                    raise RuntimeError(f"Step verification failed: text '{v_text}' not found after {v_timeout}s.")
                            else:
                                # Closed-loop default: desktop_verify_action (+ post screenshot evidence).
                                v_res = _execute_workflow_step_sync(
                                    "desktop_verify_action",
                                    {
                                        "expected_text": v_text,
                                        "expected_window": v_window,
                                        "wait_seconds": min(v_timeout, 5.0),
                                        "capture_after": bool(verify_cfg.get("capture_after", True)),
                                    },
                                    target_dev,
                                    tenant,
                                    actor,
                                    workflow_approved=workflow_approved,
                                    step_approved=approved,
                                    workflow_approval_id=workflow_approval_id,
                                    step_approval_id=step_approval_id,
                                )
                                step_entry["verification"] = v_res
                                if not v_res.get("verified", v_res.get("ok")):
                                    detail = v_text or v_window or "expected UI state"
                                    raise RuntimeError(f"Step verification failed via desktop_verify_action for '{detail}'.")

                    step_entry["status"] = "completed"
                    success = True
                except Exception as exc:
                    retries_left -= 1
                    if retries_left < 0:
                        step_entry["status"] = "failed"
                        step_entry["error"] = str(exc)
                        overall_ok = False
                        err_msg = str(exc)
                    else:
                        time.sleep(1.0)

            step_entry["duration_seconds"] = round(time.time() - t0, 2)
            step_entry["attempts"] = attempts_used
            ctxexecmod.record_execution_learning(
                conn,
                tenant=tenant,
                device_id=step_dev,
                tool=action,
                pre_context=pre_ctx,
                success=success,
                attempts_used=attempts_used,
                error_message=str(step_entry.get("error") or ""),
            )
            results.append(step_entry)

            conn.execute("UPDATE workflows SET results_json=?, updated_at=? WHERE id=?", (json.dumps(results), dbmod.now(), wf_id))
            conn.commit()

            if not success and stop_on_failure:
                break

        final_status = "completed" if overall_ok else "failed"
        conn.execute("UPDATE workflows SET status=?, error=?, results_json=?, completed_at=?, updated_at=? WHERE id=?", (final_status, err_msg, json.dumps(results), dbmod.now(), dbmod.now(), wf_id))
        conn.commit()
        audit(actor, target_dev or "local", "execute_workflow", {"workflow_id": wf_id, "status": final_status, "steps_count": len(steps)}, "allowed", tenant=tenant)

    # --- Phase 3: Fleet Telemetry, Broadcast, Triggers & Schedules ---
    app.state._running = True

    def _evaluate_device_triggers(dev_row: dict, metrics: dict, now_ts: float):
        try:
            triggers = conn.execute(
                "SELECT * FROM triggers WHERE enabled=1 AND tenant=? AND (device_id=? OR device_id IS NULL OR device_id='' OR device_id='*')",
                (dev_row["tenant"], dev_row["id"]),
            ).fetchall()
            for trig in triggers:
                metric_key = trig["metric"]
                val = metrics.get(metric_key)
                if val is None or not isinstance(val, (int, float)):
                    continue
                thresh = float(trig["threshold"])
                op = trig["operator"]
                triggered = False
                if op == ">" and val > thresh:
                    triggered = True
                elif op == ">=" and val >= thresh:
                    triggered = True
                elif op == "<" and val < thresh:
                    triggered = True
                elif op == "<=" and val <= thresh:
                    triggered = True
                elif op == "==" and abs(val - thresh) < 0.01:
                    triggered = True

                if triggered:
                    cooldown = max(10, int(trig["cooldown_seconds"]))
                    last_trig = trig["last_triggered_at"] or 0
                    if (now_ts - last_trig) >= cooldown:
                        # Atomic multi-worker cooldown claim: only the worker
                        # that wins the conditional UPDATE fires the trigger.
                        claim = conn.execute(
                            "UPDATE triggers SET last_triggered_at=? WHERE id=? AND enabled=1 AND (last_triggered_at IS NULL OR last_triggered_at <= ?)",
                            (now_ts, trig["id"], now_ts - cooldown),
                        )
                        conn.commit()
                        if claim.rowcount != 1:
                            continue
                        act_type = trig["action_type"]
                        act_target = trig["action_target"]
                        act_args = {}
                        try:
                            act_args = json.loads(trig["action_args_json"] or "{}")
                        except Exception:
                            pass
                        if act_type == "tool":
                            try:
                                created = task_service_mod.create_task(
                                    conn,
                                    actor="trigger",
                                    role="operator",
                                    tenant=dev_row["tenant"],
                                    device_id=dev_row["id"],
                                    tool=act_target,
                                    args=act_args,
                                    human_initiated=False,
                                    device_row=dev_row,
                                    get_effective_device_policy=get_effective_device_policy,
                                    get_current_policy=get_current_policy,
                                    notify=notify_tasks,
                                    audit_fn=audit,
                                )
                            except HTTPException:
                                continue  # takeover / e-stop / denied: skip this trigger action
                            task_id = created["task_id"]
                            audit(
                                "trigger", dev_row["id"], f"trigger:{trig['name']}", {"metric": metric_key, "value": val, "task_id": task_id, "tool": act_target}, "executed", tenant=dev_row["tenant"]
                            )
                        elif act_type == "webhook":
                            wh_url = act_target if act_target.startswith("http") else get_webhook_url()
                            if wh_url:
                                evt = {
                                    "event": "trigger_fired",
                                    "trigger_name": trig["name"],
                                    "device_id": dev_row["id"],
                                    "device_name": dev_row["name"],
                                    "metric": metric_key,
                                    "value": val,
                                    "threshold": thresh,
                                    "timestamp": now_ts,
                                    "content": f"🚨 Alert: Trigger '{trig['name']}' fired on {dev_row['name']}: {metric_key}={val} (threshold {op} {thresh})",
                                }
                                asyncio.run(send_webhook_notification_async(wh_url, evt, admin_token, get_public_base_url()))
                                audit("trigger", dev_row["id"], f"trigger:{trig['name']}", {"metric": metric_key, "value": val, "webhook": bool(wh_url)}, "executed", tenant=dev_row["tenant"])
        except Exception:
            pass

    def _scheduler_loop():
        from . import orchestration as orchmod

        sweep_tick = 0
        retention_tick = 0
        while getattr(app.state, "_running", True):
            try:
                time.sleep(10)
                if not getattr(app.state, "_running", True):
                    break
                plane_state.sync_emergency_stop_to_app(conn, app.state)
                sweep_tick += 1
                retention_tick += 1
                if sweep_tick >= max(1, int(os.environ.get("AI_BRIDGE_TASK_SWEEP_EVERY", "3"))):
                    sweep_tick = 0
                    stats = orchmod.sweep_stale_running_tasks(conn)
                    app.state.last_stale_sweep = stats
                    # DIST-06: re-drive gated handoffs stalled by a crash
                    # between the result commit and advance_handoff, in
                    # the same sweep tick. Never fatal to the scheduler:
                    # the sweeper already counts per-task errors, and a
                    # failure here must not stop the stale-task sweep.
                    try:
                        from . import supervisor as _supmod

                        app.state.last_handoff_sweep = _supmod.sweep_stuck_handoffs(conn)
                    except Exception:
                        pass
                if retention_tick >= max(6, int(os.environ.get("AI_BRIDGE_RETENTION_SWEEP_EVERY", "60"))):
                    retention_tick = 0
                    try:
                        app.state.last_retention_sweep = dbmod.retention_sweep_from_env(conn)
                    except Exception:
                        pass
                if sweep_tick == 0:
                    try:
                        app.state.last_alert_eval = alertsmod.evaluate_operational_alerts(
                            conn,
                            config_override=_load_alert_settings(),
                        )
                    except Exception:
                        pass
                now_ts = dbmod.now()
                due_jobs = conn.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run_at <= ?", (now_ts,)).fetchall()
                for job in due_jobs:
                    interval = max(10, int(job["interval_seconds"]))
                    next_run = now_ts + interval
                    # DIST-01: atomic claim — the UPDATE only succeeds
                    # when this worker is the first to move next_run_at;
                    # every other worker sees rowcount 0 and skips.
                    claim = conn.execute("UPDATE schedules SET last_run_at=?, next_run_at=? WHERE id=? AND next_run_at<=? AND enabled=1", (now_ts, next_run, job["id"], now_ts))
                    conn.commit()
                    if claim.rowcount != 1:
                        continue
                    tool_name = job["tool"]
                    try:
                        args = json.loads(job["args_json"] or "{}")
                    except Exception:
                        args = {}
                    target = job["device_id"]
                    try:
                        if tool_name == "__workflow__":
                            wf_steps = args.get("steps") or []
                            if wf_steps and not plane_state.is_emergency_stop(conn, app.state):
                                wf_id = uuid.uuid4().hex[:12]
                                wf_name = str(args.get("name") or job["name"] or "Scheduled Workflow")
                                conn.execute(
                                    "INSERT INTO workflows (id, name, tenant, device_id, steps_json, status, current_step, total_steps, results_json, created_at, updated_at) "
                                    "VALUES (?, ?, ?, ?, ?, 'running', 0, ?, '[]', ?, ?)",
                                    (wf_id, wf_name, job["tenant"], target or "local", json.dumps(wf_steps), len(wf_steps), now_ts, now_ts),
                                )
                                conn.commit()
                                threading.Thread(
                                    target=_run_workflow_worker,
                                    args=(
                                        wf_id,
                                        target or None,
                                        job["tenant"],
                                        wf_steps,
                                        bool(args.get("stop_on_failure", True)),
                                        int(args.get("max_retries", 1)),
                                        f"scheduler:{job['name']}",
                                        False,
                                        None,
                                        0,
                                        False,
                                        None,
                                    ),
                                    daemon=True,
                                ).start()
                        else:
                            _execute_workflow_step_sync(tool_name, args, target, job["tenant"], f"scheduler:{job['name']}", workflow_approved=False)
                    except Exception:
                        pass
            except Exception:
                time.sleep(5)

    threading.Thread(target=_scheduler_loop, name="ai-bridge-scheduler", daemon=True).start()

    # --- Phase 4: Visual Streaming, Dual Control & Fleet Auto-Updater ---
    app.state.latest_frames = {}
    app.state.webrtc_sessions = {}

    def _fetch_desktop_frame(device_id: str, scale: float, quality: int) -> dict:
        """Capture (or return a fresh cached) desktop frame. Caller must authorize."""
        eff_p = Policy(allow_tier3=True, desktop_enabled=True, max_tier_unattended=4)

        if device_id == "local":
            try:
                frame = tools.capture_desktop_frame(scale=scale, quality=quality, approval_granted=True, policy=eff_p)
                app.state.latest_frames["local"] = frame
                return frame
            except Exception as exc:
                return {
                    "available": False,
                    "error": str(exc),
                    "note": "Desktop session not currently active or requires graphical display.",
                }

        cached = app.state.latest_frames.get(device_id)
        now_ts = time.time()
        if cached and (now_ts - cached.get("timestamp", 0)) < 2.5:
            return cached

        try:
            res = _execute_workflow_step_sync(
                "capture_desktop_frame",
                {"scale": scale, "quality": quality},
                device_id,
                "default",
                "desktop_stream",
            )
            if isinstance(res, dict) and res.get("base64_data"):
                app.state.latest_frames[device_id] = res
            return res
        except Exception as exc:
            if cached:
                return cached
            return {"available": False, "error": str(exc)}

    # --- WebRTC desktop streaming (signaling only; media is peer-to-peer) ---
    def _webrtc_sessions() -> dict:
        return app.state.__dict__.setdefault("webrtc_sessions", {})

    def _load_sess(session_id: str) -> dict | None:
        from . import v1_routes

        return v1_routes.load_webrtc_session_db(conn, app.state, session_id)

    def _save_sess(sess: dict) -> dict:
        from . import v1_routes

        return v1_routes.save_webrtc_session_db(conn, app.state, sess)

    def _get_webrtc_session(device_id: str, session_id: str) -> dict:
        sess = _load_sess(session_id)
        if sess is None or sess.get("device_id") != device_id:
            raise HTTPException(status_code=404, detail="WebRTC session not found.")
        if time.time() - float(sess.get("created_at", 0)) > 600:
            sess["status"] = "ended"
            _save_sess(sess)
            raise HTTPException(status_code=410, detail="WebRTC session expired.")
        return sess

    def _queue_webrtc_task(
        device_id: str,
        session_id: str,
        scale: float,
        fps: int,
        monitor_index: int | None = None,
        bitrate_kbps: int | None = None,
        tool: str = "start_webrtc_stream",
        actor: str = "webrtc-session",
    ) -> None:
        dev_row = conn.execute("SELECT * FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
        if dev_row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        # The stream serves a session a human operator just created: mark
        # the task human-initiated so the SEC-08 takeover gate hands it out
        # (a human_control session sets the takeover flag on the device).
        args: dict[str, Any] = {"session_id": session_id, "scale": scale, "fps": fps}
        if monitor_index is not None:
            args["monitor_index"] = monitor_index
        if bitrate_kbps is not None:
            args["bitrate_kbps"] = int(bitrate_kbps)
        task_service_mod.create_task(
            conn,
            actor=actor,
            role="operator",
            tenant=_row_tenant(dev_row),
            device_id=device_id,
            tool=tool,
            args=args,
            human_initiated=True,
            preauthorized_stream=True,
            device_row=dev_row,
            get_effective_device_policy=get_effective_device_policy,
            notify=lambda _d, _t: notify_tasks(),
            audit_fn=audit,
        )

    # --- Option C: Interactive Live Terminal Endpoints ---

    def _remote_terminal_exec(target_dev_id: str, command: str, cwd: str) -> dict:
        dev_row = conn.execute("SELECT * FROM devices WHERE id=? AND revoked=0", (target_dev_id,)).fetchone()
        if not dev_row:
            raise RuntimeError(f"Device '{target_dev_id}' not found or revoked.")
        args = {"command": command, "working_directory": cwd or "", "timeout": 35}
        # R-P0-04: terminal tasks go through the same approval gate as every
        # other Tier-3 tool. human_initiated=True stamps the task so it may
        # still be claimed while human takeover is active (it IS the human).
        created = task_service_mod.create_task(
            conn,
            actor="terminal-relay",
            role="admin",
            tenant=_row_tenant(dev_row),
            device_id=target_dev_id,
            tool="terminal_exec",
            args=args,
            human_initiated=True,
            device_row=dev_row,
            get_effective_device_policy=get_effective_device_policy,
            get_current_policy=get_current_policy,
            notify=notify_tasks,
            audit_fn=audit,
        )
        task_id = created["task_id"]
        deadline = time.time() + 40.0
        while time.time() < deadline:
            time.sleep(0.2)
            t_row = conn.execute("SELECT status, result FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t_row is None:
                raise RuntimeError(f"Terminal task {task_id} disappeared.")
            if t_row["status"] == "needs_approval":
                continue  # wait for an admin to approve; the 40s deadline still applies
            if t_row["status"] in ("completed", "failed", "cancelled", "timed_out"):
                if t_row["status"] != "completed":
                    return {"stdout": "", "stderr": str(t_row["result"] or f"Task {t_row['status']}"), "returncode": 1}
                try:
                    parsed = json.loads(t_row["result"] or "{}")
                    return parsed if isinstance(parsed, dict) else {"stdout": str(parsed), "stderr": "", "returncode": 0}
                except Exception:
                    return {"stdout": str(t_row["result"] or ""), "stderr": "", "returncode": 0}
        final = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if final and final["status"] == "needs_approval":
            return {"stdout": "", "stderr": "Terminal execution requires human approval; approve it in the dashboard and retry.", "returncode": 1}
        raise TimeoutError(f"Remote device '{dev_row['name']}' did not respond within 40s.")

    # --- Settings: Webhook notifications ----------------------------------

    # --- Auth: local users / sessions ------------------------------------

    # --- Stream tickets (D-08) ------------------------------------------------
    # Browsers cannot set headers on a WebSocket handshake, so the dashboard
    # used to put its long-lived session token in the WS URL query string.
    # POST /api/stream-ticket mints a single-use, 60-second ticket bound to
    # one actor + tenant + device + WebRTC session; the WS endpoint redeems
    # it (see v1_routes.v1_session_websocket). Only the hash is stored and
    # the ticket is returned once, never logged.

    # --- Auth: OIDC (dormant unless AI_BRIDGE_OIDC_* are set) ------------

    # --- Connector: Microsoft 365 (dormant unless AI_BRIDGE_M365_* are set) ---

    # --- Policy versioning -------------------------------------------------

    # --- Connections + logs (dashboard insight views) ---------------------

    # --- Agent polling (outbound only) -----------------------------------

    @app.middleware("http")
    async def bearer_auth_normalization(request: Request, call_next):
        auth = request.headers.get("authorization")
        if auth and auth.strip().lower().startswith("bearer "):
            token_str = auth.strip()[7:].strip()
            raw_headers = list(request.scope.get("headers", []))
            hdr_keys = {h[0].lower() for h in raw_headers}
            if b"x-admin-token" not in hdr_keys and b"x-session-token" not in hdr_keys:
                raw_headers.append((b"x-admin-token", token_str.encode("utf-8")))
                raw_headers.append((b"x-session-token", token_str.encode("utf-8")))
                request.scope["headers"] = raw_headers
                request._headers = Headers(scope=request.scope)
        return await call_next(request)

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        from ..logging_config import log_context

        req_corr = (request.headers.get("x-correlation-id") or request.headers.get("x-request-id") or "").strip()
        if not req_corr or len(req_corr) > 128 or not req_corr.isascii():
            req_corr = "tr-" + uuid.uuid4().hex[:16]
        request.state.correlation_id = req_corr

        client_ip = request.client.host if request.client else "anon"
        path = request.url.path
        if path in ("/api/auth/login", "/api/auth/oidc/login", "/api/auth/oidc/callback", "/api/auth/oidc/exchange", "/api/connectors/m365/callback", "/api/mcp/oauth/callback"):
            key = f"auth:{client_ip}"
            limit = 30
        else:
            admin_hdr = request.headers.get("x-admin-token")
            dev_hdr = request.headers.get("x-device-token")
            sess_hdr = request.headers.get("x-session-token")
            auth_hdr = request.headers.get("authorization")
            token_val = admin_hdr or dev_hdr or sess_hdr or auth_hdr
            key = f"tok:{dbmod.hash_token(token_val)[:16]}" if token_val else f"ip:{client_ip}"
            limit = 1200 if token_val else 300

        t0 = time.perf_counter()
        with log_context(correlation_id=req_corr):
            try:
                # DIST-07: async-native limiter — awaited, so the shared
                # Redis limit is enforced under multiple workers.
                await _rate_limit_async(key, limit=limit, conn=getattr(request.app.state, "conn", None))
            except HTTPException as exc:
                resp = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
                resp.headers["X-Correlation-ID"] = req_corr
                return resp
            response = await call_next(request)
        duration_s = time.perf_counter() - t0
        try:
            response.headers["X-Correlation-ID"] = req_corr
        except Exception:
            pass
        try:
            from . import metrics as metricsmod

            # Collapse IDs in path for low-cardinality labels.
            endpoint = path
            for prefix in ("/api/tasks/", "/api/devices/", "/api/agents/", "/api/v1/sessions/", "/api/supervisor/handoffs/", "/api/traces/"):
                if path.startswith(prefix) and len(path) > len(prefix):
                    endpoint = prefix + "{id}"
                    break
            metricsmod.record_http_request(request.method, endpoint, int(getattr(response, "status_code", 200) or 200), duration=duration_s)
        except Exception:
            pass
        return response

    from . import ai_api, mcp_routes, v1_routes
    from .custom_agents import AgentRunner, install_custom_agent_routes

    ai_api.attach_ai_routes(
        app,
        conn,
        task_cond,
        notify_tasks,
        auth_context,
        require_role,
        get_effective_device_policy,
        audit,
        resolve_tenant=lambda requested, request: resolve_request_tenant(requested, request.headers.get("x-admin-token"), request.headers.get("x-session-token")),
    )
    v1_routes.attach_v1_routes(
        app,
        conn,
        task_cond,
        notify_tasks,
        auth_context,
        require_role,
        device_row,
        get_current_policy,
        get_effective_device_policy,
        audit,
        resolve_tenant=lambda requested, x_admin_token=None, x_session_token=None, authorization=None: resolve_request_tenant(requested, x_admin_token, x_session_token, authorization),
        require_row_tenant=_require_row_tenant,
    )
    mcp_routes.install_mcp_routes(app, conn, require_role, audit)

    # Persistent 24/7 custom agents: CRUD endpoints plus the background runner.
    def _agent_failure_notify(event: dict) -> None:
        try:
            url = ""
            row = conn.execute("SELECT value FROM settings WHERE key='webhook_url'").fetchone()
            if row is not None and row["value"]:
                url = str(row["value"]).strip()
            if not url:
                url = os.environ.get("AI_BRIDGE_WEBHOOK_URL", "").strip()
            if not url:
                return
            send_webhook_notification(
                url,
                {
                    "event": event.get("event", "agent_failed"),
                    "tool": "custom_agent_run",
                    "device_name": event.get("agent_name") or "agent",
                    "task_id": event.get("agent_id"),
                    "error": event.get("error"),
                },
            )
        except Exception:
            pass

    agent_runner = AgentRunner(
        conn,
        _execute_workflow_step_sync,
        is_paused=lambda: bool(getattr(app.state, "emergency_stop", False)),
        failure_notifier=_agent_failure_notify,
    )
    install_custom_agent_routes(app, conn, require_role, audit, agent_runner)
    agent_runner.start()

    # --- Real-Time SSE Event Stream, Alerts Config, Backups & Tenants -------

    def _load_alert_settings() -> dict[str, str]:
        row = conn.execute("SELECT value FROM settings WHERE key='alert_webhooks'").fetchone()
        stored: dict[str, str] = {}
        if row and row["value"]:
            try:
                stored = json.loads(row["value"]) or {}
            except Exception:
                stored = {}
        env_cfg = alertsmod.get_alert_config()
        return {
            "slack_webhook": stored.get("slack_webhook") or env_cfg["slack_webhook"],
            "discord_webhook": stored.get("discord_webhook") or env_cfg["discord_webhook"],
            "telegram_token": stored.get("telegram_token") or env_cfg["telegram_token"],
            "telegram_chat_id": stored.get("telegram_chat_id") or env_cfg["telegram_chat_id"],
            "generic_webhook": stored.get("generic_webhook") or env_cfg["generic_webhook"],
        }

    def _record_alert_delivery(channel: str, event_type: str, severity: str, title: str, ok: bool, detail: str = "") -> None:
        conn.execute(
            "INSERT INTO alert_deliveries (id, channel, event_type, severity, title, status, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], channel, event_type, severity, title, "delivered" if ok else "failed", detail, dbmod.now()),
        )
        conn.commit()

    backup_dir = Path(os.environ.get("AI_BRIDGE_BACKUP_DIR", "") or (Path.home() / ".ai-bridge" / "backups"))

    # BEGIN WAVE3B ROUTERS (Track A) -------------------------------------
    # Domain routers live in ai_bridge/control/routers/; each factory
    # receives the shared control-plane state (ControlContext) and
    # returns an APIRouter. Mounted in original registration order so
    # route matching is unchanged (route snapshot in
    # tests/unit/test_architecture_phase4_routers.py).
    from .routers import admin as _admin_router
    from .routers import agent as _agent_router
    from .routers import audit as _audit_router
    from .routers import auth as _auth_router
    from .routers import devices as _devices_router
    from .routers import health as _health_router
    from .routers import release4 as _release4_router
    from .routers import tasks as _tasks_router
    from .routers import webrtc as _webrtc_router
    from .routers import workflows as _workflows_router
    from .routers._context import ControlContext

    _router_ctx = ControlContext(
        app=app,
        conn=conn,
        token=token,
        admin_token=admin_token,
        db_path=db_path,
        device_ttl_days=device_ttl_days,
        backup_dir=backup_dir,
        notify_tasks=notify_tasks,
        get_webhook_url=get_webhook_url,
        get_public_base_url=get_public_base_url,
        _auth_context_full=_auth_context_full,
        auth_context=auth_context,
        session_tenant_for_request=session_tenant_for_request,
        resolve_request_tenant=resolve_request_tenant,
        _row_tenant=_row_tenant,
        _require_row_tenant=_require_row_tenant,
        _require_device_tenant=_require_device_tenant,
        require_role=require_role,
        require_admin=require_admin,
        get_current_policy=get_current_policy,
        get_effective_device_policy=get_effective_device_policy,
        reject_policy_widening=reject_policy_widening,
        device_row=device_row,
        _require_agent_mtls=_require_agent_mtls,
        audit=audit,
        _install_asset=_install_asset,
        _get_device_live_meta=_get_device_live_meta,
        _get_devices_live_meta=_get_devices_live_meta,
        expire_pending_approvals=expire_pending_approvals,
        approval_detail=approval_detail,
        _execute_workflow_step_sync=_execute_workflow_step_sync,
        _run_workflow_worker=_run_workflow_worker,
        _evaluate_device_triggers=_evaluate_device_triggers,
        _fetch_desktop_frame=_fetch_desktop_frame,
        _webrtc_sessions=_webrtc_sessions,
        _load_sess=_load_sess,
        _save_sess=_save_sess,
        _get_webrtc_session=_get_webrtc_session,
        _queue_webrtc_task=_queue_webrtc_task,
        _remote_terminal_exec=_remote_terminal_exec,
        _load_alert_settings=_load_alert_settings,
        _record_alert_delivery=_record_alert_delivery,
    )
    app.include_router(_health_router.create_health_router(_router_ctx))
    app.include_router(_devices_router.create_devices_router(_router_ctx))
    app.include_router(_tasks_router.create_tasks_router(_router_ctx))
    app.include_router(_audit_router.create_audit_router(_router_ctx))
    app.include_router(_admin_router.create_admin_router(_router_ctx))
    app.include_router(_release4_router.create_release4_router(_router_ctx))
    app.include_router(_workflows_router.create_workflows_router(_router_ctx))
    app.include_router(_webrtc_router.create_webrtc_router(_router_ctx))
    app.include_router(_auth_router.create_auth_router(_router_ctx))
    app.include_router(_agent_router.create_agent_router(_router_ctx))
    # END WAVE3B ROUTERS (Track A) ---------------------------------------

    # Mount frontend SPA static files if dashboard build dist exists
    if dashboard_dist_dir is not None:
        dist_dir = Path(dashboard_dist_dir)
    else:
        dist_dir = Path("/app/apps/dashboard/dist")
        if not dist_dir.exists():
            dist_dir = Path(__file__).resolve().parent.parent.parent.parent / "apps" / "dashboard" / "dist"
    if dist_dir.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="static_assets")

        @app.get("/{full_path:path}")
        async def serve_spa_frontend(full_path: str):
            dist_root = dist_dir.resolve()
            target = (dist_root / full_path).resolve()
            if full_path and target.is_relative_to(dist_root) and target.is_file():
                return FileResponse(target)
            return FileResponse(dist_root / "index.html")

    return app

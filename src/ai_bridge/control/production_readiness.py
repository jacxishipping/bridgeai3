"""Production profile checklist for deploy gates and /health/ready."""

from __future__ import annotations

import json
import os
from typing import Any


def is_production_profile() -> bool:
    profile = os.environ.get("AI_BRIDGE_PROFILE", os.environ.get("RENDER", "")).strip().lower()
    return profile in ("production", "prod", "1", "true", "yes") or bool(os.environ.get("RENDER"))


def turn_credentials_configured() -> bool:
    if os.environ.get("AI_BRIDGE_TURN_SECRET", "").strip():
        return True
    if os.environ.get("AI_BRIDGE_TURN_URL", "").strip():
        return True
    raw = os.environ.get("AI_BRIDGE_ICE_SERVERS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            return isinstance(parsed, list) and bool(parsed)
        except json.JSONDecodeError:
            return False
    return False


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def env_public_base_url() -> str:
    """Canonical public origin from env.

    Prefers ``AI_BRIDGE_PUBLIC_BASE_URL``; ``AI_BRIDGE_PUBLIC_URL`` remains a
    supported alias so deploy configs and readiness share one resolution order.
    """
    for key in ("AI_BRIDGE_PUBLIC_BASE_URL", "AI_BRIDGE_PUBLIC_URL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value.rstrip("/")
    return ""


def legacy_webrtc_routes_allowed() -> bool:
    """Legacy ``/api/devices/.../desktop/webrtc/*`` create path.

    Disabled by default in production unless ``AI_BRIDGE_ALLOW_LEGACY_WEBRTC=1``.
    Non-production keeps the routes for compatibility tests.
    """
    if _truthy_env("AI_BRIDGE_ALLOW_LEGACY_WEBRTC"):
        return True
    return not is_production_profile()


def configured_workers() -> int:
    raw = os.environ.get("AI_BRIDGE_WORKERS", "").strip()
    if not raw:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def require_redis_for_multi_host() -> bool:
    return _truthy_env("AI_BRIDGE_REQUIRE_REDIS") or _truthy_env("AI_BRIDGE_MULTI_HOST") or configured_workers() > 1


def enforce_multi_worker_redis(conn: Any) -> None:
    """Fail fast at boot when multi-host or multi-worker production lacks Redis.

    Enforced when ``AI_BRIDGE_REQUIRE_REDIS=1`` or ``AI_BRIDGE_MULTI_HOST=1`` is
    set, or when running under the production profile with ``AI_BRIDGE_WORKERS > 1``.
    """
    explicit_multi_host = _truthy_env("AI_BRIDGE_REQUIRE_REDIS") or _truthy_env("AI_BRIDGE_MULTI_HOST")
    prod_multi_worker = is_production_profile() and configured_workers() > 1
    if not (explicit_multi_host or prod_multi_worker):
        return
    from . import durable_ratelimit as drl

    backend = str(drl.backend_name(conn)).lower()
    if backend != "redis":
        raise RuntimeError(
            "Redis is required when multi-host operation (AI_BRIDGE_MULTI_HOST / AI_BRIDGE_REQUIRE_REDIS) "
            "or multi-worker production (AI_BRIDGE_WORKERS > 1) is enabled. "
            f"Configure reachable REDIS_URL or AI_BRIDGE_REDIS_URL (current backend: {backend})."
        )


def assess_production_readiness(conn: Any) -> dict[str, Any]:
    """Return checklist items; ``ok`` is False when critical prod issues exist."""
    from . import plane_state

    issues: list[dict[str, str]] = []
    prod = is_production_profile()

    if prod and not turn_credentials_configured():
        issues.append(
            {
                "code": "turn_secret_missing",
                "severity": "critical",
                "message": "Set AI_BRIDGE_TURN_SECRET, AI_BRIDGE_TURN_URL, or AI_BRIDGE_ICE_SERVERS in production.",
            }
        )

    admin = os.environ.get("AI_BRIDGE_ADMIN_TOKEN", "").strip()
    admin_users = 0
    try:
        _r = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0").fetchone()
        admin_users = int(_r["n"] or 0) if _r is not None else 0
    except Exception:
        admin_users = 0
    if prod and not admin and admin_users == 0:
        issues.append(
            {
                "code": "admin_access_unset",
                "severity": "critical",
                "message": "No admin access is configured in production: "
                "create an admin user, or set AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN=1 with AI_BRIDGE_ADMIN_TOKEN (legacy token; the token alone also feeds the quick-approval HMAC).",
            }
        )

    if _truthy_env("AI_BRIDGE_HTTP_ALLOW_ANON"):
        # R-P0-12 follow-up: AI_BRIDGE_HTTP_ALLOW_ANON was removed in Wave 1
        # (anonymous MCP access no longer exists), so the var now does
        # nothing; flag it as stale config instead of a critical finding.
        issues.append(
            {
                "code": "stale_anon_config",
                "severity": "warning",
                "message": "AI_BRIDGE_HTTP_ALLOW_ANON is a removed setting (anonymous access no longer exists); unset it to avoid confusion.",
            }
        )

    public_base = env_public_base_url()
    if prod and public_base.lower().startswith("http://"):
        issues.append(
            {
                "code": "public_base_url_http",
                "severity": "critical",
                "message": ("Public base URL must use https:// in production (AI_BRIDGE_PUBLIC_BASE_URL or alias AI_BRIDGE_PUBLIC_URL)."),
            }
        )
    elif prod and not public_base:
        issues.append(
            {
                "code": "tls_termination_documented",
                "severity": "warning",
                "message": ("Set AI_BRIDGE_PUBLIC_BASE_URL (or AI_BRIDGE_PUBLIC_URL) to your https:// public origin; TLS terminates at the reverse proxy."),
            }
        )

    browser_isolation: dict[str, Any] = {}
    chromium_sandbox = True
    try:
        from ai_bridge import browser_tools

        browser_isolation = browser_tools.browser_isolation_report()
        chromium_sandbox = not browser_tools._allow_browser_no_sandbox()
        browser_isolation = {**browser_isolation, "chromium_sandbox": chromium_sandbox}
    except Exception as exc:
        browser_isolation = {"error": str(exc), "chromium_sandbox": None}

    if prod and chromium_sandbox is False and not _truthy_env("AI_BRIDGE_ALLOW_UNSAFE_BROWSER"):
        issues.append(
            {
                "code": "browser_sandbox_disabled",
                "severity": "critical",
                "message": ("Chromium sandbox is disabled (AI_BRIDGE_ALLOW_BROWSER_NO_SANDBOX). Unset it, or set AI_BRIDGE_ALLOW_UNSAFE_BROWSER=1 to acknowledge risk."),
            }
        )

    try:
        plane_state.load_emergency_stop(conn)
        emergency_stop_durable = True
    except Exception:
        emergency_stop_durable = False
        issues.append(
            {
                "code": "emergency_stop_unreadable",
                "severity": "critical",
                "message": "Could not read durable emergency-stop flag from settings table.",
            }
        )

    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    dialect = getattr(conn, "dialect", "sqlite")
    if prod and not db_url and str(dialect).lower() == "sqlite":
        issues.append(
            {
                "code": "postgres_required_in_production",
                "severity": "critical",
                "message": "Set DATABASE_URL to PostgreSQL in production (SQLite remains the local/dev default).",
            }
        )

    if prod and legacy_webrtc_routes_allowed() and _truthy_env("AI_BRIDGE_ALLOW_LEGACY_WEBRTC"):
        issues.append(
            {
                "code": "legacy_webrtc_enabled",
                "severity": "warning",
                "message": "AI_BRIDGE_ALLOW_LEGACY_WEBRTC=1 keeps dual WebRTC stacks; prefer /api/v1/sessions only.",
            }
        )

    from . import durable_ratelimit as drl

    rate_backend = drl.backend_name(conn)
    backend_l = str(rate_backend).lower()
    explicit_multi_host = _truthy_env("AI_BRIDGE_REQUIRE_REDIS") or _truthy_env("AI_BRIDGE_MULTI_HOST")
    if (explicit_multi_host or (prod and require_redis_for_multi_host())) and backend_l != "redis":
        issues.append(
            {
                "code": "redis_required_for_multi_host",
                "severity": "critical",
                "message": (
                    "Multi-host / multi-worker operation (AI_BRIDGE_REQUIRE_REDIS, AI_BRIDGE_MULTI_HOST, "
                    "or AI_BRIDGE_WORKERS > 1 in production) requires reachable Redis (REDIS_URL or AI_BRIDGE_REDIS_URL)."
                ),
            }
        )
    elif prod and backend_l in ("memory", "mem", "inmemory", "local"):
        issues.append(
            {
                "code": "rate_limit_memory_backend",
                "severity": "warning",
                "message": f"Rate-limit backend is '{rate_backend}'; use Redis/DB for multi-worker production.",
            }
        )

    from ai_bridge import browser_isolation_os

    from . import device_mtls, secrets_backend, worm_siem

    secrets_info = secrets_backend.describe_secrets_backend()
    worm_info = worm_siem.describe_worm_siem()
    mtls_info = device_mtls.describe_device_mtls()
    os_browser = browser_isolation_os.describe_os_browser_isolation()

    if prod and worm_siem.worm_siem_required() and not worm_siem.worm_siem_url():
        issues.append(
            {
                "code": "worm_siem_required",
                "severity": "critical",
                "message": "AI_BRIDGE_WORM_SIEM_REQUIRED=1 but AI_BRIDGE_WORM_SIEM_URL is unset.",
            }
        )
    backend_name = secrets_backend.secrets_backend_name()
    if prod and backend_name in ("vault", "hashicorp_vault") and not os.environ.get("AI_BRIDGE_VAULT_ADDR"):
        issues.append(
            {
                "code": "vault_backend_misconfigured",
                "severity": "critical",
                "message": "AI_BRIDGE_SECRETS_BACKEND=vault requires AI_BRIDGE_VAULT_ADDR.",
            }
        )
    if prod and browser_isolation_os.browser_netns_requested():
        if os_browser.get("mode") in ("unsupported_platform", "none"):
            issues.append(
                {
                    "code": "browser_netns_unavailable",
                    "severity": "warning",
                    "message": "AI_BRIDGE_BROWSER_NETNS=1 but no usable unshare/bwrap/runsc wrapper on this host.",
                }
            )

    critical = [i for i in issues if i["severity"] == "critical"]
    return {
        "ok": len(critical) == 0,
        "production_profile": prod,
        "configured_workers": configured_workers(),
        "redis_required": require_redis_for_multi_host(),
        "turn_configured": turn_credentials_configured(),
        "emergency_stop_durable": emergency_stop_durable,
        "rate_limit_backend": rate_backend,
        "database_dialect": dialect,
        "legacy_webrtc_allowed": legacy_webrtc_routes_allowed(),
        "browser_isolation": browser_isolation,
        "os_browser_isolation": os_browser,
        "secrets_backend": secrets_info,
        "worm_siem": worm_info,
        "device_mtls": mtls_info,
        "issues": issues,
    }

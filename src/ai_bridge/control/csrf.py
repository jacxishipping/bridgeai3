"""CSRF protection for session-based authentication.

Generates per-session CSRF tokens and validates them on state-changing
requests. Uses HMAC-SHA256 with a server-side secret resolved once per
process (see :func:`get_csrf_secret`).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("ai_bridge.csrf")

# Resolved once per process by get_csrf_secret(); never regenerated per call,
# so a token minted by one worker/thread validates on every other one that
# shares the same secret source.
_CSRF_SECRET: str | None = None
_CSRF_TTL = 86400  # 24 hours


def _worker_count() -> int:
    """How many worker processes serve the plane (1 = single process).

    Read from ``AI_BRIDGE_WORKERS`` (set to 4 by Dockerfile.prod to match
    its hardcoded gunicorn ``-w 4``). A non-integer value fails closed:
    a typo must not silently mask a multi-worker deployment.
    """
    raw = os.environ.get("AI_BRIDGE_WORKERS", "1").strip()
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"AI_BRIDGE_WORKERS must be an integer, got {raw!r}") from None


def resolve_csrf_secret(*, workers: int | None = None) -> str:
    """Resolve the configured CSRF secret, WITHOUT using the module cache.

    F-05-02 (Wave 6): when more than one worker/process serves the plane,
    a per-process random secret breaks CSRF validation across workers —
    a token minted on one worker fails on the others (observed live in
    Wave 5E: ~80% of session writes failed on the 4-worker staging
    plane). Fail fast instead of silently degrading: if no shared secret
    is configured and ``workers`` > 1, raise RuntimeError. The worker
    count comes from ``AI_BRIDGE_WORKERS`` when not passed explicitly.

    Single-worker deployments keep the per-process random fallback (with
    the existing warning). A misconfigured vault/aws backend raises from
    ``get_secret`` (fail closed) and is not swallowed here.

    NOTE: this cannot see Render's instance count — each Render instance
    is a separate process with no shared worker env var. On Render the
    shared secret itself is the fix (``render.yaml`` generates one via
    ``generateValue``); the fail-fast covers the deployments where the
    worker count is knowable (gunicorn ``-w N`` / ``AI_BRIDGE_WORKERS``).
    """
    value: str | None = None
    try:
        from .secrets_backend import get_secret
    except Exception:
        get_secret = None  # type: ignore[assignment]
    if get_secret is not None:
        value = get_secret("AI_BRIDGE_CSRF_SECRET")
    if not value:
        value = os.environ.get("AI_BRIDGE_CSRF_SECRET") or None
    if value:
        return value
    n = workers if workers is not None else _worker_count()
    if n > 1:
        raise RuntimeError(
            "AI_BRIDGE_CSRF_SECRET is not configured but the plane runs "
            f"with {n} workers (AI_BRIDGE_WORKERS={n}): CSRF tokens minted "
            "by one worker would not validate on the others. Set a single "
            "shared AI_BRIDGE_CSRF_SECRET via the secrets backend or "
            "environment (render.yaml generates one; "
            "docker-compose.prod.yml requires it in .env; "
            "deployment/linux/deploy-production.sh generates it)."
        )
    value = secrets.token_hex(32)
    logger.warning(
        "AI_BRIDGE_CSRF_SECRET is not configured; using a per-process random "
        "CSRF secret. CSRF tokens will not validate across workers or restarts. "
        "Set AI_BRIDGE_CSRF_SECRET via the secrets backend or environment."
    )
    return value


def get_csrf_secret() -> str:
    """Resolve the CSRF HMAC secret, caching it in the module.

    Delegates to :func:`resolve_csrf_secret` (same order: secrets backend,
    then ``AI_BRIDGE_CSRF_SECRET`` env, then — single-worker only — a
    per-process random secret; multi-worker without a shared secret
    raises, see F-05-02).
    """
    global _CSRF_SECRET
    if _CSRF_SECRET:
        return _CSRF_SECRET
    _CSRF_SECRET = resolve_csrf_secret()
    return _CSRF_SECRET


def _csrf_key(session_token: str) -> str:
    return f"csrf:{session_token}"


def generate_csrf_token(session_token: str) -> str:
    """Generate a time-bound CSRF token for the given session token."""
    expires = int(time.time()) + _CSRF_TTL
    msg = f"{session_token}:{expires}".encode("utf-8")
    sig = hmac.new(get_csrf_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]
    return f"{expires}:{sig}"


def validate_csrf_token(session_token: str, token: str) -> bool:
    """Validate a CSRF token for the given session token."""
    try:
        parts = token.split(":")
        if len(parts) != 2:
            return False
        expires_str, sig = parts
        expires = int(expires_str)
        if time.time() > expires:
            return False
        expected = hmac.new(
            get_csrf_secret().encode("utf-8"),
            f"{session_token}:{expires}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:16]
        return hmac.compare_digest(sig, expected)
    except (ValueError, IndexError):
        return False


async def csrf_middleware(request: Request, call_next: Callable):
    """FastAPI HTTP middleware that validates CSRF tokens on mutating session-auth requests.

    Usage: app.middleware("http")(csrf_middleware)
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return await call_next(request)

    if request.url.path in ("/api/auth/login", "/api/auth/logout"):
        return await call_next(request)

    # Check X-Session-Token FIRST so a spoofed X-Admin-Token / X-Device-Token
    # header alongside a real session token can never skip CSRF validation.
    # Bearer-token normalization in app.py mirrors `Authorization: Bearer <tok>`
    # into both X-Admin-Token and X-Session-Token, so exempt only when the
    # supplied X-Admin-Token actually matches the configured admin token.
    session_token = request.headers.get("x-session-token")
    admin_hdr = request.headers.get("x-admin-token")
    if session_token:
        app_state = getattr(getattr(request, "app", None), "state", None)
        configured_admin = getattr(app_state, "admin_token", None) if app_state is not None else None
        legacy_enabled = bool(getattr(app_state, "legacy_admin_enabled", True)) if app_state is not None else True
        if admin_hdr and configured_admin and legacy_enabled and hmac.compare_digest(admin_hdr, str(configured_admin)):
            return await call_next(request)
    else:
        if admin_hdr or request.headers.get("x-device-token"):
            return await call_next(request)
        return await call_next(request)

    csrf_token = request.headers.get("x-csrf-token")
    if not csrf_token or not validate_csrf_token(session_token, csrf_token):
        return JSONResponse(
            status_code=403,
            content={"detail": "CSRF token missing or invalid. Include X-CSRF-Token header."},
        )
    return await call_next(request)

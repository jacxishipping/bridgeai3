"""health routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response

from .. import metrics as metricsmod
from .. import plane_state
from ._context import ControlContext


def create_health_router(ctx: ControlContext) -> APIRouter:
    """Build the health APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    _install_asset = ctx._install_asset
    app = ctx.app
    audit = ctx.audit
    conn = ctx.conn
    require_role = ctx.require_role

    @router.get("/install.sh", summary="Linux/macOS agent installer", include_in_schema=False)
    def install_sh():
        from fastapi.responses import FileResponse as _FileResponse
        path = _install_asset("magic-install.sh")
        return _FileResponse(path, media_type="text/x-shellscript", filename="install.sh")

    @router.get("/install.ps1", summary="Windows agent installer", include_in_schema=False)
    def install_ps1():
        from fastapi.responses import FileResponse as _FileResponse
        for relative in (
            "deployment/windows/install-agent.ps1",
            "deployment/windows/Install-AIBridge.ps1",
        ):
            try:
                path = _install_asset(relative)
                return _FileResponse(path, media_type="text/plain", filename="install.ps1")
            except HTTPException:
                continue
        raise HTTPException(status_code=404, detail="Windows installer not found")

    @router.get("/health", summary="Health check", description="Returns basic health status of the control plane including version and emergency stop state.")
    def health():
        return {"status": "ok", "version": "0.3.0", "emergency_stop": app.state.emergency_stop}

    @router.get("/health/live", summary="Liveness probe", description="Verifies the process is alive and responsive.")
    def health_live():
        """Liveness probe: verifies process is alive and responsive."""
        return {"status": "live", "uptime": time.time() - getattr(app.state, "_start_time", time.time())}

    @router.get("/health/ready", summary="Readiness probe", description="Verifies database connectivity and state.")
    def health_ready():
        """Readiness probe: verifies database connectivity and state."""
        from .. import production_readiness as prodready

        try:
            t0 = time.perf_counter()
            row = conn.execute("SELECT 1 AS alive").fetchone()
            db_latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            if not row:
                raise HTTPException(status_code=503, detail="Database unhealthy")
            plane_state.sync_emergency_stop_to_app(conn, app.state)
            checklist = prodready.assess_production_readiness(conn)
            db_stats = conn.get_db_stats() if hasattr(conn, "get_db_stats") else {"dialect": getattr(conn, "dialect", "sqlite")}
            body = {
                "status": "ready",
                "database": "connected",
                "db_latency_ms": db_latency_ms,
                "db_stats": db_stats,
                "emergency_stop": app.state.emergency_stop,
                "emergency_stop_durable": checklist.get("emergency_stop_durable"),
                "production_checklist": checklist,
            }
            if prodready.is_production_profile() and not checklist.get("ok"):
                # Unauthenticated readiness must not dump the full checklist.
                codes = [str(i.get("code") or "") for i in (checklist.get("issues") or []) if i.get("code")]
                raise HTTPException(
                    status_code=503,
                    detail={"ok": False, "status": "not_ready", "codes": codes},
                )
            return body
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Database probe failed: {exc}")

    @router.get("/health/production-checklist", summary="Production readiness checklist")
    def health_production_checklist(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("operator", x_admin_token, x_session_token)
        from .. import production_readiness as prodready

        plane_state.sync_emergency_stop_to_app(conn, app.state)
        checklist = prodready.assess_production_readiness(conn)
        return {**checklist, "emergency_stop_active": bool(app.state.emergency_stop)}

    @router.get("/metrics", summary="Prometheus metrics", description="Exposes Prometheus-compatible metrics for monitoring and alerting.")
    def metrics_endpoint(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """Prometheus metrics exposition endpoint.

        Supports dedicated ``AI_BRIDGE_METRICS_TOKEN`` (Bearer) for Prometheus
        scrapers, plus RBAC ``viewer`` role when in production profile or when
        ``AI_BRIDGE_METRICS_REQUIRE_AUTH=1``.
        """
        import hmac
        import os

        from .. import production_readiness as prodready

        metrics_token = os.environ.get("AI_BRIDGE_METRICS_TOKEN", "").strip()
        require_auth = prodready.is_production_profile() or bool(metrics_token) or (
            os.environ.get("AI_BRIDGE_METRICS_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")
        )
        if require_auth:
            bearer = ""
            if authorization and authorization.strip().lower().startswith("bearer "):
                bearer = authorization.strip()[7:].strip()
            if not (metrics_token and bearer and hmac.compare_digest(bearer, metrics_token)):
                require_role("viewer", x_admin_token, x_session_token)
        payload, content_type = metricsmod.get_metrics_payload(conn)
        return Response(content=payload, media_type=content_type)

    @router.get("/api/metrics/summary")
    def metrics_summary_api(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        summary = metricsmod.build_metrics_summary(conn)
        audit(actor, "", "metrics_summary_view", {}, "allowed")
        return summary

    return router

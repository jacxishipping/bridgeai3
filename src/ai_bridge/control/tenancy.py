"""Production SaaS Multi-Tenancy Governance, Quotas, Rate Limits & Metering (Pillar 9).

Provides tenant lifecycle management (`free`, `pro`, `enterprise` plan tiers),
per-tenant resource quotas (`max_devices`, `max_concurrent_streams`, `max_daily_tasks`,
`max_memory_chunks`, `rate_limit_rpm`), and daily usage metering stored durably
in the control-plane database without requiring destructive schema migrations.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import HTTPException

from . import db as dbmod

_TENANT_CFG_PREFIX = "tenant.config."
_TENANT_USAGE_PREFIX = "tenant.usage."

PLAN_DEFAULTS: dict[str, dict[str, Any]] = {
    "free": {
        "plan_tier": "free",
        "max_devices": 5,
        "max_concurrent_streams": 2,
        "max_daily_tasks": 500,
        "max_memory_chunks": 250,
        "rate_limit_rpm": 120,
    },
    "pro": {
        "plan_tier": "pro",
        "max_devices": 50,
        "max_concurrent_streams": 15,
        "max_daily_tasks": 10000,
        "max_memory_chunks": 5000,
        "rate_limit_rpm": 600,
    },
    "enterprise": {
        "plan_tier": "enterprise",
        "max_devices": 10000,
        "max_concurrent_streams": 500,
        "max_daily_tasks": 1000000,
        "max_memory_chunks": 500000,
        "rate_limit_rpm": 5000,
    },
}


def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def get_tenant_config(conn: Any, tenant_id: str) -> dict[str, Any]:
    """Load or synthesize the governance configuration for a tenant.

    The ``default`` tenant always defaults to the ``enterprise`` tier so single-tenant
    and existing fleet deployments never hit artificial low limits unless configured.
    """
    tid = (tenant_id or "default").strip() or "default"
    default_tier = "enterprise" if tid == "default" else "pro"
    base = {
        "tenant_id": tid,
        "name": "Default Enterprise Workspace" if tid == "default" else tid,
        "status": "active",
        **PLAN_DEFAULTS[default_tier],
        "created_at": 0.0,
        "updated_at": 0.0,
    }
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (f"{_TENANT_CFG_PREFIX}{tid}",),
        ).fetchone()
        if row and row["value"]:
            stored = json.loads(str(row["value"]))
            if isinstance(stored, dict):
                tier = str(stored.get("plan_tier") or default_tier).lower()
                if tier in PLAN_DEFAULTS:
                    base.update(PLAN_DEFAULTS[tier])
                base.update({k: v for k, v in stored.items() if v is not None})
                base["tenant_id"] = tid
    except Exception:
        pass
    return base


def save_tenant_config(
    conn: Any,
    tenant_id: str,
    *,
    name: str | None = None,
    plan_tier: str | None = None,
    status: str | None = None,
    max_devices: int | None = None,
    max_concurrent_streams: int | None = None,
    max_daily_tasks: int | None = None,
    max_memory_chunks: int | None = None,
    rate_limit_rpm: int | None = None,
) -> dict[str, Any]:
    """Create or update a SaaS tenant configuration and quotas."""
    tid = (tenant_id or "").strip()
    if not tid or len(tid) > 64:
        raise HTTPException(status_code=400, detail="tenant_id must be 1..64 characters.")
    cfg = get_tenant_config(conn, tid)
    now_ts = dbmod.now()
    if not cfg.get("created_at"):
        cfg["created_at"] = now_ts

    if plan_tier is not None:
        pt = plan_tier.strip().lower()
        if pt not in PLAN_DEFAULTS:
            raise HTTPException(status_code=400, detail=f"Invalid plan_tier '{plan_tier}'. Choose free, pro, or enterprise.")
        cfg.update(PLAN_DEFAULTS[pt])
        cfg["plan_tier"] = pt

    if name is not None and name.strip():
        cfg["name"] = name.strip()[:120]
    if status is not None:
        st = status.strip().lower()
        if st not in ("active", "suspended"):
            raise HTTPException(status_code=400, detail="status must be 'active' or 'suspended'.")
        cfg["status"] = st
    if max_devices is not None:
        cfg["max_devices"] = max(1, int(max_devices))
    if max_concurrent_streams is not None:
        cfg["max_concurrent_streams"] = max(1, int(max_concurrent_streams))
    if max_daily_tasks is not None:
        cfg["max_daily_tasks"] = max(1, int(max_daily_tasks))
    if max_memory_chunks is not None:
        cfg["max_memory_chunks"] = max(1, int(max_memory_chunks))
    if rate_limit_rpm is not None:
        cfg["rate_limit_rpm"] = max(10, int(rate_limit_rpm))

    cfg["updated_at"] = now_ts
    key = f"{_TENANT_CFG_PREFIX}{tid}"
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, json.dumps(cfg)),
    )
    conn.commit()
    return cfg


def get_tenant_usage(conn: Any, tenant_id: str, *, app_state: Any = None) -> dict[str, Any]:
    """Compute live resource consumption and quota utilization for a tenant."""
    tid = (tenant_id or "default").strip() or "default"
    cfg = get_tenant_config(conn, tid)
    now_ts = dbmod.now()
    day_start = now_ts - 86400.0

    devices_count = 0
    online_devices = 0
    tasks_24h = 0
    workflows_total = 0
    memory_chunks = 0
    users_count = 0
    try:
        d_rows = conn.execute("SELECT status, last_seen FROM devices WHERE tenant=? AND revoked=0", (tid,)).fetchall()
        devices_count = len(d_rows)
        for r in d_rows:
            ls = float(r["last_seen"] or 0)
            if r["status"] == "online" and (now_ts - ls) < 90:
                online_devices += 1
    except Exception:
        pass

    try:
        t_row = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE tenant=? AND created_at>=?", (tid, day_start)).fetchone()
        if t_row:
            tasks_24h = int(t_row["c"] if hasattr(t_row, "keys") else t_row[0])
    except Exception:
        pass

    try:
        w_row = conn.execute("SELECT COUNT(*) AS c FROM workflows WHERE tenant=?", (tid,)).fetchone()
        if w_row:
            workflows_total = int(w_row["c"] if hasattr(w_row, "keys") else w_row[0])
    except Exception:
        pass

    try:
        m_row = conn.execute("SELECT COUNT(*) AS c FROM agent_memory_chunks WHERE tenant=?", (tid,)).fetchone()
        if m_row:
            memory_chunks = int(m_row["c"] if hasattr(m_row, "keys") else m_row[0])
    except Exception:
        pass

    try:
        u_row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE tenant=? AND disabled=0", (tid,)).fetchone()
        if u_row:
            users_count = int(u_row["c"] if hasattr(u_row, "keys") else u_row[0])
    except Exception:
        pass

    active_streams = 0
    if app_state is not None:
        sessions = getattr(app_state, "webrtc_sessions", None) or {}
        for sess in sessions.values():
            if not isinstance(sess, dict):
                continue
            if sess.get("status") in ("ended", "error"):
                continue
            s_dev = str(sess.get("device_id") or "")
            if s_dev == "local" and tid == "default":
                active_streams += 1
            elif s_dev:
                try:
                    dr = conn.execute("SELECT tenant FROM devices WHERE id=?", (s_dev,)).fetchone()
                    if dr and str(dr["tenant"] or "default") == tid:
                        active_streams += 1
                except Exception:
                    pass

    # Merge any metered counters recorded today
    day_key = f"{_TENANT_USAGE_PREFIX}{tid}.{_today_utc()}"
    metered: dict[str, int] = {}
    try:
        m_rec = conn.execute("SELECT value FROM settings WHERE key=?", (day_key,)).fetchone()
        if m_rec and m_rec["value"]:
            metered = json.loads(str(m_rec["value"]))
    except Exception:
        metered = {}

    def _pct(used: int, limit: int) -> float:
        return round(min(100.0, (used / max(1, limit)) * 100.0), 1)

    return {
        "schema": "ai-bridge-tenant-usage/v1",
        "tenant_id": tid,
        "name": cfg["name"],
        "plan_tier": cfg["plan_tier"],
        "status": cfg["status"],
        "quotas": {
            "max_devices": cfg["max_devices"],
            "max_concurrent_streams": cfg["max_concurrent_streams"],
            "max_daily_tasks": cfg["max_daily_tasks"],
            "max_memory_chunks": cfg["max_memory_chunks"],
            "rate_limit_rpm": cfg["rate_limit_rpm"],
        },
        "usage": {
            "active_devices": devices_count,
            "online_devices": online_devices,
            "active_streams": active_streams,
            "daily_tasks": max(tasks_24h, int(metered.get("tasks", 0))),
            "memory_chunks": memory_chunks,
            "workflows_total": workflows_total,
            "active_users": users_count,
        },
        "utilization_pct": {
            "devices": _pct(devices_count, cfg["max_devices"]),
            "streams": _pct(active_streams, cfg["max_concurrent_streams"]),
            "daily_tasks": _pct(max(tasks_24h, int(metered.get("tasks", 0))), cfg["max_daily_tasks"]),
            "memory_chunks": _pct(memory_chunks, cfg["max_memory_chunks"]),
        },
    }


def check_tenant_quota(
    conn: Any,
    tenant_id: str,
    resource: str,
    *,
    app_state: Any = None,
) -> dict[str, Any]:
    """Enforce tenant status and resource quota before provisioning devices, tasks, streams, or memory."""
    tid = (tenant_id or "default").strip() or "default"
    summary = get_tenant_usage(conn, tid, app_state=app_state)
    if summary["status"] == "suspended":
        raise HTTPException(
            status_code=403,
            detail=f"Tenant '{tid}' is currently suspended. Contact an administrator to restore access.",
        )

    quotas = summary["quotas"]
    usage = summary["usage"]
    checks = {
        "devices": ("active_devices", "max_devices", "enrolled devices"),
        "streams": ("active_streams", "max_concurrent_streams", "concurrent desktop streams"),
        "tasks": ("daily_tasks", "max_daily_tasks", "daily tasks"),
        "memory": ("memory_chunks", "max_memory_chunks", "memory chunks"),
    }
    if resource in checks:
        u_key, q_key, label = checks[resource]
        used = int(usage.get(u_key, 0))
        limit = int(quotas.get(q_key, 1000000))
        if used >= limit:
            raise HTTPException(
                status_code=429,
                detail=f"Tenant '{tid}' ({summary['plan_tier']} plan) quota exceeded for {label}: {used}/{limit}.",
                headers={
                    "X-Tenant-Quota-Limit": str(limit),
                    "X-Tenant-Quota-Remaining": "0",
                },
            )
    return summary


def list_all_tenants(conn: Any, *, app_state: Any = None) -> list[dict[str, Any]]:
    """List all configured and discovered tenants with their plan tier and live usage."""
    tenants = {"default"}
    for tbl in ("devices", "tasks", "workflows", "custom_agents", "mcp_servers", "users"):
        try:
            for r in conn.execute(f"SELECT DISTINCT tenant FROM {tbl}").fetchall():
                if r["tenant"]:
                    tenants.add(str(r["tenant"]))
        except Exception:
            pass
    try:
        for r in conn.execute("SELECT key FROM settings WHERE key LIKE ?", (f"{_TENANT_CFG_PREFIX}%",)).fetchall():
            k = str(r["key"] or "")
            if k.startswith(_TENANT_CFG_PREFIX):
                tid = k[len(_TENANT_CFG_PREFIX):].strip()
                if tid:
                    tenants.add(tid)
    except Exception:
        pass
    return [get_tenant_usage(conn, t, app_state=app_state) for t in sorted(tenants)]


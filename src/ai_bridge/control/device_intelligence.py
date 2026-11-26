"""Unified Device & Fleet Intelligence Engine (Pillar 1 & Pillar 2).

Synthesizes live device telemetry, normalized cross-platform capability fingerprints,
task execution success/recovery rates, active desktop session states, and policy
posture into actionable per-device and fleet-wide intelligence profiles.
"""
from __future__ import annotations

from typing import Any

from . import db as dbmod
from . import desktop_capabilities as capmod
from . import fleet_health as fhmod


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def build_device_intelligence(
    conn: Any,
    device_row: dict[str, Any] | Any,
    *,
    effective_policy: dict[str, Any] | None = None,
    live_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a unified intelligence profile for one device."""
    now_ts = dbmod.now()
    d = dict(device_row) if hasattr(device_row, "keys") else dict(device_row or {})
    device_id = str(d.get("id") or "local")
    tenant = str(d.get("tenant") or "default")
    os_val = str(d.get("os") or "")
    revoked = bool(d.get("revoked"))
    last_seen = _safe_float(d.get("last_seen")) or 0.0
    heartbeat_age_sec = round(max(0.0, now_ts - last_seen), 1) if last_seen > 0 else None
    is_online = (not revoked) and (str(d.get("status") or "").lower() == "online") and (heartbeat_age_sec is not None and heartbeat_age_sec < fhmod.STALE_HEARTBEAT_SEC)
    if device_id == "local":
        is_online = True
        heartbeat_age_sec = 0.0

    eff_pol = dict(effective_policy or {})
    meta = dict(live_meta or {})

    # 1. Cross-platform capability discovery fingerprint
    if device_id == "local" and not d.get("capabilities_json"):
        discovery = capmod.probe_local_capabilities()
    else:
        raw_caps = meta.get("capabilities") or d.get("capabilities_json")
        discovery = capmod.normalize_agent_capabilities(raw_caps, os_val)

    desktop_contract = capmod.device_desktop_capabilities(
        {**d, "status": "online" if is_online else d.get("status")},
        eff_pol,
    )

    # 2. Latest telemetry snapshot
    cpu_pct = _safe_float(meta.get("cpu_percent"))
    mem_pct = _safe_float(meta.get("memory_percent"))
    disk_pct = _safe_float(meta.get("disk_percent"))
    if cpu_pct is None or mem_pct is None:
        try:
            t_row = conn.execute(
                "SELECT cpu_percent, memory_percent, disk_percent, recorded_at FROM telemetry WHERE device_id=? ORDER BY recorded_at DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            if t_row:
                if cpu_pct is None:
                    cpu_pct = _safe_float(t_row["cpu_percent"])
                if mem_pct is None:
                    mem_pct = _safe_float(t_row["memory_percent"])
                if disk_pct is None:
                    disk_pct = _safe_float(t_row["disk_percent"])
        except Exception:
            pass

    # 3. Task reliability & recent activity (last 50 tasks)
    total_recent = 0
    completed_recent = 0
    failed_recent = 0
    queued_recent = 0
    last_failed_tool: str | None = None
    try:
        t_rows = conn.execute(
            "SELECT tool, status FROM tasks WHERE device_id=? ORDER BY created_at DESC LIMIT 50",
            (device_id,),
        ).fetchall()
        for r in t_rows:
            total_recent += 1
            st = str(r["status"] or "")
            if st == "completed":
                completed_recent += 1
            elif st in ("failed", "timed_out"):
                failed_recent += 1
                if last_failed_tool is None:
                    last_failed_tool = str(r["tool"] or "")
            elif st in ("queued", "running"):
                queued_recent += 1
    except Exception:
        pass

    terminal_count = completed_recent + failed_recent
    success_rate = round((completed_recent / terminal_count) * 100.0, 1) if terminal_count > 0 else 100.0

    # 4. Device-scoped episodic memory count
    learned_episodes = 0
    try:
        m_row = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_memory_chunks WHERE tenant=? AND project_tag=?",
            (tenant, f"device:{device_id}"),
        ).fetchone()
        if m_row:
            learned_episodes = int(m_row["c"] if hasattr(m_row, "keys") else m_row[0])
    except Exception:
        pass

    # 5. Health score (0..100), anomalies, and suggested actions
    score = 100
    anomalies: list[dict[str, Any]] = []
    suggested_actions: list[dict[str, Any]] = []

    if revoked:
        score = 0
        anomalies.append({"code": "device_revoked", "severity": "critical", "detail": "Device token is revoked."})
    elif not is_online:
        score -= 45
        anomalies.append({
            "code": "device_offline",
            "severity": "warning",
            "detail": f"Device has not reported a heartbeat within {fhmod.STALE_HEARTBEAT_SEC}s.",
        })
        suggested_actions.append({
            "action": "verify_agent_connectivity",
            "title": "Verify polling agent or WebSocket tunnel connectivity",
            "tool": "get_system_info",
        })

    if cpu_pct is not None and cpu_pct >= 90.0:
        score -= 15
        anomalies.append({"code": "high_cpu", "severity": "warning", "detail": f"CPU utilization is {cpu_pct:.1f}%."})
        suggested_actions.append({"action": "inspect_processes", "title": "Inspect top CPU-consuming processes", "tool": "list_processes"})
    if mem_pct is not None and mem_pct >= 90.0:
        score -= 15
        anomalies.append({"code": "high_memory", "severity": "warning", "detail": f"Memory utilization is {mem_pct:.1f}%."})
    if disk_pct is not None and disk_pct >= 92.0:
        score -= 15
        anomalies.append({"code": "high_disk", "severity": "warning", "detail": f"Disk utilization is {disk_pct:.1f}%."})

    if terminal_count >= 3 and success_rate < 75.0:
        penalty = min(25, int((100.0 - success_rate) * 0.4))
        score -= penalty
        anomalies.append({
            "code": "elevated_task_failure_rate",
            "severity": "warning",
            "detail": f"Recent task success rate is {success_rate}% ({failed_recent} failed of {terminal_count}).",
        })
        suggested_actions.append({
            "action": "run_supervisor_recovery",
            "title": f"Trigger Supervisor Recovery for failed tasks (last failed: {last_failed_tool or 'unknown'})",
            "tool": "supervisor_recover_role_tasks",
        })

    score = max(0, min(100, score))
    if revoked:
        readiness = "revoked"
    elif not is_online:
        readiness = "offline"
    elif score >= 80:
        readiness = "optimal"
    elif score >= 50:
        readiness = "degraded"
    else:
        readiness = "critical"

    # Recommended transport selection
    transports = discovery.get("transports") or ["jpeg"]
    recommended_transport = transports[0] if transports else "jpeg"

    return {
        "schema": "ai-bridge-device-intelligence/v1",
        "device_id": device_id,
        "device_name": d.get("name") or device_id,
        "hostname": d.get("hostname") or "",
        "os": os_val,
        "platform_family": discovery["platform_family"],
        "tenant": tenant,
        "online": is_online,
        "revoked": revoked,
        "heartbeat_age_sec": heartbeat_age_sec,
        "health_score": score,
        "readiness_state": readiness,
        "capabilities": discovery,
        "desktop_contract": desktop_contract,
        "recommended_transport": recommended_transport,
        "recommended_a11y_backend": discovery.get("a11y_backend", "windows_uia"),
        "context": {
            "foreground_window": meta.get("foreground_window") or "",
            "active_user": meta.get("active_user") or "",
            "ip_address": meta.get("ip_address") or "",
            "cpu_percent": cpu_pct,
            "memory_percent": mem_pct,
            "disk_percent": disk_pct,
        },
        "reliability": {
            "recent_tasks_evaluated": total_recent,
            "completed": completed_recent,
            "failed": failed_recent,
            "active_or_queued": queued_recent,
            "success_rate_pct": success_rate,
            "last_failed_tool": last_failed_tool,
            "learned_memory_episodes": learned_episodes,
        },
        "policy_posture": {
            "desktop_enabled": bool(eff_pol.get("desktop_enabled", False)),
            "allow_writes": bool(eff_pol.get("allow_writes", True)),
            "allow_tier3": bool(eff_pol.get("allow_tier3", False)),
            "max_tier_unattended": int(eff_pol.get("max_tier_unattended", 2) or 2),
            "unrestricted": bool(eff_pol.get("unrestricted", False)),
        },
        "anomalies": anomalies,
        "suggested_actions": suggested_actions,
    }


def build_fleet_intelligence(
    conn: Any,
    *,
    tenant: str = "default",
    get_effective_device_policy: Any = None,
    live_meta_by_device: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate unified device intelligence across all active devices in a tenant."""
    rows = conn.execute(
        "SELECT * FROM devices WHERE tenant=? AND revoked=0 ORDER BY created_at DESC",
        (tenant,),
    ).fetchall()
    meta_map = live_meta_by_device or {}
    profiles: list[dict[str, Any]] = []
    platform_counts: dict[str, int] = {}
    transport_counts: dict[str, int] = {}
    a11y_counts: dict[str, int] = {}
    total_anomalies = 0

    for row in rows:
        d = dict(row)
        eff_pol = get_effective_device_policy(row) if callable(get_effective_device_policy) else {}
        prof = build_device_intelligence(
            conn,
            d,
            effective_policy=eff_pol,
            live_meta=meta_map.get(d["id"]) or {},
        )
        profiles.append(prof)
        fam = prof["platform_family"]
        platform_counts[fam] = platform_counts.get(fam, 0) + 1
        rec_tr = prof["recommended_transport"]
        transport_counts[rec_tr] = transport_counts.get(rec_tr, 0) + 1
        a11y = prof["recommended_a11y_backend"]
        a11y_counts[a11y] = a11y_counts.get(a11y, 0) + 1
        total_anomalies += len(prof["anomalies"])

    avg_score = round(sum(p["health_score"] for p in profiles) / len(profiles), 1) if profiles else 100.0
    online_count = sum(1 for p in profiles if p["online"])
    optimal_count = sum(1 for p in profiles if p["readiness_state"] == "optimal")

    return {
        "schema": "ai-bridge-fleet-intelligence/v1",
        "tenant": tenant,
        "total_devices": len(profiles),
        "online_devices": online_count,
        "optimal_devices": optimal_count,
        "fleet_health_score": avg_score,
        "total_anomalies": total_anomalies,
        "platform_breakdown": platform_counts,
        "transport_breakdown": transport_counts,
        "a11y_backend_breakdown": a11y_counts,
        "devices": profiles,
    }


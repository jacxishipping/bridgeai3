"""Fleet device health summary: heartbeats, telemetry, spool visibility, poll state, drift, and streams."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from .. import webrtc_stream
from . import db as dbmod

STALE_HEARTBEAT_SEC = 90


def _control_plane_version() -> str:
    env_ver = os.environ.get("AI_BRIDGE_TARGET_AGENT_VERSION", "").strip()
    if env_ver:
        return env_ver
    try:
        import importlib.metadata

        return importlib.metadata.version("ai-bridge")
    except Exception:
        return "0.3.0"


def _expected_policy_hash(policy_dict: dict[str, Any]) -> str:
    canonical = {
        "allow_writes": bool(policy_dict.get("allow_writes", True)),
        "allow_tier3": bool(policy_dict.get("allow_tier3", False)),
        "desktop_enabled": bool(policy_dict.get("desktop_enabled", False)),
        "max_tier_unattended": int(policy_dict.get("max_tier_unattended", 2) or 0),
        "unrestricted": bool(policy_dict.get("unrestricted", False)),
        "allowed_tools": sorted(policy_dict["allowed_tools"]) if policy_dict.get("allowed_tools") is not None else None,
        "denied_tools": sorted(policy_dict.get("denied_tools") or ()),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _device_online(row: Any, now_ts: float) -> bool:
    last_seen = row["last_seen"] or 0
    latency = (now_ts - last_seen) if last_seen else 99999.0
    return (row["status"] == "online") and latency < STALE_HEARTBEAT_SEC


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def summarize_fleet_health(conn: Any, *, tenant: str = "default") -> dict[str, Any]:
    """Aggregate CPU/RAM, stale heartbeats, spool age, poll state, drift, update state, and WebRTC stream health."""
    now_ts = dbmod.now()
    target_version = _control_plane_version()
    try:
        dev_rows = conn.execute(
            "SELECT id, name, os, status, last_seen, policy_json, "
            "agent_version, agent_commit, reported_policy_hash, update_ring, update_state, "
            "poll_state_json, spool_stats_json "
            "FROM devices WHERE tenant=? AND revoked=0",
            (tenant,),
        ).fetchall()
    except Exception:
        dev_rows = conn.execute(
            "SELECT id, name, os, status, last_seen FROM devices WHERE tenant=? AND revoked=0",
            (tenant,),
        ).fetchall()

    # Load latest global policy once so expected_policy_hash reflects the server baseline.
    global_policy: dict[str, Any] = {}
    try:
        prow = conn.execute("SELECT policy_json FROM policies ORDER BY version DESC LIMIT 1").fetchone()
        if prow and prow["policy_json"]:
            global_policy = _parse_json_dict(prow["policy_json"])
    except Exception:
        global_policy = {}

    # PERF-04: one query for the latest telemetry row per device.
    latest_rows = conn.execute(
        "SELECT t.device_id, t.cpu_percent, t.memory_percent, t.disk_percent, t.recorded_at "
        "FROM telemetry t "
        "INNER JOIN (SELECT device_id, MAX(recorded_at) AS max_ts FROM telemetry GROUP BY device_id) latest "
        "ON latest.device_id = t.device_id AND latest.max_ts = t.recorded_at "
        "WHERE t.device_id IN (SELECT id FROM devices WHERE tenant=? AND revoked=0)",
        (tenant,),
    ).fetchall()
    latest_tel: dict[str, dict[str, Any]] = {}
    for r in latest_rows:
        dev_id = r["device_id"] if hasattr(r, "keys") else r[0]
        if dev_id not in latest_tel:
            latest_tel[dev_id] = dict(r)

    devices: list[dict[str, Any]] = []
    stale_devices: list[dict[str, Any]] = []
    cpu_vals: list[float] = []
    mem_vals: list[float] = []
    online_count = 0
    spool_pending_total = 0
    spool_max_age_sec = 0.0
    version_drift_count = 0
    policy_drift_count = 0

    for d in dev_rows:
        dd = dict(d) if hasattr(d, "keys") else {}
        last_seen = dd.get("last_seen") or 0
        heartbeat_age_sec = round(now_ts - last_seen, 1) if last_seen else None
        is_online = _device_online(d, now_ts)
        if is_online:
            online_count += 1

        tel = latest_tel.get(dd["id"])
        if tel:
            if tel.get("cpu_percent") is not None:
                cpu_vals.append(float(tel["cpu_percent"]))
            if tel.get("memory_percent") is not None:
                mem_vals.append(float(tel["memory_percent"]))

        poll_state = _parse_json_dict(dd.get("poll_state_json"))
        spool_stats = _parse_json_dict(dd.get("spool_stats_json"))
        pending = int(spool_stats.get("pending_count") or 0)
        oldest_age = float(spool_stats.get("oldest_pending_age_sec") or 0.0)
        spool_pending_total += pending
        if oldest_age > spool_max_age_sec:
            spool_max_age_sec = oldest_age

        agent_ver = str(dd.get("agent_version") or "")
        version_drift = bool(agent_ver and target_version and agent_ver != target_version)
        if version_drift:
            version_drift_count += 1

        dev_pol_override = _parse_json_dict(dd.get("policy_json"))
        eff_pol = {**global_policy, **dev_pol_override}
        expected_hash = _expected_policy_hash(eff_pol)
        reported_hash = str(dd.get("reported_policy_hash") or "")
        policy_drift = bool(reported_hash and reported_hash != expected_hash)
        if policy_drift:
            policy_drift_count += 1

        health = "online" if is_online else ("stale" if dd.get("status") == "online" else "offline")
        entry = {
            "id": dd["id"],
            "name": dd["name"],
            "os": dd.get("os"),
            "health": health,
            "last_seen": last_seen,
            "heartbeat_age_sec": heartbeat_age_sec,
            "telemetry": tel,
            "agent_version": agent_ver,
            "agent_commit": str(dd.get("agent_commit") or ""),
            "target_version": target_version,
            "version_drift": version_drift,
            "reported_policy_hash": reported_hash,
            "expected_policy_hash": expected_hash,
            "policy_drift": policy_drift,
            "update_ring": str(dd.get("update_ring") or "general"),
            "update_state": str(dd.get("update_state") or "idle"),
            "poll_state": poll_state,
            "spool_stats": spool_stats,
        }
        devices.append(entry)
        if health == "stale" or (dd.get("status") == "offline" and last_seen and heartbeat_age_sec and heartbeat_age_sec > STALE_HEARTBEAT_SEC):
            stale_devices.append({"id": dd["id"], "name": dd["name"], "heartbeat_age_sec": heartbeat_age_sec})

    streams = webrtc_stream.list_active_stream_health()
    unhealthy_streams = [s for s in streams if not s.get("healthy")]

    return {
        "tenant": tenant,
        "ts": now_ts,
        "target_version": target_version,
        "total_devices": len(dev_rows),
        "online_devices": online_count,
        "stale_device_count": len(stale_devices),
        "avg_cpu_percent": round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None,
        "avg_memory_percent": round(sum(mem_vals) / len(mem_vals), 1) if mem_vals else None,
        "spool_pending_total": spool_pending_total,
        "spool_max_age_sec": round(spool_max_age_sec, 1),
        "version_drift_count": version_drift_count,
        "policy_drift_count": policy_drift_count,
        "stale_devices": stale_devices,
        "active_streams": len(streams),
        "unhealthy_streams": len(unhealthy_streams),
        "streams": streams,
        "devices": devices,
    }

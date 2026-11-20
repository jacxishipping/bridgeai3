"""Staged update rollout ring manager for fleet agents (Release 2).

Supports progressive delivery across rings (``canary`` -> ``early`` -> ``general``)
with health-gate auto-pause, explicit operator pause/resume, and fleet rollback
dispatching ``rollback_fleet_agent`` to every device touched by the rollout.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from fastapi import HTTPException

from . import db as dbmod
from . import task_service as task_service_mod

VALID_RINGS = ("canary", "early", "general")


def _parse_json(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, type(default)):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        return default


def _format_rollout(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["rings"] = _parse_json(d.pop("rings_json", None), list(VALID_RINGS))
    d["dispatched"] = _parse_json(d.pop("dispatched_json", None), {})
    return d


def set_device_ring(conn: Any, device_id: str, ring: str) -> dict[str, Any]:
    norm = (ring or "").strip().lower()
    if norm not in VALID_RINGS:
        raise HTTPException(status_code=400, detail=f"Invalid ring '{ring}'. Expected one of: {', '.join(VALID_RINGS)}.")
    cur = conn.execute("UPDATE devices SET update_ring=? WHERE id=? AND revoked=0", (norm, device_id))
    conn.commit()
    if cur.rowcount != 1:
        raise HTTPException(status_code=404, detail="Device not found.")
    return {"device_id": device_id, "update_ring": norm}


def _dispatch_ring(
    conn: Any,
    *,
    rollout_id: str,
    ring: str,
    tenant: str,
    actor: str,
    tool: str,
    get_effective_device_policy: Callable,
    get_current_policy: Callable,
    notify: Callable,
    audit_fn: Callable,
    device_ids_override: list[str] | None = None,
) -> list[dict[str, Any]]:
    if device_ids_override is not None:
        if not device_ids_override:
            return []
        placeholders = ",".join("?" for _ in device_ids_override)
        devs = conn.execute(
            f"SELECT * FROM devices WHERE tenant=? AND revoked=0 AND id IN ({placeholders})",
            (tenant, *device_ids_override),
        ).fetchall()
    else:
        devs = conn.execute(
            "SELECT * FROM devices WHERE tenant=? AND revoked=0 AND update_ring=?",
            (tenant, ring),
        ).fetchall()

    results: list[dict[str, Any]] = []
    next_state = "rolling_back" if tool == "rollback_fleet_agent" else "updating"
    for dev in devs:
        try:
            created = task_service_mod.create_task(
                conn,
                actor=actor,
                role="admin",
                tenant=tenant,
                device_id=dev["id"],
                tool=tool,
                args={"rollout_id": rollout_id, "ring": ring},
                human_initiated=True,
                device_row=dev,
                get_effective_device_policy=get_effective_device_policy,
                get_current_policy=get_current_policy,
                notify=notify,
                audit_fn=audit_fn,
            )
            conn.execute("UPDATE devices SET update_state=? WHERE id=?", (next_state, dev["id"]))
            results.append({"device_id": dev["id"], "task_id": created["task_id"], "status": created["status"]})
        except HTTPException as exc:
            results.append({"device_id": dev["id"], "task_id": None, "status": "skipped", "reason": str(exc.detail)})
    conn.commit()
    return results


def evaluate_ring_health(conn: Any, dispatched_entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute completed/failed/pending counts for tasks dispatched in a rollout ring."""
    task_ids = [e["task_id"] for e in dispatched_entries if e.get("task_id")]
    if not task_ids:
        return {"total": 0, "completed": 0, "failed": 0, "pending": 0, "failure_rate": 0.0}
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
        tuple(task_ids),
    ).fetchall()
    completed = sum(1 for r in rows if r["status"] == "completed")
    failed = sum(1 for r in rows if r["status"] in ("failed", "timed_out", "cancelled"))
    pending = sum(1 for r in rows if r["status"] in ("queued", "running", "needs_approval"))
    total = len(rows)
    failure_rate = round(failed / max(1, total), 4)
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "failure_rate": failure_rate,
    }


def list_rollouts(conn: Any, *, tenant: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM update_rollouts WHERE tenant=? ORDER BY created_at DESC LIMIT ?",
        (tenant, max(1, min(200, int(limit)))),
    ).fetchall()
    return [_format_rollout(r) for r in rows]


def create_rollout(
    conn: Any,
    *,
    name: str,
    tenant: str,
    actor: str,
    target_version: str = "",
    target_commit: str = "",
    manifest_url: str = "",
    rings: list[str] | None = None,
    max_failure_rate: float = 0.25,
    get_effective_device_policy: Callable,
    get_current_policy: Callable,
    notify: Callable,
    audit_fn: Callable,
) -> dict[str, Any]:
    ring_list = [r.strip().lower() for r in (rings or list(VALID_RINGS)) if r.strip().lower() in VALID_RINGS]
    if not ring_list:
        ring_list = list(VALID_RINGS)
    first_ring = ring_list[0]
    rid = "rol-" + uuid.uuid4().hex[:12]
    now_ts = dbmod.now()
    dispatched_first = _dispatch_ring(
        conn,
        rollout_id=rid,
        ring=first_ring,
        tenant=tenant,
        actor=actor,
        tool="update_fleet_agent",
        get_effective_device_policy=get_effective_device_policy,
        get_current_policy=get_current_policy,
        notify=notify,
        audit_fn=audit_fn,
    )
    dispatched = {first_ring: dispatched_first}
    conn.execute(
        "INSERT INTO update_rollouts "
        "(id, name, tenant, target_version, target_commit, manifest_url, current_ring, rings_json, status, max_failure_rate, dispatched_json, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid,
            (name or f"Rollout {target_version or rid}")[:120],
            tenant,
            target_version[:64],
            target_commit[:64],
            manifest_url[:500],
            first_ring,
            json.dumps(ring_list),
            "active",
            max(0.0, min(1.0, float(max_failure_rate))),
            json.dumps(dispatched),
            actor,
            now_ts,
            now_ts,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM update_rollouts WHERE id=?", (rid,)).fetchone()
    return _format_rollout(row)


def advance_rollout(
    conn: Any,
    *,
    rollout_id: str,
    tenant: str,
    actor: str,
    get_effective_device_policy: Callable,
    get_current_policy: Callable,
    notify: Callable,
    audit_fn: Callable,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM update_rollouts WHERE id=? AND tenant=?", (rollout_id, tenant)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Rollout not found.")
    data = _format_rollout(row)
    if data["status"] != "active":
        raise HTTPException(status_code=409, detail=f"Cannot advance rollout in status '{data['status']}'.")

    cur_ring = data["current_ring"]
    rings = data["rings"]
    dispatched = data["dispatched"]
    ring_health = evaluate_ring_health(conn, dispatched.get(cur_ring) or [])
    now_ts = dbmod.now()

    # Health gate: auto-pause if failure rate exceeds configured threshold.
    if ring_health["total"] > 0 and ring_health["failure_rate"] > float(data["max_failure_rate"]):
        conn.execute(
            "UPDATE update_rollouts SET status='paused', updated_at=? WHERE id=?",
            (now_ts, rollout_id),
        )
        conn.commit()
        updated = _format_rollout(conn.execute("SELECT * FROM update_rollouts WHERE id=?", (rollout_id,)).fetchone())
        return {**updated, "health_gate_failed": True, "ring_health": ring_health}

    idx = rings.index(cur_ring) if cur_ring in rings else len(rings) - 1
    if idx + 1 >= len(rings):
        conn.execute("UPDATE update_rollouts SET status='completed', updated_at=? WHERE id=?", (now_ts, rollout_id))
        conn.commit()
        updated = _format_rollout(conn.execute("SELECT * FROM update_rollouts WHERE id=?", (rollout_id,)).fetchone())
        return {**updated, "health_gate_failed": False, "ring_health": ring_health}

    next_ring = rings[idx + 1]
    dispatched[next_ring] = _dispatch_ring(
        conn,
        rollout_id=rollout_id,
        ring=next_ring,
        tenant=tenant,
        actor=actor,
        tool="update_fleet_agent",
        get_effective_device_policy=get_effective_device_policy,
        get_current_policy=get_current_policy,
        notify=notify,
        audit_fn=audit_fn,
    )
    conn.execute(
        "UPDATE update_rollouts SET current_ring=?, dispatched_json=?, updated_at=? WHERE id=?",
        (next_ring, json.dumps(dispatched), now_ts, rollout_id),
    )
    conn.commit()
    updated = _format_rollout(conn.execute("SELECT * FROM update_rollouts WHERE id=?", (rollout_id,)).fetchone())
    return {**updated, "health_gate_failed": False, "ring_health": ring_health}


def pause_rollout(conn: Any, *, rollout_id: str, tenant: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM update_rollouts WHERE id=? AND tenant=?", (rollout_id, tenant)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Rollout not found.")
    if row["status"] not in ("active", "paused"):
        raise HTTPException(status_code=409, detail=f"Cannot pause rollout in status '{row['status']}'.")
    conn.execute("UPDATE update_rollouts SET status='paused', updated_at=? WHERE id=?", (dbmod.now(), rollout_id))
    conn.commit()
    return _format_rollout(conn.execute("SELECT * FROM update_rollouts WHERE id=?", (rollout_id,)).fetchone())


def resume_rollout(conn: Any, *, rollout_id: str, tenant: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM update_rollouts WHERE id=? AND tenant=?", (rollout_id, tenant)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Rollout not found.")
    if row["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"Only paused rollouts can be resumed (status='{row['status']}').")
    conn.execute("UPDATE update_rollouts SET status='active', updated_at=? WHERE id=?", (dbmod.now(), rollout_id))
    conn.commit()
    return _format_rollout(conn.execute("SELECT * FROM update_rollouts WHERE id=?", (rollout_id,)).fetchone())


def rollback_rollout(
    conn: Any,
    *,
    rollout_id: str,
    tenant: str,
    actor: str,
    get_effective_device_policy: Callable,
    get_current_policy: Callable,
    notify: Callable,
    audit_fn: Callable,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM update_rollouts WHERE id=? AND tenant=?", (rollout_id, tenant)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Rollout not found.")
    data = _format_rollout(row)
    touched_devices: list[str] = []
    for ring_entries in (data["dispatched"] or {}).values():
        if isinstance(ring_entries, list):
            for entry in ring_entries:
                did = entry.get("device_id")
                if did and did not in touched_devices:
                    touched_devices.append(did)

    rb_tasks = _dispatch_ring(
        conn,
        rollout_id=rollout_id,
        ring="rollback",
        tenant=tenant,
        actor=actor,
        tool="rollback_fleet_agent",
        get_effective_device_policy=get_effective_device_policy,
        get_current_policy=get_current_policy,
        notify=notify,
        audit_fn=audit_fn,
        device_ids_override=touched_devices,
    )
    dispatched = dict(data["dispatched"] or {})
    dispatched["rollback"] = rb_tasks
    conn.execute(
        "UPDATE update_rollouts SET status='rolled_back', dispatched_json=?, updated_at=? WHERE id=?",
        (json.dumps(dispatched), dbmod.now(), rollout_id),
    )
    conn.commit()
    return _format_rollout(conn.execute("SELECT * FROM update_rollouts WHERE id=?", (rollout_id,)).fetchone())

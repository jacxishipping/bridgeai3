"""Release 4: In-app Notification Centre / Incident Inbox.

Synthesizes deduplicated actionable incidents for one tenant from live
failed/dead-lettered tasks, offline devices, and expired approvals, and
persists acknowledgement, assignment, and resolution history in ``settings``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import HTTPException

from . import db as dbmod

_STATE_PREFIX = "incident.state."
VALID_ACTIONS = ("ack", "assign", "resolve", "reopen")


def _state_key(tenant: str, incident_id: str) -> str:
    return f"{_STATE_PREFIX}{tenant}.{incident_id}"


def _load_incident_states(conn: Any, tenant: str) -> dict[str, dict[str, Any]]:
    prefix = f"{_STATE_PREFIX}{tenant}."
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE ?",
        (prefix + "%",),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        inc_id = str(r["key"])[len(prefix) :]
        try:
            parsed = json.loads(str(r["value"] or "{}"))
            if isinstance(parsed, dict):
                out[inc_id] = parsed
        except Exception:
            continue
    return out


def list_incidents(
    conn: Any,
    *,
    tenant: str,
    include_resolved: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    now_ts = time.time()
    cutoff = now_ts - 7 * 86400
    states = _load_incident_states(conn, tenant)
    items: list[dict[str, Any]] = []

    # 1. Failed / timed-out / dead-lettered tasks (last 7 days)
    task_rows = conn.execute(
        "SELECT id, device_id, tool, status, failure_class, failure_reason, dead_lettered, trace_id, updated_at, created_at "
        "FROM tasks WHERE tenant=? AND (status IN ('failed', 'timed_out') OR dead_lettered=1) AND created_at >= ? "
        "ORDER BY updated_at DESC LIMIT 50",
        (tenant, cutoff),
    ).fetchall()
    for r in task_rows:
        inc_id = f"task:{r['id']}"
        st = states.get(inc_id, {})
        status = str(st.get("status") or "open")
        if status == "resolved" and not include_resolved:
            continue
        items.append({
            "id": inc_id,
            "kind": "failed_task",
            "severity": "high" if r["dead_lettered"] else "medium",
            "title": f"Task {r['tool']} {r['status']}",
            "detail": r["failure_reason"] or r["failure_class"] or f"Task {r['id']} ended in status {r['status']}.",
            "device_id": r["device_id"],
            "task_id": r["id"],
            "trace_id": r["trace_id"] or None,
            "occurred_at": float(r["updated_at"] or r["created_at"] or now_ts),
            "status": status,
            "assignee": st.get("assignee"),
            "acknowledged_by": st.get("acknowledged_by"),
            "resolved_by": st.get("resolved_by"),
            "note": st.get("note") or "",
            "history": st.get("history") or [],
        })

    # 2. Offline devices (non-revoked)
    dev_rows = conn.execute(
        "SELECT id, name, os, status, last_seen, created_at FROM devices "
        "WHERE tenant=? AND revoked=0 AND (status != 'online' OR status IS NULL)",
        (tenant,),
    ).fetchall()
    for r in dev_rows:
        inc_id = f"device:{r['id']}"
        st = states.get(inc_id, {})
        status = str(st.get("status") or "open")
        if status == "resolved" and not include_resolved:
            continue
        last_seen = r["last_seen"]
        detail = (
            f"Device {r['name']} ({r['os'] or 'node'}) is offline; last heartbeat "
            + (f"{int(max(0, now_ts - float(last_seen)))}s ago." if last_seen else "never received.")
        )
        items.append({
            "id": inc_id,
            "kind": "offline_device",
            "severity": "medium",
            "title": f"Device offline: {r['name']}",
            "detail": detail,
            "device_id": r["id"],
            "task_id": None,
            "trace_id": None,
            "occurred_at": float(last_seen or r["created_at"] or now_ts),
            "status": status,
            "assignee": st.get("assignee"),
            "acknowledged_by": st.get("acknowledged_by"),
            "resolved_by": st.get("resolved_by"),
            "note": st.get("note") or "",
            "history": st.get("history") or [],
        })

    # 3. Expired approvals (joined with tasks for device_id / tier context)
    ap_rows = conn.execute(
        "SELECT a.id, a.task_id, a.tool, a.reason, a.status, a.created_at, a.expires_at, "
        "t.device_id AS device_id, t.tier AS tier "
        "FROM approvals a LEFT JOIN tasks t ON t.id = a.task_id "
        "WHERE a.tenant=? AND (a.status='expired' OR (a.status='pending' AND a.expires_at IS NOT NULL AND a.expires_at < ?)) "
        "ORDER BY a.created_at DESC LIMIT 50",
        (tenant, now_ts),
    ).fetchall()
    for r in ap_rows:
        inc_id = f"approval:{r['id']}"
        st = states.get(inc_id, {})
        status = str(st.get("status") or "open")
        if status == "resolved" and not include_resolved:
            continue
        items.append({
            "id": inc_id,
            "kind": "expired_approval",
            "severity": "low" if (r["tier"] or 3) < 4 else "medium",
            "title": f"Expired approval: {r['tool'] or 'task'} (Tier {r['tier'] or 3})",
            "detail": f"Approval {r['id']} for device {r['device_id'] or 'local'} expired before operator review.",
            "device_id": r["device_id"] or None,
            "task_id": r["task_id"] or None,
            "trace_id": None,
            "occurred_at": float(r["expires_at"] or r["created_at"] or now_ts),
            "status": status,
            "assignee": st.get("assignee"),
            "acknowledged_by": st.get("acknowledged_by"),
            "resolved_by": st.get("resolved_by"),
            "note": st.get("note") or "",
            "history": st.get("history") or [],
        })

    items.sort(key=lambda x: x["occurred_at"], reverse=True)
    capped = items[: max(1, min(200, int(limit or 100)))]
    counts = {
        "open": sum(1 for i in capped if i["status"] == "open"),
        "acknowledged": sum(1 for i in capped if i["status"] == "acknowledged"),
        "resolved": sum(1 for i in capped if i["status"] == "resolved"),
        "failed_task": sum(1 for i in capped if i["kind"] == "failed_task"),
        "offline_device": sum(1 for i in capped if i["kind"] == "offline_device"),
        "expired_approval": sum(1 for i in capped if i["kind"] == "expired_approval"),
    }
    return {"tenant": tenant, "count": len(capped), "counts": counts, "items": capped}


def update_incident_state(
    conn: Any,
    *,
    tenant: str,
    incident_id: str,
    actor: str,
    action: str,
    assignee: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    norm_action = (action or "").strip().lower()
    if norm_action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action '{action}'. Expected one of: {', '.join(VALID_ACTIONS)}.")
    clean_id = (incident_id or "").strip()
    if not clean_id or len(clean_id) > 128 or ":" not in clean_id:
        raise HTTPException(status_code=400, detail="Invalid incident_id.")

    key = _state_key(tenant, clean_id)
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    state: dict[str, Any] = {}
    if row is not None:
        try:
            loaded = json.loads(str(row["value"] or "{}"))
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {}

    now_ts = dbmod.now()
    history = list(state.get("history") or [])
    if norm_action == "ack":
        state["status"] = "acknowledged"
        state["acknowledged_by"] = actor
        state["acknowledged_at"] = now_ts
    elif norm_action == "assign":
        if not (assignee or "").strip():
            raise HTTPException(status_code=400, detail="assignee is required for assign action.")
        state["assignee"] = assignee.strip()[:120]
        if state.get("status", "open") == "open":
            state["status"] = "acknowledged"
    elif norm_action == "resolve":
        state["status"] = "resolved"
        state["resolved_by"] = actor
        state["resolved_at"] = now_ts
    elif norm_action == "reopen":
        state["status"] = "open"
        state["resolved_by"] = None
        state["resolved_at"] = None

    if note.strip():
        state["note"] = note.strip()[:500]
    history.append({"action": norm_action, "actor": actor, "assignee": state.get("assignee"), "note": note.strip()[:200], "ts": now_ts})
    state["history"] = history[-25:]
    state["updated_at"] = now_ts

    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, json.dumps(state)),
    )
    conn.commit()
    return {"incident_id": clean_id, "tenant": tenant, **state}

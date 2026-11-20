"""Task lifecycle maintenance: leases, progress heartbeats, retries, dead-letter queue, and failure classification."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from ..policy import is_tool_idempotent
from . import db as dbmod
from .plane_state import is_emergency_stop

DEFAULT_RUNNING_LEASE_SEC = int(os.environ.get("AI_BRIDGE_TASK_RUNNING_LEASE_SEC", "900"))
DEFAULT_SWEEP_INTERVAL_SEC = int(os.environ.get("AI_BRIDGE_TASK_SWEEP_INTERVAL_SEC", "30"))
DEFAULT_RETRY_BASE_BACKOFF_SEC = float(os.environ.get("AI_BRIDGE_TASK_RETRY_BACKOFF_SEC", "5.0"))
MAX_RETRY_BACKOFF_SEC = float(os.environ.get("AI_BRIDGE_TASK_MAX_RETRY_BACKOFF_SEC", "300.0"))

_TRANSIENT_HINTS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "network",
    "503",
    "502",
    "504",
    "rate limit",
    "busy",
    "try again",
)
_POLICY_HINTS = (
    "policy",
    "denied",
    "forbidden",
    "approval",
    "unauthorized",
    "not allowed",
    "blocked",
)


def retry_backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff between automatic task retry attempts."""
    exp = max(0, int(attempt) - 1)
    return min(MAX_RETRY_BACKOFF_SEC, DEFAULT_RETRY_BASE_BACKOFF_SEC * (2 ** min(exp, 6)))


def classify_failure(status: str, result_text: str = "") -> tuple[str, str]:
    """Classify a non-completed task outcome into ``(failure_class, failure_reason)``.

    ``failure_class`` is one of: ``transient``, ``permanent``, ``policy``,
    ``timeout``, ``agent_crash``, ``cancelled``.
    """
    st = str(status or "").strip().lower()
    raw = str(result_text or "")
    reason = ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            reason = str(parsed.get("error") or parsed.get("detail") or parsed.get("failure_reason") or parsed.get("message") or "").strip()
            explicit_cls = str(parsed.get("failure_class") or "").strip().lower()
            if explicit_cls in {"transient", "permanent", "policy", "timeout", "agent_crash", "cancelled"}:
                return explicit_cls, (reason or st)[:500]
    except Exception:
        pass
    if not reason:
        reason = raw.strip()[:500]
    lower = f"{st} {reason}".lower()
    if st == "cancelled":
        return "cancelled", (reason or "task_cancelled")[:500]
    if st == "timed_out":
        return "timeout", (reason or "running_lease_expired")[:500]
    if any(h in lower for h in _POLICY_HINTS):
        return "policy", (reason or "policy_denied")[:500]
    if any(h in lower for h in _TRANSIENT_HINTS):
        return "transient", (reason or "transient_error")[:500]
    return "permanent", (reason or "execution_failed")[:500]


def device_has_active_lease(conn: Any, device_id: str, *, lease_sec: int | None = None) -> bool:
    """True when a fresh 'running' task still holds the per-device execution lease."""
    lease = lease_sec if lease_sec is not None else DEFAULT_RUNNING_LEASE_SEC
    now_ts = dbmod.now()
    cutoff = now_ts - lease
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE device_id=? AND status='running' "
            "AND ((lease_expires_at IS NOT NULL AND lease_expires_at >= ?) "
            "OR (lease_expires_at IS NULL AND updated_at >= ?)) LIMIT 1",
            (device_id, now_ts, cutoff),
        ).fetchone()
    except Exception:
        row = conn.execute(
            "SELECT id FROM tasks WHERE device_id=? AND status='running' AND updated_at >= ? LIMIT 1",
            (device_id, cutoff),
        ).fetchone()
    return row is not None


def record_task_progress(
    conn: Any,
    task_id: str,
    *,
    device_id: str | None = None,
    progress_pct: float | None = None,
    status_message: str | None = None,
    lease_sec: int | None = None,
) -> bool:
    """Record that the agent owning ``task_id`` is still making progress.

    Bumps ``tasks.updated_at`` and ``tasks.lease_expires_at`` (and persists
    optional ``progress_pct`` / ``status_message`` + a ``"progress"`` row in
    ``task_events``) iff the task is still ``status='running'``; returns True
    in that case, False when the task is no longer running.
    """
    from .task_events import append_task_event

    now = dbmod.now()
    lease = lease_sec if lease_sec is not None else DEFAULT_RUNNING_LEASE_SEC
    lease_expires_at = now + lease
    msg_clean = (status_message or "")[:500] if status_message is not None else None

    sets = ["updated_at=?"]
    params: list[Any] = [now]
    # Include Release 2 columns when present on the schema.
    has_r2_cols = True
    try:
        sets.append("lease_expires_at=?")
        params.append(lease_expires_at)
        if progress_pct is not None:
            sets.append("progress_pct=?")
            params.append(float(progress_pct))
        if msg_clean is not None:
            sets.append("status_message=?")
            params.append(msg_clean)
        where = "WHERE id=? AND status='running'"
        params.append(task_id)
        if device_id:
            where += " AND device_id=?"
            params.append(device_id)
        cur = conn.execute(f"UPDATE tasks SET {', '.join(sets)} {where}", tuple(params))
    except Exception:
        has_r2_cols = False
        if device_id:
            cur = conn.execute(
                "UPDATE tasks SET updated_at=? WHERE id=? AND status='running' AND device_id=?",
                (now, task_id, device_id),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET updated_at=? WHERE id=? AND status='running'",
                (now, task_id),
            )
    if cur.rowcount != 1:
        return False

    trace_id = ""
    if has_r2_cols:
        try:
            trow = conn.execute("SELECT trace_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if trow is not None:
                trace_id = str(trow["trace_id"] if hasattr(trow, "keys") else trow[0] or "")
        except Exception:
            trace_id = ""

    detail: dict[str, Any] = {"at": now}
    if progress_pct is not None:
        detail["progress_pct"] = float(progress_pct)
    if msg_clean:
        detail["status_message"] = msg_clean
    append_task_event(conn, task_id, "progress", detail, trace_id=trace_id, commit=False)
    conn.commit()
    return True


def record_dead_letter(
    conn: Any,
    task: dict[str, Any],
    *,
    failure_reason: str,
    failure_class: str,
    last_error: str = "",
    commit: bool = False,
) -> str:
    """Persist an exhausted/terminal task into ``task_dead_letters``."""
    dlq_id = "dlq-" + uuid.uuid4().hex[:12]
    now = dbmod.now()
    task_id = str(task.get("id") or "")
    conn.execute(
        "INSERT INTO task_dead_letters "
        "(id, task_id, device_id, tenant, tool, args_json, attempts, failure_reason, failure_class, last_error, trace_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            dlq_id,
            task_id,
            str(task.get("device_id") or ""),
            str(task.get("tenant") or "default"),
            str(task.get("tool") or ""),
            str(task.get("args_json") or "{}"),
            int(task.get("attempt") or task.get("epoch") or 1),
            (failure_reason or "")[:500],
            (failure_class or "permanent")[:64],
            (last_error or "")[:4000],
            str(task.get("trace_id") or "")[:128],
            now,
        ),
    )
    try:
        conn.execute("UPDATE tasks SET dead_lettered=1 WHERE id=?", (task_id,))
    except Exception:
        pass
    try:
        from .task_events import append_task_event

        append_task_event(
            conn,
            task_id,
            "dead_lettered",
            {"dlq_id": dlq_id, "failure_class": failure_class, "failure_reason": failure_reason[:200]},
            trace_id=str(task.get("trace_id") or ""),
            commit=False,
        )
    except Exception:
        pass
    if commit:
        conn.commit()
    return dlq_id


def list_dead_letters(
    conn: Any,
    *,
    tenant: str,
    limit: int = 100,
    include_replayed: bool = False,
) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    if include_replayed:
        rows = conn.execute(
            "SELECT * FROM task_dead_letters WHERE tenant=? ORDER BY created_at DESC LIMIT ?",
            (tenant, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_dead_letters WHERE tenant=? AND replayed_task_id IS NULL ORDER BY created_at DESC LIMIT ?",
            (tenant, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def sweep_stale_running_tasks(
    conn: Any,
    *,
    lease_sec: int | None = None,
    requeue: bool | None = None,
) -> dict[str, int]:
    """Reclaim tasks stuck in 'running' longer than the lease (agent crash / network loss).

    Retry policy:
    - If a task has remaining retry budget (``attempt < max_attempts``) AND is
      idempotent (:func:`ai_bridge.policy.is_tool_idempotent`) — or if global
      ``requeue=True`` / ``AI_BRIDGE_STALE_TASK_REQUEUE=1`` is set — requeue it
      with exponential backoff (``next_attempt_at``) unless emergency stop is active.
    - Otherwise transition to ``timed_out`` with structured ``failure_class`` /
      ``failure_reason``, record a ``task_dead_letters`` entry when retry budget
      was exhausted, emit a ``task_events`` entry, update metrics, and advance
      any waiting supervisor handoff.
    """
    from .task_events import append_task_event

    lease = lease_sec if lease_sec is not None else DEFAULT_RUNNING_LEASE_SEC
    if requeue is None:
        requeue = os.environ.get("AI_BRIDGE_STALE_TASK_REQUEUE", "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    now_ts = dbmod.now()
    cutoff = now_ts - lease
    try:
        rows = conn.execute(
            "SELECT id, device_id, tenant, tool, args_json, attempt, max_attempts, epoch, trace_id, created_at "
            "FROM tasks WHERE status='running' "
            "AND ((lease_expires_at IS NOT NULL AND lease_expires_at < ?) "
            "OR (lease_expires_at IS NULL AND updated_at < ?))",
            (now_ts, cutoff),
        ).fetchall()
    except Exception:
        rows = conn.execute(
            "SELECT id, device_id, tenant, tool, args_json FROM tasks WHERE status='running' AND updated_at < ?",
            (cutoff,),
        ).fetchall()

    timed_out = 0
    requeued = 0
    dead_lettered = 0
    estop = is_emergency_stop(conn)

    for row in rows:
        d = dict(row) if hasattr(row, "keys") else {
            "id": row[0],
            "device_id": row[1],
            "tenant": "default",
            "tool": row[2] if len(row) > 2 else "",
            "args_json": row[3] if len(row) > 3 else "{}",
        }
        tid = str(d["id"])
        tool = str(d.get("tool") or "")
        attempt = int(d.get("attempt") or d.get("epoch") or 1)
        max_attempts = max(1, int(d.get("max_attempts") or 1))
        trace_id = str(d.get("trace_id") or "")

        can_auto_retry = (
            not estop
            and (
                (attempt < max_attempts and is_tool_idempotent(tool))
                or (requeue and (max_attempts == 1 or attempt < max_attempts))
            )
        )

        if can_auto_retry:
            delay = retry_backoff_seconds(attempt) if attempt < max_attempts else 0.0
            next_at = now_ts + delay if delay > 0 else None
            note = json.dumps(
                {
                    "note": "Requeued after stale running lease expired.",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "backoff_seconds": round(delay, 2),
                }
            )[:2000]
            try:
                cur = conn.execute(
                    "UPDATE tasks SET status='queued', result=?, failure_class='agent_crash', "
                    "failure_reason='lease_expired_retrying', lease_expires_at=NULL, next_attempt_at=?, updated_at=? "
                    "WHERE id=? AND status='running'",
                    (note, next_at, now_ts, tid),
                )
            except Exception:
                cur = conn.execute(
                    "UPDATE tasks SET status='queued', result=?, updated_at=? WHERE id=? AND status='running'",
                    (note, now_ts, tid),
                )
            if cur.rowcount == 1:
                requeued += 1
                try:
                    append_task_event(
                        conn,
                        tid,
                        "requeued",
                        {"reason": "lease_expired", "attempt": attempt, "backoff_seconds": round(delay, 2)},
                        trace_id=trace_id,
                        commit=False,
                    )
                except Exception:
                    pass
        else:
            err_msg = "Task exceeded running lease without completion."
            res_json = json.dumps({"error": err_msg, "failure_class": "timeout", "attempt": attempt})[:2000]
            try:
                cur = conn.execute(
                    "UPDATE tasks SET status='timed_out', result=?, failure_class='timeout', "
                    "failure_reason='running_lease_expired', lease_expires_at=NULL, updated_at=? "
                    "WHERE id=? AND status='running'",
                    (res_json, now_ts, tid),
                )
            except Exception:
                cur = conn.execute(
                    "UPDATE tasks SET status='timed_out', result=?, updated_at=? WHERE id=? AND status='running'",
                    (res_json, now_ts, tid),
                )
            if cur.rowcount == 1:
                timed_out += 1
                try:
                    append_task_event(
                        conn,
                        tid,
                        "timed_out",
                        {"failure_class": "timeout", "failure_reason": "running_lease_expired", "attempt": attempt},
                        trace_id=trace_id,
                        commit=False,
                    )
                except Exception:
                    pass
                # Move to DLQ when retries were configured and exhausted, or when configured globally.
                dlq_always = os.environ.get("AI_BRIDGE_DEAD_LETTER_ON_TIMEOUT", "1").strip().lower() in ("1", "true", "yes")
                if max_attempts > 1 or dlq_always:
                    try:
                        record_dead_letter(
                            conn,
                            d,
                            failure_reason="running_lease_expired",
                            failure_class="timeout",
                            last_error=err_msg,
                            commit=False,
                        )
                        dead_lettered += 1
                    except Exception:
                        pass
                try:
                    from . import metrics as metricsmod

                    created_at = float(d.get("created_at") or now_ts)
                    metricsmod.record_task_execution(tool or "unknown", "timed_out", max(0.0, now_ts - created_at))
                except Exception:
                    pass
                try:
                    from . import supervisor as supervisormod

                    fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                    if fresh is not None:
                        supervisormod.advance_handoff(conn, fresh, status="timed_out")
                except Exception:
                    pass

    if timed_out or requeued:
        conn.commit()
    summary: dict[str, int] = {
        "timed_out": timed_out,
        "requeued": requeued,
        "scanned": len(rows),
    }
    if dead_lettered:
        summary["dead_lettered"] = dead_lettered
    return summary

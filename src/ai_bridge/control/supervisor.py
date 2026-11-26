"""Thin supervisor orchestration across role_catalog presets (Phase 6 / MAI)."""
from __future__ import annotations

import contextlib
import json
import logging
import uuid
from contextvars import ContextVar
from typing import Any

from ..policy import TOOL_TIERS
from . import db as dbmod
from . import task_service as task_service_mod
from .role_catalog import ROLES

# Control-plane connection for dispatch-side supervisor tools (handoff / recover).
_PLANE_CONN: ContextVar[Any | None] = ContextVar("ai_bridge_plane_conn", default=None)


def set_plane_conn(conn: Any | None):
    """Bind the control-plane DB for in-process supervisor dispatch tools."""
    return _PLANE_CONN.set(conn)


def reset_plane_conn(token) -> None:
    _PLANE_CONN.reset(token)


def get_plane_conn() -> Any | None:
    return _PLANE_CONN.get()


HANDOFF_PROFILES: dict[str, list[dict[str, Any]]] = {
    "code": [
        {
            "name": "Planner — survey workspace",
            "role": "planner",
            "tool": "dev_workspace_plan",
            "args": {"relative_directory": "."},
        },
        {
            "name": "Developer — git status",
            "role": "developer",
            "tool": "git_status",
            "args": {"relative_directory": "."},
        },
        {
            "name": "Tester — structured tests",
            "role": "tester",
            "tool": "run_project_tests_structured",
            "args": {"test_command": "pytest -q"},
        },
    ],
    "research": [
        {
            "name": "Planner — survey workspace",
            "role": "planner",
            "tool": "dev_workspace_plan",
            "args": {"relative_directory": "."},
        },
        {
            "name": "Researcher — search workspace",
            "role": "researcher",
            "tool": "search_workspace",
            "args": {"query": "architecture overview", "relative_directory": "."},
        },
        {
            "name": "Browser Operator — session check",
            "role": "browser_operator",
            "tool": "browser_session_info",
            "args": {},
        },
    ],
    "desktop": [
        {
            "name": "Computer Operator — screenshot",
            "role": "computer_operator",
            "tool": "capture_desktop_screenshot",
            "args": {},
        },
        {
            "name": "Tester — desktop verify",
            "role": "tester",
            "tool": "desktop_verify_action",
            "args": {"expected_text": "", "capture_after": True},
        },
        {
            "name": "Security Auditor — recent logs",
            "role": "security_auditor",
            "tool": "get_recent_logs",
            "args": {"source": "system", "lines": 30},
        },
    ],
    "browser": [
        {
            "name": "Browser Operator — session",
            "role": "browser_operator",
            "tool": "browser_session_info",
            "args": {},
        },
        {
            "name": "Tester — browser diagnostics",
            "role": "tester",
            "tool": "browser_diagnostics",
            "args": {},
        },
    ],
    "full": [
        {
            "name": "Planner — survey workspace",
            "role": "planner",
            "tool": "dev_workspace_plan",
            "args": {"relative_directory": "."},
        },
        {
            "name": "Researcher — search workspace",
            "role": "researcher",
            "tool": "search_workspace",
            "args": {"query": "security risks", "relative_directory": "."},
        },
        {
            "name": "Developer — git status",
            "role": "developer",
            "tool": "git_status",
            "args": {"relative_directory": "."},
        },
        {
            "name": "Tester — structured tests",
            "role": "tester",
            "tool": "run_project_tests_structured",
            "args": {"test_command": "pytest -q"},
        },
        {
            "name": "Security Auditor — audit logs",
            "role": "security_auditor",
            "tool": "get_recent_logs",
            "args": {"source": "system", "lines": 40},
        },
        {
            "name": "Computer Operator — screenshot",
            "role": "computer_operator",
            "tool": "capture_desktop_screenshot",
            "args": {},
        },
        {
            "name": "Browser Operator — session",
            "role": "browser_operator",
            "tool": "browser_session_info",
            "args": {},
        },
    ],
}

RECOVERY_STEP: dict[str, Any] = {
    "name": "Recovery — service health",
    "role": "recovery",
    "tool": "get_service_status",
    "args": {"service": "ai-bridge"},
}


def role_agent_id(role: str) -> str:
    role = (role or "").strip().lower()
    if role not in ROLES:
        raise ValueError(f"Unknown role '{role}'. Choose one of: {', '.join(sorted(ROLES))}.")
    return f"agent-role-{role}"


def tag_task_args(
    args: dict | None = None,
    *,
    role: str | None = None,
    handoff_id: str | None = None,
    parent_task_id: str | None = None,
    goal: str | None = None,
    step_index: int | None = None,
    profile: str | None = None,
    mode: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Standardize handoff/parent/role metadata on task args_json."""
    out = dict(args or {})
    if extra:
        out.update(extra)
    if role:
        role_l = str(role).strip().lower()
        out["_role"] = role_l
        out["_role_agent"] = role_agent_id(role_l)
    if handoff_id:
        out["_handoff_id"] = handoff_id
    if parent_task_id:
        out["_parent_task_id"] = parent_task_id
    if goal is not None:
        out["_goal"] = goal
    if step_index is not None:
        out["_step_index"] = int(step_index)
    if profile:
        out["_profile"] = profile
    if mode:
        out["_mode"] = mode
    return out


def enqueue_role_task(
    conn: Any,
    *,
    device_id: str,
    tenant: str,
    tool: str,
    args: dict | None = None,
    role: str | None = None,
    status: str = "queued",
    handoff_id: str | None = None,
    parent_task_id: str | None = None,
    goal: str | None = None,
    step_index: int | None = None,
    profile: str | None = None,
    mode: str | None = None,
    commit: bool = True,
    actor: str = "supervisor",
) -> dict:
    """Queue a plane task through :func:`task_service.create_task` with role/handoff metadata.

    Routes through the unified policy, emergency-stop, takeover, tenant, and
    approval-gating pipeline while writing indexed ``tasks.handoff_id`` /
    ``tasks.parent_task_id`` columns (migration 015, PERF-07). ``commit=False``
    keeps the insert inside the caller's active transaction (used by the
    transactional stuck-handoff sweeper).
    """
    _ = status  # Final status ('queued' or 'needs_approval') is determined by task_service policy.
    if tool not in TOOL_TIERS:
        raise ValueError(f"Tool not in policy catalog: {tool}")
    tagged = tag_task_args(
        args,
        role=role,
        handoff_id=handoff_id,
        parent_task_id=parent_task_id,
        goal=goal,
        step_index=step_index,
        profile=profile,
        mode=mode,
    )
    if role and not tagged.get("_role"):
        tagged = tag_task_args(tagged, role=role)
    created = task_service_mod.create_task(
        conn,
        actor=actor,
        role="operator",
        tenant=tenant,
        device_id=device_id,
        tool=tool,
        args=tagged,
        handoff_id=handoff_id,
        parent_task_id=parent_task_id,
        commit=commit,
    )
    return {
        "ok": True,
        "task_id": created["task_id"],
        "status": created["status"],
        "approval_id": created.get("approval_id"),
        "tool": tool,
        "role": tagged.get("_role") or role,
        "args": tagged,
        "handoff_id": tagged.get("_handoff_id"),
        "step_index": tagged.get("_step_index"),
    }


def supervisor_handoff_plan(goal: str, profile: str = "code") -> list[dict]:
    """Deterministic role tool sequence for a named handoff profile."""
    goal = (goal or "").strip() or "unspecified goal"
    profile = (profile or "code").strip().lower()
    if profile not in HANDOFF_PROFILES:
        raise ValueError(f"Unknown handoff profile '{profile}'. Choose one of: {', '.join(sorted(HANDOFF_PROFILES))}.")
    steps: list[dict] = []
    for step in HANDOFF_PROFILES[profile]:
        args = dict(step.get("args") or {})
        args["goal"] = goal
        steps.append(
            {
                "name": step["name"],
                "role": step["role"],
                "tool": step["tool"],
                "args": args,
            }
        )
    return steps


def run_supervisor_handoff_enqueue(
    conn: Any,
    *,
    device_id: str,
    tenant: str,
    goal: str,
    profile: str = "code",
    mode: str = "gated",
    custom_steps: list[dict[str, Any]] | None = None,
) -> dict:
    """Enqueue a supervised handoff. Default mode=gated enqueues only the first step."""
    profile = (profile or "code").strip().lower()
    mode = (mode or "gated").strip().lower()
    if mode not in {"gated", "fanout"}:
        raise ValueError("mode must be 'gated' or 'fanout'")
    if custom_steps and isinstance(custom_steps, list):
        steps = []
        for idx, st in enumerate(custom_steps):
            if not isinstance(st, dict):
                continue
            tool_name = str(st.get("tool") or st.get("action") or "").strip()
            role_name = str(st.get("role") or "computer_operator").strip().lower()
            s_args = dict(st.get("args") or {})
            s_args.setdefault("goal", goal)
            steps.append({
                "step_id": str(st.get("step_id") or f"step_{idx + 1}"),
                "name": str(st.get("name") or f"{role_name}:{tool_name}"),
                "role": role_name,
                "device_id": str(st.get("device_id") or device_id),
                "tool": tool_name,
                "args": s_args,
            })
        if profile not in HANDOFF_PROFILES:
            profile = "code"
    elif profile == "auto":
        dag = plan_goal_dag(conn, goal=goal, device_id=device_id, tenant=tenant, profile="auto")
        steps = dag["steps"]
        profile = dag["profile"]
    else:
        steps = supervisor_handoff_plan(goal, profile=profile)
    handoff_id = uuid.uuid4().hex[:12]
    if custom_steps or profile:
        save_custom_handoff_plan(conn, handoff_id, steps)
    queued: list[dict] = []
    to_enqueue = steps if mode == "fanout" else steps[:1]
    for idx, step in enumerate(to_enqueue):
        target_dev = str(step.get("device_id") or device_id)
        step_args = dict(step.get("args") or {})
        step_args["_step_id"] = step.get("step_id") or f"step_{idx + 1}"
        res = enqueue_role_task(
            conn,
            device_id=target_dev,
            tenant=tenant,
            tool=step["tool"],
            args=step_args,
            role=str(step.get("role") or ""),
            handoff_id=handoff_id,
            parent_task_id=None,
            goal=goal,
            step_index=idx if mode == "fanout" else 0,
            profile=profile,
            mode=mode,
        )
        queued.append({**res, "name": step.get("name"), "device_id": target_dev})
    return {
        "ok": True,
        "goal": goal,
        "profile": profile,
        "mode": mode,
        "handoff_id": handoff_id,
        "total_steps": len(steps),
        "queued": queued,
    }


def _parse_args(row: Any) -> dict:
    raw = row["args_json"] if hasattr(row, "keys") else (row.get("args_json") if isinstance(row, dict) else None)
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


def advance_handoff(conn: Any, task: Any, *, status: str, commit: bool = True) -> dict | None:
    """On gated handoff task completion/failure, enqueue the next step or Recovery.

    ``commit=False`` leaves the enqueue uncommitted so the caller (the
    stuck-handoff sweeper) can commit the child-existence re-check and
    the advance in ONE transaction — that is what makes the advance
    transactional (DIST-06): a crash between the re-check and the
    enqueue cannot double-enqueue, and a crash between the task-result
    commit and this call is repaired by :func:`sweep_stuck_handoffs`.
    The default True preserves the old immediate-commit behaviour for
    the synchronous task_result / custom-agent callers.
    """
    args = _parse_args(task)
    mode = str(args.get("_mode") or "").lower()
    if mode != "gated":
        return None
    handoff_id = str(args.get("_handoff_id") or "").strip()
    profile = str(args.get("_profile") or "code").strip().lower()
    if not handoff_id or profile not in HANDOFF_PROFILES:
        return None
    goal = str(args.get("_goal") or args.get("goal") or "")
    step_index = int(args.get("_step_index") or 0)
    device_id = task["device_id"] if hasattr(task, "keys") else task.get("device_id")
    tenant = task["tenant"] if hasattr(task, "keys") else task.get("tenant", "default")
    parent_id = task["id"] if hasattr(task, "keys") else task.get("id")
    status_l = (status or "").strip().lower()

    if status_l == "completed":
        plan = load_custom_handoff_plan(conn, handoff_id) or supervisor_handoff_plan(goal, profile=profile)
        next_idx = step_index + 1
        # Update shared multi-agent blackboard with completed step output
        raw_res = task["result"] if hasattr(task, "keys") else (task.get("result") if isinstance(task, dict) else "")
        step_id = str(args.get("_step_id") or f"step_{step_index + 1}")
        update_handoff_blackboard(
            conn,
            handoff_id,
            step_id=step_id,
            step_index=step_index,
            role=str(args.get("_role") or ""),
            tool=str(task["tool"] if hasattr(task, "keys") else task.get("tool") or ""),
            status="completed",
            output=raw_res,
        )
        if next_idx >= len(plan):
            return {"ok": True, "handoff_id": handoff_id, "advanced": False, "reason": "handoff_complete"}
        step = plan[next_idx]
        bb = get_handoff_blackboard(conn, handoff_id)
        resolved_args = resolve_blackboard_bindings(step.get("args") or {}, bb)
        target_dev = str(step.get("device_id") or device_id)
        res = enqueue_role_task(
            conn,
            device_id=target_dev,
            tenant=tenant,
            tool=step["tool"],
            args={**resolved_args, "_step_id": step.get("step_id") or f"step_{next_idx + 1}"},
            role=str(step.get("role") or ""),
            handoff_id=handoff_id,
            parent_task_id=parent_id,
            goal=goal,
            step_index=next_idx,
            profile=profile,
            mode="gated",
            commit=commit,
        )
        return {
            "ok": True,
            "handoff_id": handoff_id,
            "advanced": True,
            "next_step": next_idx,
            "queued": res,
        }

    if status_l in {"failed", "timed_out", "cancelled"}:
        # Avoid infinite recovery loops when Recovery itself fails.
        if str(args.get("_role") or "").lower() == "recovery":
            return {"ok": True, "handoff_id": handoff_id, "advanced": False, "reason": "recovery_failed"}
        step = RECOVERY_STEP
        res = enqueue_role_task(
            conn,
            device_id=device_id,
            tenant=tenant,
            tool=step["tool"],
            args={
                **dict(step.get("args") or {}),
                "_failed_task_id": parent_id,
                "_failed_role": args.get("_role"),
                "_failed_tool": task["tool"] if hasattr(task, "keys") else task.get("tool"),
            },
            role=str(step.get("role") or "recovery"),
            handoff_id=handoff_id,
            parent_task_id=parent_id,
            goal=goal,
            step_index=step_index + 1,
            profile=profile,
            mode="gated",
            commit=commit,
        )
        return {
            "ok": True,
            "handoff_id": handoff_id,
            "advanced": True,
            "recovery": True,
            "queued": res,
        }
    return None


def _has_handoff_columns(conn: Any) -> bool:
    """True when tasks.handoff_id / parent_task_id exist (migration 015)."""
    try:
        dialect = getattr(conn, "dialect", "sqlite")
        if dialect in ("postgres", "postgresql"):
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='tasks' AND column_name='handoff_id'"
            ).fetchone()
            return row is not None
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        return "handoff_id" in cols and "parent_task_id" in cols
    except Exception:
        return False


def _handoff_child_exists(conn: Any, handoff_id: str, parent_task_id: str) -> bool:
    """True when a child task was already enqueued for this parent step."""
    try:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE handoff_id=? AND parent_task_id=? LIMIT 1",
            (handoff_id, parent_task_id),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def sweep_stuck_handoffs(conn: Any, *, limit: int = 50) -> dict[str, Any]:
    """Re-drive gated handoffs stalled by a crash (DIST-06).

    The synchronous path (task_result -> advance_handoff) can be
    interrupted by a crash between the result commit and the advance:
    the step is terminal, the next step was never enqueued, and the
    handoff stalls forever. This sweeper finds gated tasks in a terminal
    status whose next step is still missing and advances them.

    For each candidate the child-existence re-check and the advance run
    inside ONE transaction (``advance_handoff(..., commit=False)``), so
    two workers sweeping concurrently cannot double-enqueue the same
    step. Tasks that are complete (no next step) or whose recovery
    already failed are skipped, never re-advanced. Per-task errors are
    counted, never fatal to the sweep. Returns
    ``{"scanned", "advanced", "skipped", "errors"}``.
    """
    log = logging.getLogger("ai_bridge.supervisor")
    if not _has_handoff_columns(conn):
        return {"scanned": 0, "advanced": 0, "skipped": 0, "errors": ["handoff columns missing (migration 015 not applied)"]}
    rows = conn.execute(
        "SELECT id, device_id, tenant, tool, args_json, status, updated_at FROM tasks "
        "WHERE handoff_id IS NOT NULL AND handoff_id != '' "
        "AND status IN ('completed', 'failed', 'timed_out', 'cancelled') "
        "ORDER BY updated_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    scanned = len(rows)
    advanced = 0
    skipped = 0
    errors: list[str] = []
    txn = getattr(conn, "transaction", None)
    for row in rows:
        tid = row["id"] if hasattr(row, "keys") else row[0]
        status = row["status"] if hasattr(row, "keys") else row[5]
        try:
            args = _parse_args(row)
            if str(args.get("_mode") or "").lower() != "gated":
                skipped += 1
                continue
            handoff_id = str(args.get("_handoff_id") or "").strip()
            profile = str(args.get("_profile") or "code").strip().lower()
            step_index = int(args.get("_step_index") or 0)
            if not handoff_id or profile not in HANDOFF_PROFILES:
                skipped += 1
                continue
            # Nothing left to advance?
            if status == "completed":
                plan = supervisor_handoff_plan(str(args.get("_goal") or args.get("goal") or ""), profile=profile)
                if step_index + 1 >= len(plan):
                    skipped += 1
                    continue
            elif str(args.get("_role") or "").lower() == "recovery":
                skipped += 1
                continue
            if txn is not None:
                with txn():
                    if _handoff_child_exists(conn, handoff_id, tid):
                        skipped += 1
                        continue
                    res = advance_handoff(conn, row, status=status, commit=False)
            else:
                if _handoff_child_exists(conn, handoff_id, tid):
                    skipped += 1
                    continue
                res = advance_handoff(conn, row, status=status, commit=False)
                conn.commit()
            if res and res.get("advanced"):
                advanced += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 - sweeper must not die
            log.warning("sweep_stuck_handoffs: task %s failed: %s", tid, exc)
            errors.append(f"{tid}: {exc}")
            with contextlib.suppress(Exception):
                conn.rollback()
    return {"scanned": scanned, "advanced": advanced, "skipped": skipped, "errors": errors}


def handoff_status(conn: Any, handoff_id: str) -> dict:
    """Summarize child tasks for a handoff id.

    PERF-07: queries ``WHERE handoff_id=?`` on the migration-015 index
    instead of full-table-scanning tasks and filtering on args_json in
    Python. Falls back to the old scan only on databases predating
    migration 015 (no handoff_id column).
    """
    handoff_id = (handoff_id or "").strip()
    if not handoff_id:
        raise ValueError("handoff_id is required")
    indexed = _has_handoff_columns(conn)
    if indexed:
        rows = conn.execute(
            "SELECT id, device_id, tool, status, args_json, created_at, updated_at, result FROM tasks "
            "WHERE handoff_id=? ORDER BY created_at ASC",
            (handoff_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, device_id, tool, status, args_json, created_at, updated_at, result FROM tasks "
            "ORDER BY created_at ASC"
        ).fetchall()
    children: list[dict] = []
    blocked_reason = None
    for row in rows:
        args = _parse_args(row)
        if not indexed and str(args.get("_handoff_id") or "") != handoff_id:
            continue
        item = {
            "task_id": row["id"],
            "tool": row["tool"],
            "status": row["status"],
            "role": args.get("_role"),
            "step_index": args.get("_step_index"),
            "parent_task_id": args.get("_parent_task_id"),
            "profile": args.get("_profile"),
            "mode": args.get("_mode"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        children.append(item)
        if row["status"] in {"failed", "timed_out"} and blocked_reason is None:
            blocked_reason = f"step {args.get('_step_index')} ({args.get('_role')}) {row['status']}"
        if row["status"] in {"queued", "running"} and blocked_reason is None:
            blocked_reason = f"waiting on step {args.get('_step_index')} ({args.get('_role')})"
    profile = None
    mode = None
    goal = None
    if children:
        first_args = _parse_args(rows[0]) if rows else {}
        if indexed:
            # The indexed query already filtered: rows[0] is a child.
            profile = first_args.get("_profile")
            mode = first_args.get("_mode")
            goal = first_args.get("_goal")
        else:
            for row in rows:
                args = _parse_args(row)
                if str(args.get("_handoff_id") or "") == handoff_id:
                    profile = args.get("_profile")
                    mode = args.get("_mode")
                    goal = args.get("_goal")
                    break
    statuses = {c["status"] for c in children}
    if not children:
        overall = "not_found"
    elif statuses <= {"completed"}:
        overall = "completed"
        blocked_reason = None
    elif "failed" in statuses or "timed_out" in statuses:
        overall = "failed"
    elif "running" in statuses or "queued" in statuses:
        overall = "in_progress"
    else:
        overall = "unknown"
    return {
        "ok": True,
        "handoff_id": handoff_id,
        "profile": profile,
        "mode": mode,
        "goal": goal,
        "status": overall,
        "blocked_reason": blocked_reason,
        "tasks": children,
        "count": len(children),
        "blackboard": get_handoff_blackboard(conn, handoff_id),
    }


def recover_failed_role_tasks(conn: Any, *, role: str, device_id: str | None = None) -> dict:
    """Re-queue failed tasks that were tagged with a given role."""
    role = (role or "").strip().lower()
    rows = conn.execute(
        "SELECT id, device_id, tool, args_json, tenant FROM tasks WHERE status='failed'"
    ).fetchall()
    requeued: list[str] = []
    for row in rows:
        args = _parse_args(row)
        if str(args.get("_role") or "").lower() != role:
            continue
        if device_id and row["device_id"] != device_id:
            continue
        now = dbmod.now()
        conn.execute(
            "UPDATE tasks SET status='queued', result=NULL, updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        requeued.append(row["id"])
    if requeued:
        conn.commit()
    return {"ok": True, "role": role, "requeued": requeued, "count": len(requeued)}


def delegate_role_target(role: str) -> dict:
    """Map a role id to the seeded custom agent row id for agent_delegate."""
    aid = role_agent_id(role)
    return {
        "ok": True,
        "role": role,
        "target_agent": aid,
        "agent_name": ROLES[role]["name"],
        "allowed_tools": ROLES[role].get("allowed_tools") or [],
    }


def enqueue_agent_run_role(
    conn: Any,
    *,
    device_id: str,
    tenant: str,
    role: str,
    prompt: str,
    parent_task_id: str | None = None,
    handoff_id: str | None = None,
    goal: str | None = None,
) -> dict:
    """Queue a shared-task that AgentRunner claims to execute a role agent."""
    role = (role or "").strip().lower()
    if role not in ROLES:
        raise ValueError(f"Unknown role '{role}'")
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    return enqueue_role_task(
        conn,
        device_id=device_id,
        tenant=tenant,
        tool="agent_run_role",
        args={"role": role, "prompt": prompt, "target_agent": role_agent_id(role)},
        role=role,
        handoff_id=handoff_id or uuid.uuid4().hex[:12],
        parent_task_id=parent_task_id,
        goal=goal or prompt[:200],
        step_index=0,
        profile="delegate",
        mode="queued_delegate",
    )


def dispatch_supervisor_enqueue_handoff(args: dict) -> dict:
    """Dispatch entry for supervisor_enqueue_handoff (requires plane conn)."""
    conn = get_plane_conn()
    if conn is None:
        raise RuntimeError(
            "supervisor_enqueue_handoff requires a control-plane database connection "
            "(run via Supervisor agent / control plane, not a bare device poller)."
        )
    device_id = str(args.get("device_id") or args.get("device") or "").strip()
    if not device_id:
        raise ValueError("device_id is required")
    tenant = str(args.get("tenant") or "default")
    goal = str(args.get("goal") or "")
    profile = str(args.get("profile") or "code")
    mode = str(args.get("mode") or "gated")
    return run_supervisor_handoff_enqueue(
        conn,
        device_id=device_id,
        tenant=tenant,
        goal=goal,
        profile=profile,
        mode=mode,
    )


def dispatch_supervisor_recover(args: dict) -> dict:
    """Dispatch entry for supervisor_recover_role_tasks."""
    conn = get_plane_conn()
    if conn is None:
        raise RuntimeError(
            "supervisor_recover_role_tasks requires a control-plane database connection."
        )
    role = str(args.get("role") or "").strip()
    if not role:
        raise ValueError("role is required")
    device_id = args.get("device_id") or args.get("device")
    device_id = str(device_id).strip() if device_id else None
    return recover_failed_role_tasks(conn, role=role, device_id=device_id)


# ---------------------------------------------------------------------------
# Pillar 3 & Pillar 4: Autonomous Task Planning & Multi-Agent Blackboard
# ---------------------------------------------------------------------------

_BLACKBOARD_KEY_PREFIX = "handoff.blackboard."
_PLAN_KEY_PREFIX = "handoff.plan."


def get_handoff_blackboard(conn: Any, handoff_id: str) -> dict[str, Any]:
    """Load the shared multi-agent blackboard state for a handoff session."""
    if not handoff_id:
        return {"steps": {}, "shared": {}}
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (f"{_BLACKBOARD_KEY_PREFIX}{handoff_id}",),
        ).fetchone()
        if row and row["value"]:
            data = json.loads(str(row["value"]))
            if isinstance(data, dict):
                data.setdefault("steps", {})
                data.setdefault("shared", {})
                return data
    except Exception:
        pass
    return {"steps": {}, "shared": {}}


def update_handoff_blackboard(
    conn: Any,
    handoff_id: str,
    *,
    step_id: str,
    step_index: int,
    role: str,
    tool: str,
    status: str,
    output: Any,
    shared_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a step's result into the shared handoff blackboard so downstream roles can reference it."""
    bb = get_handoff_blackboard(conn, handoff_id)
    parsed_out = output
    if isinstance(output, str) and output.strip():
        try:
            parsed_out = json.loads(output)
        except Exception:
            parsed_out = output[:4000]
    summary = ""
    if isinstance(parsed_out, dict):
        summary = str(parsed_out.get("summary") or parsed_out.get("status") or parsed_out.get("system") or json.dumps(parsed_out)[:400])
    else:
        summary = str(parsed_out or "")[:400]
    entry = {
        "step_id": step_id,
        "step_index": step_index,
        "role": role,
        "tool": tool,
        "status": status,
        "output": parsed_out,
        "summary": summary,
        "updated_at": dbmod.now(),
    }
    bb["steps"][step_id] = entry
    bb["steps"][f"step_{step_index + 1}"] = entry
    if shared_updates and isinstance(shared_updates, dict):
        bb["shared"].update(shared_updates)
    try:
        key = f"{_BLACKBOARD_KEY_PREFIX}{handoff_id}"
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, json.dumps(bb, default=str)),
        )
    except Exception:
        pass
    return bb


def save_custom_handoff_plan(conn: Any, handoff_id: str, steps: list[dict[str, Any]]) -> None:
    """Persist a dynamically planned step DAG for a handoff session."""
    if not handoff_id:
        return
    try:
        key = f"{_PLAN_KEY_PREFIX}{handoff_id}"
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, json.dumps(steps, default=str)),
        )
    except Exception:
        pass


def load_custom_handoff_plan(conn: Any, handoff_id: str) -> list[dict[str, Any]] | None:
    """Load a persisted dynamic DAG plan for a handoff session if one exists."""
    if not handoff_id:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (f"{_PLAN_KEY_PREFIX}{handoff_id}",),
        ).fetchone()
        if row and row["value"]:
            parsed = json.loads(str(row["value"]))
            if isinstance(parsed, list) and parsed:
                return parsed
    except Exception:
        pass
    return None


def resolve_blackboard_bindings(args: dict[str, Any], blackboard: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``{{blackboard.<step_id>.output}}`` and ``{{blackboard.<step_id>.summary}}`` placeholders in step args."""
    import re

    steps_map = (blackboard or {}).get("steps") or {}
    shared_map = (blackboard or {}).get("shared") or {}
    pattern = re.compile(r"\{\{\s*blackboard\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_]+)\s*\}\}")

    def _sub_val(val: Any) -> Any:
        if isinstance(val, str):
            def _repl(m: re.Match) -> str:
                target = m.group(1)
                field = m.group(2)
                if target == "shared":
                    return str(shared_map.get(field, ""))
                step_obj = steps_map.get(target)
                if isinstance(step_obj, dict):
                    v = step_obj.get(field)
                    if isinstance(v, (dict, list)):
                        return json.dumps(v)
                    return str(v if v is not None else "")
                return m.group(0)

            return pattern.sub(_repl, val)
        if isinstance(val, dict):
            return {k: _sub_val(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_sub_val(x) for x in val]
        return val

    return _sub_val(dict(args or {}))


def plan_goal_dag(
    conn: Any,
    *,
    goal: str,
    device_id: str = "local",
    tenant: str = "default",
    profile: str | None = None,
    target_devices: list[str] | None = None,
    effective_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synthesize a capability-aware, memory-augmented multi-agent execution DAG from a natural-language goal."""
    from ..policy import normalize_tool_name
    from . import agent_memory as memorymod
    from . import device_intelligence as intelmod

    clean_goal = (goal or "").strip() or "Inspect device health and verify environment readiness"
    low_goal = clean_goal.lower()

    # 1. Auto-detect optimal profile when not explicitly forced or when 'auto' is passed
    chosen_profile = (profile or "auto").strip().lower()
    if chosen_profile in ("", "auto"):
        if any(k in low_goal for k in ("browser", "url", "http", "web", "website", "page", "dom")):
            chosen_profile = "browser"
        elif any(k in low_goal for k in ("window", "click", "desktop", "screen", "ui", "a11y", "notepad", "app")):
            chosen_profile = "desktop"
        elif any(k in low_goal for k in ("full", "end-to-end", "deploy", "release")):
            chosen_profile = "full"
        elif any(k in low_goal for k in ("code", "test", "git", "repo", "build", "lint", "refactor", "fix")):
            chosen_profile = "code"
        else:
            chosen_profile = "research"
    if chosen_profile not in HANDOFF_PROFILES:
        chosen_profile = "code"

    # 2. Gather target device intelligence & capability discovery
    dev_row = None
    if device_id and device_id != "local":
        try:
            dev_row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        except Exception:
            dev_row = None
    if dev_row is None:
        dev_row = {"id": device_id or "local", "name": device_id or "local", "os": "Windows", "status": "online", "tenant": tenant}
    eff_pol = dict(effective_policy or {})
    intel = intelmod.build_device_intelligence(conn, dev_row, effective_policy=eff_pol)

    # 3. Recall relevant device-scoped episodic memories (Pillar 7)
    memories = memorymod.recall_device_memories(conn, tenant=tenant, device_id=device_id or "local", limit=5)

    # 4. Build dynamic DAG nodes with context bindings, guards, verification, and tier classification
    base_steps = list(HANDOFF_PROFILES[chosen_profile])

    # Augment steps based on specific keywords in the goal and device capabilities
    dynamic_steps: list[dict[str, Any]] = []
    for idx, st in enumerate(base_steps):
        tool_name = normalize_tool_name(st["tool"])
        step_id = f"step_{idx + 1}"
        prev_id = f"step_{idx}" if idx > 0 else None
        step_args = dict(st.get("args") or {})
        step_args["goal"] = clean_goal
        if prev_id:
            step_args["_upstream_context"] = f"{{{{blackboard.{prev_id}.summary}}}}"

        # Assign target device (supports multi-device orchestration if target_devices provided)
        assigned_device = device_id or "local"
        if target_devices and len(target_devices) > 0:
            assigned_device = target_devices[idx % len(target_devices)]

        tier_enum = TOOL_TIERS.get(tool_name)
        tier_val = tier_enum.value if tier_enum is not None else 1
        max_unattended = int(eff_pol.get("max_tier_unattended", 2) or 2)
        unrestricted = bool(eff_pol.get("unrestricted", False))
        requires_approval = (not unrestricted) and (tier_val > max_unattended or tier_val >= 3)

        guard: dict[str, Any] = {"require_online": assigned_device != "local"}
        verify: dict[str, Any] | None = None
        if st["role"] == "computer_operator":
            verify = {"mode": "verify", "capture_after": True, "timeout_seconds": 5}
        elif st["role"] == "browser_operator":
            verify = {"mode": "wait", "text": "", "timeout_seconds": 8}

        dynamic_steps.append({
            "step_id": step_id,
            "index": idx + 1,
            "name": st["name"],
            "role": st["role"],
            "role_name": ROLES.get(st["role"], {}).get("name", st["role"]),
            "device_id": assigned_device,
            "tool": tool_name,
            "action": tool_name,
            "args": step_args,
            "depends_on": [prev_id] if prev_id else [],
            "tier": tier_val,
            "requires_approval": requires_approval,
            "guard": guard,
            "verify": verify,
            "retries": 1,
            "fallback_role": "recovery",
        })

    # If goal mentions accessibility / UI inspection and desktop_a11y_snapshot isn't already in the DAG, inject it
    if any(k in low_goal for k in ("a11y", "accessibility", "ui tree", "element", "@e")) and not any(
        s["tool"] == "desktop_a11y_snapshot" for s in dynamic_steps
    ):
        next_idx = len(dynamic_steps) + 1
        prev_id = f"step_{next_idx - 1}" if dynamic_steps else None
        dynamic_steps.append({
            "step_id": f"step_{next_idx}",
            "index": next_idx,
            "name": "computer_operator:desktop_a11y_snapshot",
            "role": "computer_operator",
            "role_name": ROLES["computer_operator"]["name"],
            "device_id": device_id or "local",
            "tool": "desktop_a11y_snapshot",
            "action": "desktop_a11y_snapshot",
            "args": {"interactive_only": True, "max_elements": 80, "goal": clean_goal},
            "depends_on": [prev_id] if prev_id else [],
            "tier": TOOL_TIERS["desktop_a11y_snapshot"].value if "desktop_a11y_snapshot" in TOOL_TIERS else 1,
            "requires_approval": False,
            "guard": {"require_online": (device_id or "local") != "local"},
            "verify": None,
            "retries": 1,
            "fallback_role": "recovery",
        })

    max_tier = max((s["tier"] for s in dynamic_steps), default=1)
    approval_steps = sum(1 for s in dynamic_steps if s["requires_approval"])
    roles_involved = list(dict.fromkeys(s["role"] for s in dynamic_steps))

    return {
        "schema": "ai-bridge-autonomous-plan/v1",
        "ok": True,
        "goal": clean_goal,
        "profile": chosen_profile,
        "device_id": device_id or "local",
        "tenant": tenant,
        "total_steps": len(dynamic_steps),
        "max_tier": max_tier,
        "approval_required_steps": approval_steps,
        "roles_involved": roles_involved,
        "device_intelligence": {
            "health_score": intel["health_score"],
            "readiness_state": intel["readiness_state"],
            "platform_family": intel["platform_family"],
            "recommended_transport": intel["recommended_transport"],
            "recommended_a11y_backend": intel["recommended_a11y_backend"],
        },
        "recalled_memories": [
            {
                "id": m["id"],
                "episode_type": m.get("episode_type"),
                "tool": m.get("tool"),
                "content": m.get("content"),
            }
            for m in memories
        ],
        "steps": dynamic_steps,
    }


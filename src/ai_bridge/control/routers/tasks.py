"""tasks routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...policy import TOOL_TIERS
from .. import db as dbmod
from .. import plane_state
from .. import task_service as task_service_mod
from ..app import ApprovalDecision, ApprovalUpdateIn, TaskIn, TaskUpdateIn, _rate_limit, _render_quick_decision_html, make_quick_sig, send_webhook_notification
from ._context import ControlContext


def create_tasks_router(ctx: ControlContext) -> APIRouter:
    """Build the tasks APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    _auth_context_full = ctx._auth_context_full
    _require_row_tenant = ctx._require_row_tenant
    app = ctx.app
    approval_detail = ctx.approval_detail
    audit = ctx.audit
    conn = ctx.conn
    expire_pending_approvals = ctx.expire_pending_approvals
    get_current_policy = ctx.get_current_policy
    get_effective_device_policy = ctx.get_effective_device_policy
    get_public_base_url = ctx.get_public_base_url
    get_webhook_url = ctx.get_webhook_url
    notify_tasks = ctx.notify_tasks
    require_admin = ctx.require_admin
    require_role = ctx.require_role
    resolve_request_tenant = ctx.resolve_request_tenant
    session_tenant_for_request = ctx.session_tenant_for_request
    token = ctx.token

    @router.post("/api/tasks")
    def submit_task(body: TaskIn, request: Request, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("operator", x_admin_token, x_session_token)
        request_key = request.headers.get("Idempotency-Key")
        if request_key is not None and (not request_key or len(request_key) > 128 or not request_key.isascii()):
            raise HTTPException(422, "Idempotency-Key must be 1–128 ASCII characters.")
        _actor, role, _tenant, _is_legacy = _auth_context_full(x_admin_token, x_session_token)
        tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
        # R-P0-10: the ONE task-creation service — policy check, approval
        # gate, idempotency, tenant scoping, takeover + durable e-stop.
        created = task_service_mod.create_task(
            conn, actor=actor, role=role, tenant=tenant,
            device_id=body.device_id, tool=body.tool, args=body.args,
            request_key=request_key, human_initiated=False,
            max_attempts=getattr(body, "max_attempts", 1),
            get_effective_device_policy=get_effective_device_policy,
            get_current_policy=get_current_policy, notify=notify_tasks, audit_fn=audit,
        )
        if created["status"] == "needs_approval" and get_webhook_url():
            wh_url = get_webhook_url()
            row = conn.execute("SELECT name FROM devices WHERE id=?", (body.device_id,)).fetchone()
            policy_row = conn.execute("SELECT MAX(version) AS version FROM policies").fetchone()
            evt = {
                "event": "approval_requested",
                "approval_id": created["approval_id"],
                "task_id": created["task_id"],
                "device_id": body.device_id,
                "device_name": row["name"] if row else body.device_id,
                "tool": body.tool,
                "tier": created["tier"],
                "args": body.args,
                "policy_version": (policy_row["version"] or 0) if policy_row else 0,
            }
            pub_url = get_public_base_url(request)
            threading.Thread(
                target=send_webhook_notification,
                args=(wh_url, evt, token, pub_url),
                daemon=True,
            ).start()
        return created

    @router.get("/api/tasks")
    def list_tasks(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None), tenant: str | None = None, device_id: str | None = None):
        require_role("viewer", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        # PERF-03: projected columns only — the returned dicts have no
        # "result" key, so a 200-row list can no longer return multi-MB
        # screenshot payloads. Same tenant/device filtering as before.
        return dbmod.list_tasks_projected(conn, tenant=tenant, device_id=device_id, limit=200)

    @router.get("/api/tasks/dead-letter")
    def list_dead_letter_tasks(
        include_replayed: bool = False,
        limit: int = 100,
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        from .. import orchestration as orchmod

        items = orchmod.list_dead_letters(conn, tenant=eff_tenant, limit=limit, include_replayed=include_replayed)
        return {"items": items, "count": len(items)}

    @router.post("/api/tasks/dead-letter/{dlq_id}/replay")
    def replay_dead_letter_task(
        dlq_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        _actor, role, _tenant, _is_legacy = _auth_context_full(x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM task_dead_letters WHERE id=?", (dlq_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Dead-letter entry not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        if row["replayed_task_id"]:
            raise HTTPException(status_code=409, detail=f"Dead-letter entry already replayed as task {row['replayed_task_id']}.")
        try:
            args = json.loads(row["args_json"] or "{}")
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}
        created = task_service_mod.create_task(
            conn,
            actor=actor,
            role=role,
            tenant=row["tenant"],
            device_id=row["device_id"],
            tool=row["tool"],
            args=args,
            human_initiated=True,
            get_effective_device_policy=get_effective_device_policy,
            get_current_policy=get_current_policy,
            notify=notify_tasks,
            audit_fn=audit,
        )
        now_ts = dbmod.now()
        conn.execute(
            "UPDATE task_dead_letters SET replayed_task_id=?, replayed_at=? WHERE id=?",
            (created["task_id"], now_ts, dlq_id),
        )
        conn.commit()
        audit(actor, row["device_id"], "replay_dead_letter", {"dlq_id": dlq_id, "new_task_id": created["task_id"], "original_task_id": row["task_id"]}, "allowed", tenant=row["tenant"])
        return {"dlq_id": dlq_id, "replayed": True, **created}

    @router.delete("/api/tasks/dead-letter/{dlq_id}")
    def purge_dead_letter_task(
        dlq_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM task_dead_letters WHERE id=?", (dlq_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Dead-letter entry not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        conn.execute("DELETE FROM task_dead_letters WHERE id=?", (dlq_id,))
        conn.commit()
        audit(actor, row["device_id"], "purge_dead_letter", {"dlq_id": dlq_id, "task_id": row["task_id"]}, "allowed", tenant=row["tenant"])
        return {"dlq_id": dlq_id, "deleted": True}

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, wait: float = 0.0, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        wait_sec = max(0.0, min(float(wait or 0.0), 25.0))
        deadline = time.time() + wait_sec
        while True:
            # PERF-03 (Wave 4 correction): the LIST endpoint is projected,
            # but this single-task GET keeps "result". In-repo consumers
            # depend on it: device_routing.execute_on_device() polls this
            # endpoint and returns cur["result"] to the MCP caller, the
            # dashboard task-detail view reads res.result, and ai_api
            # tells clients to retrieve the final result here. Projecting
            # it away silently returned empty results for every remote
            # execution (caught by test_enroll_task_agent_result_flow and
            # test_512kb_result_and_long_poll_wakeup). The multi-MB hazard
            # is the list (200 rows x result); one task's own result is
            # the documented contract. GET .../result remains as the
            # lightweight result-only fetch.
            row = conn.execute(f"SELECT {dbmod.TASK_LIST_COLUMNS}, result FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Task not found.")
            _require_row_tenant(row, x_admin_token, x_session_token)
            if wait_sec <= 0 or row["status"] in {"completed", "failed", "cancelled", "timed_out", "needs_approval"}:
                return dict(row)
            rem = deadline - time.time()
            if rem <= 0:
                return dict(row)
            await asyncio.sleep(min(0.15, rem))

    @router.get("/api/tasks/{task_id}/result")
    def get_task_result(task_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        """PERF-03 companion: the full ``result`` TEXT for one task.

        List/get deliberately omit "result"; callers that need the
        payload (often multi-MB screenshots) fetch it here, once, for
        one task. Same viewer auth + tenant check as get_task.
        """
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT id, tenant FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        result = dbmod.get_task_result(conn, task_id)
        return {"task_id": task_id, "result": result if result is not None else ""}

    @router.get("/api/tasks/{task_id}/events")
    def get_task_events(
        task_id: str,
        limit: int = 50,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT id, tenant FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        from .. import task_events as task_eventsmod

        return {"ok": True, "task_id": task_id, "events": task_eventsmod.list_task_events(conn, task_id, limit=limit)}

    @router.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("operator", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        if row["status"] in {"completed", "failed", "cancelled"}:
            return {"task_id": task_id, "status": row["status"]}
        now_ts = dbmod.now()
        try:
            conn.execute(
                "UPDATE tasks SET status='cancelled', failure_class='cancelled', failure_reason='operator_cancelled', lease_expires_at=NULL, updated_at=? WHERE id=?",
                (now_ts, task_id),
            )
        except Exception:
            conn.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?", (now_ts, task_id))
        try:
            from .. import task_events as task_eventsmod

            trace_id = ""
            try:
                trace_id = str(row["trace_id"] or "")
            except Exception:
                pass
            task_eventsmod.append_task_event(
                conn,
                task_id,
                "cancelled",
                {"failure_class": "cancelled", "failure_reason": "operator_cancelled"},
                trace_id=trace_id,
                commit=False,
            )
        except Exception:
            pass
        conn.commit()
        notify_tasks(row["device_id"], task_id, cancel=True)
        audit("admin", row["device_id"], "cancel_task", {"task_id": task_id}, "allowed")
        return {"task_id": task_id, "status": "cancelled"}

    @router.put("/api/tasks/{task_id}")
    def update_task(task_id: str, body: TaskUpdateIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        tool = body.tool.strip() if body.tool is not None and body.tool.strip() else row["tool"]
        tier_enum = TOOL_TIERS.get(tool)
        if tier_enum is None:
            raise HTTPException(status_code=400, detail=f"Unknown tool: {tool}")
        args_json = json.dumps(body.args) if body.args is not None else row["args_json"]
        status = body.status.strip() if body.status is not None and body.status.strip() else row["status"]
        valid_statuses = {"queued", "running", "needs_approval", "completed", "failed", "cancelled", "timed_out"}
        if status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        if status == "queued" and plane_state.is_emergency_stop(conn, app.state):
            raise HTTPException(status_code=403, detail="Emergency stop is active; cannot queue tasks.")

        dev_row = conn.execute("SELECT * FROM devices WHERE id=?", (row["device_id"],)).fetchone()
        eff_policy = get_effective_device_policy(dev_row) if dev_row else get_current_policy()
        if tool in eff_policy.get("denied_tools", []):
            raise HTTPException(status_code=403, detail=f"Tool '{tool}' is disabled by device policy.")
        if eff_policy.get("allowed_tools") is not None and tool not in eff_policy["allowed_tools"]:
            raise HTTPException(status_code=403, detail=f"Tool '{tool}' is not in the allowed tools list for this device.")

        needs_approval = tier_enum.value >= 4 or (tier_enum.value == 3 and not eff_policy.get("allow_tier3", False))
        payload_changed = (tool != row["tool"]) or (args_json != row["args_json"])
        already_ran = row["status"] in {"running", "completed", "failed", "timed_out"}
        approval_id = row["approval_id"]

        if payload_changed or (status == "queued" and already_ran):
            if approval_id:
                approval_id = None

        if status == "queued" and needs_approval:
            if not approval_id:
                raise HTTPException(status_code=409, detail="Task requires approval before queuing.")
            ap = conn.execute("SELECT status, expires_at, tool FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if ap is None or ap["status"] != "approved":
                raise HTTPException(status_code=409, detail="Task approval is not approved; create a new task to request approval again.")
            if ap["expires_at"] is not None and ap["expires_at"] <= dbmod.now():
                raise HTTPException(status_code=409, detail="Task approval has expired.")
            if ap["tool"] and ap["tool"] != tool:
                raise HTTPException(status_code=409, detail="Task approval tool mismatch.")

        conn.execute(
            "UPDATE tasks SET tool=?, args_json=?, tier=?, status=?, approval_id=?, updated_at=? WHERE id=?",
            (tool, args_json, tier_enum.value, status, approval_id, dbmod.now(), task_id),
        )
        conn.commit()
        if status == "queued":
            notify_tasks()
        audit(actor, row["device_id"], "update_task", {"task_id": task_id, "tool": tool, "status": status}, "allowed")
        updated = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(updated)

    @router.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        conn.execute("DELETE FROM approvals WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
        audit(actor, row["device_id"], "delete_task", {"task_id": task_id}, "allowed")
        return {"task_id": task_id, "deleted": True}

    @router.delete("/api/tasks")
    def clear_tasks(status: str | None = None, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        tenant, _is_legacy = session_tenant_for_request(x_admin_token, x_session_token)
        if status and status != "all":
            cur = conn.execute("DELETE FROM tasks WHERE status=? AND tenant=?", (status, tenant))
        else:
            cur = conn.execute("DELETE FROM tasks WHERE status IN ('completed', 'failed', 'cancelled', 'timed_out') AND tenant=?", (tenant,))
        conn.commit()
        audit(actor, "", "clear_tasks", {"status": status or "finished", "deleted_count": cur.rowcount}, "allowed")
        return {"deleted_count": cur.rowcount}

    @router.get("/api/approvals")
    def list_approvals(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        expire_pending_approvals()
        tenant, _is_legacy = session_tenant_for_request(x_admin_token, x_session_token)
        rows = conn.execute(
            "SELECT a.* FROM approvals a LEFT JOIN tasks t ON t.id=a.task_id "
            "WHERE (a.task_id IS NULL AND a.tenant=?) OR (t.tenant=?) "
            "ORDER BY a.created_at DESC LIMIT 200", (tenant, tenant)).fetchall()
        return [approval_detail(r) for r in rows]

    @router.post("/api/approvals/{approval_id}/decide")
    def decide_approval(approval_id: str, body: ApprovalDecision, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        expire_pending_approvals()
        if body.decision not in {"approved", "denied"}:
            raise HTTPException(status_code=400, detail="decision must be approved or denied.")
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found.")
        if row["task_id"]:
            _t = conn.execute("SELECT tenant FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if _t is not None:
                _require_row_tenant(_t, x_admin_token, x_session_token)
        else:
            _require_row_tenant(row, x_admin_token, x_session_token)
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Approval is already {row['status']}.")
        if row["expires_at"] is not None and row["expires_at"] <= dbmod.now():
            expire_pending_approvals()
            raise HTTPException(status_code=409, detail="Approval has expired.")
        now = dbmod.now()
        cur = conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'", (body.decision, actor, now, approval_id))
        if cur.rowcount != 1:
            conn.commit()
            raise HTTPException(status_code=409, detail="Approval was already decided or expired.")
        device_id = None
        if row["task_id"]:
            new_status = "queued" if body.decision == "approved" else "cancelled"
            conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status='needs_approval'", (new_status, now, row["task_id"]))
            trow = conn.execute("SELECT device_id FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            device_id = trow["device_id"] if trow else None
        conn.commit()
        if device_id and row["task_id"]:
            notify_tasks(device_id, row["task_id"], cancel=(body.decision != "approved"))
        else:
            notify_tasks()
        audit(actor, "", "decide_approval", {"approval_id": approval_id, "decision": body.decision, "reason": body.reason}, "allowed")
        return {"approval_id": approval_id, "status": body.decision, "decided_by": actor, "decided_at": now}

    @router.post("/api/approvals/{approval_id}/replan")
    def replan_approval(approval_id: str, body: ApprovalUpdateIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        expire_pending_approvals()
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found.")
        if row["task_id"]:
            _t = conn.execute("SELECT tenant FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if _t is not None:
                _require_row_tenant(_t, x_admin_token, x_session_token)
        else:
            _require_row_tenant(row, x_admin_token, x_session_token)
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Approval is already {row['status']}.")
        feedback = (body.reason or "").strip() or "Operator requested re-plan with non-destructive parameters."
        now = dbmod.now()
        cur = conn.execute("UPDATE approvals SET status='denied', decided_by=?, decided_at=?, reason=? WHERE id=? AND status='pending'", (actor, now, feedback, approval_id))
        if cur.rowcount != 1:
            conn.commit()
            raise HTTPException(status_code=409, detail="Approval was already decided or expired.")
        if row["task_id"]:
            conn.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status='needs_approval'", (now, row["task_id"]))
        conn.commit()
        notify_tasks()
        audit(actor, "", "replan_approval", {"approval_id": approval_id, "feedback": feedback}, "denied")
        return {"approval_id": approval_id, "status": "denied", "feedback": feedback, "decided_by": actor, "decided_at": now}

    @router.get("/api/approvals/quick/{approval_id}", response_class=HTMLResponse)
    def quick_approval(approval_id: str, decision: str = "approved", sig: str = "", request: Request = None):
        _rate_limit(f"quick_approval:{approval_id}", limit=10, window=60, conn=conn)
        if request and request.client:
            _rate_limit(f"quick_approval_ip:{request.client.host}", limit=30, window=60, conn=conn)
        expire_pending_approvals()
        decision = (decision or "").strip().lower()
        if decision not in {"approved", "denied"}:
            return HTMLResponse(
                content=_render_quick_decision_html(
                    title="Invalid Request",
                    badge_class="denied",
                    badge_icon="❌",
                    badge_text="INVALID DECISION",
                    heading="Invalid Action",
                    message="Decision parameter must be either 'approved' or 'denied'.",
                ),
                status_code=400,
            )

        expected_sig = make_quick_sig(token, approval_id, decision)
        if not sig or not hmac.compare_digest(sig.strip(), expected_sig):
            return HTMLResponse(
                content=_render_quick_decision_html(
                    title="Verification Failed",
                    badge_class="denied",
                    badge_icon="🛡️",
                    badge_text="ACCESS DENIED",
                    heading="Signature Verification Failed",
                    message="The HMAC signature for this quick approval action is invalid or has expired. Please sign into the dashboard to review and authorize.",
                ),
                status_code=403,
            )

        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            return HTMLResponse(
                content=_render_quick_decision_html(
                    title="Not Found",
                    badge_class="info",
                    badge_icon="❓",
                    badge_text="NOT FOUND",
                    heading="Approval Request Not Found",
                    message=f"Approval '{approval_id}' was not found or may have already been cleared.",
                ),
                status_code=404,
            )

        task = conn.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone() if row["task_id"] else None
        dev_name = "Workstation"
        tool_name = row["tool"] or "Operation"
        tier_num = "3"
        if task:
            dev_row = conn.execute("SELECT name FROM devices WHERE id=?", (task["device_id"],)).fetchone()
            if dev_row:
                dev_name = dev_row["name"]
            tool_name = task["tool"]
            tier_num = str(task["tier"])

        now = dbmod.now()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now))

        if row["status"] != "pending":
            status_text = row["status"].upper()
            badge_cls = "approved" if row["status"] == "approved" else "denied"
            icon = "✅" if row["status"] == "approved" else "❌"
            return HTMLResponse(
                content=_render_quick_decision_html(
                    title=f"Already {status_text}",
                    badge_class=badge_cls,
                    badge_icon=icon,
                    badge_text=f"ALREADY {status_text}",
                    heading="Request Already Decided",
                    message=f"This approval request was previously decided as <b>{row['status']}</b> by <code>{row['decided_by']}</code>.",
                    details={
                        "Tool": f"{tool_name} (Tier {tier_num})",
                        "Device": dev_name,
                        "Task ID": row["task_id"] or "N/A",
                        "Approval ID": approval_id,
                        "Decided At": now_str,
                    },
                ),
                status_code=200,
            )

        if row["expires_at"] is not None and row["expires_at"] <= now:
            return HTMLResponse(
                content=_render_quick_decision_html(
                    title="Expired Request",
                    badge_class="denied",
                    badge_icon="⏰",
                    badge_text="EXPIRED",
                    heading="Approval Expired",
                    message="This approval request has exceeded its time-to-live deadline and was automatically cancelled for safety.",
                    details={
                        "Tool": f"{tool_name} (Tier {tier_num})",
                        "Device": dev_name,
                        "Task ID": row["task_id"] or "N/A",
                        "Approval ID": approval_id,
                    },
                ),
                status_code=410,
            )

        cur = conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'", (decision, "mobile_quick_approval", now, approval_id))
        if cur.rowcount != 1:
            conn.commit()
            return HTMLResponse(
                content=_render_quick_decision_html(
                    title="Already Decided",
                    badge_class="info",
                    badge_icon="ℹ️",
                    badge_text="ALREADY DECIDED",
                    heading="Request Already Decided",
                    message="This approval request was already decided concurrently.",
                    details={"Approval ID": approval_id, "Task ID": row["task_id"] or "N/A"},
                ),
                status_code=200,
            )
        if row["task_id"]:
            new_status = "queued" if decision == "approved" else "cancelled"
            conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status='needs_approval'", (new_status, now, row["task_id"]))
        conn.commit()
        notify_tasks()
        audit("mobile_quick_approval", "", "decide_approval", {"approval_id": approval_id, "decision": decision, "channel": "mobile_hmac"}, "allowed")

        if decision == "approved":
            return HTMLResponse(
                content=_render_quick_decision_html(
                    title="Action Approved",
                    badge_class="approved",
                    badge_icon="✅",
                    badge_text="OPERATION APPROVED",
                    heading="Execution Authorized",
                    message=f"Task <code>{row['task_id']}</code> has been authorized and queued for execution on <b>{dev_name}</b>.",
                    details={
                        "Tool": f"{tool_name} (Tier {tier_num})",
                        "Device": dev_name,
                        "Task ID": row["task_id"] or "N/A",
                        "Approval ID": approval_id,
                        "Time": now_str,
                    },
                ),
                status_code=200,
            )
        else:
            return HTMLResponse(
                content=_render_quick_decision_html(
                    title="Action Denied",
                    badge_class="denied",
                    badge_icon="❌",
                    badge_text="OPERATION DENIED",
                    heading="Execution Blocked",
                    message=f"Task <code>{row['task_id']}</code> has been rejected and cancelled. The target device will not execute this action.",
                    details={
                        "Tool": f"{tool_name} (Tier {tier_num})",
                        "Device": dev_name,
                        "Task ID": row["task_id"] or "N/A",
                        "Approval ID": approval_id,
                        "Time": now_str,
                    },
                ),
                status_code=200,
            )

    @router.put("/api/approvals/{approval_id}")
    def update_approval(approval_id: str, body: ApprovalUpdateIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found.")
        if row["task_id"]:
            _t = conn.execute("SELECT tenant FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if _t is not None:
                _require_row_tenant(_t, x_admin_token, x_session_token)
        else:
            _require_row_tenant(row, x_admin_token, x_session_token)
        reason = body.reason if body.reason is not None else row["reason"]
        if body.status is not None:
            raise HTTPException(status_code=400, detail="Use POST /api/approvals/{id}/decide to decide an approval.")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Approval is already {row['status']}.")
        conn.execute("UPDATE approvals SET reason=? WHERE id=? AND status='pending'", (reason, approval_id))
        conn.commit()
        audit(actor, "", "update_approval", {"approval_id": approval_id, "reason_updated": body.reason is not None}, "allowed")
        return approval_detail(conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())

    @router.delete("/api/approvals/{approval_id}")
    def delete_approval(approval_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found.")
        if row["task_id"]:
            _t = conn.execute("SELECT tenant FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if _t is not None:
                _require_row_tenant(_t, x_admin_token, x_session_token)
        else:
            _require_row_tenant(row, x_admin_token, x_session_token)
        if row["task_id"] and row["status"] == "pending":
            conn.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status='needs_approval'", (dbmod.now(), row["task_id"]))
        cur = conn.execute("DELETE FROM approvals WHERE id=?", (approval_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Approval not found.")
        audit(actor, "", "delete_approval", {"approval_id": approval_id}, "allowed")
        return {"approval_id": approval_id, "deleted": True}

    return router

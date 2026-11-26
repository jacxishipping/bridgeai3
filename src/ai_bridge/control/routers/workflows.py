"""workflows routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid

from fastapi import APIRouter, Body, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from ...policy import TOOL_TIERS, normalize_tool_name
from .. import db as dbmod
from .. import live_activity as live_activitymod
from .. import plane_state, role_catalog
from .. import supervisor as supervisormod
from .. import task_service as task_service_mod
from ..app import (
    WORKFLOW_TEMPLATES,
    FleetBroadcastIn,
    ScheduleIn,
    SupervisorHandoffIn,
    SupervisorRecoverIn,
    TriggerIn,
    WorkflowIn,
    WorkflowRetryIn,
    WorkflowScheduleIn,
    WorkflowTemplateIn,
    send_webhook_notification,
)
from ._context import ControlContext


def create_workflows_router(ctx: ControlContext) -> APIRouter:
    """Build the workflows APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    _auth_context_full = ctx._auth_context_full
    _execute_workflow_step_sync = ctx._execute_workflow_step_sync
    _require_row_tenant = ctx._require_row_tenant
    _run_workflow_worker = ctx._run_workflow_worker
    admin_token = ctx.admin_token
    app = ctx.app
    audit = ctx.audit
    conn = ctx.conn
    get_current_policy = ctx.get_current_policy
    get_effective_device_policy = ctx.get_effective_device_policy
    get_public_base_url = ctx.get_public_base_url
    get_webhook_url = ctx.get_webhook_url
    notify_tasks = ctx.notify_tasks
    require_role = ctx.require_role
    resolve_request_tenant = ctx.resolve_request_tenant

    @router.get("/api/workflows/templates")
    def get_workflow_templates(tenant: str | None = None, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        custom_rows = conn.execute(
            "SELECT * FROM workflow_templates WHERE tenant=? ORDER BY created_at DESC",
            (tenant,),
        ).fetchall()
        custom = [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"] or "",
                "steps": json.loads(r["steps_json"] or "[]"),
                "custom": True,
                "created_by": r["created_by"],
            }
            for r in custom_rows
        ]
        return {"templates": [*custom, *WORKFLOW_TEMPLATES]}

    @router.post("/api/workflows/templates")
    def create_workflow_template(body: WorkflowTemplateIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("operator", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
        if not body.name.strip() or not body.steps:
            raise HTTPException(status_code=400, detail="Template name and at least one step are required.")
        tpl_id = "tpl-" + uuid.uuid4().hex[:10]
        now_ts = dbmod.now()
        conn.execute(
            "INSERT INTO workflow_templates (id, name, description, steps_json, tenant, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (tpl_id, body.name.strip(), body.description.strip(), json.dumps(body.steps), tenant, actor, now_ts),
        )
        conn.commit()
        audit(actor, "", "create_workflow_template", {"template_id": tpl_id, "name": body.name.strip()}, "allowed", tenant=tenant)
        return {"id": tpl_id, "name": body.name.strip(), "description": body.description.strip(), "steps": body.steps, "custom": True}

    @router.delete("/api/workflows/templates/{template_id}")
    def delete_workflow_template(template_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("operator", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM workflow_templates WHERE id=?", (template_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Custom template not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        cur = conn.execute("DELETE FROM workflow_templates WHERE id=?", (template_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Custom template not found.")
        audit(actor, "", "delete_workflow_template", {"template_id": template_id}, "allowed")
        return {"template_id": template_id, "deleted": True}

    @router.post("/api/workflows/{workflow_id}/schedule")
    def schedule_workflow(workflow_id: str, body: WorkflowScheduleIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("operator", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        steps = json.loads(row["steps_json"] or "[]")
        if not steps:
            raise HTTPException(status_code=400, detail="Workflow has no steps to schedule.")
        interval = max(60, int(body.interval_seconds or 3600))
        sched_id = uuid.uuid4().hex[:12]
        now_ts = dbmod.now()
        sched_name = (body.name or f"Workflow: {row['name']}").strip()
        dev_id = body.device_id if body.device_id is not None else (row["device_id"] or "")
        wf_payload = {
            "source_workflow_id": workflow_id,
            "name": row["name"],
            "steps": steps,
            "stop_on_failure": True,
            "max_retries": 1,
        }
        conn.execute(
            "INSERT INTO schedules (id, name, tenant, device_id, interval_seconds, tool, args_json, next_run_at, enabled, created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (sched_id, sched_name, row["tenant"], dev_id, interval, "__workflow__", json.dumps(wf_payload), now_ts + interval, now_ts),
        )
        conn.commit()
        audit(actor, dev_id, "schedule_workflow", {"workflow_id": workflow_id, "schedule_id": sched_id, "interval_seconds": interval, "steps": len(steps)}, "allowed", tenant=row["tenant"])
        return {"schedule_id": sched_id, "name": sched_name, "interval_seconds": interval, "total_steps": len(steps)}

    @router.get("/api/workflows")
    def list_workflows(
        tenant: str | None = None,
        status: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        if status:
            rows = conn.execute("SELECT * FROM workflows WHERE tenant=? AND status=? ORDER BY created_at DESC LIMIT 100", (tenant, status)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM workflows WHERE tenant=? ORDER BY created_at DESC LIMIT 100", (tenant,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["steps"] = json.loads(d.get("steps_json") or "[]")
            d["results"] = json.loads(d.get("results_json") or "[]")
            out.append(d)
        return out

    @router.get("/api/workflows/{workflow_id}")
    def get_workflow(
        workflow_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        d = dict(row)
        d["steps"] = json.loads(d.get("steps_json") or "[]")
        d["results"] = json.loads(d.get("results_json") or "[]")
        return d

    @router.post("/api/workflows/{workflow_id}/cancel")
    def cancel_workflow(
        workflow_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        now_ts = dbmod.now()
        conn.execute("UPDATE workflows SET status='cancelled', updated_at=?, completed_at=? WHERE id=? AND status IN ('pending', 'running', 'waiting_approval')", (now_ts, now_ts, workflow_id))
        conn.commit()
        audit(actor, row["device_id"] or "", "cancel_workflow", {"workflow_id": workflow_id}, "allowed", tenant=row["tenant"])
        return {"workflow_id": workflow_id, "status": "cancelled"}

    @router.delete("/api/workflows/{workflow_id}")
    def delete_workflow(
        workflow_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        if row["status"] in ("pending", "running", "waiting_approval"):
            raise HTTPException(status_code=409, detail="Cancel the active workflow before deleting it.")
        conn.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
        conn.commit()
        audit(actor, row["device_id"] or "", "delete_workflow", {"workflow_id": workflow_id}, "allowed", tenant=row["tenant"])
        return {"workflow_id": workflow_id, "deleted": True}

    @router.post("/api/workflows/{workflow_id}/retry")
    def retry_workflow(
        workflow_id: str,
        body: WorkflowRetryIn | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        if plane_state.is_emergency_stop(conn, app.state):
            raise HTTPException(status_code=403, detail="Emergency stop is active; workflows rejected.")
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        if row["status"] in ("pending", "running", "waiting_approval"):
            raise HTTPException(status_code=409, detail="Workflow is already active.")
        steps = json.loads(row["steps_json"] or "[]")
        prev_results = json.loads(row["results_json"] or "[]")
        from_failed = body.from_failed_step if body is not None else True
        start_index = 0
        initial_results: list[dict] = []
        if from_failed and prev_results:
            failed_idx = next((i for i, r in enumerate(prev_results) if r.get("status") == "failed"), None)
            if failed_idx is not None:
                start_index = failed_idx
                initial_results = prev_results[:failed_idx]
        now_ts = dbmod.now()
        conn.execute(
            "UPDATE workflows SET status='running', error='', current_step=?, results_json=?, completed_at=NULL, updated_at=? WHERE id=?",
            (start_index, json.dumps(initial_results), now_ts, workflow_id),
        )
        conn.commit()
        audit(actor, row["device_id"] or "local", "retry_workflow", {"workflow_id": workflow_id, "start_index": start_index}, "allowed", tenant=row["tenant"])
        threading.Thread(
            target=_run_workflow_worker,
            args=(workflow_id, row["device_id"], row["tenant"], steps, True, 1, actor, False, initial_results, start_index, False, None),
            daemon=True,
        ).start()
        return {"workflow_id": workflow_id, "status": "running", "start_step": start_index + 1, "total_steps": len(steps)}

    @router.post("/api/workflows")
    def submit_workflow(
        body: WorkflowIn,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        if plane_state.is_emergency_stop(conn, app.state):
            raise HTTPException(status_code=403, detail="Emergency stop is active; workflows rejected.")
        tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
        workflow_approved = False
        if body.workflow_approval_id:
            workflow_approved = task_service_mod.verify_workflow_approval(conn, body.workflow_approval_id)
            if not workflow_approved:
                raise HTTPException(status_code=400, detail="workflow_approval_id is not a valid approved admin workflow approval.")
        if not body.steps:
            raise HTTPException(status_code=400, detail="Workflow must contain at least one step.")

        wf_id = uuid.uuid4().hex[:12]
        now_ts = dbmod.now()
        conn.execute(
            "INSERT INTO workflows (id, name, tenant, device_id, steps_json, status, current_step, total_steps, results_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', 0, ?, '[]', ?, ?)",
            (wf_id, body.name, tenant, body.device_id, json.dumps(body.steps), len(body.steps), now_ts, now_ts),
        )
        conn.commit()
        audit(
            actor, body.device_id or "local", "create_workflow", {"workflow_id": wf_id, "name": body.name, "steps": len(body.steps), "workflow_approved": workflow_approved}, "allowed", tenant=tenant
        )

        threading.Thread(
            target=_run_workflow_worker,
            args=(wf_id, body.device_id, tenant, body.steps, body.stop_on_failure, body.max_retries, actor, body.require_step_approval, None, 0, workflow_approved, body.workflow_approval_id),
            daemon=True,
        ).start()

        return {
            "workflow_id": wf_id,
            "name": body.name,
            "status": "running",
            "total_steps": len(body.steps),
            "device_id": body.device_id,
        }

    @router.get("/api/workflows/{workflow_id}/stream")
    async def stream_workflow_progress(
        workflow_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)

        async def event_generator():
            last_step = -1
            last_status = ""
            while True:
                curr = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
                if curr is None:
                    break
                payload = {
                    "workflow_id": workflow_id,
                    "status": curr["status"],
                    "current_step": curr["current_step"],
                    "total_steps": curr["total_steps"],
                    "results": json.loads(curr["results_json"] or "[]"),
                    "error": curr["error"],
                }
                if curr["current_step"] != last_step or curr["status"] != last_status:
                    last_step = curr["current_step"]
                    last_status = curr["status"]
                    yield f"data: {json.dumps(payload)}\n\n"

                if curr["status"] in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.post("/api/supervisor/plan", summary="Synthesize a capability-aware multi-agent execution DAG from a goal")
    def supervisor_plan_api(
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object.")
        tenant = resolve_request_tenant(body.get("tenant"), x_admin_token, x_session_token)
        device_id = str(body.get("device_id") or "local").strip() or "local"
        eff_policy = {}
        if device_id != "local":
            dev = conn.execute("SELECT * FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
            if dev is None:
                raise HTTPException(status_code=404, detail="Device not found.")
            _require_row_tenant(dev, x_admin_token, x_session_token)
            eff_policy = get_effective_device_policy(dev)
        else:
            eff_policy = get_current_policy()

        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal is required.")
        profile = str(body.get("profile") or "auto").strip()
        target_devices = body.get("target_devices") if isinstance(body.get("target_devices"), list) else None
        plan = supervisormod.plan_goal_dag(
            conn,
            goal=goal,
            device_id=device_id,
            tenant=tenant,
            profile=profile,
            target_devices=target_devices,
            effective_policy=eff_policy,
        )
        audit(
            actor,
            device_id,
            "supervisor_plan_goal",
            {"goal": goal[:200], "profile": plan["profile"], "total_steps": plan["total_steps"]},
            "allowed",
            tenant=tenant,
        )
        return plan

    @router.post("/api/supervisor/handoff")
    def supervisor_handoff_api(
        body: SupervisorHandoffIn,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
        dev = conn.execute(
            "SELECT * FROM devices WHERE id=? AND revoked=0",
            (body.device_id,),
        ).fetchone()
        if dev is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(dev, x_admin_token, x_session_token)
        try:
            res = supervisormod.run_supervisor_handoff_enqueue(
                conn,
                device_id=body.device_id,
                tenant=tenant,
                goal=body.goal,
                profile=body.profile,
                mode=body.mode,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        notify_tasks()
        audit(
            actor,
            body.device_id,
            "supervisor_handoff_enqueue",
            {
                "goal": (body.goal or "")[:200],
                "profile": body.profile,
                "mode": body.mode,
                "handoff_id": res.get("handoff_id"),
                "queued_count": len(res.get("queued") or []),
            },
            "allowed",
            tenant=tenant,
        )
        return res

    @router.get("/api/supervisor/handoffs/{handoff_id}")
    def supervisor_handoff_status_api(
        handoff_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        try:
            return supervisormod.handoff_status(conn, handoff_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/supervisor/recover")
    def supervisor_recover_api(
        body: SupervisorRecoverIn,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        try:
            res = supervisormod.recover_failed_role_tasks(conn, role=body.role, device_id=body.device_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if res.get("count"):
            notify_tasks()
        audit(
            actor,
            body.device_id or "",
            "supervisor_recover_role_tasks",
            {"role": body.role, "count": res.get("count"), "requeued": res.get("requeued")},
            "allowed",
        )
        return res

    @router.get("/api/roles")
    def list_roles_api(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        return {"roles": role_catalog.list_roles(conn), "profiles": sorted(supervisormod.HANDOFF_PROFILES.keys())}

    @router.post("/api/roles/{role_id}/enable")
    def enable_role_agent_api(
        role_id: str,
        enabled: bool = Query(True),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        role_id = (role_id or "").strip().lower()
        if role_id not in role_catalog.ROLES:
            raise HTTPException(status_code=404, detail="Unknown role.")
        agent_id = f"agent-role-{role_id}"
        row = conn.execute("SELECT id FROM custom_agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            role_catalog.seed_role_catalog(conn)
        conn.execute(
            "UPDATE custom_agents SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, dbmod.now(), agent_id),
        )
        conn.commit()
        audit(actor, "", "role_agent_enable", {"role": role_id, "enabled": enabled}, "allowed")
        return {"ok": True, "role": role_id, "agent_id": agent_id, "enabled": bool(enabled)}

    @router.get("/api/activity/live")
    def live_activity(
        limit: int = 50,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        return live_activitymod.build_live_activity(conn, app.state, limit=limit)

    @router.get("/api/activity/stream")
    async def live_activity_stream(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)

        async def gen():
            while True:
                payload = live_activitymod.build_live_activity(conn, app.state, limit=40)
                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(2.0)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @router.post("/api/fleet/broadcast")
    def broadcast_fleet_command(
        body: FleetBroadcastIn,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        if plane_state.is_emergency_stop(conn, app.state):
            raise HTTPException(status_code=403, detail="Emergency stop is active; fleet broadcast is disabled.")
        _actor, role, _tenant, _is_legacy = _auth_context_full(x_admin_token, x_session_token)
        tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
        tool_name = normalize_tool_name(body.tool)
        tier = TOOL_TIERS.get(tool_name)
        if tier is None:
            raise HTTPException(status_code=400, detail=f"Unknown tool: '{body.tool}'.")
        query = "SELECT * FROM devices WHERE revoked=0 AND tenant=?"
        params = [tenant]
        if body.target_os:
            query += " AND LOWER(os) LIKE ?"
            params.append(f"%{body.target_os.lower()}%")
        devs = conn.execute(query, tuple(params)).fetchall()

        target_devs = []
        if body.target_device_ids:
            target_set = set(body.target_device_ids)
            target_devs = [d for d in devs if d["id"] in target_set]
        else:
            target_devs = list(devs)

        if not target_devs:
            raise HTTPException(status_code=400, detail="No matching online devices found for broadcast.")

        batch_id = uuid.uuid4().hex[:12]

        tasks_out = []
        for dev in target_devs:
            eff_pol = get_effective_device_policy(dev)
            if tool_name in eff_pol.get("denied_tools", []):
                continue
            if eff_pol.get("allowed_tools") is not None and tool_name not in eff_pol["allowed_tools"]:
                continue
            try:
                created = task_service_mod.create_task(
                    conn,
                    actor=actor,
                    role=role,
                    tenant=dev["tenant"],
                    device_id=dev["id"],
                    tool=tool_name,
                    args=body.args,
                    task_id=f"bcast-{batch_id}-{dev['id'][:6]}",
                    human_initiated=False,
                    device_row=dev,
                    get_effective_device_policy=get_effective_device_policy,
                    get_current_policy=get_current_policy,
                    notify=notify_tasks,
                    audit_fn=audit,
                )
            except HTTPException:
                continue  # takeover / e-stop / denied: skip this device, keep the rest
            tasks_out.append(
                {
                    "device_id": dev["id"],
                    "device_name": dev["name"],
                    "os": dev["os"],
                    "task_id": created["task_id"],
                    "status": created["status"],
                }
            )

        if not tasks_out:
            raise HTTPException(status_code=403, detail=f"Tool '{tool_name}' is blocked by policy on all matching devices.")
        conn.commit()
        notify_tasks()
        audit(actor, "fleet", "broadcast_command", {"batch_id": batch_id, "tool": tool_name, "device_count": len(tasks_out)}, "allowed", tenant=body.tenant)
        return {
            "batch_id": batch_id,
            "tool": tool_name,
            "device_count": len(tasks_out),
            "tasks": tasks_out,
        }

    @router.get("/api/fleet/broadcast/{batch_id}")
    def get_broadcast_status(
        batch_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(None, x_admin_token, x_session_token)
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE id LIKE ? AND tenant=?",
            (f"bcast-{batch_id}-%", tenant),
        ).fetchall()
        if not task_rows:
            raise HTTPException(status_code=404, detail="Broadcast batch not found.")

        results = []
        completed_count = 0
        failed_count = 0
        for r in task_rows:
            dev = conn.execute("SELECT name, os FROM devices WHERE id=?", (r["device_id"],)).fetchone()
            d_name = dev["name"] if dev else r["device_id"]
            d_os = dev["os"] if dev else ""
            status = r["status"]
            if status == "completed":
                completed_count += 1
            elif status == "failed":
                failed_count += 1
            res_val = None
            if r["result"]:
                try:
                    res_val = json.loads(r["result"])
                except Exception:
                    res_val = {"raw": r["result"]}
            results.append(
                {
                    "task_id": r["id"],
                    "device_id": r["device_id"],
                    "device_name": d_name,
                    "os": d_os,
                    "status": status,
                    "result": res_val,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )

        overall = "completed" if (completed_count + failed_count == len(task_rows)) else "running"
        return {
            "batch_id": batch_id,
            "status": overall,
            "total_tasks": len(task_rows),
            "completed": completed_count,
            "failed": failed_count,
            "tasks": results,
        }

    @router.get("/api/triggers")
    def list_triggers(
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        rows = conn.execute("SELECT * FROM triggers WHERE tenant=? ORDER BY created_at DESC", (tenant,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["action_args"] = json.loads(d.get("action_args_json") or "{}")
            except Exception:
                d["action_args"] = {}
            out.append(d)
        return out

    @router.post("/api/triggers")
    def create_trigger(
        body: TriggerIn,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
        tid = uuid.uuid4().hex[:12]
        now_ts = dbmod.now()
        conn.execute(
            "INSERT INTO triggers (id, name, tenant, device_id, metric, operator, threshold, action_type, "
            "action_target, action_args_json, cooldown_seconds, enabled, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid,
                body.name,
                tenant,
                body.device_id,
                body.metric,
                body.operator,
                body.threshold,
                body.action_type,
                body.action_target,
                json.dumps(body.action_args),
                body.cooldown_seconds,
                1 if body.enabled else 0,
                now_ts,
            ),
        )
        conn.commit()
        audit(actor, body.device_id or "", "create_trigger", {"trigger_id": tid, "name": body.name, "metric": body.metric}, "allowed", tenant=tenant)
        return {"trigger_id": tid, "status": "created"}

    @router.delete("/api/triggers/{trigger_id}")
    def delete_trigger(
        trigger_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Trigger not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        conn.execute("DELETE FROM triggers WHERE id=?", (trigger_id,))
        conn.commit()
        audit(actor, row["device_id"] or "", "delete_trigger", {"trigger_id": trigger_id}, "allowed", tenant=row["tenant"])
        return {"trigger_id": trigger_id, "status": "deleted"}

    @router.post("/api/triggers/{trigger_id}/toggle")
    def toggle_trigger(
        trigger_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Trigger not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        new_state = 0 if row["enabled"] else 1
        conn.execute("UPDATE triggers SET enabled=? WHERE id=?", (new_state, trigger_id))
        conn.commit()
        audit(actor, row["device_id"] or "", "toggle_trigger", {"trigger_id": trigger_id, "enabled": bool(new_state)}, "allowed", tenant=row["tenant"])
        return {"trigger_id": trigger_id, "enabled": bool(new_state)}

    @router.post("/api/triggers/{trigger_id}/test")
    def test_trigger(
        trigger_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Trigger not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        now_ts = dbmod.now()
        conn.execute("UPDATE triggers SET last_triggered_at=? WHERE id=?", (now_ts, trigger_id))
        conn.commit()
        act_type = row["action_type"]
        target = row["action_target"]
        args = json.loads(row["action_args_json"] or "{}")
        if act_type == "tool":
            res = _execute_workflow_step_sync(target, args, row["device_id"], row["tenant"], f"test_trigger:{row['name']}")
            return {"trigger_id": trigger_id, "status": "executed", "result": res}
        elif act_type == "webhook":
            wh_url = target if str(target).startswith("http") else get_webhook_url()
            evt = {
                "event": "trigger_test",
                "trigger_name": row["name"],
                "device_id": row["device_id"] or "fleet",
                "metric": row["metric"],
                "content": f"🧪 Test Trigger '{row['name']}' simulated by {actor}.",
            }
            send_webhook_notification(wh_url, evt, admin_token, get_public_base_url())
            return {"trigger_id": trigger_id, "status": "webhook_sent"}
        return {"trigger_id": trigger_id, "status": "ok"}

    @router.get("/api/schedules")
    def list_schedules(
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        rows = conn.execute("SELECT * FROM schedules WHERE tenant=? ORDER BY created_at DESC", (tenant,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["args"] = json.loads(d.get("args_json") or "{}")
            except Exception:
                d["args"] = {}
            out.append(d)
        return out

    @router.post("/api/schedules")
    def create_schedule(
        body: ScheduleIn,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
        sid = uuid.uuid4().hex[:12]
        now_ts = dbmod.now()
        interval = max(10, int(body.interval_seconds))
        next_run = now_ts + interval
        conn.execute(
            "INSERT INTO schedules (id, name, tenant, device_id, interval_seconds, tool, args_json, last_run_at, next_run_at, enabled, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                body.name,
                tenant,
                body.device_id,
                interval,
                body.tool,
                json.dumps(body.args),
                None,
                next_run,
                1 if body.enabled else 0,
                now_ts,
            ),
        )
        conn.commit()
        audit(actor, body.device_id or "", "create_schedule", {"schedule_id": sid, "name": body.name, "interval_seconds": interval}, "allowed", tenant=tenant)
        return {"schedule_id": sid, "status": "created", "next_run_at": next_run}

    @router.delete("/api/schedules/{schedule_id}")
    def delete_schedule(
        schedule_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Schedule not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        conn.commit()
        audit(actor, row["device_id"] or "", "delete_schedule", {"schedule_id": schedule_id}, "allowed", tenant=row["tenant"])
        return {"schedule_id": schedule_id, "status": "deleted"}

    @router.post("/api/schedules/{schedule_id}/toggle")
    def toggle_schedule(
        schedule_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Schedule not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        new_enabled = 0 if row["enabled"] else 1
        conn.execute("UPDATE schedules SET enabled=? WHERE id=?", (new_enabled, schedule_id))
        conn.commit()
        audit(actor, row["device_id"] or "", "toggle_schedule", {"schedule_id": schedule_id, "enabled": bool(new_enabled)}, "allowed", tenant=row["tenant"])
        return {"schedule_id": schedule_id, "enabled": bool(new_enabled)}

    @router.post("/api/schedules/{schedule_id}/run-now")
    def run_schedule_now(
        schedule_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Schedule not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        now_ts = dbmod.now()
        next_run = now_ts + max(10, int(row["interval_seconds"]))
        conn.execute("UPDATE schedules SET last_run_at=?, next_run_at=? WHERE id=?", (now_ts, next_run, schedule_id))
        conn.commit()
        args = json.loads(row["args_json"] or "{}")
        if row["tool"] == "__workflow__":
            wf_steps = args.get("steps") or []
            wf_id = uuid.uuid4().hex[:12]
            wf_name = str(args.get("name") or row["name"] or "Scheduled Workflow")
            conn.execute(
                "INSERT INTO workflows (id, name, tenant, device_id, steps_json, status, current_step, total_steps, results_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', 0, ?, '[]', ?, ?)",
                (wf_id, wf_name, row["tenant"], row["device_id"] or "local", json.dumps(wf_steps), len(wf_steps), now_ts, now_ts),
            )
            conn.commit()
            threading.Thread(
                target=_run_workflow_worker,
                args=(wf_id, row["device_id"] or None, row["tenant"], wf_steps, bool(args.get("stop_on_failure", True)), int(args.get("max_retries", 1)), actor),
                daemon=True,
            ).start()
            audit(actor, row["device_id"] or "", "run_schedule_now", {"schedule_id": schedule_id, "workflow_id": wf_id}, "executed", tenant=row["tenant"])
            return {"schedule_id": schedule_id, "status": "executed", "workflow_id": wf_id}
        res = _execute_workflow_step_sync(row["tool"], args, row["device_id"], row["tenant"], f"schedule_manual:{row['name']}")
        audit(actor, row["device_id"] or "", "run_schedule_now", {"schedule_id": schedule_id}, "executed", tenant=row["tenant"])
        return {"schedule_id": schedule_id, "status": "executed", "result": res}

    @router.post("/api/fleet/update")
    def update_fleet(
        target_os: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        from ... import updates as _updatesmod

        if not _updatesmod.signed_update_configured():
            raise HTTPException(
                status_code=409,
                detail="Signed fleet updates are not configured (set AI_BRIDGE_UPDATE_MANIFEST_PATH, AI_BRIDGE_UPDATE_SIGNATURE_PATH and AI_BRIDGE_UPDATE_PUBLIC_KEY); refusing unsigned fleet update.",
            )
        b_res = broadcast_fleet_command(
            FleetBroadcastIn(tool="update_fleet_agent", args={}, target_os=target_os),
            x_admin_token=x_admin_token,
            x_session_token=x_session_token,
        )
        audit(actor, "fleet", "update_fleet", {"batch_id": b_res.get("batch_id"), "target_os": target_os}, "executed")
        return b_res

    return router

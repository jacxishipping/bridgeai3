"""Release 4 governed capability routes (feature-flagged + audited).

Provides thin-slice endpoints for:
- Feature flags & adoption metrics (GET /api/features)
- Fleet analytics daily trends & CSV export (GET /api/fleet/analytics/trends)
- In-app Notification Centre / Incident Inbox (GET /api/incidents, POST /api/incidents/{incident_id}/ack)
- Policy-as-Code signed export/import (GET /api/policies/export, POST /api/policies/import)
- Curated read-only diagnostic workflow templates & per-step audit timeline
  (GET /api/workflows/curated, GET /api/workflows/{workflow_id}/timeline)
- Governed per-workspace memory snippets (GET/POST /api/memory/snippets, DELETE /api/memory/snippets/{snippet_id})
- Cross-platform device desktop capability contract (GET /api/devices/{device_id}/desktop/capabilities)
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import PlainTextResponse

from ...policy import TOOL_TIERS, normalize_tool_name
from .. import agent_memory as memorymod
from .. import analytics as analyticsmod
from .. import db as dbmod
from .. import desktop_capabilities as capmod
from .. import feature_flags as ffmod
from .. import incidents as incidentsmod
from .. import policy_bundle as bundlemod
from ._context import ControlContext

# Strictly Tier-1 read-only diagnostic workflow templates (verified against TOOL_TIERS at load time).
CURATED_DIAGNOSTIC_TEMPLATES: tuple[dict, ...] = (
    {
        "id": "curated_host_health",
        "name": "Host Health & Capacity Triage (Read-Only)",
        "description": "Collects OS version, CPU/RAM/disk utilization, and uptime without modifying host state.",
        "read_only": True,
        "max_tier": 1,
        "steps": [
            {"name": "Collect System Info", "action": "get_system_info", "args": {}},
            {"name": "Collect Resource Usage", "action": "get_resource_usage", "args": {}},
            {"name": "Inspect Disk Usage", "action": "get_disk_usage", "args": {}},
        ],
    },
    {
        "id": "curated_network_triage",
        "name": "Network & Process Snapshot (Read-Only)",
        "description": "Captures active network interfaces and top running processes for incident triage.",
        "read_only": True,
        "max_tier": 1,
        "steps": [
            {"name": "Collect Network Info", "action": "get_network_info", "args": {}},
            {"name": "List Top Processes", "action": "list_processes", "args": {"limit": 25}},
        ],
    },
    {
        "id": "curated_desktop_readonly",
        "name": "Desktop Status & Window Inventory (Read-Only)",
        "description": "Checks desktop session status and open window titles without sending any input.",
        "read_only": True,
        "max_tier": 1,
        "steps": [
            {"name": "Check Desktop Status", "action": "get_desktop_status", "args": {}},
            {"name": "List Active Windows", "action": "list_desktop_windows", "args": {}},
        ],
    },
)


def _validated_curated_templates() -> list[dict]:
    """Return curated templates verified to contain ONLY Tier-1 read-only tools."""
    safe: list[dict] = []
    for tpl in CURATED_DIAGNOSTIC_TEMPLATES:
        steps_ok = True
        for st in tpl["steps"]:
            tool = normalize_tool_name(str(st.get("action") or ""))
            tier = TOOL_TIERS.get(tool)
            if tier is None or tier.value > 1:
                steps_ok = False
                break
        if steps_ok:
            safe.append(tpl)
    return safe


def create_release4_router(ctx: ControlContext) -> APIRouter:
    router = APIRouter()
    _require_row_tenant = ctx._require_row_tenant
    audit = ctx.audit
    conn = ctx.conn
    get_current_policy = ctx.get_current_policy
    get_effective_device_policy = ctx.get_effective_device_policy
    require_role = ctx.require_role
    resolve_request_tenant = ctx.resolve_request_tenant
    token = ctx.token

    @router.get("/api/features", summary="List Release 4 feature flags and adoption metrics")
    def get_feature_flags(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        return ffmod.list_features()

    @router.get("/api/fleet/analytics/trends", summary="Daily fleet health and task-success trends (JSON or CSV)")
    def fleet_analytics_trends(
        days: int = 14,
        group: str | None = None,
        format: str = "json",
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("fleet_analytics")
        require_role("viewer", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        report = analyticsmod.compute_daily_trends(conn, tenant=eff_tenant, days=days, group=group)
        fmt = (format or "json").strip().lower()
        ffmod.record_adoption("fleet_analytics", "export_csv" if fmt == "csv" else "view_trends")
        if fmt == "csv":
            csv_body = analyticsmod.render_trends_csv(report)
            return PlainTextResponse(
                content=csv_body,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="fleet-trends.csv"'},
            )
        return report

    @router.get("/api/incidents", summary="List actionable in-app incidents (failed tasks, offline devices, expired approvals)")
    def list_incidents_route(
        include_resolved: bool = False,
        limit: int = 100,
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("incident_inbox")
        require_role("viewer", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        ffmod.record_adoption("incident_inbox", "list")
        return incidentsmod.list_incidents(conn, tenant=eff_tenant, include_resolved=include_resolved, limit=limit)

    @router.post("/api/incidents/{incident_id}/ack", summary="Acknowledge, assign, resolve, or reopen an incident")
    def update_incident_route(
        incident_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("incident_inbox")
        actor = require_role("operator", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(body.get("tenant") if isinstance(body, dict) else None, x_admin_token, x_session_token)
        action = str(body.get("action") or "ack") if isinstance(body, dict) else "ack"
        assignee = str(body.get("assignee") or "") if isinstance(body, dict) else ""
        note = str(body.get("note") or "") if isinstance(body, dict) else ""
        res = incidentsmod.update_incident_state(
            conn,
            tenant=eff_tenant,
            incident_id=incident_id,
            actor=actor,
            action=action,
            assignee=assignee or None,
            note=note,
        )
        ffmod.record_adoption("incident_inbox", action)
        audit(actor, "", f"incident_{action}", {"incident_id": incident_id, "assignee": res.get("assignee"), "status": res.get("status")}, "allowed", tenant=eff_tenant)
        return res

    @router.get("/api/policies/export", summary="Export signed Policy-as-Code bundle (HMAC-SHA256)")
    def export_policy_bundle_route(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("policy_as_code")
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT version, policy_json FROM policies ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            version = 0
            cur_policy = get_current_policy()
        else:
            version = int(row["version"])
            cur_policy = json.loads(row["policy_json"])
        bundle = bundlemod.sign_policy_bundle(cur_policy, version=version, actor=actor, fallback_admin_token=token)
        ffmod.record_adoption("policy_as_code", "export")
        audit(actor, "", "export_policy_bundle", {"version": version, "sha256": bundle["sha256"][:16]}, "allowed")
        return bundle

    @router.post("/api/policies/import", summary="Verify and import a signed Policy-as-Code bundle (supports dry_run)")
    def import_policy_bundle_route(
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("policy_as_code")
        actor = require_role("admin", x_admin_token, x_session_token)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        bundle = body.get("bundle") if isinstance(body.get("bundle"), dict) else body
        dry_run = bool(body.get("dry_run", True))
        clean_policy = bundlemod.verify_policy_bundle(bundle, fallback_admin_token=token)

        row = conn.execute("SELECT version, policy_json FROM policies ORDER BY version DESC LIMIT 1").fetchone()
        current_version = int(row["version"]) if row else 0
        current_policy = json.loads(row["policy_json"]) if row else get_current_policy()

        diffs = bundlemod.diff_policies(current_policy, clean_policy)
        simulations = bundlemod.simulate_policy_dict(clean_policy)

        if dry_run:
            ffmod.record_adoption("policy_as_code", "import_dry_run")
            return {
                "verified": True,
                "applied": False,
                "dry_run": True,
                "current_version": current_version,
                "diffs": diffs,
                "simulations": simulations,
                "policy": clean_policy,
            }

        next_version = current_version + 1
        note = str(clean_policy.get("note") or f"Imported signed bundle from v{bundle.get('source_version', 0)}")[:500]
        conn.execute(
            "INSERT INTO policies (version, policy_json, created_by, created_at, note) VALUES (?,?,?,?,?)",
            (next_version, json.dumps(clean_policy), actor, dbmod.now(), note),
        )
        conn.commit()
        ffmod.record_adoption("policy_as_code", "import_apply")
        audit(
            actor,
            "",
            "import_policy_bundle",
            {"version": next_version, "sha256": str(bundle.get("sha256", ""))[:16], "diff_count": len(diffs)},
            "allowed",
        )
        return {
            "verified": True,
            "applied": True,
            "dry_run": False,
            "version": next_version,
            "diffs": diffs,
            "simulations": simulations,
            "policy": clean_policy,
        }

    @router.get("/api/workflows/curated", summary="List curated Tier-1 read-only diagnostic workflow templates")
    def list_curated_workflows(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("curated_workflows")
        require_role("viewer", x_admin_token, x_session_token)
        ffmod.record_adoption("curated_workflows", "list_templates")
        templates = _validated_curated_templates()
        return {"templates": templates, "count": len(templates)}

    @router.get("/api/workflows/{workflow_id}/timeline", summary="Per-step approval-to-audit timeline for a workflow run")
    def workflow_audit_timeline(
        workflow_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("curated_workflows")
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        wf = dict(row)
        steps = json.loads(wf.get("steps_json") or "[]")
        results = json.loads(wf.get("results_json") or "[]")

        task_ids = [r.get("task_id") for r in results if isinstance(r, dict) and r.get("task_id")]
        events_by_task: dict[str, list[dict]] = {}
        approvals_by_task: dict[str, list[dict]] = {}
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            for ev in conn.execute(
                f"SELECT id, task_id, event, detail_json, created_at, trace_id FROM task_events WHERE task_id IN ({placeholders}) ORDER BY created_at ASC",
                tuple(task_ids),
            ).fetchall():
                events_by_task.setdefault(ev["task_id"], []).append(dict(ev))
            for ap in conn.execute(
                f"SELECT id, task_id, tool, status, decided_by, reason, created_at, decided_at "
                f"FROM approvals WHERE task_id IN ({placeholders}) ORDER BY created_at ASC",
                tuple(task_ids),
            ).fetchall():
                approvals_by_task.setdefault(ap["task_id"], []).append(dict(ap))

        step_timelines = []
        for idx, step in enumerate(steps):
            res = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {}
            tid = res.get("task_id")
            tool = normalize_tool_name(str(step.get("action") or step.get("tool") or ""))
            tier_obj = TOOL_TIERS.get(tool)
            step_timelines.append({
                "index": idx,
                "name": step.get("name") or tool or f"Step {idx + 1}",
                "tool": tool,
                "tier": tier_obj.value if tier_obj else None,
                "status": res.get("status") or ("pending" if idx >= len(results) else "unknown"),
                "task_id": tid,
                "approvals": approvals_by_task.get(tid, []) if tid else [],
                "events": events_by_task.get(tid, []) if tid else [],
            })

        ffmod.record_adoption("curated_workflows", "view_timeline")
        return {
            "workflow_id": workflow_id,
            "name": wf.get("name"),
            "status": wf.get("status"),
            "device_id": wf.get("device_id"),
            "created_at": wf.get("created_at"),
            "updated_at": wf.get("updated_at"),
            "steps": step_timelines,
        }

    @router.get("/api/memory/snippets", summary="List governed read-only workspace knowledge snippets")
    def list_memory_snippets_route(
        workspace: str | None = None,
        opt_in_only: bool = False,
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("governed_memory")
        require_role("viewer", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        items = memorymod.list_governed_snippets(conn, tenant=eff_tenant, workspace=workspace, opt_in_only=opt_in_only)
        ffmod.record_adoption("governed_memory", "list")
        return {"tenant": eff_tenant, "workspace": workspace, "count": len(items), "items": items}

    @router.post("/api/memory/snippets", summary="Create a governed read-only workspace snippet with provenance")
    def create_memory_snippet_route(
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("governed_memory")
        actor = require_role("operator", x_admin_token, x_session_token)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object.")
        eff_tenant = resolve_request_tenant(body.get("tenant"), x_admin_token, x_session_token)
        record = memorymod.create_governed_snippet(
            conn,
            tenant=eff_tenant,
            workspace=str(body.get("workspace") or ""),
            title=str(body.get("title") or ""),
            content=str(body.get("content") or ""),
            source=str(body.get("source") or ""),
            created_by=actor,
            opt_in_prompt=bool(body.get("opt_in_prompt", False)),
        )
        ffmod.record_adoption("governed_memory", "create")
        audit(
            actor,
            "",
            "create_memory_snippet",
            {"snippet_id": record["id"], "workspace": record["workspace"], "opt_in_prompt": record["opt_in_prompt"]},
            "allowed",
            tenant=eff_tenant,
        )
        return record

    @router.delete("/api/memory/snippets/{snippet_id}", summary="Delete a governed workspace snippet")
    def delete_memory_snippet_route(
        snippet_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("governed_memory")
        actor = require_role("operator", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(None, x_admin_token, x_session_token)
        deleted = memorymod.delete_governed_snippet(conn, tenant=eff_tenant, snippet_id=snippet_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Snippet not found.")
        ffmod.record_adoption("governed_memory", "delete")
        audit(actor, "", "delete_memory_snippet", {"snippet_id": snippet_id}, "allowed", tenant=eff_tenant)
        return {"snippet_id": snippet_id, "deleted": True}

    @router.get("/api/devices/{device_id}/desktop/capabilities", summary="Capability-aware cross-platform desktop contract for one device")
    def device_desktop_capabilities_route(
        device_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        ffmod.require_feature("desktop_capabilities_v2")
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        eff_policy = get_effective_device_policy(row)
        ffmod.record_adoption("desktop_capabilities_v2", "inspect")
        return capmod.device_desktop_capabilities(dict(row), eff_policy)

    @router.get("/api/devices/{device_id}/memory", summary="Recall device-scoped episodic memories and recovery playbooks")
    def get_device_memory_route(
        device_id: str,
        q: str | None = None,
        episode_type: str | None = None,
        limit: int = 20,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        if device_id != "local":
            row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Device not found.")
            _require_row_tenant(row, x_admin_token, x_session_token)
            eff_tenant = str(row["tenant"] or "default")
        else:
            eff_tenant = resolve_request_tenant(None, x_admin_token, x_session_token)
        items = memorymod.recall_device_memories(
            conn,
            tenant=eff_tenant,
            device_id=device_id,
            query=q,
            episode_type=episode_type,
            limit=limit,
        )
        return {"device_id": device_id, "tenant": eff_tenant, "count": len(items), "episodes": items}

    @router.post("/api/devices/{device_id}/memory/learn", summary="Record a device-level learned recovery episode or UI quirk")
    def learn_device_memory_route(
        device_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object.")
        if device_id != "local":
            row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Device not found.")
            _require_row_tenant(row, x_admin_token, x_session_token)
            eff_tenant = str(row["tenant"] or "default")
        else:
            eff_tenant = resolve_request_tenant(body.get("tenant"), x_admin_token, x_session_token)
        episode = memorymod.record_device_episode(
            conn,
            tenant=eff_tenant,
            device_id=device_id,
            agent_id=str(body.get("agent_id") or f"operator:{actor}"),
            episode_type=str(body.get("episode_type") or "recovery_playbook"),
            tool=str(body.get("tool") or ""),
            context_summary=str(body.get("context_summary") or ""),
            outcome=str(body.get("outcome") or "resolved"),
            resolution=str(body.get("resolution") or ""),
        )
        audit(
            actor,
            device_id,
            "learn_device_memory",
            {"episode_id": episode["id"], "episode_type": episode["episode_type"], "tool": episode["tool"]},
            "allowed",
            tenant=eff_tenant,
        )
        return episode

    return router

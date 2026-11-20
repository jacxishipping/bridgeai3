"""audit routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from .. import db as dbmod
from ._context import ControlContext


def create_audit_router(ctx: ControlContext) -> APIRouter:
    """Build the audit APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    audit = ctx.audit
    conn = ctx.conn
    require_admin = ctx.require_admin
    require_role = ctx.require_role
    resolve_request_tenant = ctx.resolve_request_tenant

    @router.get("/api/audit")
    def list_audit(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None), tenant: str | None = None, tool: str | None = None):
        import hashlib
        require_role("viewer", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        if tool:
            rows = conn.execute("SELECT * FROM audit WHERE tenant=? AND tool=? ORDER BY id ASC LIMIT 500", (tenant, tool)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit WHERE tenant=? ORDER BY id ASC LIMIT 500", (tenant,)).fetchall()
        prev_h = "0" * 64
        enriched = []
        for r in rows:
            d = dict(r)
            stored_prev = d.get("prev_hash")
            stored_entry = d.get("entry_hash")
            if stored_prev and stored_entry:
                d["prev_hash"] = stored_prev
                d["hash"] = stored_entry
                d["entry_hash"] = stored_entry
                prev_h = stored_entry
            else:
                payload = f"{prev_h}|{d.get('id')}|{d.get('ts')}|{d.get('actor')}|{d.get('device_id')}|{d.get('tool')}|{d.get('args_json')}|{d.get('decision')}"
                cur_h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                d["prev_hash"] = prev_h
                d["hash"] = cur_h
                d["entry_hash"] = cur_h
                prev_h = cur_h
            enriched.append(d)
        return list(reversed(enriched))

    @router.api_route("/api/audit/{audit_id}", methods=["PUT", "DELETE"])
    def immutable_audit(audit_id: int):
        raise HTTPException(status_code=405, detail="Audit records are immutable and cannot be modified or deleted.")

    @router.get("/api/audit/export")
    def export_audit(tenant: str | None = None, since: float | None = None, until: float | None = None,
                     x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("admin", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        clauses, params = ["tenant=?"], [tenant]
        if since is not None:
            clauses.append("ts>=?"); params.append(since)
        if until is not None:
            clauses.append("ts<=?"); params.append(until)
        rows = conn.execute("SELECT * FROM audit WHERE " + " AND ".join(clauses) + " ORDER BY ts ASC LIMIT 10000", tuple(params)).fetchall()
        return {"format": "jsonl", "count": len(rows), "events": [dict(r) for r in rows]}

    @router.get("/api/audit/verify")
    def verify_audit_chain_api(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_admin(x_admin_token, x_session_token)
        return dbmod.verify_audit_chain(conn)

    @router.post("/api/audit/retention")
    def apply_audit_retention(days: int = 365, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        days = max(30, min(int(days), 3650))
        cutoff = dbmod.now() - days * 86400
        from .. import worm_siem as worm_siemmod
        if worm_siemmod.worm_siem_required() and not worm_siemmod.worm_siem_url():
            raise HTTPException(status_code=409, detail="Audit retention refused: WORM/SIEM export is required (AI_BRIDGE_WORM_SIEM_URL) but not configured.")
        _old_rows = conn.execute("SELECT * FROM audit WHERE ts<?", (cutoff,)).fetchall()
        if worm_siemmod.worm_siem_url():
            for _r in _old_rows:
                _res = worm_siemmod.emit_audit_event(dict(_r))
                if not _res.get("ok"):
                    raise HTTPException(status_code=502, detail="Audit retention refused: WORM/SIEM export failed; no records deleted.")
        cur = conn.execute("DELETE FROM audit WHERE ts<?", (cutoff,))
        conn.commit()
        audit(actor, "", "audit_retention", {"days": days, "deleted_count": cur.rowcount}, "allowed")
        return {"retention_days": days, "deleted_count": cur.rowcount}

    @router.get("/api/traces/{trace_id}")
    def get_trace_timeline(
        trace_id: str,
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        """Return correlated end-to-end execution timeline for a trace/correlation ID.

        Joins tasks, task_events, approvals, and audit records scoped to the caller's
        tenant so a single request can be traced from dashboard/MCP submission through
        agent execution and audit logging.
        """
        require_role("viewer", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        tid = (trace_id or "").strip()
        if not tid or len(tid) > 128:
            raise HTTPException(status_code=400, detail="Invalid trace_id.")
        tasks = [
            dict(r)
            for r in conn.execute(
                f"SELECT {dbmod.TASK_LIST_COLUMNS} FROM tasks WHERE tenant=? AND trace_id=? ORDER BY created_at ASC LIMIT 200",
                (eff_tenant, tid),
            ).fetchall()
        ]
        task_ids = [t["id"] for t in tasks]
        events: list[dict] = []
        approvals: list[dict] = []
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            events = [
                dict(r)
                for r in conn.execute(
                    f"SELECT id, task_id, event, detail_json, created_at, trace_id FROM task_events "
                    f"WHERE task_id IN ({placeholders}) ORDER BY created_at ASC LIMIT 500",
                    tuple(task_ids),
                ).fetchall()
            ]
            approvals = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM approvals WHERE task_id IN ({placeholders}) ORDER BY created_at ASC LIMIT 200",
                    tuple(task_ids),
                ).fetchall()
            ]
        audit_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM audit WHERE tenant=? AND trace_id=? ORDER BY id ASC LIMIT 500",
                (eff_tenant, tid),
            ).fetchall()
        ]
        return {
            "trace_id": tid,
            "tenant": eff_tenant,
            "tasks": tasks,
            "task_events": events,
            "approvals": approvals,
            "audit": audit_rows,
        }

    return router

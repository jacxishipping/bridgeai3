"""devices routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""

from __future__ import annotations

import json
import secrets
import time
import uuid

from fastapi import APIRouter, Body, Header, HTTPException, Response

from ...policy import TOOL_TIERS
from .. import db as dbmod
from .. import desktop_capabilities as desktop_capabilitiesmod
from .. import fleet_health as fleet_healthmod
from .. import plane_state
from .. import task_service as task_service_mod
from ..app import DevicePolicyIn, DeviceRefreshIn, DeviceUpdateIn, EnrollIn
from ._context import ControlContext


def create_devices_router(ctx: ControlContext) -> APIRouter:
    """Build the devices APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    _execute_workflow_step_sync = ctx._execute_workflow_step_sync
    _get_device_live_meta = ctx._get_device_live_meta
    _get_devices_live_meta = ctx._get_devices_live_meta
    _require_device_tenant = ctx._require_device_tenant
    _require_row_tenant = ctx._require_row_tenant
    _row_tenant = ctx._row_tenant
    app = ctx.app
    audit = ctx.audit
    conn = ctx.conn
    device_row = ctx.device_row
    device_ttl_days = ctx.device_ttl_days
    get_current_policy = ctx.get_current_policy
    get_effective_device_policy = ctx.get_effective_device_policy
    notify_tasks = ctx.notify_tasks
    reject_policy_widening = ctx.reject_policy_widening
    require_admin = ctx.require_admin
    require_role = ctx.require_role
    resolve_request_tenant = ctx.resolve_request_tenant

    @router.post("/api/devices/enroll", summary="Enroll a new device", description="Registers a new device in the fleet and returns a one-time device token.")
    def enroll(body: EnrollIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
        device_id = uuid.uuid4().hex[:12]
        device_token = secrets.token_urlsafe(32)
        issued = dbmod.now()
        expires = issued + device_ttl_days * 86400
        initial_policy = "{}"
        if body.unrestricted is True or (isinstance(body.policy, dict) and body.policy.get("unrestricted")):
            initial_policy = json.dumps(
                {
                    "inherit_global": False,
                    "unrestricted": True,
                    "unrestricted_granted_by": actor,
                    "allow_writes": True,
                    "allow_tier3": True,
                    "desktop_enabled": True,
                    "allow_shell": True,
                    "allow_package_install": True,
                    "allow_file_deletion": True,
                    "max_tier_unattended": 4,
                    "allowed_apps": ["*"],
                    "allowed_services": ["*"],
                    "note": "Unrestricted Linux Server Policy (Auto-configured on enrollment)",
                }
            )
        elif isinstance(body.policy, dict) and body.policy:
            initial_policy = json.dumps(body.policy)
        conn.execute(
            "INSERT INTO devices (id, name, hostname, os, tenant, token_hash, created_at, token_issued_at, token_expires_at, policy_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (device_id, body.name, body.hostname, body.os, tenant, dbmod.hash_token(device_token), issued, issued, expires, initial_policy),
        )
        conn.commit()
        from .. import device_mtls

        mtls_fp = None
        if isinstance(body.policy, dict):
            mtls_fp = body.policy.get("mtls_fingerprint") or body.policy.get("client_cert_fingerprint")
        enrolled_fp = None
        if mtls_fp:
            enrolled_fp = device_mtls.normalize_fingerprint(str(mtls_fp))
            device_mtls.set_device_fingerprint(conn, device_id, enrolled_fp)
        audit(actor, device_id, "enroll_device", body.model_dump(), "allowed", tenant=tenant)
        if body.unrestricted is True or (isinstance(body.policy, dict) and body.policy.get("unrestricted")):
            try:
                from ... import audit_writer as _audit_writer

                _audit_writer.write_audit_event(actor=actor, action="policy.unrestricted_granted", device_id=device_id, details={"granted_by": actor, "tenant": tenant})
            except Exception:
                pass
        # Token is shown exactly once here; only its hash is stored.
        return {
            "device_id": device_id,
            "device_token": device_token,
            "token_expires_at": expires,
            "token_ttl_days": device_ttl_days,
            "mtls_fingerprint": enrolled_fp,
            "note": "Store the device token securely; it will not be shown again. It expires and must be rotated.",
        }

    @router.get(
        "/api/devices",
        summary="List devices",
        description="Lists devices in the fleet with optional server-side filtering, pagination (limit/offset + X-Total-Count), metadata, and live telemetry.",
    )
    def list_devices(
        response: Response,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        tenant: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        group: str | None = None,
        os: str | None = None,
        status: str | None = None,
        q: str | None = None,
    ):
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        require_role("viewer", x_admin_token, x_session_token)
        rows = conn.execute(
            "SELECT d.id, d.name, d.hostname, d.os, d.tenant, d.status, d.last_seen, d.revoked, d.created_at, "
            "d.token_expires_at, d.policy_json, m.group_name, m.tags_json FROM devices d "
            "LEFT JOIN device_metadata m ON m.device_id=d.id WHERE d.tenant=? ORDER BY d.created_at DESC, d.name ASC",
            (tenant,),
        ).fetchall()
        result = []
        group_q = (group or "").strip().lower()
        os_q = (os or "").strip().lower()
        status_q = (status or "").strip().lower()
        search_q = (q or "").strip().lower()

        now_ts = dbmod.now()
        filtered_rows = []
        for r in rows:
            d = dict(r)
            d["group"] = d.pop("group_name", None) or ""
            d["tags"] = json.loads(d.pop("tags_json", None) or "[]")
            if d.get("revoked"):
                d["status"] = "revoked"
            elif d.get("status") == "online" and d.get("last_seen") is not None:
                try:
                    if (now_ts - float(d["last_seen"])) >= 90:
                        d["status"] = "offline"
                except (TypeError, ValueError):
                    pass
            if group_q and d["group"].lower() != group_q:
                continue
            if os_q and os_q not in (d.get("os") or "").lower():
                continue
            if status_q:
                effective_status = "revoked" if d.get("revoked") else (d.get("status") or "offline").lower()
                if effective_status != status_q:
                    continue
            if search_q:
                haystack = " ".join([
                    str(d.get("id") or ""),
                    str(d.get("name") or ""),
                    str(d.get("hostname") or ""),
                    str(d.get("group") or ""),
                    " ".join(str(t) for t in d.get("tags") or []),
                ]).lower()
                if search_q not in haystack:
                    continue
            filtered_rows.append(d)

        total = len(filtered_rows)
        response.headers["X-Total-Count"] = str(total)
        safe_offset = max(0, int(offset or 0))
        if limit is not None:
            safe_limit = max(1, min(500, int(limit)))
            page_rows = filtered_rows[safe_offset : safe_offset + safe_limit]
        elif safe_offset > 0:
            page_rows = filtered_rows[safe_offset:]
        else:
            page_rows = filtered_rows

        # Wave 6A (W5E-01): one batched live-meta lookup for the returned page
        # instead of up to 2 task-history queries per device.
        live_metas = _get_devices_live_meta([d["id"] for d in page_rows])
        for d in page_rows:
            raw_p = d.get("policy_json") or "{}"
            try:
                loaded_p = json.loads(raw_p) if isinstance(raw_p, str) else (raw_p or {})
                parsed_p = {k: v for k, v in loaded_p.items() if v is not None} if isinstance(loaded_p, dict) else {}
            except Exception:
                parsed_p = {}
            d["has_custom_policy"] = bool(parsed_p)
            d["policy"] = parsed_p
            d["effective_policy"] = get_effective_device_policy(d)
            live_meta = live_metas.get(d["id"]) or {}
            if live_meta.get("ip_address"):
                d["ip_address"] = live_meta["ip_address"]
            result.append(d)
        return result

    @router.post("/api/devices/{device_id}/revoke")
    def revoke(device_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_admin(x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        cur = conn.execute("UPDATE devices SET revoked=1, status='revoked' WHERE id=?", (device_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Device not found.")
        # F-04-10 (Wave 7): a revoked device's cached verify binding must
        # die immediately, not at the 5 s cache TTL. This clears the
        # in-process cache of the embedded deployment (MCP server and
        # control plane sharing one process); in split deployments the
        # 5 s TTL still bounds the window.
        try:
            from ...device_routing import clear_verify_cache as _clear_verify_cache

            _clear_verify_cache()
        except Exception:  # noqa: BLE001 - cache clear is best-effort; revoke already committed
            pass
        audit("admin", device_id, "revoke_device", {}, "allowed")
        return {"device_id": device_id, "revoked": True}

    @router.put("/api/devices/{device_id}")
    def update_device(device_id: str, body: DeviceUpdateIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        name = body.name.strip() if body.name is not None and body.name.strip() else row["name"]
        hostname = body.hostname.strip() if body.hostname is not None else (row["hostname"] or "")
        os_val = body.os.strip() if body.os is not None else (row["os"] or "")
        revoked = int(bool(body.revoked)) if body.revoked is not None else int(row["revoked"] or 0)
        status = "revoked" if revoked else ("offline" if row["status"] == "revoked" else row["status"])
        policy_json = json.dumps(body.policy) if body.policy is not None else (row["policy_json"] if "policy_json" in row.keys() else "{}")
        if isinstance(body.policy, dict) and body.policy.get("unrestricted"):
            _stamped = dict(body.policy)
            _stamped["unrestricted_granted_by"] = actor
            policy_json = json.dumps(_stamped)
            try:
                from ... import audit_writer as _audit_writer

                _audit_writer.write_audit_event(actor=actor, action="policy.unrestricted_granted", device_id=device_id, details={"granted_by": actor, "tenant": _row_tenant(row)})
            except Exception:
                pass
        conn.execute(
            "UPDATE devices SET name=?, hostname=?, os=?, revoked=?, status=?, policy_json=? WHERE id=?",
            (name, hostname, os_val, revoked, status, policy_json, device_id),
        )
        conn.commit()
        audit(actor, device_id, "update_device", {"name": name, "hostname": hostname, "os": os_val, "revoked": bool(revoked), "policy_updated": body.policy is not None}, "allowed")
        updated = conn.execute(
            "SELECT id, name, hostname, os, tenant, status, last_seen, revoked, created_at, token_expires_at, policy_json FROM devices WHERE id=?",
            (device_id,),
        ).fetchone()
        d = dict(updated)
        try:
            d["policy"] = json.loads(d.get("policy_json") or "{}")
        except Exception:
            d["policy"] = {}
        d["has_custom_policy"] = bool(d["policy"])
        return d

    @router.delete("/api/devices/{device_id}")
    def delete_device(device_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        counts = dbmod.delete_device_cascade(conn, device_id)
        if counts.get("devices", 0) == 0:
            raise HTTPException(status_code=404, detail="Device not found.")
        audit(actor, device_id, "delete_device", {"cascade_counts": counts}, "allowed")
        return {"device_id": device_id, "deleted": True, "cascade_counts": counts}

    @router.get("/api/devices/{device_id}/policy")
    def get_device_policy_api(device_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        effective = get_effective_device_policy(row)
        raw_p = ""
        try:
            raw_p = row["policy_json"]
        except Exception:
            pass
        try:
            loaded_custom = json.loads(raw_p) if isinstance(raw_p, str) else (raw_p or {})
            custom = {k: v for k, v in loaded_custom.items() if v is not None} if isinstance(loaded_custom, dict) else {}
        except Exception:
            custom = {}
        all_tools = []
        for tool_name, tier_enum in sorted(TOOL_TIERS.items()):
            is_allowed = tool_name not in effective.get("denied_tools", []) and (effective.get("allowed_tools") is None or tool_name in effective["allowed_tools"])
            all_tools.append(
                {
                    "tool": tool_name,
                    "tier": tier_enum.value,
                    "tier_name": tier_enum.name,
                    "allowed": is_allowed,
                }
            )
        return {
            "device_id": device_id,
            "device_name": row["name"],
            "hostname": row["hostname"] or "",
            "os": row["os"] or "",
            "has_custom_policy": bool(custom),
            "custom_policy": custom,
            "effective_policy": effective,
            "global_policy": get_current_policy(),
            "tools": all_tools,
        }

    @router.put("/api/devices/{device_id}/policy")
    def set_device_policy_api(
        device_id: str, body: DevicePolicyIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None), x_device_token: str | None = Header(default=None)
    ):
        if x_device_token:
            dev_row = device_row(x_device_token)
            if dev_row["id"] != device_id:
                raise HTTPException(status_code=403, detail="Device token not authorized for this device_id.")
            actor = f"device:{device_id}"
            reject_policy_widening(get_effective_device_policy(dev_row), body.model_dump())
        else:
            actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        existing_custom: dict = {}
        try:
            raw_existing = row["policy_json"]
            if raw_existing:
                parsed_existing = json.loads(raw_existing) if isinstance(raw_existing, str) else raw_existing
                if isinstance(parsed_existing, dict):
                    existing_custom = {k: v for k, v in parsed_existing.items() if v is not None}
        except Exception:
            existing_custom = {}
        incoming = body.model_dump(exclude_none=True)
        policy_data = {**existing_custom, **incoming}
        if incoming.get("allow_full_pc_access") is True or incoming.get("unrestricted") is True:
            policy_data["unrestricted"] = True
            policy_data["allow_full_pc_access"] = True
        elif incoming.get("allow_full_pc_access") is False or incoming.get("unrestricted") is False:
            policy_data["unrestricted"] = False
            policy_data["allow_full_pc_access"] = False
        if not x_device_token and policy_data.get("unrestricted"):
            policy_data["unrestricted_granted_by"] = actor
            try:
                from ... import audit_writer as _audit_writer

                _audit_writer.write_audit_event(actor=actor, action="policy.unrestricted_granted", device_id=device_id, details={"granted_by": actor, "tenant": _row_tenant(row)})
            except Exception:
                pass
        conn.execute("UPDATE devices SET policy_json=? WHERE id=?", (json.dumps(policy_data), device_id))
        conn.commit()
        audit(actor, device_id, "set_device_policy", policy_data, "allowed")
        return get_device_policy_api(device_id, x_admin_token, x_session_token)

    @router.delete("/api/devices/{device_id}/policy")
    def reset_device_policy_api(
        device_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None), x_device_token: str | None = Header(default=None)
    ):
        # Only an administrator may remove a device policy: resetting to the
        # (possibly wider) global policy is a privilege increase.
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)
        conn.execute("UPDATE devices SET policy_json='{}' WHERE id=?", (device_id,))
        conn.commit()
        audit(actor, device_id, "reset_device_policy", {}, "allowed")
        return {"device_id": device_id, "reset": True, "message": "Device policy reset to inherit global server policy."}

    @router.get("/api/devices/{device_id}/overview")
    def get_device_overview(device_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(row, x_admin_token, x_session_token)

        eff_policy = get_effective_device_policy(row)
        device_data = dict(row)
        try:
            loaded_p = json.loads(device_data.get("policy_json") or "{}")
            device_data["policy"] = {k: v for k, v in loaded_p.items() if v is not None} if isinstance(loaded_p, dict) else {}
        except Exception:
            device_data["policy"] = {}
        device_data["has_custom_policy"] = bool(device_data["policy"])
        device_data["effective_policy"] = eff_policy

        # Find latest completed diagnostic tasks for this device (bounded to diagnostic tools;
        # never scan unbounded multi-MB screenshot payloads).
        _OVERVIEW_TOOLS = (
            "get_system_info",
            "get_resource_usage",
            "get_network_info",
            "list_processes",
            "list_docker_containers",
            "get_disk_usage",
            "get_memory_usage",
            "get_desktop_status",
        )
        _t_placeholders = ",".join("?" for _ in _OVERVIEW_TOOLS)
        diag_rows = conn.execute(
            f"SELECT tool, result, updated_at FROM tasks WHERE device_id=? AND status='completed' "
            f"AND tool IN ({_t_placeholders}) ORDER BY updated_at DESC LIMIT 100",
            (device_id, *_OVERVIEW_TOOLS),
        ).fetchall()

        latest_results: dict[str, dict] = {}
        for r in diag_rows:
            tool_name = r["tool"]
            if tool_name not in latest_results:
                raw_res = r["result"] or ""
                parsed = None
                if raw_res:
                    try:
                        parsed = json.loads(raw_res)
                    except Exception:
                        parsed = raw_res
                latest_results[tool_name] = {
                    "data": parsed,
                    "updated_at": r["updated_at"],
                }

        # System info parsing
        sys_entry = latest_results.get("get_system_info", {})
        sys_info = sys_entry.get("data")
        if not isinstance(sys_info, dict):
            sys_info = {
                "system": row["os"] or "Unknown OS",
                "release": "",
                "version": "",
                "machine": "",
                "python": "",
                "processor": "",
            }

        # Resource usage parsing
        res_entry = latest_results.get("get_resource_usage", {})
        res_usage = res_entry.get("data")
        if not isinstance(res_usage, dict):
            res_usage = {
                "cpu_percent": None,
                "memory_percent": None,
                "memory_total_bytes": None,
                "memory_available_bytes": None,
                "disk_total_bytes": None,
                "disk_used_bytes": None,
                "disk_free_bytes": None,
            }

        # Processes parsing
        proc_entry = latest_results.get("list_processes", {})
        proc_data = proc_entry.get("data")
        processes = proc_data if isinstance(proc_data, list) else []

        # Docker containers parsing
        docker_entry = latest_results.get("list_docker_containers", {})
        docker_data = docker_entry.get("data")

        # Network info parsing (interfaces & listening ports)
        net_entry = latest_results.get("get_network_info", {})
        net_data = net_entry.get("data")

        # Desktop / Screenshot / Windows UI
        desktop_entry = latest_results.get("get_desktop_status", {})
        screenshot_entry = latest_results.get("capture_desktop_screenshot", {})
        windows_entry = latest_results.get("perform_approved_ui_action", {})
        win_data = windows_entry.get("data")
        windows_list = win_data.get("windows") if isinstance(win_data, dict) else None

        # Packages installed via tasks
        pkg_rows = conn.execute(
            "SELECT args_json, result, updated_at FROM tasks WHERE device_id=? AND tool='install_package' AND status='completed' ORDER BY updated_at DESC",
            (device_id,),
        ).fetchall()
        packages_installed = []
        for pr in pkg_rows:
            try:
                p_args = json.loads(pr["args_json"] or "{}")
                p_res = json.loads(pr["result"] or "{}") if pr["result"] else {}
                packages_installed.append(
                    {
                        "package_id": p_args.get("package_id"),
                        "version": p_args.get("version") or p_res.get("version"),
                        "manager": p_res.get("manager", "package"),
                        "updated_at": pr["updated_at"],
                    }
                )
            except Exception:
                pass

        # Recent 20 tasks
        recent_tasks_rows = conn.execute(
            "SELECT id, tool, args_json, tier, status, result, created_at, updated_at FROM tasks WHERE device_id=? ORDER BY created_at DESC LIMIT 20",
            (device_id,),
        ).fetchall()
        recent_tasks = []
        for tr in recent_tasks_rows:
            td = dict(tr)
            try:
                td["args"] = json.loads(td.get("args_json") or "{}")
            except Exception:
                td["args"] = {}
            recent_tasks.append(td)

        pending_approval_rows = conn.execute(
            "SELECT a.id, a.task_id, a.tool, a.reason, a.created_at, a.expires_at, a.policy_version, "
            "t.tier, t.args_json FROM approvals a JOIN tasks t ON a.task_id = t.id "
            "WHERE t.device_id=? AND a.status='pending' ORDER BY a.created_at DESC",
            (device_id,),
        ).fetchall()
        pending_approvals = []
        for ar in pending_approval_rows:
            ad = dict(ar)
            try:
                ad["args"] = json.loads(ad.get("args_json") or "{}")
            except Exception:
                ad["args"] = {}
            pending_approvals.append(ad)

        now_ts = dbmod.now()
        last_seen = row["last_seen"] or 0
        latency = max(0.0, now_ts - last_seen) if last_seen else None
        is_online = (row["status"] == "online") and (latency is not None and latency < 90)
        device_data["status"] = "revoked" if row["revoked"] else ("online" if is_online else "offline")

        # Service statuses probe results (if any service queried)
        service_statuses = {}
        for r in diag_rows:
            if r["tool"] == "get_service_status":
                try:
                    s_res = json.loads(r["result"] or "{}")
                    s_name = s_res.get("service")
                    if s_name and s_name not in service_statuses:
                        service_statuses[s_name] = {
                            "output": s_res.get("output", ""),
                            "system": s_res.get("system", ""),
                            "updated_at": r["updated_at"],
                        }
                except Exception:
                    pass

        # Telemetry trend history (up to 25 historical points for sparklines)
        res_history_rows = conn.execute(
            "SELECT result, updated_at FROM tasks WHERE device_id=? AND tool='get_resource_usage' AND status='completed' ORDER BY updated_at ASC LIMIT 25",
            (device_id,),
        ).fetchall()
        res_history = []
        for rhr in res_history_rows:
            try:
                rp = json.loads(rhr["result"] or "{}")
                if "cpu_percent" in rp and rp["cpu_percent"] is not None:
                    res_history.append(
                        {
                            "cpu_percent": rp.get("cpu_percent"),
                            "memory_percent": rp.get("memory_percent"),
                            "updated_at": rhr["updated_at"],
                        }
                    )
            except Exception:
                pass

        return {
            "device": device_data,
            "health": {
                "is_online": is_online,
                "status": "revoked" if row["revoked"] else ("online" if is_online else "offline"),
                "latency_seconds": round(latency, 1) if latency is not None else None,
                "last_seen_ts": last_seen,
            },
            "system_info": {
                **sys_info,
                "updated_at": sys_entry.get("updated_at"),
            },
            "resource_usage": {
                **res_usage,
                "updated_at": res_entry.get("updated_at"),
            },
            "resource_history": res_history,
            "processes": processes,
            "processes_updated_at": proc_entry.get("updated_at"),
            "docker": docker_data,
            "docker_updated_at": docker_entry.get("updated_at"),
            "network": net_data,
            "network_updated_at": net_entry.get("updated_at"),
            "desktop": {
                "status": desktop_entry.get("data"),
                "screenshot": screenshot_entry.get("data"),
                "screenshot_updated_at": screenshot_entry.get("updated_at"),
                "windows": windows_list,
                "windows_updated_at": windows_entry.get("updated_at"),
            },
            "apps": {
                "allowed_apps": list(eff_policy.get("allowed_apps", [])),
                "installed_packages": packages_installed,
                "allowed_services": list(eff_policy.get("allowed_services", [])),
                "service_statuses": service_statuses,
            },
            "recent_tasks": recent_tasks,
            "pending_approvals": pending_approvals,
        }

    @router.post("/api/devices/{device_id}/refresh")
    def refresh_device_telemetry(device_id: str, body: DeviceRefreshIn | None = None, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("operator", x_admin_token, x_session_token)
        if plane_state.is_emergency_stop(conn, app.state):
            raise HTTPException(status_code=403, detail="Emergency stop is active; cannot queue tasks.")
        row = conn.execute("SELECT * FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found or revoked.")
        _require_row_tenant(row, x_admin_token, x_session_token)

        tools_to_run = (body.tools if body and body.tools else None) or ["get_system_info", "get_resource_usage", "get_network_info"]
        eff_policy = get_effective_device_policy(row)
        queued = []
        for tool_name in tools_to_run:
            tier = TOOL_TIERS.get(tool_name)
            # Only allow Tier 1 or Tier 2 safe read-only diagnostics to be automatically dispatched
            if tier is None or tier.value > 2:
                continue
            if tool_name in eff_policy.get("denied_tools", []):
                continue
            if eff_policy.get("allowed_tools") is not None and tool_name not in eff_policy["allowed_tools"]:
                continue

            created = task_service_mod.create_task(
                conn,
                actor=actor,
                role="operator",
                tenant=row["tenant"],
                device_id=device_id,
                tool=tool_name,
                args={},
                human_initiated=False,
                device_row=row,
                get_effective_device_policy=get_effective_device_policy,
                get_current_policy=get_current_policy,
                notify=None,
                audit_fn=audit,
            )
            queued.append({"task_id": created["task_id"], "tool": tool_name, "status": created["status"]})

        conn.commit()
        if queued:
            notify_tasks()
        audit(actor, device_id, "refresh_device_telemetry", {"tools": tools_to_run, "queued": [q["tool"] for q in queued]}, "allowed", tenant=row["tenant"])
        return {"device_id": device_id, "queued_tasks": queued}

    @router.get("/api/devices/{device_id}/telemetry")
    def get_device_telemetry(
        device_id: str,
        limit: int = 50,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        limit = min(200, max(1, limit))
        _dev = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if _dev is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(_dev, x_admin_token, x_session_token)
        rows = conn.execute(
            "SELECT cpu_percent, memory_percent, disk_percent, recorded_at FROM telemetry WHERE device_id=? ORDER BY recorded_at DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
        items = [dict(r) for r in reversed(rows)]
        return {"device_id": device_id, "telemetry": items}

    @router.get("/api/fleet/telemetry")
    def get_fleet_telemetry(
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        now_ts = dbmod.now()
        dev_rows = conn.execute("SELECT id, name, os, status, last_seen FROM devices WHERE tenant=? AND revoked=0", (tenant,)).fetchall()
        online_count = 0
        total_devices = len(dev_rows)
        device_summaries = []
        cpu_vals, mem_vals, disk_vals = [], [], []

        dev_ids = [d["id"] for d in dev_rows]
        latest_tel_by_dev: dict[str, dict] = {}
        live_meta_by_dev: dict[str, dict] = {}
        if dev_ids:
            placeholders = ",".join("?" for _ in dev_ids)
            try:
                tel_rows = conn.execute(
                    "SELECT device_id, cpu_percent, memory_percent, disk_percent, recorded_at FROM ("
                    "  SELECT device_id, cpu_percent, memory_percent, disk_percent, recorded_at, "
                    "         ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY recorded_at DESC) AS rn "
                    f"  FROM telemetry WHERE device_id IN ({placeholders})"
                    ") sub WHERE rn = 1",
                    tuple(dev_ids),
                ).fetchall()
                for tr in tel_rows:
                    td = dict(tr)
                    did = td.pop("device_id")
                    latest_tel_by_dev[did] = td
            except Exception:
                for did in dev_ids:
                    tr = conn.execute(
                        "SELECT cpu_percent, memory_percent, disk_percent, recorded_at FROM telemetry WHERE device_id=? ORDER BY recorded_at DESC LIMIT 1",
                        (did,),
                    ).fetchone()
                    if tr:
                        latest_tel_by_dev[did] = dict(tr)
            live_meta_by_dev = _get_devices_live_meta(dev_ids)

        for d in dev_rows:
            latency = (now_ts - (d["last_seen"] or 0)) if d["last_seen"] else 9999
            is_online = (d["status"] == "online") and (latency < 90)
            if is_online:
                online_count += 1
            tel_data = dict(latest_tel_by_dev[d["id"]]) if d["id"] in latest_tel_by_dev else None
            live_meta = live_meta_by_dev.get(d["id"]) or {}
            if tel_data:
                for k in ("memory_total_gb", "disk_total_gb", "uptime_hours"):
                    if live_meta.get(k) is not None:
                        tel_data[k] = live_meta[k]
                if tel_data.get("cpu_percent") is not None:
                    cpu_vals.append(tel_data["cpu_percent"])
                if tel_data.get("memory_percent") is not None:
                    mem_vals.append(tel_data["memory_percent"])
                if tel_data.get("disk_percent") is not None:
                    disk_vals.append(tel_data["disk_percent"])

            device_summaries.append(
                {
                    "id": d["id"],
                    "name": d["name"],
                    "os": d["os"],
                    "is_online": is_online,
                    "last_seen": d["last_seen"],
                    "telemetry": tel_data,
                }
            )

        avg_cpu = round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None
        avg_mem = round(sum(mem_vals) / len(mem_vals), 1) if mem_vals else None
        avg_disk = round(sum(disk_vals) / len(disk_vals), 1) if disk_vals else None
        trig_row = conn.execute("SELECT COUNT(*) AS c FROM triggers WHERE enabled=1 AND tenant=?", (tenant,)).fetchone()
        sched_row = conn.execute("SELECT COUNT(*) AS c FROM schedules WHERE enabled=1 AND tenant=?", (tenant,)).fetchone()

        return {
            "total_devices": total_devices,
            "online_devices": online_count,
            "avg_cpu_percent": avg_cpu,
            "avg_memory_percent": avg_mem,
            "avg_disk_percent": avg_disk,
            "active_triggers": trig_row["c"] if trig_row else 0,
            "active_schedules": sched_row["c"] if sched_row else 0,
            "devices": device_summaries,
        }

    @router.get("/api/fleet/health")
    def fleet_health_summary(
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        summary = fleet_healthmod.summarize_fleet_health(conn, tenant=tenant)
        audit(actor, "", "fleet_health_view", {"tenant": tenant}, "allowed", tenant=tenant)
        return summary

    @router.get("/api/fleet/desktop-capabilities")
    def fleet_desktop_capabilities(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token)
        payload = desktop_capabilitiesmod.build_capability_matrix()
        from .. import durable_ratelimit as drl

        payload["rate_limit_backend"] = drl.backend_name(conn)
        payload["native_encode"] = __import__("ai_bridge.native_encode", fromlist=["describe_native_encode"]).describe_native_encode()
        audit(actor, "", "fleet_desktop_capabilities_view", {}, "allowed")
        return payload

    @router.get("/api/fleet/version")
    def get_fleet_version(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        import subprocess

        git_commit = "unknown"
        git_branch = "main"
        try:
            c_proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
            if c_proc.returncode == 0:
                git_commit = c_proc.stdout.strip()
            b_proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=5)
            if b_proc.returncode == 0:
                git_branch = b_proc.stdout.strip()
        except Exception:
            pass

        try:
            import importlib.metadata

            cp_version = importlib.metadata.version("ai-bridge")
        except Exception:
            cp_version = "0.3.0"

        return {
            "control_plane_version": cp_version,
            "git_commit": git_commit,
            "git_branch": git_branch,
            "system_time": time.time(),
        }

    @router.post("/api/devices/{device_id}/update")
    def update_device_agent(
        device_id: str,
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
        if device_id == "local":
            try:
                res = _execute_workflow_step_sync("update_fleet_agent", {}, "local", "default", f"manual_update:{actor}")
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=f"Local agent update refused: {exc}") from exc
            audit(actor, "local", "update_device_agent", {"result": res}, "executed")
            return {"device_id": "local", "status": "updated", "result": res}

        dev_row = conn.execute("SELECT * FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
        if not dev_row:
            raise HTTPException(status_code=404, detail="Device not found.")
        _require_row_tenant(dev_row, x_admin_token, x_session_token)
        created = task_service_mod.create_task(
            conn,
            actor=actor,
            role="admin",
            tenant=_row_tenant(dev_row),
            device_id=device_id,
            tool="update_fleet_agent",
            args={},
            human_initiated=False,
            device_row=dev_row,
            get_effective_device_policy=get_effective_device_policy,
            get_current_policy=get_current_policy,
            notify=notify_tasks,
            audit_fn=audit,
        )
        audit(actor, device_id, "update_device_agent", {"task_id": created["task_id"], "status": created["status"]}, created["status"], tenant=dev_row["tenant"])
        return {"device_id": device_id, "status": created["status"], "task_id": created["task_id"]}

    @router.post("/api/devices/{device_id}/mtls-fingerprint")
    def set_device_mtls_fingerprint(
        device_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        from .. import device_mtls

        fp = device_mtls.normalize_fingerprint(str(body.get("fingerprint") or body.get("mtls_fingerprint") or ""))
        if not fp:
            raise HTTPException(status_code=400, detail="fingerprint required")
        row = conn.execute("SELECT id FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found.")
        device_mtls.set_device_fingerprint(conn, device_id, fp)
        audit(actor, device_id, "set_mtls_fingerprint", {"fingerprint": fp[:16] + "..."}, "allowed")
        return {"ok": True, "device_id": device_id, "mtls_fingerprint": fp}

    @router.put("/api/devices/{device_id}/ring")
    def set_device_update_ring(
        device_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        from .. import rollouts as rolloutsmod

        res = rolloutsmod.set_device_ring(conn, device_id, str(body.get("ring") or body.get("update_ring") or ""))
        audit(actor, device_id, "set_device_ring", res, "allowed")
        return res

    @router.get("/api/fleet/rollouts")
    def list_fleet_rollouts(
        tenant: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(tenant, x_admin_token, x_session_token)
        from .. import rollouts as rolloutsmod

        items = rolloutsmod.list_rollouts(conn, tenant=eff_tenant)
        return {"rollouts": items, "count": len(items)}

    @router.post("/api/fleet/rollouts")
    def create_fleet_rollout(
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(body.get("tenant") if isinstance(body, dict) else None, x_admin_token, x_session_token)
        from .. import rollouts as rolloutsmod

        rollout = rolloutsmod.create_rollout(
            conn,
            name=str(body.get("name") or ""),
            tenant=eff_tenant,
            actor=actor,
            target_version=str(body.get("target_version") or ""),
            target_commit=str(body.get("target_commit") or ""),
            manifest_url=str(body.get("manifest_url") or ""),
            rings=body.get("rings") if isinstance(body.get("rings"), list) else None,
            max_failure_rate=float(body.get("max_failure_rate", 0.25)),
            verify_only=bool(body.get("verify_only", False)),
            require_approval=bool(body.get("require_approval", False)),
            get_effective_device_policy=get_effective_device_policy,
            get_current_policy=get_current_policy,
            notify=notify_tasks,
            audit_fn=audit,
        )
        audit(
            actor,
            "",
            "create_rollout",
            {
                "rollout_id": rollout["id"],
                "target_version": rollout["target_version"],
                "verify_only": bool(body.get("verify_only", False)),
                "require_approval": bool(body.get("require_approval", False)),
            },
            "allowed",
            tenant=eff_tenant,
        )
        return rollout

    @router.post("/api/fleet/rollouts/{rollout_id}/advance")
    def advance_fleet_rollout(
        rollout_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(None, x_admin_token, x_session_token)
        from .. import rollouts as rolloutsmod

        res = rolloutsmod.advance_rollout(
            conn,
            rollout_id=rollout_id,
            tenant=eff_tenant,
            actor=actor,
            confirm_promote=bool(body.get("confirm_promote", False)) if isinstance(body, dict) else False,
            get_effective_device_policy=get_effective_device_policy,
            get_current_policy=get_current_policy,
            notify=notify_tasks,
            audit_fn=audit,
        )
        audit(actor, "", "advance_rollout", {"rollout_id": rollout_id, "status": res["status"], "current_ring": res["current_ring"]}, "allowed", tenant=eff_tenant)
        return res

    @router.post("/api/fleet/rollouts/{rollout_id}/pause")
    def pause_fleet_rollout(
        rollout_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(None, x_admin_token, x_session_token)
        from .. import rollouts as rolloutsmod

        res = rolloutsmod.pause_rollout(conn, rollout_id=rollout_id, tenant=eff_tenant)
        audit(actor, "", "pause_rollout", {"rollout_id": rollout_id}, "allowed", tenant=eff_tenant)
        return res

    @router.post("/api/fleet/rollouts/{rollout_id}/resume")
    def resume_fleet_rollout(
        rollout_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(None, x_admin_token, x_session_token)
        from .. import rollouts as rolloutsmod

        res = rolloutsmod.resume_rollout(conn, rollout_id=rollout_id, tenant=eff_tenant)
        audit(actor, "", "resume_rollout", {"rollout_id": rollout_id}, "allowed", tenant=eff_tenant)
        return res

    @router.post("/api/fleet/rollouts/{rollout_id}/rollback")
    def rollback_fleet_rollout(
        rollout_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        eff_tenant = resolve_request_tenant(None, x_admin_token, x_session_token)
        from .. import rollouts as rolloutsmod

        res = rolloutsmod.rollback_rollout(
            conn,
            rollout_id=rollout_id,
            tenant=eff_tenant,
            actor=actor,
            get_effective_device_policy=get_effective_device_policy,
            get_current_policy=get_current_policy,
            notify=notify_tasks,
            audit_fn=audit,
        )
        audit(actor, "", "rollback_rollout", {"rollout_id": rollout_id}, "allowed", tenant=eff_tenant)
        return res

    return router

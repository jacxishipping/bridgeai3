"""admin routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import StreamingResponse

from ...policy import TOOL_TIERS
from ...security import redact
from .. import agent_channel, plane_state
from .. import alerts as alertsmod
from .. import db as dbmod
from ..app import AlertConfigIn, OAuthClientUpdateIn, PolicyIn, WebhookIn, _valid_oauth_redirect_uri, _validate_webhook_url, send_webhook_notification
from ._context import ControlContext


def create_admin_router(ctx: ControlContext) -> APIRouter:
    """Build the admin APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    _auth_context_full = ctx._auth_context_full
    _load_alert_settings = ctx._load_alert_settings
    _record_alert_delivery = ctx._record_alert_delivery
    app = ctx.app
    audit = ctx.audit
    backup_dir = ctx.backup_dir
    conn = ctx.conn
    db_path = ctx.db_path
    get_current_policy = ctx.get_current_policy
    get_public_base_url = ctx.get_public_base_url
    get_webhook_url = ctx.get_webhook_url
    notify_tasks = ctx.notify_tasks
    require_admin = ctx.require_admin
    require_role = ctx.require_role
    session_tenant_for_request = ctx.session_tenant_for_request
    token = ctx.token

    @router.post("/api/emergency-stop")
    def emergency_stop(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        app.state.emergency_stop = True
        plane_state.set_emergency_stop(conn, True)
        notify_tasks()
        agent_channel.notify_emergency_stop(True)
        audit(actor, "", "emergency_stop", {}, "allowed")
        alertsmod.dispatch_alert("emergency_stop", "critical", "EMERGENCY STOP ACTIVATED", {"actor": actor})
        return {"emergency_stop": True}

    @router.post("/api/emergency-stop/clear")
    def emergency_clear(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        app.state.emergency_stop = False
        plane_state.set_emergency_stop(conn, False)
        notify_tasks()
        agent_channel.notify_emergency_stop(False)
        audit(actor, "", "emergency_stop_clear", {}, "allowed")
        return {"emergency_stop": False}

    @router.get("/api/settings/webhook")
    def get_webhook_setting(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_admin(x_admin_token, x_session_token)
        url = get_webhook_url()
        return {"webhook_url": url, "configured": bool(url)}

    @router.post("/api/settings/webhook")
    def set_webhook_setting(body: WebhookIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        url = (body.webhook_url or "").strip()
        if url:
            _validate_webhook_url(url)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('webhook_url', ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (url,),
        )
        conn.commit()
        audit(actor, "", "set_webhook", {"configured": bool(url)}, "allowed")
        return {"webhook_url": url, "configured": bool(url)}

    @router.post("/api/settings/webhook/test")
    def test_webhook_setting(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("admin", x_admin_token, x_session_token)
        url = get_webhook_url()
        if not url:
            raise HTTPException(status_code=400, detail="No webhook URL configured.")
        _validate_webhook_url(url)
        res = send_webhook_notification(
            url,
            {
                "event": "webhook_test",
                "approval_id": "test-approval",
                "task_id": "test-task",
                "device_id": "test-device",
                "device_name": "test-device",
                "tool": "test_webhook",
                "tier": 3,
                "args": {"test": True},
            },
            admin_token=token,
            public_base_url=get_public_base_url(),
        )
        return res

    @router.post("/api/policies")
    def create_policy(body: PolicyIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT MAX(version) AS v FROM policies").fetchone()
        version = (row["v"] or 0) + 1
        policy = body.model_dump()
        conn.execute("INSERT INTO policies (version, policy_json, created_by, created_at, note) VALUES (?,?,?,?,?)", (version, json.dumps(policy), actor, dbmod.now(), body.note))
        conn.commit()
        audit(actor, "", "create_policy", {"version": version}, "allowed")
        return {"version": version, "policy": policy}

    @router.get("/api/policies")
    def get_policy(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        row = conn.execute("SELECT version, policy_json FROM policies ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            return {"version": 0, "policy": get_current_policy(), "note": "No versioned policy yet; environment defaults apply."}
        return {"version": row["version"], "policy": json.loads(row["policy_json"])}

    @router.get("/api/policies/history")
    def policy_history(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        rows = conn.execute("SELECT version, policy_json, created_by, created_at, note FROM policies ORDER BY version DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    @router.post("/api/policies/simulate")
    def simulate_policy(body: PolicyIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("admin", x_admin_token, x_session_token)
        current = get_current_policy()
        scenarios = []
        for tool, tier in sorted(TOOL_TIERS.items()):
            requires_approval = tier.value >= 4 or (tier.value == 3 and not body.allow_tier3)
            scenarios.append({"tool": tool, "tier": tier.value, "allowed_unattended": not requires_approval, "requires_approval": requires_approval})
        return {"current_policy": current, "proposed_policy": body.model_dump(), "tools": scenarios}

    @router.post("/api/policy/simulate")
    def simulate_policy_call(body: dict = Body(default={}), x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        """FR-01: dry-run policy simulation — NEVER executes anything.

        POST ``{"tool", "policy"?, "approval_granted"?, "is_admin"?,
        "device_revoked"?, "device_id"?, "actor"?}`` and get back the
        ``decide()`` outcome (allowed / needs_approval / reason / rule)
        for that hypothetical call. With no ``"policy"`` in the body the
        live plane policy (``get_current_policy()``) is simulated
        against; with one, only its whitelisted fields are honoured —
        ``unrestricted`` / ``unrestricted_granted_by`` are deliberately
        ignored, so a caller-supplied body can never make the
        simulation report the granted-unrestricted bypass. ``"calls"``
        (a list of the same per-call objects) runs a batch instead.
        Operator or admin. Read-only: no task, approval, or policy row
        is created, and nothing is executed.
        """
        from ...policy import Policy
        from ...policy_simulation import simulate_batch, simulate_policy_decision

        actor = require_role("operator", x_admin_token, x_session_token)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Simulation body must be a JSON object.")

        def _policy_from_dict(raw: object) -> Policy:
            if not isinstance(raw, dict):
                return Policy()
            kwargs: dict = {}
            for key in ("allow_writes", "allow_tier3", "desktop_enabled"):
                if raw.get(key) is not None:
                    kwargs[key] = bool(raw[key])
            if raw.get("max_tier_unattended") is not None:
                try:
                    kwargs["max_tier_unattended"] = max(0, min(4, int(raw["max_tier_unattended"])))
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="max_tier_unattended must be an integer 0-4.") from None
            for key in ("allowed_services", "allowed_apps", "allowed_test_commands", "denied_tools"):
                if raw.get(key) is not None:
                    if not isinstance(raw[key], (list, tuple, set)):
                        raise HTTPException(status_code=400, detail=f"{key} must be a list of strings.") from None
                    kwargs[key] = frozenset(str(v) for v in raw[key])
            if raw.get("allowed_tools") is not None:
                if not isinstance(raw["allowed_tools"], (list, tuple, set)):
                    raise HTTPException(status_code=400, detail="allowed_tools must be a list of strings.") from None
                kwargs["allowed_tools"] = frozenset(str(v) for v in raw["allowed_tools"])
            # "unrestricted" / "unrestricted_granted_by" intentionally
            # never read from the body (see docstring).
            return Policy(**kwargs)

        policy = _policy_from_dict(body.get("policy")) if body.get("policy") is not None else _policy_from_dict(get_current_policy())
        _actor2, role, _tenant, _is_legacy = _auth_context_full(x_admin_token, x_session_token)
        is_admin = bool(body["is_admin"]) if body.get("is_admin") is not None else (role == "admin")
        calls = body.get("calls")
        if calls is not None:
            if not isinstance(calls, list) or not calls:
                raise HTTPException(status_code=400, detail="calls must be a non-empty list of {tool, ...} objects.")
            for entry in calls:
                if not isinstance(entry, dict) or not str(entry.get("tool") or "").strip():
                    raise HTTPException(status_code=400, detail="Each simulation entry needs a tool name.")
            return {
                "simulated": True,
                "executed": False,
                "actor": actor,
                "results": simulate_batch(calls, policy),
            }
        tool = str(body.get("tool") or "").strip()
        if not tool:
            raise HTTPException(status_code=400, detail="tool is required.")
        return simulate_policy_decision(
            tool,
            policy,
            actor=str(body.get("actor") or actor),
            device_id=str(body["device_id"]) if body.get("device_id") is not None else None,
            approval_granted=bool(body.get("approval_granted", False)),
            is_admin=is_admin,
            device_revoked=bool(body.get("device_revoked", False)),
        )

    @router.put("/api/policies/{version}")
    def update_policy_version(version: int, body: PolicyIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        row = conn.execute("SELECT * FROM policies WHERE version=?", (version,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Policy version not found.")
        policy = body.model_dump()
        conn.execute(
            "UPDATE policies SET policy_json=?, note=? WHERE version=?",
            (json.dumps(policy), body.note, version),
        )
        conn.commit()
        audit(actor, "", "update_policy", {"version": version}, "allowed")
        return {"version": version, "policy": policy, "note": body.note}

    @router.delete("/api/policies/{version}")
    def delete_policy_version(version: int, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_role("admin", x_admin_token, x_session_token)
        cur = conn.execute("DELETE FROM policies WHERE version=?", (version,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Policy version not found.")
        audit(actor, "", "delete_policy", {"version": version}, "allowed")
        return {"version": version, "deleted": True}

    @router.get("/api/connections")
    def connections(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        """Live connection status: MCP service, OAuth gateway, and the
        ChatGPT OAuth clients/tokens registered with the gateway.
        Token hashes are never returned, only counts and client metadata."""
        import socket
        import sqlite3

        require_role("viewer", x_admin_token, x_session_token)

        def tcp_up(host: str, port: int) -> bool:
            for h in (host, "127.0.0.1"):
                try:
                    sock = socket.create_connection((h, port), timeout=2)
                    sock.close()
                    return True
                except OSError:
                    continue
            return False

        clients: list[dict] = []
        active_access = 0
        active_refresh = 0
        total_tokens = 0
        last_token_at = None
        db_path = os.environ.get("AI_BRIDGE_OAUTH_DB", "/etc/ai-bridge-oauth/oauth.db")
        try:
            odb = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            odb.row_factory = sqlite3.Row
            for row in odb.execute("SELECT client_id, client_name, redirect_uris, created_at FROM clients ORDER BY created_at DESC"):
                clients.append(
                    {
                        "client_id": row["client_id"],
                        "client_id_prefix": row["client_id"][:16],
                        "client_name": row["client_name"],
                        "redirect_uris": row["redirect_uris"],
                        "created_at": row["created_at"],
                    }
                )
            now = dbmod.now()
            for row in odb.execute("SELECT kind, created_at, expires_at, revoked FROM tokens"):
                total_tokens += 1
                if row["revoked"] or row["expires_at"] < now:
                    continue
                if row["kind"] == "access":
                    active_access += 1
                elif row["kind"] == "refresh":
                    active_refresh += 1
                if last_token_at is None or row["created_at"] > last_token_at:
                    last_token_at = row["created_at"]
            odb.close()
        except Exception:
            pass
        mcp_host = os.environ.get("AI_BRIDGE_MCP_HOST", "127.0.0.1")
        oauth_host = os.environ.get("AI_BRIDGE_OAUTH_HOST", "127.0.0.1")
        return {
            "mcp": {"up": tcp_up(mcp_host, 8001), "port": 8001},
            "oauth_gateway": {"up": tcp_up(oauth_host, 8002), "port": 8002},
            "chatgpt": {
                "clients": clients,
                "active_access_tokens": active_access,
                "active_refresh_tokens": active_refresh,
                "total_tokens": total_tokens,
                "last_token_at": last_token_at,
            },
        }

    @router.put("/api/connections/clients/{client_id}")
    def update_oauth_client(client_id: str, body: OAuthClientUpdateIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        import sqlite3

        actor = require_role("admin", x_admin_token, x_session_token)
        oauth_db = os.environ.get("AI_BRIDGE_OAUTH_DB", "/etc/ai-bridge-oauth/oauth.db")
        try:
            odb = sqlite3.connect(oauth_db, timeout=10.0)
            odb.row_factory = sqlite3.Row
            row = odb.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if row is None:
                odb.close()
                raise HTTPException(status_code=404, detail="OAuth client not found.")
            cname = body.client_name.strip() if body.client_name is not None and body.client_name.strip() else row["client_name"]
            ruris = body.redirect_uris.strip() if body.redirect_uris is not None and body.redirect_uris.strip() else row["redirect_uris"]
            if body.redirect_uris is not None:
                uris = [u.strip() for u in ruris.splitlines() if u.strip()]
                if not uris or not all(_valid_oauth_redirect_uri(u) for u in uris):
                    odb.close()
                    raise HTTPException(status_code=400, detail="Invalid redirect_uris: all URIs must be https:// or http://localhost/127.0.0.1.")
                ruris = "\n".join(uris)
            odb.execute("UPDATE clients SET client_name=?, redirect_uris=? WHERE client_id=?", (cname, ruris, client_id))
            odb.commit()
            odb.close()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Failed to update OAuth client: {exc}") from exc
        audit(actor, "", "update_oauth_client", {"client_id": client_id[:16], "client_name": cname}, "allowed")
        return {"client_id": client_id, "client_name": cname, "redirect_uris": ruris}

    @router.delete("/api/connections/clients/{client_id}")
    def delete_oauth_client(client_id: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        import sqlite3

        actor = require_role("admin", x_admin_token, x_session_token)
        oauth_db = os.environ.get("AI_BRIDGE_OAUTH_DB", "/etc/ai-bridge-oauth/oauth.db")
        try:
            odb = sqlite3.connect(oauth_db, timeout=10.0)
            odb.execute("DELETE FROM tokens WHERE client_id=?", (client_id,))
            cur = odb.execute("DELETE FROM clients WHERE client_id=?", (client_id,))
            odb.commit()
            odb.close()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="OAuth client not found.")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Failed to delete OAuth client: {exc}") from exc
        audit(actor, "", "delete_oauth_client", {"client_id": client_id[:16]}, "allowed")
        return {"client_id": client_id, "deleted": True}

    @router.get("/api/logs")
    def service_logs(service: str = "control", lines: int = 100, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        """Recent log tail for one service. Admin/operator viewers only in
        practice (viewer role + dashboard auth); lines are capped."""
        import collections
        import shutil
        import subprocess

        require_role("admin", x_admin_token, x_session_token)
        lines = max(10, min(int(lines), 200))
        units = {"control": "ai-bridge-control", "mcp": "ai-bridge-mcp", "oauth": "ai-bridge-oauth"}
        if service in units:
            journal = shutil.which("journalctl")
            if journal is not None:
                proc = subprocess.run([journal, "-u", units[service], "-n", str(lines), "--no-pager"], capture_output=True, text=True, timeout=15)
                if proc.stdout and proc.stdout.strip():
                    return {"service": service, "lines": [redact(line) for line in proc.stdout.splitlines()[-lines:]]}
            # Fallback to structured audit log stream in containerized environments without journald
            rows = conn.execute("SELECT ts, actor, device_id, tool, decision, args_json FROM audit ORDER BY id DESC LIMIT ?", (lines,)).fetchall()
            formatted = [
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(r['ts'] or 0))} [{service.upper()}] actor={r['actor']} device={r['device_id'] or 'gateway'} tool={r['tool']} "
                f"decision={r['decision']} args={redact(str(r['args_json'] or '{}'))[:160]}"
                for r in reversed(rows)
            ]
            return {"service": service, "lines": formatted or [f"[{service}] Service online; no audit events recorded yet."]}
        if service == "nginx":
            try:
                with open("/var/log/nginx/access.log", errors="replace") as handle:
                    tail = collections.deque(handle, maxlen=2000)
                wanted = [redact(line.rstrip("\n")) for line in tail if "/mcp" in line or "/api/" in line]
                return {"service": service, "lines": wanted[-lines:]}
            except OSError as exc:
                return {"service": service, "lines": [f"nginx access log unavailable: {exc}"]}
        raise HTTPException(status_code=400, detail="Unknown log service.")

    @router.get("/api/events/stream")
    async def stream_control_events(
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)

        def _snapshot_sig() -> str:
            t_row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), 0) AS m FROM tasks").fetchone()
            a_row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(created_at), 0) AS m, COALESCE(MAX(decided_at), 0) AS d FROM approvals").fetchone()
            d_row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(last_seen), 0) AS m FROM devices").fetchone()
            return f"{t_row['n']}:{t_row['m']}|{a_row['n']}:{a_row['m']}:{a_row['d']}|{d_row['n']}:{d_row['m']}"

        async def _gen():
            last_sig = _snapshot_sig()
            yield f"data: {json.dumps({'type': 'connected', 'ts': dbmod.now()})}\n\n"
            while True:
                await asyncio.sleep(1.5)
                try:
                    sig = _snapshot_sig()
                    if sig != last_sig:
                        last_sig = sig
                        yield f"data: {json.dumps({'type': 'sync', 'ts': dbmod.now()})}\n\n"
                except Exception:
                    break

        return StreamingResponse(_gen(), media_type="text/event-stream")

    @router.get("/api/alerts/config")
    def get_alerts_config_route(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("admin", x_admin_token, x_session_token)
        cfg = _load_alert_settings()
        return {
            "slack_webhook": cfg["slack_webhook"],
            "discord_webhook": cfg["discord_webhook"],
            "telegram_chat_id": cfg["telegram_chat_id"],
            "telegram_configured": bool(cfg["telegram_token"] and cfg["telegram_chat_id"]),
            "generic_webhook": cfg["generic_webhook"],
        }

    @router.put("/api/alerts/config")
    def update_alerts_config_route(body: AlertConfigIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        existing = _load_alert_settings()
        payload = {
            "slack_webhook": body.slack_webhook.strip(),
            "discord_webhook": body.discord_webhook.strip(),
            "telegram_token": body.telegram_token.strip() or existing.get("telegram_token", ""),
            "telegram_chat_id": body.telegram_chat_id.strip(),
            "generic_webhook": body.generic_webhook.strip(),
        }
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('alert_webhooks', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(payload),),
        )
        conn.commit()
        audit(actor, "", "update_alert_config", {"channels": [k for k, v in payload.items() if v]}, "allowed")
        return {"saved": True}

    @router.post("/api/alerts/test")
    def test_alerts_route(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        cfg = _load_alert_settings()
        results = alertsmod.dispatch_alert(
            "test_alert",
            "info",
            "AI Bridge Test Alert",
            {"actor": actor, "source": "dashboard"},
            async_dispatch=False,
            config_override=cfg,
        )
        for ch, ok in results.items():
            _record_alert_delivery(ch, "test_alert", "info", "AI Bridge Test Alert", bool(ok), f"Triggered by {actor}")
        if not results:
            _record_alert_delivery("none", "test_alert", "info", "AI Bridge Test Alert", False, "No alert channels configured")
        return {"results": results}

    @router.get("/api/alerts/deliveries")
    def list_alert_deliveries(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        rows = conn.execute("SELECT * FROM alert_deliveries ORDER BY created_at DESC LIMIT 50").fetchall()
        return [dict(r) for r in rows]

    @router.get("/api/admin/backups")
    def list_backups(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("admin", x_admin_token, x_session_token)
        backup_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for p in sorted(backup_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix in (".db", ".json"):
                st = p.stat()
                items.append({"filename": p.name, "size_bytes": st.st_size, "created_at": st.st_mtime})
        return {"items": items, "backend": getattr(conn, "dialect", "sqlite")}

    @router.post("/api/admin/backups")
    def create_backup(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        if getattr(conn, "dialect", "sqlite") != "postgres" and db_path and Path(str(db_path)).exists():
            from ai_bridge.control.backup import verified_backup

            dest = backup_dir / f"aibridge-backup-{stamp}.db"
            verified_backup(Path(str(db_path)), dest)
        else:
            dest = backup_dir / f"aibridge-snapshot-{stamp}.json"
            snapshot = {}
            for tbl in ("devices", "tasks", "approvals", "policies", "workflows", "workflow_templates", "schedules", "triggers", "custom_agents", "mcp_servers"):
                try:
                    snapshot[tbl] = [dict(r) for r in conn.execute(f"SELECT * FROM {tbl}").fetchall()]
                except Exception:
                    snapshot[tbl] = []
            dest.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
        audit(actor, "", "create_backup", {"filename": dest.name, "size_bytes": dest.stat().st_size}, "allowed")
        return {"filename": dest.name, "size_bytes": dest.stat().st_size, "created_at": dest.stat().st_mtime}

    @router.get("/api/admin/backups/{filename}")
    def download_backup(filename: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("admin", x_admin_token, x_session_token)
        safe_name = Path(filename).name
        target = backup_dir / safe_name
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Backup file not found.")
        from fastapi.responses import FileResponse

        return FileResponse(target, filename=safe_name)

    @router.post("/api/admin/backups/{filename}/restore")
    def restore_backup(filename: str, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        safe_name = Path(filename).name
        target = backup_dir / safe_name
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Backup file not found.")

        # Always capture a pre-restore safety snapshot first
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        safety_tables = ("devices", "tasks", "approvals", "policies", "workflows", "workflow_templates", "schedules", "triggers", "custom_agents", "mcp_servers")
        if getattr(conn, "dialect", "sqlite") != "postgres" and db_path and Path(str(db_path)).exists():
            from ai_bridge.control.backup import verified_backup

            safety_dest = backup_dir / f"pre-restore-{stamp}.db"
            verified_backup(Path(str(db_path)), safety_dest)
        else:
            safety_dest = backup_dir / f"pre-restore-{stamp}.json"
            safety_snap = {}
            for tbl in safety_tables:
                try:
                    safety_snap[tbl] = [dict(r) for r in conn.execute(f"SELECT * FROM {tbl}").fetchall()]
                except Exception:
                    safety_snap[tbl] = []
            safety_dest.write_text(json.dumps(safety_snap, indent=2, default=str), encoding="utf-8")

        restored_counts: dict[str, int] = {}
        if target.suffix == ".json":
            try:
                snapshot = json.loads(target.read_text(encoding="utf-8"))
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid JSON backup: {exc}") from exc
            if not isinstance(snapshot, dict):
                raise HTTPException(status_code=400, detail="Backup snapshot must be a JSON object.")
            with conn.transaction():
                for tbl in safety_tables:
                    rows = snapshot.get(tbl)
                    if not isinstance(rows, list):
                        continue
                    conn.execute(f"DELETE FROM {tbl}")
                    inserted = 0
                    for row in rows:
                        if not isinstance(row, dict) or not row:
                            continue
                        cols = [str(k) for k in row.keys()]
                        placeholders = ",".join("?" for _ in cols)
                        col_list = ",".join(cols)
                        conn.execute(
                            f"INSERT INTO {tbl} ({col_list}) VALUES ({placeholders})",
                            tuple(row[c] for c in cols),
                        )
                        inserted += 1
                    restored_counts[tbl] = inserted
        elif target.suffix == ".db":
            if getattr(conn, "dialect", "sqlite") == "postgres" or not db_path:
                raise HTTPException(status_code=400, detail="SQLite .db restore is only supported when running on SQLite.")
            import sqlite3

            src = sqlite3.connect(str(target))
            try:
                with conn._lock:  # type: ignore[attr-defined]
                    src.backup(conn._conn)  # type: ignore[attr-defined]
                conn.commit()
            finally:
                src.close()
            restored_counts["sqlite_db"] = 1
        else:
            raise HTTPException(status_code=400, detail="Unsupported backup format; expected .db or .json.")

        audit(actor, "", "restore_backup", {"filename": safe_name, "safety_backup": safety_dest.name, "counts": restored_counts}, "allowed")
        return {"ok": True, "restored_from": safe_name, "safety_backup": safety_dest.name, "counts": restored_counts}

    @router.get("/api/mcp/tool-policies")
    def get_mcp_tool_policies_route(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        from ai_bridge.control.mcp_oauth import get_mcp_tool_policies

        return {"ok": True, "policies": get_mcp_tool_policies(conn)}

    @router.put("/api/mcp/tool-policies")
    def update_mcp_tool_policies_route(body: dict, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor = require_admin(x_admin_token, x_session_token)
        from ai_bridge.control.mcp_oauth import save_mcp_tool_policies

        raw_policies = body.get("policies") if isinstance(body, dict) and isinstance(body.get("policies"), dict) else (body if isinstance(body, dict) else {})
        saved = save_mcp_tool_policies(conn, raw_policies)
        audit(actor, "", "update_mcp_tool_policies", {"count": len(saved)}, "allowed")
        return {"ok": True, "policies": saved}

    @router.get("/api/tenants")
    def list_tenants(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_role("viewer", x_admin_token, x_session_token)
        from .. import tenancy as tenancymod

        tenant, is_legacy = session_tenant_for_request(x_admin_token, x_session_token)
        if not is_legacy:
            summary = tenancymod.get_tenant_usage(conn, tenant, app_state=app.state)
            return {"tenants": [tenant], "items": [summary]}
        items = tenancymod.list_all_tenants(conn, app_state=app.state)
        return {"tenants": [it["tenant_id"] for it in items], "items": items}

    @router.post("/api/tenants", summary="Create or configure a SaaS tenant with plan tier and resource quotas")
    def create_or_configure_tenant(
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_admin(x_admin_token, x_session_token)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object.")
        from .. import tenancy as tenancymod

        tenant_id = str(body.get("tenant_id") or body.get("id") or body.get("tenant") or "").strip()
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required.")
        cfg = tenancymod.save_tenant_config(
            conn,
            tenant_id,
            name=body.get("name"),
            plan_tier=body.get("plan_tier"),
            status=body.get("status"),
            max_devices=body.get("max_devices"),
            max_concurrent_streams=body.get("max_concurrent_streams"),
            max_daily_tasks=body.get("max_daily_tasks"),
            max_memory_chunks=body.get("max_memory_chunks"),
            rate_limit_rpm=body.get("rate_limit_rpm"),
        )
        audit(actor, "", "configure_tenant", {"tenant_id": tenant_id, "plan_tier": cfg["plan_tier"], "status": cfg["status"]}, "allowed", tenant=tenant_id)
        return tenancymod.get_tenant_usage(conn, tenant_id, app_state=app.state)

    @router.get("/api/tenants/{tenant_id}/usage", summary="Get live SaaS quota utilization and resource consumption for a tenant")
    def get_tenant_usage_route(
        tenant_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        req_tenant, is_legacy = session_tenant_for_request(x_admin_token, x_session_token)
        if not is_legacy and tenant_id != req_tenant:
            raise HTTPException(status_code=403, detail="Not authorized to view usage for another tenant.")
        from .. import tenancy as tenancymod

        return tenancymod.get_tenant_usage(conn, tenant_id, app_state=app.state)

    @router.put("/api/tenants/{tenant_id}/quotas", summary="Update SaaS resource quotas, plan tier, or active/suspended status for a tenant")
    def update_tenant_quotas_route(
        tenant_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_admin(x_admin_token, x_session_token)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object.")
        from .. import tenancy as tenancymod

        cfg = tenancymod.save_tenant_config(
            conn,
            tenant_id,
            name=body.get("name"),
            plan_tier=body.get("plan_tier"),
            status=body.get("status"),
            max_devices=body.get("max_devices"),
            max_concurrent_streams=body.get("max_concurrent_streams"),
            max_daily_tasks=body.get("max_daily_tasks"),
            max_memory_chunks=body.get("max_memory_chunks"),
            rate_limit_rpm=body.get("rate_limit_rpm"),
        )
        audit(actor, "", "update_tenant_quotas", {"tenant_id": tenant_id, "plan_tier": cfg["plan_tier"], "status": cfg["status"]}, "allowed", tenant=tenant_id)
        return tenancymod.get_tenant_usage(conn, tenant_id, app_state=app.state)

    return router

"""agent routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from fastapi import APIRouter, Body, Header, HTTPException, Request, WebSocket, WebSocketDisconnect

from .. import agent_channel, plane_state
from .. import db as dbmod
from .. import supervisor as supervisormod
from ..app import MAX_RESULT_CHARS, DevicePolicyIn, TaskResultIn
from ._context import ControlContext


def create_agent_router(ctx: ControlContext) -> APIRouter:
    """Build the agent APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    _evaluate_device_triggers = ctx._evaluate_device_triggers
    _require_agent_mtls = ctx._require_agent_mtls
    app = ctx.app
    audit = ctx.audit
    conn = ctx.conn
    device_row = ctx.device_row
    device_ttl_days = ctx.device_ttl_days
    get_effective_device_policy = ctx.get_effective_device_policy
    notify_tasks = ctx.notify_tasks
    reject_policy_widening = ctx.reject_policy_widening

    @router.post("/api/agent/heartbeat")
    def heartbeat(
        request: Request,
        body: dict = Body(default={}),
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        row = device_row(x_device_token)
        _require_agent_mtls(request, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        now_ts = dbmod.now()
        hostname = (body.get("hostname") or "").strip() if isinstance(body, dict) else ""
        os_val = (body.get("os") or "").strip() if isinstance(body, dict) else ""
        agent_version = (str(body.get("agent_version") or "").strip()[:64]) if isinstance(body, dict) else ""
        agent_commit = (str(body.get("agent_commit") or "").strip()[:64]) if isinstance(body, dict) else ""
        policy_hash = (str(body.get("policy_hash") or "").strip()[:128]) if isinstance(body, dict) else ""
        poll_state = body.get("poll_state") if isinstance(body, dict) and isinstance(body.get("poll_state"), dict) else None
        spool_stats = body.get("spool_stats") if isinstance(body, dict) and isinstance(body.get("spool_stats"), dict) else None

        set_parts = ["status='online'", "last_seen=?"]
        set_params: list = [now_ts]
        if hostname:
            set_parts.append("hostname=?")
            set_params.append(hostname)
        if os_val:
            set_parts.append("os=?")
            set_params.append(os_val)
        if agent_version:
            set_parts.append("agent_version=?")
            set_params.append(agent_version)
        if agent_commit:
            set_parts.append("agent_commit=?")
            set_params.append(agent_commit)
        if policy_hash:
            set_parts.append("reported_policy_hash=?")
            set_params.append(policy_hash)
        if poll_state is not None:
            set_parts.append("poll_state_json=?")
            set_params.append(json.dumps(poll_state, default=str)[:4000])
        if spool_stats is not None:
            set_parts.append("spool_stats_json=?")
            set_params.append(json.dumps(spool_stats, default=str)[:4000])
        set_params.append(row["id"])
        try:
            conn.execute(f"UPDATE devices SET {', '.join(set_parts)} WHERE id=?", tuple(set_params))
        except Exception:
            if hostname or os_val:
                cur_h = hostname if hostname else (row["hostname"] or "")
                cur_os = os_val if os_val else (row["os"] or "")
                conn.execute("UPDATE devices SET status='online', last_seen=?, hostname=?, os=? WHERE id=?", (now_ts, cur_h, cur_os, row["id"]))
            else:
                conn.execute("UPDATE devices SET status='online', last_seen=? WHERE id=?", (now_ts, row["id"]))
        conn.commit()

        metrics = body.get("metrics") if isinstance(body, dict) else None
        meta_entry = dict(app.state.device_live_meta.get(row["id"]) or {})
        client_ip = (
            (metrics.get("ip_address") if isinstance(metrics, dict) else None)
            or (body.get("ip_address") if isinstance(body, dict) else None)
            or request.headers.get("x-real-ip")
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "")
        )
        if client_ip and not str(client_ip).startswith("127.") and not str(client_ip).startswith("172."):
            meta_entry["ip_address"] = str(client_ip)
        elif client_ip and not meta_entry.get("ip_address"):
            meta_entry["ip_address"] = str(client_ip)

        if metrics and isinstance(metrics, dict):
            cpu_p = metrics.get("cpu_percent")
            mem_p = metrics.get("memory_percent")
            disk_p = metrics.get("disk_percent")
            for k in ("memory_total_gb", "disk_total_gb", "uptime_hours"):
                if isinstance(metrics.get(k), (int, float)):
                    meta_entry[k] = round(float(metrics[k]), 1)
            conn.execute(
                "INSERT INTO telemetry (device_id, tenant, cpu_percent, memory_percent, disk_percent, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (row["id"], row["tenant"], cpu_p, mem_p, disk_p, now_ts),
            )
            conn.commit()
            dbmod.maybe_prune_telemetry(conn, row["id"], keep=500, slack=100)
            _evaluate_device_triggers(row, metrics, now_ts)
        app.state.device_live_meta[row["id"]] = meta_entry

        plane_state.sync_emergency_stop_to_app(conn, app.state)
        return {
            "device_id": row["id"],
            "emergency_stop": plane_state.is_emergency_stop(conn, app.state),
            "token_expires_at": row["token_expires_at"],
        }

    @router.get("/api/agent/whoami")
    def whoami(x_device_token: str | None = Header(default=None), x_client_cert_fingerprint: str | None = Header(default=None), x_ssl_client_fingerprint: str | None = Header(default=None)):
        # Read-only device-token check, for the OAuth gateway's device
        # binding step. Unlike heartbeat it touches no device state, so
        # validating a token never makes a device look freshly seen.
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        return {"device_id": row["id"]}

    @router.websocket("/api/agent/ws")
    async def agent_ws(websocket: WebSocket):
        """Persistent wake/cancel side-channel for outbound agents.

        Auth: ``X-Device-Token`` header only (Wave 4 / F-05-15 follow-up:
        the ``?token=`` query fallback was removed — a token in the URL
        lands in logs and history). HTTP ``/api/agent/tasks/next``
        remains the lease/claim path.
        """
        token = websocket.headers.get("x-device-token")
        try:
            row = device_row(token)
            from .. import device_mtls as _dmtls

            _dmtls.require_device_mtls(
                conn,
                row["id"],
                _dmtls.extract_request_fingerprint(
                    None, x_client_cert_fingerprint=websocket.headers.get("x-client-cert-fingerprint"), x_ssl_client_fingerprint=websocket.headers.get("x-ssl-client-fingerprint")
                ),
            )
        except HTTPException as exc:
            await websocket.close(code=4401, reason=str(exc.detail)[:120])
            return
        await websocket.accept()
        try:
            agent_channel.bind_event_loop(asyncio.get_running_loop())
        except RuntimeError:
            pass
        if not getattr(app.state, "_redis_fanout_started", False):
            try:
                agent_channel.start_redis_fanout(asyncio.get_running_loop())
                app.state._redis_fanout_started = True
            except Exception:
                pass
        device_id = row["id"]
        agent_channel.register(device_id, websocket)
        plane_state.sync_emergency_stop_to_app(conn, app.state)
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "device_id": device_id,
                    "emergency_stop": plane_state.is_emergency_stop(conn, app.state),
                }
            )
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=45.0)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "ping", "ts": time.time()})
                    continue
                except WebSocketDisconnect:
                    break
                if not isinstance(msg, dict):
                    continue
                mtype = str(msg.get("type") or "")
                if mtype in {"pong", "heartbeat"}:
                    conn.execute(
                        "UPDATE devices SET status='online', last_seen=? WHERE id=?",
                        (dbmod.now(), device_id),
                    )
                    conn.commit()
                    await websocket.send_json(
                        {
                            "type": "heartbeat_ok",
                            "emergency_stop": plane_state.is_emergency_stop(conn, app.state),
                        }
                    )
                elif mtype == "ping":
                    await websocket.send_json({"type": "pong", "ts": time.time()})
                else:
                    # DIST-08: progress heartbeats (and any future
                    # agent-initiated message types) are handled by
                    # agent_channel; the reply is sent back when not None.
                    reply = agent_channel.handle_agent_message(conn, device_id, msg)
                    if reply is not None:
                        await websocket.send_json(reply)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            agent_channel.unregister(device_id, websocket)

    @router.get("/api/agent/policy")
    def get_agent_policy(x_device_token: str | None = Header(default=None), x_client_cert_fingerprint: str | None = Header(default=None), x_ssl_client_fingerprint: str | None = Header(default=None)):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        custom_p = {}
        try:
            custom_p = json.loads(row["policy_json"]) if row["policy_json"] else {}
        except Exception:
            custom_p = {}
        eff = get_effective_device_policy(row)
        return {
            "device_id": row["id"],
            "custom_policy": custom_p,
            "effective_policy": eff,
            "has_custom_policy": bool(custom_p),
        }

    @router.put("/api/agent/policy")
    def update_agent_policy(
        body: DevicePolicyIn,
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        policy_data = body.model_dump()
        reject_policy_widening(get_effective_device_policy(row), policy_data)
        conn.execute("UPDATE devices SET policy_json=? WHERE id=?", (json.dumps(policy_data), row["id"]))
        conn.commit()
        audit("device", row["id"], "agent_update_policy", policy_data, "allowed")
        # Re-read: the caller must see the policy that was actually stored.
        eff = get_effective_device_policy(row["id"])
        return {
            "device_id": row["id"],
            "custom_policy": policy_data,
            "effective_policy": eff,
            "saved": True,
        }

    @router.post("/api/agent/rotate")
    def rotate_token(x_device_token: str | None = Header(default=None), x_client_cert_fingerprint: str | None = Header(default=None), x_ssl_client_fingerprint: str | None = Header(default=None)):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        new_token = secrets.token_urlsafe(32)
        issued = dbmod.now()
        expires = issued + device_ttl_days * 86400
        conn.execute("UPDATE devices SET token_hash=?, token_issued_at=?, token_expires_at=? WHERE id=?", (dbmod.hash_token(new_token), issued, expires, row["id"]))
        conn.commit()
        audit("device", row["id"], "rotate_token", {}, "allowed")
        return {"device_id": row["id"], "device_token": new_token, "token_expires_at": expires, "note": "Old token is invalid immediately. Store the new token securely."}

    @router.get("/api/agent/tasks/next")
    async def next_task(
        request: Request,
        wait: float = 0.0,
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        row = device_row(x_device_token)
        from .. import device_mtls

        device_mtls.require_device_mtls(
            conn,
            row["id"],
            device_mtls.extract_request_fingerprint(
                request,
                x_client_cert_fingerprint=x_client_cert_fingerprint,
                x_ssl_client_fingerprint=x_ssl_client_fingerprint,
            ),
        )
        wait_sec = max(0.0, min(float(wait or 0.0), 25.0))
        deadline = time.time() + wait_sec
        task = None
        takeover_blocked = False
        from .. import orchestration as orchmod

        while True:
            plane_state.sync_emergency_stop_to_app(conn, app.state)
            if plane_state.is_emergency_stop(conn, app.state):
                return {"task": None, "emergency_stop": True}
            lease_blocked = False
            with conn.transaction():
                if orchmod.device_has_active_lease(conn, row["id"]):
                    lease_blocked = True
                else:
                    now_claim = dbmod.now()
                    try:
                        candidate = conn.execute(
                            "SELECT * FROM tasks WHERE device_id=? AND status='queued' "
                            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                            "ORDER BY created_at ASC LIMIT 1",
                            (row["id"], now_claim),
                        ).fetchone()
                    except Exception:
                        candidate = conn.execute(
                            "SELECT * FROM tasks WHERE device_id=? AND status='queued' ORDER BY created_at ASC LIMIT 1",
                            (row["id"],),
                        ).fetchone()
                    if candidate is not None:
                        # SEC-08: while human takeover is active, only
                        # human-initiated tasks may be claimed; autonomous
                        # queued tasks stay queued until control is handed back.
                        if plane_state.is_human_takeover_active(conn, row["id"]):
                            try:
                                _cargs = json.loads(candidate["args_json"] or "{}")
                            except Exception:
                                _cargs = {}
                            if not _cargs.get("human_initiated"):
                                takeover_blocked = True
                                break
                        lease_exp = now_claim + orchmod.DEFAULT_RUNNING_LEASE_SEC
                        try:
                            cur = conn.execute(
                                "UPDATE tasks SET status='running', epoch=epoch+1, attempt=attempt+1, "
                                "started_at=?, lease_expires_at=?, next_attempt_at=NULL, updated_at=? "
                                "WHERE id=? AND status='queued'",
                                (now_claim, lease_exp, now_claim, candidate["id"]),
                            )
                        except Exception:
                            cur = conn.execute(
                                "UPDATE tasks SET status='running', epoch=epoch+1, updated_at=? WHERE id=? AND status='queued'",
                                (now_claim, candidate["id"]),
                            )
                        if cur.rowcount == 1:
                            task = candidate
                            try:
                                from .. import task_events as task_eventsmod

                                _tid_trace = ""
                                try:
                                    _tid_trace = str(candidate["trace_id"] or "")
                                except Exception:
                                    pass
                                task_eventsmod.append_task_event(
                                    conn,
                                    candidate["id"],
                                    "running",
                                    {"device_id": row["id"], "tool": candidate["tool"]},
                                    trace_id=_tid_trace,
                                    commit=False,
                                )
                            except Exception:
                                pass
                            break
            if lease_blocked:
                rem = deadline - time.time()
                if wait_sec <= 0 or rem <= 0:
                    return {"task": None}
                await agent_channel.wait_for_device_notification(row["id"], min(0.15, rem))
                continue
            rem = deadline - time.time()
            if wait_sec <= 0 or rem <= 0:
                return {"task": None}
            # PERF-06: same wake-await as the lease branch above.
            await agent_channel.wait_for_device_notification(row["id"], min(0.15, rem))
        if takeover_blocked:
            return {"task": None, "human_takeover_active": True}
        out = dict(task)
        try:
            _fresh = conn.execute("SELECT epoch FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if _fresh is not None:
                out["epoch"] = _fresh["epoch"]
        except Exception:
            pass
        # Tell the agent whether a human approved this task. Only approved records
        # that match the tool and have not expired grant approval.
        ap = out.get("approval_id")
        if ap:
            ap_row = conn.execute("SELECT status, expires_at, tool FROM approvals WHERE id=?", (ap,)).fetchone()
            now_ts = dbmod.now()
            out["approval_granted"] = bool(ap_row and ap_row["status"] == "approved" and (ap_row["expires_at"] is None or ap_row["expires_at"] > now_ts) and ap_row["tool"] == out["tool"])
        else:
            out["approval_granted"] = False
        out["policy"] = get_effective_device_policy(row)
        return {"task": out}

    @router.get("/api/agent/tasks/{task_id}/execution-state")
    def agent_task_execution_state(
        task_id: str, x_device_token: str | None = Header(default=None), x_client_cert_fingerprint: str | None = Header(default=None), x_ssl_client_fingerprint: str | None = Header(default=None)
    ):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        task = conn.execute("SELECT id, status, device_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None or task["device_id"] != row["id"]:
            raise HTTPException(status_code=404, detail="Task not found for this device.")
        if task["status"] == "running":
            try:
                agent_channel.handle_agent_message(conn, row["id"], {"type": "progress", "task_id": task_id})
            except Exception:
                pass
        plane_state.sync_emergency_stop_to_app(conn, app.state)
        return {
            "task_id": task_id,
            "status": task["status"],
            "emergency_stop": plane_state.is_emergency_stop(conn, app.state),
        }

    @router.post("/api/agent/tasks/{task_id}/progress")
    @router.post("/api/tasks/{task_id}/progress")
    def task_progress(
        task_id: str,
        body: dict = Body(default={}),
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        """DIST-08 agent progress heartbeat (liveness only).

        The agent POSTs ``{"task_id", "progress_pct", "status_message",
        "epoch"}`` at most once per 60 s per running task. The plane
        bumps ``tasks.updated_at`` iff the task is still ``running``
        (via ``agent_channel.handle_agent_message`` ->
        ``orchestration.record_task_progress``) and never changes
        ``status``. ``progress_pct`` null means "alive, no measurable %".

        Both paths are registered: ``/api/agent/tasks/...`` is the
        agent namespace, and ``/api/tasks/{task_id}/progress`` is the
        path the shipped Wave 3C agent already POSTs to (see
        ``agents/common/poller.py``) — same handler, same device-token
        auth on both, so no agent update is needed for this to work.

        Status codes are deliberate: this path NEVER returns 404/501 —
        the agent treats those as "endpoint not implemented" and
        disables progress for the run. A task that is absent, owned by
        another device, not running, or epoch-mismatched is a 409 (the
        detail text says which). The heartbeat itself is throttled to
        one DB write per 60 s per task inside
        ``agent_channel.handle_agent_message``; calls inside the window
        return ``{"ok": true, "throttled": true}`` with no DB write, so
        ``task_events`` does not grow per call.
        """
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Progress body must be a JSON object.")
        body_task_id = str(body.get("task_id") or "").strip()
        if body_task_id and body_task_id != task_id:
            raise HTTPException(status_code=400, detail="Body task_id does not match the path task_id.")
        progress_pct = body.get("progress_pct")
        if progress_pct is not None:
            try:
                progress_pct = float(progress_pct)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="progress_pct must be a number between 0 and 100, or null.") from None
            if not 0.0 <= progress_pct <= 100.0:
                raise HTTPException(status_code=400, detail="progress_pct must be a number between 0 and 100, or null.")
        status_message = body.get("status_message")
        if status_message is not None and not isinstance(status_message, str):
            raise HTTPException(status_code=400, detail="status_message must be a string or null.")
        epoch = body.get("epoch")
        if epoch is not None:
            try:
                epoch = int(epoch)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="epoch must be an integer.") from None
        task = conn.execute("SELECT id, status, device_id, epoch FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            # Deliberately 409, not 404: see docstring (agent disables
            # progress on 404/501 for the whole run).
            raise HTTPException(status_code=409, detail="Task not found.")
        if task["device_id"] != row["id"]:
            raise HTTPException(status_code=409, detail="Task not found for this device.")
        if epoch is not None and int(task["epoch"] or 0) != epoch:
            raise HTTPException(status_code=409, detail="Stale progress: the task epoch does not match (task was re-queued or re-claimed).")
        if task["status"] != "running":
            raise HTTPException(status_code=409, detail=f"Task is '{task['status']}'; progress is only accepted while it is running.")
        ack = agent_channel.handle_agent_message(
            conn,
            row["id"],
            {
                "type": "progress",
                "task_id": task_id,
                "progress_pct": progress_pct,
                "status_message": (status_message or "")[:500] if status_message is not None else None,
            },
        )
        if ack is None or not ack.get("ok"):
            raise HTTPException(status_code=409, detail="Task is no longer running; stop work on it.")
        return {
            "task_id": task_id,
            "status": "running",
            "ok": True,
            "throttled": bool(ack.get("throttled", False)),
            "progress_pct": progress_pct,
            "status_message": (status_message or "")[:500] if status_message else None,
        }

    @router.post("/api/agent/tasks/{task_id}/result")
    def task_result(
        task_id: str,
        body: TaskResultIn,
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND device_id=?", (task_id, row["id"])).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found for this device.")
        if body.status not in {"completed", "failed", "cancelled", "timed_out"}:
            raise HTTPException(status_code=400, detail="Invalid result status.")
        # DIST-05/F07-04: results are only accepted while the task is
        # running; a late result for a finished/re-queued task is a 409.
        if task["status"] != "running":
            raise HTTPException(status_code=409, detail=f"Task is '{task['status']}'; results are only accepted while it is running (late result rejected).")
        # Screenshot results carry base64 image data; truncating them would
        # corrupt the image the model or dashboard receives.
        result_cap = 16_000_000 if task["tool"] in ("take_screenshot", "capture_desktop_screenshot", "capture_desktop_frame", "set_of_marks_screenshot") else MAX_RESULT_CHARS
        now_ts = dbmod.now()
        trimmed_result = body.result[:result_cap]
        from ...policy import is_tool_idempotent
        from .. import orchestration as orchmod

        task_dict = dict(task)
        attempt = int(task_dict.get("attempt") or task_dict.get("epoch") or 1)
        max_attempts = max(1, int(task_dict.get("max_attempts") or 1))
        trace_id = str(task_dict.get("trace_id") or "")

        final_status = body.status
        failure_class = ""
        failure_reason = ""
        next_attempt_at: float | None = None
        should_dlq = False

        if body.status != "completed":
            failure_class, failure_reason = orchmod.classify_failure(body.status, trimmed_result)
            if (
                body.status == "failed"
                and failure_class == "transient"
                and attempt < max_attempts
                and is_tool_idempotent(str(task_dict.get("tool") or ""))
                and not plane_state.is_emergency_stop(conn, app.state)
            ):
                final_status = "queued"
                next_attempt_at = now_ts + orchmod.retry_backoff_seconds(attempt)
            elif body.status in ("failed", "timed_out") and max_attempts > 1 and attempt >= max_attempts:
                should_dlq = True

        epoch_clause = " AND epoch=?" if body.epoch is not None else ""
        try:
            params: list = [
                final_status,
                trimmed_result,
                failure_class,
                failure_reason,
                next_attempt_at,
                now_ts,
                task_id,
            ]
            if body.epoch is not None:
                params.append(body.epoch)
            _cur = conn.execute(
                "UPDATE tasks SET status=?, result=?, failure_class=?, failure_reason=?, "
                f"next_attempt_at=?, lease_expires_at=NULL, updated_at=? WHERE id=? AND status='running'{epoch_clause}",
                tuple(params),
            )
        except Exception:
            if body.epoch is not None:
                _cur = conn.execute("UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=? AND status='running' AND epoch=?", (final_status, trimmed_result, now_ts, task_id, body.epoch))
            else:
                _cur = conn.execute("UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=? AND status='running'", (final_status, trimmed_result, now_ts, task_id))
        if _cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="Stale result: the task is no longer running or the execution epoch does not match (late result rejected).")
        try:
            from .. import task_events as task_eventsmod

            ev_name = "retry_scheduled" if final_status == "queued" else final_status
            ev_detail: dict = {"tool": task["tool"], "device_id": row["id"], "attempt": attempt}
            if failure_class:
                ev_detail["failure_class"] = failure_class
                ev_detail["failure_reason"] = failure_reason[:200]
            if next_attempt_at is not None:
                ev_detail["next_attempt_at"] = next_attempt_at
            task_eventsmod.append_task_event(
                conn,
                task_id,
                ev_name,
                ev_detail,
                trace_id=trace_id,
                commit=False,
            )
        except Exception:
            pass
        if should_dlq:
            try:
                orchmod.record_dead_letter(
                    conn,
                    task_dict,
                    failure_reason=failure_reason,
                    failure_class=failure_class,
                    last_error=trimmed_result[:2000],
                    commit=False,
                )
            except Exception:
                pass
        conn.commit()
        try:
            created = float(task["created_at"] or now_ts)
            duration = max(0.0, now_ts - created)
            from .. import metrics as metricsmod

            metricsmod.record_task_execution(str(task["tool"] or "unknown"), body.status, duration)
        except Exception:
            pass
        handoff_advance = None
        try:
            fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if fresh is not None:
                handoff_advance = supervisormod.advance_handoff(conn, fresh, status=body.status)
                if handoff_advance and handoff_advance.get("advanced"):
                    notify_tasks()
        except Exception:
            handoff_advance = None
        notify_tasks()
        if task["tool"] in ("capture_desktop_frame", "take_screenshot"):
            try:
                res_obj = json.loads(body.result)
                if isinstance(res_obj, dict) and res_obj.get("base64_data"):
                    if "timestamp" not in res_obj:
                        res_obj["timestamp"] = time.time()
                    app.state.latest_frames[row["id"]] = res_obj
            except Exception:
                pass
        audit("device", row["id"], task["tool"], json.loads(task["args_json"] or "{}"), body.status, body.result[:500], tenant=task["tenant"])
        out = {"task_id": task_id, "status": body.status}
        if handoff_advance:
            out["handoff_advance"] = handoff_advance
        return out

    return router

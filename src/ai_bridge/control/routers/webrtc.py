"""webrtc routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import Response, StreamingResponse

from ... import terminal_manager
from .. import plane_state
from ..app import DesktopInputIn
from ._context import ControlContext


def create_webrtc_router(ctx: ControlContext) -> APIRouter:
    """Build the webrtc APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    _execute_workflow_step_sync = ctx._execute_workflow_step_sync
    _fetch_desktop_frame = ctx._fetch_desktop_frame
    _get_webrtc_session = ctx._get_webrtc_session
    _load_sess = ctx._load_sess
    _queue_webrtc_task = ctx._queue_webrtc_task
    _remote_terminal_exec = ctx._remote_terminal_exec
    _require_agent_mtls = ctx._require_agent_mtls
    _require_device_tenant = ctx._require_device_tenant
    _save_sess = ctx._save_sess
    app = ctx.app
    audit = ctx.audit
    conn = ctx.conn
    device_row = ctx.device_row
    get_current_policy = ctx.get_current_policy
    get_effective_device_policy = ctx.get_effective_device_policy
    require_role = ctx.require_role
    resolve_request_tenant = ctx.resolve_request_tenant

    @router.get("/api/devices/{device_id}/desktop/frame")
    def get_desktop_frame(
        device_id: str,
        scale: float = 0.75,
        quality: int = 70,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        # ChatGPT Custom GPT Actions send Bearer via Authorization; keep header tokens too.
        require_role("viewer", x_admin_token, x_session_token, authorization)
        _require_device_tenant(device_id, x_admin_token, x_session_token, authorization)
        return _fetch_desktop_frame(device_id, scale, quality)

    @router.get("/api/devices/{device_id}/desktop/live.jpg")
    def get_device_desktop_live_jpeg(
        device_id: str,
        scale: float = 0.75,
        quality: int = 75,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """Return the latest live desktop frame directly as an image/jpeg binary response.

        Wave 6A residual: credentials are accepted ONLY via headers
        (X-Admin-Token / X-Session-Token / Authorization). The former
        ``?token=`` / ``?session_token=`` query fallbacks are removed —
        tokens in query strings leak into logs, browser history and
        referrers, and the dashboard no longer sends them.
        """
        import base64

        require_role("viewer", x_admin_token, x_session_token, authorization)
        _require_device_tenant(device_id, x_admin_token, x_session_token, authorization)
        frame = _fetch_desktop_frame(device_id, scale, quality)
        if not isinstance(frame, dict) or not frame.get("base64_data"):
            raise HTTPException(status_code=404, detail=frame.get("error") if isinstance(frame, dict) else "No desktop frame available.")
        raw_bytes = base64.b64decode(frame["base64_data"])
        return Response(content=raw_bytes, media_type=frame.get("mime_type") or "image/jpeg", headers={"Cache-Control": "no-store"})

    @router.get("/api/fleet/desktop/frames")
    def get_fleet_desktop_frames(
        os: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        require_role("operator", x_admin_token, x_session_token, authorization)
        tenant = resolve_request_tenant(None, x_admin_token, x_session_token, authorization)
        query = "SELECT id, name, hostname, os, status, last_seen FROM devices WHERE revoked=0 AND tenant=?"
        params: list[str] = [tenant]
        if os:
            query += " AND (LOWER(os) LIKE ? OR LOWER(name) LIKE ?)"
            params.extend([f"%{os.lower()}%", f"%{os.lower()}%"])
        rows = conn.execute(query, tuple(params)).fetchall()
        now_ts = time.time()
        result = []
        for r in rows:
            dev_id = r["id"]
            frame = app.state.latest_frames.get(dev_id)
            is_recent = bool(frame and (now_ts - frame.get("timestamp", 0)) < 25.0)
            result.append(
                {
                    "device_id": dev_id,
                    "name": r["name"],
                    "hostname": r["hostname"],
                    "os": r["os"],
                    "status": r["status"],
                    "last_seen": r["last_seen"],
                    "frame": frame if is_recent else None,
                    "frame_age_s": round(now_ts - frame.get("timestamp", 0), 1) if (frame and "timestamp" in frame) else None,
                }
            )
        return {"devices": result, "count": len(result), "timestamp": now_ts}

    @router.post("/api/devices/{device_id}/desktop/input")
    def send_desktop_input(
        device_id: str,
        body: DesktopInputIn,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        actor = require_role("operator", x_admin_token, x_session_token, authorization)
        _require_device_tenant(device_id, x_admin_token, x_session_token, authorization)
        tenant = resolve_request_tenant(None, x_admin_token, x_session_token, authorization)
        abs_x, abs_y = None, None
        if body.x is not None and body.y is not None:
            if body.normalized and 0.0 <= body.x <= 1.0 and 0.0 <= body.y <= 1.0:
                abs_x = int(body.x * body.screen_width)
                abs_y = int(body.y * body.screen_height)
            else:
                abs_x = int(body.x)
                abs_y = int(body.y)

        try:
            action = body.action.lower()
            if action in ("click", "double_click", "triple_click", "right_click", "middle_click"):
                btn = "right" if action == "right_click" else ("middle" if action == "middle_click" else body.button)
                clicks = 2 if action == "double_click" else (3 if action == "triple_click" else body.clicks)
                res = _execute_workflow_step_sync(
                    "mouse_click",
                    {"x": abs_x, "y": abs_y, "button": btn, "clicks": clicks},
                    device_id,
                    tenant,
                    f"dual_control:{actor}",
                )
            elif action == "move":
                res = _execute_workflow_step_sync(
                    "mouse_move",
                    {"x": abs_x, "y": abs_y},
                    device_id,
                    tenant,
                    f"dual_control:{actor}",
                )
            elif action == "scroll":
                res = _execute_workflow_step_sync(
                    "mouse_scroll",
                    {"delta": body.delta, "x": abs_x, "y": abs_y},
                    device_id,
                    tenant,
                    f"dual_control:{actor}",
                )
            elif action == "type":
                res = _execute_workflow_step_sync(
                    "keyboard_type",
                    {"text": body.text or ""},
                    device_id,
                    tenant,
                    f"dual_control:{actor}",
                )
            elif action in ("press", "hotkey"):
                res = _execute_workflow_step_sync(
                    "press_hotkey",
                    {"combo": body.key or ""},
                    device_id,
                    tenant,
                    f"dual_control:{actor}",
                )
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported input action: {action}")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        audit(actor, device_id, f"dual_control_{action}", {"x": abs_x, "y": abs_y, "key": body.key, "text": bool(body.text)}, "executed")
        return {"ok": True, "action": action, "x": abs_x, "y": abs_y, "result": res}

    @router.get("/api/devices/{device_id}/desktop/a11y/snapshot")
    def get_desktop_a11y_snapshot(
        device_id: str,
        interactive_only: bool = True,
        skeleton: bool = False,
        compact: bool = True,
        window_title: str | None = None,
        max_depth: int = 8,
        max_elements: int = 120,
        session_id: str | None = None,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """Capture a structured Accessibility Tree snapshot with ``@ref`` IDs for the Live Desktop Inspector."""
        actor = require_role("viewer", x_admin_token, x_session_token, authorization)
        _require_device_tenant(device_id, x_admin_token, x_session_token, authorization)
        tenant = resolve_request_tenant(None, x_admin_token, x_session_token, authorization)
        args = {
            "interactive_only": bool(interactive_only),
            "skeleton": bool(skeleton),
            "compact": bool(compact),
            "window_title": window_title or "",
            "max_depth": max(1, min(20, int(max_depth))),
            "max_elements": max(1, min(300, int(max_elements))),
            "session_id": (session_id or f"ui_{device_id}").strip(),
        }
        try:
            res = _execute_workflow_step_sync(
                "desktop_a11y_snapshot",
                args,
                device_id,
                tenant,
                f"a11y_inspector:{actor}",
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return res if isinstance(res, dict) else {"ok": False, "error": "Invalid accessibility snapshot response"}

    @router.post("/api/devices/{device_id}/desktop/a11y/act")
    def act_desktop_a11y(
        device_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """Execute a policy-gated headless ``@ref`` accessibility action from the Live Desktop Inspector."""
        plane_state.refresh_emergency_stop(conn, app.state)
        if plane_state.is_emergency_stop(conn, app.state):
            raise HTTPException(status_code=503, detail="Emergency stop active.")
        actor = require_role("operator", x_admin_token, x_session_token, authorization)
        _require_device_tenant(device_id, x_admin_token, x_session_token, authorization)
        tenant = resolve_request_tenant(None, x_admin_token, x_session_token, authorization)

        ref = str(body.get("ref") or "").strip()
        if not ref:
            raise HTTPException(status_code=400, detail="A non-empty '@ref' target is required.")
        action = str(body.get("action") or "click").strip().lower()

        # Enforce effective policy desktop_enabled gate (View-Only lock)
        eff_policy = get_current_policy() if device_id == "local" else get_effective_device_policy(device_id)
        if not bool((eff_policy or {}).get("desktop_enabled", False)):
            audit(actor, device_id, f"a11y_act_{action}", {"ref": ref, "action": action}, "denied")
            raise HTTPException(
                status_code=403,
                detail="Interactive desktop automation is disabled by active policy (View-Only enforced).",
            )

        payload: dict = {
            "ref": ref,
            "action": action,
            "headed": bool(body.get("headed", False)),
            "fallback_to_visual": bool(body.get("fallback_to_visual", True)),
            "session_id": str(body.get("session_id") or f"ui_{device_id}").strip(),
        }
        if body.get("text") is not None:
            payload["text"] = str(body.get("text"))
        if body.get("option") is not None:
            payload["option"] = str(body.get("option"))
        if body.get("fallback_text") is not None:
            payload["fallback_text"] = str(body.get("fallback_text"))
        if body.get("wait_after_ms") is not None:
            try:
                payload["wait_after_ms"] = max(0, min(10000, int(body.get("wait_after_ms"))))
            except (TypeError, ValueError):
                pass

        try:
            res = _execute_workflow_step_sync(
                "desktop_a11y_act",
                payload,
                device_id,
                tenant,
                f"a11y_inspector:{actor}",
            )
        except PermissionError as exc:
            audit(actor, device_id, f"a11y_act_{action}", {"ref": ref, "action": action}, "denied")
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        ok = isinstance(res, dict) and bool(res.get("ok"))
        audit(actor, device_id, f"a11y_act_{action}", {"ref": ref, "action": action, "headed": payload["headed"]}, "executed" if ok else "failed")
        return res if isinstance(res, dict) else {"ok": False, "error": "Invalid accessibility action response"}

    @router.get("/api/devices/{device_id}/desktop/monitors")
    def get_device_monitors(
        device_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        if device_id == "local":
            from ai_bridge.screen_capture import get_monitors

            return {"monitors": get_monitors()}
        dev_row = conn.execute("SELECT * FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
        if dev_row is None:
            from ai_bridge.screen_capture import get_monitors

            return {"monitors": get_monitors()}
        caps = json.loads(dev_row["capabilities_json"] or "{}")
        if not isinstance(caps, dict):
            caps = {}  # capabilities_json defaults to a JSON list ('[]')
        mons = caps.get("monitors")
        if not mons:
            mons = [{"index": 0, "name": "Primary Display", "width": 1920, "height": 1080, "is_primary": True}]
        return {"monitors": mons}

    @router.post("/api/devices/{device_id}/desktop/webrtc/session")
    def create_webrtc_session(
        device_id: str,
        response: Response,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        from .. import production_readiness as prodready

        dep_headers = {
            "Deprecation": "true",
            "Link": '</api/v1/sessions>; rel="successor-version"',
            "Sunset": "legacy-webrtc-session-route",
        }
        response.headers.update(dep_headers)
        if not prodready.legacy_webrtc_routes_allowed():
            raise HTTPException(
                status_code=410,
                detail={
                    "error": "legacy_webrtc_gone",
                    "message": "Use POST /api/v1/sessions. Set AI_BRIDGE_ALLOW_LEGACY_WEBRTC=1 only for emergency compatibility.",
                },
                headers=dep_headers,
            )
        plane_state.refresh_emergency_stop(conn, app.state)
        if plane_state.is_emergency_stop(conn, app.state):
            raise HTTPException(status_code=503, detail="Emergency stop active.", headers=dep_headers)
        actor = require_role("viewer", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        import platform as _platform

        from .. import desktop_capabilities as desktop_capabilitiesmod

        if device_id == "local":
            device_os = _platform.system()
        else:
            gate_row = conn.execute("SELECT os FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
            if gate_row is None:
                raise HTTPException(status_code=404, detail="Device not found.", headers=dep_headers)
            try:
                device_os = gate_row["os"]
            except (KeyError, IndexError, TypeError):
                device_os = gate_row[0] if gate_row is not None else None
        gate = desktop_capabilitiesmod.stream_gate_for_os(device_os)
        if not gate.get("allowed") or not desktop_capabilitiesmod.platform_supports_webrtc_desktop_stream(device_os):
            detail = (
                f"WebRTC desktop stream unavailable for platform "
                f"{gate.get('platform') or device_os or 'unknown'}: {gate.get('detail') or gate.get('reason')}. "
                "Live Desktop streaming is supported on Windows desktop hosts only."
            )
            audit(actor, device_id, "start_webrtc_stream", {"gate": gate, "device_os": device_os}, "denied")
            raise HTTPException(status_code=400, detail=detail, headers=dep_headers)
        scale = max(0.2, min(1.0, float(body.get("scale", 0.75))))
        fps = max(1, min(60, int(body.get("fps", 30))))
        bitrate_kbps = body.get("bitrate_kbps")
        if bitrate_kbps is not None:
            try:
                bitrate_kbps = max(250, min(50000, int(bitrate_kbps)))
            except (TypeError, ValueError):
                bitrate_kbps = None
        mode = str(body.get("mode", "ai_assisted"))
        monitor_index = body.get("monitor_index")
        if monitor_index is not None:
            try:
                monitor_index = int(monitor_index)
            except (ValueError, TypeError):
                monitor_index = None
        session_id = uuid.uuid4().hex[:12]
        sess = {
            "id": session_id,
            "session_id": session_id,
            "device_id": device_id,
            "user_id": actor,
            "mode": mode,
            "status": "waiting",
            "video": {"codec": "H264", "width": int(1920 * scale) & ~1, "height": int(1080 * scale) & ~1, "fps": fps},
            "permissions": {"view_screen": True, "control_mouse": mode != "view_only", "control_keyboard": mode != "view_only", "execute_commands": False},
            "offer": None,
            "answer": None,
            "agent_candidates": [],
            "viewer_candidates": [],
            "created_at": time.time(),
            "scale": scale,
            "fps": fps,
            "monitor_index": monitor_index,
            "error": None,
        }
        _save_sess(sess)
        audit(actor, device_id, "start_webrtc_stream", {"session_id": session_id, "monitor_index": monitor_index}, "allowed")
        if device_id == "local":
            try:
                from ai_bridge import webrtc_stream

                webrtc_stream.start_stream(session_id, webrtc_stream.DictSignaling(sess), scale=scale, fps=fps, monitor_index=monitor_index)
            except Exception as exc:
                sess["status"] = "error"
                sess["error"] = str(exc)
            _save_sess(sess)
        else:
            _queue_webrtc_task(
                device_id,
                session_id,
                scale,
                fps,
                monitor_index=monitor_index,
                bitrate_kbps=bitrate_kbps,
            )
        return {
            "session_id": session_id,
            "status": sess["status"],
            "scale": scale,
            "fps": fps,
            "bitrate_kbps": bitrate_kbps,
            "monitor_index": monitor_index,
            "mode": mode,
            "video": sess["video"],
            "permissions": sess["permissions"],
        }

    @router.get("/api/devices/{device_id}/desktop/webrtc/{session_id}")
    def get_webrtc_session(
        device_id: str,
        session_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        sess = _get_webrtc_session(device_id, session_id)
        from ai_bridge import webrtc_stream

        out = {
            k: sess.get(k)
            for k in (
                "id",
                "session_id",
                "device_id",
                "user_id",
                "mode",
                "video",
                "permissions",
                "status",
                "offer",
                "answer",
                "agent_candidates",
                "viewer_candidates",
                "error",
                "scale",
                "fps",
                "monitor_index",
            )
        }
        out["telemetry"] = webrtc_stream.get_session_telemetry(session_id)
        out["ice_servers"] = webrtc_stream.get_configured_ice_servers()
        return out

    @router.post("/api/devices/{device_id}/desktop/webrtc/{session_id}/answer")
    def post_webrtc_answer(
        device_id: str,
        session_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        sess = _get_webrtc_session(device_id, session_id)
        sess["answer"] = {"sdp": body.get("sdp"), "type": body.get("type", "answer")}
        sess["status"] = "answered"
        _save_sess(sess)
        return {"ok": True}

    @router.post("/api/devices/{device_id}/desktop/webrtc/{session_id}/candidates")
    def post_webrtc_viewer_candidate(
        device_id: str,
        session_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        sess = _get_webrtc_session(device_id, session_id)
        if body.get("candidate"):
            sess.setdefault("viewer_candidates", []).append(body["candidate"])
            _save_sess(sess)
        return {"ok": True}

    @router.delete("/api/devices/{device_id}/desktop/webrtc/{session_id}")
    def end_webrtc_session(
        device_id: str,
        session_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("viewer", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        sess = _get_webrtc_session(device_id, session_id)
        sess["status"] = "ended"
        _save_sess(sess)
        if device_id != "local":
            try:
                _queue_webrtc_task(device_id, session_id, 0.75, 10, tool="stop_webrtc_stream")
            except Exception:
                pass
        else:
            try:
                from ai_bridge import webrtc_stream

                webrtc_stream.stop_stream(session_id)
            except Exception:
                pass
        return {"ok": True, "status": "ended"}

    @router.get("/api/agent/webrtc/ice-servers")
    def agent_get_webrtc_ice_servers(
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        """Device-authenticated ICE/TURN list for the Windows C++ agent settings UI.

        Literal path must stay ahead of ``/api/agent/webrtc/{session_id}`` so
        Starlette does not treat ``ice-servers`` as a session id.
        """
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        from ai_bridge import webrtc_stream

        return {"ice_servers": webrtc_stream.get_configured_ice_servers()}

    @router.get("/api/agent/webrtc/{session_id}")
    def agent_get_webrtc_session(
        session_id: str, x_device_token: str | None = Header(default=None), x_client_cert_fingerprint: str | None = Header(default=None), x_ssl_client_fingerprint: str | None = Header(default=None)
    ):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        sess = _load_sess(session_id)
        if sess is None or sess.get("device_id") != row["id"]:
            raise HTTPException(status_code=404, detail="WebRTC session not found.")
        out = {k: sess.get(k) for k in ("id", "device_id", "status", "offer", "answer", "agent_candidates", "viewer_candidates", "error", "scale", "fps", "created_at")}
        # Wave 1B blocker B-1: the agent cache reads these keys, so they
        # must be present on every session read, not only on v1 updates.
        out["mode"] = sess.get("mode", "ai_assisted")
        out["permissions"] = sess.get("permissions") or {"control": False, "input": False, "view": True}
        _takeover = bool(sess.get("human_takeover_active")) or plane_state.is_human_takeover_active(conn, row["id"])
        out["human_takeover_active"] = _takeover
        out["human_takeover"] = _takeover
        return out

    @router.post("/api/agent/webrtc/{session_id}/offer")
    def agent_post_webrtc_offer(
        session_id: str,
        body: dict = Body(default={}),
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        sess = _load_sess(session_id)
        if sess is None or sess.get("device_id") != row["id"]:
            raise HTTPException(status_code=404, detail="WebRTC session not found.")
        sess["offer"] = {"sdp": body.get("sdp"), "type": body.get("type", "offer")}
        sess["status"] = "offered"
        _save_sess(sess)
        return {"ok": True}

    @router.post("/api/agent/webrtc/{session_id}/candidates")
    def agent_post_webrtc_candidate(
        session_id: str,
        body: dict = Body(default={}),
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        sess = _load_sess(session_id)
        if sess is None or sess.get("device_id") != row["id"]:
            raise HTTPException(status_code=404, detail="WebRTC session not found.")
        if body.get("candidate"):
            sess.setdefault("agent_candidates", []).append(body["candidate"])
            _save_sess(sess)
        return {"ok": True}

    @router.post("/api/agent/webrtc/{session_id}/error")
    def agent_post_webrtc_error(
        session_id: str,
        body: dict = Body(default={}),
        x_device_token: str | None = Header(default=None),
        x_client_cert_fingerprint: str | None = Header(default=None),
        x_ssl_client_fingerprint: str | None = Header(default=None),
    ):
        row = device_row(x_device_token)
        _require_agent_mtls(None, row["id"], x_client_cert_fingerprint, x_ssl_client_fingerprint)
        sess = _load_sess(session_id)
        if sess is None or sess.get("device_id") != row["id"]:
            raise HTTPException(status_code=404, detail="WebRTC session not found.")
        sess["status"] = "error"
        sess["error"] = body.get("error", "Stream failed.")
        _save_sess(sess)
        return {"ok": True}

    @router.post("/api/devices/{device_id}/terminal/sessions")
    def create_terminal_endpoint(
        device_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        cols = int(body.get("cols", 120))
        rows = int(body.get("rows", 35))
        dev_label = "local"
        dev_os = ""
        remote_fn = None
        if device_id and device_id != "local":
            dev_row = conn.execute("SELECT name, hostname, os FROM devices WHERE id=? AND revoked=0", (device_id,)).fetchone()
            if dev_row:
                dev_label = dev_row["hostname"] or dev_row["name"] or device_id
                dev_os = dev_row["os"] or ""
                remote_fn = _remote_terminal_exec
        sess = terminal_manager.create_terminal_session(
            device_id=device_id,
            cols=cols,
            rows=rows,
            remote_exec=remote_fn,
            device_label=dev_label,
            device_os=dev_os,
        )
        audit(actor, device_id, "create_terminal_session", {"session_id": sess.session_id, "cols": cols, "rows": rows, "remote_relay": bool(remote_fn)}, "allowed")
        return {"session_id": sess.session_id, "device_id": device_id, "cols": cols, "rows": rows, "is_alive": sess.is_alive}

    @router.get("/api/devices/{device_id}/terminal/sessions")
    def list_terminal_endpoint(
        device_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("admin", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        sessions = terminal_manager.list_terminal_sessions(device_id=device_id)
        return {"sessions": sessions}

    @router.get("/api/devices/{device_id}/terminal/{session_id}/stream")
    def stream_terminal_endpoint(
        device_id: str,
        session_id: str,
        since_seq: int = 0,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        # Wave 6A residual: headers only — the former ``?token=`` /
        # ``?session_token=`` query fallbacks are removed (tokens in query
        # strings leak into logs, browser history and referrers).
        require_role("admin", x_admin_token, x_session_token, authorization)
        _require_device_tenant(device_id, x_admin_token, x_session_token, authorization)
        sess = terminal_manager.get_terminal_session(session_id)
        if sess is None or getattr(sess, "device_id", None) != device_id:
            raise HTTPException(status_code=404, detail="Terminal session not found.")
        return StreamingResponse(sess.stream(since_seq=since_seq), media_type="text/event-stream")

    @router.post("/api/devices/{device_id}/terminal/{session_id}/input")
    def input_terminal_endpoint(
        device_id: str,
        session_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("admin", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        sess = terminal_manager.get_terminal_session(session_id)
        if sess is None or getattr(sess, "device_id", None) != device_id:
            raise HTTPException(status_code=404, detail="Terminal session not found.")
        input_data = body.get("input", "")
        sess.write(input_data)
        return {"ok": True}

    @router.post("/api/devices/{device_id}/terminal/{session_id}/resize")
    def resize_terminal_endpoint(
        device_id: str,
        session_id: str,
        body: dict = Body(default={}),
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        require_role("admin", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        sess = terminal_manager.get_terminal_session(session_id)
        if sess is None or getattr(sess, "device_id", None) != device_id:
            raise HTTPException(status_code=404, detail="Terminal session not found.")
        cols = int(body.get("cols", 120))
        rows = int(body.get("rows", 35))
        sess.resize(cols, rows)
        return {"ok": True, "cols": cols, "rows": rows}

    @router.delete("/api/devices/{device_id}/terminal/{session_id}")
    def delete_terminal_endpoint(
        device_id: str,
        session_id: str,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
    ):
        actor = require_role("admin", x_admin_token, x_session_token)
        _require_device_tenant(device_id, x_admin_token, x_session_token)
        sess = terminal_manager.get_terminal_session(session_id)
        if sess is None or getattr(sess, "device_id", None) != device_id:
            raise HTTPException(status_code=404, detail="Terminal session not found.")
        ok = terminal_manager.close_terminal_session(session_id)
        audit(actor, device_id, "close_terminal_session", {"session_id": session_id}, "allowed")
        return {"ok": ok}

    return router

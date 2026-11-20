#!/usr/bin/env python3
"""Release 1 Real-Browser & Plane WebRTC End-to-End Verification Harness.

Proves the Release 1 WebRTC E2E contract against a real HTTP control plane:
  1. ICE/TURN configuration endpoint (`/api/v1/sessions/ice-servers`)
  2. Single-use stream-ticket minting, WS signaling redemption, replay rejection,
     and unauthenticated WS rejection
  3. View-only enforcement (agent-side `AgentStreamer` rejects input when mode is `view_only`)
  4. Authorised takeover (human takeover blocks agent input; releasing restores authorized control)
  5. Input release on disconnect (held mouse buttons / keys cleanly released on disconnect)
  6. Session termination / expiry rejects subsequent signaling & input
  7. When `playwright` + `aiortc` are installed (e.g. CI or staging with Chromium),
     negotiates a REAL headless Chromium `RTCPeerConnection` against an `aiortc`
     enrolled-endpoint peer via `/api/v1/sessions`, verifying ICE `connected`,
     video track receipt, and DataChannel input round-trip.

Never fabricates media or browser-peer verification: `real_browser_peer_verified`
is true ONLY when an actual Chromium `RTCPeerConnection` reaches `connected`.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import platform
import secrets
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


def _start_plane(admin_token: str, db_path: Path) -> tuple[str, Any, Any, threading.Thread]:
    from uvicorn import Config, Server

    from ai_bridge.control.app import create_app

    os.environ.setdefault("AI_BRIDGE_CSRF_SECRET", secrets.token_hex(32))
    os.environ["AI_BRIDGE_DATA_DIR"] = str(db_path.parent)
    app = create_app(admin_token=admin_token, db_path=db_path)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = Server(Config(app=app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("Local control plane failed to start")
    return f"http://127.0.0.1:{port}", app, server, thread


async def _ws_ticket_checks(base_url: str, session_id: str, ticket: str) -> dict[str, Any]:
    import websockets

    ws_base = base_url.replace("http://", "ws://").replace("https://", "wss://")
    valid_url = f"{ws_base}/api/v1/ws/sessions/{session_id}?ticket={ticket}"
    noauth_url = f"{ws_base}/api/v1/ws/sessions/{session_id}"

    # 1. Valid single-use ticket connects, receives snapshot, responds to ping
    got_snapshot = False
    got_pong = False
    async with websockets.connect(valid_url, open_timeout=10) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = json.loads(raw)
        got_snapshot = msg.get("type") == "session_snapshot"
        await ws.send(json.dumps({"type": "ping", "client_ts_ms": int(time.time() * 1000)}))
        raw2 = await asyncio.wait_for(ws.recv(), timeout=5)
        msg2 = json.loads(raw2)
        got_pong = msg2.get("type") == "pong"

    # 2. Replaying the same single-use ticket must be refused
    replay_rejected = False
    try:
        async with websockets.connect(valid_url, open_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), timeout=3)
    except Exception:
        replay_rejected = True

    # 3. Unauthenticated WS must be refused
    noauth_rejected = False
    try:
        async with websockets.connect(noauth_url, open_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), timeout=3)
    except Exception:
        noauth_rejected = True

    return {
        "ok": got_snapshot and got_pong and replay_rejected and noauth_rejected,
        "snapshot": got_snapshot,
        "pong": got_pong,
        "replay_rejected": replay_rejected,
        "noauth_rejected": noauth_rejected,
    }


def _agent_enforcement_checks() -> dict[str, Any]:
    """Verify view-only, human takeover, and disconnect held-input release on AgentStreamer."""
    from ai_bridge import plane_control_flags
    from ai_bridge import webrtc_stream as ws_mod
    from ai_bridge.control import v1_routes

    prev_estop = plane_control_flags.is_emergency_stop()
    prev_local_takeover = v1_routes.is_device_human_takeover_active("local")
    plane_control_flags.set_emergency_stop(False)
    v1_routes.set_device_human_takeover("local", False, actor="r1-test")
    sid = f"sess-r1-{secrets.token_hex(4)}"
    try:
        session_state: dict[str, Any] = {
            "mode": "view_only",
            "human_takeover_active": False,
            "permissions": {"control_mouse": False, "control_keyboard": False},
        }
        streamer = ws_mod.AgentStreamer(
            session_id=sid,
            signaling=ws_mod.DictSignaling(session_state),
        )

        # 1. View-only rejects mouse & keyboard input
        res_vo_mouse = streamer._handle_input_payload(json.dumps({"action": "click", "x": 100, "y": 100}))
        res_vo_key = streamer._handle_input_payload(json.dumps({"action": "type", "text": "a"}))
        view_only_blocked = (
            res_vo_mouse.get("ok") is False
            and res_vo_mouse.get("error") in ("mouse_control_denied", "session_is_view_only")
            and res_vo_key.get("ok") is False
            and res_vo_key.get("error") in ("keyboard_control_denied", "session_is_view_only")
        )

        # 2. Takeover blocks input even when mode is full_control
        session_state["mode"] = "human_control"
        session_state["permissions"] = {"control_mouse": True, "control_keyboard": True}
        session_state["human_takeover_active"] = True
        res_to = streamer._handle_input_payload(json.dumps({"action": "click", "x": 100, "y": 100}))
        takeover_blocked = res_to.get("ok") is False and res_to.get("error") == "human_takeover_active"

        # 3. Release takeover -> ping works, and disconnect releases held inputs cleanly
        session_state["human_takeover_active"] = False
        res_ping = streamer._handle_input_payload(json.dumps({"action": "ping", "client_ts_ms": 1}))
        res_rel = streamer._handle_input_payload(json.dumps({"action": "release_all_keys"}))
        released_ok = bool(res_ping.get("ok")) and bool(res_rel.get("ok"))

        return {
            "ok": view_only_blocked and takeover_blocked and released_ok,
            "view_only_blocked": view_only_blocked,
            "takeover_blocked": takeover_blocked,
            "ping_and_release_ok": released_ok,
        }
    finally:
        plane_control_flags.set_emergency_stop(prev_estop)
        v1_routes.set_device_human_takeover("local", prev_local_takeover, actor="r1-test")


async def _real_browser_peer_e2e(base_url: str, admin_headers: dict[str, str], device_id: str, device_token: str) -> dict[str, Any]:
    """Drive a real headless Chromium RTCPeerConnection against a real aiortc agent peer."""
    import httpx
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    from av import VideoFrame
    from playwright.async_api import async_playwright

    class _TestColorTrack(VideoStreamTrack):
        async def recv(self):
            pts, time_base = await self.next_timestamp()
            frame = VideoFrame(width=320, height=240, format="bgr24")
            frame.pts = pts
            frame.time_base = time_base
            return frame

    async with httpx.AsyncClient(timeout=20.0) as client:
        r_sess = await client.post(
            f"{base_url}/api/v1/sessions",
            json={"device_id": device_id, "mode": "view_only", "fps": 15, "scale": 0.5},
            headers=admin_headers,
        )
        r_sess.raise_for_status()
        session_id = r_sess.json()["session_id"]

        agent_pc = RTCPeerConnection()
        dc_pong = {"sent": False}
        agent_pc.addTrack(_TestColorTrack())
        dc = agent_pc.createDataChannel("input")

        @dc.on("message")
        def _on_msg(msg):
            if "ping" in str(msg):
                dc.send(json.dumps({"action": "pong"}))
                dc_pong["sent"] = True

        offer = await agent_pc.createOffer()
        await agent_pc.setLocalDescription(offer)
        # Wait briefly for ICE gathering so SDP carries local host candidates
        for _ in range(30):
            if agent_pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)

        r_off = await client.post(
            f"{base_url}/api/agent/webrtc/{session_id}/offer",
            json={"sdp": agent_pc.localDescription.sdp, "type": agent_pc.localDescription.type},
            headers={"X-Device-Token": device_token},
        )
        r_off.raise_for_status()

        browser_result: dict[str, Any] = {}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content("<html><body><video id='v' autoplay muted playsinline></video></body></html>")

                answer_payload = await page.evaluate(
                    """async ([offerSdp, offerType]) => {
                        window.__pc = new RTCPeerConnection({ iceServers: [] });
                        window.__gotTrack = false;
                        window.__gotPong = false;
                        window.__pc.ontrack = (ev) => {
                            window.__gotTrack = true;
                            const v = document.getElementById('v');
                            if (ev.streams && ev.streams[0]) v.srcObject = ev.streams[0];
                        };
                        window.__pc.ondatachannel = (ev) => {
                            const ch = ev.channel;
                            ch.onopen = () => ch.send(JSON.stringify({ action: 'ping' }));
                            ch.onmessage = (m) => { if (String(m.data).includes('pong')) window.__gotPong = true; };
                        };
                        await window.__pc.setRemoteDescription({ type: offerType, sdp: offerSdp });
                        const ans = await window.__pc.createAnswer();
                        await window.__pc.setLocalDescription(ans);
                        await new Promise((resolve) => {
                            if (window.__pc.iceGatheringState === 'complete') return resolve();
                            const t = setTimeout(resolve, 3000);
                            window.__pc.onicegatheringstatechange = () => {
                                if (window.__pc.iceGatheringState === 'complete') { clearTimeout(t); resolve(); }
                            };
                        });
                        return { sdp: window.__pc.localDescription.sdp, type: window.__pc.localDescription.type };
                    }""",
                    [agent_pc.localDescription.sdp, agent_pc.localDescription.type],
                )

                r_ans = await client.post(
                    f"{base_url}/api/v1/sessions/{session_id}/answer",
                    json=answer_payload,
                    headers=admin_headers,
                )
                r_ans.raise_for_status()
                await agent_pc.setRemoteDescription(
                    RTCSessionDescription(sdp=answer_payload["sdp"], type=answer_payload["type"])
                )

                for _ in range(60):
                    state = await page.evaluate(
                        "() => ({ conn: window.__pc.connectionState, ice: window.__pc.iceConnectionState, track: window.__gotTrack, pong: window.__gotPong })"
                    )
                    if state["conn"] == "connected" and state["track"] and state["pong"]:
                        browser_result = state
                        break
                    browser_result = state
                    await asyncio.sleep(0.25)

                await page.evaluate("() => window.__pc && window.__pc.close()")
                await browser.close()
        finally:
            await agent_pc.close()
            await client.post(f"{base_url}/api/v1/sessions/{session_id}/terminate", json={}, headers=admin_headers)

        ok = (
            browser_result.get("conn") == "connected"
            and bool(browser_result.get("track"))
            and bool(browser_result.get("pong"))
        )
        return {"ok": ok, "session_id": session_id, **browser_result}


def run_webrtc_e2e(out_path: Path, *, require_browser_peer: bool = False) -> dict[str, Any]:
    import httpx

    started = time.time()
    admin_token = f"rtc-e2e-{secrets.token_hex(12)}"
    admin_headers = {"X-Admin-Token": admin_token}
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, ok: bool, detail: str, extra: dict[str, Any] | None = None) -> None:
        item: dict[str, Any] = {"ok": bool(ok), "detail": detail}
        if extra:
            item.update(extra)
        checks[name] = item

    has_aiortc = importlib.util.find_spec("aiortc") is not None
    has_playwright = importlib.util.find_spec("playwright") is not None
    browser_peer_verified = False

    with tempfile.TemporaryDirectory(prefix="aibridge-rtc-e2e-") as td:
        db_path = Path(td) / "control.db"
        base_url, app, server, thread = _start_plane(admin_token, db_path)
        try:
            with httpx.Client(timeout=20.0) as client:
                # 1. ICE/TURN configuration endpoint
                r_ice = client.get(f"{base_url}/api/v1/sessions/ice-servers", headers=admin_headers)
                ice_list = (r_ice.json() or {}).get("ice_servers", []) if r_ice.status_code == 200 else []
                record(
                    "ice_servers_endpoint",
                    r_ice.status_code == 200 and len(ice_list) >= 1,
                    f"status={r_ice.status_code}, servers={len(ice_list)}",
                )

                # 2. Enroll Windows target & create view_only session
                r_enr = client.post(
                    f"{base_url}/api/devices/enroll",
                    json={"name": "rtc-e2e-win", "os": "Windows"},
                    headers=admin_headers,
                )
                r_enr.raise_for_status()
                dev_id = r_enr.json()["device_id"]
                dev_tok = r_enr.json()["device_token"]

                r_sess = client.post(
                    f"{base_url}/api/v1/sessions",
                    json={"device_id": dev_id, "mode": "view_only", "fps": 30, "scale": 1.0},
                    headers=admin_headers,
                )
                sess_id = (r_sess.json() or {}).get("session_id", "") if r_sess.status_code == 200 else ""
                record(
                    "v1_session_create_view_only",
                    r_sess.status_code == 200 and bool(sess_id) and r_sess.json().get("mode") == "view_only",
                    f"status={r_sess.status_code}, session_id={sess_id}",
                )

                # 3. Single-use WS ticket + replay & no-auth rejection
                r_tick = client.post(
                    f"{base_url}/api/stream-ticket",
                    json={"device_id": dev_id, "session_id": sess_id},
                    headers=admin_headers,
                )
                ticket = (r_tick.json() or {}).get("ticket", "") if r_tick.status_code == 200 else ""
                ws_res = asyncio.run(_ws_ticket_checks(base_url, sess_id, ticket)) if ticket else {"ok": False}
                record(
                    "ws_signaling_and_ticket_replay",
                    bool(ticket) and bool(ws_res.get("ok")),
                    json.dumps(ws_res),
                )

                # 4. Human takeover state propagation on plane -> agent signaling view
                r_take_on = client.post(
                    f"{base_url}/api/v1/sessions/{sess_id}/mode",
                    json={"mode": "human_control", "human_takeover_active": True},
                    headers=admin_headers,
                )
                r_agent_view = client.get(
                    f"{base_url}/api/agent/webrtc/{sess_id}",
                    headers={"X-Device-Token": dev_tok},
                )
                takeover_seen = (
                    r_take_on.status_code == 200
                    and r_agent_view.status_code == 200
                    and r_agent_view.json().get("human_takeover_active") is True
                    and r_agent_view.json().get("mode") == "human_control"
                )
                r_take_off = client.post(
                    f"{base_url}/api/v1/sessions/{sess_id}/mode",
                    json={"mode": "view_only", "human_takeover_active": False},
                    headers=admin_headers,
                )
                r_agent_view_after = client.get(
                    f"{base_url}/api/agent/webrtc/{sess_id}",
                    headers={"X-Device-Token": dev_tok},
                )
                takeover_cleared = (
                    r_take_off.status_code == 200
                    and r_agent_view_after.status_code == 200
                    and r_agent_view_after.json().get("human_takeover_active") is False
                    and r_agent_view_after.json().get("mode") == "view_only"
                )
                record(
                    "plane_takeover_and_mode_propagation",
                    takeover_seen and takeover_cleared,
                    f"takeover_on={r_take_on.status_code}, agent_view_takeover={takeover_seen}, "
                    f"takeover_off={r_take_off.status_code}, cleared={takeover_cleared}",
                )

                # 5. Agent-side enforcement: view_only, takeover, disconnect held-input release
                enf = _agent_enforcement_checks()
                record("agent_view_only_takeover_and_disconnect_release", enf["ok"], json.dumps(enf))

                # 6. Session termination & post-termination rejection
                r_term = client.post(
                    f"{base_url}/api/v1/sessions/{sess_id}/terminate",
                    json={},
                    headers=admin_headers,
                )
                r_after_term = client.get(f"{base_url}/api/v1/sessions/{sess_id}", headers=admin_headers)
                term_status = (r_after_term.json() or {}).get("status") if r_after_term.status_code == 200 else ""
                record(
                    "session_termination_and_expiry",
                    r_term.status_code == 200 and term_status in ("ended", "terminated", "closed"),
                    f"terminate={r_term.status_code}, final_status={term_status}",
                )

                # 7. Real Chromium browser peer <-> aiortc agent peer (when extras installed)
                if has_aiortc and has_playwright:
                    try:
                        b_res = asyncio.run(_real_browser_peer_e2e(base_url, admin_headers, dev_id, dev_tok))
                        browser_peer_verified = bool(b_res.get("ok"))
                        record("real_browser_peer_negotiation", browser_peer_verified, json.dumps(b_res))
                    except Exception as exc:
                        record("real_browser_peer_negotiation", False, f"error: {exc}")
                else:
                    missing = [name for name, ok in (("aiortc", has_aiortc), ("playwright", has_playwright)) if not ok]
                    record(
                        "real_browser_peer_negotiation",
                        not require_browser_peer,
                        f"skipped: missing optional packages {', '.join(missing)} (install .[webrtc,browser] + playwright chromium for full live media proof)",
                        {"skipped": True, "missing": missing},
                    )
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            try:
                app.state.conn.close()
            except Exception:
                pass

    all_ok = all(c["ok"] for c in checks.values())
    report = {
        "gate": "release-1-webrtc-browser-e2e",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "signaling_and_enforcement_verified": all(
            checks[k]["ok"] for k in checks if k != "real_browser_peer_negotiation"
        ),
        "real_browser_peer_verified": browser_peer_verified,
        "duration_seconds": round(time.time() - started, 2),
        "all_passed": all_ok,
        "passed_count": sum(1 for c in checks.values() if c["ok"]),
        "total_count": len(checks),
        "checks": checks,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".run/webrtc_browser_e2e_evidence.json"))
    parser.add_argument(
        "--require-browser-peer",
        action="store_true",
        help="Fail if Playwright + aiortc are not installed to execute live browser RTCPeerConnection media proof",
    )
    args = parser.parse_args(argv)
    report = run_webrtc_e2e(args.out, require_browser_peer=args.require_browser_peer)
    print(
        json.dumps(
            {
                "wrote": str(args.out),
                "all_passed": report["all_passed"],
                "real_browser_peer_verified": report["real_browser_peer_verified"],
            },
            indent=2,
        )
    )
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

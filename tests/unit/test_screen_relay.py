"""Tests for vercel-labs/screen-relay (`nsl1`) codec, loopback engine, IDR cache, and WS binary relay."""
from __future__ import annotations

import struct

import pytest
from fastapi.testclient import TestClient

from ai_bridge import screen_relay
from ai_bridge.control import agent_channel
from ai_bridge.control.app import create_app
from ai_bridge.control.desktop_capabilities import build_capability_matrix, device_desktop_capabilities
from ai_bridge.policy import TOOL_TIERS, RiskTier


def test_nsl1_binary_frame_video_ack_cursor_and_sps_codec():
    # High profile Level 3.1 SPS (0x67 0x64 0x00 0x1f) + IDR slice
    annexb = b"\x00\x00\x00\x01\x67\x64\x00\x1f\xac\x00\x00\x00\x01\x65\x88\x84"
    assert screen_relay.extract_h264_codec_string(annexb) == "avc1.64001f"

    raw = screen_relay.encode_nsl1_video_frame(
        annexb,
        width=1920,
        height=1080,
        timestamp_us=123456789,
        is_keyframe=True,
        generation=7,
        seq=42,
    )
    assert raw[:4] == b"NSL1"
    assert len(raw) == screen_relay.NSL1_HEADER_SIZE + screen_relay.NSL1_VIDEO_META_SIZE + len(annexb)

    # Verify exact upstream PROTOCOL.md message types and strict flags <= 1
    assert screen_relay.NSL1_MSG_VIDEO == 1
    assert screen_relay.NSL1_MSG_ACK == 2
    assert screen_relay.NSL1_MSG_INPUT == 3
    assert screen_relay.NSL1_MSG_CONTROL_READY == 4
    assert screen_relay.NSL1_MSG_CONTROL == 5
    assert screen_relay.NSL1_MSG_CURSOR == 6
    assert screen_relay.NSL1_MSG_CLIPBOARD == 7

    frame = screen_relay.decode_nsl1_frame(raw)
    assert frame.version == 1
    assert frame.msg_type == screen_relay.NSL1_MSG_VIDEO
    assert frame.flags == 1  # Strictly 1 for keyframe (never >1)
    assert frame.is_keyframe is True
    assert frame.generation == 7
    assert frame.seq == 42

    w, h, ts, payload_nal = screen_relay.decode_nsl1_video_payload(frame.payload)
    assert (w, h, ts, payload_nal) == (1920, 1080, 123456789, annexb)

    # Viewer wait-time ACK (24 bytes) & bare ACK (20 bytes)
    ack_bare = screen_relay.encode_nsl1_ack(42, generation=7)
    assert len(ack_bare) == 20
    ack_wait = screen_relay.encode_nsl1_ack(42, generation=7, viewer_wait_us=3500)
    assert len(ack_wait) == 24
    ack_frame = screen_relay.decode_nsl1_frame(ack_wait)
    assert ack_frame.msg_type == screen_relay.NSL1_MSG_ACK
    assert struct.unpack(">I", ack_frame.payload)[0] == 3500

    # Hardware cursor sprite (type 6)
    cursor_rgba = b"\xff\x00\x00\xff" * 4  # 2x2 red sprite
    cursor_pkt = screen_relay.encode_nsl1_frame(
        screen_relay.NSL1_MSG_CURSOR,
        struct.pack(">HHHH", 2, 2, 1, 1) + cursor_rgba,
        generation=7,
        seq=0,
    )
    cur_decoded = screen_relay.decode_nsl1_cursor_payload(screen_relay.decode_nsl1_frame(cursor_pkt).payload)
    assert cur_decoded["width"] == 2 and cur_decoded["hot_x"] == 1 and cur_decoded["default_cursor"] is False

    # Reject invalid magic / truncated frame / >8 MiB payload
    with pytest.raises(ValueError, match="magic"):
        screen_relay.decode_nsl1_frame(b"BAD!" + raw[4:])
    with pytest.raises(ValueError, match="too short"):
        screen_relay.decode_nsl1_frame(b"NSL1\x01")
    with pytest.raises(ValueError, match="8 MiB"):
        screen_relay.encode_nsl1_frame(screen_relay.NSL1_MSG_VIDEO, b"\x00" * (screen_relay.NSL1_MAX_PAYLOAD_BYTES + 1))


def test_nsl1_16_byte_input_records_and_upstream_button_hid_semantics():
    # Upstream button codes: 1=left, 2=middle, 3=right
    assert screen_relay.BUTTON_CODE_LEFT == 1
    assert screen_relay.BUTTON_CODE_MIDDLE == 2
    assert screen_relay.BUTTON_CODE_RIGHT == 3

    batch = screen_relay.json_input_to_nsl1_records(
        {"action": "click", "x": 0.5, "y": 0.25, "button": "right"},
    )
    assert len(batch) == 3 * screen_relay.NSL1_INPUT_RECORD_SIZE
    decoded = screen_relay.decode_input_records(batch)
    assert len(decoded) == 3
    assert decoded[0]["type"] == screen_relay.INPUT_TYPE_POINTER_MOVE
    assert decoded[1]["type"] == screen_relay.INPUT_TYPE_BUTTON_DOWN
    assert decoded[1]["code"] == 3  # Right button = 3
    assert decoded[2]["type"] == screen_relay.INPUT_TYPE_BUTTON_UP
    assert decoded[2]["code"] == 3
    assert abs(decoded[1]["x_norm"] - 0.5) < 0.001
    assert abs(decoded[1]["y_norm"] - 0.25) < 0.001

    # Wheel delta is quantized to multiples of 120 in [-1200, 1200]
    wheel_batch = screen_relay.json_input_to_nsl1_records({"action": "wheel", "delta": -75, "x": 0.2, "y": 0.3})
    wheel_rec = screen_relay.decode_input_records(wheel_batch)[0]
    assert wheel_rec["type"] == screen_relay.INPUT_TYPE_WHEEL
    assert wheel_rec["value"] == -120

    # Text events have zero masks and encode Unicode codepoints cleanly
    txt_batch = screen_relay.json_input_to_nsl1_records({"action": "type", "text": "Hi"})
    txt_recs = screen_relay.decode_input_records(txt_batch)
    assert [r["type"] for r in txt_recs] == [screen_relay.INPUT_TYPE_TEXT, screen_relay.INPUT_TYPE_TEXT]
    assert [chr(r["value"]) for r in txt_recs] == ["H", "i"]
    assert all(r["buttons"] == 0 and r["modifiers"] == 0 for r in txt_recs)

    # HID usage mapping
    assert screen_relay.hid_usage_to_key_name(4) == "a"
    assert screen_relay.hid_usage_to_key_name(40) == "enter"
    assert screen_relay.hid_usage_to_key_name(224) == "ctrl"

    with pytest.raises(ValueError, match="256"):
        screen_relay.encode_input_batch([b"\x00" * 16] * 257)


def test_screen_relay_loopback_invariant_token_and_policy_tiers(tmp_path):
    eng = screen_relay.ScreenRelayEngine()
    assert eng.host == "127.0.0.1"
    assert eng.base_url.startswith("http://127.0.0.1:")
    assert len(eng.token) == 64 and all(c in "0123456789abcdef" for c in eng.token)

    tok_file = screen_relay._write_private_token_file(tmp_path, eng.token)
    assert tok_file.read_text(encoding="ascii").strip() == eng.token
    viewer_dir = screen_relay._ensure_viewer_stub_dir(tmp_path, "/nonexistent/relay-engine")
    assert (viewer_dir / "index.html").is_file() and (viewer_dir / "viewer.js").is_file()

    assert TOOL_TIERS["start_screen_relay_stream"] == RiskTier.SENSITIVE
    assert TOOL_TIERS["stop_screen_relay_stream"] == RiskTier.SENSITIVE

    st = screen_relay.describe_screen_relay_status()
    assert st["protocol"] == "nsl1"
    assert st["loopback_only"] is True
    assert st["webcodecs"] is True

    mac_caps = device_desktop_capabilities({"id": "mac-1", "os": "Darwin", "status": "online", "revoked": 0}, {"desktop_enabled": False})
    assert mac_caps["webrtc_stream_supported"] is False
    assert mac_caps["screen_relay_supported"] is True

    linux_caps = device_desktop_capabilities({"id": "lin-1", "os": "Linux", "status": "online", "revoked": 0}, {"desktop_enabled": False})
    assert linux_caps["webrtc_stream_supported"] is False
    assert linux_caps["screen_relay_supported"] is True

    matrix = build_capability_matrix(include_host=False)
    assert matrix["screen_relay"]["protocol"] == "nsl1"
    assert set(matrix["screen_relay"]["supported_platforms"]) == {"Windows", "Darwin", "Linux"}


def test_screen_relay_cross_os_session_idr_cache_and_ws_binary_policy_gate(tmp_path, monkeypatch):
    """macOS/Linux devices stream NSL1 video over WS with instant IDR cache for late viewers & View-Only lock."""
    monkeypatch.setenv("AI_BRIDGE_CSRF_DISABLED", "1")
    app = create_app(admin_token="relay-admin-tok", db_path=tmp_path / "relay.db")
    client = TestClient(app)
    admin_hdr = {"X-Admin-Token": "relay-admin-tok"}

    enroll = client.post(
        "/api/devices/enroll",
        headers=admin_hdr,
        json={"name": "mac-studio", "hostname": "mac-studio.local", "os": "Darwin"},
    )
    assert enroll.status_code == 200
    dev = enroll.json()
    dev_id = dev["device_id"]
    dev_tok = dev["device_token"]

    pol = client.put(
        f"/api/devices/{dev_id}/policy",
        headers=admin_hdr,
        json={"allow_writes": True, "allow_tier3": True, "desktop_enabled": True, "max_tier_unattended": 3},
    )
    assert pol.status_code == 200

    # WebRTC remains rejected on macOS
    webrtc_res = client.post(
        "/api/v1/sessions",
        headers=admin_hdr,
        json={"device_id": dev_id, "mode": "view_only", "transport": "webrtc"},
    )
    assert webrtc_res.status_code == 400

    # Screen Relay succeeds on macOS
    sr_res = client.post(
        "/api/v1/sessions",
        headers=admin_hdr,
        json={"device_id": dev_id, "mode": "view_only", "transport": "screen_relay", "fps": 60},
    )
    assert sr_res.status_code == 200
    sess = sr_res.json()
    sid = sess["session_id"]
    assert sess["transport"] == "screen_relay"
    assert sess["protocol"] == "nsl1"

    t1 = client.post("/api/stream-ticket", headers=admin_hdr, json={"device_id": dev_id, "session_id": sid}).json()["ticket"]
    t2 = client.post("/api/stream-ticket", headers=admin_hdr, json={"device_id": dev_id, "session_id": sid}).json()["ticket"]

    video_pkt = screen_relay.encode_nsl1_video_frame(
        b"\x00\x00\x00\x01\x67\x42\xe0\x1f\x00\x00\x00\x01\x65\x88",
        width=1280,
        height=720,
        timestamp_us=1000,
        is_keyframe=True,
        seq=1,
    )
    click_pkt = screen_relay.encode_nsl1_frame(
        screen_relay.NSL1_MSG_INPUT,
        screen_relay.json_input_to_nsl1_records({"action": "click", "x": 0.5, "y": 0.5}),
        seq=0,
    )

    with client.websocket_connect(f"/api/v1/ws/sessions/{sid}", headers={"X-Device-Token": dev_tok}) as agent_ws:
        assert agent_ws.receive_json()["type"] == "session_snapshot"
        with client.websocket_connect(f"/api/v1/ws/sessions/{sid}?ticket={t1}") as v1_ws:
            assert v1_ws.receive_json()["type"] == "session_snapshot"

            # 1. Agent -> Viewer binary NSL1 video broadcast
            agent_ws.send_bytes(video_pkt)
            got_video = v1_ws.receive_bytes()
            assert got_video == video_pkt

            # 2. View-Only mode blocks binary NSL1 input from viewer
            v1_ws.send_bytes(click_pkt)
            denied = v1_ws.receive_json()
            assert denied["type"] == "input_denied"
            assert denied["reason"] == "session_is_view_only"

            # 3. Upgrade session to ai_assisted -> input reaches agent, second viewer gets instant cached IDR + 4003 busy
            mode_res = client.patch(
                f"/api/v1/sessions/{sid}/mode",
                headers=admin_hdr,
                json={"mode": "ai_assisted", "human_takeover_active": False},
            )
            assert mode_res.status_code == 200
            assert v1_ws.receive_json()["type"] == "session_updated"
            assert agent_ws.receive_json()["type"] == "session_updated"

            v1_ws.send_bytes(click_pkt)
            assert agent_ws.receive_bytes() == click_pkt

            with client.websocket_connect(f"/api/v1/ws/sessions/{sid}?ticket={t2}") as v2_ws:
                assert v2_ws.receive_json()["type"] == "session_snapshot"
                # Late-joining viewer v2 immediately receives the cached IDR keyframe!
                assert v2_ws.receive_bytes() == video_pkt

                v2_ws.send_bytes(click_pkt)
                busy = v2_ws.receive_json()
                assert busy["type"] == "control_denied"
                assert busy["code"] == screen_relay.NSL1_CLOSE_BUSY

                # Explicit claim_control revokes v1 (4001 taken) and grants v2
                v2_ws.send_json({"type": "claim_control"})
                assert v2_ws.receive_json()["type"] == "control_granted"
                revoked = v1_ws.receive_json()
                assert revoked["type"] == "control_revoked"
                assert revoked["code"] == screen_relay.NSL1_CLOSE_TAKEN


@pytest.mark.anyio
async def test_agent_channel_cross_worker_nsl1_binary_fanout():
    """Verify cross-worker Redis pub/sub message handler decodes NSL1 binary frames from peer workers."""
    import base64
    import json

    received: list[tuple[str, bytes, str]] = []

    async def _handler(session_id: str, raw_bytes: bytes, *, target_role: str = "viewer") -> None:
        received.append((session_id, raw_bytes, target_role))

    agent_channel.register_nsl1_binary_handler(_handler)
    sample = screen_relay.encode_nsl1_ack(9, generation=2, viewer_wait_us=1200)
    envelope = json.dumps({
        "origin": "other_worker_id",
        "role": "agent",
        "b64": base64.b64encode(sample).decode("ascii"),
    })
    await agent_channel._handle_redis_message("aibridge:nsl1:sess_123", envelope)
    assert len(received) == 1
    assert received[0] == ("sess_123", sample, "agent")

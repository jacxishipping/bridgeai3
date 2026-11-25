"""Tests for vercel-labs/screen-relay (`nsl1`) codec, loopback engine, and WS binary relay."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_bridge import screen_relay
from ai_bridge.control.app import create_app
from ai_bridge.control.desktop_capabilities import build_capability_matrix, device_desktop_capabilities
from ai_bridge.policy import TOOL_TIERS, RiskTier


def test_nsl1_binary_frame_and_video_codec_roundtrip():
    annexb = b"\x00\x00\x00\x01\x67\x42\x00\x1f\x00\x00\x00\x01\x65\x88\x84"
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

    frame = screen_relay.decode_nsl1_frame(raw)
    assert frame.version == 1
    assert frame.msg_type == screen_relay.NSL1_MSG_VIDEO
    assert frame.is_keyframe is True
    assert frame.generation == 7
    assert frame.seq == 42

    w, h, ts, payload_nal = screen_relay.decode_nsl1_video_payload(frame.payload)
    assert (w, h, ts, payload_nal) == (1920, 1080, 123456789, annexb)

    # Reject invalid magic / truncated frame / >8 MiB payload
    with pytest.raises(ValueError, match="magic"):
        screen_relay.decode_nsl1_frame(b"BAD!" + raw[4:])
    with pytest.raises(ValueError, match="too short"):
        screen_relay.decode_nsl1_frame(b"NSL1\x01")
    with pytest.raises(ValueError, match="8 MiB"):
        screen_relay.encode_nsl1_frame(screen_relay.NSL1_MSG_VIDEO, b"\x00" * (screen_relay.NSL1_MAX_PAYLOAD_BYTES + 1))


def test_nsl1_16_byte_input_records_and_json_translation():
    batch = screen_relay.json_input_to_nsl1_records(
        {"action": "click", "x": 0.5, "y": 0.25, "button": "right"},
        seq=9,
    )
    assert len(batch) == 3 * screen_relay.NSL1_INPUT_RECORD_SIZE
    decoded = screen_relay.decode_input_records(batch)
    assert len(decoded) == 3
    assert decoded[0]["type"] == screen_relay.INPUT_TYPE_POINTER_MOVE
    assert decoded[1]["type"] == screen_relay.INPUT_TYPE_POINTER_BUTTON
    assert decoded[1]["code"] == screen_relay.BUTTON_CODE_RIGHT
    assert decoded[1]["value"] == 1
    assert decoded[2]["value"] == 0
    assert abs(decoded[1]["x_norm"] - 0.5) < 0.001
    assert abs(decoded[1]["y_norm"] - 0.25) < 0.001

    txt_batch = screen_relay.json_input_to_nsl1_records({"action": "type", "text": "Hi"}, seq=1)
    txt_recs = screen_relay.decode_input_records(txt_batch)
    assert [chr(r["value"]) for r in txt_recs] == ["H", "i"]

    with pytest.raises(ValueError, match="256"):
        screen_relay.encode_input_batch([b"\x00" * 16] * 257)


def test_screen_relay_loopback_invariant_and_policy_tiers():
    eng = screen_relay.ScreenRelayEngine()
    assert eng.host == "127.0.0.1"
    assert eng.base_url.startswith("http://127.0.0.1:")
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


def test_screen_relay_cross_os_session_and_ws_binary_policy_gate(tmp_path, monkeypatch):
    """macOS/Linux devices can create screen_relay sessions and stream NSL1 video over WS with View-Only input lock."""
    monkeypatch.setenv("AI_BRIDGE_CSRF_DISABLED", "1")
    app = create_app(admin_token="relay-admin-tok", db_path=tmp_path / "relay.db")
    client = TestClient(app)
    admin_hdr = {"X-Admin-Token": "relay-admin-tok"}

    # Enroll a macOS device (WebRTC rejected, Screen Relay allowed)
    enroll = client.post(
        "/api/devices/enroll",
        headers=admin_hdr,
        json={"name": "mac-studio", "hostname": "mac-studio.local", "os": "Darwin"},
    )
    assert enroll.status_code == 200
    dev = enroll.json()
    dev_id = dev["device_id"]
    dev_tok = dev["device_token"]

    # Enable desktop_enabled policy on this device
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

    # Screen Relay succeeds on macOS and queues start_screen_relay_stream
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

    # Mint single-use stream tickets for viewer 1 and viewer 2
    t1 = client.post("/api/stream-ticket", headers=admin_hdr, json={"device_id": dev_id, "session_id": sid}).json()["ticket"]
    t2 = client.post("/api/stream-ticket", headers=admin_hdr, json={"device_id": dev_id, "session_id": sid}).json()["ticket"]

    video_pkt = screen_relay.encode_nsl1_video_frame(
        b"\x00\x00\x00\x01\x65\x88",
        width=1280,
        height=720,
        timestamp_us=1000,
        is_keyframe=True,
        seq=1,
    )
    click_pkt = screen_relay.encode_nsl1_frame(
        screen_relay.NSL1_MSG_INPUT,
        screen_relay.json_input_to_nsl1_records({"action": "click", "x": 0.5, "y": 0.5}, seq=1),
        seq=1,
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

            # 3. Upgrade session to ai_assisted -> input reaches agent, second viewer gets 4003 busy
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


"""Unit and policy tests for the Hybrid Desktop Accessibility & Visual Fallback Engine."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from ai_bridge import hybrid_desktop
from ai_bridge.control import dispatch, role_catalog
from ai_bridge.plane_control_flags import set_emergency_stop
from ai_bridge.policy import TOOL_TIERS, Policy, RiskTier, decide


def _open_desktop_policy(**overrides) -> Policy:
    base = dict(
        allow_writes=True,
        allow_tier3=True,
        desktop_enabled=True,
        max_tier_unattended=3,
    )
    base.update(overrides)
    return Policy(**base)


def _sample_ui_elements(*_args, **_kwargs) -> dict:
    return {
        "ok": True,
        "count": 4,
        "screen_width": 1920,
        "screen_height": 1080,
        "grounding_source": "uia",
        "elements": [
            {
                "id": 1,
                "label": "Submit Order",
                "type": "Button",
                "app": "ContosoApp",
                "window_title": "Contoso Checkout",
                "x": 100,
                "y": 200,
                "width": 120,
                "height": 40,
                "enabled": True,
            },
            {
                "id": 2,
                "label": "Account Password",
                "type": "Edit",
                "app": "ContosoApp",
                "window_title": "Contoso Checkout",
                "value": "s3cr3t-pw!",
                "is_password": True,
                "x": 100,
                "y": 140,
                "width": 200,
                "height": 30,
                "enabled": True,
            },
            {
                "id": 3,
                "label": "Remember Me",
                "type": "CheckBox",
                "app": "ContosoApp",
                "window_title": "Contoso Checkout",
                "checked": True,
                "x": 100,
                "y": 175,
                "width": 20,
                "height": 20,
                "enabled": True,
            },
            {
                "id": 4,
                "label": "Forbidden Terminal",
                "type": "Button",
                "app": "cmd.exe",
                "window_title": "Command Prompt",
                "x": 500,
                "y": 500,
                "width": 100,
                "height": 30,
                "enabled": True,
            },
        ],
    }


def test_policy_tiers_and_desktop_gate():
    read_tools = (
        "desktop_a11y_status",
        "desktop_a11y_permissions",
        "desktop_a11y_snapshot",
        "desktop_a11y_find",
        "desktop_a11y_get_value",
        "desktop_a11y_list_surfaces",
        "desktop_a11y_list_apps",
        "desktop_a11y_wait",
    )
    for name in read_tools:
        assert TOOL_TIERS[name] == RiskTier.READ_ONLY
        assert decide(Policy(), name).allowed is True

    for mut_tool in ("desktop_a11y_act", "desktop_a11y_batch"):
        assert TOOL_TIERS[mut_tool] == RiskTier.SENSITIVE
        # Denied when desktop_enabled=False
        d_off = decide(Policy(allow_tier3=True, desktop_enabled=False), mut_tool)
        assert d_off.allowed is False
        assert "Desktop automation is not enabled" in d_off.reason
        # Requires approval when desktop_enabled=True but allow_tier3=False
        d_need = decide(Policy(allow_tier3=False, desktop_enabled=True), mut_tool)
        assert d_need.allowed is False and d_need.needs_approval is True
        # Allowed when desktop_enabled=True and allow_tier3=True
        d_ok = decide(_open_desktop_policy(), mut_tool)
        assert d_ok.allowed is True
        # Allowed with explicit human approval when desktop_enabled=True
        d_app = decide(Policy(allow_tier3=False, desktop_enabled=True), mut_tool, approval_granted=True)
        assert d_app.allowed is True

    # Role catalog exposes hybrid a11y tools to computer_operator
    co_tools = set(role_catalog.ROLES["computer_operator"]["allowed_tools"])
    assert {"desktop_a11y_snapshot", "desktop_a11y_act", "desktop_a11y_batch", "desktop_a11y_find"} <= co_tools


def test_snapshot_redacts_passwords_and_enforces_stale_ref(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_A11Y_BACKEND", "native")
    monkeypatch.setattr("ai_bridge.tools.desktop_api.desktop_get_ui_elements", _sample_ui_elements)
    hybrid_desktop._STORE.clear()

    snap1 = hybrid_desktop.a11y_snapshot(compact=False, policy=_open_desktop_policy())
    assert snap1["ok"] is True
    assert snap1["backend"] == "native_bridge"
    assert snap1["ref_count"] == 4
    pw_el = next(e for e in snap1["elements"] if e["name"] == "Account Password")
    assert pw_el["value"] == "[REDACTED]"
    assert pw_el["redacted"] is True
    assert "s3cr3t-pw!" not in json.dumps(snap1)

    # Reading value by active ref succeeds and keeps password redacted
    val_res = hybrid_desktop.a11y_get_value(pw_el["ref"], policy=_open_desktop_policy())
    assert val_res["ok"] is True
    assert val_res["value"] == "[REDACTED]"
    assert val_res["redacted"] is True

    # Taking a new snapshot invalidates previous snapshot refs -> STALE_REF
    snap2 = hybrid_desktop.a11y_snapshot(compact=True, policy=_open_desktop_policy())
    assert snap2["snapshot_id"] != snap1["snapshot_id"]
    stale_val = hybrid_desktop.a11y_get_value(pw_el["ref"], policy=_open_desktop_policy())
    assert stale_val["ok"] is False
    assert stale_val["error_code"] == "STALE_REF"

    stale_act = hybrid_desktop.a11y_act(
        pw_el["ref"], action="click", policy=_open_desktop_policy(), approval_granted=True
    )
    assert stale_act["ok"] is False
    assert stale_act["error_code"] == "STALE_REF"


def test_allowed_apps_filtering_and_refusal(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_A11Y_BACKEND", "native")
    monkeypatch.setattr("ai_bridge.tools.desktop_api.desktop_get_ui_elements", _sample_ui_elements)
    restricted = _open_desktop_policy(allowed_apps=frozenset({"ContosoApp"}))

    snap = hybrid_desktop.a11y_snapshot(policy=restricted)
    names = [e["name"] for e in snap["elements"]]
    assert "Submit Order" in names
    assert "Forbidden Terminal" not in names

    with pytest.raises(PermissionError, match="allowed_apps"):
        hybrid_desktop.a11y_snapshot(app="cmd.exe", policy=restricted)


def test_idempotent_actions_visual_fallback_and_emergency_stop(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_A11Y_BACKEND", "native")
    monkeypatch.setattr("ai_bridge.tools.desktop_api.desktop_get_ui_elements", _sample_ui_elements)
    set_emergency_stop(False)

    actions_seen: list[tuple] = []

    def _fake_ui_action(action, **kw):
        actions_seen.append((action, kw))
        return {"ok": True, "action": action}

    monkeypatch.setattr("ai_bridge.tools.desktop.perform_approved_ui_action", _fake_ui_action)

    pol = _open_desktop_policy()
    snap = hybrid_desktop.a11y_snapshot(policy=pol)
    btn = next(e for e in snap["elements"] if e["name"] == "Submit Order")
    chk = next(e for e in snap["elements"] if e["name"] == "Remember Me")

    # Idempotent check on already-checked box short-circuits cleanly without clicking
    noop = hybrid_desktop.a11y_act(chk["ref"], action="check", policy=pol)
    assert noop["ok"] is True and noop.get("noop") is True
    assert len(actions_seen) == 0

    # Click dispatches at bounding-box center via visual fallback
    clicked = hybrid_desktop.a11y_act(btn["ref"], action="click", policy=pol)
    assert clicked["ok"] is True
    assert clicked["coords"] == {"x": 160, "y": 220}
    assert actions_seen[-1][0] == "click"

    # Emergency stop blocks mutations immediately
    set_emergency_stop(True)
    try:
        blocked = hybrid_desktop.a11y_act(btn["ref"], action="click", policy=pol)
        assert blocked["ok"] is False
        assert blocked["error_code"] == "EMERGENCY_STOP_ACTIVE"
    finally:
        set_emergency_stop(False)


def test_batch_find_wait_and_dispatch_wiring(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_A11Y_BACKEND", "native")
    monkeypatch.setattr("ai_bridge.tools.desktop_api.desktop_get_ui_elements", _sample_ui_elements)
    monkeypatch.setattr(
        "ai_bridge.tools.desktop.perform_approved_ui_action",
        lambda action, **kw: {"ok": True, "action": action},
    )
    set_emergency_stop(False)
    pol = _open_desktop_policy()

    found = dispatch.dispatch("desktop_a11y_find", {"query": "Submit", "role": "Button"}, policy=pol)
    assert found["ok"] is True and found["count"] == 1
    ref = found["matches"][0]["ref"]

    waited = dispatch.dispatch("desktop_a11y_wait", {"condition": "exists", "query": "Submit", "timeout_seconds": 1.0}, policy=pol)
    assert waited["ok"] is True and waited["match"]["ref"]
    fresh_ref = waited["match"]["ref"]
    assert fresh_ref != ref  # wait captured a newer snapshot generation

    batch_res = dispatch.dispatch(
        "desktop_a11y_batch",
        {
            "steps": [
                {"action": "hover", "ref": fresh_ref},
                {"action": "click", "ref": fresh_ref},
            ]
        },
        policy=pol,
    )
    assert batch_res["ok"] is True
    assert batch_res["steps_executed"] == 2

    # Oversized batch rejected
    with pytest.raises(ValueError, match="maximum of 25"):
        hybrid_desktop.a11y_batch([{"action": "click", "ref": fresh_ref}] * 26, policy=pol)

    status = dispatch.dispatch("desktop_a11y_status", {}, policy=pol)
    assert status["ok"] is True and status["native_fallback_available"] is True


def test_cli_backend_pipes_text_via_stdin_never_argv(monkeypatch, tmp_path):
    fake_bin = tmp_path / "agent-desktop"
    fake_bin.write_text("", encoding="utf-8")
    monkeypatch.setenv("AI_BRIDGE_A11Y_BACKEND", "cli")
    monkeypatch.setenv("AI_BRIDGE_AGENT_DESKTOP_BIN", str(fake_bin))

    calls: list[tuple[list[str], str | None]] = []

    def _fake_run(argv, input=None, **kwargs):
        assert kwargs.get("shell") is False
        calls.append((list(argv), input))
        if "snapshot" in argv:
            out = json.dumps({
                "ok": True,
                "data": {
                    "elements": [
                        {"ref": "@s1234abcd:e1", "role": "TextField", "name": "Search", "app": "Browser", "bounds": {"x": 10, "y": 20, "width": 80, "height": 24}},
                    ]
                },
            })
            return MagicMock(returncode=0, stdout=out, stderr="")
        return MagicMock(returncode=0, stdout=json.dumps({"ok": True, "data": {"typed": True}}), stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    pol = _open_desktop_policy()

    snap = hybrid_desktop.a11y_snapshot(app="Browser", policy=pol)
    assert snap["ok"] is True
    assert snap["backend"] == "agent_desktop_cli"
    ref = snap["elements"][0]["ref"]

    res = hybrid_desktop.a11y_act(ref, action="fill", text="hello-secret-payload", policy=pol)
    assert res["ok"] is True
    assert res["backend"] == "agent_desktop_cli"
    assert res["headless"] is True
    act_argv, act_stdin = calls[-1]
    assert "--json" not in act_argv
    assert "--stdin" in act_argv
    assert "hello-secret-payload" not in " ".join(act_argv)
    assert act_stdin == "hello-secret-payload"


def test_v26_contract_sessions_short_refs_errors_and_batch_prevalidation(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_A11Y_BACKEND", "native")
    monkeypatch.setattr("ai_bridge.tools.desktop_api.desktop_get_ui_elements", _sample_ui_elements)
    set_emergency_stop(False)
    pol = _open_desktop_policy()

    # Short @e1 and session-qualified @s1234:e1 refs are both valid
    assert hybrid_desktop._validate_ref("@e1") == "@e1"
    assert hybrid_desktop._validate_ref("@s1234abcd:e1") == "@s1234abcd:e1"

    # Separate session namespaces do not invalidate each other
    s_a = hybrid_desktop.a11y_snapshot(session_id="agent-a", interactive_only=True, policy=pol)
    s_b = hybrid_desktop.a11y_snapshot(session_id="agent-b", policy=pol)
    ref_a = s_a["elements"][0]["ref"]
    assert hybrid_desktop.a11y_get_value(ref_a, session_id="agent-a", policy=pol)["ok"] is True
    # Wrong session returns v2.6 structured STALE_REF with recovery/disposition
    wrong_sess = hybrid_desktop.a11y_get_value(ref_a, session_id="agent-b", policy=pol)
    assert wrong_sess["ok"] is False
    assert wrong_sess["error_code"] == "STALE_REF"
    assert wrong_sess["error_detail"]["recovery"]["retryable"] is True
    assert wrong_sess["error_detail"]["disposition"] == {"delivery": "not_delivered", "retry": "safe"}
    assert s_b["session_id"] == "agent-b"

    # Batch pre-validates ALL steps before running step 0
    executed = []
    monkeypatch.setattr(
        "ai_bridge.tools.desktop.perform_approved_ui_action",
        lambda action, **kw: executed.append(action) or {"ok": True},
    )
    with pytest.raises(ValueError, match="Unsupported accessibility action"):
        hybrid_desktop.a11y_batch(
            [
                {"action": "click", "ref": ref_a},
                {"action": "not_a_real_action", "ref": ref_a},
            ],
            session_id="agent-a",
            policy=pol,
        )
    assert executed == []


def test_ffi_bridge_execute_by_ref_and_persistent_session(monkeypatch):
    """Layer 0 C FFI bridge reuses per-session AdAdapter* handles and dispatches ad_execute_by_ref."""
    import ctypes
    import json as _json

    calls = {"init": 0, "create": 0, "destroy": 0, "free": 0, "exec": []}

    class _FakeFn:
        def __init__(self, impl):
            self._impl = impl
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self._impl(*args)

    _buffers: list[ctypes.Array] = []

    def _mk_cstr(obj: dict) -> int:
        buf = ctypes.create_string_buffer(_json.dumps(obj).encode("utf-8"))
        _buffers.append(buf)
        return ctypes.addressof(buf)

    class _FakeCDLL:
        def __init__(self, _path: str):
            self.ad_init = _FakeFn(self._init)
            self.ad_adapter_create_with_session = _FakeFn(self._create)
            self.ad_adapter_destroy = _FakeFn(self._destroy)
            self.ad_free_string = _FakeFn(self._free)
            self.ad_snapshot = _FakeFn(self._snapshot)
            self.ad_execute_by_ref = _FakeFn(self._exec_by_ref)

        def _init(self, abi_major):
            calls["init"] += 1
            return 0 if abi_major == hybrid_desktop._FfiBridge.ABI_MAJOR else -1

        def _create(self, _sess_bytes):
            calls["create"] += 1
            return 0x1000 + calls["create"]

        def _destroy(self, _ptr):
            calls["destroy"] += 1

        def _free(self, _ptr):
            calls["free"] += 1

        def _snapshot(self, _adapter, _app, _flags, _depth, _skeleton, _compact, out_ptr_ref):
            out_ptr_ref._obj.value = _mk_cstr(
                {"ok": True, "data": {"elements": [{"ref": "@e1", "role": "button", "name": "Deploy"}]}}
            )
            return 0

        def _exec_by_ref(self, adapter, ref_bytes, action_ptr, flags, out_ptr_ref):
            act = action_ptr._obj
            calls["exec"].append(
                {
                    "adapter": adapter,
                    "ref": ref_bytes.decode("utf-8"),
                    "kind": act.kind,
                    "text": act.text.decode("utf-8") if act.text else None,
                    "flags": flags,
                }
            )
            out_ptr_ref._obj.value = _mk_cstr({"ok": True, "ref": ref_bytes.decode("utf-8"), "executed": True})
            return 0

    monkeypatch.setattr(ctypes, "CDLL", _FakeCDLL)
    bridge = hybrid_desktop._FfiBridge("fake_libagent_desktop_ffi.dll")
    try:
        snap = bridge.snapshot(app="Notepad", session_id="s-ffi")
        assert snap["ok"] is True
        res = bridge.execute_by_ref(
            session_id="s-ffi",
            ref="@e1",
            action="fill",
            text="hello",
            headed=True,
        )
        assert res["ok"] is True
        # Same persistent adapter reused for snapshot + execute_by_ref
        assert calls["init"] == 1
        assert calls["create"] == 1
        assert len(calls["exec"]) == 1
        assert calls["exec"][0]["ref"] == "@e1"
        assert calls["exec"][0]["kind"] == hybrid_desktop._AD_ACTION_KIND["fill"]
        assert calls["exec"][0]["text"] == "hello"
        assert calls["exec"][0]["flags"] == hybrid_desktop._AD_FLAG_HEADED
        assert calls["free"] == 2
    finally:
        bridge.close_session("s-ffi")
    assert calls["destroy"] == 1


def test_desktop_audio_resampler_and_pyaudiowpatch_probe(monkeypatch):
    """WASAPI audio resampler converts mono/44.1kHz to 48kHz stereo s16le and probes pyaudiowpatch."""
    import platform as _platform
    import struct
    import types

    from ai_bridge import desktop_audio

    # 1. Resample mono 44.1kHz (441 samples = 10ms) -> stereo 48kHz (480 frames)
    mono_pcm = struct.pack("<" + "h" * 441, *([1000] * 441))
    out = desktop_audio._resample_s16_stereo(mono_pcm, src_rate=44100, src_channels=1, dst_rate=48000, dst_frames=480)
    assert len(out) == 480 * 2 * 2
    first_l, first_r = struct.unpack_from("<hh", out, 0)
    assert first_l == 1000 and first_r == 1000

    # 2. Mock pyaudiowpatch probe on Windows
    class _FakePA:
        def get_default_wasapi_loopback(self):
            return {"name": "Speakers (Realtek) [Loopback]", "defaultSampleRate": 48000, "maxInputChannels": 2}

        def terminate(self):
            pass

    fake_mod = types.SimpleNamespace(PyAudio=_FakePA)
    monkeypatch.setattr(desktop_audio, "_PROBE_CACHE", None)
    monkeypatch.setattr(_platform, "system", lambda: "Windows")
    monkeypatch.setitem(__import__("sys").modules, "pyaudiowpatch", fake_mod)
    probe = desktop_audio.probe_wasapi_loopback()
    assert probe["available"] is True
    assert probe["backend"] == "pyaudiowpatch_wasapi_loopback"
    assert "Speakers" in probe["device"]

    monkeypatch.setenv("AI_BRIDGE_DESKTOP_AUDIO", "1")
    st = desktop_audio.get_desktop_audio_status()
    assert st["enabled"] is True
    assert st["live_loopback_available"] is True
    assert st["consent_required"] is True


def test_webrtc_streamer_live_monitor_switch_over_datachannel():
    """Python AgentStreamer handles switch_monitor DataChannel message without tearing down PeerConnection."""
    from ai_bridge import webrtc_stream

    class _FakeTrack:
        def __init__(self):
            self.switched_to = None

        def switch_monitor(self, idx: int) -> int:
            self.switched_to = idx
            return idx

    class _FakeDC:
        readyState = "open"

        def __init__(self):
            self.sent = []

        def send(self, msg: str):
            self.sent.append(msg)

    sess = {"id": "mon-sw-1", "mode": "view_only", "monitor_index": 0}
    streamer = webrtc_stream.AgentStreamer("mon-sw-1", webrtc_stream.DictSignaling(sess))
    streamer.video_track = _FakeTrack()
    streamer.input_channel = _FakeDC()

    res = streamer._handle_input_payload('{"type":"switch_monitor","monitor_index":2}')
    assert res["ok"] is True
    assert res["monitor_index"] == 2
    assert streamer.monitor_index == 2
    assert streamer.video_track.switched_to == 2
    assert sess["monitor_index"] == 2
    assert any('"monitor_switched"' in m and '"monitor_index": 2' in m for m in streamer.input_channel.sent)


def test_control_plane_a11y_inspector_endpoints_auth_and_policy_gate(tmp_path, monkeypatch):
    """GET /desktop/a11y/snapshot and POST /desktop/a11y/act enforce RBAC and View-Only policy lock."""
    from fastapi.testclient import TestClient

    from ai_bridge.control.app import create_app

    monkeypatch.setenv("AI_BRIDGE_CSRF_DISABLED", "1")
    app = create_app(admin_token="a11y-admin-tok", db_path=tmp_path / "a11y.db")
    client = TestClient(app)
    admin_hdr = {"X-Admin-Token": "a11y-admin-tok"}

    monkeypatch.setenv("AI_BRIDGE_A11Y_BACKEND", "native_bridge")
    monkeypatch.setattr(
        "ai_bridge.tools.desktop_api.desktop_get_ui_elements",
        lambda **kw: {
            "ok": True,
            "grounding_source": "uia",
            "screen_width": 1920,
            "screen_height": 1080,
            "elements": [
                {"label": "Save", "type": "button", "x": 100, "y": 50, "width": 80, "height": 30},
            ],
        },
    )

    # Unauthenticated rejected
    assert client.get("/api/devices/local/desktop/a11y/snapshot").status_code == 401

    # Authenticated snapshot succeeds and returns @ref with bounds
    snap_resp = client.get("/api/devices/local/desktop/a11y/snapshot", headers=admin_hdr)
    assert snap_resp.status_code == 200
    snap_body = snap_resp.json()
    assert snap_body["ok"] is True
    assert len(snap_body["elements"]) == 1
    ref_id = snap_body["elements"][0]["ref"]
    assert snap_body["elements"][0]["bounds"] == {"x": 100, "y": 50, "width": 80, "height": 30}

    # Missing ref returns 400
    assert client.post("/api/devices/local/desktop/a11y/act", headers=admin_hdr, json={"action": "click"}).status_code == 400

    # Policy desktop_enabled=False blocks POST /a11y/act (403 View-Only enforced)
    pol_res = client.post(
        "/api/policies",
        headers=admin_hdr,
        json={"allow_writes": True, "allow_tier3": False, "desktop_enabled": False, "max_tier_unattended": 2},
    )
    assert pol_res.status_code == 200
    blocked = client.post(
        "/api/devices/local/desktop/a11y/act",
        headers=admin_hdr,
        json={"ref": ref_id, "action": "click"},
    )
    assert blocked.status_code == 403
    assert "disabled by active policy" in blocked.json()["detail"].lower()

    # Enabling desktop_enabled + allow_tier3 allows the @ref action through
    pol_res2 = client.post(
        "/api/policies",
        headers=admin_hdr,
        json={"allow_writes": True, "allow_tier3": True, "desktop_enabled": True, "max_tier_unattended": 3},
    )
    assert pol_res2.status_code == 200
    monkeypatch.setattr(
        "ai_bridge.tools.desktop.perform_approved_ui_action",
        lambda action, **kw: {"ok": True, "action": action, "x": kw.get("x"), "y": kw.get("y")},
    )
    allowed = client.post(
        "/api/devices/local/desktop/a11y/act",
        headers=admin_hdr,
        json={"ref": ref_id, "action": "click", "session_id": "ui_local"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["ok"] is True

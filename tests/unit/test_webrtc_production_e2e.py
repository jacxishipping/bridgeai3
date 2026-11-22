"""Unit tests for scripts/webrtc_production_e2e.py helpers and residual honesty."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_prod_mod():
    path = ROOT / "scripts" / "webrtc_production_e2e.py"
    spec = importlib.util.spec_from_file_location("webrtc_production_e2e", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_turn_configured_from_env_stun_only(monkeypatch):
    mod = _load_prod_mod()
    monkeypatch.delenv("AI_BRIDGE_TURN_SECRET", raising=False)
    monkeypatch.delenv("AI_BRIDGE_TURN_HOST", raising=False)
    monkeypatch.delenv("AI_BRIDGE_TURN_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_ICE_SERVERS", raising=False)
    info = mod.turn_configured_from_env()
    assert info["configured"] is False
    assert info["mode"] == "stun_only"


def test_turn_configured_from_env_ephemeral(monkeypatch):
    mod = _load_prod_mod()
    monkeypatch.setenv("AI_BRIDGE_TURN_SECRET", "s" * 32)
    monkeypatch.setenv("AI_BRIDGE_TURN_HOST", "turn.example.com")
    monkeypatch.delenv("AI_BRIDGE_TURN_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_ICE_SERVERS", raising=False)
    info = mod.turn_configured_from_env()
    assert info["configured"] is True
    assert info["mode"] == "ephemeral"


def test_turn_configured_from_ice_servers_json(monkeypatch):
    mod = _load_prod_mod()
    monkeypatch.delenv("AI_BRIDGE_TURN_SECRET", raising=False)
    monkeypatch.delenv("AI_BRIDGE_TURN_HOST", raising=False)
    monkeypatch.delenv("AI_BRIDGE_TURN_URL", raising=False)
    monkeypatch.setenv(
        "AI_BRIDGE_ICE_SERVERS",
        json.dumps(
            [
                {"urls": "stun:stun.l.google.com:19302"},
                {"urls": ["turns:turn.example.com:443"], "username": "u", "credential": "c"},
            ]
        ),
    )
    info = mod.turn_configured_from_env()
    assert info["configured"] is True
    assert info["mode"] == "ice_servers_json"
    assert info["has_ice_servers_turn"] is True


def test_ice_list_has_turn():
    mod = _load_prod_mod()
    assert mod.ice_list_has_turn([{"urls": "stun:x"}]) is False
    assert mod.ice_list_has_turn([{"urls": ["turn:x:3478"]}]) is True
    assert mod.ice_list_has_turn([{"urls": "turns:x:443"}]) is True


def test_capability_residual_cleared_when_e2e_verified(monkeypatch):
    from ai_bridge.control import desktop_capabilities

    monkeypatch.delenv("AI_BRIDGE_LIBWEBRTC_E2E_VERIFIED", raising=False)
    matrix = desktop_capabilities.build_capability_matrix(include_host=False)
    assert "native_cpp_agent_e2e_with_dashboard" in matrix["residual_gaps"]

    monkeypatch.setenv("AI_BRIDGE_LIBWEBRTC_E2E_VERIFIED", "1")
    matrix2 = desktop_capabilities.build_capability_matrix(include_host=False)
    assert "native_cpp_agent_e2e_with_dashboard" not in matrix2["residual_gaps"]


def test_require_turn_fails_closed_without_config(monkeypatch, tmp_path):
    mod = _load_prod_mod()
    monkeypatch.delenv("AI_BRIDGE_TURN_SECRET", raising=False)
    monkeypatch.delenv("AI_BRIDGE_TURN_HOST", raising=False)
    monkeypatch.delenv("AI_BRIDGE_TURN_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_ICE_SERVERS", raising=False)
    out = tmp_path / "evidence.json"
    report = mod.run_production_e2e(out, require_turn=True)
    assert report["production_ready"] is False
    assert out.is_file()
    body = json.loads(out.read_text(encoding="utf-8"))
    assert "require_turn" in (body.get("signaling") or {}).get("error", "") or body["require_turn"] is True

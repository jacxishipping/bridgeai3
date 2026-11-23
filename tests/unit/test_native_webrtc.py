"""Unit tests for AI_BRIDGE_NATIVE_WEBRTC selection helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_bridge import native_webrtc


def test_native_webrtc_mode_defaults_auto(monkeypatch):
    monkeypatch.delenv("AI_BRIDGE_NATIVE_WEBRTC", raising=False)
    monkeypatch.delenv("AIBRIDGE_RUN_NATIVE_WEBRTC", raising=False)
    assert native_webrtc.native_webrtc_mode() == "auto"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", "off"),
        ("0", "off"),
        ("prefer", "prefer"),
        ("1", "prefer"),
        ("required", "required"),
    ],
)
def test_native_webrtc_mode_aliases(monkeypatch, raw, expected):
    monkeypatch.setenv("AI_BRIDGE_NATIVE_WEBRTC", raw)
    assert native_webrtc.native_webrtc_mode() == expected


def test_find_desktop_exe_respects_env(monkeypatch, tmp_path: Path):
    exe = tmp_path / "ai_bridge_desktop.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("AIBRIDGE_DESKTOP_EXE", str(exe))
    assert native_webrtc.find_desktop_exe() == exe


def test_try_start_native_off_returns_none(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_NATIVE_WEBRTC", "off")
    assert (
        native_webrtc.try_start_native_stream(
            control_url="https://example.test",
            device_token="tok",
            session_id="session_x",
        )
        is None
    )


def test_describe_native_webrtc_shape(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_NATIVE_WEBRTC", "auto")
    info = native_webrtc.describe_native_webrtc()
    assert info["mode"] == "auto"
    assert "fallback" in info
    assert info["fallback"] == "python_aiortc"

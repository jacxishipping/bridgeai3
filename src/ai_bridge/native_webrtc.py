"""Optional native C++ WebRTC path via ``ai_bridge_desktop.exe`` (libdatachannel).

Fleet Live Desktop historically used Python ``aiortc``. When a built
``ai_bridge_desktop.exe`` is present on Windows, dispatch can prefer that
PeerConnection path instead. Mode is controlled by
``AI_BRIDGE_NATIVE_WEBRTC``:

* ``off`` — never launch native (Python aiortc only)
* ``auto`` / ``prefer`` (default) — use native when the exe is found
* ``required`` — fail if the exe is missing on Windows
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

_VALID_MODES = frozenset({"off", "auto", "prefer", "required"})

# Track the managed desktop process so stop_webrtc_stream can terminate it.
_MANAGED_PROC: subprocess.Popen[str] | None = None
_MANAGED_SESSION: str | None = None


def native_webrtc_mode() -> str:
    raw = os.environ.get("AI_BRIDGE_NATIVE_WEBRTC", "auto").strip().lower()
    if raw in _VALID_MODES:
        return raw
    if raw in ("0", "false", "no", "disabled"):
        return "off"
    if raw in ("1", "true", "yes", "on"):
        return "prefer"
    # Compat with production E2E harness flag.
    if os.environ.get("AIBRIDGE_RUN_NATIVE_WEBRTC", "").strip().lower() in ("1", "true", "yes"):
        return "prefer"
    return "auto"


def find_desktop_exe() -> Path | None:
    env = os.environ.get("AIBRIDGE_DESKTOP_EXE", "").strip()
    if env and Path(env).is_file():
        return Path(env)

    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    candidates: list[Path] = []
    if local_app:
        candidates.append(Path(local_app) / "Programs" / "AIBridgeClient" / "ai_bridge_desktop.exe")

    repo_root = Path(__file__).resolve().parents[2]
    candidates.extend(
        [
            repo_root / "windows-agent" / "build" / "Release" / "ai_bridge_desktop.exe",
            repo_root / "windows-agent" / "build" / "ai_bridge_desktop.exe",
            repo_root / "windows-agent" / "build" / "Debug" / "ai_bridge_desktop.exe",
        ]
    )
    # Same directory as the running agent / python entry if bundled.
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent / "ai_bridge_desktop.exe")
    else:
        candidates.append(Path(sys.argv[0]).resolve().parent / "ai_bridge_desktop.exe")

    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def describe_native_webrtc() -> dict[str, Any]:
    mode = native_webrtc_mode()
    exe = find_desktop_exe()
    return {
        "mode": mode,
        "platform": platform.system(),
        "desktop_exe": str(exe) if exe else None,
        "desktop_exe_found": exe is not None,
        "shipping_preference": "native_libdatachannel_when_available",
        "fallback": "python_aiortc",
        "honesty": (
            "On Windows, start_webrtc_stream prefers ai_bridge_desktop.exe "
            "(libdatachannel) when present; otherwise Python aiortc/PyAV."
        ),
    }


def stop_managed_native_stream(session_id: str | None = None) -> dict[str, Any]:
    """Terminate a desktop process started by :func:`try_start_native_stream`."""
    global _MANAGED_PROC, _MANAGED_SESSION
    proc = _MANAGED_PROC
    if proc is None:
        return {"stopped": False, "reason": "no_managed_native_process"}
    if session_id and _MANAGED_SESSION and session_id != _MANAGED_SESSION:
        return {"stopped": False, "reason": "session_mismatch", "active_session": _MANAGED_SESSION}
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as exc:
        return {"stopped": False, "error": str(exc)}
    finally:
        _MANAGED_PROC = None
        _MANAGED_SESSION = None
    return {"stopped": True, "backend": "libdatachannel"}


def try_start_native_stream(
    *,
    control_url: str,
    device_token: str,
    session_id: str,
    monitor_index: int = 0,
    fps: int = 30,
    bitrate_kbps: int = 4500,
) -> dict[str, Any] | None:
    """Launch ``ai_bridge_desktop`` when mode + platform + exe allow it.

    Returns a result dict when native was selected (success or hard failure
    under ``required``). Returns ``None`` to signal the caller should fall
    back to Python aiortc.
    """
    global _MANAGED_PROC, _MANAGED_SESSION

    mode = native_webrtc_mode()
    if mode == "off":
        return None
    if platform.system() != "Windows":
        if mode == "required":
            raise RuntimeError("AI_BRIDGE_NATIVE_WEBRTC=required but platform is not Windows")
        return None

    exe = find_desktop_exe()
    if exe is None:
        if mode == "required":
            raise RuntimeError(
                "AI_BRIDGE_NATIVE_WEBRTC=required but ai_bridge_desktop.exe was not found"
            )
        return None

    stop_managed_native_stream()
    # Spaced CLI form matches windows-agent/src/desktop/desktop_agent.cpp.
    args = [
        str(exe),
        "--monitor",
        str(int(monitor_index)),
        "--fps",
        str(max(1, min(60, int(fps)))),
        "--bitrate-kbps",
        str(max(250, min(50_000, int(bitrate_kbps)))),
        "--control-url",
        control_url,
        "--device-token",
        device_token,
        "--session-id",
        session_id,
    ]
    cfg = Path.home() / ".ai-bridge" / "cpp_service_runtime.json"
    if not cfg.is_file():
        cfg = Path.home() / ".ai-bridge" / "cpp_service_runtime.plain.json"
    if cfg.is_file():
        args.extend(["--config", str(cfg)])

    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        args,
        cwd=str(exe.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    # Brief settle: catch immediate fatals (no backend / bad token).
    try:
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        pass
    if proc.poll() is not None:
        detail = (
            f"ai_bridge_desktop.exe exited immediately (code {proc.returncode})"
        )
        if mode == "required":
            raise RuntimeError(detail)
        # auto/prefer: let the caller fall back to Python aiortc.
        return None

    _MANAGED_PROC = proc
    _MANAGED_SESSION = session_id
    return {
        "ok": True,
        "backend": "libdatachannel",
        "engine": "ai_bridge_desktop",
        "pid": proc.pid,
        "session_id": session_id,
        "exe": str(exe),
        "fps": max(1, min(60, int(fps))),
        "bitrate_kbps": max(250, min(50_000, int(bitrate_kbps))),
        "monitor_index": int(monitor_index),
    }

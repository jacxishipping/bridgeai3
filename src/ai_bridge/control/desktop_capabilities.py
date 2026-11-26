"""Cross-OS desktop stream / automation honesty matrix (Phase 9)."""
from __future__ import annotations

import os
import platform
from typing import Any

_STATIC_MATRIX: dict[str, dict[str, Any]] = {
    "Windows": {
        "agent_entrypoint": "agents/windows/agent_main.py",
        "webrtc_desktop_stream": True,
        "desktop_automation": True,
        "ocr_som": True,
        "native_capture_dll": True,
        "verified_on_target_ci": False,
        "notes": "Primary fleet path; prefers ai_bridge_desktop WebRTC when present; optional ai_bridge_native.dll for DXGI/input.",
    },
    "Linux": {
        "agent_entrypoint": "agents/linux/agent_main.py",
        "webrtc_desktop_stream": False,
        "desktop_automation": "partial",
        "ocr_som": False,
        "native_capture_dll": False,
        "verified_on_target_ci": False,
        "notes": "X11 + xdotool only; Wayland-only sessions are not automated; no WebRTC desktop track.",
    },
    "Darwin": {
        "agent_entrypoint": "agents/macos/agent_main.py",
        "webrtc_desktop_stream": False,
        "desktop_automation": False,
        "ocr_som": False,
        "native_capture_dll": False,
        "verified_on_target_ci": False,
        "notes": "Entrypoint present; desktop stream/OCR not verified on macOS hardware.",
    },
}


def normalize_platform_family(os_value: str | None) -> str:
    """Map a device/host OS string to Windows | Linux | Darwin | Unknown.

    Never use bare ``\"win\" in raw`` — that falsely matches Darwin (``\"win\" in \"darwin\"``).
    """
    raw = (os_value or "").strip().lower()
    if not raw:
        return "Unknown"
    # Darwin / macOS first (before any Windows token checks).
    if (
        raw == "darwin"
        or "darwin" in raw
        or "macos" in raw
        or raw.startswith("mac os")
        or raw.startswith("macos")
        or raw in {"mac", "osx", "os x"}
    ):
        return "Darwin"
    # Windows: explicit tokens only (win32 / windows / WinNT) — not the substring "win".
    if (
        raw in {"win32", "win64", "winnt", "windows"}
        or raw.startswith("windows")
        or raw.startswith("win32")
        or raw.startswith("win64")
        or raw.startswith("winnt")
    ):
        return "Windows"
    if (
        "linux" in raw
        or "ubuntu" in raw
        or "debian" in raw
        or "fedora" in raw
        or "centos" in raw
        or "rhel" in raw
    ):
        return "Linux"
    return "Unknown"


def platform_supports_webrtc_desktop_stream(os_value: str | None) -> bool:
    """True only for Windows desktop stream hosts (case-insensitive win32/Windows)."""
    return normalize_platform_family(os_value) == "Windows"


def stream_gate_for_os(os_value: str | None) -> dict[str, Any]:
    """Return an honesty gate decision for Live Desktop session create."""
    family = normalize_platform_family(os_value)
    row = _STATIC_MATRIX.get(family)
    # Only Windows is a desktop WebRTC stream host — ignore accidental matrix flips.
    if family == "Windows" and platform_supports_webrtc_desktop_stream(os_value):
        return {
            "allowed": True,
            "platform": family,
            "stream_available": True,
            "reason": "",
        }
    notes = (row or {}).get("notes") if row else "Unknown or unreported device OS."
    if family == "Linux":
        reason = "linux_webrtc_desktop_stream_unavailable"
    elif family == "Darwin":
        reason = "macos_webrtc_desktop_stream_unavailable"
    elif family == "Unknown":
        reason = "device_os_unknown_stream_unavailable"
    else:
        reason = "webrtc_desktop_stream_unavailable"
    return {
        "allowed": False,
        "platform": family,
        "stream_available": False,
        "reason": reason,
        "detail": notes or "WebRTC desktop streaming is only available on Windows desktop hosts.",
    }


def build_capability_matrix(*, include_host: bool = True) -> dict[str, Any]:
    from ai_bridge import tools, webrtc_stream

    host_os = platform.system()
    host_status = tools.get_desktop_status() if include_host else {}
    matrix = {os_name: {**row} for os_name, row in _STATIC_MATRIX.items()}
    audio_opt_in = bool(webrtc_stream.desktop_audio_opt_in())
    capture_ok = bool(webrtc_stream.desktop_audio_capture_implemented())
    try:
        from ai_bridge import desktop_audio as desktop_audio_mod

        probe = desktop_audio_mod.probe_wasapi_loopback()
    except Exception:
        probe = {"available": False, "reason": "probe_unavailable"}
    residuals = [
        "macos_on_target_verification",
        "linux_webrtc_desktop_stream",
    ]
    # Cleared when scripts/libwebrtc_e2e_harness.py (or webrtc_production_e2e.py)
    # completes a verified C++ PeerConnection ↔ dashboard-contract viewer path.
    if os.environ.get("AI_BRIDGE_LIBWEBRTC_E2E_VERIFIED", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        residuals.append("native_cpp_agent_e2e_with_dashboard")
    if not capture_ok:
        residuals.append("wasapi_live_desktop_audio_capture")
    if audio_opt_in and capture_ok:
        _backend = probe.get("backend") or "wasapi"
        audio_reason = f"AI_BRIDGE_DESKTOP_AUDIO opted in; WASAPI loopback capture available ({_backend})"
    elif audio_opt_in:
        audio_reason = (
            "AI_BRIDGE_DESKTOP_AUDIO opted in; live loopback unavailable "
            f"({probe.get('reason')}) — emitting silent PCM"
        )
    else:
        audio_reason = "desktop audio off by default (set AI_BRIDGE_DESKTOP_AUDIO=1 to attach track)"
    try:
        from ai_bridge import screen_relay as screen_relay_mod

        relay_status = screen_relay_mod.describe_screen_relay_status()
    except Exception:
        relay_status = {"available": False, "protocol": "nsl1", "mode": "unavailable"}
    return {
        "schema": "ai-bridge-desktop-capabilities/v1",
        "host_os": host_os,
        "host_live_status": {
            **host_status,
            # Explicit: this block is the control-plane process host, not a fleet device.
            "scope": "control_plane_host",
        },
        "platforms": matrix,
        "desktop_audio": {
            "opt_in": audio_opt_in,
            "capture_implemented": capture_ok,
            "probe": probe,
            "scope": "control_plane_host",
            "reason": audio_reason,
        },
        "screen_relay": {
            **relay_status,
            "supported_platforms": ["Windows", "Darwin", "Linux"],
            "scope": "control_plane_host",
        },
        "residual_gaps": residuals,
    }


def device_desktop_capabilities(device_row: dict[str, Any], effective_policy: dict[str, Any]) -> dict[str, Any]:
    """Release 4: Honest per-device cross-platform desktop capability contract.

    Advertises read-only screen/status across Windows, Linux, and macOS, while
    advertising interactive control ONLY on tested platforms (Windows) when
    explicitly enabled by effective policy and not revoked.
    """
    family = normalize_platform_family(device_row.get("os"))
    revoked = bool(device_row.get("revoked"))
    online = (device_row.get("status") or "").lower() == "online" and not revoked
    policy_desktop = bool(effective_policy.get("desktop_enabled"))
    is_windows = family == "Windows"

    screenshot_supported = family in ("Windows", "Linux", "Darwin")
    webrtc_supported = is_windows
    interactive_supported = is_windows and policy_desktop and not revoked

    if revoked:
        mode = "unavailable"
        reason = "Device is revoked."
    elif not is_windows:
        mode = "view_only" if screenshot_supported else "unavailable"
        reason = f"Interactive desktop automation is not advertised for {family}; view-only/status contract enforced."
    elif not policy_desktop:
        mode = "view_only"
        reason = "Interactive desktop control is disabled by effective policy."
    else:
        mode = "interactive"
        reason = "Windows desktop host with policy opt-in."

    return {
        "schema": "ai-bridge-device-desktop-capabilities/v1",
        "device_id": device_row.get("id"),
        "device_name": device_row.get("name"),
        "platform_family": family,
        "online": online,
        "revoked": revoked,
        "read_only_status_supported": True,
        "screenshot_supported": screenshot_supported,
        "webrtc_stream_supported": webrtc_supported,
        "screen_relay_supported": (not revoked) and family in ("Windows", "Linux", "Darwin"),
        "interactive_input_supported": interactive_supported,
        "policy_desktop_enabled": policy_desktop,
        "advertised_mode": mode,
        "reason": reason,
        "discovery": normalize_agent_capabilities(device_row.get("capabilities_json"), device_row.get("os")),
    }


def _default_display_server(family: str) -> str:
    if family == "Windows":
        return "win32"
    if family == "Darwin":
        return "quartz"
    if family == "Linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        return "x11"
    return "headless"


def _default_a11y_backend(family: str) -> str:
    if family == "Windows":
        return "windows_uia"
    if family == "Darwin":
        return "macos_ax"
    if family == "Linux":
        return "linux_atspi"
    return "som_ocr"


def probe_local_capabilities() -> dict[str, Any]:
    """Probe the local host for display server, desktop transports, A11y backend, audio loopback, and CLI runtimes."""
    import shutil

    host_os = platform.system()
    family = normalize_platform_family(host_os)

    # Display server detection
    if family == "Windows":
        display_server = "win32"
    elif family == "Darwin":
        display_server = "quartz"
    elif family == "Linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            display_server = "wayland"
        elif os.environ.get("DISPLAY"):
            display_server = "x11"
        else:
            display_server = "headless"
    else:
        display_server = "headless"

    # Available transports ordered by performance
    transports: list[str] = []
    if family == "Windows":
        transports.append("webrtc")
    if family in ("Windows", "Darwin", "Linux"):
        transports.append("screen_relay")
    transports.append("jpeg")

    # A11y backend detection
    a11y_backend = _default_a11y_backend(family)
    try:
        from ai_bridge import hybrid_desktop

        if getattr(hybrid_desktop, "_load_agent_desktop_ffi", lambda: None)() is not None:
            a11y_backend = "agent_desktop_ffi"
        elif getattr(hybrid_desktop, "_find_agent_desktop_binary", lambda: None)() is not None:
            a11y_backend = "agent_desktop_cli"
    except Exception:
        pass

    # Audio loopback detection
    audio_loopback = False
    audio_backend = "none"
    if family == "Windows":
        try:
            from ai_bridge import desktop_audio

            probe = desktop_audio.probe_wasapi_loopback()
            if probe.get("available"):
                audio_loopback = True
                audio_backend = str(probe.get("backend") or "wasapi_loopback")
        except Exception:
            pass

    # Runtimes discovery
    candidate_runtimes = ("python", "node", "dotnet", "git", "docker", "powershell", "bash")
    runtimes = [rt for rt in candidate_runtimes if shutil.which(rt) is not None]

    # Monitors discovery
    monitors: list[dict[str, Any]] = []
    try:
        from ai_bridge.screen_capture import get_monitors

        raw_mons = get_monitors()
        if isinstance(raw_mons, list):
            monitors = raw_mons[:8]
    except Exception:
        pass
    if not monitors:
        monitors = [{"index": 0, "name": "Primary Display", "width": 1920, "height": 1080, "is_primary": True}]

    return {
        "schema": "ai-bridge-capability-discovery/v1",
        "platform_family": family,
        "display_server": display_server,
        "transports": transports,
        "recommended_transport": transports[0] if transports else "jpeg",
        "a11y_backend": a11y_backend,
        "audio_loopback": audio_loopback,
        "audio_backend": audio_backend,
        "runtimes": runtimes,
        "monitors": monitors,
        "monitor_count": len(monitors),
    }


def normalize_agent_capabilities(raw: Any, os_value: str | None = None) -> dict[str, Any]:
    """Normalize agent-reported capabilities JSON or dict into the canonical cross-platform discovery contract."""
    import json

    family = normalize_platform_family(os_value)
    parsed: dict[str, Any] = {}
    raw_list: list[str] = []
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                parsed = loaded
            elif isinstance(loaded, list):
                raw_list = [str(x) for x in loaded if x]
        except Exception:
            pass
    elif isinstance(raw, dict):
        parsed = dict(raw)
    elif isinstance(raw, list):
        raw_list = [str(x) for x in raw if x]

    default_transports: list[str] = []
    if family == "Windows":
        default_transports.append("webrtc")
    if family in ("Windows", "Darwin", "Linux"):
        default_transports.append("screen_relay")
    default_transports.append("jpeg")

    raw_transports = parsed.get("transports")
    if isinstance(raw_transports, list) and raw_transports:
        transports = [str(t).strip().lower() for t in raw_transports if str(t).strip().lower() in {"webrtc", "screen_relay", "jpeg"}]
        if family != "Windows":
            transports = [t for t in transports if t != "webrtc"]
        if "jpeg" not in transports:
            transports.append("jpeg")
    else:
        transports = default_transports

    display_server = str(parsed.get("display_server") or _default_display_server(family)).strip().lower()
    a11y_backend = str(parsed.get("a11y_backend") or _default_a11y_backend(family)).strip().lower()
    audio_loopback = bool(parsed.get("audio_loopback", family == "Windows"))
    audio_backend = str(parsed.get("audio_backend") or ("wasapi_loopback" if audio_loopback and family == "Windows" else "none"))

    raw_runtimes = parsed.get("runtimes")
    if isinstance(raw_runtimes, list):
        runtimes = [str(r).strip().lower() for r in raw_runtimes if str(r).strip()]
    elif raw_list:
        runtimes = [str(r).strip().lower() for r in raw_list if str(r).strip()]
    else:
        runtimes = ["python", "powershell"] if family == "Windows" else (["python", "bash"] if family in ("Linux", "Darwin") else ["python"])

    raw_monitors = parsed.get("monitors")
    monitors = raw_monitors if isinstance(raw_monitors, list) and raw_monitors else [
        {"index": 0, "name": "Primary Display", "width": 1920, "height": 1080, "is_primary": True}
    ]

    return {
        "schema": "ai-bridge-capability-discovery/v1",
        "platform_family": family,
        "display_server": display_server,
        "transports": transports,
        "recommended_transport": transports[0] if transports else "jpeg",
        "a11y_backend": a11y_backend,
        "audio_loopback": audio_loopback,
        "audio_backend": audio_backend,
        "runtimes": runtimes,
        "monitors": monitors,
        "monitor_count": len(monitors),
        "probed_by_agent": bool(parsed),
    }
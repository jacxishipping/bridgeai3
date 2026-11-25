"""Hybrid Desktop Accessibility & Visual Fallback Engine.

Combines deterministic, token-efficient accessibility-tree control (matching the
``vercel-labs/agent-desktop`` snapshot -> ``@ref`` -> ``STALE_REF`` observe-act
contract across Windows UIA, macOS AX, and Linux AT-SPI) with AI Bridge's
policy engine, password redaction, ``AllowedApps`` scoping, emergency-stop
enforcement, and automatic visual/OCR fallback for non-accessible UIs.

Backends (controlled by ``AI_BRIDGE_A11Y_BACKEND`` = ``auto|ffi|cli|native|off``):
  1. ``agent_desktop_ffi`` — in-process C FFI (``libagent_desktop_ffi`` via
     ``ctypes``) when ``AI_BRIDGE_AGENT_DESKTOP_LIB`` or a platform shared
     library is available.
  2. ``agent_desktop_cli`` — optional ``agent-desktop`` CLI subprocess (with
     ``shell=False``, scrubbed child env, and ``--stdin`` for text input).
  3. ``native_bridge`` — built-in zero-dependency fallback over AI Bridge's
     native UIA / Win32 / visual tools, maintaining session-scoped ephemeral
     ``@s<id>:e<n>`` refs with ``STALE_REF`` invalidation.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .policy import Policy, decide
from .tools._common import default_policy


def _enforce_policy(tool: str, *, approval_granted: bool = False, policy: Policy | None = None) -> Policy:
    eff = policy or default_policy()
    decision = decide(eff, tool, approval_granted=approval_granted)
    if not decision.allowed:
        raise PermissionError(decision.reason or f"Tool {tool!r} denied by policy.")
    return eff

A11Y_BACKEND_ENV = "AI_BRIDGE_A11Y_BACKEND"
AGENT_DESKTOP_BIN_ENV = "AI_BRIDGE_AGENT_DESKTOP_BIN"
AGENT_DESKTOP_LIB_ENV = "AI_BRIDGE_AGENT_DESKTOP_LIB"

_VALID_MODES = frozenset({"auto", "ffi", "cli", "native", "off"})
_REF_RE = re.compile(r"^@[A-Za-z0-9_-]{2,40}:[A-Za-z0-9_.-]{1,40}$")
_SECRET_HINT_RE = re.compile(
    r"(password|passwd|passcode|pin\b|secret|api[_ -]?key|token|ssn|cvv|credit.?card)",
    re.IGNORECASE,
)

SUPPORTED_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "hover",
        "type",
        "set_value",
        "clear",
        "check",
        "uncheck",
        "toggle",
        "select",
        "expand",
        "collapse",
        "scroll_to",
        "focus",
        "show_menu",
    }
)

_CLI_ACTION_MAP: dict[str, str] = {
    "click": "click",
    "double_click": "double-click",
    "right_click": "right-click",
    "hover": "hover",
    "type": "type",
    "set_value": "set-value",
    "clear": "clear",
    "check": "check",
    "uncheck": "uncheck",
    "toggle": "toggle",
    "select": "select",
    "expand": "expand",
    "collapse": "collapse",
    "scroll_to": "scroll-to",
    "focus": "focus",
    "show_menu": "click",
}

_SNAPSHOT_TTL_SECONDS = 300.0
_MAX_BATCH_STEPS = 25


@dataclass
class _RefEntry:
    ref: str
    snapshot_id: str
    role: str
    name: str
    app: str = ""
    window_title: str = ""
    value: str = ""
    is_password: bool = False
    is_enabled: bool = True
    checked: bool | None = None
    expanded: bool | None = None
    bounds: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class _SnapshotStore:
    """Thread-safe ephemeral ``@ref`` store with generation-based ``STALE_REF`` checks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_snapshot_id: str = ""
        self._created_at: float = 0.0
        self._backend: str = ""
        self._refs: dict[str, _RefEntry] = {}
        self._seen_snapshots: set[str] = set()

    def store(self, snapshot_id: str, backend: str, entries: list[_RefEntry]) -> None:
        with self._lock:
            self._active_snapshot_id = snapshot_id
            self._created_at = time.monotonic()
            self._backend = backend
            self._refs = {e.ref: e for e in entries}
            self._seen_snapshots.add(snapshot_id)
            if len(self._seen_snapshots) > 200:
                self._seen_snapshots = {snapshot_id}

    def resolve(self, ref: str) -> tuple[_RefEntry | None, str | None]:
        with self._lock:
            if not self._active_snapshot_id:
                return None, "STALE_REF"
            if (time.monotonic() - self._created_at) > _SNAPSHOT_TTL_SECONDS:
                return None, "STALE_REF"
            entry = self._refs.get(ref)
            if entry is not None and entry.snapshot_id == self._active_snapshot_id:
                return entry, None
            return None, "STALE_REF"

    def stats(self) -> dict[str, Any]:
        with self._lock:
            age_s = round(time.monotonic() - self._created_at, 2) if self._created_at else None
            expired = bool(age_s is not None and age_s > _SNAPSHOT_TTL_SECONDS)
            return {
                "active_snapshot_id": self._active_snapshot_id or None,
                "active_backend": self._backend or None,
                "cached_ref_count": 0 if expired else len(self._refs),
                "snapshot_age_seconds": age_s,
                "snapshot_expired": expired,
            }

    def clear(self) -> None:
        with self._lock:
            self._active_snapshot_id = ""
            self._created_at = 0.0
            self._backend = ""
            self._refs.clear()


_STORE = _SnapshotStore()


def a11y_backend_mode() -> str:
    raw = os.environ.get(A11Y_BACKEND_ENV, "auto").strip().lower()
    if raw in _VALID_MODES:
        return raw
    if raw in ("0", "false", "no", "disabled"):
        return "off"
    return "auto"


def _platform_adapter_name() -> str:
    if sys.platform == "win32":
        return "windows_uia"
    if sys.platform == "darwin":
        return "macos_ax"
    return "linux_atspi"


def find_agent_desktop_cli() -> str | None:
    explicit = os.environ.get(AGENT_DESKTOP_BIN_ENV, "").strip()
    if explicit:
        p = Path(explicit)
        return str(p) if p.is_file() else None
    for name in ("agent-desktop", "agent-desktop.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def find_agent_desktop_lib() -> str | None:
    explicit = os.environ.get(AGENT_DESKTOP_LIB_ENV, "").strip()
    if explicit:
        p = Path(explicit)
        return str(p) if p.is_file() else None
    candidates: list[str]
    if sys.platform == "win32":
        candidates = ["agent_desktop_ffi.dll", "libagent_desktop_ffi.dll"]
    elif sys.platform == "darwin":
        candidates = ["libagent_desktop_ffi.dylib"]
    else:
        candidates = ["libagent_desktop_ffi.so"]
    repo_root = Path(__file__).resolve().parents[2]
    search_dirs = [
        repo_root / "third_party" / "agent-desktop" / "target" / "release",
        repo_root / "windows-agent" / "build" / "Release",
        Path.home() / ".ai-bridge" / "bin",
    ]
    for d in search_dirs:
        for name in candidates:
            p = d / name
            if p.is_file():
                return str(p)
    return None


class _FfiBridge:
    """Thin, safe ctypes wrapper for ``libagent_desktop_ffi`` (``agent_desktop.h``)."""

    ABI_MAJOR = 1

    def __init__(self, lib_path: str) -> None:
        self.lib_path = lib_path
        self._lib = ctypes.CDLL(lib_path)
        self._configure()

    def _configure(self) -> None:
        self._lib.ad_init.argtypes = [ctypes.c_uint32]
        self._lib.ad_init.restype = ctypes.c_int32
        self._lib.ad_adapter_create_with_session.argtypes = [ctypes.c_char_p]
        self._lib.ad_adapter_create_with_session.restype = ctypes.c_void_p
        self._lib.ad_adapter_destroy.argtypes = [ctypes.c_void_p]
        self._lib.ad_adapter_destroy.restype = None
        self._lib.ad_free_string.argtypes = [ctypes.c_void_p]
        self._lib.ad_free_string.restype = None
        self._lib.ad_snapshot.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_bool,
            ctypes.c_bool,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._lib.ad_snapshot.restype = ctypes.c_int32

    def snapshot(
        self,
        *,
        session_id: str,
        app: str = "",
        max_depth: int = 10,
        skeleton: bool = False,
        compact: bool = True,
    ) -> dict[str, Any]:
        rc_init = int(self._lib.ad_init(self.ABI_MAJOR))
        if rc_init != 0:
            raise RuntimeError(f"agent-desktop FFI ad_init failed (rc={rc_init})")
        adapter = self._lib.ad_adapter_create_with_session(session_id.encode("utf-8"))
        if not adapter:
            raise RuntimeError("agent-desktop FFI failed to create session adapter")
        out_ptr = ctypes.c_void_p()
        try:
            app_arg = app.encode("utf-8") if app else None
            rc = int(
                self._lib.ad_snapshot(
                    adapter,
                    app_arg,
                    0,
                    max(1, int(max_depth)),
                    bool(skeleton),
                    bool(compact),
                    ctypes.byref(out_ptr),
                )
            )
            raw_json = ctypes.string_at(out_ptr.value).decode("utf-8", errors="replace") if out_ptr.value else "{}"
            if rc != 0:
                raise RuntimeError(f"agent-desktop FFI snapshot failed (rc={rc}): {raw_json[:240]}")
            return json.loads(raw_json)
        finally:
            if out_ptr.value:
                self._lib.ad_free_string(out_ptr.value)
            self._lib.ad_adapter_destroy(adapter)


def _select_backend() -> str:
    mode = a11y_backend_mode()
    if mode == "off":
        return "off"
    if mode == "ffi":
        return "agent_desktop_ffi" if find_agent_desktop_lib() else "unavailable"
    if mode == "cli":
        return "agent_desktop_cli" if find_agent_desktop_cli() else "unavailable"
    if mode == "native":
        return "native_bridge"
    if find_agent_desktop_lib():
        return "agent_desktop_ffi"
    if find_agent_desktop_cli():
        return "agent_desktop_cli"
    return "native_bridge"


def _is_sensitive_node(node: dict[str, Any]) -> bool:
    if bool(node.get("is_password") or node.get("password") or node.get("secure")):
        return True
    role = str(node.get("role") or node.get("control_type") or node.get("type") or "")
    if "password" in role.lower() or "secure" in role.lower():
        return True
    text_fields = " ".join(
        str(node.get(k) or "") for k in ("name", "label", "title", "automation_id", "identifier", "placeholder")
    )
    return bool(_SECRET_HINT_RE.search(text_fields))


def sanitize_a11y_tree(obj: Any) -> Any:
    """Recursively redact password and secret values from any accessibility snapshot."""
    if isinstance(obj, list):
        return [sanitize_a11y_tree(item) for item in obj]
    if not isinstance(obj, dict):
        return obj
    out = {k: sanitize_a11y_tree(v) if isinstance(v, (dict, list)) else v for k, v in obj.items()}
    if _is_sensitive_node(out):
        for key in ("value", "text", "selected_text"):
            if key in out and out[key]:
                out[key] = "[REDACTED]"
        out["redacted"] = True
    return out


def _normalize_app_token(name: str) -> str:
    base = os.path.basename((name or "").strip()).lower()
    if base.endswith(".exe") or base.endswith(".app"):
        base = base[:-4]
    return base


def _app_allowed_by_policy(app_name: str, policy: Policy | None) -> bool:
    if policy is None or not policy.allowed_apps:
        return True
    allowed = {_normalize_app_token(a) for a in policy.allowed_apps if a}
    if "*" in allowed:
        return True
    if not app_name:
        return False
    norm = _normalize_app_token(app_name)
    return any(norm == a or a in norm for a in allowed)


def _validate_ref(ref: str) -> str:
    cleaned = (ref or "").strip()
    if not _REF_RE.match(cleaned):
        raise ValueError(
            f"Invalid accessibility element ref {ref!r}: expected format '@<snapshot_id>:<element_id>'."
        )
    return cleaned


def _validate_action(action: str) -> str:
    act = (action or "").strip().lower().replace("-", "_")
    if act not in SUPPORTED_ACTIONS:
        raise ValueError(
            f"Unsupported accessibility action {action!r}. Supported: {', '.join(sorted(SUPPORTED_ACTIONS))}."
        )
    return act


def _check_emergency_stop() -> dict[str, Any] | None:
    try:
        from .plane_control_flags import is_emergency_stop

        if is_emergency_stop():
            return {
                "ok": False,
                "error_code": "EMERGENCY_STOP_ACTIVE",
                "error": "Emergency stop is active; desktop accessibility mutations are blocked.",
            }
    except Exception:
        pass
    return None


def _run_cli(args: list[str], *, stdin_text: str | None = None, timeout: float = 20.0) -> dict[str, Any]:
    binary = find_agent_desktop_cli()
    if not binary:
        raise RuntimeError("agent-desktop CLI binary not found on PATH or AI_BRIDGE_AGENT_DESKTOP_BIN.")
    from .tools.shell import _clean_child_env

    proc = subprocess.run(
        [binary, *args],
        input=stdin_text,
        shell=False,
        capture_output=True,
        text=True,
        timeout=max(1.0, min(60.0, float(timeout))),
        env=_clean_child_env(),
    )
    raw = (proc.stdout or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    if proc.returncode != 0:
        err = (proc.stderr or raw or f"agent-desktop exited with code {proc.returncode}").strip()
        return {"ok": False, "error_code": "CLI_ERROR", "error": err[:500]}
    return {"ok": True, "raw": raw}


def _extract_entries_from_external(
    payload: dict[str, Any], snapshot_id: str, default_app: str = "", default_window: str = ""
) -> list[_RefEntry]:
    entries: list[_RefEntry] = []

    def _walk(node: Any, cur_app: str, cur_win: str) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item, cur_app, cur_win)
            return
        if not isinstance(node, dict):
            return
        node_app = str(node.get("app") or node.get("application") or cur_app or "")
        node_win = str(node.get("window_title") or node.get("window") or cur_win or "")
        ref_val = str(node.get("ref") or node.get("ref_id") or "").strip()
        if ref_val and _REF_RE.match(ref_val):
            bounds = node.get("bounds") if isinstance(node.get("bounds"), dict) else {}
            if not bounds and all(k in node for k in ("x", "y", "width", "height")):
                bounds = {
                    "x": int(node["x"]),
                    "y": int(node["y"]),
                    "width": int(node["width"]),
                    "height": int(node["height"]),
                }
            entries.append(
                _RefEntry(
                    ref=ref_val,
                    snapshot_id=snapshot_id,
                    role=str(node.get("role") or node.get("control_type") or node.get("type") or "element"),
                    name=str(node.get("name") or node.get("label") or node.get("title") or ""),
                    app=node_app,
                    window_title=node_win,
                    value=str(node.get("value") or node.get("text") or ""),
                    is_password=_is_sensitive_node(node),
                    is_enabled=bool(node.get("enabled", node.get("is_enabled", True))),
                    checked=node.get("checked") if isinstance(node.get("checked"), bool) else None,
                    expanded=node.get("expanded") if isinstance(node.get("expanded"), bool) else None,
                    bounds=bounds,
                    raw=node,
                )
            )
        for child_key in ("children", "elements", "nodes", "surfaces", "items"):
            children = node.get(child_key)
            if isinstance(children, list):
                _walk(children, node_app, node_win)

    data = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload
    _walk(data, default_app, default_window)
    return entries


def describe_hybrid_a11y_status() -> dict[str, Any]:
    """Honest status report of the Hybrid Desktop Accessibility + Visual Fallback engine."""
    mode = a11y_backend_mode()
    lib_path = find_agent_desktop_lib()
    cli_path = find_agent_desktop_cli()
    active = _select_backend()
    return {
        "ok": True,
        "mode": mode,
        "active_backend": active,
        "platform": sys.platform,
        "platform_adapter": _platform_adapter_name(),
        "ffi_available": bool(lib_path),
        "ffi_path": lib_path or "",
        "cli_available": bool(cli_path),
        "cli_path": cli_path or "",
        "native_fallback_available": True,
        "visual_fallback_available": True,
        "supported_actions": sorted(SUPPORTED_ACTIONS),
        "store": _STORE.stats(),
        "permissions": check_a11y_permissions(),
    }


def check_a11y_permissions() -> dict[str, Any]:
    """Check OS accessibility and screen-recording permissions with actionable remediation."""
    cli = find_agent_desktop_cli()
    if a11y_backend_mode() in ("auto", "cli") and cli:
        try:
            res = _run_cli(["permissions", "--json"], timeout=10.0)
            if res.get("ok"):
                return {
                    "ok": True,
                    "source": "agent_desktop_cli",
                    "platform": sys.platform,
                    "details": res.get("data", res),
                }
        except Exception:
            pass

    if sys.platform == "win32":
        desktop_info: dict[str, Any] = {}
        try:
            from .screen_capture import get_desktop_session_state

            desktop_info = get_desktop_session_state()
        except Exception:
            desktop_info = {}
        interactive = bool(desktop_info.get("interactive_session", True))
        secure = bool(desktop_info.get("secure_desktop", False))
        return {
            "ok": interactive and not secure,
            "source": "native_bridge",
            "platform": "win32",
            "accessibility_granted": interactive and not secure,
            "screen_recording_granted": interactive and not secure,
            "interactive_session": interactive,
            "secure_desktop": secure,
            "guide": (
                "Ready: Windows UI Automation works in the logged-in interactive user session "
                "(UIPI prevents controlling elevated windows from an unelevated agent)."
                if (interactive and not secure)
                else "Run the agent in an active, unlocked user desktop session (not Session 0 / UAC secure desktop)."
            ),
        }

    if sys.platform == "darwin":
        ax_trusted: bool | None = None
        try:
            app_services = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            app_services.AXIsProcessTrusted.restype = ctypes.c_bool
            ax_trusted = bool(app_services.AXIsProcessTrusted())
        except Exception:
            ax_trusted = None
        return {
            "ok": bool(ax_trusted),
            "source": "native_bridge",
            "platform": "darwin",
            "accessibility_granted": ax_trusted,
            "screen_recording_granted": None,
            "guide": (
                "macOS TCC Accessibility is granted."
                if ax_trusted
                else "Grant Accessibility & Screen Recording permissions in System Settings -> Privacy & Security."
            ),
        }

    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    atspi_bus = bool(os.environ.get("AT_SPI_BUS_ADDRESS") or os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    return {
        "ok": has_display and atspi_bus,
        "source": "native_bridge",
        "platform": sys.platform,
        "accessibility_granted": has_display and atspi_bus,
        "screen_recording_granted": has_display,
        "display_present": has_display,
        "atspi_bus_present": atspi_bus,
        "guide": (
            "Linux graphical session and D-Bus/AT-SPI environment detected."
            if (has_display and atspi_bus)
            else "Ensure a graphical desktop session (DISPLAY/WAYLAND_DISPLAY) with at-spi2-core and DBUS_SESSION_BUS_ADDRESS is active."
        ),
    }


def a11y_snapshot(
    *,
    app: str = "",
    window_title: str = "",
    window_id: str = "",
    root_ref: str = "",
    skeleton: bool = False,
    compact: bool = True,
    max_depth: int = 10,
    max_elements: int = 80,
    monitor_index: int | None = None,
    policy: Policy | None = None,
) -> dict[str, Any]:
    """Capture a structured accessibility snapshot with ephemeral ``@ref`` IDs."""
    backend = _select_backend()
    if backend == "off":
        return {"ok": False, "error_code": "BACKEND_DISABLED", "error": "Hybrid accessibility backend is disabled (AI_BRIDGE_A11Y_BACKEND=off)."}
    if backend == "unavailable":
        return {"ok": False, "error_code": "BACKEND_UNAVAILABLE", "error": f"Requested backend {a11y_backend_mode()!r} is not installed."}

    if root_ref:
        _validate_ref(root_ref)
    if app and not _app_allowed_by_policy(app, policy):
        raise PermissionError(f"Application {app!r} is not in policy allowed_apps.")

    max_depth = max(1, min(16, int(max_depth)))
    max_elements = max(1, min(300, int(max_elements)))
    snapshot_id = f"s{uuid.uuid4().hex[:8]}"

    # 1. Try in-process agent-desktop C FFI
    if backend == "agent_desktop_ffi":
        lib_path = find_agent_desktop_lib()
        if lib_path:
            try:
                raw = _FfiBridge(lib_path).snapshot(
                    session_id=snapshot_id,
                    app=app,
                    max_depth=max_depth,
                    skeleton=skeleton,
                    compact=compact,
                )
                sanitized = sanitize_a11y_tree(raw)
                entries = _extract_entries_from_external(sanitized, snapshot_id, app, window_title)
                entries = [e for e in entries if _app_allowed_by_policy(e.app or app, policy)]
                _STORE.store(snapshot_id, "agent_desktop_ffi", entries)
                return {
                    "ok": True,
                    "backend": "agent_desktop_ffi",
                    "snapshot_id": snapshot_id,
                    "platform_adapter": _platform_adapter_name(),
                    "skeleton": bool(skeleton),
                    "compact": bool(compact),
                    "ref_count": len(entries),
                    "elements": [
                        {
                            "ref": e.ref,
                            "role": e.role,
                            "name": e.name,
                            "app": e.app,
                            "window_title": e.window_title,
                            "value": "[REDACTED]" if e.is_password else e.value,
                            "enabled": e.is_enabled,
                            "bounds": e.bounds,
                        }
                        for e in entries[:max_elements]
                    ],
                    "tree": sanitized,
                }
            except Exception:
                if a11y_backend_mode() == "ffi":
                    raise

    # 2. Try agent-desktop CLI subprocess
    if backend in ("agent_desktop_ffi", "agent_desktop_cli") and find_agent_desktop_cli():
        cmd = ["snapshot", "--json"]
        if skeleton:
            cmd.append("--skeleton")
        if compact:
            cmd.append("--compact")
        if app:
            cmd.extend(["--app", app])
        if window_id:
            cmd.extend(["--window-id", window_id])
        if root_ref:
            cmd.extend(["--root", root_ref])
        cmd.extend(["--max-depth", str(max_depth)])
        raw = _run_cli(cmd)
        if raw.get("ok", True) and not raw.get("error"):
            sanitized = sanitize_a11y_tree(raw)
            entries = _extract_entries_from_external(sanitized, snapshot_id, app, window_title)
            if not entries and isinstance(sanitized.get("data"), dict):
                # Keep snapshot id if CLI returned a ref-free summary
                pass
            else:
                # Re-key snapshot_id to match actual CLI ref prefix if provided
                first_prefix = entries[0].ref.split(":", 1)[0].lstrip("@") if entries else snapshot_id
                snapshot_id = first_prefix or snapshot_id
                for e in entries:
                    e.snapshot_id = snapshot_id
            entries = [e for e in entries if _app_allowed_by_policy(e.app or app, policy)]
            _STORE.store(snapshot_id, "agent_desktop_cli", entries)
            return {
                "ok": True,
                "backend": "agent_desktop_cli",
                "snapshot_id": snapshot_id,
                "platform_adapter": _platform_adapter_name(),
                "skeleton": bool(skeleton),
                "compact": bool(compact),
                "ref_count": len(entries),
                "elements": [
                    {
                        "ref": e.ref,
                        "role": e.role,
                        "name": e.name,
                        "app": e.app,
                        "window_title": e.window_title,
                        "value": "[REDACTED]" if e.is_password else e.value,
                        "enabled": e.is_enabled,
                        "bounds": e.bounds,
                    }
                    for e in entries[:max_elements]
                ],
                "tree": sanitized,
            }
        if a11y_backend_mode() == "cli":
            return raw

    # 3. Built-in native_bridge fallback (zero external dependencies)
    from .tools.desktop_api import desktop_get_ui_elements

    ui_res = desktop_get_ui_elements(
        include_controls=not skeleton,
        max_elements=max_elements,
        monitor_index=monitor_index,
        approval_granted=True,
        policy=policy,
    )
    raw_elements = ui_res.get("elements") or []
    entries: list[_RefEntry] = []
    public_elements: list[dict[str, Any]] = []
    app_filter = app.strip().lower()
    win_filter = window_title.strip().lower()

    for idx, el in enumerate(raw_elements, start=1):
        if not isinstance(el, dict):
            continue
        el_name = str(el.get("label") or el.get("name") or "").strip()
        el_role = str(el.get("type") or el.get("control_type") or "control").strip()
        el_app = str(el.get("app") or el.get("process") or app or "").strip()
        el_win = str(el.get("window_title") or (el_name if el_role == "window" else window_title) or "").strip()

        if app_filter and app_filter not in el_app.lower() and app_filter not in el_win.lower():
            continue
        if win_filter and win_filter not in el_win.lower() and win_filter not in el_name.lower():
            continue
        if not _app_allowed_by_policy(el_app or el_win, policy):
            continue

        is_pw = _is_sensitive_node({"name": el_name, "role": el_role, **el})
        val = "[REDACTED]" if is_pw else str(el.get("value") or "")
        ref_id = f"@{snapshot_id}:e{idx}"
        bounds = {
            "x": int(el.get("x", 0)),
            "y": int(el.get("y", 0)),
            "width": int(el.get("width", 0)),
            "height": int(el.get("height", 0)),
        }
        entry = _RefEntry(
            ref=ref_id,
            snapshot_id=snapshot_id,
            role=el_role,
            name=el_name,
            app=el_app,
            window_title=el_win,
            value=val,
            is_password=is_pw,
            is_enabled=bool(el.get("enabled", True)),
            checked=el.get("checked") if isinstance(el.get("checked"), bool) else None,
            expanded=el.get("expanded") if isinstance(el.get("expanded"), bool) else None,
            bounds=bounds,
            raw=sanitize_a11y_tree(el),
        )
        entries.append(entry)
        item: dict[str, Any] = {
            "ref": ref_id,
            "role": el_role,
            "name": el_name,
            "enabled": entry.is_enabled,
            "bounds": bounds,
        }
        if not compact:
            item["app"] = el_app
            item["window_title"] = el_win
            item["source"] = el.get("source", ui_res.get("grounding_source", "native"))
        if val:
            item["value"] = val
        if is_pw:
            item["redacted"] = True
        public_elements.append(item)

    _STORE.store(snapshot_id, "native_bridge", entries)
    return {
        "ok": True,
        "backend": "native_bridge",
        "grounding_source": ui_res.get("grounding_source", "none"),
        "snapshot_id": snapshot_id,
        "platform_adapter": _platform_adapter_name(),
        "skeleton": bool(skeleton),
        "compact": bool(compact),
        "ref_count": len(entries),
        "elements": public_elements,
        "screen": {"width": ui_res.get("screen_width", 0), "height": ui_res.get("screen_height", 0)},
    }


def a11y_find(
    *,
    query: str = "",
    role: str = "",
    app: str = "",
    window_title: str = "",
    max_results: int = 25,
    policy: Policy | None = None,
) -> dict[str, Any]:
    """Query the accessibility tree by name/role/app without dumping the full tree."""
    snap = a11y_snapshot(
        app=app,
        window_title=window_title,
        compact=False,
        max_elements=max(80, max_results * 4),
        policy=policy,
    )
    if not snap.get("ok"):
        return snap

    q = (query or "").strip().lower()
    r = (role or "").strip().lower()
    matches: list[dict[str, Any]] = []
    for el in snap.get("elements") or []:
        name_s = str(el.get("name") or "").lower()
        role_s = str(el.get("role") or "").lower()
        val_s = "" if el.get("redacted") else str(el.get("value") or "").lower()
        if q and q not in name_s and q not in val_s:
            continue
        if r and r not in role_s:
            continue
        matches.append(el)
        if len(matches) >= max(1, min(100, int(max_results))):
            break

    return {
        "ok": True,
        "backend": snap.get("backend"),
        "snapshot_id": snap.get("snapshot_id"),
        "query": query,
        "role": role,
        "count": len(matches),
        "matches": matches,
    }


def a11y_get_value(ref: str, *, policy: Policy | None = None) -> dict[str, Any]:
    """Read live text/value/state of a specific snapshot ``@ref`` (password redacted)."""
    ref_id = _validate_ref(ref)
    entry, err = _STORE.resolve(ref_id)
    if err or entry is None:
        return {
            "ok": False,
            "error_code": "STALE_REF",
            "ref": ref_id,
            "error": f"Element ref {ref_id} is stale or unknown.",
            "retry_hint": "Call desktop_a11y_snapshot or desktop_a11y_find to obtain fresh refs.",
        }
    if not _app_allowed_by_policy(entry.app or entry.window_title, policy):
        raise PermissionError(f"Application {(entry.app or entry.window_title)!r} is not in policy allowed_apps.")

    if _STORE.stats().get("active_backend") == "agent_desktop_cli" and find_agent_desktop_cli():
        cli_res = _run_cli(["get-value", ref_id, "--json"])
        if cli_res.get("ok"):
            data = sanitize_a11y_tree(cli_res.get("data", cli_res))
            if entry.is_password and isinstance(data, dict):
                data["value"] = "[REDACTED]"
                data["redacted"] = True
            return {"ok": True, "backend": "agent_desktop_cli", "ref": ref_id, "data": data}

    return {
        "ok": True,
        "backend": _STORE.stats().get("active_backend") or "native_bridge",
        "ref": ref_id,
        "snapshot_id": entry.snapshot_id,
        "role": entry.role,
        "name": entry.name,
        "value": "[REDACTED]" if entry.is_password else entry.value,
        "redacted": entry.is_password,
        "enabled": entry.is_enabled,
        "checked": entry.checked,
        "expanded": entry.expanded,
        "bounds": entry.bounds,
    }


def a11y_list_surfaces(*, app: str = "", policy: Policy | None = None) -> dict[str, Any]:
    """Enumerate active windows, dialogs, menus, context menus, popovers, and sheets."""
    if app and not _app_allowed_by_policy(app, policy):
        raise PermissionError(f"Application {app!r} is not in policy allowed_apps.")
    if _select_backend() == "agent_desktop_cli" and find_agent_desktop_cli():
        cmd = ["list-surfaces", "--json"]
        if app:
            cmd.extend(["--app", app])
        res = _run_cli(cmd)
        if res.get("ok"):
            return {"ok": True, "backend": "agent_desktop_cli", "surfaces": sanitize_a11y_tree(res.get("data", []))}

    from .tools.desktop_api import desktop_get_windows

    win_res = desktop_get_windows(approval_granted=True, policy=policy)
    raw_wins = win_res.get("windows") or []
    surfaces: list[dict[str, Any]] = []
    app_filter = (app or "").strip().lower()
    for w in raw_wins:
        if not isinstance(w, dict):
            continue
        title = str(w.get("title") or w.get("name") or "")
        w_app = str(w.get("app") or w.get("process") or title)
        if app_filter and app_filter not in w_app.lower() and app_filter not in title.lower():
            continue
        if not _app_allowed_by_policy(w_app, policy):
            continue
        surfaces.append(
            {
                "surface_type": "window",
                "title": title,
                "app": w_app,
                "hwnd": w.get("hwnd"),
                "bounds": {
                    "x": int(w.get("x", 0)),
                    "y": int(w.get("y", 0)),
                    "width": int(w.get("width", 0)),
                    "height": int(w.get("height", 0)),
                },
            }
        )
    return {
        "ok": True,
        "backend": "native_bridge",
        "count": len(surfaces),
        "surfaces": surfaces,
    }


def a11y_list_apps(*, policy: Policy | None = None) -> dict[str, Any]:
    """List running GUI applications exposing accessibility surfaces."""
    if _select_backend() == "agent_desktop_cli" and find_agent_desktop_cli():
        res = _run_cli(["list-apps", "--json"])
        if res.get("ok"):
            apps = res.get("data") if isinstance(res.get("data"), list) else []
            filtered = [
                a for a in apps
                if _app_allowed_by_policy(str(a.get("name") if isinstance(a, dict) else a), policy)
            ]
            return {"ok": True, "backend": "agent_desktop_cli", "count": len(filtered), "apps": filtered}

    surf = a11y_list_surfaces(policy=policy)
    seen: dict[str, dict[str, Any]] = {}
    for s in surf.get("surfaces") or []:
        name = str(s.get("app") or s.get("title") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in seen:
            seen[key] = {"name": name, "window_count": 1, "titles": [s.get("title", "")]}
        else:
            seen[key]["window_count"] += 1
            seen[key]["titles"].append(s.get("title", ""))
    apps_list = list(seen.values())
    return {"ok": True, "backend": "native_bridge", "count": len(apps_list), "apps": apps_list}


def a11y_act(
    ref: str,
    action: str = "click",
    *,
    text: str = "",
    option: str = "",
    headed: bool = False,
    fallback_to_visual: bool = True,
    fallback_text: str = "",
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict[str, Any]:
    """Execute a governed semantic accessibility action on ``ref`` with optional visual fallback."""
    eff_policy = _enforce_policy("desktop_a11y_act", approval_granted=approval_granted, policy=policy)
    policy = eff_policy
    estop = _check_emergency_stop()
    if estop is not None:
        return estop

    ref_id = _validate_ref(ref)
    act = _validate_action(action)
    entry, err = _STORE.resolve(ref_id)
    if err or entry is None:
        return {
            "ok": False,
            "error_code": "STALE_REF",
            "ref": ref_id,
            "action": act,
            "error": f"Element ref {ref_id} is stale or not in the active snapshot.",
            "retry_hint": "Call desktop_a11y_snapshot to refresh element refs before acting.",
        }

    target_app = entry.app or entry.window_title
    if not _app_allowed_by_policy(target_app, policy):
        raise PermissionError(f"Application {target_app!r} is not in policy allowed_apps.")

    # Idempotent short-circuit for check/uncheck/expand/collapse when state already matches
    if act == "check" and entry.checked is True:
        return {"ok": True, "backend": _STORE.stats().get("active_backend") or "native_bridge", "ref": ref_id, "action": act, "headless": True, "noop": True, "state": "checked"}
    if act == "uncheck" and entry.checked is False:
        return {"ok": True, "backend": _STORE.stats().get("active_backend") or "native_bridge", "ref": ref_id, "action": act, "headless": True, "noop": True, "state": "unchecked"}
    if act == "expand" and entry.expanded is True:
        return {"ok": True, "backend": _STORE.stats().get("active_backend") or "native_bridge", "ref": ref_id, "action": act, "headless": True, "noop": True, "state": "expanded"}
    if act == "collapse" and entry.expanded is False:
        return {"ok": True, "backend": _STORE.stats().get("active_backend") or "native_bridge", "ref": ref_id, "action": act, "headless": True, "noop": True, "state": "collapsed"}

    # Layer 1: agent-desktop CLI when this snapshot came from CLI
    if _STORE.stats().get("active_backend") == "agent_desktop_cli" and find_agent_desktop_cli():
        cli_verb = _CLI_ACTION_MAP[act]
        cmd = [cli_verb, ref_id, "--json"]
        if headed:
            cmd.append("--headed")
        stdin_payload: str | None = None
        if act in ("type", "set_value"):
            cmd.append("--stdin")
            stdin_payload = text
        elif act == "select" and option:
            cmd.extend(["--option", option])
        cli_res = _run_cli(cmd, stdin_text=stdin_payload)
        if cli_res.get("ok"):
            return {
                "ok": True,
                "backend": "agent_desktop_cli",
                "fallback_used": False,
                "headless": not headed,
                "ref": ref_id,
                "action": act,
                "result": sanitize_a11y_tree(cli_res.get("data", cli_res)),
            }
        if not fallback_to_visual:
            return {
                "ok": False,
                "backend": "agent_desktop_cli",
                "error_code": str(cli_res.get("error_code") or "ACTION_FAILED"),
                "ref": ref_id,
                "action": act,
                "error": str(cli_res.get("error") or "agent-desktop action failed"),
            }

    # Layer 2: Built-in bridgeai3 visual / UI fallback (coordinates or OCR)
    from .tools.desktop import click_text, perform_approved_ui_action

    b = entry.bounds or {}
    bw = int(b.get("width", 0))
    bh = int(b.get("height", 0))
    cx = int(b.get("x", 0)) + max(1, bw // 2) if bw > 0 else None
    cy = int(b.get("y", 0)) + max(1, bh // 2) if bh > 0 else None

    if (cx is None or cy is None) and fallback_to_visual and fallback_text:
        ocr_res = click_text(fallback_text, approval_granted=approval_granted, policy=policy)
        return {
            "ok": True,
            "backend": "visual_ocr_fallback",
            "fallback_used": True,
            "headless": False,
            "ref": ref_id,
            "action": act,
            "result": ocr_res,
        }

    if cx is None or cy is None:
        return {
            "ok": False,
            "error_code": "NO_BOUNDS",
            "ref": ref_id,
            "action": act,
            "error": "Element has no bounding coordinates and no fallback_text was provided.",
        }

    if act in ("click", "check", "uncheck", "toggle", "expand", "collapse", "select", "focus", "scroll_to"):
        res = perform_approved_ui_action("click", x=cx, y=cy, button="left", clicks=1, approval_granted=approval_granted, policy=policy)
    elif act == "double_click":
        res = perform_approved_ui_action("click", x=cx, y=cy, button="left", clicks=2, approval_granted=approval_granted, policy=policy)
    elif act in ("right_click", "show_menu"):
        res = perform_approved_ui_action("click", x=cx, y=cy, button="right", clicks=1, approval_granted=approval_granted, policy=policy)
    elif act == "hover":
        res = perform_approved_ui_action("move_mouse", x=cx, y=cy, approval_granted=approval_granted, policy=policy)
    elif act == "clear":
        perform_approved_ui_action("click", x=cx, y=cy, button="left", clicks=1, approval_granted=approval_granted, policy=policy)
        perform_approved_ui_action("hotkey", combo="ctrl+a", approval_granted=approval_granted, policy=policy)
        res = perform_approved_ui_action("press_key", key="backspace", approval_granted=approval_granted, policy=policy)
    elif act == "set_value":
        perform_approved_ui_action("click", x=cx, y=cy, button="left", clicks=1, approval_granted=approval_granted, policy=policy)
        perform_approved_ui_action("hotkey", combo="ctrl+a", approval_granted=approval_granted, policy=policy)
        res = perform_approved_ui_action("type_text", text=text, approval_granted=approval_granted, policy=policy)
    elif act == "type":
        perform_approved_ui_action("click", x=cx, y=cy, button="left", clicks=1, approval_granted=approval_granted, policy=policy)
        res = perform_approved_ui_action("type_text", text=text, approval_granted=approval_granted, policy=policy)
    else:
        raise ValueError(f"Unhandled action: {act}")

    return {
        "ok": True,
        "backend": "native_bridge",
        "fallback_used": True,
        "headless": False,
        "ref": ref_id,
        "snapshot_id": entry.snapshot_id,
        "action": act,
        "coords": {"x": cx, "y": cy},
        "result": res,
    }


def a11y_batch(
    steps: list[dict[str, Any]],
    *,
    stop_on_error: bool = True,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict[str, Any]:
    """Execute up to 25 accessibility actions in one atomic, policy-checked round-trip."""
    eff_policy = _enforce_policy("desktop_a11y_batch", approval_granted=approval_granted, policy=policy)
    policy = eff_policy
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list of action objects.")
    if len(steps) > _MAX_BATCH_STEPS:
        raise ValueError(f"Batch exceeds maximum of {_MAX_BATCH_STEPS} steps (got {len(steps)}).")

    results: list[dict[str, Any]] = []
    all_ok = True
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"Batch step {idx} must be a JSON object.")
        estop = _check_emergency_stop()
        if estop is not None:
            results.append({"step": idx, **estop})
            all_ok = False
            break
        kind = str(step.get("command") or step.get("action") or "click").strip().lower()
        if kind == "snapshot":
            out = a11y_snapshot(
                app=str(step.get("app") or ""),
                window_title=str(step.get("window_title") or ""),
                skeleton=bool(step.get("skeleton", False)),
                compact=bool(step.get("compact", True)),
                policy=policy,
            )
        elif kind == "wait":
            out = a11y_wait(
                condition=str(step.get("condition") or "exists"),
                query=str(step.get("query") or ""),
                role=str(step.get("role") or ""),
                window_title=str(step.get("window_title") or ""),
                timeout_seconds=float(step.get("timeout_seconds", 5.0)),
                policy=policy,
            )
        else:
            out = a11y_act(
                str(step.get("ref") or ""),
                action=kind,
                text=str(step.get("text") or ""),
                option=str(step.get("option") or ""),
                headed=bool(step.get("headed", False)),
                fallback_to_visual=bool(step.get("fallback_to_visual", True)),
                fallback_text=str(step.get("fallback_text") or ""),
                approval_granted=approval_granted,
                policy=policy,
            )
        results.append({"step": idx, **out})
        if not out.get("ok"):
            all_ok = False
            if stop_on_error:
                break

    return {
        "ok": all_ok,
        "steps_requested": len(steps),
        "steps_executed": len(results),
        "results": results,
    }


def a11y_wait(
    *,
    condition: str = "exists",
    query: str = "",
    role: str = "",
    app: str = "",
    window_title: str = "",
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.25,
    policy: Policy | None = None,
) -> dict[str, Any]:
    """Wait until an accessibility element or window condition is satisfied."""
    cond = (condition or "exists").strip().lower()
    if cond not in {"exists", "gone", "enabled", "window_open", "window_closed"}:
        raise ValueError(
            f"Invalid wait condition {condition!r}; choose exists, gone, enabled, window_open, or window_closed."
        )
    timeout = max(0.1, min(30.0, float(timeout_seconds)))
    poll = max(0.05, min(2.0, float(poll_interval_seconds)))
    deadline = time.monotonic() + timeout
    started = time.monotonic()

    while True:
        if cond in ("window_open", "window_closed"):
            surf = a11y_list_surfaces(app=app, policy=policy)
            q_win = (window_title or query).strip().lower()
            found_win = [
                s for s in (surf.get("surfaces") or [])
                if not q_win or q_win in str(s.get("title") or "").lower()
            ]
            matched = bool(found_win) if cond == "window_open" else not bool(found_win)
            if matched:
                return {
                    "ok": True,
                    "condition": cond,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "matches": found_win[:5],
                }
        else:
            found = a11y_find(
                query=query,
                role=role,
                app=app,
                window_title=window_title,
                max_results=10,
                policy=policy,
            )
            matches = found.get("matches") or []
            if cond == "exists" and matches:
                return {
                    "ok": True,
                    "condition": cond,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "snapshot_id": found.get("snapshot_id"),
                    "match": matches[0],
                }
            if cond == "gone" and not matches:
                return {
                    "ok": True,
                    "condition": cond,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            if cond == "enabled":
                enabled = [m for m in matches if m.get("enabled", True)]
                if enabled:
                    return {
                        "ok": True,
                        "condition": cond,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "snapshot_id": found.get("snapshot_id"),
                        "match": enabled[0],
                    }

        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "error_code": "WAIT_TIMEOUT",
                "condition": cond,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error": f"Condition {cond!r} not met within {timeout}s.",
            }
        time.sleep(min(poll, max(0.01, deadline - time.monotonic())))

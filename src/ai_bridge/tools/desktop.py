"""Local desktop automation: capture, input, windows, clipboard, SoM, OCR (F-04-06 split of tools.py)."""
# Code in this module was moved verbatim from src/ai_bridge/tools.py
# (F-04-06 package split). Behaviour is unchanged; only the module
# location changed. Import through `ai_bridge.tools` for compatibility.

from __future__ import annotations

import base64
import io
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any

from .. import diagnostics
from ..config import get_settings
from ..policy import Policy
from . import _common  # LAST_FRAME_SCALE lives here (shared mutable state)
from ._common import _LAST_SOM_LOCK, _LAST_SOM_MARKS, _check, _require_no_human_takeover, default_policy, desktop_is_action_vetoed, get_last_som_marks


def _desktop_platform_capabilities() -> dict[str, Any]:
    """Honest per-OS desktop automation / stream / OCR capability matrix (Phase 3)."""
    sys_name = platform.system()
    settings = get_settings()
    desktop_enabled = bool(settings.desktop_enabled)
    if sys_name == "Windows":
        from .. import screen_capture

        session = screen_capture.get_desktop_session_state()
        capture_ok = not bool(session.get("capture_blocked"))
        return {
            "available": desktop_enabled and capture_ok,
            "stream_available": desktop_enabled,
            "ocr_available": desktop_enabled,
            "som_available": desktop_enabled and sys.platform == "win32",
            "webrtc_desktop": desktop_enabled,
            "reason": "" if (desktop_enabled and capture_ok) else (session.get("reason") or "desktop_disabled"),
            "session": "interactive user session required; service-as-SYSTEM is not used for desktop work",
        }
    if sys_name == "Linux":
        has_x11 = bool(os.environ.get("DISPLAY"))
        has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        has_gui = has_x11 or has_wayland
        wayland_only = has_wayland and not has_x11
        xdo = shutil.which("xdotool") is not None
        if not has_gui:
            reason = "linux_headless_no_display"
        elif wayland_only:
            # xdotool is an X11 tool; pure Wayland sessions cannot be driven reliably.
            reason = "linux_wayland_xdotool_unsupported"
        elif not xdo:
            reason = "linux_xdotool_missing"
        else:
            reason = ""
        if not desktop_enabled:
            reason = reason or "desktop_disabled"
        available = bool(desktop_enabled and has_x11 and xdo and not wayland_only)
        return {
            "available": available,
            "stream_available": False,
            "ocr_available": False,
            "som_available": False,
            "webrtc_desktop": False,
            "reason": reason or "limited_linux_x11_xdotool_only",
            "session": (
                "Wayland-only session: xdotool/input automation unsupported without X11 DISPLAY; "
                "no fleet WebRTC desktop stream."
                if wayland_only
                else "Linux X11 session with xdotool required for mouse/keyboard; no fleet WebRTC desktop stream."
            ),
            "input_backend": "none" if wayland_only or not xdo else "xdotool",
            "display": "wayland_only" if wayland_only else ("x11" if has_x11 else "none"),
        }
    if sys_name == "Darwin":
        reason = "" if desktop_enabled else "desktop_disabled"
        return {
            "available": False,
            "stream_available": False,
            "ocr_available": False,
            "som_available": False,
            "webrtc_desktop": False,
            "reason": reason or "macos_desktop_automation_unverified",
            "session": "macOS agent entrypoint exists; desktop stream/OCR not verified on-target.",
        }
    return {
        "available": False,
        "stream_available": False,
        "ocr_available": False,
        "som_available": False,
        "webrtc_desktop": False,
        "reason": f"unsupported_platform:{sys_name}",
        "session": "Desktop automation not supported on this platform.",
    }


def get_desktop_status() -> dict:
    caps = _desktop_platform_capabilities()
    return {
        "desktop_enabled": get_settings().desktop_enabled,
        "system": platform.system(),
        **caps,
    }

def _grab_windows_desktop_fallback():
    """Fallback screenshot on Windows when PIL.ImageGrab.grab() raises OSError
    (e.g. when the RDP session is disconnected/locked and BitBlt on GetDC(0)
    fails with ERROR_INVALID_HANDLE). Composites visible windows on the
    Default desktop using PrintWindow(PW_RENDERFULLCONTENT)."""
    import ctypes
    import struct
    from ctypes import wintypes

    from PIL import Image  # type: ignore

    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    hdesk = u.OpenDesktopW("Default", 0, False, 0x01FF)
    if hdesk:
        u.SetThreadDesktop(hdesk)
    sw = max(u.GetSystemMetrics(0), 800)
    sh = max(u.GetSystemMetrics(1), 600)
    canvas = Image.new("RGB", (sw, sh), (30, 30, 30))
    wins: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_cb(hwnd, _lparam):
        if u.IsWindowVisible(hwnd) and not u.IsIconic(hwnd):
            wins.append(hwnd)
        return True

    cb = WNDENUMPROC(_enum_cb)
    if hdesk:
        u.EnumDesktopWindows(hdesk, cb, 0)
    else:
        u.EnumWindows(cb, 0)
    rect = wintypes.RECT()
    for hwnd in reversed(wins):
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            continue
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 10 or h <= 10 or w > sw * 2 or h > sh * 2:
            continue
        hdc = u.GetWindowDC(hwnd)
        if not hdc:
            continue
        m = g.CreateCompatibleDC(hdc)
        b = g.CreateCompatibleBitmap(hdc, w, h)
        old = g.SelectObject(m, b)
        try:
            if u.PrintWindow(hwnd, m, 2) or u.PrintWindow(hwnd, m, 0):
                bih = ctypes.create_string_buffer(struct.pack("<IiiHHIIIIII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0))
                buf = ctypes.create_string_buffer(w * h * 4)
                if g.GetDIBits(m, b, 0, h, buf, bih, 0):
                    wim = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
                    canvas.paste(wim, (rect.left, rect.top))
        finally:
            g.SelectObject(m, old)
            g.DeleteObject(b)
            g.DeleteDC(m)
            u.ReleaseDC(hwnd, hdc)
    if hdesk:
        u.CloseDesktop(hdesk)
    return canvas

def _encode_image_base64(shot) -> tuple[str, str]:
    """Encode a PIL Image (or mock object) into compact base64 JPEG (or PNG fallback).

    The image is intentionally encoded at its native size: models return click
    coordinates in the pixel space of the image they were shown, and mouse tools
    use physical screen pixels. Resizing here would silently offset every click.
    """
    try:
        img = shot.convert("RGB") if hasattr(shot, "convert") else shot
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:
        return "", "image/png"

def capture_desktop_screenshot(*, approval_granted: bool = False, policy: Policy | None = None) -> dict:
    _check("capture_desktop_screenshot", approval_granted=approval_granted, policy=policy)
    try:
        from PIL import ImageGrab  # type: ignore
    except Exception:
        return {"available": False, "note": "Screenshot requires Pillow (pip install Pillow) and an interactive session."}
    try:
        shot = ImageGrab.grab()
    except OSError:
        if platform.system() != "Windows":
            raise
        shot = _grab_windows_desktop_fallback()
    out = get_settings().data_dir / f"screenshot-{int(time.time())}.png"
    out.parent.mkdir(parents=True, exist_ok=True); shot.save(out)
    b64, mime = _encode_image_base64(shot)
    sw = shot.size[0] if isinstance(shot.size, tuple) else getattr(shot, "width", 0)
    sh = shot.size[1] if isinstance(shot.size, tuple) else getattr(shot, "height", 0)
    active_win = ""
    if platform.system() == "Windows":
        try:
            import ctypes
            fg = ctypes.windll.user32.GetForegroundWindow()
            if fg:
                len_w = ctypes.windll.user32.GetWindowTextLengthW(fg)
                if len_w > 0:
                    t_buf = ctypes.create_unicode_buffer(len_w + 1)
                    ctypes.windll.user32.GetWindowTextW(fg, t_buf, len_w + 1)
                    active_win = t_buf.value.strip()
        except Exception:
            pass
    res = {
        "available": True,
        "path": str(out),
        "size": [sw, sh],
        "width": sw,
        "height": sh,
        "image_width": sw,
        "image_height": sh,
        "active_window": active_win,
    }
    if b64:
        _common.LAST_FRAME_SCALE = 1.0
        res["image_base64"] = b64
        res["mime_type"] = mime
    return res

def _resolve_app_executable(app_name: str) -> str:
    """Resolve an allowlisted app name to an executable path (Windows).

    Plain names resolve through PATH when Popen runs. When that fails on
    Windows, fall back to the App Paths registry keys -- the mechanism
    Windows itself uses to resolve names like chrome/opera/msedge typed
    in Start > Run. Browsers installed outside PATH (Chrome's
    C:\\Program Files\\Google\\Chrome\\Application, Opera's install dir)
    register there, so the allowlist names keep working as typed.
    Returns the input unchanged off Windows, for full paths, or when no
    App Paths entry exists; the allowlist check on the *name* still
    gates the launch either way.
    """
    if os.name != "nt" or "\\" in app_name or "/" in app_name:
        return app_name
    try:
        import winreg
    except ImportError:
        return app_name
    key_name = app_name[:-4] if app_name.lower().endswith(".exe") else app_name
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{key_name}.exe") as key:
                path, _ = winreg.QueryValueEx(key, "")
                if path:
                    return path
        except OSError:
            continue
    return app_name


def launch_approved_application(app_name: str, *, approval_granted: bool = False, url: str | None = None, policy: Policy | None = None) -> dict:
    eff_policy = policy or default_policy()
    _check("launch_approved_application", approval_granted=approval_granted, policy=eff_policy)
    allowed = eff_policy.allowed_apps
    if not eff_policy.unrestricted and "*" not in allowed and app_name not in allowed:
        raise PermissionError(f"Application not in the approved list: {app_name}")
    # Optional URL argument (e.g. open a site in an allowlisted browser).
    # It is passed as a single argv element with shell=False, so it cannot
    # inject extra flags -- and it must be a plain http(s) URL, nothing else.
    argv = [app_name]
    if url is not None:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"URL argument must be an http(s) URL: {url}")
        argv.append(url)
    import subprocess
    try:
        subprocess.Popen(argv, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        resolved = _resolve_app_executable(app_name)
        if resolved == app_name:
            raise ValueError(f"Approved application not found: {app_name}")
        argv[0] = resolved
        try:
            subprocess.Popen(argv, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            raise ValueError(f"Approved application not found: {app_name}")
    return {"launched": app_name, "url": url} if url is not None else {"launched": app_name}


# --- Interactive UI Automation (Tier 3, desktop_enabled required) --------
_VK_MAP: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "select": 0x29, "print": 0x2A, "execute": 0x2B, "printscreen": 0x2C, "prtsc": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "help": 0x2F,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69, "multiply": 0x6A, "add": 0x6B,
    "separator": 0x6C, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "numlock": 0x90, "scrolllock": 0x91,
}

def _vk_for_token(token: str) -> int:
    t = token.strip().lower()
    if t in _VK_MAP:
        return _VK_MAP[t]
    if len(t) == 1 and ("a" <= t <= "z" or "0" <= t <= "9"):
        return ord(t.upper())
    raise ValueError(f"Unsupported key token: {token!r}")

def _win32_ui_action(action: str, *, x: int | None, y: int | None, text: str | None,
                     key: str | None, title: str | None, delta: int,
                     start_x: int | None = None, start_y: int | None = None) -> dict:
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass

    hdesk = u.OpenDesktopW("Default", 0, False, 0x01FF)
    if hdesk:
        u.SetThreadDesktop(hdesk)
    try:
        if action == "get_screen_size":
            sw = u.GetSystemMetrics(0)
            sh = u.GetSystemMetrics(1)
            return {"action": "get_screen_size", "width": sw, "height": sh}

        if action in {"list_windows", "focus_window"}:
            windows: list[dict] = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            rect = wintypes.RECT()

            def _cb(hwnd, _lp):
                if not u.IsWindowVisible(hwnd):
                    return True
                length = u.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                u.GetWindowTextW(hwnd, buf, length + 1)
                wtitle = buf.value.strip()
                if not wtitle:
                    return True
                u.GetWindowRect(hwnd, ctypes.byref(rect))
                windows.append({
                    "hwnd": int(hwnd),
                    "title": wtitle,
                    "minimized": bool(u.IsIconic(hwnd)),
                    "bounds": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
                })
                return True

            cb = WNDENUMPROC(_cb)
            if hdesk:
                u.EnumDesktopWindows(hdesk, cb, 0)
            else:
                u.EnumWindows(cb, 0)
            if action == "list_windows":
                return {"action": "list_windows", "windows": windows[:100]}
            if not title or not title.strip():
                raise ValueError("focus_window requires a non-empty 'title' substring.")
            needle = title.strip().lower()
            match = next((w for w in windows if needle in w["title"].lower()), None)
            if match is None:
                raise ValueError(f"No visible window matching {title!r}.")
            hwnd = match["hwnd"]
            if match["minimized"]:
                u.ShowWindow(hwnd, 9)  # SW_RESTORE
            u.SetForegroundWindow(hwnd)
            return {"action": "focus_window", "focused": match["title"], "hwnd": hwnd}

        if action in {"move_mouse", "click", "double_click", "triple_click", "right_click", "middle_click"}:
            if x is None or y is None:
                raise ValueError(f"{action} requires integer x and y coordinates.")
            ix, iy = int(x), int(y)
            u.SetCursorPos(ix, iy)
            if action == "click":
                u.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                u.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
            elif action == "double_click":
                for _ in range(2):
                    u.mouse_event(0x0002, 0, 0, 0, 0)
                    u.mouse_event(0x0004, 0, 0, 0, 0)
                    time.sleep(0.04)
            elif action == "triple_click":
                for _ in range(3):
                    u.mouse_event(0x0002, 0, 0, 0, 0)
                    u.mouse_event(0x0004, 0, 0, 0, 0)
                    time.sleep(0.04)
            elif action == "right_click":
                u.mouse_event(0x0008, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTDOWN
                u.mouse_event(0x0010, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTUP
            elif action == "middle_click":
                u.mouse_event(0x0020, 0, 0, 0, 0)  # MOUSEEVENTF_MIDDLEDOWN
                u.mouse_event(0x0040, 0, 0, 0, 0)  # MOUSEEVENTF_MIDDLEUP
            return {"action": action, "x": ix, "y": iy}

        if action in {"drag", "mouse_drag"}:
            sx = int(start_x if start_x is not None else (x or 0))
            sy = int(start_y if start_y is not None else (y or 0))
            ex = int(x if x is not None else sx)
            ey = int(y if y is not None else sy)
            u.SetCursorPos(sx, sy)
            time.sleep(0.02)
            u.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
            steps = 15
            for s in range(1, steps + 1):
                cx = int(sx + (ex - sx) * (s / steps))
                cy = int(sy + (ey - sy) * (s / steps))
                u.SetCursorPos(cx, cy)
                time.sleep(0.01)
            u.SetCursorPos(ex, ey)
            time.sleep(0.02)
            u.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
            return {"action": "drag", "start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey}

        if action == "scroll":
            if x is not None and y is not None:
                u.SetCursorPos(int(x), int(y))
            u.mouse_event(0x0800, 0, 0, int(delta or -120), 0)  # MOUSEEVENTF_WHEEL
            return {"action": "scroll", "delta": int(delta or -120)}

        if action == "type_text":
            if not isinstance(text, str) or not text:
                raise ValueError("type_text requires a non-empty 'text' string.")
            if len(text) > 4000:
                raise ValueError("type_text is capped at 4000 characters per call.")

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_void_p)]
            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                            ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]
            class HARDWAREINPUT(ctypes.Structure):
                _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                            ("wParamH", wintypes.WORD)]
            class _INPUT_U(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]
            class INPUT(ctypes.Structure):
                _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_U)]

            fg = u.GetForegroundWindow()
            for ch in text:
                if ch == "\n":
                    u.keybd_event(0x0D, 0, 0, 0)
                    u.keybd_event(0x0D, 0, 0x0002, 0)
                    time.sleep(0.01)
                    continue
                if ch == "\t":
                    u.keybd_event(0x09, 0, 0, 0)
                    u.keybd_event(0x09, 0, 0x0002, 0)
                    time.sleep(0.01)
                    continue
                try:
                    c_code = ord(ch)
                    inp_down = INPUT(type=1)
                    inp_down.u.ki.wVk = 0
                    inp_down.u.ki.wScan = c_code
                    inp_down.u.ki.dwFlags = 0x0004  # KEYEVENTF_UNICODE
                    inp_down.u.ki.time = 0
                    inp_down.u.ki.dwExtraInfo = None

                    inp_up = INPUT(type=1)
                    inp_up.u.ki.wVk = 0
                    inp_up.u.ki.wScan = c_code
                    inp_up.u.ki.dwFlags = 0x0004 | 0x0002  # KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
                    inp_up.u.ki.time = 0
                    inp_up.u.ki.dwExtraInfo = None

                    inputs = (INPUT * 2)(inp_down, inp_up)
                    u.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
                except Exception:
                    if fg:
                        u.PostMessageW(fg, 0x0102, ord(ch), 0)
                time.sleep(0.005)
            return {"action": "type_text", "length": len(text)}

        if action in {"press_key", "hotkey"}:
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{action} requires a 'key' string (e.g. 'enter' or 'ctrl+l').")
            parts = [p.strip() for p in key.split("+") if p.strip()]
            vks = [_vk_for_token(p) for p in parts]
            for vk in vks:
                u.keybd_event(vk, 0, 0, 0)
            time.sleep(0.01)
            for vk in reversed(vks):
                u.keybd_event(vk, 0, 0x0002, 0)  # KEYEVENTF_KEYUP
            return {"action": action, "key": key}
    finally:
        if hdesk:
            u.CloseDesktop(hdesk)
    raise ValueError(f"Unsupported UI action: {action!r}")


def perform_approved_ui_action(
    action: str,
    *,
    x: int | None = None,
    y: int | None = None,
    text: str | None = None,
    key: str | None = None,
    title: str | None = None,
    delta: int = -120,
    start_x: int | None = None,
    start_y: int | None = None,
    approval_granted: bool = False,
    policy: Policy | None = None,
    intent_id: str | None = None,
) -> dict:
    """Perform a bounded desktop UI action when desktop automation is enabled.

    Supported actions:
      - list_windows: list visible top-level windows with titles and bounds
      - focus_window: bring a window matching 'title' substring to the foreground
      - move_mouse, click, double_click, triple_click, right_click, middle_click: mouse action at (x, y)
      - drag / mouse_drag: mouse drag from (start_x, start_y) to (x, y)
      - scroll: mouse wheel scroll by 'delta' (optionally at x, y)
      - type_text: type 'text' into the focused window/control using Unicode SendInput
      - press_key / hotkey: press a key or combo like 'enter', 'esc', 'ctrl+l', 'alt+tab'
      - get_screen_size: get desktop screen dimensions
    """
    eff_policy = policy or default_policy()
    _check("perform_approved_ui_action", approval_granted=approval_granted, policy=eff_policy)
    if intent_id and desktop_is_action_vetoed(intent_id):
        raise PermissionError(f"Operator vetoed action (intent_id={intent_id}).")
    _require_no_human_takeover("local")
    allowed_actions = {
        "list_windows", "focus_window", "move_mouse", "click",
        "double_click", "triple_click", "right_click", "middle_click",
        "scroll", "type_text", "press_key", "hotkey", "drag", "mouse_drag",
        "get_screen_size",
        "clipboard_get", "clipboard_set", "get_clipboard", "set_clipboard",
    }
    if action not in allowed_actions:
        raise ValueError(f"Unsupported UI action {action!r}. Choose one of: {', '.join(sorted(allowed_actions))}")

    if action in {"clipboard_get", "get_clipboard"}:
        return get_clipboard(policy=eff_policy)
    if action in {"clipboard_set", "set_clipboard"}:
        if text is None:
            raise ValueError(f"Action '{action}' requires a 'text' parameter.")
        return set_clipboard(str(text), paste=False, approval_granted=approval_granted, policy=eff_policy)

    if action in {"click", "double_click", "triple_click", "right_click", "middle_click", "move_mouse"}:
        if x is None or y is None:
            raise ValueError(f"Action '{action}' requires integer x and y coordinates.")
    if action in {"drag", "mouse_drag"}:
        if x is None or y is None or start_x is None or start_y is None:
            raise ValueError(f"Action '{action}' requires start_x, start_y, x, and y coordinates.")
    if action == "type_text" and not text:
        raise ValueError("Action 'type_text' requires a non-empty string in 'text'.")
    if action in {"press_key", "hotkey"} and not key:
        raise ValueError(f"Action '{action}' requires a 'key' parameter.")
    if action == "focus_window" and not title:
        raise ValueError("Action 'focus_window' requires a non-empty window title in 'title'.")

    if platform.system() == "Windows":
        extra = {}
        if start_x is not None or start_y is not None:
            extra["start_x"] = start_x
            extra["start_y"] = start_y
        return _win32_ui_action(
            action, x=x, y=y, text=text, key=key, title=title, delta=delta, **extra
        )
    if platform.system() == "Darwin":
        return {
            "available": False,
            "reason": "macos_desktop_automation_unverified",
            "note": "macOS desktop input automation is not verified on-target.",
        }
    # Linux: xdotool requires an X11 DISPLAY. Pure Wayland sessions are unsupported.
    if bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY"):
        return {
            "available": False,
            "reason": "linux_wayland_xdotool_unsupported",
            "note": "UI automation on Wayland-only sessions is unsupported; needs X11 DISPLAY + xdotool.",
        }
    if shutil.which("xdotool") is None:
        return {
            "available": False,
            "reason": "linux_xdotool_missing",
            "note": "UI automation on Linux requires xdotool under X11 (or Windows user32).",
        }
    if action == "get_screen_size":
        out = diagnostics._run(["xdotool", "getdisplaygeometry"])
        parts = out.strip().split()
        if len(parts) >= 2:
            try:
                return {"action": "get_screen_size", "width": int(parts[0]), "height": int(parts[1])}
            except Exception:
                pass
        return {"action": "get_screen_size", "output": out}
    if action == "list_windows":
        out = diagnostics._run(["xdotool", "search", "--onlyvisible", "--name", "."])
        return {"action": "list_windows", "output": out}
    if action in {"move_mouse", "click", "double_click", "triple_click", "right_click", "middle_click"}:
        if x is None or y is None:
            raise ValueError(f"{action} requires integer x and y coordinates.")
        argv = ["xdotool", "mousemove", str(int(x)), str(int(y))]
        if action == "click":
            argv += ["click", "1"]
        elif action == "double_click":
            argv += ["click", "--repeat", "2", "1"]
        elif action == "triple_click":
            argv += ["click", "--repeat", "3", "1"]
        elif action == "right_click":
            argv += ["click", "3"]
        elif action == "middle_click":
            argv += ["click", "2"]
        diagnostics._run(argv)
        return {"action": action, "x": int(x), "y": int(y)}
    if action in {"drag", "mouse_drag"}:
        sx = int(start_x if start_x is not None else (x or 0))
        sy = int(start_y if start_y is not None else (y or 0))
        ex = int(x if x is not None else sx)
        ey = int(y if y is not None else sy)
        diagnostics._run(["xdotool", "mousemove", str(sx), str(sy), "mousedown", "1",
                         "mousemove", str(ex), str(ey), "mouseup", "1"])
        return {"action": "drag", "start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey}
    if action == "type_text":
        if not isinstance(text, str) or not text:
            raise ValueError("type_text requires a non-empty 'text' string.")
        diagnostics._run(["xdotool", "type", "--delay", "10", "--", text[:4000]])
        return {"action": "type_text", "length": len(text[:4000])}
    if action in {"press_key", "hotkey"}:
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{action} requires a 'key' string.")
        diagnostics._run(["xdotool", "key", "--", key.strip()])
        return {"action": action, "key": key.strip()}
    if action == "focus_window":
        if not title or not title.strip():
            raise ValueError("focus_window requires a non-empty 'title' substring.")
        out = diagnostics._run(["xdotool", "search", "--onlyvisible", "--name", title.strip(), "windowactivate"])
        return {"action": "focus_window", "focused": title.strip(), "output": out}
    if action == "scroll":
        if x is not None and y is not None:
            diagnostics._run(["xdotool", "mousemove", str(int(x)), str(int(y))])
        btn = "5" if int(delta or -120) < 0 else "4"
        diagnostics._run(["xdotool", "click", btn])
        return {"action": "scroll", "delta": int(delta or -120)}
    raise ValueError(f"Unsupported UI action: {action!r}")


# --- Dedicated High-Level Desktop GUI Tools (Mouse & Keyboard) -----------

def mouse_click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
    intent_id: str | None = None,
) -> dict:
    """Click the mouse at (x, y) coordinates with specified button and click count."""
    eff_policy = policy or default_policy()
    _check("mouse_click", approval_granted=approval_granted, policy=eff_policy)
    btn = (button or "left").lower()
    act = "click"
    if clicks == 2 or btn == "double":
        act = "double_click"
    elif clicks >= 3 or btn == "triple":
        act = "triple_click"
    elif btn == "right":
        act = "right_click"
    elif btn == "middle":
        act = "middle_click"
    return perform_approved_ui_action(
        act, x=x, y=y, approval_granted=approval_granted, policy=eff_policy, intent_id=intent_id
    )


def mouse_move(
    x: int,
    y: int,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Move the mouse cursor to (x, y) coordinates."""
    eff_policy = policy or default_policy()
    _check("mouse_move", approval_granted=approval_granted, policy=eff_policy)
    return perform_approved_ui_action(
        "move_mouse", x=x, y=y, approval_granted=approval_granted, policy=eff_policy
    )


def mouse_scroll(
    delta: int = -120,
    x: int | None = None,
    y: int | None = None,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Scroll the mouse wheel. Positive delta scrolls up; negative delta scrolls down."""
    eff_policy = policy or default_policy()
    _check("mouse_scroll", approval_granted=approval_granted, policy=eff_policy)
    return perform_approved_ui_action(
        "scroll", x=x, y=y, delta=delta, approval_granted=approval_granted, policy=eff_policy
    )


def mouse_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Click and drag the mouse from (start_x, start_y) to (end_x, end_y)."""
    eff_policy = policy or default_policy()
    _check("mouse_drag", approval_granted=approval_granted, policy=eff_policy)
    return perform_approved_ui_action(
        "drag", x=end_x, y=end_y, start_x=start_x, start_y=start_y,
        approval_granted=approval_granted, policy=eff_policy
    )


def keyboard_type(
    text: str,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Type unicode text into the currently active control or window."""
    eff_policy = policy or default_policy()
    _check("keyboard_type", approval_granted=approval_granted, policy=eff_policy)
    return perform_approved_ui_action(
        "type_text", text=text, approval_granted=approval_granted, policy=eff_policy
    )


def keyboard_press(
    key: str,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Press a key or hotkey combination (e.g. 'enter', 'tab', 'esc', 'ctrl+c', 'win')."""
    eff_policy = policy or default_policy()
    _check("keyboard_press", approval_granted=approval_granted, policy=eff_policy)
    return perform_approved_ui_action(
        "press_key", key=key, approval_granted=approval_granted, policy=eff_policy
    )


def list_desktop_windows(
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """List visible top-level windows on the desktop with their titles and bounds."""
    eff_policy = policy or default_policy()
    _check("list_desktop_windows", approval_granted=approval_granted, policy=eff_policy)
    return perform_approved_ui_action(
        "list_windows", approval_granted=approval_granted, policy=eff_policy
    )


def focus_window(
    title: str,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Bring the window matching 'title' to the foreground."""
    eff_policy = policy or default_policy()
    _check("focus_window", approval_granted=approval_granted, policy=eff_policy)
    return perform_approved_ui_action(
        "focus_window", title=title, approval_granted=approval_granted, policy=eff_policy
    )


def get_screen_size(
    *,
    policy: Policy | None = None,
) -> dict:
    """Get the primary desktop screen width and height in pixels."""
    eff_policy = policy or default_policy()
    _check("get_screen_size", approval_granted=True, policy=eff_policy)
    return perform_approved_ui_action(
        "get_screen_size", approval_granted=True, policy=eff_policy
    )


def ocr_screen(*, approval_granted: bool = False, policy: Policy | None = None) -> dict:
    """Run native OCR on the current desktop screen and return recognized text with coordinates."""
    eff_policy = policy or default_policy()
    _check("ocr_screen", approval_granted=approval_granted, policy=eff_policy)
    from .. import vision
    shot_res = capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)
    if not shot_res.get("available") or not shot_res.get("path"):
        return {"ok": False, "error": "Unable to capture desktop screenshot for OCR."}
    ocr_res = vision.run_ocr(shot_res["path"])
    return {
        "ok": ocr_res.get("ok", False),
        "text": ocr_res.get("text", ""),
        "line_count": len(ocr_res.get("lines", [])),
        "lines": ocr_res.get("lines", []),
        "words": ocr_res.get("words", []),
        "screenshot_path": shot_res.get("path"),
    }


def click_text(
    text: str,
    *,
    occurrence: int = 1,
    button: str = "left",
    clicks: int = 1,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Find text on screen using native OCR + UI grounding and click its center (no pixel guessing)."""
    eff_policy = policy or default_policy()
    _check("click_text", approval_granted=approval_granted, policy=eff_policy)
    from .. import vision
    shot_res = capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)
    img_path = shot_res.get("path") if shot_res.get("available") else None
    res = vision.find_text_coordinates(text, image_path=img_path, occurrence=occurrence)
    if not res.get("found") or not res.get("match"):
        raise ValueError(f"Text {text!r} not found on screen.")

    cx, cy = res["x"], res["y"]
    action_type = "double_click" if clicks == 2 else "triple_click" if clicks >= 3 else "right_click" if button == "right" else "click"
    ui_res = perform_approved_ui_action(action_type, x=cx, y=cy, approval_granted=approval_granted, policy=eff_policy)
    return {
        "ok": True,
        "action": "click_text",
        "clicked_text": text,
        "matched_text": res["match"].get("text"),
        "x": cx,
        "y": cy,
        "bounds": res["match"].get("bounds"),
        "button": button,
        "clicks": clicks,
        "ui_result": ui_res,
    }


def click_element(
    mark_id: str | None = None,
    name: str | None = None,
    *,
    button: str = "left",
    clicks: int = 1,
    approval_granted: bool = False,
    policy: Policy | None = None,
    intent_id: str | None = None,
) -> dict:
    """Click a labeled mark (from set_of_marks_screenshot) or element name."""
    eff_policy = policy or default_policy()
    _check("click_element", approval_granted=approval_granted, policy=eff_policy)
    if intent_id and desktop_is_action_vetoed(intent_id):
        raise PermissionError(f"Operator vetoed action (intent_id={intent_id}).")

    marks = get_last_som_marks()
    mark_key = str(mark_id).strip() if mark_id is not None else ""
    if mark_key and mark_key in marks:
        entry = marks[mark_key]
        cx = int(entry.get("x") or entry.get("center", [0, 0])[0])
        cy = int(entry.get("y") or entry.get("center", [0, 0])[1])
        action_type = (
            "double_click" if clicks == 2 else "triple_click" if clicks >= 3
            else "right_click" if button == "right" else "click"
        )
        ui_res = perform_approved_ui_action(
            action_type,
            x=cx,
            y=cy,
            approval_granted=approval_granted,
            policy=eff_policy,
            intent_id=intent_id,
        )
        return {
            "ok": True,
            "action": "click_element",
            "grounding": "som_mark",
            "mark_id": mark_key,
            "x": cx,
            "y": cy,
            "bounds": entry.get("bounds"),
            "label": entry.get("text", ""),
            "ui_result": ui_res,
        }

    target_text = mark_key if mark_key else str(name or "").strip()
    if not target_text:
        raise ValueError("Either 'mark_id' or 'name' must be provided to click_element.")
    # When a SoM cache is active, unknown mark ids must not silently OCR-click —
    # that would break trustworthy grounding. Empty cache → OCR/name fallback.
    if marks and mark_key:
        raise ValueError(
            f"mark_id {mark_key!r} not in cached SoM marks; refresh set_of_marks_screenshot "
            f"(available ids: {', '.join(sorted(marks.keys())[:12])})."
        )
    return click_text(target_text, button=button, clicks=clicks, approval_granted=approval_granted, policy=eff_policy)


def list_ui_elements(*, approval_granted: bool = False, policy: Policy | None = None) -> dict:
    """List visible interactive Windows controls (menus, buttons, input fields, child controls) with labels and bounds."""
    eff_policy = policy or default_policy()
    _check("list_ui_elements", approval_granted=approval_granted, policy=eff_policy)
    from .. import vision
    ctrls = vision.list_win32_ui_controls()
    return {"ok": True, "count": len(ctrls), "elements": ctrls}


def set_of_marks_screenshot(
    *,
    max_marks: int = 40,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Capture a screenshot with numbered Set-of-Marks badges ([1], [2], [3]) overlaid over interactive elements."""
    eff_policy = policy or default_policy()
    _check("set_of_marks_screenshot", approval_granted=approval_granted, policy=eff_policy)
    from .. import vision
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow is required for set_of_marks_screenshot.")

    shot_res = capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)
    if not shot_res.get("available") or not shot_res.get("path"):
        return {"available": False, "note": "Screenshot unavailable."}

    img = Image.open(shot_res["path"])
    ann_img, marks = vision.generate_set_of_marks(img, image_path=shot_res["path"], max_marks=max_marks)

    out = get_settings().data_dir / f"annotated-screenshot-{int(time.time())}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    ann_img.save(out)
    b64, mime = _encode_image_base64(ann_img)

    with _LAST_SOM_LOCK:
        _LAST_SOM_MARKS.clear()
        _LAST_SOM_MARKS.update(marks if isinstance(marks, dict) else {})

    return {
        "ok": True,
        "available": True,
        "path": str(out),
        "image_base64": b64,
        "mime_type": mime,
        "mark_count": len(marks),
        "marks": marks,
        "grounding": "som_marks_cached",
    }


def _win32_get_clipboard() -> tuple[bool, str]:
    """Read CF_UNICODETEXT directly via Win32 user32/kernel32 (sub-millisecond, no PowerShell cold start)."""
    import ctypes

    cf_unicodetext = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool

    if not user32.OpenClipboard(None):
        return False, ""
    try:
        handle = user32.GetClipboardData(cf_unicodetext)
        if not handle:
            return True, ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False, ""
        try:
            text = ctypes.wstring_at(ptr)
            return True, text.rstrip("\r\n")
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _win32_set_clipboard(content: str) -> bool:
    """Write CF_UNICODETEXT directly via Win32 user32/kernel32 (sub-millisecond, no PowerShell cold start)."""
    import ctypes

    cf_unicodetext = 13
    gmem_moveable = 0x0002
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    encoded = content.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        return False
    hmem = None
    try:
        user32.EmptyClipboard()
        hmem = kernel32.GlobalAlloc(gmem_moveable, len(encoded))
        if not hmem:
            return False
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            return False
        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(hmem)
        if not user32.SetClipboardData(cf_unicodetext, hmem):
            kernel32.GlobalFree(hmem)
            return False
        hmem = None  # Ownership transferred to Windows clipboard
        return True
    finally:
        user32.CloseClipboard()


def get_clipboard(*, policy: Policy | None = None) -> dict:
    """Read the current system clipboard text on Windows, Linux, or macOS."""
    eff_policy = policy or default_policy()
    _check("get_clipboard", policy=eff_policy)
    current_os = platform.system()
    if current_os == "Windows":
        try:
            ok, text = _win32_get_clipboard()
            if ok:
                return {"ok": True, "text": text, "length": len(text), "system": current_os}
        except Exception:
            pass
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        text = (proc.stdout or "").rstrip("\r\n")
        return {"ok": proc.returncode == 0, "text": text, "length": len(text), "system": current_os}

    for argv in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["pbpaste"],
    ):
        if shutil.which(argv[0]):
            proc = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
            if proc.returncode == 0:
                text = proc.stdout or ""
                return {"ok": True, "text": text, "length": len(text), "system": current_os}
    return {"ok": False, "text": "", "length": 0, "system": current_os, "note": "No clipboard utility available."}


def set_clipboard(
    text: str,
    *,
    paste: bool = False,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Write text to the system clipboard and optionally trigger Ctrl+V paste into the active window."""
    eff_policy = policy or default_policy()
    _check("set_clipboard", approval_granted=approval_granted, policy=eff_policy)
    content = str(text or "")
    current_os = platform.system()
    ok = False

    if current_os == "Windows":
        try:
            ok = _win32_set_clipboard(content)
        except Exception:
            ok = False
        if not ok:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
                input=content,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
            ok = proc.returncode == 0
    else:
        for argv in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
            ["pbcopy"],
        ):
            if shutil.which(argv[0]):
                proc = subprocess.run(argv, input=content, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
                if proc.returncode == 0:
                    ok = True
                    break

    pasted = False
    if ok and paste:
        time.sleep(0.15)
        try:
            press_hotkey("ctrl+v", approval_granted=approval_granted, policy=eff_policy)
            pasted = True
        except Exception:
            pasted = False

    return {"ok": ok, "length": len(content), "pasted": pasted, "system": current_os}


def wait_for_text(
    text: str,
    *,
    timeout_seconds: float = 15.0,
    poll_interval: float = 1.0,
    occurrence: int = 1,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Poll screen with native OCR until specified text appears or timeout is reached."""
    eff_policy = policy or default_policy()
    _check("wait_for_text", approval_granted=approval_granted, policy=eff_policy)
    from .. import vision

    start_t = time.time()
    deadline = start_t + max(1.0, float(timeout_seconds))
    interval = max(0.2, min(float(poll_interval), 5.0))
    attempts = 0

    from ..control.task_runtime import cooperative_sleep, raise_if_cancelled

    while True:
        raise_if_cancelled()
        attempts += 1
        shot_res = capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)
        img_path = shot_res.get("path") if shot_res.get("available") else None
        res = vision.find_text_coordinates(text, image_path=img_path, occurrence=occurrence)
        if res.get("found") and res.get("match"):
            elapsed = round(time.time() - start_t, 2)
            return {
                "ok": True,
                "found": True,
                "text": text,
                "matched_text": res["match"].get("text"),
                "elapsed_seconds": elapsed,
                "attempts": attempts,
                "x": res["x"],
                "y": res["y"],
                "bounds": res["match"].get("bounds"),
            }
        now = time.time()
        if now >= deadline:
            elapsed = round(now - start_t, 2)
            return {
                "ok": False,
                "found": False,
                "text": text,
                "elapsed_seconds": elapsed,
                "attempts": attempts,
                "error": f"Timeout waiting for text '{text}' on screen after {elapsed}s.",
            }
        rem = deadline - now
        cooperative_sleep(min(interval, rem), interval=0.25)


def verify_screen_contains(
    text: str,
    *,
    occurrence: int = 1,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """One-shot instant verification: check if text is currently displayed on screen."""
    eff_policy = policy or default_policy()
    _check("verify_screen_contains", approval_granted=approval_granted, policy=eff_policy)
    from .. import vision

    shot_res = capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)
    img_path = shot_res.get("path") if shot_res.get("available") else None
    res = vision.find_text_coordinates(text, image_path=img_path, occurrence=occurrence)
    return {
        "ok": True,
        "contains": bool(res.get("found")),
        "text": text,
        "matched_text": res.get("match", {}).get("text") if res.get("match") else None,
        "bounds": res.get("match", {}).get("bounds") if res.get("match") else None,
        "center": [res["x"], res["y"]] if res.get("found") else None,
    }


def press_hotkey(
    combo: str,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Press a standard keyboard shortcut or hotkey (e.g. 'ctrl+s', 'ctrl+t', 'alt+tab', 'enter')."""
    eff_policy = policy or default_policy()
    _check("press_hotkey", approval_granted=approval_granted, policy=eff_policy)
    c = (combo or "").strip().lower()
    if not c:
        raise ValueError("Hotkey combo must not be empty.")
    return perform_approved_ui_action("hotkey", key=c, approval_granted=approval_granted, policy=eff_policy)

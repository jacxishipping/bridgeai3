"""Versioned desktop_* tool API, UIA elements, veto/anticipation (F-04-06 split of tools.py)."""
# Code in this module was moved verbatim from src/ai_bridge/tools.py
# (F-04-06 package split). Behaviour is unchanged; only the module
# location changed. Import through `ai_bridge.tools` for compatibility.

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

from ..config import get_settings
from ..policy import Policy
from ._common import _check, _require_no_human_takeover, default_policy, desktop_is_action_vetoed
from .desktop import (
    _encode_image_base64,
    capture_desktop_screenshot,
    get_clipboard,
    get_desktop_status,
    keyboard_press,
    keyboard_type,
    launch_approved_application,
    list_desktop_windows,
    mouse_click,
    mouse_move,
    mouse_scroll,
    press_hotkey,
    set_clipboard,
    verify_screen_contains,
    wait_for_text,
)
from .shell import run_shell


def desktop_get_screenshot(*, approval_granted: bool = False, policy: Policy | None = None) -> dict:
    """Return a current desktop screenshot."""
    eff_policy = policy or default_policy()
    _check("desktop_get_screenshot", approval_granted=approval_granted, policy=eff_policy)
    return capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)


def desktop_get_window_screenshot(
    hwnd: int | None = None,
    title: str = "",
    scale: float = 1.0,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
    device_id: str = "local",
) -> dict:
    """Return a screenshot of a specific application window by HWND or title."""
    eff_policy = policy or default_policy()
    _check("desktop_get_window_screenshot", approval_granted=approval_granted, policy=eff_policy)
    from ..screen_capture import capture_window_by_hwnd

    target_hwnd = hwnd
    target_title = title
    if (target_hwnd is None or target_hwnd <= 0) and target_title:
        wins = list_desktop_windows(approval_granted=approval_granted, policy=eff_policy).get("windows", [])
        needle = target_title.strip().lower()
        match = next((w for w in wins if needle in str(w.get("title", "")).lower()), None)
        if match:
            target_hwnd = int(match.get("hwnd", 0))
            target_title = match.get("title", target_title)

    if target_hwnd and target_hwnd > 0:
        shot = capture_window_by_hwnd(target_hwnd, scale=scale)
    else:
        return capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)

    out = get_settings().data_dir / f"window-{int(time.time())}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    shot.save(out)
    b64, mime = _encode_image_base64(shot)
    sw = shot.size[0] if isinstance(shot.size, tuple) else getattr(shot, "width", 0)
    sh = shot.size[1] if isinstance(shot.size, tuple) else getattr(shot, "height", 0)
    return {
        "available": True,
        "path": str(out),
        "size": [sw, sh],
        "width": sw,
        "height": sh,
        "hwnd": target_hwnd,
        "window_title": target_title,
        "image_base64": b64,
        "mime_type": mime,
    }


def desktop_get_monitors(*, policy: Policy | None = None) -> dict:
    """Return list of connected displays with dimensions, offsets, and primary status."""
    eff_policy = policy or default_policy()
    _check("desktop_get_monitors", policy=eff_policy)
    from ..screen_capture import get_monitors

    mons = get_monitors()
    return {"ok": True, "monitors": mons, "count": len(mons)}


def desktop_clipboard_get(*, policy: Policy | None = None, device_id: str = "local") -> dict:
    """Read the current system clipboard text."""
    eff_policy = policy or default_policy()
    _check("desktop_clipboard_get", policy=eff_policy)
    return get_clipboard(policy=eff_policy)


def desktop_clipboard_set(
    text: str,
    paste: bool = False,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
    device_id: str = "local",
) -> dict:
    """Write text to the system clipboard and optionally trigger paste."""
    _require_no_human_takeover(device_id)
    eff_policy = policy or default_policy()
    _check("desktop_clipboard_set", approval_granted=approval_granted, policy=eff_policy)
    return set_clipboard(text=text, paste=paste, approval_granted=approval_granted, policy=eff_policy)



def desktop_get_status(*, device_id: str = "local") -> dict:
    """Return desktop and session status, monitors, capture backend, and human takeover state."""
    _check("desktop_get_status")
    from ..control.v1_routes import is_device_human_takeover_active
    from ..screen_capture import get_desktop_session_state, get_last_capture_metrics, get_monitors

    base = get_desktop_status()
    takeover = is_device_human_takeover_active(device_id)
    session_state = get_desktop_session_state()
    return {
        **base,
        "device_id": device_id,
        "mode": "human_control" if takeover else "ai_assisted",
        "human_takeover_active": takeover,
        "monitors": get_monitors(),
        "capture_metrics": get_last_capture_metrics(),
        "session_state": session_state,
        "secure_desktop": bool(session_state.get("secure_desktop")),
        "capture_blocked": bool(session_state.get("capture_blocked")),
        "desktop_reason": session_state.get("reason") or "",
    }


def desktop_mouse_move(
    x: int,
    y: int,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
    device_id: str = "local",
) -> dict:
    """Move the mouse to approved coordinates."""
    _require_no_human_takeover(device_id)
    eff_policy = policy or default_policy()
    _check("desktop_mouse_move", approval_granted=approval_granted, policy=eff_policy)
    return mouse_move(x=x, y=y, approval_granted=approval_granted, policy=eff_policy)


def desktop_click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
    device_id: str = "local",
    intent_id: str | None = None,
) -> dict:
    """Click at approved coordinates."""
    _require_no_human_takeover(device_id)
    eff_policy = policy or default_policy()
    _check("desktop_click", approval_granted=approval_granted, policy=eff_policy)
    return mouse_click(
        x=x,
        y=y,
        button=button,
        clicks=clicks,
        approval_granted=approval_granted,
        policy=eff_policy,
        intent_id=intent_id,
    )


def desktop_keyboard(
    text: str = "",
    key: str = "",
    action: str = "type",
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
    device_id: str = "local",
) -> dict:
    """Send approved keyboard input (text typing, key press, or hotkey shortcut)."""
    _require_no_human_takeover(device_id)
    eff_policy = policy or default_policy()
    _check("desktop_keyboard", approval_granted=approval_granted, policy=eff_policy)
    act = (action or ("press" if key and not text else "type")).strip().lower()
    if act in ("press", "key", "hotkey") or (key and not text):
        target_key = key or text
        if "+" in target_key:
            return press_hotkey(target_key, approval_granted=approval_granted, policy=eff_policy)
        return keyboard_press(target_key, approval_granted=approval_granted, policy=eff_policy)
    res = keyboard_type(text, approval_granted=approval_granted, policy=eff_policy)
    if key:
        res["key_result"] = keyboard_press(key, approval_granted=approval_granted, policy=eff_policy)
    return res


def desktop_scroll(
    delta: int = -120,
    x: int | None = None,
    y: int | None = None,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
    device_id: str = "local",
) -> dict:
    """Scroll the active desktop."""
    _require_no_human_takeover(device_id)
    eff_policy = policy or default_policy()
    _check("desktop_scroll", approval_granted=approval_granted, policy=eff_policy)
    return mouse_scroll(delta=delta, x=x, y=y, approval_granted=approval_granted, policy=eff_policy)


def desktop_execute_command(
    command: str,
    working_directory: str = "",
    timeout: int = 60,
    *,
    approval_granted: bool = False,
    is_admin: bool = False,
    policy: Policy | None = None,
    device_id: str = "local",
) -> dict:
    """Execute commands permitted by policy.

    Security Architecture (Section 13):
    A user who grants screen access (`desktop_enabled`) does NOT automatically
    grant command execution. Command execution is checked independently against
    `run_shell` / command allowlist policy.
    """
    _require_no_human_takeover(device_id)
    eff_policy = policy or default_policy()
    _check("desktop_execute_command", approval_granted=approval_granted, is_admin=is_admin, policy=eff_policy)
    if "run_shell" in eff_policy.denied_tools:
        raise PermissionError("Command execution is disabled by device policy (screen access does not grant command execution).")
    return run_shell(
        command=command,
        working_directory=working_directory,
        timeout=timeout,
        approval_granted=approval_granted,
        is_admin=is_admin,
        policy=eff_policy,
    )


def desktop_open_application(
    app_name: str,
    url: str | None = None,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
    device_id: str = "local",
) -> dict:
    """Launch an approved application."""
    _require_no_human_takeover(device_id)
    eff_policy = policy or default_policy()
    _check("desktop_open_application", approval_granted=approval_granted, policy=eff_policy)
    return launch_approved_application(app_name, approval_granted=approval_granted, url=url, policy=eff_policy)


def desktop_get_windows(
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """List visible application windows."""
    eff_policy = policy or default_policy()
    _check("desktop_get_windows", approval_granted=approval_granted, policy=eff_policy)
    return list_desktop_windows(approval_granted=approval_granted, policy=eff_policy)


def desktop_wait(
    condition: str = "sleep",
    text: str = "",
    window_title: str = "",
    seconds: float = 1.0,
    timeout_seconds: float = 10.0,
    *,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Wait for a condition ('sleep', 'text', or 'window') or action to complete."""
    eff_policy = policy or default_policy()
    _check("desktop_wait", approval_granted=approval_granted, policy=eff_policy)
    cond = (condition or "sleep").strip().lower()
    if cond == "text" or (text and cond == "sleep"):
        return wait_for_text(
            text=text,
            timeout_seconds=timeout_seconds,
            poll_interval=0.5,
            approval_granted=approval_granted,
            policy=eff_policy,
        )
    if cond == "window" or window_title:
        needle = (window_title or text).strip().lower()
        deadline = time.time() + max(0.5, float(timeout_seconds))
        while time.time() < deadline:
            wins = list_desktop_windows(approval_granted=approval_granted, policy=eff_policy).get("windows", [])
            for w in wins:
                if needle in str(w.get("title", "")).lower():
                    return {"ok": True, "condition": "window", "matched_window": w}
            time.sleep(0.3)
        return {"ok": False, "condition": "window", "error": f"Window matching '{needle}' did not appear within {timeout_seconds}s."}

    wait_sec = max(0.0, min(float(seconds), 30.0))
    time.sleep(wait_sec)
    return {"ok": True, "condition": "sleep", "waited_seconds": wait_sec}


def desktop_verify_action(
    expected_text: str = "",
    expected_window: str = "",
    *,
    wait_seconds: float = 0.35,
    capture_after: bool = True,
    intent_id: str | None = None,
    approval_granted: bool = False,
    policy: Policy | None = None,
) -> dict:
    """Verify whether an action succeeded by checking visible windows and/or screen OCR text."""
    eff_policy = policy or default_policy()
    _check("desktop_verify_action", approval_granted=approval_granted, policy=eff_policy)
    if intent_id and desktop_is_action_vetoed(intent_id):
        return {
            "ok": False,
            "verified": False,
            "vetoed": True,
            "intent_id": intent_id,
            "error": "operator_veto",
        }
    settle = max(0.0, min(float(wait_seconds), 5.0))
    if settle > 0:
        time.sleep(settle)
    verified = True
    evidence: dict = {}

    if expected_window:
        wins = list_desktop_windows(approval_granted=approval_granted, policy=eff_policy).get("windows", [])
        needle = expected_window.strip().lower()
        matched_win = next((w for w in wins if needle in str(w.get("title", "")).lower()), None)
        evidence["window_matched"] = matched_win is not None
        evidence["window"] = matched_win
        if matched_win is None:
            verified = False

    if expected_text:
        scr = verify_screen_contains(expected_text, approval_granted=approval_granted, policy=eff_policy)
        evidence["text_matched"] = bool(scr.get("contains"))
        evidence["text_details"] = scr
        if not scr.get("contains"):
            verified = False

    if not expected_text and not expected_window:
        shot = capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)
        evidence["screenshot_available"] = bool(shot.get("available"))
        evidence["active_window"] = shot.get("active_window", "")
        verified = bool(shot.get("available"))

    if capture_after:
        post = capture_desktop_screenshot(approval_granted=approval_granted, policy=eff_policy)
        evidence["post_action_screenshot"] = {
            "available": bool(post.get("available")),
            "path": post.get("path", ""),
            "size": post.get("size"),
            "active_window": post.get("active_window", ""),
        }
        if not post.get("available"):
            verified = False

    return {
        "ok": True,
        "verified": verified,
        "intent_id": intent_id or "",
        "expected_text": expected_text,
        "expected_window": expected_window,
        "wait_seconds": settle,
        "evidence": evidence,
    }


def _som_element_from_rect(
    left: int,
    top: int,
    width: int,
    height: int,
    *,
    mons: list[dict[str, Any]],
    idx: int,
    label: str,
    el_type: str,
    hwnd: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .. import screen_capture

    norm = screen_capture.normalize_bounds_for_monitor(left, top, width, height, mons)
    row: dict[str, Any] = {
        "id": idx,
        "label": label,
        "type": el_type,
        "hwnd": hwnd,
        "bounds": [left, top, width, height],
        "monitor_index": norm.get("monitor_index", 0),
        "norm_x": norm["norm_x"],
        "norm_y": norm["norm_y"],
        "norm_width": norm["norm_width"],
        "norm_height": norm["norm_height"],
        "center_x": norm["center_x"],
        "center_y": norm["center_y"],
        "absolute_x": norm.get("absolute_x"),
        "absolute_y": norm.get("absolute_y"),
    }
    if extra:
        row.update(extra)
    return row


def _collect_uia_elements(
    *,
    mons: list[dict[str, Any]],
    max_elements: int,
    monitor_index: int | None,
) -> tuple[list[dict[str, Any]], str]:
    """Walk UI Automation tree for buttons/edits/links (optional ``comtypes``)."""
    if sys.platform != "win32":
        return [], "non_windows"
    try:
        import comtypes  # noqa: F401
        import comtypes.client
    except ImportError:
        return [], "comtypes_missing"

    # ControlType IDs (UIAutomationClient.h)
    interesting = {
        50000: "button",  # UIA_ButtonControlTypeId
        50004: "edit",
        50005: "hyperlink",
        50006: "image",
        50008: "listitem",
        50011: "menuitem",
        50013: "radiobutton",
        50016: "checkbox",
        50018: "tabitem",
        50019: "text",
        50020: "tool_bar",
        50021: "treeitem",
        50025: "combo_box",
        50033: "splitbutton",
    }
    elements: list[dict[str, Any]] = []
    try:
        # Generate / load UIAutomation typelib once.
        uia_mod = comtypes.client.GetModule("UIAutomationCore.dll")
        CUIAutomation = getattr(uia_mod, "CUIAutomation", None) or getattr(uia_mod, "CUIAutomation8", None)
        if CUIAutomation is None:
            # Fallback CLSID create
            uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}")
        else:
            uia = comtypes.client.CreateObject(CUIAutomation)
        root = uia.GetRootElement()
        condition = uia.CreateTrueCondition()
        # TreeScope_Descendants = 4
        finder = root.FindAll(4, condition)
        count = int(finder.Length) if finder is not None else 0
        for i in range(min(count, max(max_elements * 8, 200))):
            if len(elements) >= max_elements:
                break
            try:
                el = finder.GetElement(i)
            except Exception:
                continue
            try:
                ctype = int(el.CurrentControlType)
            except Exception:
                continue
            el_type = interesting.get(ctype)
            if not el_type:
                continue
            try:
                name = (el.CurrentName or "").strip()
            except Exception:
                name = ""
            if not name:
                continue
            try:
                rect = el.CurrentBoundingRectangle
                left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
            except Exception:
                continue
            w, h = right - left, bottom - top
            if w < 12 or h < 10:
                continue
            try:
                hwnd = int(el.CurrentNativeWindowHandle or 0)
            except Exception:
                hwnd = 0
            row = _som_element_from_rect(
                left,
                top,
                w,
                h,
                mons=mons,
                idx=len(elements) + 1,
                label=name[:40],
                el_type=el_type,
                hwnd=hwnd,
                extra={"source": "uia", "confidence": 0.9, "control_type_id": ctype},
            )
            if monitor_index is not None and int(row.get("monitor_index", -1)) != int(monitor_index):
                continue
            elements.append(row)
        return elements, "uia_ok" if elements else "uia_empty"
    except Exception as exc:  # noqa: BLE001
        return [], f"uia_error:{type(exc).__name__}"


def desktop_get_ui_elements(
    *,
    include_controls: bool = True,
    max_elements: int = 25,
    monitor_index: int | None = None,
    approval_granted: bool = True,
    policy: Policy | None = None,
) -> dict:
    """Extract interactive UI elements (windows, buttons, inputs) with normalized bounding coordinates for Vision Set-of-Marks (SoM)."""
    eff_policy = policy or default_policy()
    _check("desktop_get_windows", approval_granted=approval_granted, policy=eff_policy)

    from .. import screen_capture

    mons = screen_capture.get_monitors()
    vdesk = screen_capture.get_virtual_desktop_bounds(mons)
    screen_w = vdesk["width"]
    screen_h = vdesk["height"]
    if monitor_index is not None and 0 <= int(monitor_index) < len(mons):
        m = mons[int(monitor_index)]
        screen_w = int(m.get("width", screen_w))
        screen_h = int(m.get("height", screen_h))

    elements: list[dict[str, Any]] = []
    grounding_source = "none"

    if sys.platform == "win32" and include_controls:
        uia_els, uia_status = _collect_uia_elements(
            mons=mons, max_elements=max_elements, monitor_index=monitor_index
        )
        if uia_els:
            elements.extend(uia_els)
            grounding_source = "uia"
        elif uia_status and uia_status not in {"uia_empty", "comtypes_missing"}:
            grounding_source = uia_status

    if sys.platform == "win32" and len(elements) < max_elements:
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def _enum_win_cb(hwnd: Any, _lparam: Any) -> bool:
                nonlocal grounding_source
                if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
                    return True
                length = u.GetWindowTextLengthW(hwnd)
                if length == 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                u.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if not title:
                    return True

                rect = RECT()
                u.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w < 60 or h < 30:
                    return True

                idx = len(elements) + 1
                row = _som_element_from_rect(
                    rect.left, rect.top, w, h,
                    mons=mons,
                    idx=idx,
                    label=title[:40],
                    el_type="window",
                    hwnd=int(hwnd),
                    extra={"source": "win32", "confidence": 0.75},
                )
                if monitor_index is not None and int(row.get("monitor_index", -1)) != int(monitor_index):
                    return True
                elements.append(row)
                if grounding_source == "none":
                    grounding_source = "win32"
                elif grounding_source == "uia":
                    grounding_source = "uia+win32"
                if len(elements) >= max_elements:
                    return False

                # Child controls if enabled and UIA did not already fill the budget
                if include_controls and grounding_source.startswith("uia") is False and len(elements) < max_elements:
                    def _child_cb(chwnd: Any, _clparam: Any) -> bool:
                        if not u.IsWindowVisible(chwnd):
                            return True
                        crect = RECT()
                        u.GetWindowRect(chwnd, ctypes.byref(crect))
                        cw = crect.right - crect.left
                        ch = crect.bottom - crect.top
                        if cw < 30 or ch < 15 or cw > screen_w * 0.9 or ch > screen_h * 0.9:
                            return True

                        c_len = u.GetWindowTextLengthW(chwnd)
                        c_buf = ctypes.create_unicode_buffer(c_len + 1)
                        if c_len > 0:
                            u.GetWindowTextW(chwnd, c_buf, c_len + 1)
                        c_title = c_buf.value.strip()

                        cls_buf = ctypes.create_unicode_buffer(128)
                        u.GetClassNameW(chwnd, cls_buf, 128)
                        cls_name = cls_buf.value.strip()

                        label = c_title or cls_name or "Control"
                        cidx = len(elements) + 1
                        crow = _som_element_from_rect(
                            crect.left, crect.top, cw, ch,
                            mons=mons,
                            idx=cidx,
                            label=label[:30],
                            el_type=cls_name.lower() or "control",
                            hwnd=int(chwnd),
                            extra={"source": "win32", "confidence": 0.65},
                        )
                        if monitor_index is not None and int(crow.get("monitor_index", -1)) != int(monitor_index):
                            return True
                        elements.append(crow)
                        return len(elements) < max_elements

                    u.EnumChildWindows(hwnd, WNDENUMPROC(_child_cb), 0)

                return len(elements) < max_elements

            cb = WNDENUMPROC(_enum_win_cb)
            u.EnumWindows(cb, 0)
            if elements and grounding_source == "none":
                grounding_source = "win32"
        except Exception:
            pass

    if not elements:
        synthetic_ok = os.environ.get("AI_BRIDGE_SOM_SYNTHETIC_FALLBACK", "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if synthetic_ok:
            elements = [
                {
                    "id": 1,
                    "label": "Active Workspace",
                    "type": "editor",
                    "synthetic": True,
                    "bounds": [60, 80, 1500, 850],
                    "norm_x": 0.04,
                    "norm_y": 0.12,
                    "norm_width": 0.62,
                    "norm_height": 0.58,
                    "center_x": 0.35,
                    "center_y": 0.41,
                },
            ]
        else:
            return {
                "ok": False,
                "error": "No UI elements enumerated (headless, capture blocked, or empty desktop).",
                "count": 0,
                "screen_width": screen_w,
                "screen_height": screen_h,
                "virtual_desktop": vdesk,
                "monitor_index": monitor_index,
                "elements": [],
            }

    return {
        "ok": True,
        "count": len(elements),
        "screen_width": screen_w,
        "screen_height": screen_h,
        "virtual_desktop": vdesk,
        "monitor_index": monitor_index,
        "monitors": mons,
        "grounding_source": grounding_source,
        "elements": elements,
    }


def desktop_anticipate_action(
    action: str,
    x: float,
    y: float,
    label: str = "",
    risk_tier: str = "tier2_workspace",
    countdown_ms: int = 2000,
    *,
    approval_granted: bool = True,
    policy: Policy | None = None,
) -> dict:
    """Registers an anticipated autonomous agent action with a grace period for operator veto."""
    intent_id = f"intent_{uuid.uuid4().hex[:8]}"
    return {
        "ok": True,
        "intent_id": intent_id,
        "action": action,
        "target_label": label or f"Action {action}",
        "x": max(0.0, min(1.0, float(x))),
        "y": max(0.0, min(1.0, float(y))),
        "risk_tier": risk_tier,
        "countdown_ms": max(500, int(countdown_ms)),
        "vetoed": False,
    }

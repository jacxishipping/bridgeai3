"""Context-Aware Execution Engine (Pillar 6).

Provides pre-execution UI/system context capture, step guardrails
(e.g. requiring a specific window title, accessibility @ref, or resource ceiling
before executing a step), and post-execution verification + automatic episodic
learning (Pillar 7).
"""
from __future__ import annotations

import time
from typing import Any

from . import agent_memory as memorymod
from . import device_intelligence as intelmod


def capture_execution_context(
    conn: Any,
    *,
    device_id: str | None,
    tenant: str,
    live_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture a pre/post-step execution context snapshot for a device."""
    dev_id = (device_id or "local").strip() or "local"
    meta = dict(live_meta or {})
    dev_row = None
    if dev_id != "local":
        try:
            dev_row = conn.execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
        except Exception:
            dev_row = None
    if dev_row is None:
        dev_row = {"id": dev_id, "name": dev_id, "os": "Windows", "status": "online", "tenant": tenant}

    intel = intelmod.build_device_intelligence(conn, dev_row, live_meta=meta)
    return {
        "device_id": dev_id,
        "captured_at": time.time(),
        "online": intel["online"],
        "health_score": intel["health_score"],
        "readiness_state": intel["readiness_state"],
        "foreground_window": intel["context"].get("foreground_window") or "",
        "cpu_percent": intel["context"].get("cpu_percent"),
        "memory_percent": intel["context"].get("memory_percent"),
        "disk_percent": intel["context"].get("disk_percent"),
        "a11y_backend": intel["recommended_a11y_backend"],
        "transport": intel["recommended_transport"],
    }


def evaluate_context_guard(
    guard: dict[str, Any] | None,
    context: dict[str, Any],
    *,
    execute_tool_fn: Any = None,
    device_id: str | None = None,
    tenant: str = "default",
    actor: str = "workflow",
) -> dict[str, Any]:
    """Evaluate a step's pre-execution context guard against live device state.

    Supported guard keys:
    - ``require_window_title_contains``: substring that must appear in the active or open window titles
    - ``require_a11y_ref_present``: ``@ref`` or label that must be present in the live A11y tree
    - ``max_cpu_pct``: abort/defer if CPU exceeds threshold
    - ``max_memory_pct``: abort/defer if RAM exceeds threshold
    - ``require_online``: default True for remote devices
    """
    if not guard or not isinstance(guard, dict):
        return {"passed": True, "checked": False, "reasons": []}

    reasons: list[str] = []
    if guard.get("require_online", True) and not context.get("online", True):
        reasons.append(f"Device '{context.get('device_id')}' is offline.")

    max_cpu = guard.get("max_cpu_pct")
    if max_cpu is not None and context.get("cpu_percent") is not None:
        try:
            if float(context["cpu_percent"]) > float(max_cpu):
                reasons.append(f"CPU {context['cpu_percent']}% exceeds guard ceiling {max_cpu}%.")
        except (TypeError, ValueError):
            pass

    max_mem = guard.get("max_memory_pct")
    if max_mem is not None and context.get("memory_percent") is not None:
        try:
            if float(context["memory_percent"]) > float(max_mem):
                reasons.append(f"Memory {context['memory_percent']}% exceeds guard ceiling {max_mem}%.")
        except (TypeError, ValueError):
            pass

    req_win = str(guard.get("require_window_title_contains") or "").strip()
    if req_win:
        fg = str(context.get("foreground_window") or "")
        matched_win = req_win.lower() in fg.lower()
        if not matched_win and callable(execute_tool_fn):
            try:
                w_res = execute_tool_fn("list_desktop_windows", {}, device_id, tenant, actor)
                windows = w_res.get("windows") if isinstance(w_res, dict) else (w_res if isinstance(w_res, list) else [])
                for w in windows or []:
                    title = str(w.get("title") if isinstance(w, dict) else w)
                    if req_win.lower() in title.lower():
                        matched_win = True
                        break
            except Exception:
                pass
        if not matched_win:
            reasons.append(f"Required window title containing '{req_win}' is not active/open.")

    req_ref = str(guard.get("require_a11y_ref_present") or "").strip()
    if req_ref and callable(execute_tool_fn):
        try:
            snap = execute_tool_fn(
                "desktop_a11y_snapshot",
                {"interactive_only": True, "max_elements": 80},
                device_id,
                tenant,
                actor,
            )
            elements = snap.get("elements") if isinstance(snap, dict) else []
            found_ref = False
            for el in elements or []:
                if not isinstance(el, dict):
                    continue
                if req_ref.lower() in str(el.get("ref") or "").lower() or req_ref.lower() in str(el.get("name") or "").lower():
                    found_ref = True
                    break
            if not found_ref:
                reasons.append(f"Required A11y target '{req_ref}' was not found in accessibility snapshot.")
        except Exception as exc:
            reasons.append(f"Failed to verify A11y guard '{req_ref}': {exc}")

    return {
        "passed": len(reasons) == 0,
        "checked": True,
        "reasons": reasons,
        "guard": guard,
    }


def record_execution_learning(
    conn: Any,
    *,
    tenant: str,
    device_id: str | None,
    tool: str,
    pre_context: dict[str, Any],
    success: bool,
    attempts_used: int,
    error_message: str = "",
) -> dict[str, Any] | None:
    """Automatically record an episodic memory when a step recovers after retry or fails with a repeatable UI/context pattern."""
    dev_id = (device_id or "local").strip() or "local"
    try:
        if success and attempts_used > 1:
            return memorymod.record_device_episode(
                conn,
                tenant=tenant,
                device_id=dev_id,
                episode_type="recovery_playbook",
                tool=tool,
                context_summary=f"window='{pre_context.get('foreground_window', '')}', health={pre_context.get('health_score', 100)}",
                outcome="recovered_on_retry",
                resolution=f"Tool '{tool}' succeeded on attempt {attempts_used} after transient failure.",
            )
        if not success and error_message:
            return memorymod.record_device_episode(
                conn,
                tenant=tenant,
                device_id=dev_id,
                episode_type="ui_quirk",
                tool=tool,
                context_summary=f"window='{pre_context.get('foreground_window', '')}', health={pre_context.get('health_score', 100)}",
                outcome="failed",
                resolution=f"Observed failure in '{tool}': {error_message[:300]}",
            )
    except Exception:
        return None
    return None


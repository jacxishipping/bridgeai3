"""Risk-based policy engine — enforced server-side, never by prompt text.

Tiers (see docs/security.md):
  1 read-only | 2 low-risk change | 3 sensitive change | 4 high-risk
Rules:
- Tier 1: always allowed for an authenticated device/user in scope.
- Tier 2: allowed when the workspace policy allows writes.
- Tier 3: allowed only if AI_BRIDGE_ALLOW_TIER3=1 (admin opt-in) AND
  no approval is pending; per-tool allowlist in ToolPolicy.
- Tier 4: ALWAYS requires an explicit human approval record first.
  No tool, AI text, or device may create that approval for itself.
- The policy object is constructed from admin config only. Tool
  arguments can never widen it (see test_policy_bypass in tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class RiskTier(IntEnum):
    READ_ONLY = 1
    LOW_RISK = 2
    SENSITIVE = 3
    HIGH_RISK = 4


# R-P0-05 (Wave 1C): the five previously untiered MCP tools. `system_info`
# is the MCP wrapper name for `get_system_info` (both READ_ONLY).
# `interact_and_view_screen` is READ_ONLY as a composite *view* tool; the
# input actions it dispatches (mouse_click, keyboard_type, ...) are each
# gated individually by the MCP wrapper gate in server.py, so an input
# action can never ride through on this tool's READ_ONLY tier.
# `select_device` is SENSITIVE but routing-only: it mutates no device
# itself, and every subsequent tool call is gated (see the exemption in
# decide() below).
TOOL_TIERS: dict[str, RiskTier] = {
    "list_devices": RiskTier.READ_ONLY,
    "get_device_status": RiskTier.READ_ONLY,
    "get_system_info": RiskTier.READ_ONLY,
    "system_info": RiskTier.READ_ONLY,
    "view_live_screen": RiskTier.READ_ONLY,
    "interact_and_view_screen": RiskTier.READ_ONLY,
    "select_device": RiskTier.SENSITIVE,
    "browser_navigate_with_shot": RiskTier.SENSITIVE,
    "get_resource_usage": RiskTier.READ_ONLY,
    "get_network_info": RiskTier.READ_ONLY,
    "list_workspace": RiskTier.READ_ONLY,
    "read_workspace_file": RiskTier.READ_ONLY,
    "read_file": RiskTier.READ_ONLY,
    "search_workspace": RiskTier.READ_ONLY,
    "get_service_status": RiskTier.READ_ONLY,
    "get_recent_logs": RiskTier.READ_ONLY,
    "list_processes": RiskTier.READ_ONLY,
    "get_disk_usage": RiskTier.READ_ONLY,
    "get_memory_usage": RiskTier.READ_ONLY,
    "list_docker_containers": RiskTier.READ_ONLY,
    "git_status": RiskTier.READ_ONLY,
    "git_diff": RiskTier.READ_ONLY,
    "get_task_status": RiskTier.READ_ONLY,
    "get_task_result": RiskTier.READ_ONLY,
    "get_desktop_status": RiskTier.READ_ONLY,
    "get_native_encode_status": RiskTier.READ_ONLY,
    "create_workspace_file": RiskTier.LOW_RISK,
    "run_project_tests": RiskTier.LOW_RISK,
    "run_linter": RiskTier.LOW_RISK,
    "edit_workspace_file": RiskTier.SENSITIVE,
    "move_workspace_file": RiskTier.SENSITIVE,
    "create_backup": RiskTier.SENSITIVE,
    "deploy_application": RiskTier.SENSITIVE,
    "capture_desktop_screenshot": RiskTier.SENSITIVE,
    "take_screenshot": RiskTier.SENSITIVE,
    "capture_desktop_frame": RiskTier.SENSITIVE,
    "start_webrtc_stream": RiskTier.SENSITIVE,
    "stop_webrtc_stream": RiskTier.SENSITIVE,
    "update_fleet_agent": RiskTier.SENSITIVE,
    "ocr_screen": RiskTier.SENSITIVE,
    "click_text": RiskTier.SENSITIVE,
    "click_element": RiskTier.SENSITIVE,
    "list_ui_elements": RiskTier.READ_ONLY,
    "set_of_marks_screenshot": RiskTier.SENSITIVE,
    "wait_for_text": RiskTier.SENSITIVE,
    "verify_screen_contains": RiskTier.SENSITIVE,
    "press_hotkey": RiskTier.SENSITIVE,
    "execute_workflow": RiskTier.SENSITIVE,
    "broadcast_fleet_command": RiskTier.SENSITIVE,
    "launch_approved_application": RiskTier.SENSITIVE,
    "perform_approved_ui_action": RiskTier.SENSITIVE,
    "mouse_click": RiskTier.SENSITIVE,
    "mouse_move": RiskTier.SENSITIVE,
    "mouse_scroll": RiskTier.SENSITIVE,
    "mouse_drag": RiskTier.SENSITIVE,
    "keyboard_type": RiskTier.SENSITIVE,
    "keyboard_press": RiskTier.SENSITIVE,
    "list_desktop_windows": RiskTier.READ_ONLY,
    "get_active_window": RiskTier.READ_ONLY,
    "focus_window": RiskTier.SENSITIVE,
    "maximize_window": RiskTier.SENSITIVE,
    "minimize_window": RiskTier.SENSITIVE,
    "restore_window": RiskTier.SENSITIVE,
    "close_window": RiskTier.SENSITIVE,
    "move_resize_window": RiskTier.SENSITIVE,
    "invoke_element": RiskTier.SENSITIVE,
    "set_element_value": RiskTier.SENSITIVE,
    "focus_element": RiskTier.SENSITIVE,
    "get_screen_size": RiskTier.READ_ONLY,
    "get_clipboard": RiskTier.READ_ONLY,
    "set_clipboard": RiskTier.SENSITIVE,
    "install_package": RiskTier.SENSITIVE,
    "run_shell": RiskTier.SENSITIVE,
    "run_powershell": RiskTier.HIGH_RISK,
    "list_drives": RiskTier.READ_ONLY,
    "list_directory": RiskTier.READ_ONLY,
    "read_pc_file": RiskTier.SENSITIVE,
    "write_pc_file": RiskTier.SENSITIVE,
    "kill_process": RiskTier.HIGH_RISK,
    "write_file": RiskTier.SENSITIVE,
    "terminal_exec": RiskTier.SENSITIVE,
    "delete_workspace_file": RiskTier.HIGH_RISK,
    "submit_task": RiskTier.SENSITIVE,
    "request_approval": RiskTier.LOW_RISK,
    "approve_task": RiskTier.HIGH_RISK,
    "cancel_task": RiskTier.LOW_RISK,
    # Canonical v1.0 MCP Computer-Use Toolset (Section 8) & v2.0 Extensions
    "desktop_get_screenshot": RiskTier.SENSITIVE,
    "desktop_get_window_screenshot": RiskTier.SENSITIVE,
    "desktop_get_status": RiskTier.READ_ONLY,
    "desktop_get_monitors": RiskTier.READ_ONLY,
    "desktop_clipboard_get": RiskTier.READ_ONLY,
    "desktop_clipboard_set": RiskTier.SENSITIVE,
    "desktop_mouse_move": RiskTier.SENSITIVE,
    "desktop_click": RiskTier.SENSITIVE,
    "desktop_keyboard": RiskTier.SENSITIVE,
    "desktop_scroll": RiskTier.SENSITIVE,
    "desktop_execute_command": RiskTier.SENSITIVE,
    "desktop_open_application": RiskTier.SENSITIVE,
    "desktop_get_windows": RiskTier.READ_ONLY,
    "desktop_wait": RiskTier.SENSITIVE,
    "desktop_verify_action": RiskTier.SENSITIVE,
    # Microsoft 365 connector (Graph). Reads are Tier 1; sending mail is Tier 4.
    "m365_get_profile": RiskTier.READ_ONLY,
    "m365_list_mail": RiskTier.READ_ONLY,
    "m365_search_mail": RiskTier.READ_ONLY,
    "m365_get_mail": RiskTier.READ_ONLY,
    "m365_list_events": RiskTier.READ_ONLY,
    "m365_list_files": RiskTier.READ_ONLY,
    "m365_search_files": RiskTier.READ_ONLY,
    "m365_create_event": RiskTier.SENSITIVE,
    "m365_send_mail": RiskTier.HIGH_RISK,
    # Web browser automation tools
    "browser_navigate": RiskTier.SENSITIVE,
    "browser_screenshot": RiskTier.SENSITIVE,
    "browser_click": RiskTier.SENSITIVE,
    "browser_type": RiskTier.SENSITIVE,
    "browser_get_text": RiskTier.READ_ONLY,
    "browser_evaluate": RiskTier.SENSITIVE,
    "browser_get_url": RiskTier.READ_ONLY,
    "browser_go_back": RiskTier.SENSITIVE,
    "browser_go_forward": RiskTier.SENSITIVE,
    "browser_scroll": RiskTier.SENSITIVE,
    "browser_close": RiskTier.SENSITIVE,
    "browser_new_page": RiskTier.SENSITIVE,
    "browser_get_elements": RiskTier.READ_ONLY,
    "browser_wait_for_selector": RiskTier.SENSITIVE,
    "browser_select_option": RiskTier.SENSITIVE,
    "browser_download_file": RiskTier.SENSITIVE,
    "browser_set_viewport": RiskTier.SENSITIVE,
    "browser_expect": RiskTier.READ_ONLY,
    "browser_wait_for_url": RiskTier.SENSITIVE,
    "browser_diagnostics": RiskTier.READ_ONLY,
    "browser_compare_screenshot": RiskTier.SENSITIVE,
    "browserbase_create_session": RiskTier.SENSITIVE,
    "browserbase_connect": RiskTier.SENSITIVE,
    "browserbase_navigate": RiskTier.SENSITIVE,
    "browserbase_screenshot": RiskTier.SENSITIVE,
    "browserbase_end_session": RiskTier.READ_ONLY,
    "browser_session_info": RiskTier.READ_ONLY,
    "you_web_search": RiskTier.READ_ONLY,
    "you_fetch_contents": RiskTier.READ_ONLY,
    "firecrawl_scrape": RiskTier.READ_ONLY,
    "firecrawl_search": RiskTier.READ_ONLY,
    "firecrawl_map_site": RiskTier.READ_ONLY,
    "firecrawl_crawl": RiskTier.READ_ONLY,
    "workspace_project_structure": RiskTier.READ_ONLY,
    "dev_workspace_plan": RiskTier.READ_ONLY,
    "run_project_tests_structured": RiskTier.LOW_RISK,
    "git_create_branch": RiskTier.SENSITIVE,
    "git_checkpoint": RiskTier.SENSITIVE,
    "git_apply_patch": RiskTier.SENSITIVE,
    "git_log": RiskTier.READ_ONLY,
    "git_commit": RiskTier.SENSITIVE,
    "desktop_get_ui_elements": RiskTier.READ_ONLY,
    "desktop_veto_action": RiskTier.READ_ONLY,
    "desktop_anticipate_action": RiskTier.READ_ONLY,
    "browser_snapshot": RiskTier.READ_ONLY,
    "run_diagnostic_task": RiskTier.READ_ONLY,
    "agent_remember": RiskTier.LOW_RISK,
    "agent_notify": RiskTier.LOW_RISK,
    "agent_delegate": RiskTier.LOW_RISK,
    "agent_run_workflow": RiskTier.SENSITIVE,
    "supervisor_delegate_role": RiskTier.READ_ONLY,
    "supervisor_enqueue_handoff": RiskTier.SENSITIVE,
    "supervisor_recover_role_tasks": RiskTier.SENSITIVE,
    "agent_run_role": RiskTier.SENSITIVE,
}

# Dotted frontend/client aliases mapped to canonical underscore tool names.
TOOL_ALIASES: dict[str, str] = {
    "desktop.capture_screen": "desktop_get_screenshot",
    "desktop.click_coordinate": "desktop_click",
    "desktop.launch_app": "desktop_open_application",
    "desktop.type": "desktop_keyboard",
    "desktop.scroll": "desktop_scroll",
    "desktop.mouse_move": "desktop_mouse_move",
    "shell.exec": "run_shell",
    "pty.spawn": "terminal_exec",
    "file.read": "read_file",
    "file.write": "write_file",
    "system.specs": "get_system_info",
}


def normalize_tool_name(tool: str) -> str:
    """Resolve dotted client aliases to canonical underscore tool names."""
    name = (tool or "").strip()
    return TOOL_ALIASES.get(name, name)


@dataclass(frozen=True)
class Policy:
    """Admin-owned policy. Frozen so tool code cannot mutate it at runtime."""

    allow_writes: bool = True
    allow_tier3: bool = False
    allowed_services: frozenset[str] = frozenset()
    allowed_apps: frozenset[str] = frozenset()
    allowed_test_commands: frozenset[str] = frozenset({"pytest", "pytest -q", "npm test", "go test"})
    desktop_enabled: bool = False
    max_tier_unattended: int = 2  # unattended execution ceiling
    allowed_tools: frozenset[str] | None = None  # None means all valid tools permitted unless denied
    denied_tools: frozenset[str] = frozenset()  # explicitly blocked tools
    unrestricted: bool = False  # Bypasses all risk tier, write, and approval blocks (server/DevOps mode)
    # R-P0-02 (Wave 1C): provenance for `unrestricted`. The bypass in
    # decide() applies ONLY when this names the admin actor who granted it
    # via grant_unrestricted_policy() (which audits the grant). A bare
    # `Policy(unrestricted=True)` — e.g. from an enrollment/request body
    # or a default — is IGNORED by decide(): fail closed.
    unrestricted_granted_by: str | None = None

    def tier_of(self, tool: str) -> RiskTier | None:
        return TOOL_TIERS.get(normalize_tool_name(tool))


@dataclass
class Decision:
    allowed: bool
    needs_approval: bool = False
    reason: str = ""


_WRITE_TOOLS = frozenset(
    {
        "create_workspace_file",
        "edit_workspace_file",
        "move_workspace_file",
        "delete_workspace_file",
        "create_backup",
        "deploy_application",
        "write_file",
        "git_create_branch",
        "git_checkpoint",
        "git_commit",
        "git_apply_patch",
    }
)


def decide(policy: Policy, tool: str, *, approval_granted: bool = False, device_revoked: bool = False, is_admin: bool = False) -> Decision:
    if device_revoked:
        return Decision(False, reason="Device is revoked.")
    canonical = normalize_tool_name(tool)
    tier = TOOL_TIERS.get(canonical)
    if tier is None:
        return Decision(False, reason=f"Unknown or unlisted tool: {tool}")
    # Device-specific and policy-specific tool blocking
    if tool in policy.denied_tools or canonical in policy.denied_tools:
        return Decision(False, reason=f"Tool '{tool}' is disabled by device policy.")
    if policy.allowed_tools is not None and tool not in policy.allowed_tools and canonical not in policy.allowed_tools:
        return Decision(False, reason=f"Tool '{tool}' is not in the allowed tools list for this policy.")

    # R-P0-02 (Wave 1C): Tier 4 ALWAYS requires a recorded approval AND an
    # admin caller. The old `max_tier_unattended >= 4` escape silently
    # authorised Tier 4 and is deleted: the ceiling governs which tiers
    # may run unattended, never Tier 4. This check runs BEFORE the
    # `unrestricted` bypass below, so even a granted-unrestricted policy
    # cannot run Tier 4 without approval.
    if tier == RiskTier.HIGH_RISK and not (approval_granted and is_admin):
        return Decision(False, needs_approval=True, reason="High-risk operation requires explicit human approval.")

    # Unrestricted server mode bypasses tier & approval guardrails — but
    # ONLY when granted by an explicit, audited admin action (see
    # grant_unrestricted_policy()). A bare unrestricted=True (enrollment
    # body, default, agent-supplied) is ignored: fall through to the
    # normal tier checks below.
    if policy.unrestricted and policy.unrestricted_granted_by:
        return Decision(True, reason=f"Allowed (unrestricted server policy, granted by {policy.unrestricted_granted_by}).")
    if canonical in _WRITE_TOOLS and not policy.allow_writes:
        return Decision(False, reason="Workspace writes are disabled by policy.")
    if tier == RiskTier.SENSITIVE:
        desktop_tools = {
            "capture_desktop_screenshot",
            "capture_desktop_frame",
            "take_screenshot",
            "start_webrtc_stream",
            "launch_approved_application",
            "perform_approved_ui_action",
            "mouse_click",
            "mouse_move",
            "mouse_scroll",
            "mouse_drag",
            "keyboard_type",
            "keyboard_press",
            "press_hotkey",
            "focus_window",
            "ocr_screen",
            "click_text",
            "click_element",
            "list_ui_elements",
            "set_of_marks_screenshot",
            "wait_for_text",
            "verify_screen_contains",
            "execute_workflow",
            "desktop_get_screenshot",
            "desktop_get_window_screenshot",
            "desktop_mouse_move",
            "desktop_click",
            "desktop_keyboard",
            "desktop_scroll",
            "desktop_clipboard_set",
            "desktop_open_application",
            "desktop_wait",
            "desktop_verify_action",
        }
        if canonical in desktop_tools and not policy.desktop_enabled:
            return Decision(False, reason="Desktop automation is not enabled.")
        # A human approval record for this task is itself the Tier-3
        # opt-in: without it, an approved task would be re-denied by the
        # agent after the control plane released it (see next_task).
        # `select_device` is exempt from the Tier-3 opt-in: it is
        # routing-only (it changes no device state) and every tool call
        # made after selecting a device is gated individually.
        if not (policy.allow_tier3 or policy.max_tier_unattended >= 3 or is_admin) and not approval_granted and canonical not in {"submit_task", "create_backup", "select_device"}:
            return Decision(False, needs_approval=True, reason="Sensitive operation requires Tier-3 opt-in or approval.")
        effective_ceiling = (
            max(policy.max_tier_unattended, 3)
            if (policy.allow_tier3 or policy.max_tier_unattended >= 3 or is_admin or canonical in {"submit_task", "create_backup", "select_device"})
            else policy.max_tier_unattended
        )
        if not is_admin and tier > effective_ceiling and not approval_granted:
            return Decision(False, needs_approval=True, reason="Operation exceeds the unattended tier ceiling.")
    return Decision(True, reason=f"Allowed (tier {tier}).")


def grant_unrestricted_policy(policy: Policy, *, granted_by: str, db_path: str | None = None) -> Policy:
    """Grant `unrestricted` mode by an explicit, audited admin action.

    R-P0-02 (Wave 1C): this is the ONLY supported way to obtain an
    unrestricted policy. `granted_by` must name the admin actor (never
    empty); the grant is written to the audit trail via the single audit
    writer before the policy is returned. A `Policy` built with a bare
    `unrestricted=True` (enrollment body, default, agent-supplied) has
    no provenance and decide() ignores it.
    """
    actor = (granted_by or "").strip()
    if not actor:
        raise ValueError("grant_unrestricted_policy requires a non-empty granted_by admin actor.")
    granted = Policy(
        allow_writes=policy.allow_writes,
        allow_tier3=policy.allow_tier3,
        allowed_services=policy.allowed_services,
        allowed_apps=policy.allowed_apps,
        allowed_test_commands=policy.allowed_test_commands,
        desktop_enabled=policy.desktop_enabled,
        max_tier_unattended=policy.max_tier_unattended,
        allowed_tools=policy.allowed_tools,
        denied_tools=policy.denied_tools,
        unrestricted=True,
        unrestricted_granted_by=actor,
    )
    # Lazy import: audit_writer must never create an import cycle with
    # policy (tools -> policy is imported very early).
    from .audit_writer import write_audit_event

    write_audit_event(
        actor=actor,
        action="policy.unrestricted_granted",
        details={"previous_unrestricted": bool(policy.unrestricted)},
        db_path=db_path,
    )
    return granted


def revoke_unrestricted_policy(policy: Policy, *, revoked_by: str, db_path: str | None = None) -> Policy:
    """Revoke `unrestricted` mode (audited). Returns a restrictive copy."""
    actor = (revoked_by or "").strip()
    if not actor:
        raise ValueError("revoke_unrestricted_policy requires a non-empty revoked_by admin actor.")
    revoked = Policy(
        allow_writes=policy.allow_writes,
        allow_tier3=policy.allow_tier3,
        allowed_services=policy.allowed_services,
        allowed_apps=policy.allowed_apps,
        allowed_test_commands=policy.allowed_test_commands,
        desktop_enabled=policy.desktop_enabled,
        max_tier_unattended=policy.max_tier_unattended,
        allowed_tools=policy.allowed_tools,
        denied_tools=policy.denied_tools,
        unrestricted=False,
        unrestricted_granted_by=None,
    )
    from .audit_writer import write_audit_event

    write_audit_event(
        actor=actor,
        action="policy.unrestricted_revoked",
        details={"previous_unrestricted": bool(policy.unrestricted)},
        db_path=db_path,
    )
    return revoked

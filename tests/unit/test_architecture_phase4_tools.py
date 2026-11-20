"""Architecture tests: tools package split, tool registry, one limiter.

Wave 3B Track B (F-04-06, INV-12 / TD-D01, TD-D05).

* The tools package must expose EXACTLY the public names the old
  single-file ``ai_bridge.tools`` module exposed (captured before the
  split from the committed tree, commit 19747b2).
* The tool registry is the single mapping of tool name -> tier/handler
  that server.py and mcp_routes.py read (Track C wires them to it).
* :mod:`ai_bridge.control.limiter` is the single rate-limit entry
  point; :mod:`ai_bridge.control.ratelimit` is a thin shim over it.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from ai_bridge import tools
from ai_bridge.control import durable_ratelimit, limiter, ratelimit
from ai_bridge.policy import TOOL_TIERS
from ai_bridge.tool_registry import (
    TOOL_REGISTRY,
    ToolEntry,
    get_tier,
    get_tool,
    list_tools,
    register_tool,
    tools_by_tier,
)

# Public names of ai_bridge.tools captured BEFORE the F-04-06 split
# (import of the committed single-file module on 19747b2). The package
# must expose exactly this set — no more, no less.
EXPECTED_TOOLS_PUBLIC_NAMES = [
    "Any",
    "LAST_FRAME_SCALE",
    "Path",
    "Policy",
    "RUNNER",
    "annotations",
    "base64",
    "broadcast_fleet_command",
    "capture_desktop_frame",
    "capture_desktop_screenshot",
    "clear_som_marks_cache",
    "click_element",
    "click_text",
    "confined_path",
    "create_backup",
    "decide",
    "default_policy",
    "deploy_application",
    "desktop_anticipate_action",
    "desktop_click",
    "desktop_clipboard_get",
    "desktop_clipboard_set",
    "desktop_execute_command",
    "desktop_get_monitors",
    "desktop_get_screenshot",
    "desktop_get_session_recording",
    "desktop_get_status",
    "desktop_get_ui_elements",
    "desktop_get_window_screenshot",
    "desktop_get_windows",
    "desktop_handle_file_chunk",
    "desktop_handle_file_end",
    "desktop_handle_file_start",
    "desktop_is_action_vetoed",
    "desktop_keyboard",
    "desktop_list_transfers",
    "desktop_mouse_move",
    "desktop_open_application",
    "desktop_prepare_file_download",
    "desktop_record_session_event",
    "desktop_scroll",
    "desktop_seal_session_recording",
    "desktop_start_session_recording",
    "desktop_verify_action",
    "desktop_veto_action",
    "desktop_wait",
    "diagnostics",
    "execute_workflow",
    "focus_window",
    "get_clipboard",
    "get_desktop_status",
    "get_last_som_marks",
    "get_network_info",
    "get_resource_usage",
    "get_screen_size",
    "get_service_status",
    "get_settings",
    "get_system_info",
    "get_system_monitors",
    "get_transfer_quarantine_dir",
    "get_transfer_storage_dir",
    "hashlib",
    "install_package",
    "io",
    "is_secret_name",
    "json",
    "keyboard_press",
    "keyboard_type",
    "launch_approved_application",
    "list_desktop_windows",
    "list_ui_elements",
    "max_transfer_bytes",
    "mouse_click",
    "mouse_drag",
    "mouse_move",
    "mouse_scroll",
    "ocr_screen",
    "os",
    "perform_approved_ui_action",
    "platform",
    "press_hotkey",
    "re",
    "read_file",
    "redact",
    "rollback_fleet_agent",
    "run_linter",
    "run_project_tests",
    "run_shell",
    "set_clipboard",
    "set_of_marks_screenshot",
    "shlex",
    "shutil",
    "subprocess",
    "sys",
    "terminal_exec",
    "threading",
    "time",
    "update_fleet_agent",
    "uuid",
    "verify_screen_contains",
    "wait_for_text",
    "workspace_root",
    "write_file",
    "zipfile",
]


# --------------------------------------------------------------- tools package
def test_tools_package_exposes_exact_pre_split_public_names():
    actual = sorted(n for n in dir(tools) if not n.startswith("_"))
    assert actual == EXPECTED_TOOLS_PUBLIC_NAMES


def test_tools_package_is_a_package_not_a_module_file():
    from pathlib import Path

    assert Path(tools.__file__).as_posix().endswith("tools/__init__.py")
    assert tools.__all__ == EXPECTED_TOOLS_PUBLIC_NAMES


def test_tools_star_import_matches_public_names():
    ns: dict = {}
    exec("from ai_bridge.tools import *", ns)
    assert sorted(k for k in ns if not k.startswith("_")) == EXPECTED_TOOLS_PUBLIC_NAMES


def test_tools_functions_live_in_their_submodules():
    assert tools.run_shell.__module__ == "ai_bridge.tools.shell"
    assert tools.terminal_exec.__module__ == "ai_bridge.tools.shell"
    assert tools.read_file.__module__ == "ai_bridge.tools.files"
    assert tools.write_file.__module__ == "ai_bridge.tools.files"
    assert tools.capture_desktop_screenshot.__module__ == "ai_bridge.tools.desktop"
    assert tools.mouse_click.__module__ == "ai_bridge.tools.desktop"
    assert tools.desktop_get_screenshot.__module__ == "ai_bridge.tools.desktop_api"
    assert tools.desktop_get_ui_elements.__module__ == "ai_bridge.tools.desktop_api"
    assert tools.broadcast_fleet_command.__module__ == "ai_bridge.tools.fleet"
    assert tools.capture_desktop_frame.__module__ == "ai_bridge.tools.fleet"
    assert tools.get_system_info.__module__ == "ai_bridge.tools.system"
    assert tools.create_backup.__module__ == "ai_bridge.tools.project"
    assert tools.desktop_handle_file_start.__module__ == "ai_bridge.tools.transfers"
    assert tools.desktop_start_session_recording.__module__ == "ai_bridge.tools.recording"
    assert tools._check.__module__ == "ai_bridge.tools._common"
    assert tools.default_policy.__module__ == "ai_bridge.tools._common"


def test_tools_private_compat_names_still_importable():
    # Private helpers other modules and tests import from ai_bridge.tools.
    from ai_bridge.tools import (
        _LAST_SOM_LOCK,
        _LAST_SOM_MARKS,
        _check,
        _collect_uia_elements,
        _encode_image_base64,
        _grab_windows_desktop_fallback,
        _resolve_app_executable,
        _vk_for_token,
    )

    assert callable(_check)
    assert callable(_vk_for_token)
    assert callable(_grab_windows_desktop_fallback)
    assert callable(_collect_uia_elements)
    assert callable(_encode_image_base64)
    assert callable(_resolve_app_executable)
    assert isinstance(_LAST_SOM_MARKS, dict)
    assert _LAST_SOM_LOCK is not None
    # Same shared objects as the owning submodule.
    from ai_bridge.tools import _common

    assert _LAST_SOM_MARKS is _common._LAST_SOM_MARKS
    assert _LAST_SOM_LOCK is _common._LAST_SOM_LOCK


def test_tools_last_frame_scale_is_live_shared_state():
    from ai_bridge.tools import _common

    assert tools.LAST_FRAME_SCALE == 1.0
    _common.LAST_FRAME_SCALE = 0.5
    try:
        assert tools.LAST_FRAME_SCALE == 0.5
    finally:
        _common.LAST_FRAME_SCALE = 1.0


def test_tools_submodules_importable_as_attributes():
    import ai_bridge.tools.desktop as desktop_mod
    import ai_bridge.tools.shell as shell_mod

    assert tools.desktop is desktop_mod
    assert tools.shell is shell_mod
    assert desktop_mod.capture_desktop_screenshot is tools.capture_desktop_screenshot
    assert shell_mod.run_shell is tools.run_shell


def test_tools_monkeypatch_on_package_propagates_to_submodules(monkeypatch):
    """A patch on `tools.X` must reach internal callers in submodules.

    In the old single-file module every internal call resolved in the
    one module namespace; the split must not silently break that
    (tests patch tools._check, tools._win32_ui_action, ...).
    """
    seen = []

    def fake_check(tool, **kwargs):
        seen.append(tool)
        raise PermissionError("patched-check")

    monkeypatch.setattr(tools, "_check", fake_check)
    # get_system_info lives in tools/system.py, _check in tools/_common.py.
    with pytest.raises(PermissionError, match="patched-check"):
        tools.get_system_info()
    assert seen == ["get_system_info"]
    # And a function in yet another submodule sees the same patch.
    with pytest.raises(PermissionError, match="patched-check"):
        tools.run_shell("echo hi")
    assert seen == ["get_system_info", "run_shell"]


def test_tools_monkeypatch_of_win32_action_reaches_mouse_click(monkeypatch):
    seen = []

    def fake_win32(act, *, x=None, y=None, text=None, key=None, title=None, delta=-120, start_x=None, start_y=None):
        seen.append({"action": act, "x": x, "y": y})
        return {"action": act, "status": "ok"}

    monkeypatch.setattr(tools, "_win32_ui_action", fake_win32)
    monkeypatch.setattr(tools.platform, "system", lambda: "Windows")
    from ai_bridge.policy import Policy

    p = Policy(allow_tier3=True, max_tier_unattended=3, desktop_enabled=True)
    res = tools.mouse_click(x=100, y=200, button="left", clicks=1, approval_granted=True, policy=p)
    assert res["status"] == "ok"
    assert seen[-1] == {"action": "click", "x": 100, "y": 200}


# --------------------------------------------------------------- tool registry
def test_registry_populated_from_policy_tool_tiers():
    assert len(TOOL_REGISTRY) == len(TOOL_TIERS)
    assert set(TOOL_REGISTRY) == set(TOOL_TIERS)
    for name, tier in TOOL_TIERS.items():
        entry = TOOL_REGISTRY[name]
        assert entry.name == name
        assert entry.tier == tier.name
        assert entry.handler is None
        assert entry.description == ""


def test_registry_requires_approval_for_sensitive_and_high_risk():
    for entry in TOOL_REGISTRY.values():
        if entry.tier in {"SENSITIVE", "HIGH_RISK"}:
            assert entry.requires_approval is True, entry.name
        else:
            assert entry.requires_approval is False, entry.name


def test_registry_get_tool_normalizes_aliases():
    assert get_tool("shell.exec").name == "run_shell"
    assert get_tool("shell.exec").tier == "SENSITIVE"
    assert get_tool("desktop.capture_screen").name == "desktop_get_screenshot"
    assert get_tool("file.read").name == "read_file"
    assert get_tool("run_shell") is get_tool("shell.exec")
    assert get_tool("no_such_tool") is None


def test_registry_get_tier_and_tools_by_tier():
    assert get_tier("run_shell") == "SENSITIVE"
    assert get_tier("shell.exec") == "SENSITIVE"
    assert get_tier("get_system_info") == "READ_ONLY"
    assert get_tier("delete_workspace_file") == "HIGH_RISK"
    assert get_tier("no_such_tool") is None
    read_only = tools_by_tier("READ_ONLY")
    assert set(read_only) == {name for name, tier in TOOL_TIERS.items() if tier.name == "READ_ONLY"}
    assert tools_by_tier("HIGH_RISK") == [name for name, tier in TOOL_TIERS.items() if tier.name == "HIGH_RISK"]
    assert tools_by_tier("NOPE") == []


def test_registry_list_tools_returns_all_entries():
    entries = list_tools()
    assert len(entries) == len(TOOL_REGISTRY)
    assert all(isinstance(e, ToolEntry) for e in entries)
    assert {e.name for e in entries} == set(TOOL_TIERS)


def test_registry_entry_is_frozen():
    entry = get_tool("run_shell")
    with pytest.raises(AttributeError):
        entry.tier = "READ_ONLY"  # type: ignore[misc]


def test_registry_register_tool_roundtrip():
    name = f"zzz_test_tool_{uuid.uuid4().hex[:8]}"
    try:
        handler = lambda: "ok"  # noqa: E731
        entry = register_tool(name, "LOW_RISK", handler, "test tool", True)
        assert entry.name == name
        assert entry.tier == "LOW_RISK"
        assert entry.handler is handler
        assert entry.description == "test tool"
        assert entry.requires_approval is True
        assert get_tool(name) is entry
        assert name in tools_by_tier("LOW_RISK")
    finally:
        TOOL_REGISTRY.pop(name, None)


def test_registry_register_tool_normalizes_alias_to_canonical():
    original = TOOL_REGISTRY["read_file"]
    try:
        entry = register_tool("file.read", "READ_ONLY", description="alias write")
        assert entry.name == "read_file"
        assert get_tool("file.read") is entry
        assert get_tool("read_file") is entry
    finally:
        TOOL_REGISTRY["read_file"] = original


# --------------------------------------------------------------- one limiter
@pytest.fixture
def _no_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_REDIS_URL", raising=False)
    limiter.reset_for_tests()
    durable_ratelimit.reset_for_tests()
    yield
    limiter.reset_for_tests()
    durable_ratelimit.reset_for_tests()


def test_limiter_default_limit_is_120_per_minute():
    assert limiter.DEFAULT_LIMIT == 120
    assert limiter.DEFAULT_WINDOW == 60


def test_limiter_allow_enforces_limit_in_memory(_no_redis):
    key = f"t-{uuid.uuid4().hex}"
    assert limiter.allow(key, 2, 60) is True
    assert limiter.allow(key, 2, 60) is True
    assert limiter.allow(key, 2, 60) is False
    assert limiter.allow(f"t-{uuid.uuid4().hex}", 2, 60) is True


def test_limiter_allow_async_enforces_limit_in_memory(_no_redis):
    async def scenario():
        key = f"t-{uuid.uuid4().hex}"
        assert await limiter.allow_async(key, 2, 60) is True
        assert await limiter.allow_async(key, 2, 60) is True
        assert await limiter.allow_async(key, 2, 60) is False

    asyncio.run(scenario())


def test_limiter_delegates_to_durable_backend_shared_state(_no_redis):
    # limiter.allow and durable_ratelimit.allow share one bucket: proof
    # the consolidation did not fork the limiter into two.
    key = f"t-{uuid.uuid4().hex}"
    assert limiter.allow(key, 1, 60) is True
    assert durable_ratelimit.allow(key, 1, 60) is False
    assert limiter.backend_name() == "memory"
    assert durable_ratelimit.backend_name() == "memory"


def test_limiter_backend_name_selection(_no_redis):
    assert limiter.backend_name() == "memory"
    assert limiter.backend_name(conn=object()) == "database"
    assert limiter.redis_required() is False


def test_limiter_rate_limit_callable_raises_429(_no_redis):
    key = f"t-{uuid.uuid4().hex}"
    check = limiter.rate_limit(key, limit=2, window=60)
    check()
    check()
    with pytest.raises(HTTPException) as exc:
        check()
    assert exc.value.status_code == 429


def test_limiter_rate_limit_async_raises_429(_no_redis):
    async def scenario():
        key = f"t-{uuid.uuid4().hex}"
        await limiter.rate_limit_async(key, limit=2, window=60)
        await limiter.rate_limit_async(key, limit=2, window=60)
        with pytest.raises(HTTPException) as exc:
            await limiter.rate_limit_async(key, limit=2, window=60)
        assert exc.value.status_code == 429

    asyncio.run(scenario())


def test_limiter_factories(_no_redis):
    key = f"t-{uuid.uuid4().hex}"
    sync_check = limiter.make_rate_limiter(limit=1, window=60)
    sync_check(key)
    with pytest.raises(HTTPException):
        sync_check(key)

    async def scenario():
        akey = f"t-{uuid.uuid4().hex}"
        async_check = limiter.make_async_rate_limiter(limit=1, window=60)
        await async_check(akey)
        with pytest.raises(HTTPException):
            await async_check(akey)

    asyncio.run(scenario())


def test_limiter_sync_rate_limit_in_loop_warns_once(_no_redis, caplog):
    """DIST-07 preserved: sync callable inside a loop warns once."""
    import logging as _logging

    async def scenario():
        key = f"t-{uuid.uuid4().hex}"
        check = limiter.rate_limit(key, limit=100, window=60)
        check()
        check()
        return True

    with caplog.at_level(_logging.WARNING, logger="ai_bridge.ratelimit"):
        assert asyncio.run(scenario()) is True
    warnings = [r for r in caplog.records if "sync rate_limit() called inside" in r.message]
    assert len(warnings) == 1


def test_limiter_reset_for_tests_clears_buckets(_no_redis):
    key = f"t-{uuid.uuid4().hex}"
    assert limiter.allow(key, 1, 60) is True
    assert limiter.allow(key, 1, 60) is False
    limiter.reset_for_tests()
    assert limiter.allow(key, 1, 60) is True


def test_ratelimit_shim_reexports_limiter(_no_redis):
    assert ratelimit.rate_limit is limiter.rate_limit
    assert ratelimit.rate_limit_async is limiter.rate_limit_async
    assert ratelimit.allow is limiter.allow
    assert ratelimit.allow_async is limiter.allow_async
    assert ratelimit.backend_name is limiter.backend_name
    assert ratelimit.redis_required is limiter.redis_required
    assert ratelimit.reset_for_tests is limiter.reset_for_tests
    assert ratelimit.make_rate_limiter is limiter.make_rate_limiter
    assert ratelimit.make_async_rate_limiter is limiter.make_async_rate_limiter
    # The shim still enforces the same limit through the same bucket.
    key = f"t-{uuid.uuid4().hex}"
    check = ratelimit.rate_limit(key, limit=1, window=60)
    check()
    with pytest.raises(HTTPException):
        check()
    assert limiter.allow(key, 1, 60) is False

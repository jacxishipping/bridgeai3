"""Public interface of the tools package (F-04-06 split of tools.py).

Every name the old single-file ``ai_bridge.tools`` module exposed is
re-exported here, so ``from ai_bridge.tools import X`` and
``ai_bridge.tools.X`` keep working unchanged. The implementation lives
in the submodules (``_common``, ``system``, ``project``, ``desktop``,
``shell``, ``files``, ``fleet``, ``desktop_api``, ``transfers`` and
``recording``); this module only re-exports.

``LAST_FRAME_SCALE`` is intentionally NOT imported statically: it is
mutable shared state owned by :mod:`ai_bridge.tools._common` and is
served live through :func:`__getattr__` below, so ``tools.LAST_FRAME_SCALE``
reports the value set by the most recent frame capture (server.py reads
it that way) instead of a stale import-time snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import sys as _sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from types import ModuleType as _ModuleType
from typing import Any

from .. import diagnostics
from ..config import get_settings
from ..execution import RUNNER
from ..policy import Policy, decide
from ..security import confined_path, is_secret_name, redact, workspace_root
from ._common import (
    _LAST_SOM_LOCK,
    _LAST_SOM_MARKS,
    _VETOED_INTENTS,
    _check,
    _ensure_dpi_aware,
    _require_no_human_takeover,
    clear_som_marks_cache,
    default_policy,
    desktop_is_action_vetoed,
    desktop_veto_action,
    get_last_som_marks,
)
from .desktop import (
    _VK_MAP,
    _desktop_platform_capabilities,
    _encode_image_base64,
    _grab_windows_desktop_fallback,
    _resolve_app_executable,
    _vk_for_token,
    _win32_ui_action,
    capture_desktop_screenshot,
    click_element,
    click_text,
    focus_window,
    get_clipboard,
    get_desktop_status,
    get_screen_size,
    keyboard_press,
    keyboard_type,
    launch_approved_application,
    list_desktop_windows,
    list_ui_elements,
    mouse_click,
    mouse_drag,
    mouse_move,
    mouse_scroll,
    ocr_screen,
    perform_approved_ui_action,
    press_hotkey,
    set_clipboard,
    set_of_marks_screenshot,
    verify_screen_contains,
    wait_for_text,
)
from .desktop_api import (
    _collect_uia_elements,
    _som_element_from_rect,
    desktop_anticipate_action,
    desktop_click,
    desktop_clipboard_get,
    desktop_clipboard_set,
    desktop_execute_command,
    desktop_get_monitors,
    desktop_get_screenshot,
    desktop_get_status,
    desktop_get_ui_elements,
    desktop_get_window_screenshot,
    desktop_get_windows,
    desktop_keyboard,
    desktop_mouse_move,
    desktop_open_application,
    desktop_scroll,
    desktop_verify_action,
    desktop_wait,
)
from .files import (
    _confine_file_path,
    _host_file_access_allowed,
    read_file,
    write_file,
)
from .fleet import broadcast_fleet_command, capture_desktop_frame, rollback_fleet_agent, update_fleet_agent
from .project import (
    _is_safe_workspace_file,
    create_backup,
    deploy_application,
    run_linter,
    run_project_tests,
)
from .recording import (
    _RECORDINGS_LOCK,
    _SESSION_RECORDINGS,
    desktop_get_session_recording,
    desktop_record_session_event,
    desktop_seal_session_recording,
    desktop_start_session_recording,
)
from .shell import (
    _CHILD_ENV_ALLOWLIST,
    _CHILD_ENV_WINDOWS_ALLOWLIST,
    _LINUX_PKG_RE,
    _PACKAGE_ID_RE,
    _SCRUB_ENV_KEYS,
    _VERSION_RE,
    _clean_child_env,
    _run_argv,
    _run_cwd,
    _split_argv,
    execute_workflow,
    install_package,
    run_shell,
    terminal_exec,
)
from .system import (
    get_network_info,
    get_resource_usage,
    get_service_status,
    get_system_info,
    get_system_monitors,
)
from .transfers import (
    _ACTIVE_TRANSFERS,
    _TRANSFERS_LOCK,
    desktop_handle_file_chunk,
    desktop_handle_file_end,
    desktop_handle_file_start,
    desktop_list_transfers,
    desktop_prepare_file_download,
    get_transfer_quarantine_dir,
    get_transfer_storage_dir,
    max_transfer_bytes,
)

# Private helpers intentionally re-exported for monkeypatch compatibility
# (F-04-06): tests and callers patch tools.<name> and every submodule that
# binds it sees the patch. They are resolved dynamically by
# _build_patch_bindings, so ruff cannot see the use; this tuple marks the
# re-export explicitly. They are NOT in __all__ (which the architecture
# tests assert is exactly the public API of the pre-split module).
_PRIVATE_REEXPORTS = (
    _ACTIVE_TRANSFERS,
    _CHILD_ENV_ALLOWLIST,
    _CHILD_ENV_WINDOWS_ALLOWLIST,
    _LAST_SOM_LOCK,
    _LAST_SOM_MARKS,
    _LINUX_PKG_RE,
    _PACKAGE_ID_RE,
    _RECORDINGS_LOCK,
    _SCRUB_ENV_KEYS,
    _SESSION_RECORDINGS,
    _TRANSFERS_LOCK,
    _VERSION_RE,
    _VETOED_INTENTS,
    _VK_MAP,
    _check,
    _clean_child_env,
    _collect_uia_elements,
    _confine_file_path,
    _desktop_platform_capabilities,
    _encode_image_base64,
    _ensure_dpi_aware,
    _grab_windows_desktop_fallback,
    _host_file_access_allowed,
    _is_safe_workspace_file,
    _require_no_human_takeover,
    _resolve_app_executable,
    _run_argv,
    _run_cwd,
    _som_element_from_rect,
    _split_argv,
    _vk_for_token,
    _win32_ui_action,
)

__all__ = [
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
    "rollback_fleet_agent",
    "update_fleet_agent",
    "uuid",
    "verify_screen_contains",
    "wait_for_text",
    "workspace_root",
    "write_file",
    "zipfile",
]


_SUBMODULES = frozenset(
    {
        "_common",
        "system",
        "project",
        "desktop",
        "shell",
        "files",
        "fleet",
        "desktop_api",
        "transfers",
        "recording",
    }
)


def __getattr__(name: str):
    """Serve LAST_FRAME_SCALE live from _common, and submodules lazily (PEP 562)."""
    if name == "LAST_FRAME_SCALE":
        from . import _common

        return _common.LAST_FRAME_SCALE
    if name in _SUBMODULES:
        import importlib

        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | {"LAST_FRAME_SCALE"})


# The `from .<submodule> import ...` lines above bind the submodule names
# on this package; drop those bindings so dir(ai_bridge.tools) is exactly
# the public API of the old single-file module. The submodules stay
# importable as ai_bridge.tools.<name> (importing one re-binds it here).
del _common, system, project, desktop, shell, files, fleet, desktop_api, transfers, recording  # noqa: F821 -- bound by the import system, see comment above


# --- Monkeypatch compatibility (F-04-06) ----------------------------------
# In the old single-file module every tool function resolved its helpers
# (`_check`, `_win32_ui_action`, `capture_desktop_screenshot`, ...) in
# the ONE module namespace, so tests and callers could
# `monkeypatch.setattr(tools, "X", fake)` and every internal caller saw
# the fake. After the split, internal callers resolve those names in
# their SUBMODULE namespace instead, so a plain package-level patch
# would silently stop working. To preserve the old behaviour, this
# package's module object propagates attribute writes to every
# submodule that binds the same name (defining module and importers
# alike), and restores them the same way when the patch is undone.

_PATCH_SUBMODULES = (
    "_common",
    "system",
    "project",
    "desktop",
    "shell",
    "files",
    "fleet",
    "desktop_api",
    "transfers",
    "recording",
)


def _build_patch_bindings() -> dict:
    """Map each re-exported name to the submodules that bind it."""
    candidates = set(__all__) | {"LAST_FRAME_SCALE"}
    for mod_name in _PATCH_SUBMODULES:
        for name, value in vars(_sys.modules[f"{__name__}.{mod_name}"]).items():
            if name.startswith("_") and not name.startswith("__") and name not in _PATCH_SUBMODULES:
                if not isinstance(value, _ModuleType):
                    candidates.add(name)
    bindings: dict = {}
    for name in candidates:
        targets = [m for m in _PATCH_SUBMODULES if name in vars(_sys.modules[f"{__name__}.{m}"])]
        if targets:
            bindings[name] = targets
    return bindings


class _ToolsModule(_ModuleType):
    """Module type for ai_bridge.tools: propagate attribute writes.

    Any write to a re-exported name on the package (e.g. a test's
    ``monkeypatch.setattr(tools, "_check", fake)``) is also applied to
    every submodule that binds that name, so internal callers see the
    patched value exactly as they did when tools.py was one file.
    """

    _patch_bindings = _build_patch_bindings()

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for mod_name in self._patch_bindings.get(name, ()):  # noqa: SLF001
            setattr(_sys.modules[f"{__name__}.{mod_name}"], name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        for mod_name in self._patch_bindings.get(name, ()):  # noqa: SLF001
            try:
                delattr(_sys.modules[f"{__name__}.{mod_name}"], name)
            except AttributeError:
                pass


_sys.modules[__name__].__class__ = _ToolsModule

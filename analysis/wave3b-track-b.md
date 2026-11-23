> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 3B Track B — tools split, one registry, one limiter

Status: DONE. No commits made. `src/ai_bridge/tools.py` was removed with
`git rm` (staged deletion, uncommitted); no reset/rebase. Nothing outside
the assigned file set was edited. Working tree was NOT clean at start
(sibling tracks had uncommitted changes, incl. Wave 3A's DIST-07 fix in
both limiter files); this track's footprint is exactly the files below.

## 1. F-04-06 — tools.py split into a package

Old `src/ai_bridge/tools.py` (3,042 lines) is replaced by
`src/ai_bridge/tools/` (11 files, 3,045 lines incl. new docstrings):

| Module | Contents |
|---|---|
| `__init__.py` | Re-exports only; PEP 562 `__getattr__`/`__dir__`; monkeypatch propagation (below) |
| `_common.py` | Shared imports, DPI init, `LAST_FRAME_SCALE`, SoM cache, `default_policy`, `_check`, veto state/fns, `_require_no_human_takeover` |
| `system.py` | `get_system_info`, `get_resource_usage`, `get_network_info`, `get_service_status`, `get_system_monitors` |
| `project.py` | `_is_safe_workspace_file`, `create_backup`, `deploy_application`, `run_project_tests`, `run_linter` |
| `desktop.py` | Local desktop automation: caps, screenshot, win32 UI, mouse/keyboard, windows, OCR, click, SoM, clipboard, `press_hotkey`, `wait_for_text`, `verify_screen_contains` |
| `shell.py` | Package install, child-env/`_run_*` helpers, `run_shell`, `terminal_exec`, `execute_workflow` |
| `files.py` | `_host_file_access_allowed`, `_confine_file_path`, `read_file`, `write_file` |
| `fleet.py` | `broadcast_fleet_command`, `capture_desktop_frame`, `update_fleet_agent` |
| `desktop_api.py` | Versioned `desktop_*` API, `_som_element_from_rect`, `_collect_uia_elements`, `desktop_get_ui_elements`, `desktop_anticipate_action` |
| `transfers.py` | Transfer state + `desktop_handle_file_*`, `desktop_list_transfers`, `desktop_prepare_file_download` |
| `recording.py` | Session-recording state + `desktop_*_session_recording` |

### Verbatim-ness (verified by AST, not by eye)

- Every top-level def/assign from the original exists exactly once in
  the package; AST comparison of all moved functions vs
  `git show 19747b2:src/ai_bridge/tools.py` differs ONLY at:
  - `capture_desktop_screenshot`, `capture_desktop_frame`: `global
    LAST_FRAME_SCALE; LAST_FRAME_SCALE = ...` became
    `_common.LAST_FRAME_SCALE = ...` (shared mutable state must live
    in exactly one module or the two writers would diverge).
  - 17 functions: lazy relative imports changed depth `from .x` ->
    `from ..x` (code moved one package level deeper; same targets).
- A scope-aware (symtable) check confirms every global referenced in
  each submodule is defined or imported there.

### Backward compatibility (mandatory) — how it is preserved

- Public names: captured BEFORE the split by importing the committed
  module: **103 names**. `dir(ai_bridge.tools)` (non-underscore) is
  exactly that set; `__all__` is that set; `from ai_bridge.tools import
  *` yields exactly that set. Test:
  `tests/unit/test_architecture_phase4_tools.py::test_tools_package_exposes_exact_pre_split_public_names`.
- The 32 private names (`_check`, `_vk_for_token`,
  `_grab_windows_desktop_fallback`, `_encode_image_base64`,
  `_resolve_app_executable`, `_collect_uia_elements`, `_LAST_SOM_MARKS`,
  `_LAST_SOM_LOCK`, ...) are also re-exported (other modules and tests
  import them: `browser_tools.py`, `webrtc_stream.py`, `server.py`).
- `LAST_FRAME_SCALE` is NOT statically imported: it is served live
  from `_common` via module `__getattr__` (PEP 562), because
  `server.py:1093` reads `tools.LAST_FRAME_SCALE` after frame captures
  and a static import would go stale. Verified live in tests.
- Submodule attribute bindings created by the re-export imports are
  `del`eted from the package namespace so `dir()` stays exact;
  submodules stay importable (`ai_bridge.tools.desktop`, ...) and are
  served lazily by `__getattr__` if accessed as attributes.
- **Monkeypatch propagation (the subtle one).** In the single-file
  module, tests could `monkeypatch.setattr(tools, "_check", fake)` /
  `tools._win32_ui_action` / `tools.capture_desktop_screenshot` /
  `tools.perform_approved_ui_action` / `tools.run_shell` /
  `tools.get_desktop_status` / `tools._collect_uia_elements` etc. and
  every internal caller saw the fake, because all callers shared one
  module namespace. After a naive split, 14 existing tests failed for
  exactly this reason (internal callers resolve names in their
  submodule namespace). Fix: the package module's class is swapped to
  `_ToolsModule` (a `ModuleType` subclass) whose `__setattr__` /
  `__delattr__` propagate every write of a re-exported name to every
  submodule that binds that name (defining module and importers
  alike), and restore it on undo. Verified by 2 new regression tests
  plus the 14 previously-failing existing tests now passing.
- No stale bytecode: old `src/ai_bridge/__pycache__/tools.*.pyc`
  removed; `__pycache__` contains no `tools.*.pyc`.

## 2. INV-12 / TD-D01 — one tool registry

`src/ai_bridge/tool_registry.py` (new). **API contract — FROZEN for
Track C, do not change signatures:**

```python
@dataclass(frozen=True)
class ToolEntry:
    name: str
    tier: str
    handler: Callable | None
    description: str = ""
    requires_approval: bool = False

TOOL_REGISTRY: dict[str, ToolEntry]   # pre-populated at import
register_tool(name, tier, handler=None, description="", requires_approval=False) -> ToolEntry
get_tool(name) -> ToolEntry | None    # normalizes via policy.normalize_tool_name
list_tools() -> list[ToolEntry]
tools_by_tier(tier) -> list[str]
get_tier(name) -> str | None
```

- Pre-populated from `policy.TOOL_TIERS` at import: every entry,
  `handler=None`, `requires_approval=True` for `SENSITIVE`/`HIGH_RISK`.
- Tier values are `RiskTier` member names (`"READ_ONLY"`, `"LOW_RISK"`,
  `"SENSITIVE"`, `"HIGH_RISK"`).
- Imports only from `.policy` (no server/control imports, no cycles).
- `register_tool` normalizes the name (alias `file.read` lands on
  canonical `read_file`); `tools_by_tier` accepts a tier-name string
  or a `RiskTier` member.
- COUNT CORRECTION vs the brief: the brief said 144 entries
  (58 READ_ONLY / 9 LOW_RISK / 74 SENSITIVE / 3 HIGH_RISK). The actual
  `TOOL_TIERS` at commit 19747b2 (verified, policy.py unmodified) has
  **149 entries: 61 READ_ONLY / 9 LOW_RISK / 76 SENSITIVE / 3 HIGH_RISK**.
  The registry contains all 149; tests assert against live
  `TOOL_TIERS`, not the brief's count.
- Track C wiring (server.py / mcp_routes.py reading this registry) is
  NOT done here — this track only defines the registry.

## 3. INV-12 / TD-D05 — one limiter

New `src/ai_bridge/control/limiter.py` — the single entry point. It
delegates backend selection to `durable_ratelimit` (implementation
layer, kept intact): Redis when configured+reachable, in-memory when
Redis is configured but down, DB only when no Redis URL is configured
(PERF-02). Exposed API:

```python
allow(key, limit=120, window=60.0, conn=None) -> bool
allow_async(key, limit=120, window=60.0, conn=None) -> bool
rate_limit(key, limit=120, window=60) -> Callable[[], None]  # raises HTTP 429
rate_limit_async(key, limit=120, window=60) -> None          # async, raises HTTP 429
make_rate_limiter(limit=120, window=60) -> Callable[[str], None]
make_async_rate_limiter(limit=120, window=60) -> Callable[[str], None]
backend_name(conn=None) -> str
redis_required() -> bool
reset_for_tests() -> None
```

- `control/ratelimit.py` is now a thin shim re-exporting the same
  objects from `limiter.py` (verified `ratelimit.rate_limit is
  limiter.rate_limit`, etc.). All old public names keep working,
  including `make_rate_limiter` / `make_async_rate_limiter`.
- `control/durable_ratelimit.py` is NOT deleted; its public functions
  are untouched except a docstring note that `limiter.py` is the single
  entry point and this module is the implementation layer.
- DIST-07 preserved (Wave 3A behaviour): the sync callable from
  `rate_limit()` warns once per process under logger
  `ai_bridge.ratelimit` with the same message text
  ("sync rate_limit() called inside...") when called inside a running
  event loop, and falls back to the in-memory limiter for that request
  (via `durable_ratelimit._memory_allow` — same-package private reuse,
  documented in code — so there is still exactly one memory bucket and
  `reset_for_tests()` stays coherent). Async callers must use
  `rate_limit_async` / `allow_async`.
- Documented limit: the limiter docstring now states the ONE enforced
  default, **120 requests/minute** (`limit=120, window=60`), verified
  against code (`_rate_limit`, `rate_limit`, `allow` all default to
  120/60). NOTE — docstring I could NOT fix because the file is not
  mine: `control/app.py:16` still claims "120 requests/minute per
  token/IP (in-memory, per process)", while the middleware actually
  enforces 1200/min for authenticated tokens and 300/min for anonymous
  IPs (explicit overrides passed to `_rate_limit`), and the backend is
  Redis/DB/memory per selection, not "in-memory, per process".
  `docs/operations.md:16` repeats the 120/min claim. Both need the
  owning track / parent to correct; the limiter docstring records the
  override situation honestly.
- No in-repo callers were moved: no file I own imports
  ratelimit/durable_ratelimit. Remaining call sites for the parent /
  Track C to re-point at `limiter.py`:
  - `control/app.py`: `from .ratelimit import rate_limit,
    make_rate_limiter` (module top) and `from .durable_ratelimit
    import allow` inside `_rate_limit`; `backend_name` use in the
    readiness/status area (was ~line 3041, line numbers drifting under
    sibling edits).
  - `control/production_readiness.py:185`: `from . import
    durable_ratelimit as drl`.
  - `webrtc_stream.py:929`: `from ai_bridge.control.durable_ratelimit
    import allow`.
  - Tests: `tests/unit/test_distributed_phase2.py` (imports both
    modules), `tests/unit/test_phase9_deep.py` (imports
    `durable_ratelimit`) — left alone; they pass against the shim.

## 4. Task service / audit writer duplicates (verified)

- **Task creation: ONE service.** `control/task_service.py::create_task`
  (docstring: "Create one task through the single approval-gated
  path"). Nothing deleted. Other `RUNNER.submit` call sites are task
  *execution* submissions, not duplicate creation services:
  `server.py` `submit_task` wrapper (~line 922), `control/dispatch.py:306`
  (`"submit_task"` lambda), `tools/project.py` `run_project_tests` /
  `run_linter`. Recorded, not changed (not my files except
  tools/project.py, where the calls are execution, not creation).
- **Audit: ONE writer, with two live bypasses (not in my files).**
  `audit_writer.py` (`record_tool_call`, `write_audit_event`) is the
  documented single writer; it delegates to `control/db.py::audit()`
  (DB hash-chain sink) with a JSONL fallback. `control/db.py::audit()`
  itself is the sink, not a duplicate.
  - DUPLICATE WRITER FOUND: `audit.py::audit_event()` — a second,
    separate JSONL writer, still called by
    `control/dispatch.py:705` (dispatch audit log, gated by
    `AI_BRIDGE_AUDIT_LOG`). It bypasses `audit_writer` entirely.
  - DIRECT DB-SINK CALLERS (bypass `audit_writer`, same DB sink):
    `control/mcp_routes.py:369,458,553,592,653,672` (via the `audit`
    callable parameter of `install_mcp_routes`, i.e. `db.audit`),
    `control/workspace.py:43,63`, `control/mcp_oauth.py:173`. These
    write control-plane admin actions, a different event class than MCP
    tool calls, but they are still a second write path.
  - NOT a duplicate: `control/dispatch_audit.py::record_dispatch_tool`
    is a callback hook with no storage backend.
  - No duplicates live in files I own, so nothing was deleted or
    redirected; the items above are for the parent to disposition.

## Tests run (targeted only; parent runs the full suite)

Runner: `.venv/bin/python -m pytest`. New file
`tests/unit/test_architecture_phase4_tools.py`: **28 passed**.
Targeted existing suites (tools + limiter areas):
`test_distributed_phase2.py`, `test_phase9_deep.py`,
`test_desktop_gui_tools.py`, `test_feature_batch.py`,
`test_install_package.py`, `test_launch_app_paths.py`,
`test_phase1_approvals_and_vision.py`, `test_phase2_workflows.py`,
`test_phase3_desktop_autonomy.py`, `test_phase3_fleet_ops.py`,
`test_phase4_streaming_and_updater.py`, `test_workspace.py`,
`test_signed_updates.py`, `test_aci_desktop_deepening.py`:
**135 passed, 2 failed, 2 skipped.**

The 2 failures are BOTH pre-existing — they fail identically on a
pristine `git archive 19747b2` baseline copy:
- `test_phase9_deep.py::test_legacy_webrtc_session_deprecation_header`
  (also in the baseline's 12 pre-existing failures).
- `test_aci_desktop_deepening.py::test_capture_desktop_bettercam_uses_monitor_index`
  (`screen_capture.Image` has no `fromarray`; `screen_capture.py` is
  sibling-owned and modified in the working tree).

Also verified by hand: `python -c` import smoke (exact 103-name
`dir()`, star import, live `LAST_FRAME_SCALE`, submodule imports,
monkeypatch propagation + restore); `py_compile` on all new/changed
files.

## Files changed (this track only)

- Deleted: `src/ai_bridge/tools.py` (replaced by the package).
- New: `src/ai_bridge/tools/` (`__init__.py`, `_common.py`, `system.py`,
  `project.py`, `desktop.py`, `shell.py`, `files.py`, `fleet.py`,
  `desktop_api.py`, `transfers.py`, `recording.py`),
  `src/ai_bridge/tool_registry.py`, `src/ai_bridge/control/limiter.py`,
  `tests/unit/test_architecture_phase4_tools.py`,
  `analysis/wave3b-track-b.md` (this file).
- Rewritten as shim: `src/ai_bridge/control/ratelimit.py`.
- Docstring note only: `src/ai_bridge/control/durable_ratelimit.py`.

## Open items / risks for the parent

1. Track C must wire `server.py` / `control/mcp_routes.py` to
   `tool_registry.py` (API frozen above) and re-point limiter callers
   at `limiter.py` (call sites listed in §3).
2. `control/app.py:16` + `docs/operations.md:16` rate-limit docstrings
   contradict the enforced 1200/300 middleware limits (see §3).
3. Audit duplicates in §4 (`audit.py::audit_event` via
   `dispatch.py:705`; direct `db.audit` callers) need a disposition.
4. The `_ToolsModule.__setattr__` propagation in `tools/__init__.py`
   is load-bearing for existing tests that monkeypatch `tools.*`; any
   future refactor of the package must keep it (covered by regression
   tests in the new test file).
5. Brief count mismatch: `TOOL_TIERS` has 149 entries, not 144 (§2).

> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 04 — Module-by-Module Analysis: Python Core (`src/ai_bridge/`)

**Scope:** `src/ai_bridge/*.py` (26 files) + `src/ai_bridge/connectors/` (5 files).
**Method:** READ-ONLY. All LOC from `wc -l` (2026-11-17); symbols from `grep -n "^class |^def |^async def "`;
behaviour from reading the files. No tests were executed for this report; no server was run.
**Conventions:** **VERIFIED** = read in the file cited. **INFERRED** = reasonable reading, not directly
in the code. **NOT FOUND** = searched, absent. Every claim cites a real path.

## 0. Inventory at a glance

| File | LOC | Role in one line |
|---|---|---|
| `tools.py` | 2,990 | Local implementation of nearly all tools; policy gate `_check()` |
| `server.py` | 1,701 | MCP server (FastMCP); sole `@mcp.tool` registration point; device-aware routing |
| `webrtc_stream.py` | 1,348 | WebRTC desktop streaming (aiortc/PyAV), DataChannel input, TURN creds |
| `browser_tools.py` | 1,075 | Playwright browser automation, singleton browser state |
| `screen_capture.py` | 742 | Multi-monitor capture: native DXGI DLL → bettercam → Win32 → Pillow |
| `terminal_manager.py` | 637 | Interactive PTY/ConPTY terminal sessions + remote relay |
| `vision.py` | 424 | WinRT OCR (PowerShell), Win32 UI enumeration, set-of-marks |
| `client.py` | 335 | Sync Python SDK (`urllib`) for control-plane REST |
| `device_routing.py` | 279 | OAuth bearer → device binding; task submit/poll via control plane |
| `policy.py` | 266 | Risk tiers 1–4, `TOOL_TIERS` (144 entries), `decide()` |
| `execution.py` | 223 | Allowlisted, bounded, cancellable task runner (`RUNNER`) |
| `workspace.py` | 179 | Workspace file ops: confined, atomic, backup-aware |
| `desktop_audio.py` | 179 | Optional WASAPI loopback capture, silent-PCM fallback |
| `updates.py` | 173 | Ed25519-signed release verification + staged apply |
| `diagnostics.py` | 165 | Read-only cross-platform diagnostics, argv-only |
| `security.py` | 137 | Workspace confinement, secret redaction, output limits |
| `native_encode.py` | 110 | Optional native-DLL H.264 encode-timing surface |
| `git_ops.py` | 109 | Workspace-confined git via argv-only `git -C` |
| `logging_config.py` | 99 | JSON/standard structured logging config |
| `browser_isolation_os.py` | 84 | Linux netns/gVisor browser-egress isolation descriptors |
| `config.py` | 60 | Env-only `Settings`, no committed secrets |
| `dev_tools.py` | 50 | Repo tree + git status/diff + structured pytest summary |
| `tasks.py` | 36 | Backwards-compat diagnostic shim |
| `audit.py` | 23 | Append-only JSONL audit writer with redaction |
| `plane_control_flags.py` | 17 | Agent-local in-memory emergency-stop mirror |
| `__init__.py` | 11 | Package entry; `__version__="0.3.0"` |
| **Core total** | **11,452** | |
| `connectors/` total | 994 | `browserbase.py` 287, `firecrawl.py` 172, `microsoft365.py` 441, `you_search.py` 93, `__init__.py` 1 |

Counts VERIFIED: `grep -c "@mcp.tool"` = **109 in `server.py`, 0 in `tools.py`**.
`policy.py` `TOOL_TIERS` = **144 entries** (VERIFIED by AST/regex count: 58 READ_ONLY, 9 LOW_RISK,
74 SENSITIVE, 3 HIGH_RISK). Cross-check VERIFIED by script: **40 tiered names have no MCP tool**;
**5 MCP tools are not in `TOOL_TIERS`** (`system_info`, `view_live_screen`, `interact_and_view_screen`,
`select_device`, `browser_navigate_with_shot`) — see §2 and report 06.

## 1. Per-module analysis

### 1.1 `server.py` — 1,701 LOC — the MCP server
**Purpose:** The only `@mcp.tool` registration point; device-aware dispatch; local approval store;
Streamable-HTTP bearer middleware. **VERIFIED.**
**Key symbols:** `mcp = FastMCP("ai-bridge", stateless_http=True, json_response=True, ...)` (L84–89);
`_APPROVALS`, `_SELECTED_DEVICE` plain dicts, no lock/TTL (L49–53); `_consume_local_approval()` (L56);
`_require_host_tools_enabled()` (L65); `_bearer_token()` (L95); `_session_key()` (L109,
`sha256(token)[:16]`, `"default"` when no token — all anonymous stdio callers share one session key);
`_oauth_bound_device()` (L116); `_session_device()` (L130); `_admin_token()` (L145);
`_resolve_device_record()` (L152); `_find_default_windows_device()` (L230);
`_resolve_target_device()` (L262); `device_aware()` decorator (L267);
`_BearerAuthMiddleware` (L~1612); `create_http_app()` (L1684); `main()` (L1687).
**Transports VERIFIED:** stdio default (`mcp.run(transport="stdio")`); Streamable HTTP via `--http` /
`AI_BRIDGE_HTTP=1` → uvicorn on `AI_BRIDGE_MCP_HOST` (default `127.0.0.1`) : `AI_BRIDGE_MCP_PORT`
(default 8001). No SSE server transport (NOT FOUND). DNS-rebinding protection on only if
`AI_BRIDGE_MCP_ALLOWED_HOSTS` set; off if `AI_BRIDGE_MCP_DISABLE_DNS_PROTECTION=1`.
**Auth VERIFIED:** HTTP middleware requires Bearer = `AI_BRIDGE_ADMIN_TOKEN` (hmac) or a
gateway-verified token via `resolve_binding(VERIFY_URL, tok)` (`VERIFY_URL` default
`http://127.0.0.1:8002/internal/verify`); `AI_BRIDGE_HTTP_ALLOW_ANON=1` skips auth entirely;
401 body `{"detail":"Missing or invalid bearer token"}`. Stdio has no bearer check (local trust).
**Policy order VERIFIED:** (1) HTTP auth (HTTP only); (2) `@device_aware` runs before the body —
explicit `device` arg → `_resolve_target_device`, else `_session_device()` (OAuth binding, else
`_SELECTED_DEVICE`), else auto-route desktop tools (`DESKTOP_ROUTING_TOOLS`, 41 names) to the default
Windows device on non-Windows hosts; if a device resolves and `bound="route"`, `run_device_tool()` is
called and **local policy code never runs** (control plane is authoritative for remote);
`bound="control"` special-cases task/device lookups; `bound="unavailable"` (only `approve_task`)
raises when device-bound; (3) if no device, the local function runs with inconsistent in-function
enforcement (see §3).
**Dependencies:** `mcp.server.fastmcp.FastMCP`, `mcp.server.transport_security`, `.browser_tools`,
`.diagnostics`, `.git_ops`, `.tools`, `.workspace`, `.device_routing`, `.execution.RUNNER`,
`.policy.TOOL_TIERS`, `.security`.
**Risks:** see Findings F-04-01…F-04-05.

### 1.2 `tools.py` — 2,990 LOC — local tool implementations (god module)
**Purpose:** Local implementation of almost all desktop/file/shell/workflow tools; enforces policy via
`_check()`; also file-transfer + session-recording helpers. **VERIFIED.** Docstring (L1–5) states the
key invariant: "No function here trusts its caller: device_revoked / approval flags are inputs the
*server* supplies, never values an AI tool argument can set for itself."
**Key symbols:** `_check(tool, *, approval_granted=False, is_admin=False, policy=None)` (L84) →
`decide()` → raises `PermissionError`; `default_policy()` (L76) builds
`Policy(allow_writes=True, allow_tier3=settings.allow_tier3, ...)` — note `max_tier_unattended`,
`unrestricted`, `allowed_tools` are **not** set from env here (defaults 2/False/None);
`run_shell` (L1414), `terminal_exec` (L1436), `read_file`/`write_file` (L1464/L1482),
`execute_workflow` (L1592), `broadcast_fleet_command` (L1673), `update_fleet_agent` (L1789),
`_require_no_human_takeover` (L1820), the `desktop_*` family (L1820–2558), file transfers
`desktop_handle_file_start/chunk/end` (L2584–2801), session recording (L2890–2975), plus
`capture_desktop_screenshot` (L340), `launch_approved_application` (L418),
`perform_approved_ui_action` (L670), mouse/keyboard/clipboard (L821–1163), `install_package` (L1219).
**Behaviour VERIFIED:** `run_shell` uses `_split_argv` via `shlex` (no pipes/redirection/`&&`/glob),
`shell=False`, timeout capped at 3600 s (default 300), `proc.kill()` on timeout, stdout/stderr redacted
+ last 20,000 chars, env scrubbed of `_SCRUB_ENV_KEYS` (`AI_BRIDGE_ADMIN_TOKEN`,
`AI_BRIDGE_DEVICE_TOKEN`, `AI_BRIDGE_OIDC_CLIENT_SECRET`, …). `terminal_exec` is a **real shell**
(`powershell.exe -NoProfile -NonInteractive -Command` on Windows; `$SHELL -c` on POSIX) — by design,
docstring says control-plane terminal relay only, not an MCP tool. `read_file`/`write_file` use
`_confine_file_path` (workspace always OK; outside requires admin/`unrestricted`/host-tools-enabled),
secret-name refusal, `write_file` refuses overwrite unless flagged, creates parent dirs.
`create_backup`/`deploy_application` zip/copy inside the workspace, skipping symlinks, secret-named
files, `.ai-bridge-backups`; `deploy_application` is staging-only by docstring.
**Dependencies:** `.diagnostics`, `.config`, `.execution.RUNNER`, `.policy`, `.security`; lazy `.vision`,
`.control.task_runtime`.
**Risks:** 2,990-LOC god module; legacy vs `desktop_*` duplication with subtly different `_check`
call sites (e.g. `get_screen_size` passes `approval_granted=True` unconditionally, L953);
`install_package` allowlist NOT verified in this sweep; `_run_argv` kills on timeout but does not kill
the process group (contrast `execution._kill`); `desktop_*` veto/anticipate state is in-memory
(INFERRED). See Findings F-04-06, F-04-07.

### 1.3 `policy.py` — 266 LOC — risk-based policy engine
**Purpose:** Server-side risk policy. Pure logic: imports only `dataclasses, enum.IntEnum` (no I/O).
**VERIFIED.**
**Key symbols:** `RiskTier` (L21): `READ_ONLY=1`, `LOW_RISK=2`, `SENSITIVE=3`, `HIGH_RISK=4`;
`TOOL_TIERS` (L~26–175, 144 entries); `TOOL_ALIASES` (L~176; e.g. `shell.exec→run_shell`,
`pty.spawn→terminal_exec`, `desktop.*`, `file.*`); `normalize_tool_name()` (L179);
`Policy` frozen dataclass (L185): `allow_writes=True, allow_tier3=False, allowed_services=frozenset()`,
`allowed_apps=frozenset()`, `allowed_test_commands={pytest, pytest -q, npm test, go test}`,
`desktop_enabled=False, max_tier_unattended=2, allowed_tools=None, denied_tools=frozenset()`,
`unrestricted=False`; `tier_of()` (L198); `Decision` (L202); `decide(policy, tool, *,
approval_granted=False, device_revoked=False, is_admin=False)` (L221).
**Decision order VERIFIED:** (1) `device_revoked` → deny; (2) unknown/unlisted tool → deny
(default-deny); (3) `denied_tools` → deny; (4) `allowed_tools` allowlist (if set) → deny if absent;
(5) **`policy.unrestricted` → allow everything** (bypasses all tier/write/approval checks);
(6) `_WRITE_TOOLS` + `not allow_writes` → deny; (7) Tier 4: deny + `needs_approval=True` unless
`approval_granted and is_admin` — **except** if `max_tier_unattended>=4` the deny is skipped and
execution falls through to allow (L~240, VERIFIED: the guard is
`if not (max_tier_unattended>=4 …): return Deny(needs_approval)`); (8) Tier 3: desktop tools require
`desktop_enabled`; otherwise deny + `needs_approval` unless `allow_tier3 or max_tier_unattended>=3 or
is_admin or approval_granted` (except `submit_task, create_backup`); then non-admin ceiling check
`tier > effective_ceiling and not approval_granted` → deny + `needs_approval`; (9) else allow.
**HIGH_RISK tools (distinct, VERIFIED):** only 3 — `delete_workspace_file`, `approve_task`,
`m365_send_mail`. `submit_task` is Tier 3 but Tier-3-exempt in `decide()`; `request_approval` /
`cancel_task` are Tier 2.
**Risks:** `unrestricted` and `max_tier_unattended>=4` are silent full bypasses settable by admin
config; Tier-3 logic is convoluted (`effective_ceiling=max(ceiling,3)` when opted in) and hard to
audit (INFERRED). See Finding F-04-08.

### 1.4 `security.py` — 137 LOC — confinement, redaction, limits
**Purpose:** Workspace confinement, secret redaction, output limits. **VERIFIED.**
**Key symbols:** `workspace_root()` (L47), `ensure_workspace()` (L51), `confined_path()` (L57),
`is_secret_name()` (L76), `read_workspace_text()` (L80), `redact()` (L92),
`refuse_device_token_log()` (L103, alias for `redact()`), `redact_obj()` (L108),
`truncate_output()` (L130). Constants: `MAX_READ_BYTES=128000`, `MAX_OUTPUT_BYTES=64000`.
**Behaviour VERIFIED:** `redact()` regexes Bearer tokens, `api_key|password|passwd|token|secret|
client_secret|authorization` + `[:=]` + ≥4 chars, inline `device_token` (≥16 alnum/`_-`),
`turn_secret|ai_bridge_turn_secret` → `***REDACTED***`. `redact_obj()` recurses dict/list/tuple/str,
redacts keys matching `api_key|password|passwd|.*token|.*secret|authorization|credential.*` except
preserved keys `image_base64, mime_type`. `truncate_output()` = redact then UTF-8 clip + truncation
marker. `confined_path()` rejects empty/NUL/absolute/drive/UNC paths, resolves against the resolved
root, rejects symlink escape. `is_secret_name()` matches `(^|\.)env(\..+)?$`, `pem|key|pfx|p12|kdbx|
npmrc|netrc`, `id_(rsa|ed25519|ecdsa|dsa)`, `agent_gui.json`, `credentials|secret|private_key|token`
(case-insensitive, **filename-only**). `read_workspace_text()` enforces confinement + secret-name
refusal + `is_file` + size limit.
**Risks:** regex redaction misses JWTs in free text and secrets in non-`key: value` shapes
(INFERRED); secret-name check is filename-only, so `secrets/mykey.txt` passes if the name is
innocuous (VERIFIED logic); `image_base64` is exempt from redaction (accepted trade-off, INFERRED).
See Finding F-04-09.

### 1.5 `device_routing.py` — 279 LOC — device-bound MCP routing
**Purpose:** Resolve OAuth bearer → device_id; submit/wait on control-plane tasks. **VERIFIED.**
**Key symbols:** `BindingError` (L22); `set_verify_hook()` (L42); `get_http_client()` (L47);
`_token_cache_key()` (L88, SHA-256 of token); `resolve_binding(verify_url, bearer_token, timeout=10)`
(L92); `_api()` (L148); `_parse_result()` (L164); `run_device_tool(control_url, admin_token, device_id,
tool, args, wait_seconds=100, *, should_abort=None)` (L171); `get_device_task` (L254);
`cancel_device_task` (L262); `list_all_devices` (L267); `get_device` (L274).
**Behaviour VERIFIED:** `resolve_binding` — in-process `_VERIFY_HOOK` if `verify_url` empty/`inprocess*`,
else GET `verify_url` with `Authorization: Bearer`, `raise_for_status`, requires `data["ok"]`, caches
`(device_id, now+15s)` keyed by token-hash, max 256 entries (evict expired-then-oldest 32); raises
`BindingError` on any failure (fail-closed at this layer). `run_device_tool` — POST
`{control_url}/api/tasks` `{device_id, tool, args}` via `_api` (`X-Admin-Token` header), returns
`needs_approval` early if the control plane says so, else long-polls `get_device_task(wait=min(10,rem))`
until `completed/failed/cancelled` or the `wait_seconds` deadline → `running` + `task_id`;
`should_abort` → `cancel_device_task`. `list_all_devices` filters `revoked`. `get_http_client`:
singleton `httpx.Client(timeout=30/connect=10, max_connections=32, max_keepalive=16,
follow_redirects=True)`, `atexit` close.
**Risks:** 15 s verify cache delays revocation visibility; `follow_redirects=True` on admin-token
calls (token could be sent to a redirect host — INFERRED); `_api` error discards the response body;
no retry; `wait_seconds=100` default can block an MCP worker ~100 s per call. See Finding F-04-10.

### 1.6 `execution.py` — 223 LOC — bounded task runner
**Purpose:** Bounded, cancellable allowlisted task runner — NOT a general shell. **VERIFIED.**
**Key symbols:** `_validate_test_command()` (L26); `TaskResult` dataclass (L46);
`TaskRunner` (L55: `submit` L65, `_argv` L81, `_run` L98, `_kill` L191, `cancel` L207, `get` L220);
module singleton `RUNNER = TaskRunner()`. `TASK_REGISTRY` = `{uptime, disk_usage, memory_summary,
cpu_summary, run_project_tests, run_linter}`.
**Behaviour VERIFIED:** `submit(name, timeout=30, cwd=None, test_command="pytest")` — unknown name
rejected; timeout clamped to `max(1, min(timeout, max_timeout_s=300))`; thread-per-task (daemon),
`Semaphore(max_concurrent_tasks=4)`; `_argv` only exact allowlisted commands (`pytest→[pytest,-q]`,
`npm test→[npm,test,--silent]`, `go test→[go,test,./...]`, `run_linter→[sys.executable,-m,ruff,check,.]`,
`uptime`/`free` only if the binary exists else a Python fallback); `shell=False`,
`start_new_session=True`, clean env `{PATH (+venv bin), HOME=cwd, LANG=C.UTF-8}` (+ Windows
`SYSTEMROOT/WINDIR/...`); `preexec_fn` RLIMITs (non-Windows): CPU `timeout+10/+30`, AS 2 GiB,
FSIZE 100 MiB; `cwd` defaults to `AI_BRIDGE_WORKSPACE` or `tempfile.gettempdir()` and is **created**
via `mkdir(parents=True, exist_ok=True)`; `proc.communicate(timeout=timeout)` → `TimeoutExpired` →
`_kill` → status `timed_out`; output via `truncate_output`. Optional `AI_BRIDGE_TASK_NETNS=1` wraps
argv in `unshare --net --map-root-user sh -c "ip link set lo up; exec $@"` and **refuses to run if
`unshare` is missing** (fail-closed).
**Risks:** `_kill` sends only `SIGTERM` via `killpg` (Windows: `taskkill /F /T /PID`), no SIGKILL
escalation — a hung process group may survive; `cancel()` marks `cancelled` even if the process
already exited; in-memory `_tasks`/`_procs` dicts are unbounded (no eviction); `cwd` mkdir is a
side effect on a caller-controlled path (INFERRED). See Finding F-04-11.

### 1.7 `workspace.py` — 179 LOC — workspace file ops
**Purpose:** Workspace file ops — confined, atomic, backup-aware. **VERIFIED.**
**Key symbols:** `list_workspace()` (L20), `search_workspace()` (L39), `_backup()` (L66),
`_atomic_write()` (L76, temp file + `os.replace`), `create_workspace_file` (L82),
`edit_workspace_file` (L95), `move_workspace_file` (L108), `delete_workspace_file` (L118),
`read_workspace_text` (L129, delegates to `security.read_workspace_text`),
`project_structure()` (L134).
**Behaviour VERIFIED:** every path via `confined_path()`; symlinks escaping the workspace omitted in
`list_workspace`; edit/delete take a timestamped backup under `.ai-bridge-backups/`;
`search_workspace` excludes secret-named files.
**Risks:** no `_check`/policy in this module — enforcement is entirely the caller's
(`server.py`/`tools.py`); `search_workspace` reads file contents and could return secrets from
non-secret-named files (INFERRED — relies on redaction at a higher layer, not here). Note
`control/workspace.py` (142 LOC) is a **name collision only, not functional duplication**: it is
FastAPI routes for user roles/device metadata/fleet inventory — no file I/O. See Finding F-04-12.

### 1.8 `browser_tools.py` — 1,075 LOC — Playwright automation
**Purpose:** Playwright sync-API browser automation (navigate/click/type/evaluate/screenshot/download,
diagnostics, screenshot diff) with singleton browser state. **VERIFIED.**
**Key symbols:** `BrowserToolError` (L95); `resolve_device_profile` (L99); `_default_timeout_sec`
(L127); `_allow_browser_no_sandbox` (L135); `_hard_browser_isolation` (L143);
`_browser_network_lockdown` (L151); `browser_isolation_report` (L159); `playwright_launch_options`
(L181); `browser_session_info` (L216); `browser_snapshot` (L234); `attach_external_page` (L252);
`_ensure_browser` (L301); `_close_browser` (L363); `browser_navigate` (L424); `browser_screenshot`
(L453); `browser_click` (L469); `browser_type` (L494); `browser_get_text` (L523);
`browser_evaluate` (L547); `browser_download_file` (L782); `browser_expect` (L878);
`browser_compare_screenshot` (L1022); `compare_image_bytes` (L1072). 21 `_check(` calls VERIFIED.
Hard-coded: device presets `desktop/desktop_1080/iphone/pixel/tablet` with UA strings (Chrome 130,
iOS 17, Pixel 8), viewport 1280×720 default, `_default_timeout_sec(fallback=30)`.
**Risks:** process-global mutable singleton `_pw/_browser/_context/_page/_console_logs/_failed_requests`
— no lock, shared across sessions/devices; `browser_evaluate(script)` executes arbitrary JS (Tier-3
gated via `_check`, but no URL allowlist/SSRF check visible in this file); f-string JS interpolation
(`window.location.href = {url!r}`, `window.scrollBy({delta_x},{delta_y})`) — numeric args, low
injection risk, but fragile pattern (INFERRED); `--no-sandbox` gated by env (exact env name NOT
verified in this sweep). See Finding F-04-13.

### 1.9 `browser_isolation_os.py` — 84 LOC — isolation descriptors
**Purpose:** Linux netns/gVisor browser-egress isolation descriptors; wraps Chromium argv with
`bwrap`/`unshare`/`runsc`. Imports only `os, shutil, sys, typing.Any`. **VERIFIED.**
**Key symbols:** `browser_netns_requested()` (L16, `AI_BRIDGE_BROWSER_NETNS`);
`browser_gvisor_requested()` (L20); `describe_os_browser_isolation()` (L24);
`chromium_launch_wrapper_argv(inner_argv)` (L52).
**Behaviour VERIFIED:** detection-only (`shutil.which`); `bwrap --ro-bind /usr,/lib,/lib64 --proc --dev
--tmpfs /tmp --unshare-net` — no seccomp, no read-only workspace bind, `--die-with-parent` only;
gVisor `runsc do --` with no flags. Caller `browser_tools.py:162` only consults it for
reporting/launch options.
**Risk:** isolation is best-effort, not an enforced sandbox (INFERRED). See Finding F-04-14.

### 1.10 `screen_capture.py` — 742 LOC — screen capture
**Purpose:** Multi-monitor, hardware-accelerated screen capture (native DXGI DLL → bettercam →
Win32 BitBlt → Pillow). **VERIFIED.**
**Key symbols:** `_AIBridgeCaptureMetrics` (L84), `_AIBridgeMonitorInfo` (L106) (ctypes Structures);
`get_native_dll()` (L124); `get_last_capture_metrics()` (L213); `get_desktop_session_state()` (L218);
`get_virtual_desktop_bounds` (L282); `find_monitor_for_point` (L312);
`normalize_bounds_for_monitor` (L339); `get_monitors` (L386); `_capture_native_cpp` (L528);
`_capture_win32_fast` (L571); `capture_desktop(monitor_index=None, scale=1.0)` (L629);
`capture_window_by_hwnd(hwnd, scale=1.0)` (L720).
**Behaviour VERIFIED:** `capture_desktop` tries native DLL → `bettercam.create(output_idx)` (cached
per monitor in `_DXGI_CAMERAS`, failed indexes in `_DXGI_FAILED_INDEXES`) → Win32 BitBlt →
`PIL.ImageGrab.grab(all_screens=True)`; scale clamped 0.2–1.0, dimensions forced even (H.264),
min 120 px; metrics dict defaults 1920×1080/H.264.
**Risks:** **Pillow-missing fallback class silently fabricates images** — `_FallbackImageInstance.save()`
writes only the first 1024 raw bytes, `resize`/`convert` are no-ops (L21–~80, VERIFIED) — callers
cannot distinguish a real capture from a stub; global mutable DXGI camera cache, no thread lock
visible; `ctypes.CDLL` on a repo-build DLL with manually set `argtypes`/`restype` (no hash/signature
check); secure-desktop/UAC detection recorded in metrics but not enforced here. See Finding F-04-15.

### 1.11 `terminal_manager.py` — 637 LOC — PTY sessions
**Purpose:** Interactive PTY terminal sessions (Linux/macOS `pty`, Windows ConPTY/piped PowerShell) +
remote-agent relay. **VERIFIED.**
**Key symbols:** `_windows_conpty_available()` (L25); `_start_windows_conpty()` (L37);
`TerminalSession` (L200: `__init__` L203, `_start_process` L246, `_broadcast` L338,
`_pty_read_loop` L357, `_pipe_read_loop` L372, `_exec_remote_line` L387, `write` L439,
`resize` L484, `stream(since_seq=0)` L511, `close` L538); `create_terminal_session` (L581);
`get_terminal_session` (L605); `close_terminal_session` (L611); `list_terminal_sessions` (L621).
**Behaviour VERIFIED:** session id `uuid4().hex[:12]`, buffer max 1000 entries, default 120×35; local
spawn is the platform shell (PTY on POSIX, ConPTY else piped PowerShell); remote sessions delegate
lines via a `remote_exec` callback.
**Risks:** no auth/policy/timeout in this module (enforcement is the caller's — `tools.terminal_exec`
does `_check`, but `TerminalSession` is directly instantiable); unbounded `_TERMINAL_SESSIONS` dict
(no TTL/eviction — INFERRED leak until explicitly closed); `write()` feeds raw strings to the shell
(by design — interactive shell, Tier-3 via caller only). See Finding F-04-16.

### 1.12 `vision.py` — 424 LOC — OCR + UI grounding
**Purpose:** OCR (WinRT) + Win32 UI enumeration + set-of-marks grounding. **VERIFIED.**
**Key symbols:** `get_ocr_script_path()` (L95); `run_ocr(image_path)` (L105);
`list_win32_ui_controls()` (L154); `find_text_coordinates` (L256); `generate_set_of_marks` (L336).
**Behaviour VERIFIED:** OCR script is an embedded PowerShell string (`_POWERSHELL_OCR_SCRIPT`) written
to `$TEMP/ai_bridge_ocr.ps1` (or `/tmp`) on first use and executed as
`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <script> -ImagePath <resolved path>`
with a 25 s timeout; non-Windows → `{ok: False, error: "Native WinRT OCR is supported on Windows hosts."}`;
`list_win32_ui_controls` uses `OpenDesktopW("Default", …)` + `SetThreadDesktop`.
**Risks:** writes an executable script to a shared temp dir with a **predictable name**
(`ai_bridge_ocr.ps1` in `$TEMP`) — TOCTOU/replacement risk if temp is shared; `-ExecutionPolicy
Bypass` by design; `run_ocr` returns raw OCR JSON (no redaction — screen text may contain secrets,
INFERRED). See Finding F-04-17.

### 1.13 `webrtc_stream.py` — 1,348 LOC — WebRTC streaming
**Purpose:** WebRTC desktop streaming (aiortc/PyAV H.264) + DataChannel input with held-key safety +
TURN credentials. **VERIFIED.**
**Key symbols:** `generate_ephemeral_turn_credentials` (L44); `get_configured_ice_servers` (L78);
`desktop_audio_opt_in` (L106); `set_agent_context` (L130); `release_all_held_keys` (L137);
`track_key_state` (L171); `get_held_keys` (L183); `get_session_telemetry` (L188);
`list_active_stream_health` (L193); `compute_adaptive_stream_params` (L220); `grab_screen` (L243);
`ScreenTrack` (L272); `DesktopAudioTrack` (L430); `HttpSignaling` (L510); `DictSignaling` (L548);
`AgentStreamer` (L571: `start` L598, `stop` L605, `_run_loop` L610, `_check_input_rate_limit` L628,
`_session_allows_input` L645, `_handle_input_payload` L671, `_run` L1110); `start_stream` (L1310);
`stop_stream` (L1342).
**Constants VERIFIED:** `SESSION_TTL_SECONDS=600`; `DEFAULT_ICE_SERVERS=[stun:stun.l.google.com:19302]`;
input rate limit default **250 RPS** (`AI_BRIDGE_WEBRTC_INPUT_RPS`, via `control.durable_ratelimit.allow`
+ per-streamer fallback counter).
**Behaviour VERIFIED:** `grab_screen` delegates to `screen_capture.capture_desktop` but duplicates its
even-dimension scaling + Pillow fallback inline; `_handle_input_payload` checks `is_emergency_stop()`
(only `ping`/`pong`/`release_all_keys` allowed during stop), `view_only` mode blocks input,
per-session perms `control_mouse`/`control_keyboard`/`clipboard_sync` (clipboard never implied by
keyboard); disconnect paths call `release_all_held_keys()` (native DLL `aibridge_release_all_keys` +
Win32 fallback).
**Risks:** TURN default secret fallback — `generate_ephemeral_turn_credentials(secret=None)` uses
`AI_BRIDGE_TURN_SECRET` else `AI_BRIDGE_TURN_SHARED_SECRET` else, if not production
(`AI_BRIDGE_PROFILE`/`RENDER` not prod), a hard-coded default shared secret (literal not reproduced
here); default TURN host `jxiptvplayer.app:3478`, STUN `stun.l.google.com:19302`; `SESSION_TTL` 600 s
but no visible re-keying in the excerpt; `HttpSignaling._req(timeout=15)` — signalling auth via
`set_agent_context(control_url, device_token)` module globals (INFERRED). See Finding F-04-18.

### 1.14 `client.py` — 335 LOC — REST SDK
**Purpose:** Synchronous Python SDK (`urllib`, no `httpx` despite `import httpx` L39 — unused import
VERIFIED) for control-plane REST. **VERIFIED.**
**Key symbols:** `AIBridgeError` (L42); `AIBridgeClient` (L50: `_request` L83, `list_devices` L131,
`get_device` L140, `get_tools` L151, `execute` L170, `run_shell` L195, `read_file` L213,
`write_file` L220, `take_screenshot` L232, `mouse_click` L243, `mouse_move` L260, `mouse_scroll`
L265, `mouse_drag` L270, `keyboard_type` L280, `keyboard_press` L285, `list_windows` L290,
`focus_window` L295, `get_screen_size` L300, `run_agent` L306, `chat` L326).
**Behaviour VERIFIED:** `base_url` = arg or `AI_BRIDGE_URL`/`AI_BRIDGE_CONTROL_URL` or
`http://127.0.0.1:8000`; token = arg or `AI_BRIDGE_ADMIN_TOKEN`/`AI_BRIDGE_TOKEN` or `""`; `_request`
sends **both** `Authorization: Bearer <token>` and `X-Admin-Token: <token>`; timeout default 60.
**Risks:** docstring example hard-codes `https://jxiptvplayer.app` (L9, L62) — a real production host
leaked as an example; no TLS verification control, no retry; `get_tools(format="openai")` claimed in
the docstring but NOT FOUND as implemented in the read portion (INFERRED: docstring may be
aspirational); sends the admin token on every call including to a user-supplied `base_url`.
See Finding F-04-19.

### 1.15 `config.py` — 60 LOC — settings
**Purpose:** Env-only `Settings` (no committed secrets). **VERIFIED.**
**Key symbols:** `_int` (L9); `_bool` (L16); `Settings` (L20, `__init__` L37); `get_settings()` (L59,
returns **a new `Settings()` every call** — no caching).
**Fields VERIFIED:** `workspace=~/ai-bridge-workspace` (`AI_BRIDGE_WORKSPACE`, `expandvars`),
`max_read_bytes=128000`, `max_output_bytes=64000`, `max_list_entries=200`, `default_timeout_s=30`,
`max_timeout_s=300`, `max_concurrent_tasks=4`, `control_api_url=http://127.0.0.1:8000`,
`control_api_port=8000`, `tenant_id="default"`, `desktop_enabled=False`, `allow_tier3=False`,
`data_dir=~/.ai-bridge`; `admin_token`/`device_token` resolved via lazy
`from .control.secrets_backend import get_secret` inside `__init__` (exact env/key names NOT
verified verbatim in this sweep).
**Risks:** `expandvars` on the workspace path (env interpolation); defaults are localhost/permissive
workspace; no validation (e.g. negative timeouts fall back only on `ValueError` in `_int`).

### 1.16 `diagnostics.py` — 165 LOC — read-only diagnostics
**Purpose:** Read-only cross-platform diagnostics, argv-only, `shell=False`. **VERIFIED.**
**Key symbols:** `_run(argv, timeout=10)` (L17); `system_info` (L29); `resource_usage` (L35);
`disk_usage(path="/")` (L51); `memory_usage` (L56); `list_processes(limit=50)` (L60);
`service_status(service, *, allowed_services=None)` (L73); `recent_logs(source="system", lines=50)`
(L85); `list_docker_containers` (L97); `network_info` (L109).
**Behaviour VERIFIED:** `_run` returns `"unavailable: …"` strings on `FileNotFoundError`/
`TimeoutExpired`/`OSError` (never raises); `service_status` rejects `; / | & $ \` " ' space/tab/newline`
and `len>100`, allowlist-enforced; `recent_logs` Windows uses fixed PowerShell
`Get-WinEvent -MaxEvents {lines}` (lines clamped 1–200, int-interpolated — safe); Linux `journalctl -n`.
**Risks:** `disk_usage("/")` ignores the caller path (hard-coded root); `resource_usage`/
`list_processes`/`network_info` silently degrade without `psutil` (returns `cpu_percent: None` +
note); `list_docker_containers` parses `docker ps` whitespace-split (fragile if names contain spaces
— INFERRED). No `_check` in this module — enforcement is the caller's job.

### 1.17 `git_ops.py` — 109 LOC — confined git
**Purpose:** Workspace-confined git via argv-only `git -C <dir>`. **VERIFIED.**
**Key symbols:** `_git_run()` (L12); `_git` (L26); `git_status` (L31); `git_diff` (L35);
`git_log(..., limit=20)` (L39); `git_create_branch` (L44); `git_commit` (L54);
`git_apply_patch(..., check_only=False)` (L71); `git_checkpoint` (L102).
**Behaviour VERIFIED:** all `subprocess.run([git, -C, dir, *args], shell=False, timeout=30)`; missing
git → `(127, "unavailable…")`, timeout → `(124, …)`, output truncated; `git_create_branch` rejects
branch names containing `space/tab/newline/~^:`; `git_commit` truncates the message to 500 chars,
refuses empty, checks the staged diff first; `git_apply_patch` writes the patch to a temp file
**inside the workspace root** (`NamedTemporaryFile(delete=False, dir=workspace_root())`) then
`git apply --whitespace=nowarn <path>`; `git_checkpoint` does `git add -A` + commit.
**Risks:** `git_diff`/`git_status`/`git_log` return raw output strings (no `_check` in this module —
enforcement is the caller's); `git add -A` in checkpoint stages everything including untracked
secrets (mitigated only by workspace confinement, not by a secret-name check — INFERRED); temp patch
file created then unlinked in `finally` (small race window).

### 1.18 `updates.py` — 173 LOC — signed updates
**Purpose:** Ed25519-signed release verification + optional staged apply. **VERIFIED.**
**Key symbols:** `_ed25519()` (L31); `generate_keypair(out_dir)` (L39); `manifest_bytes` (L51);
`sign_manifest` (L54); `verify_manifest` (L59); `hash_file` (L68); `verify_file_hashes` (L71);
`auto_update_allowed()` (L81); `_safe_manifest_path` (L85);
`download_and_stage_update(manifest, *, staging_dir, install_dir=None, apply=False)` (L96);
`main(argv=None)` (L153).
**Behaviour VERIFIED:** private key PEM PKCS8, no encryption, `chmod 0600` on the private key; manifest
canonical JSON `sort_keys, separators=(",",":")`; `verify_manifest` returns bool (swallows exception);
`download_and_stage_update` requires `path/url/sha256` per entry, downloads via
`urllib.request.urlopen(timeout=60)`, hash-checks each file + re-verifies staging, `apply` requires
`AI_BRIDGE_ALLOW_AUTO_UPDATE=1` and `install_dir`, copies via `_safe_manifest_path` (rejects absolute/
`:`-drive paths, enforces `is_relative_to` root).
**Risks:** `urlopen` with no TLS pinning/allowlist — the manifest URL controls the download origin
(this function itself does NOT verify the manifest signature — INFERRED caller responsibility);
whole file read into memory (`resp.read()`, `hash_file` reads the whole file); no download size cap.
See Finding F-04-20.

### 1.19 `desktop_audio.py` — 179 LOC — WASAPI loopback
**Purpose:** Optional WASAPI loopback capture (opt-in `AI_BRIDGE_DESKTOP_AUDIO`); silent-PCM fallback
when unavailable. **VERIFIED.**
**Key symbols:** `wasapi_capture_available()` (L20); `probe_wasapi_loopback()` (L26);
`reset_probe_cache()` (L74); `WasapiLoopbackCapture` (L79: `__init__` L82 defaults 48000 Hz/2ch/960
samples, `start` L105, `read_pcm` L168, `stop` L174).
**Risks:** probe cached process-wide (`_PROBE_CACHE`); queue maxsize 32 with 0.05 s put timeout
(drop on full — INFERRED silent gaps); Windows-only in practice, no-op elsewhere (honest fallback
VERIFIED in docstring/code).

### 1.20 `dev_tools.py` — 50 LOC — repo scaffolding
**Purpose:** Repo-aware scaffolding: tree + git status/diff + structured pytest summary.
**VERIFIED.** Symbols: `dev_workspace_plan(relative_directory=".", *, max_depth=3)` (L10);
`_parse_pytest_summary` (L20); `run_project_tests_structured` (L35). Imports `re`, `.git_ops`,
`.workspace`, `.tools.run_project_tests`.
**Risks:** thin wrapper, inherits `git_ops`/`workspace` risks; regex-only pytest parsing
(`(\d+)\s+passed/failed/...`) — brittle vs pytest output variants.

### 1.21 `tasks.py` — 36 LOC — diagnostic shim
**Purpose:** Backwards-compat diagnostic shim; new code should use `execution.RUNNER`/`diagnostics`.
**VERIFIED.** Symbols: `_uptime` (L13); `_disk_usage` (L19); `_memory_summary` (L23);
`run_diagnostic(task_name)` (L33); `TASKS` dict `{uptime, disk_usage, memory_summary}`.
**Risk:** `cpu_summary` is in `execution.TASK_REGISTRY` but NOT FOUND as a `TASKS` key in this file —
`execution._run` falls through to `diagnostics.resource_usage()` for it (INFERRED benign but
confusing); `_memory_summary` returns a "not implemented" string on non-Linux instead of an error.

### 1.22 `audit.py` — 23 LOC — JSONL audit writer
**Purpose:** Append-only JSONL audit writer; redacts secrets before write. **VERIFIED.**
**Key symbols:** `audit_event(log_path, *, actor, device_id, tool, arguments, decision, result="",
tenant="default")` (L11).
**Behaviour VERIFIED:** `redact_obj(json.loads(json.dumps(arguments, default=str)))`, `result` redacted
+ truncated to 2000 chars, `ts` UTC `%Y-%m-%dT%H:%M:%SZ`, `log_path.parent.mkdir(parents=True,
exist_ok=True)`, append mode.
**Risks:** no fsync/locking (concurrent writers can interleave), no rotation/size cap, no
integrity/hash chain — tampering/deletion undetectable; caller is only `control/dispatch.py:705`
VERIFIED (`grep -rn audit_event` — `server.py`/`tools.py` do NOT call it). See Finding F-04-21.

### 1.23 `plane_control_flags.py` — 17 LOC — emergency-stop mirror
**Purpose:** Agent-local mirror of the durable emergency-stop flag (thread-safe bool). **VERIFIED.**
Symbols: `set_emergency_stop(active)` (L10); `is_emergency_stop()` (L16); module `_LOCK` +
`_EMERGENCY_STOP=False`.
**Risks:** in-memory only — a process restart loses state; no persistence/audit here (durable flag
lives in `control/plane_state.py` — INFERRED from grep); **NOT enforced by `server.py`/`tools.py`** —
grep shows only `webrtc_stream.py:681` (`_handle_input_payload`) and `control/*` consult it. MCP
shell/desktop tools ignore emergency stop (VERIFIED). See Finding F-04-22.

### 1.24 `native_encode.py` — 110 LOC — native encode timing
**Purpose:** Honest integration surface for optional `ai_bridge_native.dll` H.264 encode-timing
(NOT a media path). **VERIFIED.**
**Key symbols:** `native_encode_mode()` (L11, env `AI_BRIDGE_NATIVE_ENCODE`, valid
`{off, auto, prefer, required}`, `0/false/no/disabled→off`, `1/true/yes/on→prefer`, default `auto`);
`native_dll_search_paths()` (L22); `describe_native_encode()` (L38);
`should_use_native_encode_timing()` (L86); `encoder_backend_label(*, native_timing_ran)` (L105).
**Behaviour VERIFIED:** DLL search paths are `windows-agent/build/{Release,Debug,}/ai_bridge_native.dll`
relative to the repo root (`parents[2]`); `required` mode raises if the DLL/
`aibridge_encode_h264_frame` is missing; `describe_*` explicitly states the fallback is
`pyav_screen_track`, the shipping path is `python_aiortc`, libwebrtc E2E `unverified` unless
`AI_BRIDGE_LIBWEBRTC_E2E_VERIFIED=1`.
**Risks:** DLL loaded from build-tree paths (no signature/hash check in this file — VERIFIED:
`get_native_dll` does `ctypes.CDLL` on the first existing candidate); env-driven mode can silently
downgrade to software.

### 1.25 `logging_config.py` — 99 LOC — logging
**Purpose:** JSON/standard structured logging config. **VERIFIED.**
**Key symbols:** `JSONFormatter` (L21, `format` L24 — emits `timestamp, level, logger, message, module`
+ traceback; message passed through `refuse_device_token_log`); `configure_logging(level=None)`
(L48). Level from arg or `AI_BRIDGE_LOG_LEVEL` or `INFO`; JSON on if `AI_BRIDGE_JSON_LOGS` in
`1/true/yes` (default `1`); `dictConfig` root + `ai_bridge` + `uvicorn` at level, `httpx` at WARNING,
stdout `StreamHandler`.
**Risks:** no file handler/rotation; `traceback` imported but VERIFIED unused in the formatter excerpt
(INFERRED: exception info may not be attached); redaction is regex-based only (see `security.py`).

### 1.26 `__init__.py` — 11 LOC
Package entry; exports `AIBridgeClient, AIBridgeError`, `__version__="0.3.0"`. No class/def.
Risk: none material; version hard-coded, no `server`/`tools` re-export (INFERRED: importers must use
submodules).

### 1.27 `connectors/` — 994 LOC
`__init__.py` (1 LOC, docstring only: "Third-party service connectors exposed to AI clients as
policy-gated tools").

| File | LOC | Purpose / key symbols |
|---|---|---|
| `browserbase.py` | 287 | Browserbase cloud-browser sessions via `httpx` to `https://www.browserbase.com/v1`. `BrowserbaseError` (L22), `browserbase_create_session` (L48), `browserbase_connect` (L95), `browserbase_navigate` (L167), `browserbase_screenshot` (L196), `browserbase_end_session` (L208), `reset_for_tests` (L223). Process-local `_BB` dict holds Playwright `pw/browser/context/page/session_id/connect_url`. Risk: durable CDP session in globals, no timeout visible in the excerpt. |
| `firecrawl.py` | 172 | Firecrawl scrape/search/map/crawl via `https://api.firecrawl.dev/v1`, `redact` on output. `FirecrawlError` (L14), `_api_key` (L18, `AI_BRIDGE_FIRECRAWL_API_KEY`/`FIRECRAWL_API_KEY`), `firecrawl_scrape` (L34), `firecrawl_search` (L60), `firecrawl_map_site` (L77), `firecrawl_crawl` (L95). |
| `microsoft365.py` | 441 | Microsoft Graph OAuth (PKCE) + mail/calendar/files. `M365Error` (L49), `config` (L54), `_require_config` (L71), `_store_path` (L87), `_fernet` (L92), `_load_all/_save_all/_put_tokens` (L101–125), `disconnect` (L140), `new_login` (L150), `_token_request` (L174), `complete_login` (L186), `status` (L203), `_access_token` (L220), `graph` (L234), `send_mail` (L374, Tier 4), `create_event` (L387, Tier 3). Tokens Fernet-encrypted at rest, file 0600, never returned; write scopes only when `AI_BRIDGE_M365_ALLOW_WRITE=1`. Risk: token store path/key envs not verified verbatim here. |
| `you_search.py` | 93 | You.com search/fetch via `https://api.ydc-index.io/v1`. `YouSearchError` (L12), `_api_key` (L16, `YOU_API_KEY`/`AI_BRIDGE_YOU_API_KEY`), `you_web_search` (L23), `you_fetch_contents` (L47). |

Note: `browserbase_*`/`m365_*` tools exist in `policy.TOOL_TIERS` but have **no `@mcp.tool` in
`server.py`** — they are reachable only via control-plane dispatch (INFERRED: `control/dispatch.py` /
agent paths), not directly via this MCP server. VERIFIED as NOT FOUND in `server.py`.

## 2. Duplicated responsibilities (VERIFIED)

1. **Workspace file ops — `workspace.py` vs `control/workspace.py`:** name collision only, NOT
   functional duplication (see §1.7). Risk is confusion/import mistakes, not double implementation.
2. **Audit — three mechanisms:** `audit.py:audit_event` = JSONL file append (used only by
   `control/dispatch.py:705` VERIFIED). `control/dispatch_audit.py` (61 LOC) = optional in-process
   callback with `should_audit_tool()` and `record_dispatch_tool()` (no-op if no callback; swallows
   callback exceptions). Control-plane `audit()` (in `control/app.py`, via `control/db.py` /
   `worm_siem.py:emit_audit_event`) is the DB/WORM path. **Gap VERIFIED:** `server.py`/`tools.py`
   never call `audit.py:audit_event`; MCP tool calls are therefore not audited by the file logger.
3. **Screen capture — three paths:** `webrtc_stream.grab_screen` (L243) delegates to
   `screen_capture.capture_desktop` but duplicates its even-dimension scaling + Pillow fallback inline;
   `tools.py` has its own `_grab_windows_desktop_fallback` (L263) and `_encode_image_base64` (L325)
   plus `capture_desktop_screenshot`/`capture_desktop_frame`/`desktop_get_screenshot` wrappers.
4. **Tool implementation vs exposure — `tools.py` vs `server.py`:** every MCP tool is implemented in
   `tools.py`/`browser_tools.py`/`workspace.py`/etc. and re-wrapped in `server.py` with
   `@mcp.tool` + `@device_aware` + a second policy check. The two layers disagree (see §3).
5. **Desktop automation — legacy vs `desktop_*` in `tools.py`:** `mouse_click/move/scroll/drag`,
   `keyboard_type/press`, `get/set_clipboard`, `take/capture_desktop_screenshot`,
   `launch_approved_application`, `run_shell` duplicated by `desktop_click/mouse_move/scroll`,
   `desktop_keyboard`, `desktop_clipboard_get/set`, `desktop_get_screenshot`,
   `desktop_open_application`, `desktop_execute_command` — same Win32 backends, different names/tiers/
   `_check` call sites. All 109 MCP tool names are listed in report 06, §3.
6. **Shell execution — three paths:** `execution.RUNNER` (allowlisted, sandboxed), `tools.run_shell`
   (shlex argv, `shell=False`), `tools.terminal_exec` (real shell `sh -c`/`PowerShell -Command`), plus
   `terminal_manager.TerminalSession` (interactive PTY). Different timeout/kill/env policies per path.
7. **Emergency stop — `plane_control_flags.py` vs `control/plane_state.py`:** local in-memory mirror
   vs durable control-plane flag. Only `webrtc_stream` reads the local mirror; MCP tools do not —
   an enforcement gap, not duplication.
8. **Diagnostics — `diagnostics.py` vs `tasks.py` vs `execution.py`:** `tasks.run_diagnostic` shim
   wraps `diagnostics`; `execution._run` calls `tasks.run_diagnostic` for `uptime`/`memory_summary`
   and `diagnostics` directly for `disk_usage`/`cpu_summary`.

## 3. The two-layer policy problem (the core's central finding)

`server.py` has only **3 `_check(` occurrences and 2 `decide(` occurrences** (VERIFIED by string
count) against 109 tools, while `tools.py`/`browser_tools.py` enforce via `_check()` inside each
implementation. An automated scan of the 109 wrappers found ~40 with no direct
`_check`/`_consume_local_approval`/`decide` in the wrapper (see report 06, §4 for the full list).
Most Tier-1 reads delegate to `tools.*`/`diagnostics`/`workspace`/`git_ops`, which mostly `_check`
internally — **but** `workspace.py`, `diagnostics.py` and `git_ops.py` have **no `_check` at all**, so
`list_workspace`, `read_workspace_file`, `search_workspace`, `get_disk_usage`, `get_memory_usage`,
`get_recent_logs`, `list_processes`, `list_docker_containers`, `git_status`, `git_diff`,
`dev_workspace_plan`, `browser_session_info` bypass `denied_tools`/`allowed_tools`/`device_revoked`
allow/deny lists even though they are Tier 1 (always-allowed by tier). Impact is limited to
allow/deny-list bypass for read-only tools (INFERRED), but it breaks the documented invariant that
"each function enforces policy before doing work" (`tools.py` docstring) at the server layer.
Separately, `create_workspace_file` does `tools._check` but `edit`/`move` use inline `decide()` and
`delete` uses only `_consume_local_approval` (no `decide()` — Tier 4 via approval record only, no
`is_admin` check in the wrapper). `write_file` (`server.py:1019`) never passes `approval_granted`,
so an approval obtained via `request_approval`/`approve_task` can never unlock it.

## 4. Findings

| ID | Severity | File | Evidence |
|---|---|---|---|
| F-04-01 | P1 | `src/ai_bridge/server.py:56-64,577-586` | `_APPROVALS` in-memory, unscoped to user/session/device, no expiry; `_consume_local_approval` consumes the first matching approved record for any tool name. Approval is single-use but cross-session. |
| F-04-02 | P1 | `src/ai_bridge/server.py:116-143` | `_oauth_bound_device()` swallows **all** exceptions → returns `None` → session treated as unbound/local. Fail-open conversion of a fail-closed `resolve_binding`. |
| F-04-03 | P0 | `src/ai_bridge/server.py:79-82,1655-1660` | `AI_BRIDGE_HTTP_ALLOW_ANON=1` disables HTTP auth; `AI_BRIDGE_MCP_DISABLE_DNS_PROTECTION=1` disables DNS-rebinding protection. Single-env-var security off-switches. |
| F-04-04 | P1 | `src/ai_bridge/server.py:924-959` | `select_device` is itself an MCP tool with no auth/policy check in the wrapper; any authenticated caller can re-target the session. Auto-routing to a Windows device is silent. |
| F-04-05 | P1 | `src/ai_bridge/server.py` (109 wrappers) | Inconsistent local policy: only 3 `_check(` / 2 `decide(` in the file; ~40 wrappers have no direct policy call; Tier-1 wrappers bypass `denied/allowed_tools` lists (see §3). |
| F-04-06 | P2 | `src/ai_bridge/tools.py` (2,990 LOC) | God module: desktop automation (Win32 SendInput, UIA, SoM), shell, files, fleet, transfers, recording in one file; legacy vs `desktop_*` duplication with different `_check` call sites. |
| F-04-07 | P2 | `src/ai_bridge/tools.py:953,1414-1436` | `get_screen_size` passes `approval_granted=True` unconditionally; three shell paths (`RUNNER`, `run_shell`, `terminal_exec`) with different timeout/kill/env policies. |
| F-04-08 | P0 | `src/ai_bridge/policy.py:196,221-266` | `Policy.unrestricted` bypasses all tier/write/approval checks; `max_tier_unattended>=4` silently authorises Tier 4 without approval. A single config value disables the model. |
| F-04-09 | P2 | `src/ai_bridge/security.py:76-92` | Regex-only redaction; `is_secret_name()` is filename-only (`secrets/mykey.txt` passes); `image_base64` exempt from redaction. |
| F-04-10 | P2 | `src/ai_bridge/device_routing.py:92-147,171-253` | 15 s verify cache delays revocation; `follow_redirects=True` on admin-token calls; `wait_seconds=100` blocks an MCP worker ~100 s per call; `_api` errors discard the body. |
| F-04-11 | P2 | `src/ai_bridge/execution.py:191-220` | `_kill` is SIGTERM-only (no SIGKILL escalation); unbounded in-memory `_tasks`/`_procs`; `cancel()` can report success after the process exited. |
| F-04-12 | P2 | `src/ai_bridge/workspace.py` + `src/ai_bridge/control/workspace.py` | No `_check` in `workspace.py` (caller-enforced only); module-name collision with the unrelated control-plane routes file. |
| F-04-13 | P1 | `src/ai_bridge/browser_tools.py:301-363,547` | Process-global mutable singleton browser shared across sessions/devices, no lock; `browser_evaluate` runs arbitrary JS with no URL allowlist/SSRF check visible in the file. |
| F-04-14 | P2 | `src/ai_bridge/browser_isolation_os.py:24-84` | Isolation is detection-only (`shutil.which`); `bwrap` has no seccomp and no read-only workspace bind; gVisor `runsc do --` with no flags. Best-effort, not an enforced sandbox. |
| F-04-15 | P1 | `src/ai_bridge/screen_capture.py:21-80,124` | Pillow-missing fallback silently fabricates images (1024 raw bytes, no-op resize/convert); `ctypes.CDLL` on a repo-build DLL with no hash/signature check; DXGI cache has no visible lock. |
| F-04-16 | P2 | `src/ai_bridge/terminal_manager.py:200-637` | No auth/policy/timeout in the module; unbounded `_TERMINAL_SESSIONS` (no TTL/eviction); `write()` feeds raw strings to the shell by design. |
| F-04-17 | P1 | `src/ai_bridge/vision.py:95-153` | OCR PowerShell script written to a predictable shared-temp path (`ai_bridge_ocr.ps1`) — TOCTOU/replacement risk; `-ExecutionPolicy Bypass`; raw OCR JSON returned unredacted. |
| F-04-18 | P1 | `src/ai_bridge/webrtc_stream.py:44-78` | Non-prod TURN fallback to a hard-coded default shared secret; default TURN host `jxiptvplayer.app:3478`; no visible session re-keying within the 600 s TTL. |
| F-04-19 | P2 | `src/ai_bridge/client.py:9,39,62,83-130` | Unused `import httpx`; docstring hard-codes `https://jxiptvplayer.app`; admin token sent via both headers on every call to a user-supplied `base_url`; no retry/TLS control. |
| F-04-20 | P2 | `src/ai_bridge/updates.py:96-152` | `urlopen` with no TLS pinning/allowlist; this function does not verify the manifest signature (caller responsibility, INFERRED); whole files read into memory; no download size cap. |
| F-04-21 | P1 | `src/ai_bridge/audit.py:11-23` | No fsync/locking/rotation/integrity hash chain; only caller is `control/dispatch.py:705` — MCP tool calls are never written to this log. |
| F-04-22 | P1 | `src/ai_bridge/plane_control_flags.py:10-17` | In-memory emergency-stop mirror; only `webrtc_stream.py:681` reads it. MCP shell/desktop tools ignore emergency stop. |
| F-04-23 | P2 | `src/ai_bridge/config.py:37-60` | `get_settings()` uncached (re-reads env + secrets backend per call — INFERRED perf, not correctness); `expandvars` on workspace path; no validation of numeric fields. |
| F-04-24 | P3 | `src/ai_bridge/tasks.py:13-36` | Shim/`TASK_REGISTRY` mismatch: `cpu_summary` in `execution.TASK_REGISTRY` but not a `TASKS` key; non-Linux `_memory_summary` returns a string, not an error. |

**NOT FOUND / not verified in this sweep:** exact env name for `_allow_browser_no_sandbox`;
`install_package` allowlist; `control/secrets_backend.get_secret` exact env/key mapping;
`test_policy_bypass` (referenced in `policy.py` docstring only); whether `server.py`'s
`view_live_screen`/`interact_and_view_screen` wrappers are covered by internal `_check` in
`tools.*` (partial — INFERRED, see report 06).

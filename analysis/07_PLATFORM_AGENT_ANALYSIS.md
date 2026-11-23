> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 07 — Platform Agent Analysis

**Scope:** Python platform agents only (`agents/` + the control-plane agent endpoints they talk to).
Native C++ is report 08; WebRTC media/input detail is report 13.
**Method:** read-only source inspection of the files listed below. No agent was executed
against a live control plane in this track; behaviour below is code-reading unless marked
otherwise. **Status labels:** `VERIFIED` = read in the cited file; `INFERRED` = follows from
code but not executed; `NOT FOUND` = searched for and absent.

## 1. Inventory (measured)

| File | LOC (`wc -l`) | Role |
|---|---|---|
| `agents/common/poller.py` | 534 | Shared outbound poller: heartbeat, claim, execute, result, rotation, WS side-channel |
| `agents/common/spool.py` | 131 | SQLite offline result spool (`agent_spool.db`) |
| `agents/linux/agent_main.py` | 1,055 | Linux CLI: run/status/doctor/config/policy/rotate-token/service/enroll/update |
| `agents/macos/agent_main.py` | 94 | macOS thin entrypoint (same `Poller`) |
| `agents/windows/agent_main.py` | 212 | Windows CLI entrypoint + saved-config resolution |
| `agents/windows/app_runtime.py` | 330 | DPAPI token seal/unseal, single-instance guard, file logging |
| `agents/windows/desktop_helper.py` | 79 | Thin wrappers over `ai_bridge.tools` desktop functions |
| `agents/windows/diagnose.py` | 201 | `--diagnose` report builder (connectivity/config/token checks) |
| `agents/windows/gui_app.py` | 2,214 | Tkinter Fluent-2 GUI; runs its **own** poll loop, not `Poller.run_forever()` |
| `agents/windows/gui_ui.py` | 1,659 | GUI widgets/theme/layout only |
| `agents/windows/windows_agent.py` | 37 | `sc query` / event-log / system-info wrappers over `ai_bridge.diagnostics` |
| `agents/linux/run-agent.sh` | — | Runner script (not line-counted here) |
| **Total above** | **~6,546** | Python agent surface |

Server side of the agent contract (read for context, owned by the control-plane track):

| File / symbol | What it does |
|---|---|
| `src/ai_bridge/control/app.py:4419` `POST /api/agent/heartbeat` | Marks device `online`, stores telemetry (capped at 500 rows/device), returns `emergency_stop`, `token_expires_at` |
| `src/ai_bridge/control/app.py:4473` `GET /api/agent/whoami` | Read-only token check; does not touch `last_seen` |
| `src/ai_bridge/control/app.py:4481` `@app.websocket("/api/agent/ws")` | Wake/cancel side-channel; HTTP claim remains authoritative |
| `src/ai_bridge/control/app.py:4542/4558` `GET/PUT /api/agent/policy` | Read / update device policy; PUT calls `reject_policy_widening()` |
| `src/ai_bridge/control/app.py:4575` `POST /api/agent/rotate` | Issues new token, **old token invalid immediately** |
| `src/ai_bridge/control/app.py:4607` `GET /api/agent/tasks/next` | Long-poll claim (see §5) |
| `src/ai_bridge/control/app.py:4691` `GET /api/agent/tasks/{id}/execution-state` | Cancel/timeout/emergency-stop polling |
| `src/ai_bridge/control/app.py:4704` `POST /api/agent/tasks/{id}/result` | Result ingest; screenshot tools get a 16 MB cap, others `MAX_RESULT_CHARS` |
| `src/ai_bridge/control/app.py:1008` `POST /api/devices/enroll` | Admin-token enrollment; token TTL from `AI_BRIDGE_DEVICE_TOKEN_TTL_DAYS` (default 7, `app.py:874`) |
| `src/ai_bridge/control/app.py:880-884` `device_row()` | Token-hash lookup, `revoked=0`, expiry → HTTP 401 |
| `src/ai_bridge/control/orchestration.py:16,27` | Per-device running lease, default 900 s (`AI_BRIDGE_TASK_RUNNING_LEASE_SEC`); stale-task sweep/requeue |
| `src/ai_bridge/control/agent_channel.py` | In-memory WS subscriber registry; `notify_task_available/cancel/emergency_stop` |

## 2. Agent lifecycle

```mermaid
flowchart TD
    A[Enroll: POST /api/devices/enroll<br/>X-Admin-Token] --> B[Save device_token<br/>+ control_url]
    B --> C[Start: Poller(control_url, token)]
    C --> D[WS side-channel<br/>/api/agent/ws]
    C --> E[Loop: spool.flush() -> heartbeat()<br/>-> GET tasks/next?wait=5/15]
    E -->|task| F[dispatch(tool, args,<br/>effective policy, approval_granted)]
    F --> G[Spool result locally<br/>then POST /api/agent/tasks/{id}/result]
    G --> E
    E -->|401| H[Retry once with<br/>saved token from disk]
    E -->|error| I[Backoff 2s doubling<br/>to 60s cap]
    F -->|cancel polled| J[TaskCancelled -><br/>status=cancelled]
    D -->|cancel / emergency_stop| F
```

### 2.1 Initialisation — VERIFIED

- **Shared core is one class.** All three platforms construct `common.poller.Poller`
  (`agents/common/poller.py:110-145`). The poller is outbound-only: it never listens on a
  port (docstring, `poller.py:1-9`). `Poller.__init__` registers the agent context for
  WebRTC with `webrtc_stream.set_agent_context(control_url, device_token)` (`poller.py:126-131`).
- **Linux** (`agents/linux/agent_main.py:1003-1055`, `run_agent_loop()`): builds an
  unrestricted `Policy` (§2.5), pushes it to the plane, installs `SIGINT`/`SIGTERM`
  handlers that call `sys.exit(0)`, then `poller.run_forever()` (or one `run_once()` with `--once`).
- **macOS** (`agents/macos/agent_main.py:81-94`): no policy argument at all — plain
  `Poller(url, token)`. No signal handlers, no config files, no enroll/doctor/status.
- **Windows CLI** (`agents/windows/agent_main.py:190-212`): configures file logging +
  excepthook (`app_runtime.configure_file_logging/install_excepthook`), then constructs
  `Poller` with DPAPI `token_saver`/`token_loader` hooks so rotation never writes plaintext.
- **Windows GUI** (`agents/windows/gui_app.py:1622-1653,1678-~1900`): does **not** use
  `Poller.run_forever()`. `start_agent()` spawns a daemon thread running its own
  `_poll_loop()`, which duplicates `run_once()` logic inline (heartbeat, `GET tasks/next`,
  cancel polling, dispatch) while also driving Tk widgets via `root.after(0, …)`.
  The GUI therefore has **two implementations of the poll loop** that can drift (§7, F07-03).

### 2.2 Enrollment — VERIFIED

- **Linux only has a built-in enroll command** (`agents/linux/agent_main.py:777-824`,
  `enroll_cmd`): `POST {control_url}/api/devices/enroll` with `X-Admin-Token`, body
  `{"name","hostname","os":"linux","tenant":"default"}` — note **tenant is hardcoded
  `"default"`**. On success it calls `save_agent_config()` (writes `agent.json`,
  `agent.env`, and `device.token`, all `0600`, config dir `0700`) and then pushes the
  unrestricted policy (`sync_linux_server_policy`, §2.5).
- **macOS / Windows CLI have no enroll subcommand.** macOS docstring says pass a token
  obtained elsewhere; Windows relies on the GUI or a manually placed token/config
  (`agent_main.py:25-93` searches `agent_gui.json`, `agent.env`, `run-agent.cmd` in
  `%APPDATA%`, `%ProgramData%`, `~/.ai-bridge`).
- **Server side** (`control/app.py:1008-1047`): enrollment requires an admin/session token,
  hashes the device token (`dbmod.hash_token`), sets `token_expires_at = now + TTL`
  (default 7 days), and returns the token **once**. There is no agent-side proof that
  the one-time token is ever shown/stored securely beyond the files above.

### 2.3 Authentication — VERIFIED

- Every agent request carries `X-Device-Token: <raw bearer token>` (`poller.py:145-152`,
  `_req()`). No mTLS from the Python agent: `device_mtls.require_device_mtls()` is
  enforced server-side on `tasks/next` (`control/app.py:4607+`) but the Python agent
  never presents a client certificate — `INFERRED`: mTLS is effectively opt-in / unenforced
  unless a fingerprint was enrolled and a proxy supplies `X-Client-Cert-Fingerprint`.
  `NOT FOUND` in `agents/`: any client-cert, fingerprint, or TLS-context configuration.
- TLS verification is the `urllib` default (no custom context, no `verify=False` found) —
  `VERIFIED` by absence of any `ssl` import in `agents/common/poller.py` and
  `agents/linux/agent_main.py` (`grep ssl` returns only unrelated matches). `INFERRED`:
  standard verification applies; a MITM with a trusted CA cert would still succeed.
- **Token storage differs by platform (§2.6).**

### 2.4 Heartbeat — VERIFIED

- `Poller.heartbeat()` (`poller.py:248-275`): `POST /api/agent/heartbeat` with
  `hostname`, `os`, and `metrics` from `_collect_metrics()` (`poller.py:52-108` — local IP
  via a UDP connect to `8.8.8.8:80` (no traffic sent), `psutil` CPU/mem/disk/uptime with
  `/proc` + `shutil` fallbacks). It applies the server's `emergency_stop` flag to the
  process-global `plane_control_flags` and triggers rotation when
  `token_expires_at - now < 48 h` (`ROTATE_THRESHOLD_SECONDS = 2*86400`, `poller.py:21`).
- **401 recovery:** on `HTTP 401` it loads the saved token from disk and retries once
  if it differs from the in-memory token (`poller.py:258-268`). `INFERRED`: this covers
  the GUI/CLI rotation race (another process rotated the token); it does **not** cover
  a genuinely expired/revoked token, which then raises and the loop backs off forever
  until an operator re-enrolls. There is no agent-side re-enrollment or alerting.
- Server heartbeat (`control/app.py:4419-4471`) updates `status='online'`, `last_seen`,
  hostname/OS, inserts one telemetry row per heartbeat and prunes to 500/device.
  Heartbeat frequency is therefore the polling cadence (one heartbeat per `run_once()`,
  i.e. roughly every long-poll wait + execution time) — `NOT FOUND`: a separate,
  fixed-interval heartbeat timer. A long-running task delays the next heartbeat for its
  whole duration, so `last_seen` goes stale during long executions (`INFERRED`).

### 2.5 Policy — VERIFIED, and the most important finding

- `Poller._effective_policy()` (`poller.py:277-344`) merges the **server-sent task policy**
  with the agent's **local explicit policy**. When an explicit policy exists (Linux
  unrestricted; Windows GUI policy from the Settings tab), the merge is **monotone
  widening**:
  ```python
  allow_writes = remote_allow_writes or base.allow_writes
  max_tier_unattended = max(remote_max_tier, base.max_tier_unattended)
  allowed_apps = remote_apps | base.allowed_apps   # union
  ```
  The server can therefore **never narrow** a locally-unrestricted agent. The server-side
  `PUT /api/agent/policy` guard (`reject_policy_widening`, `control/app.py:4558`) only
  constrains what is *stored* on the plane; the agent's local `dispatch()` call uses the
  locally-merged policy, so a compromised or misconfigured local policy file silently
  overrides plane policy. See F07-01.
- **Linux defaults to full autonomy.** `get_linux_server_policy()` (`linux/agent_main.py:108-121`)
  returns `unrestricted=True, max_tier_unattended=4, allowed_apps={"*"}, allowed_services={"*"},
  allowed_test_commands={"*"}, denied_tools={}` and `sync_linux_server_policy()` pushes
  exactly that to the plane on every start (`run_agent_loop`, `linux/agent_main.py:945-951`).
  `INFERRED`: on Linux, Tier-4 / destructive operations are pre-authorised by design;
  the "human approval" path is effectively bypassed for this platform unless an operator
  edits the stored policy afterwards — and the next agent restart re-pushes unrestricted.
- **macOS** passes no explicit policy, so it uses `default_policy()` and the remote
  policy verbatim (narrowing works there).
- **Windows GUI** defaults (`gui_app.py:321-360` `load_agent_config`): `desktop_enabled=True,
  allow_tier3=True, max_tier_unattended=3, allow_writes=True, allow_shell=True,
  allow_package_install=True`, allowed apps include `powershell`, `cmd`, `code`, `chrome`,
  `msedge`, `notepad`, `calc`, `explorer`, `mspaint`. The GUI can raise this to Tier 4
  via `apply_full_desktop_preset()` (`gui_app.py:1316`).
- The `unrestricted` branch (`poller.py:302-315`) additionally forces
  `allowed_apps={"*"}`, `allowed_services={"*"}`, `allowed_test_commands={"*"}` when
  either side says unrestricted — a server-sent `unrestricted:true` also widens the agent.

### 2.6 Token expiry / rotation / storage — VERIFIED

| Aspect | Linux | macOS | Windows CLI/GUI |
|---|---|---|---|
| Rotation trigger | `Poller.heartbeat()`, < 48 h to expiry (`poller.py:269-274`); manual `rotate-token` cmd (`linux/agent_main.py:746-775`) | Same `Poller` path | Same `Poller` path + GUI auto-rotate notice (`gui_app.py:1700+`) |
| Rotation call | `POST /api/agent/rotate` (`poller.py:241-246`) | Same | Same |
| Save | Atomic tmp+`replace`, `0600` (`poller.py:191-211`) | Same | DPAPI via `token_saver` hook (`agent_main.py:204-208`) |
| At-rest form | **Plaintext** `device.token` (+ `agent.env` containing `AI_BRIDGE_DEVICE_TOKEN=…` in clear, `linux/agent_main.py:322-360`) | Plaintext (`~/.ai-bridge/device.token`, Poller default) | DPAPI-sealed `aib-dpapi-v1:<base64>` (`app_runtime.py:118-160`); GUI JSON config strips the token (`strip_token_from_config`, `save_agent_config` in `gui_app.py:401-413`) |
| Failure mode | `rotate_token()` failures are swallowed in heartbeat (`except Exception: pass`, `poller.py:271-273`); manual command exits 1 | Same swallow | `_save_token` falls back to plaintext file if the DPAPI saver raises (`poller.py:191-200`) — silent downgrade, F07-05 |

- Server rotation (`control/app.py:4575-4585`): new token + new expiry, **old token invalid
  immediately**, audit `rotate_token`. No grace/overlap window: any concurrent in-flight
  request using the old token fails with 401. The poller's 401-retry-from-disk (§2.4)
  is the only mitigation, and it only helps if the new token reached disk first.
- **Linux `save_agent_config` duplicates the token into three places** (`agent.json`,
  `agent.env`, `device.token`) — `agent.env` is also read by systemd as an
  `EnvironmentFile`, so the bearer token lands in the service's process environment
  (visible via `/proc/<pid>/environ` to root and in `systemctl show` output in some
  configurations). See F07-06.
- **Windows DPAPI details** (`agents/windows/app_runtime.py:60-160`): `CryptProtectData`
  with `CRYPTPROTECT_UI_FORBIDDEN`, user scope (no `CRYPTPROTECT_LOCAL_MACHINE` unless
  requested — the C++ side defaults to user scope too, report 08). `unseal_token` accepts
  legacy plaintext silently. `migrate_token_file()` re-seals plaintext on first load
  (`gui_app.py:381-383`). `SingleInstanceGuard` (named mutex `AIBridgeWindowsClient` on
  Windows, `fcntl` lock file elsewhere, `app_runtime.py:226-300`) prevents two GUI
  instances fighting over rotation — but the CLI agent and the GUI can still run
  concurrently against the same token file (`NOT FOUND`: any cross-process coordination
  between `agent_main.py` and `gui_app.py` beyond the mutex, which the CLI does not take).

### 2.7 Polling / claiming — VERIFIED

- `Poller.run_forever()` (`poller.py:512-534`): `wait=5` s long-poll when the WS side-channel
  is connected, else `wait=15` s; on a task it loops immediately (`sleep(0.05)`), else
  `_wake.wait(0.5)`. Errors back off `2 → 4 → … → 60` s, resetting on success.
- Claim is server-side atomic: `GET /api/agent/tasks/next` selects the oldest `queued`
  task for the device and flips it to `running` with `WHERE status='queued'` inside a
  transaction (`control/app.py:4607-4690`, `rowcount == 1` check). `INFERRED`: safe against
  two agents sharing one device token claiming the same task; **not** safe against two
  agents with the *same token* both executing if the first crashes after claim but
  before result — that is handled by the lease sweep (§2.9), not by the claim.
- **Per-device serialisation:** `device_has_active_lease()` (`orchestration.py:16-24`)
  makes `tasks/next` return no task while a `running` task younger than the 900 s lease
  exists. `INFERRED`: an agent executes at most one task at a time per device; long
  tasks (> 900 s without a result) can be double-claimed after the sweep requeues them
  — and the original agent, if still running, will still post its result afterwards
  (last-write-wins on `tasks.result`, `control/app.py:4704+` has no execution-token /
  fencing check). See F07-04.
- **Cancel:** two mechanisms, both best-effort. (a) WS push `{"type":"cancel"}` sets
  `_ws_cancel_task_id` and the active cancel event (`poller.py:439-462`). (b) A per-task
  poller thread (`run_once`, `poller.py:395-410`) polls
  `GET /api/agent/tasks/{id}/execution-state` every 0.5 s and checks the shared
  `emergency_stop` flag. `dispatch()` receives the cancel predicate via
  `set_execution_context(task_id, _cancelled, …)` (`task_runtime.py`). `INFERRED`:
  cancellation is cooperative — a tool that never calls `raise_if_cancelled()` runs to
  completion. The cancel-poller thread is `join(timeout=2.0)` in `finally`, so a stuck
  HTTP call there can delay loop iteration by up to 2 s per task.
- **Approval:** `tasks/next` computes `approval_granted` only from a matching, unexpired
  `approvals` row for the same tool — except the special case
  `task id startswith "term-" and tool == "terminal_exec"` → `approval_granted=True`
  (`control/app.py:4670-4680`). `INFERRED`: interactive-terminal tasks are auto-approved
  by construction; worth confirming with the control-plane track.

### 2.8 Execution — VERIFIED (path only)

- `run_once()` (`poller.py:346-392`): parses `args_json`, builds the effective policy,
  calls `control.dispatch.dispatch(tool, args, policy=eff_policy,
  approval_granted=…)`. Results are JSON-serialised with `default=str` and truncated
  to `MAX_RESULT_CHARS = 512_000` (`poller.py:20, 377`). `PermissionError` →
  `failed: denied: …`; `TaskCancelled` → `cancelled`; anything else → `failed:
  <Type>: <msg>`. The exception text is sent to the plane verbatim — `INFERRED`:
  may leak local paths / internals into stored task results (low severity, audit
  track to confirm redaction).
- Execution happens **in-process, in the poll loop thread**. There is no worker pool,
  no per-task subprocess isolation, no per-task timeout in the agent itself
  (timeouts live in `task_runtime` / individual tools, if at all). A hung tool blocks
  heartbeats, claiming, and rotation for the whole agent (`INFERRED`, F07-07).
- `dispatch()` is the same code the control plane uses in-process for `device_id="local"`
  — the agent therefore executes with the full local tool surface, gated only by the
  merged policy above.

### 2.9 Result reporting, retry, offline behaviour — VERIFIED

- **Spool-then-send:** `run_once()` spools every result to SQLite *before* attempting
  transmission (`poller.py:388-392`); on success it deletes the spool row
  (`mark_flushed`). `spool.py:29-131`: `TaskSpooler` with `WAL` mode, `task_id` primary
  key, `INSERT … ON CONFLICT DO UPDATE` (re-execution overwrites the spooled result and
  bumps `attempts`). `flush()` (`spool.py:108-131`) delivers in `created_at` order,
  **stops at the first failure** (head-of-line blocking: one poison result blocks all
  older/newer spooled results behind it — `break`, not skip; F07-08). `get_pending`
  caps at 50 rows per flush call, so a large backlog drains 50 per loop iteration.
- **Flush points:** at the top of every `run_once()` and in the GUI `_poll_loop`
  (`gui_app.py:1690-1695`). If the spool DB itself is unwritable, `_init_db`/`spool`
  only log a warning and the result is **lost silently** (`spool.py:48-53, 88-94`).
- **Spool location:** `~/.ai-bridge/agent_spool.db` (or `/var/lib/ai-bridge/` as root,
  `C:\ProgramData\AIBridge\` on Windows, `spool.py:21-30`). No encryption, no size cap,
  no retention/TTL — `NOT FOUND`: any pruning of spooled rows other than successful flush.
  A result is up to 512 KB; an offline agent executing screenshot tasks could grow the
  DB without bound (`INFERRED`).
- **Offline execution:** the agent cannot *receive* tasks while offline (claim is a
  server round-trip), so the spool only protects results of tasks executed just before
  / during a connectivity drop — the "zero-data-loss" docstring (`spool.py:1-8`) is
  accurate for results, not for task intake. After a server restart, tasks stuck in
  `running` are reclaimed by the lease sweep (`orchestration.py:27-60`, default requeue
  on, `AI_BRIDGE_STALE_TASK_REQUEUE=1`) and re-executed — **not idempotent by default**:
  re-execution re-runs the tool. Whether that is safe depends on the tool (track 05).
- **No result deduplication on the server:** `POST …/result` unconditionally overwrites
  `tasks.status/result` (`control/app.py:4704+`); a retried post after a dropped response
  is harmless (same payload) but a *re-executed* task overwrites the first result.

### 2.10 Shutdown — VERIFIED (partial)

- **Linux:** `SIGINT`/`SIGTERM` → `sys.exit(0)` from the signal handler
  (`linux/agent_main.py:953-958`). `INFERRED`: if a task is mid-execution, the process
  exits immediately — no graceful drain, no cancel propagation, no result spooling of
  the in-flight task; the server lease sweep later requeues it. `SystemExit` raised
  inside `dispatch()` would propagate the same way. `NOT FOUND`: any `atexit` handler
  or shutdown hook in any agent entrypoint.
- **macOS:** no signal handling at all — default `KeyboardInterrupt` traceback on
  SIGINT; SIGTERM kills the process (`INFERRED`).
- **Windows CLI:** no signal handling either (`agent_main.py` has none); Ctrl-C raises
  `KeyboardInterrupt` inside `run_forever()`'s loop, which is **not caught there**
  (`poller.py:512-534` catches `Exception`, not `KeyboardInterrupt`) — propagates and
  exits. Same abrupt-exit semantics as Linux.
- **Windows GUI:** `stop_agent()` (`gui_app.py:1655-1674`) only sets `stop_event` and
  flips UI state; it does **not** join the worker thread and does **not** stop the WS
  side-channel (`stop_side_channel()` exists, `poller.py:505-508`, but `NOT FOUND`:
  any call to it from the GUI). The daemon worker thread keeps running until the
  current `run_once()`/long-poll returns (up to `wait_s + 6` s timeout, plus task
  duration). `INFERRED`: "Stop" in the GUI is advisory; a long task continues after
  the user pressed Stop. The WS thread is daemonised, so process exit still terminates it.
- **C++ service/desktop processes:** report 08 — the Python agent has no supervision of
  them and they have no supervision of it (`NOT FOUND`: any cross-process watch between
  `agents/windows/*` and `windows-agent/*` binaries).
- **systemd:** Linux `manage_service("install")` (`linux/agent_main.py:669-720`) writes
  a unit with `Restart=always, RestartSec=5, KillMode=process`, `LimitNOFILE=65536`,
  running as root when installed as root. `INFERRED`: crash-looping restarts every 5 s
  with no backoff ceiling. The unit's `ExecStart` points at `agents/linux/agent_main.py
  run` from the repo checkout (`.venv` python if present) — the agent therefore
  restarts with whatever code is currently on disk, including uncommitted edits.
- **macOS launchd:** `deployment/macos/com.aibridge.agent.plist` — `RunAtLoad` +
  `KeepAlive`, but the plist still contains literal `REPO_PATH` / `CONTROL_URL` /
  `DEVICE_TOKEN` placeholders and writes logs to `/tmp/ai-bridge-agent.out|err`.
  `VERIFIED` as un-templated; `INFERRED`: not installable as shipped without editing,
  and the device token in a launchd plist is world-readable by default (`NOT FOUND`:
  any chmod/ACL step for the plist in `deployment/macos/README.md` — not verified here).

## 3. Platform comparison

| Capability | Linux (`agent_main.py`, 1,055 LOC) | macOS (94 LOC) | Windows CLI (212 LOC) | Windows GUI (2,214 + 1,659 LOC) |
|---|---|---|---|---|
| Poll loop | `Poller.run_forever()` | `Poller.run_forever()` | `Poller.run_forever()` | Own `_poll_loop()` duplicate |
| Enroll | Yes, built-in | No | No (config search) | Via GUI fields + `save_agent_config` |
| Doctor / status | Yes (`doctor`, `status`) | No | `status`, `system-info`, `event-logs` | Diagnose tab + `diagnose.py` |
| systemd/launchd/scheduled-task | systemd install/uninstall/start/stop/logs | launchd plist exists, un-templated | `deployment/windows/install-agent.ps1` (scheduled task, per agent docstring) | HKCU Run autostart toggle (`gui_app.py:123-170`) |
| Token at rest | Plaintext ×3 files | Plaintext | DPAPI | DPAPI |
| Policy default | **Unrestricted Tier 4** | `default_policy()` | GUI policy (Tier 3 default) | Tier 3 default, presets to Tier 4 / lockdown |
| Update | `update` cmd: `git pull --ff-only` + `pip install -e .` + service restart | `--check-update` / `--apply-update` (signed manifest) | Same signed-manifest flags | `NOT FOUND` in GUI (update is CLI-only) |
| Desktop automation | Via `dispatch` tools (server context) | Via `dispatch` tools | `desktop_helper.py` (logged-in session only) | Vision tab test screenshot, `tools.capture_desktop_screenshot(approval_granted=True, …)` (`gui_app.py:1029`) |
| Verified on hardware | Docstring claims nothing for Linux | Docstring: **"NOT yet run on macOS … needs a Mac to verify"** (`agent_main.py:1-12`) | Docstring: **verified 2026-10-25, Windows Server 2025** (`agent_main.py:1-17`) | `NOT FOUND`: an equivalent verification claim in `gui_app.py` |
| Shutdown | Signal handler → `sys.exit(0)` | None | None (KeyboardInterrupt) | Advisory `stop_event`, no join |

## 4. `diagnose.py` and `desktop_helper.py` — VERIFIED (brief)

- `agents/windows/diagnose.py` (201 LOC): `probe_control_plane()` (HTTP health probe),
  config/token/connectivity sections, `build_report()` — a read-only diagnostic report;
  it masks secrets via `runtime.mask_secret` in the paths checked (`grep` verified
  `mask_secret` use in `gui_app.py`; `diagnose.py` itself returns token *presence*,
  not the value — `INFERRED` from `_token_section`, lines 114-138).
- `agents/windows/desktop_helper.py` (79 LOC): pure pass-through to `ai_bridge.tools`
  (`status/screenshot/frame/launch/ocr/click_text/click_element/list_elements/
  set_of_marks/wait_for_text/verify_screen/hotkey/workflow`). Its module docstring is
  the session rule: must run as the logged-in user, never as SYSTEM, gated by
  `AI_BRIDGE_DESKTOP_ENABLED=1` + approved-app list. No input validation of its own —
  it inherits whatever `tools.*` enforces.

## 5. Terminal sessions (context for report 13)

`src/ai_bridge/terminal_manager.py` (637 LOC) is **not** part of the poller, but tasks
of tool `terminal_exec` execute through it: local PTY on Linux/macOS, ConPTY/piped
PowerShell on Windows (`_start_windows_conpty`, line 37), or a remote relay when
`device_id != "local"`. Sessions live in the in-process `_TERMINAL_SESSIONS` dict
(`terminal_manager.py:581-637`), capped at a 1,000-entry output buffer per session,
with no persistence — a control-plane restart drops every live terminal session and
its scrollback (`VERIFIED` by the module-global dict; `NOT FOUND`: any DB backing).
`NOT FOUND` in this track: a per-session idle timeout or reaper (verify in track 02).

## 6. Tests covering this track — VERIFIED (counts only)

- `grep -rln "poller|Poller|webrtc_stream|AgentStreamer" tests/` returns only
  `tests/test_windows_v1_architecture.py`, `tests/test_windows_v2_architecture.py`,
  `tests/unit/test_gui_app.py`, `tests/unit/test_phase4_streaming_and_updater.py`,
  plus desktop-autonomy/orchestration unit files. **No dedicated `test_poller.py`,
  `test_spool.py`, or `test_agent_main*.py` exists** (`ls tests/` + `ls tests/unit/`).
- `tests/unit/test_phase4_streaming_and_updater.py` (9 `def test`) covers WebRTC
  session endpoints at the HTTP level (`test_webrtc_session_endpoints`,
  `test_v1_webrtc_session_endpoints`, platform gate test) — signalling only, no live
  media, no `Poller` execution.
- `tests/unit/test_gui_app.py` (7 `def test`) covers GUI config/policy helpers.
- `NOT FOUND`: tests for `_effective_policy` widening, spool flush ordering/failure,
  401-retry, rotation race, cancel polling, backoff, or the GUI `_poll_loop` duplicate.
  Test *results* were not executed in this track (read-only audit; execution belongs
  to track 10).

## 7. Findings

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| F07-01 | **P0** | **Local policy can only widen, never narrow, server policy.** `_effective_policy()` ORs/max-es/unions every field when an explicit local policy exists, so a plane policy change cannot restrict a Linux-unrestricted or GUI-configured agent. The server-side `reject_policy_widening` guard does not apply to this merge. | `agents/common/poller.py:277-344`; `agents/linux/agent_main.py:108-121,945-951` |
| F07-02 | **P0** | **Linux agent is unrestricted Tier 4 by default and re-pushes that policy on every start.** Destructive/shell/package/service actions are pre-authorised; approval flow is bypassed for this platform by design. | `agents/linux/agent_main.py:108-121,131-165,945-951` |
| F07-03 | **P1** | **Two poll-loop implementations.** GUI `_poll_loop()` duplicates `Poller.run_once()` (heartbeat, claim, cancel, dispatch, spool flush) inline; fixes to one will not reach the other. GUI "Stop" does not join the worker nor stop the WS channel. | `agents/windows/gui_app.py:1678-~1900`; `agents/common/poller.py:346-534,505-508` |
| F07-04 | **P1** | **No execution fencing.** After the 900 s lease sweep requeues a stuck task, the original agent can still post its (late) result and overwrite the re-execution's result; last-write-wins, no execution token. | `src/ai_bridge/control/orchestration.py:16-60`; `src/ai_bridge/control/app.py:4607-4690,4704+` |
| F07-05 | **P1** | **Silent token-storage downgrade.** If the DPAPI saver raises, `Poller._save_token` falls back to a plaintext file without logging. A DPAPI failure (e.g. profile unload) silently leaves a bearer token in cleartext. | `agents/common/poller.py:191-200` |
| F07-06 | **P1** | **Linux token in three plaintext places, one in the process environment.** `save_agent_config` writes `agent.json` + `agent.env` (`AI_BRIDGE_DEVICE_TOKEN=…`) + `device.token`; `agent.env` is a systemd `EnvironmentFile`. | `agents/linux/agent_main.py:322-360,669-720` |
| F07-07 | **P1** | **Single-threaded execution blocks heartbeats.** `dispatch()` runs in the poll thread; a hung tool stalls heartbeat/claim/rotation/cancel for the whole agent. No per-task timeout or worker pool in the agent. | `agents/common/poller.py:346-392,512-534` |
| F07-08 | **P2** | **Spool head-of-line blocking + unbounded growth.** `flush()` stops at the first failed result; no skip/DLQ; no size cap, TTL, or pruning for `agent_spool.db`; spool-write failure drops the result silently. | `agents/common/spool.py:48-53,88-94,108-131` |
| F07-09 | **P2** | **No graceful shutdown anywhere.** Signal handlers `sys.exit(0)` mid-task; GUI Stop is advisory; no drain, no in-flight result flush, no `atexit`. In-flight work is recovered only via the 900 s lease sweep (re-execution). | `agents/linux/agent_main.py:953-958`; `agents/windows/gui_app.py:1655-1674` |
| F07-10 | **P2** | **Heartbeat is coupled to task execution.** No independent heartbeat timer; `last_seen` goes stale for the duration of any long task, distorting fleet-health "online" state. | `agents/common/poller.py:248-275,512-534` |
| F07-11 | **P2** | **macOS agent is unverified and incomplete.** 94 LOC, no enroll/doctor/status/service, no signal handling, docstring admits it "needs a Mac to verify"; launchd plist ships with un-replaced placeholders and a plaintext token. | `agents/macos/agent_main.py:1-94`; `deployment/macos/com.aibridge.agent.plist` |
| F07-12 | **P2** | **401 retry can mask revocation.** Heartbeat retries once with the on-disk token on any 401; combined with silent rotation, an operator cannot distinguish "rotated elsewhere" from "revoked" from agent logs (both surface as backoff retries). | `agents/common/poller.py:258-268` |
| F07-13 | **P3** | **Hardcoded tenant + hardcoded default control URL on Linux.** Enrollment always uses `tenant="default"`; `DEFAULT_CONTROL_URL = "https://jxiptvplayer.app"` is a product-unrelated host that operators must override. | `agents/linux/agent_main.py:55,777-824` |
| F07-14 | **P3** | **CLI and GUI can run concurrently on the same token file.** `SingleInstanceGuard` is GUI-only; the CLI never takes it, so two agents can race rotation (mitigated only by the 401-retry). | `agents/windows/app_runtime.py:226-300`; `agents/windows/agent_main.py:190-212` |
| F07-15 | **P3** | **No agent tests for the core loop.** No `test_poller.py` / `test_spool.py`; `_effective_policy`, spool ordering, rotation, cancel, and backoff are untested. | §6; `ls tests/ tests/unit/` |

## 8. Open questions for other tracks

1. **(Track 02)** Does `dispatch()`/individual tools enforce a per-task timeout, or can a
   hung tool block the agent indefinitely (F07-07)? Confirm `task_runtime` cancellation
   coverage per tool.
2. **(Track 02/04)** Is the `term-*` + `terminal_exec` auto-approval (`control/app.py:4670-4680`)
   intentional, and is it audited as an approval?
3. **(Track 07/DB)** Is the lease-sweep requeue (`orchestration.py:27-60`) safe for
   non-idempotent tools, and should result posts carry an execution token (F07-04)?
4. **(Track 04)** Confirm whether the plane's stored policy is *ever* authoritative
   over a local explicit policy, or whether F07-01 is accepted design (Linux servers
   are intentionally autonomous).

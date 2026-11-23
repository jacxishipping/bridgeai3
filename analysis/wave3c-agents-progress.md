> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 3C Track A — Agent reliability — Progress

Date: 2026-11-17. Repo: `/home/hatch/workspace/ai-bridge-main`, git commit
`19747b2`. No commit made. No packages installed. No files touched outside
`agents/**` and `tests/unit/test_agent_reliability.py` (new). In particular
`src/ai_bridge/control/*`, `server.py`, `tools.py` were read-only.

Other modified files visible in `git status` at the time of this work
(`src/ai_bridge/*`, `windows-agent/*`, `migrations/014_*`,
`tests/unit/test_webrtc_phase3.py`, …) are sibling waves' concurrent work,
not this track's. This track's files only:

- `agents/common/spool.py` — F07-08
- `agents/common/poller.py` — F07-07/F07-09/F07-10/F07-12, DIST-08
- `agents/linux/agent_main.py` — F07-09
- `agents/macos/agent_main.py` — F07-09
- `agents/windows/agent_main.py` — F07-09
- `tests/unit/test_agent_reliability.py` — NEW (23 tests)
- `analysis/wave3c-agents-progress.md` — this file

## Verification method

Every finding was re-verified against the actual code before editing
(file:line citations in each section below). Python changes were verified
by `.venv/bin/python -m pytest` (venv already provisioned; nothing
installed). No agent was run against a live control plane; behaviour is
verified by unit tests with a fake plane, not by an end-to-end run.

## 0. Pre-fix verification (all findings confirmed)

| Finding | Verified state at 19747b2 |
|---|---|
| F07-08 | `agents/common/spool.py` (131 LOC): no cap, no TTL, no DLQ; `flush()` `break`s at the first failure (head-of-line blocking); spool-write failure only logs. Confirmed. |
| F07-09 | Linux `agent_main.py:953-958`: signal handler `sys.exit(0)` mid-task. macOS: no signal handling. Windows CLI: none (KeyboardInterrupt propagates out of `run_forever()`). GUI Stop advisory (F07-03, out of scope). Confirmed. |
| F07-07 (roadmap dep. of F07-10) | **NOT implemented**, despite the roadmap row. `poller.py run_once()` calls `dispatch()` inline in the poll thread; no worker thread/pool anywhere in `agents/`. Wave 1B did not implement it (its progress file covers R-P0-07/08/09/13, F13-01/02, F07-05/06 only). This track implements the worker-thread model as part of F07-10. |
| F07-10 | No independent heartbeat timer: `heartbeat()` is called once per `run_once()`, so `last_seen` goes stale for a whole task duration. Confirmed. |
| F07-12 | `poller.py heartbeat()`: any 401 retries once with the on-disk token, then raises; `run_forever()` backs off forever. No revocation distinction. Confirmed. |
| DIST-08 | No dedicated progress endpoint exists on the plane (see §DIST-08). Server side already has `record_task_progress()` (`control/orchestration.py:27`) and calls it from the `execution-state` GET handler (`control/app.py`, `agent_task_execution_state`) — Wave 2 work. The agent's 0.5 s cancel-poller therefore already bumps the task row implicitly, but that was incidental and unthrottled. |

## 1. F07-08 — spool DLQ + cap + TTL — DONE

Files: `agents/common/spool.py` (rewritten, same public API).

- **Cap:** `TaskSpooler(..., max_items=500, max_total_bytes=64 MiB)` (defaults).
  `spool()` prunes expired items first, then evicts oldest-first to the DLQ
  until the new result fits. A re-spool of an already-pending `task_id`
  replaces in place and consumes no extra slot.
- **TTL:** `ttl_seconds=7 days` (default), per-item (`created_at + ttl < now`).
  Expired items are moved to the DLQ by `prune_expired()`, `spool()`, and
  `get_pending()` — never delivered stale, never deleted silently.
- **DLQ:** new table `dead_letter_results` in the same SQLite file, same
  payload columns plus `dead_at` and `reason`
  (`spool_capacity_exceeded` | `ttl_expired` | `max_attempts_exceeded`).
  New methods: `move_to_dlq()`, `get_dead_letter()`, `dead_letter_count()`,
  `pending_count()`, `prune_expired()`. Nothing in the DLQ is ever deleted
  by the spooler itself.
- **Retries:** `max_attempts=10` (default). `flush()` no longer stops at the
  first failure: a failed item's attempt count is bumped and the flush
  continues with the next item (head-of-line blocking removed). At
  `max_attempts` the item is dead-lettered instead of retried forever.
- **Behaviour change (deliberate, documented in the module docstring):**
  re-spooling the same `task_id` (task re-executed) now replaces the stored
  result and resets attempts/TTL to a fresh delivery budget. The old code
  bumped `attempts` on re-spool and kept the original `created_at`, so a
  re-executed task could be dead-lettered on its first delivery failure
  and could expire under the old clock.
- **Unwritable spool DB:** still no place to record a DLQ entry, so the
  failure is logged at `error` level (unchanged, now stated explicitly) —
  the result is not silently dropped without a trace.
- Existing tests `tests/unit/test_offline_spool.py` (2 tests) pass unchanged.

Tests: `test_agent_reliability.py` — cap-by-count, cap-by-bytes, TTL expiry,
`prune_expired` count, max-attempts DLQ, no-head-of-line-blocking,
re-spool resets attempts/TTL. All pass.

Rollback: revert `spool.py`; old DBs are forward-compatible (the DLQ table
is additive; `spooled_results` schema unchanged).

## 2. F07-09 — graceful shutdown / drain — DONE

Files: `agents/common/poller.py`, `agents/linux/agent_main.py`,
`agents/macos/agent_main.py`, `agents/windows/agent_main.py`.

- `Poller.request_shutdown()` — only sets events (safe from a signal
  handler); idempotent. `run_forever()` stops claiming new tasks once it is
  set and exits after the in-flight task finishes.
- `Poller.drain_timeout` (default 60 s — inside systemd's default 90 s stop
  timeout, so the unit is not SIGKILLed mid-drain). The worker join loop
  notices shutdown within 0.5 s and waits until the deadline.
- Normal drain: the task finishes, its result is spooled + posted exactly as
  in the non-shutdown path, then the loop exits.
- **Drain timeout exceeded:** the worker is a daemon thread and dies with
  the process. Deliberately, NO result is spooled or posted for that task:
  posting `failed` would mark a task failed that may still be executing its
  side effects, and would prevent re-execution. Instead the task stays
  `running` on the plane and the stale-lease sweep re-queues it (the
  designed recovery), and the abandonment is printed loudly to stderr.
  `run_once()` returns `{"status": "drain_timeout", "drained": False}`;
  `run_forever()` returns a summary dict
  (`revoked` / `revoked_reason` / `drained` / `shutdown_requested`).
- Backoff sleep is sliced (0.2 s) so a shutdown request during backoff
  takes effect promptly.
- Entrypoints: Linux signal handler now calls `request_shutdown()` instead
  of `sys.exit(0)`; macOS gains SIGINT/SIGTERM handlers (it had none);
  Windows CLI gains SIGINT/SIGTERM handlers (guarded `try/except`, since
  signal registration can fail off the main thread) plus a
  KeyboardInterrupt backstop. All three exit `2` on revocation (distinct
  from clean `0`) and print the re-enroll prompt. `--once` paths unchanged.
- Out of scope (noted, not changed): the Windows GUI's own `_poll_loop()`
  (F07-03 — separate track) and the systemd unit (`Restart=always` will
  still restart a revoked agent; the unit is owned by `manage_service()` —
  follow-up, see §7).

Tests: drain finishes in-flight task (no second claim, result posted);
drain timeout exits promptly with no result posted and nothing spooled;
`request_shutdown` idempotent/visible. All pass.

Rollback: revert the four files; agents return to abrupt exit (lease sweep
still recovers in-flight tasks).

## 3. F07-10 — independent heartbeat timer (+ F07-07 worker thread) — DONE

Files: `agents/common/poller.py`.

- **F07-07 implemented here** (it was not in the code — see §0):
  `run_once()` now runs `dispatch()` on a daemon worker thread named
  `ai-bridge-task-{id}`. The poll thread only joins it (0.5 s slices, so
  shutdown/Ctrl-C is noticed promptly). The cancel-poller thread is
  unchanged and keeps running during execution.
- **Heartbeat timer:** `Poller.start_heartbeat_loop()` /
  `stop_heartbeat_loop()` run `heartbeat()` every `heartbeat_interval`
  (default 30 s) on a daemon thread, plus a throttled progress report while
  a task runs. The loop is started by `run_forever()`; `run_once()` skips
  its inline heartbeat while the loop is running (avoiding a duplicate
  heartbeat + rotation race) but still heartbeats inline for standalone
  callers (`--once`, GUI poll loop). `heartbeat()` is serialised by a lock
  so the timer and an inline call can never run concurrently.
- Ctrl-C during a task is converted to a shutdown request (drain), not an
  instant kill — the `KeyboardInterrupt` is caught in the worker join loop.

Tests: heartbeat fires while a task is hung (≥3 heartbeats + ≥1 progress
report during a 2 s hung dispatch; task then completes and its result is
posted). Passes.

Rollback: revert `poller.py` (restores single-threaded execution).

## 4. F07-12 — 401 revocation vs transient — DONE

Files: `agents/common/poller.py`.

- New exception hierarchy (all `RuntimeError` subclasses, so existing
  `except RuntimeError` / `except Exception` callers are unaffected):
  `ControlPlaneHTTPError` (`.status_code`, `.path`, `.body`),
  `DeviceRevokedError`, `DeviceTokenExpiredError`. `_req()` raises them
  instead of a bare `RuntimeError` string; it surfaces the plane's JSON
  `detail` when present.
- `classify_auth_failure(body)` → `"revoked"` / `"expired"` /
  `"transient"`, grounded in the plane's actual `device_row` details
  (`control/app.py:992-1001`): `"Unknown or revoked device token."` →
  revoked; `"Device token expired; ... or re-enroll."` → expired; anything
  else → transient.
- `heartbeat()` order of operations (this is the distinction):
  1. 401 → retry once with the on-disk token if it differs (rotation-race
     recovery — preserved from the old behaviour).
  2. If the retry also 401s, or there is no differing saved token, classify:
     revoked/expired → `_mark_terminal_auth()` (sets `revoked=True`,
     prints a prominent re-enroll prompt to stderr) and raise the terminal
     error; transient → raise `ControlPlaneHTTPError` for the caller's
     backoff.
  3. `run_forever()` exits on terminal auth (no infinite retry) and on a
     revoked `run_once()` result; `run_once()` returns
     `{"task": None, "revoked": True, "revoked_reason": ...}` instead of
     claiming.
- **Documented ambiguity:** the plane's `"Unknown or revoked"` detail
  conflates a token the plane never knew (e.g. stale after a rotation
  elsewhere) with a genuinely revoked device. The agent only treats it as
  revocation after the saved-token retry is exhausted; a rotation race is
  recovered first. A plane-side follow-up could split the detail into
  distinct `unknown` vs `revoked` strings — the agent already handles any
  body containing `revok`.
- **Backoff with jitter:** `backoff_delay(attempt, base=2.0, cap=60.0)` =
  `min(cap, base·2**attempt)` × uniform jitter in [0.5, 1.5], clamped to
  the cap. `run_forever()` uses it (attempt counter resets on success);
  `self.backoff` is kept updated for existing readers.
- Expired tokens are terminal for the same reason as revoked ones:
  rotation requires a valid token, so an expired token cannot be rotated
  back to health — re-enrollment is the only path.

Tests: classification table; `_req` structured errors (401 revoked /
401 expired / 500); heartbeat revoked flags + prints re-enroll prompt;
heartbeat expired terminal; transient 401 not flagged; rotation-race
recovery via saved token; `run_forever` stops on revocation in < 5 s with
zero claims; `backoff_delay` bounds/growth/cap. All pass.

Rollback: revert `poller.py`.

## 5. DIST-08 — task progress on the heartbeat — DONE (agent side)

Files: `agents/common/poller.py`; contract documented in this section.

### 5.1 What the plane already exposes (found by grep, not assumed)

- **No dedicated progress endpoint exists.** `grep progress` over
  `src/ai_bridge/control/` finds only `orchestration.record_task_progress`
  and its single caller.
- **What exists (Wave 2):** `record_task_progress(conn, task_id)`
  (`control/orchestration.py:27-60`) bumps `tasks.updated_at` and appends a
  `progress` task event iff the task is still `status='running'`, returning
  False when the task is gone. The `execution-state` GET handler
  (`control/app.py`, `agent_task_execution_state`) calls it whenever the
  task is `running`. The sweep (`sweep_stale_running_tasks`,
  `orchestration.py:62+`) re-queues `running` tasks whose `updated_at` is
  older than the lease (default 900 s) — so it already uses last-progress
  time, not claim time, for any task whose agent polls execution-state.
- Consequence: the agent's existing cancel-poller (GET `execution-state`
  every 0.5 s during a task) already keeps a long task alive. That was
  incidental and unthrottled (one `progress` task event per 0.5 s poll —
  see server-side follow-up in §7).

### 5.2 Agent-side implementation (this track)

- `Poller.report_progress(task_id, progress_pct=None, status_message=None)`
  POSTs to the contract endpoint below. It is driven by the heartbeat
  timer thread (`_maybe_report_progress`), throttled to at most one report
  per `progress_interval` (default 60 s — matching the server-side guidance
  in `record_task_progress`'s docstring that progress reports be throttled
  to bound `task_events` growth). A failed transient report does not reset
  the timer, so it is retried on the next heartbeat tick.
- `progress_pct=None` is the honest value for most tools (the agent cannot
  measure a percentage); the plane must treat null as "still alive, no %".
  `status_message` is `running: <tool> (elapsed <s>s)`.
- **404/501 tolerance:** the first 404/501 logs once, sets
  `_progress_endpoint_missing`, and disables further progress POSTs for the
  rest of the run. The agent keeps running — long tasks remain protected by
  the execution-state poll (§5.1). Other failures (5xx, network) return
  False without disabling.
- The claim epoch (when the plane returns one in `tasks/next`) is attached
  as `epoch` so the plane can apply the DIST-02/F07-04 fencing check.

### 5.3 Wire contract for the server side (Wave 3A/4 follow-up)

The plane does NOT implement this yet. When it does, it must implement
exactly this contract (the agent already speaks it):

| Item | Contract |
|---|---|
| Method / path | `POST /api/tasks/{task_id}/progress` |
| Auth | Same device-token auth as the other `/api/agent/*` routes. **Namespace note:** the task brief prescribes `/api/tasks/{task_id}/progress`, but every agent-authenticated route today lives under `/api/agent/*` (`/api/tasks/*` is operator/session auth). The server follow-up should either expose the prescribed path with device-token auth or add an agent-namespaced alias `POST /api/agent/tasks/{task_id}/progress` — the agent should be updated to the alias if the plane chooses it, since a device token will not authenticate on the operator namespace. |
| Request JSON | `{"task_id": "<id>", "progress_pct": <number 0–100 \| null>, "status_message": "<human-readable string \| null>", "epoch": <int, optional>}`. `task_id` in the body must match the path id. `progress_pct: null` means "alive, no measurable %". |
| Semantics | Record liveness only. Must bump the task's `updated_at` (equivalently `lease_until`) **iff** `status='running'` (reuse `record_task_progress`); must NOT change `status`. If `epoch` is present and does not match the task's current epoch, reject (409) — the task was re-queued and this agent is stale. If the task is no longer `running`, return 409 (or 200 with `{"alive": false}`) so the agent knows its task is gone and should stop work. |
| Responses | `200 {"ok": true, "task_id": ..., "status": "running"}` on success; `404` unknown task / wrong device (agent treats as endpoint-missing only for the progress path — see note); `409` task not running / epoch mismatch; `401` bad token. **Important:** the agent treats 404/501 on this path as "endpoint not implemented" and disables progress for the run. A 404 for a genuinely unknown `task_id` is therefore indistinguishable from a missing endpoint — the plane should prefer `409` for unknown/not-running tasks on this route, reserving 404 for a truly absent route. |
| Sweep behaviour (required) | `sweep_stale_running_tasks` must keep using `updated_at` (last progress time), **not** claim time, as the staleness cutoff — it already does. With progress reports at ≤ 60 s intervals, a task is re-queued only after a full lease (default 900 s) of agent silence. Progress reports must be throttled server-side too if the agent ever reports faster than ~60 s. |
| Volume | One progress event per report is acceptable at a 60 s cadence. Do NOT append a `task_events` row per execution-state poll (current behaviour appends one per 0.5 s poll — see §7). |

Tests: payload shape (path, `task_id`, null `progress_pct`,
`status_message`, epoch attached only for the current task); 404 tolerated
+ logged once + no second request; 501 tolerated; transient 500 does not
disable the endpoint (retried). Plus the heartbeat-independence test
asserts ≥1 progress report fires while a task is hung. All pass.

Rollback: revert `poller.py`; long tasks fall back to the incidental
execution-state progress (§5.1) only.

## 6. Test results

Command (from repo root, venv already provisioned, nothing installed):

`.venv/bin/python -m pytest tests/unit/test_agent_reliability.py -q -p no:cacheprovider`

**23 passed in 8.94 s** (0 failed). The full suite was NOT run, per the
track brief (parent runs it after all tracks).

Targeted neighbour regression check (directly-affected existing tests
only, not the full suite):

`.venv/bin/python -m pytest tests/unit/test_agent_linux.py tests/unit/test_gui_app.py tests/unit/test_offline_spool.py tests/unit/test_agent_policy_merge.py tests/unit/test_feature_batch.py::test_poller_auto_rotates_expiring_token -q -p no:cacheprovider`

**28 passed, 2 skipped.** (`test_feature_batch` run as a single test — it
exercises `run_once()` + auto-rotation, the exact path changed here.)

`py_compile` passes for `agents/common/poller.py`,
`agents/common/spool.py`, `agents/linux/agent_main.py`,
`agents/macos/agent_main.py`, `agents/windows/agent_main.py`.

No existing test was deleted or disabled. One test failure was found and
fixed during the run — a bug in the new test's own fake plane (wrong path
segment index when parsing the result URL), not in agent code.

## 7. Follow-ups / blockers for other waves

- **B-1 (server, Wave 3A/4):** implement `POST /api/tasks/{task_id}/progress`
  per the §5.3 contract (or the `/api/agent/...` alias — decide the
  namespace; the agent currently posts the brief-prescribed path).
  Until then the agent logs once per run that the endpoint is unavailable
  and relies on the execution-state poll.
- **B-2 (server, Wave 3A/4):** `record_task_progress` is called on every
  execution-state GET, i.e. every 0.5 s per running task, appending a
  `task_events` row each time. Throttle it server-side (e.g. only record
  when > 60 s since the last progress event for the task) or the
  `task_events` table floods during long tasks.
- **B-3 (server, optional):** split the `device_row` 401 detail
  `"Unknown or revoked device token."` into distinct `unknown` vs
  `revoked` signals so the agent can distinguish them without the
  saved-token retry heuristic.
- **B-4 (agent service, follow-up):** the Linux systemd unit
  (`manage_service("install")`, `Restart=always`) will restart-loop a
  revoked agent even though the agent now exits 2 on revocation. The unit
  (or a wrapper) should treat exit 2 as "needs re-enrollment, do not
  restart". Same consideration for the Windows scheduled task / macOS
  launchd plist.
- **B-5 (not in this track):** the Windows GUI's own `_poll_loop()`
  (F07-03) still duplicates `Poller.run_once()` inline and therefore does
  NOT get the worker thread, heartbeat timer, drain, or 401 handling in
  this track — it does get the new spool behaviour (shared `TaskSpooler`)
  and the classified `heartbeat()`/`_req()` errors (both `RuntimeError`
  subclasses, caught by its existing `except Exception`). F07-03 should
  rebase the GUI loop onto `Poller` to inherit all of this.
- **B-6 (not run here):** no end-to-end run against a live control plane
  was performed; the progress contract (§5.3) and the drain behaviour
  under systemd should be verified live before they count as proven.

## 8. Files changed (this track only)

- `agents/common/spool.py` — F07-08 (DLQ + cap + TTL + max attempts + no head-of-line blocking)
- `agents/common/poller.py` — F07-07 (worker thread), F07-09 (drain), F07-10 (heartbeat timer), F07-12 (401 classification + jittered backoff), DIST-08 (progress reporting)
- `agents/linux/agent_main.py` — F07-09 (signal → drain, exit 2 on revocation)
- `agents/macos/agent_main.py` — F07-09 (signal handlers added, exit 2 on revocation)
- `agents/windows/agent_main.py` — F07-09 (signal handlers added, exit 2 on revocation)
- `tests/unit/test_agent_reliability.py` — NEW (23 tests)
- `analysis/wave3c-agents-progress.md` — this file

Unchanged by design: `agents/windows/gui_app.py` (F07-03), everything
under `src/` (read-only for this track).

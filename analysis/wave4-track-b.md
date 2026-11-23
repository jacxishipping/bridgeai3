> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 4 Track B — periphery wiring: progress

Base commit: `88ad591`. No reset/rebase/commit. No packages installed.
Files owned by this track only; sibling-track files in the shared tree
(`control/app.py`, `control/dispatch.py`, `control/oidc.py`,
`control/routers/*`, `analysis/wave4-track-a.md`) were not touched.

## 0. Tasks covered

| # | Task | Status |
|---|---|---|
| 2 | F-05-18 `to_pg_sql` regex translation → safe allowlist | DONE (replace, not remove — decision below) |
| 4 | Dashboard stops sending `simulate_agent_intent`; webrtc limiter import → `control/limiter.py` | DONE |
| 6 | `docs/operations.md:16` rate limits; systemd must not restart-loop a revoked agent | DONE (with a correction — see §6) |
| 7 | Windows GUI `_poll_loop()` inherits Poller behaviour | DONE in place; full rebase NOT feasible (decision below) |
| 8 | `TaskRunner._kill` + tools shell kill paths adopt SIGKILL escalation | DONE |

## 2. F-05-18 — `to_pg_sql` (REPLACE, not remove)

### Decision: replace with a tokenizer-based allowlisted translation

Removal was investigated and rejected. Caller audit (grep over the whole
tree):

- `src/ai_bridge/control/db.py:572` — `PostgresConnection.execute`
  (`pg_sql = to_pg_sql(sql)`). This is the **query path**: every Postgres
  query in the codebase flows through it, and the call sites write
  SQLite-dialect SQL with `?` placeholders (hundreds of sites).
- `src/ai_bridge/control/db.py:600` — `PostgresConnection.executescript`
  (`cur.execute(to_pg_sql(stmt))`). **No in-tree callers** (grep for
  `.executescript(` finds only `LockedConnection.executescript` at
  `db.py:340` and the SQLite `SCHEMA` bootstrap at `db.py:461`); kept
  working through the same translation. Note migrations do NOT use it:
  `migrations._apply_file` → `_adapt_and_execute_statement`
  (`migrations.py:305`) → `conn.execute(stmt)` per statement, so
  migration statements reach `to_pg_sql` via `execute` (`db.py:572`).
- `tests/unit/test_dual_engine_migrations.py` — the only test caller.

psycopg3 accepts only `%s` placeholders, so dropping the translator would
mean rewriting every `?` call site. Replacement is the proportionate fix.

### What SQL actually flows through it

- Query path: parameterised `SELECT`/`INSERT`/`UPDATE`/`DELETE` with `?`
  placeholders, string literals containing `%` (LIKE patterns such as
  `'agent-role-%'`, `'plane.human_takeover.%'`), `?` inside string literals.
- Migration path: DDL/DML with no `?` placeholders. Exactly **one**
  upsert in all of `migrations/*.sql`:
  `migrations/011_orchestration_plane_state.sql:2`
  (`INSERT OR IGNORE INTO settings (key, value) VALUES
  ('plane.emergency_stop', '0');`). **Zero** `INSERT OR REPLACE`
  occurrences anywhere in `src/` or `migrations/`. No Postgres JSON `?|` /
  `?&` operators anywhere in the control code (checked — so no carve-out
  for those; a bare `?` in code is always a placeholder in this codebase's
  SQLite dialect).

### Flaws in the old implementation (verified, not assumed)

1. The `INSERT OR ...` regexes matched **inside string literals** — a
   query selecting the text `'INSERT OR IGNORE INTO settings'` was
   rewritten in place and gained a spurious `ON CONFLICT DO NOTHING`.
2. The quote-tracker was blind to `--` / `/* */` comments and
   dollar-quoted bodies: a `?` there became a **phantom `%s`**
   placeholder, so psycopg demanded a parameter that does not exist.
   (Latent only: no in-tree migration statement currently carries a `?`
   in a comment — the `handoff_id=?` comment in `015_foreign_keys.sql:5`
   is stripped by `_split_statements` as part of the leading block — but
   any future query-path comment with `?` would have hit it.)
3. The `ON CONFLICT` guard (`"ON CONFLICT" not in out.upper()`) also
   matched inside literals.
4. Verified against psycopg 3.3.9 (`_queries.py::_split_query`): its
   placeholder scan is **quote-blind** — it regex-scans raw query bytes,
   so a bare `%` inside a literal or a comment is a `ProgrammingError`,
   and `%%` collapses back to `%` even inside a literal. The old code's
   blanket `%` → `%%` escaping was therefore correct and is preserved;
   a literal `%s` becomes `%%s` and survives as text instead of
   swallowing a parameter.

### Change — `src/ai_bridge/control/db.py`

- New `_pg_segments()` (`db.py:233`): single-pass tokenizer producing
  `(kind, start, end)` spans — `code`, `string` (`''` escape), `ident`
  (double-quoted/backtick, `""` escape), `comment` (`--` to EOL, `/* */`
  non-nested, matching SQLite's reading), `dollar` (`$tag$...$tag$`) —
  plus the first five bare code words (for the upsert head) and a
  top-level `ON CONFLICT` flag. Words inside literals/comments/bodies are
  never reported.
- New `_render_pg()` (`db.py:355`): code segments get `?` → `%s` and `%`
  → `%%` (`%` escaped first, so the introduced `%s` is not re-escaped);
  all other segments get `%` → `%%` only.
- New `_PG_UPSERT_ALLOWLIST` (`db.py:382`): exactly two heads —
  `("ignore", "settings")` → `ON CONFLICT DO NOTHING`,
  `("replace", "settings")` → `ON CONFLICT (key) DO UPDATE SET value =
  EXCLUDED.value`.
- Rewritten `to_pg_sql()` (`db.py:388`): upsert handling is an exact
  statement-head match only (head words + offsets on the original string,
  then translate). Any other `INSERT OR ...` head raises `ValueError`
  (fail closed — the Postgres equivalent needs an explicit `ON CONFLICT`
  naming the constraint, which no rewrite can know); a statement that
  already contains a top-level `ON CONFLICT` is refused the same way
  instead of gaining a second clause. `import re` removed (now unused).
- Migrations unaffected: every statement in `migrations/*.sql` was run
  through the new translator in a probe — the only `%s`-looking output is
  the `@only: sqlite` `strftime('%s','now')` default in `015` (correctly
  escaped to `%%s`, and never sent to Postgres), and `011` translates to
  `INSERT INTO settings ... ON CONFLICT DO NOTHING` exactly as before.

### Test — `tests/unit/test_dual_engine_migrations.py`

`test_to_pg_sql_translation` **updated (not deleted)**: the original five
assertions are kept verbatim, plus new assertions proving the regex
rewriting is gone: literal `%s` escaped not treated as a placeholder;
`?` preserved in line/block comments, dollar-quoted bodies, and quoted
identifiers; both allowlisted settings upserts translate (including the
exact migration-011 statement with trailing `;`); non-allowlisted upserts
(`devices`, `tasks`) raise `ValueError`; upsert-looking text inside a
string literal passes through unchanged (the old regex rewrote it); and a
source check that the `db` module contains no `re.sub`/`re.search` and no
`import re`.

### Verification

- `tests/unit/test_dual_engine_migrations.py`: **3 passed**.
- `tests/unit/test_db_phase2.py`: **23 passed**.

## 4. Dashboard `simulate_agent_intent` + webrtc limiter import

### `apps/dashboard/src/views/LiveDesktopView.tsx` (37 lines removed)

- Removed `handleSimulateIntent` (was `:422-445`): the only send site of
  `action: "simulate_agent_intent"` over the DataChannel, plus its local
  fallback that fabricated a `pendingIntent` (`"Deploy Canary Release"`,
  `tier3_shell`, 3 s countdown).
- Removed its button (was `:1905-1913`, "Preview Agent Intent").
- No dead handler left: grep confirms zero remaining references to
  `handleSimulateIntent`, `simulate_agent_intent`, or "Preview Agent
  Intent" in the file. The `Zap` import is kept — still used by the veto
  UI at `:2246`.
- Deliberately **kept**: the `pendingIntent` state, `handleVetoAction`,
  the countdown effect, and the veto banner (`:2227+`). Those are a real
  feature, not simulation scaffolding: the inbound DataChannel handler at
  `:1217` (`data.type === "agent_intent"`) sets a genuine pending intent
  from the server, and `agent_action_vetoed` clears it. Only the
  client-side *fabrication* of intents was removed.
- Server side untouched (not my file): `webrtc_stream.py:1056-1066` still
  refuses `simulate_agent_intent` by default
  (`simulate_agent_intent_disabled`); with the dashboard no longer
  sending it, that gate is now defence-in-depth only.
- Dashboard tests: no test references the removed button/handler
  (`grep` over `apps/dashboard/src/tests/` — the "simulate" hits there
  are TerminalView demo labels, FleetOperations rule triggers, and policy
  simulation, all unrelated). TSX syntax verified with the repo's esbuild
  (`esbuild ... --outfile=/dev/null` → OK). Full `tsc`/dashboard suite
  not run (parent runs the full suite).

### `src/ai_bridge/webrtc_stream.py` (one import line, `:929`)

- `from ai_bridge.control.durable_ratelimit import allow` →
  `from ai_bridge.control.limiter import allow` inside
  `_check_input_rate_limit`. Signatures match positionally
  (`allow(key, limit, 1.0)`), and `limiter.allow` delegates to the same
  `durable_ratelimit` implementation, so behaviour is unchanged.
- Verified live: `limiter.allow("probe:webrtc-test", 250, 1.0)` → `True`,
  and `AgentStreamer._check_input_rate_limit()` on a probe instance →
  `True`.
- `tests/unit/test_webrtc_phase3.py`: **22 passed** (covers the rate
  limiter path and the `simulate_agent_intent` default-off gate).

## 6. Docs rate limits + systemd revoked-agent restart loop

### `docs/operations.md:16` (one line only)

Verified against the enforced middleware first
(`src/ai_bridge/control/app.py:1876-1892`, `rate_limit_middleware`):
auth paths (`/api/auth/login`, `/api/auth/oidc/login`,
`/api/auth/oidc/callback`, `/api/connectors/m365/callback`,
`/api/mcp/oauth/callback`) → **30/min per IP**; otherwise **1200/min per
authenticated token, 300/min per unauthenticated IP**; window 60 s
(`_rate_limit` default). This matches the Wave 3B-fixed `app.py` module
docstring (F-05-11).

Line 16 now reads:

`- Rate limiting: 30 req/min per IP on auth endpoints; 1200/min per
authenticated token, 300/min per unauthenticated IP otherwise (60 s
windows). For production,`

Line 17 (`add proxy-level limiting and per-tenant quotas.`) untouched.
(The passage wraps over lines 16–17; only line 16's numbers changed.)

### `deployment/systemd/ai-bridge-agent.service`

Exit 2 = revocation verified first: `agents/linux/agent_main.py:967`
(`sys.exit(2)` when `poller.run_forever()` reports `revoked`).

Change (`:12-18`):

```ini
# A revoked/expired device exits 2 ... and needs re-enrollment — restarting
# it would loop forever. SuccessExitStatus=2 treats that exit as clean, and
# Restart=on-failure (not `always`, under which SuccessExitStatus is inert)
# restarts only genuine failures.
Restart=on-failure
RestartSec=5
SuccessExitStatus=2
```

**Correction to the brief (important):** `SuccessExitStatus=2` alone does
NOT stop the loop. Under `Restart=always` systemd restarts the unit on
*any* exit, including exits listed in `SuccessExitStatus` (that setting
only changes whether the exit counts as clean, which `always` ignores).
The load-bearing part is therefore `Restart=always` → `Restart=on-failure`,
so exit 2 (now clean) is not restarted while genuine failures (exit 1,
signals, timeouts) still are. This also matches the repo's own convention:
`deployment/systemd/ai-bridge-control.service:12` already uses
`Restart=on-failure`. A clean exit 0 (SIGTERM drain) likewise no longer
restarts, which is correct — the agent only exits 0 after being asked to
stop. The Windows scheduled-task / macOS launchd equivalents named in
Wave 3C §5 item 6 are not in my file list; not done here.

## 7. Windows GUI `_poll_loop()` — rebase NOT feasible; behaviours adopted in place

### Decision: full rebase onto `Poller.run_once()` / `run_forever()` is NOT feasible

Documented in the code comment at `agents/windows/gui_app.py:1673-1700`
(`_poll_loop` docstring) and here. `agents/common/poller.py` is read-only
in this track, and `Poller` exposes no hooks for what the GUI needs:

1. `run_once()` returns only `{"task", "status"}` (`poller.py`,
   `run_once` return). The GUI's task-history record needs the tool name,
   args, result text, and elapsed time, and the live `"Running <tool>"`
   status + tray notification need the tool name *while* the task runs.
   `run_once()` gives the GUI none of those.
2. `run_forever()` loops internally with no hooks; the GUI cannot inject
   its per-iteration/per-task UI updates (`root.after(0, ...)` for queued
   count, token rotation, telemetry, poll note) or its 401-specific
   status text.
3. `Poller.start_heartbeat_loop()` discards the heartbeat payload, but
   the GUI's telemetry display needs `device_id` and `token_expires_at`
   from it; `Poller` stores only `_token_expires_at`, not `device_id` —
   so adopting the timer thread would regress the Device ID field to its
   last inline-heartbeat value. (The GUI already touches Poller privates
   such as `_wake`/`_try_post_result`, so reading `_token_expires_at`
   would be stylistically fine — the blocker is the missing `device_id`,
   for which no endpoint other than the heartbeat response exists.)

A rebase that silently dropped the task history, live status, tray
notifications, and device-ID display would be a user-visible regression,
so it was not faked.

### What was adopted in place — `agents/windows/gui_app.py`

- **Worker thread (F07-07)** — `gui_app.py:1825-1865`: `dispatch()` now
  runs on a `threading.Thread(name=f"ai-bridge-task-{task_id}")` and the
  poll thread only join-loops in 0.5 s slices, mirroring
  `Poller.run_once`. `args` are parsed on the poll thread for the history
  record (`record_args`); dispatch failure modes map identically
  (`TaskCancelled` → `cancelled`, `PermissionError` → `failed: denied:`,
  other → `failed: <type>: <msg>`).
- **Heartbeat while a task runs (F07-10 effect)** — `gui_app.py:1878-1905`:
  the join loop calls `poller.heartbeat()` every
  `poller.heartbeat_interval` (30 s), refreshing token rotation,
  `_last_ok_poll`, token-expiry display, and the poll note, so a hung tool
  can no longer stall heartbeats. The timer *thread* itself was not
  adopted (reason 3 above); the join-loop heartbeat gives the same
  liveness with the payload intact. Skipped once auth has gone terminal
  (`revoked_during_task` guard).
- **Bounded drain (F07-09)** — `gui_app.py:1866-1877, 1909-1932`: on
  `stop_event` mid-task, `drain_deadline = monotonic() +
  poller.drain_timeout` (60 s); on expiry the task is left `running` on
  the plane with **no result posted** (mirrors `run_once`'s
  `drain_timeout` return — the lease sweep re-queues it), a
  `drain_timeout` history entry is recorded, and the loop exits.
- **Terminal auth (F07-12)** — `gui_app.py:1993-2012` (new except clause
  before the generic handler) and the `revoked_during_task` path
  (`:1977-1991`): `DeviceRevokedError`/`DeviceTokenExpiredError` from
  `heartbeat()` or the claim now stop polling with status
  `"✕ Token revoked — re-enroll"` and a re-enroll log line, instead of
  the old behaviour (generic `except` → "Connection issue" → back off
  forever). The loop condition also checks `poller.revoked`
  (`:1724`) as a backstop. A terminal 401 mid-task still lets the task
  finish and report before stopping. Transient 401s (rotation race,
  `ControlPlaneHTTPError`) still take the old retry path.
- **Jittered backoff** — `gui_app.py:2015-2016`: the manual doubling
  (`poller.backoff = min(backoff * 2, 60)`) is replaced by
  `backoff_delay(backoff_attempts)` from `common.poller`, reset on
  success; all `poller.backoff` writes in `_poll_loop` removed.
- Imports extended at `gui_app.py:56-60` (`DeviceRevokedError`,
  `DeviceTokenExpiredError`, `backoff_delay`).

Unchanged on purpose: the WS wake/cancel side-channel
(`start_side_channel`), the cancel-poller thread, the spool flush/post
flow, the 401-specific transient status text, and the GUI stop signal
(`self.stop_event`; `stop_agent()` does not join the daemon worker
thread, same as before).

### Verification

- `py_compile` clean for `gui_app.py`.
- `tests/unit/test_gui_app.py` + `tests/unit/test_gui_config_security.py`:
  **8 passed, 2 skipped**.
- Not verified live: the GUI cannot run headless on this Linux host and
  there is no Windows host here; the drain/revocation paths are
  code-reviewed against `Poller.run_once`/`run_forever`, not executed.
  A Windows smoke test (start agent → run a long task → Stop → observe
  drain; revoke the token → observe the re-enroll stop) is still owed.

## 8. SIGKILL escalation — `execution.py` + `tools/shell.py`

### Contract reused

`terminal_manager.terminate_process_group(proc, grace_seconds=2.0)`
(`terminal_manager.py`, F-04-11): SIGTERM to the process group → grace →
SIGKILL to the group → `wait(5)`. Verified importable without cycles
before use: `terminal_manager` imports stdlib only; no `ai_bridge` module
imports it in a cycle. Both call sites import it at module top and pass
the **same 2.0 s grace default** explicitly.

### `src/ai_bridge/execution.py` — `TaskRunner._kill` (`:191-211`)

- Was SIGTERM-only: `os.killpg(pid, SIGTERM)` with a `proc.kill()`
  fallback on the direct child (no group SIGKILL, no wait).
- Now: `terminate_process_group(proc, grace_seconds=2.0)`, then a
  **Windows second-chance kill**: on `nt`, if `proc.poll()` is still
  `None` after the helper returns (it runs `taskkill /F /T` once and
  returns without verifying), run `taskkill /F /T` a second time and
  `proc.wait(timeout=5)`.
- `import signal` removed (now unused). Signature widened to
  `_kill(proc, grace_seconds=2.0)`; existing callers (`:175`, `:217`)
  call it positionally and are unaffected.

### `src/ai_bridge/tools/shell.py` — `_run_argv` timeout branch (`:213-234`)

- Was: bare `proc.kill()` on the direct child only — grandchildren in
  the `start_new_session` group survived the timeout (the exact gap the
  Wave 3C note called out: "kill() on timeout, no process-group
  SIGKILL").
- Now: `terminate_process_group(proc, grace_seconds=2.0)` + the same
  Windows second-chance `taskkill /F /T` + `wait(5)` when the process
  survives, then the existing `communicate(timeout=5)` drain and the
  unchanged `{"returncode": None, "timeout": True, ...}` return.
- This was the **only** kill site in `tools/*.py` (grep for `.kill()` /
  `terminate()` / `SIGTERM` / `SIGKILL` / `Popen` across the package:
  `desktop.py` `Popen` calls are fire-and-forget app launches with no
  kill semantics; `install_package`'s `subprocess.run(timeout=900)` kills
  its direct child itself on timeout). No other tools file changed.
- `terminal_manager.py` itself was **not** modified (read-only for this
  track), so the second-chance block is duplicated in the two call sites
  with a comment saying so — a shared helper would have had to live in
  the read-only module.

### Verification (functional, not just review)

Probe (venv python, Linux): a parent that ignores SIGTERM and spawns a
`sleep 60` child, killed via both paths:

- `_run_argv(..., timeout=2)`: returned `timeout: True`, **no leftover
  `sleep` processes** (`pgrep` empty) — the group SIGKILL worked where
  the old `proc.kill()` would have orphaned the child.
- `TaskRunner._kill(proc)`: took **2.01 s** (the 2.0 s grace), `poll() ==
  -9` (SIGKILL), no leftover sleepers.

### Tests

- `tests/unit/test_execution.py`: **4 passed, 1 failed** —
  `test_subprocess_linter_runs_cross_platform` fails with
  `No module named ruff` (ruff not installed in this venv). Pre-existing
  environmental failure, one of the 12 documented in Wave 3B §3; my
  change does not touch the linter path.
- `tests/security/test_security_hardening.py -k shell`: **1 passed**
  (`test_run_shell_scrubs_child_environment`).

## Follow-ups / blocked / observed (not done here)

1. **Task 7 live verification owed** — Windows smoke test of drain +
   revocation stop (see §7). The GUI change is code-reviewed, not
   executed end-to-end.
2. **`_run_argv` cancellation leak (pre-existing, out of scope)** — on
   `TaskCancelled`, `_run_argv` (`shell.py:195-226`) re-raises without
   killing the child at all; the process is orphaned in its own session.
   Task 8 covered the timeout kill path only; the cancel path needs its
   own fix (kill the group on `TaskCancelled`, mirroring the timeout
   branch).
3. **Windows/macOS service equivalents** — Wave 3C §5 item 6 also names
   the Windows scheduled task and macOS launchd for the exit-2
   no-restart treatment; only the systemd unit was in this track's file
   list.
4. **`api.ts` (read-only, sibling track)** — for the record:
   `apps/dashboard/src/api.ts:155-169`, `consumeIdentityRedirect()`,
   still reads the session token from the URL fragment
   (`window.location.hash.slice(1)` → `session_token` → `sessionStorage`,
   then `history.replaceState` to `#/live`). Untouched here; the OIDC
   change belongs to the sibling track.
5. **Full test suite not run** (parent runs it). Targeted runs only, as
   instructed: `test_dual_engine_migrations` 3 passed,
   `test_db_phase2` 23 passed, `test_webrtc_phase3` 22 passed,
   `test_execution` 4 passed + 1 pre-existing env failure,
   `test_security_hardening -k shell` 1 passed,
   `test_gui_app` + `test_gui_config_security` 8 passed + 2 skipped.

## Files changed (this track only)

- `src/ai_bridge/control/db.py` — tokenizer-based `to_pg_sql` (+2 helpers,
  allowlist); `import re` removed.
- `src/ai_bridge/execution.py` — `_kill` via `terminate_process_group` +
  Windows second-chance; `import signal` removed.
- `src/ai_bridge/tools/shell.py` — `_run_argv` timeout kill via
  `terminate_process_group` + Windows second-chance.
- `src/ai_bridge/webrtc_stream.py` — one import line (`:929`).
- `apps/dashboard/src/views/LiveDesktopView.tsx` — removed
  `handleSimulateIntent` + "Preview Agent Intent" button.
- `docs/operations.md` — line 16 only (rate-limit numbers).
- `deployment/systemd/ai-bridge-agent.service` — `Restart=on-failure` +
  `SuccessExitStatus=2` + comment.
- `agents/windows/gui_app.py` — `_poll_loop` in-place Poller-behaviour
  adoption + rebase decision docstring.
- `tests/unit/test_dual_engine_migrations.py` — `test_to_pg_sql_translation`
  updated (original assertions kept; new F-05-18 assertions added).
- `analysis/wave4-track-b.md` — this report.

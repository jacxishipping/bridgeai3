> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 4 progress — follow-up patches from Wave 3 + remaining P1/P2 wiring

**Base:** commit `88ad591` (Wave 3). No reset/rebase. No commit. No `pip install`.
**Work split:** two parallel tracks with disjoint file ownership (parent
integrated), then parent-owned integration (dashboard `api.ts` OIDC,
route-snapshot update, Wave 4 test file, full-suite verification).
Per-track detail lives in `analysis/wave4-track-a.md` (control plane) and
`analysis/wave4-track-b.md` (periphery). This file is the parent synthesis.

## 0. Files changed (Wave 4 total)

- Track A: `control/app.py`, `control/oidc.py` (docstring), `control/dispatch.py`,
  `control/routers/{admin,agent,auth,tasks}.py`
- Track B: `control/db.py`, `execution.py`, `tools/shell.py`, `webrtc_stream.py`
  (one import line), `apps/dashboard/src/views/LiveDesktopView.tsx`,
  `docs/operations.md` (one line), `deployment/systemd/ai-bridge-agent.service`,
  `agents/windows/gui_app.py`, `tests/unit/test_dual_engine_migrations.py`
- Parent integration: `apps/dashboard/src/api.ts`,
  `tests/unit/test_architecture_phase4_routers.py` (snapshot +5 routes),
  new `tests/unit/test_wave4_wiring.py` (7 tests)
- `git diff --stat`: 18 files changed (before the PERF-03 correction below).

## 1. Wave 3A §11 patches (against the router structure)

| Item | Status | Evidence |
|---|---|---|
| (a) WS legacy-token fallback removed | DONE | `routers/agent.py:111` — `X-Device-Token` header only; docstring updated. Test: `test_ws_legacy_token_query_param_rejected` (?token= → socket closed 4401) + header path still receives `hello`. |
| (b) F-05-15 OIDC wiring | DONE | `routers/auth.py:188` — callback redirects `?oidc_code=` (single-use 60 s code via `mint_oidc_exchange_code`); new `POST /api/auth/oidc/exchange` (`auth.py:192`, unknown/expired/replayed → 401), in the middleware auth-limiter list (`app.py:1928`). Fragment path deleted. Dashboard `api.ts`: `consumeIdentityRedirect()` rewritten by the parent — reads `?oidc_code=`, strips it from the URL, POSTs the exchange, stores the token in `sessionStorage` exactly as before, stores nothing on 401; new async `consumeIdentityRedirectAsync()` exported. **Caveat (parent):** the call site `main.tsx` is NOT in Wave 4's file list, so the sync wrapper fires the exchange in the background instead of awaiting it before render; the live view re-verifies via `/api/auth/me` before showing data. Follow-up: have `main.tsx` await `consumeIdentityRedirectAsync()`. |
| (c) DIST-07 async limiter in middleware | DONE | New `async _rate_limit_async` (`app.py:214`) using `limiter.rate_limit_async`/`allow_async`; middleware awaits it (`app.py:1943`). Sync `_rate_limit` kept for quick-approval endpoints; its inline import migrated to `limiter.allow`. `limiter.py` needed no edit. |
| (d) PERF-03 list projection + `/result` | DONE, CORRECTED (see §1h) | `GET /api/tasks` uses `db.list_tasks_projected()` (limit 200); new `GET /api/tasks/{id}/result` serves full result TEXT. |
| (e) PERF-06 long-poll wake | DONE | Both `next_task` spins (`routers/agent.py:266, 308`) now `await agent_channel.wait_for_device_notification(..., min(0.15, rem))` — same 0.15 s ceiling, wake on notify. |
| (f) DIST-08 progress-heartbeat WS loop | DONE | WS loop calls `agent_channel.handle_agent_message` and returns non-None replies (`routers/agent.py:171`); new progress endpoint on BOTH `POST /api/agent/tasks/{id}/progress` and `POST /api/tasks/{id}/progress` (`agent.py:358` — the latter is what the shipped Wave 3C agent already POSTs, so no agent update needed). Never returns 404/501 on this path — absent/other-device/not-running/epoch-mismatch are all 409 with distinguishing detail. **Correction to the brief:** `record_task_progress` does NOT throttle event writes; the throttle lives in `handle_agent_message` (60 s window), which the endpoint reuses instead. |
| (g) DIST-06 scheduler idempotency | DONE | `_scheduler_loop` calls `supervisor.sweep_stuck_handoffs` in the same tick as `sweep_stale_running_tasks` (`app.py:1693`, result in `app.state.last_handoff_sweep`, failure-proofed). |

### 1h. PERF-03 correction — single-task GET keeps `result` (parent, found by the full suite)

Track A applied the Wave 3A wording literally and projected `result` out of
`GET /api/tasks/{id}` as well as the list. The first full-suite run exposed
this as a real regression, not a test-contract nit: two previously-passing
tests failed with `KeyError: 'result'`, and tracing the consumers showed
why — `device_routing.execute_on_device()` polls `GET /api/tasks/{id}?wait=`
and returns `cur["result"]` to the MCP caller (`device_routing.py:234-244`,
also used at `server.py:754`); the dashboard task-detail view reads
`res.result` (`TaskQueueVisionView.tsx:541`); `ai_api.py:1508` tells clients
to "Retrieve final result with GET /api/tasks/{task_id}". Projecting the
single GET silently returned empty results for every remote execution.

Fix (parent, `routers/tasks.py` `get_task`): the query is now
`SELECT {TASK_LIST_COLUMNS}, result FROM tasks WHERE id=?` — the LIST
endpoint stays projected (that is where the multi-MB hazard lives: 200 rows
× result), the single-task GET keeps its documented contract, and
`GET .../result` remains as the lightweight result-only fetch. Both failing
tests then pass unchanged, and the Wave 4 test was updated to assert the
corrected contract (list excludes `result`; single GET includes it;
`/result` returns it). The Wave 3A bench justification (list page 99%
smaller) is unaffected — it measured the list, not the single GET.

## 2. F-05-18 — `to_pg_sql` (Track B): REPLACE, not remove

Caller audit: `PostgresConnection.execute` (`db.py:572`, the query path —
hundreds of `?`-placeholder call sites) and `executescript` (`db.py:600`, no
in-tree callers; migrations go through `execute` per statement). The only
in-tree upsert is migration 011's `INSERT OR IGNORE INTO settings`; zero
`INSERT OR REPLACE` anywhere. New tokenizer-based translation
(`_pg_segments` `db.py:233`, `_render_pg` `:355`, `_PG_UPSERT_ALLOWLIST`
`:382`, `to_pg_sql` `:388`): `?`→`%s` in code only, `%`→`%%` everywhere
(verified necessary against psycopg 3.3.9's quote-blind `_split_query`),
upserts by exact head match only, anything else raises `ValueError`.
`import re` removed. `test_to_pg_sql_translation` updated (original
assertions kept) with new proofs, including a source check that no
`re.sub`/`re.search` remains. Tests: `test_dual_engine_migrations` 3 passed,
`test_db_phase2` 23 passed.

## 3. FR-01 / FR-03

- **FR-01:** `POST /api/policy/simulate` (`routers/admin.py:152`,
  operator/admin), dry-run `decide()` via `policy_simulation`,
  `executed: False`; defaults to live plane policy; `unrestricted` /
  `unrestricted_granted_by` never read from the body (verified: a
  smuggled `{"policy": {"unrestricted": true}}` returns the identical
  decision to the same call without it). Batch form via `"calls"`.
- **FR-03:** server-side projection wired into the list endpoint (§1d/§1h);
  the client half (`client.py project_task_summaries()`) already existed
  from Wave 3C.

## 4. Dashboard + limiter migration

- `LiveDesktopView.tsx`: `handleSimulateIntent` and its "Preview Agent
  Intent" button removed (37 lines); zero `simulate_agent_intent`
  references remain in `apps/dashboard/src/` (grep-verified). The veto UI
  was deliberately kept — it is a real feature fed by inbound
  `agent_intent` messages (`:1217`). TSX syntax checked with esbuild;
  `test_webrtc_phase3` 22 passed.
- `webrtc_stream.py:929`: one import line → `control.limiter`.
- `app.py:200`: direct `durable_ratelimit.allow` import → `limiter.allow`
  (Track A). Verified by the Wave 4 static test (no direct import remains
  on either hot path) plus a functional `limiter.allow(...) is True` probe.

## 5. Audit-writer duplicates

- `dispatch.py:716` now uses `audit_writer.write_audit_event` (was
  `audit_mod.audit_event`). **Behaviour change, stated plainly:** the record
  now lands in audit_writer's sink (control-plane DB hash chain when
  `AI_BRIDGE_MCP_AUDIT_DB` is set, else the JSONL file), NOT in the
  `AI_BRIDGE_AUDIT_LOG` file itself — that variable now only gates whether
  the extra record is written; its path value is no longer honoured as a
  destination (`audit_writer` has no file-path parameter; honouring it would
  need thread-unsafe global `set_jsonl_path` mutation).
- Grep verification (`grep -rn "db\.audit(\|audit_event(" src/`):
  `ai_bridge/audit.py::audit_event` now has **zero callers** (dead code,
  left in place — deleting a module is out of scope; parent may remove it).
  `policy.py:332,362` and `routers/devices.py:88,158,254` already route
  through `audit_writer`. `worm_siem.emit_audit_event` hits are SIEM
  export fan-out, a different sink. `mcp_routes.py` has no direct writer.
- **Remaining direct `db.audit` writers — BLOCKED (out of Wave 4's file
  list):** `control/mcp_oauth.py:173` and `control/task_service.py:389`
  still call `dbmod.audit(...)` directly. `control/app.py:1197` is the
  `audit()` closure inside `create_app` — the sanctioned plane path that
  `audit_writer` itself delegates to via `audit_writer.py:132`; routing it
  through `audit_writer` would change sinks/tenant handling and is NOT a
  duplicate in the same sense. A wave owning `mcp_oauth.py` /
  `task_service.py` should route those two through `audit_writer`.

## 6. Docs / 401 split / systemd

- `docs/operations.md:16` corrected (the one permitted line) to the
  middleware-verified numbers: 30/min per IP on auth endpoints; 1200/min
  per authenticated token, 300/min per unauthenticated IP otherwise
  (60 s windows).
- 401 split (Track A): the string lived in `app.py:1160-1176`
  (`device_row` closure). Unknown token → 401 `"Unknown device token."`;
  revoked (`revoked=1` or `status='revoked'`) → 401
  `"Device token revoked."`. Agent `classify_auth_failure` still works:
  revoked stays terminal, unknown becomes transient (a rotation race no
  longer looks like a revocation).
- systemd: exit 2 = revoked verified (`agents/linux/agent_main.py:967`).
  `ai-bridge-agent.service` gains `SuccessExitStatus=2` **and**
  `Restart=on-failure` — the latter is the load-bearing part, because
  `SuccessExitStatus` is inert under `Restart=always` (matching the
  control unit's existing convention).

## 7. Windows GUI `_poll_loop()` (F07-03) — rebase NOT feasible; behaviours adopted

Track B documents at `gui_app.py:1673-1700` why a full rebase onto
`Poller.run_once()` / `run_forever()` is not feasible: `run_once` returns
only `{task, status}` — no tool/args/result/elapsed for the GUI history;
`run_forever` has no UI hooks; the heartbeat timer discards the payload
but the GUI needs `device_id`, which Poller doesn't store; `poller.py`
was read-only in this wave. Adopted in place instead: worker-thread
dispatch, heartbeats from the join loop during tasks, bounded drain (no
result posted on timeout, mirroring `run_once`), terminal revoked/expired
handling that stops polling, jittered `backoff_delay`. GUI tests: 8 passed,
2 skipped. Live Windows smoke test still owed.

## 8. SIGKILL escalation (execution.py + tools/shell.py)

Both now use `terminal_manager.terminate_process_group` (SIGTERM → 2.0 s
grace → SIGKILL process group; no import cycle — it imports stdlib only),
plus a Windows second-chance `taskkill /F /T` when the process survives:
`execution.py:191` `TaskRunner._kill`, `tools/shell.py:213` `_run_argv`
timeout branch. Functionally verified by Track B: a SIGTERM-ignoring
parent + child sleeper leaves no orphans via either path; `_kill` took
2.01 s, `poll() == -9`. `test_execution` 4 passed + 1 pre-existing env
failure (missing `ruff`, one of the 12 baseline failures). **Known
pre-existing leak, out of scope:** `_run_argv` cancellation
(`TaskCancelled`) still orphans the child — only the timeout path was
tasked; needs its own follow-up.

## 9. Tests + full-suite comparison

### New tests

`tests/unit/test_wave4_wiring.py` (7 tests, all passing):

- `test_ws_legacy_token_query_param_rejected` / `test_ws_header_token_still_works`
- `test_oidc_callback_completes_without_url_fragment_token` (mocked IdP via
  monkeypatched `oidc` module + `httpx.post`; 302 to `?oidc_code=`, no `#`,
  no `session_token` in the URL; exchange → 200 + working session; replay
  and unknown codes → 401)
- `test_list_endpoints_exclude_result_and_result_endpoint_returns_it`
  (corrected contract per §1h)
- `test_policy_simulate_returns_decision_without_executing` (single +
  batch; task count unchanged; smuggled `unrestricted` ignored)
- `test_limiter_imports_come_from_control_limiter` (static + functional)
- `test_to_pg_sql_has_no_regex_rewriting` (source + behaviour)

Targeted runs: wave4 (7) + routers snapshot (19) = 26 passed. Track B's
targeted runs: `test_dual_engine_migrations` 3 passed, `test_db_phase2`
23 passed, `test_webrtc_phase3` 22 passed, GUI tests 8 passed / 2 skipped,
`test_execution` 4 passed + 1 pre-existing env failure.

### Full Python suite — first run (before the §1h correction)

Command: `TMPDIR=$HOME/workspace/tmp-pytest .venv/bin/python -m pytest
tests/ -q -p no:cacheprovider --tb=no` (576 s).
**Result: 14 failed, 748 passed, 5 skipped.**
Baseline (Wave 3B): 743 passed, 5 skipped, 12 failed. The 12 baseline
failures were all present and unchanged. The 2 extra failures were both
the §1h PERF-03 over-projection (`KeyError: 'result'`):
`tests/integration/test_control_plane.py::test_enroll_task_agent_result_flow`
and `tests/unit/test_feature_batch.py::test_512kb_result_and_long_poll_wakeup`.
Both were fixed by the correction, not by touching the tests — see §1h.
(The known-flaky timing test `test_concurrent_claim_and_cancel_under_load`
did NOT fail in this run.)

### Full Python suite — second run (after the §1h correction)

Command: same as above (567 s).
**Result: 12 failed, 750 passed, 5 skipped.** The 12 failures are
exactly the Wave 3B baseline 12 (verified by diffing the FAILED lists:
run 2 adds nothing over run 1, and run 1's only extras were the two
§1h tests, which now pass). 750 = 743 baseline + the 7 new Wave 4
tests. No new failure was introduced by Wave 4. The known-flaky timing
test `test_concurrent_claim_and_cancel_under_load` passed in both runs.

### Dashboard suite

Command: `npm test` in `apps/dashboard` (exit 0).
**Result: 430/430 test cases passed, 0 failed** (R1–R5 26, Tier 1 155,
Tier 2 155, Tier 3 10, Tier 4 10, Tier 5 31, Challenger 43), plus
`test:i18n` 3/3 — identical to the Wave 3 baseline. The `api.ts` OIDC
rewrite and the `LiveDesktopView` removal introduced no dashboard
regression. Note: the Playwright e2e suite (`npm run test:browser`) was
NOT run (it needs a served dashboard + browser); its
`dashboard.spec.ts:134` fragment test is flagged in §10 item 4.

## 10. Blocked / parent follow-ups

1. **Audit writers:** `mcp_oauth.py:173`, `task_service.py:389` still call
   `dbmod.audit` directly (files outside Wave 4's list). Needs a wave
   owning those files.
2. **v1 WS legacy-token fallback:** `v1_routes.py:985` has its own
   `?token=` fallback (`q_tok` → Bearer when no admin/session/auth
   header). Out of Wave 4's file list; the file's own comment frames it
   as deliberate compat for non-dashboard WS clients ("Long-lived tokens
   in the URL remain accepted for non-dashboard WS clients"). Removing it
   needs a wave owning `v1_routes.py` and a decision on that compat.
3. **OIDC login end-to-end:** server + `api.ts` are done, but `main.tsx`
   (not a Wave 4 file) still calls the sync wrapper; the exchange runs in
   the background and the token lands before the live view verifies the
   session, but the clean fix is `main.tsx` awaiting
   `consumeIdentityRedirectAsync()` before rendering.
4. **Dashboard e2e (Playwright, not `npm test`):**
   `apps/dashboard/e2e/dashboard.spec.ts:134` navigates to
   `/#session_token=...` and asserts the old fragment behaviour — it will
   fail against the new flow and needs updating by a wave owning e2e.
5. **Live verification still owed:** two-worker Redis fan-out against real
   Redis; Postgres boot-from-migrations parity; Postgres backup/restore
   drill; Windows live smoke test for the GUI drain/revocation path and
   the C++ agent; Windows scheduled-task / macOS launchd exit-2
   equivalents (systemd only was in scope).
6. **Pre-existing `_run_argv` cancellation leak** (§8) — `TaskCancelled`
   orphans the child; timeout path only was tasked.
7. **0.5 s execution-state poll still appends a `task_events` row per
   call** (Track A §1f): throttling it would change liveness semantics;
   left as-is.
8. **`ai_bridge/audit.py::audit_event` is dead code** (zero callers) —
   parent may delete the module.
9. **198.18.0.0/15 SSRF carve-out** (Wave 3B) still open; unchanged here.

## 11. Rollback

- All route additions are additive (`include_router` / new handlers);
  removing the 5 new routes restores the old surface (the snapshot test
  would then need its 5 entries removed). The PERF-03 correction is a
  one-line query change in `routers/tasks.py`. OIDC: the old fragment
  redirect is a one-line revert in `routers/auth.py` + `api.ts` restore
  from git. `to_pg_sql`: the tokenizer is contained in `db.py`; revert
  via `git checkout` of `db.py` + the migration test. SIGKILL: revert
  `execution.py` / `tools/shell.py` hunks. Nothing is committed, so
  `git checkout` of the listed files reverts Wave 4 cleanly.
- The two pre-existing tests that caught the §1h regression were NOT
  modified — they now pass against the corrected code, which is the
  evidence the correction is right.

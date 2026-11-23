> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 4 Track A — control-plane wiring — progress

**Base:** commit `88ad591`. No reset/rebase/commit. No `pip install`.
**Files edited (allowed list only):** `src/ai_bridge/control/app.py`,
`src/ai_bridge/control/oidc.py` (docstring only),
`src/ai_bridge/control/dispatch.py`,
`src/ai_bridge/control/routers/agent.py`,
`src/ai_bridge/control/routers/auth.py`,
`src/ai_bridge/control/routers/tasks.py`,
`src/ai_bridge/control/routers/admin.py`.
`src/ai_bridge/control/limiter.py` was read, not edited — its async
interface already existed and needed no change.
**Scope check:** `git status` shows exactly those 7 files modified.
Nothing outside the allowed list was edited.

**Verification status (honest):** targeted pytest runs were BLOCKED in
this environment — no `pytest`, `fastapi`, `httpx`, or `pydantic` in
the VM Python (`python3 -m pytest` → `No module named pytest`;
`import fastapi` fails the same way), and `pip install` is forbidden.
What WAS verified here:
- `python3 -m py_compile` on all 7 edited files — OK.
- Pure-stdlib exercise of `policy_simulation.simulate_policy_decision`
  / `simulate_batch` (FR-01's underlying call): tier-1 allowed,
  tier-4 needs-approval, tier-3 with `allow_tier3` allowed, unknown
  tool denied, all with `executed: False`.
- Direct exercise of `db.list_tasks_projected(conn, tenant, device_id,
  limit=200)` + `db.get_task_result()` on a scratch SQLite DB: 2 rows,
  no `"result"` key in any projected row, 5,000-char result returned
  only via `get_task_result`, `None` for a missing task,
  `TASK_LIST_COLUMNS` contains no `result`.
- Full `git diff` review of every hunk (see per-task evidence below).
- The parent MUST run the test suite: the new routes below WILL fail
  `tests/unit/test_architecture_phase4_routers.py`'s exact route-set
  assertion (see §8) — that test file is not in this track's list, so
  its `EXPECTED_ROUTES` needs a parent-owned update, not a quiet edit
  here.

## 1a. WS legacy-token fallback removed — DONE

- `src/ai_bridge/control/routers/agent.py:111`:
  `token = websocket.headers.get("x-device-token")` — the
  `or websocket.query_params.get("token")` fallback is gone.
- `routers/agent.py:105-110`: docstring now says header-only and
  states the `?token=` fallback was removed (token in a URL lands in
  logs/history). No other `query_params.get("token")` in the agent
  router. `device_row(token)` with `None` still raises 401 "Device
  token required." as before.
- Note: the existing WS test (`test_serverai_mcp_orchestration.py::
  test_agent_ws_hello`) connects with the `X-Device-Token` header, so
  it is unaffected by this removal.

## 1b. F-05-15 OIDC wiring (server side) — DONE

- `src/ai_bridge/control/routers/auth.py:188-189` (`oidc_callback`):
  after the session row is inserted,
  `code = oidcmod.mint_oidc_exchange_code(session_token)` and
  `return RedirectResponse(dashboard + "?oidc_code=" + code,
  status_code=302)`. The old
  `dashboard + "#" + urlencode({"session_token": ...})` line is deleted;
  the now-unused `urlencode` import was removed too.
- `src/ai_bridge/control/routers/auth.py:192-211`: new
  `POST /api/auth/oidc/exchange`, body `{"code": ...}`, returns
  `{"session_token": ...}`. Unknown / expired / replayed / missing
  code → 401 `"Invalid or expired OIDC exchange code."` (redeem is
  single-use: the row is deleted on first read).
- Rate limiting: `src/ai_bridge/control/app.py:1928` adds
  `"/api/auth/oidc/exchange"` to the middleware's auth-path list, so
  it gets the 30/min-per-IP auth limiter like the other auth paths.
- `src/ai_bridge/control/oidc.py` docstring updated (stale "still uses
  the fragment" paragraph replaced with the wired flow). No code change
  needed there — `mint/redeem_oidc_exchange_code` already existed.
- Dashboard half NOT done (not this track's file): see §7 for the
  exact `apps/dashboard/src/api.ts` contract the sibling track owns.

## 1c. DIST-07 async limiter in middleware — DONE

- `src/ai_bridge/control/app.py:214-232`: new
  `async def _rate_limit_async(key, limit=120, window=60, conn=None)`.
  When a Redis URL is set it awaits `limiter.rate_limit_async`;
  otherwise it awaits `limiter.allow_async(..., conn=conn)`. Raises
  429 exactly like the sync helper.
- `src/ai_bridge/control/app.py:1943` (`rate_limit_middleware`):
  `await _rate_limit_async(key, limit=limit, conn=...)` — the sync
  `_rate_limit` call is gone from the async middleware, so the shared
  Redis limit is now enforced instead of the silent per-worker memory
  fallback.
- `src/ai_bridge/control/app.py:196-212`: sync `_rate_limit` kept for
  the sync quick-approval endpoints (`routers/tasks.py:302-304`
  unchanged), and its inline `from .durable_ratelimit import allow`
  is now `from .limiter import allow` — `limiter.py` is the single
  interface; no caller in this track imports `durable_ratelimit`
  directly any more (grep-verified across `app.py` + `routers/*.py` +
  `limiter.py`, which itself owns the delegation).
- `limiter.py` itself needed no edit: `rate_limit_async` /
  `allow_async` already existed.

## 1d. PERF-03 task-result projection — DONE

- `src/ai_bridge/control/routers/tasks.py:89-95` (`GET /api/tasks`):
  `dbmod.list_tasks_projected(conn, tenant=tenant,
  device_id=device_id, limit=200)`. Same tenant resolution and
  device filter as before; limit 200 preserved. Returned dicts have
  no `"result"` key (verified live against a scratch DB, see header).
- `src/ai_bridge/control/routers/tasks.py:98-114`
  (`GET /api/tasks/{id}`): the wait-loop fetch is now
  `SELECT {dbmod.TASK_LIST_COLUMNS} FROM tasks WHERE id=?` (projected
  columns, no `"result"`); tenant check, wait loop, and terminal-status
  early return unchanged.
- `src/ai_bridge/control/routers/tasks.py:117-130`: new
  `GET /api/tasks/{task_id}/result` — viewer auth + the same
  `_require_row_tenant` check as `get_task`, then
  `dbmod.get_task_result(conn, task_id)`; returns
  `{"task_id", "result"}` (`""` when the task has no result yet; 404
  only when the task is truly absent).
- Out of scope, deliberately untouched: `update_task` still returns
  the full row (`SELECT *`), and `DELETE /api/tasks` / cancel paths
  are unchanged. The full payload is only served by the new `/result`
  route now.

## 1e. PERF-06 long-poll wake — DONE

- `src/ai_bridge/control/routers/agent.py:266` (active-lease branch)
  and `:308` (end-of-loop branch) in `next_task`: both
  `await asyncio.sleep(min(0.15, rem))` spins are now
  `await agent_channel.wait_for_device_notification(row["id"],
  min(0.15, rem))`. Same 0.15 s ceiling, so behaviour is unchanged
  when no wake arrives; a notify wakes the poll immediately.
  No `asyncio.sleep(min(0.15, ...))` remains in the agent router
  (grep-verified). `get_task`'s wait loop in `tasks.py` was NOT
  changed — it is an operator wait, not the agent long-poll, and
  `wait_for_device_notification` is per-device, not per-task.

## 1f. DIST-08 progress heartbeat — DONE (WS loop + HTTP endpoint)

- WS loop: `src/ai_bridge/control/routers/agent.py:164-172` — the
  message loop's `else` branch (after `pong`/`heartbeat` and `ping`)
  calls `agent_channel.handle_agent_message(conn, device_id, msg)`
  and sends the returned dict back when it is not None. Unknown
  types return `None` and are ignored, as before.
- HTTP endpoint: `src/ai_bridge/control/routers/agent.py:358-430`,
  `task_progress`, registered on TWO paths with one handler:
  `POST /api/agent/tasks/{task_id}/progress` (the agent namespace,
  as tasked) and `POST /api/tasks/{task_id}/progress` (the path the
  shipped Wave 3C agent already POSTs to — see
  `agents/common/poller.py:660`). Same device-token auth + mTLS
  check on both, so no agent update is needed for this to work.
  - Payload: `{"task_id", "progress_pct" (0–100 | null),
    "status_message" (string | null), "epoch" (int, optional)}`.
    Body `task_id` mismatching the path → 400; bad `progress_pct` /
    `epoch` / `status_message` types or out-of-range pct → 400.
  - Bumps `updated_at` iff `status='running'` by delegating to
    `agent_channel.handle_agent_message` → `orchestration.
    record_task_progress`; never changes `status`. `progress_pct` /
    `status_message` are validated and echoed, not persisted
    (contract: liveness only).
  - Status codes: this path NEVER returns 404/501 (the agent treats
    those as "endpoint not implemented" and disables progress for the
    run). Absent task → 409 `"Task not found."`; task owned by another
    device → 409 `"Task not found for this device."`; not running →
    409 with the current status; epoch mismatch → 409. The task's
    "404 only if truly absent?" was resolved in favour of the wave3c
    §2 rule (prefer 409 over 404 on this path); the detail text still
    distinguishes the cases.
  - Throttle — VERIFIED, premise corrected: `record_task_progress`
    does NOT itself throttle event writes; it appends a `task_events`
    `"progress"` row on every successful call
    (`orchestration.py`, unconditional `append_task_event`). The
    throttle lives in `agent_channel.handle_agent_message` (60 s
    per-task window, liveness probe first so a just-completed task
    still gets `ok: False` inside the window). The HTTP endpoint
    reuses that exact function instead of calling
    `record_task_progress` directly, so WS and HTTP heartbeats share
    one throttle and no extra `task_events` row is written per call
    inside the window (response carries `"throttled": true`). The
    remaining per-0.5 s event flood is the pre-existing
    `execution-state` poll (`agent.py:346`, which calls
    `record_task_progress` on every poll) — wave3c §2 follow-up #1
    asked for server-side throttling there; it is in this track's
    file but was NOT changed, because throttling the execution-state
    poll would change its liveness semantics and was not in this
    task's list. Flagged for the parent: if that flood is to be
    throttled, the same `handle_agent_message` delegation (or a
    throttle inside `record_task_progress`, which lives in
    `orchestration.py`, outside this track) is the place.

## 1g. DIST-06 stuck-handoff sweep — DONE

- `src/ai_bridge/control/app.py:1685-1695` (`_scheduler_loop`): in the
  same sweep tick as `sweep_stale_running_tasks`, the scheduler now
  calls `supervisor.sweep_stuck_handoffs(conn)` and stores the result
  in `app.state.last_handoff_sweep` (alongside the existing
  `app.state.last_stale_sweep`). Wrapped in `try/except: pass` so a
  handoff-sweep failure can never kill the scheduler loop or the
  stale-task sweep (the sweeper itself already counts per-task
  errors). Runs once per sweep tick (default every 3rd 10 s loop
  iteration, `AI_BRIDGE_TASK_SWEEP_EVERY`).

## 3. FR-01 policy simulation endpoint — DONE

- `src/ai_bridge/control/routers/admin.py:152`: new
  `POST /api/policy/simulate` (singular; distinct from the existing
  `POST /api/policies/simulate`, which simulates a whole proposed
  policy across all tools and is untouched).
- Auth: `require_role("operator", ...)` — operator or admin.
- Body: `{"tool", "policy"?, "approval_granted"?, "is_admin"?,
  "device_revoked"?, "device_id"?, "actor"?}` or `{"calls": [...]}`.
  Returns `simulate_policy_decision(...)` — tool, tier/tier_name,
  allowed, needs_approval, reason, rule, actor, device_id,
  `executed: False` — i.e. the `policy_simulation.py` dry-run
  `decide()` outcome; nothing is executed, no task/approval/policy
  row is created. Batch form returns
  `{"simulated": true, "executed": false, "actor", "results": [...]}`.
- Policy source: with no `"policy"` in the body, the live plane
  policy (`get_current_policy()`) is simulated against; with one,
  only whitelisted fields are honoured (`allow_writes`, `allow_tier3`,
  `desktop_enabled`, `max_tier_unattended` clamped 0–4, set fields as
  frozensets). `unrestricted` / `unrestricted_granted_by` are
  deliberately never read from the body, so a caller-supplied body
  can never make the simulation report the granted-unrestricted
  bypass. `is_admin` defaults to the caller's actual role (admin or
  not, via `_auth_context_full`, newly bound in the admin router);
  an explicit body value overrides it for what-if simulation.
- FR-03 server side is 1d above.

## 5. Audit-writer duplicates — DONE (dispatch.py) + report

- `src/ai_bridge/control/dispatch.py:702-726`: the
  `audit_mod.audit_event(...)` call is replaced with
  `audit_writer.write_audit_event(actor="dispatch", action=tool,
  device_id=..., details={arguments, decision, result})`. The
  now-unused `from .. import audit as audit_mod` and
  `from pathlib import Path` imports were removed (`os` stays — still
  used for the env read).
- BEHAVIOUR CHANGE, stated plainly: the record now lands in
  audit_writer's sink (control-plane DB hash chain when
  `AI_BRIDGE_MCP_AUDIT_DB` is set, else the JSONL file at
  `AI_BRIDGE_MCP_AUDIT_FILE` / `~/.ai-bridge/mcp-audit.jsonl`), NOT
  in the `AI_BRIDGE_AUDIT_LOG` file itself. That variable now only
  gates whether this extra record is written; its path value is no
  longer honoured as a destination. `audit_writer` has no
  file-path parameter, so honouring the path would have required
  global `set_jsonl_path` mutation (thread-unsafe, would redirect
  all MCP audit writes too). The per-tool control-DB record
  (`dispatch_audit` → app `audit` closure) is unchanged.
- Grep `grep -rn "db\.audit(\|audit_event(" src/ --include=*.py`
  after the fix — remaining hits:
  - `src/ai_bridge/audit_writer.py:132` — `control_db.audit(...)`
    INSIDE `audit_writer._write_db`: the sanctioned sink, not a bypass.
  - `src/ai_bridge/audit_writer.py:193` — the `write_audit_event`
    definition itself.
  - `src/ai_bridge/control/routers/devices.py:88,158,254` and
    `src/ai_bridge/policy.py:332,362` — already via
    `audit_writer.write_audit_event`; no change needed.
  - `src/ai_bridge/control/routers/audit.py:90`,
    `src/ai_bridge/control/db.py:629` — `worm_siem.emit_audit_event`
    (SIEM export fan-out, a different sink, not a DB audit write).
  - `src/ai_bridge/audit.py:12` — the `audit_event` definition
    itself. It now has NO remaining callers in `src/` (verified by
    grep for `audit\.audit_event` / `audit_mod` — zero hits): it is
    dead code after this change. Left in place (deleting a module is
    outside this track's list); parent may remove `audit.py` or keep
    it for out-of-tree callers.
  - Wider grep for `\.audit(` also finds direct `dbmod.audit(`
    callers OUTSIDE this track's files — reported as blocked /
    out-of-scope, NOT edited:
    - `src/ai_bridge/control/app.py:1197` — the `audit()` closure
      inside `create_app` (the control plane's own audit function,
      passed to every router and to `task_service`). This is the
      sanctioned plane path that `audit_writer` itself delegates to;
      routing it through `audit_writer` would change sinks/tenant
      handling and is NOT a duplicate in the sense of this task.
      In this track's file but intentionally left as-is.
    - `src/ai_bridge/control/mcp_oauth.py:173` — `dbmod.audit(...)`,
      not my file.
    - `src/ai_bridge/control/task_service.py:389` —
      `dbmod.audit(...)`, not my file.
  - The task brief mentioned `mcp_routes.py` as a possible direct
    caller: grep finds NO `db.audit` / `audit_event` call in
    `mcp_routes.py` — nothing to report there.

## 6. 401 split — DONE (in this track's file)

- The string lived in `src/ai_bridge/control/app.py:1160-1176`
  (`device_row` closure) — inside this track's list after all.
  Split:
  - No row for the token hash → 401 `"Unknown device token."`
  - Row with `revoked=1` OR `status='revoked'` → 401
    `"Device token revoked."` (revoke sets both — see
    `routers/devices.py:132`; both are checked so legacy rows with
    only one marker still classify as revoked).
  - Expired detail unchanged.
- Agent compatibility: `agents/common/poller.py`
  `classify_auth_failure` keys off `"revok"` → revoked is still
  terminal (`DeviceRevokedError`); the unknown text contains no
  `"revok"`/`"expir"` marker → `"transient"` (retried, saved-token
  retry exhausted first, as before). That is the intended
  improvement: a rotation race (unknown) no longer looks like a
  revocation. The agent's own tests pass the OLD literal string to
  the classifier, so they are unaffected by the plane-side text
  change; no control-plane test asserted the old combined detail
  (grep-verified).
- Detail is kept a plain string (not a `{"code": ...}` dict) so the
  agent's body parsing is unaffected. The distinguishable codes are
  the two detail strings: `Unknown device token.` vs
  `Device token revoked.` — i.e. `device_unknown` vs `device_revoked`
  in the report's terms.

## 7. Dashboard contract for the sibling track (api.ts — NOT my file)

Server side is done; the dashboard still reads the fragment and will
break on OIDC login until this lands. Exact change required in
`apps/dashboard/src/api.ts`, function `consumeIdentityRedirect()`
(currently lines ~155-169, reads `window.location.hash`):

1. Read the code from the QUERY string, not the hash:
   `const params = new URLSearchParams(window.location.search);`
   `const code = params.get("oidc_code"); if (!code) return false;`
2. Exchange it (this MUST be async — `consumeIdentityRedirect` is
   currently sync; make it `async` and update its call site, or do
   the exchange in the caller):
   `POST {BASE}/api/auth/oidc/exchange` with JSON body
   `{"code": code}` and `Content-Type: application/json`. No auth
   headers (the code IS the credential, single-use, 60 s TTL).
3. On 200, take `response.session_token` and store it EXACTLY as
   today: `sessionStorage.setItem("ai_bridge_session_token", token)`
   (session only, never `localStorage` — the sign-in copy promises
   the browser drops it on close).
4. Strip the code from the URL in the same step, e.g.
   `window.history.replaceState(null, "", window.location.pathname + "#/live")`
   (keep the existing `#/live` landing behaviour; do NOT leave
   `?oidc_code=...` in the address bar or history).
5. On non-200 (401 = unknown/expired/replayed code, or network
   failure): do NOT store anything, return `false`, and let the
   existing login screen handle it. Do NOT retry with a second code
   (there is none — a code is single-use; a replay is a 401).
6. DELETE the fragment path entirely: remove the
   `window.location.hash` / `session_token` read. No fallback to the
   fragment — a fallback would reintroduce the vulnerability
   (fragment persists in history, readable by page scripts).
7. Server contract summary (already live): callback redirects to
   `{dashboard}/?oidc_code=<urlsafe-32>` (302); exchange endpoint
   `POST /api/auth/oidc/exchange` → 200 `{"session_token": "..."}`
   or 401 `{"detail": "Invalid or expired OIDC exchange code."}`;
   rate-limited 30/min per IP with the other auth endpoints. The
   code is valid for 60 s and single-use; the redirect must be
   followed and exchanged promptly (the dashboard should exchange on
   load, before rendering, as `consumeIdentityRedirect` does today).

## 8. Blocked / parent follow-ups

1. **Route snapshot test WILL fail until updated (parent-owned):**
   `tests/unit/test_architecture_phase4_routers.py` asserts exact
   equality with `EXPECTED_ROUTES` (19747b2 snapshot). This track
   adds 5 routes, all required by the task:
   `POST /api/auth/oidc/exchange`,
   `POST /api/policy/simulate`,
   `GET /api/tasks/{task_id}/result`,
   `POST /api/agent/tasks/{task_id}/progress`,
   `POST /api/tasks/{task_id}/progress`.
   The test file is not in this track's list; the parent must add
   those 5 entries to `EXPECTED_ROUTES`. No route was removed or
   re-methoded.
2. **Test suite not run here** (no pytest/fastapi in this VM, no pip
   install allowed). Parent run needed; besides the snapshot test,
   watch `test_serverai_mcp_orchestration.py` (WS hello),
   `test_p0_control_plane.py`, `test_db_phase2.py`,
   `test_distributed_phase2.py`, `test_agent_reliability.py`
   (classifier literals — should be unaffected), and
   `tests/integration/test_security_batch.py` (uses
   `/api/policies/simulate` — untouched).
3. **Dashboard OIDC** — §7; sibling track owns `api.ts`. Until it
   lands, OIDC login is broken end-to-end (server redirects with
   `?oidc_code=`, dashboard still looks in the hash).
4. **Out-of-scope direct audit writers** — §5:
   `mcp_oauth.py:173`, `task_service.py:389` (both `dbmod.audit`,
   not my files). `ai_bridge/audit.py:audit_event` is now dead code
   (no callers); removal is a parent call.
5. **v1 WS token fallback NOT in scope:** `control/v1_routes.py:985`
   has its own `websocket.query_params.get("token")` for the v1
   session WS. That file is not in this track's list and the task
   named only `/api/agent/ws`; left untouched, flagged here.
6. **Execution-state poll flood** (wave3c §2 follow-up #1): the
   0.5 s `execution-state` poll still appends a `task_events` row
   per call via `record_task_progress` (`routers/agent.py:346`);
   see §1f for why it was not throttled here.
7. **Live verification still owed:** Redis-backed async limiter
   (no Redis in this env — the async path is code-verified only),
   two-worker behaviour of the new sweep, and an end-to-end OIDC
   login against a real provider + dashboard.

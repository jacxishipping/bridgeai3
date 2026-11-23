> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 2A — Control Plane — Progress

Owner: Wave 2A subagent (control plane). Repo: `/home/hatch/workspace/ai-bridge-main`,
on top of git commit 726bed2 (Wave 1). Do NOT commit (parent commits per wave).
Scope files ONLY: `src/ai_bridge/control/*.py`, `src/ai_bridge/updates.py`,
`src/ai_bridge/tools.py` (SEC-07 env scrub + R-P0-16 `update_fleet_agent` only),
`migrations/*`, `windows-agent/installer/*`, `src/ai_bridge/execution.py` (only if
needed — prefer `control/task_runtime.py`).
ADDED: `src/ai_bridge/control/task_service.py`,
`tests/unit/test_p0_control_plane.py`, `tests/unit/test_tenant_isolation.py`,
`tests/unit/test_workflow_approval.py`.
Delegated to depth-2 children (same file scope, disjoint files):
- updates/env/installer → `analysis/wave2a-updates-progress.md`
- DB/audit/secrets/migration 013 → `analysis/wave2a-db-progress.md`
- distributed (OIDC/task_runtime/ratelimit/agent_channel/orchestration) → `analysis/wave2a-dist-progress.md`

Baseline (Wave 1, per commit message): 467 passed, 5 skipped, same 12 pre-existing failures.

## 1. Finding verification (before any edit)

| Finding | Verified as |
|---|---|
| R-P0-04 term auto-approval | CONFIRMED: `control/app.py` `next_task` — `elif id startswith "term-" and tool=="terminal_exec": approval_granted=True`. |
| R-P0-10 v1 bypass | CONFIRMED: `control/v1_routes.py` `v1_create_task` — direct INSERT, status `'queued'`, tenant hard-coded `"default"`, no approval branch. `v1_list_tasks` has no tenant filter. |
| R-P0-11 workflow admin | CONFIRMED: `control/app.py` `_execute_workflow_step_sync` builds `Policy(allow_writes=True, allow_tier3=True, max_tier_unattended=4)` + `dispatch(..., approval_granted=True, is_admin=True)` in both branches; `WorkflowIn.require_step_approval` defaults `False`; `_run_workflow_worker` only gates Tier>=3 when that flag is on. |
| R-P0-12 tenant caller-supplied | CONFIRMED: `TaskIn.tenant` / `list_devices(tenant="default")` / `list_tasks(tenant)` / `list_audit(tenant)` take caller tenant; `users`/`sessions` have no tenant column (`control/db.py` SCHEMA). |
| R-P0-03 local execution | CONFIRMED: `control/ai_api.py` `execute_ai_tool` local branch runs `dispatch()` in-process for any `operator`; remote branch duplicates `submit_task` approval logic incl. `is_unrestricted` bypass. |
| R-P0-02 control half | CONFIRMED: `control/app.py` `enroll()` stores `unrestricted=True, max_tier_unattended=4` from body; `submit_task`/`ai_api` skip `needs_approval` when `eff_policy["unrestricted"]`. `policy.decide()` (Wave 1C) already ignores bare `unrestricted`, but the plane never calls `decide()` here. |
| R-P0-16 unsigned fleet update | CONFIRMED: `tools.update_fleet_agent` does `git fetch origin main && git checkout -f -B main origin/main`; `control/app.py` `update_fleet`/`update_device_agent` queue that tool. Signed library `updates.py` unused by the path. (Delegated to updates child.) |
| SEC-08 takeover | CONFIRMED: `submit_task` (`/api/tasks`) has NO takeover check; `v1_create_task` DOES check (409 unless `human_initiated`); `next_task` hands out queued tasks with no takeover check; workflow/scheduler paths have no check. Durable flag exists (`plane_state.is_human_takeover_active`, settings table). |
| DB-05 audit non-atomic | CONFIRMED: `control/db.py` `audit()` read-tip → INSERT (entry_hash NULL) → UPDATE entry_hash → commit. (Delegated to DB child.) |
| SEC-06/DB-06 plaintext tokens | CONFIRMED: `mcp_servers.auth_token/oauth_refresh_token/oauth_client_secret` plaintext; `SELECT *` reads. (Delegated to DB child.) |
| SEC-07 env leak | CONFIRMED: `tools._run_argv` child env = `os.environ` minus 3 keys. (Delegated to updates child.) |
| DIST-01 scheduler | CONFIRMED: `control/app.py` `_scheduler_loop` SELECTs due jobs then `UPDATE schedules SET ... WHERE id=?` with no `next_run_at` guard and no rowcount check — every worker fires the job. |
| DIST-05/F07-04 late results | CONFIRMED: `task_result` does unconditional `UPDATE tasks SET status=? ... WHERE id=?` — a late result overwrites any terminal state. No epoch column. |
| TST-15 e-stop/OIDC | PARTIAL: e-stop IS already durable (`plane_state`, settings table) but `submit_task`/v1/workflow read in-memory `app.state.emergency_stop` without refresh; OIDC `_PENDING` is process memory (`control/oidc.py`). (OIDC delegated to dist child.) |
| F-05-02 CSRF opt-in | CONFIRMED: `create_app` registers `csrf_middleware` only when `AI_BRIDGE_CSRF_ENABLED=1`; secret is per-process random (`control/csrf.py`). |
| F-05-05 legacy admin token | CONFIRMED: `control/auth.py` `legacy_admin_enabled()` defaults ON (`!= "1"` check on DISABLE var); `create_app` resolves token from env via `get_secret`. |
| F-05-06 mTLS tasks/next only | CONFIRMED: `require_device_mtls` called only in `next_task`; heartbeat/whoami/ws/policy/rotate/result/webrtc agent routes never check. |
| F-05-07 global exec context | CONFIRMED: `control/task_runtime.py` single `_current_task_id/_cancel_check`. (Delegated to dist child.) |
| F-05-10 secrets fail-open | CONFIRMED: `control/secrets_backend.py` vault/aws fall back to env unless `AI_BRIDGE_SECRETS_REQUIRED=1`. (Delegated to DB child.) |
| F08-08 native config plaintext | See Wave 1B §10 (blocked, out of scope there). (Delegated to updates child.) |
| PERF-01 pool | DEFERRED with reason (see §4): single shared connection + RLock is correct, just serialised; a real pool requires a connection-checkout refactor across every route. No load-test harness exists to measure first (roadmap: measure first). |
| PERF-02 rate limit DB hot path | Delegated to dist child (Redis-first; DB path only when no Redis URL configured). |

## 2. Design decisions (parent)

- **task_service.create_task is the ONE task-creation service.** `submit_task`
  (`/api/tasks`), `v1_create_task`, and `ai_api.execute_ai_tool` (remote branch)
  all call it. It does: takeover check → durable e-stop check → tenant-scoped
  device lookup → `decide()` (approval_granted=False, is_admin=False, so the
  gate is purely policy-based) → approval row + `needs_approval` status when
  `decide().needs_approval` → idempotent insert via `task_submissions` → audit →
  notify. The `is_unrestricted` bypass is deleted: `decide()` ignores bare
  `unrestricted` (Wave 1C) and Tier 4 always needs approval.
- **Tenant model.** Session users are strictly scoped to their user tenant
  (`users.tenant`, copied to `sessions.tenant` at login; migration 013).
  A caller-supplied `tenant` differing from the session tenant → 403.
  The legacy admin token is a global-admin credential: it may address any
  tenant explicitly, but it is OFF by default after this wave (see F-05-05).
  Single-resource reads of another tenant's row → 404 (no existence oracle).
- **Legacy admin token (F-05-05).** `auth.legacy_admin_enabled()` now requires
  explicit `AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN=1`. `create_app(admin_token=...)`
  with an explicitly passed argument still enables it for that app instance
  (explicit configuration — this is what every existing test does); the env
  var alone no longer enables it. Startup logs a warning when enabled.
- **mTLS (F-05-06).** `require_device_mtls` is called on ALL agent routes
  (heartbeat, whoami, ws, policy get/put, rotate, mtls-fingerprint is admin,
  tasks/next, execution-state, result, agent webrtc routes). Proxy fingerprint
  headers remain the mechanism (existing test `test_device_mtls_gate` asserts
  a matching `X-Client-Cert-Fingerprint` header passes); the trusted-edge
  requirement is documented in `device_mtls.py` — the edge must strip
  client-supplied values. No new flag (a flag would break the tested contract).
- **Workflow approval (R-P0-11).** Steps run with the WORKFLOW OWNER's policy
  (effective device policy / global policy for local steps), never admin.
  Tier>=3 steps require per-step approval by default unless the workflow was
  approved as a whole by an admin (`WorkflowIn.workflow_approval_id` verified
  against an `approved` approval row for tool `execute_workflow`, unexpired,
  decided by an admin). An approved step runs with `approval_granted=True,
  is_admin=True` (the approval itself was decided by an admin — this is the
  same authorisation model as approved tasks in `dispatch()`).
- **Terminal (R-P0-04).** The `term-*` auto-approval branch is deleted.
  `_remote_terminal_exec` creates tasks via `task_service` with
  `human_initiated=True` (the human at the terminal IS the operator) and
  waits through `needs_approval` until its 40 s deadline, then returns a
  clear approval-required error instead of executing.
- **Takeover (SEC-08).** Enforced at submission (`task_service`), at claim
  (`next_task` refuses to hand out non-human-initiated queued tasks while
  takeover is active), in workflow/scheduler steps, and in the WebRTC input
  path (Wave 1B, already done). The agent poller path is covered plane-side
  by the claim gate (agents/* is out of this wave's file scope).
- **Epoch/fencing (DIST-05/F07-04).** `tasks.epoch` (migration 013) is bumped
  at claim (`epoch=epoch+1`); the claim returns it; `task_result` accepts an
  optional `epoch` and its UPDATE requires `status='running' AND epoch=?`
  (rowcount 0 → 409 stale/late result). Agents that do not send an epoch are
  still protected by the `status='running'` guard.
- **Local execution (R-P0-03).** Default OFF. Enabled only with
  `AI_BRIDGE_ALLOW_LOCAL_EXECUTION=1` + admin role; Tier>=3 additionally
  requires a valid approved approval record (`AIExecuteIn.approval_id`),
  otherwise a `needs_approval` response is returned (approval row created).
  Every local execution is audited. Otherwise execution goes through the
  task queue to an enrolled device.
- **Fleet updates (R-P0-16).** `tools.update_fleet_agent` refuses unless a
  signed release is configured (`AI_BRIDGE_UPDATE_MANIFEST_PATH` +
  `AI_BRIDGE_UPDATE_SIGNATURE_PATH` + `AI_BRIDGE_UPDATE_PUBLIC_KEY`); the
  manifest signature is verified BEFORE any fetch and the checkout pins the
  manifest's exact commit SHA with a rollback snapshot. `magic-install.sh`
  re-runs are NOT in this wave's file scope — recorded as a remaining
  follow-up in §5 (the installer scripts must call the new
  `updates.py` helpers; the helpers exist and are tested).
- **CSRF (F-05-02).** Middleware is on by default; explicit
  `AI_BRIDGE_CSRF_DISABLED=1` opts out. Secret resolution is delegated to
  the DB child (`csrf.get_csrf_secret()` from the secrets backend, then env,
  then per-process random with a warning).
- **Audit (DB-05/R-P0-06).** `db.audit()` rewritten atomically by the DB
  child; this wave wires the MCP gate's audit DB path by default
  (`AI_BRIDGE_MCP_AUDIT_DB` setdefault to the control DB in `create_app`)
  and adds `GET /api/audit/verify` (admin) over `db.verify_audit_chain`.
- **Unrestricted grants (R-P0-02 control half).** `enroll()`,
  `update_device`, and `set_device_policy_api` (admin paths) stamp
  `unrestricted_granted_by=<actor>` and write an audited
  `policy.unrestricted_granted` event via `audit_writer` when an admin
  explicitly sets `unrestricted=True`. Device-token paths cannot widen
  (`reject_policy_widening` unchanged). `task_service` builds the `Policy`
  with `unrestricted_granted_by` only from that stamped field.

## 3. Per-task record

(to be filled as tasks land; status: PENDING / IN_PROGRESS / IMPLEMENTED / TESTING / VERIFIED / COMPLETED / BLOCKED / DEFERRED)

| ID | Status | Files changed | Notes |
|---|---|---|---|
| R-P0-04 | VERIFIED | `control/app.py` (`next_task`, `_remote_terminal_exec`) | `term-*` auto-approval branch deleted; terminal tasks claim with `approval_granted=False` until approved; `_remote_terminal_exec` routes via `task_service` (`human_initiated=True`) and returns an approval-required error at its 40 s deadline instead of executing. Test: `test_term_task_without_approval_not_auto_approved`, `test_tier3_task_claimed_only_after_approval`. |
| R-P0-10 | VERIFIED | `control/task_service.py` (new), `control/app.py` (`submit_task`), `control/v1_routes.py` (`v1_create_task`, list/get/audit tenant filters) | ONE task-creation service: takeover → durable e-stop → tenant-scoped device lookup → `decide()` (pure policy) → approval row + `needs_approval` → idempotent insert → audit → notify. Both `/api/tasks` and `/api/v1/tasks` route through it; v1 no longer hard-codes tenant `"default"` (returns `status`/`tier`/`approval_id`). Tests: v1 Tier-3 → `needs_approval`, never claimed. |
| R-P0-11 | VERIFIED | `control/app.py` (`_execute_workflow_step_sync` rewrite, `_run_workflow_worker`, `submit_workflow`, `retry_workflow`, scheduler `__workflow__` branch, `_evaluate_device_triggers`) | Steps run with the workflow OWNER's policy (effective device policy / global policy for local steps), never admin; `approval_granted`/`is_admin` only from a recorded approval (per-step or whole-workflow). Tier>=3 requires approval by default (`step_needs_approval = (not workflow_approved) and (require_approval or tier>=3)`); whole-workflow approval verified via `task_service.verify_workflow_approval` (approved `execute_workflow` row, unexpired, decided by an admin; invalid id → 400). Tests: Tier-3/Tier-4 steps without approval → `waiting_approval` → deny → `failed`, nothing executed; Tier-1 step completes; whole-workflow-approved Tier-3 step executes. |
| R-P0-12 | VERIFIED | `control/app.py` (tenant resolution on every list/read/write; `_require_row_tenant` + new `_require_device_tenant`), `control/v1_routes.py`, `control/ai_api.py`, `control/auth.py`, `control/production_readiness.py`, `migrations/013_tenant_isolation.sql` + `down_013_tenant_isolation.sql` (DB child), `control/db.py` SCHEMA (DB child) | `users`/`sessions`/`approvals` gain `tenant` (migration 013, data preserved under `"default"`); tenant derived from the session server-side; caller-supplied tenant differing → 403; single-resource cross-tenant read → 404. All request-input tenant defaults changed `"default"` → `None` (a literal default was treated as an explicit caller tenant and broke non-default session users — found by smoke test). Follow-up fix in this wave: tenant checks added to 16 desktop/terminal/WebRTC/mTLS routes (`_require_device_tenant` helper; skips pseudo-device `"local"`); `create_user` tenant resolution moved out of the bare-`except` INSERT block (it was swallowing the 403 as "Username already exists"); `get_device_monitors` `capabilities_json` list-vs-dict 500 fixed. `production_readiness.py:87-92` stale `AI_BRIDGE_HTTP_ALLOW_ANON` check is now a warning. Tests: 8 tenant-isolation tests incl. cross-tenant list/read/write/desktop/terminal. |
| R-P0-03 | VERIFIED | `control/ai_api.py` (`execute_ai_tool`) | Local execution OFF by default: non-read-only local tools require admin role + `AI_BRIDGE_ALLOW_LOCAL_EXECUTION=1`, else audited 403; Tier>=3 additionally requires a valid approved approval record (else `needs_approval` response + approval row created). Pure DB reads (`list_devices`, remote `get_device_status`) stay available to operators. Tests: local `run_shell` → 403; local `list_devices` → 200 (also exposed and fixed the `tags_json` SELECT bug, see below). |
| R-P0-16 | VERIFIED (child) | `updates.py` (+178), `tools.py` (update_fleet_agent only), `control/app.py` (endpoint pre-flight + task routing) | Signed pinned-git update API: `signed_update_configured()`, `load_signed_manifest()`, `apply_pinned_git_update(repo_dir, manifest, signature_b64, public_key_path)` — signature verified BEFORE any fetch, 40-hex commit pin, rollback snapshot at `~/.ai-bridge/update-rollback.json` (previous commit + branch) written before checkout, HEAD verified against the pin, rollback on failure. `tools.update_fleet_agent` raises PermissionError when unconfigured (fail closed, no unsigned fallback); new optional `repo_dir` kwarg (default cwd). Endpoints refuse with 409 unless configured. Tests: 7 new in `tests/unit/test_signed_updates.py` (valid → pinned SHA checkout; tampered manifest/signature → PermissionError, no checkout; missing config → PermissionError; unavailable pin → refused + rolled back; short commit → ValueError) + `test_fleet_update_endpoints` updated to configure a signed release. Follow-up: plane endpoints rely on default `repo_dir` (process cwd) — pass the repo path explicitly if the plane doesn't run from the repo root. |
| SEC-08 | VERIFIED | `control/task_service.py`, `control/app.py` (`next_task`, workflow/scheduler), `control/v1_routes.py`, `control/app.py` agent WebRTC route | Takeover checked at submission (task_service), at claim (`next_task` returns `{"task": None, "human_takeover_active": True}` for non-human-initiated tasks; human-initiated tasks still flow), in workflow/scheduler steps, and `GET /api/agent/webrtc/{id}` returns `mode`/`permissions`/`human_takeover_active` (Wave 1B blocker B-1). Tests: `test_takeover_blocks_claim_but_not_human_initiated`. |
| DB-05 / R-P0-06 wiring | VERIFIED | `control/db.py` (DB child), `control/app.py` (MCP audit DB setdefault in `create_app`, `GET /api/audit/verify`) | Atomic audit insert (hash + insert in one transaction) by DB child; gate audit DB defaults to the control DB (`AI_BRIDGE_MCP_AUDIT_DB` setdefault); `GET /api/audit/verify` (admin) over `db.verify_audit_chain`. Test: `test_audit_chain_verify_endpoint` → `{"ok": true, "checked": 4, ...}`. |
| SEC-06/DB-06 | (delegated) | | see wave2a-db-progress.md — VERIFIED there (Fernet, `"fernet1:"` prefix, secret-read audit, no `SELECT *`). |
| SEC-07 | VERIFIED (child) | `tools.py` (`_run_argv` / `_clean_child_env`) | Child env is now a minimal allowlist (`PATH, HOME, LANG, LC_ALL, TZ, TERM` + Windows vars), matching `execution.py`; `DATABASE_URL`, vault/TURN/webhook/OAuth and all `AI_BRIDGE_*` secrets dropped. `_SCRUB_ENV_KEYS` kept as a documented, now-unused constant. Applies to `run_shell` and `terminal_exec`. Tests: env-scrub assertions in `test_signed_updates.py`. |
| DIST-01 | VERIFIED | `control/app.py` (`_scheduler_loop`) | Atomic claim: `UPDATE schedules SET last_run_at=?, next_run_at=? WHERE id=? AND next_run_at<=? AND enabled=1`, rowcount != 1 → skip. Test: `test_scheduler_atomic_claim_exactly_one_winner` (two claim attempts, exactly one wins). |
| DIST-05 / F07-04 | VERIFIED | `control/app.py` (`next_task` epoch bump, `task_result`), migration 013 `tasks.epoch` (DB child) | `epoch` bumped at claim and returned; `task_result` UPDATE requires `status='running' AND epoch=?` (rowcount 0 → 409). Tests: stale epoch → 409, correct epoch → 200, second result → 409. |
| TST-15 | VERIFIED | `control/oidc.py` (dist child), `control/app.py` (durable e-stop refresh at submit/v1/workflow/scheduler) | OIDC `_PENDING` moved to shared `settings` table when bound (`oidc.bind_oidc_store(conn)` wired in `create_app`); e-stop read via `plane_state` durable flag at every submission/claim/workflow/scheduler entry point instead of in-memory `app.state.emergency_stop`. |
| F-05-02 | VERIFIED | `control/app.py` (`create_app`), `control/csrf.py` (DB child) | CSRF middleware ON by default (opt-out `AI_BRIDGE_CSRF_DISABLED=1`); secret from secrets backend → env → per-process random with warning (`csrf.get_csrf_secret()`). Test: session POST without `X-CSRF-Token` → 403; with minted token → 200. |
| F-05-05 | VERIFIED | `control/auth.py`, `control/app.py` | Legacy admin token OFF by default: `legacy_admin_enabled()` requires explicit `AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN=1`; explicitly passed `admin_token=` still enables it for that instance (all existing tests). Tests: env token without opt-in → 401; with opt-in → 200. |
| F-05-06 | VERIFIED | `control/app.py` (mTLS calls on all agent routes), `control/v1_routes.py` (agent heartbeat) | `require_device_mtls` now called on heartbeat, whoami, ws, policy get/put, rotate, tasks/next, execution-state, result, agent WebRTC routes, and v1 agent heartbeat. Proxy fingerprint headers remain the mechanism (existing `test_device_mtls_gate` contract preserved); trusted-edge requirement documented. |
| F-05-07 | (delegated) | | see wave2a-dist-progress.md — VERIFIED there (per-task contexts, `TaskContext` registry + `ContextVar`). |
| F-05-10 | (delegated) | | see wave2a-db-progress.md — VERIFIED there (vault/aws fail closed unless `AI_BRIDGE_SECRETS_ALLOW_ENV_FALLBACK=1` + warning). |
| F08-08 | IMPLEMENTED (child, inspection-only) | `windows-agent/installer/install-service.ps1` (+61/−8) | DPAPI sealing via .NET ProtectedData (LocalMachine scope, `aib-dpapi-v1:` wire format — the same format the Python agent already unseals), round-trip unprotect verification before writing, fails loudly (never plaintext fallback); config ACL now SYSTEM + Administrators only (installing-user Read grant removed). Verification is inspection-only (no Windows host, no pwsh here). BLOCKER found by the child: the C++ readers (`windows_service.cpp:LoadServiceConfig`, `desktop_agent.cpp:main`) never call `DpapiSecretStore::UnprotectSecret` — sealed installer output breaks native auth until they strip the prefix + unseal (C++ files, outside this wave). Also: the standard-user desktop agent can no longer read the SYSTEM+Admins-only config — reconcile before deploying to non-admin desktops (do not silently re-add a broad Read grant). |
| PERF-01 | DEFERRED | — | see §1 |
| PERF-02 | (delegated) | | see wave2a-dist-progress.md — VERIFIED there (Redis-first; DB path only when no Redis URL). |
| SEC-05 | VERIFIED | `control/app.py` (`apply_audit_retention`) | WORM-gated retention: if a WORM/SIEM URL is required but unset → 409; if set, every row must export successfully via `emit_audit_event` or the delete is refused with 502. |
| R-P0-02 control half | VERIFIED | `control/app.py` (enroll/update_device/set_device_policy_api), `control/task_service.py` | Admin paths stamp `unrestricted_granted_by=<actor>` + audited `policy.unrestricted_granted` event when explicitly setting `unrestricted=True`; `task_service` passes `unrestricted_granted_by` through only from that stamped field, so `decide()` honours it only for genuine admin grants. |

### Bugs found and fixed by the Wave 2A tests (in-scope files)

- `dispatch.py:73` / `ai_api.py:1398` — `list_devices` selected `tags_json FROM devices`, but the column lives in `device_metadata` (migrations 004/012); the MCP `list_devices` tool and the ai_api fleet listing 500'd on any fresh DB. Fixed with `LEFT JOIN device_metadata ... COALESCE(m.tags_json, '[]')`, matching the pattern already used in `app.py:1220`.
- `app.py` `get_device_monitors` — `capabilities_json` defaults to `'[]'` (a JSON list); `caps.get("monitors")` raised `AttributeError` → 500 for every enrolled device. Fixed by normalising non-dict `caps` to `{}`.
- `app.py` `create_user` — tenant resolution ran inside the bare-`except` INSERT block, so a cross-tenant 403 was misreported as 400 "Username already exists". Resolution moved before the try-block.
- 16 desktop/terminal/WebRTC/mTLS routes had no tenant check (found by route audit): `get_desktop_frame`, `get_device_desktop_live_jpeg`, `send_desktop_input`, `get_device_monitors`, `create/get/post-answer/post-candidates/end` WebRTC session routes, `create/list/stream/input/resize/delete` terminal routes, `set_device_mtls_fingerprint`. All now call `_require_device_tenant` (new helper; skips pseudo-device `"local"`; missing row left to the handler's own handling).
- Note: there is NO `GET /api/devices/{device_id}` API route — the dashboard SPA serves that path (200 HTML). Single-device reads are `/overview` / `/policy` / `/telemetry` (all tenant-checked).

## 4. Tests executed

Baseline (verified in a clean worktree of commit `726bed2`, same venv,
17 Nov 2026): **467 passed, 5 skipped, 12 failed** — the 12 are the known
pre-existing failures (Windows DLL/clipboard/WebRTC ×4, BetterCam, Firecrawl,
screen-capture dimensions, ruff linter, legacy-WebRTC deprecation header,
connectors/firecrawl-browserbase ×2, httpx client reuse).

New Wave 2A tests (28, all passing):
- `tests/unit/test_p0_control_plane.py` (14): term-* without approval →
  `approval_granted` False; Tier-3 claimed only after approval; v1 Tier-3 →
  pending, never executed; local execution off by default (403) / read-only
  DB query still works; fleet update refused without signed config (409);
  legacy admin token off by default (401) / explicit opt-in (200); CSRF
  required for session POSTs by default / minted token accepted; audit-chain
  verifier endpoint; takeover blocks claim but not human-initiated tasks;
  stale-epoch result → 409, correct epoch → 200, second result → 409;
  scheduler atomic claim (exactly one winner); migration 013 preserves data
  under `"default"`.
- `tests/unit/test_tenant_isolation.py` (9): cross-tenant list → 403 /
  default-scoped; cross-tenant single read → 404; cross-tenant write → 403/404;
  session tenant in login + `/me`; legacy admin may address any tenant;
  user creation tenant-scoped; v1 cross-tenant; session user sees only own
  tenant; cross-tenant desktop/terminal → 404, own-tenant desktop → 200.
- `tests/unit/test_workflow_approval.py` (5): Tier-3 step without approval →
  `waiting_approval` → deny → `failed`, nothing executed; Tier-1 step
  completes; whole-workflow-approved Tier-3 step executes; invalid
  `workflow_approval_id` → 400; Tier-4 step without approval → denied/failed.
- `tests/unit/test_signed_updates.py` (7, updates child): valid signed
  manifest → pinned-SHA checkout; tampered manifest/signature → refused;
  missing config → refused; unavailable pin → refused + rolled back; short
  commit → ValueError; child env allowlist (no secrets).

Full suite (17 Nov 2026, after all fixes below): **502 passed, 5 skipped,
12 failed** — the 12 failures are exactly the Wave 1 baseline set, verified
pre-existing in the clean `726bed2` worktree. Net vs baseline: +35 passing
(28 new Wave 2A + 7 signed-update tests), zero new failures.

Failures found by the suite during this wave and how they were resolved
(all in-scope; no test was deleted and no security check was disabled):
- `broadcast_fleet_command` NameError (`task_id` undefined after the
  task_service routing) — fixed in `app.py` to use `created["task_id"]` /
  `created["status"]`.
- `ai_agent` smart-intent listed no devices — `run_autonomous_agent` never
  resolved `body.tenant` (model default changed to `None`); fixed in
  `ai_api.py` with the same `model_copy` resolution as the other endpoints.
- v1 WebRTC session task never claimed — a `human_control` session sets the
  takeover flag, but the stream-start task wasn't marked human-initiated;
  fixed by marking the task `human_initiated: True` in both queueing paths
  (`v1_routes.py` session creation, `app.py` `_queue_webrtc_task`), which
  also fixed `_queue_webrtc_task`'s hardcoded tenant `"default"`.
- `dispatch`/`ai_api` `list_devices` 500 (`tags_json` is a `device_metadata`
  column, not a `devices` column) — fixed with a LEFT JOIN in both places.
- `get_device_monitors` 500 (`capabilities_json` defaults to a JSON list) —
  fixed by normalising non-dict `caps` to `{}`.
- `create_user` swallowed cross-tenant 403 as 400 "Username already exists"
  (tenant resolution inside the bare-`except` INSERT block) — resolution
  moved before the try-block.
- 16 desktop/terminal/WebRTC/mTLS routes had no tenant check (route audit) —
  `_require_device_tenant` added to all of them; `send_desktop_input` also
  resolves the tenant (was hardcoded `"default"`) and maps a policy denial
  to a clean 403 instead of an unhandled 500.
- Existing tests asserting the old insecure behaviour were updated to the
  specified new contract (not deleted): Tier-4 auto-queue → approval flow
  (`test_device_policy.py` ×2); local execution → opt-in flag
  (`test_ai_execute_local_tool`); fleet update 200 → signed-release fixture
  (`test_fleet_update_endpoints`); desktop input → desktop + Tier-3 opt-in
  env (`test_dual_control_desktop_input_normalized`); readiness codes
  `admin_token_unset`/`anon_mcp_enabled` → `admin_access_unset` /
  `stale_anon_config` warning (`test_phase8_deep.py`); MCP OAuth tests set a
  Fernet key (`test_mcp_oauth2.py` ×2, SEC-06 fail-closed); audit-file tests
  clear `AI_BRIDGE_MCP_AUDIT_DB` (`test_p0_policy.py`, same pattern the file
  already used); dashboard telemetry test claims tasks before reporting
  results (DIST-05 `status='running'` guard); integration workspace tests
  mint CSRF tokens for session requests (F-05-02).

## 5. Remaining / blocked / deferred

- `magic-install.sh` / `deployment/linux/magic-install.sh` re-runs: out of this
  wave's file scope; the signed-update helpers exist in `updates.py` — a
  follow-up must make the installers call them instead of `git checkout`.
- PERF-01 (connection pool): deferred — needs a connection-checkout refactor
  and a load-test harness to measure first.
- C++ / Windows-only verification (installer DPAPI sealing): inspection-only
  in this Linux environment (see updates child report).
- Pseudo-device `"local"` (the control-plane host) is NOT tenant-scoped by
  `_require_device_tenant` — it is not a DB row. Local desktop/terminal
  access is governed by role + the R-P0-03 local-execution gate instead.
  Documented in the helper's docstring; a follow-up could bind the plane
  host to an explicit tenant.
- v1 mTLS beyond the agent heartbeat not done (v1 device routes other than
  heartbeat do not call `require_device_mtls`).
- Postgres live verification not done for migration 013 (SQLite only;
  `ADD COLUMN IF NOT EXISTS` is valid Postgres, but no Postgres instance
  was available).
- `ai_api.py` / `custom_agents.py` still have a `SELECT * FROM mcp_servers`
  (per the DB child — should move to the explicit loaders later).
- `ratelimit.py`'s async Redis path is effectively unreachable from the
  async middleware (sync wrapper detects a running loop → memory, DIST-07);
  needs an async-native middleware limiter — not fixed in this wave.
- Updates child (`d0cdf148`) COMPLETED — see `analysis/wave2a-updates-progress.md`
  and the R-P0-16 / SEC-07 / F08-08 rows in §3.
- F08-08 deployment blocker: C++ native readers cannot yet unseal the
  DPAPI-sealed installer config (see row above); do not deploy the sealed
  installer to native fleets until that lands and is tested on Windows.
- Signed-update `repo_dir`: plane endpoints rely on the process cwd default;
  pass the repo path explicitly if the plane doesn't run from the repo root.

## 6. Rollback

Revert the Wave 2A commit(s); migration 013 has a down migration
(`down_013_tenant_isolation.sql`). Data written under tenant `"default"`
is preserved by the down migration.

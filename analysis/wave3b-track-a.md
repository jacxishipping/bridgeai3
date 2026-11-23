> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 3B Track A — app.py APIRouter split: progress

Owner: Track A subagent. Repo: `~/workspace/ai-bridge-main`, base commit
`19747b2`. Files owned: `src/ai_bridge/control/app.py`,
`src/ai_bridge/control/routers/*.py` (new),
`tests/unit/test_architecture_phase4_routers.py` (new), this file.
No other files touched. No commits, no pip installs, no full-suite runs
(parent runs the full suite at the end).

## Status: DONE — all 9 routers extracted, snapshot identical, SSRF + docstring fixed, cleanup done, targeted tests green (see §3)

## 1. Route snapshot (OV-02 / F-05-11)

- Baseline captured FIRST from the committed tree via a clean worktree
  (`git worktree add /tmp/aib-19747b2 19747b2`, worktree removed afterwards):
  **220 (method, path) pairs**. Current working tree before my edits had
  222 = 220 + SPA `GET /{full_path:path}` + `/assets` mount (dashboard
  `dist/` exists in the real repo but not in the worktree).
- `tests/unit/test_architecture_phase4_routers.py` embeds the 220-pair
  literal and asserts the refactored app exposes the identical set,
  adding the two SPA entries only when `apps/dashboard/dist` exists
  (mirroring `create_app()` logic).
- **Result: identical — YES.** After every router extraction the live
  route set diffed empty against the pre-edit set (222 incl. SPA).
- FastAPI 0.163.1 / Starlette 1.10.0 quirk: `app.include_router()` appends
  an `_IncludedRouter` bookkeeping entry to `app.routes`; the included
  routes are NOT copied into `app.routes` (they resolve at request time
  via `original_router`). The snapshot collector (test + scratch script)
  recurses into `original_router.routes`; entries without a path are
  skipped. Request-time behaviour is unaffected (smoke tests pass).
- Pre-existing, NOT caused by this track: `GET /openapi.json` returns 500
  on the committed tree too (verified in the 19747b2 worktree;
  `PydanticInvalidForJsonSchema: Cannot generate a JsonSchema for
  core_schema.CallableSchema`). `/docs` returns 200.

## 2. Routers created (145 handlers moved; 2 middleware + SPA stay in app.py)

`app.py`: 5,538 → 2,093 lines. Only `@app` route left is the SPA catch-all
`/{full_path:path}`; middleware, CSRF/CORS setup, `ai_api`/`v1_routes`/
`mcp_routes`/custom-agent installs, scheduler + workflow-worker threads,
and all shared helpers stay in `app.py`.

| Router | File | Handlers | Notes |
|---|---|---|---|
| health | `routers/health.py` | 8 | installers, /health*, /metrics, /api/metrics/summary |
| devices | `routers/devices.py` | 17 | enroll/list/policy/overview/refresh/telemetry/fleet health+caps/version/device update/mtls |
| tasks | `routers/tasks.py` | 14 | tasks CRUD + approvals (incl. quick-approval HTML) |
| audit | `routers/audit.py` | 5 | /api/audit*, verify, retention |
| admin | `routers/admin.py` | 27 | emergency-stop, webhook settings, policies, connections, logs, events/stream, alerts, backups, mcp tool-policies, tenants |
| workflows | `routers/workflows.py` | 31 | templates, workflows, supervisor, roles, activity, fleet broadcast, triggers, schedules, fleet update |
| webrtc | `routers/webrtc.py` | 20 | desktop frame/input/monitors, webrtc sessions (device + agent), terminal sessions |
| auth | `routers/auth.py` | 14 | csrf-token, login/me/logout, stream-ticket, OIDC, M365, users |
| agent | `routers/agent.py` | 9 | heartbeat, whoami, ws, agent policy, rotate, tasks/next, execution-state, result |

Supporting files: `routers/_context.py` (`ControlContext` dataclass, 41
fields mirroring `create_app()` locals: `conn`, `app`, `token`,
`admin_token`, `db_path`, `device_ttl_days`, `backup_dir` + helper
callables), `routers/__init__.py` (light on purpose — no submodule
imports, to avoid import cycles; `app.py` imports factories lazily
inside `create_app()`).

Mechanics (see `/tmp/extract_router.py`, the AST extractor used):
- Handler bodies moved verbatim; only `@app.` → `@router.` and relative
  imports gain one level (`from . import x` → `from .. import x`,
  `from .. import x` → `from ... import x`) because routers live one
  package deeper. Per-handler free variables were computed with
  `symtable` (+ signature/decorator names) and classified into ctx
  bindings / `from ..app import …` (models, `make_quick_sig`,
  `send_webhook_notification`, …) / direct sibling-module imports.
- Only two cross-handler calls exist, both kept intra-router:
  `set_device_policy_api` → `get_device_policy_api` (devices);
  `update_fleet` → `broadcast_fleet_command` (workflows, which is why
  fleet update lives in the workflows router).
- Routers are mounted in original first-appearance order
  (health, devices, tasks, audit, admin, workflows, webrtc, auth, agent)
  in one include block just before the SPA mount; order-sensitive pairs
  (workspace `/api/tasks/history` vs `/api/tasks/{task_id}`,
  `/api/workflows/templates` vs `/api/workflows/{workflow_id}`) keep
  their precedence (workspace installs first; templates precede `{id}`
  inside the workflows router).

## 3. Targeted tests run (exact counts)

All runs used `.venv/bin/python -m pytest … -q`. Only the factories test
failed while routers were still being created (expected); it passes once
all 9 exist.

- Baseline (pre-edit): new file snapshot+smoke subset 8 passed;
  SSRF/docstring/factories subset 8 failed, 3 passed (expected pre-change).
- health: new file + `test_p0_control_plane.py` → 30 passed, 3 failed
  (snapshot tests, pre-`_IncludedRouter` collector fix).
- After `_IncludedRouter` collector fix: snapshot/smoke 7 passed.
- devices: new file + `test_device_policy.py` + `test_device_dashboard.py`
  + `test_workspace.py` → 31 passed, 1 failed (factories, expected).
- tasks: new file + `test_phase1_approvals_and_vision.py` +
  `test_workflow_approval.py` + `test_tenant_isolation.py` →
  38 passed, 1 failed (factories, expected).
- audit+admin: new file + `test_feature_batch.py` +
  `test_observability_alerts.py` + `tests/test_control_plane.py` →
  57 passed, 1 skipped, 1 failed (factories, expected).
- workflows: new file + `test_phase2_workflows.py` +
  `test_workflow_approval.py` + `test_orchestration_phases.py` →
  40 passed, 1 failed (factories, expected).
- webrtc: new file + `test_webrtc_phase3.py` + `test_webrtc_viewonly.py`
  + `test_phase4_streaming_and_updater.py` + `test_stream_ticket.py` →
  72 passed, 1 failed (factories, expected).
- auth+agent batch (new file + `test_mcp_oauth2.py` +
  `test_p0_control_plane.py` + `test_phase3_fleet_ops.py` +
  `test_stream_ticket.py`): **52 passed** (first attempt hit
  `sqlite3.OperationalError: database or disk is full` because `/tmp`
  (512M tmpfs, shared with siblings) filled to 100%; re-run with
  `TMPDIR=$HOME/workspace/tmp-pytest` passed clean).
- Broader targeted sweep (20 files, incl. `test_p0_mcp_gate`,
  `test_execution`, `test_ai_api`, `test_phase7/8/9_deep`,
  `test_phases_456_deep`, `test_device_routing`, `test_signed_updates`,
  `tests/test_security.py`, `tests/test_tasks.py`, run twice):
  **165 passed, 1 skipped, 7 failed — all 7 verified pre-existing by
  running them on a pristine 19747b2 worktree, where they fail
  identically**: `test_subprocess_linter_runs_cross_platform`,
  `test_http_client_reused_across_api_calls`,
  `test_screen_capture_desktop_dimensions`,
  `test_connectors_fail_without_api_key`,
  `test_dispatch_registers_firecrawl_and_browserbase_tools`,
  `test_concurrent_claim_and_cancel_under_load` (timing assertion,
  5.88s vs 5.0s under shared-CPU load),
  `test_legacy_webrtc_session_deprecation_header` (handler raises 400
  on Linux before the injected-Response deprecation header survives;
  same behaviour on the committed tree).
- Final new-file run after all edits: **19 passed**.

## 4. SSRF hardening — app.py sender only (SEC-11 / F-05-17)

- `_validate_webhook_url(url, *, strict_dns=False)`: scheme/host checks,
  loopback/metadata hostname blocklist (added bare `metadata` and
  `*.internal`), literal-IP check, and — new — DNS resolution of the
  hostname with EVERY resolved address checked against an explicit
  blocklist (loopback, RFC1918, link-local incl. 169.254.169.254, CGNAT
  100.64/10, multicast, reserved, documentation, ULA/Teredo/6to4,
  IPv4-mapped IPv6 unwrapped). Explicit list instead of
  `ip.is_private/is_global` on purpose: this sandbox's resolver answers
  public hostnames with 198.18.0.0/15 (benchmarking), which `ipaddress`
  reports as private — a naive `is_private` check would break the
  existing Discord/Telegram webhook tests. 198.18/15 is not an
  internal-service range, so it is deliberately not blocked (documented
  in code). DNS failure: fail closed when `strict_dns=True` (sender),
  lenient at config time (handlers call with default `False`; the sender
  re-validates strictly before every send).
- `send_webhook_notification()`: validates with `strict_dns=True`;
  `httpx.Client(..., follow_redirects=False)` + manual redirect loop,
  max 3 hops, each target re-validated (DNS + blocklist) before
  following; 301/302/303 switch to GET, 307/308 repeat the POST.
  Residual TOCTOU note: the request goes to the hostname (not a pinned
  IP literal) — a custom httpx transport pinning the resolved IP was out
  of scope; validation happens immediately before the send and on every
  redirect hop.
- Tests (in the new test file): resolved-private rejected, metadata-IP
  rejected, literal private IP rejected, DNS failure rejected by sender,
  redirect-to-private-IP not followed, redirect-to-DNS-rebinding host
  not followed, redirect-to-public followed (positive control), redirect
  limit (initial + 3 hops, 4th refused), config-time rejection via
  `POST /api/settings/webhook`. New file: 18/19 pass (only factories
  test failed pre-completion).
- Existing webhook tests still pass:
  `test_send_webhook_notification_formatting`,
  `test_webhook_settings_and_approval_trigger`,
  `TestWebhookSettings` (incl. localhost → 400): 4 passed.

## 5. Docstring fix (F-05-11)

Module docstring said "Rate limiting: 120 requests/minute per token/IP".
Actual middleware (`rate_limit_middleware`): 30/min per IP on auth
endpoints, 1200/min per authenticated token, 300/min per unauthenticated
IP, 60 s windows. Docstring now states the real numbers. Covered by
`test_rate_limit_docstring_matches_middleware`.

## 6. Remaining / blocked

- [x] Auth+agent targeted batch: 52 passed (see §3).
- [x] Broader targeted sweep: 165 passed, 7 failed, all 7 verified
  pre-existing on the pristine 19747b2 tree (see §3).
- [x] Cleanup in `app.py`: deleted the 4 now-unused create_app-level
  imports (`fleet_healthmod`, `live_activitymod`,
  `desktop_capabilitiesmod`, `supervisormod`); repaired the include-block
  END marker line (the extractor's `re.sub` regeneration had accumulated
  the trailing dashes; extractor fixed to consume the rest of that line);
  pruned now-unused module-top imports in `app.py` (`secrets`,
  `urlencode`, `Body`, `Header`, `Query`, `Response`, `WebSocket`,
  `WebSocketDisconnect`, `HTMLResponse`, `StreamingResponse`,
  `FileResponse`, `terminal_manager`, `redact`, `make_rate_limiter`,
  `generate_csrf_token`, `validate_csrf_token`) — each verified unused
  by grep/AST scan before removal; new file re-run 19 passed afterwards.
- [ ] `/tmp` is 97–100% full (shared tmpfs; siblings' pytest/parity
  dirs). Workaround: run pytest with
  `TMPDIR=$HOME/workspace/tmp-pytest`. The 19747b2 worktrees used for
  the snapshot and for the pre-existing-failure checks were removed with
  `git worktree remove --force` after use.
- Nothing reverted. No behaviour change beyond the SSRF hardening and
  the docstring fix. `from ai_bridge.control.app import create_app,
  make_quick_sig, send_webhook_notification` still work (asserted by
  `test_public_imports_still_work`).
- Sibling files were not touched. Note for the parent: `git status`
  shows sibling tracks' modifications (e.g. `mcp_routes.py`,
  `ratelimit.py`, `alerts.py`, `worm_siem.py`, `workspace.py`,
  `db.py`); this track's files are `app.py`, `routers/`, the new test
  file, and this progress file.

# FINAL IMPLEMENTATION REPORT — AI Bridge Remediation

**Date:** 2026-11-17 · **Repo:** `~/workspace/ai-bridge-main` · **Baseline:** `79c010e` ("baseline: ai-bridge-main as received", zip sha256 `bed477c750c9b782891678d44efe156c4f7b8dad07e765a4f8b011298d282d5c`)
**Head:** `61faa67` (Wave 4) · **Diff vs baseline:** 217 files changed, +36,618 / −9,841 lines.

**Status legend.** **Closed** — fix in code + tests passing in this environment, no known residual for that row. **Partially closed** — the main half is fixed, but a named residual remains (a sub-clause unaddressed, a verification dimension missing, or a deliberate carve-out). **Open** — not addressed by any wave, or explicitly deferred without a fix. **Deferred** — explicitly deferred with a reason, or deliberately not implemented after evaluation. **N/A** — the register row's stated root cause was wrong/stale on re-check.

**Verification method for this report.** Wave progress files (`analysis/wave*-progress.md`) were extracted by two read-only research subagents and then spot-checked against HEAD code by the report author with `grep`/`git show` before any status was written here. Statuses marked "verified at HEAD" were confirmed in the current tree; statuses marked "per wave report" rest on the wave's own evidence (test names, file:line) which the author did not re-run. Where a wave's claim and the code disagreed, the code wins and the disagreement is recorded (§5). Full-suite numbers below are parent-verified (see `FINAL_TEST_RESULTS.md`), not wave claims.

## 1. Per-wave summary

### Wave 0 — setup (17 Nov 2026)
Git initialised; baseline commit `79c010e`. Python 3.12 venv (`.venv`) with fastapi 0.163.1, mcp, httpx, pytest installed from requirements.txt. Tracker files created. Pristine baseline suite: **389 passed / 12 failed / 5 skipped** (parent-verified).

### Wave 1 — Phase 0 + P0s (commit `726bed2`)
Three parallel tracks, disjoint files. **1A** (deployment/CI): Tier 3 default `1`→`0` in compose/CFN/render/deploy script; default TURN secret + dev admin token removed in favour of fail-fast `${VAR:?…}`; nginx `:80` → ACME-only + 301 redirect, `/metrics` + `/mcp` gain `auth_request /internal/verify`; `curl -k` removed from both `magic-install.sh` copies (+ `AI_BRIDGE_CA_BUNDLE`); CI runs pytest with published logs; `.env.example` rewritten; 18 new tests (`test_deployment_config.py`). **1B** (agents/C++/WebRTC): agent policy merge intersection-only; Linux agent restrictive default (ceiling 2, no `*`); fake 46-byte H.264 IDR deleted (`valid=false` + `failure_reason`); remote WebRTC view-only default-deny + takeover in input path; fabricated `encode_time_ms` → real value or `None`; shifted native-encode call deleted; DPAPI failure fails loudly; Linux token only in `device.token` (0600); GDI/COM lifetime fixes. **1C** (MCP/policy/audit): env-var auth/DNS off-switches deleted (escape hatch = `--unsafe-allow-anon` flag, refused in production); `unrestricted` only via audited `grant_unrestricted_policy()`; Tier 4 always requires approval; 5 missing tools tiered; one `_gated` wrapper on all 109 `@mcp.tool` wrappers (AST-verified 109/109); new `src/ai_bridge/audit_writer.py` (`record_tool_call`, `write_audit_event`; DB hash-chain + fsync JSONL fallback); `_oauth_bound_device()` fails closed; per-process stdio session key; approval pass-through on `write_file`/screenshots; pydantic validators per tool family; session-scoped 15-min single-use approvals. **Result (parent-verified): 467 passed / same 12 failed** (+78, zero new failures). **Known limits:** no Docker daemon (compose boot unproven), no nginx binary (`nginx -t` never run), no Windows toolchain (all C++ inspection-only), CI never executed on GitHub, P-11 open, `run-agent.sh` `AI_BRIDGE_INSECURE_TLS=1` hatch untouched, CFN UserData quoted-heredoc anomaly observed but untouched.

### Wave 2 — control plane + dashboard honesty (commit `3712b81`)
**2A** (control plane): single task-creation service `control/task_service.py` used by `/api/tasks`, `/api/v1/tasks`, `ai_api.execute_ai_tool`; `term-*` auto-approval deleted; workflow steps run as workflow owner with per-step approval default on; tenant isolation via migration `013_tenant_isolation.sql` (+ down migration; `users`/`sessions`/`approvals.tenant`, `tasks.epoch`); local execution off by default (admin + `AI_BRIDGE_ALLOW_LOCAL_EXECUTION=1` + approval); signed pinned-git fleet updates via `updates.py`; scheduler atomic claim; epoch-fenced results (stale → 409); CSRF on by default; legacy admin token off by default; mTLS on all agent routes; atomic audit chain + `GET /api/audit/verify`; Fernet encryption of MCP OAuth secrets; minimal clean child env (SEC-07); DPAPI-sealed native installer config; `list_devices`/`get_device_monitors`/`create_user` bugs fixed; 16 routes gained tenant checks. **2B** (dashboard/docs/integrations): demo off by default (login-first + interstitial); fabricated Merkle deleted; fake provider cards deleted; tokens session-only; Tier 1/2 tests rewritten against real modules; Tier 5 + challenger wired into `npm test`; hardcoded prod host `jxiptvplayer.app` removed from integrations; Ollama multi-turn loop; LangChain `args_schema`; docs sweep (README test counts corrected, Fluent 2 → JX-UI, SECURITY.md v0.3.x, single mTLS/OIDC/macOS statements, `ORIGINAL_REQUEST.md` citation removed). **Result (parent-verified): Python 502 passed / same 12 failed; dashboard 407/430** (23 stale demo-default expectations). **Deferred:** PERF-01 connection pool (needs checkout refactor + load harness). **Carve-outs:** pseudo-device `"local"` not tenant-scoped; signed-update `repo_dir` defaults to process cwd; v1 operator registry routes deliberately not mTLS-gated.

### Wave 2 fix-up (commit `19747b2`)
Dashboard suite green: **430/430 + 3/3 i18n** (21 stale expectations updated to the login-first contract, none deleted; real `Device360View` null-device bug fixed). D-08 closed: `POST /api/stream-ticket` (single-use, 60 s TTL, hash-only, tenant/device/session-bound) + WS redemption in v1_routes (4401 on replay/expired/wrong-device); `session_token=` removed from WS URLs; dashboard sends CSRF token on session POSTs. F08-08 closed: C++ `UnsealIfSealed()` in local_security; service + desktop agent fail loudly on unseal failure (inspection-only). Installers verify Ed25519 manifest before checkout or refuse. v1 device routes mTLS-gated. `SELECT * FROM mcp_servers` eliminated. **Result (parent-verified): Python 513 passed / same 12 failed; npm 430/430.** **Open:** C++ DPAPI readers need a Windows build + sealed-config E2E; signed-installer dry run with a test keypair not done (no signed release manifest exists); canonical migration `014_stream_tickets.sql` not done at that point (landed in Wave 3A).

### Wave 3 — Phase 2 + Phase 4 + Phase 5 foundations (commit `88ad591`)
**3A** (distributed + DB): Redis pub/sub fan-out hardening (once-per-process outage warning); async rate limiter (`allow_async`/`rate_limit_async`, sync wrapper warns instead of silently falling back to memory); agent progress heartbeat + `wait_for_device_notification` wake primitive; idempotency on all task paths + `prune_task_submissions` (TTL 86400); stuck-handoff sweeper; OIDC exchange-code primitives (`mint`/`redeem_oidc_exchange_code`, additive edit to `oidc.py`, outside its file list); migration runner rewrite (lexer splitter, per-file atomicity, down migrations, fail-fast); migrations **014** (stream_tickets) + **015** (FKs/CHECKs/handoff columns/indexes) with down migrations; schema parity script (`PARITY OK — 47 objects`); backup/restore CLI (`scripts/backup_restore.py`); retention helpers; fleet-health single query; bench script. Measured (SQLite, synthetic 50 devices/2,000 tasks): list pages 99% smaller, handoff lookup 16 ms → 0.7 ms, wake ~5 ms vs 150 ms spin bound, fleet health 51→1 query. 39 new tests. **3B** (architecture consolidation): `control/app.py` 5,538 → 2,093 lines via 9 domain `APIRouter`s (`routers/`: health, devices, tasks, audit, admin, workflows, webrtc, auth, agent; 145 handlers; route snapshot of 220 (method, path) pairs diffed empty); `tools.py` (3,042 LOC) → `tools/` package (11 files; exact 103-name public API verified, incl. a `ModuleType` patch-propagation shim 14 monkeypatching tests depend on); one tool registry (`tool_registry.py`, **149** entries — the brief's 144 was wrong: 61/9/76/3); one limiter (`control/limiter.py` fronting Redis/memory/DB, `ratelimit.py` now a thin shim); error taxonomy `src/ai_bridge/errors.py` (13 codes) wired into MCP `@_gated` and `mcp_routes.py`; `enforce_mcp_tool_policy()` on the previously ungated `POST /api/mcp/servers/{server_id}/execute`; structured redaction (all path components, `image_base64` validated, JWT pattern); SSRF validation in `alerts.py`/`worm_siem.py` (shared `security.validate_outbound_url()`); `Settings` cached (env-fingerprint keyed) + validated. 110 new tests. **3C** (agents/WebRTC/native/features): spool bounded (500 items / 64 MiB, 7-day TTL, 10 attempts) with `dead_letter_results` DLQ; graceful shutdown/drain (60 s, exit 2 on revocation); task execution on a worker thread + heartbeat on its own 30 s timer thread; 401 classification (revoked/expired/transient, backoff with jitter); DIST-08 progress POST (throttled 60 s, 404/501 tolerated); `simulate_agent_intent` refused by default; real RTCP ABR via aiortc `getStats()`; mouse-button hold tracking + release on disconnect; stale-session refusal after restart; ICE-gathering-complete gate; C++ persistent WinHTTP signalling, viewer ICE candidates applied, config-driven TURN, throttled signalling poll; secure-desktop rule unified; monitor geometry in C++ input; single-instance named mutexes; `assert()` → thread-safe `CHECK()` + new §[7/7] tests; hand-rolled JSON **kept with written justification**; `StartSession` returns false on encoder-init failure; proper `MCPClient` (`initialize()` negotiating 2025-06-18 → 2025-03-26 → 2024-11-05, `Mcp-Session-Id`, cursor pagination bounded 50 pages); DPoP/PAR **evaluated, deliberately NOT implemented** (gateway has no PAR endpoint or DPoP support — grep-verified); one shell contract (`ShellSpec` + `run_shell_command`, argv-only, scrubbed env); SIGTERM → grace → SIGKILL process-group escalation in `terminal_manager.py`; bounded terminal sessions (max 32, idle TTL 1800 s); browser sandbox fails closed; per-session locked browser contexts + `browser_evaluate` URL allowlist; SDK tokens never in URLs, redacted in errors/repr; `policy_simulation.py` dry-run `decide()` (no endpoint yet); SDK bounded retries + backoff/jitter + `Idempotency-Key` on `execute()`; `fleet_analytics.py` **not created** (deliberate — projection lives in `client.py`). 81 new tests. **Result (parent-verified): Python 743 passed / same 12 failed; dashboard 430/430.** **Overlap resolved:** 3A and 3B both edited `durable_ratelimit.py` — the final file contains both waves' changes (3A's async Redis code + 3B's docstring/interface note); 3B layered `limiter.py` on top of 3A's async functions; no overwrite occurred (verified in `git diff`).

### Wave 4 — follow-up wiring (commit `61faa67`)
Applied the Wave 3 follow-ups that were blocked on file ownership: WS `?token=` fallback removed from the main agent WS path (header-only); OIDC callback now redirects with single-use `?oidc_code=` + `POST /api/auth/oidc/exchange` (401 on unknown/expired/replay), dashboard `api.ts` `consumeIdentityRedirect()` rewritten to the exchange flow (fragment path deleted, token in `sessionStorage` only); async limiter wired into middleware (`_rate_limit_async`, middleware awaits it); PERF-03 list projection + `GET /api/tasks/{id}/result` — **with a real regression caught and fixed in-wave**: the first cut projected `result` out of single-task `GET /api/tasks/{id}` too, breaking `device_routing.execute_on_device()` (which polls that endpoint and returns `cur["result"]`), the dashboard detail view, and `ai_api`'s documented contract; fixed by restoring `result` on the single GET only (the list stays projected, where the multi-MB hazard lives); both failing tests then passed unchanged; long-poll spins replaced with `wait_for_device_notification`; DIST-08 WS loop calls `handle_agent_message` + progress endpoint on both `/api/agent/...` and `/api/tasks/...` paths (never 404/501); scheduler sweeps stuck handoffs; `POST /api/policy/simulate` (FR-01, `executed: False`, `unrestricted` never read from the body); `to_pg_sql` regex translation replaced with a tokenizer + allowlist (only the settings upserts translate, everything else raises); 401 split into "Unknown device token." vs "Device token revoked."; dashboard stops sending `simulate_agent_intent`; limiter imports migrated in `webrtc_stream.py` and `app.py`; `docs/operations.md:16` corrected; systemd gains `SuccessExitStatus=2` **and** `Restart=on-failure` (the latter is load-bearing — `SuccessExitStatus=2` alone does not stop the loop under `Restart=always`); SIGKILL escalation in `execution.py` and `tools/shell.py` (functionally verified against a SIGTERM-ignoring process tree: no orphans, `poll() == -9`); `dispatch.py` routed through `audit_writer`; Windows GUI `_poll_loop` rebase proven infeasible and documented with exact reasons (behaviours adopted in place). 7 new tests (`test_wave4_wiring.py`). **Result (parent-verified): Python 750 passed / same 12 failed; dashboard 430/430.**

## 2. Disposition — all 174 register rows

Evidence codes: `W1A/W1B/W1C/W2A/W2B/W2F/W3A/W3B/W3C/W4` = wave that did the work; `HEAD` = verified in the current tree by this report's author (`grep`/`git show`); `per wave report` = the wave's own evidence, not re-run here.

### 2a. P0 — Critical (16 rows)

| ID(s) | Title (short) | Status | Evidence |
|---|---|---|---|
| F-04-03, F-06-01 | Env vars disable MCP auth + DNS protection | Closed | W1C; HEAD: no `AI_BRIDGE_HTTP_ALLOW_ANON`/`AI_BRIDGE_MCP_DISABLE_DNS_PROTECTION` hits in `server.py`; `--unsafe-allow-anon` flag exists, refused in production (`server.py:73,2226`) |
| F-04-08, SEC-13 | `unrestricted` bypass; silent Tier-4 authorisation | Closed | W1C+W2A; HEAD: `grant_unrestricted_policy()` at `policy.py:301`; Tier 4 always needs approval (`policy.py:255-261`) |
| F-05-01 | Local execution on the control-plane host | Closed | W2A; local exec off by default (admin + `AI_BRIDGE_ALLOW_LOCAL_EXECUTION=1` + approval for Tier ≥3); pure DB reads stay available to operators (deliberate) |
| F-05-03, SEC-04 | `term-*` auto-approval | Closed | W2A; `term-*` branch deleted from `next_task` |
| F-06-04 | 5 MCP tools untiered | Closed | W1C; 5 tools tiered; 109/109 wrappers gated; registry now holds 149 entries (W3B correction of the brief's 144) |
| F-06-09 | No audit logging for MCP tool calls | Closed | W1C+W2A; HEAD: `audit_writer.py` exists with `record_tool_call`/`write_audit_event`; gate audits every invocation. Residual: audit-write failure does not block execution (availability trade-off, loud on stderr) |
| F07-01 | Agent policy can only widen | Closed | W1B; intersection-only `_effective_policy()` in `agents/common/poller.py` |
| F07-02 | Linux agent Tier 4 by default | Closed | W1B; restrictive default (ceiling 2, `unrestricted=False`, 15-tool read-only allowlist) |
| F08-01 | Fake H.264 IDR fabricates success | Partially closed | W1B code only; fake 46-byte IDR deleted, failure returns `valid=false` + `failure_reason`. **Not compiled or run** — no Windows toolchain on this host |
| SEC-01 | `/api/v1/tasks` bypasses approval | Closed | W2A; single task service (`control/task_service.py`) for `/api/tasks`, `/api/v1/tasks`, `ai_api` |
| SEC-02 | Workflow steps run as admin | Closed | W2A; steps run as workflow owner; per-step approval default on. Note: approved steps run with `approval_granted=True, is_admin=True` (same model as approved tasks in `dispatch()`) |
| SEC-03 | Tenant is caller-supplied | Closed | W2A; migration 013 (`migrations/013_tenant_isolation.sql` + down); tenant from session server-side. Carve-out: pseudo-device `"local"` is not tenant-scoped (governed by role + R-P0-03 gate) |
| F13-03 | View-only unenforced for remote agents | Closed | W1B+W2A; HEAD: `routers/webrtc.py:320` returns `mode`/`permissions`; `:390-391` returns `human_takeover_active`; agent-side default-deny in `webrtc_stream.py`. Live remote-agent E2E never run |
| P-01 | Tier 3 ON by default in production | Closed | W1A; HEAD: `AI_BRIDGE_ALLOW_TIER3=${…:-0}` (`docker-compose.prod.yml:31`), `=0` in CFN (`cloudformation.yml:396`) |
| P-02 | Known default secrets in compose | Closed | W1A; HEAD: zero grep hits for `aibridge-dev-password` / `change-me-in-production` / `aibridge_turn_shared_secret_2026` / `dev-admin-token`. Note: 1A found the audit's `POSTGRES_PASSWORD` default claim was already stale (compose was fail-fast) |
| P-13 | Unsigned fleet update | Closed | W2A+W2F; signed pinned-git updates (`updates.py`, manifest verified before fetch, 40-hex pin, rollback snapshot); installers refuse unsigned. **Not live-tested**: no signed release manifest exists yet; `repo_dir` defaults to process cwd (`tools/fleet.py:159-160`, HEAD-verified) |

P0 tally: **15 Closed, 1 Partially closed, 0 Open.**

### 2b. P1 — High (78 rows)

| ID(s) | Title (short) | Status | Evidence |
|---|---|---|---|
| F-04-01 | Local approvals in-memory, unscoped, no expiry | Partially closed | W1C: session-scoped + 15-min TTL + single-use. **Durable DB storage never done** — HEAD: `_APPROVALS` is still an in-memory dict (`server.py:70`) |
| F-04-02, F-06-03 | `_oauth_bound_device()` fails open | Closed | W1C; verification exceptions now raise `PermissionError` |
| F-04-04, F-06-07 | `select_device` re-targets unchecked; fleet fan-out | Partially closed | W1C+W3B: `select_device` tiered SENSITIVE and gated; `broadcast_fleet_command` now `@_gated` (HEAD: `server.py:1680`). No explicit device-list or approval requirement on the fan-out itself |
| F-04-05, F-06-05 | Sparse server-layer policy enforcement | Closed | W1C; one `_gated` gate on all 109 wrappers. Carve-out: a peeked local approval satisfies Tier-4 admin at the gate (documented rationale) |
| F-04-13 | Shared browser singleton; arbitrary JS | Closed | W3C; per-session locked contexts + `browser_evaluate` allowlist. Hedge: allow-all when the allowlist var is unset (reported as `evaluate_allowlist_configured: False`) |
| F-04-15 | Pillow fallback fabricates images; unchecked DLL | Open | No wave touched it. HEAD: `_FallbackImageInstance` still present (`screen_capture.py:26-49`) |
| F-04-17 | OCR script to predictable temp path; Bypass | Closed | W3C; HEAD: `tempfile.mkstemp(prefix="ai_bridge_ocr_")`, mode 0600, `-ExecutionPolicy RemoteSigned` (`vision.py:99-153`). See §5 premise correction |
| F-04-21 | No fsync/locking/rotation/hash chain in JSONL audit | Partially closed | W1C: `audit.py` now `flush()` + `os.fsync()` per record. Hash-chain integrity is the DB `audit_writer` path (R-P0-06); JSONL remains a fallback writer |
| F-04-22 | E-stop mirror in-memory; MCP tools ignore e-stop | Open | No wave touched it. HEAD: zero `emergency_stop` references in `server.py` / `tools/_common.py` / `tools/shell.py` |
| F-05-02, SEC-10 | CSRF opt-in only; per-process secret | Closed | W2A; HEAD: CSRF on by default (`app.py:855-857`, opt-out `AI_BRIDGE_CSRF_DISABLED=1`); shared secret from secrets backend |
| F-05-04 | Hardcoded CORS, wildcard methods/headers | Open | No wave touched it. HEAD: `allow_methods=["*"], allow_headers=["*"]` with `allow_credentials=True` (`app.py:874-875`) |
| F-05-05 | Legacy admin token default-on | Closed | W2A; HEAD: `legacy_admin_enabled()` requires explicit `AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN=1` (`auth.py:40-43`) |
| F-05-06, SEC-09 | mTLS is proxy-header trust, one route | Partially closed | W2A+W2F; HEAD: `require_device_mtls` call sites on all agent routes (`routers/agent.py:41,99,116,184,201`). Header-trust mechanism unchanged; no-op unless `AI_BRIDGE_DEVICE_MTLS=1`; operator v1 registry routes deliberately ungated |
| F-05-07 | Single global execution context | Closed | W2A (dist child); HEAD: `TaskContext` + `ContextVar` in `control/task_runtime.py:26,59` |
| F-05-10 | Silent env fallback when Vault/AWS missing | Closed | W2A (db child); HEAD: fail-closed by default, env fallback only with explicit `AI_BRIDGE_SECRETS_ALLOW_ENV_FALLBACK=1` (`secrets_backend.py:30-41`) |
| F-06-02 | Stdio no auth; shared session key | Closed | W1C; per-process `stdio-<hex>` session key |
| F-06-06 | `write_file`/screenshots never pass approval | Closed | W1C; approval pass-through added |
| F-06-08 | Browser/search tools local-only, no policy check | Partially closed | W1C gate covers the wrappers; W3C allowlisted `browser_evaluate`. The local-only design of the tools themselves is unchanged |
| F-06-10 | No pydantic validation in MCP path | Closed | W1C; pydantic v2 `BaseModel`s per tool family, gate validates before `decide()` |
| F-06-12 | PKCE optional; DCR accepts any `https` redirect | Open | No wave touched it. HEAD: `pkce_ok()` still returns `True` when no challenge is sent (`deployment/oauth/gateway.py:887-888`) |
| F07-03 | Two poll-loop implementations (GUI duplicates Poller) | Partially closed | W4: full rebase proven infeasible (documented at `gui_app.py:1673-1700` — `run_once` returns no tool/args/result/elapsed, Poller stores no `device_id`); behaviours adopted in place (worker dispatch, join-loop heartbeat, bounded drain, terminal auth). GUI drain/revocation not executed — Windows smoke owed |
| F07-04, DIST-02 | No execution fencing | Closed | W2A; `tasks.epoch` bumped at claim; result UPDATE requires `status='running' AND epoch=?`, stale → 409 |
| F07-05 | Silent token-storage downgrade on DPAPI failure | Closed | W1B; DPAPI failure fails loudly, token kept in memory only |
| F07-06 | Linux token in three plaintext places | Closed | W1B; token only in `device.token` (0600); `agent.env` carries no token |
| F07-07 | Single-threaded execution blocks heartbeats | Closed | W3C; HEAD: worker thread `ai-bridge-task-{id}` (`poller.py:886`) + independent 30 s heartbeat timer (`poller.py:287-292`). See §5 premise correction |
| F08-02 | Service is not a supervisor | Open | No wave touched it |
| F08-03 | Pipe auth has no replay protection | Open | No wave touched it |
| F08-04 | Desktop agent never reads pipe reply | Open | No wave touched it |
| F08-05 | GDI fallback reports failed frames as valid | Partially closed | W1B code only; `BitBlt`/`PrintWindow` results now checked, `valid=false` on failure. **Not compiled or run** |
| F08-06 | COM lifetime bugs in encoder | Partially closed | W1B code only; HRESULTs checked, `CoUninitialize` for owned init. **Not compiled or run** |
| F08-07 | DLL input surface always-on, unauthenticated | Open | No wave touched it |
| F08-08 | Native config stores token + pipe secret in plaintext | Partially closed | W2A (installer sealing) + W2F (C++ `UnsealIfSealed()` readers); **both inspection-only**. Open issue: the standard-user desktop agent can no longer read the SYSTEM+Admins-only config — reconcile before deploying to non-admin desktops |
| SEC-05 | Admin audit-retention endpoint deletes history | Closed | W2A; WORM-gated — no WORM URL → 409; export failure → delete refused with 502 |
| SEC-06, DB-06 | MCP OAuth tokens stored plaintext | Closed | W2A (db child); HEAD: Fernet encryption, `fernet1:` prefix (`mcp_oauth.py:27-32`). Caveat: pre-existing plaintext rows (no prefix) stay readable until re-written — no migration of old rows |
| SEC-07 | `run_shell` children inherit secrets | Closed | W2A; HEAD: minimal clean child env (`tools/shell.py:117` — allowlist, `DATABASE_URL`/vault/TURN/webhook/OAuth dropped) |
| SEC-08 | Human takeover does not stop queued/shell/workflow tasks | Closed | W2A; takeover checked at submission, claim (`next_task` returns `{"task": None, "human_takeover_active": True}`), workflow/scheduler, agent WebRTC route |
| D-01 | Demo is the default experience | Closed | W2B+W2F; login-first routing + once-per-session interstitial. Positive demo-entry path unverified in a real browser (Node only covers the negative contract) |
| D-02 | Merkle "verification" is fabricated | Closed | W2B; hardcoded root/timeline/`blocksChecked: 14920` deleted; honest amber "Not verified" |
| D-03 | Provider cards present fake credentials | Closed | W2B; hardcoded keys/latencies/models deleted; live-only or "Not configured" |
| I-01, OV-07, INV-15 | Production host hardcoded as default | Partially closed | W2B: integrations + dashboard clean (`require_bridge_url()`, `SystemExit(2)` without env URL). Still defaulting to the operator host: `src/ai_bridge/client.py` docstring, `agents/linux/agent_main.py`, `deployment/**`, `deploy_to_droplet.py` |
| I-02 | Ollama agent is single-turn | Closed | W2B; real multi-turn tool loop with approval-aware reporting. Stub-tested only — no live Ollama server |
| DB-01 | No Postgres backup/restore procedure | Partially closed | W3A: SQLite backup/restore CLI tested (`test_backup_restore_drill`); Postgres `pg_dump`/`pg_restore` scripted only — **no Postgres server/binaries in this environment, drill still owed** |
| DB-02 | Zero foreign keys / CHECKs | Partially closed | W3A: migration 015 adds FKs/CHECKs; SQLite-verified (35-check scratch + `test_db_phase2.py`). Postgres `ADD CONSTRAINT` path untested; `tasks.device_id` deliberately has no FK (`'local'` is virtual) |
| DB-03 | Dual schema source of truth | Partially closed | W3A: `scripts/check_schema_parity.py` — `PARITY OK, 47 objects identical` (SQLite). Postgres boot parity untested |
| DB-05, DIST-11 | Audit hash chain non-atomic, forks, no verifier | Closed | W2A; HEAD: tip-read + hash + insert inside ONE transaction (`db.py:777-812`); verifier exists (`db.py:724`, `GET /api/audit/verify`) |
| F13-01 | Encode-time telemetry partly fabricated | Closed | W1B; real value or `None`, never a synthetic estimate. Inspection-verified only (no aiortc/av, no live capture) |
| F13-02 | Native-encode timing call passes shifted args | Closed | W1B; shifted per-frame call block deleted; `native_timing_ran` always `False` |
| F13-04 | Human takeover not checked in WebRTC input path | Closed | W1B; `_handle_input_payload()` refuses input while takeover is active, releasing held keys |
| F13-05, F08-13 | C++ WebRTC drops viewer ICE candidates, no TURN | Partially closed | W3C code only; viewer candidates parsed/applied, TURN config, persistent signalling, throttled poll. **Not compiled** — libdatachannel API names (`onGatheringStateChange`, `addRemoteCandidate`, `IceServer::username/password`) unverified |
| PERF-01, DB-04 | One DB connection + RLock serialises all DB work | Deferred | W2A explicitly deferred: needs a connection-checkout refactor across every route + a load-test harness ("measure first") |
| PERF-02 | Rate-limit DB write per request / 4× under 4 workers | Partially closed | W3A async limiter + W4 middleware wiring (`_rate_limit_async`, middleware awaits it). DB path retained as the no-Redis-URL fallback by design. **The 4-worker load test that would prove the 4× claim is gone has never been run** |
| PERF-03 | `SELECT *` task lists return multi-MB results | Closed | W4; list endpoints project columns; `GET /api/tasks/{id}/result` added; single GET keeps `result` (see §1 Wave 4 regression note) |
| TST-01, OV-08 | No Python test executed in this environment | Partially closed | Suite now executes locally in `.venv` with published logs (750 passed at HEAD — see `FINAL_TEST_RESULTS.md`); CI job exists (W1A). **No GitHub CI run has been observed** |
| TST-02 | 31/58 test files import unavailable packages | Partially closed | Suite collects and runs in the venv with dependencies installed. Lazy-import work (suite collecting with stdlib + pytest only) not done |
| TST-04, D-06, DOC-04–07 | Dashboard "356 tests, 100% pass" is self-referential | Closed | W2B+W2F; Tier 1/2 rewritten against real modules; Tier 5 + challenger wired in; `npm test` 430/430 + i18n 3/3 (parent-verified) |
| TST-05 | Tier 5 + challenger suites not wired into `npm test` | Closed | W2B; wired into `run-all-tests.ts` (order load-bearing, documented) |
| TST-09 | Thin e2e / MCP round-trip / OAuth2 coverage | Open | No wave added the 10-step E2E (enroll→task→result→audit→revoke), MCP round-trip, or OAuth2 tests |
| TST-10 | No failure/recovery tests; soak scripts unwired | Open | No wave wired soak scripts into CI or added crash/restart/DB-failure/duplicate/cancel race tests |
| TST-15 | E-stop + OIDC state in process memory | Closed | W2A; HEAD: OIDC `_PENDING` in the shared settings table when bound (`oidc.bind_oidc_store()`, wired at `app.py:848`); e-stop read via durable `plane_state` flag at submission/claim/workflow/scheduler |
| DOC-01/02/03, TST-07 | Test-count claims contradict each other | Closed | W2B; README "75 tests" → 481 grep-verified; CHANGELOG 29/33 qualified as subset counts with no run log |
| DOC-08 | README "Fluent 2 Web Dashboard" is stale | Closed | W2B; corrected to JX-UI design system |
| DOC-12 | SECURITY.md supported version v0.2.x | Closed | W2B; bumped to v0.3.x |
| DOC-13 | mTLS status stated three different ways | Closed | W2B; one statement everywhere: "code present, end-to-end verification pending" |
| DOC-14 | OIDC status contradictory | Closed | W2B; one statement: "code present, flow unverified until E2E passes" |
| DOC-15, OV-05, INV-08 | macOS agent status contradictory | Closed | W2B; one statement: "code exists, never run on macOS" |
| DOC-19, INV-02 | `ORIGINAL_REQUEST.md` cited but does not exist | Closed | W2B; citation removed; R1–R5 re-baselined on TEST_INFRA.md §1.2 + PROJECT.md |
| DOC-31 | Overstatements in marketing-facing docs | Closed | W2B; sweep of README/TEST_READY/PROJECT/SECURITY; "Hardened (P0/P1)" badge → "P0/P1 remediation in progress" |
| P-03 | No HTTP→HTTPS redirect | Closed | W1A; HEAD: `return 301 https://$host$request_uri;` (`nginx.conf:77`). `nginx -t` never run (no nginx binary) |
| P-04 | `/metrics` publicly exposed | Closed | W1A; HEAD: `auth_request /internal/verify;` on `/metrics` (`nginx.conf:141,184`) |
| P-05, I-07 | Shipped nginx omits OAuth `auth_request` wiring | Closed | W1A; HEAD: `location = /internal/verify` + `auth_request` on `/mcp`. Live 401/200 round-trip unproven (needs a live plane + gateway) |
| P-09 | Magic installer enrolls with `curl -k` | Closed | W1A; HEAD: zero `curl -k` hits in installers; `AI_BRIDGE_CA_BUNDLE` supported. `run-agent.sh` `AI_BRIDGE_INSECURE_TLS=1` → `-k` hatch unchanged (explicit opt-in escape hatch, out of scope) |
| DIST-01 | Scheduler runs in every worker, no leader election | Closed | W2A; atomic claim (`UPDATE … WHERE next_run_at<=?`, rowcount-checked); test proves exactly one winner |
| DIST-03 | WS wake/cancel fan-out is per-worker | Partially closed | W3A; Redis pub/sub hardening + tests against a fake in-process broker. **Live two-worker Redis verification never done** (no Redis server in this environment) |
| DIST-04 | OIDC login breaks across workers | Closed | W2A; HEAD: `_PENDING` in the shared settings table when bound (see TST-15) |
| DIST-05 | Late result overwrites `cancelled`/`timed_out` | Closed | W2A; result UPDATE guarded by `status='running'` + epoch, rowcount-checked |
| DIST-08 | Long tasks re-queued while still running | Partially closed | W3A+W3C+W4: agent progress heartbeat + plane `record_task_progress` + WS loop + HTTP endpoint on both namespaces. **Residual:** the 0.5 s execution-state poll still appends a `task_events` row per call — throttling deliberately not done (would change liveness semantics) |
| OV-02, INV-05 | `app.py` 5,147-line god module | Closed | W3B; `app.py` 5,538 → 2,093 lines; 9 routers; 220-pair route snapshot diffed empty |
| INV-12 | Duplicate responsibilities (workspace ×2, rate-limit ×2, MCP ×3) | Partially closed | W3B+W4: one tool registry, one limiter (`control/limiter.py`), workspace renamed with shim. Audit-writer duplicates: `dispatch.py` routed through `audit_writer` (W4); **`mcp_oauth.py:173` and `task_service.py:389` still call `dbmod.audit` directly** (HEAD-verified) |

P1 tally: **47 Closed, 20 Partially closed, 10 Open, 1 Deferred.**

### 2c. P2 — Medium (80 data rows; the DOC-30 pointer row is excluded, per register §1/§5)

| ID(s) | Title (short) | Status | Evidence |
|---|---|---|---|
| F-04-06 | `tools.py` god module | Closed | W3B; `tools/` package (11 files); exact 103-name public API verified |
| F-04-07 | Three shell paths, divergent policies | Partially closed | W3C: one shell contract (`ShellSpec` + `run_shell_command`). **Not addressed:** `get_screen_size` passing `approval_granted=True` unconditionally |
| F-04-09 | Regex-only redaction; filename-only secret check | Closed | W3B; `is_secret_name()` checks all path components; `image_base64` preserved only if a real base64 image; JWT pattern added |
| F-04-10 | 15 s verify cache; redirect-following; `wait_seconds=100` | Open | No wave touched it |
| F-04-11 | SIGTERM-only kill; unbounded `_tasks`/`_procs` | Partially closed | W3C (`terminal_manager`) + W4 (`execution.py:191`, `tools/shell.py:213` — SIGTERM → 2 s grace → SIGKILL group, functionally verified). **Residual:** `_run_argv` cancellation leak — `TaskCancelled` orphans the child (HEAD: `except TaskCancelled: raise` with no kill, `tools/shell.py`); `_tasks`/`_procs` bounding not claimed |
| F-04-12 | No `_check` in `workspace.py`; module-name collision | Closed | W3B; `control/workspace_routes.py` canonical, `workspace.py` thin shim |
| F-04-14 | Browser isolation is detection-only | Closed | W3C; fails closed when the requested sandbox is unavailable; bwrap hardened; honest `seccomp: "none"`. Escape hatch `AI_BRIDGE_ALLOW_BROWSER_NO_SANDBOX=1` documented |
| F-04-16 | No auth/policy/timeout in `terminal_manager`; unbounded sessions | Partially closed | W3C: bounded sessions (max 32, idle TTL 1800 s, eviction). Auth/policy/timeout in `terminal_manager` generally not addressed |
| F-04-18, F13-11 | Non-prod TURN fallback to hard-coded shared secret | Open | No wave touched it (W3C added config-driven TURN for the C++ path, but the Python fallback to the hard-coded default secret was not removed) |
| F-04-19 | SDK sends admin token via headers to any `base_url` | Closed | W3C; token never in URLs, Bearer only, redacted in errors/repr, `AI_BRIDGE_TOKEN_FILE` fails closed |
| F-04-20 | `urlopen` without pinning/allowlist; no manifest check in-function | Partially closed | W3C: signed-update gate delegates to `updates.load_signed_manifest` (verify before fetch). No update path exists in those files; `download_and_stage_update` itself was not reworked |
| F-04-23 | `get_settings()` uncached, unvalidated | Closed | W3B; cached via env-fingerprint `lru_cache`; `_int()` raises on garbage/out-of-range; `expandvars` removed from paths |
| F-05-08 | DB rate-limit fallback is non-atomic read-modify-write | Open | **Miscitation caught (see §5):** W3A §4 heads its section "DIST-09 / F-05-08 / F-05-14" but the body is entirely about idempotency/TTL. HEAD: `_db_allow` is still SELECT → compute → upsert → commit (`durable_ratelimit.py:142-167`) |
| F-05-09 | Stale-task requeue can re-run non-idempotent tools | Open | No wave changed the default-on requeue semantics |
| F-05-11 | Stale rate-limit docstring; `app.py` monolith | Closed | W3B (docstring now states the real 30/1200/300 limits) + W4 (`docs/operations.md:16` corrected); monolith half is OV-02 (closed) |
| F-05-12, SEC-12 | `approval_granted=True` for `desktop_anticipate_action` | Open | No wave touched it |
| F-05-13 | "WORM" is best-effort POST; audit mutable; retention can prune | Partially closed | W2A: retention is now WORM-gated (SEC-05). The emit itself remains best-effort when no WORM URL is configured (returns `skipped`) |
| F-05-14, DIST-09 | `Idempotency-Key` only on `POST /api/tasks`; no TTL | Closed | W3A: `prune_task_submissions` (TTL 86400). HEAD: `v1_create_task` (`v1_routes.py:1112`) and `ai_api` (`ai_api.py:1435`) both route through `task_service_mod.create_task`, so the key now applies on all three paths. No dedicated test proves the v1/ai_api paths honour the key |
| F-05-15 | OIDC token in URL fragment; in-memory login state | Closed | W3A mechanism + W4 wiring (exchange-code flow, `POST /api/auth/oidc/exchange`). Residuals: `main.tsx` still calls the sync wrapper (exchange runs in the background); Playwright `dashboard.spec.ts:134` still asserts the old fragment flow (e2e not run) |
| F-05-16 | Migration failure logged, not fatal | Closed | W3A; migration runner rewrite: one transaction per file, fail-fast, missing dir raises, down migrations |
| F-05-17 | SSRF validation only in `app.py`, not in senders | Closed | W3B; shared `security.validate_outbound_url()` used by `app.py`, `alerts.py`, `worm_siem.py`. **Deliberate carve-out:** 198.18.0.0/15 NOT blocked (sandbox DNS wildcard resolves everything there; blocking it would break all webhook delivery here) |
| F-05-18 | SQLite serialised; Postgres translation regex-based | Closed | W4; tokenizer-based `to_pg_sql` with an upsert allowlist (settings only); `import re` removed; anything else raises `ValueError` |
| F-06-11 | No structured per-tool error taxonomy | Closed | W3B; `errors.py` (13 codes) wired into `server.py @_gated` + `mcp_routes.py`; small MCP half in W3C (`MCPErrorCode`) |
| F-06-13 | No DPoP, mTLS, PAR, sender-constraining | Deferred | W3C evaluated, deliberately NOT implemented: gateway has no PAR endpoint or DPoP support (grep-verified); client half alone would be theatre. Revisit only when the gateway gains PAR |
| F-06-14 | `enforce_mcp_tool_policy()` not on control-plane execute path | Closed | W3B; enforced on `POST /api/mcp/servers/{server_id}/execute` before `execute_mcp_tool()` (deny → 403 `POLICY_DENIED`; approval → 403 `APPROVAL_REQUIRED` + id) |
| F-06-15 | Outbound client skips `initialize`, no session/pagination | Closed | W3C; proper `MCPClient` (`initialize()` with version negotiation, `Mcp-Session-Id`, pagination bounded 50 pages; legacy fallback kept) |
| F-06-16 | `stateless_http=True`; no SSE/resumable sessions | Open | No wave touched it. **ID note (see §5):** the progress files call the `approve_task` admin-token finding "F-06-16"; the register reserves F-06-16 for this row and calls the token finding F-06-20 |
| F-06-20 | `approve_task` takes `admin_token` as a tool argument | Closed | W3B; now `approve_task(approval_id)`, authenticated by session bearer, fail-closed in stdio. `_GATE_DECIDE_EXEMPT` deliberately kept (removing it deadlocks all approvals — documented) |
| F07-08 | Spool head-of-line blocking, unbounded growth | Closed | W3C; cap 500 items / 64 MiB, 7-day TTL, 10 attempts, `dead_letter_results` DLQ; `flush()` no longer stops at first failure |
| F07-09 | No graceful shutdown anywhere | Closed | W3C; `request_shutdown()`, 60 s drain, in-flight task finishes and reports; signal handlers on Linux/macOS/Windows; exit 2 on revocation. W4: systemd `Restart=on-failure` + `SuccessExitStatus=2`. Windows scheduled-task / macOS launchd exit-2 equivalents not done |
| F07-10 | Heartbeat coupled to task execution | Closed | W3C; independent 30 s heartbeat timer thread; heartbeat serialised by lock |
| F07-12 | 401 retry can mask revocation | Closed | W3C (agent-side classification) + W4 (plane-side 401 split: "Unknown device token." vs "Device token revoked.", `app.py:1160-1176`) |
| F08-09 | Secure-desktop detection duplicated and divergent | Partially closed | W3C code only; one rule both languages, probes cached 2 s. **C++ test not run** |
| F08-10 | C++ WebRTC input ignores monitor geometry | Partially closed | W3C code only; monitors enumerated once per session, offsets passed to every input call. **Not compiled/run** |
| F08-11 | System-monitor CPU reading always ~0% in service | Partially closed | W3C code only; persistent monitor + 5 s enum cache. **Not compiled/run** |
| F08-12 | Dual logon launch, no single-instance guard | Partially closed | W3C code only; named mutexes (desktop agent exits 3, service exits 1). **Not compiled/run; no test added** (needs launching the binary twice on Windows) |
| F08-14 | Tests are `assert()`-only, pass while broken | Partially closed | W3C code only; all `assert()` → thread-safe `CHECK()`, new §[7/7] tests. **Not compiled/run** |
| F08-15 | Hand-rolled JSON everywhere | Deferred | W3C deliberate keep, with written justification (no JSON lib vendored; unverifiable dependency swap is higher risk; helpers extract fixed fields from the plane's own well-formed responses). Instruction recorded in code: vendor nlohmann/json single-header if a general parser is ever needed |
| F08-16 | `StartSession` returns true on init failure | Partially closed | W3C code only; encoder-init failure → `false` + `status="error"`. **Not compiled/run** |
| SEC-11 | Webhook SSRF via DNS rebinding / redirect | Closed | W3B; same evidence as F-05-17. Same 198.18.0.0/15 carve-out; residual TOCTOU noted (request goes to hostname, not a pinned IP literal — pinning out of scope) |
| D-04 | Vision canvas is a hardcoded fake desktop | Closed | W2B; labelled "SIMULATED — no live frame" |
| D-07, DOC-30 | Admin token honoured from `localStorage` | Closed | W2B; tokens session-only; legacy localStorage copies removed on sign-out |
| D-08 | Credentials in URLs for SSE/WebSocket streams | Partially closed | W2F+W4: stream tickets (single-use, 60 s TTL, hash-only) replace `?session_token=`/`?token=` on the main paths. **Residual (HEAD-verified):** `v1_routes.py:985` keeps its own WS `?token=` fallback ("Long-lived tokens in the URL remain accepted" — deliberate compat for non-dashboard WS clients) |
| I-03 | LangChain tools have no argument schema | Closed | W2B; `build_args_schema()` builds pydantic models from tool JSON Schema |
| I-04 | Example agents ignore approval-gated outcomes | Closed | W2B; all 4 example agents label `needs_approval` as pending, not done |
| I-05 | Copilot Studio integration documented but untested | Open | No wave tested it against a live Copilot Studio tenant |
| DB-07, PERF-10 | Unbounded growth: tasks, events, memory, alerts | Partially closed | W3A: retention jobs (`maybe_prune_telemetry` COUNT-first, `prune_task_submissions/events/terminal_tasks`, `retention_prune_audit` raising unless `worm_export_ok=True`, `retention_sweep`, `delete_device_cascade`). Agent-memory / alert-delivery / workflow retention not claimed |
| DB-08 | Migration runner splits on `;`, no down-migrations | Closed | W3A; same evidence as F-05-16 |
| F13-06 | `simulate_agent_intent` is a live demo hook | Closed | W3C (agent refuses by default unless `AI_BRIDGE_DEV_SIMULATE_INTENT=1`) + W4 (dashboard stops sending it; zero references remain in `apps/dashboard/src/`) |
| F13-07 | Adaptive bitrate viewer-driven and cosmetic | Closed | W3C; real RTCP ABR via `getStats()` receiver reports (2.5 s poll), every sample labelled; nothing fabricated when no stats exist. **No live aiortc peer run** (aiortc not installed); fake-sender unit tests only |
| F13-08 | Mouse-button held state not tracked/released | Closed | W3C; held-button tracking in both DLL and fallback paths; release on disconnect. Caveat: in the fallback path the tracked "held" button does not correspond to a physically held OS button (`tools.py` has no `mousedown` action) |
| F13-09 | No session reconciliation after restart | Closed | W3C agent side; stale ids refused (`ended`/`error`/expired/previously-negotiated). Plane-side row expiry is a follow-up |
| F13-10 | Offer posted even if ICE gathering incomplete | Closed | W3C both paths; ICE-complete gate, no offer on timeout (`ice_gathering_incomplete`). Python `_run()` wiring verified by inspection only (aiortc not installed); C++ libdatachannel API names unverified |
| PERF-04, F-05-19 | Fleet health is N+1 | Closed | W3A; one query (latest telemetry per device via `GROUP BY` join, tenant-restricted). Bench: 51 → 1 query (SQLite synthetic) |
| PERF-05 | Telemetry prune runs on every heartbeat | Closed | W3A; COUNT-first, prune only above keep+slack (500/100) |
| PERF-06 | `next_task` long-poll spins every 0.15 s | Closed | W3A mechanism + W4 wiring; both spins now await `wait_for_device_notification` (median wake 5.25 ms in bench) |
| PERF-07 | `handoff_status` full-table-scans `tasks` | Closed | W3A; rewritten to indexed `WHERE handoff_id=?` (16.01 ms → 0.67 ms in bench) |
| PERF-08 | Agent polls `execution-state` every 0.5 s | Open | Explicitly left open by W3A/W3C/W4 (throttling would change liveness semantics); still appends a `task_events` row per call |
| PERF-11 | Missing indexes | Closed | W3A; indexes in migration 015 (`tasks(tenant,created_at)`, `tasks(status,updated_at)`, `tasks(handoff_id)`, `devices(token_hash)`, `stream_tickets(expires)`, …) |
| TST-03, INV-11 | `__init__.py` importing `httpx` blocks collection | Partially closed | Suite collects and runs in the venv with dependencies installed. Lazy-import work not done |
| TST-08, DOC-02, DOC-03 | CHANGELOG pass counts have no run logs | Closed | W2B; CHANGELOG 29/33 qualified as subset counts; CI publishes the run log (W1A) — this report set is the run log |
| TST-13 | Playwright e2e tests the demo workspace by design | Open | No wave added live-workspace browser coverage; e2e not run in any wave |
| TST-14 | C++ native tests are Windows-only, not executed | Open | No wave executed them; no Windows host in this environment |
| DOC-10 | "All 15 operational screens mapped" — screens are demo | Closed | W2B (docs sweep) |
| DOC-11 | TEST_READY R5 treats 40 seed nodes as verification | Closed | W2B (docs sweep) |
| DOC-16 | Threat-model lists signed updates as out-of-scope | Closed | W2B (docs sweep) |
| DOC-17 | "Resource cgroups are a v0.3 hardening item" | Closed | W2B (docs sweep; rlimits ≠ cgroups corrected) |
| DOC-18 | "Add signed config + admin audit alerts in v0.3" | Closed | W2B (docs sweep) |
| DOC-20, INV-03 | "Built output IS included in the release ZIP" | Closed | W2B (docs sweep; `dist/` claim corrected) |
| DOC-22 | API reference is a 15-line v0.2 stub | Closed | W2B (docs sweep) |
| P-06 | `.env.example` omits variables compose consumes | Closed | W1A; rewritten from the union of consumed variables |
| P-07 | `requirements.txt` is not locked; no lockfile/hashes | Open | No wave produced a lockfile or pinned hashes; dashboard image still uses `npm install` (not `npm ci`) per the audit — not claimed changed |
| P-08 | Deploy scripts ~90% duplicated, no health gate | Open | No wave unified them or added a post-deploy health gate |
| P-12 | Three different default control URLs | Open | No wave unified the defaults (I-01 fixed the integrations half only) |
| P-15 | Render is a single free instance, not a production topology | Open | No wave relabelled Render test-only or changed the topology |
| DIST-06 | No handoff re-driver | Closed | W3A sweeper (`sweep_stuck_handoffs`) + W4 scheduler wiring (same tick as the stale sweep) |
| DIST-07 | Rate limiting effectively 4× under 4 workers | Closed | W3A async limiter + W4 middleware wiring. Live 4-worker verification still owed (same caveat as PERF-02) |
| OV-03 | Two desktop-streaming stacks coexist | Open | No wave consolidated the Python aiortc + C++ libdatachannel stacks or made the default decision |
| OV-10, INV-01 | No git history in delivered tree | Partially closed | The remediation repo now has full git history from the `79c010e` baseline (6 commits). The original delivered archive still ships without `.git` |
| INV-04 | `node_modules/` absent — dashboard cannot be built/tested | Closed | W2B installed `node_modules` (`npm ci`, 227 packages); dashboard builds and tests run |

P2 tally: **48 Closed, 15 Partially closed, 15 Open, 2 Deferred.**

### 2d. Totals

| Status | P0 | P1 | P2 | Total |
|---|---|---|---|---|
| Closed | 15 | 47 | 48 | **110** |
| Partially closed | 1 | 20 | 15 | **36** |
| Open | 0 | 10 | 15 | **25** |
| Deferred | 0 | 1 | 2 | **3** |
| **Total** | **16** | **78** | **80** | **174** |

Counts reconciled against the register by counting table rows: P0 §2 = 16, P1 §3 = 78 (the §3 heading says "79 rows" — a typo in the register; the table contains 78 data rows), P2 §4 = 80 data rows excluding the DOC-30 pointer row (81 rows including it). Register §1/§5 agree with these counts.

## 3. Feature work (report 19, FR items touched)

| ID | Item | Status | Evidence |
|---|---|---|---|
| FR-01 | Policy simulation (dry-run) | Closed | W3C module (`policy_simulation.py`, `executed: False`) + W4 endpoint `POST /api/policy/simulate` (`routers/admin.py:152`) |
| FR-03 | Task result projection | Closed | W3C client half (`project_task_summaries()`) + W4 server half (list projection + `GET /api/tasks/{id}/result`) |
| FR-05 | Demo as explicit mode | Partially closed | W3C `is_demo_mode()` backend helper (explicit opt-in only); dashboard-side explicit-mode flag not claimed |
| FR-06 | Error taxonomy | Closed | W3B `errors.py` (13 codes) + W3C small MCP half |
| FR-07 | SDK retry / idempotency | Closed | W3C: bounded retries, backoff+jitter, `Idempotency-Key` on `execute()` |
| FR-08 | Test-suite wiring | Closed | W1A CI + W2B Tier 5 wiring; suite green (see `FINAL_TEST_RESULTS.md`) |

`fleet_analytics.py` was deliberately **not** created (W3C: the projection lives coherently in `client.py`; real analytics deferred per report 19).

## 4. Regression tests added

| Wave | New test files | New tests (claimed, all passing at their wave) |
|---|---|---|
| 1A | `tests/unit/test_deployment_config.py` | 18 |
| 1B | `tests/unit/test_agent_policy_merge.py`, `tests/unit/test_webrtc_viewonly.py` | 26 |
| 1C | `tests/unit/test_p0_mcp_gate.py`, `tests/unit/test_p0_policy.py` | 34 |
| 2A | `tests/unit/test_p0_control_plane.py`, `tests/unit/test_tenant_isolation.py`, `tests/unit/test_workflow_approval.py`, `tests/unit/test_signed_updates.py` | 35 |
| 2-fixup | `tests/unit/test_stream_ticket.py` | 11 |
| 3A | `tests/unit/test_db_phase2.py`, `tests/unit/test_distributed_phase2.py` | 39 |
| 3B | `tests/unit/test_architecture_phase4.py`, `test_architecture_phase4_routers.py`, `test_architecture_phase4_tools.py`, `test_architecture_phase4_errors.py` | 110 |
| 3C | `tests/unit/test_agent_reliability.py`, `tests/unit/test_webrtc_phase3.py`, `tests/unit/test_wave3c_sdk.py` | 81 |
| 4 | `tests/unit/test_wave4_wiring.py` | 7 |
| **Total** | | **361 new tests** (750 − 389 = 361 net passing-test growth, matching the suite delta) |

Three existing tests were rewritten in Wave 1B because they asserted the vulnerable behaviour (not deleted). Twenty-one dashboard test expectations were updated in the Wave 2 fix-up to the login-first contract (none deleted). No test was deleted or disabled in any wave to make the suite pass; no security check was disabled.

## 5. Premise corrections and discrepancies found during remediation

1. **F07-07 was NOT done in Wave 1.** The roadmap row and the Wave 1 brief assumed the worker-thread agent model existed. Verification by this report's author: `git show 726bed2:agents/common/poller.py` shows `dispatch()` called inline in `run_once()`; threads existed only for the WS loop and the cancel poller — no worker thread for task execution and no independent heartbeat timer. Wave 1B's progress file covers R-P0-07/08/09/13, F13-01/02, F07-05/06 only and never mentions F07-07. Wave 3C implemented the worker-thread model (`poller.py:848-887`) and the heartbeat timer as part of F07-10. **Status: closed by Wave 3C, not by Wave 1.**
2. **F-04-17 was NOT done in Wave 1.** Same pattern: `git show 19747b2:src/ai_bridge/vision.py:99` still wrote to the predictable `$TEMP/ai_bridge_ocr.ps1` and still invoked `-ExecutionPolicy Bypass` (line 126); no grep hit for F-04-17 in `wave1b-progress.md` / `wave1c-progress.md`. Wave 3C fixed it (random `mkstemp` path, mode 0600, `RemoteSigned`, redacted output). **Status: closed by Wave 3C, not by Wave 1.**
3. **F-05-08 miscitation in Wave 3A.** Wave 3A §4 heads its section "DIST-09 / F-05-08 / F-05-14" but the body is entirely about idempotency/TTL; the register root cause for F-05-08 (non-atomic read-modify-write in `_db_allow`) is never described as changed. HEAD confirms `_db_allow` is still read-modify-write (`durable_ratelimit.py:142-167`). **Status: open.** The idempotency/TTL half of that section belongs to F-05-14/DIST-09 (closed).
4. **F-06-16 / F-06-20 ID confusion.** The progress files call the `approve_task` admin-token finding "F-06-16"; the register calls that content **F-06-20** and reserves **F-06-16** for `stateless_http=True` (no SSE/resumable sessions). This report follows the register: F-06-20 closed, F-06-16 open.
5. **Register count headers.** Register §3 is headed "79 rows" but contains 78 data rows; §4 is headed "71 rows" but contains 80 data rows (81 including the DOC-30 pointer row). §1/§5 (16/78/80 = 174) are the correct counts and are used throughout this report.
6. **Dashboard test-count disagreement.** The register (TST-04 row) says grep counts 364 in-scope / 375 on disk; Wave 2B's parent re-verified **356** `it(` on disk with the same regex. Unresolved; the suite as it stands now is 430 tests (parent-verified by running `npm test`), which supersedes both counts.
7. **Wave 4 Track A could not run pytest** in its own environment (no pytest/fastapi/httpx/pydantic in its VM, `pip install` forbidden); its "DONE" claims rested on `py_compile` + stdlib exercises + diff review. The parent's full-suite runs are the verification for that track (750 passed / same 12 failed — see `FINAL_TEST_RESULTS.md`).
8. **`record_task_progress` does NOT throttle.** Wave 4 corrected the brief: the 60 s throttle lives in `handle_agent_message`, which the progress endpoint reuses; the endpoint itself writes a `task_events` row per call (see DIST-08 residual).
9. **Systemd `SuccessExitStatus=2` alone does not stop the restart loop.** Wave 4 corrected the brief: under `Restart=always` the load-bearing change is `always` → `Restart=on-failure`.
10. **PERF-03 was applied too literally, then corrected** (see §1, Wave 4): projecting `result` out of the single-task GET broke `device_routing.execute_on_device()`, the dashboard detail view, and `ai_api`'s documented contract. The correction (single GET keeps `result`, list stays projected) is the shipped behaviour; both failing tests passed unchanged, which is the evidence the correction is right.

## 6. What was deliberately NOT done

- **No production deployment, no destructive production operations, no commands on real enrolled devices, no production credentials touched** — per the user's authorization. Nothing in this report implies any of those happened.
- **C++ changes are inspection-verified only** (no Windows toolchain on this host). A Windows build + `ai_bridge_tests` run is still required, especially to confirm the libdatachannel API names used in Wave 3C.
- **No Postgres server, no Redis server, no Docker daemon, no live Ollama/LLM, no Copilot tenant, no live aiortc peer** in this environment. All claims depending on those are marked partial/open accordingly.
- **PERF-01 connection pool** deferred (needs checkout refactor + load harness).
- **F08-15 hand-rolled JSON** kept deliberately; **F-06-13 DPoP/PAR** evaluated and deliberately not implemented; **`fleet_analytics.py`** not created deliberately.
- **The `_GATE_DECIDE_EXEMPT` carve-out** for `approve_task` was deliberately kept (removing it deadlocks all approvals — documented in Wave 3B).
- **198.18.0.0/15 SSRF carve-out** deliberately kept (sandbox DNS wildcard; see F-05-17/SEC-11).
- **v1 WS `?token=` fallback** deliberately kept for non-dashboard WS clients (compat decision pending an owning wave).
- **Operator v1 device registry routes** deliberately not mTLS-gated (would 403 every dashboard device read under `AI_BRIDGE_DEVICE_MTLS=1`).

## 7. Remaining work (summary; full list in `REMAINING_BLOCKERS.md`)

25 register rows open (10 P1, 15 P2) + 36 partially closed + 3 deferred; the P1 opens are F-04-15, F-04-22, F-05-04, F-06-12, F08-02, F08-03, F08-04, F08-07, TST-09, TST-10. Outside the register: P-11 (deploy script prints the admin token), v1 WS `?token=` fallback, `main.tsx` sync OIDC wrapper, stale Playwright fragment assertion, `mcp_oauth.py:173` + `task_service.py:389` direct `db.audit` calls, dead `audit.py::audit_event`, `_run_argv` cancellation leak, PERF-01, `AI_BRIDGE_INSECURE_TLS=1` in `run-agent.sh`, enrollment token in plain text in `DevicesView.tsx:682`, CFN UserData quoted-heredoc issue, pseudo-device `"local"` not tenant-scoped, signed-update `repo_dir` cwd default, and all live verifications (Postgres boot/restore drill, two-worker Redis fan-out, Windows build/smoke, 4-worker load test, 10-step E2E, WebRTC E2E with a real browser peer, independent re-audit).

**Final verdict (historical Nov 17):** NO-GO. **Current assessment:** see `analysis/PRODUCTION_READINESS.md` (23 Nov 2026) — lab GO; small controlled fleet CONDITIONAL GO; SaaS NO-GO.

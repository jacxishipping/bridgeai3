# Autonomous Execution Tracker — AI Bridge Remediation (FINAL, 2026-11-17)

Started: 2026-11-17. Baseline: git commit `79c010e` ("baseline: ai-bridge-main as received"), zip sha256 `bed477c750c9b782891678d44efe156c4f7b8dad07e765a4f8b011298d282d5c`.
Head: `61faa67` (Wave 4). Diff vs baseline: 217 files changed, +36,618 / −9,841.
Venv: `~/workspace/ai-bridge-main/.venv` (Python 3.12). Final suite (parent-verified): Python **750 passed / 5 skipped / 12 failed** (same 12 pre-existing failures as baseline); dashboard **430/430 + 3/3 i18n**.
Rules held throughout: no production deploy, no real-device commands, no prod credentials. Statuses: PENDING / IN_PROGRESS / BLOCKED / IMPLEMENTED / TESTING / VERIFIED / COMPLETED / DEFERRED / OPEN.
Full per-finding dispositions (all 174 register rows): `analysis/FINAL_IMPLEMENTATION_REPORT.md` §2. Current production assessment: `analysis/PRODUCTION_READINESS.md` (supersedes removed `FINAL_PRODUCTION_READINESS.md`).

## Phase 0 — Stop the bleeding

| ID | Task | Files | Status | Evidence |
|---|---|---|---|---|
| R-P0-01 | Remove env-var auth + DNS-protection bypasses | src/ai_bridge/server.py | VERIFIED | Off-switches deleted; `--unsafe-allow-anon` flag only, refused in production. Zero grep hits at HEAD |
| R-P0-04 | Remove terminal task auto-approval | src/ai_bridge/control/app.py → routers/tasks.py, task_service.py | VERIFIED | `term-*` branch deleted (Wave 2A) |
| R-P0-14 | Tier 3 off by default | docker-compose.prod.yml, deployment/aws/cloudformation.yml | VERIFIED | Default `0` in compose (`:31`) + CFN (`:396`). Compose boot unproven (no Docker daemon) |
| R-P0-15 | Remove default prod secrets; fail fast | docker-compose.prod.yml, docker-compose.yml | VERIFIED | Zero grep hits for known defaults; fail-fast `${VAR:?…}`. Audit's POSTGRES_PASSWORD claim was already stale |
| P-03 | HTTPS redirect in nginx | deployment/docker/nginx.conf | VERIFIED | `return 301` (`:77`). `nginx -t` never run |
| P-04 | Protect /metrics | deployment/docker/nginx.conf | VERIFIED | `auth_request /internal/verify` (`:141,184`) |
| P-09 | Remove `curl -k` from installer | magic-install.sh (+ deployment installers) | VERIFIED | Zero `curl -k` hits. `run-agent.sh` `AI_BRIDGE_INSECURE_TLS=1` hatch remains (explicit opt-in) |
| R-P1-05 | One MCP wrapper gate (skeleton in Phase 0, full in Phase 1) | src/ai_bridge/server.py | VERIFIED | `_gated` on all 109 wrappers (AST-verified) |
| R-P1-53 | Real CI test execution + published results | .github/workflows/ci.yml | IMPLEMENTED | CI job exists; **no GitHub run observed**. Local runs published (FINAL_TEST_RESULTS.md) |

## Phase 1 — Close all P0 bypasses

| ID | Task | Status | Evidence |
|---|---|---|---|
| R-P0-02 | unrestricted policy + Tier-4 approval | VERIFIED | `grant_unrestricted_policy()` (`policy.py:301`); Tier 4 always needs approval (`policy.py:255-261`) |
| R-P0-03 | Local execution on control-plane host | VERIFIED | Off by default; admin + `AI_BRIDGE_ALLOW_LOCAL_EXECUTION=1` + approval (Wave 2A) |
| R-P0-05 | Tier + protect every MCP tool | VERIFIED | 5 tools tiered; 109/109 gated; registry holds 149 entries (Wave 3B) |
| R-P0-06 | Centralised MCP audit logging | VERIFIED | `audit_writer.py` live; gate audits every invocation. Residual: `mcp_oauth.py:173`, `task_service.py:389` still call `dbmod.audit` directly; audit-write failure does not block execution |
| R-P0-07 | Agent policy merge restrictive (intersection) | VERIFIED | Intersection-only `_effective_policy()` (Wave 1B) |
| R-P0-08 | Remove unrestricted Linux defaults | VERIFIED | Restrictive default: ceiling 2, `unrestricted=False` (Wave 1B) |
| R-P0-09 | Remove fabricated H.264 IDR success | IMPLEMENTED | Code deleted (`valid=false` + `failure_reason`). **Not compiled or run** — no Windows toolchain |
| R-P0-10 | Approval on /api/v1/tasks | VERIFIED | Single task service for `/api/tasks`, `/api/v1/tasks`, `ai_api` (Wave 2A) |
| R-P0-11 | Workflow/scheduler permissions | VERIFIED | Steps run as workflow owner; per-step approval default on (Wave 2A) |
| R-P0-12 | Tenant isolation via session identity (+ migration 013) | VERIFIED | Migration 013 (+ down); session-derived tenant. Carve-out: pseudo-device `"local"` not tenant-scoped |
| R-P0-13 | Remote WebRTC view-only enforced agent-side | VERIFIED | Agent default-deny (1B) + plane endpoint returns `mode`/`permissions`/`human_takeover_active` (2A; HEAD-verified `routers/webrtc.py:320,390-391`). No live E2E |
| R-P0-16 | Signed fleet updates on every update path | VERIFIED | Signed pinned-git updates; installers refuse unsigned. **Not live-tested** (no signed manifest exists); `repo_dir` defaults to process cwd |

## Phase 2 — Multi-worker + data safety

| ID | Task | Status | Evidence |
|---|---|---|---|
| DIST-01 | Scheduler atomic claim | VERIFIED | Atomic claim, exactly-one-winner test (Wave 2A) |
| DIST-02 / F07-04 | Execution fencing (epoch) | VERIFIED | `tasks.epoch`; stale result → 409 (Wave 2A) |
| DIST-03 | WS fan-out via Redis pub/sub | IMPLEMENTED | Code + fake-broker tests (Wave 3A). **Live two-worker verification never done** (no Redis server) |
| DIST-04 | OIDC `_PENDING` in shared state | VERIFIED | `bind_oidc_store()` wired in `create_app` (`app.py:848`) |
| DIST-05 | Result guard | VERIFIED | `status='running'` + epoch guard (Wave 2A) |
| DIST-06 | Handoff re-driver | VERIFIED | `sweep_stuck_handoffs()` + scheduler wiring (Waves 3A/4) |
| DIST-07 | Async rate limiter in middleware | VERIFIED | `allow_async`/`rate_limit_async` + middleware awaits it (Waves 3A/4). 4-worker load test never run |
| DIST-08 | Task progress heartbeat | VERIFIED | Agent + plane + WS/HTTP wiring (Waves 3A/3C/4). Residual: 0.5 s poll still writes a `task_events` row per call |
| DIST-09 / F-05-14 | Idempotency on all task paths + TTL | VERIFIED | Single task service (all three paths) + `prune_task_submissions` TTL 86400 (Wave 3A) |
| DB-01 | Postgres backup/restore procedure | IMPLEMENTED | SQLite CLI tested; Postgres scripted only — **drill still owed** |
| DB-02 | Foreign keys / CHECKs | IMPLEMENTED | Migration 015, SQLite-verified. Postgres path untested; `tasks.device_id` deliberately has no FK |
| DB-03 | Single schema source of truth | IMPLEMENTED | Parity script `PARITY OK — 47 objects` (SQLite). Postgres boot parity untested |
| DB-05 / DIST-11 | Atomic audit chain + verifier | VERIFIED | Single-transaction insert (HEAD: `db.py:777-812`); `verify_audit_chain` (`db.py:724`); `GET /api/audit/verify` |
| DB-07 / PERF-10 | Retention jobs | IMPLEMENTED | Retention jobs in `db.py` (Wave 3A). Agent-memory/alert/workflow retention not claimed |
| DB-08 / F-05-16 | Migration runner rewrite | VERIFIED | Lexer splitter, per-file atomicity, fail-fast, down migrations (Wave 3A) |
| SEC-06 / DB-06 | Encrypt MCP OAuth secrets at rest | VERIFIED | Fernet, `fernet1:` prefix (HEAD: `mcp_oauth.py:27-32`). Old plaintext rows not migrated |
| SEC-07 | Clean child env for `run_shell` | VERIFIED | Minimal allowlist (HEAD: `tools/shell.py:117`) |
| PERF-01 / DB-04 | Connection pool | DEFERRED | Explicitly deferred (Wave 2A): needs checkout refactor + load harness |
| PERF-02 | Rate limiting (Redis mandatory, async-native) | IMPLEMENTED | Async limiter + middleware wiring. 4-worker load test never run |
| PERF-03 | List projection + `/result` route | VERIFIED | Wave 4 (single GET keeps `result` after the §1h correction) |
| TST-15 | E-stop + OIDC state in durable shared state | VERIFIED | OIDC `_PENDING` in settings table; e-stop via durable `plane_state` flag |

## Phase 3 — Honesty (dashboard + docs)

| ID | Task | Status | Evidence |
|---|---|---|---|
| D-01 | Demo off by default | VERIFIED | Login-first routing + interstitial (Wave 2B). Positive demo-entry path unverified in a real browser |
| D-02 | Delete fabricated Merkle verification | VERIFIED | Hardcoded root/timeline deleted; honest "Not verified" (Wave 2B) |
| D-03 | Delete fake provider cards | VERIFIED | Live-only or "Not configured" (Wave 2B) |
| D-04 | Label fake vision canvas | VERIFIED | "SIMULATED — no live frame" (Wave 2B) |
| D-07 / DOC-30 | Session-only tokens | VERIFIED | Legacy localStorage copies removed on sign-out (Wave 2B) |
| D-08 | Tokens out of stream URLs | IMPLEMENTED | Stream tickets on main paths (fix-up + Wave 4). **Residual:** v1 WS `?token=` fallback remains (`v1_routes.py:985`) |
| TST-04 / D-06 / DOC-04–07 | Dashboard suite rewired to real modules | VERIFIED | Tier 1/2 real modules; Tier 5 + challenger wired; 430/430 + i18n 3/3 (parent-verified) |
| TST-05 | Tier 5 + challenger in `npm test` | VERIFIED | Wired into `run-all-tests.ts` (Wave 2B) |
| DOC sweep (DOC-01/02/03/08/12/13/14/15/19/31, DOC-10/11/16/17/18/20/22) | Correct counts, one status per feature | VERIFIED | Wave 2B sweep |
| I-01 / OV-07 / INV-15 | Remove hard-coded prod host | IMPLEMENTED | Integrations + dashboard clean (Wave 2B). `client.py` docstring, `agents/`, `deployment/` still default to the operator host |
| I-02 | Ollama multi-turn loop | VERIFIED | Rewritten as a real tool loop (Wave 2B). Stub-tested only — no live Ollama |
| I-03 | LangChain `args_schema` | VERIFIED | `build_args_schema()` (Wave 2B) |
| I-04 | Approval-aware example agents | VERIFIED | All 4 agents label `needs_approval` as pending (Wave 2B) |
| I-05 | Copilot live-tenant test | OPEN | No live Copilot Studio tenant test |

## Phase 4 — Consolidation

| ID | Task | Status | Evidence |
|---|---|---|---|
| OV-02 / INV-05 | Split `app.py` by domain | VERIFIED | `app.py` 5,538 → 2,093 lines; 9 routers; 220-pair route snapshot diffed empty (Wave 3B) |
| F-04-06 | Split `tools.py` | VERIFIED | `tools/` package; exact 103-name public API (Wave 3B) |
| INV-12 | One registry / one limiter / one task service / one audit writer | IMPLEMENTED | Registry + limiter + workspace done (Wave 3B); task service verified single (Wave 2A); audit writer **partially** — `dispatch.py` done (Wave 4), `mcp_oauth.py:173` + `task_service.py:389` still direct |
| F-04-12 | Rename `control/workspace.py` | VERIFIED | `workspace_routes.py` canonical, shim kept (Wave 3B) |
| F-04-23 | Cache + validate `Settings` | VERIFIED | Env-fingerprint `lru_cache`; `_int()` raises (Wave 3B) |
| F-05-11 | Fix stale rate-limit docstring | VERIFIED | Real limits in docstring + `docs/operations.md:16` (Waves 3B/4) |
| F-05-17 / SEC-11 | SSRF validation in the senders | VERIFIED | Shared `validate_outbound_url()` in `app.py`/`alerts.py`/`worm_siem.py` (Wave 3B). Carve-out: 198.18.0.0/15 not blocked |
| F-05-18 | Replace regex `to_pg_sql` | VERIFIED | Tokenizer + allowlist; `import re` removed (Wave 4) |
| F-06-11 / FR-06 | Error taxonomy | VERIFIED | `errors.py` (13 codes) wired into `@_gated` + `mcp_routes.py` (Wave 3B) |
| F-06-14 | `enforce_mcp_tool_policy()` on execute path | VERIFIED | Enforced before `execute_mcp_tool()` (Wave 3B) |
| F-06-20 | `approve_task` takes `admin_token` | VERIFIED | Now `approve_task(approval_id)`, session auth (Wave 3B). Note: progress files call this "F-06-16"; the register calls it F-06-20 |
| F-05-08 | Atomic DB rate-limit fallback | OPEN | **Miscitation caught:** Wave 3A §4 cites it but the body is about idempotency. HEAD: `_db_allow` still read-modify-write (`durable_ratelimit.py:142-167`) |
| F-05-09 | Stale-task requeue semantics | OPEN | Default-on requeue unchanged |
| PERF-04 / F-05-19 | Fleet health N+1 → 1 query | VERIFIED | Single query (Wave 3A); bench 51→1 (SQLite synthetic) |
| PERF-05 | Telemetry prune COUNT-first | VERIFIED | Wave 3A |
| PERF-06 | Long-poll spin → wake primitive | VERIFIED | `wait_for_device_notification` wired (Waves 3A/4) |
| PERF-07 | `handoff_status` indexed lookup | VERIFIED | 16.01 ms → 0.67 ms in bench (Wave 3A) |
| PERF-11 | Missing indexes | VERIFIED | Indexes in migration 015 (Wave 3A) |

## Phase 5 — Agents / WebRTC / features

| ID | Task | Status | Evidence |
|---|---|---|---|
| F07-07 | Worker-thread execution + independent heartbeat | VERIFIED | Worker thread `ai-bridge-task-{id}` + 30 s heartbeat timer (Wave 3C; HEAD: `poller.py:886,287-292`). **Premise correction:** NOT done in Wave 1 despite the roadmap attribution |
| F07-08 | Spool DLQ + cap + TTL | VERIFIED | 500 items / 64 MiB, 7-day TTL, `dead_letter_results` (Wave 3C) |
| F07-09 | Graceful shutdown / drain | VERIFIED | `request_shutdown()`, 60 s drain, exit 2 on revocation (Wave 3C); systemd `Restart=on-failure` + `SuccessExitStatus=2` (Wave 4) |
| F07-10 | Independent heartbeat timer | VERIFIED | Wave 3C |
| F07-12 | 401 classification (revoked vs expired vs transient) | VERIFIED | Wave 3C + Wave 4 plane-side 401 split |
| F07-03 | GUI poll-loop rebase onto Poller | IMPLEMENTED | Full rebase proven infeasible (documented `gui_app.py:1673-1700`); behaviours adopted in place (Wave 4). GUI drain/revocation not executed |
| F13-01 | Honest `encode_time_ms` | VERIFIED | Real value or `None` (Wave 1B). Inspection-verified only |
| F13-02 | Delete shifted native-encode call | VERIFIED | Block deleted (Wave 1B) |
| F13-04 | Takeover in WebRTC input path | VERIFIED | Input refused while takeover active (Wave 1B) |
| F13-05 / F08-13 | C++ ICE candidates + TURN | IMPLEMENTED | Code only (Wave 3C). **Not compiled** — libdatachannel API names unverified |
| F13-06 | Remove `simulate_agent_intent` | VERIFIED | Agent refuses by default (3C) + dashboard stops sending it (Wave 4) |
| F13-07 | Real RTCP ABR | VERIFIED | `getStats()`-based, labelled, nothing fabricated (Wave 3C). No live aiortc peer run |
| F13-08 | Mouse-button held tracking/release | VERIFIED | Both DLL and fallback paths (Wave 3C). Fallback-path caveat recorded |
| F13-09 | Session reconciliation after restart | VERIFIED | Agent-side stale-id refusal (Wave 3C). Plane-side row expiry is a follow-up |
| F13-10 | ICE-complete gate before offer | VERIFIED | Both paths (Wave 3C). Python wiring inspection-only; C++ API names unverified |
| F08-05 / F08-06 / F08-09 / F08-10 / F08-11 / F08-12 / F08-14 / F08-16 | Native fixes (GDI, COM, secure desktop, monitor geometry, CPU monitor, single-instance, CHECK() tests, honest StartSession) | IMPLEMENTED | Code only (Waves 1B/3C). **Not compiled or run** |
| F08-15 | Hand-rolled JSON | DEFERRED | Deliberate keep with written justification (Wave 3C) |
| F-06-13 | DPoP / PAR | DEFERRED | Evaluated, deliberately not implemented (Wave 3C) — gateway has no PAR/DPoP |
| F-06-15 | Proper MCP client | VERIFIED | `initialize()` + session + pagination (Wave 3C) |
| FR-01 | Policy simulation | VERIFIED | Module (3C) + endpoint `POST /api/policy/simulate` (Wave 4) |
| FR-03 | Task result projection | VERIFIED | Client half (3C) + server half (Wave 4) |
| FR-05 | Demo as explicit mode | IMPLEMENTED | Backend `is_demo_mode()` helper only (Wave 3C) |
| FR-07 | SDK retry / idempotency | VERIFIED | Bounded retries + `Idempotency-Key` (Wave 3C) |

## Open P1 rows (10) — no wave addressed them

F-04-15 (Pillow fallback fabricates images), F-04-22 (e-stop ignored by MCP tools), F-05-04 (CORS wildcards), F-06-12 (PKCE optional), F08-02 (service not a supervisor), F08-03 (pipe replay), F08-04 (desktop agent never reads pipe reply), F08-07 (DLL input always-on), TST-09 (no e2e coverage), TST-10 (no failure/recovery tests).

## Open P2 rows (15) — no wave addressed them

F-04-10, F-04-18/F13-11, F-05-08, F-05-09, F-05-12/SEC-12, F-06-16, I-05, PERF-08, TST-13, TST-14, P-07, P-08, P-12, P-15, OV-03.

## Deferred (3)

PERF-01/DB-04 (connection pool — needs checkout refactor + load harness), F-06-13 (DPoP/PAR — deliberate), F08-15 (hand-rolled JSON — deliberate keep).

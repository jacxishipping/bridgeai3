> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 18 — Technical Debt Register

> **Scope.** Deduplicated debt items: duplication, large modules, dead/vestigial code, inconsistent patterns, missing interfaces, weak typing, missing tests-as-debt, and process debt (unlocked deps, unwired suites). Security defects that are also debt are **not** repeated here in full — they live in report 17 with their IDs; this register cross-references them where the debt is the root cause. **Cost** = ongoing price of carrying the debt (maintenance, bug risk, onboarding). **Payoff** = what fixing it buys. **Priority** is this synthesis's judgement (P1 = blocks other work / high bug risk; P2 = significant drag; P3 = tidy-up). No new debt was invented; every item traces to a source report section or a file cited there.
>
> **What "debt" means here.** A design that works today but makes the next change harder, riskier, or slower — distinct from a bug (report 17). Several items are both; the register says so.

## 1. Summary by category

| Category | Items | Top cost driver |
|---|---|---|
| Duplication (functional) | TD-D01–D12 | Every fix must be made 2–4 times; enforcement diverges |
| God modules / size | TD-S01–S08 | `app.py` 5,147 LOC blocks decomposition and review |
| Dead / vestigial / confusing code | TD-X01–X08 | False confidence (tests pass while broken; fake telemetry) |
| Inconsistent patterns | TD-P01–P08 | No single way to do shell/policy/audit/rate-limiting |
| Missing interfaces / weak typing | TD-I01–I07 | Refactors are guesswork; no contract to test against |
| Test & verification debt | TD-T01–T08 | 403 Python tests, 0 executed here; dashboard suite self-referential |
| Process / dependency debt | TD-R01–R06 | Unreproducible builds; manual releases; no deploy automation |

## 2. Duplication

| ID | Item | Files | Cost | Payoff | Priority |
|---|---|---|---|---|---|
| TD-D01 | MCP surface ×3 with three enforcement behaviours | `src/ai_bridge/server.py`; `src/ai_bridge/control/mcp_routes.py`; `src/ai_bridge/control/mcp_client.py` | Policy fixes land in one surface and miss the others (F-06-14: `enforce_mcp_tool_policy()` not called on the `mcp_routes` execute path) | One tool registry + one policy gate for all three surfaces | P1 |
| TD-D02 | Desktop automation ×2 (legacy vs canonical `desktop_*`) | `src/ai_bridge/tools.py` | Same Win32 backends, different names/tiers/`_check` sites; 109-tool surface is really ~2 overlapping APIs | One canonical desktop API; legacy names as thin aliases | P1 |
| TD-D03 | Shell execution ×4 with different timeout/kill/env policies | `src/ai_bridge/execution.py` (`RUNNER`); `src/ai_bridge/tools.py` (`run_shell`, `terminal_exec`); `src/ai_bridge/terminal_manager.py` | Env-leak fix (SEC-07) must be applied per runner; `_run_argv` kills on timeout but not process group, vs `execution._kill` | One shell executor with one env/timeout/kill contract | P1 |
| TD-D04 | Audit ×3 | `src/ai_bridge/audit.py` (JSONL); `src/ai_bridge/control/dispatch_audit.py`; `src/ai_bridge/control/db.py:audit()` + `worm_siem.py` | MCP calls logged nowhere (F-06-09); chain non-atomic (DB-05); three retention stories | One audit writer (DB hash-chain) + one exporter; delete the JSONL path or make it the exporter | P1 |
| TD-D05 | Rate limiting ×2 (+ per-worker memory fallback) | `src/ai_bridge/control/ratelimit.py`; `src/ai_bridge/control/durable_ratelimit.py` | Enforcement is 4× under 4 workers (DIST-07, PERF-02); two code paths to keep consistent | One async-native limiter, Redis-backed in prod, explicit single-worker fallback | P1 |
| TD-D06 | Screen capture ×3 | `src/ai_bridge/webrtc_stream.py:grab_screen`; `src/ai_bridge/screen_capture.py`; `src/ai_bridge/tools.py` fallbacks | Divergent scaling/Pillow-fallback behaviour; fabricated images on Pillow-missing (F-04-15) | One capture module with one fallback policy (fail loudly, never fabricate) | P2 |
| TD-D07 | WebRTC ×2 (Python aiortc default + C++ libdatachannel opt-in) | `src/ai_bridge/webrtc_stream.py`; `windows-agent/src/webrtc/` | Different monitor geometry (F08-10), different ABR behaviour (F13-07), different input enforcement (F13-03/04/05); C++ path unwired and untested | Pick one shipping path; keep the other only behind a feature flag with its own tests, or delete it | P2 |
| TD-D08 | Dashboard ×2 (live workspace vs seeded demo) | `apps/dashboard/src/views/live/*`; `apps/dashboard/src/state/dashboardStore.ts` | Demo is the default experience (D-01); fake verification/cards (D-02/03); every UI change must be made twice or drifts | Demo as an explicit, labelled mode entered only from login; live as the only default | P1 |
| TD-D09 | Task creation ×3 | `src/ai_bridge/control/app.py:submit_task`; `src/ai_bridge/control/ai_api.py:execute`; `src/ai_bridge/control/v1_routes.py:v1_create_task` | Idempotency on one path only (F-05-14); approval on one path only (SEC-01) | One `create_task()` service used by all three routes | P1 |
| TD-D10 | Poll loop ×2 | `agents/common/poller.py`; `agents/windows/gui_app.py:_poll_loop()` | Fixes to one never reach the other (F07-03); GUI Stop does not join worker | GUI becomes a thin UI over the shared `Poller` | P2 |
| TD-D11 | Emergency stop ×2 / human takeover ×2 | `src/ai_bridge/plane_control_flags.py` vs `src/ai_bridge/control/plane_state.py`; `v1_routes._HUMAN_TAKEOVER_BY_DEVICE` vs `plane_state` | In-memory mirrors diverge across workers and are ignored by MCP tools (F-04-22, SEC-08) | One durable source of truth, read everywhere including MCP tools | P1 |
| TD-D12 | Update mechanism ×2 | `src/ai_bridge/updates.py` (signed) vs unsigned `git` fleet/installer/container paths | Signed library unused (P-13); every update path must be fixed separately | Route all update paths through `updates.py` | P1 |

## 3. God modules / size

| ID | Item | Files / size | Cost | Payoff | Priority |
|---|---|---|---|---|---|
| TD-S01 | `control/app.py` god module | 5,147 LOC, 142 routes, all closures in `create_app()` | Cannot review, test, or split a route in isolation; forces single-worker thinking; every change risks every route | Extract `APIRouter` modules by domain (auth, devices, tasks, audit, webrtc, workflows) behind the same app | P1 |
| TD-S02 | `tools.py` god module | 2,990 LOC, 92 top-level defs | Desktop + shell + files + fleet + transfers + recording in one file; `_check` sites inconsistent | Split by tool family; keep `server.py` wrappers thin | P2 |
| TD-S03 | `control/ai_api.py` | 2,284 LOC | Second god module; owns tool catalog, execute path, and AI-provider logic | Split catalog vs execution vs provider settings | P2 |
| TD-S04 | `agents/windows/gui_app.py` + `gui_ui.py` | 2,214 + 1,659 LOC | GUI + poll loop + dispatch in one file (see TD-D10) | Separate UI, poller, and dispatch | P2 |
| TD-S05 | Dashboard mega-views | `AiApiHubView.tsx` 2,773 LOC; `LiveDesktopView.tsx` 2,496 LOC; `dashboardStore.ts` 1,285 LOC | Fake provider cards (D-03) hide inside a 2,773-line view; live desktop logic untestable in place | Split views by panel; extract store slices | P3 |
| TD-S06 | `server.py` | 1,701 LOC, 109 wrappers | 3 `_check(` / 2 `decide(` for 109 tools; wrappers are copy-paste with divergent gates | Generate wrappers from a tool registry with a uniform gate | P2 |
| TD-S07 | `control/custom_agents.py` / `v1_routes.py` | 1,655 / 1,113 LOC | Duplicated claim/task-creation logic (see TD-D09) | Share claim + task services with `app.py` | P3 |
| TD-S08 | `webrtc_stream.py` | 1,348 LOC | Capture + encode + signalling + input + ABR + file transfer in one file; demo hook (`simulate_agent_intent`) lives in the input handler (F13-06) | Split capture/encode, signalling, input handling | P2 |

## 4. Dead / vestigial / confusing code

| ID | Item | Files | Cost | Payoff | Priority |
|---|---|---|---|---|---|
| TD-X01 | Fabricated success values (fake IDR, fake encode time, fake GDI frames, fake Merkle root, fake provider cards) | `windows-agent/src/encoder/mf_h264_encoder.cpp:376-393`; `src/ai_bridge/webrtc_stream.py:409-427`; `windows-agent/src/capture/dxgi_capture.cpp`; `apps/dashboard/src/views/AuditTrailView.tsx`, `AiApiHubView.tsx` | Operators and tests cannot distinguish healthy from broken; telemetry lies (F08-01, F13-01, F08-05, D-02, D-03) | Fail loudly; never synthesise a success value — report `valid=false` + reason | P1 |
| TD-X02 | `tasks.py` backwards-compat shim drifted from registry | `src/ai_bridge/tasks.py:13-36`; `src/ai_bridge/execution.py` | `cpu_summary` in `TASK_REGISTRY` but not a `TASKS` key; non-Linux `_memory_summary` returns a string, not an error (F-04-24) | Delete shim or regenerate it from the registry | P3 |
| TD-X03 | Unused imports / dead code | `src/ai_bridge/client.py:9` (unused `httpx`); `logging_config.py` (unused `traceback`) | Noise; `client.py` imports a heavy dep it does not use | Ruff unused-import rule in CI | P3 |
| TD-X04 | `control/workspace.py` name collision | `src/ai_bridge/workspace.py` vs `src/ai_bridge/control/workspace.py` | Same module name, different purpose (file I/O vs FastAPI routes); every import is a trap (F-04-12) | Rename the routes module (`workspace_routes.py`) | P2 |
| TD-X05 | `desktop_capabilities.py` static hardcoded matrix | `src/ai_bridge/control/desktop_capabilities.py` | No live probing except optional host probe; capability claims can be wrong | Probe or clearly label static data | P3 |
| TD-X06 | `role_catalog.py` catalog only, not enforcement | `src/ai_bridge/control/role_catalog.py` | Looks like a second enforcement layer; is not (report 05 §2.3) | Document as catalog-only or delete | P3 |
| TD-X07 | libwebrtc backend stub | `windows-agent/src/webrtc/peer_connection.cpp:28-48`; `windows-agent/CMakeLists.txt` | Enum + `DescribeAvailable…` exist, but `CreatePeerConnectionSession` only constructs libdatachannel; libwebrtc build returns `nullptr` → desktop exits 2 (F08-19) | Delete the stub or implement it; do not ship a dead backend option | P2 |
| TD-X08 | Legacy WebRTC routes (410-gated but code-present) + legacy session shapes | `src/ai_bridge/control/app.py:3546-3760`; `src/ai_bridge/control/v1_routes.py:573-700` | Legacy and v1 session shapes differ but flow into the same `AgentStreamer`; compatibility by shared key names, not schema (F13-13) | One session schema; delete legacy routes when the flag is off | P3 |

## 5. Inconsistent patterns

| ID | Item | Evidence | Cost | Payoff | Priority |
|---|---|---|---|---|---|
| TD-P01 | Policy enforcement has no single choke point | 3 `_check(` / 2 `decide(` in `server.py`; `workspace.py`/`diagnostics.py`/`git_ops.py` have no `_check` at all; `dispatch()` conflates `approval_granted → is_admin` (`dispatch.py:280`) | Every new tool re-decides where enforcement lives; bypasses are found one entry point at a time (report 12 §4) | One `decide()` call per tool invocation, in the wrapper, before any implementation runs | P1 |
| TD-P02 | Three shell runners, three env/timeout/kill policies | TD-D03; SEC-07 | Same command behaves differently by path; secrets leak on one path only | One executor contract (report 20, F-04-07/SEC-07) | P1 |
| TD-P03 | Audit has three writers and no single retention story | TD-D04; F-05-13; SEC-05 | Retention endpoint deletes what WORM was supposed to preserve | One writer, one retention policy, WORM-before-delete | P1 |
| TD-P04 | Secrets: three backends, fail-open default | `secrets_backend.py` (F-05-10); plaintext MCP OAuth in DB (SEC-06); plaintext agent tokens (F07-06, F08-08); DPAPI on one agent only | Secret handling depends on which path you look at | One secrets contract: fail-closed default, encrypted at rest everywhere | P1 |
| TD-P05 | Rate limiting: three layers, three limits | Middleware 1200/300 per min; docstring 120/min (F-05-11); nginx 30r/s on `/` only; durable DB path vs memory fallback | No single enforced limit; 4× under 4 workers (DIST-07) | One limiter, one documented limit, Redis mandatory in prod | P2 |
| TD-P06 | Error handling: raw exceptions propagate from MCP tools | Only 11 `try/except` in `server.py` (1,701 lines); `isError` never set (F-06-11) | Clients cannot distinguish failure kinds; stack text can leak paths | Per-tool error taxonomy + structured `isError` responses | P2 |
| TD-P07 | JSON parsing: stdlib `json` in Python, hand-rolled substring parsers in C++ | `windows-agent/src/service/windows_service.cpp:26-48`; `http_signaling.cpp:66-110` (F08-15) | C++ parsers mishandle escapes/nesting/duplicates | One JSON library for the native agent | P2 |
| TD-P08 | Timeouts/leases use magic numbers in several places | 900 s lease, ~30 s sweep (but `DEFAULT_SWEEP_INTERVAL_SEC=30` is not the value the loop uses — DIST §7); 600 s WebRTC TTL; 5 s ICE wait; 15 s verify cache | Drift between constants (DIST §7); behaviour changes require hunting literals | One constants module; sweep cadence from the constant, not a hard-coded tick count | P3 |

## 6. Missing interfaces / weak typing

| ID | Item | Evidence | Cost | Payoff | Priority |
|---|---|---|---|---|---|
| TD-I01 | No pydantic/`BaseModel` validation in the MCP path | F-06-10; schemas SDK-derived from type hints + docstrings | Invalid arguments fail late or are coerced unpredictably | `BaseModel` per tool family; validate before dispatch | P1 |
| TD-I02 | No `tests/conftest.py`; fixtures duplicated per test file | Reports 03/15 (INV-09, TST-16) | Every test file re-invents fixtures; suite cannot be collected without full deps (TST-03) | One conftest with shared fixtures; lazy `__init__` (TD-T02) | P2 |
| TD-I03 | `config.py`: no validation of numeric fields; `expandvars` on workspace path | F-04-23 | Misconfiguration fails late or expands unexpectedly | Validated `Settings` (pydantic) cached once | P2 |
| TD-I04 | `settings` god-table: untyped key/value store for secrets-adjacent flags, rate-limit buckets, and plane flags | `control/db.py:110`; DB-06 | No typing/namespacing; rate-limit writes land in the same table as policy flags | Namespaced, typed settings tables | P2 |
| TD-I05 | JSON-in-TEXT everywhere; Postgres gets TEXT, not `jsonb` | DB-09 | No JSON validation/indexing on Postgres; translator cannot map types | `jsonb` on Postgres with a migration | P3 |
| TD-I06 | No schema for WebRTC session shapes (legacy vs v1) | F13-13; TD-X08 | Compatibility by accident | One `Session` model validated at both session-creation paths | P2 |
| TD-I07 | C++: no logging framework; failures return `false` with no `GetLastError`/HRESULT | F08-20 | Operators get only two mains' stdout JSON; native failures are undebuggable in production | One logging facade for the native agent | P2 |

## 7. Test & verification debt

| ID | Item | Evidence | Cost | Payoff | Priority |
|---|---|---|---|---|---|
| TD-T01 | 403 Python tests counted, 0 executed in this environment; 31/58 files import unavailable third-party packages at module scope | TST-01, TST-02 | No signal at all from the suite in the delivered tree; regressions invisible | CI that runs the suite (it exists — `.github/workflows/ci.yml` has 4 jobs) + a published run log in the repo | P1 |
| TD-T02 | `ai_bridge/__init__.py` eagerly imports `httpx` via `.client` | TST-03 / INV-11 (`__init__.py:8`) | Even stdlib-only test modules cannot be collected without the full dependency set | Lazy import; `__init__` exports nothing heavy | P1 |
| TD-T03 | Dashboard suite is self-referential (310/356) and the only real-store suites are unwired | TST-04, TST-05 | False confidence; the suite cannot detect regressions | Wire Tier 5 + challenger suites into `npm test`; rewrite Tier 1/2 to import real modules | P1 |
| TD-T04 | No failure/recovery tests; soak scripts unwired | TST-10 | Crash/restart/DB-failure/duplicate/cancel behaviour is untested — the exact area where DIST findings cluster | Wire `scripts/k6/` + `scripts/locust/` soak scripts into CI; add crash/restart tests | P1 |
| TD-T05 | No agent tests for the core loop | F07-15: no `test_poller.py`/`test_spool.py`/`test_agent_main*.py` | `_effective_policy`, spool ordering, rotation, cancel, backoff untested | Unit tests for poller/spool/policy merge | P2 |
| TD-T06 | No integration tests under `integrations/`; no recorded execution of any agent script against a live plane | I-11 | Every integration claim is unverified | One smoke test per integration against a local plane | P2 |
| TD-T07 | C++ tests are `assert()`-only, stripped in Release, and pass while broken | F08-14; TST-14 (6 asserts, Windows-only, not executed here) | Fake IDR (F08-01) and black GDI frames (F08-05) pass their own tests | Real test framework (e.g. Catch2/GoogleTest), Debug+Release CI runs | P1 |
| TD-T08 | No load-test harness; no WebRTC E2E test with a real browser/aiortc peer; no Postgres boot test | Report 14 rec. §9.6; report 13 §14 Q4; DB-03 | Every performance claim is a hypothesis; Postgres boot against inline `SCHEMA` is unverified | Harness + E2E WebRTC test + CI Postgres boot before any perf claim | P2 |

## 8. Process / dependency debt

| ID | Item | Evidence | Cost | Payoff | Priority |
|---|---|---|---|---|---|
| TD-R01 | `requirements.txt` not locked; no lockfile/hashes; unpinned extras in `Dockerfile.prod`; `npm install` not `npm ci` | P-07 | Unreproducible builds; supply-chain drift | Lockfiles + hashes; `pip-compile` / `npm ci` everywhere | P1 |
| TD-R02 | No deployment automation in CI — no image build/push, deploy job, container/secret scan | P-16 | Releases are manual script runs; nothing verifies what shipped | CI deploy + scan jobs | P2 |
| TD-R03 | `.env.example` omits ≥11 variables compose actually consumes | P-06 | Every fresh deploy is a debugging session | Generate `.env.example` from compose + code | P2 |
| TD-R04 | Three default control URLs; hard-coded prod host everywhere | P-12; I-01 | Users following two guides get two servers; verbatim copies phone home to the operator's prod server | One required `CONTROL_URL` parameter; no default prod host | P1 |
| TD-R05 | No rollback/down migrations; migration runner `;`-splits and silently no-ops | DB-08; INV-10 | A bad migration cannot be rolled back; installed deployments can silently diverge | Down migrations + a migration checksum/verification step in CI | P2 |
| TD-R06 | No git history in the delivered tree; no run logs for release-time test claims | OV-10/INV-01; TST-08 | Provenance unverifiable; 29/33/356 pass claims cannot be checked | Ship the repo with history (or a signed release manifest + run logs) | P3 |

## 9. Debt that is also a bug (cross-reference only — see report 17)

TD-D01→F-06-14 · TD-D02→F-04-06 · TD-D03→SEC-07/F-04-07 · TD-D04→F-06-09/DB-05 · TD-D05→DIST-07/PERF-02 · TD-D07→F13-03/04/05/07 · TD-D08→D-01/02/03 · TD-D09→SEC-01/F-05-14 · TD-D11→F-04-22/SEC-08 · TD-D12→P-13 · TD-X01→F08-01/F13-01/D-02 · TD-X07→F08-19 · TD-P01→F-04-05/SEC-01/02/04 · TD-P04→SEC-06/07/F07-06/F08-08 · TD-T07→F08-01/F08-05/F08-14.

## 10. What to pay down first (this synthesis)

1. **TD-T02 + TD-T01** — make the test suite collectable and runnable; without signal, every other fix is unverifiable.
2. **TD-D01 + TD-P01** — one tool registry, one policy gate; this is the fix for the largest cluster of P0/P1 bypasses.
3. **TD-D09 + TD-D04** — one task-creation path, one audit writer; closes the alternate-entry-point bypasses.
4. **TD-D11 + TD-D05** — one durable source of truth for stop/takeover; one limiter; prerequisites for multi-worker operation.
5. **TD-R01 + TD-R04** — lockfiles and no default prod host; cheapest supply-chain wins.
6. **TD-S01** — split `app.py` by domain; do it incrementally behind the same routes (report 20, roadmap item R-20).

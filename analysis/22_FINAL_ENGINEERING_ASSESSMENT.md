# 22 — Final Engineering Assessment

> **How to read this report.** This is the executive synthesis of all 18 source audit reports plus the 6 synthesis reports written in this pass (02, 17–21). Every claim cites a file or a report section. **VERIFIED** = observed in code or counted by grep. **INFERRED** = a source track's inference, labelled as such. No measurement is presented as a benchmark — no load test, profiling run, or live E2E execution exists in any track (report 15: 0 Python tests executed here; report 14: all perf claims are hypotheses; report 13: no FPS/latency measurement exists). Scores are this synthesis's judgement from the cited evidence, on the brief's 1–10 scale. They are deliberately not inflated: a present-but-broken feature scores low.

## 1. What AI Bridge actually does

AI Bridge is a **real, substantial, partially-working remote-computer-control platform** (report 01, OV-01). An MCP-compatible AI client (or a human in the React dashboard) submits an operation; the control plane authenticates it, checks a 4-tier policy, and either executes it locally or queues it as a task; an outbound-polling agent on a Linux, Windows, or macOS endpoint claims the task, executes it, and reports the result; the plane stores the result and writes a hash-chained audit row. A second, parallel capability streams the endpoint's desktop over WebRTC (Python aiortc by default) and forwards mouse/keyboard input from the viewer.

What it **does well today** (VERIFIED): outbound-only agent polling (the plane never opens an inbound shell); atomic task claim with a 900 s lease; idempotent `POST /api/tasks`; a deny-by-default 4-tier policy model; PBKDF2 passwords and SHA-256 token hashes; a strong outbound OAuth 2.1/PKCE implementation; a well-engineered live dashboard workspace (`#/live`); and clean C++ RAII/crypto discipline in the native agent.

What it **does not do**, despite the docs: it does not enforce its own approval policy uniformly (three alternate entry points bypass it); it does not audit MCP tool calls at all; it does not isolate tenants; it is not safe to run with more than one worker although production compose runs four; its dashboard default experience is a seeded simulation with fabricated verification and fake provider credentials; its native Windows agent's service does not supervise anything and its encoder fabricates success; and its test suite — 403 Python tests counted, 356/364 dashboard tests — was executed **nowhere** in any audit (0 passed / 0 failed / 0 skipped / 58 files blocked in this environment).

## 2. How it works end-to-end (summary)

Full map: report 02. In one paragraph: an MCP client connects over stdio (no auth) or Streamable HTTP (bearer admin token or gateway-verified token, unless `AI_BRIDGE_HTTP_ALLOW_ANON=1` disables it — P0). The MCP server (`server.py`, 109 tools) applies policy inconsistently — 3 `_check(` calls for 109 wrappers — and, for device-bound sessions, skips local policy entirely in favour of the control plane. The control plane (`control/app.py`, 5,147 lines, 142 routes) authenticates via opaque 8 h sessions, a default-on legacy admin token, or 7-day device tokens; authorises by role (viewer/operator/admin); and dispatches either locally on the plane host (for any operator — P0) or by queueing a task. Agents long-poll `GET /api/agent/tasks/next` (atomic claim), execute in-process in the poll thread, spool the result to local SQLite, and POST it back. The plane writes the result and a hash-chained audit row (non-atomically — the chain can fork). The dashboard talks REST + SSE + WS to the plane; live desktop uses WebRTC signalling through the plane with peer-to-peer DTLS-SRTP media. Deployment is Docker Compose (prod, 8 services), Render (test-only), or AWS CloudFormation (template only, never evidenced as applied).

## 3. Implementation status matrix (VERIFIED)

| Capability | Status | Evidence |
|---|---|---|
| MCP server: 109 tools, stdio + Streamable HTTP | **Fully implemented** (works, with gaps) | Reports 04/06; 109 `@mcp.tool()` VERIFIED |
| MCP: resources, prompts, sampling, roots, elicitation, progress, cancellation | **Not implemented** | F-06-18 (NOT FOUND by grep) |
| MCP: audit of tool calls | **Not implemented** | F-06-09 (P0) |
| Control plane: auth, RBAC, tasks, approvals, audit, workflows, supervisor | **Partially implemented** | Report 05; approval works on `/api/tasks` only (SEC-01/02/04) |
| Control plane: multi-worker operation | **Not implemented safely** | DIST-01/03/04/07; docs say single-worker, compose runs 4 |
| OIDC | **Code present, flow unverified** | DOC-14; DIST-04 (~3/4 failure under 4 workers) |
| mTLS | **Code present, header-trust only, one route** | F-05-06/SEC-09; DOC-13 (three doc statuses) |
| Linux agent | **Implemented, dangerous default** | F07-02 (Tier 4 by default) |
| Windows Python agent | **Implemented** (duplicate poll loop) | F07-03 |
| macOS agent | **Code only, never run on macOS** | INV-08, F07-11, DOC-15 |
| Windows native C++ agent | **Partially implemented** (service is not a supervisor; libwebrtc is a stub) | F08-02, F08-19 |
| WebRTC Python path | **Implemented** (view-only/takeover unenforced for remote) | F13-03 (P0), F13-04 |
| WebRTC C++ path | **Opt-in, unwired, STUN-only** | F13-05, F08-13 |
| Dashboard live workspace | **Implemented, best-engineered part** | D-14 |
| Dashboard demo (default) | **Implemented — as a simulation presented as a fleet** | D-01/02/03 |
| AI integrations (ChatGPT/Claude/Codex/Gemini) | **Docs/config only** | INV-07; only `custom-ai/` has code |
| Custom AI agents (OpenAI/Anthropic/LangChain/Ollama) | **Implemented, with gaps** | I-02 (Ollama single-turn), I-04 (no approval handling) |
| Signed updates library | **Implemented, unused by fleet/installer paths** | P-13 (P0) |
| Postgres / Redis in prod | **Implemented in compose, unverified in operation** | DB-03 (PG boot unverified), DIST (Redis is rate-limit only) |
| Backups (Postgres) | **Not implemented** | DB-01 |

## 4. Where the real vulnerabilities are

The sixteen P0 items (report 17 §2) cluster into four stories:

1. **The approval gate is optional.** `/api/v1/tasks` (SEC-01), workflow steps (SEC-02), and `term-*` terminal tasks (F-05-03) all bypass it; `Policy.unrestricted` and ceiling-4 (F-04-08) and the Linux agent's Tier-4 default (F07-02) switch it off by configuration; the agent-side policy merge can only widen (F07-01). Report 12's invariant verdicts: invariant 2 ("sensitive ops cannot bypass server policy") **FAILS**; invariant 7 **PARTIAL**; invariant 10 (audit append-only) **FAILS as stated**.
2. **Authentication can be switched off or is missing.** Env vars disable MCP HTTP auth and DNS protection (F-04-03); stdio has no auth and shares one session key (F-06-02); the legacy admin token is default-on (F-05-05); tokens are stored plaintext on Linux and in the native config (F07-06, F08-08); MCP OAuth secrets are plaintext in the DB (SEC-06); `run_shell` children inherit `DATABASE_URL` and other secrets (SEC-07).
3. **Tenancy and audit do not exist as enforced properties.** Tenant is caller-supplied (SEC-03); MCP calls are never audited (F-06-09); an admin can delete audit history (SEC-05); the audit chain can fork (DB-05).
4. **Remote control is not actually controlled.** View-only mode is unenforced for remote agents (F13-03); human takeover is desktop-only and unchecked in the WebRTC input path (SEC-08, F13-04); the local branch executes on the plane host for any operator (F-05-01); fleet updates are unsigned (P-13); production ships with Tier 3 on and known default secrets (P-01, P-02); the native encoder fabricates success while the viewer sees a frozen frame (F08-01).

The honest summary from report 12: **core primitives are sound; enforcement is entry-point-dependent, not uniform.** That is the sentence the whole assessment turns on.

> **HISTORICAL (Nov 2026 audit).** Current verdict: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md). Do not treat the NO-GO scores below as current product status.

## 5. What limits production readiness

In order: (1) the P0 bypasses above; (2) multi-worker unsafety while prod compose runs 4 workers (DIST); (3) no tested Postgres backup/restore and no referential integrity (DB-01/02); (4) a test suite that has never been executed in any audit and is partly self-referential (TST-01/04); (5) deployment defaults that are unsafe as shipped (P-01/02/03/04/09/13); (6) a dashboard whose default experience is a simulation with fabricated verification (D-01/02/03); (7) documentation that overstates the product in ≥15 material places (DOC-31). Full verdict: report 21 — **NO-GO for any production use**.

## 6. Engineering scorecard

| Category | Score | Evidence and reasoning |
|---|---|---|
| Architecture | **4/10** | Coherent request flow, outbound-only polling, atomic claim — but a 5,147-line god module, single-worker constraint, three MCP surfaces, two WebRTC stacks, two dashboards, and no queue/pub-sub despite compose claiming Redis provides them (reports 02 §9, 05, DIST). |
| Python code quality | **4/10** | No `shell=True`/`eval`/`exec`/`pickle`, argv-form subprocess, clear module layout — but god modules (`tools.py` 2,990 LOC), inconsistent `_check` sites, regex-only redaction, silent fallbacks that fabricate data, and `get_settings()` uncached (report 04). |
| C++ code quality | **5/10** | Best C++ score: RAII/`ComPtr`, no raw `new`/`delete`, correct HMAC + constant-time compare, correct DPAPI use — but fake-IDR fabrication, COM lifetime bugs, hand-rolled JSON, no logging, `assert()`-only tests stripped in Release (report 08). |
| Frontend architecture | **5/10** | Live workspace is the best-engineered part (401-aware, never falls back to demo, generation counters, visibility-aware polling) and the API layer is sound — but demo is the default, Merkle verification and provider cards are fabricated, tokens travel in URLs, and the test suite is self-referential (report 09). |
| MCP implementation | **4/10** | Broad surface (109 tools), strong outbound OAuth 2.1/PKCE — but no audit, 5 tools untiered, ~40 wrappers ungated, no pydantic validation, protocol frozen at 2024-11-05, no resources/prompts, outbound client skips `initialize` (report 06). |
| Security | **3/10** | Sound primitives (deny-by-default tiers, PBKDF2, SHA-256 tokens, no `eval`) — but enforcement is entry-point-dependent: three approval bypasses, tenant unenforced, audit deletable, secrets plaintext/leaked, takeover desktop-only (report 12 invariants: 2 FAILS, 7 PARTIAL, 10 FAILS). |
| Authentication | **4/10** | Opaque tokens, hash-only storage, 7-day device tokens with rotation, OIDC RS256/JWKS verified — but legacy token default-on, stdio unauthenticated, anon off-switch, PKCE optional, plaintext token storage on two platforms (reports 05/06/07/12). |
| Authorisation | **3/10** | A real 4-tier model with deny-by-default — but bypassed via v1/workflow/terminal, widenable by the agent, untiered tools, no tenant binding, and a DLL input surface that is always-on (reports 06/07/12, F08-07). |
| Database architecture | **4/10** | Schema reasonably complete (22 tables, 16 indexes), hash-chained audit, SQLite/Postgres dual-engine intent — but zero FKs/CHECKs, dual schema source of truth with unverified Postgres boot, single serialised connection, no PG backup, non-atomic chain (report 11). |
| Distributed reliability | **3/10** | Task claim and `POST /api/tasks` idempotency are verified multi-worker-safe — but scheduler, WS fan-out, OIDC, CSRF, and rate limiting are not; execution is at-least-once with no fencing and no progress heartbeat (DIST). |
| WebRTC implementation | **4/10** | Python aiortc path ships and e-stop is checked per input message — but view-only unenforced for remote agents (P0), takeover unchecked, C++ path drops ICE candidates and has no TURN, encode telemetry partly fabricated, no perf evidence at all (report 13). |
| Performance | **4/10** | Plausibly fine at ≤20 devices / 1 worker (INFERRED from code structure) — but the single serialised DB connection, per-request DB write on the rate-limit path, multi-MB `SELECT *` lists, and N+1 fleet health cap the ceiling, and **no benchmark exists** (report 14: all impact claims are hypotheses). |
| Scalability | **3/10** | Single-worker dev topology is coherent — but 4-worker prod is unsafe, 100+ devices is "not credible without pooling, projection, indexes, retention", and multi-region is "do not attempt without a redesign" (report 14 §7). |
| Testing | **2/10** | 403 Python tests exist and `py_compile` passes on 161 files; a 13/13 manual spot-check passed — but 0 tests were executed in any audit (58/58 Python files blocked), 310/356 dashboard tests are self-referential, the only real-store suites are unwired, and there are no failure/recovery tests (report 15). |
| Maintainability | **4/10** | Codebase is navigable and honestly documented in places — but duplication is systemic (report 18: 12 duplication items), god modules dominate, no `conftest.py`, unlocked dependencies, and three default control URLs (reports 03/18). |
| Documentation | **4/10** | `implementation-status.md` and `production-readiness.md` are unusually honest and largely accurate — but README/TEST_READY/PROJECT/SECURITY overstate in ≥15 material places, several docs are 14–26-line stubs, and the R1–R5 baseline file does not exist (report 16). |
| Production readiness | **2/10** | The system runs (dev topology is coherent; compose brings up 8 services) — but with 16 P0 findings, unsafe defaults, no tested backup, no executed test suite, and multi-worker unsafety, it cannot be operated in production. The 2 reflects that it is a working lab system, not a broken one (report 21). |

**Scorecard notes.** No category scores above 5. That is intentional and consistent with the evidence: every area has at least one P0/P1 finding, and Testing (2) and Production readiness (2) are the lowest because they are the areas where claims most exceed evidence. C++ (5) and Frontend (5) are the highest because each contains a genuinely well-engineered core (RAII/crypto discipline; the live workspace) — both are dragged down by dishonest success reporting in their own subsystems.

## 7. What to refactor, improve, and add

- **Refactor (report 20):** Phase 0–1 close the 16 P0 items; one wrapper gate, one task-creation service, one audit writer; agent policy merge becomes intersection-only; `app.py` split by domain behind `APIRouter`s; duplication programme (report 18 §10). Necessary fixes are separated from optional redesign in report 20 §5 — notably, do **not** rewrite as microservices, do **not** make the C++ path the default, and do **not** attempt multi-plane federation.
- **Improve:** make the test suite runnable and real (CI + published logs, Tier 5 wired in, E2E + failure/recovery tests); make multi-worker operation safe (atomic scheduler claim, fencing, Redis pub/sub, shared OIDC/CSRF/e-stop state); make the dashboard honest (demo off by default, fake verification/cards deleted); lock dependencies and remove the hard-coded prod host.
- **Add (report 19):** build now — policy simulation (FR-01), task result projection (FR-03), demo-as-explicit-mode (FR-05), error taxonomy (FR-06), SDK retry (FR-07), test-suite wiring (FR-08). Build after prerequisites — orchestration, scheduling, agent memory, fleet analytics, security monitoring, automated recovery, enterprise identity. **Reject for now** — multi-region federation, public multi-tenant SaaS, unattended Tier-4 by default, AI-driven auto-approval, growing the MCP tool count before consolidation, and the C++ WebRTC path as default (report 19 §4).

## 8. Risks and open questions (stated, not resolved)

- **Unverified by any track (needs a live test):** Postgres boot against inline `SCHEMA` (DB-03); audit-chain fork under concurrency (DB-05); webhook SSRF via DNS rebinding/redirect (SEC-11); whether `perform_approved_ui_action()` re-checks takeover (F13-04 open question); whether the C++ WebRTC path can connect behind symmetric NAT (F13-05, inferred); whether `mcp_routes.py` OAuth state/callback has further issues (report 05 §7, not fully read); the exact `install_package` allowlist and `_allow_browser_no_sandbox` env name (report 04, NOT FOUND).
- **Counts reconciled in this synthesis (report 02 §1):** dashboard tests 375 on disk / 364 in-scope / 367 `npm test` total (report 09 correct; report 15's 356 breakdown stale); Python tests 403 VERIFIED (README "75" wrong); `app.py` 142 routes VERIFIED (report 12's "~120" discarded; 212 is the control-plane total).
- **Residual uncertainty:** all findings are from read-only code inspection; no server, agent, or binary was run, no live handshake or exploit was performed, and no performance figure in any report is a measurement.

## 9. Cross-reference index — all 24 reports

| # | Report | Subject | Key IDs |
|---|---|---|---|
| 01 | `01_MASTER_PROJECT_OVERVIEW.md` | Project overview, what is real vs claimed | OV-01–OV-10 |
| 02 | `02_COMPLETE_ARCHITECTURE_MAP.md` | **(this synthesis)** Full system map, data flows, trust boundaries, Mermaid diagrams | — (synthesis) |
| 03 | `03_SOURCE_CODE_INVENTORY.md` | Source inventory, counts, missing files | INV-01–INV-15 |
| 04 | `04_MODULE_BY_MODULE_ANALYSIS.md` | Python core, module by module | F-04-01–F-04-24 |
| 05 | `05_CONTROL_PLANE_ANALYSIS.md` | Control plane, request lifecycle | F-05-01–F-05-20 |
| 06 | `06_MCP_PROTOCOL_AUDIT.md` | MCP protocol audit | F-06-01–F-06-20 |
| 07 | `07_PLATFORM_AGENT_ANALYSIS.md` | Linux/Windows/macOS agents | F07-01–F07-15 |
| 08 | `08_WINDOWS_NATIVE_CPP_AUDIT.md` | Native Windows C++ audit | F08-01–F08-20 |
| 09 | `09_DASHBOARD_ARCHITECTURE_AUDIT.md` | Dashboard: live vs demo | D-01–D-14 |
| 10 | `10_AI_INTEGRATIONS_ANALYSIS.md` | AI integrations | I-01–I-12 |
| 11 | `11_DATABASE_AND_STORAGE_AUDIT.md` | Database and storage | DB-01–DB-11 |
| 12 | `12_SECURITY_AND_THREAT_ASSESSMENT.md` | Security and threat assessment, 10 invariants | SEC-01–SEC-14 |
| 13 | `13_WEBRTC_AND_DESKTOP_CONTROL_AUDIT.md` | WebRTC and desktop control | F13-01–F13-15 |
| 14 | `14_PERFORMANCE_AND_SCALABILITY_AUDIT.md` | Performance and scalability (hypotheses only) | PERF-01–PERF-12 |
| 15 | `15_TESTING_AND_RELIABILITY_REPORT.md` | Testing and reliability | TST-01–TST-16 |
| 16 | `16_DOCUMENTATION_CONSISTENCY_REPORT.md` | Documentation consistency | DOC-01–DOC-31 |
| 17 | `17_BUG_AND_VULNERABILITY_REGISTER.md` | **(this synthesis)** Deduplicated P0–P2 register (174 rows) | All P0–P2 IDs |
| 18 | `18_TECHNICAL_DEBT_REGISTER.md` | **(this synthesis)** Technical debt register | TD-D/S/X/P/I/T/R |
| 19 | `19_FEATURE_RECOMMENDATIONS.md` | **(this synthesis)** Feature recommendations, incl. 6 rejections | FR-01–FR-24, FR-R01–R06 |
| 20 | `20_REFACTORING_ROADMAP.md` | **(this synthesis)** P0–P4 roadmap, full P0/P1 entries | R-P0-01–16 |
| 21 | `21_PRODUCTION_READINESS_REPORT.md` | **(this synthesis)** Go/no-go per area | — |
| 22 | `22_FINAL_ENGINEERING_ASSESSMENT.md` | **(this synthesis)** Executive synthesis + scorecard | — |
| A1 | `DEPLOYMENT_AUDIT.md` | Deployment audit (appendix) | P-01–P-18 |
| A2 | `DISTRIBUTED_RELIABILITY_AUDIT.md` | Distributed reliability audit (appendix) | DIST-01–DIST-11 |

**Reading order for a new owner:** 22 (this report) → 02 (map) → 17 (what is broken) → 21 (verdict) → 20 (what to do first) → 12 (security) → 05 (control plane) → 06 (MCP) → 18/19 (debt and features).

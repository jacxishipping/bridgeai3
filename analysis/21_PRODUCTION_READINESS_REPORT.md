# 21 — Production Readiness Report

> **HISTORICAL (17 Nov 2026).** Kept for audit trail only.  
> **Current assessment:** [`PRODUCTION_READINESS.md`](./PRODUCTION_READINESS.md) (23 Nov 2026).

> **Original verdict: NO-GO for any production use.** Not "go with caveats", not "go for a pilot with production data". AI Bridge may be used today only as a **local development / lab system** on machines and data you can afford to lose, with no production credentials, no production fleet, and no unattended Tier-3/4 execution. This verdict follows from the P0 findings in report 17 — any one of the P0 approval/policy bypasses would be enough; there are sixteen.
>
> **Basis.** All 18 source audit reports (01, 03–16, DEPLOYMENT_AUDIT, DISTRIBUTED_RELIABILITY_AUDIT), reconciled in report 02 §1. No production system was contacted; no load test was run; no measurement in this report is presented as a benchmark. Where a track says "needs a live test", this report says so.

## 1. Verdict per area

| Area | Verdict | Blocking items (report 17 IDs) | What must change first |
|---|---|---|---|
| Security | **NO-GO** | F-04-03/F-06-01, F-04-08, F-05-01, F-05-03/SEC-04, SEC-01, SEC-02, SEC-03, F-06-04, F07-01, F07-02, SEC-06, SEC-07, SEC-08 | Close all approval/policy bypasses; remove env-var off-switches; tenant binding; secrets encrypted at rest + scrubbed from child env |
| Authentication | **NO-GO** | F-05-05 (legacy token default-on), F-06-01, F-06-02, F-06-12, F07-05, F07-06, F08-08, P-02, P-09 | Legacy token off by default; stdio session isolation; PKCE enforced; tokens sealed, never plaintext |
| Authorisation | **NO-GO** | F-04-05/F-06-05, F-04-04/F-06-07, SEC-01/02/04, F07-01/02, F-06-04 | One wrapper gate on all 109 tools; one approval gate on all entry points; agent cannot widen policy |
| Database | **NO-GO** | DB-01 (no PG backup), DB-02 (no FKs), DB-03 (dual schema, PG boot unverified), DB-05 (chain forks) | Tested PG backup/restore; FKs; single schema source + CI PG boot; atomic audit chain |
| Distributed reliability | **NO-GO** | DIST-01, DIST-02/F07-04, DIST-03, DIST-04, DIST-05, DIST-08, TST-15 | Scheduler atomic claim; fencing; Redis pub/sub or documented long-poll-only; OIDC/CSRF/e-stop in shared state |
| WebRTC / desktop control | **NO-GO** | F13-03 (P0), F13-04, F13-05, F08-01, F08-07 | View-only + takeover enforced agent-side; C++ ICE/TURN or Python-only; no fabricated frames; DLL input gated |
| Dashboard | **NO-GO as shipped default** (live workspace itself: conditional GO) | D-01, D-02, D-03, D-07, D-08 | Demo off by default; fake verification/cards deleted; tokens out of URLs and localStorage |
| Deployment | **NO-GO** | P-01, P-02, P-03, P-04, P-05, P-09, P-13, P-15 | Tier 3 off by default; no default secrets; HTTPS redirect; `/metrics` authed; signed updates everywhere; Render labelled test-only |
| Testing | **NO-GO** | TST-01, TST-02, TST-04, TST-05, TST-09, TST-10, F08-14 | CI runs the suite with a published log; dashboard suite rewired to real modules; E2E + failure/recovery tests |
| Documentation | **NO-GO as a trust source** | DOC-01–08, DOC-12–15, DOC-19, DOC-31 | Correct the ≥15 overstatements; one status per feature; restore or drop `ORIGINAL_REQUEST.md` |
| MCP implementation | **NO-GO** | F-06-01, F-06-04, F-06-09, F-06-05, F-06-12 | Auth always on; all tools tiered + gated; audit on; PKCE enforced |
| Platform agents | **NO-GO** | F07-01, F07-02, F07-06, F07-07, F07-09, F07-11 | Restrictive default policy; sealed tokens; worker-thread execution; graceful shutdown; macOS verified or removed from supported list |

**The only component that could be called production-shaped today:** the live dashboard workspace (`#/live`, D-14) and the outbound OAuth 2.1/PKCE implementation (report 06 §5.1) — and both sit behind the same broken approval/audit/multi-worker foundation, so neither changes the overall verdict.

## 2. Blocking items (all 16 P0 + the P1 items that block production)

### P0 — must all be closed (report 20 §2 has the full entries)
1. F-04-03/F-06-01 — env vars disable MCP auth + DNS protection
2. F-04-08/SEC-13 — `unrestricted` bypass; silent Tier-4 authorisation
3. F-05-01 — local execution on the control-plane host
4. F-05-03/SEC-04 — `term-*` auto-approval
5. F-06-04 — 5 MCP tools untiered
6. F-06-09 — no MCP audit logging
7. F07-01 — agent policy can only widen
8. F07-02 — Linux agent Tier 4 by default
9. F08-01 — fake H.264 IDR fabricates success
10. SEC-01 — `/api/v1/tasks` bypasses approval
11. SEC-02 — workflow steps run as admin
12. SEC-03 — tenant caller-supplied
13. F13-03 — view-only unenforced for remote agents
14. P-01 — Tier 3 ON by default in production
15. P-02 — known default secrets
16. P-13 — unsigned fleet update

### P1 — blocking for production (subset; full list in report 17 §3)
- **Auth:** F-05-05 legacy token default-on; F-06-02 stdio no auth; F-06-12 PKCE optional; F07-05/06 token plaintext/downgrade; F08-08 native plaintext config; SEC-06/DB-06 plaintext MCP OAuth secrets; SEC-07 env leak to `run_shell` children.
- **Authorisation:** F-04-05/F-06-05 sparse policy; F-04-04/F-06-07 `select_device`/broadcast; F-06-06 approvals that cannot unlock tools; F-04-01 unscoped approvals; F-04-22 e-stop ignored by MCP tools; SEC-08 takeover desktop-only; F13-04 takeover unchecked in WebRTC.
- **Multi-worker:** DIST-01 scheduler duplication; DIST-03 WS fan-out; DIST-04 OIDC across workers; DIST-05 late-result overwrite; DIST-08 long-task requeue; F-05-02/SEC-10 CSRF off by default + per-process secret; F-05-06/SEC-09 mTLS header-trust on one route; PERF-02/DIST-07 rate limit 4×.
- **Data safety:** DB-01 no PG backup; DB-02 no FKs; DB-03 dual schema; DB-05 audit chain forks; SEC-05 audit retention deletes history; PERF-01/DB-04 single serialised DB connection.
- **Deployment:** P-03 no HTTPS redirect; P-04 public `/metrics`; P-05 nginx OAuth wiring missing; P-09 `curl -k` enrolment.
- **Trustworthiness of the product itself:** D-01/D-02/D-03 demo fabrication; TST-04/TST-05 test suite false confidence; F08-05/F13-01 fabricated frames/telemetry; F08-07 DLL input always-on.

## 3. What must change before any production use (checklist)

**Phase 0 — stop the bleeding (report 20 Phase 0):**
- [ ] Delete `AI_BRIDGE_HTTP_ALLOW_ANON` / `AI_BRIDGE_MCP_DISABLE_DNS_PROTECTION` off-switches (R-P0-01)
- [ ] Delete `term-*` auto-approval (R-P0-04)
- [ ] Tier 3 off by default; no default secrets; compose refuses to start without secrets (R-P0-14/15)
- [ ] HTTPS redirect; `/metrics` authenticated; installer without `curl -k` (P-03/04/09)
- [ ] CI runs the Python suite and publishes the log (TST-01)

**Phase 1 — close the bypasses (report 20 Phase 1):**
- [ ] `unrestricted` + Tier-4 rules fixed; Linux agent restrictive by default; agent merge is intersection-only (R-P0-02/07/08)
- [ ] Local execution on the plane host removed or admin-only + audited (R-P0-03)
- [ ] All 109 tools tiered + gated by one wrapper gate (R-P0-05, R-P1-05)
- [ ] MCP audit logging live through one writer (R-P0-06)
- [ ] v1 + workflow + terminal approval gates uniform (R-P0-10/11, R-P0-04)
- [ ] Tenant bound to the session server-side (R-P0-12)
- [ ] View-only + takeover enforced agent-side (R-P0-13)
- [ ] Signed updates on all paths (R-P0-16); fake IDR deleted (R-P0-09)

**Phase 2 — multi-worker + data safety (report 20 Phase 2):**
- [ ] Scheduler atomic claim; fencing tokens; result guard; OIDC/CSRF/e-stop in shared state; WS fan-out via Redis pub/sub (DIST-01/03/04/05, F07-04)
- [ ] 4-worker load test passes with the configured rate limit (PERF-02)
- [ ] Tested Postgres backup/restore; FKs; single schema source + CI PG boot; atomic audit chain + verifier (DB-01/02/03/05)
- [ ] Secrets encrypted at rest; child env allowlisted (SEC-06/07)

**Phase 3 — honesty (report 20 Phase 3):**
- [ ] Demo off by default; fake Merkle verification and fake provider cards deleted (D-01/02/03)
- [ ] Dashboard test suite rewired to real modules; Tier 5 included (TST-04/05)
- [ ] Docs corrected; one status per feature (DOC sweep)

**Re-verification required before a GO:** a fresh, independent audit pass over the P0/P1 fixes (not a self-review), plus: the 10-step E2E workflow (brief Part 10) passing against a local plane; a 4-worker load test; a Postgres restore drill; a WebRTC E2E test with a real browser peer; and a run of the full Python + dashboard suites with published logs. None of these exists today.

## 4. What "production" would also require (beyond the blocking list)

- **Backups/DR:** tested restore, PITR, retention policy (DB-01, DB-07).
- **Observability:** authenticated `/metrics`, native logging (F08-20), structured logs, alerting on audit anomalies (FR-20).
- **Release discipline:** locked dependencies + hashes (P-07), signed releases actually verified by the update path (P-13), CI deploy/scan jobs (P-16), down migrations (DB-08).
- **Supportability:** no hard-coded prod host (I-01), one default control URL (P-12), complete `.env.example` (P-06), rewritten API reference (DOC-22).
- **Scale evidence:** a load-test harness and real measurements before any claim about device counts (report 14: "not credible at 100+ devices without pooling, projection, indexes, retention").

## 5. Acceptable uses today (lab only)

- Local single-worker development against SQLite, on a machine with no production credentials, no production data, and no network exposure (bind `127.0.0.1`; do not set `AI_BRIDGE_HTTP_ALLOW_ANON`, `AI_BRIDGE_ALLOW_TIER3`, or `unrestricted`).
- Studying the codebase; running the (currently unexecuted) test suite locally after installing dependencies.
- Demoing the dashboard's seeded demo **as a demo**, labelled as such, with no implication that the numbers are a real fleet.

## 6. Cross-references

- Blocking items: report 17 (register) and report 20 (roadmap, full entries).
- Per-area evidence: security report 12; control plane report 05; MCP report 06; agents report 07; native report 08; dashboard report 09; DB report 11; WebRTC report 13; performance report 14; testing report 15; docs report 16; deployment DEPLOYMENT_AUDIT; distributed DISTRIBUTED_RELIABILITY_AUDIT.
- Scorecard: report 22.

# FINAL SECURITY REASSESSMENT — AI Bridge Remediation

**Date:** 2026-11-17 · **Repo:** `~/workspace/ai-bridge-main` · **Head:** `61faa67` (Wave 4) · **Baseline for comparison:** report 12 (Security and Threat Assessment) and report 17 §2 (the 16 P0 rows).

**Method.** Each P0 is re-evaluated against the current tree. "Verified at HEAD" means the author confirmed the code with `grep`/`git show` for this report. "Per wave report" means the evidence is the wave's own (test names, file:line) and was not re-run here. Report 12's ten security invariants are re-scored where the evidence allows. Nothing in this report is based on a live exploit, a live handshake, or a run against production — no production system was contacted, and no such test was authorised.

**Headline:** all 16 P0 approval/policy bypasses are closed in code (15 fully, 1 code-only). The system is no longer trivially bypassable through the three alternate entry points, the policy model, or the agent defaults. What remains is a second tier of real but narrower risk: CORS wildcards, optional PKCE, e-stop ignored by MCP tools, plaintext token fallbacks in the native stack, an unauthenticated DLL input surface, and verification gaps (no Windows build, no Postgres/Redis live runs, no load test). Historical note: the Nov 17 `FINAL_PRODUCTION_READINESS.md` NO-GO snapshot was removed. Current assessment: `analysis/PRODUCTION_READINESS.md` (23 Nov 2026).

## 1. Re-evaluation of the 16 P0 items

### F-04-03 / F-06-01 — env vars disable MCP HTTP auth and DNS-rebinding protection
**Before:** `AI_BRIDGE_HTTP_ALLOW_ANON=1` disabled bearer auth; `AI_BRIDGE_MCP_DISABLE_DNS_PROTECTION=1` disabled DNS-rebinding protection.
**Now: CLOSED (verified at HEAD).** Both branches are deleted from `src/ai_bridge/server.py` — zero grep hits for either variable name in the file. The only escape hatch is the explicit `--unsafe-allow-anon` CLI flag, which refuses to run when `AI_BRIDGE_PROFILE=production` (`server.py:73,2226`) and prints a banner on every start. **Residual:** the flag itself is a deliberate, documented escape hatch; anyone who can pass CLI flags to the MCP server process can still run it anonymously in a non-production profile. That is a local-operator decision, not a remote bypass. `control/production_readiness.py:87-92` still references the removed variable (Wave 1C noted it; Wave 2A says the check is now a warning, not an off-switch) — a stale reference, not a live bypass.

### F-04-08 / SEC-13 — `Policy.unrestricted` bypass; silent Tier-4 authorisation
**Before:** `unrestricted` was checked before tier checks; `max_tier_unattended>=4` silently authorised Tier 4; enrollment could set `unrestricted=True` from the request body.
**Now: CLOSED (verified at HEAD).** `unrestricted` may only be set through the audited `grant_unrestricted_policy()` (`policy.py:301`, stamps `unrestricted_granted_by`); Tier 4 always requires a recorded approval (`policy.py:255-261` — the check now runs before the bypass, and the `max_tier_unattended>=4` escape is deleted). Control-plane enroll/update paths stamp `unrestricted_granted_by=<actor>` and audit the grant. **Residual:** the deliberate carve-out recorded by Wave 1C — at the MCP gate, a peeked local approval satisfies `decide()`'s admin requirement for Tier 4 (otherwise the stdio Tier-4 flow would be unreachable). The gate's `is_admin` derivation (bearer-admin OR a peeked approved local approval) weakens `decide()`'s admin requirement by design at that one call site. It is documented, but it is a carve-out, not a pure closure, and an independent reviewer should re-examine it.

### F-05-01 — local execution on the control-plane host
**Before:** `execute_ai_tool` local/host/droplet/no-target branch ran `dispatch()` in-process on the plane host for any operator.
**Now: CLOSED (per wave report, Wave 2A).** Local execution is OFF by default; it requires admin role + `AI_BRIDGE_ALLOW_LOCAL_EXECUTION=1`, and Tier ≥3 needs a valid approved approval record (otherwise `needs_approval` + an approval row). Tests: local `run_shell` → 403; local `list_devices` → 200. **Residual:** pure DB reads stay available to operators (deliberate). The pseudo-device `"local"` is not tenant-scoped (see SEC-03 below) — it is governed by role + this gate instead.

### F-05-03 / SEC-04 — `term-*` / `terminal_exec` auto-approval
**Before:** hard-coded `approval_granted=True` in `next_task` for `term-*` tasks.
**Now: CLOSED (per wave report, Wave 2A).** The branch is deleted; terminal tasks go through the same approval gate as every other Tier-3/4 tool. Tests: `test_term_task_without_approval_not_auto_approved`, `test_tier3_task_claimed_only_after_approval`. No residual recorded.

### F-06-04 — 5 MCP tools not in `TOOL_TIERS`
**Before:** `system_info`, `view_live_screen`, `interact_and_view_screen`, `select_device`, `browser_navigate_with_shot` were never `_check`ed.
**Now: CLOSED (per wave report, Wave 1C; registry verified at HEAD via Wave 3B).** All five are tiered (screen viewing READ_ONLY; `select_device`, `browser_navigate_with_shot` SENSITIVE) and all 109 wrappers are behind the single `_gated` wrapper (AST-verified 109/109). Wave 3B's registry holds **149** entries (61 READ_ONLY / 9 LOW_RISK / 76 SENSITIVE / 3 HIGH_RISK) — the brief's 144 was wrong. **Residual:** `interact_and_view_screen` is READ_ONLY only as a composite view; inner input actions are gated individually. `select_device` is Tier-3-opt-in-exempt (routing-only). Device-routed calls are audited as `"routed"`, not locally gated (deliberate).

### F-06-09 — no audit logging for MCP tool calls
**Before:** `audit_event` count = 0 in `server.py` and `tools.py`.
**Now: CLOSED (verified at HEAD for the writer; per wave report for the wiring).** `src/ai_bridge/audit_writer.py` exists (`record_tool_call`, `write_audit_event`; DB hash-chain with fsync JSONL fallback, redaction, truncation); the gate audits every invocation; `AI_BRIDGE_MCP_AUDIT_DB` defaults to the control-plane DB in `create_app`; `GET /api/audit/verify` verifies the chain. **Residuals:** (1) audit-write failure does **not** block tool execution (availability trade-off, loud on stderr) — fail-closed audit was deferred as a DB-05 decision; (2) the audit-writer consolidation is incomplete — `mcp_oauth.py:173` and `task_service.py:389` still call `dbmod.audit` directly (HEAD-verified), and `audit.py::audit_event` is now dead code with zero callers; (3) the DB audit chain's atomicity was verified at code level (`db.py:777-812`, single transaction) but the concurrent-writer fork scenario was never exercised under load.

### F07-01 — agent policy can only widen
**Before:** `_effective_policy()` ORs/maxes/unions every field; the server's `reject_policy_widening` guard did not apply to the merge.
**Now: CLOSED (per wave report, Wave 1B).** The effective policy is the intersection (most restrictive) of server and local policy: `unrestricted` = server AND local, `max_tier` = min, allowlists intersected, deny-lists unioned. 14 tests in `test_agent_policy_merge.py`. **Residual:** `allowed_apps`/`allowed_services`/`allowed_test_commands` use `_intersect_configured_allowlist`, where empty means "not configured" and does not narrow — a documented design choice, not a bypass, but an independent reviewer should confirm the empty-means-unconfigured semantics cannot be abused by an agent that clears its own lists.

### F07-02 — Linux agent Tier 4 by default
**Before:** `get_linux_server_policy()` returned `unrestricted=True, max_tier_unattended=4, allowed_*={"*"}` and pushed it on every start.
**Now: CLOSED (per wave report, Wave 1B).** Default is `max_tier_unattended=2`, `unrestricted=False`, a 15-tool read-only allowlist (each verified READ_ONLY in `TOOL_TIERS`). Tests assert the restrictive default and that Tier-3/4 tasks are denied without approval. **Residual:** none in code; the behaviour change is intended (existing Linux fleets stop running Tier-3/4 tasks until an admin opts in on the plane) and must be communicated as a breaking security fix at deploy time.

### F08-01 — fake H.264 IDR fabricates success
**Before:** on no MF output, the encoder emitted a hardcoded 44-byte SPS+PPS+empty-IDR and reported `valid=true, is_keyframe=true`.
**Now: PARTIALLY CLOSED — code only.** The fake block is deleted from `windows-agent/src/encoder/mf_h264_encoder.cpp`; failure returns `valid=false` + `failure_reason`; the §[5/6] test was rewritten with `CHECK()` and a fake-pattern negative control; grep confirms no `kValidMinimalIdrAu`/`0x67, 0x42` remnants in `windows-agent/src/`. **Not compiled or run** — no Windows toolchain on this host. The old §[5/6] pipeline coverage was lost in the rewrite (F08-16 still needs its own test). **This is the one P0 whose closure is unverified in any executable sense.**

### SEC-01 — `/api/v1/tasks` bypasses approval
**Before:** no `needs_approval` branch, no approval record, `tenant` hard-coded `"default"`.
**Now: CLOSED (per wave report, Wave 2A).** `v1_create_task` routes through the single task service (`control/task_service.py:1112` — HEAD-verified call site); v1 Tier-3 tasks queue as pending-approval and are never claimed without it; tenant comes from the session. **Residual:** the v1 WS path keeps its own `?token=` fallback (`v1_routes.py:985`, HEAD-verified — "Long-lived tokens in the URL remain accepted", deliberate compat). That is a D-08 residual, not an approval bypass, but it means tokens still travel in v1 WS URLs.

### SEC-02 — workflow/scheduler/trigger steps run as admin
**Before:** `_execute_workflow_step_sync` built an admin policy + `approval_granted=True, is_admin=True`; per-step approval was opt-in and only gated Tier ≥3 even when on.
**Now: CLOSED (per wave report, Wave 2A).** Steps execute with the workflow owner's policy, never admin; Tier ≥3 needs per-step or whole-workflow admin approval by default (`verify_workflow_approval`: approved `execute_workflow` row, unexpired, decided by an admin; invalid id → 400). 5 tests in `test_workflow_approval.py`. **Residual:** approved steps run with `approval_granted=True, is_admin=True` — the same authorisation model as approved tasks in `dispatch()`. That is a deliberate design decision (an approved step is authorised), not a bypass, but it means the approval record is the entire security boundary for workflows; a forged or over-broad whole-workflow approval authorises every step in it.

### SEC-03 — tenant is caller-supplied
**Before:** tenant was a query param defaulting to `"default"`; `users` had no tenant column; nothing bound a session to a tenant.
**Now: CLOSED (per wave report, Wave 2A; migration verified at HEAD).** Migration 013 adds `tenant` to `users`/`sessions`/`approvals` (data preserved under `"default"`, down migration exists); tenant is derived server-side from the session on every list/read/write; caller-supplied mismatch → 403; cross-tenant single read → 404; 16 routes tenant-checked via `_require_device_tenant`. 9 tests in `test_tenant_isolation.py`. **Residual:** pseudo-device `"local"` is **not** tenant-scoped (documented carve-out, `task_service.py:233-238` — HEAD-verified comment: "governed by the global policy and can only be queued by callers that already passed through v1's device check"). Any future device that is virtual rather than enrolled inherits the same carve-out unless the pattern is revisited.

### F13-03 — view-only mode not enforced agent-side for remote agents
**Before:** `_session_allows_input()` returned `True` when `signaling.session` was not a dict (remote `HttpSignaling` path); mid-session mode change never reached the remote `AgentStreamer`.
**Now: CLOSED (verified at HEAD for the plane half; per wave report for the agent half).** Agent side (Wave 1B): default-deny `_session_allows_input()`, `_refresh_session_state()` polling mode/permissions/takeover (~2.5 s). Plane side (Wave 2A): `GET /api/devices/{device_id}/desktop/webrtc/{session_id}` returns `mode`, `permissions`, and `human_takeover_active` — HEAD-verified at `routers/webrtc.py:320` (session fields incl. `mode`, `permissions`) and `:390-391` (`human_takeover_active` from session or durable flag). 12 tests in `test_webrtc_viewonly.py`. **Residual:** no live remote-agent E2E was run (no aiortc peer, no live plane-agent pair); the mid-session mode-change propagation is exercised only by unit tests with mocked state.

### P-01 — Tier 3 ON by default in production
**Before:** `AI_BRIDGE_ALLOW_TIER3=${…:-1}` / `=1`.
**Now: CLOSED (verified at HEAD).** Default is `0` in `docker-compose.prod.yml:31` (`${AI_BRIDGE_ALLOW_TIER3:-0}`), `deployment/aws/cloudformation.yml:396`, `render.yaml`, and `deploy-production.sh`. **Residual:** none in code. A deployment that explicitly sets `AI_BRIDGE_ALLOW_TIER3=1` still opts in — that is the intended opt-in, and it is now a conscious act.

### P-02 — known default secrets in compose files
**Before:** `POSTGRES_PASSWORD` defaulted to `aibridge-dev-password`; TURN secret defaulted to `change-me-in-production`.
**Now: CLOSED (verified at HEAD).** Zero grep hits for `aibridge-dev-password`, `change-me-in-production`, `aibridge_turn_shared_secret_2026`, or `dev-admin-token` in the compose files. Compose uses fail-fast `${VAR:?…}`; `deploy-production.sh` generates `.env.production` (mode 0600) with `openssl rand` if absent. **Correction to the audit itself:** Wave 1A found the audit's `POSTGRES_PASSWORD` default claim was already stale — both compose files already used `${POSTGRES_PASSWORD:?…}` as found; the live defaults were the TURN secret and the dev admin token, both removed. **Residual:** the CloudFormation UserData quoted-heredoc anomaly (literal `$(aws ssm get-parameter …)` strings written into `.env.production`, which compose will not evaluate) was observed by Wave 1A and **not touched** — it is a deployment-correctness issue that could leave a deployment without its secrets and tempt an operator to hard-code them.

### P-13 — unsigned fleet update
**Before:** fleet path did `git fetch origin main && git checkout -f -B main origin/main` with no hash pinning/signature/rollback; the signed Ed25519 library existed but was unused by that path.
**Now: CLOSED (per wave report, Wave 2A + fix-up).** Fleet updates, installer re-runs, and container redeploys route through `updates.py` (manifest + per-file SHA-256, verify-before-download, `AI_BRIDGE_ALLOW_AUTO_UPDATE=1`, 40-hex commit pin, rollback snapshot). `update_fleet_agent` raises `PermissionError` when no signed release is configured; both `magic-install.sh` re-runs verify + pin or refuse (exit 1). 7 tests in `test_signed_updates.py`. **Residuals:** (1) **not live-tested** — no signed release manifest exists yet; a dry run with a test keypair is the remaining verification step; (2) `repo_dir` defaults to the process working directory (`tools/fleet.py:159-160`, HEAD-verified) — a signed update applied from an unexpected cwd would check out into the wrong tree; (3) the manifest's trust anchor is the public key configured via env — key rotation/compromise procedure is undocumented.

## 2. Report 12 invariants — re-scored

Report 12 stated 10 security invariants. The audit's verdicts were: invariant 2 FAILS, invariant 7 PARTIAL, invariant 10 FAILS as stated (others held or were not scored as failing). Re-scored on the remediated code, with the same caution (code + local tests only, no live exercise):

| # | Invariant (report 12) | Audit verdict | Now | Basis |
|---|---|---|---|---|
| 1 | Only authenticated callers can invoke MCP tools over HTTP | (held, except the env-var off-switch) | **HOLDS** | Off-switches deleted; `--unsafe-allow-anon` is an explicit, production-refused CLI flag |
| 2 | Sensitive operations cannot bypass server policy | **FAILS** | **HOLDS, with one documented carve-out** | v1/workflow/terminal gates are uniform through the single task service; `unrestricted`/Tier-4 fixed. Carve-out: the MCP gate's peeked-local-approval admin derivation (see F-04-08) |
| 3 | Approval is required before Tier-3/4 execution | (failed via the three bypasses) | **HOLDS** | All three bypasses closed; Tier 4 always needs approval even at ceiling 4 |
| 4 | The agent cannot grant itself permissions | (failed via merge + Linux default) | **HOLDS** | Intersection-only merge; restrictive Linux default |
| 5 | Tenants cannot see each other's data | (failed — tenant caller-supplied) | **HOLDS, with the `"local"` carve-out** | Session-derived tenant; `"local"` pseudo-device exempt by design |
| 6 | Secrets are not exposed in logs, URLs, or child processes | (failed) | **MOSTLY HOLDS** | Fernet at rest; clean child env; tokens out of main WS URLs. **Exceptions:** v1 WS `?token=` fallback remains; enrollment token renders in plain text in `DevicesView.tsx:682`; `deploy-production.sh:186` prints the admin token in its success summary; `AI_BRIDGE_INSECURE_TLS=1` hatch remains in `run-agent.sh` |
| 7 | Human takeover stops conflicting AI input | **PARTIAL** | **HOLDS in code; unproven live** | Takeover checked at submission, claim, workflow/scheduler, WebRTC input path, and agent WebRTC route. No live E2E |
| 8 | Audit is append-only and verifiable | (failed — deletable, forkable) | **MOSTLY HOLDS** | Atomic single-transaction audit insert; verifier endpoint exists; retention is WORM-gated. **Exceptions:** audit-write failure does not block execution; `audit.py::audit_event` is dead code; the chain was never exercised under concurrent writers |
| 9 | Updates are authenticated and rollback-able | (failed — unsigned checkout) | **HOLDS in code; unproven live** | Signed manifest verified before fetch; pin + rollback snapshot. No signed manifest exists; no dry run done |
| 10 | The dashboard shows only real state | **FAILS as stated** | **HOLDS for the default experience** | Demo off by default; fabricated Merkle/provider cards deleted; fake vision canvas labelled. The demo workspace itself is still a simulation, now entered explicitly and labelled |

## 3. Headline security findings beyond the P0s

| Finding | Status | Evidence / residual |
|---|---|---|
| F-05-05 legacy admin token default-on | Closed | Off by default; explicit opt-in only (`auth.py:40-43`, HEAD) |
| F-05-02/SEC-10 CSRF opt-in + per-process secret | Closed | On by default; shared secret from secrets backend (`app.py:855-857`, HEAD) |
| SEC-06/DB-06 plaintext MCP OAuth secrets | Closed | Fernet, `fernet1:` prefix (HEAD). Pre-existing plaintext rows not migrated |
| SEC-07 child-env secret leak | Closed | Minimal clean env (HEAD: `tools/shell.py:117`) |
| SEC-05 audit retention deletes history | Closed | WORM-gated (409 without URL, 502 on export failure) |
| SEC-08 takeover desktop-only | Closed | Checked at submission/claim/workflow/WebRTC |
| F-05-06/SEC-09 mTLS header-trust, one route | Partially closed | On all agent routes (HEAD call sites); **header-trust mechanism unchanged** — spoofability depends on the proxy stripping the headers, which is deployment configuration, not code; no-op unless `AI_BRIDGE_DEVICE_MTLS=1`; operator v1 registry routes deliberately ungated |
| F-06-12 PKCE optional | **Open** | HEAD: `pkce_ok()` returns `True` with no challenge (`gateway.py:887-888`) |
| F-05-04 CORS wildcards | **Open** | HEAD: `allow_methods=["*"], allow_headers=["*"]` with credentials (`app.py:874-875`) |
| F-04-22 e-stop ignored by MCP tools | **Open** | HEAD: zero e-stop reads in `server.py` / `tools/` shell paths |
| F-04-15 Pillow fallback fabricates images | **Open** | HEAD: `_FallbackImageInstance` still present (`screen_capture.py:26-49`); DLL load unchecked |
| F08-07 DLL input surface always-on | **Open** | Any local process loading the DLL can inject input; no `SetPermissions`, no DLL signature/hash check |
| F08-02/03/04 native service/pipe | **Open** | Service is still not a supervisor; pipe auth still has no replay protection; desktop agent still never reads the pipe reply |
| F-06-13 DPoP/PAR | Deferred (deliberate) | Gateway has no PAR/DPoP; client half alone would be theatre |
| D-08 tokens in stream URLs | Partially closed | Stream tickets on main paths; v1 WS `?token=` fallback remains |
| P-11 deploy script prints admin token | **Open** | HEAD: `deploy-production.sh:186` prints `Admin Auth Token: ${AI_BRIDGE_ADMIN_TOKEN}` in the success summary (terminal scrollback / CI logs) |
| `AI_BRIDGE_INSECURE_TLS=1` in `run-agent.sh` | **Open** | HEAD: `deployment/linux/run-agent.sh:21-23` adds `-k` when set (explicit opt-in hatch, out of scope for Wave 1A) |
| Enrollment token in plain text | **Open** | HEAD: `DevicesView.tsx:682` renders the enrolled device token in the UI (and it is interpolated into the copyable install command) |
| F-04-01 approvals durable storage | Partially closed | Session-scoped + TTL + single-use; still in-memory (`server.py:70`) — lost on restart, single-process only |
| F-05-08 DB rate-limit atomicity | **Open** | HEAD: `_db_allow` still read-modify-write (`durable_ratelimit.py:142-167`) |
| F-06-16 stateless HTTP, no SSE/resumable sessions | **Open** | Untouched |
| F-04-10 verify cache / redirect-following / `wait_seconds=100` | **Open** | Untouched |
| F-05-12 `desktop_anticipate_action` pre-authorised | **Open** | Untouched |
| F-04-18 TURN default shared secret | **Open** | Python fallback to the hard-coded default secret not removed |

## 4. Residual risks, ranked

1. **Unverified native code (High).** Every C++ change in Waves 1–3 is inspection-only. F08-01 (the one partially-closed P0) plus F08-05/06/09/10/11/12/14/16 and F13-05/F08-13 rest on reading, not on a build or a test run. A wrong libdatachannel API name or a misapplied COM fix would only surface on Windows. **Do not deploy the native agent to any fleet until a Windows build + `ai_bridge_tests` run passes.**
2. **Secrets still leak through operational surfaces (Medium).** The deploy script prints the admin token; the enrollment token renders in the dashboard; the v1 WS fallback keeps tokens in URLs; the `INSECURE_TLS` hatch remains. Each is small; together they mean tokens still reach scrollback, logs, and proxy logs through paths the audit originally flagged.
3. **Header-trust mTLS (Medium).** mTLS is enforced on all agent routes, but the fingerprint still comes from proxy headers. If the edge proxy does not strip client-supplied `X-Client-Cert-Fingerprint` headers, device identity is spoofable. This is a deployment-configuration dependency that no code change in this remediation can close; it needs an edge test.
4. **CORS wildcards + optional PKCE (Medium).** Both are unchanged from the audit. Credentialed CORS with wildcard methods/headers against an explicit origin list is narrower than the audit's worst case (the bare-IP origin is gone per Wave 2A's CORS work — the origins list is now explicit), but the wildcard methods/headers remain. PKCE is still optional when no challenge is sent.
5. **E-stop ignored by MCP tools (Medium).** The durable e-stop flag is now consulted at task submission, claim, workflow, and scheduler — but not by the MCP server or the `tools.*` shell/desktop paths. An operator who hits emergency stop during an MCP-driven shell/desktop operation gets no enforcement on that path.
6. **Approval storage is in-memory (Medium).** `_APPROVALS` is a plain dict with a 15-minute TTL. Approvals do not survive a restart and are not shared across workers. A restart mid-approval loses the approval; a multi-worker deployment cannot rely on MCP-side approvals being visible to another worker.
7. **Audit availability trade-off (Low–Medium).** Audit-write failure does not block tool execution. That is the right availability call for a lab system, but for a production system it means an attacker who can break the audit sink can act without a record. Fail-closed audit was deferred as a DB-05 decision and never revisited.
8. **Signed-update trust anchor (Low–Medium).** The update path is signed, but no signed release manifest exists, no dry run has been done, the `repo_dir` defaults to process cwd, and the key-rotation/compromise procedure is undocumented. The mechanism is sound; the operational half is not.
9. **Fabrication risks that remain (Low).** The Pillow-missing fallback still fabricates images; the TURN fallback still uses a hard-coded default secret in non-prod; `get_screen_size` still passes `approval_granted=True` unconditionally. Each is narrow, but each is a place where the system still reports something it did not measure or authorise.
10. **Test-coverage gaps (Medium, as a security matter).** No e2e test covers enroll→task→result→audit→revoke; no failure/recovery tests exist; the C++ tests never ran; the Playwright e2e suite still asserts the old OIDC fragment flow and was never run. A regression in any of these areas would not be caught by the suite.

## 5. What is still NOT verified (evidence gaps)

- **No Windows build or run** of any C++ change (no MSVC/clang-cl, no Windows SDK on this host). All F08/F13-05 claims are inspection-only.
- **No Postgres boot, no Postgres backup/restore drill.** Migrations 013/014/015, FKs/CHECKs, the parity script, and the backup CLI were verified on SQLite only. Postgres `ADD CONSTRAINT` and `pg_dump`/`pg_restore` paths are scripted, not exercised.
- **No live Redis two-worker fan-out.** Cross-worker WS fan-out was tested with an injected fake broker only. The 4-worker load test (the actual proof that the rate limit is now the configured limit, and that the scheduler fires once) was never run.
- **No live aiortc peer run.** RTCP ABR, the ICE-complete gate, and the view-only/takeover enforcement were unit-tested with fake senders and mocked state, never against a real browser peer.
- **No live Ollama/LLM, no Copilot tenant, no live control-plane check of dashboard changes** (tsc + build + unit tests only).
- **No live deploy or enrollment.** `nginx -t` was never run; the installer `--fail`/CA-bundle path was never exercised against a plane with a valid certificate; the OAuth `auth_request` 401/200 round-trip was never proven live.
- **No Docker daemon.** Compose fail-fast behaviour was verified by YAML parse + config assertions, not by booting the stack.
- **No independent re-audit.** Every verification in this remediation was performed by the same agent chain that made the changes (waves) or its parent. Report 21 requires a fresh, independent audit pass before any GO; that has not happened.
- **No 10-step E2E workflow** (brief Part 10) passing against a local plane.

## 6. Updated security posture (self-assessed, inspection + local-test evidence only)

Report 22 scored Security 3/10, Authentication 4/10, Authorisation 3/10. On the same scale, with the same caution (no category may be scored on evidence that does not exist):

| Category | Audit (report 22) | Now | Reasoning |
|---|---|---|---|
| Security | 3/10 | **5/10** | All 16 P0 bypasses closed in code; audit is real and atomic; secrets encrypted at rest and scrubbed from child env. Held back by CORS wildcards, optional PKCE, e-stop gaps, native DLL/pipe surface, and the absence of any live verification |
| Authentication | 4/10 | **5/10** | Legacy token off by default; CSRF on by default with a shared secret; OIDC exchange-code flow; stdio session isolation; 401 split (unknown vs revoked). Held back by PKCE-optional, stdio bearer-less by design, and plaintext token storage remnants on the native side |
| Authorisation | 3/10 | **6/10** | One wrapper gate on all 109 tools; one task service; one approval gate on all entry points; tenant bound to the session; agent cannot widen policy. Held back by the `"local"` carve-out, the gate's peeked-approval admin derivation, and the deliberately ungated operator v1 registry routes |

These scores are the remediation team's self-assessment, not an independent audit. They must not be cited as an external security rating.

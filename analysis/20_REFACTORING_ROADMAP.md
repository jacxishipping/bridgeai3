> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 20 — Refactoring Roadmap

> **Method.** Priorities P0–P4 per the brief. Every P0 and every P1 row from report 17 appears here — P0 items get a full entry; P1 items get a full row in the §3 tables (all ten fields: ID, priority, affected files, root cause, recommended change, expected benefits, complexity, risk, dependencies, required tests, rollback strategy — root cause and files are stated in full in report 17 and summarised here). P2–P4 are summarised as workstreams. **Necessary fixes** (close a bypass, stop data loss, make a claim true) are separated from **optional redesign** (§5). Nothing here modifies application source code — this is a plan.
>
> **Ordering principle.** Close the approval/policy bypasses first (they are the reason the system cannot be trusted), then make the audit trail real, then make multi-worker operation safe, then consolidate duplication, then add features (report 19). Do not start FR-09–FR-19 until their prerequisites land.

## 1. Summary

| Priority | Items | Theme | Target |
|---|---|---|---|
| P0 | 16 | Close security bypasses that make the system untrustworthy | Before any production use; before any new feature |
| P1 | 78 | Make enforcement uniform, audit real, multi-worker safe, tests real | Before any production use (most); before scaling |
| P2 | 80 | Performance, maintainability, debt pay-down | Before scaling past ~20 devices |
| P3 | — | Valuable features (report 19, FR-01–08 now; FR-09+ later) | After P0/P1 |
| P4 | — | Long-term architecture (multi-plane, federation) | Rejected until FR-19 programme (report 19 §4) |

## 2. P0 — full entries (16)

### R-P0-01 — F-04-03 / F-06-01: env-var off-switches for MCP auth and DNS protection
- **Affected files:** `src/ai_bridge/server.py:79-82,1615-1682`
- **Root cause:** `AI_BRIDGE_HTTP_ALLOW_ANON=1` disables bearer auth; `AI_BRIDGE_MCP_DISABLE_DNS_PROTECTION=1` disables DNS-rebinding protection. A single env var silently removes authentication.
- **Recommended change:** Delete both off-switches. Auth is always on for HTTP MCP; DNS-rebinding protection is always on. If a local-dev escape hatch is genuinely needed, gate it behind an explicit `--unsafe-allow-anon` CLI flag that prints a banner on every start and refuses to run when `AI_BRIDGE_PROFILE=production`.
- **Expected benefits:** HTTP MCP cannot be accidentally or maliciously de-authenticated by environment.
- **Complexity:** S (delete two branches + tests).
- **Risk:** Low — breaks only deployments that relied on anon HTTP MCP, which is the insecure configuration.
- **Dependencies:** None.
- **Required tests:** HTTP MCP without a token returns 401; with `AI_BRIDGE_HTTP_ALLOW_ANON=1` set, still returns 401; DNS-rebinding check still enforced.
- **Rollback:** Revert the commit; the env vars are gone, so rollback is a code revert, not a config change.

### R-P0-02 — F-04-08 / SEC-13: `unrestricted` policy bypass and silent Tier-4 authorisation
- **Affected files:** `src/ai_bridge/policy.py:196,221-266,239-244`; `src/ai_bridge/control/app.py:1015-1030`
- **Root cause:** `Policy.unrestricted` bypasses all tier/write/approval checks; `max_tier_unattended>=4` silently authorises Tier 4; enrollment can set `unrestricted=True` from the request body.
- **Recommended change:** (1) `unrestricted` may only be set by an explicit, audited admin action (not by enrollment body, not by default); (2) Tier 4 always requires a recorded approval, even when `max_tier_unattended>=4` — the ceiling governs *which* tiers may run unattended, never Tier 4; (3) log every `unrestricted` grant to the audit chain.
- **Expected benefits:** The policy model means what it says; Tier 4 cannot be silently enabled.
- **Complexity:** M (policy logic + enrollment + audit).
- **Risk:** Medium — the Linux agent currently depends on the bypass (F07-02); fix R-P0-08 first or in the same change, or Linux agents will stop executing Tier-4 tasks (which is the intended behaviour, but it is a behaviour change).
- **Dependencies:** R-P0-08 (Linux Tier-4 default).
- **Required tests:** `decide(Policy(unrestricted=True), tier4_tool)` without approval → denied; enrollment with `unrestricted=True` in body → ignored/rejected; approval + Tier 4 → allowed and audited.
- **Rollback:** Revert; re-enable only via an explicit admin migration, not by default.

### R-P0-03 — F-05-01: local execution on the control-plane host
- **Affected files:** `src/ai_bridge/control/ai_api.py:1307-1370`
- **Root cause:** `execute_ai_tool` local/host/droplet/no-target branch runs `dispatch()` in-process on the plane host for any `operator`.
- **Recommended change:** Remove the local-execution branch for operator callers. Local execution becomes an explicit, admin-only, separately-audited capability (`AI_BRIDGE_ALLOW_LOCAL_EXECUTION=1` + admin role + approval for Tier ≥3), or is deleted entirely and all execution goes through the task queue to an enrolled device (including a plane-local agent enrolled as a device, which is the honest architecture).
- **Expected benefits:** The control plane stops being a remote-code-execution target for any operator.
- **Complexity:** M.
- **Risk:** High — may break workflows that rely on local execution; audit which callers use the local branch before removing it.
- **Dependencies:** None, but coordinate with SEC-02 (workflow steps).
- **Required tests:** Operator `execute_ai_tool` with no target → 403/denied, no local process spawned; admin local execution (if kept) → audited.
- **Rollback:** Feature-flag the branch off (default off) rather than deleting in the first release, so rollback is a flag flip; delete in the following release.

### R-P0-04 — F-05-03 / SEC-04: `term-*` auto-approval
- **Affected files:** `src/ai_bridge/control/app.py:4660-4680,3758-3800`
- **Root cause:** Hard-coded `approval_granted=True` in `next_task` for terminal tasks.
- **Recommended change:** Delete the hard-coded approval. Terminal tasks go through the same approval gate as every other Tier-3/4 tool; the terminal relay's admin-only creation remains, but creation ≠ approval to execute.
- **Expected benefits:** Real-shell execution can no longer bypass approval by naming a task `term-*`.
- **Complexity:** S.
- **Risk:** Low — terminal users will see an approval step they should already have seen.
- **Dependencies:** None.
- **Required tests:** `term-*` task claimed without approval → `approval_granted=False`; with approval → `True`.
- **Rollback:** Revert the one-line change.

### R-P0-05 — F-06-04: 5 MCP tools untiered
- **Affected files:** `src/ai_bridge/server.py`; `src/ai_bridge/policy.py`
- **Root cause:** `system_info`, `view_live_screen`, `interact_and_view_screen`, `select_device`, `browser_navigate_with_shot` have no `TOOL_TIERS` entry, so `decide()` would deny them but is never called.
- **Recommended change:** Add all five to `TOOL_TIERS` with correct tiers (screen viewing = READ_ONLY; `select_device` = SENSITIVE — it re-targets a session; `browser_navigate_with_shot` = SENSITIVE), and add the missing `_check` call in each wrapper (see R-P1-05). Add a CI test asserting every `@mcp.tool` has a tier entry and every tier entry is either exposed or explicitly marked plane-only.
- **Expected benefits:** Deny-by-default actually applies to the whole MCP surface.
- **Complexity:** S.
- **Risk:** Low.
- **Dependencies:** R-P1-05 (uniform wrapper gate) — do together.
- **Required tests:** Tier-coverage test (109 tools = 109 tier entries + explicit plane-only list); `select_device` without policy → denied.
- **Rollback:** Revert tier entries.

### R-P0-06 — F-06-09: no audit logging for MCP tool calls
- **Affected files:** `src/ai_bridge/server.py`; `src/ai_bridge/tools.py`
- **Root cause:** `audit_event` count is 0 in both files; audit exists only in the control plane.
- **Recommended change:** Log every MCP tool call (tool, args hash/redacted args, actor/session, device, decision, result summary) through the single audit writer created in R-P0-06 (DB hash-chain). Do this in the wrapper gate (R-P1-05) so no tool can opt out.
- **Expected benefits:** The highest-risk calls in the system become visible; incident response becomes possible.
- **Complexity:** M (writer + redaction + volume handling).
- **Risk:** Medium — audit volume grows; the chain must stay atomic (DB-05 fix (report 17 §3, DB-05 row)).
- **Dependencies:** R-P0-06 (one audit writer), DB-05 (atomic chain).
- **Required tests:** Each of a sample of MCP tools (read, write, shell, desktop) produces exactly one audit row; redaction verified (no secret in args stored).
- **Rollback:** Disable the MCP audit sink via flag; audit rows already written remain.

### R-P0-07 — F07-01: agent policy merge can only widen
- **Affected files:** `agents/common/poller.py:277-344`; `agents/linux/agent_main.py:108-121,945-951`
- **Root cause:** `_effective_policy()` ORs/maxes/unions every field; server `reject_policy_widening` does not apply to this merge.
- **Recommended change:** The effective policy is the **intersection** (most restrictive) of server policy and local policy. Local policy may only narrow. The server's `reject_policy_widening` guard is applied to the merge result on the plane at claim time as well (defence in depth).
- **Expected benefits:** A compromised or misconfigured agent cannot grant itself permissions.
- **Complexity:** M.
- **Risk:** Medium — agents with local policies that currently widen will lose permissions (intended).
- **Dependencies:** R-P0-08 (Linux default is the biggest widener).
- **Required tests:** Local `unrestricted=True` + server ceiling 2 → effective ceiling 2, `unrestricted=False`; local `allowed_tools=["*"]` + server list → server list wins.
- **Rollback:** Revert the merge function; agents re-read policy on next heartbeat.

### R-P0-08 — F07-02: Linux agent unrestricted Tier 4 by default
- **Affected files:** `agents/linux/agent_main.py:108-121,131-165,945-951`
- **Root cause:** `get_linux_server_policy()` returns `unrestricted=True, max_tier_unattended=4, allowed_*={"*"}`; pushed on every start.
- **Recommended change:** Default Linux server policy becomes the restrictive default (`max_tier_unattended=2`, `unrestricted=False`, explicit `allowed_tools` list). Tier 3/4 require explicit admin configuration + approval. Ship a migration note: existing deployments must opt in explicitly.
- **Expected benefits:** Removes the single most dangerous default in the tree.
- **Complexity:** S (default change) + M (migration communication).
- **Risk:** High (behaviour change) — existing Linux fleets will stop running Tier-3/4 tasks until an admin opts in. That is the point; communicate it as a breaking security fix.
- **Dependencies:** R-P0-02, R-P0-07 (same release).
- **Required tests:** Fresh Linux agent start → policy is restrictive; Tier-4 task without approval → denied.
- **Rollback:** Admin can set the explicit opt-in policy; rollback is a config change, not a code revert.

### R-P0-09 — F08-01: fake H.264 IDR fallback fabricates success
- **Affected files:** `windows-agent/src/encoder/mf_h264_encoder.cpp:376-393`; `windows-agent/tests/test_windows_agent.cpp`
- **Root cause:** On no MF output, emits a hardcoded 44-byte SPS+PPS+empty-IDR and reports `valid=true, is_keyframe=true`.
- **Recommended change:** On encoder failure, return `valid=false` with a reason code; never synthesise a frame. Telemetry counts it as a failed frame. Replace the `assert()`-only encoder test with a real test that feeds a failing encoder and asserts `valid=false`.
- **Expected benefits:** Health reporting becomes honest; a dead encoder is visible instead of a frozen frame reported as healthy.
- **Complexity:** S.
- **Risk:** Low — viewers will see an error instead of a frozen frame, which is correct.
- **Dependencies:** R-P1-55 (C++ test framework) for the test half; the code fix does not depend on it.
- **Required tests:** Failing MF encoder → `valid=false`, no fake IDR bytes emitted; healthy encoder → real IDR.
- **Rollback:** Revert the encoder change.

### R-P0-10 — SEC-01: `/api/v1/tasks` bypasses approval
- **Affected files:** `src/ai_bridge/control/v1_routes.py:1034-1073`
- **Root cause:** No `needs_approval` branch, no approval record, `tenant` hard-coded `"default"`.
- **Recommended change:** Route `v1_create_task` through the same task-creation service as `/api/tasks` (TD-D09 / R-P0-10): same policy check, same approval gate, same idempotency, real tenant from the session.
- **Expected benefits:** The approval gate becomes uniform; the v1 API stops being a bypass.
- **Complexity:** M.
- **Risk:** Medium — v1 callers that relied on immediate Tier-3/4 execution will now wait for approval (intended).
- **Dependencies:** R-P0-10 (one task-creation service).
- **Required tests:** v1 Tier-3 task without approval → queued as pending-approval, not executed; with approval → executed; tenant from session, not caller-supplied.
- **Rollback:** Feature-flag the v1 approval gate; rollback is a flag flip.

### R-P0-11 — SEC-02: workflow/scheduler/trigger steps run as admin
- **Affected files:** `src/ai_bridge/control/app.py:2238-2330,2329-2420,359-366`
- **Root cause:** `_execute_workflow_step_sync` builds an admin policy + `approval_granted=True, is_admin=True`; per-step approval is opt-in and only gates Tier ≥3 even when on.
- **Recommended change:** Workflow steps execute with the **workflow owner's** policy (not admin), and every Tier ≥3 step requires approval unless the workflow itself was approved as a whole by an admin (recorded approval covering the workflow version). Default `require_step_approval` to `True` for Tier ≥3.
- **Expected benefits:** Workflows stop being a privilege-escalation path.
- **Complexity:** L (workflow model + approval model).
- **Risk:** High — existing workflows may stop at Tier-3 steps until approved; communicate as a breaking security fix.
- **Dependencies:** R-P0-10 (task creation), R-P0-06 (audit — workflow steps must be audited).
- **Required tests:** Workflow with a Tier-3 step, no approval → step does not execute; workflow approved as a whole → steps execute with owner policy, not admin.
- **Rollback:** Flag to restore old behaviour per-workflow (audited, temporary, with an expiry date).

### R-P0-12 — SEC-03: tenant is caller-supplied
- **Affected files:** `src/ai_bridge/control/app.py:1091,1579,1972`; `src/ai_bridge/control/db.py:113-117`
- **Root cause:** Tenant is a query param defaulting `"default"`; `users` has no tenant column; nothing binds a session to a tenant.
- **Recommended change:** Add `tenant` to `users` and `sessions`; derive tenant from the session server-side on every list/read/write; reject caller-supplied `tenant` that differs from the session tenant (or ignore it entirely). Add a migration (013) and a test that a viewer in tenant A cannot list tenant B's tasks/devices.
- **Expected benefits:** Cross-tenant reads stop; multi-tenancy becomes real.
- **Complexity:** L (schema + every list endpoint + migration).
- **Risk:** High — every list endpoint changes; single-tenant deployments must migrate to an explicit default tenant.
- **Dependencies:** DB migration discipline (R-P2-08); no other P0 depends on it.
- **Required tests:** Cross-tenant list/read/write attempts → 403/empty; single-tenant migration preserves data under tenant `"default"`.
- **Rollback:** Roll back the migration (down migration required — see R-P2-08); data remains under `"default"`.

### R-P0-13 — F13-03: view-only mode not enforced agent-side for remote agents
- **Affected files:** `src/ai_bridge/webrtc_stream.py:645-669,1110-1309`; `src/ai_bridge/control/v1_routes.py:714-760`
- **Root cause:** `_session_allows_input()` returns `True` when `signaling.session` is not a dict (remote `HttpSignaling` path); mid-session mode change never reaches the remote `AgentStreamer`.
- **Recommended change:** (1) Default-deny: unknown session shape → input denied. (2) Propagate mode changes to the remote agent (signalling message + agent-side enforcement in `AgentStreamer`). (3) Dashboard stops sending input in `view_only` (defence in depth, not the enforcement point).
- **Expected benefits:** "View-only" means view-only for remote agents, not just local ones.
- **Complexity:** M.
- **Risk:** Medium — sessions in the unknown-shape state will lose input until the agent updates.
- **Dependencies:** Agent update path (P-13 fix — agents must be updatable securely to receive this).
- **Required tests:** Remote session, `view_only` → mouse/keyboard input rejected agent-side; mode change mid-session → input stops within one signalling round-trip.
- **Rollback:** Revert the default-deny to default-allow only as an emergency flag, audited, with an expiry.

### R-P0-14 — P-01: Tier 3 ON by default in production
- **Affected files:** `docker-compose.prod.yml:29`; `deployment/aws/cloudformation.yml`
- **Root cause:** `AI_BRIDGE_ALLOW_TIER3=${…:-1}` / `=1`.
- **Recommended change:** Default to `0` in compose and CloudFormation. Tier 3 becomes an explicit opt-in per deployment.
- **Expected benefits:** Fresh production deployments stop shipping shell/desktop control enabled.
- **Complexity:** S.
- **Risk:** Low (config default change; document it).
- **Dependencies:** None.
- **Required tests:** Compose config render with no env set → `AI_BRIDGE_ALLOW_TIER3=0`.
- **Rollback:** Set the env var to `1` explicitly.

### R-P0-15 — P-02: known default secrets in compose files
- **Affected files:** `docker-compose.prod.yml`; `docker-compose.yml` (coturn)
- **Root cause:** `POSTGRES_PASSWORD` defaults to `aibridge-dev-password`; TURN secret defaults to `change-me-in-production`; deploy skipped `.env.production` generation.
- **Recommended change:** Remove all default secrets. Compose refuses to start without `POSTGRES_PASSWORD` and `TURN_SECRET` set (fail fast with a clear error); `deploy-production.sh` generates `.env.production` (mode 0600) with `openssl rand` if absent (it already does this — make compose require it instead of defaulting).
- **Expected benefits:** No guessable-credential deployments.
- **Complexity:** S.
- **Risk:** Low — breaks only deployments that relied on the defaults (which are the vulnerable ones).
- **Dependencies:** None.
- **Required tests:** `docker compose config` with no env → error mentioning the missing secret; with secrets set → starts.
- **Rollback:** Set the secrets explicitly via env.

### R-P0-16 — P-13: unsigned fleet update while signed library exists
- **Affected files:** `src/ai_bridge/tools.py:1789-1813`; `src/ai_bridge/updates.py`; `src/ai_bridge/control/app.py:3915-3956`; `magic-install.sh:340-343`
- **Root cause:** Fleet path does `git fetch origin main && git checkout -f -B main origin/main` with no hash pinning/signature/rollback; signed Ed25519 library unused by that path.
- **Recommended change:** Route fleet updates, installer re-runs, and container redeploys through `updates.py` (manifest + per-file SHA-256, verify-before-download, `AI_BRIDGE_ALLOW_AUTO_UPDATE=1`). Pin the manifest to a signed release; keep a rollback snapshot.
- **Expected benefits:** A compromised git remote or mirror can no longer push arbitrary code to the whole fleet.
- **Complexity:** M.
- **Risk:** Medium — update path changes; test on a canary device first.
- **Dependencies:** None (library already exists).
- **Required tests:** Tampered manifest/file → update refused; valid signed update → applied; failed update → rollback to previous version.
- **Rollback:** Previous-version snapshot restore (built into the change).

## 3. P1 — roadmap rows (78)

Files and root causes for every row are in report 17 §3 (same IDs). "Tests" means the required test for the change; "Rollback" is the rollback strategy for the change. Labels of the form `R-P1-NN` refer to §3 row NN in the combined P1 list below (§3.1 rows 1–25, §3.2 rows 26–41, §3.3 rows 42–78); `R-P0-NN` refers to the P0 entry NN in §2.

### 3.1 Policy, MCP, and agent enforcement (rows 1–25)

| IDs | Recommended change | Expected benefits | Complexity | Risk | Dependencies | Required tests | Rollback |
|---|---|---|---|---|---|---|---|
| F-04-01 | Scope local approvals to (user, session, device, tool) with TTL and single-use; store in DB, not a plain dict | Approvals cannot be consumed cross-session | M | Medium | R-P0-06 (one audit writer) | Approval for tool A/session 1 cannot approve tool B/session 2; expired approval rejected | Revert; approvals are short-lived |
| F-04-02, F-06-03 | `_oauth_bound_device()`: never swallow exceptions — on verify failure, fail closed (deny, log) | Fail-closed binding restored | S | Low | None | Verify failure → denied, not unbound/local | Revert |
| F-04-04, F-06-07 | `select_device`: add auth + policy check + `@device_aware`; `broadcast_fleet_command`: require explicit device list or admin + approval, add `@device_aware` | Session re-targeting and fleet fan-out gated | S | Low | R-P1-05 | Unauthorised `select_device` → denied; broadcast without approval → denied | Revert |
| F-04-05, F-06-05 (R-P1-05) | **One wrapper gate:** every `@mcp.tool` wrapper calls `decide()` before implementation; generate wrappers from a tool registry | Uniform enforcement; Tier-1 bypass ends; the 109-tool surface becomes auditable | L | Medium | R-P0-05 | Coverage test: every tool wrapper calls the gate (static check + runtime test per tool family) | Revert per-wrapper; gate is additive |
| F-04-13 | Browser: one locked browser context per session/device; URL allowlist for `browser_evaluate`; no shared global | Cross-session browser leakage ends | M | Medium | None | Two sessions cannot see each other's pages; disallowed URL rejected | Revert; sessions restart |
| F-04-15 | Pillow-missing fallback returns an error, never a fabricated image; DLL load checks signature/hash/version | No fabricated screenshots; planted DLL cannot load | S | Low | R-P0-16 (signing infra) | Pillow absent → error, not 1024-byte image; tampered DLL → refused | Revert |
| F-04-17 | OCR script to a per-invocation random temp path (0600), no `-ExecutionPolicy Bypass`, redact OCR JSON before return | TOCTOU/replacement risk closed | S | Low | None | Two concurrent OCR runs use different paths; script content verified | Revert |
| F-04-21 | Retire JSONL audit as a writer; MCP audit goes through the DB writer (R-P0-06); if JSONL is kept, add fsync + rotation | One audit story; MCP calls logged | S | Low | R-P0-06 | No `audit_event` callers outside the DB writer | Revert |
| F-04-22 | Emergency stop: MCP tools and `tools.*` read the durable DB flag (with short TTL cache) on every Tier ≥2 call | E-stop actually stops MCP execution | M | Medium | F-04-22/SEC-08 (durable stop source) | E-stop set → MCP shell/desktop call refused | Flag to restore old behaviour (audited) |
| F-05-02, SEC-10 | CSRF on by default; shared secret from the secrets backend (not per-process random); 128-bit HMAC | CSRF works under 4 workers | S | Low | SEC-06/DB-06 | Token minted by worker A valid on worker B; CSRF enforced without the opt-in env var | Revert to opt-in flag |
| F-05-04 | CORS: explicit origins only, no wildcard methods/headers with credentials; remove bare-IP origin | Credentialed CORS narrowed to real origins | S | Low | None | Preflight from non-listed origin → denied | Revert config |
| F-05-05 | Legacy admin token off by default; enabling requires explicit env + a startup warning | Default deployments stop accepting a shared static token | S | Medium | None | Fresh deploy without the env var → legacy token rejected | Set the env var explicitly |
| F-05-06, SEC-09 | mTLS: verify client certificates at the TLS layer (or at the trusted edge with stripped/validated headers) on **all** agent routes, not `tasks/next` only | Device identity actually verified | M | Medium | Deployment (nginx/edge config) | Heartbeat/result/rotate without a valid client cert → rejected | Revert to `tasks/next`-only (audited) |
| F-05-07 | Replace the single global execution context with a per-task context (dict keyed by task id, or a contextvar) | Concurrent local executions stop overwriting each other | M | Medium | None | Two concurrent local tasks keep separate contexts | Revert |
| F-05-10 | Secrets backend fails closed by default; env fallback only with explicit `AI_BRIDGE_SECRETS_ALLOW_ENV_FALLBACK=1` + warning | Missing Vault/AWS no longer silently degrades to env secrets | S | Medium | None | Vault unreachable, flag unset → startup fails loudly | Set the explicit flag |
| F-06-02 | Stdio: per-invocation session key (random, TTL) instead of `"default"`; locked approval/session dicts with TTL | Anonymous stdio callers isolated from each other | S | Low | R-P1-01 | Two stdio invocations get different session keys | Revert |
| F-06-06 | Pass `approval_granted` through `write_file`, `capture_desktop_screenshot`, `take_screenshot` | Approvals can actually unlock these tools | S | Low | R-P1-05 | Approval granted → tool executes; without → Tier-3 gate applies | Revert |
| F-06-08 | Browser/search tools: add `@device_aware` + device param or mark explicitly local-only in the schema; policy check in search wrappers; `browser_evaluate` allowlisted | Browser tools stop being an ungated local-only backdoor | M | Medium | R-P1-05 | Search wrapper without policy → denied | Revert |
| F-06-10 | Add `BaseModel` validation per tool family before dispatch | Invalid args fail fast with a clear error | M | Low | None | Invalid args → validation error, no execution | Revert |
| F-06-12 | Gateway: PKCE required (reject auth requests without a challenge when the client supports it); DCR redirect URIs allowlisted per client | PKCE downgrade and open-redirect closed | S | Low | None | Auth without challenge → rejected; unlisted redirect → rejected | Revert |
| F07-03 | GUI becomes a thin UI over shared `Poller`; delete `_poll_loop()` duplication; GUI Stop joins the worker and stops the WS channel | One poll loop to fix | M | Medium | None | Stop → worker joined, no further claims | Revert |
| F07-04, DIST-02 | Add an execution epoch/fencing token to `tasks` (version column); result UPDATE requires `status='running' AND epoch=?`; claim issues the epoch | Late results cannot overwrite re-execution | M | Medium | DIST-05 (result guard) | Stale agent posts result with old epoch → rejected (409) | Revert (epoch column nullable) |
| F07-05 | DPAPI failure → fail loudly (log + keep token in memory only for this run), never silently downgrade to plaintext | Silent plaintext downgrade ends | S | Low | None | DPAPI failure → error logged, no plaintext file written | Revert |
| F07-06 | Linux token stored in one place, DPAPI/sealed or 0600 file only; never in `agent.env` / process environment | Token stops leaking via `/proc/<pid>/environ` and `systemctl show` | S | Medium | None | No `AI_BRIDGE_DEVICE_TOKEN` in process env; token file mode 0600 | Revert (re-enroll) |
| F07-07 | Agent executes tasks in a worker thread/pool; heartbeat/claim/cancel run on their own timer independent of dispatch | Hung tool stops stalling the agent | M | Medium | None | Hung task → heartbeat continues; cancel still delivered | Revert to single-threaded |

### 3.2 Native Windows, secrets, takeover, dashboard, integrations (rows 26–41)

| IDs | Recommended change | Expected benefits | Complexity | Risk | Dependencies | Required tests | Rollback |
|---|---|---|---|---|---|---|---|
| F08-02 | Service becomes a real supervisor: HTTP heartbeat, enrollment, desktop-agent launch/restart with backoff, pipe per desktop agent | Native agent actually supervised | L | High | None | Kill desktop agent → service restarts it; service exposes health | Revert service binary |
| F08-03 | Pipe auth: add nonce + timestamp + sequence (replay window), and narrow SDDL (remove `AU` generic RW; only the desktop-agent user) | Replay and any-local-user connection closed | M | Medium | None | Replayed message → rejected; other local user → connect denied | Revert SDDL/auth |
| F08-04 | Desktop agent reads the pipe reply; registration failure is fatal/logged, `active_session_id` stored | Registration failures visible | S | Low | R-P1-26 | Service returns error → desktop agent logs + exits non-zero | Revert |
| F08-05 | GDI fallback checks `BitBlt`/`PrintWindow` results and null-handles; failure → `valid=false` | Black/failed frames stop masquerading as valid | S | Low | None | Failed BitBlt → `valid=false` | Revert |
| F08-06 | Check `CoInitializeEx`/`MFStartup` HRESULTs; add `CoUninitialize` on shutdown paths | COM lifetime correct | S | Low | None | Failed init → error, not silent continuation | Revert |
| F08-07 | DLL input: `SetPermissions` enforced; input disabled by default, enabled only for an authorised session; Python side verifies DLL signature/hash | Any local process can no longer inject input via the DLL | M | Medium | R-P0-16 | Unauthorised process loads DLL → input calls rejected | Revert |
| F08-08 | Native config: DPAPI-seal device token + pipe secret (like the Python agent); config file 0600-equivalent ACL (SYSTEM + installing admin only) | Plaintext native credentials end | S | Low | None | Config contains no plaintext secrets | Revert installer |
| SEC-05 | Audit retention: require WORM export success before any delete; retention becomes archive-then-delete; admin delete is itself a separately-retained record | Audit history cannot be silently pruned | M | Medium | R-P0-06, FR-02 | Retention with WORM failing → delete refused | Revert endpoint |
| SEC-06, DB-06 | Encrypt MCP OAuth tokens/secrets at rest (Fernet/key from secrets backend); audit every read; never `SELECT *` on `mcp_servers` | Plaintext credential store ends | M | Medium | SEC-06/DB-06 | DB dump contains no plaintext tokens; API list returns no secret columns | Revert (re-encrypt migration) |
| SEC-07 | `tools._run_argv`: minimal clean env (allowlist) like `execution.py:150-160`; scrub `DATABASE_URL`, vault/TURN/webhook/OAuth material | `run_shell` children stop inheriting plane secrets | S | Low | None | Child env contains only the allowlist | Revert |
| SEC-08 | Takeover: check the DB flag at task submission (`/api/tasks`, v1), at claim (`next_task` — do not hand out queued tasks), in workflow/scheduler, and in the poller for all tool types (not just desktop) | Takeover actually stops conflicting AI input | M | Medium | F-04-22/SEC-08 | Takeover set → queued task not claimed; shell task refused; workflow step skipped | Flag to restore old scope (audited) |
| D-01 | Dashboard defaults to login/`#/live`; demo only via an explicit, labelled entry; add an interstitial warning on demo routes | Demo can no longer be mistaken for a fleet | S | Low | None | Fresh load lands on login/live, not seeded demo | Revert routing |
| D-02 | Delete the fabricated Merkle verification; show real chain status from FR-02 or show "not verified" | Fake verification removed | S | Low | FR-02 | Audit view shows real verification or an honest "unverified" state | Revert view |
| D-03 | Provider cards show live state only (or "not configured"); delete hardcoded keys/latencies/models | Fake credentials stop presenting as live state | S | Low | None | Signed-in provider view shows no hardcoded key/latency | Revert view |
| I-01, OV-07, INV-15 | Remove the hard-coded prod host default everywhere; `CONTROL_URL` becomes a required parameter with no default | Verbatim copies stop phoning home to the operator's prod server | S | Medium | None | Fresh install without a URL → error, not `jxiptvplayer.app` | Revert defaults |
| I-02 | Ollama agent: tool-result loop (call → execute → feed results back → final synthesis), like the OpenAI/Anthropic agents | Ollama agent becomes multi-turn and actually uses tool results | S | Low | None | Multi-step task completes with a synthesised final answer | Revert script |

### 3.3 Database, WebRTC, performance, testing, docs, deployment, distributed (rows 42–78)

| IDs | Recommended change | Expected benefits | Complexity | Risk | Dependencies | Required tests | Rollback |
|---|---|---|---|---|---|---|---|
| DB-01 | Postgres backup/restore procedure: scheduled `pg_dump`/PITR + tested restore runbook | Prod data stops depending on one Docker volume | M | Low | None | Restore drill: dump → fresh DB → plane boots | Keep old volume as fallback |
| DB-02 | Add foreign keys + CHECKs (status/role/tier/tool enums) via migration; delete paths cascade or explicitly clean children | Referential integrity; no orphaned rows | M | Medium | R-P0-12 (tenant) | Delete device → no orphaned tasks/telemetry; invalid status rejected | Down migration |
| DB-03 | One schema source of truth: generate `SCHEMA` from migrations (or drop inline `SCHEMA` for Postgres); CI Postgres boot test | Schema drift ends; Postgres boot verified | M | Medium | TD-T08 | CI boots Postgres from migrations alone and passes | Revert migration |
| DB-05, DIST-11 | Atomic audit insert (compute hash + insert in one transaction, or DB-side); add a verifier job (FR-02) | Chain can no longer fork under concurrent writers | M | Medium | FR-02 | Concurrent audit writers → single linear chain, verifier passes | Revert writer |
| F13-01 | `encode_time_ms`: report the real measurement or `null`; never a synthetic estimate | Telemetry honest | S | Low | None | No native value → `encode_time_ms=null` | Revert |
| F13-02 | Fix the native-encode call argument order (BGRA, w, h, fps, bitrate); drop the call if its result is discarded | CPU waste + misleading `encoder_backend` label end | S | Low | None | Call matches the C ABI; frame path uses PyAV only unless native succeeds | Revert |
| F13-04 | Check the takeover flag in `_handle_input_payload` (and in `perform_approved_ui_action`); refuse input while takeover is set | Takeover enforced in the WebRTC input path | S | Medium | R-P1-36 (SEC-08) | Takeover set → DataChannel input rejected | Revert |
| F13-05, F08-13 | C++ WebRTC: apply viewer ICE candidates, add TURN support, stop per-frame HTTPS polling (persistent signalling) | C++ path can connect behind symmetric NAT | L | High | None | Symmetric-NAT E2E test passes; poll rate sane | Revert to STUN-only (flag) |
| PERF-01, DB-04 | Connection pool per worker (e.g. pool size 5–10) instead of one shared connection + RLock | DB stops being the single serialised bottleneck | M | Medium | None | Concurrent heartbeat + claim + list under load; no serialisation | Revert pool size to 1 |
| PERF-02 | Rate limiting: Redis mandatory in prod, async-native limiter in middleware; DB path removed from the hot path | No per-request DB write; limit enforced once, not 4× | M | Medium | Redis in prod (already in compose) | 4-worker load test: effective limit = configured limit | Fall back to DB path (flag) |
| PERF-03 | List endpoints project columns (exclude `result` TEXT); separate `GET /api/tasks/{id}/result` for full results | Multi-MB list responses end | S | Low | None | 200-row list response < 1 MB | Revert projection |
| TST-01, OV-08 | CI runs the Python suite on every PR and publishes the run log in the repo | Test signal exists | S | Low | TD-T02 | CI green with a published log | — (process change) |
| TST-02 | Make heavy imports lazy / gate test modules so the suite collects without optional deps | Suite collects in a minimal env | S | Low | TD-T02 | `pytest --collect-only` passes with stdlib + pytest only for the stdlib subset | Revert |
| TST-04, D-06, DOC-04–07 | Rewrite Tier 1/2 to import real modules; wire Tier 5 + challenger suites in; correct TEST_READY/PROJECT/TEST_INFRA claims | Dashboard suite becomes real evidence | M | Low | FR-08 | Tier 1/2 import `dashboardStore`/app modules; `npm test` includes Tier 5 | Revert test files |
| TST-05 | (Folded into the row above — same change; tracked separately here so the ID is not dropped.) | — | — | — | — | Tier 5 runs in `npm test` | — |
| TST-09 | Add e2e (enroll→task→result→audit→revoke), MCP stdio/HTTP round-trip, and OAuth2 tests (target: the 10-step workflow in the brief, Part 10) | Critical paths covered | M | Low | None | 10-step E2E passes against a local plane | — (new tests) |
| TST-10 | Wire soak scripts into CI; add crash/restart/DB-failure/duplicate/cancel race tests | Failure behaviour tested | M | Low | None | Kill plane mid-task → task re-queued once, result not duplicated | — (new tests) |
| TST-15 | Move e-stop + OIDC pending state to durable shared state (DB/Redis) so multi-worker operation is safe | Single-worker constraint lifted for these states | M | Medium | R-P1-72/74 | E-stop set on worker A visible on worker B; OIDC login completes across workers | Revert to in-memory (flag) |
| DOC-01/02/03, TST-07 | Correct README "75 tests" and CHANGELOG 29/33 to the counted 403 (or cite the CI run log) | Doc claims match code | S | Low | R-P1-53 (CI log) | Counts in docs match `grep -c 'def test_'` | Revert docs |
| DOC-08 | Correct README Fluent 2 claim to the actual JX-UI design system | Stale claim removed | S | Low | None | Docs match `package.json` | Revert docs |
| DOC-12 | Bump SECURITY.md supported version to v0.3.0 | Supported-version claim true | S | Low | None | SECURITY.md matches `pyproject.toml` | Revert docs |
| DOC-13 | One mTLS status statement across all docs, matching the verified code + E2E state | Contradiction removed | S | Low | R-P1-13 (mTLS verification) | Docs agree; E2E status stated | Revert docs |
| DOC-14 | One OIDC status statement ("code present, flow unverified until E2E passes") | Contradiction removed | S | Low | R-P1-74 | Docs agree | Revert docs |
| DOC-15, OV-05, INV-08 | One macOS status statement ("code exists, never run on macOS") + a macOS verification plan | Contradiction removed | S | Low | macOS host for verification | Docs agree | Revert docs |
| DOC-19, INV-02 | Restore `ORIGINAL_REQUEST.md` or remove it as the R1–R5 baseline and re-baseline on the repo | R1–R5 checkable | S | Low | None | Baseline file exists or citations removed | Revert docs |
| DOC-31 | Sweep README/TEST_READY/PROJECT/SECURITY for the ≥15 overstatements; adopt the honest docs as the style guide | Marketing docs stop overstating | M | Low | All P1 doc rows above | No doc claim contradicts code | Revert docs |
| P-03 | nginx: HTTP→HTTPS redirect on :80 (ACME challenge only stays on :80) | Admin tokens stop travelling in cleartext | S | Low | None | `curl http://host/api/...` → 301 to https | Revert nginx |
| P-04 | `/metrics`: `auth_request` or `allow/deny` + rate limit; or remove the location | Metrics stop leaking publicly | S | Low | None | Unauthenticated `/metrics` → 401/403 | Revert nginx |
| P-05, I-07 | Ship the nginx OAuth `auth_request` wiring the gateway README sketches, or delete the sketch | Shipped config matches docs | S | Low | None | `/mcp` without a valid token → 401 at nginx | Revert nginx |
| P-09 | Magic installer: remove `curl -k`; pin the CA / use the system bundle; enrollment without `-k` | Enrolment stops disabling TLS verification | S | Medium | None | Install against a plane with a valid cert succeeds; self-signed without the proper flag fails loudly | Revert installer |
| DIST-01 (R-P1-72) | Scheduler: atomic claim (`UPDATE … WHERE next_run_at<=now` + `rowcount` check) or leader election (Redis lock) — one worker runs the loop | Duplicate scheduled workflows end | M | Medium | Redis (already in compose) | 4-worker test: each due job fires exactly once | Revert to unguarded loop (flag) |
| DIST-03 | WS fan-out via Redis pub/sub (or remove WS push and rely on long-poll only, documented) | Agents on other workers receive wake/cancel | M | Medium | Redis pub/sub (new use — compose comment already claims it exists) | Cancel issued on worker A reaches an agent connected to worker B | Revert to per-worker fan-out |
| DIST-04 | OIDC `_PENDING` + discovery cache in Redis/DB, not process memory | OIDC login works across workers | M | Low | Redis or DB table | Login on worker A, callback on worker B → 200 | Revert to in-memory |
| DIST-05 | Result UPDATE guarded by `status='running'` (+ epoch, R-P1-22); check `rowcount` | Late results stop overwriting terminal states | S | Low | R-P1-22 | Late result after cancel → rejected | Revert guard |
| DIST-08 | Agent progress heartbeat on the task row (agent reports progress; sweep uses it, not just `updated_at` at claim) | Long tasks stop being re-queued while running | M | Medium | Agent change (F07-07 worker thread) | 1,200 s task → not re-queued; dead agent's task re-queued after lease | Revert sweep to claim-time staleness |
| OV-02, INV-05 | Split `app.py` by domain behind `APIRouter`s (auth, devices, tasks, audit, webrtc, workflows, admin) — routes unchanged | God module becomes reviewable; multi-worker state isolated per module | L | Medium | None | All 142 routes still respond identically (route test snapshot) | Revert per-router (routes are additive) |
| INV-12 | Consolidation programme: one registry (TD-D01), one task service (TD-D09), one audit writer (TD-D04), one limiter (TD-D05) | Duplication pay-down (report 18 §10) | L | Medium | R-P1-05, R-P0-10, R-P0-06 | Duplication inventory shrinks; no behaviour change | Revert per consolidation |

## 4. P2 — workstreams (80 rows, summarised)

P2 items are real but not blocking. Grouped; full detail is in report 17 §4.

| Workstream | IDs | Change | Target |
|---|---|---|---|
| Code hygiene | F-04-06, F-04-12, F-04-23, F-05-11, F-05-18 | Split `tools.py`; rename `control/workspace.py`; cache/validate `Settings`; fix stale docstrings; replace regex SQL translation | After P1; before scaling |
| Shell/terminal hardening | F-04-07, F-04-11, F-04-16 | One shell contract; SIGKILL escalation; bounded sessions with TTL/eviction | With R-P1-05 |
| Browser isolation | F-04-14, F-04-19, F-04-20 | Real sandbox (seccomp, read-only bind) or fail closed; token handling in SDK; signed updates in-function | With FR-16 |
| Secrets hygiene | F-04-09, F-05-17, SEC-11 | Structured redaction (not regex-only); SSRF validation in the sender with DNS pinning + redirect control | With R-P1-35 |
| Rate-limit/idempotency | F-05-08, F-05-14/DIST-09, DIST-06, DIST-07, F-05-15, F-05-16 | Atomic durable limiter; idempotency on all task paths with TTL; transactional handoff advance + stuck-handoff sweeper; OIDC token out of URL fragment; fail-fast migrations | With DIST fixes |
| MCP protocol | F-06-11, F-06-13, F-06-14, F-06-15, F-06-16, F-06-20 | Error taxonomy; DPoP/PAR evaluation; `enforce_mcp_tool_policy()` on all execute paths; proper MCP client (`initialize`, session, pagination); admin token out of tool args | After P0/P1 MCP fixes |
| Agent reliability | F07-08, F07-09, F07-10, F07-12 | Spool DLQ + cap + TTL; graceful shutdown/drain; independent heartbeat timer; 401 handling that distinguishes revocation | With F07-07 |
| Native Windows | F08-09–F08-16 | Divergent secure-desktop detection unified; monitor geometry in C++ input; per-iteration monitor construction fixed; single-instance guard; TURN/ICE in C++; real tests; JSON library; `StartSession` honest return | With R-P1-26/31 |
| Dashboard | D-04, D-07/DOC-30, D-08 | Fake vision canvas labelled or removed; token storage session-only everywhere; tokens out of stream URLs (header-capable transport or short-lived stream tokens) | With D-01/02/03 |
| Integrations | I-03, I-04, I-05 | LangChain `args_schema`; approval-aware example agents; Copilot live-tenant test | With FR-08 |
| Database | DB-07/PERF-10, DB-08 | Retention jobs; migration runner rewrite (no `;`-split, down migrations, no silent no-op) | With DB-01/02/03 |
| WebRTC | F13-06–F13-10 | Remove `simulate_agent_intent` from production; real RTCP-based ABR or honest labelling; mouse-held release in fallback; session reconciliation after restart; ICE-complete before offer | With R-P0-13 |
| Performance | PERF-04/F-05-19, PERF-05, PERF-06, PERF-07, PERF-08, PERF-11 | N+1 → single query; prune less often (or incrementally); replace 0.15 s spin with condition/Redis wake; `handoff_id` column + index; execution-state poll 0.5 s → 2 s / WS-driven; missing indexes | After a load-test harness exists (TD-T08) — measure first |
| Testing | TST-03/INV-11, TST-08/DOC-02/03, TST-13, TST-14 | Lazy `__init__`; run logs in repo; Playwright live-workspace coverage; C++ tests on Windows CI with a real framework | With TST-01 |
| Docs | DOC-10, DOC-11, DOC-16, DOC-17, DOC-18, DOC-20/INV-03, DOC-22 | Demo/live split stated; threat-model updated for v0.3 (signed updates, rlimits vs cgroups, signed config); `dist/` claim corrected; api-reference rewritten from the 142 routes | With P1 doc rows |
| Deployment | P-06, P-07, P-08, P-12, P-15 | Complete `.env.example`; lockfiles + `npm ci`; unify deploy scripts with a health gate; one default control URL (required param); Render labelled test-only | With P-02/P-09 |
| Misc | OV-03, OV-10/INV-01, INV-04 | Consolidate the two WebRTC stacks (decision, not code, first); ship with git history or a signed release manifest; vendor or install dashboard deps in the tree | Ongoing |

## 5. Necessary fixes vs optional redesign

**Necessary (do these; the system is not trustworthy/scalable without them):** all 16 P0 items; P1 rows R-P1-05 (one gate), R-P0-10/R-P0-06 (one task service, one audit writer), F07-04/DIST-02 (fencing), DIST-01 (scheduler), DIST-05 (result guard), DIST-04 (OIDC), DB-05 (audit chain), SEC-06/DB-06 (secrets at rest), SEC-07 (env scrub), R-P1-53/55 (CI test signal), P-03/P-04/P-09 (deployment), DB-01/02/03 (backup, FKs, single schema source).

**Optional redesign (valuable, but do not block production-readiness on them):** splitting `app.py` into microservices (a modular monolith with `APIRouter`s is enough); replacing FastAPI with another framework; replacing SQLite/Postgres with another store; rewriting the dashboard; rewriting the C++ agent in another language; multi-plane federation (FR-R01 — rejected for now); replacing aiortc with the C++ path as default (FR-R06 — rejected for now).

## 6. Sequencing

| Phase | Items | Exit criteria |
|---|---|---|
| Phase 0 — stop the bleeding (weeks 1–2) | R-P0-01, R-P0-04, R-P0-14, R-P0-15, P-03, P-04, P-09, R-P1-05 (gate skeleton), R-P1-53 (CI signal) | No env-var auth bypass; no auto-approval; no default Tier 3/secrets; CI runs the suite |
| Phase 1 — close the bypasses (weeks 2–5) | R-P0-02/03/05/06/07/08/10/11/12/13/16, R-P0-09, F-04-01, R-P0-10, R-P0-06, F07-04/DIST-02, DB-05, SEC-06/DB-06, SEC-07, SEC-08 | One approval gate on all entry points; MCP audit live; agent cannot widen policy; tenant enforced |
| Phase 2 — make it multi-worker safe (weeks 5–8) | DIST-01/03/04/05/07/08, TST-15, PERF-01/02, SEC-10/CSRF, mTLS all routes | 4-worker load test passes: scheduler fires once, OIDC completes, WS fan-out works, rate limit = configured |
| Phase 3 — make it honest (weeks 6–9) | D-01/02/03, DOC sweep, F08-01/05, F13-01/02, TST-04/05, FR-01/03/05/06/07/08 | Dashboard demo labelled; no fabricated values; test suite real |
| Phase 4 — consolidate (weeks 8–12) | TD-D01–D12, TD-S01, DB-02/03, PERF-03–11 | Duplication inventory halved; `app.py` split by domain; indexes + retention in place |
| Phase 5 — scale + features (after Phase 4) | FR-09–FR-20 per report 19 §5 | Features built on a trustworthy foundation |

*Durations are estimates by this synthesis, not commitments — no team size or velocity was given in any source report.*

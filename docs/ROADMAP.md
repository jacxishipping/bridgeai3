# AI Bridge Product Roadmap

**Planning baseline:** 20 November 2026 · **Assessment refresh:** 23 November 2026 ([PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md))  
**Purpose:** Canonical, outcome-oriented product roadmap. It distinguishes code that exists from work verified in a production-like environment. Historical technical detail remains in [ai-bridge_ADVANCED_ROADMAP.md](./ai-bridge_ADVANCED_ROADMAP.md).

## Direction

AI Bridge should become a trustworthy, policy-governed way for authorised AI clients and human operators to inspect and operate enrolled computers. The next priority is **reliable, proven control of real fleets**, not a broader tool catalogue or more autonomy.

| Class | Meaning |
|---|---|
| Shipped | Implemented in this repository, with automated coverage where noted. |
| Verify / harden | Code exists, but target-platform, staging, browser-peer, or independent verification is required before a production claim. |
| Enhance / polish | A working capability needs better scale, usability, consistency, observability, or maintainability. |
| Build | A net-new capability that starts only after its listed prerequisites. |

## Features already added

### Core platform and security

- Device enrolment/revocation, expiring and rotatable device tokens, outbound polling agents, task queues, cancellation, execution-state reporting, and emergency stop.
- Four-tier policy enforcement, human approvals with context and expiry, versioned policies, policy simulation, local RBAC sessions, CSRF protection, OIDC integration code, and an optional device mTLS gate.
- Hash-chained audit records, verification/export/retention paths, redaction, encrypted MCP OAuth secrets, signed release-manifest verification, and bounded task execution.
- SQLite development support plus PostgreSQL migrations/parity, boot checks, logical `pg_dump`/`pg_restore`, and physical `pg_basebackup` + WAL PITR restore drills (`scripts/backup_restore.py`, `scripts/postgres_pitr_drill.py`); production readiness checks.
- MCP stdio and Streamable HTTP endpoints plus workspace, diagnostics, browser, desktop, developer, fleet, and connector tools (including hardened Tier 3/4 Full PC Access boundaries).

### Fleet, automation, and AI

- Linux, Windows Python, and macOS agent entrypoints; Windows installer (`.NET` launcher ACL hardening + DPAPI token sealing) and desktop helper; platform capability reporting.
- Idempotent task creation, per-device leases, stale-task recovery, result spooling, policy intersection on agents, workflow templates, schedules/triggers, custom agents, role catalogue, supervisor handoffs, and agent-memory storage.
- Playwright browser tooling with device dispatch; Firecrawl, Browserbase, You.com, and Microsoft 365 connectors.
- Fleet health, live activity/SSE, metrics, alerts/webhooks, diagnostics, backup/PITR tooling, audit search/export, and automated Release 1 verification/gate harnesses (`scripts/staging_release_gate.py`, `scripts/windows_installer_e2e.py`, `scripts/macos_launchd_e2e.py`, `scripts/webrtc_browser_e2e.py`).

### Desktop, native, and operator applications

- Python WebRTC desktop sessions, TURN/STUN configuration, stream tickets, view-only/takeover controls (including UI keyboard & mouse view-only enforcement), input throttling, monitor selection, adaptive stream parameters, SoM/OCR actions, veto, and post-action verification.
- Native C++ Windows capture/encoder/PeerConnection sources and CI harness; .NET WPF Windows client with safe-by-default policy/function gating (`MaxTierUnattended=2`, Tier 3/4 opt-in), device management, diagnostics, capture, input, and named-pipe services.
- React/Vite dashboard: sign-in, fleet/device detail, task explorer, terminal, live desktop, workflows/agents, fleet operations, policy/security, audit/logs, MCP connections, and administration/settings.
- Responsive light/dark UI, command palette, 12-second live refresh, approval attention counts, emergency stop, filtering, and role management.

## Release 1 — production proof and trust closure

**Goal:** make the existing platform defensible for a small, controlled production fleet.
**Current verification record:** [release-1-verification.md](./release-1-verification.md).

| Priority | Work | Status | Done when |
|---|---|---|---|
| P0 | Windows installer/service end-to-end test | **CI green (`windows-python-e2e` on `5eb8750`) + verified locally (`7/7` installer/DPAPI E2E + live polling Windows agent against PostgreSQL 17.11); `scripts/windows_reboot_soak.ps1` ready** — clean-VM OS reboot soak pending | A clean Windows image installs, enrols, survives reboot, completes safe and approved tasks, streams/view-controls where enabled, rotates/revokes its token, and uninstalls cleanly. |
| P0 | Real-browser WebRTC E2E | **CI green (`release-1-staging-and-webrtc-gate` Chromium `--require-browser-peer` on `5eb8750`) + verified locally (`7/7`, real Google Chrome `chrome.exe` ↔ `aiortc`: ICE `connected`, `6–7` decoded video frames, ephemeral TURN minted, live DC view-only/takeover/disconnect release)** — cross-NAT public TURN run pending | A staging test proves ICE/TURN connection, view-only enforcement, authorised takeover, input release on disconnect, and session expiry using a browser and enrolled endpoint. |
| P0 | macOS launchd E2E | **CI green (`macos-agent` on `macos-latest`, commit `5eb8750`: `plutil` plist lint + launchd agent E2E + honest desktop status)** — physical Mac OS reboot check pending (or scope macOS as experimental for Release 1) | A Mac installs launchd, enrols, survives reboot, polls, executes a safe task, and reports honest desktop capability status. |
| P0 | Production database restore drill | **CI green (`postgres-boot` on PostgreSQL 18.6, commit `5eb8750`: parity + boot + logical `pg_dump`/`pg_restore` RTO `0.78s` + physical `pg_basebackup`/WAL PITR) + verified locally on PostgreSQL 17.11 (RTO `1.24–2.98s`, `16/16` migrations, audit chain valid) + SQLite** — hosted staging/prod DB drill pending | Restore PostgreSQL backup/PITR into isolation, boot from migrations, validate users/devices/tasks/approvals/audit continuity, and record RTO/RPO. |
| P0 | Staging release gate | **CI green (`release-1-staging-and-webrtc-gate` on `5eb8750`) + verified locally (`10/10` against live PostgreSQL 17.11 plane + built v3 dashboard + `smoke_check.py` `6/6`)** — archive hosted staging RC evidence pending | Archive evidence for migrations, readiness, dashboard smoke, enrolment, safe task, approval, rollback, and security configuration review. |
| P1 | Independent security/architecture review | **Complete & remediated** (fresh read-only reviewer audited all 5 boundaries; all 3 P0 + 7 P1 findings fixed and regression-tested) | An independent reviewer signs off on policy/approval, tenant, auth, native-agent, and update boundaries, or findings are remediated. |
| P1 | Correct test health | **Complete & CI green on `5eb8750`** (all 9 CI matrix jobs green across Ubuntu, Windows, and macOS; `945 passed, 0 failed` on Windows; deterministic API reference check; explicit pytest markers) | Replace/quarantine the known timing-flaky test; separate platform/key-dependent tests into explicit jobs; publish a green supported-platform matrix. |

**Exit criteria:** P0 evidence accompanies a release candidate; no critical/high unresolved finding; support claims match the tested platform matrix; rollback and recovery are approved.
**Remaining hosted/physical-target RC sign-off checks (all automated & CI gates green on `5eb8750`):** run `scripts/windows_reboot_soak.ps1` on a clean virgin Windows VM, run `scripts/backup_drill.sh` + `scripts/staging_release_gate.py --url` + cross-NAT public TURN check against hosted staging, and optionally verify an interactive OS reboot on a physical Mac (or scope macOS as experimental for Release 1).

## Release 2 — reliability, scale, and operations

**Goal:** operate a growing fleet predictably and make failures inexpensive to diagnose.

| Theme | Enhancements needed | Status | Acceptance measures |
|---|---|---|---|
| Multi-worker correctness | Load-test cancellation, OIDC, schedules, idempotency, and rate limits across workers/restarts; require Redis when multi-host operation is enabled. | **Shipped & locally verified** (fail-fast Redis enforcement for multi-host & prod `AI_BRIDGE_WORKERS > 1`, auto-reconnecting Redis pub/sub for agent + WebRTC WS fan-out, OIDC PKCE + atomic `DELETE ... RETURNING` state/exchange + JWKS cache, durable M365 PKCE store, atomic trigger/custom-agent/approval claims, cross-worker takeover cache fix, `tests/integration/test_multi_worker_correctness.py`, `docs/fleet-limits.md`) — multi-host staging load soak pending | Reproducible load report, no duplicate schedules, cross-worker cancellation, documented fleet limits. |
| Task lifecycle | Add progress heartbeats, configurable retry/reconciliation, dead-letter handling, clear failure reasons, and task/event retention. | **Shipped & locally verified** (migration `017` + `down_017`, per-tool idempotency classifier, exponential-backoff retries, `task_dead_letters` DLQ + list/replay/purge APIs, structured `failure_class`/`failure_reason`, persisted `progress_pct`/`status_message`/explicit leases, wired `retention_sweep_from_env` + `maybe_prune_telemetry` + `delete_device_cascade`, `tests/unit/test_task_lifecycle_release2.py`) | No permanently ambiguous task state and a documented recovery path after agent failure. |
| Database operations | Add query/pool observability, capacity alarms, query-plan review, and scheduled restore drills. | **Shipped & locally verified** (`psycopg_pool.ConnectionPool` support + timeouts in `PostgresConnection`, query latency histogram & slow-query logging, `get_db_stats()` in `/health/ready` & `/api/metrics/summary`, N+1 & unbounded overview query fixes, automated `EXPLAIN QUERY PLAN` regression tests, `deployment/monitoring/alerts.yml`, `deployment/systemd/ai-bridge-backup.{service,timer}`) — quarterly hosted restore proof pending | API/task latency SLOs, DB exhaustion alerts, quarterly restore proof, tested retention. |
| Observability | Standardise structured logs, traces/correlation IDs, secured metrics, native diagnostics, and alert runbooks. | **Shipped & locally verified** (fixed `JSONFormatter` `extra` handling + `contextvars` correlation, `X-Correlation-ID` propagation across dashboard/MCP/control-plane/agent/audit + `GET /api/traces/{trace_id}`, dedicated `AI_BRIDGE_METRICS_TOKEN` Bearer scrape auth, automated `evaluate_operational_alerts`, `docs/runbooks.md`) | One task can be traced from dashboard/MCP request through agent result and audit record. |
| Release engineering | Add dependency/container/SBOM/secret/vulnerability scanning, signed artifacts, and retained release evidence. | **Shipped & locally verified** (`scripts/generate_sbom.py` CycloneDX 1.5, `scripts/assemble_release_evidence.py`, CI `security-and-supply-chain` job, tagged `.github/workflows/release.yml`, `.github/dependabot.yml`, `tests/unit/test_release_engineering.py`) — first tagged CI release run pending | Every release has artifact metadata, scans, migration/rollback plan, and test evidence. |
| Agent resilience | Improve offline spool visibility, network recovery, diagnostics, update rollout rings, and agent health remediation. | **Shipped & locally verified** (spool epoch/trace persistence + `stats()` + terminal 404/409 DLQ reconciliation, heartbeat poll-state/spool/version/policy-hash reporting, fleet health drift & spool visibility, staged rollout rings `canary`/`early`/`general` with health-gate auto-pause, pause/resume, and `rollback_fleet_agent`, dashboard `api.ts` wiring, `tests/unit/test_agent_resilience_and_rollouts.py`) | Fleet view exposes spool age, poll state, policy/version drift, and update state; rollouts can pause/rollback. |

## Release 3 — polish

| Area | Work | Status |
|---|---|---|
| Dashboard truthfulness | Keep demo and live data unmistakably separate; never show invented verification/provider/audit values as production state. Add empty, offline, denied, and degraded states to every view. | **Shipped & verified** (removed fabricated fallback sparkline `[4,6,5,8,7,9,6]` in `ui.tsx`, removed synthetic CPU/RAM baselines in `OverviewView.tsx`, removed invented default tags in `FleetView.tsx`, added shared `StateBanner` for `empty`/`offline`/`denied`/`degraded` states and live control-plane disconnect banner in `App.tsx`) |
| Fleet navigation | Add cursor/server-side pagination, saved filters, bulk-action safeguards, device groups, and clear policy inheritance/override explanations. | **Shipped & verified** (server-side pagination & filtering on `GET /api/devices` & `GET /api/tasks` with `X-Total-Count` + CORS exposure, `localStorage` saved filters, group/status pills, bulk-action blast-radius confirmation modal, and Global vs. Override vs. Effective policy inheritance matrix in `FleetView.tsx`) |
| Task experience | Add templates, dry-run/preflight display, redacted result summaries, guarded retry/re-run, ownership, and approval-to-audit timelines. | **Shipped & verified** (Tier 1 diagnostic task templates + live `simulatePolicy` dry-run preflight in Dispatch Modal, task ownership display, secret redaction summary badge, Tier 3+ guarded re-run confirmation, and inline Approval-to-Audit trace timeline in `TasksView.tsx`) |
| Approvals/policies | Show the exact rule, tier, arguments, device context, expiry, blast radius, diff, and simulation result before a policy is published. | **Shipped & verified** (enriched pending approval cards with tier, rule/reason, device context, expiry countdown, blast radius, and exact args; added Pre-Publish Review modal with field-by-field diff, inheriting device blast radius, and multi-tool dry-run simulations in `SecurityView.tsx`) |
| Desktop UX | Add connection/permission indicators, keyboard accessibility, monitor clarity, recording consent/status, and interruption recovery. Never imply control for view-only sessions. | **Shipped & verified** (explicit connection + permission badges, active display resolution clarity, operator presence/audit consent banner, Escape-key instant disarm, automatic interruption retry banner, policy/offline locking, and complete removal of keyboard input controls in view-only mode in `DesktopView.tsx`) |
| Docs and localisation | Generate API/environment references; maintain one support matrix; publish operator runbooks; extract remaining strings, validate RTL, and complete keyboard/focus/contrast review. | **Shipped & verified** (`scripts/generate_env_reference.py` → `docs/environment-reference.md`, `scripts/generate_api_reference.py` → `docs/api-reference.md`, canonical `docs/support-matrix.md`, `docs/runbooks.md`, `apps/dashboard/src/i18n.ts` LTR/RTL `en`/`ar` toggle, `role="dialog"`/`aria-modal`, `:focus-visible`, `[dir="rtl"]` logical CSS, and `prefers-reduced-motion`) |
| Code health | Continue splitting high-churn modules, remove duplicated task/audit paths, standardise validation/errors, and protect public contracts with architecture tests. | **Shipped & verified** (architecture & contract guardrails in `tests/unit/test_release3_polish.py`, `test_api_reference.py`, `test_architecture_phase4*.py`, `test_schema_parity.py`) |

## Release 4 — features to add

Start only after Release 1 exit criteria. Each capability needs policy controls, audit coverage, quotas, and a measurable success metric.

| Capability | User outcome | Prerequisites | First thin slice | Status |
|---|---|---|---|---|
| Fleet analytics/capacity planning | Health, failure, task-duration, and update trends by device/group. | Retention, efficient history queries, validated telemetry. | Daily health/task-success trend with CSV export. | **Shipped (feature-flagged)** (`AI_BRIDGE_FEATURE_FLEET_ANALYTICS`, `GET /api/fleet/analytics/trends` JSON + formula-safe CSV, dashboard trend & CSV export in `OverviewView.tsx`) |
| Notification centre/incidents | Actionable alert acknowledgement, assignment, escalation, and resolution history. | Structured events, deduplication, RBAC, audit. | In-app inbox for failed tasks, offline devices, and expired approvals. | **Shipped (feature-flagged)** (`AI_BRIDGE_FEATURE_INCIDENT_INBOX`, `GET /api/incidents`, `POST /api/incidents/{id}/ack` with ack/assign/resolve/reopen history + audit, dashboard Incident Inbox in `OverviewView.tsx`) |
| Policy-as-code | Teams review, test, promote, and roll back policy changes. | Stable schema, simulation, versioning, approval/audit. | Signed policy import/export and CI simulation fixtures. | **Shipped (feature-flagged)** (`AI_BRIDGE_FEATURE_POLICY_AS_CODE`, HMAC-SHA256 `GET /api/policies/export` & `POST /api/policies/import` with dry-run diff/simulation + `unrestricted` block, `tests/fixtures/policy_simulation_fixtures.json`, `scripts/verify_policy_fixtures.py`, dashboard import/export in `SecurityView.tsx`) |
| Enterprise identity lifecycle | Central identity, groups, least privilege, timely deprovisioning. | Live OIDC proof and security review. | OIDC group-to-role mapping with deny-by-default and audit. | **Shipped (opt-in feature flag)** (`AI_BRIDGE_FEATURE_OIDC_GROUP_MAPPING=1`, `AI_BRIDGE_OIDC_ROLE_MAP`, `AI_BRIDGE_OIDC_LEAST_PRIVILEGE`, deny-by-default `403` + `oidc_login_denied`/`oidc_role_mapped` audit + role sync on login in `oidc.py` & `routers/auth.py`) |
| Managed update channels | Staged agent/client updates with health gates and rollback. | Target-platform E2E, signatures, telemetry, rollout runbook. | Canary verify-only ring with approval, pause, and rollback. | **Shipped (feature-flagged)** (`AI_BRIDGE_FEATURE_MANAGED_UPDATE_CHANNELS`, `verify_only` canary ring + `require_approval` / `confirm_promote` promotion gate, pause/resume/rollback in `rollouts.py`) |
| Curated workflow builder | Reviewed reusable automation without bespoke scripting. | Reliable scheduling/recovery, per-step policy/approval, idempotency. | Read-only diagnostic templates with per-step audit timeline. | **Shipped (feature-flagged)** (`AI_BRIDGE_FEATURE_CURATED_WORKFLOWS`, Tier-1 verified `GET /api/workflows/curated` & `GET /api/workflows/{id}/timeline` per-step approval-to-audit timeline) |
| Governed agent memory | Scoped operational knowledge without cross-tenant or prompt-injection risk. | Tenant/provenance controls, filtering, retention, review. | Per-workspace read-only snippets with source and opt-in prompt use. | **Shipped (feature-flagged)** (`AI_BRIDGE_FEATURE_GOVERNED_MEMORY`, tenant-isolated `GET/POST/DELETE /api/memory/snippets` with provenance, injection filter, and opt-in `untrusted_data` prompt rendering) |
| Cross-platform desktop automation | One honest automation contract across Windows, Linux, and macOS. | Platform E2E matrix and capability negotiation. | Capability-aware read-only screen/status API; advertise interaction only where tested. | **Shipped (feature-flagged)** (`AI_BRIDGE_FEATURE_DESKTOP_CAPABILITIES_V2`, `GET /api/devices/{id}/desktop/capabilities` advertising interactive control only on policy-enabled Windows and enforcing view-only on Linux/macOS in `DesktopView.tsx`) |

## Explicitly deferred

- Public multi-tenant SaaS or multi-region/multi-plane federation.
- Default unattended Tier-4 execution or AI-driven auto-approval.
- MCP tool growth merely for breadth.
- Native C++ WebRTC as the default path before full browser/device E2E and operational support.
- Autonomous remediation without policy, idempotency, rollback, and approval boundaries.

## Delivery governance

1. **Release 1:** close production-proof gaps; avoid broad new features in parallel.
2. **Release 2:** make the proven system observable, recoverable, scalable, and releasable.
3. **Release 3:** polish using controlled-fleet feedback.
4. **Release 4:** add one governed capability at a time behind feature flags and adoption metrics.

Every roadmap issue should include an owner, risk tier, platforms, data/tenant implications, rollback plan, test level, acceptance evidence, and documentation updates. Security-sensitive work also requires a threat-model update and policy/approval-path test.

## Evidence

- Current source: `src/ai_bridge/`, `agents/`, `windows-agent/`, `apps/dashboard/`, `apps/windows-client/`, `apps/windows-installer/`, and `scripts/`.
- CI & Ops Proof Workflows:
  - [`.github/workflows/ci.yml`](../.github/workflows/ci.yml): Linux & Windows Python suites (`test`, `windows-python-e2e`), PostgreSQL parity/boot/restore/PITR drills (`postgres-boot`), stdlib deployment configuration checks (`deployment-config`), macOS launchd E2E (`macos-agent`), native Windows WebRTC harness (`windows-native-webrtc`), dashboard build (`dashboard`), Release 1 staging gate + real-browser Chromium WebRTC E2E (`release-1-staging-and-webrtc-gate`), C# Windows client & GUI installer builds (`windows-client`), and supply-chain SBOM/audit (`security-and-supply-chain`).
  - [`.github/workflows/ops-proof.yml`](../.github/workflows/ops-proof.yml): Multi-worker + Redis + PostgreSQL 18 concurrent soak (`scripts/multi_worker_soak.py`) and quarterly scheduled PostgreSQL restore + WAL PITR proof (365-day evidence retention).
  - [`.github/workflows/release.yml`](../.github/workflows/release.yml): Tagged `v*` GitHub Release pipeline building sdist/wheel, signed release manifest, CycloneDX SBOM + evidence bundle, and self-contained Windows `AIBridgeSetup.exe` + `.sha256`.
- Status & Reference records: [support-matrix.md](./support-matrix.md), [api-reference.md](./api-reference.md), [environment-reference.md](./environment-reference.md), [runbooks.md](./runbooks.md), [fleet-limits.md](./fleet-limits.md), [release-1-verification.md](./release-1-verification.md), [implementation-status.md](./implementation-status.md), [production-readiness.md](./production-readiness.md), [FINAL_TEST_RESULTS.md](../analysis/FINAL_TEST_RESULTS.md), and the current assessment [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md).

Historical audit documents retain earlier findings for traceability. This roadmap treats source-present work as shipped but retains production-environment validation as a separate, mandatory gate.

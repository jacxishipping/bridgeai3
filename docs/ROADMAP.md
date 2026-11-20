# AI Bridge Product Roadmap

**Planning baseline:** 20 November 2026
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
| P0 | Windows installer/service end-to-end test | **Verified locally (`7/7` installer/DPAPI E2E + live polling Windows agent against PostgreSQL 17.11); CI wired (`windows-python-e2e`) + `scripts/windows_reboot_soak.ps1` ready** — clean-VM OS reboot soak pending | A clean Windows image installs, enrols, survives reboot, completes safe and approved tasks, streams/view-controls where enabled, rotates/revokes its token, and uninstalls cleanly. |
| P0 | Real-browser WebRTC E2E | **Verified locally (`7/7`, real Google Chrome `chrome.exe` ↔ `aiortc`: ICE `connected`, `6–7` decoded video frames, ephemeral TURN minted, live DC view-only/takeover/disconnect release); CI wired (`release-1-staging-and-webrtc-gate`)** — cross-NAT public TURN run pending | A staging test proves ICE/TURN connection, view-only enforcement, authorised takeover, input release on disconnect, and session expiry using a browser and enrolled endpoint. |
| P0 | macOS launchd E2E | **Harness & CI wired (`macos-agent` on `macos-latest`)** — not yet executed on a physical Mac | A Mac installs launchd, enrols, survives reboot, polls, executes a safe task, and reports honest desktop capability status. |
| P0 | Production database restore drill | **Verified live on PostgreSQL 17.11 (active-DB logical `pg_dump`/`pg_restore` RTO `1.24–1.33s` + physical `pg_basebackup`/WAL PITR RTO `2.98s`, RPO `2.34s`, `16/16` migrations, audit chain valid) + SQLite; CI wired (`postgres-boot`)** — hosted staging/prod DB drill pending | Restore PostgreSQL backup/PITR into isolation, boot from migrations, validate users/devices/tasks/approvals/audit continuity, and record RTO/RPO. |
| P0 | Staging release gate | **Verified live (`10/10` against live PostgreSQL 17.11 plane + built v3 dashboard + `smoke_check.py` `6/6`); CI wired** — archive hosted staging RC evidence pending | Archive evidence for migrations, readiness, dashboard smoke, enrolment, safe task, approval, rollback, and security configuration review. |
| P1 | Independent security/architecture review | **Complete & remediated** (fresh read-only reviewer audited all 5 boundaries; all 3 P0 + 7 P1 findings fixed and regression-tested) | An independent reviewer signs off on policy/approval, tenant, auth, native-agent, and update boundaries, or findings are remediated. |
| P1 | Correct test health | **Complete in-tree** (2 flaky tests fixed, 10 Windows test/script bugs fixed, poller revocation exit code `2` fixed, pytest markers & supported-platform CI matrix published) | Replace/quarantine the known timing-flaky test; separate platform/key-dependent tests into explicit jobs; publish a green supported-platform matrix. |

**Exit criteria:** P0 evidence accompanies a release candidate; no critical/high unresolved finding; support claims match the tested platform matrix; rollback and recovery are approved.
**Remaining external-host sign-off checks:** push and observe all CI matrix jobs green (`postgres-boot`, `windows-python-e2e`, `macos-agent`, `release-1-staging-and-webrtc-gate`), run `scripts/windows_reboot_soak.ps1` on a clean Windows VM, run `scripts/backup_drill.sh` + `scripts/staging_release_gate.py --url` + cross-NAT TURN check against hosted staging, and verify macOS on a physical Mac (or scope macOS as experimental for Release 1).

## Release 2 — reliability, scale, and operations

**Goal:** operate a growing fleet predictably and make failures inexpensive to diagnose.

| Theme | Enhancements needed | Acceptance measures |
|---|---|---|
| Multi-worker correctness | Load-test cancellation, OIDC, schedules, idempotency, and rate limits across workers/restarts; require Redis when multi-host operation is enabled. | Reproducible load report, no duplicate schedules, cross-worker cancellation, documented fleet limits. |
| Task lifecycle | Add progress heartbeats, configurable retry/reconciliation, dead-letter handling, clear failure reasons, and task/event retention. | No permanently ambiguous task state and a documented recovery path after agent failure. |
| Database operations | Add query/pool observability, capacity alarms, query-plan review, and scheduled restore drills. | API/task latency SLOs, DB exhaustion alerts, quarterly restore proof, tested retention. |
| Observability | Standardise structured logs, traces/correlation IDs, secured metrics, native diagnostics, and alert runbooks. | One task can be traced from dashboard/MCP request through agent result and audit record. |
| Release engineering | Add dependency/container/SBOM/secret/vulnerability scanning, signed artifacts, and retained release evidence. | Every release has artifact metadata, scans, migration/rollback plan, and test evidence. |
| Agent resilience | Improve offline spool visibility, network recovery, diagnostics, update rollout rings, and agent health remediation. | Fleet view exposes spool age, poll state, policy/version drift, and update state; rollouts can pause/rollback. |

## Release 3 — polish

| Area | Work |
|---|---|
| Dashboard truthfulness | Keep demo and live data unmistakably separate; never show invented verification/provider/audit values as production state. Add empty, offline, denied, and degraded states to every view. |
| Fleet navigation | Add cursor/server-side pagination, saved filters, bulk-action safeguards, device groups, and clear policy inheritance/override explanations. |
| Task experience | Add templates, dry-run/preflight display, redacted result summaries, guarded retry/re-run, ownership, and approval-to-audit timelines. |
| Approvals/policies | Show the exact rule, tier, arguments, device context, expiry, blast radius, diff, and simulation result before a policy is published. |
| Desktop UX | Add connection/permission indicators, keyboard accessibility, monitor clarity, recording consent/status, and interruption recovery. Never imply control for view-only sessions. |
| Docs and localisation | Generate API/environment references; maintain one support matrix; publish operator runbooks; extract remaining strings, validate RTL, and complete keyboard/focus/contrast review. |
| Code health | Continue splitting high-churn modules, remove duplicated task/audit paths, standardise validation/errors, and protect public contracts with architecture tests. |

## Release 4 — features to add

Start only after Release 1 exit criteria. Each capability needs policy controls, audit coverage, quotas, and a measurable success metric.

| Capability | User outcome | Prerequisites | First thin slice |
|---|---|---|---|
| Fleet analytics/capacity planning | Health, failure, task-duration, and update trends by device/group. | Retention, efficient history queries, validated telemetry. | Daily health/task-success trend with CSV export. |
| Notification centre/incidents | Actionable alert acknowledgement, assignment, escalation, and resolution history. | Structured events, deduplication, RBAC, audit. | In-app inbox for failed tasks, offline devices, and expired approvals. |
| Policy-as-code | Teams review, test, promote, and roll back policy changes. | Stable schema, simulation, versioning, approval/audit. | Signed policy import/export and CI simulation fixtures. |
| Enterprise identity lifecycle | Central identity, groups, least privilege, timely deprovisioning. | Live OIDC proof and security review. | OIDC group-to-role mapping with deny-by-default and audit. |
| Managed update channels | Staged agent/client updates with health gates and rollback. | Target-platform E2E, signatures, telemetry, rollout runbook. | Canary verify-only ring with approval, pause, and rollback. |
| Curated workflow builder | Reviewed reusable automation without bespoke scripting. | Reliable scheduling/recovery, per-step policy/approval, idempotency. | Read-only diagnostic templates with per-step audit timeline. |
| Governed agent memory | Scoped operational knowledge without cross-tenant or prompt-injection risk. | Tenant/provenance controls, filtering, retention, review. | Per-workspace read-only snippets with source and opt-in prompt use. |
| Cross-platform desktop automation | One honest automation contract across Windows, Linux, and macOS. | Platform E2E matrix and capability negotiation. | Capability-aware read-only screen/status API; advertise interaction only where tested. |

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

- Current source: `src/ai_bridge/`, `agents/`, `windows-agent/`, `apps/dashboard/`, `apps/windows-client/`, and `scripts/`.
- CI: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), covering Linux & Windows Python suites (`test`, `windows-python-e2e`), PostgreSQL parity/boot/restore/PITR drills (`postgres-boot`), stdlib deployment configuration checks (`deployment-config`), macOS launchd E2E (`macos-agent`), native Windows WebRTC harness (`windows-native-webrtc`), dashboard build (`dashboard`), Release 1 staging gate + real-browser Chromium WebRTC E2E (`release-1-staging-and-webrtc-gate`), and C# Windows client build/tests (`windows-client`).
- Status records: [release-1-verification.md](./release-1-verification.md), [implementation-status.md](./implementation-status.md), [production-readiness.md](./production-readiness.md), [FINAL_TEST_RESULTS.md](../analysis/FINAL_TEST_RESULTS.md), and [FINAL_PRODUCTION_READINESS.md](../analysis/FINAL_PRODUCTION_READINESS.md).

Historical audit documents retain earlier findings for traceability. This roadmap treats source-present work as shipped but retains production-environment validation as a separate, mandatory gate.

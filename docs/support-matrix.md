# Platform & Capability Support Matrix

**Updated:** 27 November 2026  
**Verdict companion:** [analysis/PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md)

This document is the single canonical source of truth for what is **supported**, **preview/experimental**, and **verified in CI** for AI Bridge. Support claims must never outrun tested evidence.

## Control Plane & Data Tier

| Component | Status | Automated CI Verification | Notes |
|---|---|---|---|
| Python Control Plane (`src/ai_bridge/control/`) | **Supported** (Python 3.11+) | `test` (Linux), `windows-python-e2e` (Windows) | FastAPI modular monolith; fail-closed auth & policy engine. |
| SQLite (WAL mode) | **Supported** (single-host / single-worker) | `test`, `windows-python-e2e` | Default embedded storage; schema parity verified across migrations `001`–`017`. |
| PostgreSQL 16+ (`psycopg` + `psycopg_pool`) | **Supported** (production / multi-worker) | `postgres-boot` (schema parity, upgrade/downgrade, backup/restore, PITR drill) | Required with Redis when `AI_BRIDGE_MULTI_HOST=1` or production `AI_BRIDGE_WORKERS > 1`. |
| Redis (rate limits, WS fan-out, pub/sub) | **Supported** (multi-worker / multi-host) | Unit/integration fakes + `deployment-config` | Fail-fast enforcement at boot when multi-host or production multi-worker is enabled. |
| React Management Dashboard (`apps/dashboard/`) | **Supported** (Chromium, Firefox, Edge, Safari) | `dashboard` (`typecheck` + `build`), `release-1-staging-and-webrtc-gate` | Supports LTR (`en`) and RTL (`ar`), light/dark themes, keyboard navigation. |

## Endpoint Agents & Clients

| Target | Agent / Client | Tier 1–3 Tools & Terminal | Interactive Desktop & WebRTC | Persistence & Service | Support Tier | CI Verification |
|---|---|---|---|---|---|---|
| **Windows 10/11 & Server 2022** | Python polling agent (`agents/windows/`) + WPF Client (`apps/windows-client/` → `AIBridgeSetup.exe`) | **Supported** | **Supported** (opt-in dual-control, DPAPI token sealing, JPEG + WebRTC + **nsl1 WebCodecs screen-relay**, Hybrid A11y / agent-desktop FFI) | Windows Service / Scheduled Task | **Supported** | `windows-python-e2e`, `windows-client`, `windows-native-webrtc`, release `build-windows-installer` |
| **Linux (Ubuntu 22.04/24.04, Debian, RHEL)** | Python polling agent (`agents/linux/`) + Interactive Terminal Studio (`ai-bridge-linux-tui`) | **Supported** | View-only / capability-gated (X11/Wayland; screen-relay when display present) | `systemd` unit (`deployment/systemd/`) | **Supported** | `test`, `deployment-config`, release `build-linux-terminal-app` |
| **macOS 14+** | Python polling agent (`agents/macos/`) + Terminal Studio / TCC doctor (`ai-bridge-macos-tui`, rolling) | **Preview** | View-only / capability-gated (Screen Recording & Accessibility TCC required; screen-relay when permitted) | `launchd` LaunchAgent | **Preview** | `macos-agent` (launchd E2E; interactive physical reboot sign-off optional) |
| **Windows Native C++ WebRTC** | `windows-agent/` | N/A (media dataplane) | **Preview** (feature-flagged; browser/device E2E harness passing) | Bundled binary | **Preview** | `windows-native-webrtc` |
| **Cross-OS agent-desktop FFI** | Optional `agent-desktop` CLI + C FFI (`@ref` Accessibility Tree Inspector) | N/A | **Preview** (bundled when `bundle_agent_desktop` release input / repo var is set) | Release artifact | **Preview** | release `bundle-agent-desktop` matrix |

## Governance, Auth & Operations

| Capability | Status | Enforcement / Guarantee | Reference |
|---|---|---|---|
| RBAC (`viewer`, `operator`, `admin`) + Session & Admin Token Auth | **Supported** | Fail-closed; tenant derived server-side; CSRF double-submit on session mutations. | [api-reference.md](./api-reference.md) |
| OIDC / SSO (PKCE + JWKS + atomic state exchange) | **Supported** | Single-use `DELETE ... RETURNING` state consumption; strict redirect-URI checks. | [production-readiness.md](./production-readiness.md) |
| Device mTLS Fingerprint Pinning + Token Rotation | **Supported** | SHA-256 hashed tokens; DPAPI on Windows; `POST /api/agent/rotate`. | [runbooks.md](./runbooks.md) |
| Policy Simulation, Versioning & Device Overrides | **Supported** | Intersection (fail-closed) across global + per-device + client policies; pre-publish diff & simulation. | [api-reference.md](./api-reference.md) |
| Task Retries, Leases, Dead-Letter Queue (DLQ) & Retention | **Supported** | Per-tool idempotency classifier, exponential backoff, DLQ replay/purge, `retention_sweep_from_env`. | [runbooks.md](./runbooks.md) |
| End-to-End Traceability (`X-Correlation-ID` → `GET /api/traces/{trace_id}`) | **Supported** | Propagated across dashboard, MCP, control plane, agent spool, task events, and audit chain. | [runbooks.md](./runbooks.md) |
| Staged Rollout Rings (`canary` → `early` → `general`) | **Supported** | Health-gate auto-pause, pause/resume, and `rollback_fleet_agent`; 1-click `POST /api/devices/{id}/update` when signed `release-manifest.json.sig` is present. | [fleet-limits.md](./fleet-limits.md) |
| Supply Chain (CycloneDX SBOM, Signed Releases, Evidence Bundle) | **Supported** | `scripts/generate_sbom.py`, `scripts/assemble_release_evidence.py`, `.github/workflows/release.yml` + `AI_BRIDGE_RELEASE_SIGNING_KEY` Ed25519 PEM. | [release-1-verification.md](./release-1-verification.md) · [docs/release-signing.md](./release-signing.md) |
| 9-Pillar Control Plane Expansion | **Supported** (lab / pilot) | Unified Device Intelligence, Autonomous Planner, Multi-Agent Blackboard, Visual DAG Builder, Device Episodic Memory, SaaS Tenancy Quotas (operator-scoped; public SaaS still deferred). | Dashboard Workflows / Overview / Fleet 360 · `/api/devices/{id}/intelligence` · `/api/fleet/intelligence` |
| ChatGPT Custom GPT Actions | **Supported** (integration pack) | OpenAPI Actions + Bearer against TLS plane; policy/approvals unchanged. | [integrations/chatgpt/](../integrations/chatgpt/) · [mcp-clients.md](./mcp-clients.md) |
| ChatGPT / Copilot remote MCP | **Supported** (where plan allows) | Streamable HTTP behind TLS + bearer/OAuth gateway. | [remote-access.md](./remote-access.md) |

## Desktop transports & interactive clients (v0.3)

| Capability | Windows | Linux | macOS | Notes |
|---|---|---|---|---|
| nsl1 WebCodecs screen-relay | **Supported** (WPF + dashboard Desktop) | **Preview** (when display + relay-engine present) | **Preview** (TCC Screen Recording) | Optional `bundle_screen_relay` release artifacts |
| In-process agent-desktop FFI / `@ref` A11y inspector | **Supported** (HybridA11y) | **Preview** (AT-SPI) | **Preview** (Accessibility TCC) | Optional release matrix |
| Interactive Terminal Studio | WPF GUI (`AIBridgeClient.exe`) | `ai-bridge-linux-tui` (5-tab curses/REPL) | `ai-bridge-macos-tui` + TCC doctor (**Preview**) | Published via GitHub Releases when release gate is green |

## Explicitly Unsupported / Deferred

- Multi-region / active-active multi-plane federation / **public** multi-tenant SaaS (operator tenancy quotas exist; do not market as public SaaS).
- Unattended Tier-4 execution or AI-driven auto-approval by default.
- Native C++ WebRTC as the default transport before full production soak.
- Claiming a **signed production RC** before hosted reboot soak, staging gate, backup drill, and cross-NAT TURN evidence ([PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md) §2).

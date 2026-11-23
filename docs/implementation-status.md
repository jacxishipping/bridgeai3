# Implementation status

**Updated:** 23 November 2026  
**HEAD context:** `fix/windows-desktop-stack-gaps` @ `78fdbb4`+ (Release 1 evidence anchor `5eb8750`)  
**Verdict:** see [analysis/PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md) · platforms: [support-matrix.md](./support-matrix.md) · roadmap: [ROADMAP.md](./ROADMAP.md)

This file is the honest “what exists / what is verified” report. Prefer it over older analysis-wave notes when status conflicts.

---

## Production posture (summary)

| Class | Status |
|---|---|
| Local / lab | **GO** |
| Small controlled production fleet | **CONDITIONAL GO** — hosted/physical RC evidence still required |
| Public multi-tenant SaaS | **NO-GO** (deferred) |

Remaining RC sign-offs: Windows reboot soak, hosted staging gate, hosted Postgres restore drill, cross-NAT TURN. Details in [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md) and [release-1-verification.md](./release-1-verification.md).

---

## Control plane & security

- Device tokens: TTL (default 7 days), rotation, hashed at rest; revoke exits agents with code `2`.
- Local users: PBKDF2 passwords, sessions, RBAC (`viewer` / `operator` / `admin`); CSRF on session mutations; shared `AI_BRIDGE_CSRF_SECRET` required for multi-worker.
- Legacy admin token: off by default in production; optional via explicit enable + token.
- Policies: versioned, simulated, audited; Tier 3/4 opt-in; single `task_service.create_task` gate for HTTP/v1/AI API paths.
- Approvals: actor, args, device, tier, expiry; workflow approvals cannot cover Tier 4.
- Audit: hash-chained; export + retention prune (not unconditionally append-only).
- Emergency stop: **durable** via `plane_state` / settings (not process-memory-only).
- OIDC: in-tree (`AI_BRIDGE_OIDC_*`); PKCE; pending state in shared settings for multi-worker; live provider E2E is environment-dependent.
- Device mTLS fingerprint gate: env-gated (`AI_BRIDGE_DEVICE_MTLS`); depends on reverse-proxy headers.
- Secrets: Fernet at rest for MCP OAuth secrets (`AI_BRIDGE_MCP_TOKEN_KEY`); child env scrubbed.
- Signed updates: Ed25519 manifests; auto-install gated (`AI_BRIDGE_ALLOW_AUTO_UPDATE`).

Production readiness hard-fails without Postgres, TURN/ICE, admin access, durable e-stop, and Redis when multi-worker — see `src/ai_bridge/control/production_readiness.py`.

---

## Data & multi-worker

- SQLite: local/dev default.
- PostgreSQL: required for `AI_BRIDGE_PROFILE=production`; CI `postgres-boot` (parity, boot, logical restore, PITR).
- Redis: required for multi-host / production `AI_BRIDGE_WORKERS > 1` (rate limits, WS fan-out).
- Task lifecycle: leases, retries, DLQ, progress, retention (migration `017` era).
- Schema: migrations `001`–`017`; parity tooling in CI.

---

## Platforms

| Platform | Status | Evidence |
|---|---|---|
| Windows agent + installer + DPAPI | **Supported** | CI `windows-python-e2e` + local installer/live agent E2E (`7/7` at R1) |
| Windows WPF client | **Supported** | CI `windows-client` |
| Native C++ WebRTC (`ai_bridge_desktop`) | **Preview** | CI `windows-native-webrtc`; preferred when present (`AI_BRIDGE_NATIVE_WEBRTC=auto`) |
| Linux agent (systemd) | **Supported** | CI `test` + deployment-config |
| macOS agent (launchd) | **Preview** | CI `macos-agent` launchd E2E; physical reboot optional |

Do **not** claim macOS as fully Supported until physical reboot is signed off (or keep Preview).

---

## Desktop / WebRTC

- Canonical API: `POST /api/v1/sessions` (legacy create **410** in production unless `AI_BRIDGE_ALLOW_LEGACY_WEBRTC=1`).
- View-only / human takeover enforced agent-side (default-deny input).
- Real-browser E2E: CI Chromium + local Chrome ↔ aiortc (**7/7** at R1).
- TURN required in production profile.
- Desktop audio: off by default (`AI_BRIDGE_DESKTOP_AUDIO=1` = silent PCM stub until WASAPI is real).
- Cross-NAT public TURN: still a hosted RC item.

---

## Dashboard & operator UX

- React/Vite live workspace: honest empty/offline/denied/degraded states; demo opt-in and labelled.
- Fleet pagination, policy inheritance matrix, approvals with blast radius, Live Desktop view-only safety.
- Build: `npm --prefix apps/dashboard run build` (dist not committed).

---

## MCP & AI clients

- Stdio MCP: Claude, Codex, Gemini, generic — `integrations/*/`.
- Remote Streamable HTTP + OAuth: ChatGPT connectors, Copilot Studio — `docs/remote-access.md`, `integrations/chatgpt/`, `integrations/copilot365/`.
- **ChatGPT Custom GPT Actions** pack: `integrations/chatgpt/actions/` (OpenAPI + instructions); plane accepts Bearer on desktop frame/input and approvals.

---

## Residuals (tracked, not “unsupported”)

- Windows service launcher token still ACL-locked in `run-agent.cmd` (DPAPI migration planned).
- Some supervisor / WebRTC queue inserts not fully unified onto `task_service`.
- Update anti-rollback floor + HTTPS-only URL enforcement.
- C# `AllowedCommands` metacharacter tightening.
- Hosted RC evidence (§ Production posture).

---

## Related docs

| Doc | Role |
|---|---|
| [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md) | Current go / conditional-go / no-go |
| [release-1-verification.md](./release-1-verification.md) | R1 gate evidence @ `5eb8750` |
| [support-matrix.md](./support-matrix.md) | Supported vs Preview |
| [production-readiness.md](./production-readiness.md) | Operator deploy checklist |
| [ROADMAP.md](./ROADMAP.md) | Release themes |

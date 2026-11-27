# AI Bridge — Production Readiness Assessment

**Date:** 27 November 2026  
**Branch / HEAD:** `main` @ `v0.3.0` release train (post 9-pillar + Terminal Studio)  
**Release 1 evidence anchor:** `5eb8750` (20 Nov 2026)  
**Supersedes:** `analysis/FINAL_PRODUCTION_READINESS.md` (removed — Nov 17 NO-GO snapshot; no longer authoritative)

**Canonical companions:** [docs/ROADMAP.md](../docs/ROADMAP.md) · [docs/release-1-verification.md](../docs/release-1-verification.md) · [docs/support-matrix.md](../docs/support-matrix.md) · [docs/release-signing.md](../docs/release-signing.md) · [docs/fleet-limits.md](../docs/fleet-limits.md) · [docs/production-readiness.md](../docs/production-readiness.md) (ops checklist) · `src/ai_bridge/control/production_readiness.py`

---

## Verdict

| Deployment class | Verdict | Meaning |
|---|---|---|
| **Local / lab** | **GO** | Safe for development and demos on non-production data. SQLite or local Postgres; no production credentials. |
| **Small controlled production fleet** | **CONDITIONAL GO** | Code, CI, and local Postgres staging meet Release 1 intent. Hosted/physical RC evidence below must be archived before an operator signs a production RC. |
| **Public multi-tenant SaaS** | **NO-GO** | Explicitly deferred (multi-region, federation, public tenancy). Do not market or operate as SaaS. |

**One-line summary:** Strong lab- and CI-proven control plane for a known-operator Windows/Linux fleet; not yet a signed hosted production RC; never a public SaaS claim.

---

## 1. What is proven

### CI matrix (Release 1 anchor `5eb8750` — all green)

| Job | Surface |
|---|---|
| `test` | Linux Python suite + ruff |
| `windows-python-e2e` | Windows suite + installer/DPAPI E2E |
| `macos-agent` | launchd E2E + desktop honesty |
| `postgres-boot` | Schema parity, boot, logical restore, PITR (PG 18) |
| `deployment-config` | Stdlib compose/deploy regressions |
| `dashboard` | React typecheck + build |
| `windows-client` | .NET WPF client |
| `windows-native-webrtc` | C++ libdatachannel harness |
| `release-1-staging-and-webrtc-gate` | Staging gate + Chromium WebRTC peer |

### Local staging (Windows Server 2025, 20 Nov 2026)

- Installer/DPAPI + live polling agent: **7/7**
- Real Chrome ↔ aiortc WebRTC: **7/7** (ICE connected, frames, view-only / takeover / disconnect)
- Staging release gate vs live PostgreSQL 17.11 + built dashboard: **10/10**
- Postgres logical + physical PITR drills with valid audit chain
- Independent security review: all P0/P1 findings remediated with regression tests
- Windows suite record: **945 passed, 0 failed** (at R1 anchor)

### In tree since R1 (through v0.3.0)

- Prefer native `ai_bridge_desktop` WebRTC on Windows fleet dispatch
- Plane ICE / TURN pull into native peer; WebRTC production E2E harness
- Multi-worker e-stop / clipboard / device-function alignment
- WPF UX: poller vs Agents clarity, takeover Return Control, toasts, a11y, visual polish
- ChatGPT Custom GPT Actions pack (`integrations/chatgpt/actions/`) + Bearer on desktop/approval routes
- **Cross-OS nsl1 WebCodecs screen-relay** (dashboard Desktop + optional `relay-engine` release artifacts)
- **In-process agent-desktop FFI** + `@ref` Accessibility Tree Inspector (HybridA11y / optional release matrix)
- **Interactive Linux Terminal Studio** (`ai-bridge-linux-tui` curses/REPL; release zipapp)
- **9-Pillar expansion:** Unified Device Intelligence, Autonomous Planner, Multi-Agent Blackboard, Visual DAG Builder, Device Episodic Memory, SaaS Tenancy Quotas (operator-scoped)
- macOS Terminal Studio + TCC doctor + `ai-bridge-macos-tui` release bundle (Preview)

---

## 2. What still blocks a signed production RC

| # | Item | Priority | How to close |
|---|---|---|---|
| 1 | Clean virgin Windows VM reboot soak | **P0** | `scripts/windows_reboot_soak.ps1` (PreReboot → reboot → PostReboot → Uninstall) |
| 2 | Hosted staging release gate | **P0** | `scripts/staging_release_gate.py --url https://<staging>` + archive artifact |
| 3 | Hosted Postgres backup / PITR drill | **P0** | `scripts/backup_drill.sh` against real staging/prod DB; record RTO/RPO |
| 4 | Cross-NAT public TURN | **P0** | coturn + enrolled Windows device via `analysis/webrtc-e2e-harness.html` |
| 5 | Re-anchor CI green on HEAD / release gate | **P0 hygiene** | Green `build-sign-and-bundle` verify step so `publish-github-release` ships `AIBridgeSetup.exe` + `ai-bridge-linux-tui` (+ macOS TUI) |
| 6 | macOS physical reboot **or** keep Preview | **P1 / scope** | Physical Mac check, or leave macOS as Preview in support matrix |
| 7 | Production Ed25519 release signing | **P1** | `python scripts/generate_release_signing_key.py` → secret `AI_BRIDGE_RELEASE_SIGNING_KEY` → `release-manifest.json.sig` ([docs/release-signing.md](../docs/release-signing.md)) |
| 8 | Multi-host staging load soak | **P1** | Beyond CI/local (`ops-proof.yml` / soak scripts against hosted plane) |

Until items **1–4** (and preferably **5**) are archived with the RC, do **not** claim “production signed.”

---

## 3. Production profile hard requirements

Enforced by `assess_production_readiness()` and `/health/ready` (production returns **503** if any critical issue exists). Profile is on when `AI_BRIDGE_PROFILE` is production-like **or** `RENDER` is set.

### Critical (must pass)

| Requirement | Env / behaviour |
|---|---|
| PostgreSQL | `DATABASE_URL` (SQLite is lab-only) |
| TURN / ICE | `AI_BRIDGE_TURN_SECRET` **or** `AI_BRIDGE_TURN_URL` **or** `AI_BRIDGE_ICE_SERVERS` |
| Admin access | Active admin user **or** configured legacy admin token path |
| HTTPS public origin | `AI_BRIDGE_PUBLIC_BASE_URL` / `AI_BRIDGE_PUBLIC_URL` must not be `http://` |
| Durable emergency stop | Readable from settings |
| Redis (multi-worker) | Required when `AI_BRIDGE_WORKERS > 1`, `AI_BRIDGE_MULTI_HOST`, or `AI_BRIDGE_REQUIRE_REDIS` |
| Chromium sandbox | Must stay on unless `AI_BRIDGE_ALLOW_UNSAFE_BROWSER=1` |
| Optional SIEM / Vault | If required/configured, URL/addr must be present |

### Adjacent boot / compose gates

- Prod compose fail-fast secrets: Postgres password, admin token, **shared `AI_BRIDGE_CSRF_SECRET`**, TURN secret
- Defaults: `AI_BRIDGE_ALLOW_TIER3=0`, desktop opt-in, legacy WebRTC create disabled in production
- Fernet `AI_BRIDGE_MCP_TOKEN_KEY` for encrypted MCP OAuth secrets at rest
- Metrics scrape auth (`AI_BRIDGE_METRICS_TOKEN`)

Smoke after deploy:

```bash
python deployment/smoke_check.py https://YOUR_HOST
```

---

## 4. Platform support (honest)

| Platform | Support | Desktop / streaming / interactive client |
|---|---|---|
| Windows 10/11 / Server | **Supported** | Interactive control **Supported** (opt-in Tier 3/desktop); nsl1 screen-relay + Hybrid A11y; WPF `AIBridgeClient.exe` / `AIBridgeSetup.exe` |
| Linux (Ubuntu/Debian/RHEL-class) | **Supported** | View-only / capability-gated; Interactive Terminal Studio (`ai-bridge-linux-tui`) |
| macOS 14+ | **Preview** | View-only / capability-gated (TCC); Terminal Studio + TCC doctor Preview |
| Native C++ WebRTC | **Preview** | Preferred on Windows when `ai_bridge_desktop` present (`AI_BRIDGE_NATIVE_WEBRTC=auto`); not the default ops claim |
| agent-desktop FFI / `@ref` inspector | **Preview** | Optional cross-OS release bundle |

See [docs/support-matrix.md](../docs/support-matrix.md) for CI verification columns.

---

## 5. Security posture

| Area | Status |
|---|---|
| Policy / approvals | Four-tier fail-closed; versioned policies; Tier 3/4 opt-in; workflow approvals cannot cover Tier 4 |
| Auth | RBAC + sessions + CSRF; OIDC PKCE in tree; device tokens hashed/rotatable/TTL; optional device mTLS |
| Tenant | Server-derived scoping; R1 remediations for frames, terminals, sessions, approvals |
| Audit / e-stop | Hash-chained audit; durable emergency stop |
| Updates | Ed25519 signed manifests; auto-update gated |
| Independent review | R1 P0/P1 remediations landed with tests |

### Tracked residuals (not RC blockers for a small fleet)

- ~~Windows `run-agent.cmd` may still hold an ACL-locked launcher token (DPAPI migration planned)~~ — **Closed** (`deployment/windows/installer_gui.py` now writes `AI_BRIDGE_TOKEN_FILE` / `--token-file` pointing to DPAPI-sealed `device.token` with batch metacharacter rejection; no plaintext token in `run-agent.cmd`).
- ~~Some supervisor / WebRTC queue paths not fully unified onto `task_service.create_task()`~~ — **Closed** (`supervisor.enqueue_role_task`, `app._queue_webrtc_task`, and `v1_routes` remote WebRTC session queuing now route through `task_service.create_task()`).
- ~~`updates.py`: anti-rollback version floor + HTTPS-only URL enforcement~~ — **Closed** (`validate_update_url` enforces `https://`-only URLs; monotonic `~/.ai-bridge/update-version-floor.json` + `AI_BRIDGE_UPDATE_MIN_VERSION` blocks signed downgrades).
- ~~C# `AllowedCommands` metacharacter tightening + bounded large file reads~~ — **Closed** (`NativeToolExecutor.ValidateAllowedCommand` blocks shell metacharacters; `ReadTextBounded` streams bounded chunks without unbounded `File.ReadAllText`).

---

## 6. Acceptable vs unacceptable use

**Acceptable now**

- Local/lab single- or multi-worker (with Redis) against disposable data
- Controlled pilot fleet **after** hosted P0 evidence is archived and prod profile checks are green
- ChatGPT Custom GPT Actions / remote MCP against an authorized plane (Bearer never in chat)

**Unacceptable**

- Public multi-tenant SaaS or multi-region federation claims
- Unattended Tier-4 / AI auto-approval by default
- Production credentials in chat, browser tests, or committed files
- Claiming “production signed” without items §2.1–4

---

## 7. How this document relates to others

| Document | Role |
|---|---|
| **This file** | Current go / conditional-go / no-go assessment |
| `docs/release-1-verification.md` | Detailed R1 gate evidence @ `5eb8750` |
| `docs/ROADMAP.md` | Product direction and release themes |
| `docs/production-readiness.md` | Operator release checklist (backup, OIDC, smoke) |
| `docs/support-matrix.md` | Supported vs Preview claims |
| `analysis/21_PRODUCTION_READINESS_REPORT.md` | **Historical** Nov 17 audit (P0-era NO-GO); keep for traceability only |

When docs disagree, prefer **this file + ROADMAP + release-1-verification + support-matrix + `production_readiness.py`**.

---

## 8. Next operator actions (ordered)

1. Run Windows reboot soak on a clean VM; archive logs.  
2. Point staging URL at hosted plane; run staging gate + backup drill; archive evidence.  
3. Prove cross-NAT TURN with an enrolled Windows host.  
4. Confirm CI green on current HEAD; cut / re-cut signed `v*` release (`AIBridgeSetup.exe`, `ai-bridge-linux-tui`, optional macOS TUI).  
5. Generate Ed25519 release key (`scripts/generate_release_signing_key.py`); set `AI_BRIDGE_RELEASE_SIGNING_KEY`; verify `release-manifest.json.sig` on clideos.us staged rings.  
6. Keep macOS Preview unless a physical Mac reboot is signed off.  
7. Rotate any tokens previously exposed; set prod env per §3; leave Tier 3/desktop off until needed.

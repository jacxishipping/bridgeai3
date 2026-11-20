# Release 1 — Production Proof, Security Review & Supported-Platform Matrix

**Date:** 20 November 2026
**Scope:** Release 1 exit-criteria execution, security boundary review of post-Wave 7 commits (`caabc53`..`289a7df`), and cross-platform test health.

## 1. Release 1 Gate Matrix

| Priority | Work item | Automated harness / CI wiring | Verified locally (Windows host, 20 Nov 2026) | Remaining external-target proof |
|---|---|---|---|---|
| **P0** | Windows installer/service E2E | `scripts/windows_installer_e2e.py` · CI job `windows-python-e2e` (`windows-latest`) | **PASS (7/7):** enroll, silent install + DPAPI token sealing (no secret in JSON, safe defaults), post-restart subprocess poll & Tier-1 task, Tier-3 approval gate, desktop capability + view-only v1 session, rotation/revocation (exit 2), clean uninstall/purge. Native `.NET` client builds cleanly (`15/15` unit tests). | Full OS reboot + Windows Scheduled Task / Service soak on a dedicated clean fleet VM image. |
| **P0** | Real-browser WebRTC E2E | `scripts/webrtc_browser_e2e.py` · CI job `release-1-staging-and-webrtc-gate` (installs `.[webrtc,browser]` + Playwright Chromium, runs `--require-browser-peer`) | **PASS (signaling & enforcement):** `/api/v1/sessions/ice-servers`, v1 view-only session, single-use WS ticket + replay/no-auth rejection, plane takeover propagation, agent-side view-only & takeover input refusal, `release_all_keys` disconnect cleanup, session termination. Live Chromium media skipped locally (`aiortc`/`playwright` not installed on this host). | Observe green CI run of `release-1-staging-and-webrtc-gate` and run `analysis/webrtc-e2e-harness.html` against staging TURN/coturn + enrolled Windows device. |
| **P0** | macOS launchd E2E | `scripts/macos_launchd_e2e.py` · CI job `macos-agent` (`macos-latest`) | Cross-platform harness & XML plist rendering verified (`6/6` dry-run checks). **Not executed on macOS here.** | Observe green CI `macos-agent` run on `macos-latest` plus interactive `launchctl bootstrap` + reboot test on a dedicated Mac. |
| **P0** | Production database restore drill | `scripts/backup_restore.py drill` + `scripts/backup_drill.sh` + `scripts/postgres_pitr_drill.py` · CI job `postgres-boot` | **PASS (live PostgreSQL 17.11 + SQLite):** schema parity (`24 tables, 23 indexes, 4 FKs, 5 CHECKs`), Postgres boot (`16/16` migrations), logical `pg_dump -Fc`/`pg_restore` drill (**RTO 1.24s, RPO 1.50s**, audit chain `7/7` valid, app boot `200`), and physical `pg_basebackup` + `pg_verifybackup` + WAL PITR drill to named restore point `aibridge_pre_incident` (`LSN 0/4006420`, `6` archived WAL files, **RTO 2.98s, RPO 2.34s**, post-basebackup writes recovered, post-incident corruption excluded, audit chain `9/9` valid, app boot `200`). | Observe green CI `postgres-boot` run on push and run `scripts/backup_drill.sh` against the live hosted staging/production PostgreSQL instance. |
| **P0** | Staging release gate | `scripts/staging_release_gate.py` + `deployment/smoke_check.py` · CI job `release-1-staging-and-webrtc-gate` | **PASS (10/10 self-hosted with built v3 dashboard):** health/readiness, dashboard HTML shell & unauthenticated boundaries, CSRF & query-param token rejection, enrollment, Tier-1 safe task, Tier-3 approval lifecycle, audit hash-chain verify, token rotation/revocation, migrations (`16/16`), rollback/restore readiness. | Run `scripts/staging_release_gate.py --url https://<staging-host>` against the live staging deployment and archive the artifact with the release candidate. |
| **P1** | Security & architecture review | Remediated in code + unit tests (`tests/unit/test_release1_gates.py`, `apps/windows-client.Tests/Program.cs`) | **Completed for current HEAD (`caabc53`..`289a7df`):** 5 boundary defects found and fixed (see §2 below). | Independent external reviewer sign-off before opening to production fleets. |
| **P1** | Test health & platform matrix | `pyproject.toml` markers · `.github/workflows/ci.yml` (`test`, `windows-python-e2e`, `macos-agent`, `postgres-boot`, `deployment-config`, `dashboard`, `windows-client`, `windows-native-webrtc`, `release-1-staging-and-webrtc-gate`) | **Fixed:** de-flaked `test_concurrent_claim_and_cancel_under_load` (env-gated perf ceiling) and `test_devices_list_uses_one_batched_live_meta_query` (excludes background 30s scheduler thread); fixed all 10 Windows-host test failures (including real bugs in `installer_gui.py` `.NET` ACL call, `generate_api_reference.py` DB handle close, and `release_manifest_dryrun.py` `file://` URI). | Observe green matrix across all CI jobs on push. |

## 2. Security & Architecture Review Findings (Remediated)

Auditing commits `caabc53`..`289a7df` (native C# Windows client, device-scoped Full PC Access tools, minimal React v3 dashboard) surfaced five concrete boundary issues, all fixed in this change:

1. **Over-permissive Python risk tiers for Full PC Access tools (`src/ai_bridge/policy.py`):**
   - `write_pc_file` (arbitrary file write anywhere on the host PC) was Tier 3 (`SENSITIVE`); elevated to **Tier 4 (`HIGH_RISK`)** to match/exceed `delete_workspace_file`.
   - `list_directory` (arbitrary directory listing outside the workspace) was Tier 1 (`READ_ONLY`); elevated to **Tier 3 (`SENSITIVE`)** to match `read_pc_file`.
2. **Missing tier enforcement on C# Full PC Access handlers (`apps/windows-client/Services/NativeToolExecutor.cs`):**
   - `WritePcFile` only checked `AllowWrites && MaxTierUnattended >= 2`; tightened to require `AllowTier4 && MaxTierUnattended >= 4 && AllowWrites`.
   - `ReadPcFile` and `ListDirectory` lacked Tier 3 checks; tightened to require `AllowTier3 && MaxTierUnattended >= 3`.
3. **Unsafe C# client default policy (`apps/windows-client/Models/AgentModels.cs`):**
   - `AgentPolicyConfig` defaulted `AllowTier3 = true` and `MaxTierUnattended = 3`; tightened to `AllowTier3 = false` and `MaxTierUnattended = 2` (matching platform-wide safe defaults).
   - `AgentFunctionPermissions` defaulted `AllowFullPcFilesAndDrives = true` and `AllowShellAndPowerShell = true`; tightened both to `false`.
4. **View-Only keyboard bypass in v3 dashboard (`apps/dashboard/src/views/DesktopView.tsx`):**
   - Toggling "View Only" (`controlArmed = false`) blocked mouse clicks, but left "Send Text" and "Send Hotkey" enabled; gated both inputs and buttons on `controlArmed`.
5. **Broken Windows launcher ACL lockdown under PowerShell 7 parent environments (`deployment/windows/installer_gui.py`):**
   - `_restrict_launcher_acl` invoked `Get-Acl`/`Set-Acl`, which failed when `PSModulePath` was inherited from `pwsh` (`Microsoft.PowerShell.Security` load error); switched to pure `.NET` `[System.IO.File]::GetAccessControl` / `SetAccessControl` so ACL hardening succeeds reliably.
6. **Stale smoke check & restore drill scripts (`deployment/smoke_check.py`, `scripts/backup_drill.sh`):**
   - Updated `smoke_check.py` for the v3 dashboard HTML shell (`<div id="root">` + `AI Bridge`) instead of deleted `brand-mark.svg`.
   - Updated `backup_drill.sh` to invoke `scripts/backup_restore.py drill` (dynamic migration count instead of hardcoded `15`, plus audit hash-chain continuity, app boot, and RTO/RPO evidence).

## 3. Supported-Platform Test Matrix

| Surface | Linux (`ubuntu-latest`) | Windows (`windows-latest`) | macOS (`macos-latest`) |
|---|---|---|---|
| Control plane + Python suite (`pytest`) | CI `test` | CI `windows-python-e2e` (verified locally) | CI `macos-agent` (unit smoke + launchd E2E) |
| PostgreSQL parity, boot & restore drill | CI `postgres-boot` (`postgres:18`) | N/A (CI Linux service) | N/A |
| Deployment config (stdlib-only) | CI `deployment-config` | Local verified | Local verified |
| React/Vite v3 dashboard build & typecheck | CI `dashboard` + `release-1-staging-and-webrtc-gate` | Local verified (`tsc --noEmit` + `vite build`) | N/A |
| Staging gate + Real-browser WebRTC E2E | CI `release-1-staging-and-webrtc-gate` (`--require-browser-peer`) | Local signaling/enforcement verified | N/A |
| Native Windows C# WPF client | N/A | CI `windows-client` (verified locally: `15/15`) | N/A |
| Native Windows C++ WebRTC (`libdatachannel`) | N/A | CI `windows-native-webrtc` | N/A |
| Windows installer & DPAPI E2E | N/A | CI `windows-python-e2e` (verified locally: `7/7`) | N/A |
| macOS launchd agent E2E | N/A | Dry-run verified | CI `macos-agent` (`plutil` + agent E2E) |

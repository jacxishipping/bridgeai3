# Changelog

## Unreleased — 2026-11-23
- Documentation: replaced stale `analysis/FINAL_PRODUCTION_READINESS.md` with
  [analysis/PRODUCTION_READINESS.md](analysis/PRODUCTION_READINESS.md) (lab GO;
  controlled fleet CONDITIONAL GO; SaaS NO-GO). Rewrote
  `docs/implementation-status.md`, `docs/production-readiness.md`,
  `docs/operations.md`, `docs/mcp-clients.md`, `docs/remote-access.md`; updated
  README, support matrix, release-1 verification pointers, and SECURITY.md.
- ChatGPT Custom GPT Actions pack under `integrations/chatgpt/actions/`;
  Bearer accepted on desktop frame/input, fleet frames, and approvals.
- Windows WPF client UX polish (poller vs Agents, takeover, visuals).
- Native WebRTC preference on Windows fleet dispatch (`ai_bridge_desktop`).

## 0.3.1 — 2026-11-17 (Wave 1 remediation; see analysis/20_REFACTORING_ROADMAP.md)
- P0 fixes: env-var auth/DNS bypasses removed; one MCP policy gate on all
  tools; `unrestricted`/Tier-4 approval fixed; MCP audit logging; agent
  policy merge is intersection-only; Linux agent defaults restrictive;
  WebRTC view-only default-deny; fake H.264 IDR fallback removed;
  Tier 3 off by default; fail-fast required secrets; nginx HTTPS redirect,
  protected `/metrics`, OAuth `auth_request`; `curl -k` removed from
  installers; CI runs pytest and publishes the run log.
- Python tests: 481 `def test_` functions counted by
  `grep -rn 'def test_' tests/ | wc -l` (403 at the 2026-11-17 audit plus
  Wave 1 regression tests). Earlier entries below cite the counts recorded
  at the time they were written (29/33); those were the counts of the
  subset run then, not of the suite as it stands today — see
  analysis/15_TESTING_AND_RELIABILITY_REPORT.md and DOC-01/02/03.
## 0.3.0 — 2026-10-24 (security batch)
- Short-lived device tokens (7-day default) with expiry checks and rotation.
- Local users (PBKDF2), 8-hour sessions, viewer/operator/admin roles; legacy admin token retained unless disabled.
- Versioned, audited policies; Tier-3 tasks gated by the current policy.
- Ed25519 signed release verification + agent --check-update (no silent auto-install).
- Sandboxed task execution: minimal env, workspace cwd, CPU/memory/file-size limits.
- Dashboard username/password login and Policies view.
- Tests: 33 passed (4 new security-batch tests — the count of the subset run for this release, not the full suite), ruff clean, dashboard build succeeded, live login/role/rotation checks verified.
- Still not implemented: network-namespace egress control (see below),
  Windows-on-Windows execution. Status of the items previously listed
  here, corrected 2026-11-17 against the code (one statement per item,
  see docs/implementation-status.md):
  - OIDC login: code present and env-activated (`control/oidc.py` +
    `/api/auth/oidc/*` when `AI_BRIDGE_OIDC_*` is set); full login flow
    unverified until an end-to-end test against a registered provider passes.
  - Plane-side device mTLS: code present and env-gated
    (`AI_BRIDGE_DEVICE_MTLS=1`, fingerprint gate in
    `control/device_mtls.py`, fingerprint taken from reverse-proxy
    headers); end-to-end verification pending.
  - macOS agent: code exists (`agents/macos/agent_main.py`, launchd unit
    in `deployment/macos/`). **Later (Release 1):** CI `macos-agent` launchd
    E2E green; support tier remains **Preview** — see docs/support-matrix.md.
## 0.2.0 — 2026-10-24
- Hardened starter core: secret-name blocking, redaction, output caps,
  resolved-root confinement, atomic writes, backups.
- Full MCP tool set (workspace writes, diagnostics, git, tests, linter,
  backup, staging deploy, tasks, approvals, desktop opt-in, devices).
- Streamable HTTP MCP entrypoint alongside stdio.
- Control plane: enrollment/revocation, task queue, agent polling with
  bounded backoff, approvals, audit, tenant filtering, emergency stop,
  rate limiting.
- Linux agent (systemd) + Windows agent + interactive desktop helper.
- React/TypeScript dashboard source.
- Tests: unit, integration, security, e2e, MCP stdio (see test report
  in the release notes / final delivery message).
- Test report for this build (2026-10-24, Linux, Python 3.12):
  `pytest -q` = 29 passed (the count of the subset run for this release,
  not the full suite — see the 0.3.1 note above); `ruff check` = clean; dashboard
  `npm run build` = succeeded (31 modules, dist/index.html + assets);
  MCP stdio initialize + tools/list + tools/call verified by test;
  Streamable HTTP initialize verified live at /mcp; control plane +
  Linux agent `--once` round-trip verified live (enroll -> task ->
  agent execution -> completed result retrievable).
  No run log for this build is stored in the repo, so the 29-pass figure
  cannot be re-verified from the tree.
- Docs: architecture, threat model, security, install (Linux/Windows),
  MCP clients, remote access, operations, troubleshooting, API reference,
  honest implementation status, roadmap.
## 0.1.0 — starter (supplied ZIP)
- Stdio MCP server, 4 read-only tools, fixed diagnostics, path tests.

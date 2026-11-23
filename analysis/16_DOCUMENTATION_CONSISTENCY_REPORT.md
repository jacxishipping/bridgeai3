> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 16 — Documentation Consistency Report (AI Bridge v0.3.0)

**Scope:** Every top-level and `docs/` document compared against the code in
`/home/hatch/workspace/ai-bridge-main`. Claims are labelled VERIFIED (matches code),
CONTRADICTED (code says otherwise), STALE (true for an older version only), or
UNVERIFIABLE HERE (no evidence in the tree).
**Documents read in full or in the sections cited:** `README.md`, `PROJECT.md`,
`CHANGELOG.md`, `SECURITY.md`, `TEST_READY.md`, `TEST_INFRA.md`,
`docs/architecture.md`, `docs/security.md`, `docs/threat-model.md`,
`docs/implementation-status.md`, `docs/production-readiness.md`, `docs/api-reference.md`,
`docs/mcp-clients.md`, `docs/operations.md`, `docs/remote-access.md`, `docs/streaming.md`,
`docs/troubleshooting.md`, `docs/windows-agent.md`, `docs/render.md`,
`docs/installation-linux.md`, `docs/installation-windows.md`, `docs/localization.md`,
`docs/ROADMAP.md`, `docs/ai-bridge_ADVANCED_ROADMAP.md`, `docs/MCP_CHATBOT_SETUP.md`,
`apps/dashboard/README.md`, `apps/dashboard/DASHBOARD_DESIGN_SPEC.md`,
`deployment/aws/README.md`, `deployment/windows/README.md`, `deployment/macos/README.md`,
`deployment/oauth/README.md`, `CONTRIBUTING.md`, `.env.example`, `render.yaml`,
`docker-compose*.yml`, `.github/workflows/ci.yml`.

**Overall verdict:** the *honest* documents (`docs/implementation-status.md`,
`docs/production-readiness.md`, `docs/ROADMAP.md`, `apps/dashboard/README.md`) are largely
accurate and explicitly mark residuals. The *marketing-facing* documents (`README.md`,
`TEST_READY.md`, `PROJECT.md` milestones, `SECURITY.md`) overstate or contradict the code in
at least 15 material places. The most consequential: test counts (three different numbers),
"Fluent 2" vs the purged-Fluent-2 rebuild, OIDC/mTLS status contradictions between v0.2-era
and v0.3-era text, and a missing baseline document (`ORIGINAL_REQUEST.md`).

---

## 1. Discrepancy register

### 1.1 Test-count contradictions (highest impact — see also report 15)

| # | Claim | Source | Verified reality |
|---|---|---|---|
| DOC-01 | "Execute full test suite (75 tests)" | `README.md` § Verification & Testing | ❌ CONTRADICTED. 403 Python `def test_` functions counted by grep. No "75" anywhere else in the tree. |
| DOC-02 | "`pytest -q` = 29 passed" (v0.2.0) | `CHANGELOG.md` | ⚠️ UNVERIFIABLE HERE, and inconsistent with the current 403-test suite. No run log in the repo. Either a subset was run or the suite has grown ~14× since — neither is stated. |
| DOC-03 | "Tests: 33 passed (4 new security-batch tests)" (v0.3.0) | `CHANGELOG.md` | ⚠️ Same caveat as DOC-02. 33 ≠ 403. |
| DOC-04 | "Total Automated Test Cases: 356; 100% (356/356 passed); 0.10 seconds" | `TEST_READY.md` §§1, 3 | ⚠️ Arithmetic VERIFIED (356 `it(` calls), execution NOT re-verified here, and 310/356 assert on inline literals without importing app code (report 15, TST-04). The 0.10 s runtime corroborates that the tests do no real I/O. |
| DOC-05 | `npm test` = 356 tests | `TEST_READY.md` | ⚠️ Incomplete: `package.json` runs `run-all-tests.ts && npm run test:i18n`, and `test:i18n` adds 3 `node:test` cases (`catalogs.test.ts`) — the true `npm test` total is 359. |
| DOC-06 | "M6: Pass 100% E2E tests (Tiers 1-4) & adversarial hardening (Tier 5) — DONE" | `PROJECT.md` Milestones | ❌ CONTRADICTED. Tier 5 (`tier5-adversarial.test.ts`, 31 cases) and the three challenger suites are not imported by `run-all-tests.ts` and are therefore not part of the "100%" run. "E2E" here means the custom tsx harness, not browser E2E. |
| DOC-07 | "Tests do not write facades that artificially pass" / "No Facades" | `TEST_INFRA.md` §§1.1, 6 | ❌ CONTRADICTED in substance. Tier 1/2 tests define the expected value inside the test itself (e.g. `const token = 'ai_bridge_sec_adm_99182a4'` then asserting it starts with the right prefix). That is a facade by construction, even if no production code was modified. |

### 1.2 Dashboard identity contradictions

| # | Claim | Source | Verified reality |
|---|---|---|---|
| DOC-08 | "Fluent 2 Web Dashboard: Modern React 18 + TypeScript + Vite…" | `README.md` Features | ❌ STALE/CONTRADICTED. PROJECT.md and TEST_INFRA.md §5.1 state `fluent2.css` and `jxui.css` were **purged** and the design system is exclusively `@jacxishippingorg/jx-ui-react` + `motion` + `lucide-react`. The R1 test suite asserts the Fluent files do not exist. README was not updated. |
| DOC-09 | "Zero-Custom-UI Guarantee: absolutely no custom CSS files…, no CSS modules, SCSS, or Tailwind wrapper classes" | `PROJECT.md` Architecture | ⚠️ CONTRADICTED in the letter: `apps/dashboard/src/tailwind.css` (281 lines, Tailwind v4 import) exists and is listed in PROJECT.md's own Code Layout. Whether that counts as "custom CSS" is definitional, but the file's existence contradicts "no custom CSS files" as written. |
| DOC-10 | "All 15 operational screens … mapped" / "Complete Dashboard Reconstruction" | `TEST_READY.md` §5.3, PROJECT.md | ✅ VERIFIED structurally (15 `ViewId`s in `src/types/dashboard.ts` + `src/views/`), but the screens are the **seeded demo** — `apps/dashboard/README.md` states all 14 operational/governance screens "use seeded browser-local data, not live fleet telemetry". TEST_READY never mentions the demo/live split. |
| DOC-11 | "Centralized state store provides realistic multi-platform device models (40 nodes)…" presented as R5 functional verification | `TEST_READY.md` §5.4 | ⚠️ Misleading by omission: the 40 nodes are `SEED_DEVICES` generated in-browser (`dashboardStore.ts:68-69`); R5's tests never import the store — they check the file exists via `fs`. |

### 1.3 Security / platform status contradictions

| # | Claim | Source | Verified reality |
|---|---|---|---|
| DOC-12 | "Supported version: v0.2.x (this release)" | `SECURITY.md` | ❌ STALE. The release is v0.3.0 (`pyproject.toml`, `CHANGELOG.md`, `docs/implementation-status.md`). SECURITY.md was not bumped. |
| DOC-13 | "The plane itself does not yet verify client certificates" (mTLS) | `docs/remote-access.md` | ⚠️ CONTRADICTED by v0.3-era text: `docs/implementation-status.md` says plane-side mTLS + device fingerprint gate are in-tree (`control/device_mtls.py`, `AI_BRIDGE_DEVICE_MTLS`); CHANGELOG 0.3.0 lists "plane-side mTLS" under "Still not implemented", while `implementation-status.md` lists it under "Implemented since v0.3 (needs activation…)". Three documents, three statuses — the code file exists; end-to-end verification status is genuinely unclear. |
| DOC-14 | "OIDC login is in-tree and env-activated" | `docs/implementation-status.md`, `docs/threat-model.md` | ⚠️ Partially VERIFIED (code `control/oidc.py` + `/api/auth/oidc/*` routes exist) but "full flow needs a registered provider app" — i.e. not verified end-to-end. CHANGELOG 0.3.0 lists OIDC under "Still not implemented" and `docs/ROADMAP.md` marks the v0.3 item `[~]` (partial). Consistent only if read as "code present, flow unverified". |
| DOC-15 | "macOS agent" — CHANGELOG says "Still not implemented"; implementation-status says "macOS agent code + launchd unit (import + honesty exercised on `macos-agent` CI)" | `CHANGELOG.md` vs `docs/implementation-status.md` | ⚠️ Both true at different levels: code exists (`agents/macos/agent_main.py`, 94 lines, docstring "NOT yet run on macOS"); it has only ever been import-tested. A reader of CHANGELOG alone would think no macOS code exists; a reader of implementation-status alone would think it works. |
| DOC-16 | "Signed agent updates (documented design in docs/security.md)" listed under "Out of scope for this deepen" | `docs/threat-model.md` | ❌ STALE. Signed updates are implemented in v0.3 (`src/ai_bridge/updates.py`, Ed25519; `docs/security.md` §2b documents verify + optional apply). The threat-model paragraph is v0.2-era text. |
| DOC-17 | "Resource (CPU/RAM) cgroups are a v0.3 hardening item" | `docs/threat-model.md` | ⚠️ UNVERIFIABLE from the docs read: CHANGELOG 0.3.0 says sandboxed execution has "CPU/memory/file-size rlimits" — rlimits are not cgroups. Whether cgroup enforcement landed is a code question for the security track. |
| DOC-18 | "Add signed config + admin audit alerts in v0.3" (residual) | `docs/threat-model.md` | ⚠️ UNVERIFIABLE HERE — no evidence found in the docs read that this landed; needs a code search for signed config. |

### 1.4 Missing / dangling references

| # | Claim | Source | Verified reality |
|---|---|---|---|
| DOC-19 | `ORIGINAL_REQUEST.md` cited as the authoritative baseline for requirements R1–R5 | `TEST_INFRA.md` §§1.2, 2; `TEST_READY.md` §1 | ❌ NOT FOUND. No such file exists in the tree. The R1–R5 requirements therefore cannot be checked against their source; TEST_INFRA/TEST_READY are self-referential on this point. |
| DOC-20 | "Built output IS included in the release ZIP (`apps/dashboard/dist/`)" | `docs/implementation-status.md` | ❌ NOT FOUND in this tree (`ls apps/dashboard/dist` fails). Either the ZIP differs from this tree or the claim is wrong. `tsconfig.tsbuildinfo` exists, indicating a build ran at some point, but no `dist/` is present. |
| DOC-21 | "see test report in the release notes / final delivery message" | `CHANGELOG.md` v0.2.0 | ⚠️ UNVERIFIABLE — no release notes or delivery message file exists in the tree. |
| DOC-22 | API reference labelled "(control plane, v0.2)" | `docs/api-reference.md` | ⚠️ STALE version label; content is a 15-line endpoint list. It does not document `/api/v1/*` sessions, OIDC, policies, users, backups, or WebRTC routes that exist in `control/app.py` / `v1_routes.py`. |
| DOC-23 | `docs/operations.md`: "the SQLite/Postgres schema is additive in v0.2" (rollback = checkout previous tag) | `docs/operations.md` | ⚠️ STALE version reference; no evidence in the tree that additivity was re-verified for the 12-migration v0.3 schema (migrations 002–012 postdate v0.2). |

### 1.5 Commands and configuration

| # | Claim | Source | Verified reality |
|---|---|---|---|
| DOC-24 | `pip install -e ".[dev]"` then `pytest -v` | `README.md`, `ci.yml` | ✅ VERIFIED as the documented path; `pyproject.toml` defines the `dev` extra (`pytest>=8.0`, `ruff>=0.4`, `Pillow>=10.0`) and pytest config. **Not executable in this environment** (no pytest installed; see report 15). |
| DOC-25 | `npm --prefix apps/dashboard test` | `TEST_READY.md` | ✅ Command matches `package.json` (`"test": "tsx src/tests/run-all-tests.ts && npm run test:i18n"`). Requires `npm ci` first — `node_modules/` absent here. |
| DOC-26 | `python -m ai_bridge.server --http --port 8001` (binds 127.0.0.1 by default) | `docs/mcp-clients.md` | ✅ Consistent with `server.py` (port-8001 references) and `docker-compose.prod.yml:54`. INFERRED default-bind — not executed here. |
| DOC-27 | Magic install enrolls with the control plane at `https://jxiptvplayer.app` | `README.md` | ✅ VERIFIED present in README, `magic-install.sh`, and `docker-compose.prod.yml` (`AI_BRIDGE_MCP_ALLOWED_HOSTS` default). This is a hard-coded third-party host in install paths — flagged, not a doc error per se. |
| DOC-28 | Python "3.12–3.14" badge | `README.md` | ⚠️ `.python-version` pins `3.12`; `magic-install.ps1` "refuses anything outside 3.12–3.14" per README. Badge is plausible but the only pinned version in-tree is 3.12. |
| DOC-29 | "No `run_any_command`" / "no arbitrary PowerShell executor" | `README.md`, `docs/architecture.md` | ✅ VERIFIED in policy code (`decide(Policy(), "run_any_command").allowed` is False — manual check, report 15). |
| DOC-30 | Dashboard "sign in using your `AI_BRIDGE_ADMIN_TOKEN` or configured local credentials" at `http://localhost:5173` | `README.md` | ✅ Consistent with `apps/dashboard/src/api.ts` (login + session-token auth) and `apps/dashboard/README.md` (credentials in session storage). Note: `docs/threat-model.md` says the dashboard "stores it only in browser localStorage" — CONTRADICTED by `apps/dashboard/README.md` ("kept in session storage for the current tab, not persisted in local storage"). One of them is stale. |

### 1.6 Small / thin docs (VERIFIED)

- `docs/api-reference.md` (15 lines), `docs/operations.md` (14), `docs/mcp-clients.md` (19),
  `docs/ROADMAP.md` (11), `docs/remote-access.md` (24), `docs/threat-model.md` (24),
  `docs/troubleshooting.md` (26) are stubs or near-stubs relative to the system they describe.
  The substantive technical docs are `docs/security.md` (82), `docs/streaming.md` (58),
  `docs/architecture.md` (62), `docs/implementation-status.md` (87), and
  `docs/production-readiness.md` (110) — the last two are the most reliable documents in the repo.
- `docs/ai-bridge_ADVANCED_ROADMAP.md` (374 lines) and `docs/MCP_CHATBOT_SETUP.md` (380 lines)
  are the longest docs; they were skimmed, not fully verified, in this track.

---

## 2. Implementation-status matrix

"Claimed" = the most favourable doc statement; "Verified" = this track's finding.
Evidence paths are relative to the repo root.

| Feature | Claimed status | Verified status | Evidence |
|---|---|---|---|
| MCP stdio server | Complete (v0.1–0.2) | ✅ VERIFIED present (109 tools) | `src/ai_bridge/server.py`, `src/ai_bridge/tools.py` |
| MCP Streamable HTTP | Complete (v0.2) | ✅ VERIFIED present (code + routes) | `server.py`, `control/mcp_routes.py`, `docs/mcp-clients.md` |
| Control plane (enroll/revoke/tasks/approvals/audit) | Complete (v0.2) | ✅ VERIFIED present | `control/app.py` (143 routes), `migrations/001–012` |
| 4-tier policy engine | Complete | ✅ VERIFIED present + spot-checked 13/13 | `policy.py`, report 15 §4.3 |
| Workspace confinement | Complete | ✅ VERIFIED present + spot-checked | `security.py`, `workspace.py` |
| Linux agent | Complete | ✅ VERIFIED present (1,055 lines) | `agents/linux/agent_main.py`, `agents/common/poller.py` |
| Windows Python agent + GUI | Complete | ✅ VERIFIED present; execution on Windows NOT verified | `agents/windows/`, CHANGELOG 0.3.0 |
| Windows native C++ agent | Complete | ✅ VERIFIED present; CI-built on Windows (not observed here) | `windows-agent/`, `.github/workflows/ci.yml` |
| macOS agent | "Still not implemented" (CHANGELOG) / "code + launchd" (impl-status) | ⚠️ PARTIAL — code present, never run on macOS | `agents/macos/agent_main.py` ("NOT yet run on macOS") |
| Dashboard (live workspace) | Complete | ✅ VERIFIED present (real API client) | `apps/dashboard/src/api.ts`, `src/views/live/*` |
| Dashboard (15 demo screens) | Complete, "100% E2E" | ⚠️ PARTIAL — screens exist with seeded data; Tier 1/2 tests are self-referential; Tier 5 not in runner | `dashboardStore.ts`, report 15 §3 |
| Human approvals | Complete | ✅ VERIFIED present (routes + views) | `control/app.py`, `views/HumanApprovalsView.tsx` |
| Audit (append-only, hash-chained) | Complete (v0.3) | ✅ VERIFIED present in docs/code structure | `control/dispatch_audit.py`, `docs/security.md` §5 |
| Short-lived device tokens + rotation | Complete (v0.3) | ✅ VERIFIED as documented; code location not fully traced in this track | CHANGELOG 0.3.0, `docs/implementation-status.md` |
| Local users / PBKDF2 / roles / sessions | Complete (v0.3) | ✅ VERIFIED present (code exists) | `control/auth.py`, `role_catalog.py` |
| Versioned policies + simulation | Complete (v0.3) | ✅ VERIFIED as documented | `docs/implementation-status.md` |
| Ed25519 signed updates | Complete (v0.3) | ✅ VERIFIED present | `src/ai_bridge/updates.py` |
| OIDC login | "Still not implemented" (CHANGELOG) / "in-tree, env-activated" (impl-status) | ⚠️ PARTIAL — code present, flow unverified, needs provider app | `control/oidc.py`, `docs/implementation-status.md` |
| Plane-side device mTLS | "Still not implemented" (CHANGELOG) / "in-tree" (impl-status) / "does not yet verify" (remote-access) | ⚠️ CONTRADICTORY DOCS — code file exists, verification status unclear | `control/device_mtls.py`, DOC-13 |
| Network-namespace egress | "Implemented since v0.3 (needs activation)" | ⚠️ PARTIAL — env-gated, unverified here | `docs/implementation-status.md` |
| Redis durable rate limiting | Complete (prod) | ✅ VERIFIED present; opt-in | `control/durable_ratelimit.py` |
| Postgres production DB | Required in production | ✅ VERIFIED as documented (gated) | `docs/implementation-status.md`, `render.yaml` |
| WebRTC desktop streaming (Python aiortc) | Complete | ✅ VERIFIED present | `webrtc_stream.py`, `docs/streaming.md` |
| WebRTC native C++ PeerConnection | "Verified by harness" (CI) | ⚠️ PARTIAL — opt-in, not the default path | `docs/implementation-status.md`, `scripts/libwebrtc_e2e_harness.py` |
| Desktop audio | Off by default; WASAPI pending | ⚠️ PARTIAL — silent-PCM fallback only | `desktop_audio.py`, `docs/streaming.md` §2b |
| Browser automation / Browserbase / Firecrawl | Complete (Phase 4) | ✅ VERIFIED present | `browser_tools.py`, `connectors/` |
| ChatGPT integration | Supported via OAuth 2.1 HTTP | ⚠️ CONFIG ONLY (README + JSON) | `integrations/chatgpt/README.md` |
| Claude / Codex / Gemini integrations | Supported (stdio) | ⚠️ CONFIG ONLY (README + JSON) | `integrations/claude|codex|gemini/` |
| Copilot 365 integration | Supported (remote HTTP + OAuth) | ⚠️ README ONLY, no code | `integrations/copilot365/README.md` |
| Custom-AI agents (OpenAI/Anthropic/LangChain/Ollama) | Supported | ✅ VERIFIED present (480 lines) | `integrations/custom-ai/*.py` |
| Multi-worker control plane | Not claimed | ❌ NOT SUPPORTED — in-memory emergency-stop/OIDC state; "use one worker" | `docs/production-readiness.md` |
| Windows full installer/service E2E | Documented | ❌ NOT VERIFIED on a dedicated host | `docs/implementation-status.md`, `docs/ROADMAP.md` |
| `apps/dashboard/dist/` in release ZIP | Claimed present | ❌ NOT FOUND in this tree | DOC-20 |
| `ORIGINAL_REQUEST.md` baseline (R1–R5) | Cited as authoritative | ❌ NOT FOUND | DOC-19 |
| Signed config + admin audit alerts | "Add in v0.3" (threat-model residual) | ❓ UNVERIFIABLE HERE — no evidence found | DOC-18 |
| Cgroup resource enforcement | "v0.3 hardening item" | ❓ UNVERIFIABLE HERE — rlimits documented, cgroups not confirmed | DOC-17 |

---

## 3. What the docs get right (worth preserving)

- `docs/implementation-status.md` is explicitly labelled an "honest report" and lives up to it:
  it lists what is implemented, what needs activation, and what is "still NOT verified end-to-end".
- `docs/production-readiness.md` states its limits plainly (single worker, no million-device
  claims, "not a security certification", logs are host logs, connection check is a TCP check).
- `apps/dashboard/README.md` clearly separates the live workspace from the seeded demo and
  states demo actions never call live mutation APIs — the clearest live-vs-demo statement
  anywhere in the repo.
- `docs/ROADMAP.md` marks Milestone 4 and the v0.3 items `[~]` (partial) rather than `[x]`.
- `docs/threat-model.md` names residuals per threat instead of claiming elimination.

## 4. Recommended doc fixes (prioritised)

| # | Fix | Priority |
|---|---|---|
| DOC-F1 | README: replace "(75 tests)" with the real count or remove the number; re-run and cite the actual `pytest -q` result for the release | P1 |
| DOC-F2 | README: replace "Fluent 2 Web Dashboard" with the JX-UI design system actually used | P1 |
| DOC-F3 | SECURITY.md: bump supported version v0.2.x → v0.3.x | P1 |
| DOC-F4 | TEST_READY.md: disclose that Tier 1/2 tests assert on inline literals and do not import app code; include Tier 5/challenger in the runner or stop claiming M6 Tier-5 completion | P1 |
| DOC-F5 | Reconcile OIDC/mTLS/macOS status across CHANGELOG, implementation-status, remote-access, threat-model into one status table (use the matrix in §2 as the base) | P1 |
| DOC-F6 | Restore or remove the `ORIGINAL_REQUEST.md` citation; R1–R5 currently have no checkable source | P1 |
| DOC-F7 | Fix `docs/implementation-status.md` `dist/` claim or include the build output in the tree/ZIP | P2 |
| DOC-F8 | Update `docs/api-reference.md` to v0.3 (add `/api/v1/*`, OIDC, policies, users, backups) or mark it as a v0.2 snapshot | P2 |
| DOC-F9 | Fix the localStorage/session-storage contradiction on admin-token storage (threat-model vs dashboard README) | P2 |
| DOC-F10 | Update `docs/threat-model.md` v0.2-era residuals (signed updates now implemented; cgroup status; signed config status) | P2 |
| DOC-F11 | PROJECT.md: qualify "no custom CSS" (tailwind.css exists) or remove the file from the claim | P3 |
| DOC-F12 | `docs/operations.md`: re-verify the "schema is additive" rollback claim against migrations 002–012 | P3 |

## 5. Findings

| # | Finding | Evidence |
|---|---|---|
| DOC-01–03 | Three irreconcilable Python test counts: README 75, CHANGELOG 29/33, actual 403 | Report 15 |
| DOC-04–07 | Dashboard 356/100% claim is arithmetically right but evidentially weak; Tier 5 excluded; "no facades" contradicted by self-referential tests | TEST_READY.md, report 15 §3 |
| DOC-08/09 | README still says "Fluent 2"; PROJECT.md claims zero custom CSS while `tailwind.css` exists | README.md, PROJECT.md |
| DOC-12 | SECURITY.md supports v0.2.x; release is v0.3.0 | SECURITY.md, pyproject.toml |
| DOC-13 | mTLS status is stated three different ways across three docs | DOC-13 table |
| DOC-14/15 | OIDC and macOS agent each have two different statuses in two docs | CHANGELOG vs implementation-status |
| DOC-16 | Threat model lists signed updates as out-of-scope although v0.3 implemented them | docs/threat-model.md vs updates.py |
| DOC-19 | `ORIGINAL_REQUEST.md` missing; R1–R5 baseline unverifiable | find/ls |
| DOC-20 | `apps/dashboard/dist/` claimed in release ZIP; absent from tree | ls |
| DOC-22/23 | API reference is a v0.2 stub; operations rollback claim is v0.2-era | docs/api-reference.md, docs/operations.md |
| DOC-30 | Admin-token storage: localStorage (threat-model) vs session storage (dashboard README) | docs/threat-model.md, apps/dashboard/README.md |
| DOC-31 | The honest docs (implementation-status, production-readiness, dashboard README, ROADMAP) are accurate; the overstatements concentrate in README/TEST_READY/PROJECT milestones | §3 |

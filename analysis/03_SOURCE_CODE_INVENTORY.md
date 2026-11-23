> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 03 — Source Code Inventory (AI Bridge v0.3.0)

**Scope:** Complete file and line inventory of the repository at `/home/hatch/workspace/ai-bridge-main`
(extracted ZIP `ai-bridge-main_0_ppme.zip`; **no `.git` directory present** — no commit history,
no tags, no `git log` available).
**Method:** `find` + `wc -l` counts run 2026-11-17. All counts below are VERIFIED by those commands.
Line counts are raw `wc -l` (blank lines and comments included), not logical SLOC.
**Status labels:** VERIFIED = observed in a file or command output; INFERRED = reasonable reading of
code/docs, labelled as such; NOT FOUND = searched for and absent.

---

## 1. Repository totals

| Metric | VERIFIED value |
|---|---|
| Total files (excluding `.git`, which does not exist) | **421** |
| Total Python files | **161** |
| Total Python lines | **49,292** |
| Total TS + TSX lines | **29,723** (TS 8,866 / TSX 20,857; 74 files) |
| Total C++ (`.cpp`) lines | **3,172** (13 files) |
| Total C++ headers (`.hpp`/`.h`) lines | **646** (10 files) |
| Total Markdown lines | **15,448** (57 files) |
| Total JSON lines | **6,043** (12 files, dominated by `apps/dashboard/package-lock.json`) |
| Total HTML lines | **8,208** (14 files — mostly dashboard `stitch_design_system_implementation/` reference mockups) |
| Total Shell (`.sh`) lines | **2,775** (8 files) |
| Total PowerShell (`.ps1`) lines | **1,197** (7 files) |
| Total SQL lines | **278** (12 migration files) |
| Total YAML (`.yml`/`.yaml`) lines | **946** (7 files) |
| Repository size on disk | **14 MB** (`du -sh`) |
| Python version pinned | **3.12** (`.python-version` = `3.12`; README badge claims 3.12–3.14) |

### 1.1 Files and code lines by top-level area

| Area | Files | Code+doc lines in area* |
|---|---|---|
| `src/ai_bridge/` (+ `src/ai_bridge.egg-info/`) | 75 | 29,112 |
| `apps/` (dashboard + control-api) | 112 | 31,108 |
| `tests/` | 58 | 9,197 |
| `agents/` | 17 | 6,559 |
| `deployment/` | 31 | 5,810 |
| `windows-agent/` | 25 | 3,433 |
| `docs/` | 19 | 1,754 |
| `scripts/` | 6 | 1,080 |
| `integrations/` | 14 | 773 |
| `migrations/` | 12 | 278 |
| `shared/` | 4 | 0 (see §7 — JSON files counted as 0 by the code-line filter; they are small schema/protocol files) |
| `.agents/` (skills) | 17 | not counted in code LOC (skill docs) |
| Root files (README, PROJECT, TEST_*, deploy scripts, compose, etc.) | ~31 | — |

\* "Lines in area" = `wc -l` over `.py/.ts/.tsx/.js/.cpp/.h/.c/.sh/.ps1/.sql/.md` in that area.
Root-level files are included in the 421 total but not in any area row.

### 1.2 Lines by language (whole repo)

| Extension | Files | Lines (`wc -l`) |
|---|---|---|
| `.py` | 161 | 49,292 |
| `.tsx` | 42 | 20,857 |
| `.ts` | 32 | 8,866 |
| `.md` | 57 | 15,448 |
| `.html` | 14 | 8,208 |
| `.json` | 12 | 6,043 |
| `.cpp` | 13 | 3,172 |
| `.sh` | 8 | 2,775 |
| `.ps1` | 7 | 1,197 |
| `.yml` | 5 | 851 |
| `.hpp` | 9 | 540 |
| `.css` | 1 | 281 |
| `.sql` | 12 | 278 |
| `.yaml` | 2 | 95 |
| `.h` | 1 | 106 |
| `.js` | 1 | 75 |
| `.toml` | 1 | 52 |

---

## 2. Top-level layout (VERIFIED — `ls` + `find`)

```text
ai-bridge-main/
├── README.md  PROJECT.md  CHANGELOG.md  SECURITY.md  CONTRIBUTING.md  LICENSE
├── TEST_READY.md  TEST_INFRA.md          # dashboard test claims (see report 15/16)
├── pyproject.toml  requirements.txt  requirements-windows.txt
├── .python-version (3.12)  .env.example  .mcp.json (empty mcpServers)  stack.txt
├── magic-install.sh (23,244 B)  magic-install.ps1 (18,797 B)  Setup.bat
├── deploy_to_aws.py  deploy_to_droplet.py
├── docker-compose.yml  docker-compose.dev.yml  docker-compose.prod.yml  render.yaml
├── mcp-chatbot-clients.json
├── src/ai_bridge/        # Python core + control plane
├── agents/               # Linux / Windows / macOS poller agents + common poller
├── windows-agent/        # Native C++20 Windows service + desktop agent
├── apps/dashboard/       # React 18 + TypeScript + Vite dashboard
├── apps/control-api/     # 1-file Uvicorn entrypoint (18 lines)
├── integrations/         # Claude / Codex / Gemini / ChatGPT / Copilot365 / custom-ai / generic-mcp
├── deployment/           # aws / docker / linux / macos / oauth / systemd / windows
├── tests/                # unit / integration / security / e2e (Python)
├── docs/                 # 19 Markdown docs
├── migrations/           # 001–012 SQL
├── scripts/              # dev.sh, k6/locust soak, libwebrtc harness, setup_mcp_chatbots.py
├── shared/               # permissions matrix, protocols, session schema (JSON)
├── assets/               # (not inventoried in detail; no code)
└── .agents/skills/       # jxui-react + website-investigation-orchestrator skills
```

NOT FOUND in this tree: `.git/`, `ORIGINAL_REQUEST.md` (referenced by TEST_INFRA.md §1.2/§2 and
TEST_READY.md §1 as an authoritative baseline), `apps/dashboard/dist/` (claimed present in
`docs/implementation-status.md` — see report 16), `tests/conftest.py`, `pytest.ini`/`tox.ini`
(pytest config lives in `pyproject.toml` only), any `Dockerfile` at repo root (Dockerfiles are
under `deployment/docker/`).

---

## 3. Python core — `src/ai_bridge/` (69 files, 29,112 lines)

### 3.1 Top-level package modules

| File | Lines | Role (from file content / docstrings) |
|---|---|---|
| `tools.py` | 2,990 | Shared tool implementations; 92 top-level `def`/`async def` |
| `server.py` | 1,701 | MCP server (stdio + Streamable HTTP); **109 `@mcp.tool()` decorators (VERIFIED by grep)** |
| `webrtc_stream.py` | 1,348 | Python WebRTC (aiortc) streaming, ICE/TURN, input DataChannel |
| `browser_tools.py` | 1,075 | Playwright browser automation tools |
| `screen_capture.py` | 742 | Screen capture (native DLL / bettercam / BitBlt / PIL fallbacks) |
| `terminal_manager.py` | 637 | Terminal sessions |
| `vision.py` | 424 | Vision / screenshot analysis helpers |
| `client.py` | 335 | `AIBridgeClient` (httpx-based) |
| `device_routing.py` | 279 | MCP → control-plane device routing, keep-alive httpx client |
| `policy.py` | 266 | 4-tier risk decision engine (`Policy`, `decide`) |
| `execution.py` | 223 | Bounded task runner, process-group kill |
| `workspace.py` | 179 | Confined file ops, atomic writes, backups |
| `desktop_audio.py` | 179 | Desktop audio track (opt-in) |
| `updates.py` | 173 | Ed25519 signed-release verification |
| `diagnostics.py` | 165 | System/docker/process/log probes |
| `security.py` | 137 | Path confinement, secret-name blocking, redaction |
| `native_encode.py` | 110 | Native `ai_bridge_native.dll` H.264 timing smoke |
| `git_ops.py` | 109 | Git operations |
| `logging_config.py` | 99 | Logging setup |
| `browser_isolation_os.py` | 84 | Browser OS isolation helpers |
| `dev_tools.py` | 50 | Dev tooling |
| `config.py` | 60 | Environment-driven settings |
| `tasks.py` | 36 | Allowlisted diagnostics runner |
| `audit.py` | 23 | Audit helpers |
| `plane_control_flags.py` | 17 | Control-plane feature flags |
| `__init__.py` | 11 | **Imports `.client` → requires `httpx` at package import time (VERIFIED — see report 15)** |
| `connectors/browserbase.py` | 287 | Browserbase CDP connector |
| `connectors/microsoft365.py` | 441 | Microsoft 365 connector |
| `connectors/firecrawl.py` | 172 | Firecrawl connector |
| `connectors/you_search.py` | 93 | You.com search connector |
| `connectors/__init__.py` | 1 | Empty |

### 3.2 Control plane — `src/ai_bridge/control/` (38 files)

| File | Lines | Role |
|---|---|---|
| `app.py` | **5,147** | FastAPI app factory; **143 route decorators (VERIFIED by grep)** — the largest single file in the repo |
| `ai_api.py` | 2,284 | AI API / custom-agents endpoints; 12 route decorators |
| `custom_agents.py` | 1,655 | Custom agent definitions/execution |
| `v1_routes.py` | 1,113 | `/api/v1/*` routes (sessions, devices, policies, audit); 29 route decorators |
| `dispatch.py` | 716 | Task dispatch to devices |
| `mcp_routes.py` | 654 | MCP-over-HTTP routes; 10 route decorators |
| `supervisor.py` | 582 | Supervisor / orchestration supervision |
| `db.py` | 525 | Database layer (SQLite/PostgreSQL) |
| `mcp_oauth.py` | 370 | OAuth 2.1 / MCP OAuth |
| `role_catalog.py` | 284 | Role catalogue |
| `mcp_client.py` | 271 | MCP client for outbound calls |
| `production_readiness.py` | 250 | `/health/ready` fail-closed checks |
| `metrics.py` | 229 | Metrics |
| `agent_memory.py` | 196 | Agent memory store |
| `alerts.py` | 174 | Alerting |
| `combined.py` | 171 | Combined (embedded dashboard?) app |
| `desktop_capabilities.py` | 159 | Desktop capability probing |
| `live_activity.py` | 161 | Live activity SSE |
| `oidc.py` | 155 | OIDC login (env-gated) |
| `workspace.py` | 142 | Control-plane workspace ops |
| `migrations.py` | 139 | Migration runner (`schema_migrations` table) |
| `device_mtls.py` | 130 | Plane-side device mTLS fingerprint gate |
| `agent_channel.py` | 127 | WS side-channel (`/api/agent/ws`) |
| `durable_ratelimit.py` | 125 | Redis → settings-table → in-memory rate limiting |
| `plane_state.py` | 110 | In-process plane state (see report 15/16: emergency-stop + OIDC pending state are in-memory) |
| `ratelimit.py` | 107 | In-process rate limiting |
| `secrets_backend.py` | 94 | Secrets backends (`env` / `vault` / `aws_sm`) |
| `fleet_health.py` | 81 | Fleet health |
| `csrf.py` | 79 | CSRF protection |
| `orchestration.py` | 71 | Orchestration |
| `task_runtime.py` | 67 | Task runtime |
| `task_events.py` | 66 | Task events |
| `worm_siem.py` | 65 | WORM SIEM sink |
| `dispatch_audit.py` | 61 | Dispatch-time audit |
| `backup.py` | 48 | DB backup CLI |
| `auth.py` | 39 | Auth helpers |
| `__main__.py` | 18 | `python -m ai_bridge.control` entrypoint |
| `__init__.py` | 1 | Empty |

Entry points (VERIFIED): `ai-bridge = ai_bridge.server:main`, `ai-bridge-control =
ai_bridge.control.__main__:main` (`pyproject.toml` `[project.scripts]`); `apps/control-api/main.py`
(18 lines) is a Uvicorn entrypoint for the control plane.

**Duplication note (INFERRED, needs the module-by-module track to confirm):** workspace logic exists
in both `src/ai_bridge/workspace.py` and `src/ai_bridge/control/workspace.py`; rate limiting in both
`ratelimit.py` and `durable_ratelimit.py`; MCP surface in both `server.py` and `control/mcp_routes.py`
+ `control/mcp_client.py`. `app.py` at 5,147 lines is a god-module candidate.

---

## 4. Platform agents — `agents/` (17 files, 6,559 lines)

| File | Lines | Notes |
|---|---|---|
| `agents/linux/agent_main.py` | 1,055 | Full Linux agent CLI + execution |
| `agents/common/poller.py` | 534 | Shared HTTP poller (claim, heartbeat, backoff, token rotation) |
| `agents/common/spool.py` | 131 | Offline spool |
| `agents/windows/gui_app.py` | 2,214 | Windows GUI app (Tkinter-based; largest agent file) |
| `agents/windows/gui_ui.py` | 1,659 | Windows GUI UI |
| `agents/windows/app_runtime.py` | 330 | Windows app runtime |
| `agents/windows/agent_main.py` | 212 | Windows agent entrypoint |
| `agents/windows/diagnose.py` | 201 | Windows diagnostics |
| `agents/windows/desktop_helper.py` | 79 | Desktop screenshot helper |
| `agents/windows/windows_agent.py` | 37 | Thin wrapper |
| `agents/macos/agent_main.py` | 94 | macOS agent; docstring states **"NOT yet run on macOS"** (VERIFIED) |
| `agents/linux/run-agent.sh` | — | Runner script (counted in the 17 files) |
| `agents/{__init__.py × 4, common/__init__.py}` | 0 | Empty |

Asymmetry (VERIFIED): Linux agent is a single 1,055-line CLI; Windows is split across 6 files
(4,731 lines, mostly GUI); macOS is a 94-line thin wrapper over `common.poller`.

---

## 5. Native Windows agent — `windows-agent/` (25 files, 3,433 lines)

| File | Lines |
|---|---|
| `src/capture/dxgi_capture.cpp` | 465 |
| `src/encoder/mf_h264_encoder.cpp` | 428 |
| `src/input/sendinput_controller.cpp` | 365 |
| `src/webrtc/peer_connection.cpp` | 281 |
| `src/webrtc/http_signaling.cpp` | 268 |
| `src/c_api.cpp` | 249 |
| `src/security/local_security.cpp` | 227 |
| `src/service/windows_service.cpp` | 213 |
| `src/desktop/desktop_agent.cpp` | 176 |
| `src/monitoring/system_monitor.cpp` | 148 |
| `src/webrtc/webrtc_transport.cpp` | 112 |
| `src/mcp/mcp_executor.cpp` | 93 |
| Headers (`.hpp` × 9 + `c_api.h`) | 646 total |
| `tests/test_windows_agent.cpp` | (in the 25 files; 6 assert-based checks, Windows-only) |
| `CMakeLists.txt`, `installer/install-service.ps1` | build/install |

VERIFIED via `CMakeLists.txt`: targets `ai_bridge_desktop`, `ai_bridge_service` (INFERRED from
`docs/architecture.md`), `ai_bridge_tests` (CTest `NativeWindowsAgentTests`); libdatachannel
WebRTC is opt-in via `-DAIBRIDGE_WITH_LIBDATACHANNEL=ON` (CI job `windows-native-webrtc`).

---

## 6. Dashboard — `apps/dashboard/` (110 files, 31,108 lines)

### 6.1 Application source (`src/`, 29,723 TS/TSX lines total incl. tests)

| File | Lines | Notes |
|---|---|---|
| `src/views/AiApiHubView.tsx` | 2,773 | Largest view |
| `src/views/LiveDesktopView.tsx` | 2,496 | WebRTC `<video>` + DataChannel input |
| `src/views/TaskQueueVisionView.tsx` | 1,440 | Task queue + SoM vision inspector |
| `src/views/Device360View.tsx` | 1,423 | Device 360 |
| `src/state/dashboardStore.ts` | 1,285 | **Local seeded store: `SEED_DEVICES` = 40 generated nodes (VERIFIED)** |
| `src/views/FleetOperationsView.tsx` | 1,253 | Fleet ops |
| `src/views/ConnectionsView.tsx` | 1,213 | MCP + OAuth |
| `src/views/DevicesView.tsx` | 909 | Devices |
| `src/views/WorkflowsDemoView.tsx` | 899 | Demo-only workflows view |
| `src/views/SecurityPoliciesDemo.tsx` | 843 | Demo-only policies view |
| `src/views/TerminalView.tsx` | 803 | Terminal |
| `src/views/HumanApprovalsView.tsx` | 792 | Approvals |
| `src/views/SecurityPoliciesView.tsx` | 736 | Policies |
| `src/views/DiagnosticsLogsView.tsx` | 718 | Diagnostics/logs |
| `src/views/WorkflowsView.tsx` | 681 | Workflows |
| `src/views/LiveControlView.tsx` | 621 | **Live workspace (real API: SSE live activity)** |
| `src/views/AuditTrailView.tsx` | 525 | Audit |
| `src/api.ts` | 470 | Real REST client (`fetch` to `:8000`, `VITE_API_URL`, session-token auth) |
| `src/ui.tsx` | 391 | Shared UI primitives |
| `src/types/dashboard.ts` | 253 | Domain types |
| `src/views/OverviewView.tsx` | 246 | Overview |
| `src/App.tsx` | 202 | Root router |
| `src/views/LoginView.tsx` | 153 | Login |
| `src/shell/*` (AppShell, SidebarNav, TopHeader, CommandPaletteModal) | 139/88/98/72 | Shell |
| `src/views/live/*` (LiveWorkspace, DeviceWorkspace, Inventory, TasksPanel, ApprovalsPanel, ManagementPanels, TaskComposer, AccountsPanel, NotificationsPanel, shared, preferences) | 24–157 each | **Live workspace panels (real API)** |
| `src/i18n/catalogs.ts` | 277 | 20-locale preview catalogs |
| `src/tailwind.css` | 281 lines (1 CSS file) | Tailwind v4 import — NOTE: PROJECT.md claims "zero custom CSS"; `tailwind.css` exists (VERIFIED) |
| `src/main.tsx`, `Brand.tsx`, `DensityControl.tsx`, `LanguageControl.tsx`, `ThemeControl.tsx` | 23–64 each | |

Views total: 15 screens (login + 14) per `apps/dashboard/README.md` and `src/views/` listing;
plus 2 `*DemoView` variants (`WorkflowsDemoView`, `SecurityPoliciesDemo`) and a `live/` panel set.
`apps/dashboard/README.md` (VERIFIED) states: **"Explore demo workspace opens the advanced
dashboard preview. All 14 operational/governance screens use seeded browser-local data, not live
fleet telemetry… Demo actions never call the live mutation APIs."** The live path is the separate
`LiveWorkspace`/`LiveControlView` + `src/views/live/*` panels backed by `src/api.ts`.

### 6.2 Dashboard tests, e2e, reference mockups

| Area | Files | Lines |
|---|---|---|
| `src/tests/` (harness + run-all + R1–R5 + Tier 1–5 + challenger ×3 + catalogs) | 16 | 5,705 |
| `e2e/` (Playwright specs ×6) | 6 | 886 |
| `stitch_design_system_implementation/` (13 `code.html` + `screen.png` reference mockups + `design.md` + tactile spec) | 28 | ~8,000 HTML lines |
| Config: `package.json`, `package-lock.json`, `vite.config.ts`, `tsconfig.json`, `tsconfig.tsbuildinfo` (present — a previous `tsc` build ran), `playwright.config.ts`, `index.html` | 8 | — |
| `DASHBOARD_DESIGN_SPEC.md` (408 lines), `README.md` (125 lines) | 2 | — |

NOT FOUND: `apps/dashboard/node_modules/` (no installed dependencies in this tree),
`apps/dashboard/dist/` (build output absent despite `tsconfig.tsbuildinfo` being present).

---

## 7. Integrations, deployment, migrations, shared, scripts, docs

### 7.1 `integrations/` (14 files, 773 lines) — mostly config + thin READMEs

| Path | Lines | Content |
|---|---|---|
| `custom-ai/openai_agent.py` | 124 | OpenAI agent |
| `custom-ai/anthropic_agent.py` | 116 | Anthropic agent |
| `custom-ai/quickstart.py` | 93 | Quickstart |
| `custom-ai/langchain_agent.py` | 77 | LangChain agent |
| `custom-ai/ollama_local_agent.py` | 70 | Ollama local agent |
| `custom-ai/README.md` | 167 | Custom-AI docs |
| `copilot365/README.md` | 64 | Copilot 365 notes (README only, no code) |
| `generic-mcp/mcp-config.json` | 21 | Generic MCP config |
| `claude/claude-config.json` | 9 | Claude stdio config |
| `chatgpt/README.md` | 14 | ChatGPT notes (README only) |
| `claude/README.md` | 5 | README only |
| `codex/README.md` | 12 | README only |
| `gemini/README.md` | 10 | README only |

VERIFIED: ChatGPT/Claude/Codex/Gemini integrations are **configuration + README only** (no
integration code); only `custom-ai/` contains Python agent code (480 lines total).

### 7.2 `deployment/` (31 files, 5,810 lines)

aws (README, `cloudformation.yml`, `deploy.sh`), docker (`Dockerfile`, `Dockerfile.prod`,
`nginx.conf`), linux (`deploy-production.sh`, `install.sh`, `magic-install.sh`, `run-agent.sh`),
macos (README, `com.aibridge.agent.plist`), oauth (`README.md`, `gateway.py`), systemd
(`ai-bridge-agent.service`, `ai-bridge-control.service`), windows (`Build-EXE.bat`,
`Install-AIBridge.ps1`, `Setup.bat`, `Uninstall-AIBridge.ps1`, `Uninstall.bat`, `build_exe.py`,
`create-desktop-shortcut.ps1`, `install-agent.ps1`, `installer_gui.py`, `requirements.txt`,
`uninstall-agent.ps1`, README), plus `copilot_smoke_check.py`, `desktop-automation.md`,
`smoke_check.py`.

### 7.3 `migrations/` (12 files, 278 lines) — `001_initial.sql` … `012_full_schema_sync.sql`

Runner: `src/ai_bridge/control/migrations.py` (`schema_migrations` table, chronological apply,
SQLite + PostgreSQL). No down/rollback migrations present (VERIFIED — filenames only `NNN_*.sql`,
no `down` files).

### 7.4 `shared/` (4 JSON files)

`permissions/permissions_matrix.json`, `protocols/input_protocol.json`,
`protocols/named_pipe_protocol.json`, `schemas/session.schema.json` — canonical contracts
referenced by `docs/architecture.md` and `docs/security.md`.

### 7.5 `scripts/` (6 files, 1,080 lines)

`dev.sh`, `k6/claim_cancel_soak.js`, `locust/claim_cancel_soak.py`,
`libwebrtc_e2e_checklist.md`, `libwebrtc_e2e_harness.py`, `setup_mcp_chatbots.py`.

### 7.6 `docs/` (19 files, 1,754 lines)

`MCP_CHATBOT_SETUP.md` (380), `ai-bridge_ADVANCED_ROADMAP.md` (374), `api-reference.md` (15 —
a stub list of endpoints, labelled v0.2), `architecture.md` (62), `implementation-status.md` (87),
`installation-linux.md` (90), `installation-windows.md` (173), `localization.md` (56),
`mcp-clients.md` (19), `operations.md` (14), `production-readiness.md` (110),
`remote-access.md` (24), `render.md` (81), `security.md` (82), `streaming.md` (58),
`threat-model.md` (24), `troubleshooting.md` (26), `windows-agent.md` (68), `ROADMAP.md` (11).

### 7.8 Root scripts

`magic-install.sh` (23,244 B), `magic-install.ps1` (18,797 B), `deploy_to_aws.py` (5,052 B),
`deploy_to_droplet.py` (3,621 B), `Setup.bat` (806 B). The magic-install scripts hard-code the
control-plane URL `https://jxiptvplayer.app` (VERIFIED in `README.md` and
`docker-compose.prod.yml:62` `AI_BRIDGE_MCP_ALLOWED_HOSTS` default).

---

## 8. Largest files in the repo (VERIFIED)

| File | Lines |
|---|---|
| `src/ai_bridge/control/app.py` | 5,147 |
| `src/ai_bridge/tools.py` | 2,990 |
| `apps/dashboard/src/views/AiApiHubView.tsx` | 2,773 |
| `apps/dashboard/src/views/LiveDesktopView.tsx` | 2,496 |
| `src/ai_bridge/control/ai_api.py` | 2,284 |
| `agents/windows/gui_app.py` | 2,214 |
| `src/ai_bridge/server.py` | 1,701 |
| `src/ai_bridge/control/custom_agents.py` | 1,655 |
| `src/ai_bridge/webrtc_stream.py` | 1,348 |

## 9. Findings

| # | Finding | Evidence |
|---|---|---|
| INV-01 | No `.git` history in the delivered tree — version, tags, and provenance unverifiable from the repo itself | `git log` → "not a git repository"; no `.git/` dir |
| INV-02 | `ORIGINAL_REQUEST.md` is missing although TEST_INFRA.md/TEST_READY.md cite it as an authoritative baseline (R1–R5) | `ls ORIGINAL_REQUEST.md` → No such file |
| INV-03 | `apps/dashboard/dist/` absent although `docs/implementation-status.md` claims the built output "IS included in the release ZIP" | `ls apps/dashboard/dist` → No such file |
| INV-04 | `apps/dashboard/node_modules/` absent — dashboard cannot be built or tested in this tree without installing dependencies | `ls` → No such file |
| INV-05 | `src/ai_bridge/control/app.py` (5,147 lines, 143 routes) is a god-module; largest file in the repo | `wc -l`, route grep |
| INV-06 | MCP surface is large: 109 `@mcp.tool()` decorators in `server.py` | grep count |
| INV-07 | ChatGPT/Claude/Codex/Gemini "integrations" are README + JSON config only; only `custom-ai/` has code | `wc -l integrations/*/*` |
| INV-08 | macOS agent is a 94-line wrapper whose own docstring says "NOT yet run on macOS" | `agents/macos/agent_main.py` |
| INV-09 | No `tests/conftest.py`; pytest config only in `pyproject.toml` | `find` |
| INV-10 | No rollback/down migrations; 12 forward-only SQL files | `ls migrations/` |
| INV-11 | `src/ai_bridge/__init__.py` imports `client` (httpx) — importing any `ai_bridge.*` module requires httpx | `src/ai_bridge/__init__.py:8`, import failure observed (report 15) |
| INV-12 | Duplicate responsibilities visible at inventory level: `workspace.py` ×2, rate-limit ×2, MCP surface ×3 (`server.py`, `mcp_routes.py`, `mcp_client.py`) | file listing |
| INV-13 | Dashboard has two parallel UIs: live workspace (`src/views/live/*` + `api.ts`) vs seeded demo (`dashboardStore.ts`, 40 fake nodes) — inventory alone cannot tell which a user lands on | `apps/dashboard/README.md`, `dashboardStore.ts:68-69` |
| INV-14 | `tailwind.css` exists in `src/` while PROJECT.md claims "zero custom CSS" | `apps/dashboard/src/tailwind.css` (281 lines) |
| INV-15 | Hard-coded production control-plane host `jxiptvplayer.app` appears in README magic-install, `docker-compose.prod.yml`, and (per docs) Render config | grep |

> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Regression Register

Tests added during remediation to protect existing functionality and lock in fixes.
Final suite (parent-verified, 2026-11-17): **750 passed / 5 skipped / 12 failed** — the 12 are the pre-existing baseline failures listed below; no new failure was introduced by any wave. No test was deleted or disabled to make the suite pass.

## Pre-existing failures (present on the pristine baseline `79c010e`, unchanged through all waves)

| # | Test | Attributed cause |
|---|---|---|
| 1 | `tests/test_windows_v1_architecture.py::test_native_windows_dll_metrics_and_screen_capture` | Windows-only expectations |
| 2 | `tests/test_windows_v1_architecture.py::test_desktop_mcp_tools_and_human_takeover_enforcement` | Windows-only expectations |
| 3 | `tests/test_windows_v1_architecture.py::test_webrtc_disconnect_releases_held_keys` | Windows-only expectations |
| 4 | `tests/test_windows_v2_architecture.py::test_v2_clipboard_synchronization_and_policy` | Windows-only expectations |
| 5 | `tests/unit/test_aci_desktop_deepening.py::test_capture_desktop_bettercam_uses_monitor_index` | Missing PIL / Windows-only capture path |
| 6 | `tests/unit/test_browser_intelligence.py::test_firecrawl_crawl_requires_key` | Firecrawl API key not present |
| 7 | `tests/unit/test_enhanced_features.py::test_screen_capture_desktop_dimensions` | Missing PIL / xdotool |
| 8 | `tests/unit/test_execution.py::test_subprocess_linter_runs_cross_platform` | Missing `ruff` binary |
| 9 | `tests/unit/test_phase9_deep.py::test_legacy_webrtc_session_deprecation_header` | Legacy-WebRTC header expectation |
| 10 | `tests/unit/test_phases_456_deep.py::test_connectors_fail_without_api_key` | Connector API keys not present |
| 11 | `tests/unit/test_phases_456_deep.py::test_dispatch_registers_firecrawl_and_browserbase_tools` | Firecrawl/Browserbase keys not present |
| 12 | `tests/unit/test_serverai_mcp_orchestration.py::test_http_client_reused_across_api_calls` | httpx proxy-env artifact (`httpx.InvalidURL: Invalid port: ':1]'` in the Wave 4 log) |

## Known-flaky pre-existing test (NOT a regression, bound NOT loosened, test NOT modified)

- `test_concurrent_claim_and_cancel_under_load` — wall-clock assertion (`elapsed < 5.0`) that fails intermittently under load on this 1.5-CPU host (observed 5.09 s, 6.28 s, 6.5 s, 7.06 s). Proven pre-existing by running it in a clean worktree at `19747b2` (fails there too) and by interleaved patched/pristine runs both failing. It passed in the final Wave 4 run. **Action for a future wave:** quarantine it or give it a load-aware bound — do not silently delete it.

## Regression tests added per wave (361 new tests; all passing at their wave)

| Wave | Test files | Tests | What they guard |
|---|---|---|---|
| 1A | `tests/unit/test_deployment_config.py` | 18 | Tier-3 default, fail-fast secrets, nginx redirect/metrics/auth_request, installer `curl -k` removal, CI |
| 1B | `tests/unit/test_agent_policy_merge.py`, `tests/unit/test_webrtc_viewonly.py` | 26 | Intersection-only policy merge, restrictive Linux default, view-only default-deny + takeover |
| 1C | `tests/unit/test_p0_mcp_gate.py`, `tests/unit/test_p0_policy.py` | 34 | Env-var bypass removal, Tier-4 approval, `unrestricted` provenance, 109/109 gating, audit writer |
| 2A | `tests/unit/test_p0_control_plane.py`, `tests/unit/test_tenant_isolation.py`, `tests/unit/test_workflow_approval.py`, `tests/unit/test_signed_updates.py` | 35 | term-* approval, tenant isolation, workflow approval, signed updates, scheduler atomic claim, epoch fencing |
| 2-fixup | `tests/unit/test_stream_ticket.py` | 11 | Stream-ticket mint/redeem (single-use, 60 s TTL, 4401 on replay/expired/wrong-device) |
| 3A | `tests/unit/test_db_phase2.py`, `tests/unit/test_distributed_phase2.py` | 39 | Migrations 014/015, FKs/CHECKs, schema parity, backup/restore drill, retention, async limiter, Redis fan-out (fake broker), idempotency, stuck-handoff sweeper |
| 3B | `tests/unit/test_architecture_phase4.py`, `test_architecture_phase4_routers.py`, `test_architecture_phase4_tools.py`, `test_architecture_phase4_errors.py` | 110 | Router snapshot (220 pairs), tools-package API compatibility, error taxonomy, redaction, SSRF validation |
| 3C | `tests/unit/test_agent_reliability.py`, `tests/unit/test_webrtc_phase3.py`, `tests/unit/test_wave3c_sdk.py` | 81 | Spool DLQ, drain, heartbeat-during-hung-task, 401 classification, RTCP ABR, held-button release, stale-session refusal, ICE gate, MCP client, shell contract, browser sandbox, OCR hardening |
| 4 | `tests/unit/test_wave4_wiring.py` | 7 | WS legacy-token rejection, OIDC exchange flow, list projection + `/result`, policy-simulate endpoint, limiter import source, `to_pg_sql` translation |

## Tests rewritten (not deleted)

- Wave 1B: 3 existing tests rewritten because they asserted the vulnerable behaviour.
- Wave 2 fix-up: 21 dashboard test expectations updated to the login-first contract; 1 real bug fixed (`Device360View` null-device dereference); 2 tests rewritten (tier5 4.6 asserting the fix; 5.7 font-mono false positive corrected).
- Wave 4: PERF-03 correction — 2 tests failed when the first cut projected `result` out of the single-task GET; the implementation was corrected (single GET keeps `result`) and both tests passed **unchanged**.

## Tests never run (see FINAL_TEST_RESULTS.md §6)

Playwright e2e (48 cases, `dashboard.spec.ts:134` still asserts the old OIDC fragment flow); Windows `ai_bridge_tests`; Postgres boot/restore drill; two-worker Redis fan-out / 4-worker load test; live aiortc peer; live Ollama/LLM/Copilot; live deploy/enrollment; GitHub CI run; 10-step E2E workflow; failure/recovery tests.

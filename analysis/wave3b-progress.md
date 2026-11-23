> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 3B — Phase 4 architecture consolidation + error taxonomy + secrets hygiene: progress

Base commit: `19747b2` (Wave 2 fix-up). No reset/rebase/commit. No packages
installed. Work split into three tracks with disjoint file ownership; the
parent merged them, wrote the consolidation test file, and ran the full suite.

Per-track detail lives in:

- `analysis/wave3b-track-a.md` — app.py APIRouter split + app.py webhook SSRF
- `analysis/wave3b-track-b.md` — tools.py split + registry + limiter
- `analysis/wave3b-track-c.md` — errors/security/config/server/mcp_routes

## 0. Baseline (verified by the parent on the clean tree, before any edit)

Full suite: **513 passed, 5 skipped, 12 failed** (`.venv/bin/python -m pytest
tests/ -q`, 126 s). The 12 failures were pre-existing; their names were not
captured in that run, so the post-merge verification below re-runs them
against a pristine `git archive 19747b2` tree instead of relying on the count.

## 1. Per-task results

| Finding | Task | Status | Evidence |
|---|---|---|---|
| OV-02, INV-05 | Split `control/app.py` (5,538 LOC) by domain behind `APIRouter`s; routes unchanged | COMPLETED | `app.py` 5,538 → 2,093 lines; 9 routers in `control/routers/` (health 8, devices 17, tasks 14, audit 5, admin 27, workflows 31, webrtc 20, auth 14, agent 9 = 145 handlers, 0 reverted); `ControlContext` (41 fields) in `routers/_context.py` carries the former `create_app()` locals. Route snapshot test embeds the 19747b2 (method, path) set (220 entries) and diffed empty after every router extraction. |
| F-04-06 | Split `tools.py` (3,042 LOC) into a `tools/` package, full backward compat | COMPLETED | `tools/` package: `_common.py`, `system.py`, `project.py`, `desktop.py`, `shell.py`, `files.py`, `fleet.py`, `desktop_api.py`, `transfers.py`, `recording.py` + re-export-only `__init__.py`. `dir(ai_bridge.tools)` == the 103 pre-split public names (test-embedded); star-import matches; 32 private names also re-exported; `LAST_FRAME_SCALE` served live via PEP 562 `__getattr__`. A `_ToolsModule` (`ModuleType` subclass) propagates package-level monkeypatches to submodule namespaces (14 existing tests depended on patching `tools._check` etc.; covered by 2 new regression tests). |
| INV-12 / TD-D01 | One tool registry | COMPLETED | New `src/ai_bridge/tool_registry.py`: frozen `ToolEntry` (name/tier/handler/description/requires_approval), `TOOL_REGISTRY` pre-populated from `policy.TOOL_TIERS`, `register_tool`, `get_tool` (normalizes via `policy.normalize_tool_name`), `list_tools`, `tools_by_tier`, `get_tier`. Imports only from `.policy`. Count correction: `TOOL_TIERS` has **149 entries (61 READ_ONLY / 9 LOW_RISK / 76 SENSITIVE / 3 HIGH_RISK)**, not the 144 in the brief. `server.py` `_tool_tier()` reads the registry (fallback to `TOOL_TIERS` only if unavailable); `mcp_routes.py` execute path reads the registry-backed policy gate. |
| INV-12 / TD-D05 | One limiter | COMPLETED (interface); caller migration PENDING (see §3) | New `src/ai_bridge/control/limiter.py` is the single interface (`allow`, `allow_async`, `rate_limit`, `rate_limit_async`, `make_rate_limiter`, `make_async_rate_limiter`, `backend_name`, `redis_required`, `reset_for_tests`), delegating to `durable_ratelimit` (Redis → memory on outage → DB only when no Redis URL, PERF-02; DIST-07 async-in-loop warning preserved). `control/ratelimit.py` is now a thin shim re-exporting the identical objects. `durable_ratelimit.py` kept as the implementation layer. Default limit documented as the verified 120/min. |
| INV-12 (task service / audit writer) | Verify singletons; delete/redirect duplicates | VERIFIED (duplicates found, out of scope) | One task service confirmed (`control/task_service.py::create_task`; other `RUNNER.submit` sites are execution, not creation). One audit writer exists (`audit_writer.py` → `db.audit()` sink) but live bypasses remain, none in Wave 3B files: `audit.py::audit_event()` still called by `control/dispatch.py:705`; direct `db.audit` callers in `control/mcp_routes.py` (369, 458, 553, 592, 653, 672), `control/workspace.py` (43, 63), `control/mcp_oauth.py:173`. Disposition belongs to a wave that owns those files. |
| FR-06 / F-06-11 | Error taxonomy in `errors.py`, wired into MCP + control errors | COMPLETED | New `src/ai_bridge/errors.py`: `ErrorCode` enum (13 codes), `ERROR_CATALOG` (code → HTTP status + message), `BridgeError` with `.to_dict()`, `error_response()`, `error_detail()` (`"[CODE] message"`), `code_of_message()`. Codes: AUTH_REQUIRED 401, POLICY_DENIED 403, APPROVAL_REQUIRED 403, TENANT_FORBIDDEN 403, TASK_NOT_FOUND 404, DEVICE_NOT_FOUND 404, APPROVAL_NOT_FOUND 404, NOT_FOUND 404, STALE_EPOCH 409, RATE_LIMITED 429, UPSTREAM_UNAVAILABLE 502, VALIDATION_FAILED 400, INTERNAL_ERROR 500. Wired into `server.py` `@_gated` (denials → POLICY_DENIED / APPROVAL_REQUIRED when `needs_approval`; validation → VALIDATION_FAILED; tool exceptions re-raised with the mapped code prefixed; audit rows carry the code) and `mcp_routes.py` HTTP errors (code-prefixed details). |
| F-04-09 | Structured redaction, not regex-only | COMPLETED | `security.py`: `redact_obj()` structured traversal already existed and is kept; `is_secret_name()` now checks **all** path components relative to the workspace root (so `secrets/mykey.txt`, `config/.env`, `keys/id_rsa` are refused; absolute pytest-tmp prefixes do not false-positive). `image_base64` is preserved only when actually base64 image data (≥256 chars, valid alphabet, image magic); otherwise redacted. JWT-shaped tokens (`eyJ…`) added to `redact()`. |
| F-05-17 / SEC-11 | SSRF validation in the webhook sender (DNS pinning + redirect control + private-IP block) | COMPLETED | Shared validator `security.validate_outbound_url()`: resolves the host, checks **every** resolved A/AAAA address, rejects loopback/RFC1918/link-local (incl. 169.254.169.254)/CGNAT/multicast/reserved/ULA, rejects embedded credentials and non-http(s). All three senders use it: `app.py` `_validate_webhook_url()`/`send_webhook_notification()` (strict DNS, fail-closed; manual redirects, max 3, each target re-validated; 301/302/303→GET, 307/308→POST), `alerts.py` `_send_http_json()` (`follow_redirects=False`, max 3 re-validated hops), `worm_siem.py` `emit_audit_event()`. Documented carve-out: 198.18.0.0/15 is NOT blocked — the sandbox DNS wildcard resolves every host there, so blocking it would break all webhook delivery here; revisit where that range is routed. |
| F-06-14 | `enforce_mcp_tool_policy()` on ALL MCP execute paths | COMPLETED | `control/mcp_routes.py` `run_mcp_tool_test()` (POST `/api/mcp/servers/{server_id}/execute`) now calls `enforce_mcp_tool_policy(conn, srv, body.tool, body.arguments, actor=actor)` before `execute_mcp_tool()`: deny → HTTP 403 `POLICY_DENIED`; require_approval → HTTP 403 `APPROVAL_REQUIRED` + `approval_id`, without executing. Matches the existing call pattern in `ai_api.py:1783` / `custom_agents.py:767`. |
| F-06-16 | Admin token out of tool args | COMPLETED | `server.py` `approve_task(approval_id, admin_token)` → `approve_task(approval_id)`. Auth is now session-based via `_is_admin_caller()` (HTTP bearer == `AI_BRIDGE_ADMIN_TOKEN`); otherwise `PermissionError("[AUTH_REQUIRED] …")`, failing closed in stdio. Unknown id → `ValueError("[APPROVAL_NOT_FOUND] …")`. `_GATE_DECIDE_EXEMPT` was **kept** for `approve_task` (documented): `decide()` grants Tier 4 only on approval AND admin, and an approval for `approve_task` itself can never exist, so removing the exemption deadlocks approval for everyone. The one caller, `tests/integration/test_control_plane.py:95`, was updated to set the bearer context (`monkeypatch.setattr(mcp_server, "_bearer_token", lambda: ADMIN)`) plus a stdio fail-closed assertion; not weakened. Grep: `approve_task` was the only tool taking a secret argument (`ai_api.py` `api_key` hits are internal LLM helpers / BYOK request fields, out of scope). Note: the MCP HTTP app's session manager runs its lifespan once per process — a second `TestClient(create_http_app())` in the same pytest process breaks `test_chatgpt_mcp_discovery.py`; avoided. |
| F-04-12 | `control/workspace.py` name collision | COMPLETED | `control/workspace_routes.py` is now the canonical module; `control/workspace.py` is a thin compatibility shim re-exporting `install_workspace_routes`. `app.py`'s import untouched. |
| F-04-23 | Cache/validate Settings | COMPLETED | `config.py`: `get_settings()` cached via `functools.lru_cache` keyed on an env fingerprint (a bare cache would break tests that `monkeypatch.setenv`); `reset_for_tests()` + cache clear exposed. `_int()` raises `ValueError` on garbage/out-of-range instead of silently defaulting (ranges: read/output bytes > 0, list entries > 0, timeouts 1..3600, concurrency 1..64, port 1..65535). `expandvars` removed from workspace/data paths (`expanduser` only). |
| F-05-11 | Stale rate-limit docstring | COMPLETED | `app.py` module docstring now states the real numbers: 30/min per IP on auth endpoints; 1200/min per authenticated token, 300/min per unauthenticated IP otherwise. (Track B also flagged `docs/operations.md:16` — not a Wave 3B file; still open.) |
| F-05-18 | Replace regex SQL translation (`to_pg_sql`, `control/db.py:230`) | BLOCKED | `db.py` is in no Wave 3B track's file list. Untouched. The regex translation (placeholder/`INSERT OR REPLACE`/`INSERT OR IGNORE` rewriting) remains as-is; needs a wave that owns `db.py`. |
| — | Stale docstrings touched | COMPLETED | Updated in `server.py` (approval-store comment, `_gate_tool`/`_gated` docstrings, `_GATE_DECIDE_EXEMPT` comment, `approve_task`/`request_approval` notes), `config.py` (module docstring), `control/workspace_routes.py`, `alerts.py`/`worm_siem.py` (SSRF notes). |

## 2. Tests

New files (110 tests, all passing in the merged tree):

- `tests/unit/test_architecture_phase4.py` (parent, 15 tests) — cross-track
  invariants: route snapshot identical (220 entries + conditional SPA/mount
  entries when `apps/dashboard/dist` exists); tools re-exports complete;
  registry == `TOOL_TIERS` (149 entries, tiers + `requires_approval` flags);
  registry name normalization; `server._tool_tier()` agrees with the
  registry; limiter interface + shim identity; taxonomy coverage (all 13
  codes with statuses); `BridgeError` payload; gate denial raises a coded
  `PermissionError`; nested redaction (dicts/lists/free text/JWT/secret
  paths); SSRF private/metadata IP blocked, public allowed, non-http and
  credential-embedded URLs rejected; Settings cached + validated.
  Four of these tests initially failed on test bugs (missing `status` key —
  the taxonomy API carries status on `ErrorInfo`/`BridgeError.http_status`,
  not in the response dict; SPA entries; underscore-name filter) and were
  fixed in the test file, not in the implementation.
- `tests/unit/test_architecture_phase4_routers.py` (Track A, 19 tests) —
  snapshot, per-router smoke, SSRF ×9, docstring, imports.
- `tests/unit/test_architecture_phase4_tools.py` (Track B, 28 tests) —
  exact 103-name public API, star-import, private-name re-exports, live
  `LAST_FRAME_SCALE`, monkeypatch propagation.
- `tests/unit/test_architecture_phase4_errors.py` (Track C, 46 tests) —
  taxonomy, gate coding, `approve_task` contract, redaction, SSRF senders,
  config validation.

Targeted per-track runs (before merge): A — 30/31/38/57/40/72/52 per router
batch, all passing; broader 20-file sweep 165 passed, 1 skipped, 7 failed,
all 7 verified pre-existing on pristine 19747b2. B — new file 28 passed;
targeted 135 passed, 2 failed (both pre-existing on pristine baseline).
C — new file 46 passed; targeted 112 passed, 2 failed (environmental:
sandbox `NO_PROXY` contains `[::1]`, which httpx proxy parsing rejects in
`connectors/firecrawl.py`, an untouched file; both pass with `NO_PROXY`
unset).

## 3. Full-suite verification (parent, merged tree)

Command: `TMPDIR=$HOME/workspace/tmp-pytest .venv/bin/python -m pytest
tests/ -q -p no:cacheprovider --tb=no` (610 s; `/tmp` is a 512M tmpfs at
98%, so `TMPDIR` was redirected — Track A hit `database or disk is full`
failures before this workaround).

Result: **12 failed, 743 passed, 5 skipped, 1 warning.**

Baseline was 513 passed / 5 skipped / 12 failed. The +230 passed is the
combined tree (sibling waves 3A/3C also landed tests in the same working
tree); Wave 3B's own contribution is the 110 Phase 4 tests above. The
failure count is unchanged at 12 — and the 12 are **the same 12 tests**,
verified by running exactly those 12 against a pristine
`git archive 19747b2` extraction (pytest imports the pristine `src` —
verified with a probe test asserting `ai_bridge.server.__file__` is under
the pristine tree): all 12 fail identically there.

The 12 pre-existing failures (none in Wave 3B files except
`test_http_client_reused_across_api_calls`, which fails identically on the
pristine tree):

- `tests/test_windows_v1_architecture.py::test_native_windows_dll_metrics_and_screen_capture`
- `tests/test_windows_v1_architecture.py::test_desktop_mcp_tools_and_human_takeover_enforcement`
- `tests/test_windows_v1_architecture.py::test_webrtc_disconnect_releases_held_keys`
- `tests/test_windows_v2_architecture.py::test_v2_clipboard_synchronization_and_policy`
- `tests/unit/test_aci_desktop_deepening.py::test_capture_desktop_bettercam_uses_monitor_index`
- `tests/unit/test_browser_intelligence.py::test_firecrawl_crawl_requires_key`
- `tests/unit/test_phases_456_deep.py::test_connectors_fail_without_api_key`
- `tests/unit/test_phases_456_deep.py::test_dispatch_registers_firecrawl_and_browserbase_tools`
- `tests/unit/test_enhanced_features.py::test_screen_capture_desktop_dimensions`
- `tests/unit/test_execution.py::test_subprocess_linter_runs_cross_platform`
- `tests/unit/test_phase9_deep.py::test_legacy_webrtc_session_deprecation_header`
- `tests/unit/test_serverai_mcp_orchestration.py::test_http_client_reused_across_api_calls`

No NEW failures were introduced by Wave 3B. No tests were deleted or
disabled; no security checks were disabled. Nothing was committed.

## 4. Files changed (Wave 3B only)

- `src/ai_bridge/control/app.py`: 5,538 → 2,093 lines (routers extracted;
  middleware, CSRF/CORS, ai_api/v1/mcp/custom-agent installs, scheduler/
  workflow threads, shared helpers stay).
- New `src/ai_bridge/control/routers/`: `__init__.py`, `_context.py`,
  `health.py`, `devices.py`, `tasks.py`, `audit.py`, `admin.py`,
  `workflows.py`, `webrtc.py`, `auth.py`, `agent.py`.
- Deleted `src/ai_bridge/tools.py`; new `src/ai_bridge/tools/` package
  (11 files).
- New `src/ai_bridge/tool_registry.py`, `src/ai_bridge/errors.py`,
  `src/ai_bridge/control/limiter.py`,
  `src/ai_bridge/control/workspace_routes.py`.
- Modified: `src/ai_bridge/security.py`, `src/ai_bridge/config.py`,
  `src/ai_bridge/server.py`, `src/ai_bridge/control/mcp_routes.py`,
  `src/ai_bridge/control/alerts.py`, `src/ai_bridge/control/worm_siem.py`,
  `src/ai_bridge/control/workspace.py` (shim),
  `src/ai_bridge/control/ratelimit.py` (shim),
  `src/ai_bridge/control/durable_ratelimit.py` (docstring note only),
  `tests/integration/test_control_plane.py` (approve_task caller only).
- New tests: `tests/unit/test_architecture_phase4.py`,
  `test_architecture_phase4_routers.py`,
  `test_architecture_phase4_tools.py`,
  `test_architecture_phase4_errors.py`.
- This file + `analysis/wave3b-track-a.md`, `wave3b-track-b.md`,
  `wave3b-track-c.md`.

## 5. Open items / follow-ups (not Wave 3B failures)

1. **Limiter caller migration (TD-D05, partial):** the single interface
   exists and `ratelimit.py` re-exports it, but two call sites still
   import the implementation directly — `control/app.py:200` and
   `webrtc_stream.py:929` import `durable_ratelimit.allow`
   (`app.py:53` imports via the shim, so it is already on the interface).
   `webrtc_stream.py` and `production_readiness.py:185` (also a direct
   caller per Track B) are outside Wave 3B's file list. Re-pointing them
   is a follow-up for the owning wave.
2. **Audit-writer duplicates (§1):** `audit.py::audit_event()` via
   `control/dispatch.py:705` and direct `db.audit` callers in
   `mcp_routes.py`/`workspace.py`/`mcp_oauth.py` — needs a disposition
   from a wave owning those files.
3. **F-05-18 BLOCKED:** regex SQL translation in `control/db.py:230`
   untouched (file ownership).
4. **`docs/operations.md:16`** rate-limit docstring still contradicts the
   enforced limits (docs file, not Wave 3B scope).
5. **198.18.0.0/15 carve-out:** SSRF blocklist deliberately excludes the
   benchmarking range because the sandbox DNS wildcard resolves every
   host there; revisit where that range is routed.
6. **MCP session-manager single-lifespan:** only one
   `TestClient(create_http_app())` lifespan per pytest process; the slot
   is owned by `test_chatgpt_mcp_discovery.py`.
7. **`_ToolsModule.__setattr__` propagation** in `tools/__init__.py` is
   load-bearing for tests that monkeypatch `tools.*`; future refactors
   of the package must keep it (regression-tested).
8. **FastAPI ≥0.163 `include_router()` bookkeeping:** `app.routes` gains
   an `_IncludedRouter` entry per router; anything enumerating routes
   must recurse into `original_router.routes` (the snapshot test does).
9. **Working tree is shared with sibling waves (3A/3C):** the full-suite
   numbers in §3 measure the combined tree, not Wave 3B alone. Wave 3B
   touched only its assigned files; sibling modifications (agents/,
   windows-agent/, `db.py`, `mcp_client.py`, migrations 014–015, etc.)
   are not Wave 3B's.

## 6. Rollback

Per track, per router/module: routers are additive (`include_router` —
revert one router by restoring its handlers to `app.py`); the tools
package reverts by restoring `tools.py` from git (`git show
19747b2:src/ai_bridge/tools.py`); the registry/limiter/errors are new
modules with shims behind them (`ratelimit.py`, `workspace.py`) so
removing the new modules restores the old import paths. No commits were
made, so `git stash` / `git checkout` of the listed files reverts Wave
3B cleanly. Route-snapshot and re-export tests fail loudly on any
regression.

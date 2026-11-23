> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 1C — Progress: MCP policy gate, tiers, audit writer, unrestricted policy

Owner: Wave 1C subagent. Repo: `/home/hatch/workspace/ai-bridge-main`.
Scope files ONLY: `src/ai_bridge/server.py`, `src/ai_bridge/policy.py`,
`src/ai_bridge/tools.py` (read, NOT modified), `src/ai_bridge/audit.py`;
ADDED: `src/ai_bridge/audit_writer.py`, `tests/unit/test_p0_mcp_gate.py`,
`tests/unit/test_p0_policy.py`. No commit. No `control/*`, `agents/*`,
deployment, dashboard, or windows-agent changes.

All findings were verified against the code before editing (see §1).
All tests below ran with the repo venv python
(`/home/hatch/workspace/ai-bridge-main/.venv/bin/python`).

## 1. Finding verification (before any edit)

| Finding | Verified as |
|---|---|
| R-P0-01 env off-switches | CONFIRMED: `server.py:79` DNS branch, `server.py:1650` anon branch; `production_readiness.py:87` also references the anon var (NOT touched — Wave 2, see §5). |
| R-P0-02 unrestricted / Tier-4 ceiling | CONFIRMED: `policy.py:196,221-266` — `unrestricted` bypassed all checks; `max_tier_unattended>=4` silently authorised Tier 4. `control/app.py:1015` enrollment-body grant is control/* — NOT in this wave (see §5). |
| R-P0-05 5 untiered tools | CONFIRMED by AST count: 109 `@mcp.tool`, exactly 5 missing from `TOOL_TIERS`: `system_info`, `view_live_screen`, `interact_and_view_screen`, `select_device`, `browser_navigate_with_shot`. 40 tiered-but-unexposed tools are plane-only (agent/control), left as-is. |
| R-P0-06 no MCP audit | CONFIRMED: `audit_event` count 0 in `server.py`/`tools.py`. |
| R-P1-05 no uniform gate | CONFIRMED: ~40 wrappers call `diagnostics`/`workspace`/`git_ops` directly with no `_check`; `tools.*` functions `_check` internally but inconsistently. |
| F-04-02 `_oauth_bound_device` swallows exceptions | CONFIRMED: `server.py:116` `except Exception: return None`. |
| F-06-02 stdio session key `"default"` | CONFIRMED: `server.py:109-112`. |
| F-06-06 approval not passed through | CONFIRMED: `write_file` (server.py:1011), `capture_desktop_screenshot` (:597), `take_screenshot` (:748) called `tools.*` without `approval_granted`. |
| F-06-10 no per-family validation | CONFIRMED: no pydantic models in `server.py`; pydantic>=2.6 IS a declared dependency (`requirements.txt:11`). |
| F-04-01 approvals unscoped, no TTL | CONFIRMED: `_APPROVALS` matched by tool name only, plain dict, no expiry. Partial fix in this wave (session scope + 15-min TTL, in-memory); durable DB storage is Wave 2. |

## 2. Per-task record

### R-P0-01 — Remove env-var auth/DNS off-switches — IMPLEMENTED, VERIFIED
- Files changed: `src/ai_bridge/server.py` (docstring, `_mcp_transport_security`
  block, `_BearerAuthMiddleware`, `main()`).
- Decision: both branches deleted; literal env-var names removed from the
  file entirely (grep returns no matches in my 5 files). DNS protection is
  now FastMCP's default (verified: `TransportSecuritySettings()` defaults
  `enable_dns_rebinding_protection=True`; FastMCP auto-enables it for
  localhost when `transport_security is None`). Bearer auth is always on
  for HTTP. Escape hatch is the explicit `--unsafe-allow-anon` CLI flag:
  prints a banner on every start, `SystemExit` when
  `AI_BRIDGE_PROFILE=production`.
- Tests: `test_anon_http_mcp_returns_401_even_with_env_var_set`,
  `test_http_mcp_with_valid_admin_token_passes_middleware`,
  `test_unsafe_allow_anon_flag_refused_in_production`,
  `test_unsafe_allow_anon_flag_sets_global_and_warns` — all pass.
- Rollback: revert `server.py`; the env vars are gone, so rollback is a
  code revert, not a config change.
- Note for Wave 2: `src/ai_bridge/control/production_readiness.py:87-92`
  still references the anon env var (read-only check, not an off-switch)
  and must be updated to the new world (flag the var as removed).

### R-P0-02 — `unrestricted` policy + Tier-4 approval — IMPLEMENTED, VERIFIED
- Files changed: `src/ai_bridge/policy.py`.
- Decision:
  - `Policy.unrestricted_granted_by: str | None = None` added. `decide()`
    honours `unrestricted` ONLY when provenance is set via
    `grant_unrestricted_policy()` (which audits the grant). A bare
    `Policy(unrestricted=True)` falls through to normal tier checks.
  - Tier 4 check moved BEFORE the unrestricted bypass and the
    `max_tier_unattended >= 4` escape deleted: Tier 4 ALWAYS requires
    `approval_granted and is_admin`, even under a granted-unrestricted
    policy. The ceiling now governs only which tiers may run unattended.
  - `select_device` added to the Tier-3 opt-in exemption set (with
    `submit_task`/`create_backup`): it is routing-only, changes no device
    state, and every subsequent tool call is gated.
  - `grant_unrestricted_policy()` / `revoke_unrestricted_policy()` added
    (audited via `audit_writer.write_audit_event`, lazy import to avoid
    cycles; empty actor raises `ValueError`).
- Known cross-wave impact (intended, per roadmap): tests/fixtures that
  construct `Policy(unrestricted=True, max_tier_unattended=4)` for
  dispatch/orchestration (`test_mai_multi_agent.py`,
  `test_orchestration_phases.py`, `test_phase8_deep.py`,
  `test_phases_456_deep.py`) no longer get a silent bypass; they passed
  in this run (69 passed in that subset) because dispatch does not route
  through `decide()` for those paths, but any future `decide()`-based
  dispatch check will deny them until they use
  `grant_unrestricted_policy()`. The control-plane enrollment half
  (`control/app.py:1015`, body-supplied `unrestricted`) belongs to the
  control-plane wave.
- Tests: `tests/unit/test_p0_policy.py` (12 tests) + existing
  `tests/unit/test_policy.py` (6 tests) — all pass.
- Rollback: revert `policy.py`; re-enable only via an explicit admin
  migration, not by default.

### R-P0-05 + R-P1-05 — Tier all tools; ONE wrapper gate — IMPLEMENTED, VERIFIED
- Files changed: `src/ai_bridge/policy.py` (5 tier entries),
  `src/ai_bridge/server.py` (gate + 109 decorations).
- Tiers added: `system_info` READ_ONLY, `view_live_screen` READ_ONLY,
  `interact_and_view_screen` READ_ONLY, `select_device` SENSITIVE,
  `browser_navigate_with_shot` SENSITIVE.
  - Note on `interact_and_view_screen`: it is READ_ONLY only as a
    composite *view* tool; the input actions it dispatches call the
    `mouse_click`/`keyboard_type`/… wrappers, each of which is gated
    individually, so input can never ride through on this tier.
- Gate (`_gated`, `server.py`): every one of the 109 `@mcp.tool` wrappers
  is decorated (verified by AST: 109/109, `_gated` innermost so it runs
  on the local execution path under `@device_aware`). Per call it:
  validates args (F-06-10) → `decide()` via `_gate_tool()` (peeked
  local approval + bearer-admin status) → executes → audits via
  `audit_writer.record_tool_call` (allowed / denied / validation-failed /
  errored). `approve_task` is exempt from `decide()` only (self-referential:
  it IS the approval mechanism; it is still validated and audited, and
  authenticates by its admin-token argument).
  - Gate `is_admin` derivation: bearer-admin OR a peeked approved local
    approval. Rationale (documented in code): an "approved" local record
    could only have been created via `approve_task` (admin token) or the
    dashboard, so it IS admin authorisation for that tool; without this,
    the documented stdio Tier-4 flow (request → approve with admin token
    → retry) would be unreachable because stdio has no bearer. `decide()`
    itself is unchanged (approval-without-admin is still insufficient
    when called directly).
  - Device-routed calls skip the local gate (the agent/control plane
    authorises at dispatch — R-P0-10/R-P0-11, other waves) but are audited
    as `"routed"` in `device_aware` (route/control/unavailable branches),
    so no MCP invocation is invisible. This preserves existing routed-call
    behaviour (e.g. `test_mcp_admin_fleet.py` passes unchanged).
- Tests: `test_every_mcp_tool_has_a_tier_entry`,
  `test_every_mcp_tool_wrapper_is_gated`, gate deny/allow/audit tests —
  all pass.
- Rollback: remove the `@_gated` lines (mechanical revert); the gate is
  additive — wrappers keep their pre-existing `_check` calls.

### F-04-02 — `_oauth_bound_device()` fail-closed — IMPLEMENTED, VERIFIED
- `server.py`: verification exceptions now log to stderr and raise
  `PermissionError` (deny) instead of returning `None` (unbound/local).
  Admin-token short-circuit unchanged; no-bearer (stdio) unchanged.
- Test: `test_oauth_bound_device_fails_closed` (monkeypatched
  `resolve_binding` raising) and admin short-circuit test — pass.
- Rollback: revert the `except` block.

### F-06-02 — Per-invocation stdio session key — IMPLEMENTED, VERIFIED
- `server.py`: `_session_key()` returns one random `stdio-<hex>` key
  generated once per process for bearer-less (stdio) callers; bearer
  sessions keep the token-hash key. Stable within a process (so
  `select_device`/approvals work), distinct across invocations.
- Test: subprocess test spawns two venv-pythons and asserts distinct
  keys, neither `"default"`; plus in-process stability test — pass.
- Rollback: revert `_session_key()`.

### F-06-06 — Approval pass-through — IMPLEMENTED, VERIFIED
- `server.py`: `write_file`, `capture_desktop_screenshot`,
  `take_screenshot` now consume the local approval and pass
  `approval_granted=` to `tools.*` (which already accepted the kwarg).
- Tests: `test_write_file_passes_approval`,
  `test_capture_screenshot_passes_approval`,
  `test_take_screenshot_passes_approval`,
  `test_tier4_delete_requires_approval_then_succeeds` (single-use
  consumption verified) — pass.
- Rollback: revert the three one-line changes.

### F-06-10 — Pydantic validation per tool family — IMPLEMENTED, VERIFIED
- `server.py`: pydantic v2 `BaseModel`s (extra="ignore") for mouse /
  keyboard / scroll / drag / screenshot scale-quality / shell /
  write_file / browser navigate / select_device / hotkey. The gate
  validates bound call arguments before `decide()`; failures raise
  `ValueError` with the pydantic error list and are audited as denied.
  Bounds are deliberately generous (coords 0–100000, clicks 1–10,
  scale 0.2–1.0, quality 20–95, shell timeout 0–3600).
  - Caught during implementation: `desktop_keyboard` was first mapped
    to the key-required model, but it is legitimately called with text
    only (`key=""`); it now has its own model (`_DesktopKeyboardArgs`).
- Tests: `test_validation_rejects_bad_coordinates_and_does_not_execute`,
  `test_validation_rejects_bad_button`,
  `test_validation_rejects_bad_screenshot_scale` — pass.
- Rollback: delete the `_TOOL_VALIDATORS` mapping (validation becomes a
  no-op).

### F-04-01 (partial) — Session-scoped approvals with TTL — IMPLEMENTED (partial), VERIFIED
- `server.py`: `request_approval` records `session` + `created_at`;
  `_peek_local_approval`/`_consume_local_approval` honour only
  same-session, unexpired (15 min), single-use approvals.
- NOT done (Wave 2): durable DB storage of approvals. Recorded in
  §5.
- Test: `test_approval_from_another_session_is_not_honoured` — pass.

### R-P0-06 — Single audit writer — IMPLEMENTED, VERIFIED
- ADDED `src/ai_bridge/audit_writer.py` (215 lines). Public API (exact
  names for Wave 2): `record_tool_call(*, actor, session_id, tool, tier,
  device_id, decision, args=None, result_summary=None, db_path=None)`
  and `write_audit_event(*, actor, action, device_id=None, details=None,
  db_path=None)`.
  - Sinks: control-plane DB hash chain (`control.db.audit`, lazy import,
    `db_path` arg or `AI_BRIDGE_MCP_AUDIT_DB` env) when a DB is given;
    otherwise append-only JSONL with `fsync` per record (default
    `~/.ai-bridge/mcp-audit.jsonl`, overridable via
    `AI_BRIDGE_MCP_AUDIT_FILE` or `set_jsonl_path()` for tests). DB
    failure falls back to JSONL, never silently dropped; total failure
    is reported on stderr and returns `None`.
  - Redaction: `security.redact_obj`/`redact` on args and results;
    binary blobs (base64 image data) omitted; args capped at 64 KiB,
    individual strings at 4,096 chars, result summaries at 2,000 chars.
  - Wired: the gate calls `record_tool_call` for EVERY invocation
    (allowed/denied/validation-failed/errored); `device_aware` audits
    routed/control/unavailable calls as `"routed"`/`"denied"`.
  - Documented tradeoff: an audit-write failure does NOT block tool
    execution (availability), but is loud on stderr; fail-closed audit
    is a Wave 2 / DB-05 decision.
- `src/ai_bridge/audit.py`: `audit_event` now `flush()` + `os.fsync()`
  per record (F-04-21).
- Tests: `test_audit_writer_redacts_and_truncates`,
  `test_audit_writer_db_hash_chain` (row + `entry_hash` verified by
  direct sqlite query), `test_audit_writer_db_failure_falls_back_to_jsonl`,
  `test_unrestricted_grant_is_audited` — pass.
- Rollback: gate keeps working if the writer is disabled — records
  would stop, so rollback must be paired with restoring the old
  (absent) audit behaviour; audit rows already written remain.

## 3. Tests executed (exact commands, venv python)

```
.venv/bin/python -m pytest tests/unit/test_p0_policy.py tests/unit/test_p0_mcp_gate.py -q
  → 34 passed in 2.79s
.venv/bin/python -m pytest tests/unit/test_p0_policy.py tests/unit/test_p0_mcp_gate.py tests/unit/test_policy.py -q
  → 40 passed in 2.49s
.venv/bin/python -m pytest tests/unit/test_policy.py tests/unit/test_mcp_admin_fleet.py \
    tests/unit/test_chatgpt_mcp_discovery.py tests/unit/test_desktop_gui_tools.py -q
  → 15 passed
.venv/bin/python -m pytest tests/unit tests/security -q   (proxy env vars unset, see §4)
  → 403 passed, 5 skipped, 9 failed (first run; 2nd full unit run: 399 passed, 4 failed —
     the 5 WebRTC failures did not reproduce: flaky/order-dependent, pass in isolation)
.venv/bin/python -m pytest tests/integration -q           (proxy env vars unset)
  → 21 passed
.venv/bin/python -m ai_bridge.server --help               → flag listed
AI_BRIDGE_PROFILE=production .venv/bin/python -m ai_bridge.server --unsafe-allow-anon --http
  → "--unsafe-allow-anon is not allowed when AI_BRIDGE_PROFILE=production." (SystemExit)
```

## 4. Failures found during verification — all pre-existing, none caused by Wave 1C

| Failure | Root cause (verified) |
|---|---|
| `test_execution.py::test_subprocess_linter_runs_cross_platform` | `ruff` not installed in venv (`No module named ruff`). `ruff` is a dev extra (`pyproject.toml:26`), not installed by the parent yet. `execution.py` untouched by this wave. |
| `test_aci_desktop_deepening.py::test_capture_desktop_bettercam_uses_monitor_index`, `test_enhanced_features.py::test_screen_capture_desktop_dimensions` | `PIL` not installed (`No module named 'PIL'`, `Image.fromarray` missing). Pillow is a `desktop`/`webrtc` extra, not installed. `screen_capture.py` untouched. |
| `test_browser_intelligence.py::test_firecrawl_crawl_requires_key`, `test_phases_456_deep.py::test_connectors_fail_without_api_key`, `test_phases_456_deep.py::test_dispatch_registers_firecrawl_and_browserbase_tools` | `httpx.Client()` construction raises `InvalidURL: Invalid port: ':1]'` — httpx parses the sandbox's `no_proxy` list, whose bracketed IPv6 entries (`[::1]`, `[fd8b:…]`) break httpx's URLPattern parsing. Verified: clean env → `httpx.Client()` works; with proxy vars unset the 3 tests PASS. Environment artifact, not repo code; no Wave 1C file in any traceback. |
| `test_phase9_deep.py::test_legacy_webrtc_session_deprecation_header` | Control-plane endpoint (`control/app.py:3546`, NOT a Wave 1C file) sets `Deprecation` headers then raises `HTTPException(400)` on non-Windows; Starlette's exception handler drops headers set on the injected `Response`. No `decide()` call exists anywhere on that request path and no `control/*` module imports `ai_bridge.policy`. Pre-existing. |
| 5 WebRTC view-only/datachannel failures (first full-suite run only) | Did not reproduce on re-run (full unit suite 2nd run: 4 failed) and all pass in isolation (`tests/unit/test_webrtc_viewonly.py` + datachannel test: 13 passed). Flaky/order-dependent in the existing WebRTC tests; `webrtc_stream.py` untouched by this wave. |

## 5. Remaining / blocked / deferred (for Wave 2 or the owning wave)

- **Wave 2 — `control/production_readiness.py:87-92`**: still references the
  removed anon env var. Update to the new world (var removed; flag the
  production check accordingly). NOT touched (out of scope).
- **Control-plane wave — enrollment-body `unrestricted`**
  (`control/app.py:1015`, also `:770-788`, `:2272`, `:2315`): the
  enrollment/request-body path that can set `unrestricted=True` must be
  changed to accept it only from an explicit audited admin action
  (use `policy.grant_unrestricted_policy()`). `decide()` now ignores
  bare `unrestricted`, so this is fail-closed today, but the endpoint
  should reject/ignore the field explicitly.
- **Wave 2 — durable approval storage (F-04-01 remainder)**: approvals
  are session-scoped + TTL in memory only; move to the DB.
- **Wave 2 — fail-closed audit (DB-05)**: the writer currently warns
  and continues if all sinks fail; atomic hash-chain writes are DB-05.
- **Wave 2 — audit DB wiring**: the gate reads `AI_BRIDGE_MCP_AUDIT_DB`
  per call; Wave 2 should point it at the control-plane DB by default.
- **Not in this wave (by design)**: R-P0-03/04/07/08/09/10/11/12/13/14/15/16
  and the remaining P1s — owned by sibling waves.
- **Deliberate design decisions to preserve**: (a) routed MCP calls are
  audited but not locally gated (agent/plane authorises at dispatch);
  (b) a peeked local approval satisfies `decide()`'s admin requirement
  for Tier 4 (see R-P0-05 note); (c) `interact_and_view_screen` is
  READ_ONLY as a composite, with inner input actions gated individually;
  (d) `select_device` is SENSITIVE but exempt from the Tier-3 opt-in
  (routing-only).

## 6. Files changed

| File | Change |
|---|---|
| `src/ai_bridge/server.py` (1701 → 2204 lines) | Off-switches deleted; `--unsafe-allow-anon` flag; per-process stdio session key; fail-closed binding; session-scoped/TTL approvals; `_gated` + validators + `_gate_tool`; routed-call audit; F-06-06 pass-through; `request_approval` session/timestamp. |
| `src/ai_bridge/policy.py` (266 → 368 lines) | 5 tier entries; `unrestricted_granted_by`; Tier-4-always-needs-approval; `select_device` Tier-3 exemption; `grant_/revoke_unrestricted_policy()`. |
| `src/ai_bridge/audit.py` (23 → 28 lines) | `flush()` + `os.fsync()` per record. |
| `src/ai_bridge/audit_writer.py` (new, 215 lines) | Single MCP audit writer (DB hash chain + fsync JSONL fallback, redaction, truncation). |
| `src/ai_bridge/tools.py` | NOT modified (read only). |
| `tests/unit/test_p0_mcp_gate.py` (new, 373 lines) | 22 tests. |
| `tests/unit/test_p0_policy.py` (new, 178 lines) | 12 tests. |

Side effect (noted, not hidden): test runs that call server wrappers
without the tmp-file fixture wrote audit records to the default JSONL
path `~/.ai-bridge/mcp-audit.jsonl` (~47 KB) — expected behaviour of
the new writer; left in place as the audit trail of the test runs.

## 7. Status summary

| Finding | Status |
|---|---|
| R-P0-01 | IMPLEMENTED, TESTED, VERIFIED |
| R-P0-02 | IMPLEMENTED, TESTED, VERIFIED |
| R-P0-05 | IMPLEMENTED, TESTED, VERIFIED |
| R-P0-06 | IMPLEMENTED, TESTED, VERIFIED |
| R-P1-05 | IMPLEMENTED, TESTED, VERIFIED |
| F-04-02 | IMPLEMENTED, TESTED, VERIFIED |
| F-06-02 | IMPLEMENTED, TESTED, VERIFIED |
| F-06-06 | IMPLEMENTED, TESTED, VERIFIED |
| F-06-10 | IMPLEMENTED, TESTED, VERIFIED |
| F-04-01 | IMPLEMENTED (partial — in-memory scope + TTL), TESTED; DB storage deferred to Wave 2 |

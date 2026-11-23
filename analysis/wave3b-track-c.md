> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 3B Track C — error taxonomy + secrets hygiene + MCP gate + config

Status: DONE. No commits made. No reset/rebase. No pip installs. No files
edited outside the assigned set. Working tree was NOT clean at start
(sibling tracks had uncommitted changes); this track's footprint is exactly
the files below. Full test suite NOT run (parent runs it at the end); only
targeted tests + import smoke checks, per instructions.

## Files changed

| File | Change |
|---|---|
| NEW `src/ai_bridge/errors.py` | Error taxonomy (FR-06 / F-06-11) |
| `src/ai_bridge/security.py` | F-04-09 redaction + SSRF validator shared by both senders |
| `src/ai_bridge/config.py` | F-04-23 cached + validated Settings |
| `src/ai_bridge/server.py` | Taxonomy-wired gate, registry tier lookup, approve_task session auth |
| `src/ai_bridge/control/mcp_routes.py` | F-06-14 policy gate on execute endpoint; code-prefixed HTTP errors |
| `src/ai_bridge/control/alerts.py` | F-05-17 / SEC-11 SSRF sender |
| `src/ai_bridge/control/worm_siem.py` | F-05-17 / SEC-11 SSRF sender |
| NEW `src/ai_bridge/control/workspace_routes.py` | Canonical module (F-04-12) |
| `src/ai_bridge/control/workspace.py` | Thin compatibility shim re-exporting `install_workspace_routes` |
| `tests/integration/test_control_plane.py:95` | approve_task caller updated (new signature + bearer-context auth) |
| NEW `tests/unit/test_architecture_phase4_errors.py` | 46 tests for this track |

## 1. FR-06 / F-06-11 — error taxonomy (`errors.py`)

`ErrorCode` (str Enum), `ERROR_CATALOG` (code → http_status + message),
`BridgeError(code, message, details)` with `.to_dict()` →
`{"code", "message", "details"}`, `error_response(code, ...)`,
`error_detail(code, ...)` → `"[CODE] message"`, `error_info()`,
`code_of_message()` (extracts a leading `[CODE]` prefix).

Codes (13): AUTH_REQUIRED 401, POLICY_DENIED 403, APPROVAL_REQUIRED 403,
TENANT_FORBIDDEN 403, TASK_NOT_FOUND 404, DEVICE_NOT_FOUND 404,
APPROVAL_NOT_FOUND 404, NOT_FOUND 404 (added — needed for "MCP Server
not found"; the task's list was a minimum), STALE_EPOCH 409,
RATE_LIMITED 429, UPSTREAM_UNAVAILABLE 502, VALIDATION_FAILED 400,
INTERNAL_ERROR 500.

Wiring:
- `server.py` `@_gated`: denials raise `PermissionError("[POLICY_DENIED] …")`
  or `"[APPROVAL_REQUIRED] …"` when `decision.needs_approval`; validation
  failures raise `ValueError("[VALIDATION_FAILED] …")`; tool exceptions are
  re-raised as the same type with the mapped code prefixed
  (PermissionError→POLICY_DENIED, ValueError→VALIDATION_FAILED,
  TimeoutError/ConnectionError→UPSTREAM_UNAVAILABLE, else INTERNAL_ERROR;
  an already-coded message is never double-prefixed). The audit record
  carries the same code. Exception *types* are preserved.
- `control/mcp_routes.py`: all HTTPExceptions via `_http_error(status,
  code, msg)` → `detail="[CODE] message"` (400s→VALIDATION_FAILED,
  404s→NOT_FOUND, policy 403s→POLICY_DENIED/APPROVAL_REQUIRED). `app.py`
  itself untouched (Track A).

## 2. F-06-14 — `enforce_mcp_tool_policy()` on the execute path

`run_mcp_tool_test()` (POST `/api/mcp/servers/{server_id}/execute`) now
calls `enforce_mcp_tool_policy(conn, dict(row), body.tool,
arguments=body.arguments, actor=actor)` BEFORE `ensure_valid_mcp_token`
and `execute_mcp_tool`. Deny → 403 POLICY_DENIED; require_approval →
403 APPROVAL_REQUIRED (detail carries the `approval_id=` from the
policy result); neither executes the tool. Both audit with decision
`"denied"`. (ai_api.py:1783 and custom_agents.py:767 already called it.)

`server.py` tier lookup: `_tool_tier()` consults
`tool_registry.get_tier()` (defensive `try/except ImportError` fallback
to `policy.TOOL_TIERS` — the registry did not exist when this track
started; Track B has since landed it and the live import is used).
All tier lookups in `_gated` and `device_aware` go through it; no second
tier list in server.py. Registry returns tier *names* (`"HIGH_RISK"`);
`_tool_tier` normalizes them back to `RiskTier` members so audit rows
keep the numeric label (`"4"`). The one remaining `TOOL_TIERS` use in
server.py is `submit_task`'s `if task_name in TOOL_TIERS` known-name
membership check, not a tier lookup — left as-is.

## 3. F-06-16 — admin token out of tool args

- `approve_task(approval_id: str, admin_token: str)` →
  `approve_task(approval_id: str)`. Auth is now `_is_admin_caller()`
  (HTTP bearer == `AI_BRIDGE_ADMIN_TOKEN`); otherwise
  `PermissionError("[AUTH_REQUIRED] …")` — fails closed in stdio, where
  approval must go through the dashboard. Unknown id →
  `ValueError("[APPROVAL_NOT_FOUND] …")`.
- `_GATE_DECIDE_EXEMPT` KEEPS `approve_task`, with the reason documented
  in the comment: `decide()` grants Tier 4 only when
  `(approval_granted AND is_admin)`, and an approval record for
  `"approve_task"` itself can never exist (approvals are granted BY
  approve_task, for the target tool) — removing the exemption would
  deadlock approval for every caller including admins. The exemption is
  safe because the session-auth check now lives inside the function.
- Stale strings updated: module comment, `_gate_tool` docstring,
  `request_approval` note, `delete_workspace_file` message (now raises
  coded `[APPROVAL_REQUIRED]`).
- Test update (`tests/integration/test_control_plane.py:95`): the call is
  now `approve_task(req["approval_id"])` with the bearer context set
  (`monkeypatch.setattr(mcp_server, "_bearer_token", lambda: ADMIN)` in a
  scoped `monkeypatch.context()`), plus an added assertion that stdio
  (no-bearer) approval raises `PermissionError`. The test was NOT
  weakened: it still proves approval unlocks the delete, one-shot
  semantics, and the host-tools gate.
  - Why not the HTTP app: the MCP `StreamableHTTPSessionManager.run()`
    can only be called once per FastMCP instance per process — a
    `TestClient(create_http_app())` lifespan in this test breaks
    `tests/unit/test_chatgpt_mcp_discovery.py::test_streamable_http_accept_and_path_normalization`
    when it runs later in the same process (verified: `RuntimeError:
    Task group is not initialized` / `can only be called once`). The
    HTTP bearer transport itself is covered by that chatgpt test; this
    test covers approve_task's session check.
- Grep for other tools taking an admin token / secret as an argument:
  - `server.py`: `approve_task` was the ONLY one.
  - `ai_api.py` TOOL_CATALOG: `approve_task` entry already listed only
    `approval_id`; no catalog tool takes a token/secret/password param.
  - `ai_api.py` `api_key` hits are internal LLM-provider helpers
    (`_post_anthropic`, `validate_llm_key`, `AIAgentIn.api_key` — a
    caller-supplied BYOK field for outbound provider calls, not an
    AI Bridge credential and not an MCP tool argument). Left untouched
    as out of scope for F-06-16.

## 4. F-04-09 — structured redaction (`security.py`)

- `is_secret_name()`: inspects ALL components, not just the filename —
  `secrets/mykey.txt`, a `secrets/` directory, `.env` at any depth, and
  private-key filenames are all refused. Scoping decision (verified
  against existing tests): components are inspected *relative to the
  workspace root* when the path lies inside it, because every caller
  passes resolved absolute paths and the prefix above the workspace
  (e.g. a pytest tmp dir named after a test containing "secret", or a
  home directory) must not false-positive. Without this scoping,
  `tests/unit/test_workspace.py::test_secret_named_file_not_readable_or_mutable`
  breaks (its tmp dir name contains "secret"). Paths outside the
  workspace fall back to checking all components (fail closed).
- `image_base64`: the blanket `_PRESERVE_KEYS` exemption is gone for
  this key. A value is preserved only if it is actually base64 image
  data: ≥256 chars, valid base64 alphabet, length % 4 == 0, and the
  decoded prefix carries an image magic (JPEG/PNG/GIF/WEBP/BMP).
  Anything else (including short base64-looking strings) is redacted.
  Empty values stay empty; `mime_type` is still preserved.
- `redact()`: new JWT pattern `eyJ…\.…\.…` (three base64url segments,
  header anchored on `eyJ`) as a fallback for free-text tokens.
  Version numbers (`1.2.3`) are not matched.
- Tests: nested dicts/lists, `secrets/` paths, JWT strings, real JPEG
  base64 preserved vs junk redacted.

## 5. F-05-17 / SEC-11 — SSRF senders

Shared `validate_outbound_url()` in `security.py`, called by both
senders BEFORE connecting: http(s) only, hostname present, no embedded
credentials, DNS-resolves every A/AAAA answer, rejects
private (RFC1918), loopback, link-local (incl. 169.254.169.254 metadata),
CGNAT 100.64/10, unspecified, multicast, reserved, IETF-assignment and
documentation ranges. Both senders use `follow_redirects=False` and
manually follow at most 3 redirects, re-validating each target.
- `alerts.py::_send_http_json`: validation failure → log + `False`;
  redirect to a blocked target → `False`.
- `worm_siem.py::emit_audit_event`: validation failure → error dict
  (or raise when `AI_BRIDGE_WORM_SIEM_REQUIRED=1`, mirroring the
  existing transport-error behaviour). Still uses `httpx.post` so the
  existing `patch("httpx.post")` test keeps working.
- Documented carve-out: 198.18.0.0/15 (benchmark range) is NOT blocked.
  It is non-routable on the public internet, but this environment's DNS
  wildcard resolves every hostname into it, so blocking it would break
  all webhook delivery here — including the existing
  `test_worm_siem_emit_posts` host (`siem.example` → 198.18.150.82,
  `is_private=True`). The primary SSRF targets are all blocked.
  Revisit if webhooks ever run where 198.18/15 is routed.
- Tests mock `socket.getaddrinfo` (private vs public) and the httpx
  transport; no real network calls to private IPs.

## 6. F-04-12 — workspace name collision

DONE, low-risk path taken: `control/workspace_routes.py` is now the
canonical module (implementation moved verbatim, docstring updated);
`control/workspace.py` is a thin shim re-exporting
`install_workspace_routes`, `device_summary`, `UserUpdate`,
`DeviceMetadata`. `control/app.py`'s import (`from .workspace import
install_workspace_routes`, function-level inside `create_app`) is
unchanged (Track A owns app.py). Verified: only app.py imports it;
no import cycle (`workspace_routes` imports only fastapi/pydantic/db).

## 7. F-04-23 — cached/validated Settings (`config.py`)

- `get_settings()` is cached via `functools.lru_cache` on an internal
  `_build_settings(fingerprint)` keyed on the exact env vars Settings
  reads — repeated calls with unchanged env return the identical
  object; an env change yields a fresh object. `reset_for_tests()`
  clears the cache; `get_settings.cache_clear` is also exposed (the
  existing `tests/unit/test_phases_456_deep.py` already calls it).
  The fingerprint key (rather than a bare cache) is deliberate: several
  existing tests `monkeypatch.setenv(...)` then call code that reads
  Settings, and a bare cache would serve stale values and break them.
- `_int()` now raises `ValueError` with a clear message on non-numeric
  or out-of-range values instead of silently defaulting. Ranges:
  max_read_bytes/max_output_bytes > 0, max_list_entries > 0,
  default/max timeout 1..3600, max_concurrent_tasks 1..64,
  control_api_port 1..65535.
- `expandvars` removed from workspace/data paths (`expanduser` only);
  documented in the module docstring: `$VAR` in a path stays literal,
  export the fully-expanded path instead.

## 8. F-05-18 — BLOCKED (regex SQL translation, file ownership)

The regex → PostgreSQL SQL translation (`to_pg_sql` in
`control/db.py:230`) is OUT OF SCOPE for this wave: `db.py` is not in
any track's file list. NOT touched. Recorded here as BLOCKED (file
ownership) per instructions.

## 9. Stale docstrings

Updated in `server.py` (approval-store comment, `_gate_tool` docstring,
`_GATE_DECIDE_EXEMPT` comment, `_gated` docstring, `approve_task`
docstring, `request_approval` note, `delete_workspace_file` message),
`config.py` (module docstring), `control/workspace_routes.py`
(canonical-name note), `alerts.py`/`worm_siem.py` (SSRF notes).

## Test results (targeted only; full suite is the parent's)

New file `tests/unit/test_architecture_phase4_errors.py`: **46 passed**.

Targeted regression set (11 files + new file, 114 tests):
`test_control_plane.py` (integration), `test_p0_mcp_gate.py`,
`test_security.py`, `test_security_hardening.py`, `test_workspace.py`,
`test_phases_456_deep.py`, `test_observability_alerts.py`,
`test_residuals_security_platform.py`, `test_mcp_oauth2.py`,
`test_mcp_admin_fleet.py`, `test_chatgpt_mcp_discovery.py`,
`test_architecture_phase4_errors.py` →
**112 passed, 2 failed**.

The 2 failures are environmental and pre-existing, not caused by this
track (verified): `test_phases_456_deep.py::test_connectors_fail_without_api_key`
and `::test_dispatch_registers_firecrawl_and_browserbase_tools` fail
because the sandbox's `NO_PROXY` contains `[::1]`, which httpx's proxy
parsing rejects (`InvalidURL: Invalid port: ':1]'`) when constructing
`httpx.Client` in `connectors/firecrawl.py` — a file this track never
touched. Both pass with `NO_PROXY` unset (`env -u no_proxy -u NO_PROXY
pytest …` → 2 passed).

Also found while verifying (not a regression): the MCP HTTP app's
session manager can only run once per process — see §3 above. Any
future test that opens a second `TestClient(create_http_app())`
lifespan in the same pytest process will fail at startup; that slot is
currently owned by `test_chatgpt_mcp_discovery.py`.

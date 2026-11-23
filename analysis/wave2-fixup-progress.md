> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 2 fix-up progress — dashboard tests, stream tickets, DPAPI readers, signed installers, v1/mcp_servers leftovers

Repo: `/home/hatch/workspace/ai-bridge-main`, on top of git commit `3712b81`
(Wave 2 committed). **No commit made by this wave; no reset/rebase.**
Work: dashboard-test fix-up (child agent), everything else (parent agent,
this file's author). Read `analysis/wave2b-progress.md` (blockers §) and
`analysis/wave2a-progress.md` §5 first, as briefed.

## Status summary

| Task | Status | Evidence |
|---|---|---|
| 1. Dashboard 23 failing tests | COMPLETE — `npm test` exit 0, 430/430 + 3/3 i18n | Child report below; full log in `analysis/wave2-fixup-dashboard-tests.md` |
| 2. D-08 stream ticket (backend + WS + frontend) | COMPLETE — 11/11 new Python tests pass; dashboard builds | `tests/unit/test_stream_ticket.py`; `npm run build` exit 0 |
| 3. F08-08 follow-up: C++ DPAPI unsealing readers | COMPLETE (inspection-verified only — no Windows toolchain here) | Brace/include consistency checks below; no Windows build/run possible in this Linux VM |
| 4. Magic-installer signed updates | COMPLETE — re-runs verify + pin, or refuse | `bash -n` OK on both installers; embedded Python `py_compile` OK; no executable unsigned checkout remains |
| 5a. v1 device-authenticated routes → `require_device_mtls` | COMPLETE with a documented deviation (see §5a) | Offer/ice/WS-device paths gated; operator registry routes deliberately NOT gated |
| 5b. `SELECT * FROM mcp_servers` → explicit loaders | COMPLETE | `grep` 0 hits; loaders in `control/db.py` |
| Full Python suite | 513 passed, 12 failed (all 12 pre-existing, verified on clean 3712b81) | `pytest tests/ -q`: 513 passed / 12 failed / 5 skipped |
| Full dashboard suite | 430/430 + 3/3 i18n, exit 0; build exit 0 | `npm test`, `npm run build` in `apps/dashboard` |

## 1. Dashboard 23 failing tests (child agent)

Full per-failure log: `analysis/wave2-fixup-dashboard-tests.md`.

- Before: `npm test` exit 1 — 407/430, 23 failed (i18n never reached).
- After: exit 0 — 430/430 + 3/3 i18n (433 total); `npm run build` exit 0
  ("✓ built in 8.27s"); standalone challenger-stress 15/15, M1.2 27/27,
  Tier 5 31/31. No test deleted; test counts unchanged.
- Files changed (child only): `apps/dashboard/src/tests/challenger-stress.test.ts`,
  `challenger-m1-2-stress.test.ts`, `tier5-adversarial.test.ts`,
  `apps/dashboard/src/views/Device360View.tsx`. `api.ts` /
  `LiveDesktopView.tsx` untouched by the child (`git diff` empty there at
  handover, so the stream-ticket switch in §2 did not collide).
- Decisions:
  - 18 demo-gating failures + Challenger 4.1 listener failure (21 tests) —
    expectation updated to the D-01 login-first contract (in Node there is
    no `window`/credentials/demo-ack, so demo-view dispatches resolve to
    `login`; a `window`+`sessionStorage` shim was considered and rejected
    because it would break the Tier 1 F3.3 negative contract).
  - 2 `decidedBy` failures — expectation updated: `resolveApproval()`
    stamps `decidedBy: "operator:admin"` (`dashboardStore.ts:1233`,
    verified); `operator:sarah.j` is only the seed request invoker.
  - 1 Device360View failure — REAL BUG, component fixed: telemetry was
    already `selectedDevice?.telemetry`, but render dereferenced
    `selectedDevice.platform/hostname/arch/status/...` unguarded, so
    `devices === []` throws. Added an `if (!selectedDevice)` early return
    (after all hooks) rendering a "No device available" empty state with
    a "View fleet inventory" button. Test 4.6 rewritten to assert the
    guard (it previously asserted the bug exists).
  - 1 font-mono failure — test corrected, decided from code: `LoginView.tsx`
    is a sign-in screen with no operational metrics/codes, so the invariant
    does not apply; `OverviewView.tsx` renders metrics in mono via shared
    `ProductMetricCard`/`ProductModePill` (`ui.tsx:87,224`). Test 5.7 now
    excludes LoginView as non-operational.
- Caveat (child): the positive D-01 path (explicit "Explore demo
  workspace" entry → demo navigation succeeds) needs a real browser
  `sessionStorage` and cannot be exercised in Node; covered only by the
  Tier 1 F3.3 negative contract.

## 2. D-08 stream ticket (Wave 2B blocker 1) — CLOSED

Files changed:
- `src/ai_bridge/control/db.py` — `stream_tickets` table added to `SCHEMA`
  (SQLite path) plus `ensure_stream_tickets_table()` for databases predating
  the SCHEMA entry (PostgreSQL, whose schema comes from migrations);
  `mint_stream_ticket()` / `redeem_stream_ticket()` helpers,
  `STREAM_TICKET_TTL_S = 60`. Only the SHA-256 hash is stored; the ticket
  is returned once at mint and never logged. Redeem is atomic
  (`conn.transaction()`: SELECT unused → match device+session+expiry →
  UPDATE used=1 WHERE used=0, rowcount-checked); a wrong-device /
  wrong-session / expired / replayed presentation returns `None` without
  consuming the ticket.
- `src/ai_bridge/control/app.py` — `POST /api/stream-ticket`
  (`StreamTicketIn{device_id, session_id}`), authenticated via
  `_auth_context_full` (viewer+), tenant-scoped via
  `_require_device_tenant` (404s cross-tenant devices, R-P0-12). Audits
  `stream_ticket_minted` without the ticket value.
- `src/ai_bridge/control/v1_routes.py` — `v1_session_websocket` accepts a
  `ticket` query parameter and redeems it first (session loaded via
  `load_webrtc_session_db`, bound to the session's `device_id`); invalid /
  expired / replayed tickets close the socket with 4401. Long-lived
  tokens in the URL remain accepted for non-dashboard WS clients (e.g.
  other tooling), but the dashboard no longer sends them.
- `apps/dashboard/src/api.ts` — `wsAuthQuerySuffix` (session-token-in-URL)
  deleted; new `mintStreamTicket(deviceId, sessionId)` (POST
  `/api/stream-ticket` via `call()`, token sent as an `X-Session-Token`
  header) and `sessionWebSocketUrl(sessionId, ticket?)`, which appends
  `?ticket=…` only when a ticket is supplied. Kept the builder sync so
  the existing tests (`sessionWebSocketUrl('s1')` → bare `ws://…` URL,
  no `token=`) still pass unchanged.
- `apps/dashboard/src/views/LiveDesktopView.tsx` — `handleStartWebrtc`
  mints a ticket for the selected device/session before opening the WS
  (`await api.mintStreamTicket(selectedDevice.id, sid)`); on mint failure
  it surfaces a notice and skips the WS channel (the data channel still
  carries input), instead of silently falling back to a token URL.
- `tests/unit/test_stream_ticket.py` (new, 11 tests) — mint returns a
  ticket bound to device+session with `expires_in == 60` and only the
  hash in the DB; mint requires auth (401), CSRF token (403 without it),
  and device+session (400); redeem once then second use rejected;
  expired rejected (expiry forced via direct UPDATE); wrong device
  rejected and the ticket survives for a correct redeem; wrong session
  rejected; unknown ticket rejected; WS connect with a ticket receives
  the session snapshot and a replayed ticket is rejected with 4401;
  WS connect with a bogus ticket is rejected.

CSRF finding during implementation (pre-existing, fixed in passing):
the control plane requires `X-CSRF-Token` on mutating session-token
requests, but the dashboard never sent one — so every
session-authenticated POST (including the ticket mint) was 403. `call()`
in `api.ts` now lazily fetches `GET /api/auth/csrf-token` once per tab
and attaches `X-CSRF-Token` on non-GET session-token calls; the cache is
cleared by `clearCredentials()`. Admin-token calls are exempt server-side
and unaffected. No dashboard test mocks `fetch` or calls `call()`, so
this is test-safe (verified: suite still 430/430).

Verification: `pytest tests/unit/test_stream_ticket.py -q` → 11 passed;
`npm test` → 430/430 + 3/3 i18n, exit 0; `npm run build` → exit 0.
`grep session_token= apps/dashboard/src/api.ts apps/dashboard/src/views/LiveDesktopView.tsx`
→ 0 hits.

Follow-ups (not done here): a canonical migration `014_stream_tickets.sql`
for PostgreSQL parity (the lazy `ensure_*` covers runtime, but the
migration set is now one table behind SCHEMA); removing the legacy
long-lived-token query fallback from the WS endpoint once all WS clients
are confirmed ticket-capable.

## 3. F08-08 follow-up: C++ DPAPI unsealing readers (Wave 2A blocker) — CLOSED (inspection-only)

Files changed:
- `windows-agent/src/security/local_security.hpp` — `kSealedPrefix =
  "aib-dpapi-v1:"`, `IsDpapiSealed()`, `UnsealIfSealed()` (strip prefix →
  base64-decode → `UnprotectSecret`; passes non-sealed values through;
  returns false with empty output on any failure; documented as
  must-fail-loud, never fall back to the sealed string).
- `windows-agent/src/security/local_security.cpp` — strict RFC 4648
  base64 decoder (new; none existed in the tree) in an anonymous
  namespace, plus the two methods. The installer seals with .NET
  `ProtectedData` LocalMachine scope and the Python agent unseals the
  same format (`agents/windows/app_runtime.py` `unseal_token()`); scope
  does not matter to `CryptUnprotectData`.
- `windows-agent/src/service/windows_service.cpp` — `LoadServiceConfig`
  now returns `bool` and unseals the pipe secret from all three sources
  (config `pipe_secret`, `AI_BRIDGE_PIPE_SECRET` env, `ProgramData\ai-bridge\native-pipe.secret`
  file — the installer seals all of them); `main()` exits 1 with a
  `FATAL` stderr message if unsealing fails, before `--console` status
  output or the service dispatcher starts.
- `windows-agent/src/desktop/desktop_agent.cpp` — after config load,
  `device_token` and `pipe_secret` are unsealed (a `--device-token`
  passed on the CLI is unsealed too if it carries the prefix; plaintext
  passes through); any failure prints `FATAL` and returns 1 before
  `RegisterWithSupervisorPipe` or any signalling use.

Verification (honest): NO Windows toolchain exists in this Linux VM, so
nothing was compiled or run. Checked by inspection only: brace counts
balanced in all four files; all needed includes present (`<cstring>` for
`std::strlen`, `<iostream>` for `std::cerr` in both readers, `<vector>`/
`<cstdint>` via the header); `static constexpr` member is fine under the
project's C++20; both binaries already link `local_security.cpp`
(`windows-agent/CMakeLists.txt:115-131`); no existing API changed, only
additions; the only other `device_token`/`pipe_secret` consumers
(`webrtc/http_signaling.cpp`, `peer_connection.cpp`) receive the already
unsealed values from `desktop_agent` main. **A Windows build + a
sealed-config end-to-end run are still required before deploying the
sealed installer to native fleets** — record this as the remaining
verification step, not as done.
Note: `windows-agent/installer/install-service.ps1` still carries a NOTE
saying the C++ readers "currently read … as plaintext JSON and never call
UnprotectSecret" — that comment is now stale (file outside this wave's
scope; update it when the installer is next touched).

## 4. Magic-installer signed updates (Wave 2A §5 follow-up) — CLOSED

Files changed: `magic-install.sh`, `deployment/linux/magic-install.sh`
(identical change in both; they differ only in the Python-version header
line and a trailing newline).

- `provision_codebase()` re-run branch (existing `$INSTALL_DIR`) no longer
  runs `git fetch origin main` / `git reset --hard origin/main`. It now
  requires the installed venv Python (`$INSTALL_DIR/.venv/bin/python3`)
  and runs `ai_bridge.updates.load_signed_manifest()` (Ed25519 signature
  verification, BEFORE any checkout) followed by
  `apply_pinned_git_update()` (pinned-commit checkout, HEAD verification,
  rollback snapshot) with the manifest/signature/public key from
  `AI_BRIDGE_UPDATE_MANIFEST_PATH` / `AI_BRIDGE_UPDATE_SIGNATURE_PATH` /
  `AI_BRIDGE_UPDATE_PUBLIC_KEY`.
- If the venv Python is missing, the signed update is not configured, the
  signature fails, or the helper cannot run (e.g. `cryptography`
  missing), the installer REFUSES the update (`log_error` + `exit 1`,
  install dir left unchanged) rather than applying unsigned code.
  Fresh clones are unchanged (initial bootstrap clone is inherent).
  Header comment updated to state re-runs are signed-only.

Verification: `bash -n` exit 0 on both files; the embedded Python
snippet extracted and `py_compile`d OK; `grep` for executable unsigned
`git fetch origin main` / `git reset --hard` → only the explanatory
comment at line 341 remains. Not live-tested (no second machine / no
signed release manifest exists yet); a dry run with a test keypair +
manifest is the remaining verification step.

## 5. Small leftovers from Wave 2A §5

### 5a. v1 device mTLS — implemented for device-authenticated routes, with a documented deviation

Files changed: `src/ai_bridge/control/v1_routes.py`.

- `require_device_mtls` now runs on every v1 route where the caller
  authenticates AS the device (presents `X-Device-Token`):
  `POST /api/v1/agents/{device_id}/heartbeat` (already gated in Wave 2A),
  `POST /api/v1/sessions/{session_id}/offer`, `POST
  /api/v1/sessions/{session_id}/ice`, and the WebSocket
  `/api/v1/ws/sessions/{session_id}` (mTLS failure closes with 4401 via
  the existing `HTTPException` handler). Offer/ice gained `request` +
  fingerprint header params mirroring the heartbeat. `require_device_mtls`
  is a no-op unless `AI_BRIDGE_DEVICE_MTLS=1`, so default behaviour is
  unchanged.
- DEVIATION (deliberate, parent to overrule if desired): the
  operator-facing device registry routes (`GET /api/v1/devices…`,
  `/status`, `/monitors`, `/ui-elements`, `/files…`) were NOT gated.
  Those callers are human operators presenting a session/admin token,
  not a device certificate: when `AI_BRIDGE_DEVICE_MTLS=1`,
  `require_device_mtls` would 403 every dashboard device read (no
  fingerprint is presented by a browser, and none is enrolled for the
  operator), breaking the dashboard. Device identity binding is instead
  enforced on the device-authenticated paths above, which is where a
  stolen device token could be abused. Applying the literal wording
  ("v1 device routes other than heartbeat") to operator routes would be
  a regression, not a fix.

### 5b. `SELECT * FROM mcp_servers` → explicit loaders — DONE

Files changed: `src/ai_bridge/control/db.py` (new loaders — the only
addition to `db.py` beyond the stream-ticket work in §2),
`src/ai_bridge/control/ai_api.py`, `src/ai_bridge/control/custom_agents.py`.

- `db.MCP_SERVER_COLUMNS` names every column explicitly;
  `list_enabled_mcp_servers(conn, tenant)` and
  `get_mcp_servers_by_ids(conn, ids)` replace the two `SELECT *` sites.
  Both callers need the full server dict (url, auth tokens, oauth fields
  via `ensure_valid_mcp_token`, headers/tools JSON), so all current
  columns are listed; the point is that a future schema change —
  especially a new secret-bearing column — cannot silently leak into the
  MCP dispatch dicts. `grep -rn "SELECT * FROM mcp_servers" src/` → 0
  hits. (Other tables' `SELECT *` — devices, approvals, tasks,
  custom_agents — were out of scope for this task.)

### NOT attempted (per brief — Wave 3)

- Async-native rate limiter (DIST-07) and connection pool (PERF-01):
  untouched, as instructed.

## 6. Test results

- New: `pytest tests/unit/test_stream_ticket.py -q` → **11 passed**.
- Full Python: `.venv/bin/python -m pytest tests/ -q` → **513 passed,
  12 failed, 5 skipped** (118.9 s). Baseline (Wave 2): 502 passed / same
  12 failures → 513 = 502 + 11 new, zero new failures.
- The 12 failures were proven pre-existing: the same 12 tests were run
  on clean commit `3712b81` via `git stash` (12 failed, 13 passed in
  that subset) and the stash was popped cleanly with all fix-up changes
  restored. Failure causes are environmental and unrelated to this
  wave's files: `ModuleNotFoundError: No module named 'PIL'` (2),
  xdotool missing on Linux / Windows-only expectations (4),
  `No module named ruff` (1), Firecrawl/connector key parsing (3),
  legacy WebRTC deprecation header (1), HTTP-client reuse (1).
- Dashboard: `npm test` → **430/430 + 3/3 i18n, exit 0**;
  `npm run build` → **exit 0** ("✓ built in 8.16s").
- `py_compile` on all edited Python files → OK.

## 7. Rollback

`git checkout -- <file>` per file (no commit made). Full working-tree
diff at handover: 18 files, +596/−94 (includes the dashboard child's
3 test files + Device360View). Untracked: `tests/unit/test_stream_ticket.py`,
`analysis/wave2-fixup-dashboard-tests.md`, this file.
To roll back only this wave's backend work:
`git checkout -- src/ai_bridge/control/app.py src/ai_bridge/control/db.py src/ai_bridge/control/v1_routes.py src/ai_bridge/control/ai_api.py src/ai_bridge/control/custom_agents.py windows-agent/src/service/windows_service.cpp windows-agent/src/desktop/desktop_agent.cpp windows-agent/src/security/local_security.cpp windows-agent/src/security/local_security.hpp magic-install.sh deployment/linux/magic-install.sh`
and delete `tests/unit/test_stream_ticket.py`.

## 8. Remaining / blocked / deferred

- Windows build + sealed-config E2E run (§3) — needs a Windows machine
  with the C++ toolchain; cannot be done in this Linux VM.
- Signed-installer dry run with a test keypair + manifest (§4) — needs a
  scratch VM; no signed release manifest exists yet.
- Canonical migration `014_stream_tickets.sql` for PostgreSQL parity (§2).
- WS long-lived-token query fallback (§2) — remove once all WS clients
  are confirmed ticket-capable.
- `install-service.ps1` stale F08-08 NOTE (§3) — comment-only update.
- Positive D-01 demo-entry path needs a real browser (§1).
- DIST-07 async rate limiter, PERF-01 connection pool — Wave 3, per brief.

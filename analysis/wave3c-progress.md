> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 3C — Agents reliability, WebRTC/native P2s, MCP client, Phase 5 foundation — Progress

Date: 2026-11-17. Repo: `/home/hatch/workspace/ai-bridge-main`, base commit
`19747b2`. No commit made. No packages installed. No reset/rebase.
**Scope note:** other waves were editing the same tree concurrently
(`control/db.py`, `control/migrations.py`, `control/supervisor.py`,
`control/fleet_health.py`, `migrations/014_*`, `migrations/015_*`,
`scripts/check_schema_parity.py` in `git status` are sibling-wave work,
not Wave 3C). Wave 3C's own change set is listed in §6 below.

Per-track detail lives in:
- `analysis/wave3c-agents-progress.md` (Track A — agent reliability)
- `analysis/wave3c-webrtc-progress.md` (Track B — WebRTC + native Windows)
- `analysis/wave3c-mcp-progress.md` (Track C — MCP client / shell / browser / SDK / Phase 5)

## 1. Status summary

| Task | Finding(s) | Status | Evidence |
|---|---|---|---|
| Spool DLQ + cap + TTL | F07-08 | DONE | `agents/common/spool.py`; 23 new tests pass |
| Graceful shutdown / drain | F07-09 | DONE | `agents/common/poller.py` + 3 entrypoints; drain tests pass |
| Independent heartbeat timer (+ worker thread) | F07-10 (+F07-07) | DONE | `agents/common/poller.py`; heartbeat-fires-while-hung test passes. **Correction:** F07-07's worker-thread model was NOT in the code at 19747b2 (Wave 1B did not implement it); implemented here. |
| 401 revocation vs transient | F07-12 | DONE | `agents/common/poller.py`; classification + backoff tests pass |
| Agent progress heartbeat | DIST-08 | DONE (agent side) | `agents/common/poller.py`; contract in §2 below. Server endpoint does NOT exist yet — Wave 3A/4 follow-up. |
| Gate `simulate_agent_intent` | F13-06 | DONE | `webrtc_stream.py`; off by default, `AI_BRIDGE_DEV_SIMULATE_INTENT=1` only |
| Real RTCP ABR / honest labelling | F13-07 | DONE | `webrtc_stream.py`; `abr_source="none"` when no stats; nothing fabricated |
| Mouse-held release (fallback) | F13-08 | DONE | `webrtc_stream.py`; extends Wave 1B key release to buttons |
| Session reconciliation after restart | F13-09 | DONE (agent side) | `webrtc_stream.py`; stale ids refused. Plane-side row expiry is a follow-up. |
| ICE-complete before offer | F13-10 | DONE | Python + C++ gates; no offer on timeout |
| C++ WebRTC: ICE candidates, TURN, persistent signalling | F13-05 / F08-13 | DONE (code), **NOT COMPILED** | See §4 caveat |
| Secure-desktop detection unified | F08-09 | DONE | One rule both languages; probes cached (2 s) |
| Monitor geometry in C++ input | F08-10 | DONE (code), NOT COMPILED | `peer_connection.cpp` |
| Per-iteration monitor construction | F08-11 | DONE (code), NOT COMPILED | Persistent monitor + 5 s enum cache |
| Single-instance guard | F08-12 | DONE (code), NOT COMPILED | Named mutexes, desktop agent exits 3, service exits 1 |
| Real C++ tests (CHECK, not assert) | F08-14 | DONE (code), NOT RUN | `test_windows_agent.cpp` §[7/7] |
| Hand-rolled JSON | F08-15 | KEPT, justified in code | No vendored JSON lib; adding one unverifiable on Windows is higher risk |
| StartSession honest result | F08-16 | DONE (code), NOT COMPILED | Encoder failure now returns false |
| Proper MCP client | F-06-15 | DONE | `control/mcp_client.py`; initialize/session/pagination, legacy fallback |
| DPoP / PAR | F-06-13 | EVALUATED, NOT IMPLEMENTED (deliberate) | Gateway has no PAR endpoint / DPoP support (grep-verified); client half alone would be theatre |
| One shell contract | F-04-07 | DONE | `terminal_manager.py` `ShellSpec` + `run_shell_command` |
| SIGKILL escalation | F-04-11 | DONE | `terminal_manager.py`; SIGTERM → grace → SIGKILL group. `execution.py`/`tools.py` still SIGTERM-only — later track. |
| Bounded terminal sessions | F-04-16 | DONE | `terminal_manager.py`; max 32, idle TTL 1800 s, eviction |
| Browser sandbox or fail closed | F-04-14 | DONE | `browser_isolation_os.py`; requested-but-unavailable raises; bwrap hardened; honest `seccomp: "none"` |
| SDK token handling | F-04-19 | DONE | `client.py`; token never in URLs, redacted in errors/repr |
| Signed updates in-function | F-04-20 | DONE (gate added; no update path exists in these files) | `policy_simulation.require_signed_update()` / client gate delegate to `updates.load_signed_manifest` |
| Locked browser context + URL allowlist | F-04-13 | DONE | `browser_tools.py`; per-session contexts, evaluate allowlist, non-http(s) rejected |
| OCR hardening | F-04-17 | DONE (here) | **Correction:** Wave 1 had NOT done this (verified absent at 19747b2); fixed in `vision.py` — random 0600 temp path, `RemoteSigned`, redaction |
| Policy simulation | FR-01 | DONE | New `src/ai_bridge/policy_simulation.py`; dry-run only, `executed: False`. No control endpoint — Wave 4 follow-up. |
| Task result projection | FR-03 | DONE (client half) | `client.py project_task_summaries()`; server-side column projection is a Wave 4 follow-up (needs `control/app.py`) |
| SDK retry + idempotency | FR-07 | DONE | `client.py`; bounded retries, backoff + jitter, `Idempotency-Key` on `execute()` |
| Explicit demo-mode flag | FR-05 | DONE (backend half) | `policy_simulation.is_demo_mode()`; explicit opt-in only, never default. Dashboard out of scope. |
| Error taxonomy (small half) | FR-06 | DONE | `MCPErrorCode` / `AIBridgeError.code` |
| Fleet analytics module | — | NOT CREATED (deliberate) | FR-03 projection lives coherently in `client.py`; real fleet analytics (FR-13) deferred until PERF-04/DB-07/PERF-01 |

## 2. DIST-08 wire contract (agent side implemented; server side pending)

No Wave 3A progress file existed when this wave started, so the agent
implements the contract from DIST-08 / roadmap §3:

- **Method/path:** `POST /api/tasks/{task_id}/progress`
- **Payload:** `{"task_id", "progress_pct": number 0–100 | null, "status_message": string | null, "epoch": int optional}` — `null` progress means "alive, no measurable %".
- **Semantics:** liveness only; plane must bump `tasks.updated_at` iff `status='running'` (reuse `record_task_progress`), never change `status`; epoch mismatch → 409; not-running → 409 (prefer 409 over 404 — the agent treats 404/501 on this path as "endpoint not implemented" and disables progress for the run).
- **Agent behaviour:** heartbeat timer thread reports at most once per 60 s while a task runs; 404/501 logged once then disabled for the run; transient failures retried. Long tasks remain protected meanwhile by the existing execution-state poll (which bumps the task row via `record_task_progress`).
- **Namespace caveat:** `/api/tasks/*` is operator/session auth today; agent routes live under `/api/agent/*`. The server follow-up should either expose the prescribed path with device-token auth or add `POST /api/agent/tasks/{task_id}/progress` (agent to be updated to the alias if chosen).
- **Server follow-up also needed:** `record_task_progress` currently appends a `task_events` row per 0.5 s execution-state poll — throttle server-side (e.g. only when > 60 s since last progress event).

Full contract: `analysis/wave3c-agents-progress.md` §5.3.

## 3. Test results

New test files (each track ran its own file only):
- `tests/unit/test_agent_reliability.py` — **23 passed**
- `tests/unit/test_webrtc_phase3.py` — **22 passed**
- `tests/unit/test_wave3c_sdk.py` — **36 passed** (+ 79 passed across that file and 4 neighbouring suites in one combined run)

Neighbour checks: 28 passed / 2 skipped (agent neighbours); no new
failures introduced by any track. Pre-existing failures found during
neighbour checks (verified by stashing to pristine 19747b2):
firecrawl httpx URL error, missing `ruff`, two phase-1 approval
lifecycle tests, one order-dependent fleet-health flake — all fail on
pristine code too, not regressions. One failure introduced by Track C
(`test_ai_bridge_client_sdk` mock vs new `_request` kwarg) was fixed
without touching the test.

### Full suite

Clean comparison was done in isolated git worktrees at 19747b2 (NOT in
the main tree, for two reasons documented below):

- **Baseline (pristine 19747b2): 12 failed, 513 passed, 5 skipped**
  (517.54 s) — exactly the Wave 2 fix-up baseline. The 12 failures are
  the known pre-existing set (see `baseline-failed.txt` in the run
  notes; includes `test_firecrawl_crawl_requires_key`,
  `test_subprocess_linter_runs_cross_platform` (no `ruff`),
  `test_webrtc_disconnect_releases_held_keys`,
  `test_http_client_reused_across_api_calls`).
- **Patched (19747b2 + Wave 3C only): 13 failed, 593 passed, 5 skipped**
  (507.48 s). All 81 new tests pass (23 agent reliability + 22 WebRTC
  phase 3 + 36 SDK). Net: +80 passed, +1 failed.

The one new failure is `tests/unit/test_phase8_deep.py::
test_concurrent_claim_and_cancel_under_load` — a wall-clock assertion
`elapsed < 5.0` (observed 5.09 s in the suite run). **It is NOT a Wave
3C regression, evidence:**
- The test exercises only control-plane code (`create_app`,
  `/api/devices/enroll`, `/api/tasks`, `/api/agent/tasks/next`,
  cancel/result). Wave 3C touched none of it; its only 3C-adjacent
  import is `browser_tools` at module import time, outside the timed
  section.
- Re-run in isolation on **pristine** 19747b2: pass, pass, **fail**
  (1 of 3). Re-run on patched code, interleaved with pristine runs:
  patched 7.06 s fail, pristine 6.52 s fail — both fail with similar
  magnitude when the host is loaded. Host load average was 1.51 on a
  1.5-shared-CPU VM (saturated). The test only meets its 5.0 s bound
  when the host is quiet.
- The test was NOT modified and its bound was NOT loosened (out of
  Wave 3C scope; masking a timing test is not a fix). Recorded here
  as a known-flaky timing test on saturated hosts; a later track may
  raise the bound or mock the clock.

**Why the main-tree full runs were discarded:** two full-suite runs in
the main tree gave 40 failed / 515 passed / 51 errors and then 141
failed — both invalid. Root cause found: **`/tmp` (512 MB tmpfs) was
100% full**, so pytest `tmp_path` fixtures failed with `OSError`,
`.pyc` writes truncated (`EOFError: marshal data too short`), and
pytest itself exited 120 with empty logs. The full /tmp was caused by
concurrent sibling waves' test runs plus worktrees placed on /tmp.
The clean comparison above used `TMPDIR` on the home disk and a
worktree on the home disk, and reproduced the published baseline
exactly, which validates the method. No main-tree full-suite number
from this wave should be quoted.

No baseline failure was fixed by Wave 3C (the 12 pre-existing failures
are all in areas outside this wave's scope, except
`test_webrtc_disconnect_releases_held_keys`, which still fails in the
patched run: `release_all_held_keys()` returns 0 on this Linux host —
there is no Win32/DLL release to perform — so its `assert
released_count >= 2` fails. Pre-existing at baseline, and unrelated to
the F13-08 button-tracking change, which releases the same way.)

## 4. Honest caveats — what is NOT verified

- **C++ was not compiled or run.** No Windows toolchain on this host
  (no MSVC/clang-cl, no Windows SDK). All `windows-agent/` changes were
  verified by inspection only (tokenizer brace-balance 0/0/0 on all edited
  files; declaration↔definition↔call-site symbol checks). A Windows build
  + `ai_bridge_tests` run is REQUIRED, especially to confirm the
  libdatachannel API names used (`onGatheringStateChange` /
  `gatheringState`, `IceServer::username`/`password`,
  `addRemoteCandidate`). Recorded in `wave3c-webrtc-progress.md` §0.
- **No live aiortc peer was run** (aiortc not installed here);
  `_poll_rtcp_stats()` is unit-tested with a fake sender only.
- **No live-plane E2E run** was performed (progress contract, drain
  under systemd, TURN/symmetric-NAT, single-instance launch).
- **Dashboard still sends `simulate_agent_intent`**
  (`LiveDesktopView.tsx:425`) — the agent now refuses it by default,
  but the dashboard should stop sending it (dashboard-owning wave).

## 5. Follow-ups for other waves

1. Server: implement `POST /api/tasks/{task_id}/progress` per §2 (or the
   `/api/agent/...` alias); throttle `record_task_progress` (0.5 s event flood).
2. Server (optional): split the `"Unknown or revoked"` 401 detail into
   distinct `unknown` vs `revoked` signals.
3. Windows host: build C++ agent + run `ai_bridge_tests` (§[7/7]); confirm
   libdatachannel API names; verify single-instance guard by double launch;
   live TURN/symmetric-NAT E2E.
4. Dashboard: stop sending `simulate_agent_intent`.
5. Later track: `execution.py:TaskRunner._kill` and `tools._run_argv`
   should adopt the SIGTERM → grace → SIGKILL group escalation.
6. Service units: systemd `Restart=always` will restart-loop a revoked
   agent (exit 2) — treat exit 2 as "needs re-enrollment, do not restart".
   Same for Windows scheduled task / macOS launchd.
7. Windows GUI `_poll_loop()` (F07-03) does not inherit the worker
   thread / heartbeat timer / drain / 401 handling — rebase it onto
   `Poller` in its own track.
8. Wave 4: FR-01 simulation endpoint + FR-03 server-side column
   projection (both need `control/app.py`).
9. Tracker correction: roadmap's "Wave 1" attribution for F-04-17
   appears wrong — Wave 1 progress files do not record it; it was
   fixed in this wave.
10. DPoP/PAR only when the OAuth gateway gains a PAR endpoint and DPoP
    verification.

## 6. Files changed (Wave 3C only)

- `agents/common/spool.py`, `agents/common/poller.py`,
  `agents/linux/agent_main.py`, `agents/macos/agent_main.py`,
  `agents/windows/agent_main.py`
- `src/ai_bridge/webrtc_stream.py`, `src/ai_bridge/screen_capture.py`
- `windows-agent/src/c_api.cpp`,
  `windows-agent/src/capture/dxgi_capture.cpp/.hpp`,
  `windows-agent/src/desktop/desktop_agent.cpp`,
  `windows-agent/src/input/sendinput_controller.cpp/.hpp`,
  `windows-agent/src/service/windows_service.cpp`,
  `windows-agent/src/webrtc/http_signaling.cpp/.hpp`,
  `windows-agent/src/webrtc/peer_connection.cpp/.hpp`,
  `windows-agent/src/webrtc/webrtc_transport.cpp/.hpp`,
  `windows-agent/tests/test_windows_agent.cpp`
- `src/ai_bridge/control/mcp_client.py`, `src/ai_bridge/client.py`,
  `src/ai_bridge/terminal_manager.py`, `src/ai_bridge/browser_tools.py`,
  `src/ai_bridge/browser_isolation_os.py`, `src/ai_bridge/vision.py`
- New: `src/ai_bridge/policy_simulation.py`,
  `tests/unit/test_agent_reliability.py` (23 tests),
  `tests/unit/test_webrtc_phase3.py` (22 tests),
  `tests/unit/test_wave3c_sdk.py` (36 tests)
- New: `analysis/wave3c-agents-progress.md`,
  `analysis/wave3c-webrtc-progress.md`,
  `analysis/wave3c-mcp-progress.md`, `analysis/wave3c-progress.md` (this file)

Unchanged by design: `src/ai_bridge/desktop_audio.py`,
`src/ai_bridge/native_encode.py` (read; nothing needed changing),
`fleet_analytics.py` not created (deliberate, §1).

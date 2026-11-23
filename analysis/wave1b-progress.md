> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 1B — Agents / Native Windows C++ / WebRTC — Progress

Date: 2026-11-17. Scope: R-P0-07, R-P0-08, R-P0-09, R-P0-13, F13-01, F13-02,
F07-05, F07-06, F08-05, F08-06 (+ F08-08 assessed, blocked).
No commit made. Sibling waves were editing other files concurrently; the
files listed under each task below are the only ones this wave changed
(verified by diffing against a pristine extraction of
`ai-bridge-main_0_ppme.zip` in `/tmp/ai-bridge-pristine`).

## Verification method

Every finding was re-verified against the code before editing (file:line
citations in each section). Python changes were verified by
`.venv/bin/python -m pytest` (venv already provisioned; nothing installed).
C++ changes could NOT be compiled here: no Windows host/toolchain exists
in this environment (only `g++` for Linux; `windows.h`/`mfapi.h` are
unavailable). C++ verification is by inspection + consistency checks only —
recorded honestly per task. The C++ test cannot run here for the same reason.

## 1. R-P0-07 (F07-01) — agent policy merge is intersection-only — DONE

Files: `agents/common/poller.py` (`_effective_policy`, new helpers
`_intersect_allowlist`, `_intersect_configured_allowlist`).

Verified pre-fix: old code ORed/maxed/unioned every field
(`poller.py:277-344` pre-change) and had a dedicated `unrestricted` branch
returning ceiling 4 with `allowed_*={"*"}`.

Fix: effective policy is the most restrictive of server + local —
`unrestricted` = server AND local; `max_tier_unattended` = min();
`allow_writes`/`allow_tier3`/`desktop_enabled` = AND;
`allowed_tools` = set intersection (`None`/`"*"` = unbounded);
`denied_tools` = union, plus `allow_shell=False` /
`allow_package_install=False` implications.
Design note: `allowed_apps`/`allowed_services`/`allowed_test_commands`
use `_intersect_configured_allowlist` — their `Policy` default is an
empty set meaning "not configured", so an empty side does not narrow;
a non-empty list on either side intersects. Explicit deny-all remains
expressible via `denied_tools` / an empty `allowed_tools` list. This
preserves the default-agent case (local default empty + server list →
server list) while still narrowing when a local admin configures a list.

Tests: `tests/unit/test_agent_policy_merge.py` (14 tests) + updated
`tests/unit/test_gui_app.py::test_poller_effective_policy_merging`
(the old test asserted the widening merge; see §9). All pass.

Rollback: revert `poller.py`; agents re-read policy on next heartbeat.

## 2. R-P0-08 (F07-02) — Linux agent restrictive default — DONE

Files: `agents/linux/agent_main.py`.

Verified pre-fix: `get_linux_server_policy()` returned
`unrestricted=True, max_tier_unattended=4, allowed_*={"*"}`, pushed to the
plane on every start (`sync_linux_server_policy`, `run_agent_loop`,
enroll path); `load_agent_config()` defaulted to the same.

Fix: `get_linux_server_policy()` now returns `max_tier_unattended=2`,
`unrestricted=False`, `allow_writes=False`, `allow_tier3=False`,
`desktop_enabled=False`, and an explicit read-only allowlist
`LINUX_DEFAULT_ALLOWED_TOOLS` (15 tools, each verified present in
`ai_bridge.policy.TOOL_TIERS` as `READ_ONLY`; no `"*"`).
`sync_linux_server_policy()` now pushes that restrictive policy (same
function name/callers; payload + log messages updated). Docstring,
run-loop log line, and `policy` subcommand help updated. A comment
states Tier 3/4 need explicit admin opt-in on the plane + approval, and
warns against restoring the old default. Behaviour change (intended):
existing Linux fleets stop running Tier-3/4 tasks until an admin opts
in on the plane; `config set unrestricted` locally no longer widens
anything because the run path uses this default as the local side and
the merge (§1) is intersection-only.

Tests: `test_agent_policy_merge.py::test_linux_default_policy_is_restrictive`,
`test_linux_default_policy_denies_tier3_and_tier4`; updated
`tests/unit/test_agent_linux.py::test_linux_server_policy_unrestricted`
→ renamed `test_linux_server_policy_restrictive_default`. All pass.

Rollback: admin opts in explicitly on the plane (config change, not a
code revert); or revert the default function.

## 3. R-P0-09 (F08-01) — fake H.264 IDR fallback removed — DONE (code), NOT COMPILED

Files: `windows-agent/src/encoder/mf_h264_encoder.cpp`,
`mf_h264_encoder.hpp`, `windows-agent/tests/test_windows_agent.cpp`.

Verified pre-fix: `EncodeBgraFrame` emitted a hardcoded 46-byte
SPS/PPS/empty-IDR (audit said 44; counted 46 in the source) with
`valid=true, is_keyframe=true` whenever the MFT produced nothing, and
set `telemetry_.backend_name = "Software H.264 Fallback"`.

Fix: the fake-IDR block is deleted. On encoder failure the access unit
is returned with `valid=false`, an empty byte vector, and a new
`EncodedAccessUnit::failure_reason` (`invalid_frame_arguments` |
`mf_reinit_failed` | `mf_no_output` | `encoder_not_initialized`).
The existing end-of-function telemetry block already increments
`dropped_frames` when `!valid`, so failures are now counted as failed
frames. `NativeWebRtcPipeline::StepCaptureAndEncode` (unchanged) already
only increments `frames_encoded` on `enc_ok`, so pipeline stats stay
consistent.

Tests: `test_windows_agent.cpp` §[5/6] rewritten — it now tests the
encoder directly (not via the pipeline, whose `StartSession` returns
true even on init failure, F08-16, out of scope here). If `Initialize`
fails, it asserts `valid=false`, empty bytes, non-empty
`failure_reason`, `dropped_frames >= 1`, and bytes != the deleted
46-byte fake pattern. If init succeeds, it asserts a real frame whose
bytes differ from the fake pattern. A null-buffer call is always a
failure. `CHECK()` (prints + returns 1) replaces `assert()` for the
encoder checks because `assert` is stripped under `NDEBUG`. NOTE: the
old §[5/6] also covered the pipeline; that coverage is gone with the
rewrite — pipeline honesty (F08-16) still needs its own test.

Verification: inspection only — grep confirms no `kValidMinimalIdrAu`
/ `0x67, 0x42` remnants in `windows-agent/src/` (the byte pattern
remains only in the test file, as the negative control). NOT compiled
or run: no Windows toolchain on this host.

Rollback: revert the three files.

## 4. R-P0-13 (F13-03) — remote WebRTC view-only enforced — DONE (agent side)

Files: `src/ai_bridge/webrtc_stream.py` (`AgentStreamer`).

Verified pre-fix: `_session_allows_input()` returned `True` when
`signaling.session` was not a dict — the `HttpSignaling` remote-agent
path — and mode changes never reached the remote streamer. Also
verified the root cause on the plane side: `GET /api/agent/webrtc/{id}`
(`src/ai_bridge/control/app.py:3715-3721`) returns only
`id, device_id, status, offer, answer, agent_candidates,
viewer_candidates, error, scale, fps, created_at` — NO `mode`,
`permissions`, or `human_takeover_active`. That endpoint is NOT in this
wave's file set (see Blocker B-1).

Fix (agent side):
- `_session_allows_input()` is now default-DENY: input is allowed only
  when a session-state dict exists (live `DictSignaling` dict, or state
  cached from `poll_session()`) and that state permits it. Unknown
  shape → `False`. `view_only` → `False`; takeover flag in state →
  `False`; per-permission checks unchanged. Local (dict) behaviour for
  `human_control` sessions is unchanged.
- New `_refresh_session_state()` caches `mode` / `permissions` /
  `human_takeover_active` / `human_takeover` from every `poll_session()`
  result; called in both `_run()` poll loops (answer-wait and
  keep-alive, every ~2.5 s), so a mid-session mode change takes effect
  within one poll interval.
- New `_human_takeover_active()` (F13-04): checks the live dict, the
  cached state, and — best effort, only meaningful when this process
  IS the plane — the `v1_routes` takeover registry, guarded so an
  unavailable registry never breaks the streamer.
- `_handle_input_payload()` refuses all input actions while takeover
  is active (`{"ok": False, "error": "human_takeover_active"}`,
  releasing held keys); read-only `ping`/`pong` and safety
  `release_all_keys` stay available.

Consequence (intended, fail-closed): until the plane endpoint exposes
mode (Blocker B-1), a remote agent's cached state stays empty and ALL
remote input is denied. Local DictSignaling sessions are unaffected.
This matches the roadmap's expectation that unknown-shape sessions
lose input, but the plane follow-up is required to restore remote
input at all — see Blocker B-1.

Tests: `tests/unit/test_webrtc_viewonly.py` (12 tests: unknown shape
denies, empty cache denies, view_only rejects, human_control gate
allows, poll-driven mode change, takeover rejects incl. via cached
remote state, ping/release survive takeover, takeover clear restores
the gate, shifted-call guard). All pass.

Rollback: revert `webrtc_stream.py` (restores fail-open — do not do so
without the plane fix in place).

## 5. F13-01 — encode-time telemetry honest — DONE

File: `src/ai_bridge/webrtc_stream.py` (`ScreenTrack.recv` telemetry).

Verified pre-fix: `"encode_time_ms": round(cap_meta.get("encode_time_ms")
or max(1.2, prep_ms * 1.4), 2)`.

Fix: `encode_time_ms` is the real measured value when present, else
`None` (JSON null). No synthetic estimate is ever reported.

Verification: code inspection + grep (no `max(1.2` / `prep_ms * 1.4`
remains outside a comment). Not runtime-verified: building the
telemetry requires `aiortc`/`av` (not installed here) and a live
capture. Recorded as inspection-verified.

Rollback: revert the one expression.

## 6. F13-02 — shifted native-encode timing call removed — DONE

File: `src/ai_bridge/webrtc_stream.py` (`ScreenTrack.recv`).

Verified pre-fix: the per-frame call passed
`(rgb_buffer, len(rgb), w, h, 3, force, ...)` against the C ABI
`(bgra, width, height, fps, bitrate_kbps, force_keyframe, out, max,
metrics)` in `windows-agent/src/c_api.cpp:141-151` (argtypes in
`screen_capture.py:151-163` confirm the 9-arg ABI without a length
argument); RGB where BGRA belongs; result discarded; exceptions
swallowed. Pure CPU waste + misleading `encoder_backend` label.

Fix: the call block is DELETED, not repaired (roadmap: drop the call
if its result is discarded). `native_timing_ran` is now always `False`,
so the telemetry label honestly reports PyAV. Guarded by
`test_webrtc_viewonly.py::test_shifted_native_encode_call_removed`
(source guard asserting `aibridge_encode_h264_frame` and
`should_use_native_encode_timing` no longer appear in the file).

Rollback: revert the block deletion (do not restore the shifted call).

## 7. F07-05 — DPAPI failure fails loudly — DONE

File: `agents/common/poller.py` (`Poller._save_token`).

Verified pre-fix: a raising `_token_saver` was swallowed
(`except Exception: pass`) and execution fell through to the plaintext
file write.

Fix: if a saver is configured and raises, print a `[SECURITY]` warning
to stderr and return — token kept in memory only for this run, nothing
written to disk. The no-saver path (Linux 0600 file) is unchanged
except that an `OSError` there is now also logged loudly instead of
swallowed.

Tests: `test_token_saver_failure_writes_no_plaintext_file`,
`test_token_saver_success_still_works`. Pass.

Rollback: revert `_save_token`.

## 8. F07-06 — Linux token in one place only — DONE

File: `agents/linux/agent_main.py` (`save_agent_config`,
`_resolve_token_and_file`, `load_agent_config` defaults).

Verified pre-fix: `save_agent_config` wrote the token into
`agent.json`, `agent.env` (`AI_BRIDGE_DEVICE_TOKEN=...`, readable via
`/proc/<pid>/environ` through systemd's `EnvironmentFile`), and
`device.token`; `agent.env` also wrote `AI_BRIDGE_UNRESTRICTED=1` etc.

Fix: token is stored ONLY in `device.token` (0600). `agent.json` is
written with `device_token` stripped; `agent.env` now carries only
`AI_BRIDGE_CONTROL_URL` / `AI_BRIDGE_WORKSPACE` (no token, no
unrestricted/tier env lines). `load_agent_config` still resolves the
token from `device.token` (and legacy `agent.json`/`agent.env` reads
are kept as migration paths — the `agent.env` read now warns once to
stderr that the token will move to `device.token` on next save).
`save_agent_config` overwrites a legacy `agent.env`, so one save
migrates the install. The systemd unit (`EnvironmentFile=-agent.env`)
still works: the `run` path resolves the token from `device.token`
(`_resolve_token_and_file` candidates), not from the environment.

Tests: `test_linux_token_stored_only_in_device_token` (token in
`device.token` 0600; absent from `agent.json`/`agent.env`; no
`AI_BRIDGE_DEVICE_TOKEN`/`AI_BRIDGE_UNRESTRICTED`/
`AI_BRIDGE_MAX_TIER_UNATTENDED` lines; `load_agent_config` still
resolves it). Existing `test_linux_agent_config_persistence` and
`test_resolve_token_from_agent_env` still pass. Pass.

Rollback: revert `save_agent_config`; re-enroll if a token was lost
(tokens are not deleted by this change — only their copies).

## 9. F08-05 / F08-06 — GDI fallback + COM lifetime — DONE (code), NOT COMPILED

Files: `windows-agent/src/capture/dxgi_capture.cpp`,
`dxgi_capture.hpp`, `windows-agent/src/encoder/mf_h264_encoder.cpp`,
`mf_h264_encoder.hpp`.

Verified pre-fix (F08-05): `CaptureViaGdiFallbackLocked` computed
`blt_ok` and ignored it; `valid = (bmp != nullptr)`; `GetDIBits`
unchecked; `monitors[0]` dereferenced without an empty check; same
shape in `CaptureWindow` (`PrintWindow`/`BitBlt` results ignored).
Verified pre-fix (F08-06): `Initialize` ignored the `CoInitializeEx`
result; the `EncodeBgraFrame` re-init path ignored both
`CoInitializeEx` and `MFStartup`; no `CoUninitialize` anywhere.

Fix (F08-05): both GDI paths now null-check every DC/bitmap handle,
check `BitBlt`/`PrintWindow` results and the `GetDIBits` scan-line
count (`dib_lines != height` → failure), and set
`CapturedFrame::failure_reason` (`no_monitors` | `gdi_dc_failed` |
`gdi_bitmap_failed` | `gdi_blt_failed` | `gdi_dibits_failed`) with
`valid=false` on any failure. Handles are released on every early
return path.
Fix (F08-06): new `EnsureComInitializedLocked()` checks the
`CoInitializeEx` HRESULT (`S_OK`/`S_FALSE` = owned;
`RPC_E_CHANGED_MODE` = usable but not ours to uninitialise; anything
else = fail); `Initialize` and the re-init path check both it and
`MFStartup`; the re-init path no longer attempts MFT creation when
the MF platform failed to start; `Shutdown()` calls `CoUninitialize()`
only for a COM initialisation this object owns.

Verification: inspection only — no Windows toolchain on this host.
Consistency checked: new member `com_initialized_` declared in the
header and used in the .cpp; `failure_reason` fields added to both
structs in their headers; no remaining references to the removed
fallback string. NOT compiled or run.

Rollback: revert the four files.

## 10. F08-08 — native config plaintext secrets — BLOCKED (out of scope)

Assessed, not fixed. The installer (`windows-agent/installer/`) and
the config readers (`windows_service.cpp`, `desktop_agent.cpp`) are
outside this wave's file set, so the plaintext `native-agent.json`
(and its permissive ACL) cannot be fixed here. Note: the sealing
primitive already exists — `DpapiSecretStore::ProtectSecret` in
`windows-agent/src/security/local_security.cpp` (verified, unchanged)
— the installer simply does not use it. Follow-up for the wave owning
`windows-agent/installer/`: seal the device token + pipe secret with
`DpapiSecretStore` at install time and restrict the config ACL to
SYSTEM + installing admin.

## 11. macOS agent — assessed, no change needed

`agents/macos/agent_main.py` passes no policy (uses `default_policy()`
via `Poller`) and stores no token itself (Poller's default
`~/.ai-bridge/device.token`, 0600 via `_save_token`). No unrestricted
default, no `agent.env` write. No edit made. (F07-11 — macOS agent
unverified/incomplete — is a separate P2 item, not in this wave.)

## 12. Existing tests updated (deliberate scope note)

Three existing test files asserted the exact vulnerable/fail-open
behaviour removed by this wave; leaving them would leave the suite
red by design. Each was updated minimally, keeping every other
assertion intact:

- `tests/unit/test_agent_linux.py::test_linux_server_policy_unrestricted`
  → `test_linux_server_policy_restrictive_default` (R-P0-08). One
  assertion corrected during the run: `decide()` denies unlisted
  tools before the approval branch, so `needs_approval` is `False`
  with reason "not in the allowed tools list" — correct
  deny-by-default; the test now asserts that reason.
- `tests/unit/test_gui_app.py::test_poller_effective_policy_merging`
  (R-P0-07): ceiling is now `min()` (2, not ≥3); app allowlist is the
  intersection (disjoint lists → empty; overlapping lists → `{"chrome"}`).
- `tests/unit/test_enhanced_features.py::test_webrtc_datachannel_input_handling`
  (R-P0-13): used `MagicMock()` signalling, which relied on the
  fail-open default; now uses a real `DictSignaling` session with
  `mode="human_control"`. Its intent (click/move/type execute via
  `perform_approved_ui_action`) is unchanged.

No test was deleted. No failing test was suppressed.

## 13. Test results

Command (from repo root, venv already provisioned, nothing installed):
`.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider`

Final: **395 passed, 5 skipped, 8 failed** (54.54 s).
The 8 failures are byte-for-byte the pre-existing baseline: the same
9-test subset run against a pristine extraction of
`ai-bridge-main_0_ppme.zip` in `/tmp/ai-bridge-pristine` fails 8/9
there too (the 9th, `test_webrtc_datachannel_input_handling`, passed
on pristine and now passes after the §12 fix). Pre-existing failures
(unrelated to this wave — screen-capture dimensions on headless Linux,
Firecrawl key, linter, legacy-route header, connector keys, HTTP
client reuse):
`test_aci_desktop_deepening::test_capture_desktop_bettercam_uses_monitor_index`,
`test_browser_intelligence::test_firecrawl_crawl_requires_key`,
`test_enhanced_features::test_screen_capture_desktop_dimensions`,
`test_execution::test_subprocess_linter_runs_cross_platform`,
`test_phase9_deep::test_legacy_webrtc_session_deprecation_header`,
`test_phases_456_deep::test_connectors_fail_without_api_key`,
`test_phases_456_deep::test_dispatch_registers_firecrawl_and_browserbase_tools`,
`test_serverai_mcp_orchestration::test_http_client_reused_across_api_calls`.

Targeted run (this wave's scope):
`.venv/bin/python -m pytest tests/unit/test_agent_policy_merge.py
tests/unit/test_webrtc_viewonly.py tests/unit/test_agent_linux.py
tests/unit/test_gui_app.py::test_poller_effective_policy_merging
tests/unit/test_enhanced_features.py::test_webrtc_datachannel_input_handling
-q -p no:cacheprovider`
→ **34 passed** (0.97 s).

Incidents during the run (fixed, not hidden):
- An early edit accidentally consumed the `def _collect_metrics():`
  line in `poller.py`, leaving its body unreachable and the name
  undefined; caught by `test_poller_auto_rotates_expiring_token`
  (`NameError`), repaired by restoring the def line, re-verified by
  the full suite.
- 4 new WebRTC tests failed only under the full suite: earlier test
  modules leave the process-global emergency-stop flag set. Fixed by
  an autouse fixture in `test_webrtc_viewonly.py` that clears the
  emergency-stop and takeover registry before/after each test — the
  production code is unchanged by this.

`py_compile` passes for `webrtc_stream.py`, `poller.py`,
`agent_main.py` (Linux + macOS).

## 14. Blockers / follow-ups for other waves

- **B-1 (blocks full R-P0-13):** `GET /api/agent/webrtc/{session_id}`
  (`src/ai_bridge/control/app.py:3715-3721`) must include `mode`,
  `permissions`, and `human_takeover_active` in the agent projection.
  Until then the agent-side cache never fills and remote input stays
  denied (fail-closed, intended). Owner: wave owning `control/*`.
  The agent side is ready: `_refresh_session_state()` will pick the
  keys up as soon as the endpoint returns them, with no agent change.
- **B-2 (blocks F08-08):** installer must seal native config secrets
  (see §10). Owner: wave owning `windows-agent/installer/`.
- **B-3 (not compiled/run):** all C++ changes in §3/§9 need a Windows
  build + `test_windows_agent.cpp` run on a Windows host before they
  count as verified. Owner: whoever has a Windows environment.
- **B-4 (pre-existing, unrelated):** the 8 baseline unit failures in
  §13 predate this wave; several are environment-dependent (headless
  Linux capture, missing API keys). Not touched.

## 15. Files changed (this wave only)

- `agents/common/poller.py` — R-P0-07, F07-05
- `agents/linux/agent_main.py` — R-P0-08, F07-06
- `src/ai_bridge/webrtc_stream.py` — R-P0-13, F13-01, F13-02, F13-04
- `windows-agent/src/encoder/mf_h264_encoder.cpp` — R-P0-09, F08-06
- `windows-agent/src/encoder/mf_h264_encoder.hpp` — R-P0-09, F08-06
- `windows-agent/src/capture/dxgi_capture.cpp` — F08-05
- `windows-agent/src/capture/dxgi_capture.hpp` — F08-05
- `windows-agent/tests/test_windows_agent.cpp` — R-P0-09 test
- `tests/unit/test_agent_policy_merge.py` — NEW (14 tests)
- `tests/unit/test_webrtc_viewonly.py` — NEW (12 tests)
- `tests/unit/test_agent_linux.py` — updated (§12)
- `tests/unit/test_gui_app.py` — updated (§12, one test)
- `tests/unit/test_enhanced_features.py` — updated (§12, one test)
- `analysis/wave1b-progress.md` — this file

Unchanged by design: `agents/macos/agent_main.py`,
`windows-agent/src/security/local_security.*` (DPAPI primitive already
correct), `agents/windows/*.py` (GUI already seals tokens via DPAPI;
no changes needed for this wave's items).

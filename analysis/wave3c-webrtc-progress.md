> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 3C Track B — WebRTC P2s + Native Windows P2s — Progress

Date: 2026-11-17. Repo: `/home/hatch/workspace/ai-bridge-main`, commit
`19747b2`. No commit made. No packages installed. No files touched outside
the assigned set (`windows-agent/src/**`, `windows-agent/tests/**`,
`src/ai_bridge/webrtc_stream.py`, `src/ai_bridge/screen_capture.py`,
`src/ai_bridge/desktop_audio.py`, `src/ai_bridge/native_encode.py`,
`tests/unit/test_webrtc_phase3.py`). No edits to `control/app.py`,
`control/db.py`, `server.py`, `tools.py`, dashboard, deployment, or
migrations. (Sibling waves were editing other files concurrently; the
file list in §12 is this track's only change set.)

## ⚠️ C++ WAS NOT COMPILED — read this first

**No Windows toolchain exists on this host** (no MSVC/clang-cl, no
Windows SDK; only Linux `g++`, and `windows.h`/`mfapi.h`/`winhttp.h`
are unavailable). Every C++ change below was verified by inspection
only:

- Deliberate brace/paren/bracket balance check with a proper
  tokenizer (comments/strings/char-literals stripped): all edited
  C++ files balance `(0, 0, 0)`, matching their HEAD baseline.
- Every new symbol cross-checked declaration ↔ definition ↔ call site
  (see §11).
- One lifetime hazard found and fixed during review (gathering promise
  now heap-held — §6).

**A Windows build + `ai_bridge_tests` run is REQUIRED before any of
the C++ changes count as verified.** In particular the new
libdatachannel API uses in `peer_connection.cpp`
(`onGatheringStateChange` / `gatheringState()` /
`GatheringState::Complete`, `IceServer::username`/`password`,
`addRemoteCandidate(rtc::Candidate)`) need a Windows build to confirm
against the linked libdatachannel version — if the linked version
names these differently, the build will fail at those lines (by
design: the code comments say not to silently drop the gate to make
it compile).

Python changes were verified by pytest (aiortc/av are NOT installed
in this environment, so no live peer connection was run; tests use
direct `AgentStreamer` construction and fakes, as in Wave 1B).

## Verification method (per finding)

Each finding was re-verified against the actual code before editing;
file:line citations are in each section below. Line numbers are
pre-change unless marked otherwise.

## 1. F13-06 — `simulate_agent_intent` gated off by default — DONE

Verified pre-fix: `webrtc_stream.py:797` handled `simulate_agent_intent`
unconditionally in `_handle_input_payload` — any DataChannel holder
could inject a fake "Deploy Production Build" tier-3 intent with a 3 s
countdown into the viewer's UI. No env flag, no mode check (it also
returns before the rate-limit gate).

Fix (`src/ai_bridge/webrtc_stream.py`):
- New `dev_simulate_intent_enabled()`: true only when
  `AI_BRIDGE_DEV_SIMULATE_INTENT=1` exactly (other values — "0",
  "true", "yes", "on" — stay off). Default OFF.
- The handler now returns `{"ok": False, "error":
  "simulate_agent_intent_disabled"}` and sends nothing when the flag
  is off; with the flag set, behaviour is unchanged.

Tests: `tests/unit/test_webrtc_phase3.py` — off by default (no send),
on with the flag (payload sent), other values stay off. Pass.

Follow-up (out of this track's file set): `apps/dashboard/src/views/
LiveDesktopView.tsx:425` still SENDS `simulate_agent_intent`. The
agent now refuses it by default, but the dashboard should stop
sending it (or gate it the same way). Owner: wave owning the dashboard.

Rollback: revert the gate; the action is refused by default again only
via the flag.

## 2. F13-07 — real RTCP-based adaptive bitrate, honestly labelled — DONE

Verified pre-fix: no RTCP feedback loop existed anywhere in
`webrtc_stream.py` (grep: no `getStats`, no `roundTripTime`). The
`network_quality` handler applied viewer-supplied `rtt_ms` /
`packet_loss_pct` as if they were measurements, and
`apply_stream_params` only changed the frame-pacing target / telemetry
label — it does not reconfigure the aiortc/PyAV encoder mid-stream.
`bitrate_kbps_target` was therefore a target presented alongside
measured-looking rtt/loss figures.

Fix (`src/ai_bridge/webrtc_stream.py`):
- New `AgentStreamer._poll_rtcp_stats()`: reads real RTCP receiver
  reports via aiortc `RTCRtpSender.getStats()`, looking for a
  `remote-inbound-rtp` report (`roundTripTime` seconds → ms,
  `packetsLost`/`packetsSent` → loss %). On success it applies the
  sample via `apply_rtcp_feedback(source="rtcp")`. When the sender is
  absent, `getStats()` is unavailable, or no receiver report exists,
  it returns `None` and records `abr_available=False` /
  `abr_source="none"` — **no figures are fabricated**. It never raises.
  It is called once per keep-alive interval (2.5 s) in `_run()`.
- New `AgentStreamer.apply_rtcp_feedback(rtt_ms, packet_loss_pct, *,
  source="rtcp")`: the single place feedback is applied — computes via
  the pure `compute_adaptive_stream_params()`, applies to the streamer
  and video track, records `rtt_ms` / `packet_loss_pct` /
  `abr_source` / `abr_available` in telemetry. Source is always
  recorded: `"rtcp"` (real receiver reports), `"viewer_reported"`
  (unverified client messages), `"viewer_requested"` (explicit viewer
  bitrate/fps request).
- The `network_quality` handler now routes viewer-supplied rtt/loss
  through `apply_rtcp_feedback(source="viewer_reported")`; a zero
  rtt/loss is recorded as `None` (no measurement), not `0.0`.
- New `describe_adaptive_bitrate()`: honest mechanism description —
  mechanism `rtcp_receiver_reports_via_aiortc_sender_getStats`,
  `encoder_reconfigured: False`,
  `bitrate_kbps_target_is: "target_label_not_measured_output"`.

Known limitation (documented in code, not hidden): `getStats()` was
not exercised against a live aiortc peer connection here (aiortc is
not installed in this environment). The parsing path is unit-tested
with a fake sender; a live E2E check is still needed on a Windows
host with aiortc installed.

Tests: `test_webrtc_phase3.py` — descriptor honesty, RTCP labelling +
telemetry, viewer-report labelling, no-sender → nothing fabricated,
fake receiver report → real values applied (rtt 200 ms, loss 10%).
Pass. Existing `test_phase3_desktop_autonomy.py::
test_network_quality_applies_fps_to_video_track` was read and remains
compatible (single `apply_stream_params` call, fps ≤ 30); it was NOT
re-run here (parent runs the full suite).

Rollback: revert `_poll_rtcp_stats` / `apply_rtcp_feedback` /
handler routing; ABR returns to viewer-driven-only with no source label.

## 3. F13-08 — mouse-held release in the fallback path — DONE (extends Wave 1B)

Verified pre-fix: Wave 1B's `release_all_held_keys()` released the
native DLL's held set plus Python-tracked KEYS (`_HELD_KEYS`) only.
Mouse buttons were not tracked in Python at all. Worse, the fallback
(non-DLL) path has no release of its own: `tools.
perform_approved_ui_action` has NO `mousedown`/`mouseup` actions
(verified: `tools.py:703-708` allowlist; `_win32_ui_action` only does
complete clicks / drags), so a `mousedown` with no `mouseup` left the
button held after disconnect in the fallback path.

Fix (`src/ai_bridge/webrtc_stream.py`):
- New `_HELD_MOUSE_BUTTONS` set (+ lock reuse), `track_mouse_button_state()`,
  `get_held_mouse_buttons()`. The mouse branch of
  `_handle_input_payload` tracks `mousedown` / `mouseup` /
  `mouse_button` down/up in BOTH paths (DLL and fallback), before the
  DLL/fallback dispatch. Clicks never hold.
- `release_all_held_keys()` now also releases tracked buttons: native
  DLL first, then a Win32 `mouse_event` button-up per held button
  (`LEFTUP 0x0004` / `RIGHTUP 0x0010` / `MIDDLEUP 0x0040`), mirroring
  the existing key-release loop. A duplicate button-up is harmless.
  On non-Windows the set is simply cleared (nothing to synthesise).
- Session teardown already called `release_all_held_keys()` on
  DataChannel close, PeerConnection disconnect/failure,
  `AgentStreamer.stop()`, `_run_loop` finally, and `stop_stream()` —
  those paths now release buttons as well as keys, with no new call
  sites needed.

Caveat (honest): in the fallback path a `mousedown` is still executed
by the tools layer as a complete click (no mousedown action exists in
`tools.py`, outside this track's file set), so the tracked "held"
button does not correspond to a physically held OS button there; the
disconnect button-up is a harmless no-op in that case. The tracking
is still correct for the DLL path and for teardown hygiene.

C++ side: `WindowsInputController` already tracked held buttons and
released them (`sendinput_controller.cpp`); added
`GetHeldMouseButtonCount()` for the test (§11).

Tests: `test_webrtc_phase3.py` — track/release, mousedown tracked via
the input handler + released on `stop()`, mouseup untracks,
`stop_stream()` releases. Pass.

Rollback: revert the button set + handler tracking + release loop;
keys-only behaviour (Wave 1B) returns.

## 4. F13-09 — session reconciliation after restart — DONE (agent side)

Verified pre-fix: media state is in-process only (`_STREAMERS`);
after a restart the registry is empty but the plane's signalling row
can still say `answered`/`connected` for the dead incarnation, and
`start_stream()` would happily start a new streamer on the recycled
id (verified: no staleness check existed in `start_stream()` or
`_run()` before the offer was posted).

Fix (`src/ai_bridge/webrtc_stream.py`):
- New `is_stale_session_state(sess, now=None) -> (bool, reason)`:
  stale only on positive evidence — status `ended`/`error`,
  `created_at` older than `SESSION_TTL_SECONDS` (600 s), or status
  `answered`/`connected` WITH both an `offer` and an `answer` (a
  previous incarnation negotiated it; a fresh session is `waiting`
  with neither). Empty/unknown state is NOT stale, so a transient
  poll failure never kills a live session.
- New `reconcile_session_before_start(signaling, session_id)`: checks
  the live dict (DictSignaling) or one poll (HttpSignaling); poll
  failure is reported as ok/unknown, not stale.
- `start_stream()` now refuses a stale id with `RuntimeError("Stale
  WebRTC session …: refusing to resume a previous incarnation after
  restart; create a fresh session id.")`. Callers already handle this:
  `v1_routes.py` catches `Exception` around `start_stream` and marks
  the session `error`; the dispatch task path fails the task with the
  message. A new session always gets a fresh id — ids are never
  recycled.
- `AgentStreamer._run()` re-checks with one poll BEFORE posting its
  offer: stale → `post_error("stale_session:<reason>")` and return
  without offering.

Scope note: this is agent-side only. The plane-side row cleanup
(expire stale rows on restart) lives in `control/*`, outside this
track's file set; the agent now refuses to serve a stale row, which
is the fail-closed half.

Tests: `test_webrtc_phase3.py` — all `is_stale_session_state`
branches, reconcile fresh/stale dict + remote poll stale + poll
failure, `start_stream` refuses a stale id, fresh `start_stream`
starts. Pass.

Rollback: revert the three helpers + the two call sites; stale ids
are servable again (do not do so without the plane-side fix).

## 5. F13-10 — ICE-complete before offer — DONE (both paths)

Verified pre-fix (Python): `_run()` waited up to 5 s for
`iceGatheringState == "complete"` and then posted the offer ANYWAY,
with only a `warning` flag the viewer could ignore.

Fix (Python, `src/ai_bridge/webrtc_stream.py`):
- New `wait_for_ice_gathering_complete(pc, timeout_seconds=
  ICE_GATHERING_TIMEOUT_SECONDS, poll_interval=0.05) -> bool`
  (testable with a fake pc).
- `_run()` now: if gathering is not complete after the timeout, it
  does NOT post the offer — it marks the session dict
  `status="error"` / `error="ice_gathering_incomplete"`, calls
  `post_error("ice_gathering_incomplete")`, and returns.

Fix (C++, `windows-agent/src/webrtc/peer_connection.cpp`):
- `onGatheringStateChange` registered BEFORE `setLocalDescription()`
  (promise fulfilled on `GatheringState::Complete`; the promise is
  heap-held via `shared_ptr` so a callback during teardown cannot
  dangle — see §11 review note).
- After the local description is ready, if gathering is not already
  complete, wait up to 5 s; on timeout set
  `error_out="ice_gathering_incomplete"`, `PostError`, return false —
  no offer is posted.
- **Needs a Windows build to verify** the libdatachannel gathering
  API names (see the header caveat in §0).

Tests (Python): `test_webrtc_phase3.py` — immediate complete, timeout
without complete, late completion. Pass. (The `_run()` wiring itself
could not be exercised here — aiortc is not installed; verified by
inspection.)

Rollback: revert the gate in both files; offers post after the 5 s
wait again (with the warning flag).

## 6. F13-05 — C++ WebRTC: viewer candidates, TURN, no per-frame polling — DONE (code), NOT COMPILED

Verified pre-fix: `PollSession` (`http_signaling.cpp`) read the answer
SDP only — `viewer_candidates` were never parsed, so the peer could
never apply them. Only STUN was configured
(`peer_connection.cpp`: single `stun:stun.l.google.com:19302`).
`RequestJson` opened a fresh WinHTTP session + connection (+ TLS
handshake for HTTPS) on EVERY request, and the peer loop called
`PollSession` once per video frame (~30 req/s with a handshake each).

Fix:
- `windows-agent/src/webrtc/http_signaling.cpp` / `.hpp`:
  - `HttpSignalingClient` now holds ONE persistent WinHTTP session +
    connection, created lazily by `EnsureConnected()` (URL parsed
    once, host/port/scheme/path cached), closed by `Close()` / the
    destructor. `RequestJson` opens/closes only the per-request
    handle. A failed request drops the connection so the next request
    re-establishes it (server restart / network change). Copying is
    deleted (handles are owned).
  - New `ParseViewerCandidates()` + `JsonGetInt()`: parses the
    `viewer_candidates` array from the session snapshot, handling
    both the flat shape
    `{"candidate":"candidate:…","sdpMid":"0","sdpMLineIndex":0}` and
    the nested shape `{"candidate":{…}}` the plane echoes back.
    Empty candidates are skipped; a malformed trailing entry stops
    the parse (partial list beats dropping the snapshot).
    `PollSession` now fills `snap.viewer_candidates`.
- `windows-agent/src/webrtc/peer_connection.cpp` / `.hpp`:
  - TURN: `PeerConnectionConfig` gains `turn_url` /
    `turn_username` / `turn_credential` (config-driven). When
    `turn_url` is set, it is appended to the libdatachannel ICE
    servers with the credentials, so the peer can connect behind
    symmetric NAT; empty means STUN-only (old behaviour).
    `desktop_agent.cpp` populates them from the config file
    (`turn_url` / `turn_username` / `turn_credential`) with env
    fallback (`AI_BRIDGE_TURN_URL` / `AI_BRIDGE_TURN_USERNAME` /
    `AI_BRIDGE_TURN_CREDENTIAL`, same names as the Python path).
  - Viewer candidates: the loop applies each new viewer candidate
    once, in order, via `pc->addRemoteCandidate(rtc::Candidate(
    cand.candidate, cand.sdp_mid))`, only after the remote
    description is set; one bad candidate cannot kill the session.
  - Polling: the loop now polls signalling at most every 500 ms
    while waiting for the answer, every 2 s once connected; the
    frame pump still runs at `cfg_.fps` in between. It also breaks
    out when the snapshot says `ended`/`error`.
  - The old `sdp_mline_index = 0` on local candidates is kept with a
    comment: it is correct, not a placeholder — the offer carries a
    single video m-line (index 0); the data channel is SCTP and has
    no m-line.
- ICE gate: see §5 (C++ half).

Tests: no automated test runs here (no Windows host). The C++ test
file gained no direct test for this section — TURN/candidates need a
live libdatachannel peer + plane; recorded as a follow-up for Windows
CI/E2E (see §13).

Rollback: revert the three files (+ config fields in
`desktop_agent.cpp`); STUN-only + per-frame polling return.

## 7. F08-09 — secure-desktop detection unified — DONE

Verified pre-fix: C++ (`dxgi_capture.cpp:30-48`) treated ANY
non-`Default` desktop as secure; Python
(`screen_capture.py:218-280`) listed only `("winlogon", "disconnect",
"screensaver")` — so the two paths disagreed on e.g. `"Screen-saver"`
(hyphenated) and any future desktop name. The C++ check also ran on
every frame (`CaptureNextFrame` called `IsSecureDesktopActive()` per
frame); Python's `get_desktop_session_state()` is likewise called per
frame by `ScreenTrack.recv()`.

Fix:
- ONE rule, both languages: secure iff the input-desktop name is
  non-empty and not `"Default"` (case-insensitive), or
  `OpenInputDesktop` fails.
  - C++: `DxgiDesktopCapturer::IsSecureDesktopActive()` is now the
    documented single helper (rule unchanged — it was already the
    correct one); no second desktop-name list exists anywhere.
  - Python: `screen_capture.is_secure_desktop_name()` implements the
    same predicate; `get_desktop_session_state()` uses it via the new
    `_probe_input_desktop_name()` helper (the old inline Win32 probe
    + name list is deleted).
- Per-frame cost removed on both sides:
  - C++: new `IsSecureDesktopActiveCached()` (2 s TTL; cache members
    on the capturer); `CaptureNextFrame` uses it. It deliberately does
    NOT lock `mutex_` itself — the caller already holds it.
  - Python: the desktop NAME probe is cached for 2 s
    (`_DESKTOP_NAME_CACHE`); metrics/capture_blocked are still
    computed fresh on every call.

Tests: Python — `test_webrtc_phase3.py::
test_secure_desktop_name_rule` ("Default"/"default" false,
"Winlogon"/"Disconnect"/"Screen-saver" true, "" false). Pass. C++ —
new test §[7/7] checks the cached probe agrees with the uncached
helper (no assertion on the value — it depends on host lock state).
NOT RUN (no Windows host).

Rollback: revert `dxgi_capture.*` + the Python probe/predicate;
per-frame probing and the divergent lists return.

## 8. F08-10 — monitor geometry in C++ input — DONE (code), NOT COMPILED

Verified pre-fix: `peer_connection.cpp` called
`input_ctrl.MoveMouse(x, y, true)` / `MouseButton(…, true)` with no
monitor arguments, so normalised coordinates resolved against the
virtual-desktop origin; Python passes explicit monitor offsets, so
multi-monitor clicks landed on different monitors per path.

Fix (`peer_connection.cpp`): `RunUntilStopped` enumerates monitors
ONCE per session via `DxgiDesktopCapturer::EnumerateMonitors()`,
picks the session's `monitor_index` (first monitor as fallback), and
passes `mon_left/mon_top/mon_width/mon_height` to every `MoveMouse` /
`MouseButton` call (the `onMessage` lambda captures them by value).
`TransformCoordinates` already accepted these parameters — this wires
them up.

Tests: C++ test §[7/7] — relative-offset assertion
(`TransformCoordinates(0,0,…,100,50,…)` is exactly +100/+50 px vs the
unoffset call; origin coordinates, so it holds on any virtual-desktop
size). NOT RUN (no Windows host).

Rollback: revert the geometry block + call sites; input resolves
against the virtual desktop again.

## 9. F08-11 — per-iteration monitor construction — DONE (code), NOT COMPILED

Verified pre-fix: `windows_service.cpp:100-104` built a fresh
`WindowsSystemMonitor` on every 1 s supervisor iteration, so its
`GetSystemTimes` delta baselined at construction and the CPU reading
was always ~0%. The same shape existed in capture:
`CaptureViaGdiFallbackLocked` called `EnumerateMonitors()` (a full
DXGI adapter/output walk) on every frame when DXGI was unavailable.

Fix:
- Service: `RunSupervisorIteration` now takes a
  `WindowsSystemMonitor&` (constructed once in `ServiceMain` for the
  life of the service; the interactive fallback in `main()` builds
  one for its single iteration). Every later `CollectSnapshot()` is a
  real tick delta.
- Capture: `EnumerateMonitors()` caches its result for 5 s (mutable
  cache members; the GDI fallback's per-frame call now hits the
  cache). Displays rarely change; 5 s staleness is acceptable.

Tests: C++ test §[7/7] — two `EnumerateMonitors()` calls return the
same list size (cache hit). NOT RUN. (A CPU-%-nonzero test is not
added: the first snapshot after construction is legitimately ~0 on a
fresh monitor; the fix is structural.)

Rollback: revert the service signature + the enumerator cache.

## 10. F08-12 — single-instance guard — DONE (code), NOT COMPILED

Verified pre-fix: the installer launches the desktop agent twice
(HKLM Run + scheduled task) and `desktop_agent.cpp` had no mutex or
lock file (unlike the Python GUI).

Fix:
- Desktop agent (`desktop_agent.cpp`): a named mutex
  `Global\AIBridgeDesktopAgent-Session-<session-id>` (one per
  interactive session) is created at startup and held for the life of
  the process; a second launch in the same session prints
  `{"ok":false,"error":"already_running",…}` and exits with code 3.
- Service (`windows_service.cpp`): `Global\AIBridgeAgentService`
  mutex in `main()` before the service dispatcher starts; a duplicate
  long-running supervisor exits 1. The `--once` diagnostic path
  returns before the guard (it never runs the supervisor loop).

Tests: none added — a second-instance test would need to launch the
binary twice on Windows; recorded as a manual/CI follow-up (§13).

Rollback: delete the two mutex blocks; dual launch is possible again.

## 11. F08-13 — covered by §6 (F13-05)

F08-13 is the same finding as F13-05 from the native side (viewer
candidates dropped, no TURN, per-frame HTTPS polling). Fixed in §6.
The `sdp_mline_index` hardcode is addressed there too (correct value,
documented).

## 12. F08-14 — real tests replacing assert()-only tests — DONE (code), NOT COMPILED

Verified pre-fix: `test_windows_agent.cpp` used `assert()` in §[1/6],
[2/6], [3/6], [4/6], [6/6] (only the §[5/6] encoder checks used
`CHECK()` after Wave 1B); `assert` is stripped under `NDEBUG`, so a
Release build could pass while broken.

Fix (`windows-agent/tests/test_windows_agent.cpp`):
- All remaining `assert()` calls converted to `CHECK()` (grep confirms
  zero `assert(` calls remain outside comments). `check_impl` is now
  thread-safe (mutex) because the pipe test asserts from a server
  thread. Section numbering updated [1/6]→[1/7].
- New section [7/7] (Wave 3C, all `CHECK()`):
  - F08-10 monitor-geometry input (relative offset, §8).
  - F13-08/C++ mouse-button held release: `MouseButton("left",
    "down", …)` → `GetHeldMouseButtonCount() == 1` →
    `ReleaseAllHeldKeys()` → 0. The `MouseButton` return value is
    deliberately NOT asserted: `SendInput` can legitimately fail in a
    non-interactive session (Session 0 / UIPI) while the held-button
    mask is still set — the mask is what must be released.
  - F08-16 `StartSession` honesty: either it starts and
    `status=="streaming"` with empty `error`, or it refuses with
    `status=="error"` and a non-empty reason; after `StopSession`
    status is `"stopped"` and `StepCaptureAndEncode` returns false.
  - F08-09 cached secure-desktop probe agrees with the uncached
    helper; F08-11 monitor enumeration cache returns the same list.
- New `WindowsInputController::GetHeldMouseButtonCount()` (header +
  implementation) to support the button test.

NOT RUN — no Windows host/toolchain here. Needs a Windows build +
`ai_bridge_tests` run (see §0).

Rollback: revert the test file (and the one-line helper if unused).

## 13. F08-15 — hand-rolled JSON: kept, with written justification — DONE

Decision: **keep the hand-rolled JSON extractors; do not vendor a
library in this wave.** Justification, recorded in code
(`http_signaling.cpp` — full text; `peer_connection.cpp`,
`windows_service.cpp`, `desktop_agent.cpp` — short pointer) and here:

1. No JSON library is vendored anywhere in this tree. Adding one
   (e.g. nlohmann/json) means a new third-party dependency whose
   integration (CMake fetch/vendoring, include paths, ABI) cannot be
   verified on any Windows host in this environment — an unverifiable
   dependency swap is higher risk than keeping the current parsers.
2. The current helpers are deliberately NOT a general JSON parser:
   they extract a fixed set of string/number fields from the plane's
   own well-formed responses, with escape-aware string handling
   (`JsonGetString` handles `\n \r \t \" \\ \/ \u`). The new
   `ParseViewerCandidates` is the most complex one and is bounded to
   the plane's `viewer_candidates` array shape (flat + nested).
3. The failure mode of these helpers is fail-closed-ish for their
   call sites: a field that cannot be parsed reads as empty, and
   callers treat empty answer/candidates as "not yet available"
   (never as fabricated data).

If a general parser is ever needed, vendor nlohmann/json as a single
header and replace these helpers — do not grow them into a parser.
That instruction is in the code comment.

## 14. F08-16 — StartSession returns an honest result — DONE (code), NOT COMPILED

Verified pre-fix: `webrtc_transport.cpp:24-52` ignored both
`capturer_.Initialize()` and `encoder_.Initialize()` return values
and always returned `true`; `c_api.cpp:167` set
`g_encoder_initialized = true` unconditionally after
`g_encoder.Initialize(cfg)`, so a failed encoder init was recorded
as success and later calls skipped re-initialisation.

Fix:
- `NativeWebRtcPipeline::StartSession` now returns `false` when the
  ENCODER fails to initialise (fatal — no frame can ever be
  produced): `stats.status = "error"`, `stats.error =
  "encoder_init_failed"`, `active_` stays false. A failed CAPTURE
  init alone does NOT fail the session (the per-frame GDI fallback
  may still work) but is recorded in the new
  `StreamSessionStats::capture_init_ok` (and `error` is a new stats
  field). `StartSession`'s contract is documented on the declaration
  in `webrtc_transport.hpp`.
- Callers check it: `peer_connection.cpp` starts the pipeline BEFORE
  posting the offer and returns false (+ `PostError`) when start
  fails — a session that cannot encode is never offered.
  `desktop_agent.cpp` probe mode reports `session_started` /
  `session_error` in its JSON.
- `c_api.cpp`: `g_encoder_initialized` is set only from the actual
  `Initialize()` return value. No early return was added on failure
  — `EncodeBgraFrame` still runs and fails honestly (`valid=false`,
  Wave 1B), and the metrics block still runs so callers see the
  failure rather than stale data.

Tests: C++ test §[7/7] (see §12). NOT RUN (no Windows host).

Rollback: revert `webrtc_transport.*`, the `peer_connection.cpp`
call-site check, the probe JSON, and the `c_api.cpp` flag; `true`
always returns again.

## 15. Test results (Python)

Command (repo root, venv already provisioned, nothing installed):
`.venv/bin/python -m pytest tests/unit/test_webrtc_phase3.py -q -p no:cacheprovider`

Result: **22 passed** (1.03–1.66 s across runs; final run 22 passed in
1.36 s). `py_compile` passes for `webrtc_stream.py` and
`screen_capture.py`.

Per the task, ONLY the new test file was run here — the full suite
(and neighbouring files such as `test_webrtc_viewonly.py`,
`test_phase3_desktop_autonomy.py`, `test_windows_p0_p2_gaps.py`) was
NOT run by this track; the parent runs the full suite after all
tracks. One neighbouring test was READ (not run) for compatibility:
`test_phase3_desktop_autonomy.py::
test_network_quality_applies_fps_to_video_track` still holds under
the new handler (single `apply_stream_params` call, fps ≤ 30).

C++ tests: NOT compiled or run — no Windows toolchain on this host
(see §0).

## 16. Files changed (this track only)

- `src/ai_bridge/webrtc_stream.py` — F13-06, F13-07, F13-08, F13-09, F13-10
- `src/ai_bridge/screen_capture.py` — F08-09 (Python half: unified rule + cached probe)
- `tests/unit/test_webrtc_phase3.py` — NEW (22 tests)
- `windows-agent/src/webrtc/http_signaling.cpp` — F13-05 (persistent connection, viewer candidates), F08-15 (justification)
- `windows-agent/src/webrtc/http_signaling.hpp` — F13-05 (persistent handles)
- `windows-agent/src/webrtc/peer_connection.cpp` — F13-05 (TURN, candidates, poll throttle, ICE gate), F08-10 (monitor geometry), F08-16 (StartSession check), F08-15 (note)
- `windows-agent/src/webrtc/peer_connection.hpp` — F13-05 (TURN config)
- `windows-agent/src/webrtc/webrtc_transport.cpp` — F08-16 (honest StartSession)
- `windows-agent/src/webrtc/webrtc_transport.hpp` — F08-16 (error/capture_init_ok fields)
- `windows-agent/src/capture/dxgi_capture.cpp` — F08-09 (cached secure-desktop probe), F08-11 (monitor cache)
- `windows-agent/src/capture/dxgi_capture.hpp` — F08-09, F08-11 (cache members)
- `windows-agent/src/input/sendinput_controller.cpp` — F08-14 (GetHeldMouseButtonCount)
- `windows-agent/src/input/sendinput_controller.hpp` — F08-14 (declaration)
- `windows-agent/src/service/windows_service.cpp` — F08-11 (persistent monitor), F08-12 (service mutex), F08-15 (note)
- `windows-agent/src/desktop/desktop_agent.cpp` — F08-12 (per-session mutex), F13-05 (TURN config), F08-16 (probe honesty), F08-15 (note)
- `windows-agent/src/c_api.cpp` — F08-16 (encoder-init flag honesty)
- `windows-agent/tests/test_windows_agent.cpp` — F08-14 (CHECK everywhere + §[7/7])
- `analysis/wave3c-webrtc-progress.md` — this file

Untouched by design: `src/ai_bridge/desktop_audio.py`,
`src/ai_bridge/native_encode.py` (read; nothing in them needed
changing for these findings), `control/*`, `server.py`, `tools.py`,
dashboard, deployment, migrations.

## 17. Review notes / incidents during the work

- The C++ brace/paren balance check with a naive regex counter
  initially flagged four files; re-checking with a proper tokenizer
  (comments/strings/char-literals stripped) showed all four balance
  `(0, 0, 0)` in both HEAD and the working tree — the naive counter
  was fooled by char literals such as `'{'`. Lesson: use the proper
  tokenizer for this check.
- The ICE-gathering promise in `peer_connection.cpp` was first
  captured by reference in the `onGatheringStateChange` lambda; review
  caught that the promise's scope is destroyed before the peer
  connection, so a callback during teardown would dangle. Fixed by
  heap-holding it in a `shared_ptr` (same latent pattern pre-exists
  for `local_ready`/`onLocalDescription` — not changed here).
- The new C++ test's monitor-offset assertion was first written with
  centre coordinates (0.5, 0.5); review caught that the virtual
  desktop clamps absolute coordinates, so it would fail on a narrow
  (<1061 px) display. Rewritten with origin coordinates (0.0, 0.0),
  where the +100/+50 px relative assertion holds on any host.
- The `c_api.cpp` encoder-init fix deliberately does NOT early-return
  on init failure, so the metrics block still runs and callers see
  the failure instead of stale data.

## 18. Blockers / follow-ups for other waves

- **B-1 (needs a Windows host):** build the C++ agent + run
  `ai_bridge_tests` (§[7/7]) and confirm the libdatachannel API
  names used in §5/§6 (`onGatheringStateChange`, `gatheringState`,
  `IceServer::username`/`password`, `addRemoteCandidate`).
  Owner: whoever has a Windows environment.
- **B-2 (dashboard, out of scope):** `LiveDesktopView.tsx:425`
  still sends `simulate_agent_intent`; stop sending it or gate it
  the same way. Owner: wave owning the dashboard.
- **B-3 (needs live aiortc):** `_poll_rtcp_stats()` is unit-tested
  with a fake sender only; verify against a real aiortc peer
  connection (receiver reports present, ABR steps down under loss).
- **B-4 (needs live peer + plane):** C++ TURN / viewer-candidate /
  poll-throttle behaviour needs a Windows E2E run (symmetric-NAT
  case, TURN configured).
- **B-5 (plane side, out of scope):** expire stale WebRTC session
  rows on plane restart (`control/*`); the agent now refuses stale
  ids, which is the fail-closed half.
- **B-6 (manual):** single-instance guard — verify by launching
  `ai_bridge_desktop` twice in one session (second exits 3) and the
  service twice (second exits 1).
- **B-7 (not run here):** full Python suite + neighbouring WebRTC
  test files — parent runs after all tracks.

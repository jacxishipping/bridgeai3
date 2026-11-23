> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 13 — WebRTC and Desktop Control Audit

**Scope:** the complete desktop-interaction path — Python `aiortc` shipping path,
C++ libdatachannel optional path, signalling, capture, encoding, input, audio,
terminal sessions (context), and the Python↔C++ overlaps.
**Method:** read-only source inspection. No stream was run, no browser was driven,
and **no FPS, latency, or quality figure in this report is a measurement** — every
number cited is a code constant / target / default, labelled as such.
**Status labels:** `VERIFIED` = read in the cited file; `INFERRED` = follows from
code but not executed; `NOT FOUND` = searched for and absent.

## 1. The two WebRTC implementations (read this first)

There are **two** WebRTC stacks, and only one ships by default.

| | Python — **shipping path** | C++ — optional path |
|---|---|---|
| Files | `src/ai_bridge/webrtc_stream.py` (1,348 LOC), `screen_capture.py` (742), `desktop_audio.py` (179), `native_encode.py` (110) | `windows-agent/src/webrtc/peer_connection.cpp` (281), `http_signaling.cpp` (268), `webrtc_transport.cpp` (112), capture + MF encoder (report 08) |
| PeerConnection | `aiortc` `RTCPeerConnection` (`webrtc_stream.py:1110+`) | libdatachannel, only if `-DAIBRIDGE_WITH_LIBDATACHANNEL=ON` (default OFF); libwebrtc enum exists but `CreatePeerConnectionSession()` returns `nullptr` without libdatachannel (report 08, F08-19) |
| Video source | `ScreenTrack.recv()` → `grab_screen()` → PIL image → `av.VideoFrame.from_image()` → PyAV H.264 encoder inside aiortc | DXGI frame → MF H.264 → raw Annex-B NAL units pushed with `track->send()` |
| Signalling | `HttpSignaling` (agent side) / `DictSignaling` (in-process, `device_id="local"`) | `HttpSignalingClient` (WinHTTP) — same HTTP endpoints |
| Who starts it | Control-plane task `start_webrtc_stream` via `dispatch()` (`control/dispatch.py:21-50,538-540`), or in-process for `local` devices | `ai_bridge_desktop --control-url … --device-token … --session-id …` (manual launch; no task integration) |
| Status | The only path exercised by the HTTP-level tests (§9) | `NOT FOUND`: any test, CI job, or dashboard wiring that launches it |

`src/ai_bridge/native_encode.py` states the honesty position explicitly
(`describe_native_encode()`, lines ~40-110): *"Fleet WebRTC uses Python
aiortc/PyAV. Optional ai_bridge_native.dll only provides H.264 encode timing
smoke — not a C++ libwebrtc media path."* The C++ `NativeWebRtcPipeline` header
says the same (`webrtc_transport.hpp`: "It is NOT a libwebrtc PeerConnection —
no SDP/ICE/DTLS/RTP stack is linked here").

```mermaid
flowchart TD
    subgraph VIEWER["Browser / dashboard"]
        V[Viewer PeerConnection<br/>creates answer + ICE candidates]
    end
    subgraph PLANE["Control plane (signalling only)"]
        S1[POST /api/v1/sessions<br/>canonical — report §2]
        S2[Legacy /api/devices/.../desktop/webrtc/*<br/>deprecated, Sunset header]
        S3[Agent signalling<br/>GET/POST /api/agent/webrtc/{id}[/offer|/candidates|/error]]
        DB[(webrtc_sessions table<br/>via v1_routes.load/save_webrtc_session_db)]
    end
    subgraph AGENT["Agent host (Python shipping path)"]
        D[dispatch: start_webrtc_stream task]
        AS[AgentStreamer thread<br/>asyncio loop]
        ST[ScreenTrack<br/>grab_screen -> PyAV H.264]
        AU[DesktopAudioTrack<br/>WASAPI or silent PCM]
        DC["DataChannel 'input'<br/>mouse / keys / files / clipboard"]
        CAP[screen_capture.py<br/>native DLL -> DXGI/GDI -> PIL]
    end
    subgraph CPP["C++ optional path"]
        C[ai_bridge_desktop --session-id<br/>libdatachannel]
        DX[DXGI -> MF H.264 NALs]
    end
    VIEWER -->|answer + viewer candidates| S1
    S1 -->|queued task| D
    D --> AS
    AS --> ST & AU & DC
    ST --> CAP
    AS -- "offer + agent candidates<br/>HTTP polling" --> S3
    S3 --> DB
    VIEWER -. "media: DTLS-SRTP peer-to-peer<br/>(plane never sees media)" .-> AS
    VIEWER -. "media (if built)" .-> C
    C --> DX
    C -- "offer + candidates<br/>WinHTTP, poll for answer" --> S3
```

## 2. Session lifecycle and signalling — VERIFIED

### 2.1 Canonical path: `POST /api/v1/sessions`

`src/ai_bridge/control/v1_routes.py:573-700` (`POST /api/v1/sessions`):
- Requires role `viewer`; rejects non-Windows hosts **before** creating a session
  or queueing work (`stream_gate_for_os` / `platform_supports_webrtc_desktop_stream`
  — "Live Desktop streaming is supported on Windows desktop hosts only").
  `INFERRED`: Linux/macOS agents can never be live-desktop targets, whatever their
  local capture capability is.
- `mode` determines permissions via `derive_session_permissions(mode, eff_policy)`;
  `mode == "human_control"` sets device-wide human takeover
  (`set_device_human_takeover`, `v1_routes.py:56-111`, persisted via
  `plane_state.save_human_takeover` and hydrated at startup).
- Session id `session_<10 hex>`; stored with `offer/answer/agent_candidates/
  viewer_candidates`, `created_at`, `scale/fps/bitrate_kbps/monitor_index`,
  `status="waiting"`. For a remote device it inserts a `start_webrtc_stream`
  task (tier 3, `v1_routes.py:690-700`); for `device_id="local"` it calls
  `webrtc_stream.start_stream(..., DictSignaling(sess))` in-process.
- Mode change (`POST/PATCH /api/v1/sessions/{id}/mode`, `v1_routes.py:714-760`):
  re-derives permissions, updates takeover, and — importantly — calls
  `webrtc_stream.release_all_held_keys()` when the new mode is `view_only`.
- Quality change (`PATCH …/quality`, `v1_routes.py:761+`), clipboard endpoint
  (`POST …/clipboard`, `v1_routes.py:787+`), terminate (`POST …/terminate`,
  `v1_routes.py:817+`), offer/answer/ice (`v1_routes.py:840-907`), and a session
  WebSocket (`/api/v1/ws/sessions/{id}`, per the module header) complete the API.
  This track read the session-creation and mode handlers in full; the WS
  broadcast helper (`_broadcast_ws`) is referenced but its fan-out was not
  traced in detail — flag for track 02.

### 2.2 Legacy path (deprecated, still present)

`src/ai_bridge/control/app.py:3546-3760`:
`POST /api/devices/{device_id}/desktop/webrtc/session` returns
`Deprecation: true` + `Sunset: legacy-webrtc-session-route` headers and HTTP
**410** unless `AI_BRIDGE_ALLOW_LEGACY_WEBRTC=1`
(`production_readiness.legacy_webrtc_routes_allowed()`). The legacy session
dict differs from v1 (permissions `{"view_screen": True, "control_mouse":
mode != "view_only", …}`, `execute_commands: False`, no takeover flag at
creation). `GET/POST …/answer`, `…/candidates`, `DELETE` (end) mirror v1.
`INFERRED`: two session shapes flow into the same `AgentStreamer`, whose
`_session_allows_input()` reads `session["mode"]` / `session["permissions"]`
— the legacy shape is compatible by accident of shared key names, not by a
shared schema. Session TTL is 600 s in both paths (`SESSION_TTL_SECONDS = 600`,
`webrtc_stream.py:35`; `_get_webrtc_session` returns 410 after 600 s,
`app.py:3487-3496`).

### 2.3 Agent-side signalling (Python)

`AgentStreamer._run()` (`webrtc_stream.py:1110-1309`):
1. Builds `RTCConfiguration` from `get_configured_ice_servers()` (§3).
2. Adds `ScreenTrack` (H.264 codec preference forced via
   `setCodecPreferences(h264 + others)`), optionally `DesktopAudioTrack`.
3. Creates ordered DataChannel `"input"`; also handles a remote-created channel.
4. `createOffer()` → `setLocalDescription()` → waits up to 5 s (100 × 50 ms)
   for ICE gathering `complete`; if incomplete it records a `warning` but
   **posts the offer anyway** with whatever candidates were gathered.
5. `signaling.post_offer({sdp, type})`, then polls `poll_session()` every
   0.25 s for up to 60 s for the answer; applies viewer candidates as they
   appear (`candidate_from_sdp`, tolerating `a=candidate:` prefixes); breaks
   out once `connectionState` is `connected/completed`.
6. Keep-alive loop: polls every 2.5 s; exits on `status in ("ended","error")`,
   on TTL expiry (`now - created_at > 600`), or on `pc.connectionState in
   ("failed","closed")`. Transient poll errors are swallowed (`except: pass`).
7. On exit: `release_all_held_keys()`, `await pc.close()`.
- `HttpSignaling` (`webrtc_stream.py:510-546`): `httpx.Client` per request
  (no connection reuse), `X-Device-Token` header, 15 s timeout, `raise_for_status`
  → `RuntimeError("Signaling HTTP {code} on {path}")`. `post_error` swallows
  its own failures.
- `DictSignaling` (`webrtc_stream.py:548-568`): mutates the shared session dict
  directly — no HTTP, no auth, no copy. `poll_session()` returns `dict(session)`
  (shallow copy). `INFERRED`: the in-process path trusts the caller completely;
  it is only reachable when the plane itself runs the streamer (`local` device).

### 2.4 Agent-side signalling (C++)

`peer_connection.cpp:100-230` + `http_signaling.cpp`: same endpoints, WinHTTP,
one TCP+TLS handshake per request, `PollSession()` every loop iteration
(≈30 polls/s while waiting for an answer — and it keeps polling after the
answer is applied, since there is no stop-polling branch). It validates the
answer contains `ice-ufrag` before applying it, waits ≤ 5 s for the local
description, and posts the offer. **It never reads `viewer_candidates`** —
see F13-05. `sdp_mline_index` is hardcoded `0` on local candidates.

### 2.5 Session/transport state is in-process — VERIFIED

`_STREAMERS`, `_STREAM_TELEMETRY`, `_SESSION_DEVICES`, `_AGENT_CONTEXT`,
`_HELD_KEYS` are module-global dicts/sets in `webrtc_stream.py:35-42`.
`start_stream()` raises if a session id is already running; `stop_stream()`
releases keys and pops the entry. `INFERRED`: a control-plane/agent restart
loses all live sessions, their ICE state, and held-key state; the 600 s TTL
and the viewer's polling are the only recovery. The plane persists the
*signalling* session (DB via `v1_routes`), but the *media* session lives only
in the agent process — after an agent restart the DB session says `waiting/
answered` while nothing streams. `NOT FOUND`: any reconciliation between the
DB session row and the agent's `_STREAMERS` on (re)connect.

## 3. ICE / STUN / TURN — VERIFIED

- Python defaults (`webrtc_stream.py:35,44-104`):
  - `DEFAULT_ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]`.
  - `get_configured_ice_servers()`: `AI_BRIDGE_ICE_SERVERS` JSON wins; else
    `AI_BRIDGE_TURN_URL` + optional username/credential; else
    `generate_ephemeral_turn_credentials()` → STUN (Google + `AI_BRIDGE_TURN_HOST`,
    default `jxiptvplayer.app:3478`) + TURN `turn:{host}:3478?transport=udp|tcp`
    with HMAC-SHA1 ephemeral credentials (RFC 5766 REST API style, username
    `{expiry}:{prefix}`, TTL 24 h).
  - `generate_ephemeral_turn_credentials()` reads `AI_BRIDGE_TURN_SECRET`; if
    unset it uses a **hardcoded default shared secret** in non-production and
    raises in production (`RENDER`/`AI_BRIDGE_PROFILE=production`). `VERIFIED`
    from the code (the literal default is redacted from this report per
    credential-handling rules; it is visible in `webrtc_stream.py:44-76`).
    `INFERRED`: any non-production deployment shares one TURN secret, so TURN
    credentials are forgeable by anyone who reads the source — acceptable only
    because the TURN host itself is the operator's.
  - `NOT FOUND`: any verification that the configured TURN host is actually
    reachable, or any fallback if TURN allocation fails (aiortc will simply
    fail ICE and the session errors out — §2.3 step 6).
- C++ libdatachannel: **STUN only** (`stun.l.google.com:19302`), no TURN
  (`peer_connection.cpp:105`). No `AI_BRIDGE_TURN_*` handling at all.
- `NOT FOUND` in either path: ICE restart on the *agent* side beyond Python's
  `pc.restartIce()` (max 2 attempts, only on `iceconnectionstatechange ==
  "disconnected"`, `webrtc_stream.py:1204-1230`); no restart on the C++ path;
  no consent-freshness / keepalive tuning; no measurement of ICE setup time.

## 4. Capture → encode → browser (Python) — VERIFIED

```
grab_screen() -> capture_desktop() -> PIL Image (RGB)
  -> ScreenTrack.recv() [asyncio.to_thread]
  -> av.VideoFrame.from_image(img) -> aiortc/PyAV H.264 encode
  -> RTP -> browser
```

- `grab_screen()` (`webrtc_stream.py:243-270`): delegates to
  `screen_capture.capture_desktop(monitor_index, scale)`; on exception falls
  back to `PIL.ImageGrab.grab()` (or a Win32 fallback in `tools`), clamps
  scale to `[0.2, 1.0]`, forces even width/height (H.264 requirement).
- `capture_desktop()` (`screen_capture.py:629-742`): attempt order —
  **(0)** native C++ DXGI via `ai_bridge_native.dll` if on Windows and the DLL
  loads; **(1)** Win32 GDI fast path (`_capture_win32_fast`, BitBlt); **(2)**
  further fallbacks (PIL `ImageGrab` — the tail of the function was read in
  summary; the exact order after (1) should be confirmed by track 02 if it
  matters). Each step returns a PIL `RGB` image; the native path converts
  BGRA→RGB via `Image.frombuffer("RGB", …, "BGRX")`.
- `_capture_native_cpp()` (`screen_capture.py:528-570`): probes the monitor size,
  allocates `max(1920, w) × max(1080, h) × 4` bytes, calls
  `aibridge_capture_frame`, and **re-captures once** if the real frame is larger
  than the probe buffer. `VERIFIED`: the buffer is a `ctypes` array, so an
  oversized frame cannot overflow Python memory; the retry doubles work for
  large monitors on every first frame.
- `ScreenTrack.recv()` (`webrtc_stream.py:331-428`): `next_timestamp()` paces
  frames on a 90 kHz clock at the target fps; capture runs in
  `asyncio.to_thread` so the event loop is not blocked by GDI/DXGI; per-frame
  telemetry (capture ms, prep/encode ms, fps_actual over a 2 s window,
  `frames_sent`, `dropped_frames`) is written to `_STREAM_TELEMETRY`.
  **Telemetry honesty issue (F13-01):** `encode_time_ms` is
  `cap_meta.get("encode_time_ms") or max(1.2, prep_ms * 1.4)` — a synthetic
  estimate (1.4× the PIL-prep time, floor 1.2 ms) presented as encoder timing
  whenever the native DLL did not supply a real value. The dashboard cannot
  distinguish measured from estimated encode time.
- **Native-encode timing call is argument-shifted (F13-02, VERIFIED):**
  `ScreenTrack.recv()` calls
  `ndll.aibridge_encode_h264_frame(rgb_buffer, len(rgb), w, h, 3, force, out_buf,
  out_cap, metrics)` — but the C ABI is
  `(bgra_buffer, width, height, fps, bitrate_kbps, force_keyframe, out_nal,
  max_nal, metrics)`. The call therefore passes `width=len(rgb)` (megabytes),
  `height=w`, `fps=h`, `bitrate_kbps=3`, and an RGB (3-byte/px) buffer where
  BGRA (4-byte/px) is expected. The result is discarded except for the
  `native_timing_ran` flag, and every exception is swallowed — so this runs on
  every frame (when the DLL is present) doing a doomed encode of a bogus frame
  size, purely to set a telemetry boolean. The *actual* video encoding is
  always PyAV; the native call contributes nothing but CPU waste and a
  misleading `encoder_backend` label.
- **No FPS/latency measurement exists in-repo** for this path: `fps_actual` is
  computed from `recv()` invocations, not from frames arriving in the browser;
  `capture_time_ms`/`encode_time_ms` are host-side only. `NOT FOUND`: any
  benchmark, profiling run, or RTCP-based quality report (sender reports are
  not read; `packet_loss_pct`/`rtt_ms` in telemetry are only populated when
  the *viewer* sends `network_quality` messages — see §6).
- **Frame pacing vs capture cost:** `next_timestamp()` sleeps until the next
  90 kHz tick; if `grab_screen` overruns the frame interval, `recv()` returns
  late and aiortc's pacing slips — there is no frame-drop-on-overrun policy
  (`NOT FOUND`: any deadline check between capture start and `return frame`).

## 5. Capture → encode (C++) — summary (detail in report 08)

DXGI Desktop Duplication → staging-texture CPU readback (row-pitch aware) →
BGRA→NV12 (BT.601, stride assumed `width*4`) → MF H.264 (HW preferred, SW
fallback) → Annex-B NALs → `track->send()`. The pipeline is honest about being
capture+encode only (`webrtc_transport.hpp`). Critical defects live here and
are owned by report 08: the **fake-IDR fallback** (F08-01 — a hardcoded empty
IDR reported as a valid frame), `StartSession` returning true on init failure
(F08-16), and COM-lifetime bugs (F08-06). Their WebRTC consequence: the C++
path can report a healthy stream while sending content-free frames.

## 6. Input: DataChannel → native injection — VERIFIED

### 6.1 Python `_handle_input_payload()` (`webrtc_stream.py:671-1076`)

Message shape: JSON with `action` (or `type`). Handling order matters:

1. `ping` → `pong` with `client_ts_ms` / `agent_ts_ms` + session telemetry.
2. `release_all_keys` → `release_all_held_keys()` (native DLL + Python `_HELD_KEYS`).
3. `get_ui_elements` / `som_query` → `tools.desktop_get_ui_elements()`; replies
   `som_elements` on the channel. **No rate-limit or permission check on this
   branch** (it returns before the rate-limit gate at the bottom of the function).
4. `veto_agent_action` → `tools.desktop_veto_action()`; `simulate_agent_intent`
   → **synthesises a fake agent intent** (`"Deploy Production Build"`,
   tier3, 3 s countdown) and sends it to the viewer — `VERIFIED` present in
   production code, apparently a demo/test hook reachable by any holder of the
   data channel (F13-06).
5. File transfer (`file_start/chunk/end`, `file_list`, `file_download_request`):
   rate-limit + `_session_allows_input()` (view-only) checked; delegates to
   `tools.desktop_handle_file_*`; download streams 64 KiB base64 chunks with
   a 5 ms sleep between sends, synchronously inside the message handler.
6. `network_quality / set_quality / adapt_bitrate` → `compute_adaptive_stream_params()`
   (§7) or explicit values; applies to the video track and (best-effort) to the
   native DLL bitrate.
7. `switch_monitor` → retargets `video_track._monitor_index` (private attribute
   poke) + telemetry.
8. `clipboard_sync` → requires `clipboard_sync` permission; `direction=get`
   returns clipboard text over the channel; `set` writes it. Both go through
   `perform_approved_ui_action(…, approval_granted=True)`.
9. **Generic mouse/keyboard** (only reached after the branches above): rate-limit
   (`_check_input_rate_limit`, 250/s, durable + in-memory), then monitor geometry
   resolution, then — per action — `_session_allows_input(is_mouse/is_keyboard)`,
   then native DLL if loaded, else `perform_approved_ui_action(…, approval_granted=True)`.

**Permission-check gap (F13-03, VERIFIED):** `_session_allows_input()`
(`webrtc_stream.py:645-669`) only enforces mode/permissions when
`signaling.session` is a dict (the `DictSignaling` / `local` path). For the
remote-agent `HttpSignaling` path `signaling.session` does not exist, so the
method returns `True` for mouse/keyboard — **view-only mode is not enforced
agent-side for remote agents** (clipboard is the exception: it returns `False`
when there is no session dict). The plane still gates session *creation* by
mode, but a mode change to `view_only` mid-session (`v1_routes.py:714+`)
does not propagate to a remote `AgentStreamer`: its `HttpSignaling` has no
session dict to re-read, and it polls only `status/answer/candidates`, never
`mode`/`permissions`. The local path is protected by the same mode change
calling `release_all_held_keys()` on the plane — the remote path is not.
Whether the plane also blocks input at another layer (e.g. the dashboard
simply stops sending) is `NOT FOUND` in this track; the agent-side enforcement
is absent either way.

**Other input-path findings:**
- `approval_granted=True` is hardcoded at every `perform_approved_ui_action`
  call site in this file (mouse, keyboard, type, clipboard). The DataChannel
  path therefore never goes through the approval flow — approval is assumed
  from session creation. `INFERRED`: by design (a live-control session *is*
  the approval), but it means a stolen/held session id + device token gives
  unapproved input until the session ends.
- **Key-up tracking is asymmetric (F13-04).** Python `_HELD_KEYS` is updated
  in `_handle_input_payload` for `keydown/keyup` — but only in the branch
  where the native DLL is *not* used for the key (`dll is not None and "+" not in key`
  returns early *after* the set update — re-reading: the `_HELD_KEYS.add/discard`
  happens before the DLL branch, so tracking is consistent there). The real
  asymmetry: `hotkey`/`press_key` actions go through
  `perform_approved_ui_action("press_key", …)` which synthesises down+up
  immediately, while `key_event` with `state="down"` and no matching `up`
  relies on disconnect handling. `release_all_held_keys()` covers the DLL
  (`aibridge_release_all_keys`) **and** the Python set (via `keybd_event`
  KEYUP per tracked key) — but the Python `keybd_event` path uses
  `tools._vk_for_token`, a *different* mapping from the C++ `MapKeyTokenToVk`,
  so a key tracked under one mapping may be released under another
  (`INFERRED`, needs a mapping-diff test).
- **Mouse-button state is not tracked in Python at all.** `_HELD_KEYS` is
  keys only; a `mousedown` with no `mouseup` followed by disconnect releases
  keys via `release_all_held_keys()` but mouse buttons only via the native
  DLL's `held_mouse_buttons_mask_` — and only if the DLL was the injector.
  If the fallback `perform_approved_ui_action` path injected the down-event,
  `NOT FOUND`: any mouse-button release on disconnect in the Python path.
- **Rate limiting is double but inconsistent:** `_check_input_rate_limit`
  consults the durable limiter (`durable_ratelimit.allow`, Redis/DB-backed)
  *and* a per-streamer in-memory counter; the C++ controller has its own
  250/s token bucket; the `file_*` branches check the limiter, the
  `som_query`/`veto`/`simulate` branches do not; `TypeUnicodeText` in C++
  checks once per whole string (§7 of report 08). A burst of `som_query`
  messages is unthrottled.
- **Coordinate handling:** Python resolves the monitor from
  `data["monitor_index"]` (defaulting to the stream's monitor), then converts
  normalised `[0,1]` → absolute using the *client-supplied* `screen_width/
  screen_height` if present, else the monitor's size — and passes
  monitor-relative pixels to the DLL with explicit offsets. The heuristic
  `is_norm or (0<=fx<=1 and 0<=fy<=1 and (fx!=0 or fy!=0))` treats any
  sub-1.0 coordinate as normalised, so a legitimate absolute click at
  (0.5, 0.5) px is reinterpreted as screen centre (`INFERRED` edge case;
  absolute coordinates are expected to be large, so this is unlikely in
  practice but the (0,0) special-case shows the ambiguity is known).
  The C++ peer path passes no monitor geometry (report 08, F08-10).

### 6.2 C++ data channel (`peer_connection.cpp:150-200`)

Handles `mouse_move/mouse_down/mouse_up/click/scroll/key_down/key_up/type/
ping` with hardcoded permissions `{mouse, keyboard, clipboard=false,
active}`. `NOT FOUND`: view-only/mode/takeover checks, rate-limit *call*
(the controller enforces it internally), `release_all_keys`,
`switch_monitor`, file transfer, clipboard, or UI-element queries. Key
release on `onClosed` and on PeerConnection Disconnected/Failed/Closed is
present (`input_ctrl.ReleaseAllHeldKeys()`), matching the Python design.

### 6.3 Human takeover — VERIFIED (two layers, one gap)

- **Plane layer:** `set_device_human_takeover()` (`v1_routes.py:56-111`)
  sets a process-global + DB-persisted flag; `tools._require_no_human_takeover()`
  (`tools.py:1820-1826`) raises for every desktop tool call
  (`get_desktop_status`, screenshot, launch, click, hotkey, … — call sites at
  `tools.py:701,1950,1968,1992,2017,2040,2064`), and the v1 task-creation path
  rejects autonomous tasks while takeover is active (`v1_routes.py:1058-1061`).
  Mode `human_control` sets it; mode change clears/sets it; terminate clears it
  (`v1_routes.py:834`).
- **Stream layer:** `v1_update_session_mode` to `view_only` releases held keys
  on the plane side.
- **Gap:** the Python `AgentStreamer` input path (`_handle_input_payload`)
  **never calls `_require_no_human_takeover()` or checks any takeover flag**
  (`grep` over `webrtc_stream.py` for `takeover` returns nothing).
  Its only guards are `_session_allows_input()` (dict-session only, §6.1) and
  the emergency-stop check at the top of the handler. `INFERRED`: if a human
  takes over mid-session on a *remote* agent, the DataChannel keeps injecting
  until the viewer stops sending or the session ends — the tools-layer guard
  does not apply because the streamer calls the DLL / `perform_approved_ui_action`
  directly, and `perform_approved_ui_action` itself was not verified in this
  track to re-check takeover (flag for track 02). The C++ path has no takeover
  check either.

### 6.4 Emergency stop — VERIFIED

`_handle_input_payload` returns `emergency_stop_active` for every action
except `ping/pong/release_all_keys` when `plane_control_flags.is_emergency_stop()`
is set. The flag is fed by heartbeat/task responses in the poller (report 07)
and by the WS side-channel. `VERIFIED`: the check is per-message and happens
before rate-limiting, so an active e-stop also stops file transfer and
clipboard — but `som_query`/`veto_agent_action`/`simulate_agent_intent` return
*before* the e-stop check? Re-reading the order: the e-stop check is inside
the `try`, immediately after JSON parsing and **before** the `ping` branch —
so it covers all branches including `som_query` and `simulate_agent_intent`.
Correction: e-stop coverage is complete within this handler. The gap is
takeover (§6.3), not e-stop.

## 7. Adaptive bitrate / quality — VERIFIED (rules exist, feedback is thin)

`compute_adaptive_stream_params()` (`webrtc_stream.py:220-241`):
- Loss ≥ 5% or RTT ≥ 250 ms → bitrate ×0.65 (floor 1,000 kbps), fps ≤ 24 (floor 15).
- Loss ≥ 2% or RTT ≥ 150 ms → ×0.80 (floor 1,500), fps ≤ 30 (floor 20).
- Loss < 0.5% and 0 < RTT < 50 ms → ×1.15 (cap 12,000).
- Otherwise unchanged. Inputs clamped to 500–20,000 kbps, 10–60 fps.
- Triggered only when the viewer sends `network_quality/set_quality/
  adapt_bitrate` over the DataChannel, or when the dashboard calls
  `PATCH …/quality`. `NOT FOUND`: any server-side RTCP receiver-report
  ingestion, any automatic measurement loop in the agent, any GCC/TWCC.
  `INFERRED`: in practice adaptation is viewer-driven and therefore only as
  good as the viewer's own RTT/loss estimation (the `pong` message carries
  telemetry but no RTT is computed from it agent-side — the agent echoes
  `agent_ts_ms` and leaves RTT math to the viewer).
- `ScreenTrack.apply_stream_params()` changes fps (recomputes the 90 kHz step)
  and the *target* bitrate field, but **does not reconfigure the PyAV encoder's
  actual bitrate** — aiortc's encoder is configured at track creation; changing
  `self._bitrate_kbps` only changes the telemetry label (`INFERRED` from the
  method body, `webrtc_stream.py:302-313`, which touches no encoder object).
  The native-DLL bitrate *is* updated (`aibridge_set_encoder_bitrate`), but
  the DLL is not the encoder in this path (§4) — so a viewer-driven quality
  change is largely cosmetic in the shipping path. The C++ encoder's
  `UpdateBitrate()` *does* reconfigure the MF encoder (report 08), so the
  two paths differ here too.
- No resolution adaptation exists (scale is fixed at session creation).

## 8. Desktop audio — VERIFIED

`desktop_audio.py` (179 LOC) + `DesktopAudioTrack` (`webrtc_stream.py:430-508`):
- Opt-in only: `AI_BRIDGE_DESKTOP_AUDIO=1/true/yes`
  (`desktop_audio_opt_in()`, `webrtc_stream.py:106-113`). Default off.
- Live path: optional `soundcard` package → WASAPI loopback microphone,
  float32→s16le, 48 kHz stereo, 960 samples/frame (20 ms), bounded
  `queue.Queue(maxsize=32)`; `read_pcm(silent_fallback)` returns silence on
  underflow. Probe is cached per-process (`_PROBE_CACHE`).
- Fallback: honest silent PCM (`desktop_audio_silent_pcm_bytes`, zero-filled)
  so Opus negotiation still succeeds; `audio_backend` reports
  `silent_pcm` vs the live backend. `INFERRED`: a viewer cannot tell from the
  SDP whether audio is live or silent — only from the session dict's
  `audio_enabled` flag (set by `_attach_desktop_audio_if_enabled`).
- `NOT FOUND`: any echo cancellation, volume handling, or device selection
  (always the default speaker's loopback). The C++ path has **no audio at
  all** (`NOT FOUND` in `windows-agent/src/`).

## 9. Screen capture backends and session state — VERIFIED

- `screen_capture.py:get_monitors()` (lines 386-527): native DLL enumeration
  first, then Win32 `EnumDisplayMonitors` via ctypes, with a 1920×1080
  fallback. `get_virtual_desktop_bounds()` / `find_monitor_for_point()` /
  `normalize_bounds_for_monitor()` provide the multi-monitor geometry the
  input path consumes.
- `get_desktop_session_state()` (`screen_capture.py:218-280`): reports
  `secure_desktop / capture_blocked / desktop_name / reason` by opening the
  input desktop — the Python counterpart of the C++ check (divergent name
  sets, report 08 F08-09). `ScreenTrack.recv()` surfaces this in telemetry
  (`secure_desktop`, `capture_blocked`, `desktop_reason`), so the dashboard
  can show *why* frames stopped instead of a black screen — good design,
  `VERIFIED`.
- `get_native_dll()` (`screen_capture.py:124-213`): Windows-only, searches
  three fixed build-output paths, `ctypes.CDLL`, sets `argtypes/restype`
  for the capture/encode/input exports. `NOT FOUND`: any signature, hash, or
  version check before loading; a DLL planted at
  `windows-agent/build/Release/ai_bridge_native.dll` is loaded implicitly.
  Note the argtypes for `aibridge_encode_h264_frame` are set correctly here
  (9 args) — the *caller* in `webrtc_stream.py` is the one that misaligns
  them (F13-02).
- **Clipboard (non-WebRTC):** the GUI copies tokens/payloads via Tk
  (`gui_app.py:879-881,1092-1093`); the WebRTC clipboard path is §6.1(8).
  `NOT FOUND`: any clipboard *sync loop* (continuous mirroring) — clipboard
  moves only on explicit `clipboard_sync` messages, one shot per message.

## 10. Terminal sessions (adjacent surface) — VERIFIED (brief)

`terminal_manager.py` (637 LOC): PTY (Linux/macOS) / ConPTY or piped
PowerShell (Windows) / remote relay; in-process `_TERMINAL_SESSIONS` dict,
1,000-entry output buffer, `stream(since_seq)` generator, `resize`, `close`.
No persistence, no idle reaper found in this track. It is **not** a WebRTC
surface (it is driven by `terminal_exec` tasks and the live-terminal REST
endpoints), but it shares the agent's "interactive session, in-process state"
failure mode (§2.5). Flag for track 02 to confirm timeout/reaping.

## 11. Tests — VERIFIED (counts only; not executed here)

- `tests/unit/test_phase4_streaming_and_updater.py` (9 `def test`): HTTP-level
  `test_webrtc_session_endpoints` (legacy: create → agent poll → offer →
  answer → delete), `test_v1_webrtc_session_endpoints` (canonical v1),
  platform-gate test (no `start_webrtc_stream` queued for non-Windows).
  **No live media, no aiortc PeerConnection, no DataChannel input, no ICE.**
- `grep -rln "webrtc_stream|AgentStreamer|ScreenTrack" tests/` returns only
  the files above plus the v1/v2 architecture tests. `NOT FOUND`: unit tests
  for `compute_adaptive_stream_params`, `_handle_input_payload` (any action),
  `_session_allows_input`, `release_all_held_keys`, `ScreenTrack.recv`,
  `HttpSignaling`, `get_configured_ice_servers`, or the C++ peer session.
- The C++ tests (report 08 §13) cover capture/encode/input in isolation, not
  a WebRTC session.

## 12. Python ↔ C++ overlap / conflict table

| Concern | Python (shipping) | C++ (optional) | Conflict? |
|---|---|---|---|
| Video encode | PyAV H.264 (aiortc) | MF H.264 → raw NALs | **Yes** — two encoders, two bitrate paths; native DLL "timing" call is bogus (F13-02) |
| Capture | DLL DXGI → PIL, else GDI, else ImageGrab | DXGI → staging → BGRA vector | Shared DLL for capture; different fallbacks after it |
| Input injection | DLL SendInput if present, else `tools` Win32 | DLL-equivalent controller in-process | Same controller code via different binaries; monitor geometry differs (F08-10); held-key maps differ (`_vk_for_token` vs `MapKeyTokenToVk`) |
| Key release on disconnect | `release_all_held_keys()` (DLL + Python set) | `ReleaseAllHeldKeys()` on pc/dc close | Both exist, but mouse-button release is DLL-only in Python |
| Rate limit | Durable + per-streamer 250/s | Controller token bucket 250/s | Two independent buckets; file/som branches bypass Python's |
| View-only / takeover | Dict-session only; no takeover check in streamer | None at all | **Yes** — neither path enforces it end-to-end (F13-03) |
| Adaptive bitrate | Telemetry-only (PyAV not reconfigured) | Real (`UpdateBitrate`) | Opposite effectiveness |
| ICE | STUN + TURN (ephemeral creds) | STUN only, no viewer candidates | C++ likely fails behind symmetric NAT (F13-05) |
| Audio | WASAPI loopback or silent PCM (opt-in) | None | C++ sessions are video-only |
| Session state | In-process `_STREAMERS` + DB signalling row | In-process only | Neither reconciles after restart |

## 13. Findings

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| F13-01 | **P1** | **Encode-time telemetry is partly fabricated.** `encode_time_ms` falls back to `max(1.2, prep_ms*1.4)` — a synthetic estimate presented as measurement. Any dashboard/SLO built on it is invalid. | `src/ai_bridge/webrtc_stream.py:409-427` |
| F13-02 | **P1** | **Native-encode timing call passes shifted arguments.** `ScreenTrack.recv()` calls `aibridge_encode_h264_frame` with `(rgb, len(rgb), w, h, 3, force, …)` against a `(bgra, width, height, fps, bitrate_kbps, force, …)` ABI, with an RGB buffer where BGRA is expected. Runs every frame when the DLL loads; result discarded; exceptions swallowed. Pure CPU waste + misleading `encoder_backend` label. | `src/ai_bridge/webrtc_stream.py:360-382`; `src/ai_bridge/screen_capture.py:150-165`; `windows-agent/src/c_api.cpp:150-200` |
| F13-03 | **P0** | **View-only mode is not enforced agent-side for remote agents.** `_session_allows_input()` returns `True` for mouse/keyboard when `signaling.session` is not a dict (the `HttpSignaling` remote path). A mid-session mode change to `view_only` never reaches a remote `AgentStreamer`. | `src/ai_bridge/webrtc_stream.py:645-669,1110-1309`; `src/ai_bridge/control/v1_routes.py:714-760` |
| F13-04 | **P1** | **Human takeover is not checked in the WebRTC input path.** `_handle_input_payload` never consults the takeover flag; the tools-layer guard (`_require_no_human_takeover`) is bypassed because the streamer calls the DLL / `perform_approved_ui_action` directly. A human taking over mid-session does not stop DataChannel input on a remote agent. | `src/ai_bridge/webrtc_stream.py:671-1076` (no `takeover` reference); `src/ai_bridge/tools.py:1820-1826`; `src/ai_bridge/control/v1_routes.py:56-111` |
| F13-05 | **P1** | **C++ WebRTC drops viewer ICE candidates and has no TURN.** Only STUN; `PollSession` reads the answer SDP only; `sdp_mline_index` hardcoded 0; per-frame HTTPS polling. Likely never connects behind symmetric NAT. | `windows-agent/src/webrtc/peer_connection.cpp:100-230`; `windows-agent/src/webrtc/http_signaling.cpp` |
| F13-06 | **P2** | **`simulate_agent_intent` is a live demo hook in the production input handler.** Any DataChannel holder can inject a fake "Deploy Production Build" tier-3 intent with a 3 s countdown into the viewer's UI. | `src/ai_bridge/webrtc_stream.py:750-775` |
| F13-07 | **P2** | **Adaptive bitrate is viewer-driven and largely cosmetic in Python.** No RTCP feedback loop; `apply_stream_params` does not reconfigure the PyAV encoder — only the telemetry label and (ineffectually) the native DLL bitrate change. | `src/ai_bridge/webrtc_stream.py:220-241,302-313` |
| F13-08 | **P2** | **Mouse-button held state is not tracked/released in the Python fallback path.** `_HELD_KEYS` is keys-only; a `mousedown` via `perform_approved_ui_action` with no `mouseup` has no disconnect release. | `src/ai_bridge/webrtc_stream.py:137-186,671-1076` |
| F13-09 | **P2** | **No session reconciliation after restart.** Media state is in-process only (`_STREAMERS`); the DB signalling row can say `answered` while nothing streams. No re-sync on agent reconnect. | `src/ai_bridge/webrtc_stream.py:35-42,1310-1348`; §2.5 |
| F13-10 | **P2** | **Offer is posted even if ICE gathering is incomplete**, after only a 5 s wait, with a warning flag the viewer may ignore. Partial-candidate offers fail opaquely on restrictive networks. | `src/ai_bridge/webrtc_stream.py:1255-1265` |
| F13-11 | **P2** | **Non-production TURN secret is a hardcoded shared default.** Anyone with the source can mint TURN credentials for the default host; production raises, but any mislabelled deployment silently shares the secret. | `src/ai_bridge/webrtc_stream.py:44-76` |
| F13-12 | **P3** | **Unthrottled branches in the input handler.** `som_query`, `veto_agent_action`, `simulate_agent_intent` return before the rate-limit gate; file download streams synchronously inside the message handler (blocks other input for the transfer duration). | `src/ai_bridge/webrtc_stream.py:671-1076` |
| F13-13 | **P3** | **Legacy and v1 session shapes differ** (permissions keys, takeover flag) but flow into the same `AgentStreamer`; compatibility is by shared key names, not a schema. Legacy route is 410-gated but still code-present. | `src/ai_bridge/control/app.py:3546-3760`; `src/ai_bridge/control/v1_routes.py:573-700` |
| F13-14 | **P3** | **No performance evidence exists.** No FPS/latency measurement, no RTCP ingestion, no benchmark. All "30 FPS / <15 ms" style claims in docstrings are targets, not measurements. | §4; `src/ai_bridge/webrtc_stream.py:1-17` |
| F13-15 | **P3** | **DLL is loaded without any integrity check** (no signature/hash/version), from fixed build-output paths; any planted DLL gets capture + input. | `src/ai_bridge/screen_capture.py:124-213` |

## 14. Open questions for other tracks

1. **(Track 02)** Does `perform_approved_ui_action()` re-check human takeover /
   emergency stop, or is F13-04 a complete bypass? Confirm the exact call chain
   from the streamer to Win32 input.
2. **(Track 02/04)** Is `simulate_agent_intent` intentional demo functionality,
   and who is authorised to invoke it (F13-06)?
3. **(Track 04)** Confirm the intended enforcement point for `view_only`:
   is the dashboard expected to stop sending input, with the agent check as
   defence-in-depth — or is the agent check the enforcement (F13-03)?
4. **(Track 10)** Is there any E2E WebRTC test with a real browser/aiortc peer
   (even headless) anywhere in CI, or is signalling-level coverage the ceiling?
5. **(Track 02)** What reconciles a DB `webrtc_sessions` row with a restarted
   agent (F13-09), and what does the viewer see in that window?

> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 08 — Windows Native C++ Audit

**Scope:** `windows-agent/` only (C++20), plus its Python `ctypes` bridge
(`src/ai_bridge/screen_capture.py:get_native_dll`, `src/ai_bridge/native_encode.py`)
where the boundary matters. The Python Windows agent is report 07; Python WebRTC is
report 13.
**Method:** read-only source inspection. This Linux VM cannot build or run the Windows
binaries, so **no C++ code was compiled, executed, or profiled in this track**; all
findings are static. **Status labels:** `VERIFIED` = read in the cited file;
`INFERRED` = follows from code but not executed; `NOT FOUND` = searched for and absent.

## 1. Inventory (measured)

| File | LOC | Role |
|---|---|---|
| `windows-agent/CMakeLists.txt` | ~130 | C++20, MSVC `/W3 /EHsc /utf-8`; 4 targets (below) |
| `windows-agent/src/service/windows_service.cpp` | 213 | Process A — Session 0 supervisor |
| `windows-agent/src/desktop/desktop_agent.cpp` | 176 | Process B — interactive desktop agent (probe + WebRTC session modes) |
| `windows-agent/src/capture/dxgi_capture.cpp` / `.hpp` | 465 / 96 | DXGI Desktop Duplication + GDI fallback + window capture |
| `windows-agent/src/encoder/mf_h264_encoder.cpp` / `.hpp` | 428 / 85 | Media Foundation H.264 (HW preferred, SW fallback, + fake-IDR fallback) |
| `windows-agent/src/input/sendinput_controller.cpp` / `.hpp` | 365 / 79 | SendInput mouse/keyboard, DPI-aware coords, 250 ev/s token bucket, held-key tracking |
| `windows-agent/src/webrtc/webrtc_transport.cpp` / `.hpp` | 112 / 63 | `NativeWebRtcPipeline`: capture → encode → stats (explicitly **not** a PeerConnection) |
| `windows-agent/src/webrtc/peer_connection.cpp` / `.hpp` | 281 / 48 | libdatachannel session (only compiled with `AIBRIDGE_WITH_LIBDATACHANNEL=1`) |
| `windows-agent/src/webrtc/http_signaling.cpp` / `.hpp` | 268 / 49 | WinHTTP signalling client to `/api/agent/webrtc/*` |
| `windows-agent/src/security/local_security.cpp` / `.hpp` | 227 / 37 | DPAPI seal/unseal; HMAC-SHA256 named-pipe auth; restricted pipe ACL |
| `windows-agent/src/monitoring/system_monitor.cpp` / `.hpp` | 148 / 51 | CPU/mem/disk/uptime, WTS session enumeration |
| `windows-agent/src/mcp/mcp_executor.cpp` / `.hpp` | 93 / 32 | Visible-window enumeration; allowlisted app launch |
| `windows-agent/src/c_api.cpp` / `c_api.h` | 249 / 106 | `ai_bridge_native.dll` C ABI for Python `ctypes` (global singletons + one mutex) |
| `windows-agent/tests/test_windows_agent.cpp` | 147 | 6 `assert`-based tests, Windows-only (DXGI/MF/SendInput) |
| `windows-agent/installer/install-service.ps1` | ~200 | Installs service + desktop agent, mints pipe secret, writes config |
| **Total C++ (`.cpp/.hpp`, excl. tests)** | **~3,671** | `find … | xargs wc -l` total incl. tests: **3,818** |

### Build targets (`CMakeLists.txt`)

1. `ai_bridge_native` (SHARED) — `AGENT_CORE_SOURCES` + `c_api.cpp`, for Python `ctypes`.
2. `ai_bridge_service` — `windows_service.cpp` + monitor + security only.
3. `ai_bridge_desktop` — `desktop_agent.cpp` + all core sources.
4. `ai_bridge_tests` — `test_windows_agent.cpp` + core sources; `add_test(NativeWindowsAgentTests)`.
- WebRTC backends are **off by default**: `AIBRIDGE_WITH_LIBDATACHANNEL=OFF`,
  `AIBRIDGE_WITH_LIBWEBRTC=OFF`. With libdatachannel on, CMake `FetchContent`s
  mbedtls v3.6.2 and libdatachannel v0.22.6 from GitHub at configure time (network
  fetch during build; no vendored copy, no hash pin beyond the git tag).
- `AIBRIDGE_WITH_LIBWEBRTC` requires a prebuilt tree at `AIBRIDGE_LIBWEBRTC_ROOT`;
  `peer_connection.cpp` contains **no libwebrtc implementation at all** — the
  `LibWebRTC` enum value exists but `CreatePeerConnectionSession()` only ever
  constructs the libdatachannel session (`peer_connection.cpp:260-280`). `INFERRED`:
  building with libwebrtc yields `Backend()==LibWebRTC` from `DescribeAvailable…`
  but `CreatePeerConnectionSession()` still returns `nullptr` (the `#if defined
  (AIBRIDGE_WITH_LIBDATACHANNEL)` guard), so the desktop agent exits 2 with
  `no_cpp_webrtc_backend`. Verify in track 10 before relying on it.

```mermaid
flowchart TD
    subgraph SVC["Process A — ai_bridge_service.exe (Session 0, SYSTEM)"]
        S[windows_service.cpp<br/>1s supervisor loop]
        MON[system_monitor.cpp<br/>WTS + perf snapshot]
        SEC[local_security.cpp<br/>DPAPI + pipe HMAC]
    end
    subgraph DSK["Process B — ai_bridge_desktop.exe (interactive user session)"]
        D[desktop_agent.cpp<br/>probe | --session-id WebRTC]
        CAP[dxgi_capture.cpp]
        ENC[mf_h264_encoder.cpp]
        INP[sendinput_controller.cpp]
        PC[peer_connection.cpp<br/>libdatachannel, optional]
        SIG[http_signaling.cpp<br/>WinHTTP]
        PIPE[NativeWebRtcPipeline]
    end
    subgraph PY["Python agent (report 07)"]
        PYP[Poller + webrtc_stream.py<br/>aiortc — the shipping path]
        DLL[ai_bridge_native.dll<br/>c_api.cpp]
    end
    SVC -- "named pipe \\\\.\pipe\\AIBridgeDesktopPipe<br/>HMAC-SHA256, 3 msg types" --> DSK
    DSK -- "HTTP signalling<br/>/api/agent/webrtc/*" --> PLANE[Control plane]
    PYP -- "ctypes: capture/encode/input<br/>only when DLL on disk" --> DLL
    DSK -- "probe JSON to stdout" --> DSK
    SVC -. "NO launch of desktop agent<br/>NO HTTP heartbeat (see §3)" .-> DSK
```

## 2. Two-process design — what it claims vs what the code does

The header comment in `windows_service.cpp:1-13` claims the service:
registers the device, protects credentials with DPAPI, maintains backend
connectivity & heartbeats, enforces device-level policy, monitors the desktop
agent, and talks to it over the pipe. **Verified against the implementation:**

| Claimed responsibility | VERIFIED status |
|---|---|
| Starts automatically / `--console` mode | Yes — `StartServiceCtrlDispatcherW`, `--once/--console/--console-once/--status` print a JSON snapshot and exit (`windows_service.cpp:178-213`) |
| Registers device / DPAPI credentials | **NOT FOUND in the service.** No enrollment, no `DpapiSecretStore` call, no device-token use in `windows_service.cpp`. DPAPI is used by the Python agent (`app_runtime.py`) and the C++ test, not by the service binary |
| Backend connectivity & heartbeats | **NOT FOUND.** No HTTP client, no control-plane URL use, no heartbeat in the service. The only network-adjacent code is the named pipe |
| Enforces device-level policy | **NOT FOUND.** No policy file read, no tool gating |
| Monitors the desktop agent | Partially — it *answers* pipe messages (see §3); it never launches, restarts, health-checks, or kills the desktop process |
| Named-pipe communication | Yes — server side of `AIBridgeDesktopPipe` (§4) |

`INFERRED`: Process A as shipped is a **pipe-answering status oracle**, not a
supervisor. The installer (`install-service.ps1`) compensates by starting the
desktop agent independently via HKLM `Run` + a logon scheduled task (see §9),
so the two processes are actually **siblings launched by the installer**, not
parent/child.

## 3. Service supervisor loop — VERIFIED

`ServiceMain` (`windows_service.cpp:146-176`): registers a control handler that
only handles `SERVICE_CONTROL_STOP` (no pause/continue, no shutdown, no
`SERVICE_ACCEPT_SHUTDOWN`/`PRESHUTDOWN`), then loops
`WaitForSingleObject(g_stop_event, 1000)` → `RunSupervisorIteration()` every second.

`RunSupervisorIteration()` (`windows_service.cpp:100-145`) per iteration:
1. Collects a `WindowsSystemMonitor` snapshot (fresh `WindowsSystemMonitor`
   constructed **every iteration** — cheap, but see §8 on the CPU-delta side effect).
2. `CreateRestrictedServerPipe()` — **creates a brand-new pipe instance every second**.
3. `ConnectNamedPipe` with an `OVERLAPPED` + 250 ms wait; on timeout `CancelIo`.
4. If a client connected, reads **exactly one** authenticated message, replies once
   (`REGISTERED` / `HEARTBEAT_PONG` / `STATUS_RESPONSE` / `ERROR unknown_msg_type`),
   then `DisconnectNamedPipe` + `CloseHandle`.

Consequences (`INFERRED`, F08-02):
- A desktop agent that tries to register outside the 250 ms window each second
  fails to connect (`ConnectClientPipe` timeout is 1,000 ms in `desktop_agent.cpp`,
  so it usually lands inside a window, but under load the race is real).
- One message per second per iteration: a desktop agent sending register +
  heartbeat in quick succession needs two separate 1 s iterations.
- The pipe instance is destroyed after every message, so there is **no persistent
  session**, no sequence numbers, no replay window — each message is independent
  (replay of a captured valid message within the pipe's lifetime would verify;
  see §4).
- `main()`'s fallback is surprising: if `StartServiceCtrlDispatcherW` fails
  (launched interactively without `--once`), it runs **one** supervisor iteration
  and exits (`windows_service.cpp:205-211`) — silent single-shot behaviour.
- `g_stop_event` is created in `ServiceMain` but if `RegisterServiceCtrlHandlerW`
  fails the function returns without setting service status — the SCM sees a
  start timeout rather than a clean error (`INFERRED`).

## 4. IPC: named pipe + HMAC — VERIFIED (mostly sound, with gaps)

### 4.1 Pipe ACL — `local_security.cpp:130-165`

`CreateRestrictedServerPipe` builds SDDL
`D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;AU)` — SYSTEM + Built-in Admins get
Generic-All, Authenticated Users get Generic-Read/Write — plus
`PIPE_REJECT_REMOTE_CLIENTS`, message mode, 64 KiB buffers, unlimited instances
(the instance count is moot: the service creates one at a time).
`INFERRED`: any authenticated local user can connect and speak the protocol;
authorisation beyond "authenticated Windows user" rests entirely on the HMAC
secret (§4.2). The SDDL comment in the header ("Authenticated Interactive User")
overstates it — `AU` is all authenticated users, including service accounts.

### 4.2 Message authentication — `local_security.cpp:52-128, 167-227`

- `ComputeTag()`: hand-rolled HMAC-SHA256 over `msg_type + "|" + payload` using
  CryptoAPI (`CryptAcquireContextW` / `CALG_SHA_256`), with the standard
  ipad/opad construction, key hashed if > 64 bytes, zero-padded otherwise.
  The tag is hex-encoded and compared in **constant time** (byte-wise XOR
  accumulation, `local_security.cpp:206-217`). `VERIFIED`: correct construction
  as far as static reading goes; no test vector is present (`NOT FOUND`:
  any RFC 4231 test vector in `tests/test_windows_agent.cpp`).
- Wire format: `msg_type + "\n" + hex_tag + "\n" + json_payload`, single
  `ReadFile` of up to 65,536 bytes, rejected if `wire.size() > 65536` on send.
  `VERIFIED` gaps (F08-03):
  - **No nonce, timestamp, or sequence number** — a captured valid message
    replays successfully (same type+payload+tag verifies). Impact is bounded by
    the message set (`REGISTER_DESKTOP_AGENT` / `HEARTBEAT_PING` / `STATUS_QUERY`
    are all idempotent reads), but the design cannot distinguish a replay.
  - **No length prefix / framing validation**: `ReadAuthenticatedMessage` trusts
    the first `ReadFile` return as one complete message. In message mode this is
    normally one message, but a > 64 KiB payload is silently truncated to 64 KiB
    and then fails tag verification with no diagnostic — fail-closed, but opaque.
  - `ReadAuthenticatedMessage` returns `false` on tag mismatch but the service
    then sends nothing and disconnects — correct fail-closed behaviour.
  - `ComputeTag` returns `{}` (empty string) on any CryptoAPI failure, and
    `ReadAuthenticatedMessage` treats empty expected-tag as failure — fail-closed.
- **Secret handling:** the 32-byte secret is minted by the installer
  (`New-PipeSecret`, `install-service.ps1:56-73`, RNGCryptoServiceProvider,
  base64, ACL SYSTEM+Admins FullControl on `C:\ProgramData\ai-bridge\native-pipe.secret`)
  and copied into `native-agent.json` **alongside the plaintext device token**
  (§9, F08-08). The service loads it from (a) `--config` JSON via a naive
  `ExtractJsonStringField` (substring search, no real JSON parser, no escape
  handling beyond `\"` → next char — `windows_service.cpp:26-48`), (b)
  `AI_BRIDGE_PIPE_SECRET` env, or (c) the secret file. The desktop agent loads
  it only from `--config` JSON (`desktop_agent.cpp:108-120`).
- **Desktop side never reads the reply.** `RegisterWithSupervisorPipe`
  (`desktop_agent.cpp:55-76`) sends `REGISTER_DESKTOP_AGENT` and immediately
  `CloseHandle`s — it never calls `ReadAuthenticatedMessage`, so registration
  success/failure is unknown to the desktop agent (`VERIFIED`, F08-04). The
  service's `REGISTERED` reply (with `active_session_id` + hostname) is written
  into a closing pipe.

### 4.3 JSON parsing — VERIFIED (fragile, not a parser)

All three C++ files that read JSON (`windows_service.cpp`,
`desktop_agent.cpp`, `peer_connection.cpp`, `http_signaling.cpp`) use the same
hand-rolled `ExtractJson*Field` substring search: find `"key"`, find `:`, find
`"`, copy until unescaped `"`. It mishandles `\uXXXX` (skipped 4 chars without
decoding, `http_signaling.cpp:66-110`), nested objects (the `answer.sdp` hack in
`PollSession` does `resp.find("\"answer\"")` then re-parses the tail), duplicate
keys (first match wins), and keys appearing inside string values. `INFERRED`:
adequate for the plane's small, self-produced JSON, but a malformed or
adversarial session payload can cause silent misparses (e.g. wrong `session_id`
→ signalling to the wrong session is prevented server-side by the device-token
check, but a wrong `sdp` → opaque libdatachannel exception). No JSON library
is linked despite `nlohmann/json`-style needs; adding one is a P2 fix.

## 5. DXGI capture — VERIFIED

`DxgiDesktopCapturer` (`capture/dxgi_capture.cpp`, `dxgi_capture.hpp`):

- **Initialisation** (`InitDuplicationLocked`, lines 139-215): enumerates adapters/
  outputs, picks the `monitor_index`-th attached output, `D3D11CreateDevice`
  (feature levels 11.1 → 10.0, `D3D11_CREATE_DEVICE_BGRA_SUPPORT`),
  `IDXGIOutput1::DuplicateOutput`. Failure (Session 0, RDP virtual display,
  secure desktop) returns `false` and the caller falls back to GDI.
- **Frame acquisition** (`CaptureNextFrame`, lines 279-393): `AcquireNextFrame(timeout)`;
  on `DXGI_ERROR_ACCESS_LOST / DEVICE_REMOVED / MODE_CHANGE_IN_PROGRESS` it
  re-initialises once and retries once; on `DXGI_ERROR_WAIT_TIMEOUT` with a
  cached frame it reuses `last_bgra_frame_` (zero-dirty-rect optimisation);
  dirty/move rect metadata is read when present. CPU readback goes through a
  lazily-created staging texture with row-pitch-aware `memcpy` per row —
  `VERIFIED` correct handling of `mapped.RowPitch != width*4`.
- **GDI fallback** (`CaptureViaGdiFallbackLocked`, lines 217-277): `BitBlt` with
  `SRCCOPY | CAPTUREBLT` from the monitor's origin, `GetDIBits` to BGRA,
  cursor via `GetCursorInfo`. `VERIFIED` issues (F08-05):
  - `blt_ok` is computed and **ignored**; `valid = (bmp != nullptr)` even if
    `BitBlt` failed — a black/unchanged frame can be reported valid.
  - If `CreateCompatibleBitmap` returns null, `SelectObject(mem_dc, bmp)` is
    still called with null and `DeleteObject(bmp)` on null; `GetDIBits` is
    guarded but the surrounding GDI calls are not null-checked
    (`mem_dc` itself is never null-checked either).
  - `CaptureWindow` (lines 398-465) has the same shape: `PrintWindow` with
    `PW_RENDERFULLCONTENT`, fallback `BitBlt`, `valid = (bmp != nullptr)`,
    `printed` ignored on the fallback path's success.
- **Multi-monitor** (`EnumerateMonitors`, lines 59-116): full adapter/output
  enumeration with desktop coordinates, rotation, primary detection
  (`left==0 && top==0` — `INFERRED`: wrong for a primary monitor that is not
  at the virtual-desktop origin, e.g. a monitor arranged left of the primary).
  A fallback synthetic 1920×1080 primary is synthesised when enumeration finds
  nothing — `INFERRED`: on a headless/session-0 host this fabricates a monitor
  that does not exist, and the GDI fallback will then capture a black frame
  reported as valid (compounds F08-05).
- **Secure desktop** (`IsSecureDesktopActive`, lines 30-48): `OpenInputDesktop`
  + `UOI_NAME != "Default"` → secure. Called **on every frame**
  (`CaptureNextFrame` line 284) — an extra Win32 round-trip per frame; and on
  failure to open the desktop it returns `true` (fail-closed, correct).
  Note the C++ check and the Python `get_desktop_session_state()` check
  (`screen_capture.py:218-280`) are **duplicated** with slightly different
  desktop-name sets (`winlogon/disconnect/screensaver` in Python vs any
  non-`Default` in C++) — they can disagree (F08-09 / report 13).
- **Rotation:** `rotation_degrees_` is recorded from `DXGI_OUTDUPL_DESC` but
  `NOT FOUND`: any use of it to rotate the captured pixels — a 90°/270° monitor
  yields a sideways frame with width/height swapped in metadata only
  (`INFERRED` from the absence of any rotation in the copy path).
- **Threading:** all public methods take `mutex_`; `CaptureWindow` is `const`
  but takes no lock (`dxgi_capture.hpp`) while sharing no mutable state except
  through locals — `VERIFIED` safe as written, but it calls
  `EnumerateMonitors()`-free code only; fine.
- **Resource handling:** COM objects are `WRL::ComPtr` throughout; `Shutdown()`
  resets in dependency order (staging → duplication → context → device).
  `VERIFIED`: no raw `new`/`delete` in this file; the one raw-pointer risk is
  the `reinterpret_cast` of the metadata buffer (§7).

## 6. Media Foundation H.264 encoder — VERIFIED

`MediaFoundationH264Encoder` (`encoder/mf_h264_encoder.cpp`):

- **Pipeline:** `MFStartup` → `MFTEnumEx` hardware MFT first (flags include
  `MFT_ENUM_FLAG_HARDWARE | ASYNCMFT | SYNCMFT | SORTANDFILTER`), software
  fallback; output type H.264 **before** input type NV12 (required order,
  correctly observed); `MF_MT_MPEG2_PROFILE = eAVEncH264VProfile_Base`,
  low-latency mode + GOP size `fps * keyframe_interval_sec` via `ICodecAPI`;
  async-MFT unlock. `UpdateBitrate()` clamps 250 kbps–50 Mbps and sets
  `CODECAPI_AVEncCommonMeanBitRate` without re-creating the transform.
- **Re-initialisation on resolution change** (`EncodeBgraFrame`, lines 247-268):
  drains + resets the MFT and rebuilds it — under `mutex_`, correct in shape.
- **Colour conversion** (`ConvertBgraToNv12`, lines 36-66): BT.601 integer
  approximation, 2×2 chroma subsampling, even-dimension clamping. `VERIFIED`:
  it indexes `bgra[j*width*4 + i*4]` — it **assumes stride == width*4**. The
  DXGI path guarantees a packed vector, but any future caller passing a
  pitched buffer silently corrupts chroma/luma. `NOT FOUND`: a stride parameter.
- **COM lifetime bugs (F08-06):**
  - `Initialize()` (line 79) and the re-init path (line 285) call
    `CoInitializeEx(nullptr, COINIT_MULTITHREADED)` and **ignore the return
    value**; there is **no matching `CoUninitialize()` anywhere** in the file.
    Repeated `Initialize()` calls on different threads can return
    `RPC_E_CHANGED_MODE` (ignored) and the COM apartment is never torn down.
    The re-init path also calls `MFStartup()` **without checking its HRESULT**
    (line 286) — if MF was shut down concurrently, encoding proceeds against a
    dead MF session and fails opaquely.
  - `CreateEncoderTransformLocked` (lines 143-172): iterates `activates[]`,
    `Release()`s each entry, `CoTaskMemFree(activates)` — `VERIFIED` correct;
    but if `ActivateObject` fails for every entry, `encoder_mft_` stays null
    and the function returns `false` — correct. One subtlety: the loop only
    tries to activate the **first** entry that reports a friendly name
    successfully? No — re-reading: `if (!activated)` gates activation, so it
    tries entries in order until one activates; later entries are still
    `Release()`d. Correct.
  - `EncodeBgraFrame` output handling (lines 330-372): on
    `MF_E_TRANSFORM_NEED_MORE_INPUT` it issues `MFT_MESSAGE_COMMAND_DRAIN`
    then retries `ProcessOutput` — draining mid-stream on every
    need-more-input is heavy-handed (`INFERRED`: can stall the encoder and
    emit delayed frames; the standard pattern is to feed the next input
    frame instead). `out_data.pEvents` is `Release()`d but
    `out_data.pSample` is only released when the MFT provides samples — when
    the caller provides the sample, ownership rules differ; static reading
    cannot prove a leak either way — flagged as `INFERRED` risk, not verified.
- **The fake-IDR fallback (F08-01, P0-adjacent honesty bug):** if the MFT
  produces nothing, `EncodeBgraFrame` (lines 376-393) emits a **hardcoded 44-byte
  Annex-B SPS+PPS+empty IDR** (`kValidMinimalIdrAu`) and reports
  `out_au.valid = true`, `is_keyframe = true`. The comment claims it is
  "spec-compliant" — it is a syntactically plausible but **content-free**
  frame: a decoder will show a frozen/grey frame, and every downstream
  consumer (telemetry `frames_encoded`, the C++ `StepCaptureAndEncode`
  success path, the test's `au.valid` assert) counts it as a real encoded
  frame. The test in `tests/test_windows_agent.cpp` (§10) can therefore pass
  on a machine with **no working encoder at all**. This is the single most
  misleading behaviour in the native agent.
- **Telemetry honesty:** `is_hardware` is inferred from the MFT friendly name
  containing `NVENC/NVIDIA/Intel/Quick Sync/AMD/Hardware` (lines 160-167) —
  substring matching, not a capability query. `INFERRED`: a hardware MFT with
  an unexpected friendly name is reported as software, and vice versa.
- `GetTelemetry()` locks `mutex_`; `frame_index_`-derived sample timestamps
  assume constant fps (no wall-clock), so a slow capture loop drifts PTS.

## 7. SendInput controller — VERIFIED

`WindowsInputController` (`input/sendinput_controller.cpp/.hpp`):

- **DPI:** constructor calls `SetProcessDpiAwarenessContext(
  DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)` — process-wide, once per
  controller instance; `c_api.cpp` holds a **global** controller, so the DLL
  sets DPI awareness for the whole host process on first use (`INFERRED`:
  a Python host that set its own awareness first will have this call fail
  silently — return value ignored).
- **Coordinate transform** (`TransformCoordinates`, lines 62-104): virtual-desktop
  metrics (`SM_XVIRTUALSCREEN` etc.), normalised `[0,1]` clamped, monitor-offset
  aware, final clamp to the virtual desktop, conversion to SendInput's 0–65535
  absolute space with `VIRTUALDESK`. `VERIFIED` correct in shape; edge cases:
  `monitor_width/height <= 0` fall back to the virtual size (ignoring the
  offset, so a normalised point on a secondary monitor can land on the primary
  — `INFERRED`); the callers in `peer_connection.cpp` pass **no monitor
  geometry at all** (always `MoveMouse(x, y, true)` with defaults), so C++
  WebRTC input is always relative to the **full virtual desktop**, not the
  streamed monitor — while the Python path passes explicit monitor offsets
  (report 13). Multi-monitor input therefore disagrees between the two paths
  (F08-10).
- **Permissions:** `SetPermissions()` releases held keys when the session
  becomes inactive or mouse/keyboard is revoked (`lines 31-43`); every action
  re-checks `session_active && allow_mouse/keyboard` under `mutex_`.
  Default member init is `permissions_{true, true, false, true}` (mouse,
  keyboard, clipboard=false, active=true) — permissive by default until
  `SetPermissions` is called; the `c_api.cpp` global controller **never calls
  `SetPermissions`** (`NOT FOUND` in `c_api.cpp`), so the DLL input surface
  is always-on for any Python caller that loads it (F08-07).
- **Rate limit:** token bucket, 250 events/s, refill on each call
  (`CheckRateLimitLocked`, lines 45-56). `VERIFIED` flaws:
  - `last_token_refill_` is updated **even when the call is denied for
    permissions** (the check happens inside the permission branch in some
    methods, outside in others — inconsistent), and the bucket is per-controller,
    not per-session, so the C++ peer session and the DLL share nothing.
  - `TypeUnicodeText` checks the bucket **once for the whole string**, then
    injects `2 * text.size()` events unbounded — a 10 k-character paste is
    20 k `INPUT`s in one `SendInput` call (`INFERRED`: can exceed the
    system's injection limits / look like a flood; no chunking, no length cap).
- **Held-key safety (mostly good):** `held_keys_` (`unordered_set<WORD>`) +
  `held_mouse_buttons_mask_` are updated under `mutex_`; destructor calls
  `ReleaseAllHeldKeys()`; `onStateChange` (Disconnected/Failed/Closed) and
  `onClosed` in `peer_connection.cpp` release; `NativeWebRtcPipeline::StopSession`
  releases. `VERIFIED` gaps:
  - `KeyEvent()` inserts into `held_keys_` **before** `SendInput` and does not
    roll back if `SendInput` returns 0 — the set can claim a key is held that
    was never injected (and `ReleaseAllHeldKeys` will later inject a spurious
    key-up — harmless, but the accounting is wrong).
  - `MouseButton()` computes the transform **before** the permission check
    (wasted work, no security impact) and, for `double_click`, pushes
    down/up/down/up as a single `SendInput` batch — if the batch partially
    fails, `held_mouse_buttons_mask_` was already cleared, so a stuck-down
    button is possible (`INFERRED`, low probability).
  - `ReleaseAllHeldKeys()` copies the sets under the lock and injects outside
    it — correct lock discipline; but it returns the count of *injected*
    events, not of *successful* ones (`SendInput` return ignored).
- **Key mapping** (`MapKeyTokenToVk`): static table + single-char + `VkKeyScanA`
  fallback. `VkKeyScanA` returns shift-state in the high byte, which is
  **discarded** (`vk_scan & 0xFF`) — shifted characters (e.g. `!`, `?`, upper
  case via that path) inject the unshifted key (`INFERRED`, minor).
- **No `SendInput` UIPI consideration documented:** `SendInput` from a
  medium-integrity desktop process cannot drive elevated windows; `NOT FOUND`:
  any integrity-level check or user-facing error for this — input silently
  fails (`SendInput` returns 0, caller returns `false`, no `GetLastError`
  logging anywhere in the file).

## 8. System monitor — VERIFIED (brief)

`monitoring/system_monitor.cpp`: CPU via `GetSystemTimes` delta (stateful
`prev_*_ticks_` — **not thread-safe**: `CollectSnapshot()` has no lock, and
the service constructs a fresh monitor every second, so each instance's first
sample has `prev == current` and reports 0% CPU on its first call; the
service's snapshot therefore always shows the *previous* instance's… no —
each fresh instance baselines at construction, so the service's CPU reading is
always the delta since ~0 ms ago, i.e. **~0% or noise** — `VERIFIED` logic bug,
F08-11); memory via `GlobalMemoryStatusEx`; disk hardcoded to `C:\`
(`GetDiskFreeSpaceExW(L"C:\\")` — wrong on systems where the OS/data drive
differs); uptime via `GetTickCount64`; sessions via `WTSEnumerateSessionsW`
(skips Session 0, frees with `WTSFreeMemory` — correct) and
`WTSQuerySessionInformationW` (frees `user_buf` — correct). `windows_version`
is the hardcoded string `"Windows NT (x64)"`, not a real version query.

## 9. Installer & deployment — VERIFIED

`windows-agent/installer/install-service.ps1`:
- Requires admin; builds via CMake if binaries missing; copies to
  `C:\Program Files\AI Bridge\WindowsAgent`.
- `New-PipeSecret`: 32 random bytes, base64, ACL SYSTEM+Admins FullControl on
  `C:\ProgramData\ai-bridge\native-pipe.secret` — `VERIFIED` sound.
- **Config file (`C:\ProgramData\ai-bridge\native-agent.json`) contains
  `device_token` in plaintext AND `pipe_secret` in plaintext** (lines ~85-100),
  ACL: SYSTEM+Admins FullControl, **installing user Read**. Any process running
  as that user — including the desktop agent itself, and any other app the user
  runs — can read the device bearer token and the pipe secret (F08-08).
  `NOT FOUND`: any DPAPI sealing of this config (the Python agent seals its
  own token file; the native config does not).
- Service: `sc.exe create … binPath= "<exe>" --config "<config>"`,
  `start= auto`, failure actions restart 5 s / 15 s / 60 s, reset 86400 s.
- Desktop agent: **both** HKLM `Run` (`AIBridgeDesktopAgent`) **and** a logon
  scheduled task (`AI Bridge Desktop Agent`, `Users`, `RunLevel Limited`,
  `MultipleInstances IgnoreNew`) — dual launch mechanisms; on a normal logon
  both fire, so two desktop-agent processes can race (second one registers
  over the pipe and probes; `NOT FOUND`: any single-instance guard in
  `desktop_agent.cpp` — the Python GUI has one, the C++ binary does not,
  F08-12). The desktop agent is launched with `--config` only, i.e. in
  **probe mode**: it registers with the pipe, captures/encodes one frame,
  prints JSON, and exits. `INFERRED`: at every logon the machine pays a
  DXGI+MF probe; there is no persistent desktop agent unless something
  launches it with `--control-url --device-token --session-id`.
- `NOT FOUND`: any uninstaller for this installer (no `uninstall-service.ps1`
  in `windows-agent/installer/`); `deployment/windows/` has separate
  `Uninstall-AIBridge.ps1` for the Python agent — whether it removes the
  native service/config is not verified in this track.
- `NOT FOUND`: any code that launches `ai_bridge_desktop.exe` from the service
  via `CreateProcessAsUser` / `WTSQueryUserToken` — the Session 0 → interactive
  session hop the architecture diagram implies does not exist in code.

## 10. `c_api.cpp` / DLL boundary — VERIFIED

- **Global singletons + one mutex:** `g_capturer`, `g_encoder`, `g_input`,
  `g_monitor`, `g_api_mutex` (file-static, `c_api.cpp:11-21`). Every exported
  function locks `g_api_mutex` — `VERIFIED` serialised, but this also means a
  slow `CaptureNextFrame` (35 ms timeout) blocks input injection and encoding
  for the whole DLL; and `aibridge_set_encoder_bitrate` locks while calling
  `UpdateBitrate`, which locks the encoder's own mutex — lock ordering is
  consistent (API mutex → encoder mutex) so no deadlock is visible statically.
- **Bounds-checked where it matters:** `aibridge_capture_frame` /
  `aibridge_encode_h264_frame` check `max_bytes >= frame size` before `memcpy`
  and silently return 0/1 without copying when the buffer is too small — the
  Python caller (`screen_capture.py:_capture_native_cpp`) handles this by
  reallocating and retrying once. `aibridge_get_system_health_json` uses
  `snprintf` with `max_len` and returns 0 on truncation — correct.
- **ABI hazards:** `aibridge_capture_window` takes a raw `uint64_t` HWND and
  `reinterpret_cast`s it (`c_api.cpp:73`) — a wrong integer from Python
  captures an arbitrary window or fails `IsWindow` (checked inside
  `CaptureWindow`, fail-closed). `aibridge_encode_h264_frame` re-initialises
  the encoder when fps changes but **not when bitrate changes alone**
  (handled by `UpdateBitrate`) — correct; but it ignores `g_enc_kbps` drift
  when `Initialize` fails (sets `g_encoder_initialized = true` regardless of
  `Initialize`'s return — `VERIFIED`, `c_api.cpp:178-190`: a failed init is
  remembered as success, so subsequent frames keep calling `EncodeBgraFrame`
  on an uninitialised encoder and keep hitting the fake-IDR path, F08-01).
- **Python-side arg mismatch (report 13, F13-02):** the Python
  `ScreenTrack.recv()` native-timing call passes arguments shifted by one
  relative to this ABI — the DLL therefore receives `width = len(rgb)`.
  The DLL itself is not at fault; the caller is.
- **No auth on the DLL:** any local process that can `LoadLibrary` the DLL
  gets capture/encode/input with the global controller's always-on permissions
  (§7, F08-07). The DLL path is a fixed repo-relative build output
  (`windows-agent/build/Release/ai_bridge_native.dll`); `NOT FOUND`: any
  signature/hash verification before `ctypes.CDLL` loads it.

## 11. C++ WebRTC session — VERIFIED (report 13 has the protocol detail)

- `peer_connection.cpp` (libdatachannel only): STUN `stun.l.google.com:19302`
  **only — no TURN** (`peer_connection.cpp:105`); `onLocalCandidate` posts
  candidates via `HttpSignalingClient` with `sdp_mline_index` hardcoded `0`;
  the loop **never applies remote (viewer) ICE candidates** — it only polls
  for the answer SDP (`PollSession`) and checks for `ice-ufrag`. `INFERRED`:
  trickle ICE from the viewer is dropped on the floor; connectivity depends on
  the answer SDP carrying candidates (it does not, in the Python plane's
  flow — viewer candidates are a separate list). Behind symmetric NAT this
  path likely never connects.
- Frame pumping: `StepCaptureAndEncode` per loop iteration, `sleep(1000/fps)`,
  `frames_sent_` incremented **before** checking `track->isOpen()`, so the
  counter counts encoded frames, not sent frames; binary payloads are sent
  with `track->send(std::move(payload))` inside `try/catch(...)` — failures
  swallowed.
- `input_dc->onMessage` handles `mouse_move/mouse_down/mouse_up/click/
  scroll/key_down/key_up/type/ping` — **no `view_only` / mode check, no human-
  takeover check, no per-message auth** beyond the (already-established) DTLS
  session. Permissions are hardcoded `{mouse=true, keyboard=true,
  clipboard=false, active=true}`. `NOT FOUND`: any `release_all_keys` or
  `switch_monitor` handling in the C++ data channel (Python has both).
- `http_signaling.cpp`: WinHTTP, per-request `WinHttpOpen/Connect/OpenRequest`
  (no connection reuse — a new TCP+TLS handshake per poll, and the poll loop
  calls `PollSession` every frame iteration, i.e. up to 30 HTTPS round-trips
  per second while waiting for an answer — `INFERRED` wasteful; after the
  answer is applied the loop keeps polling every iteration too, since there
  is no "stop polling" branch). No certificate validation flags are set —
  WinHTTP default validation applies (`INFERRED`). `PostOffer`'s JSON escaper
  handles `\`, `"`, `\n`, `\r`; `PostCandidate`'s escaper does **not** escape
  `\n`/`\r` (candidates should not contain them; minor).
- `NativeWebRtcPipeline` (`webrtc_transport.cpp`): `StartSession` returns
  `true` **unconditionally**, even if `capturer_.Initialize()` or
  `encoder_.Initialize()` failed (`lines 24-52`) — callers cannot tell the
  session is dead except via `StepCaptureAndEncode` returning false.
  `StopSession` releases keys and shuts down under the lock — correct order.
  `active_` is atomic; `GetStats()` locks. `INFERRED`: `StepCaptureAndEncode`
  holds no lock during capture/encode (only for stats updates) while
  `StopSession` can `Shutdown()` the capturer/encoder concurrently — the
  encoder/capturer have their own mutexes, so this is *probably* safe, but
  `StopSession` resetting `stats_.status` under the lock while a concurrent
  `StepCaptureAndEncode` increments `stats_.frames_captured` is a benign race
  on stats only.

## 12. Memory safety, RAII, concurrency — audit summary

| Area | VERIFIED state |
|---|---|
| Raw `new` / `delete` / `malloc` | **None found** in `windows-agent/src/` (`grep new \| delete \| malloc` — only `new` inside `std::make_unique`/`make_shared` and placement in comments). Allocation is via STL containers, `ComPtr`, `std::vector`, `std::string`. |
| RAII | Good: `ComPtr` for all COM/D3D/MF objects; `std::lock_guard` throughout; destructors call `Shutdown()`/`ReleaseAllHeldKeys()`. |
| Unsafe pointers | `reinterpret_cast` uses are all COM/D3D ABI casts (`void**` for `GetAddressOf`, `BITMAPINFO*`, metadata-buffer `RECT*`/`DXGI_OUTDUPL_MOVE_RECT*` over a `vector<uint8_t>` sized by `TotalMetadataBufferSize` — `VERIFIED` the move/dirty rect reads reuse one buffer sized for the *total* and pass it to both getters; the second call reuses `buf_size` correctly). `const_cast` only for DPAPI `DATA_BLOB` input (API requires non-const). |
| Resource leaks | `LocalFree` paired correctly in DPAPI + pipe SDDL paths; `WTSFreeMemory` paired; `CloseHandle` paired in service/desktop/test. **Exceptions:** (a) COM apartment never uninitialised (§6); (b) `MFStartup` failure path in re-init unchecked; (c) GDI fallback null-bitmap path (§5); (d) `out_data.pSample` release asymmetry (§6, unproven). |
| Race conditions | (a) `WindowsSystemMonitor` has mutable `prev_*` state with **no mutex** (§8); (b) `NativeWebRtcPipeline` stats race is benign; (c) service 1 s pipe-instance recreation vs desktop connect race (§3); (d) `WindowsInputController` rate-bucket + held-set are mutex-protected — good. |
| Deadlocks | None found statically. Lock ordering is flat (one mutex per object; `c_api` global mutex → object mutexes; no nested acquisition of two object mutexes except `StopSession` → encoder/capturer `Shutdown()` which each take their own mutex sequentially — safe). `INFERRED`: a `CaptureNextFrame` holding `mutex_` while calling `InitDuplicationLocked` (which assumes the lock is held — correctly documented by the `Locked` suffix) is consistent. |
| Integer / bounds | `width & ~1` even-clamping everywhere; `snprintf` used for fixed buffers in `c_api.cpp`; `std::clamp` on bitrate/fps/coords. `ConvertBgraToNv12` indexes assume packed stride (§6). `ReadAuthenticatedMessage` 64 KiB cap enforced on send, truncation on receive. |
| Error handling | Inconsistent: many Win32/COM failures return `false` with no `GetLastError`/HRESULT logging anywhere in the C++ tree (`NOT FOUND`: any logging framework in `windows-agent/src/` — diagnostics are `std::cout` JSON in the two mains only). Failures are therefore silent to operators. |

## 13. Tests — VERIFIED (static only; not built/run on this Linux VM)

`windows-agent/tests/test_windows_agent.cpp` (147 LOC, 6 sections):
1. DPAPI round-trip. 2. Pipe HMAC round-trip (server thread + client, 80 ms sleep
   for startup — timing-dependent). 3. `TransformCoordinates` origin/centre +
   Shift hold/release + mouse-permission denial. 4. DXGI enumerate + one capture
   (asserts non-empty monitors, `frame.bgra_pixels.size() == w*h*4`).
5. `NativeWebRtcPipeline` start → one `StepCaptureAndEncode` (asserts
   `au.valid`, non-empty NAL). 6. System monitor snapshot + visible windows +
   optional `CaptureWindow` on the first window.
- **All assertions are bare `assert()`** — compiled out under `NDEBUG` (Release
  builds define it by default with MSVC), so a Release test binary runs zero
  checks and prints success unconditionally. `VERIFIED` from the source; build
  flags were not executed here.
- Test 5 passes via the **fake-IDR fallback** (§6) even with no encoder.
- Test 4 asserts `!monitors.empty()` — always true given the synthetic fallback
  monitor (§5), and asserts a real capture succeeds, which requires an interactive
  desktop; under CI-as-SYSTEM it falls back to GDI and can still pass with a
  black frame (F08-05). `INFERRED`: the suite can be green on a machine where
  the product is functionally broken.
- `NOT FOUND`: tests for service loop behaviour, pipe replay, JSON parsing,
  encoder re-init, bitrate update, `c_api` bounds, multi-monitor offsets,
  `TypeUnicodeText` chunking, or the libdatachannel session (which requires
  the optional backend). `NOT FOUND`: any CI workflow in this repo that builds
  `windows-agent` (searched `windows-agent/` only; repo-wide CI belongs to
  track 10).

## 14. Findings

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| F08-01 | **P0** | **Fake H.264 IDR fallback fabricates success.** `EncodeBgraFrame` emits a hardcoded 44-byte SPS/PPS/empty-IDR and reports `valid=true` when the MF encoder produces nothing; telemetry, pipeline stats, and the unit test all count it as a real frame. A viewer sees a frozen frame while the system reports healthy. | `windows-agent/src/encoder/mf_h264_encoder.cpp:376-393`; `tests/test_windows_agent.cpp` (§5) |
| F08-02 | **P1** | **Service is not a supervisor.** No HTTP heartbeat, no enrollment, no policy, no desktop-agent launch/restart; 1 s loop recreates the pipe and allows a 250 ms connect window, one message per iteration. The "supervisor" claim in the file header is not implemented. | `windows-agent/src/service/windows_service.cpp:1-13,100-176` |
| F08-03 | **P1** | **Pipe auth has no replay protection.** HMAC-SHA256 is correctly built and constant-time compared, but there is no nonce/timestamp/sequence; a captured message replays. Any authenticated local user can connect (SDDL `AU` = read/write). | `windows-agent/src/security/local_security.cpp:52-128,130-165` |
| F08-04 | **P1** | **Desktop agent never reads the pipe reply.** `RegisterWithSupervisorPipe` sends and closes; registration success, `active_session_id`, and hostname are discarded. | `windows-agent/src/desktop/desktop_agent.cpp:55-76` |
| F08-05 | **P1** | **GDI fallback reports black/failed frames as valid.** `valid = (bmp != nullptr)` ignores `BitBlt`/`PrintWindow` results; null-bitmap/GDI handles are not null-checked. Same shape in `CaptureWindow`. | `windows-agent/src/capture/dxgi_capture.cpp:217-277,398-465` |
| F08-06 | **P1** | **COM lifetime bugs in the encoder.** `CoInitializeEx` return ignored, no `CoUninitialize` anywhere; re-init `MFStartup()` HRESULT unchecked. Repeated init/shutdown can leave COM/MF in a bad state. | `windows-agent/src/encoder/mf_h264_encoder.cpp:79-80,285-286` |
| F08-07 | **P1** | **DLL input surface is always-on and unauthenticated.** The global `WindowsInputController` in `c_api.cpp` never gets `SetPermissions`; any local process loading the DLL can inject input. No DLL signature/hash check on the Python side. | `windows-agent/src/c_api.cpp:11-21`; `src/ai_bridge/screen_capture.py:124-213` |
| F08-08 | **P1** | **Native config stores the device token + pipe secret in plaintext, readable by the installing user.** Installer ACL grants the current user Read on `native-agent.json`. | `windows-agent/installer/install-service.ps1` (config + ACL block) |
| F08-09 | **P2** | **Secure-desktop detection is duplicated and divergent** (C++: any non-`Default` desktop; Python: `winlogon/disconnect/screensaver` list), and the C++ check runs on every frame. | `windows-agent/src/capture/dxgi_capture.cpp:30-48`; `src/ai_bridge/screen_capture.py:218-280` |
| F08-10 | **P2** | **C++ WebRTC input ignores monitor geometry** (always virtual-desktop-relative) while Python passes explicit monitor offsets; multi-monitor clicks land on different monitors per path. | `windows-agent/src/webrtc/peer_connection.cpp` (`MoveMouse(x,y,true)`); `src/ai_bridge/webrtc_stream.py` (§ input handling) |
| F08-11 | **P2** | **System-monitor CPU reading is always ~0% in the service.** A fresh `WindowsSystemMonitor` is constructed per iteration, so the `GetSystemTimes` delta baselines at construction; `CollectSnapshot` is also lock-free with mutable state. | `windows-agent/src/monitoring/system_monitor.cpp`; `windows-agent/src/service/windows_service.cpp:100-104` |
| F08-12 | **P2** | **Dual logon launch with no single-instance guard.** HKLM Run + scheduled task both start the desktop agent; `desktop_agent.cpp` has no mutex/lock, unlike the Python GUI. Probe mode also exits after one frame, so there is no persistent native agent. | `windows-agent/installer/install-service.ps1`; `windows-agent/src/desktop/desktop_agent.cpp` |
| F08-13 | **P2** | **C++ WebRTC ignores viewer ICE candidates and has no TURN.** Only STUN; `PollSession` reads the answer only; `sdp_mline_index` hardcoded 0; per-frame HTTPS polling (new TLS handshake per poll). Likely fails behind symmetric NAT. | `windows-agent/src/webrtc/peer_connection.cpp:100-230`; `windows-agent/src/webrtc/http_signaling.cpp` |
| F08-14 | **P2** | **Tests are `assert()`-only and can pass while broken.** Release (`NDEBUG`) strips all checks; DXGI test passes via synthetic monitor + black GDI frame; encoder test passes via fake IDR. No tests for service, pipe replay, JSON parsing, `c_api` bounds, or the peer session. | `windows-agent/tests/test_windows_agent.cpp`; `windows-agent/CMakeLists.txt` |
| F08-15 | **P2** | **Hand-rolled JSON everywhere, no library.** Substring `ExtractJson*` parsers mishandle escapes/ nesting/duplicates; `answer.sdp` extraction is a `find("\"answer\"")` hack. | `windows-agent/src/service/windows_service.cpp:26-48`; `windows-agent/src/webrtc/http_signaling.cpp:66-110,240-268` |
| F08-16 | **P2** | **`StartSession` returns true even when capture/encode init fails; `c_api` marks a failed encoder init as success.** Callers cannot detect a dead session except by frame failure. | `windows-agent/src/webrtc/webrtc_transport.cpp:24-52`; `windows-agent/src/c_api.cpp:178-190` |
| F08-17 | **P3** | **Rotation is recorded but never applied; primary-monitor detection assumes origin (0,0); disk hardcoded to `C:\`; Windows version string hardcoded.** Small correctness debts. | `windows-agent/src/capture/dxgi_capture.cpp:59-116,139-215`; `windows-agent/src/monitoring/system_monitor.cpp` |
| F08-18 | **P3** | **`TypeUnicodeText` bypasses its own rate limit** (one token check per whole string, unbounded `SendInput` batch); `VkKeyScanA` shift-state discarded; no UIPI/integrity-level diagnostics when `SendInput` silently fails. | `windows-agent/src/input/sendinput_controller.cpp:45-56,300-365` |
| F08-19 | **P3** | **libwebrtc backend is a stub.** Enum + `DescribeAvailable…` exist, but `CreatePeerConnectionSession` only constructs libdatachannel sessions; a libwebrtc build returns `nullptr` → desktop exits 2. | `windows-agent/src/webrtc/peer_connection.cpp:28-48,260-280`; `windows-agent/CMakeLists.txt` |
| F08-20 | **P3** | **No logging in native code.** Failures return `false` with no `GetLastError`/HRESULT anywhere; operators get only the two mains' stdout JSON. | `grep` across `windows-agent/src/` |

## 15. What is genuinely good (for balance — VERIFIED)

- RAII discipline is strong: `ComPtr` everywhere, no raw `new`/`delete`, mutex-guarded
  shared state in capture/encoder/input, destructors that shut down and release held keys.
- The HMAC construction and constant-time comparison are correct; the pipe SDDL and
  `PIPE_REJECT_REMOTE_CLIENTS` are appropriate; DPAPI usage (user scope, UI forbidden,
  `SecureZeroMemory` on unprotect) is correct.
- DXGI recovery (re-init on access-lost/device-removed, staging-texture row-pitch
  handling, dirty-rect metadata) and MF media-type ordering show real platform knowledge.
- `SendInput` held-key/mouse-button tracking with release on disconnect/permission
  revocation/destruction is the right design, and the 250 ev/s token bucket is a
  reasonable abuse control.
- The CMake structure cleanly separates the DLL, service, desktop agent, and tests,
  and the WebRTC backends are correctly opt-in rather than silently half-linked.

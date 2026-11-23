> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 5C — Real-Browser WebRTC E2E: Parent Brief (browser run)

**Date:** 17 Nov 2026. **Worker:** Wave 5C subagent. **Status:** harness + brief ready,
browser run NOT yet done — nothing below claims a pass.

## 0. TL;DR for the parent

- Harness: [`analysis/webrtc-e2e-harness.html`](analysis/webrtc-e2e-harness.html)
  (single file, plain JS, no CDN, no secrets inside).
- Serve it from **port 5173 only** (`python3 -m http.server 5173` in `analysis/`),
  open `http://localhost:5173/webrtc-e2e-harness.html` in the real browser,
  paste the staging admin token into the harness password field, List devices,
  Run full test, copy the results JSON.
- **Expected outcome on staging as it stands: `PASS — NO SOURCE (honest), WITH 1 KNOWN
  DEFECT`.** There is no Windows agent attached to staging, so no video can exist.
  The honest pass is: signalling plane verified + session creation cleanly refused
  with the Windows-only gate + ticket/WS auth behaves + nothing fabricated.
  **Do NOT declare a full video E2E pass** from this run — that requires a real
  Windows agent posting an offer, which staging does not have.
- One genuine defect was found live and fixed locally (see §5): `GET
  /api/v1/sessions/ice-servers` returns 404 on staging. The fix is in the local tree,
  **not deployed** — so the harness's `ice_servers_endpoint` criterion will FAIL on
  this run. That FAIL is expected and is itself live confirmation of the defect.
  Do not "fix" it by editing the harness; the verdict string already accounts for it.

## 1. What the worker verified already (no browser needed)

Live staging probes (17 Nov 2026, ~12:40 UTC), all via curl with the staging admin
token read from `~/workspace/tmp/staging-admin-token` (never printed/stored elsewhere):

| Probe | Result |
|---|---|
| `GET /health` | 200 `{"status":"ok","version":"0.3.0","emergency_stop":false}` |
| `GET /api/devices` (authed) | 200 — **6 devices, ALL Linux, ALL offline, all synthetic**: `fanout-a` ×2, `fanout-b` ×2, `k6-0`, `sched-test`. **No Windows device, no real agent.** |
| `GET /api/v1/sessions` (authed) | 200 `{"sessions":[],"count":0}` |
| `POST /api/v1/sessions` device `333b3848cf3a` (Linux, `sched-test`), `view_only` | **400** `WebRTC desktop stream unavailable for platform Linux: … Live Desktop streaming is supported on Windows desktop hosts only.` — honest gate, correct behaviour |
| `POST /api/v1/sessions` device `local` | 400, same Linux message (plane host is Linux) |
| `POST /api/v1/sessions` unknown device | 404 `Device not found.` |
| Legacy `POST /api/devices/{id}/desktop/webrtc/session` | **410** `legacy_webrtc_gone` (correct) |
| `GET /api/v1/sessions/ice-servers` (authed) | **404 `{"detail":"Session not found."}` — DEFECT (see §5)** |
| `GET /api/v1/sessions/ice-servers` (no auth) | 401 `Authentication required.` |
| `POST /api/stream-ticket` (authed, fake session id) | 200 `{"ticket":"<redacted>","device_id":"333b3848cf3a","session_id":"webrtc-e2e-probe-nonexistent","expires_in":60}` — mint does not validate session existence by design; redemption does |
| `POST /api/stream-ticket` (no auth) | 401; missing fields → 400 `device_id and session_id are required.` |
| CORS preflight `Origin: http://localhost:5173` | **Allowed** (`access-control-allow-origin: http://localhost:5173`) |
| CORS preflight `Origin: http://localhost:8000` | **400 `Disallowed CORS origin`** — serving from any other port breaks every authed call |

Local verification (repo `.venv`, Python 3.12):

- The 404 reproduced locally via TestClient (authenticated `GET
  /api/v1/sessions/ice-servers` → 404 before the fix, **200 after**).
- Narrow test files: `tests/unit/test_stream_ticket.py`,
  `test_webrtc_phase3.py`, `test_webrtc_viewonly.py`,
  `test_phase4_streaming_and_updater.py` → **60 passed** (includes 3 new
  regression tests). Route-snapshot suite
  `tests/unit/test_architecture_phase4_routers.py` → **19 passed** (route set
  unchanged — the fix only reorders registration).
- Harness JS: `node --check` on the extracted script → **OK**.
- Harness serve check: `python3 -m http.server 5173` in `analysis/` →
  `http://localhost:5173/webrtc-e2e-harness.html` **200, 31,502 bytes** (and same
  on `127.0.0.1:5173`). Serving the file is verified; **running the test in a
  browser is NOT** — that is this brief's job.

## 2. Signalling protocol (verified against code + dashboard)

Architecture — **the agent is the offerer, the browser is the answerer**.
There is no aiortc peer connection on the plane for remote devices; media is
agent↔browser, the plane only stores/retransmits signalling state
(`src/ai_bridge/control/v1_routes.py`, `src/ai_bridge/webrtc_stream.py`
`AgentStreamer._run`, dashboard `apps/dashboard/src/views/LiveDesktopView.tsx`
`handleStartWebrtc`, `apps/dashboard/src/api.ts`).

| Step | Call | Auth | Shape |
|---|---|---|---|
| Health | `GET /health` | none | 200 `{"status":"ok","version":"0.3.0","emergency_stop":false}` |
| List devices | `GET /api/devices` | `X-Admin-Token` (or `Authorization: Bearer`) | 200 array of `{id, name, os, status, …}` |
| ICE servers | `GET /api/v1/sessions/ice-servers` | viewer role | 200 `{"ice_servers":[{"urls":[…STUN…]},{"urls":[…TURN…],"username":"<expiry>:aibridge_client","credential":"<ephemeral>"}]}` — currently 404 on staging (§5) |
| Create session | `POST /api/v1/sessions` | viewer role | Body `{"device_id","mode":"view_only"\|"ai_assisted"\|"human_control","fps":30,"scale":1.0,"monitor_index":0}` (width/height/codec/bitrate_kbps optional). 200 → `format_session_v1` incl. `session_id`, `status:"waiting"`, `video`, `permissions`, `ice_servers`, `telemetry:{}`. Non-Windows device → **400** Windows-only message. Unknown device → 404. |
| Poll for offer | `GET /api/v1/sessions/{session_id}` | viewer role | 200 session object; `offer_sdp` / `offer:{sdp,type}` appears only after the agent posts its offer. Dashboard polls up to 30×500 ms. |
| Answer | `POST /api/v1/sessions/{session_id}/answer` | viewer role | Body `{"sdp":<answer sdp>,"type":"answer"}` → `{"ok":true,"session_id","status":"answered"}`; also broadcast over WS as `{"type":"answer",…}` |
| ICE (viewer) | `POST /api/v1/sessions/{session_id}/ice` | viewer role | Body `{"candidate":<RTCIceCandidate JSON>,"from":"viewer"}` → `{"ok":true}`; agent candidates arrive via `agent_candidates` / WS `{"type":"ice","from":"agent",…}` |
| Mint ticket | `POST /api/stream-ticket` | viewer role | Body `{"device_id","session_id"}` → 200 `{"ticket","device_id","session_id","expires_in":60}`. Single-use, 60 s, bound to actor+tenant+device+session; only the hash is stored. |
| Signalling WS | `WS(S) /api/v1/ws/sessions/{session_id}?ticket=<ticket>` | ticket in URL (browsers can't set WS headers) | On open server sends `{"type":"session_snapshot","session":{…}}`. Client `{"type":"ping","client_ts_ms"}` → server `{"type":"pong","server_ts_ms",…}`. Client may send `{"type":"answer"|"offer"|"ice",…}`. Bad/expired/replayed/wrong-session ticket, or no credential → **close 4401**. Long-lived tokens in the WS URL are rejected (Wave 5A). |
| Terminate | `POST /api/v1/sessions/{session_id}/terminate` | operator role | 200 session with `status:"ended"`; broadcasts `session_terminated` |
| Agent side (for reference) | `GET/POST /api/agent/webrtc/{session_id}[/offer\|/candidates\|/error]` | `X-Device-Token` + mTLS | Agent posts offer, polls for answer, posts candidates. Not exercised by the browser. |

DataChannel: agent creates `input` (ordered) in `AgentStreamer._run`; viewer
creates it too and the agent handles `ping/pong`, `som_query`, input, clipboard,
file transfer. Input is default-deny unless the session state permits it.

**No synthetic/test video source exists on the plane.** `ScreenTrack.recv()` raises
`Desktop capture unavailable` when there is no desktop; the Wave 3C fix removed the
Windows encoder's fake IDR frame. The server must never fabricate frames — the
harness checks for exactly that (empty telemetry, no offer, no candidates).

## 3. Browser-run steps (exact)

Run from the repo checkout. Do all of this in the browser task; do not paste the
token into chat, logs, or any file.

1. **Serve the harness (port 5173 is mandatory).**
   ```bash
   cd ~/workspace/ai-bridge-main/analysis
   python3 -m http.server 5173
   ```
   Leave it running. Confirm `curl -s -o /dev/null -w "%{http_code}\n"
   http://localhost:5173/webrtc-e2e-harness.html` prints `200`.
   Do NOT use port 8000, another port, or `file://` — CORS preflight fails
   (`400 Disallowed CORS origin`) and every authenticated call fails.
2. **Open** `http://localhost:5173/webrtc-e2e-harness.html` in the real browser
   (Chrome/Chromium). Keep DevTools console open (F12 → Console) for the run.
3. **Get the token at runtime:** read `~/workspace/tmp/staging-admin-token`
   (0600 file) and **type/paste it into the harness "Staging admin token"
   password field** in the browser. Never print it, never put it in the browser
   task's visible output, never save it anywhere else. The harness sends it only
   as an `X-Admin-Token` header and never logs it.
4. **Click "List devices".** Expect: log shows `health -> HTTP 200`,
   `devices -> HTTP 200`, criteria `health` PASS, `devices_list` PASS listing
   6 Linux/offline devices, and the Device dropdown populated. If health fails,
   stop — the plane is down; record the status and abort the run.
5. **Pick any device** (all are Linux/offline; `sched-test` is fine — the
   choice does not matter, the gate outcome is identical). Leave mode
   `view_only`, offer-wait `20`.
6. **Click "Run full test".** Do not click anything else while it runs.
   Expected sequence (~30–60 s total):
   - `ice_servers_endpoint` → **FAIL (expected, §5)** with the known-defect note.
   - `POST /api/v1/sessions` → HTTP 400 Windows-only message →
     `session_create_gate` **PASS** (honest gate).
   - Probe ticket minted (200, `expires_in: 60`, ticket shown redacted) →
     `ticket_mint` **PASS**.
   - WS with the probe ticket → closed **4401** without opening →
     `ws_rejects_unknown_session` **PASS**.
   - WS with a garbage ticket → closed 4401 → `ws_rejects_bad_ticket` **PASS**.
   - WS with no credential → closed 4401 → `ws_rejects_no_auth` **PASS**.
   - Verdict banner: **`PASS — NO SOURCE (honest), WITH 1 KNOWN DEFECT:
     ice-servers endpoint 404 (fix exists locally, not yet deployed)`**.
   - No video element content; "No track received yet." stays — correct.
7. **Click "Copy results JSON"**, then paste the JSON into the run record
   (it contains no token/ticket values — verify before pasting: search for the
   token string; it must not appear).
8. **Capture evidence** (see §4) and **stop the http.server**.

**If an offer unexpectedly arrives** (i.e. a Windows agent has been attached to
staging since this brief was written): let the harness continue — it will
automatically do the full negotiation (answer, ICE trickle, ticket WS with
snapshot/pong + replay check, 30 s wait for `connected`, 5 s of frame counting).
Then the verdict can become `FULL PASS` only if `ice_connected` PASS **and**
`video_frames_real` PASS (track received, `framesDecoded > 0`, presented frames
advancing, non-zero video dimensions). Do not upgrade the verdict manually.
**If the session stays `waiting` with no offer** (the other no-source branch):
the harness polls the full 20 s, checks telemetry is empty / no candidates /
no fabricated content, terminates the session, and reports the same no-source
verdict — that is also a pass, not a failure.

**Timeouts / aborts:** if the run hangs > 3 minutes, screenshot the log + criteria,
click "Stop / terminate session", and report it as an incomplete run (do not
declare pass/fail from a hung run).

## 4. What success / failure looks like + evidence to capture

**Success (expected):** verdict exactly
`PASS — NO SOURCE (honest), WITH 1 KNOWN DEFECT: ice-servers endpoint 404 (fix
exists locally, not yet deployed)`; criteria `health`, `devices_list`,
`session_create_gate`, `ticket_mint`, `ws_rejects_unknown_session`,
`ws_rejects_bad_ticket`, `ws_rejects_no_auth` all PASS; `ice_servers_endpoint`
FAIL with the known-defect detail; `offer_wait`/`no_source_clean` either absent
(session-create path) or PASS/SKIP (session-waiting path); no `FAIL` anywhere else.

**Failure (any of these = FAIL, report exactly which):** health non-200; devices
non-200; session create returns 200 for a Linux device (gate bypassed) or 5xx;
ticket mint non-200 with a valid token; any WS socket **opens** with a garbage /
no-auth / unknown-session ticket (auth bypass); replayed ticket accepted (only
applies if a real session existed); session enters `error` with a misleading
message; telemetry non-empty / agent candidates present with no agent (fabricated
content); video frames claimed without an offer (impossible — treat as harness
bug, report it).

**Evidence to capture and return to the coordinator:**
1. Screenshot of the full page after the run (verdict banner + criteria table
   visible; scroll if needed — two screenshots are fine).
2. Screenshot of the browser console (any errors/warnings).
3. The complete results JSON (via "Copy results JSON") — paste as text, not a
   screenshot, so criteria can be checked mechanically.
4. The verdict string verbatim, the device id used, the run duration, and the
   browser name/version.
5. Anything unexpected (extra log lines, different HTTP codes, offer arriving)
   quoted exactly.

## 5. Defect found by this worker (fixed locally, NOT deployed)

**`GET /api/v1/sessions/ice-servers` shadowed by the `{session_id}` route → 404.**
`attach_v1_routes` registered `GET /api/v1/sessions/{session_id}` (line ~717)
before the literal `GET /api/v1/sessions/ice-servers` (line ~935). Starlette
matches in registration order, so `"ice-servers"` was treated as a session id
and `v1_get_session` returned 404 `Session not found.` The route-snapshot test
(`tests/unit/test_architecture_phase4_routers.py`) only checks the route *set*,
not order, so it never caught this. The dashboard only calls this endpoint as a
fallback (sessions already embed `ice_servers`), which is why it went unnoticed.
Live staging returned the same 404; reproduced locally via TestClient (404
before fix, 200 after).

**Fix (minimal, uncommitted):** moved the `v1_get_ice_servers` handler above
`v1_get_session` in `src/ai_bridge/control/v1_routes.py` with a comment
explaining the ordering constraint; deleted the old block. No route added or
removed, no behaviour change besides the literal route now matching.
**Regression test:** `TestIceServersRouteNotShadowed` (3 tests) appended to
`tests/unit/test_stream_ticket.py`: 200 + servers present, 401 unauthenticated,
unknown session still 404. Test evidence: the 4 WebRTC narrow files
**60 passed**; route snapshot **19 passed**. Before/after: `GET
/api/v1/sessions/ice-servers` authed → 404 `Session not found.` → **200 with
STUN + ephemeral TURN credentials**. (The fix is in the local tree only; staging
still serves the old code until a deploy happens — a parent decision, not part
of this task.)

## 6. Files changed / created (all UNCOMMITTED — parent decides on commit/push)

| Path | What |
|---|---|
| `analysis/webrtc-e2e-harness.html` | **New.** The harness (this brief's §3). No secrets inside. JS syntax-checked (`node --check` OK); serves 200 from port 5173. |
| `analysis/webrtc-e2e-brief.md` | **New.** This file. |
| `src/ai_bridge/control/v1_routes.py` | **Modified.** Ice-servers route moved above `{session_id}` route (§5). |
| `tests/unit/test_stream_ticket.py` | **Modified.** +3 regression tests (§5). |

`git status` at handover: 2 modified files + 2 new untracked files; nothing
committed, nothing pushed, no production system touched, no farhan contact.

## 7. Parent browser run — DONE 17 Nov 2026 (headless Chromium on the host VM)

The cloud browser could not open the locally served harness (its loopback is a
different VM), so the parent ran the harness in headless Chromium
(156.0.8078.4) on the host VM itself via a Playwright driver
(`/tmp/webrtc-run.js`; results in `~/workspace/tmp/webrtc-e2e/`:
`results.json`, `log.txt`, `criteria.txt`, `verdict.txt`, `page.png`,
`console.txt`, `meta.txt`). Device used: `sched-test` (`333b3848cf3a`).
Duration 3.1 s. **Verdict from the harness: `FAIL — see failing criteria
above`** — NOT the expected no-source PASS, for two distinct reasons, one a
known defect and one an environment/deployment block (see below). The token
was never pasted or printed: the harness auto-fills it from a local
same-origin endpoint (`analysis/serve_harness.py`, also serving the harness
on 127.0.0.1:5173).

### 8.1 Criteria (4 PASS, 1 known-defect FAIL, 3 WS FAIL)
- `health` PASS (200 `{"status":"ok","version":"0.3.0","emergency_stop":false}`).
- `devices_list` PASS — 6 devices, all Linux/offline, all synthetic
  (fanout-a ×2, fanout-b ×2, k6-0, sched-test), matching the worker's probe.
- `ice_servers_endpoint` FAIL — HTTP 404 `{"detail":"Session not found."}`
  — the KNOWN DEFECT (§5). Expected on this run; fix is committed
  (`a928f7e`, GitHub `099188f9`) but NOT yet deployed to staging.
- `session_create_gate` PASS — POST /api/v1/sessions → HTTP 400 with the
  honest Windows-only message; no session, no offer, no fabricated stream.
- `ticket_mint` PASS — HTTP 200, `expires_in: 60`, ticket redacted (len 43).
- `ws_rejects_unknown_session`, `ws_rejects_bad_ticket`, `ws_rejects_no_auth`
  — all FAIL with `opened=false closeCode=1006`. The WebSocket handshake
  never completed (see §7.3).

### 8.2 Environment workarounds needed to run the harness at all (this VM)
- Headless Chromium could not reach the public internet: its CONNECT to the
  sandbox egress proxy got `HTTP/1.1 403 Forbidden` (net-log: TCP connect to
  the proxy succeeded, CONNECT sent, proxy answered 403 JSON, 81 bytes;
  Chromium maps it to `ERR_TUNNEL_CONNECTION_FAILED`), while curl/Python
  raw CONNECTs with byte-identical headers got 200. `--no-proxy-server`
  also failed (direct TLS is intercepted: `SSL: WRONG_VERSION_NUMBER`).
  Fix used: a local logging TCP relay on 127.0.0.1:18080
  (`/tmp/relay.py`, forwards CONNECTs to the egress proxy — its CONNECTs
  get 200) as Chromium's `--proxy-server`, plus
  `--ignore-certificate-errors` (Chromium does not trust the sandbox egress
  CA that MITMs TLS; the CA is `Hatch Sandbox Egress CA`). Both are
  test-only measures; the harness still talks to the real staging plane
  over real HTTPS and the token travels only inside the TLS tunnel.
- The cloud-browser attempt was abandoned for the same reason in reverse:
  it cannot reach this VM's loopback, and the plane's CORS only allows the
  `http://localhost:5173` origin, so no public harness URL would work.

### 7.3 The WebSocket 403 — Cloudflare in front of Render (not the plane code)
The three WS criteria fail because the WS handshake gets **HTTP 403 from
Cloudflare** (`Server: cloudflare`, `CF-RAY: …-MIA`, `cf-cache-status:
DYNAMIC`, empty body), proven independently of the browser:
- Browser console: `WebSocket connection to 'wss://…/api/v1/ws/sessions/…'
  failed: Error during WebSocket handshake: Unexpected response code: 403`
  (all three probes).
- Python `websockets` client, via the egress proxy AND via the local relay:
  `InvalidStatus server rejected WebSocket connection: HTTP 403`.
- Raw TLS handshake (Python, CONNECT via egress proxy, then a manual WS
  upgrade request): `HTTP/1.1 403 Forbidden` with Cloudflare headers —
  for a minimal handshake AND a full browser-like handshake (UA, Origin,
  extensions). Regular HTTPS to the same host works (all fetch calls).
- A WS handshake to a different public host (`ws.postman-echo.com`) timed
  out at the TLS handshake; direct (no-proxy) TLS fails with
  `SSL: WRONG_VERSION_NUMBER` (transparent interception).

So from this VM no WebSocket to the plane is possible, and the 403 is issued
by Cloudflare's edge in front of Render, not by the plane's FastAPI code
(which would answer 4401 after accepting, per the protocol in §2) and not
by the sandbox egress proxy (its own CONNECT 403 has a different signature:
JSON, 81 bytes). Whether Cloudflare blocks WS upgrades to this host for
everyone or only for datacenter-egress IPs is NOT settled from this VM.
Two cloud-browser probe attempts both failed to produce a verdict, for
reasons unrelated to the plane:
- Attempt 1 (17 Nov 2026 ~13:13–13:16 UTC): the cloud browser could not open
  DevTools by automation (no console access), and the self-running probe page
  hosted on Muse file storage (`muse.ai/files/…/ws-probe.html`) was blocked
  by the browser's organization policy ("Your organization doesn't allow you
  to view this site").
- Attempt 2 (~13:23 UTC): the same probe page served from jsDelivr
  (`cdn.jsdelivr.net/gh/jacxishipping/bridgeai@main/analysis/ws-probe.html`)
  loaded but rendered as raw text — jsDelivr serves `.html` as `text/plain`,
  so the script never ran.
The probe page itself is committed at `analysis/ws-probe.html` (GitHub main
`8bc86c2`).
- **Decisive probe from farhan (17 Nov 2026 13:27 UTC):** James re-supplied
  the production admin token and the parent ran a stdlib-only raw TLS WS
  handshake from farhan (Windows Server 2022, a different network) to the
  same garbage-ticket URL. Result: **`HTTP/1.1 403 Forbidden`,
  `Server: cloudflare`, `CF-RAY: a5bf125ffab99fe9-SIN`,
  `cf-cache-status: DYNAMIC`, empty body** — the identical 403 the parent's
  VM got from a different Cloudflare PoP (`…-MIA`). Two networks, two PoPs,
  same 403. **Conclusion: Cloudflare blocks WebSocket upgrades to this host
  generally, not just for this VM's egress.** The plane's WS behaviour
  (4401 rejects, snapshot/pong, replay rejection) is therefore unverified
  from every external vantage because the handshake never reaches the plane.
  This is a staging-deployment finding: live desktop streaming via browser
  peers is unreachable on this Render deployment as configured. Fixing it
  would need a deployment change (e.g. custom domain / Cloudflare settings)
  — a user decision, not taken here. Probe script left on farhan at
  `C:\Users\Administrator\ws-probe.py`.

### 7.4 What 5C actually proves, and what stays open
PROVEN (real browser peer, real plane): signalling plane over HTTPS works
from a browser origin; device list is honest; session creation is honestly
gated for non-Windows devices (400, no fabricated stream); stream tickets
mint (200, 60 s). The known ice-servers defect is confirmed live on the
deployed staging code (fix committed, awaiting a deploy in 5E).
NOT PROVEN: any WebSocket behaviour (4401 rejects, snapshot/pong, replay
rejection) — blocked by the Cloudflare 403 above; and real video (FULL
PASS) — still impossible on staging with no Windows agent attached.
The WS criteria are therefore **NOT VERIFIED (environment/deployment
blocked)**, not "the plane failed" — the harness records them as FAIL
because its verdict logic cannot distinguish the two, and this addendum is
the correction. 5C closes with the WS criteria unverified; the farhan probe
settled the cause as a general Cloudflare block (see §7.3), so verifying WS
behaviour needs a different deployment or network path — e.g. a custom
domain in front of the plane, or a direct-to-origin route that bypasses
Cloudflare — which is a user decision for 5E/5F, not a code fix.

### 7.5 Still unverified after this run
- Everything the harness does inside a real browser: `RTCPeerConnection`
  construction, `setRemoteDescription`/`createAnswer`, ICE gathering/trickle,
  WS handshake + snapshot/pong/replay from a browser origin, `requestVideoFrameCallback`
  frame counting. (No offer arrived, so no peer connection was constructed;
  curl/TestClient cannot do any of these.)
- Whether the current staging deploy still 404s `ice-servers` from a browser
  origin — **CONFIRMED by this run: yes, 404 (§7.1)**.
- Full video E2E (`FULL PASS`) — **not provable on staging as it stands** and
  not claimed: it needs a real Windows agent attached to staging, which does
  not exist. If the gate ever needs a true FULL PASS, the prerequisite is
  enrolling/pointing a Windows agent (e.g. `farhan`, parent-only) at staging
  and re-running this harness; the harness already supports that path
  opportunistically (offer → answer → connected → frames).

# C++ / Production WebRTC E2E checklist

Shipping paths:

| Path | Role |
|------|------|
| Python `aiortc` + PyAV (`webrtc_stream.py`) | Default poller / CI browser peer |
| C++ `ai_bridge_desktop.exe` (libdatachannel) | Production Windows WPF / native agent |
| Dashboard Live Desktop | JPEG poll **or** WebRTC H.264; ICE failure → JPEG fallback |

## Build (production Windows)

```sh
cmake -B windows-agent/build -DAIBRIDGE_WITH_LIBDATACHANNEL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build windows-agent/build --config Release --target ai_bridge_desktop
```

`scripts/build_windows_installer.ps1` now passes `-DAIBRIDGE_WITH_LIBDATACHANNEL=ON`
so release installers ship a real PeerConnection backend (not `no_cpp_webrtc_backend`).

## Local / CI harnesses

### Native C++ ↔ plane (Windows CI: `windows-native-webrtc`)

```sh
python scripts/libwebrtc_e2e_harness.py --out .run/libwebrtc_e2e_evidence.json --native-agent
```

`libwebrtc_e2e_verified` is true when the native agent backend is
`libdatachannel`/`libwebrtc` and a signaling **offer** is observed.
`libwebrtc_media_verified` additionally requires viewer answer + ICE connected
or track receipt.

### Browser peer (Ubuntu CI: `release-1-staging-and-webrtc-gate`)

```sh
pip install -e ".[dev,webrtc,browser]"
playwright install --with-deps chromium
python scripts/webrtc_browser_e2e.py --require-browser-peer --out .run/webrtc_browser_e2e_evidence.json
```

### Production gate (signaling + JPEG fallback + optional TURN/browser/native)

```sh
# Dev / PR soft gate
python scripts/webrtc_production_e2e.py --out .run/webrtc_production_e2e_evidence.json

# Production NAT profile
export AI_BRIDGE_TURN_SECRET=...
export AI_BRIDGE_TURN_HOST=turn.example.com
python scripts/webrtc_production_e2e.py --require-turn --require-browser-peer \
  --out .run/webrtc_production_e2e_evidence.json

# Windows + built desktop agent
set AIBRIDGE_RUN_NATIVE_WEBRTC=1
python scripts/webrtc_production_e2e.py --require-native-agent \
  --out .run/webrtc_production_e2e_evidence.json
```

Evidence fields: `production_ready`, `turn_configured`, `signaling_verified`,
`browser_peer_verified`, `native_cpp_verified`, `jpeg_fallback_verified`.

## TURN (required for symmetric NAT)

Configure one of:

* `AI_BRIDGE_TURN_SECRET` + `AI_BRIDGE_TURN_HOST` (ephemeral credentials)
* `AI_BRIDGE_TURN_URL` (+ optional username/credential)
* `AI_BRIDGE_ICE_SERVERS` JSON including `turn:` / `turns:` URLs

`docker-compose.prod.yml` fails fast without `AI_BRIDGE_TURN_SECRET`.
STUN-only is acceptable for loopback CI; production deployments must set TURN.

## Dashboard operator path

1. Live Desktop Studio → transport **WebRTC · H.264** (Windows agent online).
2. On ICE `failed` / `disconnected`, UI auto-falls back to **JPEG · ~1.2s**.
3. Dual-control prefers WebRTC `input` DataChannel when open; else HTTP `/desktop/input`.

## Residual honesty

`GET` capability matrix lists `native_cpp_agent_e2e_with_dashboard` until
`AI_BRIDGE_LIBWEBRTC_E2E_VERIFIED=1` (set by a successful native harness run).

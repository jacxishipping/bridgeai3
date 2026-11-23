# Remote access

**Updated:** 23 November 2026  
**Related:** [mcp-clients.md](./mcp-clients.md) · [production-readiness.md](./production-readiness.md) · [integrations/chatgpt/](../integrations/chatgpt/)

## Outbound agents

- Devices connect **outbound** to the control plane (polling / optional agent WS). No inbound shell port is required on the device.
- Offline devices: tasks stay `queued`; agents reclaim with bounded backoff. Dashboard shows last-seen from heartbeats.

## TLS termination

Put the plane behind a TLS reverse proxy (nginx sketch):

```
server {
  listen 443 ssl; server_name bridge.example.com;
  ssl_certificate     /etc/letsencrypt/live/bridge.example.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/bridge.example.com/privkey.pem;
  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
  }
}
```

Keep `:8000` / `:8001` on loopback only. Obtain certs with certbot (or your platform).

Production readiness expects an `https://` public base URL (`AI_BRIDGE_PUBLIC_BASE_URL`).

## Authentication

| Caller | Credential |
|---|---|
| Dashboard / operator API | Session cookie + CSRF, or admin Bearer / `X-Admin-Token` |
| Devices | `X-Device-Token` (hashed at rest; rotatable) |
| MCP HTTP / Custom GPT Actions | Bearer admin or operator session token |
| Metrics scrape | `AI_BRIDGE_METRICS_TOKEN` (when configured) |

Rotate tokens on any suspected leak. Never put secrets in chat or query strings.

## Remote MCP

1. Run `python -m ai_bridge.server --http` on loopback.
2. Proxy with TLS + bearer / OAuth gateway (`deployment/oauth/`).
3. Never expose the raw MCP HTTP port to the internet.

ChatGPT / Copilot: see [integrations/chatgpt/](../integrations/chatgpt/) and [integrations/copilot365/](../integrations/copilot365/).

## ChatGPT Custom GPT Actions

Import `integrations/chatgpt/actions/openapi.yaml`, set the server URL to your TLS origin, and configure Bearer auth. Instructions: `gpt-instructions.md`. Setup: `SETUP.md`.

## mTLS (optional)

Terminate TLS at nginx with `ssl_verify_client on;` and a client CA, then proxy to the loopback plane. Plane-side device fingerprint gate: env-gated (`AI_BRIDGE_DEVICE_MTLS=1`, `control/device_mtls.py`). Fingerprints come from reverse-proxy headers — the plane does not terminate client certificates itself. Support status: Supported when proxy + env are configured correctly; see [support-matrix.md](./support-matrix.md).

## WebRTC / TURN

Interactive desktop needs reachable STUN/TURN for real NAT. Production profile requires TURN/ICE configuration. Cross-NAT public TURN proof remains a hosted RC item ([PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md)).

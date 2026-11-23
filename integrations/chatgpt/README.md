# ChatGPT × AI Bridge

Two supported paths — pick what your ChatGPT plan allows.

| Path | When to use | Auth |
|------|-------------|------|
| **Custom GPT Actions** (this folder) | ChatGPT Plus/Team/Enterprise Custom GPTs | Bearer admin or operator session token |
| **Remote MCP (Streamable HTTP)** | Developer mode / connectors that speak MCP | Same TLS plane + bearer (see `docs/remote-access.md`) |

ChatGPT **web does not** run local stdio MCP. Do not paste secrets into chat.
For coding CLI work, prefer `integrations/codex/`.

---

## Custom GPT Actions (recommended for ChatGPT UI)

Pack under `integrations/chatgpt/actions/`:

| File | Purpose |
|------|---------|
| `openapi.yaml` | OpenAPI 3.1 Actions schema (devices, desktop frame/input, tasks, sessions, policy, audit) |
| `gpt-instructions.md` | Paste into the GPT **Instructions** field |
| `conversation-starters.txt` | Optional prompts for the GPT builder |
| `plugin.json` | Manifest metadata (name, auth, description) |
| `SETUP.md` | Step-by-step builder checklist |

### Quick setup

1. Open [ChatGPT](https://chatgpt.com) → **Explore GPTs** → **Create** → **Configure**.
2. Name: **AI Bridge Operator**. Paste `actions/gpt-instructions.md` into Instructions.
3. **Actions** → **Create new action** → import schema from `actions/openapi.yaml` (or paste contents).
4. **Authentication** → **API Key**:
   - Auth Type: **Bearer**
   - API Key: your plane admin token **or** an operator session token
5. Edit the schema `servers[0].url` to your TLS origin (default `https://clideos.us`).
6. **Test** `healthReady` and `listDevices` in the Actions tester, then publish (private or workspace).

### What the GPT can do

- List fleet devices and live status  
- Capture desktop frames / inspect UI elements  
- Send mouse/keyboard input (honors human takeover)  
- Queue policy-gated tools via `createTask` and poll results  
- Start/terminate live WebRTC sessions (Windows)  
- Read policies and audit when something is blocked  

Tier 3/4 tools may return `needs_approval` — approve on the dashboard/plane, then poll `getTask`.

### Safety

- Bearer token lives in GPT Action auth settings, never in chat.  
- Plane policy, e-stop, and takeover still apply.  
- Prefer observe → plan → act; confirm destructive shell/file actions once.

---

## Remote MCP (alternative)

If your workspace supports remote MCP connectors:

```
https://<your-tls-origin>/mcp
```

Use the OAuth or bearer setup documented in `docs/remote-access.md` and `deployment/oauth/README.md`. Same policy surface as every other MCP client.

---

## Smoke check (from your machine)

```bash
# Health (no auth)
curl -sS https://clideos.us/health/ready

# Devices (Bearer)
curl -sS -H "Authorization: Bearer $AI_BRIDGE_ADMIN_TOKEN" https://clideos.us/api/v1/devices
```

Replace the origin with your deployment.

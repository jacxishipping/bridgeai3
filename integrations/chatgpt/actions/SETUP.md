# AI Bridge Operator — Custom GPT setup checklist

## Prerequisites

- [ ] Control plane reachable over **HTTPS** (e.g. `https://clideos.us`)
- [ ] `GET /health/ready` returns ready / non-e-stop
- [ ] Admin token or operator **session** token ready (never paste into chat)
- [ ] At least one enrolled online device for desktop/task demos
- [ ] ChatGPT plan that allows **Custom GPTs + Actions**

## Builder steps

1. [ ] Create GPT → name **AI Bridge Operator**
2. [ ] Paste contents of `gpt-instructions.md` into **Instructions**
3. [ ] Add conversation starters from `conversation-starters.txt`
4. [ ] Actions → import `openapi.yaml`
5. [ ] Set `servers[0].url` to your plane origin
6. [ ] Authentication → API Key → **Bearer** → paste token
7. [ ] Test Actions in order:
   - [ ] `healthReady`
   - [ ] `listDevices`
   - [ ] `getDesktopFrame` (Windows host)
   - [ ] `createTask` with `tool: get_system_info`
   - [ ] `getTask` until status settles
8. [ ] Publish (Only me / Anyone with link / Workspace)

## Failure triage

| Symptom | Likely cause |
|---------|----------------|
| 401 / 403 on Actions | Wrong/expired token; role below operator for mutating calls |
| Empty device list | Wrong tenant / no enrollments |
| Frame / input 403 with takeover message | Human takeover active on Windows client — Return Control |
| `needs_approval` | Approve on dashboard, then poll `getTask` |
| Emergency stop 403 | Clear e-stop on plane before queuing tasks |
| Schema import errors | OpenAPI 3.1 only; remove unsupported vendor extensions |

## Optional: Remote MCP instead

If Actions are unavailable, point a ChatGPT remote MCP connector at `https://<origin>/mcp` (see parent `README.md`).

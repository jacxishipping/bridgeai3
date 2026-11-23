# MCP clients

**Updated:** 23 November 2026

| Client | Transport | Pack |
|---|---|---|
| Claude / Claude Code | Local stdio MCP | [integrations/claude/](../integrations/claude/) |
| Codex CLI | Local stdio (`config.toml`) | [integrations/codex/](../integrations/codex/) |
| Gemini CLI | Local stdio (`settings.json`) | [integrations/gemini/](../integrations/gemini/) |
| Generic MCP apps | Local stdio | [integrations/generic-mcp/](../integrations/generic-mcp/) |
| **ChatGPT Custom GPT Actions** | HTTPS OpenAPI Actions + Bearer | [integrations/chatgpt/actions/](../integrations/chatgpt/actions/) |
| ChatGPT remote MCP | Streamable HTTP (+ OAuth where supported) | [integrations/chatgpt/](../integrations/chatgpt/) · [remote-access.md](./remote-access.md) |
| Microsoft 365 Copilot / Copilot Studio | Remote Streamable HTTP + OAuth gateway | [integrations/copilot365/](../integrations/copilot365/) |

## Important

- ChatGPT **web does not** run local stdio MCP. Prefer Custom GPT Actions or remote MCP behind TLS.
- Never paste admin/session tokens into chat. Configure Bearer / OAuth in the client’s auth settings.
- Non-loopback HTTP MCP requires bearer auth (admin/operator token via reverse proxy). Default bind for `python -m ai_bridge.server --http` is loopback.

## Tools

Tools are policy-tiered and audited. Stable families include: system/resource info, workspace list/read/search/create/edit/move/delete, services/logs/processes, Docker/git/tests/linter, backup, task submit/status/result/cancel, approvals, desktop status/screenshot/launch (opt-in), device list/status. Exact catalogue is versioned in code (`TOOL_TIERS` / MCP registry).

## Related

- [remote-access.md](./remote-access.md) — TLS, OAuth gateway, public MCP  
- [security.md](./security.md) · [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md)

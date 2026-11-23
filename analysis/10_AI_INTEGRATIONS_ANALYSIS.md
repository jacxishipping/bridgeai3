> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 10 — AI Integrations Analysis

**Scope:** `integrations/` (all 7 subdirectories), the client SDK they depend
on (`src/ai_bridge/client.py`, 335 lines), the backend AI surface they call
(`src/ai_bridge/control/ai_api.py`, 2,285 lines; `src/ai_bridge/server.py`,
1,702 lines), and the OAuth helper (`scripts/setup_mcp_chatbots.py`).
**Method:** read-only source inspection; no integration was executed against
a live client, tenant, or control plane in this track. Status labels:
**VERIFIED** (seen in a cited file), **INFERRED** (reasonable reading,
labelled), **NOT FOUND** (looked for, absent). The MCP *protocol* audit
itself (schemas, authz per tool) belongs to the MCP track; this report covers
how each *client* connects, authenticates, discovers tools, submits work,
and handles errors.

## 1. Executive summary

There are **two genuinely different integration architectures** in this repo,
and the documentation is unusually honest about which is which:

1. **MCP (native)** — Claude Desktop/Code, Codex CLI, Gemini CLI and any
   stdio-capable client spawn `python -m ai_bridge.server` locally and speak
   MCP directly. This is the real, local-first path: no API key, tools served
   by `src/ai_bridge/server.py`, policy enforced where the tools run.
2. **Remote MCP + REST (hosted)** — ChatGPT, Microsoft 365 Copilot and any
   cloud client cannot reach `127.0.0.1`, so they go through Streamable HTTP
   (`/mcp` on port 8001) behind the OAuth 2.1 gateway
   (`deployment/oauth/gateway.py`, 992 lines; PKCE S256, dynamic client
   registration, refresh rotation). The READMEs explicitly refuse to fake
   this: "Any guide claiming ChatGPT web can directly run local stdio MCP is
   incorrect; this project does not fake that integration."
   (`integrations/chatgpt/README.md:11–13`).
3. **Custom-AI / SDK agents** — a third path: Python scripts use
   `AIBridgeClient` (REST: `/api/ai/tools`, `/api/ai/execute`,
   `/api/ai/devices`, `/api/ai/agent`, `/v1/chat/completions`) and wrap
   OpenAI / Anthropic / LangChain / Ollama around it. Five example agents
   (480 lines total) plus a 167-line README.

Verified strengths: the ChatGPT/Copilot READMEs are correct about the
platform limits; the OAuth gateway is a real implementation (not a stub);
the SDK sends both `Authorization: Bearer` and `X-Admin-Token`; the
OpenAI/Anthropic agent loops cap turns at 8 and surface tool errors back to
the model. Verified weaknesses: **every example and config hardcodes the
production host `https://jxiptvplayer.app`** as the default URL (12+ sites);
**Copilot Studio is explicitly "not yet tested against a live tenant"**;
the Ollama agent is single-turn (tool results are printed, never returned
to the model); the LangChain adapter drops tool schemas
(`StructuredTool.from_function` with no `args_schema`, while `create_model`
is imported and unused); no example handles approval-gated results
(`needs_approval` / Tier-4) — a tool call that requires human approval is
reported to the model as a generic error string; and the "49 tools" claim
is repeated in two places but was **not** independently counted in this
track (the catalog is data-driven; see §6).

## 2. Integration inventory

| Integration | Directory | What exists (VERIFIED) | Transport | Auth |
|---|---|---|---|---|
| ChatGPT | `integrations/chatgpt/` | `README.md` only (14 lines). No code, no config. | Remote Streamable HTTP `/mcp` behind TLS + OAuth gateway | OAuth 2.1 via gateway (bearer <redacted> explicitly rejected: "ChatGPT's remote MCP connectors accept OAuth or no authentication, not a static Bearer <redacted> |
| Claude / Claude Code | `integrations/claude/` | `README.md` (5 lines) + `claude-config.json` (9 lines) | Local stdio | None (local process; "No API key is needed: Claude calls the shared tools") |
| Codex CLI | `integrations/codex/` | `README.md` only (12 lines, inline TOML snippet) | Local stdio | None (local process) |
| Gemini / Gemini CLI | `integrations/gemini/` | `README.md` only (10 lines, inline JSON snippet) | Local stdio (CLI); hosted web app explicitly unsupported | None (local process) |
| Generic MCP | `integrations/generic-mcp/` | `README.md` (21 lines) + `mcp-config.json` (12 lines: local stdio + remote URL entries) | stdio **or** remote Streamable HTTP | Remote: OAuth flow in the client; "Keep the token out of the MCP config" |
| Microsoft 365 Copilot / Copilot Studio | `integrations/copilot365/` | `README.md` only (64 lines) | Remote Streamable HTTP, public HTTPS required | OAuth 2.0 Dynamic discovery, PKCE S256, optional device binding |
| Custom AI (OpenAI / Anthropic / LangChain / Ollama / quickstart) | `integrations/custom-ai/` | `README.md` (167 lines) + 5 Python files (480 lines) | REST (`/api/ai/*`, `/v1/chat/completions`) via `AIBridgeClient` | Admin token (`AI_BRIDGE_TOKEN` / `AI_BRIDGE_ADMIN_TOKEN` env, or `token=` arg) sent as `Bearer` + `X-Admin-Token` |

File sizes (VERIFIED by `wc`): `openai_agent.py` 124, `anthropic_agent.py`
116, `quickstart.py` 93, `langchain_agent.py` 77, `ollama_local_agent.py`
70 lines; configs 9 and 12 lines. There is **no test file** for any
integration under `integrations/` (NOT FOUND — no `test*`/`*test.py` there;
integration coverage, if any, lives in the repo-level `tests/` and was not
in this track's scope).

## 3. Path 1 — Native MCP (Claude, Codex, Gemini CLI, generic stdio)

### 3.1 How it connects

All four use the same one-liner (VERIFIED, byte-identical shape in each
README/config):

```json
{ "mcpServers": { "ai-bridge": {
  "command": "/absolute/path/to/ai-bridge/.venv/bin/python",
  "args": ["-m", "ai_bridge.server"],
  "env": { "AI_BRIDGE_WORKSPACE": "/home/YOUR_USER/ai-bridge-workspace" }
} } }
```

- Claude: `integrations/claude/claude-config.json` (verbatim above).
- Codex: `integrations/codex/README.md:4–8` — TOML
  `[mcp_servers.ai-bridge]`, same command/args/env.
- Gemini CLI: `integrations/gemini/README.md:3–8` — `~/.gemini/settings.json`,
  same command/args/env.
- Generic: `integrations/generic-mcp/mcp-config.json` — same local entry
  plus a remote entry (see §4).

The client spawns the server as a subprocess and speaks MCP over stdio.
`src/ai_bridge/server.py` is the server (1,702 lines); its `--http` mode is
covered in §4. Tool discovery is standard MCP: the client sends
`initialize`, negotiates the protocol version, then `tools/list`. No
AI-Bridge-specific discovery code exists on the client side — discovery is
entirely the server's `tools/list` response. That is correct MCP behaviour,
and it means **tool availability is whatever the server exposes for that
session** (device-bound sessions expose a reduced set — see §4.3).

### 3.2 Authentication — none, by design

No API key, no token: the local MCP server runs as the user's own process
with the user's own workspace (`AI_BRIDGE_WORKSPACE`). Trust is the OS
user boundary. The Claude README says so explicitly. **Implication
(INFERRED):** anyone who can invoke the client's MCP config can run tools
with the local user's privileges, subject only to the in-process policy
engine (`policy.py`, tiers) — there is no second identity. That matches
Claude Desktop/Code's own model, but it is worth stating: the local stdio
path has **no authentication boundary at all**, and its security rests
entirely on (a) the local policy engine and (b) the fact that the server
only runs when the user launches the client.

### 3.3 Error handling and recovery

**NOT FOUND on the client side:** there is no client-side retry, timeout,
or reconnection code in any of these integrations — they are config files
and READMEs, not code. Recovery is whatever Claude/Codex/Gemini CLI does
when a stdio server exits (typically: report the tool call as failed,
offer restart). Server-side error behaviour (structured errors, session
expiry) is the MCP track's subject, not this report's. The one operational
instruction that exists is "Restart Claude, then ask it to list the
workspace" (`integrations/claude/README.md:4`).

### 3.4 Limitations (VERIFIED from the READMEs themselves)

- **Hosted web apps do not work on this path.** Gemini README: "The hosted
  Gemini web app does not run local stdio servers; use a supported
  remote-MCP / connector path or Gemini CLI instead."
  (`integrations/gemini/README.md:8–9`). Codex README says the same for
  ChatGPT web. These are correct statements of platform fact.
- **Placeholder paths.** Every config uses `/absolute/path/to/ai-bridge`
  and `/home/YOUR_USER/ai-bridge-workspace` — the user must edit them.
  No installer writes these configs (the Windows installer does not touch
  Claude/Codex/Gemini config — NOT FOUND any such code in
  `deployment/windows/`).
- **No per-client scoping.** The local server exposes the same toolset to
  every stdio client; there is no client identity to bind a policy to.

## 4. Path 2 — Remote MCP + OAuth gateway (ChatGPT, Copilot, generic remote)

### 4.1 Topology (VERIFIED)

```
ChatGPT / Copilot Studio / generic remote client
  │  HTTPS, OAuth 2.1 (PKCE S256, dynamic registration)
  ▼
nginx (deployment/docker/nginx.conf) — :443, TLS 1.2/1.3
  │  location = /mcp → auth_request /internal/verify
  │  location /.well-known/oauth-* and /oauth/ → oauth-gateway:8002
  ▼
oauth-gateway (deployment/oauth/gateway.py, 992 lines, FastAPI)
  │  /oauth/register · /oauth/authorize · /oauth/token · /oauth/revoke
  │  /internal/verify (nginx internal only)
  ▼
mcp-http (python -m ai_bridge.server --http --port 8001, loopback only)
  │  AI_BRIDGE_GATEWAY_VERIFY_URL=http://oauth-gateway:8002/internal/verify
  │  AI_BRIDGE_CONTROL_URL=http://control-api:8000
  ▼
control plane :8000 (tasks, policy, approvals)
```

Key server facts (VERIFIED in `src/ai_bridge/server.py`): `--http` serves
`mcp.streamable_http_app()` wrapped in `_BearerAuthMiddleware`
(`server.py:1683–1695`); the verify URL defaults to
`http://127.0.0.1:8002/internal/verify` and the control URL to
`http://127.0.0.1:8000` (`server.py:91–92`); `stateless_http=True`
(`server.py:86`). The MCP server itself never listens publicly — the
gateway README and the nginx config both enforce loopback-only exposure.

### 4.2 The OAuth gateway (VERIFIED, `deployment/oauth/gateway.py`)

- **Discovery:** `/.well-known/oauth-protected-resource` (+ `/mcp`
  variant), `/.well-known/oauth-authorization-server`,
  `/.well-known/openid-configuration` (routes at `gateway.py:127–153`).
- **Registration:** `POST /oauth/register` — dynamic client registration
  for public clients with PKCE (`gateway.py:160`). `scripts/setup_mcp_chatbots.py`
  is a helper that registers one client per chatbot (Claude, ChatGPT,
  Gemini, Grok, …) with `redirect_uris`, generates PKCE
  `code_verifier`/`code_challenge` (S256), and prints the authorisation URL.
  It takes the admin token as `--token` (i.e. the operator's secret on a
  command line — see finding I-06).
- **Authorisation:** `GET/POST /oauth/authorize` — an approval page; the
  **admin token is the only approval credential, checked once, never
  stored** (`deployment/oauth/README.md:11–13`; `admin_ok()` at
  `gateway.py:100`). Redirect URIs must be registered and `https`
  (`valid_redirect_uri`, `gateway.py:105`); `code_challenge_method` must be
  `S256` (`gateway.py:769`).
- **Tokens:** `POST /oauth/token` — authorisation-code + PKCE (S256) and
  refresh grants, with **refresh-token rotation and reuse rejection**
  (`gateway.py:896–954`); `POST /oauth/revoke` (`:955`). Only SHA-256
  hashes of codes/tokens are stored (SQLite, next to the service).
- **Verification:** `GET /internal/verify` (`gateway.py:968`) — accepts
  OAuth access tokens **and** the raw admin token; nginx marks the location
  `internal` so it cannot be called directly from the internet
  (`deployment/docker/nginx.conf`, `/internal/verify` is **NOT** present as
  a public location in the prod nginx file — it exists only in the README's
  sketch; see finding I-07 and `DEPLOYMENT_AUDIT.md`).
- **Device binding:** at approval time the operator may paste a **device
  token**; the gateway validates it against the control plane's read-only
  `GET /api/agent/whoami` and stores `device_id` on the client record
  (`gateway.py:723`, `deployment/oauth/README.md:60–77`). Bound sessions
  route tool calls to that device as control-plane tasks; droplet-only
  tools (`run_shell`, `read_file`, `write_file`) are **refused** in bound
  sessions; Tier-3 device tools still need dashboard approval; long tasks
  return `status: running` + `task_id` for later `get_task_result`.
  Re-approval without a token never silently unbinds. This is a thoughtful,
  genuinely implemented scoping mechanism — the strongest client-isolation
  feature in the integrations.

### 4.3 ChatGPT specifics (VERIFIED)

`integrations/chatgpt/README.md` (14 lines) lists three paths in preference
order: (1) remote MCP where the plan supports it, pointing at the Streamable
HTTP endpoint behind TLS with OAuth; (2) ChatGPT desktop/API apps that
support MCP, using the generic stdio config; (3) Codex CLI for coding work.
It warns "do not paste secrets into chat". There is no code and no test.
The honest-limits framing is a strength; the weakness is that path (1)
depends on the user's ChatGPT plan/workspace supporting remote MCP
connectors, which the repo cannot verify — **NOT VERIFIED** against any
live ChatGPT offering in this track.

### 4.4 Copilot 365 specifics (VERIFIED — and its own caveat)

`integrations/copilot365/README.md` (64 lines) is the most detailed
integration doc: expose the bridge (MCP :8001 loopback + gateway :8002 +
TLS proxy), verify with `python deployment/copilot_smoke_check.py
https://your-domain`, then Copilot Studio → Generative orchestration →
Tools → Add → MCP → server `https://your-domain/mcp` → OAuth 2.0 Dynamic
discovery → approval page (admin token once, optional device binding) →
publish to M365 Copilot. It states the safety model is unchanged (tiers,
path confinement, per-device binding, Tier-4 approval; Tier 3/4 disabled
unless `AI_BRIDGE_ALLOW_TIER3`). **Its final section states, verbatim:
"Written from Microsoft's documented MCP onboarding flow. Not yet tested
against a live Copilot Studio tenant; the Copilot Studio UI labels may
differ slightly."** — i.e. this integration is **documented, not
verified**. `deployment/copilot_smoke_check.py` checks only the server
side (HTTPS, discovery docs, PKCE S256 + registration, rejection of an
insecure redirect URI, `/mcp` refusing unauthenticated requests) and
states it "cannot verify the Copilot Studio side (tenant, agent
publishing)". `deployment/smoke_check.py` is the parallel generic
read-only check (`/health/live`, `/health/ready`, `/api/auth/me` → 401,
`/api/auth/oidc/configured`, `/`).

### 4.5 Generic remote (`integrations/generic-mcp/`)

`mcp-config.json` contains both entries: `ai-bridge-local` (stdio, same as
§3) and `ai-bridge-remote` with `"url": "https://jxiptvplayer.app/mcp"`.
The README explains the OAuth connect flow ("Complete the browser approval
page using your Bridge admin token; the gateway uses it for approval and
does not store it. Keep the token out of the MCP config and chat.") and the
self-hosting command (`python -m ai_bridge.server --http --port 8001`,
loopback by default, "put TLS + auth in front for any non-local exposure").
**Finding I-01:** the remote URL hardcodes the operator's production host;
anyone copying the config verbatim points their client at
`jxiptvplayer.app`. The same hardcode appears in `src/ai_bridge/client.py`
docstrings and all five custom-ai agents (see §5.1).

## 5. Path 3 — Custom AI / SDK agents (`integrations/custom-ai/`)

### 5.1 The SDK: `AIBridgeClient` (`src/ai_bridge/client.py`, 335 lines) — VERIFIED

- **Transport:** stdlib `urllib.request` only (no `httpx`/`requests`
  dependency). `__init__` resolves base URL from arg →
  `AI_BRIDGE_URL` → `AI_BRIDGE_CONTROL_URL` → `http://127.0.0.1:8000`;
  token from arg → `AI_BRIDGE_ADMIN_TOKEN` → `AI_BRIDGE_TOKEN` → `""`.
  Default timeout 60 s.
- **Auth:** every request sends **both** `Authorization: Bearer <token>`
  **and** `X-Admin-Token: <token>` when a token is set
  (`client.py:100–104`). Sending the same secret under two header names is
  redundant but harmless; it maximises compatibility with whichever
  backend check runs first.
- **Requests:** `_request()` (`:83–129`) JSON-encodes payloads, parses
  JSON responses (falling back to raw text), and raises `AIBridgeError`
  with HTTP status + server `detail`/`error` on `HTTPError`. **No retry,
  no back-off, no connection pooling** (each call is a fresh
  `urlopen`) — **INFERRED** consequence: transient network failures fail
  the agent turn outright.
- **Surface:** `list_devices()` (`GET /api/ai/devices`, falls back to
  `GET /api/devices`), `get_device()` (client-side name/ID match),
  `get_tools(format, device, category)` (`GET /api/ai/tools`),
  `execute(tool, device, args, timeout)` (`POST /api/ai/execute`,
  timeout+10 s HTTP timeout), `run_shell`, `read_file`/`write_file`,
  `take_screenshot`, `mouse_click/move/scroll/drag`, `keyboard_type/press`,
  `list_windows`/`focus_window`, `get_screen_size`, `run_agent()`
  (`POST /api/ai/agent`, 120 s timeout), `chat()` (`POST
  /v1/chat/completions`, 120 s timeout).
- **Unauthenticated default:** if no token is set, requests go out with no
  auth headers at all (`quickstart.py:23–27` even prints "Using default
  unauthenticated connection for preview"). The backend will reject
  protected endpoints; the SDK does not warn beyond that print.

### 5.2 The backend AI surface (VERIFIED, `src/ai_bridge/control/ai_api.py`)

2,285 lines; the module docstring (`:5–7`) describes the four endpoints
the SDK uses. Routes found by grep:

| Route | Purpose |
|---|---|
| `GET /api/ai/tools` (`:1285`) | Tool catalog in `raw` / `openai` / `anthropic` / `gemini` format, optionally filtered by `device`/`category` |
| `POST /api/ai/execute` (`:1307`) | Synchronous tool execution; 400 `Unknown tool: …` for bad names (`:1328`) |
| `POST /api/ai/agent` (`:1508`) | Server-side ReAct agent (BYOK: OpenAI, Claude, Gemini, Groq, Ollama) |
| `POST /v1/chat/completions` + `POST /api/v1/chat/completions` (`:1883–1884`) | OpenAI-compatible proxy for LangChain/AutoGen/CrewAI/Cursor |
| `GET /api/ai/devices` (`:1955`) | Fleet capabilities for AI callers |
| `POST /api/ai/chat` (`:2121`), `POST /api/ai/chat/stream` (`:2197`, SSE) | Chat with live tool progress (used by the dashboard's AI hub) |

The README's tool-format claims (OpenAI / Anthropic / Gemini / raw JSON
Schema) match the `format` parameter. The README's tier summary (§"Security
& Policy Architecture") claims Tier 3 is "Allowed unattended when configured
or when targeting an unrestricted Linux server" — whether that matches the
live policy engine is the security track's question, not verified here.

### 5.3 OpenAI agent (`openai_agent.py`, 124 lines) — VERIFIED

- Requires `OPENAI_API_KEY`, `AI_BRIDGE_URL` (default
  `https://jxiptvplayer.app`), `AI_BRIDGE_TOKEN` (default `""`).
- Flow: `list_devices()` → `get_tools(format="openai")` → system prompt
  embedding the fleet summary → loop `max_turns=8`: `chat.completions.create
  (model="gpt-4o", tools=tools, tool_choice="auto")` → for each tool call,
  `json.loads` args (fallback `{}` on bad JSON), pop `device`, `bridge.execute()`,
  append a `role: "tool"` message with the result (truncated to 120 chars
  in the print, full JSON in the message). Tool failure is caught and
  returned to the model as `{"error": str(exc)}` (`:103–109`). No final
  answer → prints "Reached maximum conversation turns."
- Gaps: **no handling of approval-gated outcomes** — if the backend
  returns `needs_approval` (or queues a Tier-4 task), the agent treats the
  payload as a normal result and will summarise it as done (finding I-04);
  no token/cost accounting; no timeout on the OpenAI call itself beyond the
  SDK default; `model="gpt-4o"` hardcoded; prints full-ish tool output to
  stdout (120-char truncation only in the log line).

### 5.4 Anthropic agent (`anthropic_agent.py`, 116 lines) — VERIFIED

Same shape as §5.3 with `claude-3-5-sonnet-20241022`, `max_tokens=4096`,
`max_turns=8`. Differences: it prints Claude's text "thoughts" per turn;
it stops when `tool_uses` is empty **or** `stop_reason == "end_turn"`;
tool results are appended as a `role: "user"` message with
`tool_result` blocks (`:96–113`). Same gaps as §5.3 (no approval
handling, no budget, hardcoded model). One correctness note: the stop
condition checks `not tool_uses or stop_reason == "end_turn"` — if Claude
returns text **and** a `tool_use` in the same turn with
`stop_reason == "tool_use"`, the loop continues correctly; the logic is
sound.

### 5.5 LangChain agent (`langchain_agent.py`, 77 lines) — VERIFIED

- `create_ai_bridge_langchain_tools()` fetches `get_tools(format="raw")`
  and builds one `StructuredTool.from_function` per tool, with a closure
  `make_func(tool_name)` that pops `device` and calls `client.execute()`.
  The closure is correctly parameterised (no late-binding bug — `make_func`
  takes `tool_name` as an argument).
- **Real defect (finding I-03):** the tools are built with **no
  `args_schema`** — `StructuredTool.from_function(func=…, name=…,
  description=…)` only. `create_model` is imported from pydantic
  (`:28`) but **never used** (grep: 1 occurrence, the import). The raw
  tool spec's `parameters.properties` are read into a local (`:34`) and
  then discarded. Consequence: LangChain agents using these tools have no
  argument schema, so the LLM must guess argument names/shapes from the
  description text; validation is absent. The file is otherwise only a
  demo: `main()` prints the first 5 tools and suggests passing them to
  `create_react_agent` or CrewAI — it never runs an agent.

### 5.6 Ollama agent (`ollama_local_agent.py`, 70 lines) — VERIFIED

- Local-only: `ollama.chat(model="llama3.2", messages=[user],
  tools=tools_openai)` against `http://localhost:11434` (default Ollama),
  tools in OpenAI format via `get_tools(format="openai")`.
- **Real defect (finding I-02):** it is **single-turn only**. There is one
  `ollama.chat` call (`ollama.chat` occurs once in the file). If the model
  returns tool calls, each is executed and the result **printed**
  (`str(res)[:150]`), but the results are **never sent back to the model**
  — there is no second `ollama.chat` call, no loop, no final answer
  synthesis. For a prompt needing a tool, the user gets raw printed
  results and no interpreted answer; for a prompt needing two dependent
  tool calls, the second never happens. The OpenAI/Anthropic agents both
  loop (8 turns); Ollama does not.

### 5.7 Quickstart (`quickstart.py`, 93 lines) — VERIFIED

Five sequential demos (list devices → fetch OpenAI tools → run shell on
`'ubuntu'` → screenshot `'windows-pc'` → …), each wrapped in
`try/except Exception` that prints "Could not …: {exc}" and continues.
Hardcoded device names `'ubuntu'` / `'windows-pc'` (the same names used in
every README example). It is a smoke script, not an agent, and is honest
about that. Same unauthenticated-preview caveat as §5.1.

## 6. Tool discovery and the "49 tools" claim

- Discovery for MCP clients is `tools/list` (server-side; see MCP track).
  Discovery for SDK agents is `GET /api/ai/tools?format=…&device=…&
  category=…`, which reformats the server catalog per provider.
- The README (`integrations/custom-ai/README.md:63`) and the backend
  (`src/ai_bridge/control/ai_api.py:1645`) both say **49 tools**. The
  catalog in `ai_api.py` is data-driven (`TOOL_CATALOG`, plus
  `DESKTOP_TOOLS` at `:31` and `_ADDITIONAL_TOOL_SPECS` at `:566`, with
  device-conditional entries such as `capture_desktop_screenshot` at
  `:553`), so a static count is unreliable — a regex count of dict keys
  over-counts (it also matches nested schemas). **The count of 49 was NOT
  independently verified in this track**; it requires a runtime call to
  `/api/ai/tools?format=raw` (or a careful AST parse of the catalog
  construction). Treat "49" as an **unverified repeated claim**, not a
  finding either way. The dashboard's AI hub (`AiApiHubView`) fetches the
  same endpoint live (`api.aiTools("raw", device)`), so the live number is
  whatever the server returns for that device.
- **Device filtering exists but is client-hinted:** `get_tools` accepts a
  `device` parameter and the MCP bound-session path restricts the toolset
  server-side (§4.2). In the SDK path, `device` is popped from the tool
  args by the *agent script* and passed as a separate parameter — if a
  model puts the device inside `args` under a different key, or omits it,
  the call goes to the default device. That is a prompt/contract fragility,
  not a server bug.

## 7. Error handling, retries, and recovery — cross-cutting

| Layer | Behaviour (VERIFIED) |
|---|---|
| SDK `_request` | Raises `AIBridgeError(status, detail)` on HTTP errors; wraps transport failures as "Request failed: …". No retry, no back-off, no timeout distinction (a timeout and a 500 look similar to the caller except via the message). |
| OpenAI/Anthropic loops | Per-tool-call `try/except` → `{"error": str(exc)}` fed back to the model; bad tool-arg JSON → `{}`; max 8 turns then stop. No retry of a failed tool call; no distinction between transient (retryable) and permanent errors. |
| Ollama | No error handling around `client.execute` at all in the tool loop (an exception propagates and kills the script); no loop-back (I-02). |
| LangChain | `run_tool` has no `try/except`; exceptions propagate to the LangChain executor. |
| MCP stdio clients | No client-side code; recovery is the host client's behaviour (NOT FOUND in repo). |
| Remote MCP (ChatGPT/Copilot) | Recovery is the gateway + nginx: refresh-token rotation with reuse rejection; no client-side code in repo. |
| `setup_mcp_chatbots.py` | `register_client` prints the HTTP error body and re-raises; PKCE generation is correct (32 random bytes, SHA-256, base64url, no padding). |

## 8. Documentation consistency (integrations only)

- `docs/mcp-clients.md` and `docs/remote-access.md` **do exist** (VERIFIED
  in `docs/` listing) and are the targets of the README cross-references —
  the links are not dangling. Their contents were not audited in this track.
- `docs/MCP_CHATBOT_SETUP.md` also exists and links the same guides.
- The custom-ai README's security section, cURL examples and
  `/v1/chat/completions` proxy section all match real backend routes
  (§5.2) — **VERIFIED** route-for-route.
- The custom-ai README's example URLs all use `https://jxiptvplayer.app`
  (8 occurrences in the README alone). That is the operator's real host,
  but as documentation it teaches every reader to point at someone else's
  production server. Same for `mcp-config.json`'s remote entry.
- The Copilot README's "not yet tested" caveat (§4.4) is the only
  integration that states its verification status outright; by symmetry,
  **no other integration README states any test status** — ChatGPT remote
  MCP, Gemini CLI, Codex CLI and the custom-ai agents have no recorded
  verification either (NOT FOUND any test report for them in this track).

## 9. Findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| I-01 | High | **Production host hardcoded as the default everywhere.** `https://jxiptvplayer.app` is the default `AI_BRIDGE_URL` in all 5 custom-ai scripts, the base URL in every custom-ai README example (8×), the remote entry in `integrations/generic-mcp/mcp-config.json:9`, and the docstring example in `src/ai_bridge/client.py:9,62`. A user who copies any example verbatim sends their admin token and fleet commands to the operator's production server. | Listed files; `grep -rn jxiptvplayer integrations/ src/ai_bridge/client.py` (12+ hits) |
| I-02 | High | **Ollama agent is single-turn: tool results are never returned to the model.** One `ollama.chat` call; results are printed (`str(res)[:150]`) and the script ends. Multi-step tasks cannot complete; the user gets raw output, not an answer. | `integrations/custom-ai/ollama_local_agent.py` (single `ollama.chat`, no loop, no second call) |
| I-03 | Medium | **LangChain tools have no argument schema.** `StructuredTool.from_function` is called without `args_schema`; `create_model` is imported but unused; the raw spec's `properties` are read and discarded. LLM must guess arguments; no validation. | `integrations/custom-ai/langchain_agent.py:25–58` |
| I-04 | Medium | **No example agent handles approval-gated outcomes.** `needs_approval` / queued Tier-3/4 results are treated as ordinary success payloads and summarised as done. An agent can tell the user a destructive action "succeeded" when it is actually awaiting human approval (or was refused). | `integrations/custom-ai/openai_agent.py:95–113`; `anthropic_agent.py:96–113` (no status check on `bridge.execute` result) |
| I-05 | Medium | **Copilot Studio integration is documented but explicitly untested** ("Not yet tested against a live Copilot Studio tenant"). The smoke check verifies only the server side and says so. Do not present Copilot as a working integration until a tenant test exists. | `integrations/copilot365/README.md` (Status section); `deployment/copilot_smoke_check.py` docstring |
| I-06 | Medium | **Admin token on a command line.** `scripts/setup_mcp_chatbots.py --token YOUR_ADMIN_TOKEN` puts the admin secret in shell history / process listings; the gateway's own approval page is designed to avoid storing the token, but the helper reintroduces exposure at registration time. | `scripts/setup_mcp_chatbots.py:112` (`--token`, `required=True`); contrast `deployment/oauth/README.md:11–13` |
| I-07 | Medium | **Nginx `/internal/verify` exists only in the README sketch, not in the shipped prod nginx config.** `deployment/docker/nginx.conf` has no `internal` location for `/internal/verify` (grep: absent), so a deploy that follows only the compose file — rather than the README sketch — leaves the gateway verification path unwired or exposed depending on how `/mcp` is routed. The compose `mcp-http` service does set `AI_BRIDGE_GATEWAY_VERIFY_URL`, so the MCP server can still call the gateway directly; the auth_request wiring is what is unverified. | `deployment/docker/nginx.conf` (no `/internal/verify`); `deployment/oauth/README.md` (sketch); `docker-compose.prod.yml` (gateway verify URL env) |
| I-08 | Low | **No retry/back-off anywhere in the SDK or agents.** One transient failure fails the turn; the 8-turn loops do not retry failed tool calls. | `src/ai_bridge/client.py:83–129`; agent loops (§5.3–5.6) |
| I-09 | Low | **Hardcoded models and device names in examples** (`gpt-4o`, `claude-3-5-sonnet-20241022`, `llama3.2`, devices `'ubuntu'`/`'windows-pc'`). Harmless as examples, but they rot (model IDs already lag the dashboard's lists) and teach copy-paste. | `integrations/custom-ai/*.py`; `integrations/custom-ai/README.md` |
| I-10 | Low | **"49 tools" is repeated but unverified.** Two sources assert it; the catalog is data-driven and device-conditional, so the number depends on the device and the build. Verify by calling `/api/ai/tools?format=raw` before repeating it. | `integrations/custom-ai/README.md:63`; `src/ai_bridge/control/ai_api.py:1645` |
| I-11 | Low | **No tests for any integration.** No test files under `integrations/`; no recorded execution of any agent script against a live plane in this track. | Absence in `integrations/` tree |
| I-12 | Info | **ChatGPT/Copilot READMEs are commendably honest** about platform limits (no fake local-stdio-for-web claims; Copilot's untested status stated). This is the right pattern; extend it to the other integrations (state verification status per integration). | `integrations/chatgpt/README.md`; `integrations/copilot365/README.md` |

## 10. Recommendations (integrations only)

1. **P1 — Replace the hardcoded production host** (I-01) with
   `http://127.0.0.1:8000` as the default in all scripts, configs and docs,
   and require an explicit `AI_BRIDGE_URL` for anything remote. This is a
   one-line change per file and removes a live credential-misdirection risk.
2. **P1 — Fix the Ollama agent loop** (I-02): feed tool results back with a
   second `ollama.chat` call inside a capped turn loop (mirror
   `openai_agent.py`'s 8-turn structure).
3. **P2 — Add `args_schema` to the LangChain adapter** (I-03) using the
   already-imported `create_model` and the `parameters.properties` the code
   already reads; add a smoke test that every generated tool validates a
   sample argument set.
4. **P2 — Handle approval-gated results in both agent loops** (I-04):
   check the `status`/`needs_approval` fields of `bridge.execute`
   results; when approval is pending, tell the user explicitly and stop
   (do not summarise as success); surface the approval ID and dashboard
   link.
5. **P2 — Verify Copilot against a real tenant** (I-05) or keep it labelled
   "documented, not verified" in every user-facing list; same for ChatGPT
   remote MCP.
6. **P3 — Registration helper should read the admin token from stdin or
   env, not `--token`** (I-06); add a `--token-file` / env-only option.
7. **P3 — Reconcile the nginx `/internal/verify` wiring** (I-07): either
   ship the `internal` location in `deployment/docker/nginx.conf` or
   document that the compose path relies on direct gateway verification
   and remove the sketch's implication.
8. **P3 — Add minimal retry with back-off in `AIBridgeClient._request`**
   for transient failures (I-08), and a per-integration verification
   status line in each README (I-12 pattern).

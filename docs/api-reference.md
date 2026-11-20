# API reference (control plane, v0.3)

> **Status:** this file documents the v0.3 control-plane HTTP surface.
> The route tables below are **generated** from the live app by
> `scripts/generate_api_reference.py` — do not edit them by hand. If a
> route is added, removed, or re-pathed, regenerate them and the test
> suite (`tests/unit/test_api_reference.py`) will fail until the file is
> regenerated. The prose in this file (auth model, conventions, mounted
> surfaces) is hand-written; the tables are the authoritative route list.
> Interactive docs are also served live at `/docs` (Swagger UI),
> `/openapi.json`, and `/redoc`.

Base URL: `http://127.0.0.1:8000` for local development (the Docker image
listens on `$PORT`, default 8000). All request and response bodies are
JSON unless noted (JPEG frame, SSE stream, file download).

## Authentication

Four credential types, by header. A route's **Auth** column in the tables
is derived from the headers its handler declares; `—` means the route
declares no credential header and its auth is enforced inside the handler
(e.g. a signed query parameter) — see the notes below.

- **Admin — `X-Admin-Token`.** The legacy shared admin token. It is OFF
  by default and is accepted only when the operator explicitly enables it
  (`AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN=1` or passed to `create_app`).
  Session-based admin users are the supported admin path.
- **Session — `X-Session-Token`.** `POST /api/auth/login` with a username
  and password returns a `session_token` (8 h TTL). Send it as
  `X-Session-Token`. Mutating (non-GET) session-authenticated requests
  also require `X-CSRF-Token`, minted per session from
  `GET /api/auth/csrf-token` (24 h TTL, HMAC-SHA256 over a single shared
  server secret — see `AI_BRIDGE_CSRF_SECRET`; a missing or invalid CSRF
  token is a 403). Requests carrying an admin or device token skip CSRF.
- **Device — `X-Device-Token`.** The per-device token returned once at
  enrollment (`POST /api/devices/enroll`). Agents use it for heartbeat,
  task polling, results, and the agent WebSocket. When device mTLS is
  enabled (`AI_BRIDGE_DEVICE_MTLS=1`), the agent must also present a
  client certificate whose fingerprint matches the enrolled fingerprint.
- **Bearer — `Authorization: Bearer <token>`.** An OAuth access token
  (MCP/OAuth flow). Accepted on the v1 routes and on the desktop JPEG /
  terminal stream endpoints, alongside the admin and session headers.

**OIDC login:** `GET /api/auth/oidc/login` redirects to the configured
identity provider; the provider returns to
`GET /api/auth/oidc/callback`, which redirects to the dashboard with a
single-use `?oidc_code=` (60 s TTL, stored hashed in the shared
database, so it is redeemable on any worker). The dashboard redeems it
via `POST /api/auth/oidc/exchange` and receives a session token. No
token is ever placed in a URL fragment.

**Streaming:** `POST /api/stream-ticket` mints a single-use stream ticket
(60 s TTL). The browser opens
`WS /api/v1/ws/sessions/{session_id}?ticket=<ticket>`; a bad, expired,
or replayed ticket closes the socket with 4401. The agent side is the
offerer: it posts its offer to
`POST /api/v1/sessions/{session_id}/offer` (device token) and the viewer
answers via `POST .../answer`.

**Quick approval links:** `GET /api/approvals/quick/{approval_id}` takes
no header — it is authorized by an HMAC-signed query parameter
(`?sig=...&exp=...`), minted by the approval flow. Do not share the link
beyond the approver.

## Conventions

- **Errors** are JSON `{detail: ...}` and never include tokens or
  credentials. Common codes: 400 malformed input / unknown tool, 401
  missing or invalid credential, 403 denied by policy / CSRF / emergency
  stop / takeover, 404 unknown device/task/session, 409 conflict
  (duplicate result, takeover active, idempotency conflict), 429 rate
  limited.
- **Lists** cap at 200–500 rows; use `?tenant=` to scope by tenant and
  the per-endpoint filters (`device_id`, `tool`, `status`) where offered.
- **Rate limits** (shared across workers via Redis when configured):
  1200 requests/minute per token, 300/minute per anonymous IP, 30/minute
  on auth paths. Only admitted requests count toward the sliding window.
- **Tenancy:** resources are tenant-scoped. Admin-token calls may pass
  `?tenant=`; session calls are scoped to the session's tenant. The
  pseudo-device `"local"` is a virtual device, not a tenant row.
- **Tasks** are created through the single approval-gated path
  (`POST /api/tasks` or `POST /api/v1/tasks`): Tier 1–2 tools queue
  immediately, Tier 3–4 need a recorded approval (`needs_approval` +
  `approval_id`). `request_key` makes creation idempotent. Every task
  creation, approval decision, and device action is written to the
  hash-chained audit table (`GET /api/audit`, `GET /api/audit/verify`).
- **Emergency stop** (`POST /api/emergency-stop`) blocks new task creation
  plane-wide until cleared.

## Route tables

<!-- BEGIN GENERATED ROUTES (scripts/generate_api_reference.py) -->
### Health, readiness & meta

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| MOUNT | `/assets` | — | Static files |
| GET HEAD | `/docs` | Public | swagger_ui_html |
| GET HEAD | `/docs/oauth2-redirect` | Public | swagger_ui_redirect |
| GET | `/health` | Public | Health check |
| GET | `/health/live` | Public | Liveness probe |
| GET | `/health/production-checklist` | Admin, Session | Production readiness checklist |
| GET | `/health/ready` | Public | Readiness probe |
| GET | `/install.ps1` | Public | Windows agent installer |
| GET | `/install.sh` | Public | Linux/macOS agent installer |
| GET | `/metrics` | Bearer, Admin, Session | Prometheus metrics |
| GET HEAD | `/openapi.json` | Public | openapi |
| GET HEAD | `/redoc` | Public | redoc_html |
| GET | `/{full_path:path}` | Public | serve_spa_frontend |

### Devices

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/devices` | Admin, Session | List devices |
| POST | `/api/devices/enroll` | Admin, Session | Enroll a new device |
| DELETE | `/api/devices/{device_id}` | Admin, Session | delete_device |
| PUT | `/api/devices/{device_id}` | Admin, Session | update_device |
| GET | `/api/devices/{device_id}/desktop/frame` | Admin, Session | get_desktop_frame |
| POST | `/api/devices/{device_id}/desktop/input` | Admin, Session | send_desktop_input |
| GET | `/api/devices/{device_id}/desktop/live.jpg` | Bearer, Admin, Session | get_device_desktop_live_jpeg |
| GET | `/api/devices/{device_id}/desktop/monitors` | Admin, Session | get_device_monitors |
| POST | `/api/devices/{device_id}/desktop/webrtc/session` | Admin, Session | create_webrtc_session |
| DELETE | `/api/devices/{device_id}/desktop/webrtc/{session_id}` | Admin, Session | end_webrtc_session |
| GET | `/api/devices/{device_id}/desktop/webrtc/{session_id}` | Admin, Session | get_webrtc_session |
| POST | `/api/devices/{device_id}/desktop/webrtc/{session_id}/answer` | Admin, Session | post_webrtc_answer |
| POST | `/api/devices/{device_id}/desktop/webrtc/{session_id}/candidates` | Admin, Session | post_webrtc_viewer_candidate |
| PUT | `/api/devices/{device_id}/metadata` | Admin, Session | set_metadata |
| POST | `/api/devices/{device_id}/mtls-fingerprint` | Admin, Session | set_device_mtls_fingerprint |
| GET | `/api/devices/{device_id}/overview` | Admin, Session | get_device_overview |
| DELETE | `/api/devices/{device_id}/policy` | Admin, Device, Session | reset_device_policy_api |
| GET | `/api/devices/{device_id}/policy` | Admin, Session | get_device_policy_api |
| PUT | `/api/devices/{device_id}/policy` | Admin, Device, Session | set_device_policy_api |
| POST | `/api/devices/{device_id}/refresh` | Admin, Session | refresh_device_telemetry |
| POST | `/api/devices/{device_id}/revoke` | Admin, Session | revoke |
| PUT | `/api/devices/{device_id}/ring` | Admin, Session | set_device_update_ring |
| GET | `/api/devices/{device_id}/telemetry` | Admin, Session | get_device_telemetry |
| GET | `/api/devices/{device_id}/terminal/sessions` | Admin, Session | list_terminal_endpoint |
| POST | `/api/devices/{device_id}/terminal/sessions` | Admin, Session | create_terminal_endpoint |
| DELETE | `/api/devices/{device_id}/terminal/{session_id}` | Admin, Session | delete_terminal_endpoint |
| POST | `/api/devices/{device_id}/terminal/{session_id}/input` | Admin, Session | input_terminal_endpoint |
| POST | `/api/devices/{device_id}/terminal/{session_id}/resize` | Admin, Session | resize_terminal_endpoint |
| GET | `/api/devices/{device_id}/terminal/{session_id}/stream` | Bearer, Admin, Session | stream_terminal_endpoint |
| POST | `/api/devices/{device_id}/update` | Admin, Session | update_device_agent |

### Tasks

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| DELETE | `/api/tasks` | Admin, Session | clear_tasks |
| GET | `/api/tasks` | Admin, Session | list_tasks |
| POST | `/api/tasks` | Admin, Session | submit_task |
| GET | `/api/tasks/dead-letter` | Admin, Session | list_dead_letter_tasks |
| DELETE | `/api/tasks/dead-letter/{dlq_id}` | Admin, Session | purge_dead_letter_task |
| POST | `/api/tasks/dead-letter/{dlq_id}/replay` | Admin, Session | replay_dead_letter_task |
| GET | `/api/tasks/history` | Admin, Session | task_history |
| DELETE | `/api/tasks/{task_id}` | Admin, Session | delete_task |
| GET | `/api/tasks/{task_id}` | Admin, Session | get_task |
| PUT | `/api/tasks/{task_id}` | Admin, Session | update_task |
| POST | `/api/tasks/{task_id}/cancel` | Admin, Session | cancel_task |
| GET | `/api/tasks/{task_id}/events` | Admin, Session | get_task_events |
| POST | `/api/tasks/{task_id}/progress` | mTLS, Device | task_progress |
| GET | `/api/tasks/{task_id}/result` | Admin, Session | get_task_result |

### Agent API

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| POST | `/api/agent/heartbeat` | mTLS, Device | heartbeat |
| GET | `/api/agent/policy` | mTLS, Device | get_agent_policy |
| PUT | `/api/agent/policy` | mTLS, Device | update_agent_policy |
| POST | `/api/agent/rotate` | mTLS, Device | rotate_token |
| GET | `/api/agent/tasks/next` | mTLS, Device | next_task |
| GET | `/api/agent/tasks/{task_id}/execution-state` | mTLS, Device | agent_task_execution_state |
| POST | `/api/agent/tasks/{task_id}/progress` | mTLS, Device | task_progress |
| POST | `/api/agent/tasks/{task_id}/result` | mTLS, Device | task_result |
| GET | `/api/agent/webrtc/{session_id}` | mTLS, Device | agent_get_webrtc_session |
| POST | `/api/agent/webrtc/{session_id}/candidates` | mTLS, Device | agent_post_webrtc_candidate |
| POST | `/api/agent/webrtc/{session_id}/error` | mTLS, Device | agent_post_webrtc_error |
| POST | `/api/agent/webrtc/{session_id}/offer` | mTLS, Device | agent_post_webrtc_offer |
| GET | `/api/agent/whoami` | mTLS, Device | whoami |
| WS | `/api/agent/ws` | Device | agent_ws |

### Auth, users & OIDC

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/auth/csrf-token` | Session | get_csrf_token |
| POST | `/api/auth/login` | Public | login |
| POST | `/api/auth/logout` | Session | logout |
| GET | `/api/auth/me` | Admin, Session | me |
| GET | `/api/auth/oidc/callback` | Public | oidc_callback |
| GET | `/api/auth/oidc/configured` | Public | oidc_configured |
| POST | `/api/auth/oidc/exchange` | Public | oidc_exchange |
| GET | `/api/auth/oidc/login` | Public | oidc_login |
| GET | `/api/auth/users` | Admin, Session | list_users |
| POST | `/api/auth/users` | Admin, Session | create_user |
| PATCH | `/api/auth/users/{username}` | Admin, Session | update_user |
| GET | `/api/roles` | Admin, Session | list_roles_api |
| POST | `/api/roles/{role_id}/enable` | Admin, Session | enable_role_agent_api |
| GET | `/api/tenants` | Admin, Session | list_tenants |

### Approvals

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/approvals` | Admin, Session | list_approvals |
| GET | `/api/approvals/quick/{approval_id}` | — | quick_approval |
| DELETE | `/api/approvals/{approval_id}` | Admin, Session | delete_approval |
| PUT | `/api/approvals/{approval_id}` | Admin, Session | update_approval |
| POST | `/api/approvals/{approval_id}/decide` | Admin, Session | decide_approval |
| POST | `/api/approvals/{approval_id}/replan` | Admin, Session | replan_approval |

### Audit

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/audit` | Admin, Session | list_audit |
| GET | `/api/audit/export` | Admin, Session | export_audit |
| POST | `/api/audit/retention` | Admin, Session | apply_audit_retention |
| GET | `/api/audit/verify` | Admin, Session | verify_audit_chain_api |
| DELETE PUT | `/api/audit/{audit_id}` | — | immutable_audit |

### Policies

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/policies` | Admin, Session | get_policy |
| POST | `/api/policies` | Admin, Session | create_policy |
| GET | `/api/policies/history` | Admin, Session | policy_history |
| POST | `/api/policies/simulate` | Admin, Session | simulate_policy |
| DELETE | `/api/policies/{version}` | Admin, Session | delete_policy_version |
| PUT | `/api/policies/{version}` | Admin, Session | update_policy_version |
| POST | `/api/policy/simulate` | Admin, Session | simulate_policy_call |

### Workflows, schedules & triggers

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/schedules` | Admin, Session | list_schedules |
| POST | `/api/schedules` | Admin, Session | create_schedule |
| DELETE | `/api/schedules/{schedule_id}` | Admin, Session | delete_schedule |
| POST | `/api/schedules/{schedule_id}/run-now` | Admin, Session | run_schedule_now |
| POST | `/api/schedules/{schedule_id}/toggle` | Admin, Session | toggle_schedule |
| GET | `/api/triggers` | Admin, Session | list_triggers |
| POST | `/api/triggers` | Admin, Session | create_trigger |
| DELETE | `/api/triggers/{trigger_id}` | Admin, Session | delete_trigger |
| POST | `/api/triggers/{trigger_id}/test` | Admin, Session | test_trigger |
| POST | `/api/triggers/{trigger_id}/toggle` | Admin, Session | toggle_trigger |
| GET | `/api/workflows` | Admin, Session | list_workflows |
| POST | `/api/workflows` | Admin, Session | submit_workflow |
| GET | `/api/workflows/templates` | Admin, Session | get_workflow_templates |
| POST | `/api/workflows/templates` | Admin, Session | create_workflow_template |
| DELETE | `/api/workflows/templates/{template_id}` | Admin, Session | delete_workflow_template |
| DELETE | `/api/workflows/{workflow_id}` | Admin, Session | delete_workflow |
| GET | `/api/workflows/{workflow_id}` | Admin, Session | get_workflow |
| POST | `/api/workflows/{workflow_id}/cancel` | Admin, Session | cancel_workflow |
| POST | `/api/workflows/{workflow_id}/retry` | Admin, Session | retry_workflow |
| POST | `/api/workflows/{workflow_id}/schedule` | Admin, Session | schedule_workflow |
| GET | `/api/workflows/{workflow_id}/stream` | Admin, Session | stream_workflow_progress |

### Backups

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/admin/backups` | Admin, Session | list_backups |
| POST | `/api/admin/backups` | Admin, Session | create_backup |
| GET | `/api/admin/backups/{filename}` | Admin, Session | download_backup |
| POST | `/api/admin/backups/{filename}/restore` | Admin, Session | restore_backup |

### Sessions & desktop streaming (v1)

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| POST | `/api/stream-ticket` | Bearer, Admin, Session | mint_stream_ticket_route |
| POST | `/api/v1/agents/{device_id}/heartbeat` | mTLS, Device | v1_agent_heartbeat |
| GET | `/api/v1/audit` | Bearer, Admin, Session | v1_get_audit |
| POST | `/api/v1/chat/completions` | Admin, Session | chat_completions |
| GET | `/api/v1/devices` | Bearer, Admin, Session | v1_list_devices |
| GET | `/api/v1/devices/{device_id}` | Bearer, Admin, Session | v1_get_device |
| GET | `/api/v1/devices/{device_id}/files` | Bearer, Admin, Session | v1_list_device_files |
| GET | `/api/v1/devices/{device_id}/files/download` | Bearer, Admin, Session | v1_download_device_file |
| POST | `/api/v1/devices/{device_id}/files/upload` | Bearer, Admin, Session | v1_upload_device_file |
| GET | `/api/v1/devices/{device_id}/monitors` | Bearer, Admin, Session | v1_get_device_monitors |
| GET | `/api/v1/devices/{device_id}/status` | Bearer, Admin, Session | v1_get_device_status |
| GET | `/api/v1/devices/{device_id}/ui-elements` | Bearer, Admin, Session | v1_get_device_ui_elements |
| GET | `/api/v1/models` | Public | list_models |
| GET | `/api/v1/policies` | Bearer, Admin, Session | v1_get_policies |
| GET | `/api/v1/sessions` | Bearer, Admin, Session | v1_list_sessions |
| POST | `/api/v1/sessions` | Bearer, Admin, Session | v1_create_session |
| GET | `/api/v1/sessions/ice-servers` | Bearer, Admin, Session | v1_get_ice_servers |
| GET | `/api/v1/sessions/{session_id}` | Bearer, Admin, Session | v1_get_session |
| POST | `/api/v1/sessions/{session_id}/answer` | Bearer, Admin, Session | v1_post_answer |
| POST | `/api/v1/sessions/{session_id}/clipboard` | Bearer, Admin, Session | v1_session_clipboard |
| POST | `/api/v1/sessions/{session_id}/ice` | Bearer, Admin, mTLS, Device, Session | v1_post_ice |
| PATCH | `/api/v1/sessions/{session_id}/mode` | Bearer, Admin, Session | v1_update_session_mode |
| POST | `/api/v1/sessions/{session_id}/mode` | Bearer, Admin, Session | v1_update_session_mode |
| POST | `/api/v1/sessions/{session_id}/offer` | Bearer, Admin, mTLS, Device, Session | v1_post_offer |
| PATCH | `/api/v1/sessions/{session_id}/quality` | Bearer, Admin, Session | v1_update_session_quality |
| GET | `/api/v1/sessions/{session_id}/recording` | Bearer, Admin, Session | v1_get_session_recording |
| POST | `/api/v1/sessions/{session_id}/recording/seal` | Bearer, Admin, Session | v1_seal_session_recording |
| POST | `/api/v1/sessions/{session_id}/terminate` | Bearer, Admin, Session | v1_terminate_session |
| GET | `/api/v1/tasks` | Bearer, Admin, Session | v1_list_tasks |
| POST | `/api/v1/tasks` | Bearer, Admin, Session | v1_create_task |
| GET | `/api/v1/tasks/{task_id}` | Bearer, Admin, Session | v1_get_task |
| WS | `/api/v1/ws/sessions/{session_id}` | Ticket (?ticket=) | v1_session_websocket |

### AI & custom agents

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/agents` | Admin, Session | list_agents |
| POST | `/api/agents` | Admin, Session | create_agent |
| DELETE | `/api/agents/{agent_id}` | Admin, Session | delete_agent |
| PATCH | `/api/agents/{agent_id}` | Admin, Session | update_agent |
| POST | `/api/agents/{agent_id}/ask` | Admin, Session | ask_agent |
| DELETE | `/api/agents/{agent_id}/memory/chunks` | Admin, Session | clear_agent_memory_chunks |
| GET | `/api/agents/{agent_id}/memory/chunks` | Admin, Session | agent_memory_chunks |
| POST | `/api/agents/{agent_id}/memory/chunks` | Admin, Session | append_agent_memory_chunk |
| POST | `/api/agents/{agent_id}/run` | Admin, Session | run_agent_now |
| GET | `/api/agents/{agent_id}/runs` | Admin, Session | agent_runs |
| POST | `/api/ai/agent` | Admin, Session | run_autonomous_agent |
| POST | `/api/ai/chat` | Admin, Session | ai_chat |
| POST | `/api/ai/chat/stream` | Admin, Session | ai_chat_stream |
| GET | `/api/ai/devices` | Admin, Session | get_ai_fleet |
| POST | `/api/ai/execute` | Admin, Session | execute_ai_tool |
| GET | `/api/ai/tools` | Admin, Session | get_ai_tools |
| POST | `/v1/chat/completions` | Admin, Session | chat_completions |
| GET | `/v1/models` | Public | list_models |

### MCP servers

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/mcp/oauth/callback` | Public | mcp_oauth_callback |
| POST | `/api/mcp/oauth/discover` | Admin, Session | discover_mcp_oauth |
| GET | `/api/mcp/servers` | Admin, Session | list_mcp_servers |
| POST | `/api/mcp/servers` | Admin, Session | create_mcp_server |
| DELETE | `/api/mcp/servers/{server_id}` | Admin, Session | delete_mcp_server |
| PATCH | `/api/mcp/servers/{server_id}` | Admin, Session | update_mcp_server |
| POST | `/api/mcp/servers/{server_id}/execute` | Admin, Session | run_mcp_tool_test |
| POST | `/api/mcp/servers/{server_id}/oauth/start` | Admin, Session | start_mcp_oauth |
| POST | `/api/mcp/servers/{server_id}/sync` | Admin, Session | sync_mcp_server |
| GET | `/api/mcp/templates` | Admin, Session | get_mcp_templates |
| GET | `/api/mcp/tool-policies` | Admin, Session | get_mcp_tool_policies_route |
| PUT | `/api/mcp/tool-policies` | Admin, Session | update_mcp_tool_policies_route |

### Connectors

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| DELETE | `/api/connectors/m365` | Admin, Session | m365_disconnect |
| GET | `/api/connectors/m365/callback` | Public | m365_callback |
| POST | `/api/connectors/m365/login` | Admin, Session | m365_login |
| GET | `/api/connectors/m365/status` | Admin, Session | m365_status |

### Admin, fleet & operations

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/activity/live` | Admin, Session | live_activity |
| GET | `/api/activity/stream` | Admin, Session | live_activity_stream |
| GET | `/api/alerts/config` | Admin, Session | get_alerts_config_route |
| PUT | `/api/alerts/config` | Admin, Session | update_alerts_config_route |
| GET | `/api/alerts/deliveries` | Admin, Session | list_alert_deliveries |
| POST | `/api/alerts/test` | Admin, Session | test_alerts_route |
| GET | `/api/connections` | Admin, Session | connections |
| DELETE | `/api/connections/clients/{client_id}` | Admin, Session | delete_oauth_client |
| PUT | `/api/connections/clients/{client_id}` | Admin, Session | update_oauth_client |
| POST | `/api/emergency-stop` | Admin, Session | emergency_stop |
| POST | `/api/emergency-stop/clear` | Admin, Session | emergency_clear |
| GET | `/api/events/stream` | Admin, Session | stream_control_events |
| POST | `/api/fleet/broadcast` | Admin, Session | broadcast_fleet_command |
| GET | `/api/fleet/broadcast/{batch_id}` | Admin, Session | get_broadcast_status |
| GET | `/api/fleet/desktop-capabilities` | Admin, Session | fleet_desktop_capabilities |
| GET | `/api/fleet/desktop/frames` | Admin, Session | get_fleet_desktop_frames |
| GET | `/api/fleet/health` | Admin, Session | fleet_health_summary |
| GET | `/api/fleet/inventory` | Admin, Session | inventory |
| GET | `/api/fleet/notifications` | Admin, Session | notifications |
| GET | `/api/fleet/rollouts` | Admin, Session | list_fleet_rollouts |
| POST | `/api/fleet/rollouts` | Admin, Session | create_fleet_rollout |
| POST | `/api/fleet/rollouts/{rollout_id}/advance` | Admin, Session | advance_fleet_rollout |
| POST | `/api/fleet/rollouts/{rollout_id}/pause` | Admin, Session | pause_fleet_rollout |
| POST | `/api/fleet/rollouts/{rollout_id}/resume` | Admin, Session | resume_fleet_rollout |
| POST | `/api/fleet/rollouts/{rollout_id}/rollback` | Admin, Session | rollback_fleet_rollout |
| GET | `/api/fleet/telemetry` | Admin, Session | get_fleet_telemetry |
| POST | `/api/fleet/update` | Admin, Session | update_fleet |
| GET | `/api/fleet/version` | Admin, Session | get_fleet_version |
| GET | `/api/logs` | Admin, Session | service_logs |
| GET | `/api/metrics/summary` | Admin, Session | metrics_summary_api |
| GET | `/api/settings/ai` | Admin, Session | get_ai_settings |
| POST | `/api/settings/ai` | Admin, Session | update_ai_settings |
| GET | `/api/settings/webhook` | Admin, Session | get_webhook_setting |
| POST | `/api/settings/webhook` | Admin, Session | set_webhook_setting |
| POST | `/api/settings/webhook/test` | Admin, Session | test_webhook_setting |
| POST | `/api/supervisor/handoff` | Admin, Session | supervisor_handoff_api |
| GET | `/api/supervisor/handoffs/{handoff_id}` | Admin, Session | supervisor_handoff_status_api |
| POST | `/api/supervisor/recover` | Admin, Session | supervisor_recover_api |
| GET | `/api/traces/{trace_id}` | Admin, Session | get_trace_timeline |

*238 (method, path) pairs listed — generated from the live app; see the note at the top of this file.*
<!-- END GENERATED ROUTES -->

## Mounted surfaces (combined app)

`python -m ai_bridge.control` serves the combined app, which dispatches
by path prefix before the control plane sees the request:

- `/mcp` — Streamable HTTP MCP server (109 tools), authenticated at the
  edge by the OAuth gateway and again by the MCP server's own bearer
  check. See `MCP_REFERENCE.md`.
- `/oauth/*`, `/.well-known/oauth-*`, `/.well-known/openid-configuration`,
  `/internal/verify` — the OAuth gateway (authorization server, token,
  registration, revocation, and the internal verification endpoint used
  by nginx `auth_request`).
- `/assets` and the `/{full_path:path}` catch-all — the dashboard SPA
  static files (when the dashboard build exists).

## Regenerating

```sh
python3 scripts/generate_api_reference.py          # rewrite the tables
python3 scripts/generate_api_reference.py --check  # fail if stale
```

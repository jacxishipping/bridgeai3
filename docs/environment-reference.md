# Environment Variable Reference

> Generated from `src/ai_bridge/` and `agents/` by `scripts/generate_env_reference.py`.
> Run `python scripts/generate_env_reference.py` after adding or removing configuration variables.

Total documented variables: **147**

## Control plane & runtime

| Variable | Observed default(s) | Referenced in |
|---|---|---|
| `AI_BRIDGE_ALLOWED_APPS` | `''` | `src/ai_bridge/tools/_common.py` |
| `AI_BRIDGE_ALLOWED_SERVICES` | `''` | `src/ai_bridge/tools/_common.py` |
| `AI_BRIDGE_APPROVAL_TTL_SECONDS` | `'3600'` | `src/ai_bridge/control/ai_api.py` |
| `AI_BRIDGE_AUDIT_LOG` | `''` | `src/ai_bridge/control/dispatch.py` |
| `AI_BRIDGE_AWS_SECRET_ID` | — | `src/ai_bridge/control/secrets_backend.py` |
| `AI_BRIDGE_BROWSERBASE_API_KEY` | `''` | `src/ai_bridge/connectors/browserbase.py` |
| `AI_BRIDGE_BROWSERBASE_PROJECT_ID` | `''` | `src/ai_bridge/connectors/browserbase.py` |
| `AI_BRIDGE_BROWSER_EVALUATE_ALLOWLIST` | `''` | `src/ai_bridge/browser_tools.py` |
| `AI_BRIDGE_BROWSER_GVISOR` | `''` | `src/ai_bridge/browser_isolation_os.py` |
| `AI_BRIDGE_BROWSER_HARD_ISOLATION` | `'0'` | `src/ai_bridge/browser_tools.py` |
| `AI_BRIDGE_BROWSER_NAV_TIMEOUT_SEC` | — | `src/ai_bridge/browser_tools.py` |
| `AI_BRIDGE_BROWSER_NETNS` | `''` | `src/ai_bridge/browser_isolation_os.py` |
| `AI_BRIDGE_BROWSER_NETWORK_LOCKDOWN` | `'0'` | `src/ai_bridge/browser_tools.py` |
| `AI_BRIDGE_BROWSER_REQUIRE_SANDBOX` | `''` | `src/ai_bridge/browser_isolation_os.py` |
| `AI_BRIDGE_CONFIGURE_LOGGING` | `'1'` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_CONTROL_HOST` | — | `src/ai_bridge/control/__main__.py` |
| `AI_BRIDGE_CONTROL_PORT` | `'8000'` | `src/ai_bridge/control/__main__.py` |
| `AI_BRIDGE_CORS_ORIGINS` | `''` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_CUSTOM_API_KEY` | — | `src/ai_bridge/control/ai_api.py` |
| `AI_BRIDGE_CUSTOM_BASE_URL` | `''` | `src/ai_bridge/control/ai_api.py`, `src/ai_bridge/control/custom_agents.py` |
| `AI_BRIDGE_DASHBOARD_URL` | `''` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_DATA_DIR` | — | `src/ai_bridge/config.py`, `src/ai_bridge/connectors/microsoft365.py`, `src/ai_bridge/control/app.py` (+2 more) |
| `AI_BRIDGE_DEAD_LETTER_ON_TIMEOUT` | `'1'` | `src/ai_bridge/control/orchestration.py` |
| `AI_BRIDGE_DEV_SIMULATE_INTENT` | `''` | `src/ai_bridge/webrtc_stream.py` |
| `AI_BRIDGE_DISABLE_HOST_TOOLS` | `'1'` | `src/ai_bridge/server.py`, `src/ai_bridge/tools/files.py`, `src/ai_bridge/tools/shell.py` |
| `AI_BRIDGE_EMBED_MCP` | `''` | `src/ai_bridge/control/combined.py` |
| `AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN` | `''`, `'0'` | `src/ai_bridge/control/app.py`, `src/ai_bridge/control/auth.py` |
| `AI_BRIDGE_FIRECRAWL_API_KEY` | `''` | `src/ai_bridge/connectors/firecrawl.py` |
| `AI_BRIDGE_GATEWAY_VERIFY_URL` | `'http://127.0.0.1:8002/internal/verify'` | `src/ai_bridge/control/combined.py`, `src/ai_bridge/server.py` |
| `AI_BRIDGE_GIT_COMMIT` | `''` | `agents/common/poller.py` |
| `AI_BRIDGE_HTTP` | — | `src/ai_bridge/server.py` |
| `AI_BRIDGE_ICE_SERVERS` | `''` | `src/ai_bridge/control/production_readiness.py`, `src/ai_bridge/webrtc_stream.py` |
| `AI_BRIDGE_INSTALL_DIR` | `''` | `agents/macos/agent_main.py`, `agents/windows/agent_main.py` |
| `AI_BRIDGE_JSON_LOGS` | `'1'` | `src/ai_bridge/logging_config.py` |
| `AI_BRIDGE_LIBWEBRTC_E2E_VERIFIED` | `''` | `src/ai_bridge/control/desktop_capabilities.py`, `src/ai_bridge/native_encode.py` |
| `AI_BRIDGE_MAX_TERMINAL_SESSIONS` | `'32'` | `src/ai_bridge/terminal_manager.py` |
| `AI_BRIDGE_MAX_TRANSFER_BYTES` | — | `src/ai_bridge/tools/transfers.py` |
| `AI_BRIDGE_NATIVE_DLL_SHA256` | `''` | `src/ai_bridge/screen_capture.py` |
| `AI_BRIDGE_NATIVE_ENCODE` | `'auto'` | `src/ai_bridge/native_encode.py` |
| `AI_BRIDGE_NATIVE_WEBRTC` | `'auto'` | `src/ai_bridge/native_webrtc.py` |
| `AI_BRIDGE_POLICY_SIGNING_KEY` | — | `src/ai_bridge/control/policy_bundle.py` |
| `AI_BRIDGE_PROFILE` | `''` | `src/ai_bridge/control/production_readiness.py`, `src/ai_bridge/server.py` |
| `AI_BRIDGE_SCREEN_RELAY_BIN` | `''` | `src/ai_bridge/screen_relay.py` |
| `AI_BRIDGE_SLOW_QUERY_MS` | `'250'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_SOM_SYNTHETIC_FALLBACK` | `'0'` | `src/ai_bridge/tools/desktop_api.py` |
| `AI_BRIDGE_STALE_TASK_REQUEUE` | `'0'` | `src/ai_bridge/control/orchestration.py` |
| `AI_BRIDGE_TARGET_AGENT_VERSION` | `''` | `src/ai_bridge/control/fleet_health.py` |
| `AI_BRIDGE_TASK_MAX_RETRY_BACKOFF_SEC` | `'300.0'` | `src/ai_bridge/control/orchestration.py` |
| `AI_BRIDGE_TASK_NETNS` | `''` | `src/ai_bridge/execution.py` |
| `AI_BRIDGE_TASK_RETRY_BACKOFF_SEC` | `'5.0'` | `src/ai_bridge/control/orchestration.py` |
| `AI_BRIDGE_TASK_RUNNING_LEASE_SEC` | `'900'` | `src/ai_bridge/control/orchestration.py` |
| `AI_BRIDGE_TASK_SWEEP_EVERY` | `'3'` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_TASK_SWEEP_INTERVAL_SEC` | `'30'` | `src/ai_bridge/control/orchestration.py` |
| `AI_BRIDGE_TENANT` | `'default'` | `src/ai_bridge/config.py` |
| `AI_BRIDGE_TERMINAL_SESSION_IDLE_TTL_SEC` | `'1800'` | `src/ai_bridge/terminal_manager.py` |
| `AI_BRIDGE_TRANSFERS_DIR` | — | `src/ai_bridge/tools/transfers.py` |
| `AI_BRIDGE_URL` | — | `src/ai_bridge/client.py` |
| `AI_BRIDGE_WEBHOOK_URL` | `''` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_WORKSPACE` | — | `src/ai_bridge/config.py`, `src/ai_bridge/execution.py` |
| `AI_BRIDGE_WORM_SIEM_HMAC_SECRET` | — | `src/ai_bridge/control/worm_siem.py` |
| `AI_BRIDGE_WORM_SIEM_REQUIRED` | `''` | `src/ai_bridge/control/worm_siem.py` |
| `AI_BRIDGE_WORM_SIEM_URL` | — | `src/ai_bridge/control/worm_siem.py` |
| `AI_BRIDGE_YOU_API_KEY` | `''` | `src/ai_bridge/connectors/you_search.py` |
| `PORT` | — | `src/ai_bridge/control/__main__.py` |
| `RENDER` | `''` | `src/ai_bridge/control/production_readiness.py` |

## Authentication, secrets & security

| Variable | Observed default(s) | Referenced in |
|---|---|---|
| `AI_BRIDGE_ADMIN_TOKEN` | `''` | `src/ai_bridge/client.py`, `src/ai_bridge/config.py`, `src/ai_bridge/control/app.py` (+3 more) |
| `AI_BRIDGE_ALLOW_AUTO_UPDATE` | `''` | `src/ai_bridge/updates.py` |
| `AI_BRIDGE_ALLOW_BOOTSTRAP` | — | `src/ai_bridge/control/routers/auth.py` |
| `AI_BRIDGE_ALLOW_BROWSER_NO_SANDBOX` | `''`, `'0'` | `src/ai_bridge/browser_isolation_os.py`, `src/ai_bridge/browser_tools.py` |
| `AI_BRIDGE_ALLOW_CLIPBOARD` | `''` | `src/ai_bridge/control/v1_routes.py` |
| `AI_BRIDGE_ALLOW_LOCAL_EXECUTION` | `'0'` | `src/ai_bridge/control/ai_api.py` |
| `AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE` | `'1'` | `agents/common/poller.py` |
| `AI_BRIDGE_ALLOW_TIER3` | `'0'` | `src/ai_bridge/control/app.py`, `src/ai_bridge/control/task_service.py` |
| `AI_BRIDGE_CSRF_DISABLED` | `''` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_CSRF_SECRET` | — | `src/ai_bridge/control/csrf.py` |
| `AI_BRIDGE_OIDC_CLIENT_ID` | `''` | `src/ai_bridge/control/oidc.py` |
| `AI_BRIDGE_OIDC_CLIENT_SECRET` | `''` | `src/ai_bridge/control/oidc.py` |
| `AI_BRIDGE_OIDC_DASHBOARD_URL` | `'/'` | `src/ai_bridge/control/oidc.py`, `src/ai_bridge/control/routers/auth.py` |
| `AI_BRIDGE_OIDC_GROUP_CLAIM` | — | `src/ai_bridge/control/oidc.py` |
| `AI_BRIDGE_OIDC_ISSUER` | `''` | `src/ai_bridge/control/oidc.py` |
| `AI_BRIDGE_OIDC_LEAST_PRIVILEGE` | `''` | `src/ai_bridge/control/oidc.py` |
| `AI_BRIDGE_OIDC_REDIRECT_URI` | `''` | `src/ai_bridge/control/oidc.py` |
| `AI_BRIDGE_OIDC_ROLE_MAP` | — | `src/ai_bridge/control/oidc.py` |
| `AI_BRIDGE_SECRETS_BACKEND` | — | `src/ai_bridge/control/secrets_backend.py` |
| `AI_BRIDGE_TOKEN` | — | `src/ai_bridge/client.py` |
| `AI_BRIDGE_TOKEN_FILE` | `''` | `agents/linux/agent_main.py`, `agents/linux/tui_app.py`, `agents/macos/agent_main.py` (+3 more) |
| `AI_BRIDGE_VAULT_ADDR` | — | `src/ai_bridge/control/production_readiness.py`, `src/ai_bridge/control/secrets_backend.py` |
| `AI_BRIDGE_VAULT_SECRET_PATH` | — | `src/ai_bridge/control/secrets_backend.py` |
| `AI_BRIDGE_VAULT_TOKEN` | — | `src/ai_bridge/control/secrets_backend.py` |

## Database, pooling & retention

| Variable | Observed default(s) | Referenced in |
|---|---|---|
| `AI_BRIDGE_BACKUP_DIR` | `''` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_DB_LOCK_TIMEOUT_MS` | `'5000'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_DB_POOL_ENABLED` | `'1'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_DB_POOL_MAX` | `'10'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_DB_POOL_MIN` | `'2'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_DB_POOL_TIMEOUT` | `'15'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_DB_STATEMENT_TIMEOUT_MS` | `'30000'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_RETENTION_IDEMPOTENCY_HOURS` | `'24'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_RETENTION_SWEEP_EVERY` | `'60'` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_RETENTION_TASKS_DAYS` | `'90'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_RETENTION_TASK_EVENTS_DAYS` | `'30'` | `src/ai_bridge/control/db.py` |
| `AI_BRIDGE_RETENTION_TELEMETRY_KEEP` | `'500'` | `src/ai_bridge/control/db.py` |
| `DATABASE_URL` | — | `src/ai_bridge/control/db.py`, `src/ai_bridge/control/production_readiness.py` |

## Multi-worker & Redis

| Variable | Observed default(s) | Referenced in |
|---|---|---|
| `AI_BRIDGE_REDIS_URL` | — | `src/ai_bridge/control/agent_channel.py`, `src/ai_bridge/control/app.py`, `src/ai_bridge/control/durable_ratelimit.py` |
| `AI_BRIDGE_WORKERS` | `''`, `'1'` | `src/ai_bridge/control/csrf.py`, `src/ai_bridge/control/production_readiness.py`, `src/ai_bridge/server.py` |
| `REDIS_URL` | — | `src/ai_bridge/control/agent_channel.py`, `src/ai_bridge/control/app.py`, `src/ai_bridge/control/durable_ratelimit.py` |

## Observability, metrics & alerts

| Variable | Observed default(s) | Referenced in |
|---|---|---|
| `AI_BRIDGE_ALERT_DLQ_THRESHOLD` | `'5'` | `src/ai_bridge/control/alerts.py` |
| `AI_BRIDGE_ALERT_SPOOL_THRESHOLD` | `'25'` | `src/ai_bridge/control/alerts.py` |
| `AI_BRIDGE_ALERT_VERIFY_AUDIT` | `'0'` | `src/ai_bridge/control/alerts.py` |
| `AI_BRIDGE_LOG_LEVEL` | — | `src/ai_bridge/logging_config.py` |
| `AI_BRIDGE_METRICS_REQUIRE_AUTH` | `''` | `src/ai_bridge/control/routers/health.py` |
| `AI_BRIDGE_METRICS_TOKEN` | `''` | `src/ai_bridge/control/routers/health.py` |
| `ALERT_WEBHOOK_URL` | `''` | `src/ai_bridge/control/alerts.py` |
| `DISCORD_WEBHOOK_URL` | `''` | `src/ai_bridge/control/alerts.py` |
| `SLACK_WEBHOOK_URL` | `''` | `src/ai_bridge/control/alerts.py` |
| `TELEGRAM_BOT_TOKEN` | `''` | `src/ai_bridge/control/alerts.py` |
| `TELEGRAM_CHAT_ID` | `''` | `src/ai_bridge/control/alerts.py` |

## Agents, spool & rollouts

| Variable | Observed default(s) | Referenced in |
|---|---|---|
| `AI_BRIDGE_AGENT_REPO_DIR` | — | `src/ai_bridge/tools/fleet.py` |
| `AI_BRIDGE_CONTROL_URL` | `''`, `'http://127.0.0.1:8000'` | `agents/linux/agent_main.py`, `agents/linux/tui_app.py`, `agents/macos/agent_main.py` (+6 more) |
| `AI_BRIDGE_DEVICE_MTLS` | `''` | `src/ai_bridge/control/device_mtls.py` |
| `AI_BRIDGE_DEVICE_TOKEN` | `''` | `agents/linux/agent_main.py`, `agents/linux/tui_app.py`, `agents/macos/agent_main.py` (+4 more) |
| `AI_BRIDGE_DEVICE_TOKEN_TTL_DAYS` | `'7'` | `src/ai_bridge/control/app.py` |
| `AI_BRIDGE_UPDATE_STAGING` | — | `agents/macos/agent_main.py`, `agents/windows/agent_main.py` |

## Desktop & WebRTC streaming

| Variable | Observed default(s) | Referenced in |
|---|---|---|
| `AI_BRIDGE_DESKTOP_AUDIO` | `''` | `src/ai_bridge/desktop_audio.py`, `src/ai_bridge/webrtc_stream.py` |
| `AI_BRIDGE_DESKTOP_ENABLED` | `'0'` | `src/ai_bridge/control/app.py`, `src/ai_bridge/control/task_service.py` |
| `AI_BRIDGE_TURN_CREDENTIAL` | `''` | `src/ai_bridge/webrtc_stream.py` |
| `AI_BRIDGE_TURN_HOST` | `''` | `src/ai_bridge/webrtc_stream.py` |
| `AI_BRIDGE_TURN_SECRET` | `''` | `src/ai_bridge/control/production_readiness.py`, `src/ai_bridge/webrtc_stream.py` |
| `AI_BRIDGE_TURN_URL` | `''` | `src/ai_bridge/control/production_readiness.py`, `src/ai_bridge/webrtc_stream.py` |
| `AI_BRIDGE_TURN_USERNAME` | `''` | `src/ai_bridge/webrtc_stream.py` |
| `AI_BRIDGE_WEBRTC_INPUT_RPS` | `'250'` | `src/ai_bridge/webrtc_stream.py` |

## MCP, OAuth & AI connectors

| Variable | Observed default(s) | Referenced in |
|---|---|---|
| `AI_BRIDGE_M365_ALLOW_WRITE` | `'0'` | `src/ai_bridge/connectors/microsoft365.py` |
| `AI_BRIDGE_M365_CLIENT_ID` | `''` | `src/ai_bridge/connectors/microsoft365.py` |
| `AI_BRIDGE_M365_CLIENT_SECRET` | `''` | `src/ai_bridge/connectors/microsoft365.py` |
| `AI_BRIDGE_M365_REDIRECT_URI` | `''` | `src/ai_bridge/connectors/microsoft365.py` |
| `AI_BRIDGE_M365_TENANT` | `'common'` | `src/ai_bridge/connectors/microsoft365.py` |
| `AI_BRIDGE_M365_TOKEN_KEY` | `''` | `src/ai_bridge/connectors/microsoft365.py` |
| `AI_BRIDGE_MCP_ALLOWED_HOSTS` | `''` | `src/ai_bridge/control/combined.py`, `src/ai_bridge/server.py` |
| `AI_BRIDGE_MCP_ALLOWED_ORIGINS` | `''` | `src/ai_bridge/server.py` |
| `AI_BRIDGE_MCP_AUDIT_DB` | `''` | `src/ai_bridge/audit_writer.py`, `src/ai_bridge/server.py` |
| `AI_BRIDGE_MCP_AUDIT_FILE` | `''` | `src/ai_bridge/audit_writer.py` |
| `AI_BRIDGE_MCP_HOST` | `'127.0.0.1'` | `src/ai_bridge/control/routers/admin.py`, `src/ai_bridge/server.py` |
| `AI_BRIDGE_MCP_PORT` | `'8001'` | `src/ai_bridge/server.py` |
| `AI_BRIDGE_MCP_STATEFUL_HTTP` | `''` | `src/ai_bridge/server.py` |
| `AI_BRIDGE_OAUTH_DB` | `'/etc/ai-bridge-oauth/oauth.db'` | `src/ai_bridge/control/routers/admin.py` |
| `AI_BRIDGE_OAUTH_HOST` | `'127.0.0.1'` | `src/ai_bridge/control/routers/admin.py` |
| `AI_BRIDGE_OAUTH_ISSUER` | — | `src/ai_bridge/control/combined.py` |
| `AI_BRIDGE_OPENAI_API_KEY` | — | `src/ai_bridge/control/ai_api.py` |

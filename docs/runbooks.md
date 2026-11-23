# AI Bridge Operational Runbooks & SLOs

**Updated:** 23 November 2026 · [operations.md](./operations.md) · [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md)

## Service Level Objectives (SLOs)

| Signal | Target | Prometheus Metric |
|---|---|---|
| API control-plane latency (non-long-poll) | p95 < 500ms, alert > 1.0s | `aibridge_http_request_duration_seconds` |
| Database query latency | p95 < 100ms, slow-query log > 250ms | `aibridge_db_query_duration_seconds` |
| Task claim-to-start (online device) | p95 < 2.0s (WS) / < 15s (HTTP poll) | `aibridge_task_duration_seconds` |
| Heartbeat freshness | < 90s for online devices | `aibridge_stale_heartbeat_devices` |
| PostgreSQL restore RTO | < 5 minutes (isolated drill verified) | `scripts/backup_restore.py drill` |

## End-to-End Task Tracing

Every HTTP request receives or propagates `X-Correlation-ID` (echoed in responses and injected into NDJSON logs, `tasks.trace_id`, `task_events.trace_id`, agent progress/result headers, and `audit.trace_id`).

To inspect one task's full lifecycle across dashboard/MCP submission, agent execution, approvals, and audit records:

```bash
curl -H "Authorization: Bearer $SESSION_TOKEN" \
  https://<control-plane>/api/traces/<trace_id>
```

## Alert Runbooks

### <a id="api-latency-slo"></a>API Latency SLO Breach (`AIBridgeApiLatencyHigh`)
1. Check `/health/ready` for `db_latency_ms` and `db_stats` (pool waiting / slow queries).
2. Inspect NDJSON logs filtered by `"Slow DB query detected"` or high latency endpoints.
3. Verify Redis reachability (`REDIS_URL`) so rate limiting and WS fan-out are not degraded.

### <a id="database-pool-exhaustion"></a>Database Pool Exhaustion (`AIBridgeDbPoolExhaustion`)
1. Inspect `GET /health/ready` (`db_stats.pool_size`, `pool_available`, `pool_waiting`).
2. Check PostgreSQL active sessions (`pg_stat_activity`) for blocked transactions or lock waits (`lock_timeout` defaults to 5s, `statement_timeout` to 30s).
3. Adjust `AI_BRIDGE_DB_POOL_MAX` per worker while ensuring `workers * AI_BRIDGE_DB_POOL_MAX < max_connections - 20`.
4. Verify periodic retention sweeps (`AI_BRIDGE_RETENTION_TASKS_DAYS`, `AI_BRIDGE_RETENTION_TASK_EVENTS_DAYS`) are keeping table sizes bounded.

### <a id="stale-running-tasks"></a>Stale Running Tasks (`AIBridgeStaleRunningTasks`)
1. Check `GET /api/fleet/health` to confirm whether the target agent is offline, backing off, or experiencing network loss.
2. Idempotent tasks with remaining `max_attempts` retry automatically with exponential backoff; non-idempotent tasks fail closed to `timed_out` and land in the dead-letter queue (`GET /api/tasks/dead-letter`).
3. Do NOT blindly enable `AI_BRIDGE_STALE_TASK_REQUEUE=1` for mutating workloads.

### <a id="dead-letter-queue"></a>Dead-Letter Queue Backlog (`AIBridgeDeadLetterBacklog`)
1. List unreplayed items: `GET /api/tasks/dead-letter`.
2. Inspect `failure_class` (`transient`, `timeout`, `policy`, `permanent`) and `failure_reason`.
3. After resolving the underlying host/network/policy issue, replay safely via `POST /api/tasks/dead-letter/{dlq_id}/replay` or purge obsolete items via `DELETE /api/tasks/dead-letter/{dlq_id}`.

### <a id="stale-heartbeat-devices"></a>Stale Heartbeat Devices (`AIBridgeStaleHeartbeatDevices`)
1. Inspect `GET /api/fleet/health` for `poll_state` (`ws_connected`, `backoff_attempts`, `last_error`) and `spool_stats`.
2. On the endpoint, run `ai-bridge-agent status` / `diagnose` or check the OS service (`systemctl status ai-bridge-agent`, Windows Service / Task Scheduler, or `launchctl list`).

### <a id="agent-spool-backlog"></a>Agent Spool Backlog (`AIBridgeAgentSpoolBacklog`)
1. Check `spool_stats` (`pending_count`, `oldest_pending_age_sec`, `dead_letter_count`) in `GET /api/fleet/health`.
2. Verify device token validity (expired/revoked tokens cause terminal 401 pauses) and outbound HTTPS connectivity to the control plane.
3. Once connectivity is restored, the agent automatically flushes spooled results on its next poll tick.

### <a id="audit-chain-failure"></a>Audit Chain Failure (`AIBridgeAuditChainBroken`)
1. Immediately run `GET /api/audit/verify` as an admin and note `broken_at`.
2. Trigger emergency stop (`POST /api/emergency-stop`) if unauthorized DB tampering is suspected.
3. Compare against external WORM/SIEM export (`AI_BRIDGE_WORM_SIEM_URL`) and latest verified PostgreSQL backup.

## Scheduled Backups & Quarterly Restore Drills

- Enable the systemd timer: `systemctl enable --now ai-bridge-backup.timer` (runs `scripts/backup_drill.sh` daily at 03:15 UTC).
- Quarterly restore proof: run `python scripts/backup_restore.py drill --pg-dsn "$DATABASE_URL" --restore-dsn "$DRILL_DSN" --evidence-out restore-proof.json` and archive the JSON evidence (RTO/RPO, migration count, audit hash-chain verification).

# Operations

**Updated:** 23 November 2026  
**Related:** [runbooks.md](./runbooks.md) · [production-readiness.md](./production-readiness.md) · [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md)

## Health & fleet

- Liveness / readiness: `GET /health`, `GET /health/live`, `GET /health/ready` (production returns **503** on critical readiness issues).
- Devices: `GET /api/v1/devices` or `GET /api/devices` (role-scoped).
- Smoke after deploy: `python deployment/smoke_check.py https://YOUR_HOST`.

## Logs & audit

- Host logs: `journalctl -u ai-bridge-*` (Linux) or your container/platform log drain.
- Audit: `GET /api/audit` / search-export paths; verify chain with `GET /api/audit/verify`.
- Correlation: propagate `X-Correlation-ID`; inspect `GET /api/traces/{trace_id}`.

## Emergency stop

- Dashboard control or admin `POST /api/emergency-stop`; clear with `POST /api/emergency-stop/clear`.
- State is **durable** (settings / `plane_state`) — shared across workers; does not rely on process memory alone.
- While active: new tasks rejected (**403**); agents report `emergency_stop: true`.

## Upgrades

1. Prefer tagged releases with signed manifests (see release workflow).
2. Back up Postgres / SQLite **before** migrations.
3. Deploy control plane + dashboard together; re-run smoke + a Tier-1 task.
4. Rollback: restore from a tested backup rather than assuming a git checkout alone undoes schema.

Agent updates: Ed25519-verified manifests; auto-install gated (`AI_BRIDGE_ALLOW_AUTO_UPDATE`). Use staged rollout rings when enabled.

## Backups

- Nightly: `pg_dump` (prod) or SQLite backup API (`python -m ai_bridge.control.backup …`).
- Drill: `scripts/backup_drill.sh` / `scripts/backup_restore.py` / `scripts/postgres_pitr_drill.py` into an isolated DB.
- Workspace file backups: `<workspace>/.ai-bridge-backups/` (timestamped); copy off-device.

## Rate limiting

- Auth endpoints: tighter per-IP windows; authenticated tokens have higher caps (see env reference).
- Multi-worker / multi-host: use **Redis** so limits and WS fan-out are shared. Memory backend is lab-only / readiness warning in production.
- Add proxy-level limiting and per-tenant quotas for production edges.

## Alerts

- See [runbooks.md](./runbooks.md) and `deployment/monitoring/alerts.yml` for operational alert evaluation.

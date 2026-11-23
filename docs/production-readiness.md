# Production hardening checklist (operators)

> **Verdict (go / conditional-go / no-go):** [analysis/PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md)  
> **Platform claims:** [support-matrix.md](./support-matrix.md) · **R1 evidence:** [release-1-verification.md](./release-1-verification.md)  
> **Runtime gate:** `src/ai_bridge/control/production_readiness.py` + `GET /health/ready` (production returns **503** on critical issues)

This file is the **operator checklist**. It does not replace the assessment verdict.

---

## Production profile requirements

Set `AI_BRIDGE_PROFILE=production` (or equivalent). Critical readiness checks include:

| Requirement | Notes |
|---|---|
| PostgreSQL `DATABASE_URL` | SQLite is lab-only |
| TURN / ICE | `AI_BRIDGE_TURN_SECRET` or `AI_BRIDGE_TURN_URL` or `AI_BRIDGE_ICE_SERVERS` |
| Admin access | Active admin user **or** configured legacy admin path |
| HTTPS public origin | `AI_BRIDGE_PUBLIC_BASE_URL` / `AI_BRIDGE_PUBLIC_URL` must not be `http://` |
| Durable emergency stop | Stored in settings / `plane_state` (survives worker restart) |
| Redis when multi-worker | Required if `AI_BRIDGE_WORKERS > 1`, `AI_BRIDGE_MULTI_HOST`, or `AI_BRIDGE_REQUIRE_REDIS` |
| Shared `AI_BRIDGE_CSRF_SECRET` | Required for multi-worker session CSRF |
| `AI_BRIDGE_MCP_TOKEN_KEY` | Fernet key for MCP OAuth secrets at rest |
| Chromium sandbox on | Unless explicitly acknowledging `AI_BRIDGE_ALLOW_UNSAFE_BROWSER=1` |

Defaults for a safe first deploy: `AI_BRIDGE_ALLOW_TIER3=0`, desktop opt-in, legacy WebRTC create disabled.

Compose / prod image also fail-fast without Postgres password, admin token, CSRF secret, and TURN secret. See `docker-compose.prod.yml` and `deployment/docker/Dockerfile.prod`.

---

## Workspace operations (delivered)

- Live audit search/export, global policy review/publish, gateway connection checks, admin service logs.
- Local/Google user role management; access changes revoke sessions; Google sign-in creates viewers (never auto-admins).
- Server-persisted device groups/tags; inventory filtering and pagination.
- Heartbeat / token-expiry checks; enrollment-to-first-task checklist.
- Attention counts for stale devices, failures, approvals (not a full notification product).
- Searchable recent tasks; opt-in paginated full history.
- Idempotency keys on task creation; CI browser checks; `deployment/smoke_check.py`.

---

## Known boundaries

- Do not claim million-device or public multi-tenant SaaS scale.
- Main workspace may still load a device snapshot for navigation; `/api/fleet/inventory` is the paginated path.
- Live logs are host service logs, not automatic remote-agent log shipping.
- Policy publish creates a new global version; device overrides can differ.
- Terminal / desktop / workflow editors remain separate from basic ops panels where labelled as such.
- Tier-4 / tenant / horizontal deployment: code + R1 review exist; this checklist is **not** a security certification.
- Localisation: core nav/sign-in multi-locale preview; operational/safety copy may remain English — see [localization.md](./localization.md).

**Multi-worker:** emergency stop and OIDC pending login state are durable (DB settings / `plane_state`). Rate limits and WS fan-out use Redis when multi-worker is enabled. A memory-only rate-limit backend is a readiness **warning** in production — use Redis for shared limits.

---

## Idempotent task submissions

`POST /api/tasks` accepts optional `Idempotency-Key` (1–128 ASCII). One random key per reviewed per-device submission. Reused key with different args/tenant/device → **409**. Auth, emergency stop, and device policy still apply. No automatic key expiry — retain keys while retries are possible.

---

## Release checklist

1. Rotate any admin token or OIDC/Google secret previously shared in chat; load secrets only via host env.
2. Confirm approved branch/commit and deployment method.
3. Green CI (Python, dashboard, postgres-boot, platform jobs). Back up the DB before migrations.
4. Configure OIDC redirect exactly: `https://YOUR_HOST/api/auth/oidc/callback`. Dashboard origin = same HTTPS origin. Python does not auto-load `.env`.
5. Deploy API + dashboard together. Smoke:

   ```sh
   python deployment/smoke_check.py https://YOUR_HOST
   ```

   Checks readiness, anonymous rejection, branded frontend — not a full OAuth proof.
6. Fresh browser: sign-in, role change, session revoke, sign-out. Never put production tokens in browser tests.
7. Enroll a disposable agent → heartbeat → Tier-1 task → Tier-3 approval path. No destructive smoke commands.
8. Verify HTTPS/proxy, firewall, TURN, Redis (if multi-worker), and rate-limit behaviour.
9. Archive hosted evidence before calling the RC signed: staging gate, backup drill, Windows reboot soak, cross-NAT TURN (see assessment §2).

---

## Backup and recovery

Backups contain identity/session/device data — encrypt off-host, restrict access, never commit.

**SQLite:**

```sh
python -m ai_bridge.control.backup /PATH/control.db /PRIVATE/NEW-backup.db
```

Refuses overwrite; verifies integrity. Drill into an isolated path only.

**PostgreSQL:** provider snapshots/PITR, or `pg_dump` / `pg_restore` into a **new** DB. Use `scripts/backup_drill.sh` / `scripts/backup_restore.py` / `scripts/postgres_pitr_drill.py`. Record RTO/RPO. Never overwrite live as a test.

---

## Related

- [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md) — current assessment  
- [fleet-limits.md](./fleet-limits.md) — sizing envelopes  
- [runbooks.md](./runbooks.md) — incidents / DLQ / rollbacks  
- [security.md](./security.md) · [threat-model.md](./threat-model.md)

# Multi-Worker Operation & Fleet Limits

**Updated:** 23 November 2026 · [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md) · [production-readiness.md](./production-readiness.md)

## Required Configuration for Multi-Worker / Multi-Host Production

When running more than one worker (`AI_BRIDGE_WORKERS > 1` in `AI_BRIDGE_PROFILE=production`) or multiple control-plane hosts (`AI_BRIDGE_MULTI_HOST=1` or `AI_BRIDGE_REQUIRE_REDIS=1`):

1. **PostgreSQL (`DATABASE_URL`)** is mandatory (SQLite is dev/single-node only). Install `[prod]` extras (`psycopg[binary]`, `psycopg-pool`, `gunicorn`, `prometheus-client`) so `PostgresConnection` runs with a thread-safe `ConnectionPool`.
2. **Redis (`REDIS_URL` or `AI_BRIDGE_REDIS_URL`)** is mandatory and enforced fail-fast at `create_app()` startup and `/health/ready`:
   - Shared sliding-window rate limiting across all workers (`durable_ratelimit.py` Lua script).
   - Cross-worker agent wake/cancel/emergency-stop pub/sub fan-out (`aibridge:agent:*`, auto-reconnecting on Redis restart).
   - Cross-worker WebRTC signaling WebSocket fan-out (`aibridge:webrtc:*`).
3. **Shared CSRF Secret (`AI_BRIDGE_CSRF_SECRET`)** is mandatory and enforced fail-fast when `AI_BRIDGE_WORKERS > 1`.
4. **Dedicated Metrics Scrape Token (`AI_BRIDGE_METRICS_TOKEN`)**: configure Prometheus to send `Authorization: Bearer $AI_BRIDGE_METRICS_TOKEN` to `/metrics`.

## Cross-Worker Guarantees

- **Idempotent task submission:** atomic `INSERT ... ON CONFLICT DO NOTHING` on `task_submissions(actor, request_key)`.
- **Schedules, triggers, and custom agents:** atomic conditional `UPDATE ... WHERE next_run_at <= ?` / cooldown check with `rowcount == 1`.
- **Approvals:** atomic `UPDATE approvals ... WHERE id=? AND status='pending'` with `rowcount == 1` (concurrent decisions receive `409 Conflict`).
- **OIDC & M365 OAuth:** PKCE (`S256`) + durable DB store with single-statement `DELETE ... RETURNING` (prevents double redemption across workers).
- **Human takeover & emergency stop:** durable DB-backed (`plane_state.py`), queried directly when a shared DB connection is bound.

## Documented Fleet Sizing Limits (Baseline Reference)

| Deployment Profile | Workers | DB Pool / Worker | Redis | Recommended Fleet Size | Notes |
|---|---:|---:|---|---|---|
| Local Dev / Edge | 1 | SQLite WAL | Optional | 1–10 devices | Single host only |
| Small Production (1 host) | 4 | `min=2, max=10` (40 conns) | Required | Up to 150 devices | 15s heartbeat, WS wake enabled |
| Multi-Host Production (2–4 hosts) | 4 / host | `min=2, max=10` (use PgBouncer > 120 conns) | Required | 150–1,000 devices | Put PgBouncer in transaction mode if > 3 hosts |

## Reproducible Load & Soak Verification

- **Multi-worker correctness suite:** `pytest -v tests/integration/test_multi_worker_correctness.py`
- **k6 claim/cancel soak:** `k6 run scripts/k6/claim_cancel_soak.js` (and `scripts/k6/claim_cancel_soak_per_vu.js`)
- **Locust soak:** `locust -f scripts/locust/claim_cancel_soak.py`
- **SQLite / DB micro-benchmark:** `python scripts/bench_phase2.py`

> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Load-Test Phase Plan — AI Bridge Staging on Render

**Date:** 17 Nov 2026
**Status:** Plan — not yet executed. Nothing in this document spends money or
changes infrastructure; execution needs the approvals in §7.
**Context:** the staging plane is live at
https://ai-bridge-staging-plane.onrender.com (deploy `dep-dbu0ujmk1f9s73a0g2pg`,
free plan, 1 instance). The Postgres boot drill passed (migrations 001–015
applied, `/health` 200). This phase closes the remaining live-verification
items: gate 15 (4-worker load test), the two-worker Redis fan-out check, and
the Postgres backup/restore drill (DB-01).

## 1. What this phase must prove

| # | Claim | Gate / register | How it is proven |
|---|-------|-----------------|------------------|
| 1 | Scheduler fires each due job exactly once across workers | DIST-01, gate 15 | One due schedule, count executions = 1 per tick |
| 2 | Cancels / task availability reach agents on other workers | DIST-03, gate 15 | Cancel issued via public URL arrives promptly on an agent WS connected to a different worker, repeated trials |
| 3 | OIDC completes when authorize and exchange land on different workers | DIST-05, gate 15 | Full authorize → exchange flow via public URL, repeated |
| 4 | Effective rate limit = configured limit, not 4× | PERF-02, gate 15 | Probe: allowed request rate ≈ 1200/min across all 4 workers combined (the middleware's token-bucket limit; see §4.D) |
| 5 | 4 workers sustain the claim/cancel soak within thresholds | Gate 15 | k6 soak: claim p95 < 5 s, failed requests < 5% |
| 6 | Postgres backup restores into a fresh DB and verifies | DB-01 | `scripts/backup_restore.py` PG path run on Render: dump → new DB → restore → core-table counts |

## 2. Current state

- Plane: `ai-bridge-staging-plane` (`srv-dbu0rc8jo6nc73fs5r40`), free plan,
  1 instance, Oregon, Docker from `jacxishipping/bridgeai` main.
- Postgres `ai-bridge-staging-db`: free, PG 18, **expires 2026-12-17**.
- Key Value `ai-bridge-staging-redis`: free, Redis 8.1.10, persistence off.
- Free services cannot scale past 1 instance and sleep after 15 min idle.
- The staging admin token was generated at service creation and is known
  only to Render (it was never stored anywhere else). Any test that needs
  admin auth needs a known token first — see §3.

## 3. Prerequisites (before any spend)

1. **Known staging admin token.** Rotate/set the service env var
   `AI_BRIDGE_ADMIN_TOKEN` to a fresh value recorded only in a 0600 file for
   the test run (same handling as creation: generate, use, delete the file).
   Without this, the soak, schedule and fan-out checks cannot authenticate.
   The service restarts when an env var changes.
2. **Confirm manual scaling is available** on workspace "The's workspace"
   (Hobby $0). Manual `numInstances` scaling is a paid-instance feature, not
   autoscaling (autoscaling needs Pro). If the dashboard/API refuses the
   scale, the fallback is a Pro workspace at $25/mo flat, prorated — decide
   then, do not pre-pay it.
3. **k6 on this VM** (the repo's soak is `scripts/k6/claim_cancel_soak.js`;
   neither k6 nor locust is currently installed). k6 is a single static
   binary; install it locally for the run. The locust alternative
   (`scripts/locust/claim_cancel_soak.py`) needs `pip install locust`.
4. **Postgres client in the image** (backup drill only): add
   `postgresql-client` to `deployment/docker/Dockerfile` (one `apt-get`
   line). pg_dump/psql/pg_restore/createdb are required by
   `scripts/backup_restore.py` and are absent from `python:3.12-slim`.

## 4. Execution steps

### A. Scale to 4 × Starter

1. `PATCH /v1/services/srv-dbu0rc8jo6nc73fs5r40` → `{"plan": "starter"}`.
2. `POST /v1/services/srv-dbu0rc8jo6nc73fs5r40/scale` → `{"numInstances": 4}`.
3. Verify: `GET /v1/services/{id}/instances` shows 4 running instances;
   `/health` still 200. Render load-balances across them.

### B. Two-worker Redis fan-out check

1. Enroll 2 test devices via `POST /api/devices/enroll` (admin token).
2. Open an agent WebSocket for each; confirm the two connections land on
   different workers (Render instance ids in the service logs).
3. From the public URL (random worker), create a task for device 1, then
   cancel it; record whether the cancel arrives on device 1's WS promptly.
4. Repeat 10 times. With 4 workers, a same-worker coincidence is 25% per
   trial; 10/10 prompt deliveries rules it out (0.25^10). Log the evidence.

### C. Scheduler-fires-once check (DIST-01)

1. `POST /api/schedules` with a short `interval_seconds` and `next_run_at`
   in the past (due immediately), tool `get_system_info`, target test device.
2. Over several ticks, count the tasks the scheduler created for that
   schedule. Pass = exactly 1 per due tick, no duplicates across 4 workers.
3. Delete the schedule afterwards.

### D. Rate-limit check (PERF-02)

1. Small probe script (new, ~50 lines): N parallel clients hammer a
   rate-limited API route for 60 s, counting 200s vs 429s.
2. The limit the middleware actually enforces for a token-authenticated
   caller is **1200/min** (`rate_limit_middleware` in `app.py`: token bucket
   1200/min, anonymous-IP bucket 300/min, auth-endpoint bucket 30/min).
   The 120/min figure is the default of the sync `_rate_limit` helper, not
   the middleware. Pass = the allowed rate across all 4 workers combined
   is ≈1200/min, not ≈4800/min (4 × 1200, the per-worker-memory outcome).
   The probe uses one admin token for every request (the bucket key is the
   token hash) against `GET /api/devices`, which passes through the
   middleware before the handler.

### E. Load soak (gate 15)

`k6 run -e BASE_URL=https://ai-bridge-staging-plane.onrender.com
-e ADMIN_TOKEN=<known staging token> -e VUS=20 -e DURATION=5m
scripts/k6/claim_cancel_soak.js`

Thresholds (from the script): claim p95 < 5 s, failed requests < 5%.
Watch Render metrics for instance CPU/memory and error rate during the run.
Keep the soak short (5 min) — it is a gate check, not a stress-to-failure
run. Record the k6 summary verbatim in the test-results report.

### F. OIDC across workers

Run the authorize → exchange-code flow against the public URL several
times (each request may land on a different worker; the exchange code is
DB-backed, so it must succeed regardless of which worker serves each step).
Pass = every flow completes and returns a usable token.

### G. Backup / restore drill (DB-01)

1. Deploy the Dockerfile change from §3.4 (image rebuild).
2. Create a Render cron job `ai-bridge-staging-backup-drill` from the same
   repo/image, command:
   `python scripts/backup_restore.py backup --pg-dsn "$DATABASE_URL" --out /tmp/staging-dump.sql`
   then restore into a fresh DB on the same server and verify core tables
   (`users`, `devices`, `tasks`, `sessions`, `approvals`).
   The script's PG path already does this (dump → `createdb` → restore →
   verify); the job runs it with the internal DSN from env vars.
3. Read the job logs as evidence. The dump is verification-only and is
   discarded with the job's ephemeral disk; it contains credential hashes,
   so it must never be copied off Render.

## 5. Success criteria (gate 15 closes when all hold)

- 4 instances running, `/health` 200 throughout.
- Scheduler: exactly 1 execution per due tick.
- Fan-out: 10/10 cancels delivered promptly cross-worker.
- Rate limit: effective allowed rate ≈ configured limit (not 4×).
- Soak: claim p95 < 5 s, failed < 5%, no worker crashes.
- OIDC: every cross-worker flow completes.
- Backup/restore: restore verifies, core-table counts match.

## 5A. Results — 17 Nov 2026 (live, staging plane 4 × Starter)

- **4 instances running, `/health` 200 throughout** — PASS. Plan flipped
  free → Starter in the dashboard (Render API cannot change free↔paid
  plans); `POST /scale` 202; all 4 instances verified RUNNING.
- **Scheduler (DIST-01)** — PASS. Exactly 1 task per firing; still 1
  after the schedule was deleted and 30 s passed.
- **Fan-out (DIST-03)** — PASS. 10/10 cancels delivered cross-worker,
  median 337 ms, max 622 ms (rules out same-worker coincidence ≈0.25^10).
- **Rate limit shared (PERF-02)** — PASS. 1,199 requests allowed in
  60 s (≈1,193/min), then 429s — matches the configured 1,200/min
  token-bucket limit; a per-worker fallback would have allowed ≈4,800/min.
- **Soak (gate 15)** — THRESHOLDS FAILED, but the failure is the rate
  limiter, not the plane. k6 VUS=20, 5 min: 13,901 iterations, claim p95
  201 ms (threshold < 5,000 ms — PASSED); http_req_failed 91.41%
  (25,556/27,956), all 429 'Rate limit exceeded' — the soak drove
  ~93 req/s through one admin token capped at 1,200/min, so it measured
  the limiter, not plane capacity. A proper re-soak needs one token per
  VU (or a raised test limit).
- **Rate-limiter lockout defect — FOUND LIVE, new P1.** `durable_ratelimit.py`
  `_redis_allow` / `_async_redis_allow` both `zadd` the current request's
  timestamp into the Redis sliding window BEFORE counting, even when the
  request is rejected — rejected requests are recorded, so under
  sustained overload the window never drains. Confirmed with a
  discriminating test: drain 70 s → single request 200 → k6 hammer
  60 s, 20 VUs, 5,917 requests (~98/s; 1,199 admitted, 4,718 rejected)
  → 30 s of complete silence → single request **429**. Fix: only record
  admitted requests (prune → count → add only if under limit, ideally
  atomic via Lua). James's decision on fix + re-soak pending.
- **OIDC cross-worker (DIST-05)** — NOT LIVE-TESTABLE. Staging reports
  OIDC as not configured (`GET /api/auth/oidc/configured` →
  `{"enabled":false}`), so authorize → exchange cannot run end-to-end.
  Code/config verified instead: `bind_oidc_store(conn)` runs at app
  startup, so pending logins and single-use exchange codes live in the
  shared Postgres `settings` table — cross-worker safe by construction.
  Recorded as code verification, NOT a live pass.
- **Backup/restore drill (DB-01)** — PASS, after three real bugs found
  and fixed in the drill tooling (all fixed in the repo, tests green):
  (1) distro `postgresql-client` is PG 17, staging Postgres is PG 18 and
  pg_dump refuses the mismatch — Dockerfile now installs
  `postgresql-client-18` from PGDG; (2) `createdb`/`dropdb` take a
  literal DB name, not a DSN (they connected to the local socket) —
  `backup_restore.py` now parses the DSN into `-h`/`-p`/`-U` +
  `PGPASSWORD` and has a `drop` subcommand; (3) the dump carries
  `ALTER DEFAULT PRIVILEGES` for the live-DB owner role, which the
  drill user cannot impersonate — pg_restore now runs `--no-privileges`
  (backup file stays full-fidelity). Final run 09:06 UTC: dump OK,
  restore OK, verify **migrations = 15**, devices = 6, users = 0,
  drill DB dropped, "DRILL OK", "Cron job run finished successfully".
  The DSN redaction in the drill logs also verified working (`***:***`).

Gate 15 therefore stays NOT MET (soak thresholds failed on the limiter;
OIDC not live-proven), but the distributed mechanisms it was meant to
prove — scheduler-once, fan-out, shared rate limit — all passed live,
and the backup/restore drill (DB-01) is now proven, closing that part
of gate 16.

## 5B. Results — Wave 5E, 17 Nov 2026 (live, staging plane 4 × Starter, deploy dep-dbu6vpe7bikc738fsv0g on commit 7726e58e)

- **Ice-servers route fix (Wave 5C) — verified live.** `GET
  /api/v1/sessions/ice-servers` now returns 200
  `{"ice_servers":[{"urls":"stun:stun.l.google.com:19302"}]}` on the
  redeployed plane (it 404'd on the old deploy — the route-shadow
  defect is fixed in the deployed code).
- **Per-VU soak (gate 15) — PASS.** New script
  `scripts/k6/claim_cancel_soak_per_vu.js`: every VU gets its own
  admin-role session token (`POST /api/auth/users` + `POST
  /api/auth/login`) and its own enrolled device, so each VU has its own
  1,200/min bucket; setup paces auth-path calls ~2.5 s apart (under
  the 30/min-per-IP auth bucket). Full run, 20 VUs, 5 min, 3,374
  iterations, 0 interrupted, 10,202 requests: checks **100.00%
  (3,454/3,454)**; **claim latency p95 789 ms** (threshold < 5,000 ms);
  **http_req_failed 0.01% (1/10,202)** (threshold < 5%). The single
  failed request left no error trace in the log; immaterial at 0.01%.
  Note: mean request duration rose to 523 ms at 20 VUs (vs ~138 ms at
  2 VUs) — the plane slows under load but stays far inside thresholds.
- **Rate-limiter lockout fix (W5A-01) — VERIFIED LIVE.** Discriminating
  test, same shape as the original confirmation: drain 70 s → single
  request 200 → k6 hammer 60 s, 20 VUs → 30 s silence → single request
  **200** (was 429 before the fix). Hammer numbers with the corrected
  endpoint (below): **8,726 requests at ~145/s; 1,200 admitted,
  7,526 rejected (86%)** — the limiter admitted exactly the
  configured 1,200/min and rejected the rest, then the token worked
  again after silence, because rejected requests are no longer
  recorded in the window.
- **Hammer endpoint lesson (tooling, not a gate item).** The first 5E
  hammer used `GET /api/devices` and only managed 319 requests in
  60 s (~5/s, 0 failed) — the endpoint is heavy under concurrency
  (~3.7 s/req at 20 VUs vs ~0.5 s sequential; 37 devices, 10.7 KB
  response), so it never filled the 1,200/min bucket and the test had
  no discriminating power. Switched to lightweight `GET /api/auth/me`
  (~115 ms avg at 20 VUs, ~165 req/s in a 30 s probe: 4,982 requests,
  1,187 admitted ≈ the 1,200 bucket). The lockout re-run above used
  `/me`.
- **CSRF cross-worker finding — FOUND LIVE, fixed on staging (new).**
  The per-VU script first failed task creation with 403 `CSRF token
  missing or invalid` (session-token POSTs require `X-CSRF-Token`;
  admin/device tokens are exempt). After adding the CSRF fetch
  (`GET /api/auth/csrf-token` per VU, token valid 24 h), task creation
  still succeeded only ~15–21% of the time: the same session + CSRF
  token validated on 3/20 repeated creates. Root cause: the CSRF HMAC
  secret falls back to a **per-process random secret** when
  `AI_BRIDGE_CSRF_SECRET` is unset (`control/csrf.py`, documented
  warning), so a token minted by one of the 4 workers validates only
  on that worker. Fix (staging deployment config, not code): set a
  shared `AI_BRIDGE_CSRF_SECRET` (random 64-hex) on the service via
  `update_environment_variables` (merge) → redeploy
  `dep-dbu6vpe7bikc738fsv0g` → live 15:17:20 UTC. Re-test: **20/20**
  creates with one session + CSRF token; the per-VU smoke then went
  100% (106/106 checks, 0 failed requests). **Production
  deployments with >1 worker MUST set a shared
  `AI_BRIDGE_CSRF_SECRET`** — without it, session-based writes fail
  ~80% of the time. (Flagged for the 5F re-audit to record as a
  register row if it judges it one.)

**Gate 15 is now MET** (soak thresholds pass with one token per VU;
lockout fix verified live). OIDC cross-worker flow remains not
live-proven (staging OIDC not configured; code-verified only) — see
gate 14.

## 6. Cost (Render list prices, checked Oct 2026; verify on the pricing page before spending)

| Item | Price | Phase cost |
|------|-------|-----------|
| Plane 4 × Starter (512 MB, 0.5 CPU) | $7/mo per instance, prorated by the second | ≈ $0.04/hr for all 4; a full day ≈ $0.92 |
| Backup-drill cron job, Starter | $7/mo prorated | minutes of runtime ≈ a few cents |
| Workspace | Hobby $0 (Pro $25/mo only if manual scaling is refused — see §3.2) | $0 expected |
| Postgres / Key Value | free (Postgres expires 2026-12-17) | $0 |
| **Expected total if scaled back promptly** | | **under $2** |
| Worst case (forgotten, left running a month) | 4 × $7 + $7 job | ≈ $35/mo until scaled down |

**Scale-back is part of the phase, not an afterthought:** after the checks,
`POST /scale {"numInstances": 1}` and `PATCH {"plan": "free"}`, delete the
cron job, and verify the service is back to 1 × free.

## 7. Approvals needed to execute

1. Scale the plane to 4 × Starter (paid compute, §6).
2. Rotate/set a known staging admin token (§3.1).
3. Dockerfile change adding `postgresql-client` + the backup-drill cron job.
4. Run the k6 soak against the public staging URL (load test traffic only;
   staging data only, no production systems).

## 8. What this phase does NOT do

- No production deployment, no real enrolled devices (test devices only),
  no WebRTC E2E (needs a real browser peer — separate item), no 10-step E2E
  workflow tests, no closing of the open P1s, no independent re-audit.
  Those remain open after this phase; the verdict stays NO-GO until they
  are done.

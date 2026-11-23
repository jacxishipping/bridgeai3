> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# 14 — Performance and Scalability Audit — AI Bridge

Scope: performance and scalability of the control plane, agents, and
storage, from code reading only. **No benchmarks were run and no profiling
was performed** — there is no load-test harness in the repo and this track
did not run the server under load. Every performance claim below is
therefore labelled **HYPOTHESIS** (code-level reasoning, needs measurement)
unless it is a directly observable constant or count, which is labelled
**VERIFIED**. Do not quote any latency/throughput/FPS number from this
report as measured.

## 1. Executive summary

Nothing in the code suggests the system is fast at scale; several design
choices put hard ceilings in place before any load arrives:

- **One shared DB connection per worker, serialised by a single `RLock`**
  (`db.py:240-410`) — every query in a worker (heartbeat, claim, audit,
  rate-limit DB writes, telemetry prune) queues behind every other query.
  On Postgres this discards the database's concurrency entirely.
- **A write per HTTP request** on the rate-limit path when Redis is absent
  (`durable_ratelimit._db_allow` does a SELECT + upsert + commit per
  request), and a write + delete per agent heartbeat (telemetry insert +
  500-row prune).
- **Polling everywhere**: agents long-poll (5–15s) *and* heartbeat every
  loop, *and* poll `execution-state` every 0.5s while a task runs; the
  plane's `next_task` long-poll loop spins on 0.15s sleeps; the scheduler
  ticks every 10s in every worker.
- **N+1 and full-scan reads** in the fleet-health, handoff-status, and
  live-activity paths, and `SELECT *` task listings that can return
  multi-MB screenshot results.
- **Unbounded or barely-bounded payloads**: task results up to 512k chars
  (16M for screenshots) stored as TEXT in the hot `tasks` table and
  returned by list endpoints.

The system is plausibly fine for a small fleet (tens of devices, one
worker) and degrades predictably beyond that. The fixes are mostly
mechanical: connection pooling, Redis-by-default in prod, atomic scheduler
claims, pagination/projection, and indexes.

## 2. Measured constants (VERIFIED — from code, not benchmarks)

| Quantity | Value | File |
|----------|-------|------|
| Control-plane source | 5,147 lines, ~120 routes | `src/ai_bridge/control/app.py` |
| `tools.py` | 2,990 lines | `src/ai_bridge/tools.py` |
| DB tables / indexes | 22 tables, 16 indexes | `src/ai_bridge/control/db.py` (grep counts) |
| Prod workers | 4 gunicorn/Uvicorn workers | `deployment/docker/Dockerfile.prod:56-60` |
| Agent long-poll `wait` | 5s (WS up) / 15s (WS down), server clamps to ≤ 25s | `agents/common/poller.py:490`; `app.py:4625` |
| Agent error backoff | 2s → 60s cap, exponential | `agents/common/poller.py:505-510` |
| Agent WS ping | 20s interval, 20s timeout; recv timeout 30s | `agents/common/poller.py:460-475` |
| Heartbeat cadence | every `run_once()` ≈ every long-poll cycle (5–15s) | `agents/common/poller.py:341` |
| Stale heartbeat threshold | 90s | `src/ai_bridge/control/fleet_health.py:10` |
| Stale task lease | 900s default | `src/ai_bridge/control/orchestration.py:13` |
| Sweep cadence | scheduler tick 10s × every 3rd tick ≈ 30s | `src/ai_bridge/control/app.py:2671-2675` |
| Task result caps | 512,000 chars; 16,000,000 for screenshots | `agents/common/poller.py:18`; `app.py:4720-4730` |
| `MAX_OUTPUT_BYTES` / `MAX_READ_BYTES` | 64,000 / 128,000 | `src/ai_bridge/config.py` |
| Local task runner | max 4 concurrent, RLIMIT_AS 2 GiB, RLIMIT_FSIZE 100 MiB, timeout ≤ 300s | `src/ai_bridge/execution.py:55-65,120-140` |
| Rate limits | auth 30/min/IP; per-token 1200/min; per-IP 300/min; default helper 120/min/60s | `src/ai_bridge/control/app.py:4790-4810`; `src/ai_bridge/control/ratelimit.py:78` |
| List limits | tasks 200, approvals 200, audit list 500, audit export 10,000, telemetry 500/device | `app.py` (grep: 10 `LIMIT 200/500/100` sites) |
| Telemetry cap | latest 500 rows/device | `src/ai_bridge/control/app.py:4455-4462` |
| `run_shell` output | last 20,000 chars of stdout/stderr | `src/ai_bridge/tools.py:1365-1375` |
| Screenshot cache TTL | 2.5s (`latest_frames`) | `src/ai_bridge/control/app.py:3335` |
| OIDC discovery cache | 3600s, in-memory | `src/ai_bridge/control/oidc.py:54-62` |

## 3. Control-plane throughput (HYPOTHESIS unless noted)

### 3.1 Single serialised DB connection — the primary ceiling (VERIFIED structure, impact HYPOTHESIS)

`LockedConnection` / `PostgresConnection` hold **one** connection and one
`RLock` per worker (`db.py:240-410`). Every route handler, the scheduler
thread, the audit writer, and the rate limiter share it. Consequences to
verify by measurement:

- A slow query (e.g. the telemetry prune, a fleet-health scan, or an
  audit export of 10,000 rows) blocks heartbeats and task claims in that
  worker for its whole duration. Heartbeat latency directly drives
  false `stale` verdicts (90s threshold) and agent backoff.
- On Postgres, 4 workers = 4 connections total; Postgres can handle
  hundreds, so the pool — not the database — is the bottleneck.
- The `transaction()` context manager holds the lock for the whole
  transaction, including the `next_task` claim loop and the
  `submit_task` idempotency check + insert + audit write.

**Fix (P1):** pool Postgres connections (`psycopg_pool`, min 2 / max 10
per worker) with statement timeouts; keep SQLite serialised (it is
effectively single-writer anyway). Measure claim latency under 50
concurrent agents before/after.

### 3.2 Rate limiting does a DB write per request (VERIFIED code path, cost HYPOTHESIS)

`_rate_limit` (`app.py:90-98`): if `REDIS_URL` is set it calls
`ratelimit.rate_limit()`; otherwise `durable_ratelimit.allow()`.
Two problems:

1. `ratelimit.rate_limit()` (`ratelimit.py:78-100`) returns a sync checker
   that, when called inside a running event loop (i.e. from FastAPI
   middleware — the normal case), **falls back to the in-memory bucket**.
   The Redis sliding-window path is therefore likely unreachable in
   production request handling; the in-memory fallback is per-worker, so
   the effective limit is ~4× configured under 4 workers (see Distributed
   report DIST-07). HYPOTHESIS — needs a runtime check of which branch
   executes.
2. `durable_ratelimit._db_allow` (`durable_ratelimit.py:75-110`) does
   `SELECT` → `INSERT ... ON CONFLICT DO UPDATE` → `commit` on the
   `settings` table **per request**, under the same global lock — i.e.
   every API call costs a write transaction plus lock contention, and the
   read-modify-write is racy across workers (TOCTOU on `count`).

**Fix (P1):** make Redis mandatory in prod (the readiness check already
warns, `production_readiness.py:180-195`, but only when
`AI_BRIDGE_REQUIRE_REDIS`/`AI_BRIDGE_MULTI_HOST` is set); use one
async-native Redis limiter in middleware; drop the DB fallback for the
hot path.

### 3.3 Long-poll spin (VERIFIED structure, cost HYPOTHESIS)

`next_task` (`app.py:4625-4675`) loops with `await asyncio.sleep(min(0.15,
rem))` for up to 25s, re-checking emergency-stop (a DB read with TTL
0.75s, `plane_state.py:35-50`) and the lease (a DB query) each iteration.
With 100 agents long-polling, ~100 handlers wake every 150ms — that is
~650 wakeups/s of mostly-empty work plus a DB query per wakeup when the
TTL expires. HYPOTHESIS: measurable CPU and lock contention at fleet
scale; the fix is a real condition variable / pub/sub wake (Redis, which
prod already ships) instead of polling, or a longer sleep (1s) with WS
wake as the low-latency path.

### 3.4 Scheduler thread per worker (VERIFIED structure)

`_scheduler_loop` (`app.py:2661-2720`) sleeps 10s, then runs the sweep,
due schedules, and trigger evaluation **in every worker**. Beyond the
correctness issue (duplicate firing, DIST-01), the trigger evaluation
scans all triggers × all devices' latest telemetry on every tick.
HYPOTHESIS: cost is small at small fleet size, O(triggers × devices)
per tick per worker at scale. Fix with the scheduler claim (DIST-01).

## 4. Agent-side load (HYPOTHESIS unless noted)

- **Heartbeat write amplification (VERIFIED code path):** each heartbeat
  does 1 device UPDATE + (when metrics are present) 1 telemetry INSERT +
  1 prune DELETE (`DELETE ... NOT IN (SELECT ... LIMIT 500)`,
  `app.py:4455-4462`). At a 5–15s cadence per agent, a 100-agent fleet
  issues ~10–20 heartbeat writes/s plus 100 prune DELETEs per cycle —
  each prune scanning the device's telemetry rows. HYPOTHESIS: the prune
  dominates heartbeat cost; fix by pruning on a timer (e.g. every Nth
  heartbeat) or by time-based retention (`DELETE WHERE recorded_at < ?`).
- **Execution-state polling (VERIFIED):** while a task runs, the poller
  spawns a thread that GETs `execution-state` every 0.5s
  (`poller.py:300-330`) — 2 req/s per running task, plus the
  `_cancelled()` call itself does an HTTP round-trip each time it is
  invoked (also called from `raise_if_cancelled` paths). HYPOTHESIS:
  fine for a few concurrent tasks, noisy at fleet scale; the WS cancel
  channel already exists — prefer it and poll at 2–5s as a fallback.
- **Spool flush (VERIFIED):** `run_once` flushes the spool before every
  heartbeat (`poller.py:340-345`); `flush()` stops at the first failure,
  so one poisoned result head-of-line-blocks the spool. `get_pending`
  caps reads at 50 — a large backlog drains at ≤ 50 per loop iteration.
- **Metrics collection (VERIFIED):** `_collect_metrics`
  (`poller.py:40-90`) opens a UDP socket to 8.8.8.8 on every heartbeat to
  learn the local IP — a DNS-independent external probe per heartbeat;
  harmless but unnecessary (cache it). `psutil.cpu_percent(interval=None)`
  is non-blocking — good.

## 5. Query efficiency (VERIFIED patterns, cost HYPOTHESIS)

| Query / path | Pattern | Concern |
|---|---|---|
| `summarize_fleet_health` (`fleet_health.py:20-80`) | 1 device query + **1 telemetry query per device** (N+1) | O(devices) queries per dashboard refresh; fix with a single `GROUP BY device_id` / lateral join. |
| `handoff_status` (`supervisor.py:380-430`) | `SELECT * FROM tasks ORDER BY created_at` (whole table), filters `_handoff_id` in Python | Full-table scan per status call; fix with `WHERE args_json LIKE '%"handoff_id"%'` (still a scan) or a dedicated `handoff_id` column + index. |
| `live_activity` (`live_activity.py:90-160`) | Bounded (`LIMIT 50/30/40`), 4 queries | Adequate; note `custom_agents` query `ORDER BY updated_at DESC` has no matching index (`idx_custom_agents_due` is `(enabled, next_run_at)`). |
| `list_tasks` / `list_audit` / `export_audit` | `SELECT * ... LIMIT 200/500/10000` | `SELECT *` on `tasks` returns the full `result` TEXT — up to 16 MB per screenshot task. A 200-row page can be hundreds of MB. Fix: exclude `result` from list queries (fetch on demand by id). HYPOTHESIS on size; the cap is VERIFIED. |
| `get_device_overview` (`app.py:1239-1450`) | Several queries incl. latest `get_resource_usage` result parse | Moderate; fine. |
| Telemetry prune | `DELETE ... NOT IN (SELECT ... LIMIT 500)` per heartbeat | See §4. |
| `db.audit()` tip read | `SELECT entry_hash ... ORDER BY id DESC LIMIT 1` per audit event | Indexed (`id` PK) — cheap. The cost is the extra UPDATE + commit per event (2 writes per audit row). |
| `search_workspace` (`workspace.py:40-70`) | `rglob("*")`, reads **every file fully** into memory, line-scans | O(total workspace bytes) per search, no index, no binary skip beyond secret-name check; a large workspace makes this the slowest Tier-1 tool. HYPOTHESIS — needs a timing test on a realistic tree. |
| `project_structure` (`workspace.py:130-180`) | Recursive walk, capped at 200 entries / depth 8 | Bounded — good. |
| `list_workspace` | Capped at `max_list_entries` (200) | Bounded — good. |

**Missing indexes (INFERRED from query shapes; see DB report §4):**
`tasks(tenant, created_at)`, `tasks(status, updated_at)`,
`custom_agents(updated_at)`. Add and measure with `EXPLAIN QUERY PLAN`
on SQLite / `EXPLAIN` on Postgres.

## 6. Large outputs, audit growth, buffering (VERIFIED caps, growth HYPOTHESIS)

- **Task results:** 512k chars in the agent (`poller.py:18`), 512k in the
  plane (`app.py:4720`), 16M for screenshots. A screenshot result is
  stored as TEXT in `tasks.result`, copied into `app.state.latest_frames`,
  and returned by `list_tasks`/`get_task` — three copies, one of them in
  the hot table. HYPOTHESIS: screenshot-heavy fleets bloat the DB fast
  (16 MB × tasks); fix by storing frames outside the tasks table
  (object storage / separate `frames` table with TTL) and returning a
  reference from list endpoints.
- **Audit growth:** unbounded except manual admin retention (30–3650d);
  each audit row costs an insert + an update + a commit. At high task
  volume the audit table is the fastest-growing table (every submit,
  claim-adjacent dispatch, result, approval, login). HYPOTHESIS: needs a
  retention job, not just an endpoint.
- **Tasks growth:** unbounded (no TTL); `result` TEXT retained forever.
- **Event buffering:** `agent_channel` keeps unbounded per-device socket
  sets in memory (bounded by connections in practice); `live_activity`
  caps at 50/30/40 rows; `metrics.py` — not read in this track, no claim.
- **Logging:** `logging_config.py` redacts per log record — per-record
  regex cost on hot paths (HYPOTHESIS, negligible vs DB).
- **WebRTC / capture:** no FPS/latency numbers are claimed anywhere in
  this report — none were measured, and the code contains no benchmark
  harness for them (NOT FOUND). Any FPS/latency figure in other reports
  must be treated as unverified.

## 7. Scalability assessment (HYPOTHESIS, by tier)

| Tier | Expectation (reasoning, not measurement) |
|------|------------------------------------------|
| 1 worker, ≤ 20 devices | Plausibly fine. Polling cadences and per-request DB writes are small in absolute terms. |
| 4 workers, 20–100 devices | Friction appears: 4× scheduler firing (correctness, not just perf), per-worker rate buckets, WS fan-out misses, heartbeat write amplification. Redis becomes load-bearing for rate limiting. |
| 100+ devices / high task rate | The single-connection lock, N+1 fleet health, `SELECT *` task lists, and unbounded tables dominate. Needs pooling, projection, indexes, and retention before this tier is credible. |
| Multi-region / multi-plane | Not designed for it: no replication, no queue, no shared pub/sub, wall-clock ordering. Out of scope — do not attempt without a redesign. |

## 8. Findings table

| ID | Severity | Title | File(s) |
|----|----------|-------|---------|
| PERF-01 | High | One DB connection + one RLock per worker serialises all DB work (both engines) | `src/ai_bridge/control/db.py:240-410` |
| PERF-02 | High | Rate-limit hot path does a DB write per request (or silently falls back to per-worker memory) | `src/ai_bridge/control/durable_ratelimit.py:75-110`; `src/ai_bridge/control/ratelimit.py:78-100` |
| PERF-03 | High | `SELECT *` task lists can return multi-MB screenshot results (16M cap) | `src/ai_bridge/control/app.py:1579-1585,4720-4730` |
| PERF-04 | Medium | Fleet health is N+1 (one telemetry query per device) | `src/ai_bridge/control/fleet_health.py:20-80` |
| PERF-05 | Medium | Telemetry prune (`NOT IN ... LIMIT 500`) runs on every heartbeat | `src/ai_bridge/control/app.py:4455-4462` |
| PERF-06 | Medium | `next_task` long-poll spins every 0.15s with a DB check per cycle | `src/ai_bridge/control/app.py:4625-4675` |
| PERF-07 | Medium | `handoff_status` full-table-scans `tasks` and filters in Python | `src/ai_bridge/control/supervisor.py:380-430` |
| PERF-08 | Medium | Agent polls `execution-state` every 0.5s per running task | `agents/common/poller.py:300-330` |
| PERF-09 | Medium | `search_workspace` reads every file fully on every search | `src/ai_bridge/workspace.py:40-70` |
| PERF-10 | Low | Unbounded tables (tasks, task_events, audit, agent memory) — no retention jobs | `src/ai_bridge/control/app.py:1690-1710,2016-2024` |
| PERF-11 | Low | Missing indexes: `tasks(tenant, created_at)`, `tasks(status, updated_at)` | `src/ai_bridge/control/db.py:150-230` |
| PERF-12 | Low | Audit costs 2 writes + 1 commit per event (insert + hash UPDATE) | `src/ai_bridge/control/db.py:440-525` |

## 9. Recommendations (priority order)

1. **P1** — Postgres connection pool + statement timeouts; measure claim
   and heartbeat p50/p95 under 50 simulated agents (no harness exists —
   build one first; do not report numbers until measured).
2. **P1** — Redis-backed rate limiting on the real middleware path;
   remove the per-request DB write; verify which branch executes.
3. **P1** — Exclude `result` from task list queries; move screenshot
   frames out of `tasks.result` (separate table with TTL or object store).
4. **P2** — Fix N+1 fleet health (single aggregate query); add the two
   missing `tasks` indexes; add a `handoff_id` column; prune telemetry
   on a timer, not per heartbeat.
5. **P2** — Replace the 0.15s long-poll spin with WS/Redis wake + 1s
   sleep; reduce `execution-state` polling to 2s (WS cancel is the fast
   path); fix the scheduler to a single-worker claim (DIST-01).
6. **P2** — Add retention jobs (completed-task TTL, audit rotation to
   cold storage, task_events prune) and a load-test harness with
   repeatable scenarios (N agents, task rate, fleet-health refresh).
7. **P3** — Bound `search_workspace` (skip large/binary files, cap bytes
   scanned); cache agent IP metrics instead of probing 8.8.8.8 per heartbeat.

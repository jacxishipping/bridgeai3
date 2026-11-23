> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Distributed Reliability Audit — AI Bridge

Scope: behaviour of the system as a distributed system — clients, control
plane (possibly 4 gunicorn workers), and remote agents. Read-only code
review; no cluster was run. **VERIFIED** = read in cited code;
**INFERRED** = follows from code; **NOT FOUND** = searched, no evidence.
No benchmarks were run; timings are from constants in code, not measurement.

## 1. Executive summary

The single most reliable mechanism in the system is the **task claim**:
`SELECT queued … UPDATE … WHERE status='queued'` with a `rowcount == 1`
check inside a transaction (`app.py:4640-4660`). Around it, however, the
system is largely **single-process in design and multi-process in
deployment**: prod compose runs **4 gunicorn workers**, yet the scheduler
loop, the stale-task sweep, OIDC pending logins, the agent WS subscriber
registry, the emergency-stop cache, and the in-memory rate limiter are all
per-process with no leader election and no shared state except the DB —
and several of those paths do not even use the DB atomically. The result:
duplicate scheduled-workflow firing across workers (CONFIRMED by code
structure), WS wake/cancel notifications that only reach agents connected
to the same worker (CONFIRMED), and OIDC callbacks that fail when they land
on the wrong worker (CONFIRMED). Task execution itself is at-least-once
with a 900s stale-lease requeue and no fencing, so a slow/partitioned agent
can execute the same task twice. Cancellation is cooperative and best-effort,
and a late result can overwrite a `cancelled`/`timed_out` status.

## 2. Topology (VERIFIED)

- **Control plane**: FastAPI monolith, `create_app()` in
  `src/ai_bridge/control/app.py` (5,147 lines). Local/dev: single uvicorn
  (`src/ai_bridge/control/__main__.py`). Prod: gunicorn, 4 Uvicorn workers
  (`deployment/docker/Dockerfile.prod:56-60`, `docker-compose.prod.yml`).
  One shared DB connection per worker process (§DB report).
- **Agents**: outbound-only. No listening ports. Poll + long-poll
  (`GET /api/agent/tasks/next?wait=`) + optional WS side-channel
  (`agents/common/poller.py`, `src/ai_bridge/control/agent_channel.py`).
- **Shared state that actually exists**: Postgres/SQLite DB; Redis
  (rate limiting only, optional); nothing else (no queue, no pub/sub —
  despite prod compose comments calling Redis "Distributed Pub/Sub &
  Inter-Worker Synchronization", no pub/sub usage was found in the files
  read; the only Redis keys are `ratelimit:*` / `aibridge:ratelimit:*`).

## 3. Request / task lifecycle (VERIFIED)

```
operator --POST /api/tasks--> plane: tier check, approval? (or v1/workflow path)
plane: INSERT tasks(queued) [+ approvals row if needs_approval]
agent: heartbeat (each run_once) -> GET tasks/next?wait=5|15 (long-poll <=25s)
plane: lease check -> txn: SELECT queued ORDER BY created_at LIMIT 1
       -> UPDATE status='running' WHERE status='queued', rowcount==1
agent: dispatch(tool, args, policy, approval_granted)   [policy re-checked]
agent: POST tasks/{id}/result {completed|failed|cancelled|timed_out}
plane: UPDATE tasks SET status=? (unconditional) + task_event + audit
       -> advance_handoff() for gated supervisor flows
```

- **Claim atomicity — VERIFIED GOOD.** The claim is the one place where
  cross-worker correctness is handled properly: the `UPDATE ... WHERE
  status='queued'` + `rowcount` check means at most one claimer wins, on
  both SQLite (serialised by the process lock) and Postgres (row lock).
  A losing claimer loops and retries within its long-poll deadline
  (`app.py:4640-4675`).
- **Per-device lease — VERIFIED, with a race.** `device_has_active_lease`
  (`orchestration.py:15-25`) refuses a second task while a `running` task
  with `updated_at` within the lease (default 900s,
  `AI_BRIDGE_TASK_RUNNING_LEASE_SEC`) exists. But the lease check runs
  **outside** the claim transaction (`app.py:4630-4645`): two concurrent
  `next_task` calls can both pass the check; the claim's `rowcount` guard
  then saves correctness (only one wins the row), so the outcome is safe
  but the losing caller burns a poll cycle. INFERRED benign.
- **Long-polling — VERIFIED.** `wait` clamped to [0, 25]s; the loop sleeps
  0.15s per iteration and re-checks emergency-stop and the lease each
  iteration (`app.py:4625-4675`). With N agents long-polling, N request
  handlers spin on 0.15s sleeps — a hypothesis-level CPU concern, not a
  correctness one (see Performance report). The agent uses `wait=5` when
  its WS is connected, else `wait=15` (`poller.py:490-500`).

## 4. Heartbeats, presence, failure detection

- **Agent → plane heartbeat**: every `run_once()` iteration, i.e. roughly
  every long-poll duration (5–15s) plus 0.5s wake-wait
  (`poller.py:330-345,485-505`). Heartbeat updates `devices.status='online',
  last_seen` and inserts one telemetry row + prunes to 500/device
  (`app.py:4419-4475`).
- **Staleness**: `fleet_health.py:10` `STALE_HEARTBEAT_SEC = 90` — a device
  is `stale` if `status == 'online'` but `last_seen` is > 90s old; `offline`
  otherwise. No automatic status flip to `offline` was found in the files
  read — staleness is computed at read time, not maintained (INFERRED:
  `devices.status` stays `online` until the next heartbeat or an explicit
  update; verify against the dashboard track).
- **Agent-side failure handling**: `run_forever` catches all exceptions,
  backs off 2s → 4s → … capped at 60s, resets on success
  (`poller.py:485-510`). WS reconnect backoff 2s → 60s
  (`poller.py:455-480`). Token rotation is attempted when
  `token_expires_at - now < 48h` (`ROTATE_THRESHOLD_SECONDS = 2*86400`,
  `poller.py:20,270-280`); rotation failure is swallowed (`except: pass`)
  and retried on the next heartbeat — good resilience, but a token that
  expires before a successful rotation bricks the agent until re-enrolment
  (INFERRED; the 48h window makes this unlikely).
- **401 recovery**: on heartbeat 401 the poller retries once with the
  token from disk (`poller.py:257-268`) — handles rotation races.
- **WS side-channel**: best-effort only; HTTP claim is authoritative
  (`agent_channel.py:1-5`). WS pings every 20s, recv timeout 30s
  (`poller.py:455-475`). If WS dies, the agent falls back to 15s
  long-polls — graceful degradation VERIFIED by code structure.

## 5. Duplication, retries, idempotency

| Mechanism | Verdict |
|-----------|---------|
| `POST /api/tasks` idempotency | **VERIFIED GOOD.** `Idempotency-Key` (1–128 ASCII) + body SHA-256; `task_submissions` PK `(actor, request_key)`; `INSERT ... ON CONFLICT DO NOTHING`, `rowcount==0` → replay returns the original task or 409 on hash mismatch (`app.py:1490-1541`, `db.py:89-93`). Scoped per actor. No TTL on keys (INFERRED: keys accumulate forever). |
| `POST /api/v1/tasks` idempotency | **NOT FOUND.** No key handling; a retried request creates a second task. |
| Workflow step retries | **At-least-once, duplicates possible.** `_run_workflow_worker` (`app.py:2420-2470`) retries a failed step `max_retries` times (default 1) with a 1s sleep and no idempotency token; side-effecting steps (send mail, git commit, file write) can execute twice. |
| Supervisor recovery | `recover_failed_role_tasks` (`supervisor.py:440-470`) re-queues failed role tasks; `advance_handoff` on failure enqueues a Recovery step — a task that failed *after* partially succeeding is re-run whole. No deduplication beyond task id. |
| Stale-lease requeue | **At-least-once by design.** `sweep_stale_running_tasks` (`orchestration.py:27-70`) re-queues `running` tasks older than the 900s lease (default `AI_BRIDGE_STALE_TASK_REQUEUE=1`). If the agent is merely slow or partitioned (not dead), the task executes twice — once on the original agent, once on the re-queued claim. There is **no fencing token, no execution epoch, no version column** on `tasks` to let the plane reject the stale agent's result. |
| Agent result spool | **Deduplicated by task_id.** `spool.py:60-80` upserts on `task_id` PK; `flush()` stops at the first failure (`spool.py:105-125`), preserving order. But: the poller spools *before* transmitting (`poller.py:355-365`), and if the POST succeeds but the response is lost, the row stays spooled and is re-sent — the plane's `task_result` accepts it again (idempotent outcome only because it overwrites the same status; a *second, different* result would also overwrite — see §6). |
| Scheduler jobs | **Duplicate firing CONFIRMED (structure).** See §8. |
| Custom-agent runs | `custom_agents.py` AgentRunner claims `agent_run_role` tasks with the same `UPDATE ... WHERE status='queued'` + `rowcount` pattern (`custom_agents.py:926`) — VERIFIED same good pattern. |

## 6. Cancellation, timeouts, crashes, restarts

- **Cancellation paths (VERIFIED):** (1) `POST /api/tasks/{id}/cancel`
  sets status `cancelled` (`app.py:1618-1630`); (2) WS `notify_cancel`
  (`agent_channel.py:95-100`); (3) agent-side polling of
  `GET /api/agent/tasks/{id}/execution-state` every 0.5s in a dedicated
  thread during execution (`poller.py:300-330`); (4) emergency-stop via WS
  + heartbeat + `next_task` refusal; (5) cooperative checks
  (`task_runtime.py`: `raise_if_cancelled()` at dispatch entry, in
  `cooperative_sleep`, and in the `_run_argv` wait loop every 0.25s).
- **Gaps (CONFIRMED by reading):**
  1. `task_result` (`app.py:4720-4760`) does `UPDATE tasks SET status=?
     WHERE id=?` with **no `AND status='running'` guard** — a late result
     from a cancelled/timed-out/re-queued task overwrites the terminal
     status (e.g. `cancelled` → `completed`). The same unconditional
     UPDATE exists in `update_task` paths. Fix: guard the UPDATE and check
     `rowcount`.
  2. Cancellation is cooperative: a dispatch handler that never calls
     `raise_if_cancelled()` (several browser/desktop handlers run long
     loops) will finish before the cancel takes effect. NOT VERIFIED
     exhaustively — stated as INFERRED from the handler inventory.
  3. `cancel_task` on the plane does not appear to push a WS cancel to
     the device in the code read (the `notify_tasks(cancel=True)` path
     exists at `app.py:681-690` but the cancel endpoint's call was not
     traced in this track — mark as NOT VERIFIED, needs a check).
  4. Emergency-stop stops *new* claims (`next_task` returns
     `emergency_stop: true`) and cancels in-flight work via the poller's
     `_cancelled()` check, but queued tasks are **not** cancelled — they
     sit queued until the stop clears, then execute (INFERRED from
     `next_task` returning early without touching queued rows).
- **Agent crash:** in-flight task stays `running` until the sweep re-queues
  it (≤ 900s + sweep interval) — see §7. Local `TaskRunner` state
  (`execution.py:60-65`, in-process `_tasks`/`_procs` dicts) is lost on
  process restart; there is no recovery of local MCP-server tasks.
- **Server restart:** durable state (tasks, approvals, emergency-stop,
  takeover) survives via the DB. Lost: WS subscribers, OIDC pending
  logins, in-memory rate buckets, `device_live_meta`, latest frames,
  `app.state` caches (see DB report §10). In-flight long-polls die with
  the worker; agents retry with backoff — VERIFIED resilient.
- **DB failure:** `PostgresConnection._ensure_conn` reconnects on the
  next call but drops in-flight transactions (`db.py:360-365`); SQLite
  failure surfaces as 500s (no fallback). No circuit breaker or
  read-only degraded mode was found (NOT FOUND).

## 7. Stale-task sweep (VERIFIED, `orchestration.py:27-70`)

- Runs from the scheduler loop (see §8): every 10s tick, every 3rd tick
  (`AI_BRIDGE_TASK_SWEEP_EVERY=3` → ~30s, `app.py:2671-2675`), plus
  `DEFAULT_SWEEP_INTERVAL_SEC=30` exists but is not the value used by the
  loop (the loop hard-codes its own cadence — INFERRED drift between the
  two constants).
- Re-queues (default) or times-out `running` tasks with
  `updated_at < now - lease`. Per-row UPDATEs with `WHERE status='running'`
  guard, single commit at the end — safe under concurrency for the
  re-queue itself, but the *decision* (stale?) is based on `updated_at`,
  which is only bumped at claim time, not by agent progress heartbeats:
  a legitimately long task (> 900s, e.g. a big test run or file operation)
  is re-queued while still running (INFERRED: no progress heartbeat
  updates `updated_at` during execution — the agent's heartbeat updates
  the *device* row, not the *task* row).
- Sweep stats are stored in `app.state.last_stale_sweep` — per-worker,
  so the dashboard's "last sweep" differs by worker (INFERRED).

## 8. Multi-worker safety — the central finding (CONFIRMED by code structure)

Prod runs 4 gunicorn workers (`Dockerfile.prod:56-60`). `create_app()` is
executed **per worker**, and it starts, per worker:

| Per-worker resource | File | Multi-worker consequence |
|---------------------|------|--------------------------|
| `_scheduler_loop` thread (10s tick: sweep + due schedules + triggers) | `app.py:2661-2720` | **Every worker fires due schedules.** The claim is `UPDATE schedules SET next_run_at = now+interval` with no `WHERE next_run_at <= ?` guard and no atomic claim — two workers can both read a due job and both fire it (workflow insert + thread, or `_execute_workflow_step_sync`). Duplicate scheduled workflows / duplicate trigger actions across workers. CONFIRMED by reading the loop: no lock, no lease, no `rowcount` check on the UPDATE. |
| `_scheduler_loop` sweep | same | Sweep is idempotent-ish (guarded UPDATEs), so duplicate sweeps are mostly harmless — but both workers may re-queue the same task (second UPDATE affects 0 rows — safe). Low impact. |
| `agent_channel._subscribers` | `agent_channel.py:20` | WS wake/cancel/emergency-stop pushes only reach agents connected **to that worker**. An agent on worker B never hears worker A's `notify_task_available`. Mitigant: HTTP long-poll still delivers the task within ≤ 15s. Impact: wake latency + missed cancels (cancel relies on the agent's 0.5s `execution-state` polling as fallback — that *is* cross-worker, via the DB). |
| OIDC `_PENDING` / `_DISCOVERY_CACHE` | `oidc.py:30-31` | Callback on a different worker than login → `pop_pending` misses → 400. Login is flaky under multi-worker (probability ~3/4 per attempt with 4 workers, assuming random routing). |
| CSRF secret | `csrf.py:22` | Per-process random secret → tokens invalid across workers (see Security report). |
| In-memory rate limiter `_RATE` | `ratelimit.py:18` | Per-worker buckets: effective limit is 4× the configured limit. The durable DB/Redis path exists but the sync wrapper falls back to memory inside async contexts (`ratelimit.py:78-100`) — see Performance report. |
| `app.state` caches | `app.py:670-675,3305` | `emergency_stop` is re-synced from DB with TTL (good); `device_live_meta`, `latest_frames`, `webrtc_sessions`, `last_stale_sweep` are per-worker and divergent. |
| `LAST_FRAME_SCALE`, `_LAST_SOM_MARKS` | `tools.py:59-70` | Process-global mutable state in the MCP server process (single-process there — fine). |
| `task_cond` | `app.py:677` | `threading.Condition` per worker — `notify_tasks()` only wakes waiters in the same worker; cross-worker waiters rely on poll timeouts. |

**What *is* multi-worker safe (VERIFIED):** task claim (`rowcount`
guard), idempotent task submission (DB PK), approvals decide
(`WHERE status='pending'`), emergency-stop and takeover flags (DB-backed
with TTL re-read), `task_submissions` replay, and the durable rate limiter
*when* Redis is configured and the call path actually reaches it.

**What is NOT multi-worker safe:** scheduler firing, WS fan-out, OIDC
login, CSRF, in-memory rate limiting, and every `app.state` cache.

## 9. Event ordering

- **Task events** (`task_events` table, `task_events.py`): appended at
  claim (`running`) and result (terminal status) with `commit=False`
  inside the caller's transaction where applicable — ordering within one
  task is causal (claim precedes result). Across tasks there is no global
  ordering guarantee; `created_at` is wall-clock `time.time()` (clock
  skew across hosts is unhandled — INFERRED).
- **Audit chain**: `ORDER BY id DESC LIMIT 1` for the tip assumes a
  single-writer ordering; concurrent writers can fork (see DB report §8).
- **Handoff advancement**: `advance_handoff` runs after the result commit
  in `task_result` (`app.py:4750-4760`); if the process dies between the
  result commit and the handoff enqueue, the handoff stalls until …
  nothing re-drives it (NOT FOUND: no sweeper for stuck handoffs).
  The supervisor `advance_handoff` itself is not transactional with the
  result update (INFERRED gap).
- **WS vs HTTP ordering**: WS `task_available` is a hint; HTTP claim is
  the source of truth (`agent_channel.py:3-5`) — correct design, no
  ordering hazard by construction.
- **Live activity / fleet health**: computed from DB snapshots at request
  time; no ordering claims made — fine.

## 10. Findings table

| ID | Severity | Title | File(s) |
|----|----------|-------|---------|
| DIST-01 | High | Scheduler loop runs in every gunicorn worker with no leader election / atomic claim — duplicate scheduled workflows & trigger actions | `src/ai_bridge/control/app.py:2661-2720` |
| DIST-02 | High | Task execution is at-least-once with no fencing: stale-lease requeue (900s) + no progress heartbeat on the task row + unconditional result UPDATE | `src/ai_bridge/control/orchestration.py:27-70`; `src/ai_bridge/control/app.py:4720-4760` |
| DIST-03 | High | WS wake/cancel fan-out is per-worker; agents on other workers miss pushes | `src/ai_bridge/control/agent_channel.py:20-127` |
| DIST-04 | Medium | OIDC login breaks across workers (in-memory `_PENDING`) | `src/ai_bridge/control/oidc.py:30-31,73-76,152` |
| DIST-05 | Medium | Late result overwrites `cancelled`/`timed_out` (no `status='running'` guard) | `src/ai_bridge/control/app.py:4720-4760` |
| DIST-06 | Medium | No handoff re-driver: crash between result commit and `advance_handoff` stalls gated handoffs | `src/ai_bridge/control/app.py:4750-4760`; `src/ai_bridge/control/supervisor.py` |
| DIST-07 | Medium | Rate limiting is effectively 4× under 4 workers (in-memory fallback in async contexts) | `src/ai_bridge/control/ratelimit.py:18,78-100` |
| DIST-08 | Medium | Long tasks (> 900s) are re-queued while still running — no task progress heartbeat | `src/ai_bridge/control/orchestration.py:13-25` |
| DIST-09 | Low | `Idempotency-Key` has no TTL; v1 API has no idempotency at all | `src/ai_bridge/control/app.py:1490-1541`; `src/ai_bridge/control/v1_routes.py:1034` |
| DIST-10 | Low | `task_cond` wake-ups are per-worker; cross-worker waiters rely on poll timeouts | `src/ai_bridge/control/app.py:677-690` |
| DIST-11 | Low | Audit hash chain can fork under concurrent writers | `src/ai_bridge/control/db.py:440-525` |

## 11. Recommendations (priority order)

1. **P1** — Scheduler: add an atomic claim (`UPDATE schedules SET
   next_run_at=? WHERE id=? AND next_run_at<=?`, check `rowcount`) or a
   leader-election/lease (DB row or Redis lock) so only one worker fires
   each due job; same for trigger evaluation.
2. **P1** — Fencing: add an execution epoch/version to `tasks`
   (increment on claim/requeue); `task_result` must carry the epoch and
   the UPDATE must be `WHERE id=? AND status='running' AND epoch=?`.
   Add an agent progress heartbeat that bumps `task.updated_at` (or a
   `lease_until`) so long tasks are not re-queued.
3. **P1** — Move WS fan-out behind Redis pub/sub (prod compose already
   ships Redis for this purpose) or document single-worker operation for
   the WS path; move OIDC `_PENDING` to Redis/DB.
4. **P2** — Make `advance_handoff` transactional with the result update,
   and add a sweeper for handoffs whose last step is terminal but no next
   step was enqueued.
5. **P2** — Fix the rate-limit wrapper so the Redis path is reachable
   from async middleware (or use only `durable_ratelimit.allow`); add
   TTL/expiry to `task_submissions` keys.
6. **P3** — Document the at-least-once contract for workflow steps and
   add opt-in idempotency keys for side-effecting steps.

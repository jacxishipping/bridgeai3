> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 2A — Distributed Reliability (dist child) — Progress

Owner: Wave 2A dist subagent. Repo: `/home/hatch/workspace/ai-bridge-main`,
on top of git commit 726bed2 (do NOT reset/rebase, do NOT commit, do NOT pip
install). Parent record: `analysis/wave2a-progress.md` (this file is the
delegated record it points to for DIST-03/04/08, F-05-07, TST-15-OIDC,
PERF-02).

Scope files ONLY (no other source files touched):
`src/ai_bridge/control/oidc.py`, `src/ai_bridge/control/task_runtime.py`,
`src/ai_bridge/control/durable_ratelimit.py`,
`src/ai_bridge/control/ratelimit.py`,
`src/ai_bridge/control/agent_channel.py`,
`src/ai_bridge/control/orchestration.py`.
Parent owns wiring into `app.py` / `v1_routes.py` / `db.py` / `task_service.py`
(`create_app` / `next_task` / `task_result`); the exact new APIs are listed
in §3 so the parent can wire them.

## 1. Finding verification (before any edit — all CONFIRMED)

| Finding | Verified as |
|---|---|
| TST-15 / DIST-04 OIDC `_PENDING` | CONFIRMED: `oidc.py:31` `_PENDING: dict` is process memory; `new_login()` (line 73) writes it, `pop_pending()` (line 152) reads it; used by `app.py:4054` (`oidc_login`) / `app.py:4065` (`oidc_callback`). A callback landing on another worker misses → 400. Discovery cache is a cache (refetch safe) — left in-memory per task. `verify_id_token` untouched. |
| F-05-07 global exec context | CONFIRMED: `task_runtime.py:15-18` single `_current_task_id` / `_cancel_check` / `_current_device_id` under one lock; two concurrent local executions overwrite each other's cancel predicate. Callers: `dispatch.py`, `browser_tools.py`, `tools.py` (all via the public API, which is preserved). |
| PERF-02 rate-limit DB hot path | CONFIRMED: `durable_ratelimit.allow()` — when Redis is unreachable `_redis_allow()` returns None and `allow()` falls through to `_db_allow()` (a per-request `settings` write) whenever a `conn` is supplied. `app.py:90-96` `_rate_limit` calls `durable_ratelimit.allow(..., conn=...)` when no Redis URL is set, and `ratelimit.rate_limit()` when one is set. |
| DIST-03 WS fan-out per-process | CONFIRMED: `agent_channel.py:20` `_subscribers` is a per-process dict; `notify_*` only `_schedule(push_to_device(...))`. No Redis pub/sub usage anywhere in the repo (only `ratelimit:*` / `aibridge:ratelimit:*` keys). `notify_*` callers: `app.py:687-689` (`notify_tasks`), `app.py:2032/2043` (e-stop). |
| DIST-08 no task progress heartbeat | CONFIRMED: `orchestration.py:27-70` `sweep_stale_running_tasks` re-queues `running` tasks with `updated_at < now - lease` (default 900s); `updated_at` is only bumped at claim time, never by agent progress. `task_events.append_task_event` exists (`task_events.py:29`) and is reused for the progress event. Sweep lease default (900s) unchanged. |
| `ratelimit.py` async Redis path usage | CHECKED: `ratelimit.rate_limit()` IS used by the app (`app.py:50,90-92` `_rate_limit`, called from the auth/metrics middleware). BUT the async Redis path `_redis_rate_limit` is effectively unreachable from the async middleware: the sync callable detects a running loop and falls back to memory (DIST-07). It is reachable from sync contexts via `asyncio.run`. The module has no DB path at all, so the PERF-02 rule (Redis down → memory, never DB) already held there. |

## 2. Per-task record

### TST-15 / DIST-04 — durable OIDC pending state (`oidc.py`) — VERIFIED
- `bind_oidc_store(conn)`: module-level, optional; `None` unbinds. Parent
  wires it in `create_app()` with the plane connection.
- `_store_pending(state, record)` / `_pop_pending_store(state)`: when a
  conn is bound, rows in the shared `settings` table, key
  `oidc.pending.<state>`, JSON `{"nonce": ..., "at": ...}`, 600s TTL
  (`_PENDING_TTL_SEC`), expired/malformed rows pruned on read (and on
  write); single-use via DELETE-on-pop. On any store error, falls back to
  the in-memory dict with a warning log (login availability over hard
  failure). With no conn bound, behaviour is exactly the old in-memory
  dict (dev/test).
- `new_login()` / `pop_pending()` use the store transparently; discovery
  cache untouched; `verify_id_token` semantics unchanged.

### F-05-07 — per-task execution contexts (`task_runtime.py`) — VERIFIED
- Replaced the single global with a registry (`dict[task_id, TaskContext]`,
  lock-guarded) plus a `contextvars.ContextVar` for the "current" task.
- Public API preserved and still working: `set_execution_context` /
  `clear_execution_context` / `current_task_id` / `current_device_id` /
  `is_cancelled` / `raise_if_cancelled` / `cooperative_sleep` — all now
  consult the current contextvar, not a global. `TaskCancelled` unchanged.
- New: `register_task_context(task_id, cancel_check, device_id=None) ->
  TaskContext`, `unregister_task_context(task_id) -> None`,
  `is_task_cancelled(task_id) -> bool`, `task_context(task_id) ->
  TaskContext | None`. `TaskContext` is a dataclass
  (`task_id`, `cancel_check`, `device_id`, `.is_cancelled()`).
- `clear_execution_context()` unregisters the current task (no registry
  leak); other registered tasks are untouched. `set_execution_context(None,
  ...)` clears the current context as before.

### PERF-02 — Redis rate-limit discipline (`durable_ratelimit.py`, `ratelimit.py`) — VERIFIED
- `durable_ratelimit.allow()`: when a Redis URL is configured and Redis is
  unreachable, falls back to the in-memory limiter — the DB path is skipped
  entirely on that path. DB path now applies only when no Redis URL is
  configured at all. Outage logged once per process
  (`_note_redis_unreachable`, flag `_REDIS_UNREACHABLE_WARNED`); a 10s
  negative cache avoids re-attempting a dead Redis on every request.
- New: `redis_required() -> bool` (True iff `REDIS_URL` /
  `AI_BRIDGE_REDIS_URL` is set). `backend_name(conn)` is now accurate:
  `"redis"` (reachable), `"memory"` (Redis configured but down — DB not
  used), `"database"` (no Redis URL + conn), else `"memory"`.
- `ratelimit.py`: the async Redis path `_redis_rate_limit` already fell
  back to memory (no DB path exists in this module); added the same
  once-per-process warning on that fallback plus `redis_required() ->
  bool` for symmetry. No behaviour change beyond the log line.
- NOT fixed here (still open, DIST-07): the sync callable from
  `rate_limit()` detects a running loop and uses the in-memory limiter in
  async contexts, so app.py's async middleware never reaches Redis even
  when healthy — fixing that needs an async-native limiter in the
  middleware (roadmap PERF-02 second half).

### DIST-03 — Redis WS fan-out (`agent_channel.py`) — VERIFIED
- `publish_redis(device_id, payload) -> bool`: sync redis-py publish to
  channel `aibridge:agent:<device_id>`; envelope
  `{"origin": <worker-id>, "payload": {...}}` so a worker's own fan-out
  subscriber can skip its own messages (it already pushed locally).
  No-op returning False when Redis is unconfigured/unreachable; never
  raises (10s negative cache on dead Redis).
- `start_redis_fanout(loop) -> asyncio.Task | None`: subscribes with
  pattern `aibridge:agent:*` via `redis.asyncio` on a background task and
  forwards received payloads to local subscribers via `push_to_device`.
  Returns None when no Redis URL is configured or no loop is available;
  when Redis is unreachable the task logs a warning and exits (no fan-out,
  no exception). Idempotent (returns the existing live task).
- `notify_task_available` / `notify_cancel` / `notify_emergency_stop` now
  ALSO publish to Redis, in addition to the existing in-process push.
  E-stop uses the broadcast channel `aibridge:agent:broadcast` (this
  worker does not know the ids of devices on other workers); the
  subscriber forwards broadcast payloads to all local devices. All
  publish calls are guarded so a Redis outage never raises out of
  `notify_*`.

### DIST-08 — task progress heartbeat (`orchestration.py`) — VERIFIED
- `record_task_progress(conn, task_id) -> bool`:
  `UPDATE tasks SET updated_at=now WHERE id=? AND status='running'`
  (rowcount==1 means still running) plus a `task_events` row
  (`"progress"`, `{"at": now}`), committed immediately. Returns False
  for non-running / unknown tasks (agent should stop work). Docstring
  states: `sweep_stale_running_tasks` keys off `updated_at`, so a task
  whose agent reports progress is not re-queued while running; only a
  task silent for a full lease is reclaimed. Sweep lease default (900s)
  unchanged. Callers should throttle reports (e.g. ≤1 per 60s per task)
  to bound `task_events` growth.

## 3. Exact APIs added (for parent wiring)

```python
# oidc.py — parent: bind_oidc_store(app.state.conn) in create_app()
bind_oidc_store(conn) -> None                      # conn=None unbinds
_store_pending(state: str, record: dict) -> None   # internal
_pop_pending_store(state: str) -> dict | None      # internal

# task_runtime.py — no wiring change needed; existing callers keep working.
# Parent may additionally use, for concurrent local execution:
register_task_context(task_id, cancel_check, device_id=None) -> TaskContext
unregister_task_context(task_id) -> None
is_task_cancelled(task_id) -> bool
task_context(task_id) -> TaskContext | None        # .task_id/.cancel_check/.device_id

# durable_ratelimit.py
redis_required() -> bool
# allow()/backend_name() signatures unchanged; semantics per §2.

# ratelimit.py
redis_required() -> bool

# agent_channel.py — parent: start_redis_fanout(asyncio.get_running_loop())
#   in create_app() (after bind_event_loop); notify_* need no wiring change.
publish_redis(device_id: str, payload: dict) -> bool
start_redis_fanout(loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Task | None

# orchestration.py — parent: call from task_result()/heartbeat path when the
#   agent reports progress (agent sends progress; plane records it).
record_task_progress(conn, task_id: str) -> bool
```

## 4. Tests executed

Scratch verification script (not committed): `/tmp/wave2a_verify.py`,
run with `.venv/bin/python /tmp/wave2a_verify.py` → **ALL SCRATCH CHECKS
PASSED** (output below, abridged; the durable_ratelimit warning line is
the expected once-per-process log):

```
durable_ratelimit: Redis configured (REDIS_URL/AI_BRIDGE_REDIS_URL) but unreachable
  (Error 111 connecting to 127.0.0.1:6399. Connection refused.); falling back to
  in-memory rate limiting for this process
OIDC cross-worker round-trip: OK (state=3nrs0trV... nonce matched, single-use)
OIDC stale (601s) record: rejected + pruned OK
OIDC unbound in-memory fallback: OK
task_runtime per-task contexts: OK (a=cancelled, b=not, current switches)
durable_ratelimit bogus REDIS_URL: OK (memory fallback, limit enforced, settings rows 1 -> 1)
durable_ratelimit no-Redis backend_name: database OK
agent_channel bogus REDIS_URL: publish=False, notify_* no-raise OK
agent_channel fan-out handler: OK (foreign forwarded, own skipped, broadcast to all)
agent_channel start_redis_fanout no-URL: None OK
orchestration record_task_progress: OK (running=True+bump+event, queued/missing=False, sweep spares it)
ALL SCRATCH CHECKS PASSED
```

Scratch checks performed: OIDC `new_login` on conn A → `pop_pending` on
conn B (same DB file, two connections = two workers), single-use,
600s-stale rejection + prune, unbound in-memory fallback; two registered
task contexts with opposite predicates (`is_task_cancelled` per-task),
current-context switching, `cooperative_sleep` raising only for the
cancelled current task; `durable_ratelimit.allow` with bogus
`REDIS_URL=redis://127.0.0.1:6399/0` — limit enforced in memory and
`settings` row count unchanged (1 → 1, i.e. no DB write);
`agent_channel.publish_redis` → False and `notify_*` no-raise with bogus
Redis; fan-out handler unit check (foreign-origin forwarded, own-origin
skipped, broadcast to all local devices); `record_task_progress` bumps
`updated_at` + writes the `progress` event, returns False for
queued/missing tasks, and the progressed task is spared by
`sweep_stale_running_tasks(lease_sec=900)`. Live Redis pub/sub was NOT
tested (no Redis server in this environment); the subscriber path was
verified via direct `_handle_redis_message` calls.

Existing test files (affected):
```
.venv/bin/python -m pytest tests/unit/test_phase9_deep.py \
  tests/unit/test_serverai_mcp_orchestration.py \
  tests/unit/test_orchestration_phases.py tests/unit/test_phase8_deep.py -q
→ 2 failed, 55 passed, 1 warning in 24.35s
```
The 2 failures are **pre-existing, unrelated**: 
`test_legacy_webrtc_session_deprecation_header` and
`test_http_client_reused_across_api_calls`, both failing with
`httpx.InvalidURL: Invalid port: ':1]'`. Proven pre-existing by restoring
all 6 files from HEAD (`git checkout -- <6 files>`) and re-running just
those two tests → same 2 failures; files then restored byte-identical from
`/tmp/w2a_backup/` and re-verified (`py_compile` OK, scratch script
re-run → ALL PASSED, plus
`test_phase9_deep.py::test_durable_ratelimit_db_multi_worker` +
`test_orchestration_phases.py` + `test_phase8_deep.py` → 33 passed).
No existing test covers control-plane OIDC (only `test_microsoft365.py`
covers the analogous M365 flow); no new test files were added — the
scratch script is the verification for the new APIs, and the parent may
promote it into the suite.

## 5. Decisions / notes

- OIDC store uses the `settings` table (no new table/migration) because
  the durable e-stop already uses it and the parent's migration 013 is
  owned by the DB child; the store is optional so dev/test without a bound
  conn keep the old behaviour. Store errors fall back to memory with a
  warning rather than failing login.
- `task_runtime.clear_execution_context()` unregisters the current task;
  `set_execution_context(task_id=None)` only clears the contextvar (the
  task id is unknown there). The contextvar means the "current" context is
  per-thread/asyncio-task, matching how dispatch executes tasks.
- `durable_ratelimit` negative cache (10s) on a dead Redis: without it,
  every `allow()` call would pay a TCP connect timeout to a dead Redis.
- `agent_channel` e-stop broadcast channel is `aibridge:agent:broadcast`;
  a bare (non-envelope) JSON payload on a device channel is still
  forwarded, for tolerance of other publishers.
- `record_task_progress` commits immediately (heartbeat durability across
  workers) and uses `task_events.append_task_event(..., commit=False)` +
  one `conn.commit()`.

## 6. Rollback

Revert the 6 files (`git checkout -- src/ai_bridge/control/{oidc,
task_runtime,durable_ratelimit,ratelimit,agent_channel,orchestration}.py`
against the Wave 2A commit). No schema change was made (OIDC pending rows
and `progress` events are ordinary `settings` / `task_events` rows; stale
`oidc.pending.*` rows expire on read and are harmless if the code is
reverted). If the parent has already wired `bind_oidc_store` /
`start_redis_fanout` / `record_task_progress` into `app.py`, revert those
call sites in the same revert — the helpers are additive, so un-wiring is
just deleting the calls.

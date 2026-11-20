"""Cross-worker rate limiting with Redis, DB, or in-process fallback (Phase 9).

Implementation layer for :mod:`.limiter`, which is the single entry
point for rate limiting (INV-12 / TD-D05, Wave 3B). New code must
import from :mod:`.limiter`; this module's public functions stay intact
so existing callers and tests keep working, but they are the backend,
not the interface.

Backend selection (Wave 2A / PERF-02):

* Redis URL configured and reachable -> Redis sliding window.
* Redis URL configured but unreachable -> in-memory limiter. The DB path
  is NOT used on this path: a per-request DB write under a Redis outage
  would both hammer the shared DB and give a different (single, global)
  limit from the one Redis would enforce. The outage is logged once per
  process; the in-memory buckets are per-worker by necessity.
* No Redis URL configured -> DB limiter when a connection is supplied
  (multi-worker safe), else in-memory.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from typing import Any

log = logging.getLogger("ai_bridge.durable_ratelimit")

# Wave 5A (rate-limiter lockout, confirmed live 2026-11-17): the Redis
# sliding window must record ONLY admitted requests. The old code did
# ``zadd`` of the current request BEFORE counting, so every rejected
# request refreshed the window and a token that tripped the limit stayed
# 429 forever under sustained load. The Lua script below makes the
# prune -> count -> add-if-under-limit sequence atomic on real Redis
# (one round trip, no interleaving across workers). Clients without
# ``eval`` (old redis-py, test fakes) use the pipeline fallback in
# ``_redis_allow_pipeline`` / ``_async_redis_allow_pipeline``, which
# performs the same admit-only sequence in two round trips (racy under
# concurrent writers, but it can never lock the window out: rejected
# requests are never recorded).
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local ttl = math.max(2, math.floor(window * 2) + 1)
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  redis.call('EXPIRE', key, ttl)
  return 0
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, ttl)
return 1
"""


def _rate_limit_ttl(window: float) -> int:
    return max(2, int(window * 2) + 1)


def _rate_limit_member(now: float) -> str:
    # Unique per request: two requests in the same microsecond must not
    # collapse into one sorted-set member (that would under-count).
    return f"{now:.6f}:{secrets.token_hex(4)}"


def _eval_not_supported(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "unknown command" in msg or "noscript" in msg or "eval" in msg


_RATE_LOCK = threading.Lock()
_MEMORY: dict[str, list[float]] = {}
_SYNC_REDIS = None
_SYNC_REDIS_OK = False
_SYNC_REDIS_FAILED_AT = 0.0
_SYNC_REDIS_FAILED_COOLDOWN_SEC = 10.0
_REDIS_UNREACHABLE_WARNED = False


def _redis_url() -> str:
    return (os.environ.get("REDIS_URL") or os.environ.get("AI_BRIDGE_REDIS_URL") or "").strip()


def redis_required() -> bool:
    """True when a Redis URL is configured (Redis is the required backend)."""
    return bool(_redis_url())


def _note_redis_unreachable(exc: Exception) -> None:
    global _REDIS_UNREACHABLE_WARNED
    if not _REDIS_UNREACHABLE_WARNED:
        _REDIS_UNREACHABLE_WARNED = True
        log.warning(
            "durable_ratelimit: Redis configured (%s) but unreachable (%s); "
            "falling back to in-memory rate limiting for this process",
            "REDIS_URL/AI_BRIDGE_REDIS_URL",
            exc,
        )


def _get_sync_redis():
    global _SYNC_REDIS, _SYNC_REDIS_OK, _SYNC_REDIS_FAILED_AT
    if _SYNC_REDIS_OK:
        return _SYNC_REDIS
    url = _redis_url()
    if not url:
        return None
    if _SYNC_REDIS is None:
        # Negative cache: do not re-attempt a dead Redis on every request.
        if time.time() - _SYNC_REDIS_FAILED_AT < _SYNC_REDIS_FAILED_COOLDOWN_SEC:
            return None
        try:
            import redis

            _SYNC_REDIS = redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
            _SYNC_REDIS.ping()
            _SYNC_REDIS_OK = True
            _SYNC_REDIS_FAILED_AT = 0.0
            return _SYNC_REDIS
        except Exception as exc:
            _SYNC_REDIS = None
            _SYNC_REDIS_OK = False
            _SYNC_REDIS_FAILED_AT = time.time()
            _note_redis_unreachable(exc)
    return None


def backend_name(conn: Any | None = None) -> str:
    """Name of the backend allow() would actually use.

    "redis" when Redis is configured and reachable, "memory" when Redis is
    configured but unreachable (the DB path is not used then), "database"
    when no Redis URL is configured and a connection is supplied, else
    "memory".
    """
    if redis_required():
        return "redis" if _get_sync_redis() is not None else "memory"
    if conn is not None:
        return "database"
    return "memory"


def _memory_allow(key: str, limit: int, window: float) -> bool:
    now = time.time()
    with _RATE_LOCK:
        bucket = [t for t in _MEMORY.get(key, []) if now - t < window]
        if len(bucket) >= limit:
            _MEMORY[key] = bucket
            return False
        bucket.append(now)
        _MEMORY[key] = bucket
        return True


def _redis_allow_pipeline(client: Any, key: str, limit: int, window: float, now: float) -> bool:
    """Admit-only fallback for Redis clients without Lua ``eval``.

    Prune expired entries, count, and only then record the request —
    and only when it is admitted. Two round trips (racy under
    concurrent writers, unlike the Lua path) but never records a
    rejected request, so the window always drains.
    """
    redis_key = f"aibridge:ratelimit:{key}"
    window_start = now - window
    ttl = _rate_limit_ttl(window)
    pipe = client.pipeline()
    pipe.zremrangebyscore(redis_key, "-inf", window_start)
    pipe.zcard(redis_key)
    _, count = pipe.execute()
    if count >= limit:
        # Rejected: refresh the TTL so the key still expires, but do
        # NOT add this request to the window. Via a pipeline: some
        # clients (notably the async fakeredis-style clients used in
        # tests) expose expire only on pipelines, not on the client.
        pipe = client.pipeline()
        pipe.expire(redis_key, ttl)
        pipe.execute()
        return False
    pipe = client.pipeline()
    pipe.zadd(redis_key, {_rate_limit_member(now): now})
    pipe.expire(redis_key, ttl)
    pipe.execute()
    return True


def _redis_allow(key: str, limit: int, window: float) -> bool | None:
    global _SYNC_REDIS, _SYNC_REDIS_OK, _SYNC_REDIS_FAILED_AT
    client = _get_sync_redis()
    if client is None:
        return None
    redis_key = f"aibridge:ratelimit:{key}"
    now = time.time()
    try:
        eval_fn = getattr(client, "eval", None)
        if callable(eval_fn):
            try:
                admitted = eval_fn(_RATE_LIMIT_LUA, 1, redis_key, now, window, limit, _rate_limit_member(now))
            except Exception as exc:
                if _eval_not_supported(exc):
                    return _redis_allow_pipeline(client, key, limit, window, now)
                raise
            return bool(admitted)
        return _redis_allow_pipeline(client, key, limit, window, now)
    except Exception as exc:
        # Redis died mid-request: drop the cached client so the next call
        # re-probes (after the negative-cache cooldown) instead of reusing
        # a dead connection, and warn once per process.
        _SYNC_REDIS = None
        _SYNC_REDIS_OK = False
        _SYNC_REDIS_FAILED_AT = time.time()
        _note_redis_unreachable(exc)
        return None


# F-05-08: DB-backend counter encoding. One decimal integer
# ``N = t0_ms * _DB_COUNT_MODULUS + count`` stored as TEXT (see
# _db_allow); six digits of count, so the supported limit is < 1e6.
_DB_COUNT_MODULUS = 1_000_000

# F-05-08: single-statement conditional UPSERT for _db_allow. Six
# parameters, in order: (key, fresh_value, now_ms - window_ms,
# fresh_value, limit, legacy_reset_value). Portable across SQLite and
# Postgres — the ``%`` modulo becomes ``%%`` under to_pg_sql and
# psycopg collapses it back to ``%``; every other construct
# (substr/length/ABS/CAST/BETWEEN/||/CASE/RETURNING) exists in both.
# ``RETURNING value`` carries the decision: a leading '-' means this
# statement denied (counter unchanged), anything else admitted.
_DB_ALLOW_SQL = (
    "INSERT INTO settings (key, value) VALUES (?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = CASE "
    "WHEN substr(settings.value, 1, 1) BETWEEN '0' AND '9' "
    "OR (substr(settings.value, 1, 1) = '-' "
    "AND length(settings.value) > 1 "
    "AND substr(settings.value, 2, 1) BETWEEN '0' AND '9') "
    "THEN CASE "
    "WHEN ABS(CAST(settings.value AS BIGINT)) / 1000000 <= ? "
    "THEN ? "
    "WHEN ABS(CAST(settings.value AS BIGINT)) % 1000000 >= ? "
    "THEN '-' || CAST(ABS(CAST(settings.value AS BIGINT)) AS TEXT) "
    "ELSE CAST(ABS(CAST(settings.value AS BIGINT)) + 1 AS TEXT) "
    "END "
    "ELSE ? END "
    "RETURNING value"
)


def _db_allow(conn: Any, key: str, limit: int, window: float) -> bool:
    """Fixed-window allow via ONE conditional UPSERT (F-05-08).

    The old code did SELECT -> parse -> INSERT in three statements, so
    two workers racing on the same key both read ``count = limit - 1``
    and both admitted, overshooting the limit. This version issues a
    single ``INSERT ... ON CONFLICT DO UPDATE`` whose ``CASE`` decides
    admit/deny from the stored row, and the row lock the UPSERT takes
    serialises concurrent writers on both engines (SQLite's write lock
    / Postgres's row lock). The decision comes back via ``RETURNING``,
    so no second read is needed.

    Row encoding (one portable dialect, no JSON functions — SQLite's
    ``json_extract`` and Postgres's ``->>`` do not overlap, and the
    ``settings.value`` column is TEXT on both engines):

    * ``value`` is the decimal text of ``N = t0_ms * 1_000_000 + count``
      (``t0_ms`` = window-start epoch ms, ``count`` in [0, 999999]).
      ``t0_ms`` is recovered as ``N / 1000000``, ``count`` as
      ``N % 1000000`` — integer division / modulo behave identically in
      SQLite and Postgres, as do ``substr``, ``length``, ``ABS``,
      ``CAST(x AS BIGINT)`` / ``CAST(x AS TEXT)`` and ``||``.
    * A leading ``-`` means THIS statement denied the request: the
      stored counter is unchanged, only the sign marks the outcome, so
      the caller can tell admit from deny even when the count equals
      the limit (``RETURNING`` sees only the new row).
    * Rows in the pre-fix JSON format (``{"t0": ..., "count": ...}``) or
      otherwise malformed are denied once and reset to a fresh window
      with count 0 (negative), so the next call re-admits under the new
      format. Fail-closed on upgrade, exactly one request per key.

    Bounds: ``limit`` must be in [1, 999999] — the count field is six
    decimal digits, and a larger limit would carry into the timestamp
    field. ``limit < 1`` denies (as before); ``limit > 999999`` raises
    ``ValueError`` instead of silently corrupting the encoding.
    """
    if limit < 1:
        return False
    if limit > _DB_COUNT_MODULUS - 1:
        raise ValueError(f"durable_ratelimit: DB limit {limit} exceeds supported maximum {_DB_COUNT_MODULUS - 1}")
    setting_key = f"ratelimit.{key}"
    now_ms = int(time.time() * 1000)
    window_ms = int(window * 1000)
    fresh_value = str(now_ms * _DB_COUNT_MODULUS + 1)
    # Legacy/malformed row: deny this call, reset to a fresh window
    # with count 0 (negative sign = denied) in the new encoding.
    legacy_reset_value = "-" + str(now_ms * _DB_COUNT_MODULUS)
    row = conn.execute(
        _DB_ALLOW_SQL,
        (
            setting_key,
            fresh_value,
            now_ms - window_ms,
            fresh_value,
            limit,
            legacy_reset_value,
        ),
    ).fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError("durable_ratelimit: DB allow UPSERT returned no row")
    new_value = row["value"] if hasattr(row, "keys") else row[0]
    return not str(new_value).startswith("-")


def allow(key: str, limit: int, window: float = 60.0, *, conn: Any | None = None) -> bool:
    """Return True if the request is within the rate limit.

    When a Redis URL is configured, Redis is the only durable backend:
    if it is unreachable the in-memory limiter is used and the DB path
    is skipped entirely (PERF-02). The DB path applies only when no
    Redis URL is configured at all.
    """
    if redis_required():
        redis_ok = _redis_allow(key, limit, window)
        if redis_ok is not None:
            return redis_ok
        return _memory_allow(key, limit, window)
    redis_ok = _redis_allow(key, limit, window)
    if redis_ok is not None:
        return redis_ok
    if conn is not None:
        try:
            return _db_allow(conn, key, limit, window)
        except Exception:
            pass
    return _memory_allow(key, limit, window)


_ASYNC_REDIS = None
_ASYNC_REDIS_OK = False
_ASYNC_REDIS_FAILED_AT = 0.0


async def _get_async_redis():
    """Cached redis.asyncio client, or None when unconfigured/unreachable."""
    global _ASYNC_REDIS, _ASYNC_REDIS_OK, _ASYNC_REDIS_FAILED_AT
    if _ASYNC_REDIS_OK:
        return _ASYNC_REDIS
    url = _redis_url()
    if not url:
        return None
    if _ASYNC_REDIS is None:
        if time.time() - _ASYNC_REDIS_FAILED_AT < _SYNC_REDIS_FAILED_COOLDOWN_SEC:
            return None
        try:
            import redis.asyncio as aioredis

            _ASYNC_REDIS = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=2)
            await _ASYNC_REDIS.ping()
            _ASYNC_REDIS_OK = True
            _ASYNC_REDIS_FAILED_AT = 0.0
            return _ASYNC_REDIS
        except Exception as exc:
            _ASYNC_REDIS = None
            _ASYNC_REDIS_OK = False
            _ASYNC_REDIS_FAILED_AT = time.time()
            _note_redis_unreachable(exc)
    return None


async def _async_redis_allow_pipeline(client: Any, key: str, limit: int, window: float, now: float) -> bool:
    """Async admit-only fallback for Redis clients without Lua ``eval``.

    Same sequence as :func:`_redis_allow_pipeline`: prune, count, and
    record only admitted requests.
    """
    redis_key = f"aibridge:ratelimit:{key}"
    window_start = now - window
    ttl = _rate_limit_ttl(window)
    pipe = client.pipeline()
    pipe.zremrangebyscore(redis_key, "-inf", window_start)
    pipe.zcard(redis_key)
    _, count = await pipe.execute()
    if count >= limit:
        # Rejected: refresh the TTL via a pipeline (see the sync
        # fallback) and do NOT add this request to the window.
        pipe = client.pipeline()
        pipe.expire(redis_key, ttl)
        await pipe.execute()
        return False
    pipe = client.pipeline()
    pipe.zadd(redis_key, {_rate_limit_member(now): now})
    pipe.expire(redis_key, ttl)
    await pipe.execute()
    return True


async def _async_redis_allow(key: str, limit: int, window: float) -> bool | None:
    """Async Redis sliding-window check; None when Redis is unavailable."""
    global _ASYNC_REDIS, _ASYNC_REDIS_OK, _ASYNC_REDIS_FAILED_AT
    client = await _get_async_redis()
    if client is None:
        return None
    redis_key = f"aibridge:ratelimit:{key}"
    now = time.time()
    try:
        eval_fn = getattr(client, "eval", None)
        if callable(eval_fn):
            try:
                admitted = eval_fn(_RATE_LIMIT_LUA, 1, redis_key, now, window, limit, _rate_limit_member(now))
                if asyncio.iscoroutine(admitted):
                    admitted = await admitted
            except Exception as exc:
                if _eval_not_supported(exc):
                    return await _async_redis_allow_pipeline(client, key, limit, window, now)
                raise
            return bool(admitted)
        return await _async_redis_allow_pipeline(client, key, limit, window, now)
    except Exception as exc:
        _ASYNC_REDIS = None
        _ASYNC_REDIS_OK = False
        _ASYNC_REDIS_FAILED_AT = time.time()
        _note_redis_unreachable(exc)
        return None


async def allow_async(key: str, limit: int, window: float = 60.0, *, conn: Any | None = None) -> bool:
    """Async-native version of :func:`allow` (DIST-07).

    Same backend selection as :func:`allow`: Redis when configured and
    reachable, in-memory when Redis is configured but down (the DB path
    is skipped then, PERF-02), DB only when no Redis URL is configured
    at all. The DB path is synchronous and short (one settings row); it
    runs inline rather than in a thread because it is a single indexed
    lookup + upsert.
    """
    if redis_required():
        redis_ok = await _async_redis_allow(key, limit, window)
        if redis_ok is not None:
            return redis_ok
        return _memory_allow(key, limit, window)
    redis_ok = await _async_redis_allow(key, limit, window)
    if redis_ok is not None:
        return redis_ok
    if conn is not None:
        try:
            return _db_allow(conn, key, limit, window)
        except Exception:
            pass
    return _memory_allow(key, limit, window)


def prune_expired_db_ratelimits(conn: Any, *, max_age_sec: float = 3600.0) -> int:
    """Remove stale ``ratelimit.*`` counter rows from the shared settings table."""
    cutoff_ms = int((time.time() - max(60.0, float(max_age_sec))) * 1000)
    rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'ratelimit.%'").fetchall()
    removed = 0
    for row in rows:
        k = row["key"] if hasattr(row, "keys") else row[0]
        raw = str(row["value"] if hasattr(row, "keys") else row[1])
        try:
            num = abs(int(raw))
            t0_ms = num // _DB_COUNT_MODULUS
            stale = t0_ms < cutoff_ms
        except Exception:
            stale = True
        if stale:
            conn.execute("DELETE FROM settings WHERE key=?", (k,))
            removed += 1
    if removed:
        conn.commit()
    return removed


def reset_for_tests() -> None:
    """Clear cached clients, once-per-process warnings, and buckets."""
    global _SYNC_REDIS, _SYNC_REDIS_OK, _SYNC_REDIS_FAILED_AT
    global _ASYNC_REDIS, _ASYNC_REDIS_OK, _ASYNC_REDIS_FAILED_AT
    global _REDIS_UNREACHABLE_WARNED
    _SYNC_REDIS = None
    _SYNC_REDIS_OK = False
    _SYNC_REDIS_FAILED_AT = 0.0
    _ASYNC_REDIS = None
    _ASYNC_REDIS_OK = False
    _ASYNC_REDIS_FAILED_AT = 0.0
    _REDIS_UNREACHABLE_WARNED = False
    with _RATE_LOCK:
        _MEMORY.clear()
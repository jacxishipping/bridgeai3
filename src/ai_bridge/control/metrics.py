"""Prometheus Metrics & Observability Collector for AI Bridge Control Plane.

Exposes standard Prometheus metrics (/metrics) for monitoring fleet health,
task execution throughput, queue latencies, DB query/pool observability,
HTTP latency SLOs, dead-letter backlog, and agent spool state.
"""
from __future__ import annotations

import json
import time
from typing import Any

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

_START_TIME = time.time()

if _PROMETHEUS_AVAILABLE:
    # 1. Fleet & Device Gauges
    ACTIVE_DEVICES = Gauge(
        "aibridge_active_devices",
        "Number of managed fleet devices categorized by current status and OS",
        ["status", "os"],
    )

    # 2. Task Counters & Histograms
    TASKS_TOTAL = Counter(
        "aibridge_tasks_total",
        "Total count of tasks dispatched and processed",
        ["tool", "status"],
    )

    TASK_DURATION_SECONDS = Histogram(
        "aibridge_task_duration_seconds",
        "Execution latency of completed tasks in seconds",
        ["tool"],
        buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
    )

    QUEUE_DEPTH = Gauge(
        "aibridge_queue_depth",
        "Current number of queued or pending tasks awaiting device pickup",
    )

    STALE_RUNNING_TASKS = Gauge(
        "aibridge_stale_running_tasks",
        "Tasks in running state older than the configured lease window",
    )

    DEAD_LETTER_TASKS = Gauge(
        "aibridge_dead_letter_tasks",
        "Unreplayed tasks currently in the dead-letter queue",
    )

    STALE_HEARTBEAT_DEVICES = Gauge(
        "aibridge_stale_heartbeat_devices",
        "Fleet devices marked online but past the heartbeat SLO window",
    )

    AGENT_SPOOL_PENDING = Gauge(
        "aibridge_agent_spool_pending",
        "Total pending offline-spooled task results reported by fleet agents",
    )

    AGENT_SPOOL_MAX_AGE_SECONDS = Gauge(
        "aibridge_agent_spool_max_age_seconds",
        "Age in seconds of the oldest pending spooled result across fleet agents",
    )

    ADVANCED_PHASE_DEPTH = Gauge(
        "aibridge_advanced_phase_depth",
        "Roadmap phase depth (2=deep, 1=core) for phases 2-9",
        ["phase"],
    )

    # 3. HTTP Request Metrics
    HTTP_REQUESTS_TOTAL = Counter(
        "aibridge_http_requests_total",
        "Total HTTP requests received by the control plane API",
        ["method", "endpoint", "status_code"],
    )

    HTTP_REQUEST_DURATION_SECONDS = Histogram(
        "aibridge_http_request_duration_seconds",
        "HTTP request latency in seconds for API SLO monitoring",
        ["method", "endpoint"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0),
    )

    # 4. Database Query & Pool Observability
    DB_QUERY_DURATION_SECONDS = Histogram(
        "aibridge_db_query_duration_seconds",
        "Database query execution latency in seconds",
        ["dialect", "op"],
        buckets=(0.0005, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0),
    )

    DB_QUERIES_TOTAL = Counter(
        "aibridge_db_queries_total",
        "Total database queries executed by dialect, op, and outcome",
        ["dialect", "op", "status"],
    )

    DB_POOL_SIZE = Gauge(
        "aibridge_db_pool_size",
        "Current database connection pool size",
    )

    DB_POOL_AVAILABLE = Gauge(
        "aibridge_db_pool_available",
        "Idle database connections available in pool",
    )

    DB_POOL_WAITING = Gauge(
        "aibridge_db_pool_waiting",
        "Requests waiting to acquire a database connection from pool",
    )

    UPTIME_SECONDS = Gauge(
        "aibridge_uptime_seconds",
        "Total uptime of the AI Bridge Control Plane in seconds",
    )
else:
    ACTIVE_DEVICES = None
    TASKS_TOTAL = None
    TASK_DURATION_SECONDS = None
    QUEUE_DEPTH = None
    DEAD_LETTER_TASKS = None
    AGENT_SPOOL_PENDING = None
    AGENT_SPOOL_MAX_AGE_SECONDS = None
    HTTP_REQUESTS_TOTAL = None
    HTTP_REQUEST_DURATION_SECONDS = None
    DB_QUERY_DURATION_SECONDS = None
    DB_QUERIES_TOTAL = None
    DB_POOL_SIZE = None
    DB_POOL_AVAILABLE = None
    DB_POOL_WAITING = None
    UPTIME_SECONDS = None
    STALE_RUNNING_TASKS = None
    STALE_HEARTBEAT_DEVICES = None
    ADVANCED_PHASE_DEPTH = None

_PHASE_DEPTH = {
    "2": 2,
    "3": 2,
    "4": 2,
    "5": 2,
    "6": 2,
    "7": 2,
    "8": 2,
    "9": 2,
}


def record_task_execution(tool: str, status: str, duration: float | None = None) -> None:
    """Record metrics for a completed or failed task."""
    if not _PROMETHEUS_AVAILABLE:
        return
    TASKS_TOTAL.labels(tool=tool, status=status).inc()
    if duration is not None and duration > 0:
        TASK_DURATION_SECONDS.labels(tool=tool).observe(duration)


def record_http_request(method: str, endpoint: str, status_code: int, duration: float | None = None) -> None:
    """Record metrics for an incoming HTTP request."""
    if not _PROMETHEUS_AVAILABLE:
        return
    HTTP_REQUESTS_TOTAL.labels(
        method=method,
        endpoint=endpoint,
        status_code=str(status_code),
    ).inc()
    if duration is not None and duration >= 0:
        HTTP_REQUEST_DURATION_SECONDS.labels(method=method, endpoint=endpoint).observe(duration)


def record_db_query(dialect: str, op: str, duration_s: float, *, ok: bool = True) -> None:
    """Record latency and outcome of one database query."""
    if not _PROMETHEUS_AVAILABLE:
        return
    DB_QUERY_DURATION_SECONDS.labels(dialect=dialect, op=op).observe(max(0.0, duration_s))
    DB_QUERIES_TOTAL.labels(dialect=dialect, op=op, status="ok" if ok else "error").inc()


def update_fleet_gauges(conn: Any) -> None:
    """Synchronize gauge metrics with the database and connection pool state."""
    if not _PROMETHEUS_AVAILABLE:
        return

    UPTIME_SECONDS.set(time.time() - _START_TIME)

    # DB pool metrics
    try:
        if hasattr(conn, "get_db_stats"):
            st = conn.get_db_stats()
            DB_POOL_SIZE.set(float(st.get("pool_size", 1)))
            DB_POOL_AVAILABLE.set(float(st.get("pool_available", 1)))
            DB_POOL_WAITING.set(float(st.get("pool_waiting", 0)))
    except Exception:
        pass

    try:
        # Queue depth
        q_row = conn.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status='queued'").fetchone()
        q_depth = q_row["cnt"] if hasattr(q_row, "keys") else (q_row[0] if q_row else 0)
        QUEUE_DEPTH.set(q_depth)

        from . import db as dbmod
        from . import orchestration as orchmod

        now_ts = dbmod.now()
        cutoff = now_ts - orchmod.DEFAULT_RUNNING_LEASE_SEC
        stale_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE status='running' AND updated_at < ?",
            (cutoff,),
        ).fetchone()
        stale_cnt = stale_row["cnt"] if hasattr(stale_row, "keys") else (stale_row[0] if stale_row else 0)
        STALE_RUNNING_TASKS.set(stale_cnt)

        try:
            dlq_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM task_dead_letters WHERE replayed_task_id IS NULL"
            ).fetchone()
            dlq_cnt = dlq_row["cnt"] if hasattr(dlq_row, "keys") else (dlq_row[0] if dlq_row else 0)
            DEAD_LETTER_TASKS.set(dlq_cnt)
        except Exception:
            pass

        # Device counts by status — clear stale label combinations before repopulating
        ACTIVE_DEVICES.clear()
        dev_rows = conn.execute("SELECT status, os, COUNT(*) as cnt FROM devices WHERE revoked=0 GROUP BY status, os").fetchall()
        for r in dev_rows:
            st = r["status"] if hasattr(r, "keys") else r[0]
            os_name = (r["os"] if hasattr(r, "keys") else r[1]) or "unknown"
            cnt = r["cnt"] if hasattr(r, "keys") else r[2]
            ACTIVE_DEVICES.labels(status=st, os=os_name).set(cnt)

        from . import fleet_health as fleet_healthmod

        stale_hb = 0
        spool_pending_total = 0
        spool_oldest_max = 0.0
        try:
            rows_hb = conn.execute(
                "SELECT status, last_seen, spool_stats_json FROM devices WHERE revoked=0",
            ).fetchall()
        except Exception:
            rows_hb = conn.execute(
                "SELECT status, last_seen FROM devices WHERE revoked=0",
            ).fetchall()
        for d in rows_hb:
            last_seen = d["last_seen"] or 0
            if d["status"] == "online" and last_seen and (now_ts - last_seen) >= fleet_healthmod.STALE_HEARTBEAT_SEC:
                stale_hb += 1
            raw_spool = d["spool_stats_json"] if hasattr(d, "keys") and "spool_stats_json" in d.keys() else None
            if raw_spool:
                try:
                    sp = json.loads(raw_spool)
                    if isinstance(sp, dict):
                        spool_pending_total += int(sp.get("pending_count") or 0)
                        age = float(sp.get("oldest_pending_age_sec") or 0.0)
                        if age > spool_oldest_max:
                            spool_oldest_max = age
                except Exception:
                    pass
        STALE_HEARTBEAT_DEVICES.set(stale_hb)
        AGENT_SPOOL_PENDING.set(spool_pending_total)
        AGENT_SPOOL_MAX_AGE_SECONDS.set(spool_oldest_max)

        for phase, depth in _PHASE_DEPTH.items():
            ADVANCED_PHASE_DEPTH.labels(phase=phase).set(depth)
    except Exception:
        pass


def get_metrics_payload(conn: Any | None = None) -> tuple[bytes, str]:
    """Generate Prometheus exposition text format payload."""
    if conn is not None:
        update_fleet_gauges(conn)

    if _PROMETHEUS_AVAILABLE:
        return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
    else:
        uptime = time.time() - _START_TIME
        fallback = f"# AI Bridge Metrics (fallback)\naibridge_uptime_seconds {uptime:.2f}\n"
        return fallback.encode("utf-8"), CONTENT_TYPE_LATEST


def build_metrics_summary(conn: Any) -> dict[str, Any]:
    """Operator-facing KPI summary without requiring Grafana."""
    from . import db as dbmod
    from . import fleet_health as fleet_healthmod

    now_ts = dbmod.now()
    window = now_ts - 3600.0
    queued = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='queued'").fetchone()["c"]
    running = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='running'").fetchone()["c"]
    completed_1h = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE status='completed' AND updated_at>=?",
        (window,),
    ).fetchone()["c"]
    failed_1h = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE status IN ('failed','timed_out') AND updated_at>=?",
        (window,),
    ).fetchone()["c"]
    dur_row = conn.execute(
        "SELECT AVG(updated_at - created_at) AS avg_d FROM tasks "
        "WHERE status='completed' AND updated_at>=? AND updated_at>created_at",
        (window,),
    ).fetchone()
    avg_duration = float(dur_row["avg_d"]) if dur_row and dur_row["avg_d"] is not None else None
    dlq_pending = 0
    try:
        dlq_row = conn.execute("SELECT COUNT(*) AS c FROM task_dead_letters WHERE replayed_task_id IS NULL").fetchone()
        dlq_pending = int(dlq_row["c"] or 0) if dlq_row else 0
    except Exception:
        pass
    health = fleet_healthmod.summarize_fleet_health(conn)
    db_stats = conn.get_db_stats() if hasattr(conn, "get_db_stats") else {"dialect": getattr(conn, "dialect", "sqlite")}
    return {
        "ok": True,
        "ts": now_ts,
        "queue_depth": int(queued or 0),
        "running_tasks": int(running or 0),
        "completed_1h": int(completed_1h or 0),
        "failed_1h": int(failed_1h or 0),
        "dead_letter_pending": dlq_pending,
        "avg_task_duration_sec": round(avg_duration, 2) if avg_duration is not None else None,
        "online_devices": health.get("online_devices"),
        "stale_device_count": health.get("stale_device_count"),
        "unhealthy_streams": health.get("unhealthy_streams"),
        "db_stats": db_stats,
        "uptime_seconds": round(time.time() - _START_TIME, 1),
    }

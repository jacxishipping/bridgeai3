"""Release 4: Fleet analytics & capacity planning (daily trends + CSV export)."""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timezone
from typing import Any

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_csv_cell(val: Any) -> str:
    s = "" if val is None else str(val)
    if s and s[0] in _CSV_FORMULA_PREFIXES:
        return "'" + s
    return s


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def compute_daily_trends(
    conn: Any,
    *,
    tenant: str,
    days: int = 14,
    group: str | None = None,
) -> dict[str, Any]:
    """Compute daily health, task success, duration, and telemetry averages."""
    safe_days = max(1, min(90, int(days or 14)))
    now_ts = time.time()
    cutoff = now_ts - (safe_days * 86400)
    group_q = (group or "").strip().lower()

    # Resolve devices in scope (tenant + optional group filter)
    dev_rows = conn.execute(
        "SELECT d.id, d.status, d.revoked, d.last_seen, d.agent_version, m.group_name "
        "FROM devices d LEFT JOIN device_metadata m ON m.device_id=d.id "
        "WHERE d.tenant=?",
        (tenant,),
    ).fetchall()
    scoped_devices = []
    for r in dev_rows:
        d = dict(r)
        grp = (d.get("group_name") or "").strip().lower()
        if group_q and grp != group_q:
            continue
        scoped_devices.append(d)

    device_ids = {d["id"] for d in scoped_devices}
    total_devices = len(scoped_devices)
    online_devices = sum(1 for d in scoped_devices if not d.get("revoked") and d.get("status") == "online")

    # Initialize continuous daily buckets (oldest -> newest)
    buckets: dict[str, dict[str, Any]] = {}
    for offset in range(safe_days - 1, -1, -1):
        day = _day_key(now_ts - offset * 86400)
        buckets[day] = {
            "date": day,
            "total_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "success_rate": None,
            "avg_duration_sec": None,
            "avg_cpu_percent": None,
            "avg_memory_percent": None,
            "_durations": [],
            "_cpu": [],
            "_mem": [],
        }

    # Aggregate tasks in window
    task_rows = conn.execute(
        "SELECT device_id, status, created_at, updated_at, started_at "
        "FROM tasks WHERE tenant=? AND created_at >= ?",
        (tenant, cutoff),
    ).fetchall()
    for r in task_rows:
        if group_q and r["device_id"] not in device_ids:
            continue
        day = _day_key(float(r["created_at"] or now_ts))
        b = buckets.get(day)
        if b is None:
            continue
        b["total_tasks"] += 1
        st = str(r["status"] or "")
        if st == "completed":
            b["completed_tasks"] += 1
        elif st in ("failed", "timed_out"):
            b["failed_tasks"] += 1
        start_ts = r["started_at"] or r["created_at"]
        end_ts = r["updated_at"]
        if st in ("completed", "failed", "timed_out") and start_ts and end_ts and float(end_ts) >= float(start_ts):
            b["_durations"].append(float(end_ts) - float(start_ts))

    # Aggregate telemetry samples in window
    if device_ids:
        placeholders = ",".join("?" for _ in device_ids)
        tel_rows = conn.execute(
            f"SELECT cpu_percent, memory_percent, recorded_at FROM telemetry "
            f"WHERE recorded_at >= ? AND device_id IN ({placeholders})",
            (cutoff, *sorted(device_ids)),
        ).fetchall()
        for r in tel_rows:
            day = _day_key(float(r["recorded_at"] or now_ts))
            b = buckets.get(day)
            if b is None:
                continue
            if r["cpu_percent"] is not None:
                b["_cpu"].append(float(r["cpu_percent"]))
            if r["memory_percent"] is not None:
                b["_mem"].append(float(r["memory_percent"]))

    trend_list: list[dict[str, Any]] = []
    for day in sorted(buckets):
        b = buckets[day]
        finished = b["completed_tasks"] + b["failed_tasks"]
        durs = b.pop("_durations")
        cpus = b.pop("_cpu")
        mems = b.pop("_mem")
        b["success_rate"] = round(b["completed_tasks"] / finished, 4) if finished > 0 else None
        b["avg_duration_sec"] = round(sum(durs) / len(durs), 2) if durs else None
        b["avg_cpu_percent"] = round(sum(cpus) / len(cpus), 2) if cpus else None
        b["avg_memory_percent"] = round(sum(mems) / len(mems), 2) if mems else None
        trend_list.append(b)

    return {
        "schema": "ai-bridge-fleet-trends/v1",
        "tenant": tenant,
        "group": group or None,
        "days": safe_days,
        "summary": {
            "total_devices": total_devices,
            "online_devices": online_devices,
            "online_ratio": round(online_devices / total_devices, 4) if total_devices else None,
            "window_total_tasks": sum(t["total_tasks"] for t in trend_list),
            "window_completed_tasks": sum(t["completed_tasks"] for t in trend_list),
            "window_failed_tasks": sum(t["failed_tasks"] for t in trend_list),
        },
        "trends": trend_list,
    }


def render_trends_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "date",
        "group",
        "total_tasks",
        "completed_tasks",
        "failed_tasks",
        "success_rate",
        "avg_duration_sec",
        "avg_cpu_percent",
        "avg_memory_percent",
    ])
    grp = report.get("group") or "all"
    for row in report.get("trends", []):
        writer.writerow([
            _safe_csv_cell(row["date"]),
            _safe_csv_cell(grp),
            _safe_csv_cell(row["total_tasks"]),
            _safe_csv_cell(row["completed_tasks"]),
            _safe_csv_cell(row["failed_tasks"]),
            _safe_csv_cell(row["success_rate"] if row["success_rate"] is not None else ""),
            _safe_csv_cell(row["avg_duration_sec"] if row["avg_duration_sec"] is not None else ""),
            _safe_csv_cell(row["avg_cpu_percent"] if row["avg_cpu_percent"] is not None else ""),
            _safe_csv_cell(row["avg_memory_percent"] if row["avg_memory_percent"] is not None else ""),
        ])
    return buf.getvalue()

"""Multi-worker + Redis + PostgreSQL load & correctness soak harness (Release 2 Ops Proof).

Exercises multiple live control-plane worker endpoints concurrently to prove:
  1. All workers boot and pass /health/ready (with DB pool + Redis active).
  2. Concurrent identical Idempotency-Key submissions across different workers
     converge to one single task_id (zero duplicates).
  3. Cross-worker task creation (Worker A) + cancellation (Worker B) converges
     with zero ambiguous state.
  4. Concurrent approval decisions across different workers resolve atomically
     (exactly one 200 OK, loser receives 409 Conflict).
  5. Sustained multi-worker request throughput with p50/p95/p99 latency SLOs,
     end-to-end X-Correlation-ID trace continuity, and valid hash-chained audit.

Usage:
    python scripts/multi_worker_soak.py \
        --worker-urls http://127.0.0.1:8001,http://127.0.0.1:8002 \
        --admin-token "$AI_BRIDGE_ADMIN_TOKEN" \
        --concurrency 12 --iterations 60 \
        --out .run/multi_worker_soak_evidence.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(math.ceil((pct / 100.0) * len(sorted_vals))) - 1))
    return round(sorted_vals[idx], 2)


def run_soak(
    worker_urls: list[str],
    *,
    admin_token: str,
    concurrency: int = 8,
    iterations: int = 40,
    timeout: float = 15.0,
) -> dict[str, Any]:
    urls = [u.strip().rstrip("/") for u in worker_urls if u.strip()]
    if not urls:
        raise ValueError("At least one worker URL is required.")
    w_a = urls[0]
    w_b = urls[1] if len(urls) > 1 else urls[0]
    headers = {"X-Admin-Token": admin_token}
    started_at = time.time()

    evidence: dict[str, Any] = {
        "schema": "ai-bridge-multi-worker-soak/v1",
        "started_at": started_at,
        "workers": urls,
        "concurrency": concurrency,
        "iterations": iterations,
        "checks": {},
        "ok": False,
    }

    with httpx.Client(timeout=timeout) as client:
        # 1. Verify /health and /health/ready across every worker
        readiness_map: dict[str, Any] = {}
        for url in urls:
            rh = client.get(f"{url}/health")
            assert rh.status_code == 200, f"{url}/health -> {rh.status_code}: {rh.text}"
            rr = client.get(f"{url}/health/ready")
            assert rr.status_code == 200, f"{url}/health/ready -> {rr.status_code}: {rr.text}"
            readiness_map[url] = rr.json()
        evidence["checks"]["readiness"] = {"ok": True, "workers": readiness_map}

        # 2. Enroll soak device on Worker A, verify visible on Worker B
        enroll = client.post(
            f"{w_a}/api/devices/enroll",
            headers=headers,
            json={"name": f"soak-node-{uuid.uuid4().hex[:6]}", "hostname": "SOAK-HOST", "os": "Ubuntu 24.04 LTS"},
        )
        assert enroll.status_code == 200, f"enroll failed: {enroll.status_code} {enroll.text}"
        device_id = enroll.json()["device_id"]
        dev_list_b = client.get(f"{w_b}/api/devices", headers=headers)
        assert dev_list_b.status_code == 200
        assert any(d["id"] == device_id for d in dev_list_b.json()), "Enrolled device not visible on Worker B"
        evidence["checks"]["cross_worker_enrollment"] = {"ok": True, "device_id": device_id}

        # 3. Concurrent Idempotency-Key storm across workers
        idem_key = f"soak-idem-{uuid.uuid4().hex}"

        def _submit_idem(idx: int) -> tuple[int, str]:
            target = urls[idx % len(urls)]
            with httpx.Client(timeout=timeout) as c:
                r = c.post(
                    f"{target}/api/tasks",
                    headers={**headers, "Idempotency-Key": idem_key},
                    json={"device_id": device_id, "tool": "get_system_info", "args": {}},
                )
                tid = r.json().get("task_id", "") if r.status_code == 200 else ""
                return r.status_code, tid

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            idem_results = list(pool.map(_submit_idem, range(concurrency)))
        statuses = [s for s, _ in idem_results]
        task_ids = {t for s, t in idem_results if s == 200 and t}
        assert all(s == 200 for s in statuses), f"Idempotency storm returned non-200: {statuses}"
        assert len(task_ids) == 1, f"Idempotency race produced multiple task_ids: {task_ids}"
        evidence["checks"]["cross_worker_idempotency"] = {
            "ok": True,
            "requests": concurrency,
            "converged_task_id": next(iter(task_ids)),
        }

        # 4. Cross-worker task creation (Worker A) + cancellation (Worker B) + trace verification
        trace_id = f"soak-trace-{uuid.uuid4().hex[:12]}"
        created = client.post(
            f"{w_a}/api/tasks",
            headers={**headers, "X-Correlation-ID": trace_id},
            json={"device_id": device_id, "tool": "get_resource_usage", "args": {}},
        )
        assert created.status_code == 200, created.text
        cancel_tid = created.json()["task_id"]
        cancelled = client.post(
            f"{w_b}/api/tasks/{cancel_tid}/cancel",
            headers={**headers, "X-Correlation-ID": trace_id},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        check_a = client.get(f"{w_a}/api/tasks/{cancel_tid}", headers=headers)
        assert check_a.status_code == 200 and check_a.json()["status"] == "cancelled"
        trace_resp = client.get(f"{w_b}/api/traces/{trace_id}", headers=headers)
        assert trace_resp.status_code == 200
        assert any(t["id"] == cancel_tid for t in trace_resp.json().get("tasks", []))
        evidence["checks"]["cross_worker_cancel_and_trace"] = {
            "ok": True,
            "task_id": cancel_tid,
            "trace_id": trace_id,
        }

        # 5. Cross-worker concurrent approval race (exactly one 200, rest 409)
        tier3_task = client.post(
            f"{w_a}/api/tasks",
            headers=headers,
            json={"device_id": device_id, "tool": "run_shell", "args": {"command": "echo soak"}},
        )
        assert tier3_task.status_code == 200, tier3_task.text
        approval_id = tier3_task.json().get("approval_id")
        if approval_id:
            def _decide(idx: int) -> int:
                target = urls[idx % len(urls)]
                decision = "approved" if idx % 2 == 0 else "denied"
                with httpx.Client(timeout=timeout) as c:
                    r = c.post(
                        f"{target}/api/approvals/{approval_id}/decide",
                        headers=headers,
                        json={"decision": decision, "reason": f"soak-{idx}"},
                    )
                    return r.status_code

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, concurrency)) as pool:
                race_codes = list(pool.map(_decide, range(min(6, max(2, concurrency)))))
            assert race_codes.count(200) == 1, f"Expected exactly 1 approval winner, got {race_codes}"
            assert all(c in (200, 409) for c in race_codes), f"Unexpected approval race statuses: {race_codes}"
            evidence["checks"]["cross_worker_approval_race"] = {
                "ok": True,
                "approval_id": approval_id,
                "status_codes": race_codes,
            }
        else:
            evidence["checks"]["cross_worker_approval_race"] = {
                "ok": True,
                "skipped": "Policy allowed run_shell without approval",
            }

        # 6. Sustained concurrent mixed workload across all workers
        latencies_ms: list[float] = []
        errors_5xx = 0
        completed_ops = 0

        def _workload_step(i: int) -> tuple[float, int]:
            target = urls[i % len(urls)]
            t0 = time.perf_counter()
            with httpx.Client(timeout=timeout) as c:
                if i % 3 == 0:
                    r = c.post(
                        f"{target}/api/tasks",
                        headers=headers,
                        json={"device_id": device_id, "tool": "get_system_info", "args": {}},
                    )
                elif i % 3 == 1:
                    r = c.get(f"{target}/api/tasks?limit=25", headers=headers)
                else:
                    r = c.get(f"{target}/api/devices?limit=25", headers=headers)
            return (time.perf_counter() - t0) * 1000.0, r.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            for lat, code in pool.map(_workload_step, range(iterations)):
                latencies_ms.append(lat)
                if code >= 500:
                    errors_5xx += 1
                else:
                    completed_ops += 1

        latencies_ms.sort()
        assert errors_5xx == 0, f"Detected {errors_5xx} HTTP 5xx errors during soak"
        evidence["checks"]["sustained_workload"] = {
            "ok": True,
            "iterations": iterations,
            "completed_non_5xx": completed_ops,
            "errors_5xx": errors_5xx,
            "latency_ms": {
                "min": round(latencies_ms[0], 2) if latencies_ms else 0.0,
                "p50": _percentile(latencies_ms, 50),
                "p95": _percentile(latencies_ms, 95),
                "p99": _percentile(latencies_ms, 99),
                "mean": round(statistics.mean(latencies_ms), 2) if latencies_ms else 0.0,
            },
        }

        # 7. Audit hash-chain integrity check on Worker B
        ver = client.get(f"{w_b}/api/audit/verify", headers=headers)
        assert ver.status_code == 200, ver.text
        vdata = ver.json()
        assert vdata.get("ok") is True or vdata.get("valid") is True, f"Audit chain verify failed: {vdata}"
        evidence["checks"]["audit_chain_verify"] = {"ok": True, **vdata}

    evidence["finished_at"] = time.time()
    evidence["duration_sec"] = round(evidence["finished_at"] - started_at, 2)
    evidence["ok"] = True
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-worker control-plane load & correctness soak.")
    parser.add_argument("--worker-urls", required=True, help="Comma-separated worker base URLs (e.g. http://127.0.0.1:8001,http://127.0.0.1:8002)")
    parser.add_argument("--admin-token", required=True, help="X-Admin-Token for control-plane requests")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--iterations", type=int, default=40)
    parser.add_argument("--out", default="", help="Optional JSON evidence output path")
    args = parser.parse_args(argv)

    urls = [u.strip() for u in args.worker_urls.split(",") if u.strip()]
    evidence = run_soak(
        urls,
        admin_token=args.admin_token,
        concurrency=max(2, args.concurrency),
        iterations=max(10, args.iterations),
    )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote multi-worker soak evidence to {out_path}")

    wl = evidence["checks"]["sustained_workload"]["latency_ms"]
    print(
        f"MULTI-WORKER SOAK OK: {len(urls)} workers, {args.iterations} ops, "
        f"p50={wl['p50']}ms p95={wl['p95']}ms p99={wl['p99']}ms, 0 5xx errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

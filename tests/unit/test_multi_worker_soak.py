"""End-to-end verification of scripts/multi_worker_soak.py against 2 live local Uvicorn workers."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
SOAK_SCRIPT = REPO_ROOT / "scripts" / "multi_worker_soak.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise TimeoutError(f"Worker at {url} failed to become healthy within {timeout}s")


def test_multi_worker_soak_script_two_live_workers(tmp_path: Path):
    db_file = tmp_path / "shared_soak.db"
    evidence_file = tmp_path / "soak_evidence.json"
    token = "unit-soak-admin-token"
    p1 = _free_port()
    p2 = _free_port()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["AI_BRIDGE_ADMIN_TOKEN"] = token
    env["AI_BRIDGE_CSRF_SECRET"] = "unit-soak-csrf-secret"
    env["AI_BRIDGE_DATA_DIR"] = str(tmp_path)

    # Pre-initialize shared database schema once before spawning parallel workers
    from ai_bridge.control import db as dbmod

    init_conn = dbmod.connect(db_path=str(db_file))
    init_conn.close()

    worker_code = (
        "import sys, uvicorn; "
        "from ai_bridge.control.app import create_app; "
        "app = create_app(db_path=sys.argv[1], admin_token=sys.argv[2]); "
        "uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[3]), log_level='error')"
    )

    procs = [
        subprocess.Popen([sys.executable, "-c", worker_code, str(db_file), token, str(p1)], env=env),
        subprocess.Popen([sys.executable, "-c", worker_code, str(db_file), token, str(p2)], env=env),
    ]
    try:
        u1 = f"http://127.0.0.1:{p1}"
        u2 = f"http://127.0.0.1:{p2}"
        _wait_ready(u1)
        _wait_ready(u2)

        proc = subprocess.run(
            [
                sys.executable,
                str(SOAK_SCRIPT),
                "--worker-urls",
                f"{u1},{u2}",
                "--admin-token",
                token,
                "--concurrency",
                "6",
                "--iterations",
                "24",
                "--out",
                str(evidence_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "MULTI-WORKER SOAK OK" in proc.stdout
        assert evidence_file.is_file()
        data = json.loads(evidence_file.read_text(encoding="utf-8"))
        assert data["ok"] is True
        assert data["checks"]["cross_worker_idempotency"]["ok"] is True
        assert data["checks"]["cross_worker_cancel_and_trace"]["ok"] is True
        assert data["checks"]["cross_worker_approval_race"]["ok"] is True
        assert data["checks"]["sustained_workload"]["errors_5xx"] == 0
        assert data["checks"]["audit_chain_verify"]["ok"] is True
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()

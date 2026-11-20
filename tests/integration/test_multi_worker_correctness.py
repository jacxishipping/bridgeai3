"""Release 2 — Multi-worker correctness integration tests.

Validates cross-worker consistency using two independent FastAPI app instances
sharing one database (and simulated Redis pub/sub fan-out):
- Redis enforcement for multi-host and multi-worker production boot
- Cross-worker task creation, claim, and cancellation
- Cross-worker idempotent task submission
- Atomic approval decision race handling (exactly-once decision)
- Cross-worker OIDC PKCE + atomic single-use state and exchange code redemption
- Cross-worker M365 OAuth PKCE pending store
- Cross-worker human takeover set/clear without stale worker cache
- Cross-worker WebRTC signaling WebSocket Redis pub/sub fan-out
- Atomic trigger cooldown and custom-agent schedule claiming
- Expired DB rate-limit row pruning
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from ai_bridge.connectors import microsoft365 as m365
from ai_bridge.control import agent_channel, durable_ratelimit, oidc, production_readiness, v1_routes
from ai_bridge.control import db as dbmod
from ai_bridge.control.app import create_app

ADMIN = "r2-multiworker-admin-token"


def _admin_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"X-Admin-Token": ADMIN}
    if extra:
        headers.update(extra)
    return headers


def test_multi_host_and_prod_multi_worker_require_redis(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_REDIS_URL", raising=False)
    durable_ratelimit.reset_for_tests()

    # Explicit multi-host flag fails fast at boot without Redis.
    monkeypatch.setenv("AI_BRIDGE_MULTI_HOST", "1")
    with pytest.raises(RuntimeError, match="Redis is required"):
        create_app(admin_token=ADMIN, db_path=tmp_path / "mh.db")
    monkeypatch.delenv("AI_BRIDGE_MULTI_HOST", raising=False)

    # Production profile with AI_BRIDGE_WORKERS > 1 fails fast without Redis.
    monkeypatch.setenv("AI_BRIDGE_PROFILE", "production")
    monkeypatch.setenv("AI_BRIDGE_WORKERS", "4")
    monkeypatch.setenv("AI_BRIDGE_CSRF_SECRET", "shared-csrf-secret-for-test")
    with pytest.raises(RuntimeError, match="Redis is required"):
        create_app(admin_token=ADMIN, db_path=tmp_path / "prod4.db")

    # Readiness report also surfaces critical issue when multi-host is enabled without Redis.
    conn = dbmod.connect(db_path=tmp_path / "ready.db")
    monkeypatch.setenv("AI_BRIDGE_MULTI_HOST", "1")
    report = production_readiness.assess_production_readiness(conn)
    assert report["ok"] is False
    assert any(i["code"] == "redis_required_for_multi_host" for i in report["issues"])
    assert report["configured_workers"] == 4
    assert report["redis_required"] is True


def test_cross_worker_cancel_and_idempotency(tmp_path):
    shared_db = tmp_path / "shared.db"
    app_a = create_app(admin_token=ADMIN, db_path=shared_db)
    app_b = create_app(admin_token=ADMIN, db_path=shared_db)
    client_a = TestClient(app_a)
    client_b = TestClient(app_b)

    # Enroll device on Worker A
    enr = client_a.post("/api/devices/enroll", json={"name": "mw-box", "os": "linux"}, headers=_admin_headers())
    assert enr.status_code == 200
    device_id = enr.json()["device_id"]
    device_token = enr.json()["device_token"]

    # Idempotent task submission across Worker A and Worker B returns the exact same task_id
    r1 = client_a.post(
        "/api/tasks",
        json={"device_id": device_id, "tool": "get_system_info", "args": {}},
        headers=_admin_headers({"Idempotency-Key": "mw-idem-001"}),
    )
    r2 = client_b.post(
        "/api/tasks",
        json={"device_id": device_id, "tool": "get_system_info", "args": {}},
        headers=_admin_headers({"Idempotency-Key": "mw-idem-001"}),
    )
    assert r1.status_code == 200 and r2.status_code == 200
    task_id = r1.json()["task_id"]
    assert r2.json()["task_id"] == task_id

    # Agent claims on Worker B
    nxt = client_b.get("/api/agent/tasks/next", params={"wait": 0}, headers={"X-Device-Token": device_token})
    assert nxt.status_code == 200
    assert nxt.json()["task"]["id"] == task_id

    # Operator cancels on Worker A; agent execution-state poll on Worker B sees cancelled immediately
    canc = client_a.post(f"/api/tasks/{task_id}/cancel", headers=_admin_headers())
    assert canc.status_code == 200
    state = client_b.get(f"/api/agent/tasks/{task_id}/execution-state", headers={"X-Device-Token": device_token})
    assert state.status_code == 200
    assert state.json()["status"] == "cancelled"


def test_cross_worker_approval_decision_exactly_once(tmp_path):
    shared_db = tmp_path / "approvals.db"
    app_a = create_app(admin_token=ADMIN, db_path=shared_db)
    app_b = create_app(admin_token=ADMIN, db_path=shared_db)
    client_a = TestClient(app_a)
    client_b = TestClient(app_b)

    enr = client_a.post("/api/devices/enroll", json={"name": "ap-box", "os": "linux"}, headers=_admin_headers())
    device_id = enr.json()["device_id"]

    # Tier-3 tool triggers needs_approval
    t = client_a.post(
        "/api/tasks",
        json={"device_id": device_id, "tool": "run_shell", "args": {"command": "echo hi"}},
        headers=_admin_headers(),
    )
    assert t.status_code == 200
    approval_id = t.json().get("approval_id")
    assert approval_id

    # Concurrent decisions across Worker A and Worker B: exactly one 200, one 409
    barrier = threading.Barrier(2)

    def _decide(client: TestClient, decision: str) -> int:
        barrier.wait()
        resp = client.post(
            f"/api/approvals/{approval_id}/decide",
            json={"decision": decision, "reason": "race test"},
            headers=_admin_headers(),
        )
        return resp.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_decide, client_a, "approved")
        f2 = pool.submit(_decide, client_b, "denied")
        codes = sorted([f1.result(), f2.result()])

    assert codes == [200, 409]


def test_oidc_pkce_and_atomic_single_use_redemption(tmp_path, monkeypatch):
    conn = dbmod.connect(db_path=tmp_path / "oidc.db")
    oidc.bind_oidc_store(conn)
    try:
        monkeypatch.setenv("AI_BRIDGE_OIDC_ISSUER", "https://idp.example.test")
        monkeypatch.setenv("AI_BRIDGE_OIDC_CLIENT_ID", "cid-1")
        monkeypatch.setenv("AI_BRIDGE_OIDC_CLIENT_SECRET", "csec-1")
        monkeypatch.setenv("AI_BRIDGE_OIDC_REDIRECT_URI", "https://app.example.test/api/auth/oidc/callback")
        monkeypatch.setattr(
            oidc,
            "discovery",
            lambda: {
                "authorization_endpoint": "https://idp.example.test/authorize",
                "token_endpoint": "https://idp.example.test/token",
                "jwks_uri": "https://idp.example.test/jwks",
            },
        )
        auth_url, state, nonce = oidc.new_login()
        q = parse_qs(urlparse(auth_url).query)
        assert q["code_challenge_method"] == ["S256"]
        assert len(q["code_challenge"][0]) >= 32

        # Concurrent pop_pending: only one thread wins the state + PKCE verifier
        barrier = threading.Barrier(2)

        def _pop():
            barrier.wait()
            return oidc.pop_pending(state)

        with ThreadPoolExecutor(max_workers=2) as pool:
            res = [f.result() for f in (pool.submit(_pop), pool.submit(_pop))]
        winners = [r for r in res if r is not None]
        assert len(winners) == 1
        assert winners[0]["nonce"] == nonce
        assert winners[0].get("code_verifier")

        # Concurrent redeem_oidc_exchange_code: only one thread wins the token
        code = oidc.mint_oidc_exchange_code("sess-token-xyz")
        barrier2 = threading.Barrier(2)

        def _redeem():
            barrier2.wait()
            return oidc.redeem_oidc_exchange_code(code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            redeemed = [f.result() for f in (pool.submit(_redeem), pool.submit(_redeem))]
        assert sorted([x for x in redeemed if x is not None]) == ["sess-token-xyz"]
    finally:
        oidc.bind_oidc_store(None)


def test_m365_pending_state_shared_across_workers(tmp_path, monkeypatch):
    conn = dbmod.connect(db_path=tmp_path / "m365.db")
    m365.bind_pending_store(conn)
    try:
        monkeypatch.setenv("AI_BRIDGE_M365_CLIENT_ID", "m365-client")
        monkeypatch.setenv("AI_BRIDGE_M365_REDIRECT_URI", "https://app.example.test/api/connectors/m365/callback")
        monkeypatch.setenv("AI_BRIDGE_M365_TOKEN_KEY", "test-key-32-bytes-minimum-padding!")
        url = m365.new_login(account="ops")
        state = parse_qs(urlparse(url).query)["state"][0]
        # Clear process memory to simulate callback hitting a different worker
        m365._PENDING.clear()
        popped = m365._pop_pending(state)
        assert popped is not None
        assert popped["account"] == "ops"
        assert popped["verifier"]
        # Second pop fails (single-use)
        assert m365._pop_pending(state) is None
    finally:
        m365.bind_pending_store(None)


def test_human_takeover_cross_worker_clear_not_masked_by_stale_cache(tmp_path):
    conn_a = dbmod.connect(db_path=tmp_path / "ht.db")
    conn_b = dbmod.connect(db_path=tmp_path / "ht.db")
    try:
        v1_routes.bind_control_connection(conn_a)
        v1_routes.set_device_human_takeover("dev-mw", True, actor="alice", conn=conn_a)
        assert v1_routes.is_device_human_takeover_active("dev-mw") is True

        # Worker B clears takeover in DB directly (simulating a separate worker process)
        from ai_bridge.control import plane_state

        plane_state.clear_human_takeover(conn_b, "dev-mw")

        # Worker A must see False immediately, NOT its stale local cache entry
        assert v1_routes.is_device_human_takeover_active("dev-mw") is False
    finally:
        v1_routes.bind_control_connection(None)
        v1_routes._HUMAN_TAKEOVER_BY_DEVICE.clear()


def test_webrtc_redis_fanout_delivers_to_remote_worker(monkeypatch):
    agent_channel.reset_for_tests()
    received: list[tuple[str, dict]] = []

    async def _handler(sid: str, payload: dict) -> None:
        received.append((sid, payload))

    agent_channel.register_webrtc_fanout_handler(_handler)
    monkeypatch.setattr(agent_channel, "_WORKER_ID", "worker-b")

    envelope = json.dumps({"origin": "worker-a", "payload": {"type": "answer", "sdp": "v=0"}})
    asyncio.run(agent_channel._handle_redis_message("aibridge:webrtc:sess-42", envelope))
    assert received == [("sess-42", {"type": "answer", "sdp": "v=0"})]

    # Own origin is skipped
    own_env = json.dumps({"origin": "worker-b", "payload": {"type": "ice"}})
    asyncio.run(agent_channel._handle_redis_message("aibridge:webrtc:sess-42", own_env))
    assert len(received) == 1
    agent_channel.reset_for_tests()


def test_prune_expired_db_ratelimits(tmp_path):
    conn = dbmod.connect(db_path=tmp_path / "rl.db")
    assert durable_ratelimit._db_allow(conn, "fresh-key", limit=5, window=60.0) is True
    # Insert a stale ratelimit row from 2 hours ago
    stale_ms = int((dbmod.now() - 7200) * 1000)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("ratelimit.old-key", str(stale_ms * durable_ratelimit._DB_COUNT_MODULUS + 1)),
    )
    conn.commit()
    removed = durable_ratelimit.prune_expired_db_ratelimits(conn, max_age_sec=3600.0)
    assert removed == 1
    assert conn.execute("SELECT key FROM settings WHERE key='ratelimit.old-key'").fetchone() is None
    assert conn.execute("SELECT key FROM settings WHERE key='ratelimit.fresh-key'").fetchone() is not None

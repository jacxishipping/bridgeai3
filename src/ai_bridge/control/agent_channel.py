"""Persistent agent wake/cancel side-channel (WebSocket).

HTTP claim (`GET /api/agent/tasks/next`) remains the source of truth for leases.
Connected agents receive push hints so they can claim or abort sooner.

Fan-out (Wave 2A / DIST-03): the in-process subscriber registry only
reaches agents connected to *this* worker. Every notify_* therefore also
publishes to Redis pub/sub (channel ``aibridge:agent:<device_id>``), and
:func:`start_redis_fanout` runs a background subscriber (pattern
``aibridge:agent:*``) that forwards messages received from other workers
to this worker's local subscribers via :func:`push_to_device`. Redis is
purely a hint channel: if it is absent or unreachable, notification
degrades to in-process-only push plus the agents' HTTP long-poll, and no
notify_* call ever raises because of Redis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("ai_bridge.agent_channel")

_lock = threading.Lock()
# device_id -> set of WebSocket
_subscribers: dict[str, set[WebSocket]] = {}
_loop: asyncio.AbstractEventLoop | None = None

# --- Redis fan-out -------------------------------------------------------
_CHANNEL_PREFIX = "aibridge:agent:"
_BROADCAST_CHANNEL = _CHANNEL_PREFIX + "broadcast"
_WEBRTC_CHANNEL_PREFIX = "aibridge:webrtc:"
_WORKER_ID = uuid.uuid4().hex[:12]
_sync_redis = None
_sync_redis_failed_at = 0.0
_SYNC_REDIS_FAILED_COOLDOWN_SEC = 10.0
_fanout_task: asyncio.Task | None = None
_REDIS_UNAVAILABLE_WARNED = False
_webrtc_fanout_handlers: list[Any] = []

# PERF-06: per-device wake events so the next_task long-poll can await a
# notification instead of spinning every 0.15 s with a DB check per cycle.
# A wake is a hint only — the HTTP claim endpoint stays the source of
# truth, so a lost wake is harmless (the poll re-checks the DB).
_events: dict[str, asyncio.Event] = {}

# DIST-08: last progress-report time per (device_id, task_id), to throttle
# agent progress heartbeats to at most one DB write per minute per task.
_PROGRESS_THROTTLE_SEC = 60.0
_progress_last: dict[tuple[str, str], float] = {}


def _note_redis_unavailable(exc: Exception) -> None:
    """Warn once per process that Redis fan-out is down (Wave 3A).

    A Redis outage must never be silent: without this warning an
    operator cannot tell per-worker fan-out from cross-worker fan-out.
    Notification still degrades to in-process push + HTTP long-poll.
    """
    global _REDIS_UNAVAILABLE_WARNED
    if not _REDIS_UNAVAILABLE_WARNED:
        _REDIS_UNAVAILABLE_WARNED = True
        log.warning(
            "agent_channel: Redis fan-out unavailable (%s); WS push is "
            "per-worker only until Redis recovers",
            exc,
        )


def _signal_event(device_id: str) -> None:
    """Wake any long-poll waiter for ``device_id`` (PERF-06)."""
    event = _events.get(device_id)
    if event is not None and not event.is_set():
        event.set()


async def wait_for_device_notification(device_id: str, timeout: float) -> bool:
    """Await a wake for ``device_id`` (PERF-06).

    Returns True when a notify_* / Redis fan-out message arrived before
    ``timeout`` seconds, False on timeout. The event is consumed
    (cleared) on success so the next call blocks again. Callers must
    re-check the task queue after either outcome — this is a hint, not
    a delivery guarantee.
    """
    event = _events.get(device_id)
    if event is None:
        event = asyncio.Event()
        _events[device_id] = event
    elif event.is_set():
        event.clear()
        return True
    try:
        await asyncio.wait_for(event.wait(), timeout=max(0.0, float(timeout)))
    except (asyncio.TimeoutError, TimeoutError):
        return False
    event.clear()
    return True


def _redis_url() -> str:
    return (os.environ.get("REDIS_URL") or os.environ.get("AI_BRIDGE_REDIS_URL") or "").strip()


def _get_sync_redis():
    """Cached sync Redis client, or None when unconfigured/unreachable."""
    global _sync_redis, _sync_redis_failed_at
    url = _redis_url()
    if not url:
        return None
    if _sync_redis is not None:
        return _sync_redis
    if time.time() - _sync_redis_failed_at < _SYNC_REDIS_FAILED_COOLDOWN_SEC:
        return None
    try:
        import redis

        client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        client.ping()
        _sync_redis = client
        _sync_redis_failed_at = 0.0
        return _sync_redis
    except Exception as exc:
        _sync_redis = None
        _sync_redis_failed_at = time.time()
        _note_redis_unavailable(exc)
        log.debug("agent_channel: Redis publish unavailable (%s)", exc)
        return None


def _channel_for(device_id: str) -> str:
    return _CHANNEL_PREFIX + device_id


def register_webrtc_fanout_handler(handler: Any) -> None:
    """Register an async callback ``handler(session_id, payload)`` for cross-worker WebRTC WS fan-out."""
    with _lock:
        if handler not in _webrtc_fanout_handlers:
            _webrtc_fanout_handlers.append(handler)


def publish_webrtc_event(session_id: str, payload: dict[str, Any]) -> bool:
    """Publish a WebRTC signaling event to Redis pub/sub for other workers."""
    global _sync_redis, _sync_redis_failed_at
    try:
        client = _get_sync_redis()
        if client is None:
            return False
        envelope = json.dumps({"origin": _WORKER_ID, "payload": payload})
        client.publish(_WEBRTC_CHANNEL_PREFIX + session_id, envelope)
        return True
    except Exception as exc:
        _sync_redis = None
        _sync_redis_failed_at = time.time()
        _note_redis_unavailable(exc)
        log.debug("agent_channel: Redis WebRTC publish failed (%s)", exc)
        return False


def publish_redis(device_id: str, payload: dict[str, Any]) -> bool:
    """Publish ``payload`` to the Redis fan-out channel for ``device_id``.

    No-op (returns False) when Redis is unconfigured or unreachable.
    Never raises. The envelope carries this worker's id so the local
    fan-out subscriber can skip messages this worker published itself
    (it already pushed those to local sockets directly).
    """
    global _sync_redis, _sync_redis_failed_at
    try:
        client = _get_sync_redis()
        if client is None:
            return False
        envelope = json.dumps({"origin": _WORKER_ID, "payload": payload})
        client.publish(_channel_for(device_id), envelope)
        return True
    except Exception as exc:
        _sync_redis = None
        _sync_redis_failed_at = time.time()
        _note_redis_unavailable(exc)
        log.debug("agent_channel: Redis publish failed (%s)", exc)
        return False


async def _handle_redis_message(channel: str, data: str) -> None:
    """Forward one fan-out message to local subscribers (if any)."""
    try:
        msg = json.loads(data)
    except Exception:
        return
    if isinstance(msg, dict) and "payload" in msg:
        if msg.get("origin") == _WORKER_ID:
            return  # published by this worker; already pushed locally
        payload = msg["payload"]
    elif isinstance(msg, dict):
        payload = msg  # tolerate a bare payload published by another client
    else:
        return
    if not isinstance(payload, dict):
        return
    if channel.startswith(_WEBRTC_CHANNEL_PREFIX):
        session_id = channel[len(_WEBRTC_CHANNEL_PREFIX):]
        if not session_id:
            return
        with _lock:
            handlers = list(_webrtc_fanout_handlers)
        for handler in handlers:
            try:
                res = handler(session_id, payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        return
    device_id = channel[len(_CHANNEL_PREFIX):] if channel.startswith(_CHANNEL_PREFIX) else ""
    if not device_id:
        return
    if device_id == "broadcast":
        with _lock:
            targets = list(_subscribers.keys())
        for target in targets:
            await push_to_device(target, payload)
        return
    await push_to_device(device_id, payload)


async def _fanout_main() -> None:
    """Subscribe to aibridge:agent:* and aibridge:webrtc:* and forward to local subscribers.

    Reconnects with bounded exponential backoff if Redis drops the connection so
    cross-worker fan-out recovers automatically after Redis restarts.
    """
    url = _redis_url()
    backoff = 1.0
    max_backoff = 30.0
    while True:
        client = None
        pubsub = None
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=2)
            await client.ping()
            pubsub = client.pubsub()
            await pubsub.psubscribe(_CHANNEL_PREFIX + "*")
            await pubsub.psubscribe(_WEBRTC_CHANNEL_PREFIX + "*")
            backoff = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                await _handle_redis_message(
                    str(message.get("channel", "")), str(message.get("data", ""))
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _note_redis_unavailable(exc)
            log.warning("agent_channel: Redis fan-out unavailable (%s); retrying in %.1fs", exc, backoff)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
        if not _redis_url():
            return
        await asyncio.sleep(backoff)
        backoff = min(max_backoff, backoff * 2.0)


def start_redis_fanout(loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Task | None:
    """Start the background Redis fan-out subscriber.

    Returns the background task, or None when no Redis URL is configured
    (or a fan-out task is already running, in which case the existing one
    is returned). When Redis is unreachable the task starts, logs a
    warning, and exits — notification simply stays per-worker. The
    parent wires this in create_app() with the ASGI event loop.
    """
    global _fanout_task
    if not _redis_url():
        log.debug("agent_channel: no REDIS_URL; Redis fan-out not started")
        return None
    if _fanout_task is not None and not _fanout_task.done():
        return _fanout_task
    target_loop = loop or _loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        _fanout_task = running.create_task(_fanout_main())
    elif target_loop is not None and target_loop.is_running():
        _fanout_task = target_loop.create_task(_fanout_main())
    else:
        log.debug("agent_channel: no event loop; Redis fan-out not started")
        return None
    return _fanout_task


def bind_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Remember the ASGI loop so sync notify_* can schedule coroutines."""
    global _loop
    _loop = loop


def register(device_id: str, websocket: WebSocket) -> None:
    with _lock:
        _subscribers.setdefault(device_id, set()).add(websocket)


def unregister(device_id: str, websocket: WebSocket) -> None:
    with _lock:
        bucket = _subscribers.get(device_id)
        if not bucket:
            return
        bucket.discard(websocket)
        if not bucket:
            _subscribers.pop(device_id, None)


def connected_device_ids() -> list[str]:
    with _lock:
        return list(_subscribers.keys())


def subscriber_count(device_id: str | None = None) -> int:
    with _lock:
        if device_id is None:
            return sum(len(s) for s in _subscribers.values())
        return len(_subscribers.get(device_id) or ())


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def push_to_device(device_id: str, payload: dict[str, Any]) -> int:
    """Send payload to all sockets for device_id. Returns successful send count."""
    _signal_event(device_id)
    with _lock:
        sockets = list(_subscribers.get(device_id) or ())
    if not sockets:
        return 0
    dead: list[WebSocket] = []
    ok = 0
    for ws in sockets:
        if await _send_json(ws, payload):
            ok += 1
        else:
            dead.append(ws)
    for ws in dead:
        unregister(device_id, ws)
    return ok


def _schedule(coro) -> None:
    loop = _loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        running.create_task(coro)
        return
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)
        return
    # Best-effort: drop if no loop (tests may call notify without ASGI).
    log.debug("agent_channel: no event loop; drop push")
    try:
        coro.close()
    except Exception:
        pass


def _publish_best_effort(device_id: str, payload: dict[str, Any]) -> None:
    """Redis fan-out for a notify_*; a Redis outage never raises."""
    try:
        publish_redis(device_id, payload)
    except Exception as exc:  # publish_redis already guards; belt and braces
        log.debug("agent_channel: Redis fan-out failed (%s)", exc)


def notify_task_available(device_id: str, task_id: str | None = None) -> None:
    payload: dict[str, Any] = {"type": "task_available"}
    if task_id:
        payload["task_id"] = task_id
    _schedule(push_to_device(device_id, payload))
    _publish_best_effort(device_id, payload)


def notify_cancel(device_id: str, task_id: str, *, emergency_stop: bool = False) -> None:
    payload: dict[str, Any] = {"type": "cancel", "task_id": task_id}
    if emergency_stop:
        payload["emergency_stop"] = True
    _schedule(push_to_device(device_id, payload))
    _publish_best_effort(device_id, payload)


def notify_emergency_stop(active: bool) -> None:
    payload = {"type": "emergency_stop", "active": bool(active)}
    with _lock:
        device_ids = list(_subscribers.keys())
    for device_id in device_ids:
        _schedule(push_to_device(device_id, payload))
    # Broadcast channel: devices connected to *other* workers must hear
    # this too, and this worker does not know their ids.
    _publish_best_effort("broadcast", payload)


def handle_agent_message(conn: Any, device_id: str, msg: Any) -> dict[str, Any] | None:
    """Handle one JSON message from an agent's WS socket (DIST-08).

    Wire contract (for the Wave 3C agent change — the poller sends this):
    the agent sends ``{"type": "progress", "task_id": "<id>"}`` at most
    once per 60 s per running task; the plane replies
    ``{"type": "progress_ack", "task_id": "<id>", "ok": true}`` while the
    task is still ``running``. ``ok: false`` means the task is terminal,
    re-queued, or unknown — the agent MUST stop work on it. Reports for
    a task the device does not own are rejected (``ok: false``) so one
    agent cannot keep another device's task alive. Reports arriving
    within 60 s of the last accepted one for the same task are throttled
    (``throttled: true``, no DB write) to bound ``task_events`` growth.

    Unknown message types return None (the caller ignores them). This
    function is sync and side-effect-free for non-progress messages;
    the app.py WS loop wires it in (parent follow-up).
    """
    if not isinstance(msg, dict):
        return None
    if msg.get("type") != "progress":
        return None
    task_id = str(msg.get("task_id") or "").strip()
    if not task_id:
        return {"type": "progress_ack", "task_id": "", "ok": False}
    # Liveness probe FIRST (cheap indexed PK read): a task that just
    # completed must get ok=False even inside the 60 s throttle window,
    # or the agent would keep working on a finished task for up to a
    # minute. The throttle below only skips DB *writes* for tasks that
    # are still running.
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=? AND device_id=?", (task_id, device_id)
        ).fetchone()
    except Exception:
        row = None
    status = row["status"] if row is not None and hasattr(row, "keys") else (row[0] if row else None)
    if status != "running":
        return {"type": "progress_ack", "task_id": task_id, "ok": False}
    now_ts = time.time()
    key = (device_id, task_id)
    last = _progress_last.get(key, 0.0)
    if now_ts - last < _PROGRESS_THROTTLE_SEC:
        return {"type": "progress_ack", "task_id": task_id, "ok": True, "throttled": True}
    from .orchestration import record_task_progress

    progress_pct = msg.get("progress_pct")
    status_message = msg.get("status_message")
    ok = record_task_progress(
        conn,
        task_id,
        device_id=device_id,
        progress_pct=float(progress_pct) if progress_pct is not None else None,
        status_message=str(status_message) if status_message is not None else None,
    )
    if ok:
        _progress_last[key] = now_ts
    return {"type": "progress_ack", "task_id": task_id, "ok": bool(ok)}


def reset_for_tests() -> None:
    with _lock:
        _subscribers.clear()
        _webrtc_fanout_handlers.clear()
    _events.clear()
    _progress_last.clear()
    global _sync_redis, _sync_redis_failed_at, _REDIS_UNAVAILABLE_WARNED
    _sync_redis = None
    _sync_redis_failed_at = 0.0
    _REDIS_UNAVAILABLE_WARNED = False

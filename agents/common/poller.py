"""Agent poller: outbound-only loop with long-polling, auto token rotation,
and bounded exponential backoff.

The agent never listens on a port. It heartbeats, asks for the next
queued task, executes it through ai_bridge.control.dispatch
(policy-enforced), and posts the result. On transient errors it backs
off with jitter, capped at 60s, resetting on the next success.

Wave 3C reliability (F07-07/F07-09/F07-10/F07-12, DIST-08):

* Task execution runs on a worker thread; the heartbeat runs on its own
  timer thread, so a hung tool can no longer stall heartbeats, claiming,
  rotation, or cancellation.
* ``request_shutdown()`` stops claiming new tasks; the in-flight task is
  allowed to finish within a bounded drain timeout, then the loop exits.
* A 401 is classified: revocation / token expiry is terminal (stop
  polling, surface a re-enroll prompt, no infinite retry); any other 401
  (e.g. a rotation race) is transient and retried with backoff + jitter.
* While a task runs, the heartbeat timer reports task progress so a long
  task is not re-queued by the server's stale-lease sweep.
"""
from __future__ import annotations

import json
import os
import platform
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ai_bridge.control.dispatch import dispatch
from ai_bridge.control.task_runtime import TaskCancelled, clear_execution_context, set_execution_context
from ai_bridge.policy import Policy
from ai_bridge.tools import default_policy

from .spool import TaskSpooler

MAX_RESULT_CHARS = 512_000
ROTATE_THRESHOLD_SECONDS = 2 * 86400  # Rotate when < 48h remaining

# PERF-08 (Wave 7): the cancel poller used to GET `execution-state`
# every 0.5 s per running task (2 req/s/task) AND every `_cancelled()`
# call — invoked by the task runtime on every cooperative-cancel check —
# cost a further HTTP round-trip each. The WS side-channel already
# delivers `cancel` / `emergency_stop` messages, so cancellation is now
# WS-driven when the channel is connected and HTTP is only a fallback:
# at most one `execution-state` GET per CANCEL_POLL_INTERVAL_S, and no
# HTTP at all while the WS is up. Worst-case cancel latency is therefore
# ~instant on WS, and one poll interval (default 5 s) with the WS down —
# the price of the 10x request reduction in the degraded path.
CANCEL_POLL_INTERVAL_S = 5.0

# Operators sometimes paste the static dashboard URL; agents must hit the control plane API.
_DASHBOARD_TO_CONTROL = {
    "https://ai-bridge-dashboard.onrender.com": "https://ai-bridge-control.onrender.com",
    "http://ai-bridge-dashboard.onrender.com": "https://ai-bridge-control.onrender.com",
}


def normalize_control_url(url: str) -> str:
    """Strip trailing slash and remap known dashboard hosts to the control plane."""
    cleaned = (url or "").strip().rstrip("/")
    mapped = _DASHBOARD_TO_CONTROL.get(cleaned.lower()) or _DASHBOARD_TO_CONTROL.get(cleaned)
    if mapped:
        return mapped
    lower = cleaned.lower()
    if "ai-bridge-dashboard.onrender.com" in lower:
        return "https://ai-bridge-control.onrender.com"
    return cleaned


class ControlPlaneHTTPError(RuntimeError):
    """An HTTP error response from the control plane, with status + body.

    A ``RuntimeError`` subclass, so existing callers that catch
    ``RuntimeError`` keep working; the status code and body let callers
    distinguish revocation (401 naming the token revoked) from transient
    failures (5xx, network, rotation-race 401s).
    """

    def __init__(self, status_code: int, path: str, body: str = "") -> None:
        self.status_code = int(status_code)
        self.path = path
        self.body = body or ""
        detail = f" body={self.body!r}" if self.body else ""
        super().__init__(f"Control plane returned HTTP {self.status_code} for {path}.{detail}")


class DeviceRevokedError(ControlPlaneHTTPError):
    """The device token was revoked (or is unknown to the plane).

    Terminal: retrying cannot help. The agent must stop polling and
    surface a re-enroll prompt instead of backing off forever (F07-12).
    """


class DeviceTokenExpiredError(ControlPlaneHTTPError):
    """The device token expired. Rotation needs a valid token, so an
    expired token is terminal too: stop polling, re-enroll (F07-12)."""


def classify_auth_failure(body: str | None) -> str:
    """Classify a 401 response body: ``"revoked"``, ``"expired"``, or
    ``"transient"`` (F07-12).

    The plane's ``device_row`` (``control/app.py``) returns three distinct
    401 details, which is what makes the distinction possible:

    * ``"Unknown or revoked device token."`` -> ``"revoked"``
    * ``"Device token expired; rotate it ... or re-enroll."`` -> ``"expired"``
    * anything else (e.g. ``"Device token required."``) -> ``"transient"``

    Caveat, documented honestly: ``"Unknown or revoked"`` conflates a
    token the plane never knew (e.g. a stale token after a rotation
    elsewhere) with a genuinely revoked device. The poller therefore only
    treats it as revocation AFTER the saved-token retry is exhausted
    (see :meth:`Poller.heartbeat`); a rotation race is recovered first.
    """
    text = (body or "").lower()
    if "revok" in text or "unauthorized device" in text or "device is revoked" in text:
        return "revoked"
    if "expir" in text:
        return "expired"
    return "transient"


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 60.0,
                  jitter: bool = True, rng: random.Random | None = None) -> float:
    """Bounded exponential backoff with jitter (F07-12).

    ``min(cap, base * 2**attempt)`` seconds, multiplied by a uniform
    jitter factor in [0.5, 1.5] when ``jitter`` is set (clamped back to
    ``cap``). ``attempt`` 0 is the first retry. Pass an explicit ``rng``
    for deterministic tests.
    """
    delay = min(cap, base * (2 ** max(0, int(attempt))))
    if jitter:
        r = rng or random
        delay *= r.uniform(0.5, 1.5)
    return min(cap, max(0.0, float(delay)))


def _intersect_allowlist(server_value, local_value):
    """Most-restrictive intersection of two allowlists (R-P0-07).

    ``"*"`` on a side means that side is unbounded; ``None`` means that side
    states no restriction. Returns ``None`` when both sides are unbounded
    (caller maps that to its own unbounded representation), otherwise the
    intersection as a frozenset. An explicitly empty set on either side is a
    real restriction (deny-all) and stays empty after intersection.
    """
    def _norm(value):
        if value is None:
            return None
        items = frozenset(value)
        return None if "*" in items else items

    server = _norm(server_value)
    local = _norm(local_value)
    if server is None:
        return local
    if local is None:
        return server
    return server & local


def _intersect_configured_allowlist(server_value, local_value):
    """Intersection for allowlists whose default is an empty set.

    ``Policy.allowed_apps`` / ``allowed_services`` default to ``frozenset()``
    when nothing is configured, and an empty set there means "not
    configured", not "deny everything" (explicit deny-all is expressed via
    ``denied_tools`` / an empty ``allowed_tools`` list). So an empty side
    here does not narrow; a non-empty list on either side does. ``"*"``
    still means unbounded on that side.
    """
    server = None if server_value is None else frozenset(server_value)
    local = None if local_value is None else frozenset(local_value)
    if server is None or len(server) == 0:
        return local
    if local is None or len(local) == 0:
        return server
    if "*" in server:
        return local
    if "*" in local:
        return server
    return server & local


def _collect_metrics() -> dict:
    metrics: dict = {}
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            metrics["ip_address"] = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass

    try:
        import psutil
        vm = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=None)
        root = os.path.splitdrive(os.getcwd())[0] or "/"
        if not root.endswith(os.sep):
            root += os.sep
        disk = psutil.disk_usage(root)
        metrics["cpu_percent"] = round(float(cpu), 1)
        metrics["memory_percent"] = round(float(vm.percent), 1)
        metrics["memory_total_gb"] = round(float(vm.total) / (1024 ** 3), 1)
        metrics["disk_percent"] = round(float(disk.percent), 1)
        metrics["disk_total_gb"] = round(float(disk.total) / (1024 ** 3), 1)
        boot_ts = psutil.boot_time()
        if boot_ts:
            metrics["uptime_hours"] = round(max(0.0, time.time() - float(boot_ts)) / 3600.0, 1)
    except Exception:
        try:
            import shutil
            root = os.path.splitdrive(os.getcwd())[0] or "/"
            if not root.endswith(os.sep):
                root += os.sep
            tot, used, free = shutil.disk_usage(root)
            if tot > 0:
                metrics["disk_percent"] = round((used / tot) * 100.0, 1)
                metrics["disk_total_gb"] = round(float(tot) / (1024 ** 3), 1)
        except Exception:
            pass
        if os.path.exists("/proc/uptime"):
            try:
                with open("/proc/uptime", encoding="utf-8") as f:
                    up_sec = float(f.read().split()[0])
                    metrics["uptime_hours"] = round(up_sec / 3600.0, 1)
            except Exception:
                pass
    return metrics


class Poller:
    def __init__(
        self,
        control_url: str,
        device_token: str,
        *,
        policy: Policy | None = None,
        token_file: Path | None = None,
        token_saver: Callable[[str], None] | None = None,
        token_loader: Callable[[], str | None] | None = None,
        heartbeat_interval: float = 30.0,
        drain_timeout: float = 60.0,
        progress_interval: float = 60.0,
        cancel_poll_interval: float = CANCEL_POLL_INTERVAL_S,
    ):
        self.control_url = normalize_control_url(control_url)
        self.device_token = device_token
        # WebRTC streaming tasks need these to signal back to the plane.
        try:
            from ai_bridge import webrtc_stream

            webrtc_stream.set_agent_context(self.control_url, self.device_token)
        except Exception:
            pass
        self._explicit_policy = policy
        self.policy = policy if policy is not None else default_policy()
        self.token_file = token_file or (Path.home() / ".ai-bridge" / "device.token")
        # Optional persistence hooks: the Windows desktop app uses these to keep
        # rotated tokens DPAPI-sealed instead of writing plaintext.
        self._token_saver = token_saver
        self._token_loader = token_loader
        spool_path = self.token_file.parent / "agent_spool.db" if self.token_file else None
        self.spooler = TaskSpooler(spool_path)
        self.backoff = 2.0
        self._backoff_attempts = 0
        self._wake = threading.Event()
        self._ws_cancel_task_id: str | None = None
        self._ws_cancel_lock = threading.Lock()
        self._ws_connected = threading.Event()
        self._ws_stop = threading.Event()
        self._active_cancel_event: threading.Event | None = None
        # PERF-08: throttled fallback cancel checks. `_last_cancel_state`
        # is the cached result of the most recent `execution-state` GET;
        # `_last_cancel_check_at` (monotonic) gates re-checks to at most
        # one per `cancel_poll_interval`. Both are reset at every task
        # claim — a cached "cancelled" from a previous task must never
        # leak into the next one.
        self.cancel_poll_interval = float(cancel_poll_interval)
        self._cancel_check_lock = threading.Lock()
        self._last_cancel_check_at = 0.0
        self._last_cancel_state = False
        self._ws_thread: threading.Thread | None = None
        # --- Wave 3C reliability state (F07-09/F07-10/F07-12, DIST-08) ---
        # Graceful shutdown: set by request_shutdown() (signal handlers);
        # run_forever() stops claiming new tasks once it is set.
        self._shutdown_requested = threading.Event()
        # Bounded drain: an in-flight task may finish for at most this
        # long after shutdown is requested (default 60 s — inside
        # systemd's default 90 s stop timeout, so the unit is not SIGKILLed
        # mid-drain). Past the deadline the task is left `running` on the
        # plane and the lease sweep re-queues it; see run_once().
        self.drain_timeout = float(drain_timeout)
        self._drain_timed_out = False
        # Independent heartbeat timer (F07-10): a daemon thread that fires
        # heartbeat() every heartbeat_interval regardless of task execution.
        self.heartbeat_interval = float(heartbeat_interval)
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_loop_running = False
        # Terminal auth state (F07-12): set when the plane says the token
        # was revoked or expired. run_forever() exits instead of retrying.
        self.revoked = False
        self.revoked_reason: str | None = None
        self._token_expires_at: float | None = None
        # Current task, for progress reporting (DIST-08) and drain.
        self._current_task_id: str | None = None
        self._current_task_epoch: int | None = None
        self._current_task_tool: str | None = None
        self._current_task_started_at: float | None = None
        self._last_progress_at = 0.0
        self.progress_interval = float(progress_interval)
        # The progress endpoint does not exist on the plane yet (Wave 3A/4
        # follow-up): once it answers 404/501 we stop trying for this run.
        self._progress_endpoint_missing = False

    # -- graceful shutdown (F07-09) ----------------------------------------

    @property
    def shutdown_requested(self) -> bool:
        """True once request_shutdown() has been called."""
        return self._shutdown_requested.is_set()

    def request_shutdown(self) -> None:
        """Ask the agent to stop claiming new tasks and drain.

        Safe to call from a signal handler: it only sets events. The
        in-flight task (if any) keeps running until it finishes or the
        drain timeout expires; run_forever() then exits. Idempotent.
        """
        self._shutdown_requested.set()
        self._wake.set()

    def _drain_and_stop(self) -> None:
        """Stop background channels and make a final spool-flush attempt.

        Called once from run_forever() after the loop exits (shutdown,
        revocation, or terminal error). Never blocks long: the heartbeat
        and WS threads are only joined briefly, and a task whose drain
        timed out is left to the server lease sweep (it stays `running`
        on the plane and is re-queued — deliberately NOT marked failed,
        so it gets re-executed instead of being lost).
        """
        self.stop_heartbeat_loop()
        self.stop_side_channel()
        if self._ws_thread is not None and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)
        try:
            self.spooler.flush(self._try_post_result)
        except Exception:
            pass
        if self.revoked:
            print(
                "[AGENT] Device token revoked/expired — agent stopped. "
                "Re-enroll this device to resume.",
                file=sys.stderr, flush=True,
            )
        elif self._drain_timed_out:
            print(
                "[AGENT] Shutdown complete, but an in-flight task did not "
                "finish within the drain timeout; it was left running on "
                "the control plane and will be re-queued by the lease sweep.",
                file=sys.stderr, flush=True,
            )

    def _req(self, method: str, path: str, payload: dict | None = None, timeout: int = 15) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.control_url + path,
            data=data,
            method=method,
            headers={
                "X-Device-Token": self.device_token,
                "Content-Type": "application/json",
                "User-Agent": "AI-Bridge-Agent/0.3",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read() or b"{}"
                ctype = (resp.headers.get("Content-Type") or "").lower()
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    snippet = raw[:120].decode("utf-8", errors="replace").replace("\n", " ")
                    hint = ""
                    if "text/html" in ctype or snippet.lstrip().startswith("<!"):
                        hint = (
                            " Got HTML instead of JSON — control_url is probably the dashboard. "
                            "Use https://ai-bridge-control.onrender.com (not *-dashboard.*)."
                        )
                    raise RuntimeError(
                        f"Control plane at {self.control_url} returned non-JSON for {path}.{hint} "
                        f"Parse error: {exc}. Body starts: {snippet!r}"
                    ) from exc
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = (exc.read() or b"")[:500].decode("utf-8", errors="replace")
            except Exception:
                pass
            # F07-12: keep the status + body structured so callers can
            # distinguish revocation/expiry (terminal) from transient 401s.
            # Try to surface a JSON "detail" string when the plane sends one.
            detail = body
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
                    detail = parsed["detail"]
            except (ValueError, TypeError):
                pass
            if exc.code == 401 and classify_auth_failure(detail) == "revoked":
                raise DeviceRevokedError(exc.code, path, detail) from exc
            if exc.code == 401 and classify_auth_failure(detail) == "expired":
                raise DeviceTokenExpiredError(exc.code, path, detail) from exc
            raise ControlPlaneHTTPError(exc.code, path, detail) from exc

    def _save_token(self, token: str) -> None:
        # F07-05: a configured token saver (e.g. the Windows DPAPI sealer) is
        # the only authorised writer for this token. If it fails, fail LOUDLY
        # and keep the token in memory only for this run — NEVER silently
        # downgrade to a plaintext file. A DPAPI failure must be visible to
        # the operator, not papered over with a cleartext bearer token.
        if self._token_saver is not None:
            try:
                self._token_saver(token)
                return
            except Exception as exc:
                print(
                    f"[SECURITY] Token saver failed ({type(exc).__name__}: {exc}); "
                    "NOT writing the device token to disk. The token is kept "
                    "in memory only for this run — fix the token store and "
                    "re-enroll/rotate if the agent restarts.",
                    file=sys.stderr,
                    flush=True,
                )
                return
        # Wave 6A (F07-05, remainder): no sealed store is configured, so the
        # token can only persist as a plaintext file (0600) — that is what
        # keeps the agent enrolled across restarts on platforms without a
        # saver, and re-enrollment here is manual. That write must NEVER be
        # silent: warn loudly every time it happens, and honour an explicit
        # opt-out — AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE=0 refuses the
        # plaintext write entirely (memory-only for this run, operator must
        # configure a saver or re-enroll after a restart).
        if os.environ.get("AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE", "1").strip().lower() in ("0", "false", "no"):
            print(
                "[SECURITY] AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE=0: refusing to "
                f"write the device token to {self.token_file} in plaintext. "
                "The token is kept in memory only for this run — configure a "
                "sealed token store or re-enroll after a restart.",
                file=sys.stderr,
                flush=True,
            )
            return
        print(
            "[SECURITY] No sealed token store is configured: writing the "
            f"device token to {self.token_file} in PLAINTEXT (0600). Anyone "
            "who can read this file can impersonate this device. Configure a "
            "sealed token store, or set AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE=0 "
            "to refuse plaintext persistence.",
            file=sys.stderr,
            flush=True,
        )
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.token_file.with_name(f"{self.token_file.name}.tmp")
            tmp_file.write_text(token, encoding="utf-8")
            try:
                os.chmod(tmp_file, 0o600)
            except OSError:
                pass
            tmp_file.replace(self.token_file)
            try:
                os.chmod(self.token_file, 0o600)
            except OSError:
                pass
        except OSError as exc:
            print(
                f"[SECURITY] Could not persist the device token to {self.token_file} "
                f"({exc}); the token is kept in memory only for this run.",
                file=sys.stderr,
                flush=True,
            )

    def _load_saved_token(self) -> str | None:
        if self._token_loader is not None:
            try:
                tok = self._token_loader()
                if tok and tok.startswith("aib-dpapi-v1:"):
                    return self._unseal_if_dpapi(tok)
                return tok
            except Exception:
                return None
        try:
            if self.token_file.exists():
                tok = self.token_file.read_text(encoding="utf-8").strip()
                if tok.startswith("aib-dpapi-v1:"):
                    tok = self._unseal_if_dpapi(tok)
                return tok or None
        except OSError:
            pass
        return None

    @staticmethod
    def _unseal_if_dpapi(tok: str) -> str:
        try:
            from windows.app_runtime import unseal_token
            return unseal_token(tok)
        except Exception:
            try:
                from app_runtime import unseal_token
                return unseal_token(tok)
            except Exception:
                return tok

    def rotate_token(self) -> str:
        res = self._req("POST", "/api/agent/rotate", {})
        new_tok = res.get("device_token")
        if new_tok:
            self.device_token = new_tok
            self._save_token(new_tok)
        return self.device_token

    def _mark_terminal_auth(self, kind: str, detail: str) -> None:
        """Record a terminal auth failure and surface the re-enroll prompt.

        ``kind`` is ``"revoked"`` or ``"expired"`` (F07-12). This is
        printed prominently (not just logged) because the agent will stop
        polling: an operator who misses it sees an agent that silently
        never comes back.
        """
        self.revoked = True
        self.revoked_reason = f"{kind}: {detail}" if detail else kind
        print(
            f"\n[AGENT] *** DEVICE TOKEN {kind.upper()} — AGENT STOPPING ***\n"
            f"[AGENT] Control plane: {detail or 'no detail given'}\n"
            "[AGENT] This is NOT a transient error: retrying cannot help.\n"
            "[AGENT] To resume, re-enroll this device:\n"
            "[AGENT]   Linux:   python agents/linux/agent_main.py enroll --admin-token <ADMIN_TOKEN>\n"
            "[AGENT]   Windows: re-enroll from the GUI, or place a fresh token with the CLI\n"
            "[AGENT]   macOS:   enroll on another host and pass the new --device-token\n",
            file=sys.stderr, flush=True,
        )

    def heartbeat(self) -> dict:
        """POST /api/agent/heartbeat, serialised against the heartbeat loop.

        F07-12: on a 401, first retry once with the token saved on disk
        (covers the rotation race: another process rotated the token and
        this process still holds the old one). If that also fails — or
        there is no differing saved token — classify the failure: a
        revocation/expiry signal is terminal (mark revoked, raise the
        terminal error, no further retries); anything else is transient
        and raises ControlPlaneHTTPError for the caller's backoff.
        """
        with self._heartbeat_lock:
            return self._heartbeat_once()

    def _heartbeat_once(self) -> dict:
        hb_body = {
            "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}".strip(),
            "metrics": _collect_metrics(),
        }
        try:
            hb = self._req("POST", "/api/agent/heartbeat", hb_body)
        except (DeviceRevokedError, DeviceTokenExpiredError, ControlPlaneHTTPError) as exc:
            status = getattr(exc, "status_code", None)
            if status != 401:
                raise
            # 401: try the on-disk token once (rotation race recovery).
            saved = self._load_saved_token()
            if saved and saved != self.device_token:
                self.device_token = saved
                try:
                    hb = self._req("POST", "/api/agent/heartbeat", hb_body)
                except (DeviceRevokedError, DeviceTokenExpiredError) as exc2:
                    kind = "revoked" if isinstance(exc2, DeviceRevokedError) else "expired"
                    self._mark_terminal_auth(kind, exc2.body)
                    raise
                except ControlPlaneHTTPError as exc2:
                    if getattr(exc2, "status_code", None) == 401:
                        kind = classify_auth_failure(exc2.body)
                        if kind in ("revoked", "expired"):
                            self._mark_terminal_auth(kind, exc2.body)
                            if kind == "revoked":
                                raise DeviceRevokedError(exc2.status_code, exc2.path, exc2.body) from exc2
                            raise DeviceTokenExpiredError(exc2.status_code, exc2.path, exc2.body) from exc2
                    raise
            elif isinstance(exc, (DeviceRevokedError, DeviceTokenExpiredError)):
                kind = "revoked" if isinstance(exc, DeviceRevokedError) else "expired"
                self._mark_terminal_auth(kind, exc.body)
                raise
            else:
                kind = classify_auth_failure(getattr(exc, "body", ""))
                if kind in ("revoked", "expired"):
                    self._mark_terminal_auth(kind, getattr(exc, "body", ""))
                    if kind == "revoked":
                        raise DeviceRevokedError(exc.status_code, exc.path, exc.body) from exc
                    raise DeviceTokenExpiredError(exc.status_code, exc.path, exc.body) from exc
                raise
        except Exception:
            raise
        try:
            from ai_bridge.plane_control_flags import set_emergency_stop

            set_emergency_stop(bool(hb.get("emergency_stop")))
        except Exception:
            pass
        expires = hb.get("token_expires_at")
        if isinstance(expires, (int, float)):
            self._token_expires_at = float(expires)
        if isinstance(expires, (int, float)) and expires - time.time() < ROTATE_THRESHOLD_SECONDS:
            try:
                self.rotate_token()
            except Exception:
                pass
        return hb

    # -- independent heartbeat timer + task progress (F07-10, DIST-08) -----

    def start_heartbeat_loop(self) -> None:
        """Start the independent heartbeat timer thread (F07-10).

        The thread fires heartbeat() every ``heartbeat_interval`` and
        reports task progress while a task runs, on its own thread — it is
        never blocked by a hung task executing on the worker thread.
        Idempotent; safe to call twice.
        """
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_loop_running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="ai-bridge-agent-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def stop_heartbeat_loop(self) -> None:
        """Stop the heartbeat timer thread (brief join; never blocks long)."""
        self._heartbeat_stop.set()
        self._heartbeat_loop_running = False
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)
        self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.is_set():
            if self._shutdown_requested.is_set() or self.revoked:
                break
            try:
                self.heartbeat()
            except (DeviceRevokedError, DeviceTokenExpiredError):
                break  # terminal auth: run_forever() exits; do not retry
            except Exception as exc:
                print(f"[{time.strftime('%H:%M:%S')}] Heartbeat failed: {exc}", flush=True)
            self._maybe_report_progress()
            self._heartbeat_stop.wait(timeout=self.heartbeat_interval)
        self._heartbeat_loop_running = False

    def report_progress(
        self,
        task_id: str,
        progress_pct: float | None = None,
        status_message: str | None = None,
    ) -> bool:
        """Report task progress to the control plane (DIST-08).

        POSTs ``POST /api/tasks/{task_id}/progress`` with
        ``{"task_id", "progress_pct", "status_message", "epoch"?}`` — see
        ``analysis/wave3c-agents-progress.md`` §DIST-08 for the full wire
        contract and the server-side follow-up. ``progress_pct`` is None
        when the agent cannot measure a percentage (the honest value for
        most tools); the plane must treat null as "still alive, no %".

        The endpoint does not exist on the plane yet: a 404/501 is
        tolerated gracefully — logged once, the endpoint is marked missing
        for the rest of this run, and the agent keeps running. (Until the
        server side lands, progress is still recorded implicitly: the
        heartbeat loop's progress reports (DIST-08, default every 60 s)
        bump the task row, and the throttled `execution-state` fallback
        poll (PERF-08, default every 5 s, WS-down only) bumps it on each
        check via the plane's `record_task_progress`.)
        Returns True when the plane accepted the report.
        """
        if self._progress_endpoint_missing:
            return False
        payload: dict = {
            "task_id": task_id,
            "progress_pct": progress_pct,
            "status_message": status_message,
        }
        if task_id == self._current_task_id and self._current_task_epoch is not None:
            payload["epoch"] = self._current_task_epoch
        try:
            self._req("POST", f"/api/tasks/{task_id}/progress", payload, timeout=10)
            return True
        except ControlPlaneHTTPError as exc:
            if exc.status_code in (404, 501):
                self._progress_endpoint_missing = True
                print(
                    f"[{time.strftime('%H:%M:%S')}] Task-progress endpoint not available "
                    f"(HTTP {exc.status_code}); progress reports disabled for this run — "
                    "server side is a Wave 3A/4 follow-up. Long tasks are still "
                    "protected by the execution-state poll.",
                    flush=True,
                )
                return False
            return False
        except Exception:
            return False

    def _maybe_report_progress(self) -> None:
        """Send one throttled progress report for the running task, if any.

        Called from the heartbeat timer (F07-10/DIST-08): at most one
        report per ``progress_interval`` (default 60 s, matching the
        server-side guidance that progress events be throttled so
        ``task_events`` does not flood). A failed report does not reset
        the timer, so it is retried on the next heartbeat tick.
        """
        task_id = self._current_task_id
        if not task_id or self._shutdown_requested.is_set():
            return
        now = time.time()
        if now - self._last_progress_at < self.progress_interval:
            return
        elapsed = now - (self._current_task_started_at or now)
        tool = self._current_task_tool or "task"
        if self.report_progress(
            task_id,
            progress_pct=None,
            status_message=f"running: {tool} (elapsed {elapsed:.0f}s)",
        ):
            self._last_progress_at = now

    def _effective_policy(self, remote_policy: dict | None) -> Policy:
        """Most-restrictive merge of server policy and local policy (R-P0-07).

        The server (control plane) policy is the ceiling. The local explicit
        policy may only NARROW it — it can never widen it:

        * ``unrestricted`` is True only when BOTH sides say True.
        * ``max_tier_unattended`` is the MINIMUM of the two ceilings.
        * ``allow_writes`` / ``allow_tier3`` / ``desktop_enabled`` are ANDed.
        * ``allowed_tools`` is the set intersection (``None`` = unbounded).
        * ``allowed_apps`` / ``allowed_services`` / ``allowed_test_commands``
          are intersected when both sides configure a list (an empty default
          means "not configured", see ``_intersect_configured_allowlist``).
        * ``denied_tools`` is the UNION, plus the tools implied by
          ``allow_shell=False`` / ``allow_package_install=False``.

        A compromised or misconfigured agent therefore cannot grant itself
        permissions the plane did not grant.
        """
        base = self._explicit_policy if self._explicit_policy is not None else default_policy()
        if not isinstance(remote_policy, dict):
            return base

        unrestricted = bool(remote_policy.get("unrestricted", False)) and bool(
            getattr(base, "unrestricted", False)
        )

        try:
            server_ceiling = int(remote_policy.get("max_tier_unattended", base.max_tier_unattended))
        except (TypeError, ValueError):
            server_ceiling = base.max_tier_unattended
        max_tier_unattended = min(server_ceiling, base.max_tier_unattended)

        allow_writes = bool(remote_policy.get("allow_writes", base.allow_writes)) and bool(base.allow_writes)
        allow_tier3 = bool(remote_policy.get("allow_tier3", base.allow_tier3)) and bool(base.allow_tier3)
        desktop_enabled = bool(remote_policy.get("desktop_enabled", base.desktop_enabled)) and bool(
            base.desktop_enabled
        )

        remote_denied = set(remote_policy.get("denied_tools") or [])
        if remote_policy.get("allow_shell") is False:
            remote_denied.update({"run_shell", "terminal_exec", "desktop_execute_command"})
        if remote_policy.get("allow_package_install") is False:
            remote_denied.add("install_package")
        denied_tools = frozenset(set(base.denied_tools) | remote_denied)

        allowed_tools = _intersect_allowlist(
            remote_policy.get("allowed_tools"), base.allowed_tools
        )
        allowed_apps = _intersect_configured_allowlist(
            remote_policy.get("allowed_apps"), base.allowed_apps
        )
        allowed_services = _intersect_configured_allowlist(
            remote_policy.get("allowed_services"), base.allowed_services
        )
        allowed_test_commands = _intersect_configured_allowlist(
            remote_policy.get("allowed_test_commands"), base.allowed_test_commands
        )

        return Policy(
            allow_writes=allow_writes,
            allow_tier3=allow_tier3,
            desktop_enabled=desktop_enabled,
            max_tier_unattended=max_tier_unattended,
            unrestricted=unrestricted,
            allowed_apps=frozenset({"*"}) if allowed_apps is None else allowed_apps,
            allowed_services=frozenset({"*"}) if allowed_services is None else allowed_services,
            allowed_test_commands=(
                frozenset({"*"}) if allowed_test_commands is None else allowed_test_commands
            ),
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
        )

    def _try_post_result(self, task_id: str, status: str, result: str) -> bool:
        try:
            self._req("POST", f"/api/agent/tasks/{task_id}/result", {"status": status, "result": result})
            return True
        except Exception:
            return False

    def run_once(self, wait: int = 0) -> dict:
        # A revoked/expired device never claims again (F07-12).
        if self.revoked:
            return {"task": None, "revoked": True, "revoked_reason": self.revoked_reason}
        try:
            self.spooler.flush(self._try_post_result)
        except Exception:
            pass
        # When the independent heartbeat timer is running it owns the
        # heartbeat; a second inline heartbeat here would only risk a
        # rotation race. Standalone callers (--once, GUI poll loop) have
        # no timer, so they still heartbeat inline (F07-10).
        if not self._heartbeat_loop_running:
            try:
                self.heartbeat()
            except (DeviceRevokedError, DeviceTokenExpiredError):
                return {"task": None, "revoked": True, "revoked_reason": self.revoked_reason}
        path = f"/api/agent/tasks/next?wait={wait}" if wait > 0 else "/api/agent/tasks/next"
        try:
            nxt = self._req("GET", path, timeout=max(15, wait + 5))
        except (DeviceRevokedError, DeviceTokenExpiredError) as exc:
            if not self.revoked:
                kind = "revoked" if isinstance(exc, DeviceRevokedError) else "expired"
                self._mark_terminal_auth(kind, getattr(exc, "body", ""))
            return {"task": None, "revoked": True, "revoked_reason": self.revoked_reason}
        task = nxt.get("task")
        from ai_bridge.plane_control_flags import set_emergency_stop

        set_emergency_stop(bool(nxt.get("emergency_stop")))
        if not task:
            self.backoff = 2.0
            self._backoff_attempts = 0
            return {"task": None, "emergency_stop": nxt.get("emergency_stop", False)}
        task_id = task["id"]
        cancel_stop = threading.Event()
        self._active_cancel_event = cancel_stop
        # PERF-08: fresh cancel-check cache for this task (see the
        # constructor comment — a stale "cancelled" must never leak
        # from the previous task into this one).
        with self._cancel_check_lock:
            self._last_cancel_check_at = 0.0
            self._last_cancel_state = False
        with self._ws_cancel_lock:
            if self._ws_cancel_task_id == task_id:
                cancel_stop.set()
                self._ws_cancel_task_id = None

        def _cancelled() -> bool:
            """True when the plane cancelled/timed-out this task.

            PERF-08: cancellation is WS-driven when the side-channel is
            connected — a `cancel` / `emergency_stop` WS message sets
            `cancel_stop` (via `_handle_ws_message`) with no HTTP at
            all. Only when the WS is down does this fall back to polling
            `execution-state`, throttled to at most one GET per
            `cancel_poll_interval` no matter how often the task runtime
            calls this predicate (previously every call was a round-trip).
            The emergency-stop mirror is refreshed by the fallback fetch
            and, on the WS path, by WS messages and the heartbeat.
            """
            if cancel_stop.is_set():
                return True
            with self._ws_cancel_lock:
                if self._ws_cancel_task_id == task_id:
                    cancel_stop.set()
                    self._ws_cancel_task_id = None
                    return True
            if self._ws_connected.is_set():
                return False
            now = time.monotonic()
            with self._cancel_check_lock:
                if now - self._last_cancel_check_at < self.cancel_poll_interval:
                    return self._last_cancel_state
                self._last_cancel_check_at = now
            try:
                st = self._req("GET", f"/api/agent/tasks/{task_id}/execution-state", timeout=8)
                if st.get("emergency_stop"):
                    set_emergency_stop(True)
                    cancelled = True
                else:
                    set_emergency_stop(bool(st.get("emergency_stop")))
                    cancelled = str(st.get("status") or "") in {"cancelled", "timed_out"}
            except Exception:
                return self._last_cancel_state
            with self._cancel_check_lock:
                self._last_cancel_state = cancelled
            return cancelled

        def _cancel_poller() -> None:
            # PERF-08: was `time.sleep(0.5)` + an HTTP GET per cycle
            # (2 req/s per running task). Now the thread blocks on
            # `cancel_stop` for up to `cancel_poll_interval`: a WS
            # cancel wakes it instantly (the event is set by
            # `_handle_ws_message`), and without a WS the next
            # `_cancelled()` call is the throttled fallback fetch.
            # Worst-case cancel latency is therefore ~instant on WS and
            # one poll interval with the WS down.
            while not cancel_stop.is_set():
                if _cancelled():
                    cancel_stop.set()
                    return
                cancel_stop.wait(timeout=self.cancel_poll_interval)

        poll_thread = threading.Thread(target=_cancel_poller, daemon=True)
        set_execution_context(task_id, _cancelled, device_id=str(task.get("device_id") or ""))
        poll_thread.start()
        # --- F07-07: execute on a worker thread, not the poll thread. ---
        # The poll thread (this one) only waits; the heartbeat timer thread
        # and the cancel poller keep running while the worker executes, so
        # a hung tool can no longer stall heartbeats, claims, rotation, or
        # cancellation. The join loop below also implements the graceful
        # drain (F07-09): if shutdown is requested mid-task, the worker
        # gets until drain_timeout to finish.
        args = json.loads(task.get("args_json") or "{}")
        eff_policy = self._effective_policy(task.get("policy"))
        tool_name = task["tool"]
        approval = bool(task.get("approval_granted"))
        result_box: dict = {}

        def _run_task() -> None:
            try:
                result = dispatch(tool_name, args, policy=eff_policy, approval_granted=approval)
                result_box.update(
                    status="completed",
                    text=json.dumps(result, default=str)[:MAX_RESULT_CHARS],
                )
            except TaskCancelled:
                result_box.update(
                    status="cancelled",
                    text=json.dumps({"error": "Task cancelled or timed out."}),
                )
            except PermissionError as exc:
                result_box.update(status="failed", text=f"denied: {exc}")
            except Exception as exc:
                result_box.update(status="failed", text=f"{type(exc).__name__}: {exc}")

        self._current_task_id = task_id
        try:
            self._current_task_epoch = int(task["epoch"]) if task.get("epoch") is not None else None
        except (TypeError, ValueError):
            self._current_task_epoch = None
        self._current_task_tool = tool_name
        self._current_task_started_at = time.time()
        self._last_progress_at = time.time()
        worker = threading.Thread(target=_run_task, name=f"ai-bridge-task-{task_id}", daemon=True)
        worker.start()
        drain_deadline: float | None = None
        drain_timed_out = False
        try:
            while worker.is_alive():
                try:
                    worker.join(timeout=0.5)
                except KeyboardInterrupt:
                    # Ctrl-C during a task is a shutdown request, not an
                    # instant kill: drain below, like SIGTERM (F07-09).
                    self.request_shutdown()
                if self._shutdown_requested.is_set():
                    if drain_deadline is None:
                        drain_deadline = time.monotonic() + self.drain_timeout
                        print(
                            f"[{time.strftime('%H:%M:%S')}] Shutdown requested during task "
                            f"{task_id}; draining (up to {self.drain_timeout:.0f}s)...",
                            flush=True,
                        )
                    if time.monotonic() >= drain_deadline:
                        drain_timed_out = True
                        self._drain_timed_out = True
                        print(
                            f"[{time.strftime('%H:%M:%S')}] Drain timeout ({self.drain_timeout:.0f}s) "
                            f"exceeded for task {task_id}; leaving it running on the control "
                            "plane for the lease sweep to re-queue. No result is reported for "
                            "it — it is NOT marked failed, so it will be re-executed rather "
                            "than lost.",
                            file=sys.stderr, flush=True,
                        )
                        break
        finally:
            cancel_stop.set()
            self._active_cancel_event = None
            poll_thread.join(timeout=2.0)
            clear_execution_context()
            self._current_task_id = None
            self._current_task_epoch = None
            self._current_task_tool = None
            self._current_task_started_at = None
        if drain_timed_out and worker.is_alive():
            # Bounded drain exceeded: do NOT spool or post a result. The
            # worker is a daemon thread and dies with the process; the
            # task stays `running` server-side and the stale-lease sweep
            # re-queues it. Reporting a `failed` result here would mark a
            # task failed that may still be executing its side effects.
            self.backoff = 2.0
            self._backoff_attempts = 0
            return {"task": task_id, "status": "drain_timeout", "drained": False}
        status, text = result_box.get("status", "failed"), result_box.get(
            "text", "failed: worker thread ended without a result."
        )
        # Persist locally in spool before transmission
        self.spooler.spool(task_id, status, text)
        if self._try_post_result(task_id, status, text):
            self.spooler.mark_flushed(task_id)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] Task {task_id} result spooled offline (will flush upon reconnect).", flush=True)
        print(f"[{time.strftime('%H:%M:%S')}] Task {task_id} ({task['tool']}) -> {status}", flush=True)
        self.backoff = 2.0
        self._backoff_attempts = 0
        return {"task": task_id, "status": status}

    def _handle_ws_message(self, msg: dict) -> None:
        from ai_bridge.plane_control_flags import set_emergency_stop

        mtype = str(msg.get("type") or "")
        if mtype == "task_available":
            self._wake.set()
        elif mtype == "cancel":
            tid = str(msg.get("task_id") or "")
            with self._ws_cancel_lock:
                self._ws_cancel_task_id = tid or self._ws_cancel_task_id
            if self._active_cancel_event is not None:
                self._active_cancel_event.set()
            if msg.get("emergency_stop"):
                set_emergency_stop(True)
            self._wake.set()
        elif mtype == "emergency_stop":
            set_emergency_stop(bool(msg.get("active")))
            if msg.get("active") and self._active_cancel_event is not None:
                self._active_cancel_event.set()
            self._wake.set()
        elif mtype == "hello":
            set_emergency_stop(bool(msg.get("emergency_stop")))

    def _ws_loop(self) -> None:
        """Best-effort wake/cancel side-channel; HTTP claim remains authoritative."""
        try:
            from websockets.sync.client import connect as ws_connect
        except Exception:
            print(f"[{time.strftime('%H:%M:%S')}] Agent WS unavailable (install websockets); HTTP poll only.", flush=True)
            return
        base = self.control_url
        if base.startswith("https://"):
            ws_url = "wss://" + base[len("https://") :] + "/api/agent/ws"
        elif base.startswith("http://"):
            ws_url = "ws://" + base[len("http://") :] + "/api/agent/ws"
        else:
            ws_url = base.rstrip("/") + "/api/agent/ws"
        backoff = 2.0
        while not self._ws_stop.is_set():
            try:
                with ws_connect(
                    ws_url,
                    additional_headers={"X-Device-Token": self.device_token},
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws_connected.set()
                    backoff = 2.0
                    print(f"[{time.strftime('%H:%M:%S')}] Agent WS connected ({ws_url})", flush=True)
                    while not self._ws_stop.is_set():
                        try:
                            raw = ws.recv(timeout=30)
                        except TimeoutError:
                            try:
                                ws.send(json.dumps({"type": "heartbeat"}))
                            except Exception:
                                break
                            continue
                        if raw is None:
                            break
                        try:
                            msg = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else {}
                        except Exception:
                            continue
                        if isinstance(msg, dict):
                            if msg.get("type") == "ping":
                                try:
                                    ws.send(json.dumps({"type": "pong"}))
                                except Exception:
                                    break
                            self._handle_ws_message(msg)
            except Exception as exc:
                self._ws_connected.clear()
                if self._ws_stop.is_set():
                    break
                print(f"[{time.strftime('%H:%M:%S')}] Agent WS reconnect in {backoff:.0f}s: {exc}", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        self._ws_connected.clear()

    def start_side_channel(self) -> None:
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._ws_stop.clear()
        self._ws_thread = threading.Thread(target=self._ws_loop, name="ai-bridge-agent-ws", daemon=True)
        self._ws_thread.start()

    def stop_side_channel(self) -> None:
        self._ws_stop.set()
        self._wake.set()

    def run_forever(self) -> dict:
        """Poll forever until shutdown is requested or auth is terminal.

        F07-09/F07-12: the loop exits (instead of retrying forever) when
        shutdown is requested — after the in-flight task drains — or when
        the device token is revoked/expired. Returns a small summary dict
        (``{"revoked": bool, "drained": bool, ...}``) for entrypoints and
        tests; existing callers that ignore the return value are unaffected.
        """
        print(f"[{time.strftime('%H:%M:%S')}] AI Bridge Agent polling {self.control_url} ...", flush=True)
        self.start_side_channel()
        self.start_heartbeat_loop()
        had_error = False
        try:
            while not self._shutdown_requested.is_set():
                if self.revoked:
                    break
                try:
                    # Shorter long-poll when WS wake channel is up; full wait as fallback.
                    wait = 5 if self._ws_connected.is_set() else 15
                    self._wake.clear()
                    out = self.run_once(wait=wait)
                    if out.get("revoked"):
                        break
                    if had_error:
                        print(f"[{time.strftime('%H:%M:%S')}] Agent reconnected to control plane (OK).", flush=True)
                        had_error = False
                    self._backoff_attempts = 0
                    self.backoff = 2.0
                    if out.get("task"):
                        # A drain timeout ends the loop: shutdown was
                        # requested and the task did not finish in time.
                        if out.get("status") == "drain_timeout":
                            break
                        time.sleep(0.05)
                    elif self._wake.wait(timeout=0.5):
                        self._wake.clear()
                    else:
                        time.sleep(0.1)
                except (DeviceRevokedError, DeviceTokenExpiredError) as exc:
                    # Terminal auth (F07-12): ensure revoked flag is set even when
                    # raised directly from _req() on /api/agent/tasks/next rather
                    # than from _heartbeat_once().
                    if not self.revoked:
                        kind = "revoked" if isinstance(exc, DeviceRevokedError) else "expired"
                        self._mark_terminal_auth(kind, getattr(exc, "body", ""))
                    break
                except KeyboardInterrupt:
                    # Ctrl-C outside a task: request a graceful drain.
                    self.request_shutdown()
                    break
                except Exception as exc:
                    had_error = True
                    delay = backoff_delay(self._backoff_attempts)
                    self._backoff_attempts += 1
                    self.backoff = delay
                    print(f"[{time.strftime('%H:%M:%S')}] Agent error (retry in {delay:.0f}s): {exc}", flush=True)
                    # Sleep in small slices so a shutdown request during
                    # backoff takes effect promptly instead of waiting out
                    # the whole delay.
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        if self._shutdown_requested.is_set() or self.revoked:
                            break
                        time.sleep(min(0.2, deadline - time.monotonic()))
        finally:
            self._drain_and_stop()
        return {
            "revoked": self.revoked,
            "revoked_reason": self.revoked_reason,
            "drained": not self._drain_timed_out,
            "shutdown_requested": self._shutdown_requested.is_set(),
        }

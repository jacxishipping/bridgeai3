"""AI Bridge Linux Interactive Terminal Application (TUI Console & REPL).

Provides a zero-dependency, full-featured terminal user interface for Linux servers
and workstations:
- Full-screen curses dashboard (when an interactive TTY with curses is available)
  with 5 live tabs:
    [1] Overview & Telemetry (CPU/RAM/Disk gauges, Capabilities, Cloud & Policy posture)
    [2] Poller & Tasks (Start/Stop/Pause background Poller thread, Run-Once, live task history)
    [3] Enroll & Auth (Sign in with Control Plane username/password or Admin Token, Rotate Token)
    [4] Policy & Systemd (Inspect effective policy, Sync restrictive default, Systemd install/start/stop)
    [5] Doctor & Live Logs (8-point health audit and ring-buffered live log stream)
- Interactive line-oriented REPL menu fallback (`--repl` or non-curses terminals)
- Headless snapshot & JSON/ASCII rendering (`render_dashboard_text`, `collect_tui_state`)
  for automated tests, CI verification, and SSH non-interactive inspection.
"""
from __future__ import annotations

import collections
import datetime
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Ensure repo src/ and agents/ are importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
if str(_REPO_ROOT / "agents") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "agents"))

from common.poller import Poller  # noqa: E402

from ai_bridge.control.desktop_capabilities import probe_local_capabilities  # noqa: E402


def _ascii_bar(percent: float, width: int = 22) -> str:
    pct = max(0.0, min(100.0, float(percent or 0.0)))
    filled = int(round((pct / 100.0) * width))
    empty = max(0, width - filled)
    return f"[{'#' * filled}{'-' * empty}] {pct:5.1f}%"


class LinuxTuiController:
    """State and background poller controller for the Linux Terminal Application."""

    def __init__(
        self,
        control_url: str = "",
        device_token: str = "",
        token_file: Path | None = None,
        config_dir: Path | None = None,
    ) -> None:
        from linux import agent_main as linux_agent

        self._linux_agent = linux_agent
        self.config_dir = config_dir or linux_agent.DEFAULT_CONFIG_DIR
        self.cfg = linux_agent.load_agent_config(self.config_dir)
        self.control_url = (control_url or self.cfg.get("control_url") or linux_agent.DEFAULT_CONTROL_URL).rstrip("/")
        tok, tfile = linux_agent._resolve_token_and_file(device_token or self.cfg.get("device_token", ""), str(token_file or ""))
        self.device_token = (tok or "").lstrip("\ufeff").strip()
        self.token_file = tfile or (self.config_dir / "device.token")

        self.poller: Poller | None = None
        self.poller_thread: threading.Thread | None = None
        self.poller_running = False
        self.paused = False

        self.device_id = "unregistered"
        self.cloud_connected = False
        self.cloud_error = ""
        self.effective_policy: dict[str, Any] = {}
        self.emergency_stop = False
        self.last_cloud_check = 0.0

        self.logs: collections.deque[str] = collections.deque(maxlen=150)
        self.task_history: collections.deque[dict[str, Any]] = collections.deque(maxlen=50)
        self._lock = threading.Lock()
        self.log("AI Bridge Linux Terminal Console initialized.")

    def log(self, message: str, level: str = "INFO") -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] [{level}] {message}"
        with self._lock:
            self.logs.append(entry)

    def sample_telemetry(self) -> dict[str, Any]:
        return self._linux_agent.gather_linux_telemetry()

    def sample_capabilities(self) -> dict[str, Any]:
        try:
            return probe_local_capabilities()
        except Exception:
            return {
                "platform_family": "Linux",
                "display_server": "x11" if os.environ.get("DISPLAY") else "headless",
                "transports": ["screen_relay", "jpeg"],
                "recommended_transport": "screen_relay",
                "a11y_backend": "linux_atspi",
                "runtimes": ["python", "bash"],
            }

    def systemd_status(self) -> str:
        if not shutil.which("systemctl"):
            return "unavailable"
        try:
            is_root = os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0
            cmd = ["systemctl"] if is_root else ["systemctl", "--user"]
            res = subprocess.run(
                cmd + ["is-active", "ai-bridge-agent.service"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            state = (res.stdout or "").strip()
            return state or "inactive"
        except Exception:
            return "unknown"

    def probe_control_plane(self) -> dict[str, Any]:
        """Query `/api/agent/whoami` and `/api/agent/policy` to refresh live connection state."""
        self.last_cloud_check = time.time()
        if not self.device_token:
            self.cloud_connected = False
            self.device_id = "unregistered"
            self.cloud_error = "No device token configured"
            return {"connected": False, "error": self.cloud_error}

        headers = {
            "X-Device-Token": self.device_token,
            "User-Agent": "AI-Bridge-Linux-TUI/0.3.0",
        }
        try:
            req = urllib.request.Request(f"{self.control_url}/api/agent/whoami", headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.device_id = str(data.get("device_id") or "unknown")
                self.cloud_connected = True
                self.cloud_error = ""
        except Exception as exc:
            self.cloud_connected = False
            self.cloud_error = str(exc)
            return {"connected": False, "error": self.cloud_error}

        try:
            preq = urllib.request.Request(f"{self.control_url}/api/agent/policy", headers=headers)
            with urllib.request.urlopen(preq, timeout=5) as resp:
                pdata = json.loads(resp.read().decode("utf-8"))
                self.effective_policy = dict(pdata.get("effective_policy") or {})
        except Exception:
            pass

        return {
            "connected": self.cloud_connected,
            "device_id": self.device_id,
            "effective_policy": self.effective_policy,
        }

    def enroll_with_admin_token(self, control_url: str, admin_token: str, device_name: str = "") -> dict[str, Any]:
        clean_url = (control_url or self.control_url).strip().rstrip("/")
        if not admin_token.strip():
            raise ValueError("Admin token is required.")
        name = (device_name or platform.node()).strip() or platform.node()
        payload = {
            "name": name,
            "hostname": platform.node(),
            "os": "Linux",
            "tenant": "default",
        }
        req = urllib.request.Request(
            f"{clean_url}/api/devices/enroll",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Admin-Token": admin_token.strip(),
                "User-Agent": "AI-Bridge-Linux-TUI/0.3.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        dev_tok = str(res.get("device_token") or "")
        dev_id = str(res.get("device_id") or "")
        if not dev_tok:
            raise RuntimeError("Server did not return a device_token.")
        self.control_url = clean_url
        self.device_token = dev_tok
        self.device_id = dev_id
        self.cfg["control_url"] = clean_url
        self.cfg["device_token"] = dev_tok
        self.cfg["device_name"] = name
        self._linux_agent.save_agent_config(self.cfg, self.config_dir)
        self._linux_agent.sync_linux_server_policy(clean_url, dev_tok, notify=False)
        self.log(f"Enrolled device '{name}' as {dev_id} on {clean_url}", "OK")
        self.probe_control_plane()
        return {"ok": True, "device_id": dev_id, "device_name": name, "control_url": clean_url}

    def login_and_enroll(
        self,
        control_url: str,
        username: str,
        password: str,
        device_name: str = "",
        tenant: str = "default",
    ) -> dict[str, Any]:
        """Authenticate with operator/admin username & password and enroll this Linux host."""
        clean_url = (control_url or self.control_url).strip().rstrip("/")
        login_req = urllib.request.Request(
            f"{clean_url}/api/auth/login",
            data=json.dumps({"username": username.strip(), "password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AI-Bridge-Linux-TUI/0.3.0"},
            method="POST",
        )
        with urllib.request.urlopen(login_req, timeout=10) as resp:
            login_res = json.loads(resp.read().decode("utf-8"))
        session_token = str(login_res.get("session_token") or "")
        resolved_tenant = str(login_res.get("tenant") or tenant or "default")
        if not session_token:
            raise RuntimeError("Login succeeded but no session_token was returned.")

        name = (device_name or platform.node()).strip() or platform.node()
        enroll_req = urllib.request.Request(
            f"{clean_url}/api/devices/enroll",
            data=json.dumps({
                "name": name,
                "hostname": platform.node(),
                "os": "Linux",
                "tenant": resolved_tenant,
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Session-Token": session_token,
                "User-Agent": "AI-Bridge-Linux-TUI/0.3.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(enroll_req, timeout=10) as resp:
            enroll_res = json.loads(resp.read().decode("utf-8"))
        dev_tok = str(enroll_res.get("device_token") or "")
        dev_id = str(enroll_res.get("device_id") or "")
        if not dev_tok:
            raise RuntimeError("Enrollment did not return a device_token.")

        self.control_url = clean_url
        self.device_token = dev_tok
        self.device_id = dev_id
        self.cfg["control_url"] = clean_url
        self.cfg["device_token"] = dev_tok
        self.cfg["device_name"] = name
        self._linux_agent.save_agent_config(self.cfg, self.config_dir)
        self._linux_agent.sync_linux_server_policy(clean_url, dev_tok, notify=False)
        self.log(f"Signed in as {username} ({resolved_tenant}) and enrolled device {dev_id}", "OK")
        self.probe_control_plane()
        return {"ok": True, "device_id": dev_id, "tenant": resolved_tenant, "device_name": name}

    def rotate_token(self) -> dict[str, Any]:
        if not self.device_token:
            raise RuntimeError("No device token configured.")
        req = urllib.request.Request(
            f"{self.control_url}/api/agent/rotate",
            headers={"X-Device-Token": self.device_token, "User-Agent": "AI-Bridge-Linux-TUI/0.3.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        new_tok = str(data.get("device_token") or "")
        if not new_tok:
            raise RuntimeError("No new device_token returned.")
        self.device_token = new_tok
        self.cfg["device_token"] = new_tok
        self._linux_agent.save_agent_config(self.cfg, self.config_dir)
        self.log("Device token rotated and saved to 0600 device.token", "OK")
        return {"ok": True, "token_expires_at": data.get("token_expires_at")}

    def sync_restrictive_policy(self) -> bool:
        ok = self._linux_agent.sync_linux_server_policy(self.control_url, self.device_token, notify=False)
        if ok:
            self.log("Synchronized restrictive default policy with Control Plane.", "OK")
            self.probe_control_plane()
        else:
            self.log("Failed to sync restrictive policy with Control Plane.", "WARN")
        return ok

    def start_poller(self) -> bool:
        if self.poller_running:
            self.log("Background poller is already running.", "WARN")
            return True
        if not self.device_token:
            self.log("Cannot start poller: enroll or configure a device token first.", "ERROR")
            return False

        policy = self._linux_agent.get_linux_server_policy()
        self.poller = Poller(self.control_url, self.device_token, policy=policy, token_file=self.token_file)
        self.poller_running = True
        self.paused = False

        def _loop() -> None:
            self.log(f"Background poller started -> {self.control_url}", "OK")
            while self.poller_running and self.poller is not None and not self.poller.revoked:
                if self.paused:
                    time.sleep(0.5)
                    continue
                try:
                    res = self.poller.run_once()
                    if isinstance(res, dict) and res.get("task_id"):
                        entry = {
                            "task_id": res.get("task_id"),
                            "tool": res.get("tool", "task"),
                            "status": res.get("status", "completed"),
                            "ts": datetime.datetime.now().strftime("%H:%M:%S"),
                        }
                        with self._lock:
                            self.task_history.appendleft(entry)
                        self.log(f"Executed task {entry['task_id']} ({entry['tool']}) -> {entry['status']}", "OK")
                except Exception as exc:
                    self.log(f"Poll cycle warning: {exc}", "WARN")
                    time.sleep(2.0)
            self.poller_running = False
            self.log("Background poller stopped.", "INFO")

        self.poller_thread = threading.Thread(target=_loop, name="ai-bridge-linux-tui-poller", daemon=True)
        self.poller_thread.start()
        return True

    def stop_poller(self) -> None:
        if not self.poller_running:
            return
        self.poller_running = False
        if self.poller is not None:
            try:
                self.poller.request_shutdown()
            except Exception:
                pass
        self.log("Requested background poller stop.", "INFO")

    def run_once_poll(self) -> dict[str, Any]:
        if not self.device_token:
            raise RuntimeError("No device token configured.")
        policy = self._linux_agent.get_linux_server_policy()
        poller = Poller(self.control_url, self.device_token, policy=policy, token_file=self.token_file)
        res = poller.run_once()
        if isinstance(res, dict) and res.get("task_id"):
            entry = {
                "task_id": res.get("task_id"),
                "tool": res.get("tool", "task"),
                "status": res.get("status", "completed"),
                "ts": datetime.datetime.now().strftime("%H:%M:%S"),
            }
            with self._lock:
                self.task_history.appendleft(entry)
            self.log(f"Single poll executed task {entry['task_id']} -> {entry['status']}", "OK")
        else:
            self.log("Single poll cycle completed (no queued tasks).", "INFO")
        return res if isinstance(res, dict) else {"result": res}

    def collect_state(self, *, probe_cloud: bool = False) -> dict[str, Any]:
        if probe_cloud or (self.device_token and (time.time() - self.last_cloud_check) > 20.0):
            try:
                self.probe_control_plane()
            except Exception:
                pass
        telem = self.sample_telemetry()
        caps = self.sample_capabilities()
        with self._lock:
            recent_logs = list(self.logs)[-25:]
            recent_tasks = list(self.task_history)[:15]
        return {
            "schema": "ai-bridge-linux-tui-state/v1",
            "control_url": self.control_url,
            "device_id": self.device_id,
            "device_name": self.cfg.get("device_name") or telem["hostname"],
            "has_token": bool(self.device_token),
            "masked_token": (
                f"{self.device_token[:6]}...{self.device_token[-4:]}"
                if len(self.device_token) > 12
                else ("configured" if self.device_token else "none")
            ),
            "cloud_connected": self.cloud_connected,
            "cloud_error": self.cloud_error,
            "poller_running": self.poller_running,
            "poller_paused": self.paused,
            "systemd_state": self.systemd_status(),
            "telemetry": telem,
            "capabilities": caps,
            "effective_policy": self.effective_policy,
            "recent_tasks": recent_tasks,
            "recent_logs": recent_logs,
        }


def render_dashboard_text(state: dict[str, Any]) -> str:
    """Render a clean ASCII/ANSI summary of the Linux Terminal Console state."""
    telem = state.get("telemetry") or {}
    caps = state.get("capabilities") or {}
    pol = state.get("effective_policy") or {}
    cloud_str = "CONNECTED" if state.get("cloud_connected") else f"OFFLINE ({state.get('cloud_error') or 'unverified'})"
    poller_str = "PAUSED" if state.get("poller_paused") else ("RUNNING" if state.get("poller_running") else "STOPPED")
    max_tier = pol.get("max_tier_unattended", 2)
    pol_mode = "UNRESTRICTED (Tier 4)" if pol.get("unrestricted") else f"RESTRICTED (Ceiling: Tier {max_tier})"

    lines = [
        "==============================================================================",
        "  AI BRIDGE LINUX TERMINAL STUDIO (v0.3.0) — INTERACTIVE SERVER & DESKTOP APP",
        "==============================================================================",
        f"  Host:          {telem.get('hostname', 'linux')} ({telem.get('os', '')} · {telem.get('arch', '')})",
        f"  Control Plane: {state.get('control_url')}  [{cloud_str}]",
        f"  Device ID:     {state.get('device_id')}  (Token: {state.get('masked_token')})",
        f"  Agent Poller:  {poller_str}   |   Systemd Service: {str(state.get('systemd_state', 'unknown')).upper()}",
        f"  Policy Mode:   {pol_mode}",
        "------------------------------------------------------------------------------",
        f"  CPU Usage:     {_ascii_bar(telem.get('cpu_percent', 0.0))}  ({telem.get('cpu_count', 1)} cores, load {telem.get('load_avg', [0, 0, 0])})",
        f"  Memory Usage:  {_ascii_bar(telem.get('memory_percent', 0.0))}  ({telem.get('memory_used_gb', 0)}/{telem.get('memory_total_gb', 0)} GB)",
        f"  Disk Usage:    {_ascii_bar(telem.get('disk_percent', 0.0))}  ({telem.get('disk_free_gb', 0)} GB free / {telem.get('disk_total_gb', 0)} GB)",
        "------------------------------------------------------------------------------",
        f"  Display/A11y:  display={caps.get('display_server', 'headless')} | a11y={caps.get('a11y_backend', 'linux_atspi')}",
        f"  Transports:    {', '.join(caps.get('transports') or ['screen_relay', 'jpeg'])} (recommended: {caps.get('recommended_transport', 'screen_relay')})",
        f"  Runtimes:      {', '.join(caps.get('runtimes') or ['python'])}",
        "------------------------------------------------------------------------------",
        "  Recent Tasks:",
    ]
    tasks = state.get("recent_tasks") or []
    if not tasks:
        lines.append("    (no tasks executed in this session yet)")
    else:
        for t in tasks[:5]:
            lines.append(f"    - [{t.get('ts')}] {t.get('task_id')}  {t.get('tool')} -> {t.get('status')}")

    lines.append("------------------------------------------------------------------------------")
    lines.append("  Recent Console Logs:")
    for lg in (state.get("recent_logs") or [])[-6:]:
        lines.append(f"    {lg}")
    lines.append("==============================================================================")
    return "\n".join(lines)


def run_interactive_repl(controller: LinuxTuiController) -> None:
    """Interactive keyboard-driven terminal menu (works over any SSH / serial / container TTY)."""
    controller.probe_control_plane()
    while True:
        state = controller.collect_state(probe_cloud=False)
        print("\n" + render_dashboard_text(state))
        print(
            "\nCommands:\n"
            "  [1] Refresh Status & Cloud Probe     [6] Enroll with Username & Password\n"
            "  [2] Start / Stop Background Poller   [7] Rotate Device Token\n"
            "  [3] Run Single Poll Cycle (--once)   [8] Sync Restrictive Default Policy\n"
            "  [4] Pause / Resume Poller            [9] Run Full System Doctor Audit\n"
            "  [5] Enroll with Admin Token          [q] Quit Terminal Studio\n"
        )
        try:
            choice = input("ai-bridge-tui> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting AI Bridge Linux Terminal Studio...")
            controller.stop_poller()
            break

        if choice in ("q", "quit", "exit"):
            controller.stop_poller()
            break
        elif choice in ("1", "r", "refresh", "status"):
            controller.probe_control_plane()
        elif choice in ("2", "start", "stop"):
            if controller.poller_running:
                controller.stop_poller()
            else:
                controller.start_poller()
        elif choice in ("3", "once"):
            try:
                controller.run_once_poll()
            except Exception as exc:
                controller.log(f"Single poll failed: {exc}", "ERROR")
        elif choice in ("4", "pause"):
            controller.paused = not controller.paused
            controller.log(f"Poller {'PAUSED' if controller.paused else 'RESUMED'}.", "INFO")
        elif choice in ("5", "enroll"):
            try:
                url = input(f"Control Plane URL [{controller.control_url}]: ").strip() or controller.control_url
                name = input(f"Device Name [{platform.node()}]: ").strip() or platform.node()
                adm = getpass.getpass("Admin Token: ").strip()
                controller.enroll_with_admin_token(url, adm, name)
            except Exception as exc:
                controller.log(f"Enrollment failed: {exc}", "ERROR")
        elif choice in ("6", "login"):
            try:
                url = input(f"Control Plane URL [{controller.control_url}]: ").strip() or controller.control_url
                user = input("Username: ").strip()
                pw = getpass.getpass("Password: ")
                name = input(f"Device Name [{platform.node()}]: ").strip() or platform.node()
                controller.login_and_enroll(url, user, pw, name)
            except Exception as exc:
                controller.log(f"Login & enrollment failed: {exc}", "ERROR")
        elif choice in ("7", "rotate"):
            try:
                controller.rotate_token()
            except Exception as exc:
                controller.log(f"Token rotation failed: {exc}", "ERROR")
        elif choice in ("8", "policy"):
            controller.sync_restrictive_policy()
        elif choice in ("9", "doctor"):
            controller._linux_agent.run_doctor(controller.control_url, controller.device_token)
            input("\nPress Enter to return to Terminal Studio...")


def run_curses_tui(controller: LinuxTuiController) -> None:
    """Full-screen curses interactive TUI with 5 tabs and live hotkeys."""
    import curses

    controller.probe_control_plane()

    def _curses_main(stdscr: Any) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(500)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)

        active_tab = 1
        status_banner = "Ready — Press 1..5 to switch tabs, [s] Start/Stop Poller, [o] Run-Once, [r] Refresh, [q] Quit"

        def _prompt_input(prompt_label: str, secret: bool = False) -> str:
            stdscr.nodelay(False)
            h, w = stdscr.getmaxyx()
            stdscr.move(h - 2, 2)
            stdscr.clrtoeol()
            stdscr.addstr(h - 2, 2, prompt_label[: w - 4], curses.A_BOLD)
            stdscr.refresh()
            if not secret:
                curses.echo()
            curses.curs_set(1)
            try:
                raw = stdscr.getstr(h - 2, min(w - 2, len(prompt_label) + 3), 160)
                return raw.decode("utf-8", errors="ignore").strip()
            except Exception:
                return ""
            finally:
                curses.noecho()
                curses.curs_set(0)
                stdscr.nodelay(True)
                stdscr.timeout(500)

        while True:
            state = controller.collect_state(probe_cloud=False)
            telem = state["telemetry"]
            caps = state["capabilities"]
            pol = state["effective_policy"]

            stdscr.erase()
            h, w = stdscr.getmaxyx()

            def _put(y: int, x: int, text: str, attr: int = 0) -> None:
                if 0 <= y < h - 1 and 0 <= x < w - 1:
                    try:
                        stdscr.addstr(y, x, text[: max(0, w - x - 1)], attr)
                    except Exception:
                        pass

            # Top header bar
            c_cyan = curses.color_pair(1) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
            c_green = curses.color_pair(2) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
            c_yellow = curses.color_pair(3) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
            c_red = curses.color_pair(4) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD

            _put(0, 2, "AI BRIDGE LINUX TERMINAL STUDIO v0.3.0", c_cyan)
            cloud_badge = "● CLOUD ONLINE" if state["cloud_connected"] else "○ CLOUD OFFLINE"
            _put(0, max(2, w - len(cloud_badge) - 4), cloud_badge, c_green if state["cloud_connected"] else c_red)

            # Navigation Tabs
            tabs = [
                (1, "1:Overview"),
                (2, "2:Poller & Tasks"),
                (3, "3:Enroll & Auth"),
                (4, "4:Policy & Systemd"),
                (5, "5:Logs & Capabilities"),
            ]
            tx = 2
            for idx, label in tabs:
                style = (curses.A_REVERSE | curses.A_BOLD) if active_tab == idx else curses.A_DIM
                _put(2, tx, f" {label} ", style)
                tx += len(label) + 4

            _put(3, 2, "─" * max(10, w - 4))

            if active_tab == 1:
                _put(5, 4, f"Host & OS:        {telem['hostname']} ({telem['os']} · {telem['arch']})", curses.A_BOLD)
                _put(6, 4, f"Control Plane:    {state['control_url']}")
                _put(7, 4, f"Device ID:        {state['device_id']}  (Token: {state['masked_token']})")
                p_state = "PAUSED" if state["poller_paused"] else ("RUNNING" if state["poller_running"] else "STOPPED")
                _put(8, 4, f"In-App Poller:    {p_state}", c_green if state["poller_running"] else c_yellow)
                _put(9, 4, f"Systemd Service:  {str(state['systemd_state']).upper()}")

                _put(11, 4, "Live Hardware Telemetry:", c_cyan)
                _put(12, 6, f"CPU:    {_ascii_bar(telem['cpu_percent'])}  ({telem['cpu_count']} cores, load {telem['load_avg']})")
                _put(13, 6, f"Memory: {_ascii_bar(telem['memory_percent'])}  ({telem['memory_used_gb']}/{telem['memory_total_gb']} GB)")
                _put(14, 6, f"Disk:   {_ascii_bar(telem['disk_percent'])}  ({telem['disk_free_gb']}/{telem['disk_total_gb']} GB free)")

                _put(16, 4, "Quick Actions:", c_cyan)
                _put(17, 6, "[s] Start/Stop Poller   [o] Run Single Poll Cycle   [p] Pause/Resume   [r] Probe Cloud")

            elif active_tab == 2:
                p_state = "PAUSED" if state["poller_paused"] else ("RUNNING" if state["poller_running"] else "STOPPED")
                _put(5, 4, f"Background Agent Poller: {p_state}", c_green if state["poller_running"] else c_yellow)
                _put(6, 4, "Keys: [s] Start/Stop Poller   [p] Pause/Resume   [o] Run Single Poll Cycle")
                _put(8, 4, "Recent Task Executions:", c_cyan)
                tasks = state["recent_tasks"]
                if not tasks:
                    _put(10, 6, "No tasks executed in this session yet. Press [s] to start polling or [o] to run once.")
                else:
                    for i, t in enumerate(tasks[: max(1, h - 14)]):
                        _put(10 + i, 6, f"[{t.get('ts')}] Task {t.get('task_id')} | Tool: {t.get('tool')} | Status: {t.get('status')}")

            elif active_tab == 3:
                _put(5, 4, "Enrollment & Credential Management", c_cyan)
                _put(6, 6, f"Control Plane URL: {state['control_url']}")
                _put(7, 6, f"Device ID:         {state['device_id']}")
                _put(8, 6, f"Device Token:      {state['masked_token']} (stored in 0600 device.token)")
                _put(10, 4, "Interactive Enrollment Actions:", c_cyan)
                _put(11, 6, "[e] Enroll Device with Admin Token")
                _put(12, 6, "[l] Sign In with Operator Username & Password and Auto-Enroll")
                _put(13, 6, "[u] Change Control Plane URL")
                _put(14, 6, "[t] Rotate Device Token Now")

            elif active_tab == 4:
                _put(5, 4, "Effective Policy & Systemd Service Governance", c_cyan)
                unrest = bool(pol.get("unrestricted"))
                _put(
                    6,
                    6,
                    f"Policy Mode:          {'UNRESTRICTED (Tier 4)' if unrest else 'RESTRICTED DEFAULT'}",
                    c_green if unrest else c_yellow,
                )
                _put(7, 6, f"Unattended Ceiling:   Tier {pol.get('max_tier_unattended', 2)}")
                _put(8, 6, f"Allow Writes / Shell: writes={bool(pol.get('allow_writes'))} | shell={bool(pol.get('allow_shell'))}")
                _put(9, 6, f"Systemd Unit Status:  {str(state['systemd_state']).upper()} (ai-bridge-agent.service)")
                _put(11, 4, "Governance Actions:", c_cyan)
                _put(12, 6, "[y] Sync Restrictive Default Policy to Control Plane")
                _put(13, 6, "[r] Refresh Effective Policy from Control Plane")

            elif active_tab == 5:
                _put(5, 4, "Cross-Platform Capability Discovery:", c_cyan)
                _put(6, 6, f"Display Server: {caps.get('display_server')}  |  A11y Backend: {caps.get('a11y_backend')}")
                _put(7, 6, f"Transports:     {', '.join(caps.get('transports') or [])} (recommended: {caps.get('recommended_transport')})")
                _put(8, 6, f"Runtimes:       {', '.join(caps.get('runtimes') or [])}")
                _put(10, 4, "Live Session Logs:", c_cyan)
                logs = state["recent_logs"]
                max_log_rows = max(1, h - 14)
                for i, lg in enumerate(logs[-max_log_rows:]):
                    _put(11 + i, 6, lg)

            # Footer status bar
            _put(h - 3, 2, "─" * max(10, w - 4))
            _put(h - 2, 2, status_banner)
            stdscr.refresh()

            ch = stdscr.getch()
            if ch == -1:
                continue
            if ch in (ord("q"), ord("Q")):
                controller.stop_poller()
                break
            elif ch in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5")):
                active_tab = ch - ord("0")
            elif ch == curses.KEY_RIGHT:
                active_tab = (active_tab % 5) + 1
            elif ch == curses.KEY_LEFT:
                active_tab = 5 if active_tab == 1 else active_tab - 1
            elif ch in (ord("r"), ord("R")):
                res = controller.probe_control_plane()
                status_banner = (
                    f"Cloud probe OK: device_id={res.get('device_id')}"
                    if res.get("connected")
                    else f"Cloud probe failed: {res.get('error')}"
                )
            elif ch in (ord("s"), ord("S")):
                if controller.poller_running:
                    controller.stop_poller()
                    status_banner = "Stopped background poller."
                else:
                    ok = controller.start_poller()
                    status_banner = "Started background poller." if ok else "Failed to start poller (missing token)."
            elif ch in (ord("p"), ord("P")):
                controller.paused = not controller.paused
                status_banner = f"Poller {'PAUSED' if controller.paused else 'RESUMED'}."
            elif ch in (ord("o"), ord("O")):
                try:
                    controller.run_once_poll()
                    status_banner = "Executed single poll cycle."
                except Exception as exc:
                    status_banner = f"Single poll error: {exc}"
            elif ch in (ord("y"), ord("Y")):
                ok = controller.sync_restrictive_policy()
                status_banner = "Synced restrictive default policy." if ok else "Policy sync failed."
            elif ch in (ord("t"), ord("T")):
                try:
                    controller.rotate_token()
                    status_banner = "Rotated device token and saved to device.token."
                except Exception as exc:
                    status_banner = f"Token rotation failed: {exc}"
            elif ch in (ord("u"), ord("U")):
                new_url = _prompt_input(f"New Control Plane URL [{controller.control_url}]: ")
                if new_url:
                    controller.control_url = new_url.rstrip("/")
                    controller.cfg["control_url"] = controller.control_url
                    controller._linux_agent.save_agent_config(controller.cfg, controller.config_dir)
                    status_banner = f"Updated Control Plane URL to {controller.control_url}"
            elif ch in (ord("e"), ord("E")):
                adm = _prompt_input("Enter Control Plane Admin Token: ", secret=True)
                if adm:
                    name = _prompt_input(f"Device Name [{platform.node()}]: ") or platform.node()
                    try:
                        res = controller.enroll_with_admin_token(controller.control_url, adm, name)
                        status_banner = f"Enrolled as {res['device_id']}!"
                    except Exception as exc:
                        status_banner = f"Enrollment error: {exc}"
            elif ch in (ord("l"), ord("L")):
                user = _prompt_input("Control Plane Username: ")
                if user:
                    pw = _prompt_input("Control Plane Password: ", secret=True)
                    name = _prompt_input(f"Device Name [{platform.node()}]: ") or platform.node()
                    try:
                        res = controller.login_and_enroll(controller.control_url, user, pw, name)
                        status_banner = f"Signed in & enrolled as {res['device_id']}!"
                    except Exception as exc:
                        status_banner = f"Sign-in enrollment error: {exc}"

    curses.wrapper(_curses_main)


def launch_linux_tui(
    *,
    control_url: str = "",
    device_token: str = "",
    token_file: Path | None = None,
    mode: str = "auto",
    snapshot_only: bool = False,
    json_output: bool = False,
) -> int:
    """Entrypoint for `ai-bridge-agent tui` and `ai-bridge-linux-tui`."""
    controller = LinuxTuiController(
        control_url=control_url,
        device_token=device_token,
        token_file=token_file,
    )
    if snapshot_only:
        state = controller.collect_state(probe_cloud=bool(controller.device_token))
        if json_output:
            print(json.dumps(state, indent=2))
        else:
            print(render_dashboard_text(state))
        return 0

    chosen_mode = (mode or "auto").strip().lower()
    if chosen_mode == "repl" or not sys.stdin.isatty() or not sys.stdout.isatty():
        run_interactive_repl(controller)
        return 0

    try:
        import curses  # noqa: F401

        run_curses_tui(controller)
        return 0
    except Exception:
        run_interactive_repl(controller)
        return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="ai-bridge-linux-tui",
        description="AI Bridge Linux Interactive Terminal Studio (Full-screen Curses TUI & REPL).",
    )
    parser.add_argument("--control-url", default=os.environ.get("AI_BRIDGE_CONTROL_URL", ""))
    parser.add_argument("--device-token", default=os.environ.get("AI_BRIDGE_DEVICE_TOKEN", ""))
    parser.add_argument("--token-file", default=os.environ.get("AI_BRIDGE_TOKEN_FILE", ""))
    parser.add_argument(
        "--mode",
        choices=["auto", "curses", "repl"],
        default="auto",
        help="UI mode: 'auto' (curses with REPL fallback), 'curses', or 'repl'",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Render a single non-interactive dashboard snapshot and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --snapshot, emit the structured TUI state as JSON.",
    )
    args = parser.parse_args()
    tfile = Path(args.token_file) if args.token_file else None
    code = launch_linux_tui(
        control_url=args.control_url,
        device_token=args.device_token,
        token_file=tfile,
        mode=args.mode,
        snapshot_only=args.snapshot,
        json_output=args.json,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()

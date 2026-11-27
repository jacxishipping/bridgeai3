"""AI Bridge macOS Agent — Desktop polling, LaunchAgent management & Terminal Studio.

Provides a macOS desktop agent runtime with launchd service management, TCC
permission checks, and the Interactive Terminal Studio (curses / REPL).

Key Capabilities:
- Restrictive Default Policy: Read-only diagnostics by default (Tier 1-2,
  unattended ceiling 2, no unrestricted mode). Tier 3/4 operations require
  explicit admin opt-in on the control plane plus human approval — see
  get_macos_agent_policy() and the R-P0-08 audit fix.
- Multi-Command CLI: run, status, doctor, config, policy, rotate-token, service, enroll, update.
- Automated Maintenance: Proactive token rotation, heartbeat telemetry, self-healing backoff.
- launchd LaunchAgent Management: One-command install/uninstall, status inspection, and live logs.
- Environment & Health Auditing: Comprehensive system doctor verifying network, disk, and runtime.

Usage:
  python agents/macos/agent_main.py [command] [options]
  # Or use the all-in-one runner:
  python agents/macos/agent_main.py [command] [options]
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Insert source and common agent paths
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.poller import Poller  # noqa: E402

from ai_bridge.policy import Policy  # noqa: E402

# Configuration Paths (macOS LaunchAgent runs as the logged-in user)
DEFAULT_CONFIG_DIR = Path.home() / ".ai-bridge"
DEFAULT_WORKSPACE = Path.home() / ".ai-bridge" / "workspace"
LAUNCHD_LABEL = "com.aibridge.agent"

CONFIG_JSON_FILE = DEFAULT_CONFIG_DIR / "agent.json"
CONFIG_ENV_FILE = DEFAULT_CONFIG_DIR / "agent.env"
TOKEN_FILE = DEFAULT_CONFIG_DIR / "device.token"
DEFAULT_CONTROL_URL = "https://jxiptvplayer.app"

# Terminal Colors
HAS_TTY = sys.stdout.isatty()
CLR_RESET = "\033[0m" if HAS_TTY else ""
CLR_BOLD = "\033[1m" if HAS_TTY else ""
CLR_RED = "\033[0;31m" if HAS_TTY else ""
CLR_GREEN = "\033[0;32m" if HAS_TTY else ""
CLR_YELLOW = "\033[1;33m" if HAS_TTY else ""
CLR_BLUE = "\033[0;34m" if HAS_TTY else ""
CLR_CYAN = "\033[0;36m" if HAS_TTY else ""
CLR_MAGENTA = "\033[0;35m" if HAS_TTY else ""


def log_info(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{CLR_CYAN}[INFO {ts}]{CLR_RESET} {msg}")


def log_ok(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{CLR_GREEN}[OK   {ts}]{CLR_RESET} {msg}")


def log_warn(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{CLR_YELLOW}[WARN {ts}]{CLR_RESET} {msg}")


def log_error(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{CLR_RED}[FAIL {ts}]{CLR_RESET} {msg}", file=sys.stderr)


# -----------------------------------------------------------------------------
# Restrictive Default Policy (R-P0-08)
#
# The Linux agent ships with a restrictive default: read-only diagnostics,
# unattended ceiling Tier 2, no unrestricted mode. Tier 3/4 operations
# (shell, packages, services, file deletion, desktop control) require
# EXPLICIT admin opt-in on the control plane plus human approval for the
# task. Do not restore the old unrestricted Tier-4 default here: a default
# that pre-authorises destructive operations is the single most dangerous
# default this agent ever shipped (audit F07-02 / R-P0-08). An admin who
# genuinely needs wider powers must set them explicitly on the plane
# (device policy) — never by editing this default back to "*".
# -----------------------------------------------------------------------------
#: Explicit, small, read-only tool allowlist for the restrictive default.
#: Every entry is verified in ai_bridge.policy.TOOL_TIERS as READ_ONLY.
#: No "*" entries: a wildcard here would silently re-open the whole surface.
MACOS_DEFAULT_ALLOWED_TOOLS = frozenset(
    {
        "get_system_info",
        "get_resource_usage",
        "get_disk_usage",
        "get_memory_usage",
        "list_workspace",
        "read_workspace_file",
        "search_workspace",
        "git_status",
        "git_diff",
        "list_processes",
        "get_service_status",
        "get_recent_logs",
        "list_docker_containers",
        "get_task_status",
        "get_task_result",
        "desktop_get_status",
        "desktop_a11y_status",
        "desktop_a11y_permissions",
        "desktop_get_monitors",
        "desktop_a11y_list_apps",
    }
)


def get_macos_agent_policy() -> Policy:
    """Return the restrictive default policy for macOS desktop agents.

    Read-only diagnostics only; Tier 3/4 need explicit admin opt-in.
    """
    return Policy(
        allow_writes=False,
        allow_tier3=False,
        desktop_enabled=True,
        max_tier_unattended=2,
        unrestricted=False,
        allowed_apps=frozenset(),
        allowed_services=frozenset(),
        allowed_test_commands=frozenset({"pytest", "pytest -q"}),
        allowed_tools=MACOS_DEFAULT_ALLOWED_TOOLS,
        denied_tools=frozenset(),
    )


def sync_macos_agent_policy(control_url: str, token: str, notify: bool = False) -> bool:
    """Synchronize the restrictive default policy to the Control Plane.

    NOTE: this used to push an unrestricted Tier-4 policy on every start
    (audit F07-02). It now pushes the restrictive default above. Admins
    widen a device's policy on the plane itself; the agent never pushes a
    widening policy.
    """
    if not control_url or not token:
        return False
    payload = {
        "inherit_global": False,
        "unrestricted": False,
        "allow_writes": False,
        "allow_tier3": False,
        "desktop_enabled": True,
        "allow_shell": False,
        "allow_package_install": False,
        "allow_file_deletion": False,
        "max_tier_unattended": 2,
        "allowed_tools": sorted(MACOS_DEFAULT_ALLOWED_TOOLS),
        "note": "Restrictive macOS default policy (read-only diagnostics; Tier 3/4 need explicit admin opt-in + approval)",
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{control_url.rstrip('/')}/api/agent/policy",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Device-Token": token,
                "User-Agent": "AI-Bridge-macOS-Agent/2.0",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                if notify:
                    log_ok("Synchronized restrictive default policy with Control Plane (read-only diagnostics).")
                return True
    except Exception as exc:
        if notify:
            log_warn(f"Could not push server policy to Control Plane: {exc}")
    return False


# -----------------------------------------------------------------------------
# Configuration Resolution & Persistence
# -----------------------------------------------------------------------------
def _resolve_token_and_file(explicit_token: str, explicit_file: str) -> tuple[str, Path | None]:
    token = explicit_token.strip()
    token_path = Path(explicit_file).expanduser().resolve() if explicit_file else None

    # 1. If explicit token provided, find or establish an appropriate token file to persist rotations
    if token:
        if token_path is None:
            candidate = Path.home() / ".ai-bridge" / "device.token"
            token_path = candidate
        return token, token_path

    # 2. Check explicit token file
    if token_path and token_path.is_file():
        try:
            tok = token_path.read_text(encoding="utf-8-sig").strip()
            if tok:
                return tok, token_path
        except OSError:
            pass

    # 3. Check environment variable AI_BRIDGE_TOKEN_FILE
    env_file = os.environ.get("AI_BRIDGE_TOKEN_FILE")
    if env_file:
        p = Path(env_file).expanduser().resolve()
        if p.is_file():
            try:
                tok = p.read_text(encoding="utf-8-sig").strip()
                if tok:
                    return tok, p
            except OSError:
                pass

    # 4. Check standard device.token candidate locations
    candidates = [
        Path.home() / ".ai-bridge" / "device.token",
    ]
    for cand in candidates:
        if cand.is_file():
            try:
                tok = cand.read_text(encoding="utf-8-sig").strip()
                if tok:
                    return tok, cand
            except OSError:
                pass

    # 5. Check agent.json candidate locations
    json_candidates = [
        Path.home() / ".ai-bridge" / "agent.json",
    ]
    for jcand in json_candidates:
        if jcand.is_file():
            try:
                cfg = json.loads(jcand.read_text(encoding="utf-8"))
                tok = (cfg.get("device_token") or "").strip()
                if tok:
                    return tok, jcand.parent / "device.token"
            except Exception:
                pass

    # 6. Legacy agent.env files may still hold a token written by older
    # versions (F07-06 migration read). The token is no longer written
    # there — save_agent_config() stores it only in device.token (0600).
    # This read exists so existing installs keep working; it is removed
    # once the token has been re-saved to device.token.
    env_candidates = [
        Path.home() / ".ai-bridge" / "agent.env",
    ]
    for ecand in env_candidates:
        if ecand.is_file():
            try:
                for line in ecand.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("AI_BRIDGE_DEVICE_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            _warn_legacy_agent_env_token(ecand)
                            token_p = ecand.parent / "device.token"
                            return val, token_p
            except OSError:
                pass

    return "", token_path


_LEGACY_AGENT_ENV_WARNED = False


def _warn_legacy_agent_env_token(path: Path) -> None:
    """Warn once that the device token still lives in a legacy agent.env.

    The token is migrated to device.token (0600) on the next
    save_agent_config() call; agent.env must not hold credentials.
    """
    global _LEGACY_AGENT_ENV_WARNED
    if _LEGACY_AGENT_ENV_WARNED:
        return
    _LEGACY_AGENT_ENV_WARNED = True
    print(
        f"[WARN] Device token found in legacy {path} (F07-06). "
        "Tokens no longer belong in agent.env — launchd may expose env "
        "vars from the plist / EnvironmentVariables. The token will be "
        "moved to device.token (0600) on the next config save.",
        file=sys.stderr,
    )


def load_agent_config(config_dir: Path | None = None) -> dict:
    """Load configuration from agent.json, agent.env, or environment variables."""
    target_dir = config_dir or DEFAULT_CONFIG_DIR
    cfg = {
        "control_url": os.environ.get("AI_BRIDGE_CONTROL_URL", DEFAULT_CONTROL_URL),
        "device_token": os.environ.get("AI_BRIDGE_DEVICE_TOKEN", ""),
        "device_name": platform.node(),
        "workspace": str(DEFAULT_WORKSPACE),
        "poll_wait_s": 10,
        "unrestricted": False,
        "max_tier_unattended": 2,
        "log_level": "INFO",
    }

    # Load from agent.json
    for cand in [target_dir / "agent.json", Path.home() / ".ai-bridge" / "agent.json"]:
        if cand.is_file():
            try:
                saved = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    cfg.update(saved)
                break
            except Exception:
                pass

    # Fallback to agent.env
    for ecand in [target_dir / "agent.env", Path.home() / ".ai-bridge" / "agent.env"]:
        if ecand.is_file():
            try:
                for line in ecand.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k == "AI_BRIDGE_CONTROL_URL":
                            cfg["control_url"] = v
                        elif k == "AI_BRIDGE_DEVICE_TOKEN":
                            cfg["device_token"] = v
                        elif k == "AI_BRIDGE_WORKSPACE":
                            cfg["workspace"] = v
                break
            except OSError:
                pass

    # Resolve token if not loaded
    if not cfg.get("device_token"):
        tok, _ = _resolve_token_and_file("", str(target_dir / "device.token"))
        if tok:
            cfg["device_token"] = tok

    return cfg


def save_agent_config(cfg: dict, config_dir: Path | None = None) -> Path:
    """Persist agent configuration.

    F07-06: the device token is stored in ONE place only — ``device.token``
    (0600). It is never written to ``agent.json`` and never to ``agent.env``:
    launchd LaunchAgents can surface env vars from the plist, so credentials
    must not live in ``agent.env``. That file carries only non-secret settings
    (control URL, workspace). Policy lives on the control plane, not in env vars.
    """
    target_dir = config_dir or DEFAULT_CONFIG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target_dir, 0o700)
    except OSError:
        pass

    # agent.json holds non-secret settings only — strip any token a caller
    # (e.g. `config set device_token`) may have put in the dict.
    json_cfg = {k: v for k, v in cfg.items() if k != "device_token"}
    json_path = target_dir / "agent.json"
    json_path.write_text(json.dumps(json_cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(json_path, 0o600)
    except OSError:
        pass

    # agent.env for local non-secret settings / optional sourcing only.
    env_path = target_dir / "agent.env"
    env_content = (
        f"# AI Bridge macOS Agent Environment Configuration\n"
        f"# NOTE: no credentials here. The device token lives only in\n"
        f"# device.token (0600) — see save_agent_config() (F07-06).\n"
        f"AI_BRIDGE_CONTROL_URL={cfg.get('control_url', DEFAULT_CONTROL_URL)}\n"
        f"AI_BRIDGE_WORKSPACE={cfg.get('workspace', str(DEFAULT_WORKSPACE))}\n"
    )
    env_path.write_text(env_content, encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass

    # Persist device.token — the single, 0600 home of the credential.
    if cfg.get("device_token"):
        token_path = target_dir / "device.token"
        token_path.write_text(cfg["device_token"].strip(), encoding="utf-8")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass

    return json_path


# -----------------------------------------------------------------------------
# System & Telemetry Diagnostics
# -----------------------------------------------------------------------------
def gather_macos_telemetry() -> dict:
    """Collect real-time macOS hardware, kernel, and process metrics."""
    telemetry = {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "kernel": platform.release(),
        "arch": platform.machine(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count() or 1,
        "cpu_percent": 0.0,
        "load_avg": [0.0, 0.0, 0.0],
        "memory_total_gb": 0.0,
        "memory_used_gb": 0.0,
        "memory_free_gb": 0.0,
        "memory_percent": 0.0,
        "disk_total_gb": 0.0,
        "disk_free_gb": 0.0,
        "disk_percent": 0.0,
        "uptime_hours": 0.0,
    }

    # Load average
    try:
        if hasattr(os, "getloadavg"):
            telemetry["load_avg"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        pass

    # Uptime via /proc/uptime
    try:
        up_p = Path("/proc/uptime")
        if up_p.exists():
            secs = float(up_p.read_text(encoding="utf-8").split()[0])
            telemetry["uptime_hours"] = round(secs / 3600.0, 1)
    except Exception:
        pass

    # Memory via /proc/meminfo or psutil
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        telemetry["memory_total_gb"] = round(vm.total / (1024**3), 2)
        telemetry["memory_used_gb"] = round(vm.used / (1024**3), 2)
        telemetry["memory_free_gb"] = round(vm.available / (1024**3), 2)
        telemetry["memory_percent"] = vm.percent
        telemetry["cpu_percent"] = psutil.cpu_percent(interval=0.1)

        du = psutil.disk_usage("/")
        telemetry["disk_total_gb"] = round(du.total / (1024**3), 2)
        telemetry["disk_free_gb"] = round(du.free / (1024**3), 2)
        telemetry["disk_percent"] = du.percent
    except Exception:
        # Fallback reading /proc/meminfo
        try:
            mem_text = Path("/proc/meminfo").read_text(encoding="utf-8")
            mem_dict = {}
            for line in mem_text.splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    mem_dict[k] = int(val)
            total_kb = mem_dict.get("MemTotal", 0)
            avail_kb = mem_dict.get("MemAvailable", mem_dict.get("MemFree", 0))
            if total_kb > 0:
                telemetry["memory_total_gb"] = round(total_kb / 1048576, 2)
                telemetry["memory_free_gb"] = round(avail_kb / 1048576, 2)
                used_kb = total_kb - avail_kb
                telemetry["memory_used_gb"] = round(used_kb / 1048576, 2)
                telemetry["memory_percent"] = round((used_kb / total_kb) * 100, 1)
        except Exception:
            pass
        # Fallback disk usage
        try:
            total, used, free = shutil.disk_usage("/")
            telemetry["disk_total_gb"] = round(total / (1024**3), 2)
            telemetry["disk_free_gb"] = round(free / (1024**3), 2)
            telemetry["disk_percent"] = round((used / total) * 100, 1)
        except Exception:
            pass

    return telemetry


# -----------------------------------------------------------------------------
# TCC permission probe (Screen Recording / Accessibility)
# -----------------------------------------------------------------------------

def probe_tcc_permissions(*, request_screen_capture: bool = False) -> dict:
    """Return structured Screen Recording / Accessibility permission state.

    On non-macOS hosts every field degrades to ``not_applicable`` so unit tests
    and CI on Windows/Linux can import and assert on stable JSON.
    """
    base: dict = {
        "schema": "ai-bridge-macos-tcc/v1",
        "platform": sys.platform,
        "tccutil_available": bool(shutil.which("tccutil")),
    }
    if sys.platform != "darwin":
        base["status"] = "not_applicable"
        base["screen_recording"] = {
            "status": "not_applicable",
            "granted": None,
            "detail": "Screen Recording checks require macOS.",
            "method": "none",
        }
        base["accessibility"] = {
            "status": "not_applicable",
            "granted": None,
            "detail": "Accessibility checks require macOS.",
            "method": "none",
        }
        return base

    screen: dict = {"method": "CGPreflightScreenCaptureAccess", "granted": None, "status": "unknown", "detail": ""}
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        granted = bool(cg.CGPreflightScreenCaptureAccess())
        screen["granted"] = granted
        screen["status"] = "granted" if granted else "denied"
        screen["detail"] = "Screen Recording permission is granted." if granted else "Grant Screen Recording in System Settings → Privacy & Security."
        if request_screen_capture and not granted:
            try:
                cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
                cg.CGRequestScreenCaptureAccess()
            except Exception:
                pass
    except Exception as exc:
        screen["status"] = "unsupported"
        screen["detail"] = f"CoreGraphics preflight unavailable: {exc}"

    ax: dict = {"method": "AXIsProcessTrusted", "granted": None, "status": "unknown", "detail": ""}
    try:
        app_services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        app_services.AXIsProcessTrusted.restype = ctypes.c_bool
        granted_ax = bool(app_services.AXIsProcessTrusted())
        ax["granted"] = granted_ax
        ax["status"] = "granted" if granted_ax else "denied"
        ax["detail"] = (
            "Accessibility permission is granted."
            if granted_ax
            else "Grant Accessibility in System Settings → Privacy & Security."
        )
    except Exception as exc:
        ax["status"] = "unsupported"
        ax["detail"] = f"AXIsProcessTrusted unavailable: {exc}"

    if shutil.which("tccutil"):
        ax["tccutil"] = "available (reset only; use System Settings to grant)"
        screen["tccutil"] = "available (reset only; use System Settings to grant)"

    overall = "not_applicable"
    if screen.get("granted") is True and ax.get("granted") is True:
        overall = "granted"
    elif screen.get("status") == "not_applicable" or ax.get("status") == "not_applicable":
        overall = "not_applicable"
    elif screen.get("granted") or ax.get("granted"):
        overall = "partial"
    else:
        overall = "denied"

    base["status"] = overall
    base["screen_recording"] = screen
    base["accessibility"] = ax
    return base


# -----------------------------------------------------------------------------
# Doctor & Diagnostics Subcommand
# -----------------------------------------------------------------------------
def run_doctor(control_url: str, token: str) -> bool:
    """Run comprehensive environment, capability, and connectivity audit."""
    print(f"\n{CLR_BOLD}{CLR_BLUE}===================================================================={CLR_RESET}")
    print(f"{CLR_BOLD}{CLR_BLUE}  AI Bridge macOS Agent — System Doctor & Health Audit{CLR_RESET}")
    print(f"{CLR_BOLD}{CLR_BLUE}===================================================================={CLR_RESET}\n")

    all_ok = True

    def check_item(title: str, ok: bool, details: str = "", warn: bool = False) -> None:
        nonlocal all_ok
        if ok:
            badge = f"{CLR_GREEN}[✓ PASS]{CLR_RESET}"
        elif warn:
            badge = f"{CLR_YELLOW}[! WARN]{CLR_RESET}"
        else:
            badge = f"{CLR_RED}[✗ FAIL]{CLR_RESET}"
            all_ok = False
        print(f"  {badge} {CLR_BOLD}{title}{CLR_RESET}")
        if details:
            print(f"         {CLR_CYAN}{details}{CLR_RESET}")

    # 1. Host & Platform
    distro = "Linux"
    if Path("/etc/os-release").exists():
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                distro = line.split("=", 1)[1].strip('"')
                break
    check_item("Operating System & Kernel", True, f"{distro} (Kernel: {platform.release()}, Arch: {platform.machine()})")

    # 2. Python Runtime
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    check_item("Python Environment", sys.version_info >= (3, 9), f"Python {py_ver} at {sys.executable}")

    # 3. Virtual Environment
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    check_item("Virtual Environment Isolation", in_venv, f"Prefix: {sys.prefix}", warn=not in_venv)

    # 4. Core Package Dependencies
    missing_deps = []
    for pkg in ["mcp", "fastapi", "psutil"]:
        try:
            __import__(pkg)
        except ImportError:
            missing_deps.append(pkg)
    check_item(
        "Python Dependencies",
        len(missing_deps) == 0,
        "All required libraries installed" if not missing_deps else f"Missing: {', '.join(missing_deps)} (run: pip install -e .)",
        warn=len(missing_deps) > 0,
    )

    # 5. Control Plane Reachability
    clean_url = control_url.rstrip("/")
    t0 = time.time()
    reach_ok = False
    status_text = ""
    try:
        req = urllib.request.Request(f"{clean_url}/health", headers={"User-Agent": "AI-Bridge-macOS-Doctor"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            lat = (time.time() - t0) * 1000
            reach_ok = resp.status == 200
            status_text = f"HTTP {resp.status} in {lat:.1f}ms ({clean_url})"
    except Exception as exc:
        status_text = f"Connection failed: {exc}"
    check_item("Control Plane Connectivity", reach_ok, status_text)

    # 6. Authentication & Device Verification
    auth_ok = False
    auth_details = ""
    if token:
        try:
            req = urllib.request.Request(
                f"{clean_url}/api/agent/whoami",
                headers={"X-Device-Token": token, "User-Agent": "AI-Bridge-macOS-Doctor"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                did = data.get("device_id")
                if did:
                    auth_ok = True
                    auth_details = f"Authenticated as Device ID: {did}"
        except Exception as exc:
            auth_details = f"Authentication rejected: {exc}"
    else:
        auth_details = "No device token configured. Run 'ai-bridge-agent enroll' to connect."
    check_item("Device Token Verification", auth_ok, auth_details)

    # 7. Policy Mode
    pol_ok = False
    pol_details = ""
    if auth_ok:
        try:
            req = urllib.request.Request(
                f"{clean_url}/api/agent/policy",
                headers={"X-Device-Token": token, "User-Agent": "AI-Bridge-macOS-Doctor"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pdata = json.loads(resp.read().decode("utf-8"))
                eff = pdata.get("effective_policy") or {}
                is_unrest = eff.get("unrestricted") is True or eff.get("max_tier_unattended", 1) >= 4
                pol_ok = is_unrest
                pol_details = "⚡ Active Mode: UNRESTRICTED SERVER (Tier 4 / Full Autonomy - No Policy Blocks)" if is_unrest else f"Restricted Mode (Ceiling: Tier {eff.get('max_tier_unattended', 2)})"
        except Exception as exc:
            pol_details = f"Policy lookup failed: {exc}"
    check_item("Execution Policy Guardrails", pol_ok, pol_details, warn=not pol_ok)

    # macOS TCC (Screen Recording / Accessibility)
    tcc = probe_tcc_permissions()
    sr = tcc.get("screen_recording") or {}
    ax = tcc.get("accessibility") or {}
    tcc_na = tcc.get("status") == "not_applicable"
    check_item(
        "TCC Screen Recording",
        tcc_na or sr.get("granted") is True,
        sr.get("detail") or tcc.get("status", ""),
        warn=tcc_na or sr.get("granted") is not True,
    )
    check_item(
        "TCC Accessibility",
        tcc_na or ax.get("granted") is True,
        ax.get("detail") or "",
        warn=tcc_na or ax.get("granted") is not True,
    )

    # macOS launchd / utilities
    if sys.platform == "darwin":
        utils = ["launchctl", "tccutil", "python3"]
        found_utils = [u for u in utils if shutil.which(u)]
        check_item("macOS Agent Binaries", True, f"Found: {', '.join(found_utils)}")
    else:
        check_item("macOS Agent Binaries", True, "Non-macOS host — launchd/TCC checks skipped", warn=True)


    # 9. Hardware Telemetry
    telem = gather_macos_telemetry()
    check_item(
        "System Resource Headroom",
        telem["disk_free_gb"] > 1.0 and telem["memory_free_gb"] > 0.2,
        f"RAM Free: {telem['memory_free_gb']} GB ({telem['memory_percent']}% used) | Disk Free: {telem['disk_free_gb']} GB ({telem['disk_percent']}% used) | Load: {telem['load_avg']}",
    )

    print()
    if all_ok:
        log_ok("All critical server checks PASSED. Agent is ready for autonomous operations.")
    else:
        log_warn("One or more checks failed or require attention. See diagnostics above.")
    return all_ok


# -----------------------------------------------------------------------------
# Status Subcommand
# -----------------------------------------------------------------------------
def show_status(control_url: str, token: str) -> None:
    """Display comprehensive status dashboard for this Mac."""
    print(f"\n{CLR_BOLD}{CLR_BLUE}===================================================================={CLR_RESET}")
    print(f"{CLR_BOLD}{CLR_BLUE}  AI Bridge macOS Agent — Status & Identity Dashboard{CLR_RESET}")
    print(f"{CLR_BOLD}{CLR_BLUE}===================================================================={CLR_RESET}\n")

    cfg = load_agent_config()
    clean_url = (control_url or cfg.get("control_url", DEFAULT_CONTROL_URL)).rstrip("/")
    dev_token = token or cfg.get("device_token", "")

    # Host & Telemetry
    telem = gather_macos_telemetry()
    print(f"  {CLR_BOLD}Host Platform:{CLR_RESET}        {telem['hostname']} ({telem['os']})")
    print(f"  {CLR_BOLD}Kernel & Arch:{CLR_RESET}        {telem['kernel']} ({telem['arch']})")
    print(f"  {CLR_BOLD}System Uptime:{CLR_RESET}        {telem['uptime_hours']} hours | Load: {telem['load_avg']}")
    print(
        f"  {CLR_BOLD}CPU & Memory:{CLR_RESET}         {telem['cpu_count']} Cores ({telem['cpu_percent']}%) | RAM: {telem['memory_used_gb']}/{telem['memory_total_gb']} GB ({telem['memory_percent']}%)"
    )
    print(f"  {CLR_BOLD}Storage Space:{CLR_RESET}        {telem['disk_free_gb']} GB Free / {telem['disk_total_gb']} GB Total ({telem['disk_percent']}%)")
    print()

    # Control Plane & Token
    print(f"  {CLR_BOLD}Control Plane:{CLR_RESET}        {clean_url}")
    if not dev_token:
        print(f"  {CLR_BOLD}Device Token:{CLR_RESET}         {CLR_YELLOW}[Not Configured]{CLR_RESET}")
        print(f"  {CLR_BOLD}Status:{CLR_RESET}               {CLR_YELLOW}Unregistered (Run 'ai-bridge-agent enroll --admin-token <TOK>'){CLR_RESET}\n")
        return

    masked_tok = dev_token[:8] + "..." + dev_token[-6:] if len(dev_token) > 16 else "***"
    print(f"  {CLR_BOLD}Device Token:{CLR_RESET}         {masked_tok}")

    # Query Control Plane
    try:
        req = urllib.request.Request(
            f"{clean_url}/api/agent/whoami",
            headers={"X-Device-Token": dev_token, "User-Agent": "AI-Bridge-macOS-Agent"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            did = data.get("device_id", "—")
            print(f"  {CLR_BOLD}Device ID:{CLR_RESET}            {CLR_GREEN}{did}{CLR_RESET}")
            print(f"  {CLR_BOLD}Cloud Status:{CLR_RESET}         {CLR_GREEN}CONNECTED & AUTHENTICATED ✓{CLR_RESET}")
    except Exception as exc:
        print(f"  {CLR_BOLD}Cloud Status:{CLR_RESET}         {CLR_RED}DISCONNECTED ({exc}){CLR_RESET}")

    # Query Policy
    try:
        req = urllib.request.Request(
            f"{clean_url}/api/agent/policy",
            headers={"X-Device-Token": dev_token, "User-Agent": "AI-Bridge-macOS-Agent"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pdata = json.loads(resp.read().decode("utf-8"))
            eff = pdata.get("effective_policy") or {}
            is_unrest = eff.get("unrestricted") is True or eff.get("max_tier_unattended", 1) >= 4
            if is_unrest:
                print(f"  {CLR_BOLD}Policy Mode:{CLR_RESET}          {CLR_GREEN}⚡ UNRESTRICTED SERVER (Tier 4 / Full Autonomy - No Policy Blocks){CLR_RESET}")
            else:
                print(f"  {CLR_BOLD}Policy Mode:{CLR_RESET}          {CLR_YELLOW}Restricted (Ceiling: Tier {eff.get('max_tier_unattended', 2)}){CLR_RESET}")
    except Exception:
        pass

    # LaunchAgent state
    ld = launchd_service_state()
    color = CLR_GREEN if ld == "loaded" else (CLR_YELLOW if ld in ("unloaded", "not_installed") else CLR_RED)
    print(f"  {CLR_BOLD}LaunchAgent:{CLR_RESET}          {color}{ld.upper()}{CLR_RESET} ({LAUNCHD_LABEL})")
    tcc = probe_tcc_permissions()
    print(
        f"  {CLR_BOLD}TCC Permissions:{CLR_RESET}      "
        f"Screen={(tcc.get('screen_recording') or {}).get('status', 'n/a')} | "
        f"Accessibility={(tcc.get('accessibility') or {}).get('status', 'n/a')}"
    )
    print()


# -----------------------------------------------------------------------------
# LaunchAgent (launchd) Service Management
# -----------------------------------------------------------------------------
def launchd_service_state() -> str:
    """Return loaded | unloaded | not_installed | unavailable."""
    if sys.platform != "darwin" or not hasattr(os, "getuid"):
        return "unavailable"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if not plist.is_file():
        return "not_installed"
    try:
        res = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return "loaded"
    except Exception:
        pass
    try:
        res = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
        if LAUNCHD_LABEL in (res.stdout or ""):
            return "loaded"
    except Exception:
        pass
    return "unloaded"


def manage_launchd_service(action: str) -> None:
    """Install, load, unload, or inspect the user LaunchAgent."""
    if sys.platform != "darwin":
        log_error("LaunchAgent management requires macOS (launchctl).")
        sys.exit(1)

    label = LAUNCHD_LABEL
    plist_dest = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    domain_target = f"gui/{os.getuid()}"

    if action == "install":
        cfg = load_agent_config()
        repo_root = Path(__file__).resolve().parents[2]
        exec_py = shutil.which("python3") or sys.executable
        env_file = DEFAULT_CONFIG_DIR / "agent.env"
        DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        save_agent_config(cfg)

        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exec_py}</string>
        <string>{repo_root / "agents/macos/agent_main.py"}</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AI_BRIDGE_CONTROL_URL</key>
        <string>{cfg.get("control_url", DEFAULT_CONTROL_URL)}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ai-bridge-agent.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ai-bridge-agent.err</string>
</dict>
</plist>
"""
        plist_dest.parent.mkdir(parents=True, exist_ok=True)
        plist_dest.write_text(content, encoding="utf-8")
        log_ok(f"Wrote LaunchAgent plist to {plist_dest}")
        if env_file.is_file():
            log_info(f"Non-secret settings: {env_file} (device token remains in device.token only).")
        subprocess.run(["launchctl", "bootstrap", domain_target, str(plist_dest)], check=False)
        subprocess.run(["launchctl", "enable", f"{domain_target}/{label}"], check=False)
        log_ok("LaunchAgent installed. Use: ai-bridge-agent service start")

    elif action == "uninstall":
        subprocess.run(["launchctl", "bootout", domain_target, str(plist_dest)], check=False)
        if plist_dest.exists():
            plist_dest.unlink()
        log_ok("LaunchAgent removed.")

    elif action == "start":
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain_target}/{label}"], check=False)
    elif action == "stop":
        subprocess.run(["launchctl", "bootout", domain_target, str(plist_dest)], check=False)
    elif action == "restart":
        subprocess.run(["launchctl", "bootout", domain_target, str(plist_dest)], check=False)
        subprocess.run(["launchctl", "bootstrap", domain_target, str(plist_dest)], check=False)
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain_target}/{label}"], check=False)
    elif action == "status":
        print(f"LaunchAgent {label}: {launchd_service_state().upper()}")
        sys.exit(0 if launchd_service_state() == "loaded" else 1)
    elif action == "logs":
        for path in ("/tmp/ai-bridge-agent.out", "/tmp/ai-bridge-agent.err"):
            p = Path(path)
            if p.is_file():
                print(f"--- {path} ---")
                print(p.read_text(encoding="utf-8", errors="replace")[-8000:])
    else:
        log_error(f"Unknown service action: {action}")
        sys.exit(1)


# -----------------------------------------------------------------------------
# Automated Token Rotation
# -----------------------------------------------------------------------------
def rotate_token_cmd(control_url: str, token: str) -> None:
    """Manually rotate the device token with the Control Plane."""
    cfg = load_agent_config()
    target_url = (control_url or cfg.get("control_url", DEFAULT_CONTROL_URL)).rstrip("/")
    dev_token = token or cfg.get("device_token", "")
    if not dev_token:
        log_error("No device token configured to rotate.")
        sys.exit(1)

    log_info(f"Rotating device token with {target_url} ...")
    try:
        req = urllib.request.Request(
            f"{target_url}/api/agent/rotate",
            headers={"X-Device-Token": dev_token, "User-Agent": "AI-Bridge-macOS-Agent"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            new_tok = data.get("device_token")
            exp = data.get("token_expires_at")
            if new_tok:
                cfg["device_token"] = new_tok
                save_agent_config(cfg)
                exp_str = f"in {max(0, int((exp - time.time()) / 86400))} days" if exp else "never"
                log_ok(f"Device token successfully rotated and persisted securely! (Expires: {exp_str})")
            else:
                log_error("No new token returned by server.")
                sys.exit(1)
    except Exception as exc:
        log_error(f"Token rotation failed: {exc}")
        sys.exit(1)


# -----------------------------------------------------------------------------
# Automated Device Enrollment
# -----------------------------------------------------------------------------
def enroll_cmd(control_url: str, admin_token: str, name: str) -> None:
    """Automate one-step enrollment with the Control Plane using Admin Token."""
    clean_url = (control_url or DEFAULT_CONTROL_URL).rstrip("/")
    if not admin_token:
        log_error("Admin token is required for enrollment (--admin-token).")
        sys.exit(1)

    device_name = name or platform.node()
    log_info(f"Enrolling device '{device_name}' with {clean_url} ...")

    payload = {
        "name": device_name,
        "hostname": platform.node(),
        "os": "macos",
        "tenant": "default",
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{clean_url}/api/devices/enroll",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Admin-Token": admin_token,
                "User-Agent": "AI-Bridge-macOS-Agent",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            dev_tok = res.get("device_token")
            dev_id = res.get("device_id")
            if dev_tok:
                cfg = load_agent_config()
                cfg["control_url"] = clean_url
                cfg["device_token"] = dev_tok
                cfg["device_name"] = device_name
                cfg_path = save_agent_config(cfg)
                log_ok(f"Successfully enrolled as Device ID: {dev_id}")
                log_ok(f"Configuration and credentials saved to {cfg_path}")

                # Sync the restrictive default policy
                sync_macos_agent_policy(clean_url, dev_tok, notify=True)
            else:
                log_error(f"Enrollment response did not contain device_token: {res}")
                sys.exit(1)
    except Exception as exc:
        log_error(f"Enrollment failed: {exc}")
        sys.exit(1)


# -----------------------------------------------------------------------------
# Codebase Self-Update
# -----------------------------------------------------------------------------
def update_cmd() -> None:
    """Update the agent checkout to a SIGNED, pinned release commit.

    Gate 13 (Wave 7): the old unsigned ``git fetch origin main &&
    git pull --ff-only origin main`` path is deleted. A signed release
    manifest must be configured (AI_BRIDGE_UPDATE_MANIFEST_PATH /
    AI_BRIDGE_UPDATE_SIGNATURE_PATH / AI_BRIDGE_UPDATE_PUBLIC_KEY); the
    Ed25519 signature is verified BEFORE any checkout and exactly the
    pinned commit is checked out (with rollback on failure — see
    ai_bridge.updates.apply_pinned_git_update). If no signed release is
    configured, or verification fails, the update is REFUSED (exit 1)
    and no unsigned code is pulled: configure a signed release and
    re-run, or reinstall from scratch.
    """
    repo_root = Path(__file__).resolve().parents[2]
    log_info(f"Updating AI Bridge repository at {repo_root}...")

    # Signed update only (only when running inside a git checkout).
    if not (repo_root / ".git").exists():
        log_info("No .git checkout found; skipping git update.")
        return
    try:
        from ai_bridge import updates

        if not updates.signed_update_configured():
            raise PermissionError(
                "Refusing unsigned agent update: a signed release pin is required. "
                "Set AI_BRIDGE_UPDATE_MANIFEST_PATH, AI_BRIDGE_UPDATE_SIGNATURE_PATH "
                "and AI_BRIDGE_UPDATE_PUBLIC_KEY to a signed release manifest."
            )
        manifest, signature_b64 = updates.load_signed_manifest()
        public_key_path = Path(os.environ[updates.UPDATE_PUBLIC_KEY_ENV])
        result = updates.apply_pinned_git_update(
            repo_root, manifest, signature_b64, public_key_path
        )
        log_ok(f"Signed update applied: HEAD is now {result['commit'][:12]}.")
    except Exception as exc:  # noqa: BLE001 - refusal must never pull unsigned code
        log_error(f"Signed update refused or failed; no code was pulled: {exc}")
        log_error(
            "Configure a signed release (AI_BRIDGE_UPDATE_MANIFEST_PATH, "
            "AI_BRIDGE_UPDATE_SIGNATURE_PATH, AI_BRIDGE_UPDATE_PUBLIC_KEY) "
            "and re-run, or reinstall from scratch."
        )
        sys.exit(1)

    # Virtualenv update
    exec_pip = repo_root / ".venv" / "bin" / "pip"
    if exec_pip.exists():
        try:
            log_info("Updating dependencies in virtualenv...")
            subprocess.run([str(exec_pip), "install", "-e", str(repo_root), "--quiet"], check=True)
            log_ok("Dependencies updated.")
        except Exception as exc:
            log_warn(f"Pip update warning: {exc}")

    # Restart service if active
    if sys.platform == "darwin" and launchd_service_state() == "loaded":
        try:
            domain_target = f"gui/{os.getuid()}"
            log_info("Restarting LaunchAgent...")
            subprocess.run(["launchctl", "kickstart", "-k", f"{domain_target}/{LAUNCHD_LABEL}"], check=False)
            log_ok("LaunchAgent kickstarted with latest code.")
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Foreground Runner Loop
# -----------------------------------------------------------------------------
def run_agent_loop(control_url: str, token: str, token_file: Path | None, once: bool = False) -> None:
    """Execute the core polling loop with the restrictive default policy.

    Tier 3/4 work needs explicit admin opt-in on the control plane plus
    human approval; the agent never grants it to itself (R-P0-08).
    """
    server_policy = get_macos_agent_policy()
    poller = Poller(control_url, token, policy=server_policy, token_file=token_file)

    log_info(f"Starting AI Bridge macOS Agent ({control_url})")
    log_info("Policy Mode: RESTRICTIVE DEFAULT (read-only diagnostics, Tier 2 ceiling; Tier 3/4 need admin opt-in + approval)")

    # Push the restrictive default policy to the Control Plane.
    sync_macos_agent_policy(control_url, token, notify=True)

    # Signal handling for graceful shutdown (F07-09): do NOT sys.exit(0)
    # mid-task (the old behaviour abandoned the claimed task). Instead,
    # ask the poller to stop claiming new tasks and drain the in-flight
    # one (bounded by Poller.drain_timeout); run_forever() then returns
    # and the process exits cleanly.
    def _handle_signal(signum, frame):
        log_warn(f"Received signal {signum}. Stopping new claims; draining in-flight task (up to {poller.drain_timeout:.0f}s)...")
        poller.request_shutdown()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if once:
        log_info("Executing single poll cycle (--once)...")
        res = poller.run_once()
        print(json.dumps(res, indent=2))
        return

    log_ok("Agent poller listening for incoming tasks...")
    try:
        result = poller.run_forever()
    except KeyboardInterrupt:
        # Backstop: run_forever() normally converts Ctrl-C into a drain
        # itself; if it arrives outside the loop, request the drain here.
        log_warn("Agent interrupted by user. Draining...")
        poller.request_shutdown()
        result = {"revoked": poller.revoked, "drained": not poller._drain_timed_out}
    if result.get("revoked"):
        log_error(f"Device token revoked/expired ({result.get('revoked_reason')}). Re-enroll this device to resume.")
        sys.exit(2)
    if not result.get("drained", True):
        log_warn("An in-flight task did not finish within the drain timeout; it was left running for the lease sweep to re-queue.")
    log_info("Agent stopped cleanly.")


# -----------------------------------------------------------------------------
# Main CLI Entrypoint
# -----------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ai-bridge-agent",
        description="AI Bridge macOS Agent — Advanced Server Management, Deployment & Runner.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available commands")

    # Global options
    parser.add_argument("--control-url", default=os.environ.get("AI_BRIDGE_CONTROL_URL", DEFAULT_CONTROL_URL))
    parser.add_argument("--device-token", default=os.environ.get("AI_BRIDGE_DEVICE_TOKEN", ""))
    parser.add_argument("--token-file", default=os.environ.get("AI_BRIDGE_TOKEN_FILE", ""))
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle, then exit.")
    parser.add_argument("--check-update", action="store_true", help="Verify release manifest and exit.")
    parser.add_argument("--manifest-url", default="")
    parser.add_argument("--signature-url", default="")
    parser.add_argument("--public-key", default="")
    parser.add_argument(
        "--apply-update",
        action="store_true",
        help="After verifying the signed manifest, download into staging and apply when AI_BRIDGE_ALLOW_AUTO_UPDATE=1.",
    )
    parser.add_argument(
        "--staging-dir",
        default=os.environ.get("AI_BRIDGE_UPDATE_STAGING", str(Path.home() / ".ai-bridge" / "update-staging")),
        help="Directory for verified download staging before apply.",
    )
    parser.add_argument(
        "--install-dir",
        default=os.environ.get("AI_BRIDGE_INSTALL_DIR", ""),
        help="Install root for --apply-update (defaults to the agent repo root when unset).",
    )

    # Subcommand: run
    p_run = subparsers.add_parser("run", help="Run agent poller in foreground")
    p_run.add_argument("--control-url", default=argparse.SUPPRESS)
    p_run.add_argument("--device-token", default=argparse.SUPPRESS)
    p_run.add_argument("--token-file", default=argparse.SUPPRESS)
    p_run.add_argument("--once", action="store_true", default=argparse.SUPPRESS)

    # Subcommand: status
    p_status = subparsers.add_parser("status", help="Show system status and device identity")
    p_status.add_argument("--control-url", default=argparse.SUPPRESS)
    p_status.add_argument("--device-token", default=argparse.SUPPRESS)

    # Subcommand: doctor
    p_doc = subparsers.add_parser("doctor", help="Run complete server and connectivity health audit")
    p_doc.add_argument("--control-url", default=argparse.SUPPRESS)
    p_doc.add_argument("--device-token", default=argparse.SUPPRESS)

    # Subcommand: config
    p_cfg = subparsers.add_parser("config", help="View or modify agent configuration")
    p_cfg.add_argument("action", choices=["show", "get", "set", "path"], default="show", nargs="?")
    p_cfg.add_argument("key", nargs="?", default="")
    p_cfg.add_argument("value", nargs="?", default="")

    # Subcommand: policy
    p_pol = subparsers.add_parser("policy", help="Inspect or sync the restrictive default policy")
    p_pol.add_argument("action", choices=["get", "sync"], default="get", nargs="?")

    # Subcommand: rotate-token
    p_rot = subparsers.add_parser("rotate-token", help="Rotate device token with the Control Plane")
    p_rot.add_argument("--control-url", default=argparse.SUPPRESS)
    p_rot.add_argument("--device-token", default=argparse.SUPPRESS)

    # Subcommand: service
    p_svc = subparsers.add_parser("service", help="Manage launchd LaunchAgent")
    p_svc.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status", "logs"])

    # Subcommand: enroll
    p_enr = subparsers.add_parser("enroll", help="Enroll device using Admin Token")
    p_enr.add_argument("--control-url", default=argparse.SUPPRESS)
    p_enr.add_argument("--admin-token", required=True, help="Control plane admin token")
    p_enr.add_argument("--name", default="", help="Device name (defaults to hostname)")

    # Subcommand: update
    subparsers.add_parser("update", help="Update codebase to a signed release and restart service")

    # Subcommand: tui (Interactive Terminal Studio)
    p_tui = subparsers.add_parser("tui", help="Launch interactive macOS Terminal Studio (Curses TUI / REPL)")
    p_tui.add_argument("--control-url", default=argparse.SUPPRESS)
    p_tui.add_argument("--device-token", default=argparse.SUPPRESS)
    p_tui.add_argument("--token-file", default=argparse.SUPPRESS)
    p_tui.add_argument("--mode", choices=["auto", "curses", "repl"], default="auto")
    p_tui.add_argument("--snapshot", action="store_true", help="Print a single dashboard snapshot and exit")
    p_tui.add_argument("--json", action="store_true", help="With --snapshot, emit JSON state")

    args = parser.parse_args()

    # Legacy manifest verification / apply
    if args.check_update or getattr(args, "apply_update", False):
        if not (args.manifest_url and args.signature_url and args.public_key):
            parser.error("--check-update requires --manifest-url, --signature-url and --public-key.")
        from ai_bridge.updates import verify_manifest

        with urllib.request.urlopen(args.manifest_url, timeout=15) as r:
            manifest = json.loads(r.read())
        with urllib.request.urlopen(args.signature_url, timeout=15) as r:
            signature = r.read().decode().strip()
        ok = verify_manifest(manifest, signature, Path(args.public_key))
        print(f"update manifest {manifest.get('name')} {manifest.get('version')}: {'VERIFIED' if ok else 'SIGNATURE INVALID - refusing'}")
        if not ok:
            sys.exit(1)
        if getattr(args, "apply_update", False):
            from ai_bridge.updates import download_and_stage_update, enforce_version_floor

            enforce_version_floor(manifest)
            install_dir = Path(args.install_dir) if args.install_dir else Path(__file__).resolve().parents[2]
            result = download_and_stage_update(
                manifest,
                staging_dir=Path(args.staging_dir),
                install_dir=install_dir,
                apply=True,
            )
            print(
                f"update applied version={result.get('version')} "
                f"files={len(result.get('applied') or [])} install_dir={result.get('install_dir')}"
            )
        sys.exit(0)

    # Dispatch subcommands
    subcmd = args.subcommand

    if subcmd == "doctor":
        cfg = load_agent_config()
        url = getattr(args, "control_url", "") or cfg.get("control_url", DEFAULT_CONTROL_URL)
        tok = getattr(args, "device_token", "") or cfg.get("device_token", "")
        run_doctor(url, tok)
        return

    if subcmd == "status":
        cfg = load_agent_config()
        url = getattr(args, "control_url", "") or cfg.get("control_url", DEFAULT_CONTROL_URL)
        tok = getattr(args, "device_token", "") or cfg.get("device_token", "")
        show_status(url, tok)
        return

    if subcmd == "service":
        manage_launchd_service(args.action)
        return

    if subcmd == "rotate-token":
        cfg = load_agent_config()
        url = getattr(args, "control_url", "") or cfg.get("control_url", DEFAULT_CONTROL_URL)
        tok = getattr(args, "device_token", "") or cfg.get("device_token", "")
        rotate_token_cmd(url, tok)
        return

    if subcmd == "enroll":
        cfg = load_agent_config()
        url = getattr(args, "control_url", "") or cfg.get("control_url", DEFAULT_CONTROL_URL)
        enroll_cmd(url, args.admin_token, args.name)
        return

    if subcmd == "update":
        update_cmd()
        return

    if subcmd == "tui":
        from macos.tui_app import launch_macos_tui

        cfg = load_agent_config()
        url = getattr(args, "control_url", "") or cfg.get("control_url", DEFAULT_CONTROL_URL)
        tok = getattr(args, "device_token", "") or cfg.get("device_token", "")
        tf = Path(getattr(args, "token_file", "")) if getattr(args, "token_file", "") else None
        code = launch_macos_tui(
            control_url=url,
            device_token=tok,
            token_file=tf,
            mode=getattr(args, "mode", "auto"),
            snapshot_only=bool(getattr(args, "snapshot", False)),
            json_output=bool(getattr(args, "json", False)),
        )
        sys.exit(code)

    if subcmd == "policy":
        cfg = load_agent_config()
        url = cfg.get("control_url", DEFAULT_CONTROL_URL)
        tok = cfg.get("device_token", "")
        if args.action == "sync":
            sync_macos_agent_policy(url, tok, notify=True)
        else:
            show_status(url, tok)
        return

    if subcmd == "config":
        cfg = load_agent_config()
        action = args.action
        if action == "show":
            masked = dict(cfg)
            if masked.get("device_token"):
                masked["device_token"] = masked["device_token"][:6] + "..." + masked["device_token"][-4:]
            print(json.dumps(masked, indent=2))
        elif action == "path":
            print(f"Config directory: {DEFAULT_CONFIG_DIR}")
            print(f"JSON config:      {CONFIG_JSON_FILE}")
            print(f"ENV config:       {CONFIG_ENV_FILE}")
            print(f"Token file:       {TOKEN_FILE}")
            print(f"Workspace root:   {DEFAULT_WORKSPACE}")
        elif action == "get":
            if not args.key:
                print(json.dumps(cfg, indent=2))
            else:
                print(cfg.get(args.key, ""))
        elif action == "set":
            if not args.key or not args.value:
                log_error("Usage: ai-bridge-agent config set <key> <value>")
                sys.exit(1)
            cfg[args.key] = args.value
            saved_p = save_agent_config(cfg)
            log_ok(f"Saved {args.key} = {args.value} to {saved_p}")
        return

    # Default / 'run' command
    cfg = load_agent_config()
    cli_url = getattr(args, "control_url", "") or ""
    cli_token = getattr(args, "device_token", "") or ""
    cli_file = getattr(args, "token_file", "") or ""
    once_flag = bool(getattr(args, "once", False))

    target_url = cli_url or cfg.get("control_url", DEFAULT_CONTROL_URL)
    token, token_file = _resolve_token_and_file(cli_token, cli_file)
    if not token:
        token = cfg.get("device_token", "")

    if not token:
        log_error("A device token is required to run the agent.")
        print("\nTo enroll this device now:")
        print(f"  {sys.executable} agents/macos/agent_main.py enroll --admin-token <ADMIN_TOKEN>")
        print("Or configure a token manually:")
        print(f"  {sys.executable} agents/macos/agent_main.py config set device_token <DEVICE_TOKEN>\n")
        sys.exit(1)

    run_agent_loop(target_url, token, token_file, once=once_flag)


if __name__ == "__main__":
    main()

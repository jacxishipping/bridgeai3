"""Unit tests for the AI Bridge Linux Interactive Terminal Studio (`agents/linux/tui_app.py`)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "agents"))

from linux.tui_app import LinuxTuiController, _ascii_bar, render_dashboard_text  # noqa: E402


def test_ascii_bar_formatting():
    bar0 = _ascii_bar(0.0, width=10)
    assert "[----------]" in bar0
    assert "0.0%" in bar0

    bar50 = _ascii_bar(50.0, width=10)
    assert "[#####-----]" in bar50
    assert "50.0%" in bar50

    bar100 = _ascii_bar(120.0, width=10)
    assert "[##########]" in bar100
    assert "100.0%" in bar100


def test_linux_tui_controller_state_and_dashboard_rendering(tmp_path: Path):
    ctrl = LinuxTuiController(
        control_url="https://clideos.us",
        device_token="test-device-token-1234567890",
        config_dir=tmp_path,
    )
    ctrl.log("Test log entry from unit test", "OK")

    state = ctrl.collect_state(probe_cloud=False)
    assert state["schema"] == "ai-bridge-linux-tui-state/v1"
    assert state["control_url"] == "https://clideos.us"
    assert state["has_token"] is True
    assert state["masked_token"] == "test-d...7890"
    assert "cpu_percent" in state["telemetry"]
    assert "transports" in state["capabilities"]
    assert any("Test log entry" in lg for lg in state["recent_logs"])

    text = render_dashboard_text(state)
    assert "AI BRIDGE LINUX TERMINAL STUDIO" in text
    assert "https://clideos.us" in text
    assert "CPU Usage:" in text
    assert "Memory Usage:" in text
    assert "Disk Usage:" in text
    assert "Test log entry from unit test" in text


def test_agent_main_tui_snapshot_cli():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "agents" / "linux" / "agent_main.py"), "tui", "--snapshot", "--json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema"] == "ai-bridge-linux-tui-state/v1"
    assert "telemetry" in payload
    assert "capabilities" in payload

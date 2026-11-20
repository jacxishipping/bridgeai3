"""Regression tests for R-P0-07 / F07-01: agent policy merge is intersection-only.

The local (agent-side) policy may only NARROW the server (control plane)
policy — it can never widen it. A compromised or misconfigured agent must
not be able to grant itself permissions the plane did not grant.

Also covers R-P0-08 (restrictive Linux default), F07-05 (no silent
plaintext downgrade when the token saver fails) and F07-06 (Linux token
stored in one place only: device.token, 0600).
"""

import os
from pathlib import Path

from agents.common.poller import Poller
from ai_bridge.policy import Policy, decide


def _poller(local_policy):
    return Poller("http://127.0.0.1:9", "test-token", policy=local_policy)


def _server_policy(**overrides):
    base = {
        "unrestricted": False,
        "allow_writes": True,
        "allow_tier3": False,
        "desktop_enabled": False,
        "max_tier_unattended": 2,
    }
    base.update(overrides)
    return base


# --- R-P0-07: intersection-only merge -------------------------------------


def test_local_unrestricted_cannot_widen_server_ceiling():
    # Roadmap case: local unrestricted + server ceiling 2 -> ceiling 2,
    # unrestricted False.
    local = Policy(unrestricted=True, max_tier_unattended=4, allow_tier3=True)
    eff = _poller(local)._effective_policy(_server_policy(max_tier_unattended=2))
    assert eff.max_tier_unattended == 2
    assert eff.unrestricted is False
    assert eff.allow_tier3 is False


def test_unrestricted_requires_both_sides():
    local = Policy(unrestricted=True, max_tier_unattended=4)
    # Server also unrestricted: AND holds.
    eff = _poller(local)._effective_policy(_server_policy(unrestricted=True))
    assert eff.unrestricted is True
    # Server not unrestricted: local alone cannot enable it.
    eff = _poller(local)._effective_policy(_server_policy(unrestricted=False))
    assert eff.unrestricted is False


def test_ceiling_is_the_minimum():
    local = Policy(max_tier_unattended=2)
    eff = _poller(local)._effective_policy(_server_policy(max_tier_unattended=4))
    assert eff.max_tier_unattended == 2
    local = Policy(max_tier_unattended=4)
    eff = _poller(local)._effective_policy(_server_policy(max_tier_unattended=3))
    assert eff.max_tier_unattended == 3


def test_allowed_tools_wildcard_cannot_widen():
    # Roadmap case: local allowed ["*"] + server list -> server list wins.
    local = Policy(allowed_tools=frozenset({"*"}))
    eff = _poller(local)._effective_policy(
        _server_policy(allowed_tools=["get_system_info", "git_status"])
    )
    assert eff.allowed_tools == frozenset({"get_system_info", "git_status"})


def test_allowed_tools_is_set_intersection():
    local = Policy(allowed_tools=frozenset({"a_tool", "b_tool", "c_tool"}))
    eff = _poller(local)._effective_policy(
        _server_policy(allowed_tools=["b_tool", "c_tool", "d_tool"])
    )
    assert eff.allowed_tools == frozenset({"b_tool", "c_tool"})


def test_denied_tools_is_union():
    local = Policy(denied_tools=frozenset({"run_shell"}))
    eff = _poller(local)._effective_policy(
        _server_policy(denied_tools=["install_package"])
    )
    assert eff.denied_tools == frozenset({"run_shell", "install_package"})


def test_boolean_flags_are_anded():
    # Server allows writes, local forbids them -> forbidden.
    eff = _poller(Policy(allow_writes=False))._effective_policy(
        _server_policy(allow_writes=True)
    )
    assert eff.allow_writes is False
    # Local allows writes, server forbids them -> forbidden.
    eff = _poller(Policy(allow_writes=True))._effective_policy(
        _server_policy(allow_writes=False)
    )
    assert eff.allow_writes is False


def test_no_remote_policy_returns_local_policy():
    local = Policy(max_tier_unattended=1)
    assert _poller(local)._effective_policy(None).max_tier_unattended == 1


def test_merged_policy_denies_tier4_without_approval():
    local = Policy(unrestricted=True, max_tier_unattended=4)
    eff = _poller(local)._effective_policy(_server_policy(max_tier_unattended=2))
    assert not decide(eff, "delete_workspace_file").allowed
    assert decide(eff, "get_system_info").allowed


# --- R-P0-08: restrictive Linux default ------------------------------------


def test_linux_default_policy_is_restrictive():
    from agents.linux.agent_main import (
        LINUX_DEFAULT_ALLOWED_TOOLS,
        get_linux_server_policy,
    )
    from ai_bridge.policy import TOOL_TIERS, RiskTier

    pol = get_linux_server_policy()
    assert pol.unrestricted is False
    assert pol.max_tier_unattended == 2
    assert pol.allow_tier3 is False
    assert pol.desktop_enabled is False
    assert "*" not in pol.allowed_tools
    assert pol.allowed_tools == LINUX_DEFAULT_ALLOWED_TOOLS
    # Every allowlisted tool is a real, READ_ONLY-tiered tool.
    assert len(pol.allowed_tools) > 0
    for tool in pol.allowed_tools:
        assert TOOL_TIERS.get(tool) is RiskTier.READ_ONLY, tool


def test_linux_default_policy_denies_tier3_and_tier4():
    from agents.linux.agent_main import get_linux_server_policy

    pol = get_linux_server_policy()
    assert not decide(pol, "delete_workspace_file").allowed
    assert not decide(pol, "run_shell").allowed
    assert decide(pol, "get_system_info").allowed


# --- F07-05: token saver failure must fail loudly, never plaintext --------


def test_token_saver_failure_writes_no_plaintext_file(tmp_path, capsys):
    def failing_saver(token):
        raise RuntimeError("DPAPI unavailable in this test")

    token_file = tmp_path / "device.token"
    poller = Poller(
        "http://127.0.0.1:9",
        "test-token",
        token_file=token_file,
        token_saver=failing_saver,
    )
    poller._save_token("rotated-secret-token")
    assert not token_file.exists()
    assert "SECURITY" in capsys.readouterr().err


def test_token_saver_success_still_works(tmp_path):
    saved = {}

    def ok_saver(token):
        saved["token"] = token

    poller = Poller(
        "http://127.0.0.1:9",
        "test-token",
        token_file=tmp_path / "device.token",
        token_saver=ok_saver,
    )
    poller._save_token("rotated-secret-token")
    assert saved["token"] == "rotated-secret-token"
    assert not (tmp_path / "device.token").exists()


# --- F07-06: Linux token stored in one place only --------------------------


def test_linux_token_stored_only_in_device_token(tmp_path, monkeypatch):
    from agents.linux import agent_main

    monkeypatch.setattr(agent_main, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = {
        "control_url": "https://test.control.plane",
        "device_token": "linux-test-token-777",
        "device_name": "linux-prod-node-1",
        "workspace": str(tmp_path / "workspace"),
    }
    agent_main.save_agent_config(cfg)

    token_file = tmp_path / "device.token"
    assert token_file.read_text(encoding="utf-8").strip() == "linux-test-token-777"
    if os.name != "nt":
        assert (token_file.stat().st_mode & 0o777) == 0o600

    agent_json = (tmp_path / "agent.json").read_text(encoding="utf-8")
    assert "linux-test-token-777" not in agent_json

    agent_env = (tmp_path / "agent.env").read_text(encoding="utf-8")
    assert "linux-test-token-777" not in agent_env
    assert "AI_BRIDGE_DEVICE_TOKEN" not in agent_env
    assert "AI_BRIDGE_UNRESTRICTED" not in agent_env
    assert "AI_BRIDGE_MAX_TIER_UNATTENDED" not in agent_env

    # The agent still resolves its token — from device.token, not env.
    loaded = agent_main.load_agent_config()
    assert loaded["device_token"] == "linux-test-token-777"

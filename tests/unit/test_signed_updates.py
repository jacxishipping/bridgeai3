"""Wave 2A — signed fleet updates (R-P0-16) and child-env scrub (SEC-07).

Covers:
  * valid signed manifest with a pinned commit -> checkout of exactly that SHA
  * tampered manifest / tampered signature -> refused, no checkout
  * missing signed-update config -> PermissionError (fail closed, no unsigned fallback)
  * pinned commit not available locally and no remote -> refused + rollback to previous HEAD
  * _run_argv child env contains only the allowlist (no DATABASE_URL / secrets)
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ai_bridge import tools, updates
from ai_bridge.policy import Policy
from ai_bridge.updates import (
    generate_keypair,
    load_signed_manifest,
    sign_manifest,
    signed_update_configured,
)

POLICY = Policy(allow_tier3=True)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def _make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-b", "main").returncode == 0
    # Gate 13 (Wave 7): update_fleet_agent() only updates directories that
    # provably are AI Bridge checkouts (.git + pyproject.toml named
    # ai-bridge), so the scratch repos must look like real checkouts.
    (repo / "pyproject.toml").write_text('[project]\nname = "ai-bridge"\nversion = "0.3.0"\n')
    (repo / "a.txt").write_text("one\n")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-m", "first").returncode == 0
    sha1 = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("two\n")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-m", "second").returncode == 0
    sha2 = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert sha1 != sha2 and len(sha1) == 40 and len(sha2) == 40
    return repo, sha1, sha2


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_signed(tmp_path: Path, manifest: dict) -> tuple[Path, Path, Path]:
    keys = tmp_path / "keys"
    priv, pub = generate_keypair(keys)
    sig = sign_manifest(manifest, priv)
    manifest_path = tmp_path / "release.json"
    sig_path = tmp_path / "release.json.sig"
    manifest_path.write_text(json.dumps(manifest))
    sig_path.write_text(sig)
    return manifest_path, sig_path, pub


def _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub):
    monkeypatch.setenv("AI_BRIDGE_UPDATE_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("AI_BRIDGE_UPDATE_SIGNATURE_PATH", str(sig_path))
    monkeypatch.setenv("AI_BRIDGE_UPDATE_PUBLIC_KEY", str(pub))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


def test_valid_signed_manifest_checkouts_pinned_commit(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    manifest = {"name": "ai-bridge", "version": "0.3.0", "commit": sha2, "files": []}
    manifest_path, sig_path, pub = _write_signed(tmp_path, manifest)
    _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub)

    assert signed_update_configured() is True
    loaded, _sig = load_signed_manifest()
    assert loaded["commit"] == sha2

    result = tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=repo)
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert result["stdout"] == sha2  # pinned commit in stdout
    assert _head(repo) == sha2
    rollback = json.loads((tmp_path / "home" / ".ai-bridge" / "update-rollback.json").read_text())
    assert rollback["previous_commit"] == sha1
    assert rollback["pinned_commit"] == sha2


def test_tampered_manifest_refused(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    manifest = {"name": "ai-bridge", "version": "0.3.0", "commit": sha2, "files": []}
    manifest_path, sig_path, pub = _write_signed(tmp_path, manifest)
    _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub)
    # Tamper AFTER signing: point the pin at the other commit.
    manifest_path.write_text(json.dumps({**manifest, "commit": sha1}))

    with pytest.raises(PermissionError):
        tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=repo)
    assert _head(repo) == sha1  # no checkout happened


def test_tampered_signature_refused(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    manifest = {"name": "ai-bridge", "version": "0.3.0", "commit": sha2, "files": []}
    manifest_path, sig_path, pub = _write_signed(tmp_path, manifest)
    _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub)
    sig_path.write_text("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    with pytest.raises(PermissionError):
        tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=repo)
    assert _head(repo) == sha1


def test_missing_signed_update_config_refused(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    for var in ("AI_BRIDGE_UPDATE_MANIFEST_PATH", "AI_BRIDGE_UPDATE_SIGNATURE_PATH",
                "AI_BRIDGE_UPDATE_PUBLIC_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert signed_update_configured() is False
    with pytest.raises(PermissionError, match="signed release pin"):
        tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=repo)
    assert _head(repo) == sha1  # no unsigned fallback checkout


def test_unavailable_pinned_commit_refused_and_rolls_back(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    missing = "0" * 39 + "1"  # well-formed 40-hex SHA, not in this repo, no remote
    manifest = {"name": "ai-bridge", "version": "0.3.0", "commit": missing, "files": []}
    manifest_path, sig_path, pub = _write_signed(tmp_path, manifest)
    _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub)

    with pytest.raises((ValueError, RuntimeError)):
        tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=repo)
    assert _head(repo) == sha1  # rolled back to the recorded HEAD


def test_short_commit_refused(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    manifest = {"name": "ai-bridge", "version": "0.3.0", "commit": sha2[:7], "files": []}
    manifest_path, sig_path, pub = _write_signed(tmp_path, manifest)
    _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub)
    with pytest.raises(ValueError, match="40-hex"):
        updates.apply_pinned_git_update(repo, manifest, sig_path.read_text().strip(), pub)
    assert _head(repo) == sha1


# --- Gate 13 (Wave 7): repo_dir resolution must never silently use an
# arbitrary process working directory. ---


def test_non_checkout_repo_dir_refused(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    manifest = {"name": "ai-bridge", "version": "0.3.0", "commit": sha2, "files": []}
    manifest_path, sig_path, pub = _write_signed(tmp_path, manifest)
    _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub)
    # A git repo that is NOT an ai-bridge checkout (no pyproject.toml).
    other = tmp_path / "other"
    other.mkdir()
    assert _git(other, "init", "-b", "main").returncode == 0
    (other / "a.txt").write_text("x\n")
    assert _git(other, "add", ".").returncode == 0
    assert _git(other, "commit", "-m", "x").returncode == 0
    with pytest.raises(ValueError, match="not an AI Bridge checkout"):
        tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=other)
    assert _head(repo) == sha1


def test_repo_dir_from_env_var(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    manifest = {"name": "ai-bridge", "version": "0.3.0", "commit": sha2, "files": []}
    manifest_path, sig_path, pub = _write_signed(tmp_path, manifest)
    _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub)
    monkeypatch.setenv("AI_BRIDGE_AGENT_REPO_DIR", str(repo))
    result = tools.update_fleet_agent(approval_granted=True, policy=POLICY)
    assert result["ok"] is True
    assert _head(repo) == sha2


def test_cwd_fallback_refused_when_not_a_checkout(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    manifest = {"name": "ai-bridge", "version": "0.3.0", "commit": sha2, "files": []}
    manifest_path, sig_path, pub = _write_signed(tmp_path, manifest)
    _configure(monkeypatch, tmp_path, manifest_path, sig_path, pub)
    monkeypatch.delenv("AI_BRIDGE_AGENT_REPO_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # not a checkout: no .git / pyproject.toml
    with pytest.raises(ValueError, match="not an AI Bridge checkout"):
        tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=None)


def test_child_env_contains_only_allowlist(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:secretpass@db:5432/ai")
    monkeypatch.setenv("AI_BRIDGE_VAULT_TOKEN", "vault-secret")
    monkeypatch.setenv("AI_BRIDGE_TURN_SECRET", "turn-secret")
    monkeypatch.setenv("AI_BRIDGE_DEVICE_TOKEN", "device-secret")
    monkeypatch.setenv("AI_BRIDGE_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setenv("SOME_WEBHOOK_URL", "https://hooks.example.invalid/x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")

    env = tools._clean_child_env()
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM",
               "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
               "USERPROFILE", "LOCALAPPDATA", "APPDATA"}
    assert set(env) <= allowed
    for leaked in ("DATABASE_URL", "AI_BRIDGE_VAULT_TOKEN", "AI_BRIDGE_TURN_SECRET",
                   "AI_BRIDGE_DEVICE_TOKEN", "AI_BRIDGE_OIDC_CLIENT_SECRET",
                   "SOME_WEBHOOK_URL", "OPENAI_API_KEY"):
        assert leaked not in env
    assert "PATH" in env and "LANG" in env

    # End-to-end through the real runner: the child sees only the allowlist.
    result = tools._run_argv(["env"], None, 30)
    assert result["returncode"] == 0
    seen = {}
    for line in (result["stdout"] or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            seen[key] = value
    assert set(seen) <= allowed
    for leaked in ("DATABASE_URL", "AI_BRIDGE_VAULT_TOKEN", "AI_BRIDGE_TURN_SECRET",
                   "AI_BRIDGE_DEVICE_TOKEN", "SOME_WEBHOOK_URL", "OPENAI_API_KEY"):
        assert leaked not in seen


def test_update_url_enforces_https_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    assert updates.validate_update_url("https://releases.example.com/v0.4.0/app.zip") == (
        "https://releases.example.com/v0.4.0/app.zip"
    )
    for bad in (
        "http://releases.example.com/app.zip",
        "file:///etc/passwd",
        "ftp://releases.example.com/app.zip",
        "https:///missing-host.zip",
        "",
    ):
        with pytest.raises(ValueError, match="https://|required"):
            updates.validate_update_url(bad)

    manifest = {
        "name": "ai-bridge",
        "version": "0.4.0",
        "files": [{"path": "a.bin", "sha256": "0" * 64, "url": "http://insecure.example/a.bin"}],
    }
    with pytest.raises(ValueError, match="https://"):
        updates.download_and_stage_update(manifest, staging_dir=tmp_path / "staging")


def test_monotonic_version_floor_blocks_rollback(tmp_path, monkeypatch):
    repo, sha1, sha2 = _make_repo(tmp_path)
    assert _git(repo, "checkout", "-f", sha1).returncode == 0
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    # Upgrade to 0.4.0 ratchets the monotonic floor.
    m_up = {"name": "ai-bridge", "version": "0.4.0", "commit": sha2, "files": []}
    mp_up, sp_up, pub_up = _write_signed(tmp_path / "up", m_up)
    res = updates.apply_pinned_git_update(repo, m_up, sp_up.read_text().strip(), pub_up)
    assert res["ok"] is True
    assert updates.get_version_floor() == "0.4.0"

    # A validly signed older manifest (0.3.9 or 0.4.0-rc1) must be refused.
    for older in ("0.3.9", "0.4.0-rc1"):
        m_old = {"name": "ai-bridge", "version": older, "commit": sha1, "files": []}
        mp_old, sp_old, pub_old = _write_signed(tmp_path / f"old_{older}", m_old)
        with pytest.raises(ValueError, match="Rollback refused"):
            updates.apply_pinned_git_update(repo, m_old, sp_old.read_text().strip(), pub_old)
        assert _head(repo) == sha2

    # Same-version or higher-version manifest is allowed.
    m_next = {"name": "ai-bridge", "version": "0.4.1", "commit": sha1, "files": []}
    mp_next, sp_next, pub_next = _write_signed(tmp_path / "next", m_next)
    res2 = updates.apply_pinned_git_update(repo, m_next, sp_next.read_text().strip(), pub_next)
    assert res2["ok"] is True
    assert updates.get_version_floor() == "0.4.1"

    # download_and_stage_update also refuses downgrades against persisted floor or env floor.
    with pytest.raises(ValueError, match="Rollback refused"):
        updates.download_and_stage_update(
            {"name": "ai-bridge", "version": "0.4.0", "files": []},
            staging_dir=tmp_path / "staging",
        )

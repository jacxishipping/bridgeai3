"""Wave 7 (gate 13) — release manifest generation + signed-release dry run.

Covers:
  * generate_release_manifest.build_manifest pins the repo HEAD commit
    (full 40-hex SHA) and lists payload files with correct SHA-256 + URL
  * --include globs filter the payload; a non-git directory is refused
  * scripts/release_manifest_dryrun.py runs end to end with a throwaway
    test keypair (keygen -> manifest -> sign -> verify -> hash-verified
    download -> pinned fleet apply -> unavailable-pin refusal) and
    exits 0 with an evidence JSON
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_release_manifest", SCRIPTS / "generate_release_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "ai-bridge"\nversion = "0.3.0-test"\n')
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_bytes(b"print('hi')\n")
    (repo / "notes.txt").write_bytes(b"notes\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "first")
    return repo


def test_build_manifest_pins_head_and_hashes_files(tmp_path):
    gen = _load_generator()
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    manifest = gen.build_manifest(
        repo, name="ai-bridge", version="0.3.0-test",
        base_url="https://example.com/releases/0.3.0-test/",
        includes=[],
    )
    assert manifest["name"] == "ai-bridge"
    assert manifest["version"] == "0.3.0-test"
    assert manifest["commit"] == head and len(head) == 40
    by_path = {f["path"]: f for f in manifest["files"]}
    assert sorted(by_path) == ["notes.txt", "pyproject.toml", "src/app.py"]
    import hashlib

    assert by_path["src/app.py"]["sha256"] == hashlib.sha256(b"print('hi')\n").hexdigest()
    assert by_path["src/app.py"]["url"] == "https://example.com/releases/0.3.0-test/src/app.py"


def test_build_manifest_include_filter(tmp_path):
    gen = _load_generator()
    repo = _make_repo(tmp_path)
    manifest = gen.build_manifest(
        repo, name="ai-bridge", version="0.3.0-test",
        base_url="https://example.com/r/", includes=["src/**"],
    )
    assert [f["path"] for f in manifest["files"]] == ["src/app.py"]


def test_build_manifest_refuses_non_repo(tmp_path):
    gen = _load_generator()
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a git checkout"):
        gen.build_manifest(plain, name="ai-bridge", version="0.3.0-test",
                           base_url="https://example.com/r/", includes=[])


def test_release_manifest_dryrun_end_to_end(tmp_path):
    work = tmp_path / "dryrun"
    evidence = tmp_path / "evidence.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "release_manifest_dryrun.py"),
         "--workdir", str(work), "--evidence-out", str(evidence)],
        capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DRY RUN OK" in proc.stdout
    data = json.loads(evidence.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert len(data["sha_v1"]) == 40 and len(data["sha_v2"]) == 40
    assert data["sha_v1"] != data["sha_v2"]
    assert all(step["ok"] for step in data["steps"])
    assert [s["step"] for s in data["steps"]] == [
        "keygen", "origin-repo", "agent-clone-at-v1", "manifest-generated",
        "sign-and-verify", "tampered-manifest-refused",
        "corrupt-signature-refused", "download-and-stage",
        "fleet-apply-pinned", "unavailable-pin-refused-and-rolled-back",
    ]
    # The throwaway test keypair must not survive the run.
    assert not (work / "keys").exists()
    assert (work / "evidence.json").exists() or evidence.exists()

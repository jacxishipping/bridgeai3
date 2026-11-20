#!/usr/bin/env python3
"""End-to-end dry run of the signed release flow with a throwaway keypair.

Gate 13 (Wave 7): this is the dry run the production gate requires —
a signed release manifest is generated, signed with a TEST Ed25519
keypair, verified, downloaded hash-checked, and applied to a scratch
agent checkout. Nothing is published, nothing is deployed, and no real
fleet device is touched. The test keypair lives only inside the dry-run
work directory and is deleted with it (unless --keep-workdir).

Steps:
  1. generate a test Ed25519 keypair (ai_bridge.updates.generate_keypair)
  2. build a scratch origin repo with two commits (v1 -> v2)
  3. clone it as the "agent" checkout, parked at the v1 commit
  4. generate a release manifest pinned to the v2 commit
     (scripts/generate_release_manifest.py, file:// download URLs)
  5. sign the manifest (ai_bridge.updates CLI) and verify the signature
  6. negative: tampered manifest and corrupted signature both fail verify
  7. download_and_stage_update: hash-verified download of every file
  8. apply via tools.update_fleet_agent(repo_dir=<agent clone>) ->
     HEAD == v2 commit, rollback snapshot recorded
  9. negative: a manifest pinned to an unavailable commit is refused
     and the agent checkout stays at v2 (rollback)

Usage:
    python scripts/release_manifest_dryrun.py [--workdir DIR] [--keep-workdir]
        [--evidence-out PATH]

Exit 0 prints "DRY RUN OK" and writes an evidence JSON file with the
commit SHAs, file hashes, and per-step results.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_bridge import tools, updates  # noqa: E402
from ai_bridge.policy import Policy  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "dryrun",
    "GIT_AUTHOR_EMAIL": "dryrun@example.invalid",
    "GIT_COMMITTER_NAME": "dryrun",
    "GIT_COMMITTER_EMAIL": "dryrun@example.invalid",
}
POLICY = Policy(allow_tier3=True)


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, timeout=120)


def _git(repo: Path, *args: str) -> str:
    proc = _run(["git", "-C", str(repo), *args], env=GIT_ENV)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _step(log: list[dict], name: str, ok: bool, detail: str = "") -> None:
    log.append({"step": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise RuntimeError(f"dry-run step failed: {name}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_manifest_dryrun")
    parser.add_argument("--workdir", default=None, help="work directory (default: fresh mkdtemp)")
    parser.add_argument("--keep-workdir", action="store_true",
                        help="do not delete the work directory (test keypair stays inside it)")
    parser.add_argument("--evidence-out", default=None, help="evidence JSON path")
    args = parser.parse_args(argv)

    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="ai-bridge-dryrun-"))
    work.mkdir(parents=True, exist_ok=True)
    evidence_path = Path(args.evidence_out) if args.evidence_out else work / "evidence.json"
    log: list[dict] = []
    evidence: dict = {"workdir": str(work), "steps": log}
    try:
        # 1. Test keypair.
        keys = work / "keys"
        priv, pub = updates.generate_keypair(keys)
        _step(log, "keygen", priv.is_file() and pub.is_file(), f"public={pub.name}")

        # 2. Scratch origin repo, two commits.
        origin = work / "origin"
        origin.mkdir()
        _git(origin, "init", "-b", "main")
        (origin / "pyproject.toml").write_text('[project]\nname = "ai-bridge"\nversion = "0.3.0-test"\n')
        (origin / "a.txt").write_text("one\n")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "v1")
        sha1 = _git(origin, "rev-parse", "HEAD")
        (origin / "a.txt").write_text("two\n")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "v2")
        sha2 = _git(origin, "rev-parse", "HEAD")
        assert sha1 != sha2 and len(sha1) == 40 and len(sha2) == 40
        evidence.update({"sha_v1": sha1, "sha_v2": sha2})
        _step(log, "origin-repo", True, f"v1={sha1[:12]} v2={sha2[:12]}")

        # 3. Agent clone parked at v1.
        agent = work / "agent"
        proc = _run(["git", "clone", str(origin), str(agent)], env=GIT_ENV)
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {proc.stderr.strip()}")
        _git(agent, "checkout", "-f", sha1)
        assert _git(agent, "rev-parse", "HEAD") == sha1
        _step(log, "agent-clone-at-v1", True, f"HEAD={sha1[:12]}")

        # 4. Generate the manifest pinned to v2 (file:// download URLs).
        manifest_path = work / "release.json"
        gen = _run([
            sys.executable, str(REPO_ROOT / "scripts" / "generate_release_manifest.py"),
            "--repo", str(origin), "--version", "0.3.0-test",
            "--base-url", origin.resolve().as_uri(), "--out", str(manifest_path),
        ])
        if gen.returncode != 0:
            raise RuntimeError(f"manifest generation failed: {gen.stdout} {gen.stderr}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["commit"] == sha2 and len(manifest["files"]) == 2
        evidence["manifest_files"] = [
            {"path": f["path"], "sha256": f["sha256"]} for f in manifest["files"]
        ]
        _step(log, "manifest-generated", True,
              f"commit={sha2[:12]} files={len(manifest['files'])}")

        # 5. Sign + verify via the CLI.
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        sign = _run([sys.executable, "-m", "ai_bridge.updates", "sign",
                     "--private-key", str(priv), "--manifest", str(manifest_path)], env=env)
        if sign.returncode != 0:
            raise RuntimeError(f"sign failed: {sign.stdout} {sign.stderr}")
        sig_path = work / "release.json.sig"
        sig_path.write_text(sign.stdout.strip())
        verify = _run([sys.executable, "-m", "ai_bridge.updates", "verify",
                       "--public-key", str(pub), "--manifest", str(manifest_path),
                       "--signature", str(sig_path)], env=env)
        _step(log, "sign-and-verify", verify.returncode == 0 and "verified" in verify.stdout,
              f"signature={sign.stdout.strip()[:24]}...")
        evidence["signature_prefix"] = sign.stdout.strip()[:24]

        # 6. Negatives: tampered manifest / corrupted signature fail verify.
        tampered = work / "release-tampered.json"
        tampered.write_text(json.dumps({**manifest, "commit": sha1}))
        bad_verify = _run([sys.executable, "-m", "ai_bridge.updates", "verify",
                           "--public-key", str(pub), "--manifest", str(tampered),
                           "--signature", str(sig_path)], env=env)
        _step(log, "tampered-manifest-refused", bad_verify.returncode != 0,
              f"exit={bad_verify.returncode}")
        bad_sig = work / "release-bad.sig"
        bad_sig.write_text("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        bad_verify2 = _run([sys.executable, "-m", "ai_bridge.updates", "verify",
                            "--public-key", str(pub), "--manifest", str(manifest_path),
                            "--signature", str(bad_sig)], env=env)
        _step(log, "corrupt-signature-refused", bad_verify2.returncode != 0,
              f"exit={bad_verify2.returncode}")

        # 7. Hash-verified download into staging (file:// URLs).
        staging = work / "staging"
        staged = updates.download_and_stage_update(manifest, staging_dir=staging, apply=False)
        _step(log, "download-and-stage", staged["ok"] and staged["downloaded"] == ["a.txt", "pyproject.toml"],
              f"downloaded={staged['downloaded']}")

        # 8. Apply via the fleet entry point.
        home = work / "home"
        home.mkdir()
        old_env = dict(os.environ)
        os.environ["AI_BRIDGE_UPDATE_MANIFEST_PATH"] = str(manifest_path)
        os.environ["AI_BRIDGE_UPDATE_SIGNATURE_PATH"] = str(sig_path)
        os.environ["AI_BRIDGE_UPDATE_PUBLIC_KEY"] = str(pub)
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        try:
            result = tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=agent)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert result["ok"] is True and result["stdout"] == sha2
        assert _git(agent, "rev-parse", "HEAD") == sha2
        rollback = json.loads((home / ".ai-bridge" / "update-rollback.json").read_text())
        assert rollback["previous_commit"] == sha1 and rollback["pinned_commit"] == sha2
        _step(log, "fleet-apply-pinned", True,
              f"HEAD={sha2[:12]} rollback_previous={sha1[:12]}")

        # 9. Negative: unavailable pinned commit is refused, HEAD stays at v2.
        missing = "0" * 39 + "1"
        bad_manifest = {**manifest, "commit": missing}
        bad_manifest_path = work / "release-missing.json"
        bad_manifest_path.write_text(json.dumps(bad_manifest))
        bad_sig2 = updates.sign_manifest(bad_manifest, priv)
        bad_sig_path = work / "release-missing.json.sig"
        bad_sig_path.write_text(bad_sig2)
        os.environ["AI_BRIDGE_UPDATE_MANIFEST_PATH"] = str(bad_manifest_path)
        os.environ["AI_BRIDGE_UPDATE_SIGNATURE_PATH"] = str(bad_sig_path)
        os.environ["AI_BRIDGE_UPDATE_PUBLIC_KEY"] = str(pub)
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        try:
            tools.update_fleet_agent(approval_granted=True, policy=POLICY, repo_dir=agent)
            refused = False
        except (ValueError, RuntimeError):
            refused = True
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        assert _git(agent, "rev-parse", "HEAD") == sha2
        _step(log, "unavailable-pin-refused-and-rolled-back", refused,
              "HEAD still v2 after refusal")

        evidence["ok"] = True
        print("DRY RUN OK")
        return 0
    except Exception:
        evidence["ok"] = False
        traceback.print_exc()
        print("DRY RUN FAILED")
        return 1
    finally:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        print(f"evidence: {evidence_path}")
        if not args.keep_workdir:
            # Delete the work directory (the throwaway test keypair is
            # always deleted) but preserve the evidence file itself.
            keep = None
            try:
                if evidence_path.resolve().is_relative_to(work.resolve()):
                    keep = evidence_path.resolve()
            except Exception:
                keep = None
            for child in list(work.iterdir()):
                if keep is not None and child.resolve() == keep:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            if keep is None:
                shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

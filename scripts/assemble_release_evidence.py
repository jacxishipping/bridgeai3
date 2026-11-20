#!/usr/bin/env python3
"""Assemble a self-contained, checksummed Release Evidence Bundle (Release 2).

Packages:
- CycloneDX 1.5 SBOM (``sbom.cdx.json``)
- Migration & rollback inventory (``migration-rollback-plan.json``)
- Artifact metadata & git provenance (``artifact-metadata.json``)
- Copied test/scan/gate evidence files from ``--evidence-dir`` (if any)
- ``SHA256SUMS.txt`` covering every file in the output bundle

Usage:
    python scripts/assemble_release_evidence.py --out-dir .run/release-evidence [--evidence-dir .run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def build_migration_rollback_plan(repo_root: Path) -> dict[str, Any]:
    mig_dir = repo_root / "migrations"
    up_files = sorted(p.name for p in mig_dir.glob("[0-9][0-9][0-9]_*.sql"))
    down_files = sorted(p.name for p in mig_dir.glob("down_*.sql"))
    down_versions = {name.split("_")[1] for name in down_files if len(name.split("_")) >= 2}
    up_entries = []
    for name in up_files:
        ver = name.split("_")[0]
        up_entries.append({
            "version": ver,
            "up_file": name,
            "sha256": _sha256(mig_dir / name),
            "has_down_migration": ver in down_versions,
        })
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "up_count": len(up_entries),
        "down_count": len(down_files),
        "latest_version": up_entries[-1]["version"] if up_entries else None,
        "migrations": up_entries,
        "down_files": down_files,
        "rollback_procedure": [
            "1. Pause traffic / enable emergency stop (POST /api/emergency-stop) before schema rollback.",
            "2. Take a fresh verified database backup (scripts/backup_restore.py backup).",
            "3. Run down migration(s) via ai_bridge.control.migrations.run_down_migration(conn, version).",
            "4. Roll back control-plane image/package and re-verify GET /health/ready + GET /api/audit/verify.",
        ],
    }


def assemble_bundle(
    repo_root: Path,
    out_dir: Path,
    *,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    import generate_sbom  # type: ignore[import-not-found]

    out_dir.mkdir(parents=True, exist_ok=True)

    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(pyproject.get("project", {}).get("version") or "0.0.0")
    commit = _git_output(repo_root, "rev-parse", "HEAD")
    branch = _git_output(repo_root, "rev-parse", "--abbrev-ref", "HEAD")

    # 1. CycloneDX SBOM
    sbom = generate_sbom.build_sbom(repo_root)
    sbom_path = out_dir / "sbom.cdx.json"
    sbom_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # 2. Migration & rollback plan
    mig_plan = build_migration_rollback_plan(repo_root)
    mig_path = out_dir / "migration-rollback-plan.json"
    mig_path.write_text(json.dumps(mig_plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # 3. Copy any test/scan/gate evidence files
    copied_evidence: list[str] = []
    if evidence_dir is not None and evidence_dir.is_dir():
        ev_out = out_dir / "evidence"
        ev_out.mkdir(parents=True, exist_ok=True)
        for src in sorted(evidence_dir.iterdir()):
            if src.is_file() and src.suffix.lower() in (".json", ".xml", ".sarif", ".txt", ".log"):
                if src.resolve().is_relative_to(out_dir.resolve()):
                    continue
                dst = ev_out / src.name
                shutil.copy2(src, dst)
                copied_evidence.append(f"evidence/{src.name}")

    # 4. Artifact metadata
    meta = {
        "name": "ai-bridge",
        "version": version,
        "git_commit": commit,
        "git_branch": branch,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sbom_components": len(sbom["components"]),
        "migrations_latest": mig_plan["latest_version"],
        "migrations_count": mig_plan["up_count"],
        "copied_evidence": copied_evidence,
    }
    meta_path = out_dir / "artifact-metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # 5. Deterministic SHA256SUMS.txt
    checksum_lines: list[str] = []
    for p in sorted(out_dir.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS.txt":
            rel = p.relative_to(out_dir).as_posix()
            checksum_lines.append(f"{_sha256(p)}  {rel}")
    sums_path = out_dir / "SHA256SUMS.txt"
    sums_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    return {**meta, "out_dir": str(out_dir), "checksum_files": len(checksum_lines)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble AI Bridge Release Evidence Bundle.")
    parser.add_argument("--out-dir", default=".run/release-evidence", help="Output bundle directory")
    parser.add_argument("--evidence-dir", default=None, help="Optional directory containing CI test/scan evidence files")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="Repository root")
    args = parser.parse_args(argv)

    summary = assemble_bundle(
        Path(args.repo),
        Path(args.out_dir),
        evidence_dir=Path(args.evidence_dir) if args.evidence_dir else None,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

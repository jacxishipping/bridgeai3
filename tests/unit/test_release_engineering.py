"""Release 2 — Release engineering, SBOM, and supply-chain gate tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generate_sbom_produces_valid_cyclonedx_1_5(tmp_path):
    mod = _load_script("generate_sbom")
    out = tmp_path / "sbom.cdx.json"
    assert mod.main(["--out", str(out), "--repo", str(REPO_ROOT)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["bomFormat"] == "CycloneDX"
    assert data["specVersion"] == "1.5"
    assert data["metadata"]["component"]["name"] == "ai-bridge"
    names = {c["name"].lower() for c in data["components"]}
    assert "fastapi" in names
    assert "pydantic" in names
    # npm lockfile components are also inventoried when package-lock.json exists
    if (REPO_ROOT / "apps" / "dashboard" / "package-lock.json").is_file():
        assert any(c["purl"].startswith("pkg:npm/") for c in data["components"])


def test_assemble_release_evidence_bundle_with_checksums_and_rollback_plan(tmp_path):
    mod = _load_script("assemble_release_evidence")
    ev_in = tmp_path / "ci-in"
    ev_in.mkdir()
    (ev_in / "sample-gate.json").write_text('{"ok": true}\n', encoding="utf-8")

    out_dir = tmp_path / "bundle"
    summary = mod.assemble_bundle(REPO_ROOT, out_dir, evidence_dir=ev_in)
    assert summary["name"] == "ai-bridge"
    assert summary["migrations_count"] >= 17
    assert summary["migrations_latest"] == "017"
    assert "evidence/sample-gate.json" in summary["copied_evidence"]

    assert (out_dir / "sbom.cdx.json").is_file()
    assert (out_dir / "migration-rollback-plan.json").is_file()
    assert (out_dir / "artifact-metadata.json").is_file()
    assert (out_dir / "SHA256SUMS.txt").is_file()

    mig_plan = json.loads((out_dir / "migration-rollback-plan.json").read_text(encoding="utf-8"))
    assert any(m["version"] == "017" and m["has_down_migration"] is True for m in mig_plan["migrations"])

    sums = (out_dir / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert any(line.endswith("  sbom.cdx.json") for line in sums)
    assert any(line.endswith("  migration-rollback-plan.json") for line in sums)
    assert any(line.endswith("  evidence/sample-gate.json") for line in sums)


def test_ci_release_and_dependabot_configs_present():
    ci_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "security-and-supply-chain:" in ci_text
    assert "generate_sbom.py" in ci_text
    assert "assemble_release_evidence.py" in ci_text
    assert "release_manifest_dryrun.py" in ci_text

    rel_path = REPO_ROOT / ".github" / "workflows" / "release.yml"
    assert rel_path.is_file()
    rel_text = rel_path.read_text(encoding="utf-8")
    assert "AI_BRIDGE_RELEASE_SIGNING_KEY" in rel_text
    assert "assemble_release_evidence.py" in rel_text

    dep_path = REPO_ROOT / ".github" / "dependabot.yml"
    assert dep_path.is_file()
    dep_text = dep_path.read_text(encoding="utf-8")
    for eco in ("pip", "npm", "nuget", "docker", "github-actions"):
        assert f'"{eco}"' in dep_text

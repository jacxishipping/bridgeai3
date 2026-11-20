#!/usr/bin/env python3
"""Zero-dependency CycloneDX 1.5 SBOM generator for AI Bridge (Release 2).

Inventory sources:
- Python dependencies from ``pyproject.toml`` and installed ``importlib.metadata``
- Dashboard JavaScript dependencies from ``apps/dashboard/package-lock.json``
- .NET WPF client references from ``apps/windows-client/AIBridgeClient.csproj``

Usage:
    python scripts/generate_sbom.py --out .run/sbom.cdx.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import time
import tomllib
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _parse_req_name(spec: str) -> str:
    return re.split(r"[\s\[<>=!~;]", spec.strip(), maxsplit=1)[0].strip()


def collect_python_components(repo_root: Path) -> tuple[str, list[dict[str, Any]]]:
    pyproject = repo_root / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    proj = data.get("project", {})
    version = str(proj.get("version") or "0.0.0")

    declared: set[str] = set()
    for dep in proj.get("dependencies", []):
        name = _parse_req_name(str(dep))
        if name:
            declared.add(name.lower())
    for group in (proj.get("optional-dependencies") or {}).values():
        for dep in group:
            name = _parse_req_name(str(dep))
            if name:
                declared.add(name.lower())

    components: dict[str, dict[str, Any]] = {}
    for dist in importlib.metadata.distributions():
        name = str(dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        ver = str(dist.version or "")
        key = name.lower()
        components[key] = {
            "type": "library",
            "name": name,
            "version": ver,
            "purl": f"pkg:pypi/{key}@{ver}" if ver else f"pkg:pypi/{key}",
            "scope": "required" if key in declared else "optional",
        }

    # Ensure declared deps appear even if not installed in this venv
    for name in sorted(declared):
        if name not in components:
            components[name] = {
                "type": "library",
                "name": name,
                "version": "declared",
                "purl": f"pkg:pypi/{name}",
                "scope": "required",
            }
    return version, [components[k] for k in sorted(components)]


def collect_npm_components(repo_root: Path) -> list[dict[str, Any]]:
    lock = repo_root / "apps" / "dashboard" / "package-lock.json"
    if not lock.is_file():
        return []
    data = json.loads(lock.read_text(encoding="utf-8"))
    packages = data.get("packages") or {}
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for pkg_path, meta in packages.items():
        if not pkg_path or not isinstance(meta, dict):
            continue
        name = meta.get("name") or pkg_path.split("node_modules/")[-1]
        ver = str(meta.get("version") or "")
        if not name or not ver:
            continue
        key = (str(name), ver)
        if key not in seen:
            entry: dict[str, Any] = {
                "type": "library",
                "name": str(name),
                "version": ver,
                "purl": f"pkg:npm/{name}@{ver}",
                "scope": "optional" if meta.get("dev") else "required",
            }
            if meta.get("integrity"):
                entry["properties"] = [{"name": "npm:integrity", "value": str(meta["integrity"])}]
            seen[key] = entry
    return [seen[k] for k in sorted(seen)]


def collect_dotnet_components(repo_root: Path) -> list[dict[str, Any]]:
    csproj = repo_root / "apps" / "windows-client" / "AIBridgeClient.csproj"
    if not csproj.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        tree = ET.parse(csproj)
        for elem in tree.iter("PackageReference"):
            name = (elem.attrib.get("Include") or "").strip()
            ver = (elem.attrib.get("Version") or "").strip()
            if name:
                out.append({
                    "type": "library",
                    "name": name,
                    "version": ver or "unspecified",
                    "purl": f"pkg:nuget/{name}@{ver}" if ver else f"pkg:nuget/{name}",
                    "scope": "required",
                })
    except Exception:
        pass
    return sorted(out, key=lambda c: (c["name"], c["version"]))


def build_sbom(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    version, py_comps = collect_python_components(repo_root)
    npm_comps = collect_npm_components(repo_root)
    dotnet_comps = collect_dotnet_components(repo_root)
    components = py_comps + npm_comps + dotnet_comps

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "component": {
                "type": "application",
                "name": "ai-bridge",
                "version": version,
                "purl": f"pkg:pypi/ai-bridge@{version}",
            },
            "tools": [{"name": "ai-bridge-generate-sbom", "version": "1.0.0"}],
            "properties": [
                {"name": "aibridge:python_components", "value": str(len(py_comps))},
                {"name": "aibridge:npm_components", "value": str(len(npm_comps))},
                {"name": "aibridge:dotnet_components", "value": str(len(dotnet_comps))},
            ],
        },
        "components": components,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate CycloneDX 1.5 SBOM for AI Bridge.")
    parser.add_argument("--out", default=".run/sbom.cdx.json", help="Output CycloneDX JSON path")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="Repository root")
    args = parser.parse_args(argv)

    sbom = build_sbom(Path(args.repo))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"SBOM written: {out_path} ({len(sbom['components'])} components, sha256={_sha256_file(out_path)[:16]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Provision the native C++ ``relay-engine`` binary (vercel-labs/screen-relay) into ``~/.ai-bridge/bin``.

Usage:
    python scripts/install_screen_relay.py                  # Try GitHub Release download, then local source build
    python scripts/install_screen_relay.py --from-source    # Clone vercel-labs/screen-relay & build with make
    python scripts/install_screen_relay.py --check          # Verify installed relay-engine binary & backend status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

UPSTREAM_GIT_URL = "https://github.com/vercel-labs/screen-relay.git"
DEFAULT_RELEASE_REPO = os.environ.get("AI_BRIDGE_RELEASE_REPO", "KHAEntertainment/bridgeai3")


def _target_descriptor() -> tuple[str, str]:
    sys_os = platform.system()
    if sys_os == "Windows":
        return "windows-x64", "relay-engine.exe"
    if sys_os == "Darwin":
        return "macos-arm64", "relay-engine"
    return "linux-x64", "relay-engine"


def _install_dir() -> Path:
    custom = os.environ.get("AI_BRIDGE_BIN_DIR", "").strip()
    target = Path(custom) if custom else (Path.home() / ".ai-bridge" / "bin")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_viewer_stub(dest_dir: Path) -> None:
    viewer = dest_dir / "viewer"
    viewer.mkdir(parents=True, exist_ok=True)
    idx = viewer / "index.html"
    js = viewer / "viewer.js"
    if not idx.is_file():
        idx.write_text("<!doctype html><title>AI Bridge Screen Relay</title>\n", encoding="utf-8")
    if not js.is_file():
        js.write_text("// loopback headless viewer stub\n", encoding="utf-8")


def _download_from_release(dest_dir: Path) -> Path | None:
    target, exe_name = _target_descriptor()
    ext = ".exe" if exe_name.endswith(".exe") else ""
    asset_name = f"relay-engine-{target}{ext}"
    api_url = f"https://api.github.com/repos/{DEFAULT_RELEASE_REPO}/releases/latest"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "AI-Bridge-ScreenRelay-Installer/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            release = json.loads(resp.read().decode("utf-8"))
        assets = {a["name"]: a["browser_download_url"] for a in release.get("assets") or []}
        if asset_name not in assets:
            return None

        raw_req = urllib.request.Request(assets[asset_name], headers={"User-Agent": "AI-Bridge-ScreenRelay-Installer/1.0"})
        with urllib.request.urlopen(raw_req, timeout=60) as dl:
            binary_bytes = dl.read()

        sha_name = f"relay-engine-{target}.sha256"
        if sha_name in assets:
            sha_req = urllib.request.Request(assets[sha_name], headers={"User-Agent": "AI-Bridge-ScreenRelay-Installer/1.0"})
            with urllib.request.urlopen(sha_req, timeout=15) as sdl:
                sha_lines = sdl.read().decode("utf-8", errors="replace").splitlines()
            expected = None
            for line in sha_lines:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
                    expected = parts[0].lower()
                    break
            if expected:
                actual = hashlib.sha256(binary_bytes).hexdigest().lower()
                if actual != expected:
                    raise RuntimeError(f"SHA-256 mismatch for {asset_name}: expected {expected}, got {actual}")

        out_path = dest_dir / exe_name
        out_path.write_bytes(binary_bytes)
        if os.name != "nt":
            out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _write_viewer_stub(dest_dir)
        return out_path
    except Exception as exc:
        print(f"[info] Prebuilt release asset unavailable ({exc}); trying local source build...", file=sys.stderr)
        return None


def _build_from_source(dest_dir: Path) -> Path:
    _target, exe_name = _target_descriptor()
    if not shutil.which("git"):
        raise RuntimeError("'git' is required to build vercel-labs/screen-relay from source.")
    if not shutil.which("make"):
        raise RuntimeError("'make' (and pkg-config + libx264 + libwebsockets) is required to build relay-engine.")

    with tempfile.TemporaryDirectory(prefix="screen-relay-build-") as tmp:
        src_dir = Path(tmp) / "screen-relay"
        print(f"==> Cloning {UPSTREAM_GIT_URL} ...")
        subprocess.run(["git", "clone", "--depth", "1", UPSTREAM_GIT_URL, str(src_dir)], check=True)

        make_cmd = ["make", f"-j{max(1, os.cpu_count() or 2)}"]
        if platform.system() == "Linux":
            # Include Wayland if wayland-scanner is installed
            if shutil.which("wayland-scanner"):
                make_cmd.append("BACKENDS=x11 wayland")
        make_cmd.append("relay-engine")

        print(f"==> Compiling relay-engine ({' '.join(make_cmd)}) ...")
        subprocess.run(make_cmd, cwd=str(src_dir), check=True)

        built = src_dir / exe_name
        if not built.is_file():
            built = src_dir / "relay-engine"
        if not built.is_file():
            raise RuntimeError(f"Build succeeded cleanly, but {exe_name} was not found in {src_dir}")

        out_path = dest_dir / exe_name
        shutil.copy2(built, out_path)
        if os.name != "nt":
            out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _write_viewer_stub(dest_dir)
        return out_path


def _verify_binary(bin_path: Path) -> dict:
    try:
        help_res = subprocess.run([str(bin_path), "--help"], capture_output=True, text=True, timeout=5, check=False)
        check_res = subprocess.run([str(bin_path), "--check"], capture_output=True, text=True, timeout=5, check=False)
        return {
            "installed": bin_path.is_file(),
            "binary_path": str(bin_path),
            "cli_ready": help_res.returncode == 0,
            "display_backend_ready": check_res.returncode == 0,
            "help_summary": (help_res.stdout or "").splitlines()[-1] if help_res.stdout else "",
        }
    except Exception as exc:
        return {"installed": bin_path.is_file(), "binary_path": str(bin_path), "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or verify native screen-relay (relay-engine) binary.")
    parser.add_argument("--from-source", action="store_true", help="Compile relay-engine directly from upstream source")
    parser.add_argument("--check", action="store_true", help="Check installed relay-engine binary and exit")
    args = parser.parse_args()

    dest_dir = _install_dir()
    _target, exe_name = _target_descriptor()
    existing = dest_dir / exe_name

    if args.check:
        if not existing.is_file():
            found = shutil.which(exe_name)
            if found:
                existing = Path(found)
        info = _verify_binary(existing) if existing.is_file() else {"installed": False, "expected_path": str(existing)}
        print(json.dumps(info, indent=2))
        return 0 if info.get("installed") else 1

    installed_path: Path | None = None
    if not args.from_source:
        installed_path = _download_from_release(dest_dir)
    if installed_path is None:
        installed_path = _build_from_source(dest_dir)

    status = _verify_binary(installed_path)
    print(json.dumps(status, indent=2))
    return 0 if status.get("installed") else 1


if __name__ == "__main__":
    raise SystemExit(main())


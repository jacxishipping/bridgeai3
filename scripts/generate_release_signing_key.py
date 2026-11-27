#!/usr/bin/env python3
"""Generate an Ed25519 release signing keypair for AI_BRIDGE_RELEASE_SIGNING_KEY.

Usage:
  python scripts/generate_release_signing_key.py --out-dir keys/release

Never commit private.pem. Paste private.pem into the GitHub Actions secret
AI_BRIDGE_RELEASE_SIGNING_KEY so release.yml can emit release-manifest.json.sig.
See docs/release-signing.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_bridge.updates import generate_keypair  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default="keys/release",
        help="Directory for private.pem + public.pem (default: keys/release)",
    )
    args = parser.parse_args()
    out = Path(args.out_dir)
    priv, pub = generate_keypair(out)
    print(f"private: {priv}")
    print(f"public:  {pub}")
    print()
    print("Next steps:")
    print("  1. Store private PEM as GitHub secret AI_BRIDGE_RELEASE_SIGNING_KEY")
    print("  2. Distribute public.pem to agents (AI_BRIDGE_UPDATE_PUBLIC_KEY)")
    print("  3. Push a v* tag (or re-run Release) to produce release-manifest.json.sig")
    print("  4. Never commit private.pem")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

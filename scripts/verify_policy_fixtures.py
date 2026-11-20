"""Verify CI Policy-as-Code simulation fixtures against the live Policy engine.

Usage:
    python scripts/verify_policy_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_bridge.policy import Policy  # noqa: E402
from ai_bridge.policy_simulation import simulate_policy_decision  # noqa: E402

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "policy_simulation_fixtures.json"


def run_fixtures(fixture_path: Path = FIXTURE_PATH) -> tuple[int, list[str]]:
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    cases = data.get("cases") or []
    for case in cases:
        name = case["name"]
        raw_pol = case.get("policy") or {}
        kwargs = {}
        for k in ("allow_writes", "allow_tier3", "desktop_enabled"):
            if k in raw_pol:
                kwargs[k] = bool(raw_pol[k])
        if "max_tier_unattended" in raw_pol:
            kwargs["max_tier_unattended"] = int(raw_pol["max_tier_unattended"])
        for k in ("allowed_services", "allowed_apps", "allowed_test_commands", "denied_tools", "allowed_tools"):
            if k in raw_pol:
                kwargs[k] = frozenset(raw_pol[k])
        pol = Policy(**kwargs)
        call = case["call"]
        res = simulate_policy_decision(
            call["tool"],
            pol,
            actor=str(call.get("actor") or "ci-fixture"),
            device_id=call.get("device_id"),
            approval_granted=bool(call.get("approval_granted", False)),
            is_admin=bool(call.get("is_admin", False)),
            device_revoked=bool(call.get("device_revoked", False)),
        )
        expected = case.get("expected") or {}
        for ek, ev in expected.items():
            if res.get(ek) != ev:
                failures.append(f"{name}: expected {ek}={ev!r}, got {res.get(ek)!r} (full={res})")
    return len(cases), failures


def main() -> int:
    total, failures = run_fixtures()
    if failures:
        print(f"FAIL: {len(failures)}/{total} policy simulation fixtures failed:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"OK: {total}/{total} policy simulation fixtures passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

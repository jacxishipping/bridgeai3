"""Release 4: Policy-as-Code signed policy import/export & dry-run verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import HTTPException

from ..policy import Policy
from ..policy_simulation import simulate_batch

BUNDLE_SCHEMA = "ai-bridge-policy-bundle/v1"

# Strict whitelist of importable policy fields; ``unrestricted`` and
# ``unrestricted_granted_by`` are NEVER importable via policy bundles.
ALLOWED_POLICY_FIELDS = (
    "allow_writes",
    "allow_tier3",
    "allow_file_deletion",
    "allow_shell",
    "allow_package_install",
    "desktop_enabled",
    "max_tier_unattended",
    "allowed_apps",
    "allowed_services",
    "allowed_test_commands",
    "allowed_tools",
    "denied_tools",
    "note",
)

REPRESENTATIVE_SIM_CALLS = [
    {"tool": "get_system_info"},
    {"tool": "write_workspace_file"},
    {"tool": "run_shell"},
    {"tool": "mouse_click"},
    {"tool": "delete_workspace_file"},
]


def _signing_key(fallback_admin_token: str | None = None) -> bytes:
    raw = (os.environ.get("AI_BRIDGE_POLICY_SIGNING_KEY") or fallback_admin_token or os.environ.get("AI_BRIDGE_ADMIN_TOKEN") or "").strip()
    if not raw:
        raise HTTPException(
            status_code=503,
            detail="Policy bundle signing key not configured (set AI_BRIDGE_POLICY_SIGNING_KEY or AI_BRIDGE_ADMIN_TOKEN).",
        )
    return raw.encode("utf-8")


def sanitize_policy_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a validated, sanitized policy dict stripped of escalation keys."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="policy must be a JSON object.")
    if raw.get("unrestricted") or raw.get("unrestricted_granted_by"):
        raise HTTPException(status_code=403, detail="Policy bundles cannot set 'unrestricted' bypass fields.")
    out: dict[str, Any] = {}
    for key in ALLOWED_POLICY_FIELDS:
        if key not in raw or raw[key] is None:
            continue
        val = raw[key]
        if key in ("allow_writes", "allow_tier3", "allow_file_deletion", "allow_shell", "allow_package_install", "desktop_enabled"):
            out[key] = bool(val)
        elif key == "max_tier_unattended":
            try:
                tier_int = int(val)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_tier_unattended must be an integer 0-4.") from None
            if not (0 <= tier_int <= 4):
                raise HTTPException(status_code=400, detail="max_tier_unattended must be between 0 and 4.")
            out[key] = tier_int
        elif key == "note":
            out[key] = str(val)[:500]
        else:
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise HTTPException(status_code=400, detail=f"{key} must be a list of strings.")
            out[key] = sorted(set(val))
    return out


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_policy_bundle(
    policy: dict[str, Any],
    *,
    version: int,
    actor: str,
    fallback_admin_token: str | None = None,
) -> dict[str, Any]:
    clean = sanitize_policy_dict(policy)
    payload = {
        "schema": BUNDLE_SCHEMA,
        "source_version": int(version),
        "exported_by": actor,
        "exported_at": round(time.time(), 3),
        "policy": clean,
    }
    canon = _canonical_json(payload)
    digest = hashlib.sha256(canon).hexdigest()
    sig = hmac.new(_signing_key(fallback_admin_token), canon, hashlib.sha256).hexdigest()
    return {
        **payload,
        "sha256": digest,
        "signature_alg": "hmac-sha256",
        "signature": sig,
    }


def verify_policy_bundle(
    bundle: dict[str, Any],
    *,
    fallback_admin_token: str | None = None,
) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=400, detail="Bundle must be a JSON object.")
    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise HTTPException(status_code=400, detail=f"Unsupported bundle schema (expected '{BUNDLE_SCHEMA}').")
    sig = str(bundle.get("signature") or "").strip()
    digest = str(bundle.get("sha256") or "").strip()
    if not sig or not digest:
        raise HTTPException(status_code=400, detail="Policy bundle must include sha256 digest and HMAC signature.")
    payload = {
        "schema": bundle.get("schema"),
        "source_version": bundle.get("source_version"),
        "exported_by": bundle.get("exported_by"),
        "exported_at": bundle.get("exported_at"),
        "policy": bundle.get("policy"),
    }
    canon = _canonical_json(payload)
    expected_digest = hashlib.sha256(canon).hexdigest()
    if not hmac.compare_digest(digest, expected_digest):
        raise HTTPException(status_code=403, detail="Policy bundle SHA-256 digest mismatch (tampered payload).")
    expected_sig = hmac.new(_signing_key(fallback_admin_token), canon, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise HTTPException(status_code=403, detail="Policy bundle HMAC signature verification failed.")
    clean = sanitize_policy_dict(bundle["policy"])
    return clean


def diff_policies(current: dict[str, Any], proposed: dict[str, Any]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for key in ALLOWED_POLICY_FIELDS:
        before = current.get(key)
        after = proposed.get(key)
        if before != after:
            diffs.append({"field": key, "before": before, "after": after})
    return diffs


def simulate_policy_dict(policy_dict: dict[str, Any]) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {}
    for k in ("allow_writes", "allow_tier3", "desktop_enabled"):
        if k in policy_dict:
            kwargs[k] = bool(policy_dict[k])
    if "max_tier_unattended" in policy_dict:
        kwargs["max_tier_unattended"] = int(policy_dict["max_tier_unattended"])
    for k in ("allowed_services", "allowed_apps", "allowed_test_commands", "denied_tools", "allowed_tools"):
        if k in policy_dict and isinstance(policy_dict[k], list):
            kwargs[k] = frozenset(policy_dict[k])
    pol = Policy(**kwargs)
    return simulate_batch(REPRESENTATIVE_SIM_CALLS, pol)

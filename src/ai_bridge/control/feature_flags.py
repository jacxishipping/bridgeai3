"""Release 4 feature flags and adoption metrics registry.

Every Release 4 capability is gated behind an explicit flag controlled by
``AI_BRIDGE_FEATURE_<FLAG_UPPER>=1|0`` and records lightweight adoption
counters so operators can measure usage and roll back safely.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import HTTPException

# Flag -> (default_enabled, description)
FEATURE_DEFINITIONS: dict[str, tuple[bool, str]] = {
    "fleet_analytics": (True, "Daily fleet health and task-success trends with CSV export."),
    "incident_inbox": (True, "In-app notification centre / incident inbox for failed tasks, offline devices, and expired approvals."),
    "policy_as_code": (True, "Signed policy bundle import/export and CI simulation fixtures."),
    "oidc_group_mapping": (False, "OIDC group-to-role mapping with deny-by-default and audit logging (explicit opt-in)."),
    "managed_update_channels": (True, "Canary verify-only update ring with approval gate, pause, and rollback."),
    "curated_workflows": (True, "Read-only diagnostic workflow templates with per-step approval-to-audit timeline."),
    "governed_memory": (True, "Per-workspace read-only knowledge snippets with provenance, injection filtering, and opt-in prompt use."),
    "desktop_capabilities_v2": (True, "Capability-aware per-device desktop contract advertising interaction only where tested and permitted."),
}

_lock = threading.Lock()
_adoption_counts: dict[str, dict[str, int]] = {}


def env_var_for_flag(flag: str) -> str:
    return f"AI_BRIDGE_FEATURE_{flag.strip().upper()}"


def is_enabled(flag: str) -> bool:
    if flag not in FEATURE_DEFINITIONS:
        return False
    default_on, _desc = FEATURE_DEFINITIONS[flag]
    raw = os.environ.get(env_var_for_flag(flag))
    if raw is None or not raw.strip():
        return default_on
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default_on


def require_feature(flag: str) -> None:
    if not is_enabled(flag):
        raise HTTPException(
            status_code=404,
            detail=f"Feature '{flag}' is disabled (enable via {env_var_for_flag(flag)}=1).",
        )


def record_adoption(flag: str, action: str = "use") -> None:
    with _lock:
        bucket = _adoption_counts.setdefault(flag, {})
        bucket[action] = bucket.get(action, 0) + 1


def reset_adoption_metrics() -> None:
    with _lock:
        _adoption_counts.clear()


def list_features() -> dict[str, Any]:
    with _lock:
        snapshot = {k: dict(v) for k, v in _adoption_counts.items()}
    features = {}
    for flag, (_default, desc) in FEATURE_DEFINITIONS.items():
        features[flag] = {
            "enabled": is_enabled(flag),
            "env_var": env_var_for_flag(flag),
            "description": desc,
            "adoption": snapshot.get(flag, {}),
        }
    return {"features": features}

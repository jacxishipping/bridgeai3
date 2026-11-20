"""One task-creation service (R-P0-10, Wave 2A).

Every entry point that creates a queued task — ``POST /api/tasks``,
``POST /api/v1/tasks``, the AI API's remote-execution branch, workflow
steps that target a remote device, and terminal relay tasks — routes
through :func:`create_task` here, so the policy check, the approval gate,
idempotency, tenant scoping, takeover and emergency-stop checks are
identical everywhere. There is no second task-creation path.

Rules enforced here (all server-side):
- Human takeover active on the device blocks autonomous (non-human-initiated)
  task creation with 409.
- Durable emergency stop blocks all new tasks with 403.
- The device is looked up tenant-scoped; a device in another tenant is
  indistinguishable from a missing device (404).
- The tool must be known (400) and allowed by ``policy.decide()`` with
  ``approval_granted=False, is_admin=False`` — the gate is purely
  policy-based, so an admin caller does not silently bypass the Tier-3
  opt-in. A bare ``unrestricted`` device policy is ignored by ``decide()``
  (Wave 1C, R-P0-02); only a policy stamped with ``unrestricted_granted_by``
  by an explicit audited admin action counts, and even then Tier 4 always
  requires a recorded approval.
- When ``decide()`` reports ``needs_approval``, an approval row is created
  and the task is stored as ``needs_approval`` — it is never executed until
  an admin decides it (``decide_approval`` flips it to ``queued``).
- Idempotency via ``task_submissions`` (actor + Idempotency-Key), exactly
  as ``/api/tasks`` did before: same key + same payload replays the
  original task; same key + different payload is a 409.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Callable

from fastapi import HTTPException

from ..policy import TOOL_TIERS, Policy, decide, normalize_tool_name
from . import db as dbmod
from . import plane_state

_APPROVAL_TTL_ENV = "AI_BRIDGE_APPROVAL_TTL_SECONDS"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Best-effort column read that tolerates sqlite3.Row and RowDict."""
    if row is None:
        return default
    try:
        return dict(row).get(key, default)
    except Exception:
        try:
            return row[key]
        except Exception:
            return default


def policy_from_plane_dict(eff: dict[str, Any] | None) -> Policy:
    """Build an admin-owned :class:`Policy` from a plane policy dict.

    A bare ``unrestricted=True`` in the dict is carried into the Policy
    object, but ``decide()`` ignores it unless ``unrestricted_granted_by``
    names the admin who granted it (Wave 1C, R-P0-02). Only the
    ``unrestricted_granted_by`` stamp — written by the admin-only
    enroll / set-policy paths — confers the bypass, and Tier 4 still
    always needs approval.
    """
    eff = eff or {}
    allowed_tools = eff.get("allowed_tools")
    return Policy(
        allow_writes=bool(eff.get("allow_writes", True)),
        allow_tier3=bool(eff.get("allow_tier3", False)),
        desktop_enabled=bool(eff.get("desktop_enabled", False)),
        max_tier_unattended=int(eff.get("max_tier_unattended", 2) or 0),
        allowed_tools=frozenset(allowed_tools) if allowed_tools is not None else None,
        denied_tools=frozenset(eff.get("denied_tools") or []),
        allowed_apps=frozenset(eff.get("allowed_apps") or []),
        allowed_services=frozenset(eff.get("allowed_services") or []),
        allowed_test_commands=frozenset(
            eff.get("allowed_test_commands")
            or ["pytest", "pytest -q", "npm test", "go test"]
        ),
        unrestricted=bool(eff.get("unrestricted", False)),
        unrestricted_granted_by=eff.get("unrestricted_granted_by") or None,
    )


def user_tenant(conn: Any, username: str | None) -> str:
    """Tenant owning a user account (migration 013; ``"default"`` if unset)."""
    if not username:
        return "default"
    try:
        row = conn.execute(
            "SELECT tenant FROM users WHERE username=?", (username,)
        ).fetchone()
    except Exception:
        return "default"
    tenant = _row_get(row, "tenant", None)
    return str(tenant or "default")


def session_tenant(conn: Any, session_token: str | None) -> str | None:
    """Tenant bound to a session token, or None if the token is unknown."""
    if not session_token:
        return None
    try:
        row = conn.execute(
            "SELECT username, tenant FROM sessions WHERE token_hash=?",
            (dbmod.hash_token(session_token),),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    tenant = _row_get(row, "tenant", None)
    if tenant:
        return str(tenant)
    return user_tenant(conn, _row_get(row, "username"))


def resolve_tenant(
    requested: str | None,
    *,
    session_tenant: str | None,
    is_legacy_admin: bool,
) -> str:
    """Resolve the effective tenant for a request (R-P0-12).

    Session users are strictly scoped to their own tenant: a
    caller-supplied tenant that differs is rejected with 403.
    The legacy admin token is a global-admin credential and may address
    any tenant explicitly (it is off by default — see F-05-05).
    """
    requested = (requested or "").strip() or None
    if is_legacy_admin:
        return requested or session_tenant or "default"
    own = session_tenant or "default"
    if requested is not None and requested != own:
        raise HTTPException(
            status_code=403,
            detail=f"Tenant '{requested}' does not match the session tenant.",
        )
    return own


def verify_workflow_approval(conn: Any, approval_id: str | None) -> bool:
    """True iff ``approval_id`` is an approved whole-workflow approval.

    A whole-workflow approval is an ``approvals`` row for tool
    ``execute_workflow`` (or ``workflow``), status ``approved``,
    unexpired, decided by a named admin. It covers the workflow as
    submitted — it does not bypass per-step policy, it only satisfies
    the per-step approval requirement (R-P0-11).
    """
    if not approval_id:
        return False
    row = conn.execute(
        "SELECT status, tool, expires_at, decided_by FROM approvals WHERE id=?",
        (approval_id,),
    ).fetchone()
    if row is None:
        return False
    if _row_get(row, "status") != "approved":
        return False
    if _row_get(row, "tool") not in ("execute_workflow", "workflow"):
        return False
    expires_at = _row_get(row, "expires_at")
    if expires_at is not None and float(expires_at) <= dbmod.now():
        return False
    if not (_row_get(row, "decided_by") or "").strip():
        return False
    return True


def create_task(
    conn: Any,
    *,
    actor: str,
    role: str,
    tenant: str,
    device_id: str,
    tool: str,
    args: dict[str, Any] | None = None,
    request_key: str | None = None,
    task_id: str | None = None,
    approval_id: str | None = None,
    human_initiated: bool = False,
    device_row: Any = None,
    get_effective_device_policy: Callable[[Any], dict[str, Any]] | None = None,
    get_current_policy: Callable[[], dict[str, Any]] | None = None,
    notify: Callable[[str, str], None] | None = None,
    audit_fn: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Create one task through the single approval-gated path.

    Returns ``{"task_id", "status", "tier", "approval_id"}`` where status
    is ``"queued"`` or ``"needs_approval"``. Raises :class:`HTTPException`
    (400 unknown tool, 403 denied / e-stop / takeover-mismatch, 404
    unknown device, 409 takeover active / idempotency conflict).

    ``task_id`` lets bulk callers (fleet broadcast) keep their id format.
    ``approval_id`` lets a caller present a recorded approval (workflow
    step / whole-workflow approval); it is validated (approved, unexpired,
    tool matches or is a whole-workflow approval, same tenant) and, when
    valid, satisfies the approval gate instead of creating a new row.
    """
    args = dict(args or {})
    tool_name = normalize_tool_name(str(tool or "").strip())
    if not tool_name:
        raise HTTPException(status_code=400, detail="tool is required.")
    tier = TOOL_TIERS.get(tool_name)
    if tier is None:
        raise HTTPException(status_code=400, detail=f"Unknown tool: '{tool_name}'.")

    # Human takeover: autonomous tasks are paused; a human-initiated task
    # (interactive terminal relay) is the human's own action and proceeds.
    if not human_initiated and plane_state.is_human_takeover_active(conn, device_id):
        raise HTTPException(
            status_code=409,
            detail="Human takeover is active on this device; autonomous AI tasks are paused until control is handed back.",
        )
    if plane_state.load_emergency_stop(conn):
        raise HTTPException(
            status_code=403, detail="Emergency stop is active; no new tasks accepted."
        )

    if human_initiated:
        args["human_initiated"] = True

    # Tenant-scoped device lookup. device_id "local" is a virtual device
    # (no enrolled row): it is governed by the global policy and can only
    # be queued by callers that already passed through v1's device check.
    is_local = device_id == "local"
    row = device_row
    if row is None and not is_local:
        row = conn.execute(
            "SELECT * FROM devices WHERE id=? AND tenant=? AND revoked=0",
            (device_id, tenant),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found or revoked.")

    if row is not None and get_effective_device_policy is not None:
        eff_policy = get_effective_device_policy(row)
    elif get_current_policy is not None:
        eff_policy = get_current_policy()
    else:
        eff_policy = {}
    policy = policy_from_plane_dict(eff_policy)

    decision = decide(policy, tool_name, approval_granted=False, is_admin=False)
    if not decision.allowed and not decision.needs_approval:
        raise HTTPException(status_code=403, detail=decision.reason or "Tool denied by policy.")
    needs_approval = bool(decision.needs_approval)
    # decide() enforces the ceiling for Tier 3/4; Tier 1/2 need the same
    # ceiling check (pre-existing /api/tasks behaviour): anything above
    # max_tier_unattended requires a recorded approval to run.
    if not needs_approval and tier.value < 3 and tier.value > policy.max_tier_unattended:
        needs_approval = True

    task_id = task_id or uuid.uuid4().hex[:12]
    now_ts = dbmod.now()
    # A recorded approval supplied by the caller (workflow step / whole-
    # workflow approval): validate it and let it satisfy the gate instead
    # of creating a second approval row. A whole-workflow approval
    # (tool "execute_workflow"/"workflow") covers any step tool.
    if approval_id is not None:
        ap = conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if ap is None:
            raise HTTPException(
                status_code=409, detail="Provided approval does not exist."
            )
        ap_tool = _row_get(ap, "tool")
        if _row_get(ap, "status") != "approved":
            raise HTTPException(
                status_code=409, detail="Provided approval is not approved."
            )
        _ap_exp = _row_get(ap, "expires_at")
        if _ap_exp is not None and float(_ap_exp) <= dbmod.now():
            raise HTTPException(
                status_code=409, detail="Provided approval has expired."
            )
        allowed_ap_tools = (tool_name,) if tier.value >= 4 else (None, "", tool_name, "execute_workflow", "workflow")
        if ap_tool not in allowed_ap_tools:
            raise HTTPException(
                status_code=409,
                detail="Provided approval does not cover this tool.",
            )
        ap_tenant = _row_get(ap, "tenant")
        if ap_tenant and ap_tenant != tenant:
            raise HTTPException(
                status_code=403,
                detail="Provided approval belongs to another tenant.",
            )
        needs_approval = False

    if request_key is not None:
        request_hash = hashlib.sha256(
            json.dumps(
                {"device_id": device_id, "tenant": tenant, "tool": tool_name, "args": args},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    else:
        request_hash = ""

    with conn.transaction():
        if request_key:
            claim = conn.execute(
                "INSERT INTO task_submissions (actor, request_key, request_hash, task_id) VALUES (?,?,?,?) "
                "ON CONFLICT (actor, request_key) DO NOTHING",
                (actor, request_key, request_hash, task_id),
            )
            if claim.rowcount == 0:
                prior = conn.execute(
                    "SELECT * FROM task_submissions WHERE actor=? AND request_key=?",
                    (actor, request_key),
                ).fetchone()
                if prior is None or prior["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different task.",
                    )
                task = conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (prior["task_id"],)
                ).fetchone()
                if task is None:
                    raise HTTPException(
                        status_code=409,
                        detail="Original task was removed. This submission cannot be replayed.",
                    )
                return {
                    "task_id": task["id"],
                    "status": task["status"],
                    "tier": task["tier"],
                    "approval_id": task["approval_id"],
                }
        status = "queued"
        if needs_approval:
            approval_id = uuid.uuid4().hex[:12]
            approval_ttl = max(
                60, int(os.environ.get(_APPROVAL_TTL_ENV, "3600") or "3600")
            )
            policy_row = conn.execute("SELECT MAX(version) AS version FROM policies").fetchone()
            policy_version = _row_get(policy_row, "version") or 0
            conn.execute(
                "INSERT INTO approvals (id, task_id, tool, reason, tenant, created_at, expires_at, policy_version) VALUES (?,?,?,?,?,?,?,?)",
                (
                    approval_id,
                    task_id,
                    tool_name,
                    f"Tier-{tier.value} operation requires human approval.",
                    tenant,
                    now_ts,
                    now_ts + approval_ttl,
                    policy_version,
                ),
            )
            status = "needs_approval"
        conn.execute(
            "INSERT INTO tasks (id, device_id, tenant, tool, args_json, tier, status, approval_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                device_id,
                tenant,
                tool_name,
                json.dumps(args),
                tier.value,
                status,
                approval_id,
                now_ts,
                now_ts,
            ),
        )

    if audit_fn is not None:
        try:
            audit_fn(actor, device_id, tool_name, args, status, tenant=tenant)
        except Exception:
            pass
    else:
        # Gate 9 (Wave 6): no direct db.audit — route through the single
        # audit writer on the caller's live connection, so the row commits
        # with the task insert and keeps the task's tenant.
        try:
            from ..audit_writer import write_audit_event

            write_audit_event(
                actor=actor,
                action=tool_name,
                device_id=device_id,
                details=args,
                conn=conn,
                tenant=tenant,
                decision=status,
            )
        except Exception:
            pass
    if status == "queued" and notify is not None:
        try:
            notify(device_id, task_id)
        except Exception:
            pass
    return {"task_id": task_id, "status": status, "tier": tier.value, "approval_id": approval_id}

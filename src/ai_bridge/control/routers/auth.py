"""auth routes for the AI Bridge control plane (Wave 3B Track A).

Moved verbatim from ai_bridge.control.app.create_app(); handlers close
over the shared state via the ControlContext passed to the factory
(see routers/_context.py). Paths, methods and response shapes are
unchanged (see tests/unit/test_architecture_phase4_routers.py).
"""
from __future__ import annotations

import os
import secrets
import uuid

from fastapi import APIRouter, Body, Header, HTTPException

from .. import auth as authmod
from .. import db as dbmod
from ..app import _DUMMY_HASH, _DUMMY_SALT, LoginIn, StreamTicketIn, UserIn
from ..csrf import generate_csrf_token
from ._context import ControlContext


def create_auth_router(ctx: ControlContext) -> APIRouter:
    """Build the auth APIRouter bound to ``ctx``."""
    router = APIRouter()
    # Shared control-plane state (mirrors create_app() locals).
    _auth_context_full = ctx._auth_context_full
    _require_device_tenant = ctx._require_device_tenant
    _row_tenant = ctx._row_tenant
    audit = ctx.audit
    conn = ctx.conn
    require_admin = ctx.require_admin
    require_role = ctx.require_role
    resolve_request_tenant = ctx.resolve_request_tenant
    session_tenant_for_request = ctx.session_tenant_for_request
    token = ctx.token

    @router.get("/api/auth/csrf-token")
    def get_csrf_token(x_session_token: str | None = Header(default=None)):
        if not x_session_token:
            raise HTTPException(status_code=401, detail="Session token required.")
        row = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (dbmod.hash_token(x_session_token),)).fetchone()
        if row is None or row["expires_at"] < dbmod.now():
            raise HTTPException(status_code=401, detail="Invalid or expired session.")
        token = generate_csrf_token(x_session_token)
        return {"csrf_token": token, "expires_in": 86400}

    @router.post("/api/auth/login")
    def login(body: LoginIn):
        user = conn.execute("SELECT * FROM users WHERE username=? AND disabled=0", (body.username,)).fetchone()
        if user is None:
            authmod.verify_password(body.password, _DUMMY_SALT, _DUMMY_HASH)
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        if not authmod.verify_password(body.password, user["salt"], user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        session_token = secrets.token_urlsafe(32)
        expires = dbmod.now() + 8 * 3600
        conn.execute("INSERT INTO sessions (token_hash, username, role, tenant, expires_at, created_at) VALUES (?,?,?,?,?,?)",
                     (dbmod.hash_token(session_token), user["username"], user["role"], _row_tenant(user), expires, dbmod.now()))
        conn.commit()
        audit(user["username"], "", "login", {}, "allowed")
        return {"session_token": session_token, "username": user["username"], "role": user["role"], "tenant": _row_tenant(user), "expires_at": expires}

    @router.get("/api/auth/me")
    def me(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        actor, role, tenant, _is_legacy = _auth_context_full(x_admin_token, x_session_token)
        return {"username": actor, "role": role, "tenant": tenant}

    @router.post("/api/auth/logout")
    def logout(x_session_token: str | None = Header(default=None)):
        if not x_session_token:
            raise HTTPException(status_code=401, detail="Session token required.")
        cur = conn.execute("DELETE FROM sessions WHERE token_hash=?", (dbmod.hash_token(x_session_token),))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(status_code=401, detail="Invalid or expired session.")
        return {"logged_out": True}

    @router.post("/api/stream-ticket")
    def mint_stream_ticket_route(
        body: StreamTicketIn,
        x_admin_token: str | None = Header(default=None),
        x_session_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        actor, role, tenant, _is_legacy = _auth_context_full(x_admin_token, x_session_token, authorization)
        if authmod.ROLES.get(role, 0) < authmod.ROLES["viewer"]:
            raise HTTPException(status_code=403, detail=f"Role '{role}' cannot mint stream tickets (requires viewer).")
        device_id = (body.device_id or "").strip()
        session_id = (body.session_id or "").strip()
        if not device_id or not session_id:
            raise HTTPException(status_code=400, detail="device_id and session_id are required.")
        _require_device_tenant(device_id, x_admin_token, x_session_token, authorization)
        ticket, ttl = dbmod.mint_stream_ticket(
            conn, actor=actor, tenant=tenant, device_id=device_id, session_id=session_id
        )
        audit(actor, device_id, "stream_ticket_minted", {"session_id": session_id}, "allowed")
        return {"ticket": ticket, "device_id": device_id, "session_id": session_id, "expires_in": ttl}

    @router.get("/api/auth/oidc/configured")
    def oidc_configured():
        from .. import oidc as oidcmod

        return {"enabled": oidcmod.oidc_config() is not None}

    @router.get("/api/auth/oidc/login")
    def oidc_login():
        from fastapi.responses import RedirectResponse

        from .. import oidc as oidcmod

        if oidcmod.oidc_config() is None:
            raise HTTPException(status_code=400, detail="OIDC is not configured.")
        url, _, _ = oidcmod.new_login()
        return RedirectResponse(url, status_code=302)

    @router.get("/api/auth/oidc/callback")
    def oidc_callback(code: str = "", state: str = ""):
        import httpx
        from fastapi.responses import RedirectResponse

        from .. import oidc as oidcmod

        cfg = oidcmod.oidc_config()
        if cfg is None:
            raise HTTPException(status_code=400, detail="OIDC is not configured.")
        pending = oidcmod.pop_pending(state)
        if pending is None or not code:
            raise HTTPException(status_code=400, detail="Invalid or expired login attempt.")
        try:
            doc = oidcmod.discovery()
            token_form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": cfg["redirect_uri"],
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
            }
            if pending.get("code_verifier"):
                token_form["code_verifier"] = str(pending["code_verifier"])
            resp = httpx.post(
                doc["token_endpoint"],
                data=token_form,
                timeout=15,
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=401, detail="OIDC code exchange failed.")
            tokens = resp.json()
            claims = oidcmod.verify_id_token(tokens["id_token"], pending["nonce"])
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"OIDC token verification failed: {exc}") from exc

        email_verified = bool(claims.get("email_verified"))
        email = claims.get("email")
        if email and email_verified:
            username = str(email)[:200]
        else:
            sub = claims.get("sub") or claims.get("preferred_username")
            if not sub:
                raise HTTPException(status_code=400, detail="No usable identity in ID token.")
            username = f"oidc:{sub}"[:200]

        with conn.transaction():
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if user is not None and user["disabled"]:
                raise HTTPException(status_code=403, detail="User account is disabled.")
            if user is None:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, salt, role, created_at) VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], username, "oidc-only-" + secrets.token_hex(16), authmod.new_salt(), "viewer", dbmod.now()),
                )
                user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            session_token = secrets.token_urlsafe(32)
            expires = dbmod.now() + 8 * 3600
            conn.execute(
                "INSERT INTO sessions (token_hash, username, role, tenant, expires_at, created_at) VALUES (?,?,?,?,?,?)",
                (dbmod.hash_token(session_token), user["username"], user["role"], _row_tenant(user), expires, dbmod.now()),
            )
            audit(user["username"], "", "oidc_login", {}, "allowed")

        dashboard = cfg["dashboard_url"].rstrip("/") + "/"
        # F-05-15: the session token NEVER travels in the URL. Mint a
        # single-use, 60 s exchange code and hand the *code* to the
        # dashboard as a query param; the dashboard swaps it for the
        # token over POST /api/auth/oidc/exchange. The old
        # `#session_token=` fragment path is deleted, not kept as a
        # fallback (a fragment persists in browser history and is
        # readable by any script on the page).
        code = oidcmod.mint_oidc_exchange_code(session_token)
        return RedirectResponse(dashboard + "?oidc_code=" + code, status_code=302)

    @router.post("/api/auth/oidc/exchange")
    def oidc_exchange(body: dict = Body(default={})):
        """Swap a one-time OIDC exchange code for its session token.

        F-05-15 companion to ``oidc_callback``: the dashboard POSTs the
        ``?oidc_code=`` value it received on the redirect and gets
        ``{"session_token": ...}`` back in a JSON body — the token never
        appears in a URL. Unknown, expired, or replayed codes return
        401 (the code is single-use: redeem deletes it on first read).
        Rate-limited with the auth limiter via the HTTP middleware
        (``/api/auth/oidc/exchange`` is in its auth-path list).
        """
        from .. import oidc as oidcmod

        if not isinstance(body, dict):
            raise HTTPException(status_code=401, detail="Invalid or expired OIDC exchange code.")
        code = str(body.get("code") or "").strip()
        session_token = oidcmod.redeem_oidc_exchange_code(code) if code else None
        if session_token is None:
            raise HTTPException(status_code=401, detail="Invalid or expired OIDC exchange code.")
        return {"session_token": session_token}

    @router.get("/api/connectors/m365/status")
    def m365_status(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        from ...connectors import microsoft365 as m365

        require_role("viewer", x_admin_token, x_session_token)
        return m365.status()

    @router.post("/api/connectors/m365/login")
    def m365_login(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        from ...connectors import microsoft365 as m365

        actor = require_admin(x_admin_token, x_session_token)
        try:
            url = m365.new_login()
        except m365.M365Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(actor, "", "m365_login_start", {}, "allowed")
        return {"authorize_url": url}

    @router.get("/api/connectors/m365/callback")
    def m365_callback(code: str = "", state: str = ""):
        from fastapi.responses import RedirectResponse

        from ...connectors import microsoft365 as m365

        try:
            m365.complete_login(code, state)
        except m365.M365Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit("m365", "", "m365_connected", {}, "allowed")
        return RedirectResponse(os.environ.get("AI_BRIDGE_OIDC_DASHBOARD_URL", "/"), status_code=302)

    @router.delete("/api/connectors/m365")
    def m365_disconnect(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        from ...connectors import microsoft365 as m365

        actor = require_admin(x_admin_token, x_session_token)
        try:
            removed = m365.disconnect()
        except m365.M365Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(actor, "", "m365_disconnect", {}, "allowed")
        return {"disconnected": removed}

    @router.post("/api/auth/users")
    def create_user(body: UserIn, x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        with conn.transaction():
            existing = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            allow_bootstrap = bool(os.environ.get("AI_BRIDGE_ALLOW_BOOTSTRAP"))
            if (token and not allow_bootstrap) or existing > 0:
                actor = require_admin(x_admin_token, x_session_token)
            else:
                actor = "bootstrap"
            if body.role not in authmod.ROLES:
                raise HTTPException(status_code=400, detail=f"Unknown role: {body.role}")
            salt = authmod.new_salt()
            # Resolve the tenant BEFORE the INSERT try-block: resolve raises
            # HTTPException(403) on a cross-tenant request, which the bare
            # except below would otherwise misreport as "already exists".
            if x_admin_token or x_session_token:
                tenant = resolve_request_tenant(body.tenant, x_admin_token, x_session_token)
            else:
                tenant = body.tenant or "default"  # bootstrap: no credentials yet, first user owns the tenant
            try:
                conn.execute("INSERT INTO users (id, username, password_hash, salt, role, tenant, created_at) VALUES (?,?,?,?,?,?,?)",
                             (uuid.uuid4().hex[:12], body.username, authmod.hash_password(body.password, salt), salt, body.role, tenant, dbmod.now()))
            except Exception:
                raise HTTPException(status_code=400, detail="Username already exists.")
            audit(actor, "", "create_user", {"username": body.username, "role": body.role}, "allowed")
        return {"username": body.username, "role": body.role, "tenant": tenant}

    @router.get("/api/auth/users")
    def list_users(x_admin_token: str | None = Header(default=None), x_session_token: str | None = Header(default=None)):
        require_admin(x_admin_token, x_session_token)
        tenant, is_legacy = session_tenant_for_request(x_admin_token, x_session_token)
        if is_legacy:
            rows = conn.execute("SELECT username, role, tenant, created_at, disabled FROM users").fetchall()
        else:
            rows = conn.execute("SELECT username, role, tenant, created_at, disabled FROM users WHERE tenant=?", (tenant,)).fetchall()
        return [dict(r) for r in rows]

    return router

"""Generate the route tables in docs/api-reference.md from the live app.

The reference's route tables are generated, never hand-edited: this
script builds the control-plane app (``create_app``), expands the
included routers (FastAPI >= 0.163 keeps them as ``_IncludedRouter``
bookkeeping entries), and renders one Markdown table per area between
the ``BEGIN/END GENERATED ROUTES`` markers in docs/api-reference.md.

Usage:
    python3 scripts/generate_api_reference.py            # rewrite the tables
    python3 scripts/generate_api_reference.py --check    # exit 1 if stale

tests/unit/test_api_reference.py runs --check and also asserts every
(method, path) pair in the committed route snapshot appears in the doc.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DOC_PATH = REPO_ROOT / "docs" / "api-reference.md"
BEGIN = "<!-- BEGIN GENERATED ROUTES (scripts/generate_api_reference.py) -->"
END = "<!-- END GENERATED ROUTES -->"

# Paths that are genuinely public (no credential header declared and no
# in-handler auth): health/readiness, OpenAPI docs, installers, the SPA,
# the OIDC + OAuth callback/bootstrap endpoints, and the public model list.
PUBLIC_PATHS = {
    "/health", "/health/live", "/health/ready",
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    "/install.sh", "/install.ps1", "/{full_path:path}",
    "/api/auth/login",
    "/api/auth/oidc/login", "/api/auth/oidc/callback",
    "/api/auth/oidc/exchange", "/api/auth/oidc/configured",
    "/api/mcp/oauth/callback", "/api/connectors/m365/callback",
    "/api/v1/models", "/v1/models",
}

_HEADER_LABELS = {
    "x_admin_token": "Admin",
    "x_session_token": "Session",
    "x_device_token": "Device",
    "authorization": "Bearer",
    "x_client_cert_fingerprint": "mTLS",
    "x_ssl_client_fingerprint": "mTLS",
}

# (area title, matcher) in first-match order. Matchers take the path
# segments (split on "/") so "/api/agent" never swallows "/api/agents".
AREAS: list[tuple[str, object]] = []


def _seg(path: str) -> tuple[str, ...]:
    return tuple(s for s in path.split("/") if s)


def _area(path: str) -> str:
    s = _seg(path)
    if path in ("/health", "/health/live", "/health/ready",
                "/health/production-checklist", "/metrics",
                "/install.sh", "/install.ps1", "/openapi.json",
                "/docs", "/docs/oauth2-redirect", "/redoc",
                "/assets", "/{full_path:path}"):
        return "Health, readiness & meta"
    if s[:2] == ("api", "devices"):
        return "Devices"
    if s[:2] == ("api", "tasks"):
        return "Tasks"
    if s[:2] == ("api", "agent"):
        return "Agent API"
    if s[:2] == ("api", "auth") or s[:2] in (("api", "roles"), ("api", "tenants")):
        return "Auth, users & OIDC"
    if s[:2] == ("api", "approvals"):
        return "Approvals"
    if s[:2] == ("api", "audit"):
        return "Audit"
    if s[:2] == ("api", "policies") or s[:2] == ("api", "policy"):
        return "Policies"
    if s[:2] in (("api", "workflows"), ("api", "schedules"), ("api", "triggers")):
        return "Workflows, schedules & triggers"
    if s[:3] == ("api", "admin", "backups"):
        return "Backups"
    if s[:2] == ("api", "v1") or path == "/api/stream-ticket":
        return "Sessions & desktop streaming (v1)"
    if s[:2] in (("api", "ai"), ("api", "agents")) or s[:1] == ("v1",):
        return "AI & custom agents"
    if s[:2] == ("api", "mcp"):
        return "MCP servers"
    if s[:2] == ("api", "connectors"):
        return "Connectors"
    if s[:1] == ("api",):
        return "Admin, fleet & operations"
    return "Other"


AREA_ORDER = [
    "Health, readiness & meta",
    "Devices",
    "Tasks",
    "Agent API",
    "Auth, users & OIDC",
    "Approvals",
    "Audit",
    "Policies",
    "Workflows, schedules & triggers",
    "Backups",
    "Sessions & desktop streaming (v1)",
    "AI & custom agents",
    "MCP servers",
    "Connectors",
    "Admin, fleet & operations",
    "Other",
]


def _auth_label(path: str, methods: list[str], headers: list[str]) -> str:
    if not methods and path == "/api/agent/ws":
        return "Device"
    if not methods and "/ws/" in path:
        return "Ticket (?ticket=)"
    if not methods:
        return "—"
    labels: list[str] = []
    for h in headers:
        label = _HEADER_LABELS.get(h)
        if label and label not in labels:
            labels.append(label)
    if labels:
        return ", ".join(labels)
    if path in PUBLIC_PATHS:
        return "Public"
    # No declared credential header and not a known-public path: auth is
    # enforced inside the handler (e.g. a signed query param, or the route
    # is a guard that always rejects). Do not claim "Public".
    return "—"


def _collect_routes(app) -> list[dict]:
    from starlette.routing import Mount

    out: list[dict] = []

    def collect(routes) -> None:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                collect(original.routes)
                continue
            if isinstance(route, Mount):
                out.append({"methods": ["MOUNT"], "path": route.path,
                            "summary": "Static files", "headers": []})
                collect(route.routes)
                continue
            methods = sorted(getattr(route, "methods", set()) or set())
            dependant = getattr(route, "dependant", None)
            headers = sorted({p.name for p in (dependant.header_params or [])}) if dependant is not None else []
            out.append({
                "methods": methods,
                "path": getattr(route, "path", ""),
                "summary": getattr(route, "summary", None) or getattr(route, "name", ""),
                "headers": headers,
            })

    collect(app.routes)
    # Deduplicate identical rows (the same route object can be reached
    # twice through a mount + the app) and sort deterministically.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for row in out:
        key = (tuple(row["methods"]), row["path"], row["summary"], tuple(row["headers"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda r: (r["path"], " ".join(r["methods"])))
    return unique


def render_generated_block() -> str:
    """Render the generated Markdown (areas + tables + total)."""
    os.environ.setdefault("AI_BRIDGE_CSRF_SECRET", "api-reference-generator")
    from ai_bridge.control.app import create_app

    with tempfile.TemporaryDirectory(prefix="apiref-") as tmp:
        app = create_app(admin_token="test", db_path=str(Path(tmp) / "control.db"))
        try:
            routes = _collect_routes(app)
        finally:
            try:
                app.state.conn.close()
            except Exception:
                pass

    grouped: dict[str, list[dict]] = {area: [] for area in AREA_ORDER}
    for row in routes:
        grouped[_area(row["path"])].append(row)

    lines: list[str] = []
    total = 0
    for area in AREA_ORDER:
        rows = grouped[area]
        if not rows:
            continue
        lines.append(f"### {area}")
        lines.append("")
        lines.append("| Method | Path | Auth | Summary |")
        lines.append("| --- | --- | --- | --- |")
        for row in rows:
            methods = " ".join(row["methods"]) if row["methods"] else "WS"
            auth = _auth_label(row["path"], row["methods"], row["headers"])
            summary = (row["summary"] or "").replace("|", "\\|")
            lines.append(f"| {methods} | `{row['path']}` | {auth} | {summary} |")
            total += len(row["methods"]) if row["methods"] else 1
        lines.append("")
    lines.append(f"*{total} (method, path) pairs listed — generated from the live app; "
                 "see the note at the top of this file.*")
    return "\n".join(lines) + "\n"


def read_doc_block() -> str:
    text = DOC_PATH.read_text(encoding="utf-8")
    start = text.index(BEGIN) + len(BEGIN)
    end = text.index(END)
    return text[start:end].strip("\n") + "\n"


def write_doc_block(block: str) -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    start = text.index(BEGIN) + len(BEGIN)
    end = text.index(END)
    DOC_PATH.write_text(text[:start] + "\n" + block + text[end:], encoding="utf-8")


def main(argv: list[str]) -> int:
    block = render_generated_block()
    if "--check" in argv:
        current = read_doc_block()
        if current != block:
            print("docs/api-reference.md route tables are stale: run "
                  "python3 scripts/generate_api_reference.py", file=sys.stderr)
            return 1
        print("docs/api-reference.md route tables are current.")
        return 0
    write_doc_block(block)
    print(f"docs/api-reference.md route tables regenerated ({block.count(chr(10))} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

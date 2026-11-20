"""Generate docs/environment-reference.md from source code env-var references.

Scans ``src/ai_bridge/`` and ``agents/`` for ``os.environ.get(...)``,
``os.getenv(...)``, and ``os.environ[...]`` lookups, classifies variables
by subsystem, and renders a deterministic Markdown reference.

Usage:
    python scripts/generate_env_reference.py          # write docs/environment-reference.md
    python scripts/generate_env_reference.py --check  # exit 1 if stale
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (REPO_ROOT / "src" / "ai_bridge", REPO_ROOT / "agents")
DOC_PATH = REPO_ROOT / "docs" / "environment-reference.md"

ALLOWED_PREFIXES = (
    "AI_BRIDGE_",
    "DATABASE_URL",
    "REDIS_URL",
    "SLACK_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL",
    "TELEGRAM_",
    "ALERT_WEBHOOK_URL",
    "RENDER",
    "PORT",
)


def _is_documented_var(name: str) -> bool:
    if not name or not name.isascii():
        return False
    return any(name == p or name.startswith(p) for p in ALLOWED_PREFIXES)


def _category(name: str) -> str:
    if name.startswith(("DATABASE_URL", "AI_BRIDGE_DB", "AI_BRIDGE_PG", "AI_BRIDGE_SQLITE", "AI_BRIDGE_RETENTION", "AI_BRIDGE_BACKUP")):
        return "Database, pooling & retention"
    if name.startswith(("REDIS_URL", "AI_BRIDGE_REDIS", "AI_BRIDGE_WORKERS", "AI_BRIDGE_MULTI_HOST", "AI_BRIDGE_REQUIRE_REDIS")):
        return "Multi-worker & Redis"
    if name.startswith(
        (
            "AI_BRIDGE_ADMIN",
            "AI_BRIDGE_OIDC",
            "AI_BRIDGE_AUTH",
            "AI_BRIDGE_SESSION",
            "AI_BRIDGE_CSRF",
            "AI_BRIDGE_TOKEN",
            "AI_BRIDGE_SECRET",
            "AI_BRIDGE_MTLS",
            "AI_BRIDGE_KMS",
            "AI_BRIDGE_VAULT",
            "AI_BRIDGE_FERNET",
            "AI_BRIDGE_ENCRYPTION",
            "AI_BRIDGE_SIGNING",
            "AI_BRIDGE_UPDATE_PUBLIC_KEY",
            "AI_BRIDGE_ALLOW_",
        )
    ):
        return "Authentication, secrets & security"
    if name.startswith(("AI_BRIDGE_METRICS", "AI_BRIDGE_LOG", "AI_BRIDGE_ALERT", "SLACK_", "DISCORD_", "TELEGRAM_", "ALERT_WEBHOOK_URL")):
        return "Observability, metrics & alerts"
    if name.startswith(
        (
            "AI_BRIDGE_AGENT",
            "AI_BRIDGE_CONTROL_URL",
            "AI_BRIDGE_DEVICE",
            "AI_BRIDGE_SPOOL",
            "AI_BRIDGE_POLL",
            "AI_BRIDGE_HEARTBEAT",
            "AI_BRIDGE_UPDATE",
            "AI_BRIDGE_ROLLOUT",
            "AI_BRIDGE_RING",
        )
    ):
        return "Agents, spool & rollouts"
    if name.startswith(("AI_BRIDGE_WEBRTC", "AI_BRIDGE_TURN", "AI_BRIDGE_STUN", "AI_BRIDGE_DESKTOP", "AI_BRIDGE_STREAM")):
        return "Desktop & WebRTC streaming"
    if name.startswith(("AI_BRIDGE_MCP", "AI_BRIDGE_M365", "AI_BRIDGE_OAUTH", "AI_BRIDGE_OPENAI", "AI_BRIDGE_ANTHROPIC", "AI_BRIDGE_GEMINI", "AI_BRIDGE_LLM")):
        return "MCP, OAuth & AI connectors"
    return "Control plane & runtime"


CATEGORY_ORDER = [
    "Control plane & runtime",
    "Authentication, secrets & security",
    "Database, pooling & retention",
    "Multi-worker & Redis",
    "Observability, metrics & alerts",
    "Agents, spool & rollouts",
    "Desktop & WebRTC streaming",
    "MCP, OAuth & AI connectors",
]


def _extract_from_file(path: Path) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return found

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_env_call = False
            if isinstance(func, ast.Attribute) and func.attr == "get":
                if isinstance(func.value, ast.Attribute) and func.value.attr == "environ":
                    is_env_call = True
            elif isinstance(func, ast.Attribute) and func.attr == "getenv":
                is_env_call = True
            if is_env_call and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                var = node.args[0].value.strip()
                default: str | None = None
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    default = repr(node.args[1].value)
                found.append((var, default))
        elif isinstance(node, ast.Subscript):
            if (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                found.append((node.slice.value.strip(), None))
    return found


def collect_env_vars() -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for py_file in sorted(root.rglob("*.py")):
            rel = py_file.relative_to(REPO_ROOT).as_posix()
            for var, default in _extract_from_file(py_file):
                if not _is_documented_var(var):
                    continue
                entry = records.setdefault(var, {"files": set(), "defaults": set()})
                entry["files"].add(rel)  # type: ignore[union-attr]
                if default is not None:
                    entry["defaults"].add(default)  # type: ignore[union-attr]
    return records


def render_markdown(records: dict[str, dict[str, object]]) -> str:
    by_cat: dict[str, list[str]] = defaultdict(list)
    for var in sorted(records):
        by_cat[_category(var)].append(var)

    lines: list[str] = [
        "# Environment Variable Reference",
        "",
        "> Generated from `src/ai_bridge/` and `agents/` by `scripts/generate_env_reference.py`.",
        "> Run `python scripts/generate_env_reference.py` after adding or removing configuration variables.",
        "",
        f"Total documented variables: **{len(records)}**",
        "",
    ]

    for cat in CATEGORY_ORDER:
        vars_in_cat = by_cat.get(cat, [])
        if not vars_in_cat:
            continue
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| Variable | Observed default(s) | Referenced in |")
        lines.append("|---|---|---|")
        for var in vars_in_cat:
            entry = records[var]
            defaults = sorted(entry["defaults"])  # type: ignore[arg-type]
            files = sorted(entry["files"])  # type: ignore[arg-type]
            def_str = ", ".join(f"`{d}`" for d in defaults[:3]) if defaults else "—"
            file_str = ", ".join(f"`{f}`" for f in files[:3])
            if len(files) > 3:
                file_str += f" (+{len(files) - 3} more)"
            lines.append(f"| `{var}` | {def_str} | {file_str} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv
    rendered = render_markdown(collect_env_vars())

    if check:
        if not DOC_PATH.exists():
            print(f"MISSING: {DOC_PATH}")
            return 1
        current = DOC_PATH.read_text(encoding="utf-8")
        if current != rendered:
            print("STALE: docs/environment-reference.md does not match generated output.")
            print("Run: python scripts/generate_env_reference.py")
            return 1
        print(f"OK: {DOC_PATH.relative_to(REPO_ROOT).as_posix()} is up to date.")
        return 0

    DOC_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {DOC_PATH.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

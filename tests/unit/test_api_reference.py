"""Gate 20 (Wave 6): docs/api-reference.md is a complete v0.3 reference.

The route tables are generated from the live app by
scripts/generate_api_reference.py. These tests fail if the tables drift
from the app (run the generator) or if any route in the committed route
snapshot is missing from the doc.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "api-reference.md"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_generator():
    return _load_module(
        "generate_api_reference", REPO_ROOT / "scripts" / "generate_api_reference.py"
    )


def _doc_table_pairs() -> set[tuple[str, str]]:
    """Parse (method, path) pairs out of the generated tables."""
    gen = _load_generator()
    text = DOC_PATH.read_text(encoding="utf-8")
    start = text.index(gen.BEGIN) + len(gen.BEGIN)
    end = text.index(gen.END)
    block = text[start:end]
    pairs: set[tuple[str, str]] = set()
    for line in block.splitlines():
        m = re.match(r"^\|\s*([A-Z ]+?)\s*\|\s*`([^`]+)`\s*\|", line)
        if not m:
            continue
        for method in m.group(1).split():
            pairs.add((method, m.group(2)))
    return pairs


def test_generated_table_is_current():
    gen = _load_generator()
    assert gen.read_doc_block() == gen.render_generated_block(), (
        "docs/api-reference.md route tables are stale: run "
        "python3 scripts/generate_api_reference.py"
    )


def test_generated_table_is_independent_of_local_dashboard_dist(tmp_path):
    """create_app respects dashboard_dist_dir and the generator stays deterministic
    whether or not apps/dashboard/dist is built on disk."""
    from ai_bridge.control.app import create_app

    app = create_app(
        admin_token="test",
        db_path=tmp_path / "control.db",
        dashboard_dist_dir=tmp_path / "no-such-dist",
    )
    try:
        paths = {getattr(r, "path", "") for r in app.routes}
    finally:
        app.state.conn.close()
    assert "/assets" not in paths
    assert "/{full_path:path}" not in paths

    gen = _load_generator()
    assert gen.read_doc_block() == gen.render_generated_block()


def test_every_snapshot_route_is_documented():
    snap = _load_module(
        "test_architecture_phase4_routers",
        REPO_ROOT / "tests" / "unit" / "test_architecture_phase4_routers.py",
    )

    expected = snap._expected_routes()
    documented = _doc_table_pairs()
    missing = sorted(expected - documented)
    assert not missing, f"routes missing from docs/api-reference.md: {missing}"


def test_doc_is_v03_not_v02_snapshot():
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "v0.2 snapshot" not in text
    assert "v0.3" in text
    for section in ("OIDC", "Policies", "Backups", "Sessions & desktop streaming"):
        assert section in text, f"api-reference.md omits the {section} section"

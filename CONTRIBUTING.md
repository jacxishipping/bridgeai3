# Contributing

- Python 3.11+, `pip install -e ".[dev]"`, `pytest -q` before pushing.
- New tools: add the tier to `src/ai_bridge/policy.py`, the handler to
  `src/ai_bridge/tools.py` or `src/ai_bridge/control/dispatch.py`, a
  policy test, and a docs update. No tool may widen its own policy.
- Never commit secrets, tokens, or `.env`. CI runs ruff + pytest.
- Windows/macOS changes must state whether they were executed on that
  OS (see [docs/support-matrix.md](docs/support-matrix.md) and
  [docs/implementation-status.md](docs/implementation-status.md)).
- Do not cite historical `analysis/21_*` / wave NO-GO notes as current
  status — use [analysis/PRODUCTION_READINESS.md](analysis/PRODUCTION_READINESS.md).

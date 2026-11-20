# Repository Guidelines

## Project Structure & Module Organization

- `src/ai_bridge/` contains the Python package: MCP server, policy enforcement, tools, and the FastAPI control plane (`control/`).
- `agents/linux`, `agents/windows`, and `agents/macos` provide platform polling agents. Keep host-specific behavior isolated here.
- `apps/dashboard/` is the React/Vite management UI; `apps/windows-client/` is the .NET WPF client. Native C++ Windows code is in `windows-agent/`.
- `tests/` holds Python unit, integration, security, and end-to-end tests. Database migrations live in `migrations/`; deployment assets and operational scripts are under `deployment/` and `scripts/`.
- Put client setup examples in `integrations/` and longer operational material in `docs/`.

## Build, Test, and Development Commands

Use Python 3.11 or newer. Install development dependencies with:

```bash
pip install -e ".[dev]"
ruff check .
pytest -v
```

Start the control plane with `python -m ai_bridge.control`. For the dashboard, run `npm --prefix apps/dashboard ci` once, then `npm --prefix apps/dashboard run dev`; use `npm --prefix apps/dashboard run build` or `typecheck` before submitting UI changes. Build the WPF client with `dotnet build apps/windows-client/AIBridgeClient.csproj -c Release`.

## Coding Style & Naming Conventions

Follow existing Python conventions: four-space indentation, `snake_case` functions/modules, `PascalCase` classes, and type hints where they clarify interfaces. Ruff enforces import ordering and lint rules; its line limit is 200. Run `pre-commit run --all-files` when available. Keep React components in `PascalCase.tsx`; use existing TypeScript and XAML naming patterns rather than introducing a parallel convention.

## Testing Guidelines

Name Python tests `test_*.py` and individual cases `test_*`; place them in the matching `tests/unit`, `tests/integration`, `tests/security`, or `tests/e2e` area. Add policy tests whenever a tool, capability tier, or dispatch path changes. Run targeted tests during iteration (for example, `pytest tests/security -q`) and the full suite before pushing. State explicitly when Windows or macOS work was not run on its target OS.

## Commit & Pull Request Guidelines

Recent commits use concise, imperative summaries (for example, `Fix Windows client safety and startup issues`). Keep each commit scoped and explain behavioral or security-sensitive changes in its body. Pull requests should describe the problem and validation performed, link related issues, include dashboard screenshots for visual changes, and call out migrations, configuration changes, and platform-specific verification. Never commit tokens, credentials, or `.env` files.

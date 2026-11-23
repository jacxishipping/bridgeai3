> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 2A — signed updates + child-env scrub + native installer sealing: progress record

Date: 2026-11-17. Base: git commit `726bed2` (no reset/rebase, no commit, no pip install).
Scope note: the working tree contains many other modified files from concurrent
waves; this record covers ONLY the Wave 2A files below.

## Findings (verified against code before editing)

- **SEC-07 — CONFIRMED.** `src/ai_bridge/tools.py` `_run_argv` (~1344) built the
  child env as `os.environ` minus only `_SCRUB_ENV_KEYS` (3 keys: admin token,
  device token, OIDC client secret). `DATABASE_URL`, `AI_BRIDGE_VAULT_TOKEN`,
  `AI_BRIDGE_TURN_SECRET`, webhook/OAuth material all reached `run_shell` /
  `terminal_exec` children. `src/ai_bridge/execution.py:144-160` already used a
  minimal clean env — the two runners were inconsistent, as reported.
- **R-P0-16 — CONFIRMED.** `tools.update_fleet_agent` (~1789) ran unsigned
  `git fetch origin main && git checkout -f -B main origin/main` with no pin,
  signature, or rollback, while `src/ai_bridge/updates.py` already had a signed
  Ed25519 manifest library (`generate_keypair` / `sign_manifest` /
  `verify_manifest`) unused by that path.
- **F08-08 — CONFIRMED, with a reader gap found during the work.** The installer
  wrote the device token and pipe secret in plaintext (`native-agent.json`,
  `native-pipe.secret`); the config ACL also granted Read to the installing user.
  Sealing pattern found: C++ `windows-agent/src/security/local_security.*`
  (`DpapiSecretStore::ProtectSecret/UnprotectSecret`, `CryptProtectData`, optional
  `machine_scope`) and Python `agents/windows/app_runtime.py` (`seal_token` /
  `unseal_token`, wire format `aib-dpapi-v1:<base64>`, DPAPI CurrentUser scope).
  **Reader gap (verified by grep): `DpapiSecretStore::UnprotectSecret` is never
  called anywhere under `windows-agent/src/`** — the C++ service
  (`service/windows_service.cpp:LoadServiceConfig`) and desktop agent
  (`desktop/desktop_agent.cpp:main`) read `device_token` / `pipe_secret` as
  plaintext JSON fields. So sealed values written by the installer cannot be
  consumed by the native binaries until they strip the prefix, base64-decode,
  and call the existing `UnprotectSecret()` (small follow-up, in C++ files
  outside this wave's allowed list). The Python agent (`agent_main.py`
  `_resolve_saved_config`) already unseals the `aib-dpapi-v1:` prefix via
  `unseal_token()`, which is why the installer reuses exactly that wire format.
  Sealing uses **LocalMachine** scope, not CurrentUser: the sealed value must be
  readable by both the service (SYSTEM) and the desktop agent (interactive
  user), different accounts. LocalMachine scope protects against off-machine
  theft, not against other local users — the SYSTEM+Admins-only ACL is the
  local boundary. This trade-off is stated in the installer comments.

## Files changed (Wave 2A only)

- `src/ai_bridge/updates.py` (+178) — new signed pinned-git update API (below).
- `src/ai_bridge/tools.py` (+67/−15) — `_run_argv` env scrub + `update_fleet_agent`
  signed path only; nothing else in the file touched.
- `windows-agent/installer/install-service.ps1` (+61/−8) — DPAPI sealing + ACL.
- `tests/unit/test_signed_updates.py` (new, 7 tests).

## Decisions

1. **SEC-07:** `_clean_child_env()` allowlist — `PATH, HOME, LANG, LC_ALL, TZ,
   TERM`, plus on Windows `SYSTEMROOT, WINDIR, COMSPEC, PATHEXT, TEMP, TMP,
   USERPROFILE, LOCALAPPDATA, APPDATA`. `PATH` defaults to
   `/usr/local/bin:/usr/bin:/bin` (Windows: `C:\Windows\System32;C:\Windows`) if
   unset; `LANG` defaults to `C.UTF-8`. `DATABASE_URL`, vault/TURN/webhook/OAuth,
   and all `AI_BRIDGE_*` secrets are dropped. `_SCRUB_ENV_KEYS` kept as a
   documented, now-unused reference constant. `shell=False`, timeout/kill
   behaviour in `_run_argv` unchanged. Applies to both `run_shell` and
   `terminal_exec` (both go through `_run_argv`).
2. **R-P0-16:** fail closed — `update_fleet_agent` raises `PermissionError`
   ("Refusing unsigned fleet update: a signed release pin is required…") when
   the signed config is absent; no unsigned fallback anywhere. The `_check()`
   approval gate call is unchanged. New optional kwarg `repo_dir` (default:
   current working directory) so the control plane / tests can point at the
   checkout root; existing callers (`control/dispatch.py:377` lambda,
   `control/app.py` fleet/local update endpoints) pass only
   `approval_granted`/`policy` and still work. `apply_pinned_git_update` fetches
   `origin` only if the pinned commit is not already present locally (lets a
   local temp repo work in tests; real fleets fetch). Rollback snapshot is
   written BEFORE any fetch/checkout and records both previous commit and
   previous branch; rollback restores the branch when there was one, else the
   SHA, and a failed rollback raises `RuntimeError` chaining the original error.
   Rollback file: `~/.ai-bridge/update-rollback.json`.
3. **F08-08:** installer adds `Protect-SecretDPAPI` (`.NET ProtectedData`,
   LocalMachine scope, `aib-dpapi-v1:` prefix, round-trip unprotect verification
   before writing, fails loudly — never plaintext fallback). Both the pipe-secret
   file and the config's `device_token` / `pipe_secret` now hold sealed values
   only. Config ACL is now SYSTEM (S-1-5-18) + Administrators (S-1-5-32-544)
   only; the previous installing-user Read grant is removed.
   **Known caveat (documented in the script):** the desktop-agent scheduled task
   runs as a standard user (Users / Limited) and reads this config via
   `--config`, so with the SYSTEM+Admins-only ACL it can no longer read it
   unless it runs elevated or the service hands it the secrets (e.g. over the
   authenticated pipe, F08-02). Do NOT silently re-add a broad Read grant as
   the fix; reconcile before deploying to non-admin desktops.
4. Installer verification is **inspection-only**: no Windows host here, `pwsh` /
   `powershell` are not installed (`which pwsh powershell` → nothing), so no
   syntax check could be run. Verified by reading the script and grepping that
   no plaintext `$DeviceToken` / pipe secret is written (`Set-Content` targets
   now receive sealed values only).

## Tests run (exact output)

Subset (required minimum):
```
.venv/bin/python -m pytest tests/unit/test_signed_updates.py tests/unit/test_phase4_streaming_and_updater.py -q
16 passed, 1 warning in 4.59s
```
(The 1 warning is the pre-existing FastAPI/httpx StarletteDeprecationWarning.)
New tests in `tests/unit/test_signed_updates.py` (7): valid signed manifest →
checkout of exactly the pinned SHA + rollback snapshot records previous HEAD;
tampered manifest → PermissionError, no checkout; tampered signature →
PermissionError, no checkout; missing config → PermissionError, no unsigned
fallback; unavailable pinned commit (valid 40-hex, no remote) → refused and
HEAD rolled back; short commit → ValueError (40-hex required); child env from
`_run_argv(["env"])` and `_clean_child_env()` contains only the allowlist
(no `DATABASE_URL` / vault / TURN / webhook / OAuth / `AI_BRIDGE_*`).
First run had 3 failures, all a test-side bug (fresh temp repo HEAD is the
second commit; the tests asserted the first) — fixed in the tests only, then
16 passed.

Full suite (run for completeness):
```
.venv/bin/python -m pytest tests/ -q
12 failed, 474 passed, 5 skipped, 1 warning in 90.36s
```
All 12 failures were re-run against a clean worktree of baseline commit
`726bed2` (no Wave 2A changes) and **all 12 failed there too — pre-existing**,
unrelated to this change (missing `PIL`, missing `ruff`, httpx `InvalidURL`,
firecrawl/browserbase keys, clipboard/WebRTC cases). Failing tests:
`test_windows_v1_architecture` ×3, `test_windows_v2_architecture` ×1,
`test_aci_desktop_deepening` ×1, `test_browser_intelligence` ×1,
`test_enhanced_features` ×1, `test_execution::test_subprocess_linter_runs_cross_platform`
(`ruff` not installed; uses `execution.py`, untouched here), `test_phase9_deep` ×1,
`test_phases_456_deep` ×2, `test_serverai_mcp_orchestration` ×1.
No other test files were modified.

## Rollback

- Revert the three source files (`git checkout -- src/ai_bridge/tools.py
  src/ai_bridge/updates.py windows-agent/installer/install-service.ps1`) and
  delete `tests/unit/test_signed_updates.py`. `update_fleet_agent` then returns
  to the unsigned path (insecure — do not deploy that way); the rollback
  snapshot file `~/.ai-bridge/update-rollback.json` is inert JSON and can be
  deleted. Installer-written sealed configs remain sealed until the C++ readers
  gain unseal support — re-running the pre-change installer restores plaintext
  writing (also insecure).

## Open follow-ups (not done — outside allowed files / no Windows host)

1. C++ readers must unseal: `windows_service.cpp:LoadServiceConfig` and
   `desktop_agent.cpp:main` need prefix-strip + base64-decode +
   `DpapiSecretStore::UnprotectSecret()` for `device_token` / `pipe_secret`
   (including the `native-pipe.secret` file fallback), else the sealed
   installer output breaks native auth. Do not deploy the sealed installer to
   native fleets until that lands and is tested on Windows.
2. Reconcile the SYSTEM+Admins-only config ACL with the standard-user desktop
   agent (see caveat above).
3. Installer PowerShell syntax/run check on a real Windows host (not possible
   here — no `pwsh`, no Windows).
4. Control plane wiring: `control/app.py` fleet/local update endpoints currently
   rely on the default `repo_dir` (process cwd). If the plane does not run from
   the repo root, pass `repo_dir=<repo path>` explicitly, and set the three
   `AI_BRIDGE_UPDATE_*` env vars in the deployment (no defaults exist by design).

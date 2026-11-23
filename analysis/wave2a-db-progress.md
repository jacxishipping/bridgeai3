> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 2A — Progress: database, audit chain, secrets encryption, migration 013

Owner: Wave 2A subagent (DB/audit/secrets). Repo: `/home/hatch/workspace/ai-bridge-main`,
on top of git commit `726bed2`. No commit, no reset/rebase, no `pip install`.
Sibling waves' uncommitted changes were present in the working tree; only the
files listed in §6 were touched by this wave.

All findings were verified against the code before editing (see §1).
All tests ran with the repo venv python
(`/home/hatch/workspace/ai-bridge-main/.venv/bin/python`), proxy env vars unset
(`http_proxy/https_proxy/HTTP_PROXY/HTTPS_PROXY/no_proxy/NO_PROXY` — the sandbox
proxy vars break `httpx.Client()` construction, per Wave 1C §4).

## 1. Finding verification (before any edit)

| Finding | Verified as |
|---|---|
| R-P0-12 tenant caller-supplied | CONFIRMED: `db.py` `users`/`sessions` had no `tenant` column; `tasks` already had one. `approvals` also had none (parent addendum: workflow-step approvals have `task_id` NULL, so they cannot be tenant-scoped via the tasks join). |
| F07-04/DIST-02 epoch/fencing | CONFIRMED: `tasks` had no `epoch`/version column. |
| DB-05 non-atomic audit | CONFIRMED: `db.py` `audit()` did read-tip → INSERT with `entry_hash` NULL → recompute hash with row id → UPDATE → commit. |
| SEC-06/DB-06 plaintext MCP secrets | CONFIRMED: `db.py:118-148` `mcp_servers.auth_token/oauth_client_secret/oauth_refresh_token` plaintext; `mcp_oauth.py` refresh path wrote them back plaintext; `mcp_routes.py` used `SELECT * FROM mcp_servers` in 8 places. `ai_api.py:1666` and `custom_agents.py:157` also `SELECT *` on `mcp_servers` — NOT this wave's files, left untouched (they still work: `ensure_valid_mcp_token` now decrypts the row they pass in). |
| F-05-10 secrets env fallback | CONFIRMED: `secrets_backend.py` `_vault_get`/`_aws_sm_get` silently returned `os.environ.get(name)` on missing config/fetch failure unless `AI_BRIDGE_SECRETS_REQUIRED=1`. |
| F-05-02/CSRF secret | CONFIRMED: `csrf.py` `_CSRF_SECRET = os.environ.get(...) or secrets.token_hex(32)` at import — per-process random by default, so tokens minted on one worker do not validate on another. |
| Migration format / runner | CONFIRMED: `migrations/012_full_schema_sync.sql` style (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `;`-terminated); `control/migrations.py` strips lines starting `--`, splits on `;`, globs `^\d+_*.sql`. |

## 2. Per-task record

### Task 1 — Migration 013 (+ parent addendum: `approvals.tenant`) — DONE, VERIFIED
- Files: ADDED `migrations/013_tenant_isolation.sql`, `migrations/down_013_tenant_isolation.sql`.
- `013` adds (idempotent `ADD COLUMN IF NOT EXISTS`, matching 001/012 format):
  `users.tenant TEXT NOT NULL DEFAULT 'default'`,
  `sessions.tenant TEXT NOT NULL DEFAULT 'default'`,
  `approvals.tenant TEXT NOT NULL DEFAULT 'default'`,
  `tasks.epoch INTEGER NOT NULL DEFAULT 0`.
  Single-tenant data migrates under `"default"` via the `NOT NULL DEFAULT`
  backfill — no data movement (verified: a pre-013 row reads back `tenant='default'`, `epoch=0`).
  Down migration is documented as a comment block in `013` (Postgres
  `DROP COLUMN` + pointer to the down file); the down file is named
  `down_013_tenant_isolation.sql` so the runner's `^\d+_*.sql` glob never
  picks it up.
- Down file contains the FULL explicit SQLite rebuild SQL for
  `users`/`sessions`/`approvals`/`tasks` (new table → `INSERT ... SELECT` →
  `DROP` → `RENAME`), recreates `idx_tasks_device_status` and
  `idx_approvals_task_id` (dropped with the tables), deletes the `013`
  `schema_migrations` row, and carries a backup/run-while-stopped warning.
  It was EXECUTED against a migrated temp DB and verified: columns gone,
  all rows preserved, both indexes recreated, `013` record removed.
- Rollback: run the down file manually (see its header); revert the `013` file.

### Task 2 — `db.py` SCHEMA — DONE, VERIFIED
- Same four columns added to the inline `SCHEMA` (`users`/`sessions`/
  `approvals.tenant`, `tasks.epoch`) so fresh installs match migrated DBs.
- NO new index added (deliberate): the fencing UPDATE is by primary key
  (`id`, plus `status`/`epoch` guard), so an `(id, epoch)` index is
  pointless; `idx_tasks_device_status` already covers device/status
  sweeps. A `(device_id, status, epoch)` index would only help if the
  parent's sweep filters on epoch by device — revisit if it does.
- Rollback: revert the `SCHEMA` hunks.

### Task 3 — DB-05 atomic audit + chain verifier — DONE, VERIFIED
- Files: `src/ai_bridge/control/db.py` only.
- `audit()` signature identical. It now computes `prev_hash` + `entry_hash`
  and INSERTs the complete row inside ONE `conn.transaction()` (tip read +
  insert in the same transaction; WORM/SIEM emit after commit). The hash
  payload is `prev_hash|ts|actor|device_id|tool|args_json|decision` —
  computed WITHOUT the row id, so no post-insert UPDATE exists any more.
  Helpers: `_audit_payload()`, `_audit_payload_legacy()`,
  `_audit_entry_hash()`, `_row_get()`.
- ADDED `verify_audit_chain(conn) -> {"ok": bool, "checked": int,
  "broken_at": int | None, "legacy_rows": int}`. Walks the chain in `id`
  order recomputing hashes; a row whose hash matches the legacy scheme
  (`prev|id|ts|actor|device_id|tool|args_json|decision`) is counted in
  `legacy_rows`, not reported broken; the first row with a mismatched
  `prev_hash`, missing `entry_hash`, or neither-scheme hash sets
  `broken_at` (its id) and stops the walk; `checked` counts rows examined
  up to and including the broken row (or all rows when intact).
- Rollback: revert `audit()`/`verify_audit_chain` (rows already written in
  the new scheme verify as `legacy_rows: 0`; old-scheme rows keep verifying
  as legacy under the new verifier).

### Task 4 — SEC-06/DB-06 Fernet encryption at rest — DONE, VERIFIED
- Files: `src/ai_bridge/control/mcp_oauth.py`, `src/ai_bridge/control/mcp_routes.py`.
- ADDED in `mcp_oauth.py`:
  - `get_token_cipher()` — key from `secrets_backend.get_secret("AI_BRIDGE_MCP_TOKEN_KEY")`
    (env fallback of the same name). Raises `RuntimeError` with generation
    instructions when unset or invalid. Accepts a Fernet-generated key or
    the 64-hex-char encoding of the same 32 bytes; a passphrase is NOT
    silently stretched.
  - `encrypt_token(str) -> str` — `"fernet1:"` + Fernet ciphertext; `""` → `""`
    (absent stays absent, no cipher needed); raises on missing key (fail closed).
  - `decrypt_token(str) -> str` — prefixed values decrypted (wrong key /
    corrupt → `RuntimeError`); legacy plaintext without the prefix returned
    as-is. `decrypt_server_secrets(srv) -> dict` decrypts the three secret
    columns in memory.
  - `MCP_SERVER_ALL_COLUMNS`, `MCP_SERVER_PUBLIC_COLUMNS` (explicit lists),
    `get_mcp_server(conn, server_id) -> dict | None` (explicit columns,
    secrets decrypted, read audited),
    `get_mcp_server_public(conn, server_id) -> dict | None` (non-secret
    columns + `has_auth_token` / `has_oauth_client_secret` /
    `has_oauth_refresh_token` flags),
    `list_mcp_servers_public(conn, tenant="default") -> list[dict]`.
  - `_audit_secret_read(conn, srv)` — `db.audit(conn, tenant=<server tenant>,
    actor="system", device_id=<server id>, tool="mcp_server_secret_read",
    args={"server_id": ...}, decision="allowed")`; best-effort (logged, not
    raised) so a broken audit sink cannot wedge token refresh; `db.audit`
    never reads `mcp_servers`, so no recursion. A `_mcp_secret_read_audited`
    marker on rows from `get_mcp_server()` prevents double-auditing when
    the same row is then passed to `ensure_valid_mcp_token()` (which audits
    rows that arrived via other paths, e.g. `ai_api.py`/`custom_agents.py`).
- `ensure_valid_mcp_token()` now decrypts secrets on entry for EVERY auth
  type (bearer tokens are stored encrypted too), returns decrypted values
  to callers, and persists refreshed tokens ENCRYPTED (encryption happens
  before the DB try/except so a missing key raises instead of being
  swallowed as a persist warning). The refresh UPDATE also re-encrypts a
  legacy-plaintext `oauth_client_secret` (opportunistic re-encryption).
  Decryption failure logs a warning and returns the row unchanged rather
  than sending ciphertext as a bearer token.
- `mcp_routes.py`: all 8 `SELECT * FROM mcp_servers` replaced — list/create/
  sync/update/delete re-reads use `get_mcp_server_public` /
  `list_mcp_servers_public`; start/callback/sync/execute use
  `get_mcp_server`. Create/start/callback encrypt `auth_token`,
  `oauth_refresh_token`, `oauth_client_secret` on write; PATCH only touches
  secret columns when the caller supplies a new value (stored ciphertext
  otherwise left untouched). `_serialize_mcp_server()` never emits secret
  columns and derives `oauth_connected` / `oauth_secret_configured` /
  `oauth_has_refresh_token` from the `has_*` flags on public rows.
  (The one remaining `SELECT *` in the file is on `mcp_oauth_states`, a
  table with no secrets — left as-is.)
- KNOWN REQUIRED FOLLOW-UP (by design, fail-closed): deployments and test
  fixtures must set `AI_BRIDGE_MCP_TOKEN_KEY` to a Fernet key
  (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
  Without it, creating/refreshing an MCP server with secrets now raises
  `RuntimeError` instead of storing plaintext. The two existing
  `test_mcp_oauth2.py` tests fail without the key and pass with it (see §3).
  Legacy plaintext rows keep working (read compat) and are re-encrypted on
  the next write that touches them.
- Rollback: revert both files; rows written encrypted stay encrypted until
  re-written (decrypt with the key to recover, or restore a pre-change DB
  backup). Keep `AI_BRIDGE_MCP_TOKEN_KEY` configured until rollback completes.

### Task 5 — F-05-10 secrets backend fail-closed — DONE, VERIFIED
- Files: `src/ai_bridge/control/secrets_backend.py` only.
- `_vault_get`/`_aws_sm_get`: missing config OR fetch failure now raises
  `RuntimeError` by default. Only an explicit
  `AI_BRIDGE_SECRETS_ALLOW_ENV_FALLBACK=1` permits env fallback, with a
  `logging` warning naming the secret and the failure. `AI_BRIDGE_SECRETS_REQUIRED=1`
  behaviour (raise) is unchanged and takes precedence. Default backend
  `"env"` is unchanged.
- Deliberate boundary (documented, not hidden): a Vault/AWS backend that
  answers successfully but simply does not contain the requested key still
  falls back to env — the brief's raise conditions are "config missing or
  fetch fails", and that is also the pre-existing `REQUIRED=1` behaviour for
  the not-found case. Tighten to raise-on-absent-key if the parent wants
  Vault fully authoritative.
- Checked `tests/` first: only `test_residuals_security_platform.py`
  references the backend (`test_secrets_backend_env_default`,
  `test_secrets_backend_vault_fail_closed` with `REQUIRED=1`) — both still pass.
- Rollback: revert the file (or set `AI_BRIDGE_SECRETS_ALLOW_ENV_FALLBACK=1`
  as an emergency restore of the old behaviour).

### Task 6 — CSRF shared secret — DONE, VERIFIED
- Files: `src/ai_bridge/control/csrf.py` only (app.py wiring is the parent's).
- ADDED `get_csrf_secret() -> str`: resolves from
  `secrets_backend.get_secret("AI_BRIDGE_CSRF_SECRET")` first, then env
  `AI_BRIDGE_CSRF_SECRET`, and only as a last resort generates a per-process
  random secret with a logged warning (tokens then do not validate across
  workers/restarts); the resolved value is cached in the module and never
  regenerated per call. A misconfigured vault/aws backend raises out of
  `get_secret` and is NOT swallowed (fail closed, consistent with F-05-10).
  `generate_csrf_token` / `validate_csrf_token` signatures identical, now
  calling `get_csrf_secret()` instead of the import-time
  `_CSRF_SECRET = env or random` (deleted).
- Rollback: revert the file.

## 3. Tests executed (exact commands, venv python, proxy vars unset)

Scratch verification (NOT a committed test file): `/tmp/wave2a_db_verify.py`

```
.venv/bin/python /tmp/wave2a_db_verify.py
  → 42 passed, 0 failed
  (013 on a pre-013 DB built from the 726bed2 SCHEMA: applied
  ['001'..'013'], re-run no-op, all 4 columns present, defaults
  'default'/0; fresh connect(): columns + 013 recorded, empty chain
  verifies; 40 concurrent audit writes (8 threads) verify
  {ok:True, checked:40, legacy_rows:0}, no NULL entry_hash; hand-built
  legacy row + a new row after it → {ok:True, checked:42,
  legacy_rows:1}; tampered entry_hash → {ok:False, checked:6,
  broken_at:6}; Fernet round-trip, legacy passthrough, empty→empty,
  invalid/missing key → RuntimeError; public loader has no secret
  columns + has_* flags; secret loader decrypts; secret read audited;
  stored value starts 'fernet1:'; vault/aws misconfigured → RuntimeError
  by default, env fallback only with ALLOW flag, REQUIRED=1 raises, env
  backend unchanged; CSRF secret from env, cached, token validates,
  wrong session/garbage rejected, random fallback cached 64 hex chars)
```

Down-migration execution check (inline python, temp DB):
```
migrated DB → executescript(down_013_tenant_isolation.sql)
  → users/sessions/approvals/tasks columns back to pre-013 sets; rows
  preserved (users u1, tasks t1 epoch dropped, approvals ap1);
  schema_migrations '013' removed (0); idx_tasks_device_status and
  idx_approvals_task_id recreated.
```

Existing test files:
```
.venv/bin/python -m pytest tests/unit/test_residuals_security_platform.py \
    tests/unit/test_dual_engine_migrations.py tests/test_control_plane.py -q
  → 39 passed in 22.04s
.venv/bin/python -m pytest tests/unit/test_mcp_oauth2.py tests/unit/test_p0_mcp_gate.py \
    tests/unit/test_p0_policy.py -q
  → 33 passed, 3 failed (see §4)
AI_BRIDGE_MCP_TOKEN_KEY=<generated Fernet key> .venv/bin/python -m pytest \
    tests/unit/test_mcp_oauth2.py -q
  → 2 passed in 1.98s
.venv/bin/python -m pytest tests/unit/test_p0_policy.py tests/unit/test_p0_mcp_gate.py -q
  → 34 passed in 2.41s
```

## 4. Failures found during verification

| Failure | Cause (verified) | Status |
|---|---|---|
| `test_mcp_oauth2.py` (2 tests) fail without `AI_BRIDGE_MCP_TOKEN_KEY` | INTENDED fail-closed behaviour (Task 4): `create_mcp_server` → `encrypt_token` raises `RuntimeError: AI_BRIDGE_MCP_TOKEN_KEY is not configured…`. Both tests PASS with the key set (2 passed). | By design; fixtures/deployments must set the key (see Task 4 follow-up). |
| `test_p0_policy.py::test_unrestricted_grant_is_audited` fails ONLY when run in the same process after `test_mcp_oauth2.py` | NOT caused by this wave: a sibling wave's uncommitted change to `control/app.py:670-673` does `os.environ.setdefault("AI_BRIDGE_MCP_AUDIT_DB", str(db_path))` inside `create_app` (absent at base commit `726bed2` — verified via `git show`). The leaked process env var makes `audit_writer` prefer the DB sink over the test's `AI_BRIDGE_MCP_AUDIT_FILE` JSONL, so the JSONL file is never created → `FileNotFoundError`. Passes alone (1 passed) and with `test_p0_mcp_gate` (34 passed). My files never set that env var and never touch `audit_writer`. | Pre-existing in the working tree (sibling change); flag to the parent — `create_app` should not `setdefault` process env, or the test needs the env var cleared. |

## 5. Remaining / blocked / deferred (for the parent or owning wave)

- **Parent — set `AI_BRIDGE_MCP_TOKEN_KEY`**: deployments, and the
  `test_mcp_oauth2.py` fixtures (or a `conftest`/`monkeypatch` key), need a
  Fernet key; without it MCP server creation with secrets raises (intended).
- **Parent (`app.py`, `task_service.py`) — call these new APIs**:
  `db.verify_audit_chain(conn)`; `mcp_oauth.get_mcp_server_public(conn, id)` /
  `get_mcp_server(conn, id)` / `list_mcp_servers_public(conn, tenant)`;
  `mcp_oauth.encrypt_token` / `decrypt_token` / `get_token_cipher`;
  `csrf.get_csrf_secret()`; write the workflow tenant into
  `approvals.tenant` when creating step approvals (parent addendum rationale).
  Tenant enforcement on list/read/write endpoints (R-P0-12 remainder) and
  epoch fencing on result UPDATEs (DIST-05/F07-04 remainder) are the parent's.
- **Not in this wave (by design)**: `ai_api.py`/`custom_agents.py`
  `SELECT * FROM mcp_servers` (still correct via `ensure_valid_mcp_token`
  decryption, but should move to the explicit loaders); `app.py` CSRF
  wiring / tenant enforcement / v1 approval gate; Postgres live verification
  (all DB checks here are SQLite — the migration SQL uses
  `ADD COLUMN IF NOT EXISTS`, which Postgres also supports, but no Postgres
  instance was available in this environment).
- **Deliberate decisions to preserve**: (a) vault/aws "key absent after
  successful fetch" still falls back to env (see Task 5 boundary); (b) no
  new tasks index (see Task 2); (c) secret-read audit is best-effort, never
  raises into token use; (d) `encrypt_token("") == ""` so secret-less
  servers work with no key configured.

## 6. Files changed (this wave only)

| File | Change |
|---|---|
| `migrations/013_tenant_isolation.sql` (new) | 4 columns (users/sessions/approvals.tenant, tasks.epoch); down-migration comment block. |
| `migrations/down_013_tenant_isolation.sql` (new) | Full SQLite table-rebuild down migration + index recreation + migration-record delete. |
| `src/ai_bridge/control/db.py` | SCHEMA: 4 columns; atomic `audit()` (one transaction, id-free hash); `verify_audit_chain()` + `_audit_payload[_legacy]` helpers. |
| `src/ai_bridge/control/mcp_oauth.py` | `get_token_cipher` / `encrypt_token` / `decrypt_token` / `decrypt_server_secrets`; explicit column lists; `get_mcp_server` / `get_mcp_server_public` / `list_mcp_servers_public`; secret-read audit; `ensure_valid_mcp_token` decrypt-on-entry + encrypted persist + client-secret re-encryption. |
| `src/ai_bridge/control/mcp_routes.py` | No `SELECT *` on `mcp_servers`; public loader for list/API paths; encryption on all secret write paths; PATCH touches secrets only when supplied. |
| `src/ai_bridge/control/secrets_backend.py` | Vault/AWS fail closed by default; env fallback only with `AI_BRIDGE_SECRETS_ALLOW_ENV_FALLBACK=1` + warning; `REQUIRED=1` unchanged. |
| `src/ai_bridge/control/csrf.py` | `get_csrf_secret()` (secrets backend → env → random-with-warning, cached); token functions use it. |

## 7. Status summary

| Finding | Status |
|---|---|
| R-P0-12 migration 013 (+ approvals.tenant addendum) | IMPLEMENTED, TESTED, VERIFIED (up + down executed) |
| DB SCHEMA sync (incl. approvals.tenant) | IMPLEMENTED, TESTED, VERIFIED |
| DB-05 atomic audit + verifier | IMPLEMENTED, TESTED, VERIFIED |
| SEC-06/DB-06 Fernet at rest + no SELECT * + secret-read audit | IMPLEMENTED, TESTED, VERIFIED (key provisioning is a parent follow-up) |
| F-05-10 fail-closed secrets backend | IMPLEMENTED, TESTED, VERIFIED |
| F-05-02/CSRF shared secret | IMPLEMENTED, TESTED, VERIFIED |

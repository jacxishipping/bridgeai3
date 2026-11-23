> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 1A — Progress Report: Phase 0 Deployment / CI / Installer

**Scope:** roadmap R-P0-14, R-P0-15, P-03, P-04, P-05, P-09, R-P1-53, P-06.
**Status: all 8 tasks IMPLEMENTED and TESTED** (see verification limits below).
**Files changed (only the Wave 1A allow-list, plus the duplicate installer copy):**
`docker-compose.yml`, `docker-compose.dev.yml`, `docker-compose.prod.yml`,
`render.yaml`, `deployment/docker/nginx.conf`,
`deployment/aws/cloudformation.yml`,
`deployment/linux/deploy-production.sh`, `deployment/linux/install.sh`
(untouched — listed for reference), `magic-install.sh`,
`deployment/linux/magic-install.sh` (duplicate copy, see R-P0-15/P-09 notes),
`magic-install.ps1` (verified clean, no change), `.github/workflows/ci.yml`,
`.env.example`. **Added:** `tests/unit/test_deployment_config.py` (18 tests).

No Python/TS/C++ source modified. No commit made (parent commits per wave).
No packages installed. No Docker daemon, no nginx binary, no deployment run.

## Per-task results

### 1. R-P0-14 — Tier 3 ON by default → default 0 (VERIFIED)
- `docker-compose.prod.yml`: `AI_BRIDGE_ALLOW_TIER3=${AI_BRIDGE_ALLOW_TIER3:-1}` →
  `${AI_BRIDGE_ALLOW_TIER3:-0}`, with an explicit-opt-in comment.
- `deployment/aws/cloudformation.yml` UserData: `AI_BRIDGE_ALLOW_TIER3=1` → `=0`.
- `render.yaml`: added explicit `AI_BRIDGE_ALLOW_TIER3: "0"` (was unset).
- `deployment/linux/deploy-production.sh`: generated `.env.production` now
  writes `AI_BRIDGE_ALLOW_TIER3=0` explicitly.
- `.env.example`: documents `AI_BRIDGE_ALLOW_TIER3=0` (default off).
- **Rollback:** set `AI_BRIDGE_ALLOW_TIER3=1` explicitly per deployment.

### 2. R-P0-15 — Remove default secrets; fail fast (VERIFIED, with one correction to the audit)
- **Audit correction:** the audit claimed `POSTGRES_PASSWORD` defaults to
  `aibridge-dev-password` in prod compose. Verified against the actual
  files: both `docker-compose.prod.yml` and `docker-compose.yml` already
  used `${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}` (fail-fast).
  The remaining live defaults were the TURN secret and the dev admin token.
- `docker-compose.prod.yml`: coturn `--static-auth-secret` was
  `${AI_BRIDGE_TURN_SECRET:-aibridge_turn_shared_secret_2026}` → now
  `${AI_BRIDGE_TURN_SECRET:?Set AI_BRIDGE_TURN_SECRET in .env.production}`;
  `DATABASE_URL` now also uses `${POSTGRES_PASSWORD:?...}` explicitly.
- `docker-compose.yml`: coturn literal `--static-auth-secret=aibridge_turn_shared_secret_2026`
  → `${AI_BRIDGE_TURN_SECRET:?Set AI_BRIDGE_TURN_SECRET in .env}`.
- `docker-compose.dev.yml`: `AI_BRIDGE_ADMIN_TOKEN=${...:-dev-admin-token}` →
  `${AI_BRIDGE_ADMIN_TOKEN:?Set AI_BRIDGE_ADMIN_TOKEN in the environment}`
  (dev-only, loopback-only, but a known default token; `scripts/dev.sh`
  already refuses to start without it).
- `deployment/linux/deploy-production.sh`: now generates `AI_BRIDGE_TURN_SECRET`
  (`openssl rand -hex 32`) into `.env.production` (mode 0600, already
  present) — previously the one secret compose now requires that the
  generator did not create. Also fixed the health probe `curl -sk` →
  `curl -s` (plain-HTTP loopback probe; `-k` was meaningless, and the
  probe already accepts the 301 nginx now returns).
- `deployment/aws/cloudformation.yml`: `TurnSecret` parameter defaults to
  `""`; added a UserData fallback after the env-file heredoc that fills an
  empty `AI_BRIDGE_TURN_SECRET=` with `openssl rand -hex 32`, so CFN
  deploys keep working under the new fail-fast compose instead of failing
  on the empty parameter.
- **Rollback:** set the secrets explicitly via env / `.env.production`.

### 3. P-03 — nginx HTTP→HTTPS redirect (VERIFIED by config inspection + tests)
- `deployment/docker/nginx.conf` `:80` server: the `location /` proxy block
  is gone; only `/.well-known/acme-challenge/` is served on :80, everything
  else `return 301 https://$host$request_uri;`.
- **Rollback:** revert nginx.conf.

### 4. P-04 — /metrics protected (VERIFIED by config inspection + tests)
- `location /metrics` now has `auth_request /internal/verify;` (admin token
  or valid OAuth access token as Bearer) plus `limit_req zone=api_limit
  burst=20 nodelay;`. Unauthenticated requests get 401 from the gateway.
  Scrapers must now send an `Authorization` header.
- **Rollback:** revert nginx.conf.

### 5. P-05 — Ship the OAuth auth_request wiring (VERIFIED by config inspection + tests)
- Added `location = /internal/verify { internal; proxy_pass
  http://oauth_gateway/internal/verify; proxy_pass_request_body off;
  proxy_set_header Content-Length ""; proxy_set_header Authorization
  $http_authorization; }` to the shipped nginx.conf, and `auth_request
  /internal/verify;` on `location = /mcp` (existing `Host 127.0.0.1:8001`
  kept — the MCP DNS-rebinding guard requires a local Host value).
- Design note, verified in `deployment/oauth/gateway.py:968-997`:
  `/internal/verify` returns `{"ok": true, "device_id": ...}` as JSON with
  no response headers, so nginx uses only the 2xx/401 status as the gate;
  device binding is still enforced by the MCP server's own direct verify
  call, which reads the body. The README sketch and shipped config now
  agree; no sketch was deleted.
- **Rollback:** revert nginx.conf.

### 6. P-09 — Remove `curl -k` from installers (VERIFIED)
- `magic-install.sh` **and** `deployment/linux/magic-install.sh`: the
  enrollment call was `curl -s -k ...`; now `curl -s --fail ...` with no
  `-k`/`--insecure` anywhere. Added `AI_BRIDGE_CA_BUNDLE` support
  (`--cacert "$AI_BRIDGE_CA_BUNDLE"`) so a self-signed/private-CA control
  plane is usable by supplying its CA explicitly instead of bypassing
  verification. On a bad cert, curl now fails, the response is empty, and
  enrollment logs an error and proceeds unconfigured (pre-existing
  fail-loud path, now actually reachable).
- The two copies differ only in a comment (Python 3.12+ vs 3.11+) and a
  trailing blank line; the same fix was applied to both because
  `deployment/linux/install.sh` executes the `deployment/linux/` copy —
  fixing only the root copy would have left the executed path vulnerable.
- `magic-install.ps1`: verified **no** cert bypass exists
  (`SkipCertificateCheck` / `ServerCertificateValidationCallback` absent;
  `Invoke-RestMethod` uses system validation). No change made.
- `deployment/linux/deploy-production.sh` health probe: see task 2
  (`curl -sk` → `curl -s`).
- **Rollback:** revert the installer scripts.

### 7. R-P1-53 — CI actually runs the suite with published logs (VERIFIED by YAML parse; CI itself not run here)
- `.github/workflows/ci.yml`, `test` job: `pytest -v
  --junitxml=.run/pytest-junit.xml 2>&1 | tee .run/pytest.log`
  (GitHub bash runs with pipefail, so a pytest failure still fails the
  step), plus `upload-artifact@v4` (`pytest-results`: log + JUnit,
  30-day retention) on `if: always()`.
- New `deployment-config` job: runs `tests/unit/test_deployment_config.py`
  with plain `python3`, no package install, and uploads its log
  (30-day retention).
- `dashboard` job: `npm run build` retained before tests (it runs
  `tsc -b && vite build`, i.e. it is the typecheck); `npm test` output is
  now captured to `.run/dashboard-test.log` and the artifact upload
  changed from `if: failure()` to `if: always()` (30-day retention).
- **Not claimed:** CI passing. No GitHub run was triggered from here.
- **Rollback:** revert ci.yml.

### 8. P-06 — Complete .env.example (VERIFIED)
- Rewritten from the union of variables consumed by compose + Render +
  CFN: added `DATABASE_URL`, `REDIS_URL`, `REDIS_PASSWORD`,
  `POSTGRES_PASSWORD`, `AI_BRIDGE_PUBLIC_URL`, `AI_BRIDGE_PUBLIC_BASE_URL`,
  `AI_BRIDGE_DOMAIN`, `AI_BRIDGE_TURN_SECRET`, `AI_BRIDGE_TURN_HOST/URL/
  USERNAME/CREDENTIAL`, `SECRET_KEY`, `AI_BRIDGE_CORS_ORIGINS`,
  `AI_BRIDGE_EMBED_MCP`, `AI_BRIDGE_MCP_HOST`, `AI_BRIDGE_OAUTH_HOST`,
  plus webhook vars. All secrets empty; only non-secret operational
  defaults present (port, db name/user, TTL, Tier3=0). Header states no
  secret has a default anywhere.
- **Rollback:** revert .env.example.

## Tests run (exact output)

New file `tests/unit/test_deployment_config.py` — 18 tests, stdlib only
(PyYAML used when present, regex fallback otherwise):

```
$ python3 tests/unit/test_deployment_config.py
Ran 18 tests in 0.067s
OK

$ .venv/bin/python -m pytest tests/unit/test_deployment_config.py -q
..................  18 passed in 0.05s

$ python3 - <<'EOF'   # yaml module blocked -> regex fallback path
Ran 18 tests in 0.005s
OK
FALLBACK-PATH OK
```

First run failed 3/18 and the failures were fixed in the test code, not
hidden: (1) a Tier-3 substring check matched my own opt-in comment —
fixed by stripping comment lines in the test and rewording the comment;
(2) the TURN check looked in `environment:` but the secret lives in the
coturn `command:` list — fixed to check raw text; (3) `^VAR=` regex
needed `(?m)`. All three now pass on all three runners.

**Venv note:** per instructions, the venv was not installed by this wave.
At first check `.venv/bin/python -c "import fastapi"` failed; by test time
the parent's venv install had progressed and `import fastapi, pytest`
succeeded, so the venv-pytest run above is genuine.

## Other verification performed

- `python3 -c "yaml.safe_load(...)"` — OK for `docker-compose.yml`,
  `docker-compose.dev.yml`, `docker-compose.prod.yml`, `render.yaml`,
  `.github/workflows/ci.yml`.
- `bash -n` — OK for `magic-install.sh`,
  `deployment/linux/magic-install.sh`,
  `deployment/linux/deploy-production.sh`, `deployment/linux/install.sh`.
- Final grep sweep over all edited files: zero matches for
  `aibridge-dev-password`, `change-me-in-production`,
  `aibridge_turn_shared_secret_2026`, `dev-admin-token`; zero `curl -k` /
  `--insecure` / `curl -sk` outside my own explanatory comment
  ("no -k / --insecure anywhere in this installer").
- `AI_BRIDGE_ALLOW_TIER3` now `:-0` / `=0` / `"0"` in prod compose, CFN,
  render.yaml, `.env.example`, deploy-production.sh.
- No `.env.production` was created locally (deploy script not run).

## Verification limits (NOT verified — needs another environment)

- **No Docker daemon here:** `docker compose config` / `up` not run, so
  the fail-fast `${VAR:?}` behaviour and the full prod stack boot are
  unproven until run on a Docker host.
- **No nginx binary here:** `nginx -t` not run; nginx.conf changes are
  verified by inspection + the structural tests only.
- **No live deploy / enrollment:** the installer `--fail`/CA-bundle path
  and the nginx `auth_request` round-trip (401 without token, 200 with)
  need a live control plane + gateway to prove end-to-end.
- **CI not executed:** the rewritten workflow is YAML-valid and the jobs
  reference real files, but no GitHub Actions run was triggered.

## Observations for the parent (out of Wave 1A scope, not changed)

1. **CFN UserData anomaly (needs verification by another wave):**
   `deployment/aws/cloudformation.yml` writes `.env.production` via a
   *quoted* heredoc (`<< 'ENVEOF'`), so the `$(aws ssm get-parameter …)`
   lines for `POSTGRES_PASSWORD` / `REDIS_PASSWORD` / `SECRET_KEY` are
   written literally into the file. `docker compose --env-file` does not
   evaluate command substitution, so a CFN-deployed stack may receive
   those literal strings as credentials. This was not touched (out of
   scope for R-P0-14/15) but should be checked before any CFN deploy.
2. **P-11 still open:** `deploy-production.sh` still prints the admin
   token in its success summary (audit finding P-11, not in Wave 1A).
3. `deployment/linux/run-agent.sh` (`AI_BRIDGE_INSECURE_TLS=1` → `-k`)
   was not in the Wave 1A file list and was not changed; it is an
   explicit opt-in escape hatch, but it exists.
4. The audit's P-02 claim about a `POSTGRES_PASSWORD` default was stale
   for the compose files as found (already fail-fast); the live defaults
   were TURN + dev admin token, both now removed.

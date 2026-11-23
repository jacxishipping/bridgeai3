> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 6A working notes (coordinator scratch — not a deliverable)

## F-05-02 — CLOSED (code + configs + live proof)
- Local commit 8e4cff9; GitHub main 058b0900 (pushed via snapshot route, 9 files;
  blob SHAs verified via API: csrf.py e5a616a8, app.py a508e969).
- Fix: csrf.resolve_csrf_secret() (shared secret via backend/env; raises when
  workers>1 and unset); create_app() fails fast at boot (CSRF enabled);
  render.yaml generateValue; compose requires it; Dockerfile.prod
  AI_BRIDGE_WORKERS=4 (= gunicorn -w 4); deploy-production.sh generates it;
  .env.example documents it.
- Tests: tests/unit/test_csrf.py (8) + 4 deployment-config tests; 44 passed
  with test_p0_control_plane.py; ruff clean.
- LIVE (staging, 4 workers, current deploy dep-dbu6tj8jo6nc738cbl8g on 7726e58e,
  shared secret set in 5E): /tmp/csrf_crossworker_test.py -> 20/20 POSTs 200,
  0 x 403; no-CSRF control 403 with CSRF detail. VERDICT: CROSS-WORKER CSRF PASS.
- Leftover: staging user csrf-test-6a-35c813 (operator, random pw) — no DELETE
  endpoint; remove at 6A close-out via a staging-DB cleanup job. Staging
  already had 31 users before this test.
- CI run on 058b0900: check at 6A boundary.

## Next: Gate 9 (single audit writer)

## Gate 9 — CLOSED (code + tests)
- Local commit 9f4ddfe (not yet pushed; push batched at end of 6A code work).
- write_audit_event() now takes conn/tenant/decision/result; _write_db_conn()
  writes on the caller's connection; _write_db reuses it (tenant from event).
- Routed: mcp_oauth._audit_secret_read, task_service else-branch, app.py audit
  closure (covers ai_api, custom_agents, v1_routes, workspace, dispatch).
  Only remaining db.audit call is the writer's own sink (audit_writer.py:135).
- Deleted src/ai_bridge/audit.py (zero importers, verified).
- Tests: tests/unit/test_audit_writer_routing.py (6) — all pass; ruff clean.
- CI run 44120566461 on 058b0900 (F-05-02 only) still in progress at Gate 9 time.

## Next: Gate 20 (API reference rewrite)

## Gate 20 — CLOSED (rewrite + generator + tests)
- Local commit (pending): docs/api-reference.md rewritten from the live
  v0.3 app; scripts/generate_api_reference.py renders the route tables
  (227 (method, path) pairs, 16 areas, Auth derived from declared
  headers); tests/unit/test_api_reference.py (3) — drift check + snapshot
  coverage + v0.3 status. All pass; ruff clean.
- Key correction while writing it: create_app() exposes 73 direct routes
  + 9 _IncludedRouter entries (FastAPI >= 0.163 lazy include) = 227 pairs
  once expanded; the "142 routes" figure in the old doc counted source
  decorators. The generator expands _IncludedRouter via original_router,
  same as the route-snapshot test.
- Not yet pushed (batched with the rest of 6A).

## Next: Gate 16 (CI Postgres boot + schema parity)

## Gate 16 — DONE in code (CI verification pending the push)
- Local commit (pending): scripts/check_schema_parity.py gains --postgres
  mode (structural cross-dialect comparison: 24 tables, uniques, explicit
  indexes, FKs, CHECKs with IN<->ANY normalization); scripts/test_postgres_boot.py
  (boots the plane on PG, exercises health/enroll/tasks/login/CSRF/audit);
  .github/workflows/ci.yml gains the postgres-boot job (postgres:18 service);
  tests/unit/test_schema_parity.py (6) — all pass locally; ruff clean.
- No local Postgres exists in this VM, so the PG path can ONLY be verified
  by the CI run on the pushed commit. PG-vs-SQLite substance pre-checked
  statically: the 5 CHECK exprs and 4 FKs (all ON DELETE CASCADE) match
  across the migration files' two dialect blocks.
- Not yet pushed (batched with the rest of 6A).

## Next: Gate 17 (staging migration drill) + residuals + OIDC

## PUSH BLOCKED — GitHub App cannot create git trees (17 Nov 2026 ~16:11 UTC)
- push_files (snapshot route) 403s TWICE on POST /git/trees:
  "Resource not accessible by integration" (same as the 5D saga).
  Retried once after new evidence (see below) — same 403. No more retries.
- NARROWED DIAGNOSIS: the App CAN write via the Contents API —
  delete_file for src/ai_bridge/audit.py SUCCEEDED (commit 4144a30f,
  parent 058b0900, "Wave 6A: Gate 9 — remove dead audit.py"). So the
  outage is specific to the git-data trees endpoint, not a general
  write loss. Reads still work.
- Per-file create_or_update_file was NOT used for the 13 files: they are
  interdependent across the 4 commits (e.g. app.py Gate-9 version calls
  writer kwargs the old audit_writer.py rejects), so single-file commits
  would land broken intermediate states on main + trigger 13 CI runs.
  The atomic unit is the snapshot (or a single push by James).
- GitHub main is now 4144a30f (058b0900 + audit.py deletion). Local HEAD
  698a24a. Sync record gained an entry for the delete.
- FALLBACK ARTIFACTS (verified, ready for the parent/James):
  * Zip: ~/workspace/your_files/ai-bridge-main.zip — git archive of HEAD
    698a24a, 651 entries, testzip clean, audit.py absent, all new files
    present. (Overwrites the 5D zip of 551c150, already pushed by James.)
  * Patch: ~/workspace/tmp/wave6a-6a.patch — 2,064 lines, the 13 files
    only (audit.py deletion excluded: it is already on main). Verified:
    applies cleanly onto 8e4cff9 in a temp worktree and reproduces HEAD
    byte-for-byte on all 13 files.
  * James's push path: extract the zip over his clone (or `git apply`
    the patch), `git add -A`, commit, push. Then CI runs on the new HEAD
    (now 6 jobs incl. postgres-boot) and the Gate 17 drill can proceed.
- CI run 44120566461 on 058b0900 (F-05-02): completed SUCCESS, all 5
  jobs green — second fully-green CI run in repo history.

## BLOCKED on the push: Gate 17 staging drill, CI verification of Gate 16,
## staging redeploy + live re-verification, register/readiness updates.
## NOT YET STARTED: residuals (F07-05, query-param tokens, F-05-12,
## F-05-06, W5E-01), OIDC live proof, final full-suite/dashboard/
## Playwright runs, 6B. Awaiting the parent's call on the push.

---

## RESUME SESSION (17 Nov 2026 ~16:40 UTC onwards — James pushed the zip)

- GitHub main HEAD now 973273a7d889f74f5603e9cf6dcf710355199d90 (James's push,
  "Wave 5E/5F/6: CSRF shared-secret guard, audit routing, schema parity,
  API reference, and readiness updates"). Parent verified all 13 6A files
  match local HEAD 698a24a byte-for-byte; audit.py 404 (absent).
- CI run 44127496573 on 973273a7 (first 6-job run): completed FAILURE.
  test/dashboard/Deployment-config-checks/windows-native-webrtc = SUCCESS;
  **postgres-boot = FAILURE** (step "Postgres schema parity + boot test",
  job 134195161654); **macos-agent = CANCELLED** (ran 16:35:28-16:50:29,
  NO steps in API — not a supersede, no newer run exists; cause
  undeterminable from the API). The "fast step" timestamps (e.g. test suite
  in 5 s) are an API artifact — the same 4-5 s timestamps appear on the
  known-good green run 44109295864, so they are not evidence of
  short-circuiting. The test job's run command is the full
  `pytest -v --junitxml=.run/pytest-junit.xml` (ci.yml:21).
- CI watcher subagent f81b1f1e completed 16:51:53 UTC with the above report.

## Gate 17 — DRILL PASSED + BOOT-HOOK PROOF PASSED (live, staging)
- Cron jobs (all Starter, Docker, schedule "0 0 1 1 *" so they never fire
  on their own, autoDeploy no; env DATABASE_URL + AI_BRIDGE_MCP_TOKEN_KEY
  from the gate17 key file ~/workspace/tmp/gate17-key, 0600 — never print):
  * ai-bridge-migrate-drill crn-dbu87jff3r2c73db2af0 — --mode drill —
    PASSED: row drill-4a847910a5ea, inserted 3, migrate_total 3 (1 per
    column), verify all_fernet1_prefix true, all decrypt round-trips
    true, idempotent second run 0, deleted true.
  * ai-bridge-migrate-insert crn-dbu8a7h5efls739imcvg — --mode insert —
    row drill-4ad5134098cd (3 plaintext cols), then row drill-e74c8f46e38b.
  * ai-bridge-migrate-verify crn-dbu8c56i0phs7392gm1g — --mode verify
    --row-id drill-4ad5134098cd — FIRST RUN: ok FALSE (all_fernet1_prefix
    false, all_decrypt_ok true-vacuous since decrypt_token returns
    plaintext as-is, deleted true).
  * ai-bridge-migrate-verify2 crn-dbu8gf59fdbs73e0oplg — --mode verify
    --row-id drill-e74c8f46e38b — PASSED: ok true, all_fernet1_prefix
    true, all_decrypt_ok true, deleted true.
- FIRST VERIFY FAILURE ROOT CAUSE (confirmed in code AND plane logs):
  the plane had NO AI_BRIDGE_MCP_TOKEN_KEY set, so every worker's boot
  hook logged "migrate_plaintext_secrets: cannot encrypt <col> for
  server drill-4ad5134098cd: AI_BRIDGE_MCP_TOKEN_KEY is not configured"
  (12 warnings, 3 cols x 4 workers, 16:49:32-33) and left the row
  plaintext; the hook is best-effort by design (app.py:908, never fails
  boot). FIX: set AI_BRIDGE_MCP_TOKEN_KEY on the plane via
  update_environment_variables (merge mode) to the gate17 key value
  (now the staging plane's token key); redeploy dep-dbu8ee8m7kps73ba7jmg
  live 16:56+; plane log 16:56:42 "migrated 3 plaintext MCP OAuth
  secret(s) to Fernet". Then insert row drill-e74c8f46e38b (16:55) BEFORE
  the redeploy, verify2 PASSED 17:02:51. **Gate 17 remainder CLOSED live.**
- DELETE at close-out: the 4 migrate cron jobs above + the backup-drill
  cron crn-dbu11u4ipgvc73addrl0. (MCP cannot delete services; use REST
  DELETE or note for the parent.)

## Residuals — FIXED in code, committed locally 9e39c50
- "Wave 6A: residuals — query-param tokens, F-05-12, F-05-06, F07-05,
  W5E-01" (10 files, +351/-48). live.jpg + terminal stream: query
  token/session_token removed (headers only). F-05-12: dispatch.py
  desktop_anticipate_action now passes the caller's approval_granted
  (was hardcoded True). F-05-06: nginx location / (control plane) clears
  all four fingerprint headers; device_mtls.py docstring states the trust
  boundary (bundled nginx clears them; mTLS needs ssl_verify_client +
  header from $ssl_client_fingerprint); _require_agent_mtls already at
  six sites in routers/agent.py (5F "single call site" note is stale).
  F07-05: poller.py _save_token plaintext default now prints a loud
  [SECURITY] warning on every write + honours
  AI_BRIDGE_ALLOW_PLAINTEXT_TOKEN_FILE=0 to refuse plaintext persistence
  (memory-only); DPAPI-saver-failure path already failed loudly.
  W5E-01: GET /api/devices batched _get_devices_live_meta (ONE
  window-function query for latest get_resource_usage/get_network_info
  across all ids); _get_device_live_meta is now a thin wrapper.
- Tests: tests/unit/test_residuals_wave6a.py (8) + new TestNginx test in
  test_deployment_config.py — 31 passed (8 new + 23 config); webrtc/
  device/agent-policy suites 63 passed; ruff check clean on all changed
  files. LESSON: do NOT run `ruff format` repo-wide (CI runs only
  `ruff check.`); format-only hunks were reverted before committing.

## OIDC test IdP — PREPARED, not deployed (blocked on the next push)
- Local commit 6bf819a "Wave 6A: test-only Dex OIDC provider for the
  staging cross-worker proof" (4 files): scripts/test-idp/dex.yaml
  (placeholders only — NO secret committed; filled from env at start),
  scripts/test-idp/Dockerfile (FROM ghcr.io/dexidp/dex:v2.46.1),
  scripts/test-idp/entrypoint.sh, scripts/test-idp/README.md.
  Credentials ONLY in ~/workspace/tmp/test-idp-credentials.json (0600 —
  never print). After James pushes the next zip: create Render web service
  ai-bridge-test-idp (free, Docker, dockerfilePath scripts/test-idp/
  Dockerfile), set its 2 env vars, set the plane's 5 OIDC env vars
  (merge mode -> plane redeploy), run the scripted login->callback->
  exchange flow 3x.

## Postgres-boot failure — LOCAL REPRODUCTION IN PROGRESS
- Local apt has NO sources configured (empty sources.list; first
  apt-get update fetched nothing usable). PGDG repo added
  (/etc/apt/sources.list.d/pgdg.list, key ACCC4CF8): first update failed
  with GPG NO_PUBKEY 7FCC7D46ACCC4CF8 — the .asc was saved directly as
  .gpg without dearmoring; fix applied (gpg --dearmor) and update
  re-run (background proc_06d8a79f03d0, pending). Next: install
  postgresql-18, run check_schema_parity.py --postgres + test_postgres_boot.py
  against a local PG 18 with DSN postgresql://ai_bridge:ai_bridge_pw@localhost:5432/postgres
  (same as CI). If local repro fails, ask the parent for the CI job logs
  via James's GitHub sign-in — do not guess the root cause.
- Local HEAD is now 6bf819a (698a24a -> 9e39c50 residuals -> 6bf819a IdP).
  GitHub main is 973273a7 (6A code through 698a24a only).
- Staging plane LIVE on 973273a7, deploy dep-dbu8ee8m7kps73ba7jmg, 4x
  Starter, /health 200. (CSRF fix deployed; plane now also has
  AI_BRIDGE_MCP_TOKEN_KEY set from the gate17 key.)

## Postgres-boot failure — ROOT CAUSE FOUND + FIXED (local PG 18.7)
- Local PG 18 installed via PGDG (apt had no sources; keyring had to be
  dearmored AND chmod 644 — apt's sandbox user could not read the 0660
  keyring, which is why gpgv said "Good signature" manually but apt
  still failed). Cluster 18/main online, user ai_bridge (superuser).
- REPRODUCED: check_schema_parity.py --postgres exited 1 with drift in
  tables/indexes/checks (log ~/workspace/tmp/parity-pg-output.txt).
  Root causes (schemas genuinely agree; the comparison was too strict):
  * tables/PK: SQLite reports NOT NULL=0 for PK cols, PG reports 1.
  * tables/order: PG appends tenant via ALTER TABLE (approvals, sessions,
    users), SQLite has it inline.
  * tables/default: strftime('%s','now') (SQLite) vs EXTRACT(epoch FROM
    now()) (PG) — same value, dialect-specific spelling.
  * indexes/DESC: pg_get_indexdef renders "started_at DESC" (6 indexes);
    SQLite index_info never reports direction.
  * indexes/MISSING: idx_approvals_task_id exists on SQLite but NOT on
    PG — REAL drift: 015's PG block only adds the FK constraint and PG
    does not auto-index FK columns; no migration creates it on PG.
  * checks/parens: pg_get_constraintdef wraps every AND/OR operand
    ((TIER IS NULL)OR((TIER >= 1)AND(TIER <= 4))) vs SQLite's spelling.
    The old test used a GUESSED rendering and passed locally while CI
    failed — the new test pins the real rendering.
- SECOND BUG (same CI step): test_postgres_boot.py printed POSTGRES BOOT
  OK then exited 1 at cleanup — DROP DATABASE refused (ObjectInUse:
  the TestClient leaves the app's connection open).
- FIX, committed locally 2b57770 (5 files):
  * migrations/016_approvals_task_index.sql (+ down_016): CREATE INDEX
    IF NOT EXISTS idx_approvals_task_id (dialect-neutral; no-op where it
    exists; staging picks it up on the next plane boot).
  * check_schema_parity.py: PK implies NOT NULL (both sides); column
    order-insensitive comparison; epoch-now canonicalization; strip
    ASC/DESC from PG index cols; iterative paren strip (single
    comparisons + AND-chains; never OR-chains or IN-lists; space-padded
    to avoid word fusion).
  * test_postgres_boot.py: migration count 15 -> 16; close the app's
    connection + pg_terminate_backend stray sessions before DROP DATABASE.
  * tests/unit/test_schema_parity.py: 7 new tests (12 total).
- VERIFIED locally on PG 18.7, in CI order: PG PARITY OK (24 tables,
  23 explicit indexes, 4 FKs, 5 CHECKs, 16 migrations) + POSTGRES BOOT
  OK, both exit 0; scratch DBs dropped.
- CI run 44127496573's macos-agent cancellation (no steps, not a
  supersede) is UNRESOLVED from the API — the next CI run (after the
  new zip is pushed) will show whether it was transient.
- Local HEAD is now 2b57770 (6bf819a -> 2b57770 Gate 16 fix). GitHub main
  is still 973273a7 — residuals (9e39c50), IdP (6bf819a) and the Gate 16
  fix (2b57770) all need the next zip + patch for James.
- **Gate 17 cron jobs DELETED (17 Nov 2026 ~17:20 UTC, REST DELETE 204
  each):** ai-bridge-migrate-drill, -insert, -verify, -verify2. The
  backup-drill cron crn-dbu11u4ipgvc73addrl0 REMAINS (daily 08:41 UTC)
  for phase close-out.
- **Full suite (6A HEAD): Python 823 passed / 11 failed / 5 skipped**
  (log ~/workspace/tmp/pytest-6a.log, 782 s; the 11 are exactly the
  known baseline set — no new failures). **Dashboard 430/430 + i18n 3/3,
  build OK** (log ~/workspace/tmp/dashboard-6a.log). **Playwright:**
  first run 125/126 (one flake: ai-api-and-agents accessible layout at
  1440px — passes in isolation, 8.4 s); **full re-run 126/126 passed
  (6.5 min)**.
  **CORRECTION (6B): the 823/11 number was measured at the wrong tree**
  — the run started ~17:08, before commit 2b57770 (17:17:11) landed
  mid-run, so it collected 839 tests at the pre-fix tree and never saw
  migration 016. See the 6B section below for the true numbers.
- **Register/readiness/blockers reconciled for 6A** (uncommitted at
  this writing): open rows 13 (0 P1, 13 P2), partials 36; gate tally
  17 MET / 3 PARTIALLY MET (13, 14, 16). D-08's register row is
  truncated mid-cell (pre-existing); its closure is recorded in the
  6A reconciliation note, not the row.

## Wave 6B — independent audit + fix (17 Nov 2026, ~18:04 UTC)

- **Auditor:** fresh subagent a8b9f7ab (no remediation-wave participation).
  Full report: ~/workspace/tmp/wave6b-boundary-report.md. Read-only; no
  credential printed. **Verdict: NO-GO.**
- **Confirmed by the auditor:** F-05-02 (re-proved live itself — 20/20
  cross-worker POSTs 200, no-CSRF control 403, deploy
  dep-dbu8ee8m7kps73ba7jmg; its test user csrf-test-6a-9e0ad9 remains on
  staging alongside 6A's csrf-test-6a-35c813 — both need a staging-DB
  cleanup job); gate 9 (only *.audit( call in src/ is the writer's own
  sink); gate 20 (generator --check exit 0, plain run zero git diff,
  227 pairs); gate 17 PARTIALLY (re-read the Render logs itself — 12x
  "cannot encrypt ... AI_BRIDGE_MCP_TOKEN_KEY is not configured" at
  16:49:32-33, then "migrated 3 plaintext MCP OAuth secret(s) to
  Fernet" at 16:56:42; all 4 migrate crons gone; the drill/verify cron
  outputs themselves rest on 6A's notes alone — jobs deleted, DB
  unreachable from here); residuals in code + tests (8/8 + TestNginx);
  gate 16 reproduced on local PG 18.7 (parity + boot exit 0); gate
  tally 17/3/0; open rows 13 (0 P1, 13 P2). Targeted tests 60/60, ruff
  clean, dashboard 430/430 + i18n 3/3, Playwright 126/126 in 6.2 min.
- **REGRESSION found by the auditor:** migration 016 broke
  tests/unit/test_db_phase2.py::test_minimal_pre013_db_applies_013_014_015
  (pinned applied == ["013","014","015"]; runner now also applies
  "016"). The 6A suite run never collected it (started ~17:08, before
  2b57770 at 17:17:11). **Fixed in 6B (commit 9e98d63):** pinned list
  -> ["013","014","015","016"], test renamed. Sibling assertion
  (applied == ["015"] after the down-migrations) is correct as-is:
  that fixture never removed 016, so it rightly does not re-apply.
  No other pinned migration lists exist in the repo (grep).
- **True suite numbers:** at 6A HEAD 3fb8170 — **828 passed / 12 failed /
  5 skipped** (auditor's run, log ~/workspace/tmp/pytest-6b-rerun.log,
  725 s, 845 collected; the 12th failure is the regression). At the
  fixed HEAD 9e98d63 — **829 passed / 11 failed / 5 skipped** (log
  ~/workspace/tmp/pytest-6b-fix.log, 734 s, 845 collected); the 11 are
  byte-for-byte the 6A baseline set (FAILED lists diffed — identical).
- **Nits fixed in 6B (commit 9e98d63):** IdP entrypoint sed
  metacharacters (secret/hash escaped for &, backslash and the |
  delimiter — verified verbatim with a hostile secret containing /,
  &, backslash and |); nginx comment "all three" -> "all four";
  _require_agent_mtls call sites corrected (eight in routers/agent.py
  49,107,198,220,238,353,406,461 + four in routers/webrtc.py
  428,451,469,487 = 12 enforcement calls); parity index-direction
  blind spot documented in check_schema_parity.py (ASC/DESC stripped,
  so an ASC-vs-DESC divergence would not be caught). The 2b57770
  commit message's "7 new parity tests" was actually 6 (6->12) —
  recorded in the docs, not rewritten.
- **What still blocks a GO (auditor):** (1) regression fixed + suite
  re-run at the fixed HEAD — DONE in 6B; (2) Gate 16 CI job never
  passed — needs James's push + fresh run; re-observe the unexplained
  macos-agent cancellation on run 44127496573 (0 steps, not a
  supersede — ci.yml has no concurrency config, still the latest run);
  (3) residuals + migration 016 not on staging yet — live verification
  pending the redeploy; (4) OIDC cross-worker live proof not done
  (IdP not deployed; sed nit fixed); (5) pre-existing: gate 13, gate
  14 (OIDC), Windows build + native tests (farhan RDP re-run),
  WebRTC E2E (Cloudflare WS block — user decision — plus TURN),
  13 open P2 rows; (6) housekeeping: two leftover staging test users
  (csrf-test-6a-35c813, csrf-test-6a-9e0ad9) need a staging-DB cleanup
  job; Gate 17 drill-row deletion could not be re-verified (DB
  unreachable from here).
- **Docs reconciled for 6B** (commit below): register, readiness,
  blockers, FINAL_TEST_RESULTS.md (new §1 rows + §7 provenance note),
  WAVE6A_NOTES.md (this section). Gate tally unchanged 17/3/0.
  Open rows unchanged 13 (0 P1, 13 P2); partials 36.
- **Local HEAD after 6B fix + reconciliation:** see git log. GitHub
  main is still 973273a7 — commits 9e39c50, 6bf819a, 2b57770, 3fb8170,
  9e98d63 and the 6B reconciliation commit are local-only and need the
  next zip + patch for James.

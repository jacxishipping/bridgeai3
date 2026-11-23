> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Wave 2B progress — dashboard honesty, docs, integrations (2026-11-17)

Repo: `/home/hatch/workspace/ai-bridge-main`, on top of git commit `726bed2`
(Wave 1). No commit made by this wave; no reset/rebase. Work split into
three tracks: dashboard honesty (child agent), dashboard tests (child
agent), integrations + docs (parent agent, this file's author). Only
Wave 2B's allowed files were touched by this wave; the `src/`,
`windows-agent/`, `migrations/` and `analysis/wave2a-*` changes visible in
`git status` belong to the concurrent Wave 2A and are NOT Wave 2B work.

## Status summary

| Finding | Status | Evidence |
|---|---|---|
| D-01 demo gating | IMPLEMENTED + VERIFIED (tsc exit 0) | Fresh load lands on `#/login`; demo only via explicit login-screen entry + interstitial; `#/live` untouched |
| D-02 fabricated Merkle | IMPLEMENTED + VERIFIED (grep: 0 hits) | Hardcoded root, tautology, `blocksChecked: 14920`, seal banner deleted; honest "Not verified" state |
| D-03 fake provider cards | IMPLEMENTED + VERIFIED (grep: 0 hits) | Cards live-only via `GET /api/settings/ai` or "Not configured" |
| D-04 fake vision canvas | IMPLEMENTED | Labelled "SIMULATED — no live frame" |
| D-07 session-only tokens | IMPLEMENTED + VERIFIED (grep) | `token()`/`sessionToken()` read sessionStorage only; legacy localStorage copies removed on sign-out |
| D-08 tokens in stream URLs | PARTIALLY IMPLEMENTED — 1 blocker | SSE/terminal/workflow URLs token-free; WS signalling URL still needs `?session_token=` (no backend stream-ticket endpoint; browsers cannot set WS headers) |
| TST-04 / D-06 tests real | IMPLEMENTED + TESTED | Tier 1/2 rewritten to import real modules, 155/155 each; Tier 5 + challenger wired in; `npm test` 407/430 |
| DOC-01/02/03 test counts | IMPLEMENTED | README 75 → 481 (grep-verified); CHANGELOG 29/33 qualified as subset counts |
| DOC-08 Fluent 2 | IMPLEMENTED | README + PROJECT.md now say JX-UI design system |
| DOC-12 SECURITY.md version | IMPLEMENTED | v0.2.x → v0.3.x |
| DOC-13 mTLS one statement | IMPLEMENTED | Canonical statement in CHANGELOG, implementation-status, remote-access, threat-model, security.md |
| DOC-14 OIDC one statement | IMPLEMENTED | "Code present, flow unverified until E2E passes" everywhere |
| DOC-15 macOS one statement | IMPLEMENTED | "Code exists, never run on macOS" in README, CHANGELOG, implementation-status |
| DOC-19 ORIGINAL_REQUEST.md | IMPLEMENTED | Citation removed as authoritative baseline; R1–R5 re-baselined on TEST_INFRA.md §1.2 + PROJECT.md |
| DOC-31 overstatement sweep | IMPLEMENTED | README/TEST_READY/PROJECT/SECURITY swept (see §DOC sweep) |
| DOC-10/11 demo-live split | IMPLEMENTED | TEST_READY §5.3/5.4 + dashboard README disclose the split |
| DOC-16 signed updates | IMPLEMENTED | Threat-model "out of scope" paragraph corrected |
| DOC-17 cgroups | IMPLEMENTED | Verified: no cgroup code anywhere; threat-model now says rlimits only |
| DOC-18 signed config | IMPLEMENTED | Verified: no signed-config / admin-audit-alert code; marked NOT implemented |
| DOC-20 dist/ claim | IMPLEMENTED | implementation-status now says dist/ is NOT in the tree |
| DOC-22 api-reference | IMPLEMENTED | Marked v0.2 snapshot, missing areas listed |
| DOC-30 token storage | IMPLEMENTED | Threat-model now says session storage (matches code) |
| D-10 language picker | IMPLEMENTED | Dashboard README corrected (picker exists; no full RTL claim) |
| DOC-09 zero custom CSS | IMPLEMENTED | PROJECT.md corrected — `tailwind.css` (281 lines) contains custom classes/tokens |
| I-01 hardcoded prod host | IMPLEMENTED + VERIFIED | 0 hits in integrations/ + apps/dashboard/ (see below) |
| I-02 Ollama single-turn | IMPLEMENTED + TESTED (stub) | Real multi-turn tool loop; stub-client check passed |
| I-03 LangChain args_schema | IMPLEMENTED + TESTED (live) | pydantic model built from tool JSON Schema; validation verified |
| I-04 approval-blind agents | IMPLEMENTED | All 4 example agents label `needs_approval` as pending, not done |
| I-05 Copilot untested | CONFIRMED (no change needed) | README already states "Not yet tested against a live Copilot Studio tenant"; no doc presents it as working |
| M6 milestone | CORRECTED to NOT DONE | 23 pre-existing Tier 5/challenger failures block it |

## 1. Dashboard honesty (D-01/02/03/04/07/08)

Files changed (child agent, verified by parent with independent greps):
- `apps/dashboard/src/api.ts` — session-only tokens; SSE/terminal/workflow
  stream URLs token-free; WS helper renamed `wsAuthQuerySuffix` with
  documented TODO; `clearCredentials()` also clears demo-ack flag.
- `apps/dashboard/src/state/dashboardStore.ts` — `DEMO_VIEWS` +
  `gateDemoView()`; fresh load on a demo route without credentials and
  without explicit demo entry is forced to `#/login`; login-screen
  navigation counts as the explicit entry (sessionStorage flag);
  `syncRoute()` re-gates typed/bookmarked demo hashes. `#/live`/`#/login`
  never gated.
- `apps/dashboard/src/shell/AppShell.tsx` — once-per-session interstitial
  modal on unauthenticated demo mounts ("Simulated data — demo
  workspace… sample data, not a real fleet").
- `apps/dashboard/src/views/AuditTrailView.tsx` — hardcoded Merkle root,
  tautological chain check, `blocksChecked: 14920`, `RECORDING_SEALED`
  banner, fabricated timeline, `merkleRoot` in JSON-LD export, "immutable
  records" wording all removed. "Check verification" refreshes live data
  if signed in, then shows honest amber "Not verified — no cryptographic
  verification available". Per-record modal renamed "Audit record details".
  Backend check (read-only): no audit chain-verification endpoint exists.
- `apps/dashboard/src/views/AiApiHubView.tsx` — hardcoded masked keys,
  fixed latencies, stale model lists, formula demo ping, fabricated
  execution removed. Cards initialise "Not configured / Not measured / No
  key configured" and are driven by `GET /api/settings/ai`
  (`keys_configured`/`masked_keys`); Ollama "No key required — local
  endpoint". Demo execution returns `status: "not_executed"`.
- `apps/dashboard/src/views/TaskQueueVisionView.tsx` — no-live-frame
  canvas shows amber "SIMULATED — no live frame" banner; "● Live" →
  "● Simulated"; marks labelled placeholder positions.
- `apps/dashboard/src/views/LiveDesktopView.tsx` — demo seal notice now
  "Demo mode: recording seal is simulated — no real recording was sealed."
  Live branch (real `merkle_leaf`) untouched.
- `apps/dashboard/src/views/LoginView.tsx` (parent) — caption added under
  the demo button: "Demo workspace — simulated data, not a real fleet."
  (i18n key left intact to avoid breaking the 20-locale catalog.)

Verification (parent, independent):
- `grep -rn "0x7fa281b94d" apps/dashboard/src --exclude-dir=tests` → no hits.
- `grep -rn "sk-ant-api03|sk-proj-|AIzaSy|o1-preview|gemini-ultra|blocksChecked: 14920|Demo Mode Sandbox"` over views/api/store/shell → no hits.
- `grep -rn "localStorage.getItem" apps/dashboard/src/` → only density,
  theme, locale, copilot messages, preferences — no tokens.
- `grep -rn "session_token=" apps/dashboard/src/ --exclude-dir=tests` →
  only `api.ts:106` (WS helper).
- `npx tsc --noEmit` in `apps/dashboard` → exit 0.
- `git diff --name-only` for the dashboard = exactly the 8 files above
  (plus tests files owned by the tests track).

Rollback: `git checkout -- apps/dashboard/src/api.ts apps/dashboard/src/state/dashboardStore.ts apps/dashboard/src/shell/AppShell.tsx apps/dashboard/src/views/AuditTrailView.tsx apps/dashboard/src/views/AiApiHubView.tsx apps/dashboard/src/views/TaskQueueVisionView.tsx apps/dashboard/src/views/LiveDesktopView.tsx apps/dashboard/src/views/LoginView.tsx`.

### D-08 blocker (remaining)
`apps/dashboard/src/api.ts:456` (`sessionWebSocketUrl`, used by
`views/LiveDesktopView.tsx:1344`) still appends `?session_token=…`.
Browsers cannot set headers on a WebSocket handshake and the control
plane exposes no short-lived stream-ticket endpoint (`v1_routes.py`
accepts `x-session-token` header OR `session_token` query). Mitigations
applied: token is now session-scoped only (sessionStorage, dies with the
tab). Full close requires a backend single-use stream-ticket endpoint —
out of this wave's scope (no `src/` edits allowed). Owner: Wave 2A/2C
(backend).

## 2. Dashboard tests (TST-04 / D-06)

Files changed (child agent, all under `apps/dashboard/src/tests/`):
`tier1-feature-coverage.test.ts`, `tier2-boundary-corner.test.ts`,
`harness.ts`, `run-all-tests.ts`, `tier5-adversarial.test.ts`,
`challenger-stress.test.ts`, `challenger-m1-2-stress.test.ts`,
`challenger-compliance.test.ts`. `package.json` and `e2e/` untouched.

What changed:
- Tier 1/2 fully rewritten to import real modules (`dashboardStore`,
  `api`, i18n `messages`/`translate`/`languages`, navigation, types);
  source-level checks labelled `[source-level]`. Every assertion that
  defined its expected value inside the test was deleted/rewritten.
- Tier 5 + all three challenger suites wired into `run-all-tests.ts`
  (steps 1/12–12/12; order is load-bearing and documented — challenger
  suites before tier5 because tier5 resolves seed approvals).
- Honesty-track updates applied to tests (Merkle "Not verified",
  provider "Not configured"). Two provably wrong literals corrected
  with byte-level evidence (enrollment token is NOT redacted in
  `DevicesView.tsx` — see observation below; `srv-win-prod-01` does not
  exist; palette handler is `onKeyDown`; log-1 says "Handshake" not
  "mTLS"; offline `cpuCores` can go negative).

Verified counts (method: regex `\bit\(\s*['"\`]` for `it(`; parent
re-verified the 356 `it(` total independently with the same regex):
- `it(` on disk: **356** (R1 7, R2–R3 6, R4 7, R5 6, Tier 1 155,
  Tier 2 155, Tier 3 10, Tier 4 10).
- Master runner scope: **430** = 356 + Tier 5 (31 `test(` cases) +
  challenger-stress (15) + challenger-m1-2 (27 executed) +
  challenger-compliance (1).
- `npm test` total incl. `test:i18n`: **433** (catalogs.test.ts: 3
  `node:test` cases).

Exact test results (child agent, Node v24.22.1, npm 10.9.4):
- `npm ci` in `apps/dashboard`: completed, "added 227 packages in 13s".
- `npx tsx src/tests/tier1-feature-coverage.test.ts`: exit 0, 155/155.
- `npx tsx src/tests/tier2-boundary-corner.test.ts`: exit 0, 155/155.
- `npm test`: **exit 1 — 430 total, 407 passed, 23 failed** (R1–R5
  26/26, Tier 1 155/155, Tier 2 155/155, Tier 3 10/10, Tier 4 10/10,
  Tier 5 25/31, Challenger 26/43). `test:i18n` run separately (the
  `&&` chain short-circuits on runner exit 1): **exit 0, 3/3**.
- `npm run build`: **exit 0, "✓ built in 10.28s"** (one CSS-minify
  warning: `"task" is not a known CSS property`; `dist/` written,
  git-ignored).
- Standalone baselines matched the in-run failures exactly (0 new
  failures): 19 cases assert the pre-Wave-2B demo-default behaviour
  (D-01 gating now resolves demo views to `login` in Node); 2 assert
  `decidedBy: 'operator:sarah.j'` vs store `'operator:admin'`
  (`dashboardStore.ts:1171`); 1 asserts a stale Device360View telemetry
  line; 1 asserts `font-mono` in Login/Overview views (absent). All 23
  are pre-existing test-expectation issues, not app regressions.
  Rollback: `git checkout -- apps/dashboard/src/tests/`.

Observation for the parent (from the tests track, NOT fixed here):
the enrollment token is NOT redacted — `DevicesView.tsx:682`
interpolates the raw `${enrolledToken}` into the copyable install
command and renders it in plain text. The pre-audit claim of redaction
was wrong (byte-level check: zero occurrences of `redacted` in the
file). Displaying the token in a copyable enrollment command is
arguably by design (the token must be in the command to work), so no
change was made; parent to decide whether to mask it in the UI.

## 3. Integrations (I-01 / I-02 / I-03 / I-04 / I-05)

Files changed (parent):
- `integrations/custom-ai/_bridge_config.py` (new) — `require_bridge_url()`
  (env `AI_BRIDGE_URL`/`AI_BRIDGE_CONTROL_URL`, else `SystemExit(2)`
  with a clear error; no default), `describe_result()` /
  `is_pending_approval()` (honest labelling of `needs_approval` /
  denied / failed results).
- `integrations/custom-ai/{openai,anthropic,langchain,ollama}_agent.py`,
  `quickstart.py` — hardcoded `https://jxiptvplayer.app` default removed;
  all use `require_bridge_url()`; docstring examples now use
  `https://your-control-plane.example`.
- `integrations/custom-ai/README.md`, `integrations/generic-mcp/README.md`,
  `integrations/generic-mcp/mcp-config.json` — example URLs replaced with
  the placeholder host. `docs/MCP_CHATBOT_SETUP.md`,
  `docs/installation-linux.md`, `docs/render.md` — same replacement.
- `integrations/custom-ai/ollama_local_agent.py` — rewritten as a real
  multi-turn tool loop (model → tools → results fed back → final
  synthesis, 8-turn cap) with approval-aware reporting (I-02 + I-04).
- `integrations/custom-ai/langchain_agent.py` — `build_args_schema()`
  builds a pydantic model from each tool's JSON Schema parameters
  (type map string/int/number/bool/array/object; required → required
  field); `StructuredTool.from_function(..., args_schema=...)`; `run_tool`
  returns `describe_result()` output (I-03 + I-04).
- `integrations/custom-ai/openai_agent.py`, `anthropic_agent.py` —
  tool results passed through `describe_result()` so the model is told
  a `needs_approval` result is queued, not executed (I-04).
- I-05: no change needed — `integrations/copilot365/README.md` already
  states "Not yet tested against a live Copilot Studio tenant" and no
  doc presents Copilot as working. A live-tenant test remains blocked
  (no tenant available).

Verification (parent):
- `grep -rn "jxiptvplayer" integrations/ apps/dashboard/` → zero hits
  except the helper's docstring, which names the former default as
  history (not a default). `README.md` examples also replaced.
- `python3 -m py_compile integrations/custom-ai/*.py` → OK.
- `/tmp/wave2b_integration_check.py` (stdlib + repo venv
  `.venv/bin/python`, pydantic 2.14.3): unset URL → `SystemExit(2)`
  PASS; set URL returned with trailing slash stripped PASS;
  `needs_approval` labelled "NOT been executed" PASS; completed result
  unchanged PASS; denied labelled PASS; Ollama `execute_tool_calls`
  with a stub client (needs_approval + completed) → pending flagged,
  results fed back PASS.
- `build_args_schema` live check (venv): model fields
  `['command', 'timeout']`; required field enforced (missing →
  `ValidationError`); optional int accepted with correct type;
  no-properties tool → `None`. All PASS.
- NOT tested live: no Ollama server, no LangChain/OpenAI/Anthropic
  packages in this env for the full agent loops (loop code is
  structural; the Ollama loop's tool-execution half is stub-tested
  above). No live control plane was contacted.

Rollback: `git checkout -- integrations/` + `rm integrations/custom-ai/_bridge_config.py`.

Remaining (out of this wave's scope — other waves own `src/`):
- `src/ai_bridge/client.py` docstring example still shows
  `https://jxiptvplayer.app` (code default is already
  `http://127.0.0.1:8000`; only the docstring example needs fixing).
- `agents/linux/agent_main.py`, `deployment/**`, `deploy_to_droplet.py`
  still default to the operator host — flagged for the wave that owns
  those files.

## 4. Docs sweep (DOC-01/02/03/08/10/11/12/13/14/15/16/17/18/19/20/22/30/31 + D-10 + DOC-09)

Files changed (parent): `README.md`, `PROJECT.md`, `CHANGELOG.md`,
`SECURITY.md`, `TEST_READY.md`, `TEST_INFRA.md`,
`apps/dashboard/README.md`, `docs/MCP_CHATBOT_SETUP.md`,
`docs/api-reference.md`, `docs/implementation-status.md`,
`docs/installation-linux.md`, `docs/operations.md`,
`docs/remote-access.md`, `docs/render.md`, `docs/security.md`,
`docs/threat-model.md`.

- DOC-01/02/03: README "(75 tests)" → "(481 test functions counted by
  `grep -c 'def test_' tests/`; CI publishes the run log)" — count
  re-verified by parent (`grep -rn 'def test_' tests/ | wc -l` = 481;
  403 at audit + Wave 1 regression tests). CHANGELOG: new 0.3.1 Wave 1
  entry; 29/33 qualified as the counts of the subset run for those
  releases, with "no run log stored in the repo" noted.
- DOC-08: README "Fluent 2 Web Dashboard" → "Web Dashboard (JX-UI
  design system)"; Mermaid label and repo-layout line fixed too.
- DOC-12: SECURITY.md supported version v0.2.x → v0.3.x.
- DOC-13/14/15: one statement each, used identically in CHANGELOG,
  implementation-status, remote-access, threat-model, security.md:
  mTLS — "code present, end-to-end verification pending" (fingerprint
  gate env-gated, proxy-header trust); OIDC — "code present, flow
  unverified until E2E passes"; macOS — "code exists, never run on
  macOS". Verified against code: `control/device_mtls.py:22-23`
  (env gate), `control/oidc.py` + `/api/auth/oidc/*` routes exist,
  `agents/macos/agent_main.py` docstring "NOT yet run on macOS".
- DOC-19: `ORIGINAL_REQUEST.md` citation removed as authoritative
  baseline in TEST_READY §1 and TEST_INFRA §1.2; R1–R5 re-baselined on
  TEST_INFRA.md §1.2 + PROJECT.md.
- DOC-31 sweep: README security badge "Hardened (P0/P1)" → "P0/P1
  remediation in progress"; "append-only audit logging" qualified in
  README + implementation-status + security.md (retention pruning can
  delete rows; WORM sink off by default); Tier-3/README claims left as
  written where Wave 1 made them true (Tier 3 off by default).
- DOC-10/11: TEST_READY §5.3/5.4 now disclose the demo/live split and
  that the 40 nodes are in-browser `SEED_DEVICES`; dashboard README
  already stated the split honestly.
- DOC-16/17/18: threat-model corrected — signed updates implemented
  (no longer "out of scope"); cgroups NOT implemented (no cgroup code
  anywhere in the tree; rlimits only); signed config + admin audit
  alerts NOT implemented (no code found).
- DOC-20: implementation-status `dist/` claim corrected (NOT in tree;
  rebuild with `npm install && npm run build`).
- DOC-22: api-reference marked "v0.2 snapshot — incomplete for v0.3"
  with the undocumented areas listed.
- DOC-23: operations.md rollback claim qualified (additivity NOT
  re-verified for v0.3 migrations 002–012; restore from backup).
- DOC-30: threat-model token storage localStorage → session storage
  (matches the D-07 code fix).
- DOC-09: PROJECT.md "Zero-Custom-UI Guarantee" corrected —
  `src/tailwind.css` (281 lines) really contains custom component
  classes (`.product-mobile-actions`, `.product-table-sticky-first`)
  and theme tokens, verified by reading the file.
- D-10: dashboard README "there is no language picker" corrected —
  `LanguageControl` is rendered in TopHeader, LiveControlView and
  LoginView (verified by grep).
- `docs/security.md`: `AI_BRIDGE_HTTP_ALLOW_ANON` row updated (bypass
  removed in Wave 1; setting it still fails production readiness);
  mTLS line corrected to the canonical statement.
- PROJECT.md M6: DONE → NOT DONE (23 pre-existing failures block it).
- TEST_READY.md v1.1.0: 430 master / 433 `npm test`, 407/430 pass,
  23 pre-existing failures itemised, "100% (356/356)" withdrawn,
  "0.10 seconds" withdrawn (not re-measured), verdict NOT YET READY.
- TEST_INFRA.md v1.1.0: facade honesty note (pre-Wave-2B Tier 1/2
  violated §1.1 in substance), R1 corrected, 4-tier → 5-tier +
  challenger methodology.

Leftover sweep check (parent): `grep -n "Fluent 2|75 tests|356 / 356|v0.2.x|jxiptvplayer"`
over README/PROJECT/CHANGELOG/SECURITY/TEST_READY/TEST_INFRA/docs →
only intentional mentions remain (the withdrawn-claim sentence in
TEST_READY §1 and the helper docstring in `_bridge_config.py`).

## 5. Remaining blockers / deferred

1. **D-08 WS token in URL** — needs a backend single-use stream-ticket
   endpoint (`src/`, out of scope for this wave).
2. **23 pre-existing Tier 5 / challenger failures** — 19 need their
   expectations updated to the Wave 2B demo-gating behaviour; 2
   `decidedBy`, 1 stale telemetry line, 1 `font-mono`. M6 stays NOT DONE
   until `npm test` exits 0.
3. **`npm test` `&&` chain** — `test:i18n` is skipped while the runner
   exits 1; switching the `test` script to `;` would always run it
   (left unchanged; `package.json` scripts edit deferred to parent).
4. **Live verification gaps** — no live Ollama/LangChain/OpenAI/Anthropic
   run, no live Copilot tenant test, no live control-plane check of the
   dashboard honesty changes (only `tsc --noEmit` exit 0 + build exit 0).
5. **`src/ai_bridge/client.py` docstring + `agents/`/`deployment/`
   host defaults** — out of this wave's file scope; flagged above.
6. **Enrollment token shown in plain text** in the copyable install
   command (`DevicesView.tsx:682`) — observed, not changed; parent to
   decide whether masking is wanted (it is arguably by design).

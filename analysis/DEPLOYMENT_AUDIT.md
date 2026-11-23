> **HISTORICAL analysis.** Current production assessment: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md).

# Deployment Audit (Appendix to reports 09–10)

**Scope:** every deployment artefact in the repo: `docker-compose*.yml`,
`deployment/docker/*`, `deployment/aws/*`, `render.yaml`, `deploy_to_aws.py`,
`deploy_to_droplet.py`, `deployment/linux/*`, `deployment/macos/*`,
`deployment/windows/*`, `deployment/systemd/*`, `deployment/oauth/*`,
`deployment/smoke_check.py`, `deployment/copilot_smoke_check.py`,
`magic-install.sh`, `magic-install.ps1`, `Setup.bat`,
`scripts/dev.sh`, `scripts/setup_mcp_chatbots.py`, `.env.example`,
`pyproject.toml`, `requirements.txt`, `requirements-windows.txt`,
`.github/workflows/ci.yml`.
**Method:** read-only source inspection. Nothing was built, run, or deployed
in this track; no cloud account was touched. Status labels: **VERIFIED**
(seen in a cited file), **INFERRED** (reasonable reading, labelled),
**NOT FOUND** (looked for, absent).

## 1. Executive summary

There are **four deployment shapes** in this repo, and they are not the same
system:

| Shape | Entry point | Stack | Status |
|---|---|---|---|
| Dev (local) | `docker-compose.dev.yml` + `scripts/dev.sh` | Control plane only, SQLite, hot reload, loopback :8000 | VERIFIED, coherent |
| Docker Compose prod | `docker-compose.prod.yml` + `deployment/linux/deploy-production.sh` | nginx + control-api (Gunicorn 4 workers) + mcp-http + oauth-gateway + Postgres 16 + Redis 7 + coturn + certbot | VERIFIED, most complete; several wiring gaps (see §3) |
| Render (test deployment) | `render.yaml` | 1 free web service (control plane, `AI_BRIDGE_EMBED_MCP=1`) + 1 static dashboard + managed Postgres; **no Redis, no separate MCP/gateway, no TURN** | VERIFIED; explicitly a *test* deployment (file header) |
| AWS CloudFormation | `deployment/aws/cloudformation.yml` + `deploy.sh` | ALB + EC2 Auto Scaling + RDS Postgres + ElastiCache Redis + EFS + Route53 + ACM, running the *Compose prod stack* on each EC2 host | VERIFIED as a template; **NOT FOUND** any evidence it has ever been applied (no state/outputs in repo) |

Plus three agent-install paths (Linux `magic-install.sh`, Windows
`installer_gui.py`/`Install-AIBridge.ps1`, macOS launchd plist) and an
update story split in two: **fleet self-update is an unsigned `git pull`**
(`tools.update_fleet_agent`), while **signed updates exist only as a
library** (`src/ai_bridge/updates.py`, Ed25519) that the fleet path does
not use.

The single most important deployment fact: **the production host
`jxiptvplayer.app` is the default domain in almost every script**
(`docker-compose.prod.yml`, `deployment/linux/deploy-production.sh`,
`magic-install.sh/.ps1`, `deployment/oauth/gateway.py` default issuer,
`deployment/aws/cloudformation.yml` UserData, `render.yaml` comments) —
the repo is configured for one operator's server, and every other deployer
must override it. Defaults that silently point at someone else's production
server are the recurring theme of this audit (see also report 10, I-01).

## 2. Docker and Compose

### 2.1 Dockerfiles — VERIFIED

| File | Base | Behaviour |
|---|---|---|
| `deployment/docker/Dockerfile` | `python:3.12-slim`, stages `base` / `dev` / `production` | `pip install -e .` from copied `pyproject.toml` + `src/`; `dev` adds `watchfiles`, exposes 8000; `production` stage runs `python -m ai_bridge.control`. Note: the `production` stage has **no non-root user, no HEALTHCHECK** (those exist only in `Dockerfile.prod`). |
| `deployment/docker/Dockerfile.prod` | Stage 1 `node:22-alpine` builds the dashboard (`npm install && npm run build`); stage 2 `python:3.12-slim-bookworm` | Installs `gunicorn`, `psycopg[binary]`, `prometheus-client`; copies `migrations/`; creates non-root user `aibridge` (1001) and `USER aibridge`; `HEALTHCHECK curl -f http://127.0.0.1:8000/health/ready`; `CMD gunicorn -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 ai_bridge.control.app:create_app()`. **Gap:** the frontend-builder stage runs `npm install` (not `npm ci`), so the dashboard build is not lockfile-pinned inside the image. |

### 2.2 Compose files — VERIFIED

**`docker-compose.yml`** (dev default): `control-api` (loopback
`127.0.0.1:8000`, `AI_BRIDGE_ADMIN_TOKEN=<redacted>` **placeholder**,
workspace volume), `postgres:16` behind profile `production`, `coturn`
behind profile `coturn` (static auth secret `"<redacted>"`
— a literal placeholder secret committed in the file). Header comment warns
"Do not expose :8000 publicly without the TLS reverse proxy".

**`docker-compose.dev.yml`**: control-api only, `target: dev`, source
mounted read-only (`./src:/app/src:ro`), workspace mounted, `uvicorn …
--reload`, `AI_BRIDGE_ALLOW_BOOTSTRAP=1`, `AI_BRIDGE_LOG_LEVEL=DEBUG`.
Coherent dev loop; `AI_BRIDGE_ALLOW_BOOTSTRAP=1` in a dev file is
acceptable, but the flag's production effect was not verified in this track.

**`docker-compose.prod.yml`** (6,988 bytes, 8 services): `control-api`
(Gunicorn image), `mcp-http` (`python -m ai_bridge.server --http --port
8001`, `AI_BRIDGE_DISABLE_HOST_TOOLS=1`,
`AI_BRIDGE_GATEWAY_VERIFY_URL=http://oauth-gateway:8002/internal/verify`),
`oauth-gateway` (`uvicorn gateway:app --port 8002`, working dir `/gateway`,
`./deployment/oauth:/gateway:ro`), `postgres:16-alpine` (healthcheck
`pg_isready`), `redis:7-alpine` (`--appendonly yes`), `coturn`
(ports 3478 tcp/udp, `--min-port=49152 --max-port=49200 --fingerprint`,
realm = domain), `nginx` (ports 80/443, `nginx.conf` template via
`envsubst`, `/etc/letsencrypt` read-only mount), `certbot` (renew loop,
12 h). Volumes: `aibridge-data`, `aibridge-pg`, `aibridge-redis`,
`certbot-challenge`. Networks: single `aibridge-network` bridge.

**Verified prod-compose issues:**

- `AI_BRIDGE_ALLOW_TIER3=${AI_BRIDGE_ALLOW_TIER3:-1}` (control-api env)
  — **Tier 3 is ON by default in production** unless the operator sets
  it to 0. The same `=1` appears in the CloudFormation UserData. For a
  remote-computer-control platform, the secure default would be 0 with
  explicit opt-in. (Finding P-01.)
- `POSTGRES_PASSWORD=<redacted>` is passed via `${POSTGRES_PASSWORD:-aibridge-dev-password}`
  in the control-api env (default `aibridge-dev-password` appears in the
  file's variable expansion for the DB URL) and `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-aibridge-dev-password}`
  for the postgres service — i.e. **a known default database password is
  baked into the compose file** if `.env.production` is absent. The
  `deploy-production.sh` generator does create random secrets, but compose
  alone does not require them. (Finding P-02.)
- `coturn` static auth secret defaults to `"change-me-in-production"`
  in the base compose file; the prod file uses
  `${AI_BRIDGE_TURN_SECRET:-change-me-in-production}` (same placeholder).
  (Finding P-02, same class.)
- The `mcp-http` healthcheck is a raw TCP connect to 127.0.0.1:8001, not
  an MCP-level check; the `oauth-gateway` healthcheck curls the discovery
  document (good); `control-api` has **no compose-level healthcheck**
  (it relies on the image HEALTHCHECK).
- `depends_on` for `control-api` waits for `postgres` healthy and `redis`
  started — but `mcp-http`/`oauth-gateway` only `depends_on: control-api`
  (no condition), so they can start before the plane is ready.
- The dashboard is **not a service** in prod compose: it is built into
  the control-api image (`Dockerfile.prod` copies `dist` to
  `/app/apps/dashboard/dist`) and presumably served by the control plane
  — the mechanism (static mount route) was **NOT FOUND** in this track's
  reading of `app.py` and should be confirmed by the control-plane track.

### 2.3 nginx (`deployment/docker/nginx.conf`) — VERIFIED

- HTTP `:80`: ACME challenge served from `/var/www/certbot`; **everything
  else proxies to the control plane with no redirect to HTTPS**
  (`location / { proxy_pass http://control_plane; … }`). There is no
  `return 301 https://$host$request_uri`. Plain-HTTP API access therefore
  works (and sends admin tokens in cleartext) whenever port 80 is open.
  (Finding P-03.)
- HTTPS `:443`: TLS 1.2 + 1.3, modern ciphers, `ssl_session_tickets off`,
  HSTS `max-age=63072000; includeSubDomains; preload`,
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: strict-origin-when-cross-origin`, and a CSP of
  `default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: wss:;
  frame-ancestors 'none'`. TLS configuration is competent.
- Routing: `/.well-known/(oauth-protected-resource|oauth-authorization-server|openid-configuration)`
  → oauth-gateway; `/oauth/` → oauth-gateway; `location = /mcp` → mcp-http
  with `proxy_set_header Host 127.0.0.1:8001` (required by the MCP server's
  DNS-rebinding guard) and `Authorization` forwarded; `location /` →
  control plane. Rate limiting: `30r/s` per IP, burst 100, `limit_req_status
  429`, applied to `/` in both servers but **not** to `/mcp`, `/oauth/` or
  the well-known locations. SSE/WebSocket: `proxy_buffering off`,
  `proxy_read_timeout 86400s`.
- **`/metrics` is unauthenticated.** `location /metrics { proxy_pass
  http://control_plane/metrics; … }` has no `auth_request`, no
  `allow/deny`, and no rate limit; the comment says "(optional restricted
  access)" but no restriction is implemented. Prometheus metrics (which can
  leak route, device and error counters) are public on the HTTPS vhost.
  (Finding P-04.)
- **`/internal/verify` is absent.** The OAuth README's nginx sketch
  defines `location = /internal/verify { internal; … }`, but the shipped
  `nginx.conf` contains no such location — so the `auth_request
  /internal/verify` line in the sketch is not what ships, and the shipped
  `/mcp` location has **no `auth_request` at all**. Authentication of
  `/mcp` in the shipped compose therefore depends entirely on the MCP
  server's own `_BearerAuthMiddleware` calling the gateway verify URL
  directly (which the compose env wires), not on nginx. That may be
  intentional, but the README sketch and the shipped config disagree, and
  a deployer following one will misconfigure the other. (Finding P-05;
  see also report 10, I-07.)

## 3. Dev vs prod configuration

| Concern | Dev (`docker-compose.dev.yml`, `scripts/dev.sh`) | Prod (`docker-compose.prod.yml`, `deployment/linux/deploy-production.sh`) |
|---|---|---|
| Database | SQLite (no Postgres service) | Postgres 16 (`DATABASE_URL=postgresql+psycopg://ai_bridge:${POSTGRES_PASSWORD}@postgres:5432/ai_bridge`), health-gated |
| Cache / shared state | None | Redis 7, `REDIS_URL=redis://redis:6379/0`, AOF persistence |
| Workers | Single uvicorn, `--reload` | Gunicorn, 4 Uvicorn workers |
| TLS | None (loopback :8000) | nginx, TLS 1.2/1.3, Let's Encrypt via certbot (12 h renew loop) |
| MCP | Not run (or embedded only if enabled) | Separate `mcp-http` :8001 + `oauth-gateway` :8002 |
| TURN | None (dev compose profile only) | coturn :3478, static-auth-secret |
| Dashboard | Vite dev :5173, `VITE_API_URL` empty → :8000 | Built into the control image; Render serves it as a separate static site |
| Secrets | `AI_BRIDGE_ADMIN_TOKEN=<redacted>` placeholder in compose; `scripts/dev.sh` **refuses to start** unless `AI_BRIDGE_ADMIN_TOKEN` and `AI_BRIDGE_OIDC_CLIENT_SECRET` are set in the environment (never from `.env` — the script's header says secrets are "intentionally NOT stored in .env") | `deploy-production.sh` generates `.env.production` (mode 600) with random secrets if absent; CloudFormation/Render take secrets from SSM / dashboard (never in files) |
| Bootstrap | `AI_BRIDGE_ALLOW_BOOTSTRAP=1`, `LOG_LEVEL=DEBUG`, `JSON_LOGS=false` | `AI_BRIDGE_PROFILE` unset in compose (Render sets `production`); no bootstrap flag |
| User | Root (dev container default) | Non-root `aibridge` (1001) in `Dockerfile.prod` only |

**Divergences that matter (VERIFIED):**

- **Redis exists in prod compose and AWS, but not in Render.** The
  control plane reads `REDIS_URL`/`AI_BRIDGE_REDIS_URL` only if set
  (`src/ai_bridge/control/app.py:91`). Multi-worker correctness (rate
  limits, pub/sub, locks) therefore differs by deployment: 4 Gunicorn
  workers **with** Redis (compose/AWS) vs a single Render instance
  **without** Redis vs dev single-process. Whether the plane is safe with
  4 workers and no Redis is the distributed-systems track's question; from
  the deployment side, the matrix is untested in combination.
- **`AI_BRIDGE_ALLOW_TIER3` defaults to 1 in prod compose and in
  CloudFormation UserData**, while Render does not set it at all (so the
  app default applies — NOT FOUND what that default is in this track).
  The tier posture is therefore deployment-dependent.
- **`.env.example`** (21 lines) lists the variable names with empty
  values (good: no secrets), but it omits several variables the compose
  files actually consume (`DATABASE_URL`, `REDIS_URL`, `POSTGRES_PASSWORD`,
  `AI_BRIDGE_PUBLIC_URL`, `AI_BRIDGE_MCP_HOST`, `AI_BRIDGE_OAUTH_HOST`,
  `AI_BRIDGE_TURN_SECRET`, `SECRET_KEY`, `SLACK/DISCORD/ALERT_WEBHOOK_URL`).
  A deployer following only `.env.example` will miss them. (Finding P-06.)
- **`requirements.txt` vs `pyproject.toml`:** `requirements.txt` is
  `-e .` plus loose lower bounds (`fastapi>=0.110`, `mcp>=1.6.0,<2.0.0`),
  i.e. **not actually locked** despite the header comment "Locked
  dependencies … Update with: pip-compile". There is no
  `requirements*.lock`, no `pip-compile` output, and no hash pinning. The
  prod Dockerfile installs `gunicorn`, `psycopg[binary]`, and
  `prometheus-client` **outside** `pyproject.toml` (extra, unpinned
  `>=3.2`/`>=0.20`). Reproducibility is weaker than the comments claim.
  (Finding P-07.)

## 4. Render (`render.yaml`) — VERIFIED

- Header: "Render Blueprint for AI Bridge **(test deployment)**".
- `ai-bridge-db`: Postgres `basic-256mb`, Frankfurt, `aibridge`.
- `ai-bridge-control`: Python, **free plan**, Frankfurt,
  `pip install -e .`, `python -m ai_bridge.control`, `/health` check,
  `PYTHON_VERSION 3.12.10`. Env: `AI_BRIDGE_ADMIN_TOKEN` (`sync: false`),
  `DATABASE_URL` from the database, `AI_BRIDGE_PUBLIC_URL` and
  `AI_BRIDGE_PUBLIC_BASE_URL` =
  `https://ai-bridge-control.onrender.com`, `AI_BRIDGE_PROFILE=production`,
  `AI_BRIDGE_OAUTH_ISSUER` = same host, `AI_BRIDGE_CONTROL_URL` = same
  host, **`AI_BRIDGE_EMBED_MCP=1`**, `AI_BRIDGE_MCP_ALLOWED_HOSTS` =
  the Render host + localhost, `AI_BRIDGE_CORS_ORIGINS` =
  `https://ai-bridge-dashboard.onrender.com`,
  `AI_BRIDGE_DEVICE_TOKEN_TTL_DAYS=7`.
- `ai-bridge-dashboard`: static site, `cd apps/dashboard && npm install &&
  npm run build`, `staticPublishPath: ./apps/dashboard/dist`, SPA rewrite
  `/* → /index.html`, `VITE_API_URL` baked to the control host at build
  time (comment: "Keep in sync with AI_BRIDGE_PUBLIC_URL / the control
  service hostname after any rename").
- **Gaps (VERIFIED):** no Redis (see §3); no separate MCP/gateway services
  (embedded MCP only); no TURN/coturn (WebRTC relay for the free tier is
  absent — the dashboard falls back to Google STUN, report 09 §6); free
  plan sleeps when idle (the dashboard's 60 s cold-start probe exists
  precisely because of this, `apps/dashboard/src/api.ts:20–57`); `npm
  install` (not `ci`) in the build command; and the baked-in `VITE_API_URL`
  means a control-host rename requires a dashboard rebuild **and** a
  control redeploy in the right order.

## 5. AWS (`deployment/aws/`) — VERIFIED as template, NOT verified as deployed

- `cloudformation.yml` (515 lines): parameters with `NoEcho: true` for
  `AdminToken` and three other secrets; resources: 4 security groups, RDS
  Postgres (`DBInstance`), ElastiCache Redis (`CacheCluster`), EFS
  filesystem + mount targets, ALB + target group + 2 listeners (80/443),
  Route53 record, IAM role/instance profile, EC2 LaunchTemplate (Ubuntu
  24.04 AMI via SSM parameter, `InstanceType` param), Auto Scaling Group
  + scaling policy, CloudWatch dashboard.
- **UserData** (the interesting part, `:365–410`): mounts EFS at
  `/var/lib/ai-bridge`, installs Docker via `curl -fsSL https://get.docker.com | bash`,
  clones `https://github.com/jacxishipping/ai-bridge.git` to
  `/opt/ai-bridge`, writes `.env.production` containing
  `POSTGRES_PASSWORD`/`REDIS_PASSWORD`/`AI_BRIDGE_ADMIN_TOKEN` fetched
  from SSM (`aws ssm get-parameter … --with-decryption`) plus
  `SECRET_KEY`, then `docker compose --env-file .env.production -f
  docker-compose.prod.yml up -d`. Note: `AI_BRIDGE_ALLOW_TIER3=1` is
  hardcoded in the generated env file; `AI_BRIDGE_OIDC_ISSUER`,
  `AI_BRIDGE_OIDC_CLIENT_ID/SECRET` are written **empty** (OIDC off by
  default on AWS).
- `deploy.sh` (74 lines): interactive prompts (region, stack, domain,
  hosted zone, ACM ARN, key pair, VPC, subnets, admin token with
  auto-generate fallback `openssl rand -hex 32`), stores three secrets in
  SSM (`/ai-bridge/$STACK_NAME/{db-password,redis-password,secret-key}`),
  then `aws cloudformation deploy … --capabilities CAPABILITY_IAM`.
  Sensible secret handling; the admin token is a CloudFormation parameter
  (`NoEcho`), which is the correct mechanism.
- `README.md` (111 lines): architecture diagram (Route53 → ALB → EC2 ASG
  → RDS, plus ElastiCache and EFS), prerequisites, quick start, and the
  SSM secret commands. Coherent with the template.
- **Divergence (INFERRED, needs the distributed-systems track to confirm):**
  the ASG runs the *whole Compose stack* (including its own Postgres
  client and Redis client) on each EC2 host, while RDS/ElastiCache/EFS are
  shared. That means each host also runs nginx/coturn/certbot and a local
  `aibridge-data` volume — but the compose volumes are host-local, so
  any state written to `aibridge-data` (OAuth SQLite DB at
  `/var/lib/ai-bridge/oauth/oauth.db`, `AI_BRIDGE_DATA_DIR`) is **not**
  shared between ASG hosts unless EFS is mounted over it (UserData mounts
  EFS at `/var/lib/ai-bridge`, which does cover it — the mount happens
  before compose starts, so this *may* be correct; it was not verified
  end-to-end here). The ALB target group health check and the ASG
  min/max/desired values were not extracted in this track.
- **`deploy_to_aws.py`** (113 lines) is a *different* AWS path from
  CloudFormation: it SSHes (or uses SSM `AWS-RunShellScript`) into an
  existing EC2 host and runs `git pull --ff-only origin main`, `docker
  compose … build control-api`, `up -d --no-deps control-api mcp-http
  oauth-gateway`, `up -d --no-deps --force-recreate nginx`, then `docker
  ps`. Bootstrap mode (`AI_BRIDGE_BOOTSTRAP=1`) does `apt-get upgrade`,
  installs Docker via `curl … | sudo bash`, installs the Compose plugin
  from `releases/latest`, and `git clone`s the repo. SSH uses
  `StrictHostKeyChecking=accept-new`. No health gate after the restart
  (unlike `deploy-production.sh`, which polls `/health/ready`).
  `deploy_to_droplet.py` (83 lines) is the same script with different
  defaults (`ubuntu@jxiptvplayer.app`) and `--force-recreate` on the app
  services. The two scripts are ~90% duplicated (finding P-08).

## 6. Linux agent install and service management

- **Magic installer** (`magic-install.sh`, 622 lines; identical copy at
  `deployment/linux/magic-install.sh`; `deployment/linux/install.sh` is a
  17-line shim delegating to it): distro detection (apt/dnf/apk/brew),
  Python 3.12 provisioning, venv, `git clone` or, if present,
  `git fetch origin main && git reset --hard origin/main` (`:340–343`) —
  i.e. **the installer itself performs an unsigned `git reset --hard` to
  origin/main on every re-run**. Auto-enrollment posts to
  `/api/devices/enroll` with `curl -s -k` (`:466`) — **`-k` disables TLS
  verification for the enrollment call that carries the admin token**
  (finding P-09). The payload sets `unrestricted: true`. Default control
  URL is `https://jxiptvplayer.app` (`:30`).
- **`deployment/linux/run-agent.sh`** (1,048 lines): "All-in-One Manager &
  Runner" — distro/deps/venv/enroll, config persistence
  (`/etc/ai-bridge` or `~/.ai-bridge`), systemd unit generation
  (`ExecStart=… agents/linux/agent_main.py run`, `Restart=always`,
  `RestartSec=5`), foreground/debug modes, token rotation and "automatic
  codebase updates". `AI_BRIDGE_INSECURE_TLS=1` adds `-k` to curl
  (`:21`) — an explicit insecure escape hatch, at least opt-in.
- **`deployment/systemd/`**: `ai-bridge-control.service` —
  `User=ai-bridge`, `EnvironmentFile=/etc/ai-bridge/control.env`,
  `ExecStart=/opt/ai-bridge/.venv/bin/python -m ai_bridge.control`,
  `Restart=on-failure`, `RestartSec=5`, `NoNewPrivileges=true`,
  `PrivateTmp=true`. `ai-bridge-agent.service` — **`User=root`**,
  `WorkingDirectory=/opt/ai-bridge`, `EnvironmentFile=-/etc/ai-bridge/agent.env`,
  `ExecStart=…/python agents/linux/agent_main.py run`,
  `Restart=always`, `KillMode=process`, `LimitNOFILE=65536`,
  `TasksMax=infinity`. The agent runs as root by design (it manages
  services/packages); the control plane does not. The contrast is
  deliberate, but `TasksMax=infinity` + root + `Restart=always` means a
  faulty agent can fork without bound — worth a `TasksMax` (finding P-10,
  low).
- **`deployment/linux/deploy-production.sh`** (187 lines): root check,
  Docker install via `curl -fsSL https://get.docker.com | sh`, generates
  `.env.production` (600) with `openssl rand` secrets if absent (domain
  default `jxiptvplayer.app`, ACME email `admin@jxiptvplayer.app`),
  creates a **30-day self-signed certificate** if no Let's Encrypt cert
  exists so nginx can boot, builds + `up -d`, polls
  `http://127.0.0.1:80/health/ready` (note: **over plain HTTP on port 80**,
  `-k`, up to 60 s), then **prints the admin token in the success summary**
  (`:174`, "Admin Auth Token: ${AI_BRIDGE_ADMIN_TOKEN}") — the token is
  echoed to the terminal/log on every deploy (finding P-11). It also
  prints the metrics URL. Health gate exists, but only checks the
  readiness endpoint through nginx, not the MCP or gateway services.

## 7. Windows installers

- **Canonical path:** `Setup.bat` (root, 22 lines) → `powershell -ExecutionPolicy Bypass -File deployment\windows\Install-AIBridge.ps1`.
  `Install-AIBridge.ps1` (184 lines) is a shim: it resolves a tested
  Python (3.12–3.14 only; 3.15 refused), then forwards to
  `installer_gui.py` (1,316 lines, the real wizard) or, for `-Mode
  Service`, to `install-agent.ps1` (332 lines).
- **`installer_gui.py`** (version `0.3.0`): six-step wizard — detect/repair,
  control URL + device token with **live verification against `/health`
  and the agent task route before installing**, Python selection, venv +
  `requirements.txt` install + import verification, config in
  `~/.ai-bridge/agent_gui.json` **without any credential**, device token
  sealed with DPAPI in `~/.ai-bridge/device.token`, background agent at
  sign-in (scheduled task when elevated, HKCU Run otherwise), shortcuts,
  uninstall/repair. Default control URL in the wizard is
  `https://ai-bridge-control.onrender.com` (the Render test host — a
  different default from the shell installers' `jxiptvplayer.app`;
  finding P-12, inconsistency). `--silent` runs the same steps headlessly
  and is what the tests drive.
- **`install-agent.ps1` / `Install-AIBridge.ps1`**: modes Attended
  (tray/desktop client), Headless (poller at sign-in), Service (poller at
  boot); `-HeadlessService` is a legacy alias for Headless. Native service
  path (`windows-agent/installer/install-service.ps1`, referenced in
  `deployment/windows/README.md`) uses `sc create` + pipe secret + HKLM
  Run — session-0 service + logon desktop agent. `Build-EXE.bat` /
  `build_exe.py` (327 lines) build a one-file `AIBridge-Setup.exe`
  (PyInstaller-style, `sys._MEIPASS` handling in the wizard).
- **`magic-install.ps1`** (411 lines): `irm … | iex` bootstrap, admin
  auto-elevation, silent Python 3.12 + Git installs, venv, package install
  (BetterCam, PyWin32), auto-enroll against `https://jxiptvplayer.app`,
  startup registration, `-Diagnose` support report. Same default-host
  issue as the `.sh` (P-12).
- **`deployment/windows/requirements.txt`** (content: `-r
  deployment/windows/requirements.txt` — i.e. the file at
  `requirements-windows.txt` in the repo root, which itself contains a
  single line `-r deployment/windows/requirements.txt`; the chain was not
  fully resolved in this track — **INFERRED** circular/self-referential
  risk, verify before running a Windows pip install from these files).
  The Windows README's package table (aiortc, av, Pillow, psutil, fastapi,
  uvicorn, pydantic, httpx, cryptography, pystray) is the authoritative
  list; whether `deployment/windows/requirements.txt` matches it was
  **NOT VERIFIED** line-by-line here.

## 8. macOS

- `deployment/macos/com.aibridge.agent.plist`: `Label
  com.aibridge.agent`, `ProgramArguments` = `/usr/bin/python3
  REPO_PATH/agents/macos/agent_main.py --control-url CONTROL_URL`,
  `AI_BRIDGE_DEVICE_TOKEN=DEVICE_TOKEN` in `EnvironmentVariables`,
  `RunAtLoad` + `KeepAlive`, logs to `/tmp/ai-bridge-agent.out/.err`.
  **The plist ships with literal placeholders** (`REPO_PATH`,
  `CONTROL_URL`, `DEVICE_TOKEN`) that the operator must edit by hand, and
  the device token sits in **plaintext in the plist**
  (`EnvironmentVariables` in a user-readable file).
- `deployment/macos/README.md`: install is `cp` the plist to
  `~/Library/LaunchAgents/`, edit, `launchctl load`. Its Status section
  states: "**Not yet run on a Mac** — launchd behaviour still needs a real
  macOS verification before this is claimed working." The agent runs as
  the logged-in user so desktop automation runs in that user's session.
  **macOS deployment is therefore documented, not verified** — same
  honesty pattern as Copilot (report 10, I-05).

## 9. OAuth gateway and reverse proxy (deployment view)

Covered in depth in report 10 §4; deployment-specific notes:

- The gateway is a **separate service only in prod compose** (port 8002,
  loopback, `./deployment/oauth:/gateway:ro`). On Render it does not exist
  as a service — `AI_BRIDGE_EMBED_MCP=1` means the control plane embeds
  the MCP endpoint and presumably the OAuth handling in-process (the exact
  embedded behaviour was **NOT FOUND** in this track; confirm with the
  control-plane track whether the embedded path enforces the same PKCE /
  rotation / device-binding rules as the standalone gateway).
- The gateway stores only hashes (SQLite, `AI_BRIDGE_OAUTH_DB`, default
  `https://jxiptvplayer.app` issuer). In prod compose the DB lives on the
  shared `aibridge-data` volume (`/var/lib/ai-bridge/oauth/oauth.db`) —
  correct for a single host, but see §5 on ASG multi-host sharing.
- The nginx sketch in `deployment/oauth/README.md` (with
  `location = /internal/verify { internal; … }` and the warning "Do NOT
  override the Host header: the MCP server rejects non-local Host
  values") is the **only** place the auth_request wiring is documented;
  the shipped nginx.conf omits it (finding P-05).

## 10. Update mechanism — two stories, only one signed

| Mechanism | Implementation | Signed? | VERIFIED |
|---|---|---|---|
| Fleet self-update (dashboard "Update fleet" / "Update agent" buttons → `POST /api/fleet/update`, `POST /api/devices/{id}/update`) | Control plane queues a task with tool `update_fleet_agent` (`src/ai_bridge/control/app.py:3915–3956`); the agent runs `src/ai_bridge/tools.py:1789` `update_fleet_agent()`, which does `git fetch origin main && git checkout -f -B main origin/main && git rev-parse --short HEAD` — an **unsigned `git checkout -f`** of whatever `origin/main` points at, with no hash pinning, no signature check, no rollback | **No** | `tools.py:1789–1813`; `app.py:3915–3956` |
| Signed releases (library) | `src/ai_bridge/updates.py` (173 lines): Ed25519 keygen/sign/verify (`cryptography`), manifest `{name, version, files[]}` with per-file SHA-256, `download_and_stage_update` with path-traversal guard (`_safe_manifest_path`), `auto_update_allowed()` gated on `AI_BRIDGE_ALLOW_AUTO_UPDATE=1`; agent CLI `--check-update --manifest-url … --signature-url … --public-key …` verifies **before** downloading and refuses on bad signature/hash; staged apply does not restart the agent | **Yes** | `updates.py:1–173` (docstring workflow) |
| Installer re-run | `magic-install.sh` re-run does `git fetch origin main && git reset --hard origin/main` (same unsigned pattern as the fleet path) | **No** | `magic-install.sh:340–343` |
| Container redeploy | `deploy_to_aws.py` / `deploy_to_droplet.py`: `git pull --ff-only origin main` + `docker compose build` + `up -d` | **No** (git transport + `--ff-only` only) | `deploy_to_aws.py`, `deploy_to_droplet.py` |
| Windows agent update | Dashboard "agent upgrade" in demo is simulated; live path calls `updateDeviceAgent` (same task mechanism as fleet). No separate Windows signed-update flow was found in this track. | **No** (via fleet path) | report 09 §3 |

**The signed-update library exists but nothing in the deployment path uses
it.** The fleet update that an operator actually triggers from the dashboard
is an unsigned forced checkout of a moving branch. If the GitHub org or any
mirror in the chain is compromised, every fleet agent that accepts the task
checks out attacker code. This is the highest-severity deployment finding
(P-13). The fix is to route `update_fleet_agent` through `updates.py`
(manifest + signature + hash verification, with the public key distributed
out-of-band), or to remove the dashboard update buttons until that is done.

## 11. Environment variables (consolidated, VERIFIED from the files cited)

| Variable | Dev | Prod compose | Render | AWS CFN | `.env.example` |
|---|---|---|---|---|---|
| `AI_BRIDGE_ADMIN_TOKEN` | placeholder `change-me` | `${…}` / generated | `sync: false` (dashboard secret) | SSM + `NoEcho` param | listed (empty) |
| `AI_BRIDGE_WORKSPACE` | `/workspace` | `/var/lib/ai-bridge/workspace` | — | `/var/lib/ai-bridge/workspace` | listed |
| `AI_BRIDGE_DATA_DIR` | `/workspace/.ai-bridge` | `/var/lib/ai-bridge` | — | `/var/lib/ai-bridge` | listed |
| `AI_BRIDGE_CONTROL_PORT` / `PORT` | 8000 | 8000 | `$PORT` (Render) | 8000 | `CONTROL_PORT` listed |
| `AI_BRIDGE_CONTROL_URL` | — | `http://control-api:8000` (mcp-http) | = Render host | — | listed |
| `DATABASE_URL` | — (SQLite) | `postgresql+psycopg://…` | fromDatabase | RDS (via SSM password) | **missing** |
| `REDIS_URL` | — | `redis://redis:6379/0` | — (absent) | ElastiCache (endpoint not shown in UserData) | **missing** |
| `AI_BRIDGE_ALLOW_TIER3` | — | default `1` | — (absent) | `1` (UserData) | listed (empty) |
| `AI_BRIDGE_DEVICE_TOKEN_TTL_DAYS` | — | default `7` | `7` | `7` | listed |
| `AI_BRIDGE_PUBLIC_URL` / `AI_BRIDGE_DOMAIN` | — | default `https://jxiptvplayer.app` | Render host | `${DomainName}` | **missing** |
| `AI_BRIDGE_OIDC_*` | required by `dev.sh` (`CLIENT_SECRET`) | env-passed (often empty) | — | empty | listed |
| `AI_BRIDGE_CORS_ORIGINS` | — | — | dashboard origin | — | **missing** |
| `AI_BRIDGE_EMBED_MCP` | — | — | `1` | — | **missing** |
| `AI_BRIDGE_MCP_HOST` / `AI_BRIDGE_OAUTH_HOST` | — | `mcp-http` / `oauth-gateway` | — | — | **missing** |
| `AI_BRIDGE_TURN_SECRET` / `POSTGRES_PASSWORD` | placeholder secrets | `${…:-change-me…}` / `${…:-aibridge-dev-password}` | — | SSM | **missing** |
| `SECRET_KEY` | — | generated by `deploy-production.sh` | — | SSM | **missing** |
| `VITE_API_URL` | empty → :8000 | — (built into image) | baked Render host | — | n/a (frontend) |

## 12. CI (`.github/workflows/ci.yml`) — VERIFIED

Four jobs: `test` (Ubuntu, Python 3.12, `pip install -e ".[dev]"`,
`ruff check .`, `pytest -v`); `macos-agent` (import check + one unit
test); `windows-native-webrtc` (CMake + libdatachannel build of
`ai_bridge_desktop`, then `scripts/libwebrtc_e2e_harness.py`, evidence
artifact uploaded, 14-day retention); `dashboard` (Node 20, `npm ci`,
`npm run build`, `npm test`, Playwright Chromium install + `npm run
test:browser`, failure artifacts 7 days). **No deployment job, no image
build/push, no Render/AWS deploy step, no secret scanning, no container
scan** — CI validates code, not releases. That is consistent with a
project that deploys by hand via the scripts above.

## 13. Findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| P-01 | High | **Tier 3 ON by default in production.** `AI_BRIDGE_ALLOW_TIER3=${…:-1}` in prod compose and `=1` in CloudFormation UserData. Shell/desktop control is opt-out, not opt-in, on the production deployment path. | `docker-compose.prod.yml:29`; `deployment/aws/cloudformation.yml` (UserData env) |
| P-02 | High | **Known default secrets in compose files.** `POSTGRES_PASSWORD` defaults to `aibridge-dev-password`; TURN secret defaults to `change-me-in-production`; base compose has a literal `"<redacted>"` TURN secret. A deploy that skips `.env.production` generation gets guessable credentials. | `docker-compose.prod.yml`; `docker-compose.yml` (coturn) |
| P-03 | High | **No HTTP→HTTPS redirect.** Port 80 proxies the API in cleartext; admin tokens can be sent over HTTP whenever :80 is reachable. | `deployment/docker/nginx.conf` (`:80` server block) |
| P-04 | High | **`/metrics` publicly exposed without auth** on the HTTPS vhost ("optional restricted access" is not implemented). | `deployment/docker/nginx.conf:175–179` |
| P-05 | High | **Shipped nginx omits the OAuth `auth_request` wiring** the gateway README sketches (`/internal/verify` + `auth_request` on `/mcp`). The two documents disagree about how `/mcp` is authenticated. | `deployment/docker/nginx.conf` (absent) vs `deployment/oauth/README.md` (sketch) |
| P-06 | Medium | **`.env.example` omits variables compose actually consumes** (`DATABASE_URL`, `REDIS_URL`, `POSTGRES_PASSWORD`, `AI_BRIDGE_PUBLIC_URL`, `AI_BRIDGE_TURN_SECRET`, `SECRET_KEY`, `AI_BRIDGE_CORS_ORIGINS`, `AI_BRIDGE_EMBED_MCP`, …). | `.env.example` (21 lines) vs `docker-compose.prod.yml`, `render.yaml` |
| P-07 | Medium | **`requirements.txt` is not locked** despite claiming to be; no lockfile or hashes; prod Dockerfile adds unpinned `gunicorn`/`psycopg`/`prometheus-client` outside `pyproject.toml`; dashboard image build uses `npm install`, not `npm ci`. | `requirements.txt`; `deployment/docker/Dockerfile.prod`; `render.yaml` (buildCommand) |
| P-08 | Medium | **`deploy_to_aws.py` and `deploy_to_droplet.py` are ~90% duplicated** with different defaults (`ubuntu@ec2-instance` vs `ubuntu@jxiptvplayer.app`); neither has a post-deploy health gate. | Both files (113 vs 83 lines) |
| P-09 | Medium | **Magic installer enrolls with `curl -k`** — TLS verification disabled on the call that transmits the admin token and receives the device token. | `magic-install.sh:466`; `deployment/linux/magic-install.sh:466` |
| P-10 | Low | **Agent systemd unit runs as root with `TasksMax=infinity`** and `Restart=always`. Deliberate (it manages services), but unbounded. | `deployment/systemd/ai-bridge-agent.service` |
| P-11 | Low | **`deploy-production.sh` prints the admin token** in its success summary (and the metrics URL). Tokens in terminal scrollback / CI logs. | `deployment/linux/deploy-production.sh:174` |
| P-12 | Low | **Three different default control URLs**: `jxiptvplayer.app` (shell installers, compose, CFN), `ai-bridge-control.onrender.com` (Windows wizard), `127.0.0.1:8000` (SDK/dev). A user following two guides gets two different servers. | `magic-install.sh:30`; `deployment/windows/installer_gui.py` (`DEFAULT_CONTROL_URL`); `src/ai_bridge/client.py` |
| P-13 | High | **Fleet update is an unsigned forced `git checkout`** (`update_fleet_agent`: `git fetch` + `checkout -f -B main origin/main`), while a signed Ed25519 update library (`updates.py`) exists and is unused by that path. Dashboard "Update fleet/agent" buttons trigger the unsigned path. The installer re-run path has the same flaw (`git reset --hard origin/main`). | `src/ai_bridge/tools.py:1789–1813`; `src/ai_bridge/updates.py`; `magic-install.sh:340–343`; `src/ai_bridge/control/app.py:3915–3956` |
| P-14 | Medium | **macOS deployment is documented, not verified** ("Not yet run on a Mac"), and the plist ships literal placeholders plus a **plaintext device token** in `EnvironmentVariables`. | `deployment/macos/com.aibridge.agent.plist`; `deployment/macos/README.md` (Status) |
| P-15 | Medium | **Render is a single free instance with no Redis, no separate MCP/gateway, no TURN, and cold-start sleeps** — fine as the labelled test deployment, but it is not a production topology and its env matrix (embedded MCP, no tier flag) differs from prod compose. | `render.yaml`; §3 table |
| P-16 | Low | **No deployment automation in CI** — no image build/push, no deploy job, no container/secret scan. Releases are manual script runs. | `.github/workflows/ci.yml` (4 jobs, none deploy) |
| P-17 | Low | **Windows requirements chain is confusing** (`requirements-windows.txt` → `-r deployment/windows/requirements.txt`; the inner file's contents were not verified line-by-line here) and the wizard's package table is the de-facto source of truth. Reconcile the two. | `requirements-windows.txt`; `deployment/windows/requirements.txt`; `deployment/windows/README.md` |
| P-18 | Info | **Good practices worth keeping:** non-root user + HEALTHCHECK in `Dockerfile.prod`; SSM `SecureString` + `NoEcho` params in AWS; `dev.sh` refusing to start without secrets and refusing to store them in `.env`; gateway stores only token hashes; CloudFormation UserData mounts EFS before starting compose. | Cited files in §2/§5/§6 |

## 14. Recommendations (deployment only)

1. **P0 — Route fleet updates through the signed-update library** (P-13):
   make `update_fleet_agent` verify an Ed25519 manifest + per-file hashes
   (public key distributed with the agent install, not fetched at update
   time) before any checkout/apply; add a rollback path; until then, gate
   the dashboard update buttons behind an explicit warning or disable
   them. Same for the magic-installer re-run path.
2. **P0 — Change prod defaults** (P-01/P-02): `AI_BRIDGE_ALLOW_TIER3`
   default `0`; fail compose startup when `POSTGRES_PASSWORD` /
   `AI_BRIDGE_TURN_SECRET` / `AI_BRIDGE_ADMIN_TOKEN` are unset or equal
   to a known default, instead of silently substituting one.
3. **P1 — Fix the edge** (P-03/P-04/P-05): add the HTTP→HTTPS redirect
   (keeping only the ACME challenge on :80); restrict `/metrics` (auth
   or `allow 127.0.0.1` + internal network); reconcile the nginx
   `/internal/verify` + `auth_request` wiring between the README sketch
   and the shipped `nginx.conf` (ship one, delete the other).
4. **P1 — Remove `curl -k` from enrollment** (P-09) and from the
   deploy-production health probe where avoidable; pin the CA bundle or
   require a valid cert for the control URL at install time.
5. **P2 — Unify the deploy scripts** (P-08/P-12): merge
   `deploy_to_aws.py`/`deploy_to_droplet.py` into one script with a
   `--target` flag and a post-deploy health gate (poll `/health/ready`,
   `/mcp` unauthenticated-rejection, and discovery docs — reuse
   `deployment/smoke_check.py` / `copilot_smoke_check.py`); make the
   default control URL a required parameter everywhere (no silent
   `jxiptvplayer.app`).
6. **P2 — Complete `.env.example`** (P-06) from the union of variables
   consumed by compose + Render + CFN; generate a real lockfile
   (`pip-compile`) and use `npm ci` in the Dockerfile.prod frontend stage
   (P-07).
7. **P3 — macOS:** verify on a real Mac and replace the plaintext token
   in the plist with a keychain lookup or an env file (P-14); stop printing
   the admin token in `deploy-production.sh` output (P-11); bound the
   agent unit (`TasksMax`, P-10).

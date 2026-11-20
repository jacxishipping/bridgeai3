FROM python:3.15-slim AS base
# Postgres client tools for the backup/restore drill (scripts/backup_restore.py
# needs pg_dump/psql/pg_restore/createdb on PATH). Staging Postgres is PG 18
# but Debian trixie's postgresql-client is PG 17, and pg_dump refuses a
# newer server — so install postgresql-client-18 from the PGDG repo.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt trixie-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Gate 13 (Wave 7): the fleet self-update target. update_fleet_agent()
# resolves repo_dir as explicit arg -> AI_BRIDGE_AGENT_REPO_DIR -> the
# process cwd, but only if the cwd provably is an AI Bridge checkout
# (pyproject.toml named ai-bridge + .git); an explicit, deterministic
# value here keeps the Docker image independent of the worker's cwd.
ENV AI_BRIDGE_AGENT_REPO_DIR=/app
COPY pyproject.toml README.md ./
COPY src ./src
# The control plane embeds the OAuth gateway on PaaS (combined.py loads
# deployment/oauth/gateway.py relative to the repo root), so it must be in
# the image too — without it the container crashes on boot with
# ModuleNotFoundError: No module named 'gateway'.
COPY deployment/oauth ./deployment/oauth
# The Postgres boot runs migrations from the repo-root migrations/
# directory (migrations.py refuses to start without it, DB-08).
COPY migrations ./migrations
# Ops scripts (backup/restore drill) — also used by the Render cron job.
COPY scripts ./scripts
RUN pip install --no-cache-dir -e .

FROM base AS dev
RUN pip install --no-cache-dir watchfiles
EXPOSE 8000

FROM base AS production
# Render overrides with $PORT at runtime; the app binds 0.0.0.0:$PORT when set.
EXPOSE 8000
CMD ["python", "-m", "ai_bridge.control"]

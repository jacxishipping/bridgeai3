#!/bin/sh
# One-shot Postgres backup/restore drill for the staging/production plane (Release 1 P0).
#
# Run as a Render cron job (Docker runtime, same image as the plane) or in CI:
#   dockerCommand: sh scripts/backup_drill.sh
#   env: DATABASE_URL = internal DSN of the PostgreSQL database
#
# Steps: pg_dump live DB -> restore into isolated ai_bridge_staging_drill ->
# verify all migrations match migrations/[0-9]*.sql -> validate users/devices/
# tasks/sessions/approvals/audit counts -> verify audit hash-chain continuity ->
# boot the FastAPI control plane against the restored DB -> record RTO/RPO ->
# drop the drill DB. The live database is never written to and DSN passwords
# are redacted.
set -eu

RESTORE_DSN=$(python3 -c "import os; from urllib.parse import urlsplit, urlunsplit; u = urlsplit(os.environ['DATABASE_URL']); print(urlunsplit((u.scheme, u.netloc, '/ai_bridge_staging_drill', u.query, u.fragment)))")
EVIDENCE_OUT="${AI_BRIDGE_DRILL_EVIDENCE:-.run/postgres_restore_drill_evidence.json}"

echo "== running Release 1 PostgreSQL restore drill (evidence: $EVIDENCE_OUT)"
python3 scripts/backup_restore.py drill \
  --pg-dsn "$DATABASE_URL" \
  --restore-dsn "$RESTORE_DSN" \
  --evidence-out "$EVIDENCE_OUT"

echo "DRILL OK"

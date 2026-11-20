-- Migration 017: Release 2 reliability, task lifecycle, observability, and agent resilience.
-- Adds:
--   * tasks: retry/attempt tracking, structured failure reasons, explicit lease/start/progress,
--     dead-letter flag, and end-to-end trace_id correlation
--   * task_events & audit: trace_id correlation column
--   * devices: agent_version, agent_commit, reported_policy_hash, update_ring, update_state,
--     poll_state_json, spool_stats_json
--   * task_dead_letters: durable dead-letter queue for exhausted tasks with replay linkage
--   * update_rollouts: staged update rollout rings with health-gate pause and rollback state
--   * hot-path & retention indexes

-- Tasks lifecycle & tracing columns
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS next_attempt_at REAL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS failure_reason TEXT DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS failure_class TEXT DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lease_expires_at REAL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_at REAL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_pct REAL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS status_message TEXT DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS dead_lettered INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS trace_id TEXT DEFAULT '';

-- End-to-end correlation columns on task_events and audit
-- @requires-table: task_events
ALTER TABLE task_events ADD COLUMN IF NOT EXISTS trace_id TEXT DEFAULT '';
-- @requires-table: audit
ALTER TABLE audit ADD COLUMN IF NOT EXISTS trace_id TEXT DEFAULT '';

-- Agent resilience, spool visibility, drift, and rollout ring columns on devices
-- @requires-table: devices
ALTER TABLE devices ADD COLUMN IF NOT EXISTS agent_version TEXT DEFAULT '';
-- @requires-table: devices
ALTER TABLE devices ADD COLUMN IF NOT EXISTS agent_commit TEXT DEFAULT '';
-- @requires-table: devices
ALTER TABLE devices ADD COLUMN IF NOT EXISTS reported_policy_hash TEXT DEFAULT '';
-- @requires-table: devices
ALTER TABLE devices ADD COLUMN IF NOT EXISTS update_ring TEXT NOT NULL DEFAULT 'general';
-- @requires-table: devices
ALTER TABLE devices ADD COLUMN IF NOT EXISTS update_state TEXT DEFAULT 'idle';
-- @requires-table: devices
ALTER TABLE devices ADD COLUMN IF NOT EXISTS poll_state_json TEXT NOT NULL DEFAULT '{}';
-- @requires-table: devices
ALTER TABLE devices ADD COLUMN IF NOT EXISTS spool_stats_json TEXT NOT NULL DEFAULT '{}';

-- Dead-letter queue table for exhausted / permanently failed tasks
CREATE TABLE IF NOT EXISTS task_dead_letters (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default',
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT DEFAULT '',
  failure_class TEXT DEFAULT '',
  last_error TEXT DEFAULT '',
  trace_id TEXT DEFAULT '',
  created_at REAL NOT NULL,
  replayed_task_id TEXT,
  replayed_at REAL
);

-- Staged agent update rollout campaigns (canary -> early -> general)
CREATE TABLE IF NOT EXISTS update_rollouts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT 'default',
  target_version TEXT NOT NULL DEFAULT '',
  target_commit TEXT NOT NULL DEFAULT '',
  manifest_url TEXT DEFAULT '',
  current_ring TEXT NOT NULL DEFAULT 'canary',
  rings_json TEXT NOT NULL DEFAULT '["canary","early","general"]',
  status TEXT NOT NULL DEFAULT 'active',
  max_failure_rate REAL NOT NULL DEFAULT 0.25,
  dispatched_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT DEFAULT 'admin',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

-- Hot-path, tracing, and retention indexes
-- @requires-table: tasks
CREATE INDEX IF NOT EXISTS idx_tasks_device_status_created ON tasks(device_id, status, created_at);
-- @requires-table: tasks
CREATE INDEX IF NOT EXISTS idx_tasks_device_tool_status_updated ON tasks(device_id, tool, status, updated_at DESC);
-- @requires-table: tasks
CREATE INDEX IF NOT EXISTS idx_tasks_trace_id ON tasks(trace_id);
-- @requires-table: task_submissions
CREATE INDEX IF NOT EXISTS idx_task_submissions_created ON task_submissions(created_at);
-- @requires-table: task_events
CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at);
-- @requires-table: sessions
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
-- @requires-table: approvals
CREATE INDEX IF NOT EXISTS idx_approvals_status_expires ON approvals(status, expires_at);
-- @requires-table: audit
CREATE INDEX IF NOT EXISTS idx_audit_trace_id ON audit(trace_id);
CREATE INDEX IF NOT EXISTS idx_task_dead_letters_tenant ON task_dead_letters(tenant, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_update_rollouts_tenant ON update_rollouts(tenant, created_at DESC);

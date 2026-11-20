-- Down migration for 017_release2_reliability.sql
DROP INDEX IF EXISTS idx_update_rollouts_tenant;
DROP INDEX IF EXISTS idx_task_dead_letters_tenant;
DROP INDEX IF EXISTS idx_audit_trace_id;
DROP INDEX IF EXISTS idx_approvals_status_expires;
DROP INDEX IF EXISTS idx_sessions_expires;
DROP INDEX IF EXISTS idx_task_events_created;
DROP INDEX IF EXISTS idx_task_submissions_created;
DROP INDEX IF EXISTS idx_tasks_trace_id;
DROP INDEX IF EXISTS idx_tasks_device_tool_status_updated;
DROP INDEX IF EXISTS idx_tasks_device_status_created;
DROP TABLE IF EXISTS update_rollouts;
DROP TABLE IF EXISTS task_dead_letters;
-- @only: postgres
ALTER TABLE devices DROP COLUMN IF EXISTS spool_stats_json;
-- @only: postgres
ALTER TABLE devices DROP COLUMN IF EXISTS poll_state_json;
-- @only: postgres
ALTER TABLE devices DROP COLUMN IF EXISTS update_state;
-- @only: postgres
ALTER TABLE devices DROP COLUMN IF EXISTS update_ring;
-- @only: postgres
ALTER TABLE devices DROP COLUMN IF EXISTS reported_policy_hash;
-- @only: postgres
ALTER TABLE devices DROP COLUMN IF EXISTS agent_commit;
-- @only: postgres
ALTER TABLE devices DROP COLUMN IF EXISTS agent_version;
-- @only: postgres
ALTER TABLE audit DROP COLUMN IF EXISTS trace_id;
-- @only: postgres
ALTER TABLE task_events DROP COLUMN IF EXISTS trace_id;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS trace_id;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS dead_lettered;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS status_message;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS progress_pct;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS started_at;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS lease_expires_at;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS failure_class;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS failure_reason;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS next_attempt_at;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS max_attempts;
-- @only: postgres
ALTER TABLE tasks DROP COLUMN IF EXISTS attempt;

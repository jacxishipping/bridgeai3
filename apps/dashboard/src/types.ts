export type ThemeMode = "dark" | "light";

export type NavSection =
  | "overview"
  | "devices"
  | "desktop"
  | "terminal"
  | "tasks"
  | "workflows"
  | "fleetops"
  | "security"
  | "audit"
  | "connections"
  | "settings";

export interface AuthSession {
  mode: "session" | "admin_token";
  token: string;
  username: string;
  role: string;
  tenant: string;
  controlUrl: string;
}

export interface HealthResponse {
  status: string;
  version?: string;
  emergency_stop?: boolean;
}

export interface ReadinessResponse {
  status: string;
  database?: string;
  emergency_stop?: boolean;
  emergency_stop_durable?: boolean;
  production_checklist?: {
    ok: boolean;
    issues?: Array<{ code: string; detail?: string; severity?: string }>;
  };
}

export interface DeviceRecord {
  id: string;
  name: string;
  hostname?: string;
  os?: string;
  tenant?: string;
  status: string;
  last_seen?: number | null;
  revoked?: number;
  created_at?: number;
  token_expires_at?: number | null;
  group?: string;
  tags: string[];
  has_custom_policy?: boolean;
  policy?: Record<string, unknown>;
  ip_address?: string;
}

export interface DeviceOverview {
  device: DeviceRecord;
  health?: {
    is_online?: boolean;
    status?: string;
    latency_seconds?: number | null;
    last_seen_ts?: number;
  };
  resource_usage?: {
    cpu_percent?: number | null;
    memory_percent?: number | null;
    memory_total_bytes?: number | null;
    memory_available_bytes?: number | null;
    disk_total_bytes?: number | null;
    disk_used_bytes?: number | null;
    disk_free_bytes?: number | null;
    updated_at?: number | null;
  };
  resource_history?: Array<{
    cpu_percent?: number | null;
    memory_percent?: number | null;
    updated_at?: number | null;
  }>;
  system_info?: Record<string, unknown>;
  processes?: Array<Record<string, unknown>>;
  docker?: unknown;
  network?: unknown;
  apps?: {
    allowed_apps?: string[];
    allowed_services?: string[];
    installed_packages?: Array<Record<string, unknown>>;
  };
  recent_tasks?: TaskRecord[];
  pending_approvals?: ApprovalRecord[];
}

export interface TelemetryPoint {
  cpu_percent?: number | null;
  memory_percent?: number | null;
  disk_percent?: number | null;
  recorded_at?: number | null;
}

export interface FleetTelemetrySummary {
  total_devices: number;
  online_devices: number;
  avg_cpu_percent?: number | null;
  avg_memory_percent?: number | null;
  avg_disk_percent?: number | null;
  active_triggers?: number;
  active_schedules?: number;
  devices?: Array<{
    id: string;
    name: string;
    os?: string;
    is_online: boolean;
    last_seen?: number | null;
    telemetry?: TelemetryPoint | null;
  }>;
}

export interface TaskRecord {
  id: string;
  device_id: string;
  tool: string;
  args_json?: string;
  tier?: number;
  status: string;
  result?: string | null;
  created_by?: string;
  created_at?: number;
  updated_at?: number;
  approval_id?: string | null;
  attempt?: number;
  max_attempts?: number;
  next_attempt_at?: number | null;
  failure_reason?: string;
  failure_class?: string;
  progress_pct?: number | null;
  status_message?: string;
  dead_lettered?: number;
  trace_id?: string;
}

export interface DeadLetterRecord {
  id: string;
  task_id: string;
  device_id: string;
  tenant: string;
  tool: string;
  args_json?: string;
  attempts: number;
  failure_reason?: string;
  failure_class?: string;
  last_error?: string;
  trace_id?: string;
  created_at: number;
  replayed_task_id?: string | null;
  replayed_at?: number | null;
}

export interface TaskEventRecord {
  id: string;
  task_id: string;
  event: string;
  detail_json?: string;
  detail?: Record<string, unknown>;
  created_at: number;
  trace_id?: string;
}

export interface TraceTimeline {
  trace_id: string;
  tenant: string;
  tasks: TaskRecord[];
  task_events: TaskEventRecord[];
  approvals: ApprovalRecord[];
  audit: AuditRecord[];
}

export interface RolloutDispatchEntry {
  device_id: string;
  task_id: string | null;
  status: string;
  reason?: string;
}

export interface RolloutRecord {
  id: string;
  name: string;
  tenant: string;
  target_version: string;
  target_commit: string;
  manifest_url?: string;
  current_ring: string;
  rings: string[];
  status: string;
  max_failure_rate: number;
  dispatched: Record<string, RolloutDispatchEntry[]>;
  created_by?: string;
  created_at: number;
  updated_at: number;
  health_gate_failed?: boolean;
  ring_health?: {
    total: number;
    completed: number;
    failed: number;
    pending: number;
    failure_rate: number;
  };
}

export interface FleetHealthDevice {
  id: string;
  name: string;
  os?: string;
  health: string;
  last_seen?: number;
  heartbeat_age_sec?: number | null;
  agent_version?: string;
  agent_commit?: string;
  target_version?: string;
  version_drift?: boolean;
  reported_policy_hash?: string;
  expected_policy_hash?: string;
  policy_drift?: boolean;
  update_ring?: "canary" | "early" | "general" | string;
  update_state?: string;
  poll_state?: {
    ws_connected?: boolean;
    poll_mode?: string;
    backoff_attempts?: number;
    reconnect_count?: number;
    active_task_id?: string | null;
    last_error?: string;
  };
  spool_stats?: {
    pending_count?: number;
    oldest_pending_age_sec?: number;
    total_bytes?: number;
    dead_letter_count?: number;
  };
}

export interface FleetHealthSummary {
  tenant: string;
  ts: number;
  target_version?: string;
  total_devices: number;
  online_devices: number;
  stale_device_count: number;
  spool_pending_total?: number;
  spool_max_age_sec?: number;
  version_drift_count?: number;
  policy_drift_count?: number;
  devices: FleetHealthDevice[];
}

export interface ApprovalRecord {
  id: string;
  task_id?: string | null;
  device_id?: string;
  tool?: string;
  tier?: number;
  status: string;
  requested_by?: string;
  decided_by?: string | null;
  reason?: string | null;
  args_json?: string;
  created_at?: number;
  decided_at?: number | null;
  expires_at?: number | null;
}

export interface PolicyState {
  allow_writes: boolean;
  allow_tier3: boolean;
  allow_file_deletion?: boolean;
  allow_shell?: boolean;
  allow_package_install?: boolean;
  desktop_enabled?: boolean;
  max_tier_unattended: number;
  allowed_apps?: string[];
  note?: string;
}

export interface PolicyHistoryEntry {
  version: number;
  policy_json: string;
  created_by?: string;
  created_at?: number;
  note?: string;
}

export interface AuditRecord {
  id: number;
  ts: number;
  actor: string;
  device_id?: string;
  tool: string;
  args_json?: string;
  decision: string;
  entry_hash?: string;
  prev_hash?: string;
  trace_id?: string;
}

export interface AuditVerifyResult {
  ok?: boolean;
  valid?: boolean;
  checked?: number;
  broken_at?: number | null;
  legacy_rows?: number;
  reason?: string;
}

export interface MonitorDescriptor {
  index: number;
  name: string;
  width: number;
  height: number;
  is_primary?: boolean;
}

export interface EnrollResult {
  device_id: string;
  device_token: string;
  token_expires_at: number;
  token_ttl_days: number;
  note?: string;
}

export interface TerminalSessionSummary {
  session_id: string;
  device_id: string;
  cols: number;
  rows: number;
  is_alive: boolean;
  device_label?: string;
}

export interface WorkflowTemplate {
  id: string;
  name: string;
  description?: string;
  steps: Array<{ tool: string; args?: Record<string, unknown> }>;
  custom?: boolean;
}

export interface WorkflowRecord {
  id: string;
  name: string;
  device_id?: string;
  status: string;
  current_step: number;
  total_steps: number;
  steps: Array<{ tool: string; args?: Record<string, unknown> }>;
  results: Array<Record<string, unknown>>;
  error?: string;
  created_at?: number;
  updated_at?: number;
}

export interface RoleAgentRecord {
  role: string;
  name?: string;
  description?: string;
  enabled?: boolean;
}

export interface ScheduleRecord {
  id: string;
  name: string;
  device_id?: string;
  interval_seconds: number;
  tool: string;
  args?: Record<string, unknown>;
  enabled: number | boolean;
  last_run_at?: number | null;
  next_run_at?: number | null;
}

export interface TriggerRecord {
  id: string;
  name: string;
  device_id?: string;
  metric: string;
  operator: string;
  threshold: number;
  action_type: string;
  action_target: string;
  cooldown_seconds: number;
  enabled: number | boolean;
  last_triggered_at?: number | null;
}

export interface BroadcastBatchStatus {
  batch_id: string;
  status: string;
  total_tasks: number;
  completed: number;
  failed: number;
  tasks: Array<{
    task_id: string;
    device_id: string;
    device_name: string;
    os?: string;
    status: string;
    result?: unknown;
  }>;
}

export interface ConnectionsSummary {
  mcp: { up: boolean; port: number };
  oauth_gateway: { up: boolean; port: number };
  chatgpt: {
    clients: Array<{
      client_id: string;
      client_id_prefix: string;
      client_name: string;
      redirect_uris: string;
      created_at: number;
    }>;
    active_access_tokens: number;
    active_refresh_tokens: number;
    total_tokens: number;
    last_token_at?: number | null;
  };
}

export interface UserRecord {
  username: string;
  role: "viewer" | "operator" | "admin";
  tenant: string;
  created_at?: number;
  disabled?: number | boolean;
}

export interface BackupItem {
  filename: string;
  size_bytes: number;
  created_at: number;
}

export interface AlertConfigState {
  slack_webhook: string;
  discord_webhook: string;
  telegram_chat_id: string;
  telegram_configured?: boolean;
  generic_webhook: string;
  telegram_token?: string;
}

export interface AlertDelivery {
  id: number;
  channel: string;
  event_type: string;
  severity: string;
  title: string;
  ok: number | boolean;
  detail?: string;
  created_at: number;
}

// Release 4 governed capabilities types
export interface FleetTrendPoint {
  date: string;
  total_tasks: number;
  completed_tasks: number;
  failed_tasks: number;
  success_rate: number | null;
  avg_duration_sec: number | null;
  avg_cpu_percent: number | null;
  avg_memory_percent: number | null;
}

export interface FleetTrendsReport {
  schema: string;
  tenant: string;
  group?: string | null;
  days: number;
  summary: {
    total_devices: number;
    online_devices: number;
    online_ratio: number | null;
    window_total_tasks: number;
    window_completed_tasks: number;
    window_failed_tasks: number;
  };
  trends: FleetTrendPoint[];
}

export interface IncidentRecord {
  id: string;
  kind: "failed_task" | "offline_device" | "expired_approval";
  severity: "high" | "medium" | "low";
  title: string;
  detail: string;
  device_id?: string | null;
  task_id?: string | null;
  trace_id?: string | null;
  occurred_at: number;
  status: "open" | "acknowledged" | "resolved";
  assignee?: string | null;
  acknowledged_by?: string | null;
  resolved_by?: string | null;
  note?: string;
}

export interface IncidentInboxResponse {
  tenant: string;
  count: number;
  counts: Record<string, number>;
  items: IncidentRecord[];
}

export interface PolicyBundle {
  schema: string;
  source_version: number;
  exported_by: string;
  exported_at: number;
  policy: PolicyState;
  sha256: string;
  signature_alg: string;
  signature: string;
}

export interface DeviceDesktopCapabilities {
  schema: string;
  device_id: string;
  device_name?: string;
  platform_family: "Windows" | "Linux" | "Darwin" | "Unknown";
  online: boolean;
  revoked: boolean;
  read_only_status_supported: boolean;
  screenshot_supported: boolean;
  webrtc_stream_supported: boolean;
  interactive_input_supported: boolean;
  policy_desktop_enabled: boolean;
  advertised_mode: "interactive" | "view_only" | "unavailable";
  reason: string;
}

export interface MemorySnippetRecord {
  id: string;
  tenant: string;
  workspace: string;
  title: string;
  content: string;
  source: string;
  created_by: string;
  created_at: number;
  read_only: boolean;
  opt_in_prompt: boolean;
}

import type {
  A11yActPayload,
  A11ySnapshotResponse,
  AlertConfigState,
  AlertDelivery,
  ApprovalRecord,
  AuditRecord,
  AuditVerifyResult,
  AuthSession,
  BackupItem,
  BroadcastBatchStatus,
  ConnectionsSummary,
  DeadLetterRecord,
  DeviceDesktopCapabilities,
  DeviceOverview,
  DeviceRecord,
  WebRtcIceServer,
  WebRtcSessionV1,
  EnrollResult,
  FleetHealthSummary,
  FleetTelemetrySummary,
  FleetTrendsReport,
  HealthResponse,
  IncidentInboxResponse,
  MemorySnippetRecord,
  MonitorDescriptor,
  PolicyBundle,
  PolicyHistoryEntry,
  PolicyState,
  ReadinessResponse,
  RoleAgentRecord,
  RolloutRecord,
  ScheduleRecord,
  TaskRecord,
  TelemetryPoint,
  TerminalSessionSummary,
  TraceTimeline,
  TriggerRecord,
  UserRecord,
  WorkflowRecord,
  WorkflowTemplate,
} from "./types";

const SESSION_STORAGE_KEY = "aibridge.session.v1";

export function loadStoredSession(): AuthSession | null {
  try {
    const raw = localStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AuthSession;
    if (!parsed?.token) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveStoredSession(session: AuthSession | null): void {
  try {
    if (!session) {
      localStorage.removeItem(SESSION_STORAGE_KEY);
    } else {
      localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
    }
  } catch {
    // Ignore storage errors
  }
}

export function normalizeBaseUrl(url: string): string {
  const trimmed = url.trim();
  if (!trimmed) return "";
  return trimmed.replace(/\/+$/, "");
}

export class ApiClient {
  private session: AuthSession | null = null;
  private csrfToken: string | null = null;
  /** Legacy admin tenant override for list/read endpoints (`?tenant=`). */
  private tenantFilter: string | null = null;

  constructor(session?: AuthSession | null) {
    this.session = session ?? null;
  }

  setSession(session: AuthSession | null): void {
    this.session = session;
    this.csrfToken = null;
  }

  setTenantFilter(tenant: string | null): void {
    const next = (tenant || "").trim();
    this.tenantFilter = next || null;
  }

  getTenantFilter(): string | null {
    return this.tenantFilter;
  }

  private withTenantQuery(path: string): string {
    if (!this.tenantFilter) return path;
    if (/[?&]tenant=/.test(path)) return path;
    const join = path.includes("?") ? "&" : "?";
    return `${path}${join}tenant=${encodeURIComponent(this.tenantFilter)}`;
  }

  resolveUrl(path: string, customBase?: string): string {
    const base = normalizeBaseUrl(customBase ?? this.session?.controlUrl ?? "");
    return base ? `${base}${path}` : path;
  }

  authHeaders(): Record<string, string> {
    if (!this.session?.token) return {};
    if (this.session.mode === "admin_token") {
      return { "X-Admin-Token": this.session.token };
    }
    return { "X-Session-Token": this.session.token };
  }

  private async ensureCsrfToken(): Promise<string | null> {
    if (!this.session || this.session.mode !== "session") return null;
    if (this.csrfToken) return this.csrfToken;
    const res = await fetch(this.resolveUrl("/api/auth/csrf-token"), {
      headers: { ...this.authHeaders() },
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { csrf_token?: string };
    this.csrfToken = data.csrf_token ?? null;
    return this.csrfToken;
  }

  private async request<T>(
    path: string,
    options: RequestInit & { baseUrlOverride?: string } = {},
  ): Promise<T> {
    const { baseUrlOverride, ...fetchOpts } = options;
    const method = (fetchOpts.method ?? "GET").toUpperCase();
    const isMutating = method !== "GET" && method !== "HEAD" && method !== "OPTIONS";

    const correlationId =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? `ui-${crypto.randomUUID().replace(/-/g, "").slice(0, 16)}`
        : `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

    const headers: Record<string, string> = {
      ...(fetchOpts.body ? { "Content-Type": "application/json" } : {}),
      "X-Correlation-ID": correlationId,
      ...this.authHeaders(),
      ...(fetchOpts.headers as Record<string, string> | undefined),
    };

    if (isMutating && this.session?.mode === "session") {
      const csrf = await this.ensureCsrfToken();
      if (csrf) headers["X-CSRF-Token"] = csrf;
    }

    const resolvedPath = this.withTenantQuery(path);
    let res = await fetch(this.resolveUrl(resolvedPath, baseUrlOverride), {
      ...fetchOpts,
      headers,
    });

    if (res.status === 403 && isMutating && this.session?.mode === "session") {
      this.csrfToken = null;
      const freshCsrf = await this.ensureCsrfToken();
      if (freshCsrf) {
        headers["X-CSRF-Token"] = freshCsrf;
        res = await fetch(this.resolveUrl(resolvedPath, baseUrlOverride), {
          ...fetchOpts,
          headers,
        });
      }
    }

    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const errBody = await res.json();
        detail =
          typeof errBody?.detail === "string"
            ? errBody.detail
            : errBody?.detail?.message ?? JSON.stringify(errBody?.detail ?? errBody);
      } catch {
        // Fallback
      }
      throw new Error(detail);
    }

    const text = await res.text();
    if (!text) return {} as T;
    return JSON.parse(text) as T;
  }

  // Health & Readiness
  async pingHealth(controlUrl = ""): Promise<{ data: HealthResponse; latencyMs: number }> {
    const start = performance.now();
    const data = await this.request<HealthResponse>("/health", { baseUrlOverride: controlUrl });
    return { data, latencyMs: Math.max(1, Math.round(performance.now() - start)) };
  }

  async getReadiness(): Promise<ReadinessResponse> {
    return this.request<ReadinessResponse>("/health/ready");
  }

  async getProductionChecklist(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("/health/production-checklist");
  }

  // Auth
  async loginWithPassword(controlUrl: string, username: string, password: string): Promise<AuthSession> {
    const data = await this.request<{
      session_token: string;
      username: string;
      role: string;
      tenant: string;
    }>("/api/auth/login", {
      method: "POST",
      baseUrlOverride: controlUrl,
      body: JSON.stringify({ username, password }),
    });
    const session: AuthSession = {
      mode: "session",
      token: data.session_token,
      username: data.username,
      role: data.role,
      tenant: data.tenant || "default",
      controlUrl: normalizeBaseUrl(controlUrl),
    };
    this.setSession(session);
    return session;
  }

  async verifyAdminToken(controlUrl: string, adminToken: string): Promise<AuthSession> {
    const tempClient = new ApiClient({
      mode: "admin_token",
      token: adminToken,
      username: "admin",
      role: "admin",
      tenant: "default",
      controlUrl: normalizeBaseUrl(controlUrl),
    });
    const me = await tempClient.request<{ username: string; role: string; tenant: string }>("/api/auth/me");
    const session: AuthSession = {
      mode: "admin_token",
      token: adminToken,
      username: me.username || "admin",
      role: me.role || "admin",
      tenant: me.tenant || "default",
      controlUrl: normalizeBaseUrl(controlUrl),
    };
    this.setSession(session);
    return session;
  }

  async exchangeOidcCode(controlUrl: string, code: string): Promise<AuthSession> {
    const res = await this.request<{ session_token: string }>("/api/auth/oidc/exchange", {
      method: "POST",
      baseUrlOverride: controlUrl,
      body: JSON.stringify({ code }),
    });
    const session: AuthSession = {
      mode: "session",
      token: res.session_token,
      username: "oidc-user",
      role: "viewer",
      tenant: "default",
      controlUrl: normalizeBaseUrl(controlUrl),
    };
    this.setSession(session);
    const me = await this.request<{ username: string; role: string; tenant: string }>("/api/auth/me");
    session.username = me.username;
    session.role = me.role;
    session.tenant = me.tenant || "default";
    return session;
  }

  async isOidcConfigured(controlUrl = ""): Promise<boolean> {
    try {
      const res = await this.request<{ enabled: boolean }>("/api/auth/oidc/configured", {
        baseUrlOverride: controlUrl,
      });
      return Boolean(res.enabled);
    } catch {
      return false;
    }
  }

  async logout(): Promise<void> {
    if (this.session?.mode === "session") {
      try {
        await this.request("/api/auth/logout", { method: "POST" });
      } catch {
        // Ignore
      }
    }
    this.setSession(null);
  }

  // Devices & Telemetry
  async listDevices(): Promise<DeviceRecord[]> {
    return this.request<DeviceRecord[]>("/api/devices");
  }

  async listDevicesPaged(params?: {
    limit?: number;
    offset?: number;
    group?: string;
    os?: string;
    status?: string;
    q?: string;
  }): Promise<{ items: DeviceRecord[]; total: number }> {
    const qs = new URLSearchParams();
    if (params?.limit != null) qs.set("limit", String(params.limit));
    if (params?.offset != null && params.offset > 0) qs.set("offset", String(params.offset));
    if (params?.group) qs.set("group", params.group);
    if (params?.os && params.os !== "all") qs.set("os", params.os);
    if (params?.status && params.status !== "all") qs.set("status", params.status);
    if (params?.q?.trim()) qs.set("q", params.q.trim());
    if (this.tenantFilter) qs.set("tenant", this.tenantFilter);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    const res = await fetch(this.resolveUrl(`/api/devices${suffix}`), {
      headers: { ...this.authHeaders() },
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const items = (await res.json()) as DeviceRecord[];
    const totalHeader = res.headers.get("X-Total-Count");
    const total = totalHeader !== null ? Number(totalHeader) || items.length : items.length;
    return { items, total };
  }

  async enrollDevice(payload: {
    name: string;
    hostname: string;
    os: string;
    policy?: Record<string, unknown>;
  }): Promise<EnrollResult> {
    return this.request<EnrollResult>("/api/devices/enroll", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async revokeDevice(deviceId: string): Promise<{ device_id: string; revoked: boolean }> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/revoke`, { method: "POST" });
  }

  async getDeviceOverview(deviceId: string): Promise<DeviceOverview> {
    return this.request<DeviceOverview>(`/api/devices/${encodeURIComponent(deviceId)}/overview`);
  }

  async refreshDeviceTelemetry(deviceId: string, tools?: string[]): Promise<unknown> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/refresh`, {
      method: "POST",
      body: JSON.stringify(tools ? { tools } : {}),
    });
  }

  async setDeviceMetadata(deviceId: string, group: string, tags: string[]): Promise<{ group: string; tags: string[] }> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/metadata`, {
      method: "PUT",
      body: JSON.stringify({ group, tags }),
    });
  }

  async getDeviceTelemetry(deviceId: string, limit = 30): Promise<TelemetryPoint[]> {
    const res = await this.request<{ telemetry: TelemetryPoint[] }>(
      `/api/devices/${encodeURIComponent(deviceId)}/telemetry?limit=${limit}`,
    );
    return res.telemetry || [];
  }

  async setDevicePolicy(deviceId: string, policy: Record<string, unknown>): Promise<unknown> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/policy`, {
      method: "PUT",
      body: JSON.stringify(policy),
    });
  }

  async resetDevicePolicy(deviceId: string): Promise<unknown> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/policy`, { method: "DELETE" });
  }

  async getFleetTelemetry(): Promise<FleetTelemetrySummary> {
    return this.request<FleetTelemetrySummary>("/api/fleet/telemetry");
  }

  async getFleetHealth(): Promise<FleetHealthSummary> {
    return this.request<FleetHealthSummary>("/api/fleet/health");
  }

  async getDesktopCapabilities(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("/api/fleet/desktop-capabilities");
  }

  // Tasks & Approvals
  async listTasks(deviceId?: string): Promise<TaskRecord[]> {
    const q = deviceId ? `?device_id=${encodeURIComponent(deviceId)}` : "";
    return this.request<TaskRecord[]>(`/api/tasks${q}`);
  }

  async listTasksPaged(params?: {
    deviceId?: string;
    status?: string;
    limit?: number;
    offset?: number;
  }): Promise<{ items: TaskRecord[]; total: number }> {
    const qs = new URLSearchParams();
    if (params?.deviceId) qs.set("device_id", params.deviceId);
    if (params?.status && params.status !== "all") qs.set("status", params.status);
    if (params?.limit != null) qs.set("limit", String(params.limit));
    if (params?.offset != null && params.offset > 0) qs.set("offset", String(params.offset));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    const res = await fetch(this.resolveUrl(`/api/tasks${suffix}`), {
      headers: { ...this.authHeaders() },
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const items = (await res.json()) as TaskRecord[];
    const totalHeader = res.headers.get("X-Total-Count");
    const total = totalHeader !== null ? Number(totalHeader) || items.length : items.length;
    return { items, total };
  }

  async getTaskEvents(taskId: string): Promise<Array<{ id: string; event: string; detail?: Record<string, unknown>; detail_json?: string; created_at: number; trace_id?: string }>> {
    return this.request(`/api/tasks/${encodeURIComponent(taskId)}/events`);
  }

  async getTask(taskId: string): Promise<TaskRecord> {
    return this.request<TaskRecord>(`/api/tasks/${encodeURIComponent(taskId)}`);
  }

  async submitTask(payload: {
    device_id: string;
    tool: string;
    args: Record<string, unknown>;
    max_attempts?: number;
  }): Promise<Record<string, unknown>> {
    return this.request("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
  }

  async cancelTask(taskId: string): Promise<{ task_id: string; status: string }> {
    return this.request(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
  }

  async clearFinishedTasks(): Promise<{ deleted_count: number }> {
    return this.request("/api/tasks", { method: "DELETE" });
  }

  async listApprovals(): Promise<ApprovalRecord[]> {
    return this.request<ApprovalRecord[]>("/api/approvals");
  }

  async decideApproval(approvalId: string, decision: "approved" | "denied", reason?: string): Promise<unknown> {
    return this.request(`/api/approvals/${encodeURIComponent(approvalId)}/decide`, {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    });
  }

  async replanApproval(approvalId: string, reason: string): Promise<unknown> {
    return this.request(`/api/approvals/${encodeURIComponent(approvalId)}/replan`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
  }

  // Policies
  async getPolicy(): Promise<{ version: number; policy: PolicyState; note?: string }> {
    return this.request("/api/policies");
  }

  async getPolicyHistory(): Promise<PolicyHistoryEntry[]> {
    return this.request<PolicyHistoryEntry[]>("/api/policies/history");
  }

  async savePolicy(policy: PolicyState): Promise<{ version: number; policy: PolicyState }> {
    return this.request("/api/policies", { method: "POST", body: JSON.stringify(policy) });
  }

  async simulatePolicy(tool: string, policy?: Partial<PolicyState>): Promise<Record<string, unknown>> {
    return this.request("/api/policy/simulate", {
      method: "POST",
      body: JSON.stringify({ tool, ...(policy ? { policy } : {}) }),
    });
  }

  // Audit & Logs
  async listAudit(tool?: string): Promise<AuditRecord[]> {
    const q = tool ? `?tool=${encodeURIComponent(tool)}` : "";
    return this.request<AuditRecord[]>(`/api/audit${q}`);
  }

  async verifyAuditChain(): Promise<AuditVerifyResult> {
    return this.request<AuditVerifyResult>("/api/audit/verify");
  }

  async exportAudit(): Promise<{ format: string; count: number; events: AuditRecord[] }> {
    return this.request("/api/audit/export");
  }

  async getServiceLogs(service: "control" | "mcp" | "oauth" | "nginx", lines = 120): Promise<{ service: string; lines: string[] }> {
    return this.request(`/api/logs?service=${encodeURIComponent(service)}&lines=${lines}`);
  }

  // Emergency & Webhooks & Alerts
  async setEmergencyStop(active: boolean): Promise<{ emergency_stop: boolean }> {
    return this.request(active ? "/api/emergency-stop" : "/api/emergency-stop/clear", { method: "POST" });
  }

  async getWebhookSetting(): Promise<{ webhook_url: string; configured: boolean }> {
    return this.request("/api/settings/webhook");
  }

  async setWebhookSetting(webhookUrl: string): Promise<{ webhook_url: string; configured: boolean }> {
    return this.request("/api/settings/webhook", { method: "POST", body: JSON.stringify({ webhook_url: webhookUrl }) });
  }

  async testWebhookSetting(): Promise<Record<string, unknown>> {
    return this.request("/api/settings/webhook/test", { method: "POST" });
  }

  async getAlertsConfig(): Promise<AlertConfigState> {
    return this.request<AlertConfigState>("/api/alerts/config");
  }

  async updateAlertsConfig(cfg: AlertConfigState): Promise<{ saved: boolean }> {
    return this.request("/api/alerts/config", { method: "PUT", body: JSON.stringify(cfg) });
  }

  async testAlerts(): Promise<{ results: Record<string, boolean> }> {
    return this.request("/api/alerts/test", { method: "POST" });
  }

  async listAlertDeliveries(): Promise<AlertDelivery[]> {
    return this.request<AlertDelivery[]>("/api/alerts/deliveries");
  }

  // Terminal
  async listTerminalSessions(deviceId: string): Promise<TerminalSessionSummary[]> {
    const res = await this.request<{ sessions: TerminalSessionSummary[] }>(
      `/api/devices/${encodeURIComponent(deviceId)}/terminal/sessions`,
    );
    return res.sessions || [];
  }

  async createTerminalSession(deviceId: string, cols = 110, rows = 30): Promise<TerminalSessionSummary> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/terminal/sessions`, {
      method: "POST",
      body: JSON.stringify({ cols, rows }),
    });
  }

  async sendTerminalInput(deviceId: string, sessionId: string, input: string): Promise<void> {
    await this.request(`/api/devices/${encodeURIComponent(deviceId)}/terminal/${encodeURIComponent(sessionId)}/input`, {
      method: "POST",
      body: JSON.stringify({ input }),
    });
  }

  async closeTerminalSession(deviceId: string, sessionId: string): Promise<void> {
    await this.request(`/api/devices/${encodeURIComponent(deviceId)}/terminal/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    });
  }

  // Workflows & Supervisor Roles
  async listWorkflowTemplates(): Promise<WorkflowTemplate[]> {
    const res = await this.request<{ templates: WorkflowTemplate[] }>("/api/workflows/templates");
    return res.templates || [];
  }

  async createWorkflowTemplate(payload: {
    name: string;
    description?: string;
    steps: Array<Record<string, unknown>>;
    tenant?: string;
  }): Promise<WorkflowTemplate> {
    return this.request("/api/workflows/templates", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async listWorkflows(): Promise<WorkflowRecord[]> {
    return this.request<WorkflowRecord[]>("/api/workflows");
  }

  async submitWorkflow(payload: {
    name: string;
    device_id?: string;
    steps: Array<{ tool: string; args?: Record<string, unknown> }>;
    stop_on_failure?: boolean;
  }): Promise<{ workflow_id: string; status: string }> {
    return this.request("/api/workflows", { method: "POST", body: JSON.stringify(payload) });
  }

  async cancelWorkflow(workflowId: string): Promise<unknown> {
    return this.request(`/api/workflows/${encodeURIComponent(workflowId)}/cancel`, { method: "POST" });
  }

  async retryWorkflow(workflowId: string): Promise<unknown> {
    return this.request(`/api/workflows/${encodeURIComponent(workflowId)}/retry`, {
      method: "POST",
      body: JSON.stringify({ from_failed_step: true }),
    });
  }

  async listRoles(): Promise<{ roles: RoleAgentRecord[]; profiles: string[] }> {
    return this.request("/api/roles");
  }

  async toggleRole(roleId: string, enabled: boolean): Promise<unknown> {
    return this.request(`/api/roles/${encodeURIComponent(roleId)}/enable?enabled=${enabled}`, {
      method: "POST",
    });
  }

  async supervisorHandoff(payload: {
    device_id: string;
    goal: string;
    profile: string;
    mode?: string;
  }): Promise<Record<string, unknown>> {
    return this.request("/api/supervisor/handoff", { method: "POST", body: JSON.stringify(payload) });
  }

  // Fleet Broadcast, Schedules & Triggers
  async broadcastFleet(payload: {
    tool: string;
    args: Record<string, unknown>;
    target_os?: string;
    target_device_ids?: string[];
  }): Promise<{ batch_id: string; device_count: number }> {
    return this.request("/api/fleet/broadcast", { method: "POST", body: JSON.stringify(payload) });
  }

  async getBroadcastStatus(batchId: string): Promise<BroadcastBatchStatus> {
    return this.request(`/api/fleet/broadcast/${encodeURIComponent(batchId)}`);
  }

  async listSchedules(): Promise<ScheduleRecord[]> {
    return this.request<ScheduleRecord[]>("/api/schedules");
  }

  async createSchedule(payload: {
    name: string;
    device_id: string;
    interval_seconds: number;
    tool: string;
    args: Record<string, unknown>;
    enabled?: boolean;
    tenant?: string;
  }): Promise<unknown> {
    return this.request("/api/schedules", { method: "POST", body: JSON.stringify(payload) });
  }

  async toggleSchedule(scheduleId: string): Promise<unknown> {
    return this.request(`/api/schedules/${encodeURIComponent(scheduleId)}/toggle`, { method: "POST" });
  }

  async runScheduleNow(scheduleId: string): Promise<unknown> {
    return this.request(`/api/schedules/${encodeURIComponent(scheduleId)}/run-now`, { method: "POST" });
  }

  async deleteSchedule(scheduleId: string): Promise<unknown> {
    return this.request(`/api/schedules/${encodeURIComponent(scheduleId)}`, { method: "DELETE" });
  }

  async listTriggers(): Promise<TriggerRecord[]> {
    return this.request<TriggerRecord[]>("/api/triggers");
  }

  async createTrigger(payload: {
    name: string;
    device_id: string;
    metric: string;
    operator: string;
    threshold: number;
    action_type: string;
    action_target: string;
    action_args: Record<string, unknown>;
  }): Promise<unknown> {
    return this.request("/api/triggers", { method: "POST", body: JSON.stringify(payload) });
  }

  async toggleTrigger(triggerId: string): Promise<unknown> {
    return this.request(`/api/triggers/${encodeURIComponent(triggerId)}/toggle`, { method: "POST" });
  }

  async testTrigger(triggerId: string): Promise<unknown> {
    return this.request(`/api/triggers/${encodeURIComponent(triggerId)}/test`, { method: "POST" });
  }

  async deleteTrigger(triggerId: string): Promise<unknown> {
    return this.request(`/api/triggers/${encodeURIComponent(triggerId)}`, { method: "DELETE" });
  }

  // Connections, OAuth & MCP
  async getConnections(): Promise<ConnectionsSummary> {
    return this.request<ConnectionsSummary>("/api/connections");
  }

  async deleteOAuthClient(clientId: string): Promise<unknown> {
    return this.request(`/api/connections/clients/${encodeURIComponent(clientId)}`, { method: "DELETE" });
  }

  async getMcpToolPolicies(): Promise<Record<string, unknown>> {
    return this.request("/api/mcp/tool-policies");
  }

  async saveMcpToolPolicies(policies: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.request("/api/mcp/tool-policies", {
      method: "PUT",
      body: JSON.stringify({ policies }),
    });
  }

  async getM365Status(): Promise<Record<string, unknown>> {
    return this.request("/api/connectors/m365/status");
  }

  // Users & Backups
  async listUsers(): Promise<UserRecord[]> {
    return this.request<UserRecord[]>("/api/auth/users");
  }

  async createUser(payload: {
    username: string;
    password: string;
    role: "viewer" | "operator" | "admin";
  }): Promise<unknown> {
    return this.request("/api/auth/users", { method: "POST", body: JSON.stringify(payload) });
  }

  async updateUser(username: string, role: "viewer" | "operator" | "admin", disabled: boolean): Promise<unknown> {
    return this.request(`/api/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: JSON.stringify({ role, disabled }),
    });
  }

  async listBackups(): Promise<{ items: BackupItem[]; backend: string }> {
    return this.request("/api/admin/backups");
  }

  async createBackup(): Promise<BackupItem> {
    return this.request("/api/admin/backups", { method: "POST" });
  }

  async restoreBackup(filename: string): Promise<Record<string, unknown>> {
    return this.request(`/api/admin/backups/${encodeURIComponent(filename)}/restore`, { method: "POST" });
  }

  // Desktop Live Frame & Input
  async getMonitors(deviceId: string): Promise<MonitorDescriptor[]> {
    const res = await this.request<{ monitors: MonitorDescriptor[] }>(
      `/api/devices/${encodeURIComponent(deviceId)}/desktop/monitors`,
    );
    return res.monitors || [];
  }

  async fetchLiveFrameUrl(deviceId: string, scale = 0.75, quality = 75): Promise<string> {
    const res = await fetch(
      this.resolveUrl(
        `/api/devices/${encodeURIComponent(deviceId)}/desktop/live.jpg?scale=${scale}&quality=${quality}&t=${Date.now()}`,
      ),
      { headers: { ...this.authHeaders() } },
    );
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        if (j?.detail) detail = j.detail;
      } catch {
        // Ignore
      }
      throw new Error(detail);
    }
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  }

  async sendDesktopInput(
    deviceId: string,
    payload: {
      action: string;
      x?: number;
      y?: number;
      normalized?: boolean;
      screen_width?: number;
      screen_height?: number;
      button?: string;
      clicks?: number;
      delta?: number;
      text?: string;
      key?: string;
    },
  ): Promise<unknown> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/desktop/input`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async getDesktopA11ySnapshot(
    deviceId: string,
    params?: {
      interactive_only?: boolean;
      skeleton?: boolean;
      compact?: boolean;
      window_title?: string;
      max_depth?: number;
      max_elements?: number;
      session_id?: string;
    },
  ): Promise<A11ySnapshotResponse> {
    const qs = new URLSearchParams();
    if (params?.interactive_only !== undefined) qs.set("interactive_only", String(params.interactive_only));
    if (params?.skeleton !== undefined) qs.set("skeleton", String(params.skeleton));
    if (params?.compact !== undefined) qs.set("compact", String(params.compact));
    if (params?.window_title) qs.set("window_title", params.window_title);
    if (params?.max_depth !== undefined) qs.set("max_depth", String(params.max_depth));
    if (params?.max_elements !== undefined) qs.set("max_elements", String(params.max_elements));
    if (params?.session_id) qs.set("session_id", params.session_id);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return this.request<A11ySnapshotResponse>(
      `/api/devices/${encodeURIComponent(deviceId)}/desktop/a11y/snapshot${suffix}`,
    );
  }

  async actDesktopA11y(
    deviceId: string,
    payload: A11yActPayload,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      `/api/devices/${encodeURIComponent(deviceId)}/desktop/a11y/act`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  }

  // Release 2: Dead-letter queue, end-to-end traces, update rings & staged rollouts
  async listDeadLetterTasks(includeReplayed = false): Promise<{ items: DeadLetterRecord[]; count: number }> {
    return this.request(`/api/tasks/dead-letter?include_replayed=${includeReplayed ? "true" : "false"}`);
  }

  async replayDeadLetterTask(dlqId: string): Promise<Record<string, unknown>> {
    return this.request(`/api/tasks/dead-letter/${encodeURIComponent(dlqId)}/replay`, { method: "POST" });
  }

  async deleteDeadLetterTask(dlqId: string): Promise<{ dlq_id: string; deleted: boolean }> {
    return this.request(`/api/tasks/dead-letter/${encodeURIComponent(dlqId)}`, { method: "DELETE" });
  }

  async getTrace(traceId: string): Promise<TraceTimeline> {
    return this.request<TraceTimeline>(`/api/traces/${encodeURIComponent(traceId)}`);
  }

  async updateDeviceAgent(deviceId: string): Promise<Record<string, unknown>> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/update`, { method: "POST" });
  }

  async setDeviceRing(deviceId: string, ring: "canary" | "early" | "general"): Promise<{ device_id: string; update_ring: string }> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/ring`, {
      method: "PUT",
      body: JSON.stringify({ ring }),
    });
  }

  async listRollouts(): Promise<{ rollouts: RolloutRecord[]; count: number }> {
    return this.request("/api/fleet/rollouts");
  }

  async createRollout(payload: {
    name?: string;
    target_version?: string;
    target_commit?: string;
    manifest_url?: string;
    rings?: string[];
    max_failure_rate?: number;
  }): Promise<RolloutRecord> {
    return this.request<RolloutRecord>("/api/fleet/rollouts", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async advanceRollout(rolloutId: string): Promise<RolloutRecord> {
    return this.request<RolloutRecord>(`/api/fleet/rollouts/${encodeURIComponent(rolloutId)}/advance`, { method: "POST" });
  }

  async pauseRollout(rolloutId: string): Promise<RolloutRecord> {
    return this.request<RolloutRecord>(`/api/fleet/rollouts/${encodeURIComponent(rolloutId)}/pause`, { method: "POST" });
  }

  async resumeRollout(rolloutId: string): Promise<RolloutRecord> {
    return this.request<RolloutRecord>(`/api/fleet/rollouts/${encodeURIComponent(rolloutId)}/resume`, { method: "POST" });
  }

  async rollbackRollout(rolloutId: string): Promise<RolloutRecord> {
    return this.request<RolloutRecord>(`/api/fleet/rollouts/${encodeURIComponent(rolloutId)}/rollback`, { method: "POST" });
  }

  // Release 4: Fleet analytics, incident inbox, policy-as-code, desktop capabilities, governed memory
  async getFleetTrends(days = 14, group?: string): Promise<FleetTrendsReport> {
    const qs = new URLSearchParams({ days: String(days) });
    if (group) qs.set("group", group);
    return this.request<FleetTrendsReport>(`/api/fleet/analytics/trends?${qs.toString()}`);
  }

  async downloadFleetTrendsCsv(days = 14, group?: string): Promise<string> {
    const qs = new URLSearchParams({ days: String(days), format: "csv" });
    if (group) qs.set("group", group);
    const res = await fetch(this.resolveUrl(`/api/fleet/analytics/trends?${qs.toString()}`), {
      headers: { ...this.authHeaders() },
    });
    if (!res.ok) throw new Error(`CSV export failed (HTTP ${res.status})`);
    return res.text();
  }

  async listIncidents(includeResolved = false): Promise<IncidentInboxResponse> {
    return this.request<IncidentInboxResponse>(`/api/incidents?include_resolved=${includeResolved ? "true" : "false"}`);
  }

  async updateIncident(
    incidentId: string,
    payload: { action: "ack" | "assign" | "resolve" | "reopen"; assignee?: string; note?: string },
  ): Promise<Record<string, unknown>> {
    return this.request(`/api/incidents/${encodeURIComponent(incidentId)}/ack`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async exportPolicyBundle(): Promise<PolicyBundle> {
    return this.request<PolicyBundle>("/api/policies/export");
  }

  async importPolicyBundle(bundle: Record<string, unknown>, dryRun = true): Promise<Record<string, unknown>> {
    return this.request("/api/policies/import", {
      method: "POST",
      body: JSON.stringify({ bundle, dry_run: dryRun }),
    });
  }

  async getDeviceDesktopCapabilities(deviceId: string): Promise<DeviceDesktopCapabilities> {
    return this.request<DeviceDesktopCapabilities>(`/api/devices/${encodeURIComponent(deviceId)}/desktop/capabilities`);
  }

  // v1 WebRTC Live Desktop (signaling only — media is peer-to-peer)
  async getWebRtcIceServers(): Promise<WebRtcIceServer[]> {
    const res = await this.request<{ ice_servers: WebRtcIceServer[] }>("/api/v1/sessions/ice-servers");
    return res.ice_servers || [];
  }

  async createWebRtcSession(payload: {
    device_id: string;
    mode?: "view_only" | "human_control" | "ai_assisted";
    transport?: "auto" | "webrtc" | "screen_relay" | "jpeg";
    fps?: number;
    scale?: number;
    monitor_index?: number;
    bitrate_kbps?: number;
    width?: number;
    height?: number;
  }): Promise<WebRtcSessionV1> {
    return this.request<WebRtcSessionV1>("/api/v1/sessions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async failoverSessionTransport(
    sessionId: string,
    payload?: { target_transport?: "webrtc" | "screen_relay" | "jpeg"; reason?: string },
  ): Promise<WebRtcSessionV1> {
    return this.request<WebRtcSessionV1>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/failover`, {
      method: "POST",
      body: JSON.stringify(payload || {}),
    });
  }

  async mintStreamTicket(
    deviceId: string,
    sessionId: string,
  ): Promise<{ ticket: string; device_id: string; session_id: string; expires_in: number }> {
    return this.request("/api/stream-ticket", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId, session_id: sessionId }),
    });
  }

  async buildSessionWsUrl(deviceId: string, sessionId: string): Promise<string> {
    const { ticket } = await this.mintStreamTicket(deviceId, sessionId);
    const httpUrl = this.resolveUrl(`/api/v1/ws/sessions/${encodeURIComponent(sessionId)}?ticket=${encodeURIComponent(ticket)}`);
    if (httpUrl.startsWith("https://")) return `wss://${httpUrl.slice("https://".length)}`;
    if (httpUrl.startsWith("http://")) return `ws://${httpUrl.slice("http://".length)}`;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}${httpUrl}`;
  }

  async getWebRtcSession(sessionId: string): Promise<WebRtcSessionV1> {
    return this.request<WebRtcSessionV1>(`/api/v1/sessions/${encodeURIComponent(sessionId)}`);
  }

  async postWebRtcAnswer(sessionId: string, sdp: string, type = "answer"): Promise<Record<string, unknown>> {
    return this.request(`/api/v1/sessions/${encodeURIComponent(sessionId)}/answer`, {
      method: "POST",
      body: JSON.stringify({ sdp, type }),
    });
  }

  async postWebRtcIce(
    sessionId: string,
    candidate: RTCIceCandidateInit,
    from: "viewer" | "agent" = "viewer",
  ): Promise<Record<string, unknown>> {
    return this.request(`/api/v1/sessions/${encodeURIComponent(sessionId)}/ice`, {
      method: "POST",
      body: JSON.stringify({ from, candidate }),
    });
  }

  async setWebRtcSessionMode(
    sessionId: string,
    mode: "view_only" | "human_control" | "ai_assisted",
  ): Promise<WebRtcSessionV1> {
    return this.request<WebRtcSessionV1>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/mode`, {
      method: "PATCH",
      body: JSON.stringify({ mode }),
    });
  }

  async terminateWebRtcSession(sessionId: string): Promise<WebRtcSessionV1> {
    return this.request<WebRtcSessionV1>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/terminate`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  async sessionClipboard(
    sessionId: string,
    payload: { action: "get" | "set"; text?: string; paste?: boolean },
  ): Promise<{ ok: boolean; action: string; text?: string; length?: number }> {
    return this.request(`/api/v1/sessions/${encodeURIComponent(sessionId)}/clipboard`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async listMemorySnippets(workspace?: string): Promise<{ items: MemorySnippetRecord[]; count: number }> {
    const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
    return this.request(`/api/memory/snippets${q}`);
  }

  async createMemorySnippet(payload: {
    workspace: string;
    title: string;
    content: string;
    source: string;
    opt_in_prompt?: boolean;
  }): Promise<MemorySnippetRecord> {
    return this.request<MemorySnippetRecord>("/api/memory/snippets", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async deleteMemorySnippet(snippetId: string): Promise<{ snippet_id: string; deleted: boolean }> {
    return this.request(`/api/memory/snippets/${encodeURIComponent(snippetId)}`, { method: "DELETE" });
  }

  // 9-Pillar Architectural Expansion APIs
  async getDeviceIntelligence(deviceId: string): Promise<import("./types").DeviceIntelligenceProfile> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/intelligence`);
  }

  async getFleetIntelligence(): Promise<{
    total_devices: number;
    online_devices: number;
    optimal_devices: number;
    fleet_health_score: number;
    total_anomalies: number;
    platform_breakdown: Record<string, number>;
    transport_breakdown: Record<string, number>;
    a11y_backend_breakdown: Record<string, number>;
    devices: Array<import("./types").DeviceIntelligenceProfile>;
  }> {
    return this.request("/api/fleet/intelligence");
  }

  async supervisorPlanGoal(payload: {
    device_id: string;
    goal: string;
    profile?: string;
    target_devices?: string[];
  }): Promise<import("./types").AutonomousPlanResponse> {
    return this.request("/api/supervisor/plan", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async getHandoffStatus(handoffId: string): Promise<Record<string, unknown>> {
    return this.request(`/api/supervisor/handoffs/${encodeURIComponent(handoffId)}`);
  }

  async getDeviceMemories(
    deviceId: string,
    params?: { q?: string; episode_type?: string; limit?: number },
  ): Promise<{ device_id: string; tenant: string; count: number; episodes: Array<import("./types").DeviceMemoryEpisode> }> {
    const qs = new URLSearchParams();
    if (params?.q) qs.set("q", params.q);
    if (params?.episode_type) qs.set("episode_type", params.episode_type);
    if (params?.limit != null) qs.set("limit", String(params.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/memory${suffix}`);
  }

  async learnDeviceMemory(
    deviceId: string,
    payload: {
      episode_type?: string;
      tool?: string;
      context_summary?: string;
      outcome?: string;
      resolution: string;
    },
  ): Promise<import("./types").DeviceMemoryEpisode> {
    return this.request(`/api/devices/${encodeURIComponent(deviceId)}/memory/learn`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async listTenants(): Promise<{ tenants: string[]; items?: Array<import("./types").TenantUsageSummary> }> {
    return this.request("/api/tenants");
  }

  async createOrConfigureTenant(payload: {
    tenant_id: string;
    name?: string;
    plan_tier?: "free" | "pro" | "enterprise";
    status?: "active" | "suspended";
    max_devices?: number;
    max_concurrent_streams?: number;
    max_daily_tasks?: number;
    max_memory_chunks?: number;
    rate_limit_rpm?: number;
  }): Promise<import("./types").TenantUsageSummary> {
    return this.request("/api/tenants", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async getTenantUsage(tenantId: string): Promise<import("./types").TenantUsageSummary> {
    return this.request(`/api/tenants/${encodeURIComponent(tenantId)}/usage`);
  }

  async updateTenantQuotas(
    tenantId: string,
    payload: Partial<{
      name: string;
      plan_tier: "free" | "pro" | "enterprise";
      status: "active" | "suspended";
      max_devices: number;
      max_concurrent_streams: number;
      max_daily_tasks: number;
      max_memory_chunks: number;
      rate_limit_rpm: number;
    }>,
  ): Promise<import("./types").TenantUsageSummary> {
    return this.request(`/api/tenants/${encodeURIComponent(tenantId)}/quotas`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  }
}

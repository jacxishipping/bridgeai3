import { useEffect, useState } from "react";
import { AlertOctagon, Bell, Database, Globe, Moon, Plus, Sun, Users } from "lucide-react";
import type { ApiClient } from "../api";
import type { Locale } from "../i18n";
import type {
  AlertConfigState,
  AlertDelivery,
  AuthSession,
  BackupItem,
  ReadinessResponse,
  TenantUsageSummary,
  ThemeMode,
  UserRecord,
} from "../types";
import { Button, Modal, StatusBadge, SurfaceCard, Toggle } from "../components/ui";

export function SettingsView({
  api,
  session,
  theme,
  locale,
  emergencyStop,
  onToggleTheme,
  onLocaleChange,
  onEmergencyStopChange,
  onLogout,
  onToast,
}: {
  api: ApiClient;
  session: AuthSession;
  theme: ThemeMode;
  locale: Locale;
  emergencyStop: boolean;
  onToggleTheme: () => void;
  onLocaleChange: (next: Locale) => void;
  onEmergencyStopChange: (next: boolean) => Promise<void>;
  onLogout: () => Promise<void>;
  onToast: (msg: string) => void;
}) {
  const [readiness, setReadiness] = useState<ReadinessResponse | null>(null);
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [backups, setBackups] = useState<BackupItem[]>([]);
  const [backupBackend, setBackupBackend] = useState("sqlite");
  const [alerts, setAlerts] = useState<AlertConfigState>({
    slack_webhook: "",
    discord_webhook: "",
    telegram_chat_id: "",
    generic_webhook: "",
  });
  const [deliveries, setDeliveries] = useState<AlertDelivery[]>([]);
  const [tenants, setTenants] = useState<TenantUsageSummary[]>([]);
  const [tenantOpen, setTenantOpen] = useState(false);
  const [newTenantId, setNewTenantId] = useState("");
  const [newTenantName, setNewTenantName] = useState("");
  const [newTenantTier, setNewTenantTier] = useState<"free" | "pro" | "enterprise">("pro");

  // Create User Modal
  const [userOpen, setUserOpen] = useState(false);
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<"viewer" | "operator" | "admin">("operator");

  async function loadAll() {
    const [rd, us, bk, al, dl, tn] = await Promise.all([
      api.getReadiness().catch(() => null),
      api.listUsers().catch(() => []),
      api.listBackups().catch(() => ({ items: [], backend: "sqlite" })),
      api.getAlertsConfig().catch(() => null),
      api.listAlertDeliveries().catch(() => []),
      api.listTenants().catch(() => ({ tenants: ["default"], items: [] })),
    ]);
    setReadiness(rd);
    setUsers(us);
    setBackups(bk.items || []);
    setBackupBackend(bk.backend || "sqlite");
    if (al) setAlerts((prev) => ({ ...prev, ...al }));
    setDeliveries(dl);
    setTenants(tn.items || []);
  }

  useEffect(() => {
    void loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  return (
    <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>
          Admin, RBAC, Backups &amp; Alerts
        </h1>
        <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
          Operator accounts, verified SQLite/Postgres backups, multi-channel alert webhooks, and emergency lockdown.
        </p>
      </div>

      <div className="split-2">
        {/* Session & Appearance */}
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>Operator Session &amp; Theme</span>

          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: "var(--bg-elevated)",
              border: "1px solid var(--border-hair)",
              display: "flex",
              flexDirection: "column",
              gap: 6,
              fontSize: 12,
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-muted)" }}>Principal</span>
              <span className="mono">{session.username}</span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-muted)" }}>Role / Tenant</span>
              <span className="mono">
                {session.role} · {session.tenant}
              </span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-muted)" }}>Auth Mode</span>
              <span className="mono">{session.mode}</span>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ fontSize: 12.5 }}>Color Theme ({theme})</span>
            <Button size="sm" onClick={onToggleTheme}>
              {theme === "dark" ? <Sun size={13} /> : <Moon size={13} />}
              <span>Switch to {theme === "dark" ? "Light" : "Dark"}</span>
            </Button>
          </div>

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5 }}>
              <Globe size={13} /> Language &amp; Direction
            </span>
            <select
              aria-label="Language and direction"
              className="field mono"
              style={{ width: 170 }}
              value={locale}
              onChange={(e) => onLocaleChange(e.target.value === "ar" ? "ar" : "en")}
            >
              <option value="en">English (LTR)</option>
              <option value="ar">العربية (RTL)</option>
            </select>
          </div>

          <div style={{ paddingTop: 6 }}>
            <Button variant="danger" size="sm" onClick={() => void onLogout()}>
              Sign Out
            </Button>
          </div>
        </SurfaceCard>

        {/* Emergency Stop & Readiness */}
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
              <AlertOctagon size={15} style={{ color: "var(--rose)" }} /> Emergency Fleet Lockdown
            </span>
            <StatusBadge
              status={emergencyStop ? "err" : "ok"}
              label={emergencyStop ? "STOPPED" : "Normal"}
            />
          </div>

          <p style={{ margin: 0, fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5 }}>
            Immediately halts task dispatch across all connected agents and broadcasts emergency-stop cancellation.
          </p>

          <Toggle
            checked={emergencyStop}
            onChange={(v) => void onEmergencyStopChange(v)}
            label={emergencyStop ? "Emergency Stop Active (toggle off to resume)" : "Activate Emergency Stop"}
          />

          <div
            style={{
              marginTop: 4,
              paddingTop: 12,
              borderTop: "1px solid var(--border-hair)",
              display: "flex",
              justifyContent: "space-between",
              fontSize: 12,
            }}
          >
            <span style={{ color: "var(--text-muted)" }}>Database / Readiness</span>
            <span className="mono">
              {readiness?.status ?? "unknown"} ({readiness?.database ?? "connected"})
            </span>
          </div>
        </SurfaceCard>
      </div>

      {/* Users & RBAC + Backups */}
      <div className="split-2">
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
              <Users size={15} /> Users &amp; RBAC ({users.length})
            </span>
            <Button size="sm" onClick={() => setUserOpen(true)}>
              <Plus size={12} /> Add User
            </Button>
          </div>
          {users.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              No local user accounts created yet. Create an operator/admin user to enable password logins.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {users.map((u) => (
                <div
                  key={u.username}
                  style={{
                    padding: 10,
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-hair)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                    fontSize: 12,
                  }}
                >
                  <div>
                    <span className="mono" style={{ fontWeight: 600 }}>
                      {u.username}
                    </span>{" "}
                    <span className="mono" style={{ color: "var(--text-muted)" }}>
                      ({u.role})
                    </span>
                  </div>
                  <Toggle
                    checked={!u.disabled}
                    onChange={async (active) => {
                      await api.updateUser(u.username, u.role, !active);
                      onToast(`${u.username} ${active ? "enabled" : "disabled"}`);
                      await loadAll();
                    }}
                    label={u.disabled ? "Disabled" : "Active"}
                  />
                </div>
              ))}
            </div>
          )}
        </SurfaceCard>

        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
              <Database size={15} /> Backups ({backupBackend})
            </span>
            <Button
              size="sm"
              variant="primary"
              onClick={async () => {
                const bk = await api.createBackup();
                onToast(`Created backup ${bk.filename}`);
                await loadAll();
              }}
            >
              <Plus size={12} /> Create Snapshot
            </Button>
          </div>
          {backups.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              No snapshots taken yet.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {backups.slice(0, 6).map((b) => (
                <div
                  key={b.filename}
                  style={{
                    padding: 10,
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-hair)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                    fontSize: 12,
                  }}
                >
                  <div>
                    <div className="mono" style={{ fontSize: 11.5 }}>{b.filename}</div>
                    <div className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
                      {(b.size_bytes / 1024).toFixed(1)} KB
                    </div>
                  </div>
                  <Button
                    size="sm"
                    onClick={async () => {
                      await api.restoreBackup(b.filename);
                      onToast(`Restored ${b.filename}`);
                      await loadAll();
                    }}
                  >
                    Restore
                  </Button>
                </div>
              ))}
            </div>
          )}
        </SurfaceCard>
      </div>

      {/* Multi-Channel Alert Webhooks */}
      <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
          <Bell size={15} /> Multi-Channel Alert Dispatcher
        </div>
        <div className="split-2">
          <div>
            <label style={{ display: "block", fontSize: 11.5, color: "var(--text-muted)", marginBottom: 4 }}>
              Slack Webhook URL
            </label>
            <input
              type="url"
              className="field mono"
              placeholder="https://hooks.slack.com/services/..."
              value={alerts.slack_webhook}
              onChange={(e) => setAlerts((a) => ({ ...a, slack_webhook: e.target.value }))}
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 11.5, color: "var(--text-muted)", marginBottom: 4 }}>
              Discord Webhook URL
            </label>
            <input
              type="url"
              className="field mono"
              placeholder="https://discord.com/api/webhooks/..."
              value={alerts.discord_webhook}
              onChange={(e) => setAlerts((a) => ({ ...a, discord_webhook: e.target.value }))}
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 11.5, color: "var(--text-muted)", marginBottom: 4 }}>
              Telegram Chat ID
            </label>
            <input
              type="text"
              className="field mono"
              placeholder="-100..."
              value={alerts.telegram_chat_id}
              onChange={(e) => setAlerts((a) => ({ ...a, telegram_chat_id: e.target.value }))}
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 11.5, color: "var(--text-muted)", marginBottom: 4 }}>
              Generic / PagerDuty Webhook URL
            </label>
            <input
              type="url"
              className="field mono"
              placeholder="https://..."
              value={alerts.generic_webhook}
              onChange={(e) => setAlerts((a) => ({ ...a, generic_webhook: e.target.value }))}
            />
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Button
            variant="primary"
            size="sm"
            onClick={async () => {
              await api.updateAlertsConfig(alerts);
              onToast("Alert channels saved");
            }}
          >
            Save Channels
          </Button>
          <Button
            size="sm"
            onClick={async () => {
              await api.testAlerts();
              onToast("Dispatched test alert");
              await loadAll();
            }}
          >
            Send Test Alert
          </Button>
        </div>

        {deliveries.length > 0 && (
          <div style={{ fontSize: 11.5, color: "var(--text-muted)" }} className="mono">
            Last delivery: [{deliveries[0].channel}] {deliveries[0].title} —{" "}
            {deliveries[0].ok ? "OK" : deliveries[0].detail || "Failed"}
          </div>
        )}
      </SurfaceCard>

      {/* Pillar 9: Production SaaS Multi-Tenancy, Quotas & Usage Metering */}
      <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
            <Globe size={15} style={{ color: "var(--accent)" }} /> Production SaaS Tenancy &amp; Resource Quotas ({tenants.length || 1})
          </span>
          <Button size="sm" variant="primary" onClick={() => setTenantOpen(true)}>
            <Plus size={12} /> Provision Tenant
          </Button>
        </div>

        {tenants.length === 0 ? (
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Default Enterprise Workspace active. Provision additional tenants to isolate devices, tasks, streams, and memory quotas.
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 10 }}>
            {tenants.map((t) => (
              <div
                key={t.tenant_id}
                style={{
                  padding: 12,
                  borderRadius: 10,
                  background: "var(--bg-elevated)",
                  border: "1px solid var(--border-hair)",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  fontSize: 12,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                  <div>
                    <div style={{ fontWeight: 600 }}>{t.name}</div>
                    <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                      tenant: {t.tenant_id} · {t.quotas.rate_limit_rpm} RPM
                    </div>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <StatusBadge
                      status={t.status === "active" ? "ok" : "err"}
                      label={`${t.plan_tier.toUpperCase()} · ${t.status}`}
                    />
                  </div>
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, fontSize: 11.5 }} className="mono">
                  <div>
                    Devices: {t.usage.active_devices}/{t.quotas.max_devices} ({t.utilization_pct.devices}%)
                  </div>
                  <div>
                    Streams: {t.usage.active_streams}/{t.quotas.max_concurrent_streams}
                  </div>
                  <div>
                    24h Tasks: {t.usage.daily_tasks}/{t.quotas.max_daily_tasks}
                  </div>
                  <div>
                    Memory: {t.usage.memory_chunks}/{t.quotas.max_memory_chunks}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </SurfaceCard>

      {/* Provision SaaS Tenant Modal */}
      <Modal open={tenantOpen} title="Provision / Configure SaaS Tenant" onClose={() => setTenantOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <input
            className="field mono"
            placeholder="Tenant ID (e.g. acme-prod)"
            value={newTenantId}
            onChange={(e) => setNewTenantId(e.target.value)}
          />
          <input
            className="field"
            placeholder="Organization / Workspace Name (e.g. Acme Robotics)"
            value={newTenantName}
            onChange={(e) => setNewTenantName(e.target.value)}
          />
          <select
            className="field mono"
            value={newTenantTier}
            onChange={(e) => setNewTenantTier(e.target.value as "free" | "pro" | "enterprise")}
          >
            <option value="free">free (5 devices · 2 streams · 500 daily tasks)</option>
            <option value="pro">pro (50 devices · 15 streams · 10,000 daily tasks)</option>
            <option value="enterprise">enterprise (10,000 devices · 500 streams · 1M daily tasks)</option>
          </select>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" onClick={() => setTenantOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={!newTenantId.trim()}
              onClick={async () => {
                try {
                  await api.createOrConfigureTenant({
                    tenant_id: newTenantId.trim(),
                    name: newTenantName.trim() || newTenantId.trim(),
                    plan_tier: newTenantTier,
                  });
                  setTenantOpen(false);
                  setNewTenantId("");
                  setNewTenantName("");
                  onToast(`Configured SaaS tenant '${newTenantId.trim()}' (${newTenantTier})`);
                  await loadAll();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Failed to configure tenant");
                }
              }}
            >
              Save Tenant
            </Button>
          </div>
        </div>
      </Modal>

      {/* Add User Modal */}
      <Modal open={userOpen} title="Create Control Plane User" onClose={() => setUserOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <input
            className="field"
            placeholder="Username"
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
          />
          <input
            type="password"
            className="field"
            placeholder="Password (min 8 chars)"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
          />
          <select
            className="field mono"
            value={newRole}
            onChange={(e) => setNewRole(e.target.value as "viewer" | "operator" | "admin")}
          >
            <option value="viewer">viewer (Read-only)</option>
            <option value="operator">operator (Tasks &amp; Desktop)</option>
            <option value="admin">admin (Full Governance)</option>
          </select>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" onClick={() => setUserOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              onClick={async () => {
                try {
                  await api.createUser({ username: newUsername, password: newPassword, role: newRole });
                  setUserOpen(false);
                  setNewUsername("");
                  setNewPassword("");
                  onToast(`Created user ${newUsername}`);
                  await loadAll();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Failed to create user");
                }
              }}
            >
              Create User
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

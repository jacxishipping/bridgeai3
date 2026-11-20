import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Cable,
  Command,
  FileSearch,
  GitBranch,
  ListChecks,
  Menu,
  Monitor,
  Radio,
  RefreshCw,
  Server,
  Settings,
  Shield,
  Terminal,
  Sun,
  Moon,
} from "lucide-react";
import {
  ApiClient,
  loadStoredSession,
  saveStoredSession,
} from "./api";
import type {
  ApprovalRecord,
  AuditRecord,
  AuthSession,
  DeviceRecord,
  FleetTelemetrySummary,
  NavSection,
  TaskRecord,
  ThemeMode,
} from "./types";
import {
  Button,
  DropdownMenu,
  Modal,
  SkeletonCard,
  StateBanner,
  StatusBadge,
  ToastBanner,
} from "./components/ui";
import {
  applyLocaleDocument,
  loadStoredLocale,
  saveStoredLocale,
  t,
  type Locale,
} from "./i18n";
import { SignInView } from "./views/SignInView";
import { OverviewView } from "./views/OverviewView";
import { FleetView } from "./views/FleetView";
import { TerminalView } from "./views/TerminalView";
import { TasksView } from "./views/TasksView";
import { DesktopView } from "./views/DesktopView";
import { WorkflowsView } from "./views/WorkflowsView";
import { FleetOpsView } from "./views/FleetOpsView";
import { SecurityView } from "./views/SecurityView";
import { AuditLogsView } from "./views/AuditLogsView";
import { ConnectionsView } from "./views/ConnectionsView";
import { SettingsView } from "./views/SettingsView";

const NAV_ITEMS: Array<{
  group: string;
  id: NavSection;
  label: string;
  icon: typeof Activity;
}> = [
  { group: "Core", id: "overview", label: "Overview", icon: Activity },
  { group: "Core", id: "devices", label: "Fleet & Device 360", icon: Server },
  { group: "Core", id: "terminal", label: "Live Terminal", icon: Terminal },
  { group: "Core", id: "desktop", label: "Live Desktop", icon: Monitor },
  { group: "Automation", id: "tasks", label: "Task Explorer", icon: ListChecks },
  { group: "Automation", id: "workflows", label: "Workflows & Agents", icon: GitBranch },
  { group: "Automation", id: "fleetops", label: "Fleet Ops & Cron", icon: Radio },
  { group: "Governance", id: "security", label: "Security & Policy", icon: Shield },
  { group: "Governance", id: "audit", label: "Audit & Logs", icon: FileSearch },
  { group: "Platform", id: "connections", label: "Connections & MCP", icon: Cable },
  { group: "Platform", id: "settings", label: "Admin & Settings", icon: Settings },
];

export default function App() {
  const [theme, setTheme] = useState<ThemeMode>(() => {
    const saved = localStorage.getItem("aibridge.theme");
    return saved === "light" ? "light" : "dark";
  });
  const [locale, setLocale] = useState<Locale>(() => loadStoredLocale());
  const [session, setSession] = useState<AuthSession | null>(() => loadStoredSession());
  const api = useMemo(() => new ApiClient(session), [session]);

  const [section, setSection] = useState<NavSection>("overview");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);

  // Live Control Plane State
  const [loading, setLoading] = useState(false);
  const [refreshSpin, setRefreshSpin] = useState(false);
  const [latencyMs, setLatencyMs] = useState<number | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [emergencyStop, setEmergencyStop] = useState(false);
  const [devices, setDevices] = useState<DeviceRecord[]>([]);
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [audit, setAudit] = useState<AuditRecord[]>([]);
  const [telemetry, setTelemetry] = useState<FleetTelemetrySummary | null>(null);
  const [inspectedDevice, setInspectedDevice] = useState<DeviceRecord | null>(null);
  const [inspectedTraceId, setInspectedTraceId] = useState<string>("");

  const showToast = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 2600);
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("aibridge.theme", theme);
  }, [theme]);

  useEffect(() => {
    applyLocaleDocument(locale);
    saveStoredLocale(locale);
  }, [locale]);

  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      showToast(`Switched to ${next} theme`);
      return next;
    });
  }, [showToast]);

  const refreshAll = useCallback(
    async (initial = false) => {
      if (!session) return;
      if (initial) setLoading(true);
      setRefreshSpin(true);
      try {
        const [healthRes, devList, taskList, approvalList, auditList, fleetTel] =
          await Promise.all([
            api.pingHealth(session.controlUrl),
            api.listDevices(),
            api.listTasks(),
            api.listApprovals().catch(() => [] as ApprovalRecord[]),
            api.listAudit().catch(() => [] as AuditRecord[]),
            api.getFleetTelemetry().catch(() => null),
          ]);
        setLatencyMs(healthRes.latencyMs);
        setPollError(null);
        setEmergencyStop(Boolean(healthRes.data.emergency_stop));
        setDevices(devList);
        setTasks(taskList);
        setApprovals(approvalList);
        setAudit(auditList);
        setTelemetry(fleetTel);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Refresh error";
        if (msg.includes("401") || msg.toLowerCase().includes("session") || msg.toLowerCase().includes("token")) {
          saveStoredSession(null);
          setSession(null);
          showToast("Session expired — please sign in again");
        } else {
          setPollError(msg);
          showToast(msg);
        }
      } finally {
        setLoading(false);
        window.setTimeout(() => setRefreshSpin(false), 380);
      }
    },
    [api, session, showToast],
  );

  useEffect(() => {
    if (!session) return;
    void refreshAll(true);
    const id = window.setInterval(() => {
      void refreshAll(false);
    }, 12000);
    return () => window.clearInterval(id);
  }, [session, refreshAll]);

  // Ctrl+K / Cmd+K Quick Jump Shortcut
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((open) => !open);
        setPaletteQuery("");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!session) {
    return (
      <>
        <SignInView
          api={api}
          theme={theme}
          onToggleTheme={toggleTheme}
          onSignedIn={(next) => {
            saveStoredSession(next);
            setSession(next);
            showToast(`Signed in as ${next.username}`);
          }}
          onToast={showToast}
        />
        <ToastBanner message={toast} />
      </>
    );
  }

  const activeNav = NAV_ITEMS.find((n) => n.id === section) ?? NAV_ITEMS[0];
  const groups = Array.from(new Set(NAV_ITEMS.map((i) => i.group)));
  const pendingCount = approvals.filter((a) => a.status === "pending").length;

  const paletteCommands = [
    ...NAV_ITEMS.map((i) => ({
      id: `nav-${i.id}`,
      label: `Go to ${i.label}`,
      meta: i.group,
      run: () => setSection(i.id),
    })),
    {
      id: "act-theme",
      label: `Switch to ${theme === "dark" ? "Light" : "Dark"} Theme`,
      meta: "Appearance",
      run: toggleTheme,
    },
    {
      id: "act-refresh",
      label: "Refresh Fleet Telemetry",
      meta: "Action",
      run: () => void refreshAll(false),
    },
  ].filter(
    (c) =>
      !paletteQuery.trim() ||
      c.label.toLowerCase().includes(paletteQuery.toLowerCase()) ||
      c.meta.toLowerCase().includes(paletteQuery.toLowerCase()),
  );

  return (
    <div className="shell">
      <div
        className={`mobile-sidebar-backdrop ${mobileNavOpen ? "open" : ""}`}
        onClick={() => setMobileNavOpen(false)}
      />

      <aside className={`sidebar ${mobileNavOpen ? "mobile-open" : ""}`}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "2px 8px 16px",
          }}
        >
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 8,
              background: "var(--accent)",
              color: "#fff",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 600,
              fontSize: 12,
              flexShrink: 0,
            }}
          >
            AI
          </div>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.2 }}>AI Bridge</div>
            <div className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
              Control Plane v0.3.0
            </div>
          </div>
        </div>

        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 14 }}>
          {groups.map((grp) => (
            <div key={grp}>
              <div className="nav-group-label">{t(locale, `nav.group.${grp}`, grp)}</div>
              <nav style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                {NAV_ITEMS.filter((n) => n.group === grp).map((item) => {
                  const Icon = item.icon;
                  const active = item.id === section;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className={`nav-item ${active ? "active" : ""}`}
                      onClick={() => {
                        setSection(item.id);
                        setMobileNavOpen(false);
                      }}
                    >
                      <Icon size={15} />
                      <span>{t(locale, `nav.${item.id}`, item.label)}</span>
                      {item.id === "security" && pendingCount > 0 && (
                        <span
                          className="mono"
                          style={{
                            marginInlineStart: "auto",
                            fontSize: 10.5,
                            padding: "1px 6px",
                            borderRadius: 999,
                            background: "rgba(245,158,11,0.16)",
                            color: "var(--amber)",
                          }}
                        >
                          {pendingCount}
                        </span>
                      )}
                    </button>
                  );
                })}
              </nav>
            </div>
          ))}
        </div>

        <div
          className="surface surface-lift"
          style={{ padding: 12, marginTop: 14, cursor: "pointer" }}
          onClick={() => {
            setSection("settings");
            setMobileNavOpen(false);
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 4,
            }}
          >
            <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>
              {t(locale, "status.controlLink")}
            </span>
            <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {latencyMs !== null ? `${latencyMs} ms` : "—"}
            </span>
          </div>
          <StatusBadge
            status={emergencyStop ? "err" : pollError ? "err" : latencyMs !== null ? "ok" : "warn"}
            label={
              emergencyStop
                ? t(locale, "status.lockdown")
                : pollError
                  ? t(locale, "status.degraded")
                  : latencyMs !== null
                    ? t(locale, "status.connected")
                    : t(locale, "status.connecting")
            }
          />
        </div>
      </aside>

      <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        <header className="topbar">
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0, flex: 1 }}>
            <Button
              size="icon"
              className="mobile-menu-btn"
              aria-label="Open menu"
              onClick={() => setMobileNavOpen(true)}
            >
              <Menu size={16} />
            </Button>

            <nav
              style={{
                display: "flex",
                alignItems: "center",
                gap: 7,
                fontSize: 12,
                color: "var(--text-muted)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                flex: 1,
                minWidth: 0,
              }}
            >
              <span className="breadcrumb-prefix">AI Bridge</span>
              <span className="breadcrumb-prefix">/</span>
              <span className="breadcrumb-prefix">{activeNav.group}</span>
              <span className="breadcrumb-prefix">/</span>
              <span
                style={{
                  fontWeight: 500,
                  color: "var(--text-primary)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {activeNav.label}
              </span>
            </nav>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
            <Button
              size="sm"
              onClick={() => {
                setPaletteQuery("");
                setPaletteOpen(true);
              }}
            >
              <Command size={13} />
              <span className="mono cmd-kbd" style={{ fontSize: 11 }}>
                Ctrl+K
              </span>
            </Button>

            <Button size="icon" title="Refresh telemetry" onClick={() => void refreshAll(false)}>
              <RefreshCw
                size={14}
                style={{
                  transition: "transform 0.4s ease",
                  transform: refreshSpin ? "rotate(360deg)" : "rotate(0deg)",
                }}
              />
            </Button>

            <Button size="icon" title="Toggle light / dark" onClick={toggleTheme}>
              {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
            </Button>

            <DropdownMenu
              label={session.username}
              items={[
                { label: "Fleet & Device 360", shortcut: "G F", onClick: () => setSection("devices") },
                { label: "Live Terminal", shortcut: "G T", onClick: () => setSection("terminal") },
                { label: "Security & Policy", shortcut: "G S", onClick: () => setSection("security") },
                { type: "divider" },
                {
                  label: emergencyStop ? "Clear Emergency Stop" : "Emergency Stop",
                  danger: !emergencyStop,
                  onClick: async () => {
                    try {
                      const res = await api.setEmergencyStop(!emergencyStop);
                      setEmergencyStop(res.emergency_stop);
                      showToast(res.emergency_stop ? "Emergency stop engaged" : "Emergency stop cleared");
                    } catch (err) {
                      showToast(err instanceof Error ? err.message : "Failed");
                    }
                  },
                },
                {
                  label: "Sign Out",
                  danger: true,
                  onClick: async () => {
                    await api.logout();
                    saveStoredSession(null);
                    setSession(null);
                  },
                },
              ]}
            />
          </div>
        </header>

        <main className="main-content">
          {pollError && (
            <div style={{ marginBottom: 16 }}>
              <StateBanner
                kind="offline"
                title={t(locale, "state.offlineTitle")}
                detail={`${t(locale, "state.offlineDetail")} (${pollError})`}
                action={
                  <Button size="sm" onClick={() => void refreshAll(false)}>
                    Retry now
                  </Button>
                }
              />
            </div>
          )}
          {loading ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <SkeletonCard lines={2} />
              <div className="split-2">
                <SkeletonCard lines={4} />
                <SkeletonCard lines={4} />
              </div>
            </div>
          ) : section === "overview" ? (
            <OverviewView
              api={api}
              devices={devices}
              tasks={tasks}
              audit={audit}
              telemetry={telemetry}
              latencyMs={latencyMs}
              emergencyStop={emergencyStop}
              onNavigate={setSection}
              onInspectDevice={(dev) => {
                setInspectedDevice(dev);
                setSection("devices");
              }}
              onToast={showToast}
            />
          ) : section === "devices" ? (
            <FleetView
              api={api}
              devices={devices}
              selectedDevice={inspectedDevice}
              onSelectDevice={setInspectedDevice}
              onRefresh={() => refreshAll(false)}
              onToast={showToast}
            />
          ) : section === "terminal" ? (
            <TerminalView api={api} devices={devices} onToast={showToast} />
          ) : section === "desktop" ? (
            <DesktopView api={api} devices={devices} onToast={showToast} />
          ) : section === "tasks" ? (
            <TasksView
              api={api}
              tasks={tasks}
              devices={devices}
              onRefresh={() => refreshAll(false)}
              onToast={showToast}
              onInspectTrace={(traceId) => {
                setInspectedTraceId(traceId);
                setSection("audit");
              }}
            />
          ) : section === "workflows" ? (
            <WorkflowsView api={api} devices={devices} onToast={showToast} />
          ) : section === "fleetops" ? (
            <FleetOpsView api={api} devices={devices} onToast={showToast} />
          ) : section === "security" ? (
            <SecurityView
              api={api}
              approvals={approvals}
              audit={audit}
              devices={devices}
              onRefresh={() => refreshAll(false)}
              onToast={showToast}
            />
          ) : section === "audit" ? (
            <AuditLogsView
              api={api}
              audit={audit}
              initialTraceId={inspectedTraceId}
              onToast={showToast}
            />
          ) : section === "connections" ? (
            <ConnectionsView api={api} onToast={showToast} />
          ) : (
            <SettingsView
              api={api}
              session={session}
              theme={theme}
              locale={locale}
              emergencyStop={emergencyStop}
              onToggleTheme={toggleTheme}
              onLocaleChange={(next) => {
                setLocale(next);
                showToast(next === "ar" ? "Switched to Arabic (RTL)" : "Switched to English (LTR)");
              }}
              onEmergencyStopChange={async (next) => {
                const res = await api.setEmergencyStop(next);
                setEmergencyStop(res.emergency_stop);
                showToast(res.emergency_stop ? "Emergency stop engaged" : "Emergency stop cleared");
              }}
              onLogout={async () => {
                await api.logout();
                saveStoredSession(null);
                setSession(null);
              }}
              onToast={showToast}
            />
          )}
        </main>
      </div>

      <Modal
        open={paletteOpen}
        title="Quick Jump (Ctrl+K)"
        onClose={() => setPaletteOpen(false)}
        maxWidth={440}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <input
            type="text"
            className="field"
            autoFocus
            placeholder="Jump to view or action..."
            value={paletteQuery}
            onChange={(e) => setPaletteQuery(e.target.value)}
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 3, maxHeight: 340, overflowY: "auto" }}>
            {paletteCommands.map((cmd) => (
              <button
                key={cmd.id}
                type="button"
                className="nav-item"
                onClick={() => {
                  setPaletteOpen(false);
                  cmd.run();
                }}
              >
                <span style={{ flex: 1 }}>{cmd.label}</span>
                <span className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
                  {cmd.meta}
                </span>
              </button>
            ))}
          </div>
        </div>
      </Modal>

      <ToastBanner message={toast} />
    </div>
  );
}

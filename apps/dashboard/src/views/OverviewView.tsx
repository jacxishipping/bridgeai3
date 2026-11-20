import { useMemo } from "react";
import { Activity, Cpu, HardDrive, Monitor, Terminal } from "lucide-react";
import type {
  AuditRecord,
  DeviceRecord,
  FleetTelemetrySummary,
  NavSection,
  TaskRecord,
} from "../types";
import {
  Button,
  Sparkline,
  StateBanner,
  StatusBadge,
  SurfaceCard,
  TimelineFeed,
} from "../components/ui";

export function OverviewView({
  devices,
  tasks,
  audit,
  telemetry,
  latencyMs,
  emergencyStop,
  onNavigate,
  onInspectDevice,
}: {
  devices: DeviceRecord[];
  tasks: TaskRecord[];
  audit: AuditRecord[];
  telemetry: FleetTelemetrySummary | null;
  latencyMs: number | null;
  emergencyStop: boolean;
  onNavigate: (sec: NavSection) => void;
  onInspectDevice: (device: DeviceRecord) => void;
}) {
  const onlineDevices = devices.filter((d) => d.status === "online" && !d.revoked).length;
  const pendingOrRunning = tasks.filter(
    (t) => t.status === "queued" || t.status === "running" || t.status === "needs_approval",
  ).length;
  const failedTasks = tasks.filter((t) => t.status === "failed").length;

  const avgCpu = telemetry?.avg_cpu_percent;
  const avgMem = telemetry?.avg_memory_percent;

  // Release 3 truthfulness: only plot real per-device telemetry readings — never invent placeholder curves.
  const cpuSeries = useMemo(() => {
    const samples = (telemetry?.devices ?? [])
      .map((d) => d.telemetry?.cpu_percent)
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    return samples;
  }, [telemetry]);

  const memSeries = useMemo(() => {
    const samples = (telemetry?.devices ?? [])
      .map((d) => d.telemetry?.memory_percent)
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    return samples;
  }, [telemetry]);

  const reportingNodes = useMemo(
    () =>
      (telemetry?.devices ?? []).filter(
        (d) => typeof d.telemetry?.cpu_percent === "number" || typeof d.telemetry?.memory_percent === "number",
      ).length,
    [telemetry],
  );

  const timelineItems = audit.slice(0, 5).map((a) => ({
    id: a.id,
    title: `${a.tool || "event"} · ${a.decision}`,
    meta: `${new Date(a.ts * 1000).toLocaleTimeString()} · ${a.actor || "system"}${
      a.device_id ? ` · ${a.device_id}` : ""
    }`,
    color:
      a.decision === "allowed" || a.decision === "executed"
        ? "var(--emerald)"
        : a.decision === "denied"
          ? "var(--rose)"
          : "var(--accent)",
  }));

  return (
    <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>
            Fleet Overview
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Real-time control plane telemetry, active nodes, and recent execution stream.
          </p>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <StatusBadge
            status={reportingNodes > 0 ? "ok" : "muted"}
            label={
              reportingNodes > 0
                ? `Live telemetry · ${reportingNodes} node${reportingNodes === 1 ? "" : "s"}`
                : "No live telemetry yet"
            }
          />
        </div>
      </div>

      {devices.length === 0 && (
        <StateBanner
          kind="empty"
          title="No endpoints enrolled yet"
          detail="Live fleet telemetry and task execution appear here once you enroll at least one Windows, Linux, or macOS agent."
          action={
            <Button size="sm" variant="primary" onClick={() => onNavigate("devices")}>
              Enroll First Node
            </Button>
          }
        />
      )}

      {/* KPI Row */}
      <div className="kpi-grid">
        <SurfaceCard lift style={{ padding: 16 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              fontSize: 12,
              color: "var(--text-muted)",
              marginBottom: 8,
            }}
          >
            <span>Enrolled Nodes</span>
            <Monitor size={14} />
          </div>
          <div className="mono tnum" style={{ fontSize: 22, fontWeight: 600 }}>
            {onlineDevices}
            <span style={{ fontSize: 13, color: "var(--text-muted)", fontWeight: 400 }}>
              {" "}
              / {devices.length} online
            </span>
          </div>
        </SurfaceCard>

        <SurfaceCard lift style={{ padding: 16 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              fontSize: 12,
              color: "var(--text-muted)",
              marginBottom: 8,
            }}
          >
            <span>Control Plane Latency</span>
            <span className="pulse-dot" />
          </div>
          <div className="mono tnum status-ok" style={{ fontSize: 22, fontWeight: 600 }}>
            {latencyMs !== null ? `${latencyMs} ms` : "—"}
          </div>
        </SurfaceCard>

        <SurfaceCard lift style={{ padding: 16 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              fontSize: 12,
              color: "var(--text-muted)",
              marginBottom: 8,
            }}
          >
            <span>Active / Queued Tasks</span>
            <Activity size={14} />
          </div>
          <div className="mono tnum" style={{ fontSize: 22, fontWeight: 600 }}>
            {pendingOrRunning}
            <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 400 }}>
              {" "}
              ({failedTasks} failed)
            </span>
          </div>
        </SurfaceCard>

        <SurfaceCard lift style={{ padding: 16 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              fontSize: 12,
              color: "var(--text-muted)",
              marginBottom: 8,
            }}
          >
            <span>Global Guardrail State</span>
            <HardDrive size={14} />
          </div>
          <div style={{ paddingTop: 4 }}>
            <StatusBadge
              status={emergencyStop ? "err" : "ok"}
              label={emergencyStop ? "LOCKDOWN ACTIVE" : "Armed & Enforcing"}
            />
          </div>
        </SurfaceCard>
      </div>

      {/* Sparklines Row */}
      <div className="split-2">
        <SurfaceCard lift style={{ padding: 18 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12.5,
                fontWeight: 500,
                color: "var(--text-secondary)",
              }}
            >
              <Cpu size={14} /> Fleet CPU Across Reporting Nodes
            </span>
            <span className="mono status-ok" style={{ fontSize: 12.5, fontWeight: 600 }}>
              {typeof avgCpu === "number" ? `${avgCpu.toFixed(1)}% avg` : "No telemetry"}
            </span>
          </div>
          <Sparkline
            values={cpuSeries}
            stroke="#6366F1"
            emptyLabel="Requires ≥2 nodes reporting CPU telemetry"
          />
        </SurfaceCard>

        <SurfaceCard lift style={{ padding: 18 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12.5,
                fontWeight: 500,
                color: "var(--text-secondary)",
              }}
            >
              <HardDrive size={14} /> Fleet Memory Across Reporting Nodes
            </span>
            <span className="mono" style={{ fontSize: 12.5, fontWeight: 600, color: "var(--emerald)" }}>
              {typeof avgMem === "number" ? `${avgMem.toFixed(1)}% avg` : "No telemetry"}
            </span>
          </div>
          <Sparkline
            values={memSeries}
            stroke="#10B981"
            emptyLabel="Requires ≥2 nodes reporting memory telemetry"
          />
        </SurfaceCard>
      </div>

      {/* Bottom Split: Node List + Activity Timeline */}
      <div className="split-wide">
        <SurfaceCard style={{ overflow: "hidden" }}>
          <div
            style={{
              padding: "14px 16px",
              borderBottom: "1px solid var(--border-hair)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>Workstations &amp; Agents</span>
            <Button size="sm" variant="ghost" onClick={() => onNavigate("devices")}>
              Manage Fleet
            </Button>
          </div>
          <div className="table-wrap">
            <table className="saas-table">
              <thead>
                <tr>
                  <th>Node</th>
                  <th>OS</th>
                  <th>Status</th>
                  <th>IP Address</th>
                </tr>
              </thead>
              <tbody>
                {devices.length === 0 ? (
                  <tr>
                    <td colSpan={4} style={{ color: "var(--text-muted)", textAlign: "center", padding: 24 }}>
                      No devices enrolled yet. Enroll a node from Fleet &amp; Nodes.
                    </td>
                  </tr>
                ) : (
                  devices.slice(0, 6).map((dev) => {
                    const st = dev.revoked
                      ? "err"
                      : dev.status === "online"
                        ? "ok"
                        : "muted";
                    return (
                      <tr
                        key={dev.id}
                        style={{ cursor: "pointer" }}
                        onClick={() => onInspectDevice(dev)}
                      >
                        <td>
                          <div style={{ fontWeight: 500 }}>{dev.name}</div>
                          <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                            {dev.id}
                          </div>
                        </td>
                        <td>{dev.os || "—"}</td>
                        <td>
                          <StatusBadge
                            status={st}
                            label={dev.revoked ? "Revoked" : dev.status || "offline"}
                          />
                        </td>
                        <td className="mono">{dev.ip_address || "—"}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </SurfaceCard>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <SurfaceCard style={{ padding: 16 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: 14,
              }}
            >
              <span style={{ fontSize: 13.5, fontWeight: 600 }}>Audit Activity</span>
              <Button size="sm" variant="ghost" onClick={() => onNavigate("security")}>
                View Ledger
              </Button>
            </div>
            <TimelineFeed items={timelineItems} />
          </SurfaceCard>

          <div className="terminal">
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                marginBottom: 8,
                color: "#94a3b8",
                fontSize: 11,
              }}
            >
              <Terminal size={12} /> Recent Task Stream
            </div>
            {tasks.slice(0, 4).map((t) => (
              <div key={t.id} style={{ marginBottom: 4 }}>
                <span style={{ color: "#64748b" }}>[{t.id.slice(0, 8)}]</span>{" "}
                <span
                  style={{
                    color:
                      t.status === "completed"
                        ? "#34d399"
                        : t.status === "failed"
                          ? "#f87171"
                          : "#fbbf24",
                  }}
                >
                  [{t.status.toUpperCase()}]
                </span>{" "}
                {t.tool} ({t.device_id})
              </div>
            ))}
            {tasks.length === 0 && (
              <div style={{ color: "#64748b" }}>Waiting for task dispatches...</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

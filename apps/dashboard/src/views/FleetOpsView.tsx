import { useEffect, useState } from "react";
import { Clock, Play, Plus, Radio, Trash2, Zap } from "lucide-react";
import type { ApiClient } from "../api";
import type { BroadcastBatchStatus, DeviceRecord, ScheduleRecord, TriggerRecord } from "../types";
import { Button, Chip, Modal, StatusBadge, SurfaceCard, Toggle } from "../components/ui";

export function FleetOpsView({
  api,
  devices,
  onToast,
}: {
  api: ApiClient;
  devices: DeviceRecord[];
  onToast: (msg: string) => void;
}) {
  const [broadcastTool, setBroadcastTool] = useState("get_resource_usage");
  const [broadcastOs, setBroadcastOs] = useState("");
  const [broadcastBatch, setBroadcastBatch] = useState<BroadcastBatchStatus | null>(null);
  const [broadcastBusy, setBroadcastBusy] = useState(false);

  const [schedules, setSchedules] = useState<ScheduleRecord[]>([]);
  const [triggers, setTriggers] = useState<TriggerRecord[]>([]);

  // Create Schedule Modal
  const [schedOpen, setSchedOpen] = useState(false);
  const [schedName, setSchedName] = useState("Hourly Telemetry");
  const [schedInterval, setSchedInterval] = useState("3600");
  const [schedTool, setSchedTool] = useState("get_resource_usage");
  const [schedDevice, setSchedDevice] = useState(devices[0]?.id ?? "");

  // Create Trigger Modal
  const [trigOpen, setTrigOpen] = useState(false);
  const [trigName, setTrigName] = useState("High CPU Alert");
  const [trigMetric, setTrigMetric] = useState("cpu_percent");
  const [trigThreshold, setTrigThreshold] = useState("90");
  const [trigAction, setTrigAction] = useState("list_processes");

  async function loadAll() {
    const [sc, tr] = await Promise.all([
      api.listSchedules().catch(() => []),
      api.listTriggers().catch(() => []),
    ]);
    setSchedules(sc);
    setTriggers(tr);
  }

  useEffect(() => {
    void loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  return (
    <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>
          Fleet Operations, Cron &amp; Self-Healing
        </h1>
        <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
          Broadcast commands across enrolled nodes, manage scheduled cron jobs, and configure reactive triggers.
        </p>
      </div>

      {/* Multi-Node Broadcast Bus */}
      <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
          <Radio size={15} style={{ color: "var(--accent)" }} /> Multi-Node Command Broadcast
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          <input
            type="text"
            className="field mono"
            style={{ flex: "1 1 200px" }}
            value={broadcastTool}
            onChange={(e) => setBroadcastTool(e.target.value)}
            placeholder="Tool name (e.g. get_system_info, get_resource_usage)"
          />
          <input
            type="text"
            className="field mono"
            style={{ width: 170 }}
            value={broadcastOs}
            onChange={(e) => setBroadcastOs(e.target.value)}
            placeholder="Filter OS (optional)"
          />
          <Button
            variant="primary"
            disabled={broadcastBusy}
            onClick={async () => {
              setBroadcastBusy(true);
              try {
                const res = await api.broadcastFleet({
                  tool: broadcastTool.trim(),
                  args: {},
                  ...(broadcastOs.trim() ? { target_os: broadcastOs.trim() } : {}),
                });
                const st = await api.getBroadcastStatus(res.batch_id);
                setBroadcastBatch(st);
                onToast(`Broadcast queued on ${res.device_count} node(s)`);
              } catch (err) {
                onToast(err instanceof Error ? err.message : "Broadcast failed");
              } finally {
                setBroadcastBusy(false);
              }
            }}
          >
            {broadcastBusy ? "Broadcasting..." : "Broadcast to Fleet"}
          </Button>
        </div>

        {broadcastBatch && (
          <div
            style={{
              padding: 12,
              borderRadius: 8,
              background: "var(--bg-elevated)",
              border: "1px solid var(--border-hair)",
              fontSize: 12,
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
              <span className="mono">
                Batch {broadcastBatch.batch_id} ({broadcastBatch.completed}/{broadcastBatch.total_tasks} complete)
              </span>
              <Chip
                onClick={async () => {
                  const st = await api.getBroadcastStatus(broadcastBatch.batch_id);
                  setBroadcastBatch(st);
                }}
              >
                Refresh Status
              </Chip>
            </div>
            {broadcastBatch.tasks.map((t) => (
              <div key={t.task_id} className="mono" style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                • {t.device_name} ({t.os || "?"}) → {t.status}
              </div>
            ))}
          </div>
        )}
      </SurfaceCard>

      {/* Schedules + Triggers */}
      <div className="split-2">
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
              <Clock size={15} /> Scheduled Cron Jobs
            </span>
            <Button size="sm" onClick={() => setSchedOpen(true)}>
              <Plus size={12} /> Add Schedule
            </Button>
          </div>

          {schedules.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              No recurring schedules configured.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {schedules.map((sc) => (
                <div
                  key={sc.id}
                  style={{
                    padding: 12,
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-hair)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                  }}
                >
                  <div>
                    <div style={{ fontSize: 12.5, fontWeight: 600 }}>{sc.name}</div>
                    <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                      every {sc.interval_seconds}s · {sc.tool} ({sc.device_id || "all"})
                    </div>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <Toggle
                      checked={Boolean(sc.enabled)}
                      onChange={async () => {
                        await api.toggleSchedule(sc.id);
                        await loadAll();
                      }}
                    />
                    <Button
                      size="sm"
                      onClick={async () => {
                        await api.runScheduleNow(sc.id);
                        onToast(`Triggered ${sc.name}`);
                      }}
                    >
                      <Play size={11} />
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={async () => {
                        await api.deleteSchedule(sc.id);
                        await loadAll();
                      }}
                    >
                      <Trash2 size={11} />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </SurfaceCard>

        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
              <Zap size={15} /> Self-Healing Metric Triggers
            </span>
            <Button size="sm" onClick={() => setTrigOpen(true)}>
              <Plus size={12} /> Add Trigger
            </Button>
          </div>

          {triggers.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              No automated remediation triggers configured.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {triggers.map((tr) => (
                <div
                  key={tr.id}
                  style={{
                    padding: 12,
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-hair)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                  }}
                >
                  <div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{ fontSize: 12.5, fontWeight: 600 }}>{tr.name}</span>
                      <StatusBadge
                        status={tr.enabled ? "ok" : "muted"}
                        label={tr.enabled ? "Armed" : "Off"}
                      />
                    </div>
                    <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                      if {tr.metric} {tr.operator} {tr.threshold} → {tr.action_type}:{tr.action_target}
                    </div>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <Toggle
                      checked={Boolean(tr.enabled)}
                      onChange={async () => {
                        await api.toggleTrigger(tr.id);
                        await loadAll();
                      }}
                    />
                    <Button
                      size="sm"
                      onClick={async () => {
                        await api.testTrigger(tr.id);
                        onToast(`Simulated trigger ${tr.name}`);
                      }}
                    >
                      Test
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={async () => {
                        await api.deleteTrigger(tr.id);
                        await loadAll();
                      }}
                    >
                      <Trash2 size={11} />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </SurfaceCard>
      </div>

      {/* Add Schedule Modal */}
      <Modal open={schedOpen} title="Create Scheduled Job" onClose={() => setSchedOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <input className="field" value={schedName} onChange={(e) => setSchedName(e.target.value)} placeholder="Job Name" />
          <input className="field mono" value={schedTool} onChange={(e) => setSchedTool(e.target.value)} placeholder="Tool" />
          <input className="field mono" value={schedInterval} onChange={(e) => setSchedInterval(e.target.value)} placeholder="Interval seconds" />
          <select className="field mono" value={schedDevice} onChange={(e) => setSchedDevice(e.target.value)}>
            <option value="">All Devices / Control Plane</option>
            {devices.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name} ({d.id})
              </option>
            ))}
          </select>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" onClick={() => setSchedOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              onClick={async () => {
                await api.createSchedule({
                  name: schedName,
                  device_id: schedDevice,
                  interval_seconds: Math.max(10, Number(schedInterval) || 3600),
                  tool: schedTool,
                  args: {},
                });
                setSchedOpen(false);
                onToast("Schedule created");
                await loadAll();
              }}
            >
              Save Schedule
            </Button>
          </div>
        </div>
      </Modal>

      {/* Add Trigger Modal */}
      <Modal open={trigOpen} title="Create Self-Healing Trigger" onClose={() => setTrigOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <input className="field" value={trigName} onChange={(e) => setTrigName(e.target.value)} placeholder="Trigger Name" />
          <div className="split-2">
            <select className="field mono" value={trigMetric} onChange={(e) => setTrigMetric(e.target.value)}>
              <option value="cpu_percent">cpu_percent</option>
              <option value="memory_percent">memory_percent</option>
              <option value="disk_percent">disk_percent</option>
            </select>
            <input className="field mono" value={trigThreshold} onChange={(e) => setTrigThreshold(e.target.value)} placeholder="Threshold >=" />
          </div>
          <input className="field mono" value={trigAction} onChange={(e) => setTrigAction(e.target.value)} placeholder="Remediation tool" />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" onClick={() => setTrigOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              onClick={async () => {
                await api.createTrigger({
                  name: trigName,
                  device_id: devices[0]?.id ?? "",
                  metric: trigMetric,
                  operator: ">",
                  threshold: Number(trigThreshold) || 90,
                  action_type: "tool",
                  action_target: trigAction,
                  action_args: {},
                });
                setTrigOpen(false);
                onToast("Trigger armed");
                await loadAll();
              }}
            >
              Save Trigger
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

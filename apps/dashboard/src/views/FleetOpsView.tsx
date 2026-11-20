import { useEffect, useState } from "react";
import { Clock, FastForward, Layers, Pause, Play, Plus, Radio, RefreshCw, RotateCcw, Trash2, Zap } from "lucide-react";
import type { ApiClient } from "../api";
import type {
  BroadcastBatchStatus,
  DeviceRecord,
  FleetHealthSummary,
  RolloutRecord,
  ScheduleRecord,
  TriggerRecord,
} from "../types";
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
  const [fleetHealth, setFleetHealth] = useState<FleetHealthSummary | null>(null);
  const [rollouts, setRollouts] = useState<RolloutRecord[]>([]);
  const [rolloutBusyId, setRolloutBusyId] = useState<string | null>(null);

  // Create Rollout Modal (Release 2)
  const [rolloutOpen, setRolloutOpen] = useState(false);
  const [rolloutName, setRolloutName] = useState("Staged Agent Update");
  const [rolloutVersion, setRolloutVersion] = useState("0.3.0");
  const [rolloutCommit, setRolloutCommit] = useState("");
  const [rolloutMaxFail, setRolloutMaxFail] = useState("0.25");

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
    const [sc, tr, fh, rl] = await Promise.all([
      api.listSchedules().catch(() => []),
      api.listTriggers().catch(() => []),
      api.getFleetHealth().catch(() => null),
      api.listRollouts().catch(() => ({ rollouts: [], count: 0 })),
    ]);
    setSchedules(sc);
    setTriggers(tr);
    setFleetHealth(fh);
    setRollouts(rl.rollouts || []);
  }

  function rolloutStatusKind(st: string): "ok" | "warn" | "err" | "muted" {
    if (st === "completed") return "ok";
    if (st === "active" || st === "paused") return "warn";
    if (st === "rolled_back" || st === "failed") return "err";
    return "muted";
  }

  useEffect(() => {
    void loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  return (
    <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>
            Fleet Operations, Rollout Rings &amp; Self-Healing
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Manage staged update rings, monitor agent spool/drift resilience, broadcast commands, and run schedules &amp; triggers.
          </p>
        </div>

      {/* Release 2: Staged Update Rollout Rings & Agent Resilience */}
      <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
            <Layers size={15} style={{ color: "var(--accent)" }} /> Staged Update Rollout Rings &amp; Agent Resilience
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Button size="sm" onClick={() => void loadAll()}>
              <RefreshCw size={12} /> Refresh
            </Button>
            <Button size="sm" variant="primary" onClick={() => setRolloutOpen(true)}>
              <Plus size={12} /> New Staged Rollout
            </Button>
          </div>
        </div>

        {fleetHealth && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <StatusBadge
              status="ok"
              label={`Online: ${fleetHealth.online_devices}/${fleetHealth.total_devices}`}
            />
            <StatusBadge
              status={(fleetHealth.version_drift_count ?? 0) > 0 ? "warn" : "ok"}
              label={`Version Drift: ${fleetHealth.version_drift_count ?? 0} (target ${fleetHealth.target_version || "0.3.0"})`}
            />
            <StatusBadge
              status={(fleetHealth.policy_drift_count ?? 0) > 0 ? "warn" : "ok"}
              label={`Policy Drift: ${fleetHealth.policy_drift_count ?? 0}`}
            />
            <StatusBadge
              status={(fleetHealth.spool_pending_total ?? 0) > 0 ? "warn" : "muted"}
              label={`Offline Spool: ${fleetHealth.spool_pending_total ?? 0} pending (max age ${Math.round(fleetHealth.spool_max_age_sec ?? 0)}s)`}
            />
          </div>
        )}

        {/* Active / Recent Rollout Campaigns */}
        {rollouts.length === 0 ? (
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            No staged update rollouts yet. Assign devices to <span className="mono">canary</span>, <span className="mono">early</span>, or <span className="mono">general</span> below, then launch a rollout.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {rollouts.map((rol) => (
              <div
                key={rol.id}
                style={{
                  padding: 12,
                  borderRadius: 8,
                  background: "var(--bg-elevated)",
                  border: "1px solid var(--border-hair)",
                  display: "flex",
                  flexWrap: "wrap",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 10,
                }}
              >
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>{rol.name}</span>
                    <StatusBadge status={rolloutStatusKind(rol.status)} label={rol.status} />
                    <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                      ring: <strong>{rol.current_ring}</strong> ({rol.rings.join(" → ")})
                    </span>
                  </div>
                  <div className="mono" style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 3 }}>
                    {rol.id} · target v{rol.target_version || "?"}{rol.target_commit ? ` @ ${rol.target_commit.slice(0, 10)}` : ""} · fail gate ≤ {Math.round(rol.max_failure_rate * 100)}%
                  </div>
                </div>

                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
                  {rol.status === "active" && (
                    <>
                      <Button
                        size="sm"
                        variant="primary"
                        disabled={rolloutBusyId === rol.id}
                        onClick={async () => {
                          setRolloutBusyId(rol.id);
                          try {
                            const next = await api.advanceRollout(rol.id);
                            onToast(
                              next.health_gate_failed
                                ? `Health gate tripped — rollout ${rol.name} auto-paused`
                                : `Advanced ${rol.name} → ${next.current_ring} (${next.status})`,
                            );
                            await loadAll();
                          } catch (err) {
                            onToast(err instanceof Error ? err.message : "Advance failed");
                          } finally {
                            setRolloutBusyId(null);
                          }
                        }}
                      >
                        <FastForward size={11} /> Advance Ring
                      </Button>
                      <Button
                        size="sm"
                        disabled={rolloutBusyId === rol.id}
                        onClick={async () => {
                          setRolloutBusyId(rol.id);
                          try {
                            await api.pauseRollout(rol.id);
                            onToast(`Paused rollout ${rol.name}`);
                            await loadAll();
                          } finally {
                            setRolloutBusyId(null);
                          }
                        }}
                      >
                        <Pause size={11} /> Pause
                      </Button>
                    </>
                  )}
                  {rol.status === "paused" && (
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={rolloutBusyId === rol.id}
                      onClick={async () => {
                        setRolloutBusyId(rol.id);
                        try {
                          await api.resumeRollout(rol.id);
                          onToast(`Resumed rollout ${rol.name}`);
                          await loadAll();
                        } finally {
                          setRolloutBusyId(null);
                        }
                      }}
                    >
                      <Play size={11} /> Resume
                    </Button>
                  )}
                  {rol.status !== "rolled_back" && (
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={rolloutBusyId === rol.id}
                      onClick={async () => {
                        setRolloutBusyId(rol.id);
                        try {
                          await api.rollbackRollout(rol.id);
                          onToast(`Rollback dispatched for ${rol.name}`);
                          await loadAll();
                        } catch (err) {
                          onToast(err instanceof Error ? err.message : "Rollback failed");
                        } finally {
                          setRolloutBusyId(null);
                        }
                      }}
                    >
                      <RotateCcw size={11} /> Rollback
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Per-Device Ring Assignment, Drift & Spool Table */}
        {fleetHealth && fleetHealth.devices.length > 0 && (
          <div className="table-wrap">
            <table className="saas-table">
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Update Ring</th>
                  <th>Agent Version / Drift</th>
                  <th>Poll State</th>
                  <th>Offline Spool</th>
                  <th>Update State</th>
                </tr>
              </thead>
              <tbody>
                {fleetHealth.devices.map((dev) => (
                  <tr key={dev.id}>
                    <td>
                      <div style={{ fontWeight: 500 }}>{dev.name}</div>
                      <div className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>{dev.id}</div>
                    </td>
                    <td>
                      <select
                        className="field mono"
                        style={{ padding: "4px 8px", fontSize: 11.5, width: 110 }}
                        value={dev.update_ring || "general"}
                        onChange={async (e) => {
                          const ring = e.target.value as "canary" | "early" | "general";
                          try {
                            await api.setDeviceRing(dev.id, ring);
                            onToast(`Assigned ${dev.name} to ${ring} ring`);
                            await loadAll();
                          } catch (err) {
                            onToast(err instanceof Error ? err.message : "Ring update requires admin");
                          }
                        }}
                      >
                        <option value="canary">canary</option>
                        <option value="early">early</option>
                        <option value="general">general</option>
                      </select>
                    </td>
                    <td>
                      <span className="mono" style={{ fontSize: 11.5 }}>
                        {dev.agent_version || "—"}
                      </span>
                      {dev.version_drift && (
                        <span style={{ marginLeft: 6 }}>
                          <StatusBadge status="warn" label="ver drift" />
                        </span>
                      )}
                      {dev.policy_drift && (
                        <span style={{ marginLeft: 6 }}>
                          <StatusBadge status="err" label="policy drift" />
                        </span>
                      )}
                    </td>
                    <td className="mono" style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                      {dev.poll_state?.poll_mode || dev.health}
                      {(dev.poll_state?.backoff_attempts ?? 0) > 0 && ` (backoff ×${dev.poll_state?.backoff_attempts})`}
                    </td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {dev.spool_stats?.pending_count ?? 0} pending
                      {(dev.spool_stats?.oldest_pending_age_sec ?? 0) > 0 && ` · ${Math.round(dev.spool_stats!.oldest_pending_age_sec!)}s`}
                      {(dev.spool_stats?.dead_letter_count ?? 0) > 0 && ` · ${dev.spool_stats!.dead_letter_count} DLQ`}
                    </td>
                    <td>
                      <StatusBadge
                        status={dev.update_state === "updating" || dev.update_state === "rolling_back" ? "warn" : "muted"}
                        label={dev.update_state || "idle"}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SurfaceCard>

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

      {/* Create Staged Rollout Modal */}
      <Modal open={rolloutOpen} title="Launch Staged Update Rollout" onClose={() => setRolloutOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <input
            className="field"
            value={rolloutName}
            onChange={(e) => setRolloutName(e.target.value)}
            placeholder="Rollout Name (e.g. v0.3.1 Security Release)"
          />
          <div className="split-2">
            <input
              className="field mono"
              value={rolloutVersion}
              onChange={(e) => setRolloutVersion(e.target.value)}
              placeholder="Target Version (e.g. 0.3.1)"
            />
            <input
              className="field mono"
              value={rolloutMaxFail}
              onChange={(e) => setRolloutMaxFail(e.target.value)}
              placeholder="Max Failure Rate (0.25 = 25%)"
            />
          </div>
          <input
            className="field mono"
            value={rolloutCommit}
            onChange={(e) => setRolloutCommit(e.target.value)}
            placeholder="Target Signed Git Commit SHA (optional)"
          />
          <div style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
            Starts immediately on the <span className="mono">canary</span> ring, then promotes through <span className="mono">early</span> → <span className="mono">general</span> with health-gate auto-pause.
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" onClick={() => setRolloutOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              onClick={async () => {
                try {
                  const res = await api.createRollout({
                    name: rolloutName.trim() || "Staged Rollout",
                    target_version: rolloutVersion.trim(),
                    target_commit: rolloutCommit.trim(),
                    rings: ["canary", "early", "general"],
                    max_failure_rate: Math.max(0, Math.min(1, Number(rolloutMaxFail) || 0.25)),
                  });
                  setRolloutOpen(false);
                  onToast(`Started rollout ${res.name} on ${res.current_ring} ring`);
                  await loadAll();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Failed to start rollout");
                }
              }}
            >
              Start Canary Ring
            </Button>
          </div>
        </div>
      </Modal>

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

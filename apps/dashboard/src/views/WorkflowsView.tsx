import { useEffect, useState } from "react";
import { GitBranch, Play, RotateCcw, Square, Users } from "lucide-react";
import type { ApiClient } from "../api";
import type { DeviceRecord, RoleAgentRecord, WorkflowRecord, WorkflowTemplate } from "../types";
import { Button, Chip, Modal, StatusBadge, SurfaceCard, Toggle } from "../components/ui";

export function WorkflowsView({
  api,
  devices,
  onToast,
}: {
  api: ApiClient;
  devices: DeviceRecord[];
  onToast: (msg: string) => void;
}) {
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowRecord[]>([]);
  const [roles, setRoles] = useState<RoleAgentRecord[]>([]);
  const [profiles, setProfiles] = useState<string[]>(["balanced", "fast", "thorough"]);

  // Supervisor Handoff State
  const [handoffDevice, setHandoffDevice] = useState(devices[0]?.id ?? "local");
  const [handoffProfile, setHandoffProfile] = useState("balanced");
  const [handoffGoal, setHandoffGoal] = useState("");
  const [handoffBusy, setHandoffBusy] = useState(false);

  // Launch Workflow Modal
  const [launchOpen, setLaunchOpen] = useState(false);
  const [wfName, setWfName] = useState("Diagnostic Sweep");
  const [wfDevice, setWfDevice] = useState(devices[0]?.id ?? "local");
  const [wfStepsJson, setWfStepsJson] = useState(
    JSON.stringify(
      [
        { tool: "get_system_info", args: {} },
        { tool: "get_resource_usage", args: {} },
      ],
      null,
      2,
    ),
  );

  async function loadAll() {
    try {
      const [tpls, wfs, rl] = await Promise.all([
        api.listWorkflowTemplates().catch(() => []),
        api.listWorkflows().catch(() => []),
        api.listRoles().catch(() => ({ roles: [], profiles: ["balanced"] })),
      ]);
      setTemplates(tpls);
      setWorkflows(wfs);
      setRoles(rl.roles || []);
      if (rl.profiles?.length) setProfiles(rl.profiles);
    } catch {
      // Ignore partial fetch error
    }
  }

  useEffect(() => {
    void loadAll();
    const t = window.setInterval(() => void loadAll(), 5000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  function statusKind(s: string): "ok" | "warn" | "err" | "muted" {
    if (s === "completed") return "ok";
    if (s === "running" || s === "pending" || s === "waiting_approval") return "warn";
    if (s === "failed") return "err";
    return "muted";
  }

  return (
    <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
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
            Autonomous Workflows &amp; Role Agents
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Execute multi-step playbooks, inspect DAG step progression, and delegate goals to role agents.
          </p>
        </div>

        <Button variant="primary" onClick={() => setLaunchOpen(true)}>
          <Play size={13} /> Run Workflow
        </Button>
      </div>

      {/* Supervisor Handoff + Role Catalog */}
      <div className="split-2">
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
            <GitBranch size={15} style={{ color: "var(--accent)" }} /> Supervisor Goal Handoff
          </div>
          <div className="split-2">
            <select
              className="field mono"
              value={handoffDevice}
              onChange={(e) => setHandoffDevice(e.target.value)}
            >
              <option value="local">local</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.id})
                </option>
              ))}
            </select>
            <select
              className="field mono"
              value={handoffProfile}
              onChange={(e) => setHandoffProfile(e.target.value)}
            >
              {profiles.map((p) => (
                <option key={p} value={p}>
                  profile: {p}
                </option>
              ))}
            </select>
          </div>
          <textarea
            rows={2}
            className="field"
            placeholder="Describe goal (e.g., Inspect disk usage, capture logs, and verify service health)..."
            value={handoffGoal}
            onChange={(e) => setHandoffGoal(e.target.value)}
          />
          <div>
            <Button
              variant="primary"
              size="sm"
              disabled={handoffBusy || !handoffGoal.trim()}
              onClick={async () => {
                setHandoffBusy(true);
                try {
                  await api.supervisorHandoff({
                    device_id: handoffDevice,
                    goal: handoffGoal.trim(),
                    profile: handoffProfile,
                  });
                  setHandoffGoal("");
                  onToast("Supervisor handoff enqueued");
                  await loadAll();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Handoff failed");
                } finally {
                  setHandoffBusy(false);
                }
              }}
            >
              {handoffBusy ? "Dispatching..." : "Dispatch Goal Handoff"}
            </Button>
          </div>
        </SurfaceCard>

        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
            <Users size={15} /> Autonomous Role Catalog
          </div>
          {roles.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              No specialized roles registered.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {roles.slice(0, 5).map((r) => (
                <div
                  key={r.role}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 10,
                    fontSize: 12.5,
                  }}
                >
                  <div>
                    <span className="mono" style={{ fontWeight: 600 }}>
                      {r.name || r.role}
                    </span>
                    {r.description && (
                      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                        {r.description}
                      </div>
                    )}
                  </div>
                  <Toggle
                    checked={Boolean(r.enabled)}
                    onChange={async (next) => {
                      await api.toggleRole(r.role, next);
                      onToast(`Role ${r.role} ${next ? "enabled" : "disabled"}`);
                      await loadAll();
                    }}
                  />
                </div>
              ))}
            </div>
          )}
        </SurfaceCard>
      </div>

      {/* Playbook Templates */}
      {templates.length > 0 && (
        <SurfaceCard style={{ padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>
            Workflow Templates ({templates.length})
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {templates.map((tpl) => (
              <Chip
                key={tpl.id}
                onClick={() => {
                  setWfName(tpl.name);
                  setWfStepsJson(JSON.stringify(tpl.steps || [], null, 2));
                  setLaunchOpen(true);
                }}
              >
                {tpl.name} ({tpl.steps?.length ?? 0} steps)
              </Chip>
            ))}
          </div>
        </SurfaceCard>
      )}

      {/* Active & Historical Workflows Table */}
      <SurfaceCard style={{ overflow: "hidden" }}>
        <div
          style={{
            padding: "12px 16px",
            borderBottom: "1px solid var(--border-hair)",
            fontSize: 13.5,
            fontWeight: 600,
          }}
        >
          Workflow Runs
        </div>
        <div className="table-wrap">
          <table className="saas-table">
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Node</th>
                <th>Step Progress</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {workflows.length === 0 ? (
                <tr>
                  <td colSpan={5} style={{ textAlign: "center", padding: 26, color: "var(--text-muted)" }}>
                    No workflow executions yet. Launch one from a template or custom JSON.
                  </td>
                </tr>
              ) : (
                workflows.map((wf) => {
                  const pct = wf.total_steps > 0 ? Math.round((wf.current_step / wf.total_steps) * 100) : 0;
                  return (
                    <tr key={wf.id}>
                      <td>
                        <div style={{ fontWeight: 500 }}>{wf.name}</div>
                        <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                          {wf.id}
                        </div>
                      </td>
                      <td className="mono">{wf.device_id || "local"}</td>
                      <td style={{ minWidth: 160 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <div
                            style={{
                              flex: 1,
                              height: 6,
                              borderRadius: 999,
                              background: "var(--bg-elevated)",
                              overflow: "hidden",
                            }}
                          >
                            <div
                              style={{
                                height: "100%",
                                width: `${pct}%`,
                                background:
                                  wf.status === "failed" ? "var(--rose)" : "var(--accent)",
                              }}
                            />
                          </div>
                          <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                            {wf.current_step}/{wf.total_steps}
                          </span>
                        </div>
                      </td>
                      <td>
                        <StatusBadge status={statusKind(wf.status)} label={wf.status} />
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 6 }}>
                          {(wf.status === "running" || wf.status === "pending") && (
                            <Button
                              size="sm"
                              variant="danger"
                              onClick={async () => {
                                await api.cancelWorkflow(wf.id);
                                onToast(`Cancelled ${wf.id}`);
                                await loadAll();
                              }}
                            >
                              <Square size={11} /> Cancel
                            </Button>
                          )}
                          {(wf.status === "failed" || wf.status === "cancelled") && (
                            <Button
                              size="sm"
                              onClick={async () => {
                                await api.retryWorkflow(wf.id);
                                onToast(`Retrying ${wf.id}`);
                                await loadAll();
                              }}
                            >
                              <RotateCcw size={11} /> Retry
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </SurfaceCard>

      <Modal open={launchOpen} title="Launch Multi-Step Workflow" onClose={() => setLaunchOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 4 }}>
              Workflow Name
            </label>
            <input type="text" className="field" value={wfName} onChange={(e) => setWfName(e.target.value)} />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 4 }}>
              Target Device
            </label>
            <select className="field mono" value={wfDevice} onChange={(e) => setWfDevice(e.target.value)}>
              <option value="local">local</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.id})
                </option>
              ))}
            </select>
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 4 }}>
              Steps (JSON Array)
            </label>
            <textarea
              rows={6}
              className="field mono"
              value={wfStepsJson}
              onChange={(e) => setWfStepsJson(e.target.value)}
            />
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" onClick={() => setLaunchOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={async () => {
                try {
                  const steps = JSON.parse(wfStepsJson) as Array<{ tool: string; args?: Record<string, unknown> }>;
                  await api.submitWorkflow({ name: wfName, device_id: wfDevice, steps });
                  setLaunchOpen(false);
                  onToast(`Workflow '${wfName}' started`);
                  await loadAll();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Invalid workflow JSON");
                }
              }}
            >
              Launch Workflow
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

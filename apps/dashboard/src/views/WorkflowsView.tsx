import { useEffect, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  BookmarkPlus,
  Brain,
  CalendarClock,
  Code2,
  Cpu,
  Eye,
  GitBranch,
  Layers,
  Play,
  Plus,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
  Users,
} from "lucide-react";
import type { ApiClient } from "../api";
import type {
  AutonomousPlanResponse,
  DeviceIntelligenceProfile,
  DeviceMemoryEpisode,
  DeviceRecord,
  RoleAgentRecord,
  WorkflowDagStep,
  WorkflowRecord,
  WorkflowTemplate,
} from "../types";
import { Button, Chip, Modal, SlideOverDrawer, StatusBadge, SurfaceCard, Toggle } from "../components/ui";

const TOOL_CATALOG: Array<{ category: string; tool: string; tier: number; defaultArgs: Record<string, unknown> }> = [
  { category: "System & Telemetry", tool: "get_system_info", tier: 1, defaultArgs: {} },
  { category: "System & Telemetry", tool: "get_resource_usage", tier: 1, defaultArgs: {} },
  { category: "System & Telemetry", tool: "get_disk_usage", tier: 1, defaultArgs: {} },
  { category: "System & Telemetry", tool: "get_network_info", tier: 1, defaultArgs: {} },
  { category: "System & Telemetry", tool: "list_processes", tier: 1, defaultArgs: { limit: 25 } },
  { category: "Desktop & A11y", tool: "get_desktop_status", tier: 1, defaultArgs: {} },
  { category: "Desktop & A11y", tool: "list_desktop_windows", tier: 1, defaultArgs: {} },
  { category: "Desktop & A11y", tool: "desktop_a11y_snapshot", tier: 1, defaultArgs: { interactive_only: true, max_elements: 80 } },
  { category: "Desktop & A11y", tool: "desktop_a11y_act", tier: 2, defaultArgs: { ref: "@e1", action: "click", headed: false } },
  { category: "Desktop & A11y", tool: "take_screenshot", tier: 1, defaultArgs: { quality: 80 } },
  { category: "Desktop & A11y", tool: "wait_for_text", tier: 1, defaultArgs: { text: "Ready", timeout_seconds: 8 } },
  { category: "Desktop & A11y", tool: "desktop_verify_action", tier: 1, defaultArgs: { expected_text: "", wait_seconds: 3 } },
  { category: "Desktop & A11y", tool: "launch_approved_application", tier: 3, defaultArgs: { app_name: "notepad" } },
  { category: "Desktop & A11y", tool: "keyboard_type", tier: 3, defaultArgs: { text: "Hello from AI Bridge" } },
  { category: "Desktop & A11y", tool: "press_hotkey", tier: 3, defaultArgs: { combo: "ctrl+s" } },
  { category: "Workspace & Code", tool: "list_workspace_files", tier: 1, defaultArgs: {} },
  { category: "Workspace & Code", tool: "git_status", tier: 1, defaultArgs: {} },
  { category: "Workspace & Code", tool: "git_diff", tier: 1, defaultArgs: {} },
  { category: "Workspace & Code", tool: "run_tests", tier: 2, defaultArgs: {} },
  { category: "Browser", tool: "browser_open", tier: 2, defaultArgs: { url: "https://example.com" } },
  { category: "Browser", tool: "browser_snapshot", tier: 1, defaultArgs: {} },
];

const ROLE_OPTIONS = [
  "planner",
  "researcher",
  "developer",
  "computer_operator",
  "browser_operator",
  "tester",
  "security_auditor",
  "recovery",
];

function inferTier(tool: string): number {
  const found = TOOL_CATALOG.find((t) => t.tool === tool);
  return found ? found.tier : 1;
}

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
  const [profiles, setProfiles] = useState<string[]>(["auto", "code", "research", "desktop", "browser", "full"]);

  // Supervisor Handoff + Autonomous Planner State
  const [handoffDevice, setHandoffDevice] = useState(devices[0]?.id ?? "local");
  const [handoffProfile, setHandoffProfile] = useState("auto");
  const [handoffGoal, setHandoffGoal] = useState("");
  const [handoffBusy, setHandoffBusy] = useState(false);
  const [planBusy, setPlanBusy] = useState(false);
  const [generatedPlan, setGeneratedPlan] = useState<AutonomousPlanResponse | null>(null);

  // Device Intelligence & Episodic Memory State
  const [deviceIntel, setDeviceIntel] = useState<DeviceIntelligenceProfile | null>(null);
  const [deviceMemories, setDeviceMemories] = useState<DeviceMemoryEpisode[]>([]);
  const [learnOpen, setLearnOpen] = useState(false);
  const [learnTool, setLearnTool] = useState("desktop_a11y_act");
  const [learnType, setLearnType] = useState("recovery_playbook");
  const [learnContext, setLearnContext] = useState("");
  const [learnResolution, setLearnResolution] = useState("");

  // Visual Workflow Builder State
  const [builderMode, setBuilderMode] = useState<"visual" | "json">("visual");
  const [wfName, setWfName] = useState("Autonomous Diagnostic & Verification DAG");
  const [wfDevice, setWfDevice] = useState(devices[0]?.id ?? "local");
  const [visualSteps, setVisualSteps] = useState<WorkflowDagStep[]>([
    {
      step_id: "step_1",
      name: "Planner: System Fingerprint",
      role: "planner",
      device_id: devices[0]?.id ?? "local",
      tool: "get_system_info",
      args: {},
      tier: 1,
      guard: { require_online: true },
      verify: null,
      retries: 1,
    },
    {
      step_id: "step_2",
      name: "Operator: Accessibility Tree Snapshot",
      role: "computer_operator",
      device_id: devices[0]?.id ?? "local",
      tool: "desktop_a11y_snapshot",
      args: { interactive_only: true, max_elements: 80, _upstream: "{{blackboard.step_1.summary}}" },
      depends_on: ["step_1"],
      tier: 1,
      guard: { require_online: true },
      verify: null,
      retries: 1,
    },
  ]);
  const [wfStepsJson, setWfStepsJson] = useState(() => JSON.stringify(visualSteps, null, 2));
  const [inspectedWorkflow, setInspectedWorkflow] = useState<WorkflowRecord | null>(null);
  const [templateBusy, setTemplateBusy] = useState(false);
  const [scheduleBusy, setScheduleBusy] = useState(false);
  const [blackboardOpen, setBlackboardOpen] = useState(false);
  const [blackboardHandoffId, setBlackboardHandoffId] = useState("");
  const [blackboardState, setBlackboardState] = useState<Record<string, unknown> | null>(null);
  const [blackboardBusy, setBlackboardBusy] = useState(false);
  const [lastHandoffId, setLastHandoffId] = useState("");

  async function loadAll() {
    try {
      const [tpls, wfs, rl] = await Promise.all([
        api.listWorkflowTemplates().catch(() => []),
        api.listWorkflows().catch(() => []),
        api.listRoles().catch(() => ({ roles: [], profiles: ["code", "research", "desktop", "browser", "full"] })),
      ]);
      setTemplates(tpls);
      setWorkflows(wfs);
      setRoles(rl.roles || []);
      if (rl.profiles?.length) {
        setProfiles(["auto", ...rl.profiles.filter((p) => p !== "auto")]);
      }
    } catch {
      // Ignore partial fetch error
    }
  }

  async function loadDeviceIntelligenceAndMemory(targetDevice: string) {
    try {
      const [intel, mem] = await Promise.all([
        api.getDeviceIntelligence(targetDevice).catch(() => null),
        api.getDeviceMemories(targetDevice, { limit: 8 }).catch(() => ({ episodes: [] })),
      ]);
      setDeviceIntel(intel);
      setDeviceMemories(mem.episodes || []);
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    void loadAll();
    const t = window.setInterval(() => void loadAll(), 5000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  useEffect(() => {
    void loadDeviceIntelligenceAndMemory(handoffDevice);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, handoffDevice]);

  useEffect(() => {
    if (!blackboardOpen || !blackboardHandoffId.trim()) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const st = await api.getHandoffStatus(blackboardHandoffId.trim());
        if (!cancelled) setBlackboardState(st);
      } catch {
        // Keep last good snapshot while handoff settles
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 2500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [api, blackboardOpen, blackboardHandoffId]);

  function syncStepsToVisual(nextSteps: WorkflowDagStep[]) {
    const normalized = nextSteps.map((s, idx) => ({
      ...s,
      step_id: s.step_id || `step_${idx + 1}`,
      index: idx + 1,
      tool: s.tool || s.action || "get_system_info",
      action: s.tool || s.action || "get_system_info",
      depends_on: idx > 0 ? [`step_${idx}`] : [],
      tier: s.tier ?? inferTier(s.tool || s.action || "get_system_info"),
    }));
    setVisualSteps(normalized);
    setWfStepsJson(JSON.stringify(normalized, null, 2));
  }

  function addStep() {
    const idx = visualSteps.length + 1;
    const next: WorkflowDagStep = {
      step_id: `step_${idx}`,
      index: idx,
      name: `Step ${idx}: get_resource_usage`,
      role: "computer_operator",
      device_id: wfDevice,
      tool: "get_resource_usage",
      action: "get_resource_usage",
      args: idx > 1 ? { _upstream_context: `{{blackboard.step_${idx - 1}.summary}}` } : {},
      depends_on: idx > 1 ? [`step_${idx - 1}`] : [],
      tier: 1,
      guard: { require_online: true },
      verify: null,
      retries: 1,
    };
    syncStepsToVisual([...visualSteps, next]);
  }

  function updateStep(index: number, patch: Partial<WorkflowDagStep>) {
    const copy = [...visualSteps];
    const current = copy[index];
    if (!current) return;
    const updated: WorkflowDagStep = { ...current, ...patch };
    if (patch.tool) {
      updated.action = patch.tool;
      updated.tier = inferTier(patch.tool);
      const cat = TOOL_CATALOG.find((t) => t.tool === patch.tool);
      if (cat && (!current.args || Object.keys(current.args).length === 0)) {
        updated.args = { ...cat.defaultArgs };
      }
    }
    copy[index] = updated;
    syncStepsToVisual(copy);
  }

  function moveStep(index: number, dir: -1 | 1) {
    const target = index + dir;
    if (target < 0 || target >= visualSteps.length) return;
    const copy = [...visualSteps];
    const tmp = copy[index];
    copy[index] = copy[target];
    copy[target] = tmp;
    syncStepsToVisual(copy);
  }

  function removeStep(index: number) {
    const copy = visualSteps.filter((_, i) => i !== index);
    syncStepsToVisual(copy);
  }

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
            Visual Workflow Studio &amp; Multi-Agent Coordinator
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Capability-aware autonomous task planning, blackboard context passing, visual DAG authoring, and device-level episodic learning.
          </p>
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          <Button size="sm" onClick={() => setLearnOpen(true)}>
            <Brain size={13} /> Teach Device Memory
          </Button>
          <Button
            variant="primary"
            onClick={async () => {
              try {
                const stepsToRun =
                  builderMode === "json"
                    ? (JSON.parse(wfStepsJson) as WorkflowDagStep[])
                    : visualSteps;
                await api.submitWorkflow({
                  name: wfName,
                  device_id: wfDevice,
                  steps: stepsToRun.map((s) => ({
                    ...s,
                    tool: s.tool || s.action || "get_system_info",
                    action: s.tool || s.action || "get_system_info",
                  })),
                });
                onToast(`Workflow '${wfName}' launched (${stepsToRun.length} steps)`);
                await loadAll();
              } catch (err) {
                onToast(err instanceof Error ? err.message : "Failed to launch workflow");
              }
            }}
          >
            <Play size={13} /> Execute DAG ({visualSteps.length} Steps)
          </Button>
        </div>
      </div>

      {/* Pillar 1, 2, 3, 4, 7: Autonomous Goal Planner + Device Intelligence & Memory */}
      <div className="split-2">
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
              <Sparkles size={15} style={{ color: "var(--accent)" }} /> Autonomous Task Planner &amp; Supervisor Handoff
            </div>
            {generatedPlan && (
              <StatusBadge
                status="ok"
                label={`Planned ${generatedPlan.total_steps} steps · Max T${generatedPlan.max_tier}`}
              />
            )}
          </div>
          <div className="split-2">
            <select
              aria-label="Target device for planner"
              className="field mono"
              value={handoffDevice}
              onChange={(e) => {
                setHandoffDevice(e.target.value);
                setWfDevice(e.target.value);
              }}
            >
              <option value="local">local</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.os || d.id})
                </option>
              ))}
            </select>
            <select
              aria-label="Handoff profile"
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
            placeholder="Describe autonomous goal (e.g., Inspect Windows accessibility tree, verify UI elements, and check system health)..."
            value={handoffGoal}
            onChange={(e) => setHandoffGoal(e.target.value)}
          />
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <Button
              variant="primary"
              size="sm"
              disabled={planBusy || !handoffGoal.trim()}
              onClick={async () => {
                setPlanBusy(true);
                try {
                  const plan = await api.supervisorPlanGoal({
                    device_id: handoffDevice,
                    goal: handoffGoal.trim(),
                    profile: handoffProfile,
                  });
                  setGeneratedPlan(plan);
                  setWfName(`AI Plan: ${handoffGoal.trim().slice(0, 48)}`);
                  setWfDevice(handoffDevice);
                  syncStepsToVisual(plan.steps);
                  onToast(`Synthesized ${plan.total_steps}-step DAG (${plan.profile}) into Visual Builder`);
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Planning failed");
                } finally {
                  setPlanBusy(false);
                }
              }}
            >
              <Sparkles size={12} /> {planBusy ? "Synthesizing DAG..." : "Synthesize Plan into Visual Builder"}
            </Button>
            <Button
              size="sm"
              disabled={handoffBusy || !handoffGoal.trim()}
              onClick={async () => {
                setHandoffBusy(true);
                try {
                  const res = await api.supervisorHandoff({
                    device_id: handoffDevice,
                    goal: handoffGoal.trim(),
                    profile: handoffProfile === "auto" ? "code" : handoffProfile,
                  });
                  const hid = String((res as { handoff_id?: string }).handoff_id || "");
                  if (hid) {
                    setLastHandoffId(hid);
                    setBlackboardHandoffId(hid);
                  }
                  onToast(hid ? `Handoff enqueued (${hid})` : "Supervisor multi-agent handoff enqueued");
                  await loadAll();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Handoff failed");
                } finally {
                  setHandoffBusy(false);
                }
              }}
            >
              <GitBranch size={12} /> {handoffBusy ? "Dispatching..." : "Enqueue Direct Role Handoff"}
            </Button>
            <Button
              size="sm"
              onClick={() => {
                if (lastHandoffId) setBlackboardHandoffId(lastHandoffId);
                setBlackboardOpen(true);
              }}
            >
              <Eye size={12} /> Live Blackboard
            </Button>
          </div>
        </SurfaceCard>

        {/* Pillar 1, 2 & 7: Unified Device Intelligence & Device-Level Episodic Memory */}
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
              <Cpu size={15} style={{ color: "var(--accent)" }} /> Unified Device Intelligence ({handoffDevice})
            </div>
            {deviceIntel && (
              <StatusBadge
                status={
                  deviceIntel.health_score >= 80
                    ? "ok"
                    : deviceIntel.health_score >= 50
                      ? "warn"
                      : "err"
                }
                label={`Score ${deviceIntel.health_score}/100 · ${deviceIntel.readiness_state}`}
              />
            )}
          </div>

          {deviceIntel ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 12 }}>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                <Chip>OS: {deviceIntel.platform_family}</Chip>
                <Chip>Transport: {deviceIntel.recommended_transport}</Chip>
                <Chip>A11y: {deviceIntel.recommended_a11y_backend}</Chip>
                <Chip>Reliability: {deviceIntel.reliability.success_rate_pct}%</Chip>
                <Chip>Memories: {deviceIntel.reliability.learned_memory_episodes}</Chip>
              </div>
              {deviceMemories.length > 0 && (
                <div
                  style={{
                    padding: 8,
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-hair)",
                    fontSize: 11.5,
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>
                    Recent Learned Device Memory ({deviceMemories.length}):
                  </div>
                  <div className="mono" style={{ color: "var(--text-secondary)", fontSize: 11 }}>
                    {deviceMemories[0].content.slice(0, 140)}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Loading device capability fingerprint and episodic memory...
            </div>
          )}
        </SurfaceCard>
      </div>

      {/* Pillar 5 & 6: Interactive Visual Workflow & Multi-Agent DAG Builder */}
      <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Layers size={16} style={{ color: "var(--accent)" }} />
            <span style={{ fontSize: 14.5, fontWeight: 600 }}>Visual Workflow &amp; Context-Aware DAG Builder</span>
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
            {templates.slice(0, 4).map((tpl) => (
              <Chip
                key={tpl.id}
                onClick={() => {
                  setWfName(tpl.name);
                  syncStepsToVisual(
                    (tpl.steps || []).map((st, idx) => ({
                      step_id: `step_${idx + 1}`,
                      name: (st as { name?: string }).name || `${st.tool}`,
                      role: "computer_operator",
                      device_id: wfDevice,
                      tool: st.tool || (st as { action?: string }).action || "get_system_info",
                      args: st.args || {},
                    })),
                  );
                  onToast(`Loaded template '${tpl.name}' into Visual Builder`);
                }}
              >
                {tpl.name}
              </Chip>
            ))}
            <Button
              size="sm"
              variant={builderMode === "visual" ? "primary" : "ghost"}
              onClick={() => {
                if (builderMode === "json") {
                  try {
                    const parsed = JSON.parse(wfStepsJson) as WorkflowDagStep[];
                    syncStepsToVisual(parsed);
                    setBuilderMode("visual");
                  } catch {
                    onToast("Fix JSON syntax before switching to Visual Canvas");
                  }
                }
              }}
            >
              <Layers size={12} /> Visual Canvas
            </Button>
            <Button
              size="sm"
              variant={builderMode === "json" ? "primary" : "ghost"}
              onClick={() => {
                setWfStepsJson(JSON.stringify(visualSteps, null, 2));
                setBuilderMode("json");
              }}
            >
              <Code2 size={12} /> JSON Code
            </Button>
            <Button
              size="sm"
              disabled={templateBusy || visualSteps.length === 0}
              onClick={async () => {
                setTemplateBusy(true);
                try {
                  const steps = visualSteps.map((st) => ({
                    tool: st.tool,
                    args: st.args || {},
                    name: st.name,
                    role: st.role,
                    device_id: st.device_id || wfDevice,
                    depends_on: st.depends_on,
                    tier: st.tier,
                    guard: st.guard,
                    verify: st.verify,
                    retries: st.retries,
                  }));
                  await api.createWorkflowTemplate({
                    name: wfName.trim() || "Custom DAG Template",
                    description: `Saved from Visual Canvas (${steps.length} steps)`,
                    steps,
                  });
                  onToast("Saved custom workflow template");
                  await loadAll();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Failed to save template");
                } finally {
                  setTemplateBusy(false);
                }
              }}
            >
              <BookmarkPlus size={12} /> {templateBusy ? "Saving..." : "Save as Custom Template"}
            </Button>
            <Button
              size="sm"
              disabled={scheduleBusy || visualSteps.length === 0}
              onClick={async () => {
                setScheduleBusy(true);
                try {
                  await api.createSchedule({
                    name: `Scheduled: ${wfName.trim() || "DAG"}`,
                    device_id: wfDevice || "local",
                    interval_seconds: 3600,
                    tool: "__workflow__",
                    args: {
                      name: wfName.trim() || "Scheduled DAG",
                      steps: visualSteps.map((st) => ({
                        tool: st.tool,
                        args: st.args || {},
                        name: st.name,
                        role: st.role,
                        device_id: st.device_id || wfDevice,
                        depends_on: st.depends_on,
                      })),
                      stop_on_failure: true,
                    },
                  });
                  onToast("Scheduled DAG (hourly). Manage under Fleet Ops & Cron.");
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Failed to schedule DAG");
                } finally {
                  setScheduleBusy(false);
                }
              }}
            >
              <CalendarClock size={12} /> {scheduleBusy ? "Scheduling..." : "Schedule DAG"}
            </Button>
          </div>
        </div>

        <div className="split-2">
          <div>
            <label style={{ display: "block", fontSize: 11.5, color: "var(--text-muted)", marginBottom: 4 }}>
              Workflow / Playbook Name
            </label>
            <input
              type="text"
              className="field"
              value={wfName}
              onChange={(e) => setWfName(e.target.value)}
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 11.5, color: "var(--text-muted)", marginBottom: 4 }}>
              Default Target Device
            </label>
            <select
              className="field mono"
              value={wfDevice}
              onChange={(e) => setWfDevice(e.target.value)}
            >
              <option value="local">local</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.id})
                </option>
              ))}
            </select>
          </div>
        </div>

        {builderMode === "json" ? (
          <textarea
            rows={10}
            className="field mono"
            value={wfStepsJson}
            onChange={(e) => setWfStepsJson(e.target.value)}
          />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {visualSteps.map((step, idx) => {
              const tier = step.tier ?? inferTier(step.tool);
              return (
                <div
                  key={step.step_id || idx}
                  style={{
                    padding: 14,
                    borderRadius: 10,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-hair)",
                    display: "flex",
                    flexDirection: "column",
                    gap: 10,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 8,
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span
                        className="mono"
                        style={{
                          padding: "2px 8px",
                          borderRadius: 6,
                          background: "rgba(99, 102, 241, 0.16)",
                          color: "var(--accent)",
                          fontSize: 11.5,
                          fontWeight: 600,
                        }}
                      >
                        #{idx + 1} · {step.step_id || `step_${idx + 1}`}
                      </span>
                      <StatusBadge
                        status={tier >= 3 ? "warn" : "ok"}
                        label={`Tier ${tier}${tier >= 3 ? " · Approval Gate" : " · Unattended"}`}
                      />
                      {step.depends_on && step.depends_on.length > 0 && (
                        <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                          depends on: {step.depends_on.join(", ")}
                        </span>
                      )}
                    </div>

                    <div style={{ display: "flex", gap: 4 }}>
                      <Button size="sm" variant="ghost" disabled={idx === 0} onClick={() => moveStep(idx, -1)}>
                        <ArrowUp size={12} />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={idx === visualSteps.length - 1}
                        onClick={() => moveStep(idx, 1)}
                      >
                        <ArrowDown size={12} />
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        disabled={visualSteps.length <= 1}
                        onClick={() => removeStep(idx)}
                      >
                        <Trash2 size={12} />
                      </Button>
                    </div>
                  </div>

                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 8 }}>
                    <div>
                      <label style={{ display: "block", fontSize: 11, color: "var(--text-muted)", marginBottom: 3 }}>
                        Step Label
                      </label>
                      <input
                        className="field"
                        value={step.name || ""}
                        onChange={(e) => updateStep(idx, { name: e.target.value })}
                      />
                    </div>
                    <div>
                      <label style={{ display: "block", fontSize: 11, color: "var(--text-muted)", marginBottom: 3 }}>
                        Assigned Role Agent
                      </label>
                      <select
                        className="field mono"
                        value={step.role || "computer_operator"}
                        onChange={(e) => updateStep(idx, { role: e.target.value })}
                      >
                        {ROLE_OPTIONS.map((r) => (
                          <option key={r} value={r}>
                            role: {r}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label style={{ display: "block", fontSize: 11, color: "var(--text-muted)", marginBottom: 3 }}>
                        Tool Action
                      </label>
                      <select
                        className="field mono"
                        value={step.tool}
                        onChange={(e) => updateStep(idx, { tool: e.target.value })}
                      >
                        {TOOL_CATALOG.map((tc) => (
                          <option key={tc.tool} value={tc.tool}>
                            [{tc.category}] {tc.tool} (T{tc.tier})
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label style={{ display: "block", fontSize: 11, color: "var(--text-muted)", marginBottom: 3 }}>
                        Node Device
                      </label>
                      <select
                        className="field mono"
                        value={step.device_id || wfDevice}
                        onChange={(e) => updateStep(idx, { device_id: e.target.value })}
                      >
                        <option value="local">local</option>
                        {devices.map((d) => (
                          <option key={d.id} value={d.id}>
                            {d.name} ({d.id})
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>

                  {/* Step Arguments & Context Guard / Verification */}
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 8 }}>
                    <div>
                      <label style={{ display: "block", fontSize: 11, color: "var(--text-muted)", marginBottom: 3 }}>
                        Arguments JSON (supports {"{{blackboard.step_1.summary}}"})
                      </label>
                      <input
                        className="field mono"
                        value={JSON.stringify(step.args || {})}
                        onChange={(e) => {
                          try {
                            const parsed = JSON.parse(e.target.value) as Record<string, unknown>;
                            updateStep(idx, { args: parsed });
                          } catch {
                            // wait until valid JSON
                          }
                        }}
                      />
                    </div>
                    <div>
                      <label style={{ display: "block", fontSize: 11, color: "var(--text-muted)", marginBottom: 3 }}>
                        Pre-Step Window Guard (optional title substring)
                      </label>
                      <input
                        className="field mono"
                        placeholder="e.g. Notepad or Visual Studio"
                        value={step.guard?.require_window_title_contains || ""}
                        onChange={(e) =>
                          updateStep(idx, {
                            guard: {
                              ...(step.guard || {}),
                              require_window_title_contains: e.target.value || undefined,
                            },
                          })
                        }
                      />
                    </div>
                    <div>
                      <label style={{ display: "block", fontSize: 11, color: "var(--text-muted)", marginBottom: 3 }}>
                        Post-Step Verify Expected Text (optional)
                      </label>
                      <input
                        className="field mono"
                        placeholder="Verify text appears on screen after step"
                        value={step.verify?.text || ""}
                        onChange={(e) =>
                          updateStep(idx, {
                            verify: e.target.value
                              ? { mode: "verify", text: e.target.value, timeout_seconds: 5 }
                              : null,
                          })
                        }
                      />
                    </div>
                  </div>
                </div>
              );
            })}

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <Button size="sm" onClick={addStep}>
                <Plus size={13} /> Add DAG Step
              </Button>
              <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
                <ShieldCheck size={12} style={{ verticalAlign: "middle", marginRight: 4 }} />
                Blackboard variables &amp; context guards are evaluated automatically before each step.
              </span>
            </div>
          </div>
        )}
      </SurfaceCard>

      {/* Role Catalog + Active & Historical Workflow Runs */}
      <div className="split-2">
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
            <Users size={15} /> Autonomous Role Catalog ({roles.length})
          </div>
          {roles.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No specialized roles registered.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {roles.slice(0, 6).map((r) => (
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
                      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{r.description}</div>
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

        <SurfaceCard style={{ overflow: "hidden" }}>
          <div
            style={{
              padding: "12px 16px",
              borderBottom: "1px solid var(--border-hair)",
              fontSize: 13.5,
              fontWeight: 600,
            }}
          >
            Workflow &amp; DAG Executions
          </div>
          <div className="table-wrap">
            <table className="saas-table">
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>Node</th>
                  <th>Progress</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {workflows.length === 0 ? (
                  <tr>
                    <td colSpan={5} style={{ textAlign: "center", padding: 26, color: "var(--text-muted)" }}>
                      No workflow executions yet. Launch a DAG from the Visual Builder above.
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
                        <td style={{ minWidth: 130 }}>
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
                                  background: wf.status === "failed" ? "var(--rose)" : "var(--accent)",
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
                            <Button size="sm" variant="ghost" onClick={() => setInspectedWorkflow(wf)}>
                              <Eye size={11} /> Inspect
                            </Button>
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
      </div>

      {/* Inspect Workflow Step-by-Step Results & Context Snapshots */}
      <Modal
        open={Boolean(inspectedWorkflow)}
        title={inspectedWorkflow ? `DAG Execution Inspector · ${inspectedWorkflow.name}` : "Inspector"}
        onClose={() => setInspectedWorkflow(null)}
      >
        {inspectedWorkflow && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, maxHeight: "65vh", overflowY: "auto" }}>
            {(inspectedWorkflow.results || []).length === 0 ? (
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No step results recorded yet.</div>
            ) : (
              (inspectedWorkflow.results || []).map((res, idx) => (
                <div
                  key={idx}
                  style={{
                    padding: 10,
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-hair)",
                    fontSize: 12,
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                    <span className="mono" style={{ fontWeight: 600 }}>
                      Step {String(res.index ?? idx + 1)}: {String(res.name || res.action || "")}
                    </span>
                    <StatusBadge
                      status={statusKind(String(res.status || "pending"))}
                      label={String(res.status || "pending")}
                    />
                  </div>
                  {Boolean(res.error) && (
                    <div style={{ color: "var(--rose)", fontSize: 11.5, marginBottom: 4 }}>
                      Error: {String(res.error)}
                    </div>
                  )}
                  <pre
                    className="mono"
                    style={{
                      margin: 0,
                      padding: 8,
                      borderRadius: 6,
                      background: "var(--bg-terminal)",
                      fontSize: 11,
                      overflowX: "auto",
                      maxHeight: 160,
                    }}
                  >
                    {JSON.stringify(res.result ?? res, null, 2)}
                  </pre>
                </div>
              ))
            )}
          </div>
        )}
      </Modal>

      {/* Teach Device-Level Episodic Memory Modal */}
      <Modal
        open={learnOpen}
        title={`Teach Device Episodic Memory (${handoffDevice})`}
        onClose={() => setLearnOpen(false)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <select className="field mono" value={learnType} onChange={(e) => setLearnType(e.target.value)}>
            <option value="recovery_playbook">recovery_playbook (Tool / UI Recovery Pattern)</option>
            <option value="ui_quirk">ui_quirk (Application / Window Quirk)</option>
            <option value="verified_workflow">verified_workflow (Verified Task Sequence)</option>
            <option value="fact">fact (Device Environment Fact)</option>
          </select>
          <input
            className="field mono"
            placeholder="Associated tool (e.g. desktop_a11y_act)"
            value={learnTool}
            onChange={(e) => setLearnTool(e.target.value)}
          />
          <input
            className="field"
            placeholder="Context summary (e.g. When modal dialog blocks main window)"
            value={learnContext}
            onChange={(e) => setLearnContext(e.target.value)}
          />
          <textarea
            rows={3}
            className="field"
            placeholder="Resolution / learned rule (e.g. Snapshot with interactive_only=true and click @e2 confirm button)"
            value={learnResolution}
            onChange={(e) => setLearnResolution(e.target.value)}
          />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" onClick={() => setLearnOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={!learnResolution.trim()}
              onClick={async () => {
                try {
                  await api.learnDeviceMemory(handoffDevice, {
                    episode_type: learnType,
                    tool: learnTool,
                    context_summary: learnContext,
                    resolution: learnResolution,
                  });
                  setLearnOpen(false);
                  setLearnContext("");
                  setLearnResolution("");
                  onToast(`Recorded device memory episode for ${handoffDevice}`);
                  await loadDeviceIntelligenceAndMemory(handoffDevice);
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Failed to save device memory");
                }
              }}
            >
              Save Device Memory
            </Button>
          </div>
        </div>
      </Modal>

      <SlideOverDrawer
        open={blackboardOpen}
        title="Live Blackboard State Inspector"
        subtitle="Planner → Computer Operator → Tester handoff outputs"
        onClose={() => setBlackboardOpen(false)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              className="field mono"
              placeholder="handoff_id"
              value={blackboardHandoffId}
              onChange={(e) => setBlackboardHandoffId(e.target.value)}
            />
            <Button
              size="sm"
              variant="primary"
              disabled={blackboardBusy || !blackboardHandoffId.trim()}
              onClick={async () => {
                setBlackboardBusy(true);
                try {
                  const st = await api.getHandoffStatus(blackboardHandoffId.trim());
                  setBlackboardState(st);
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Failed to load handoff");
                } finally {
                  setBlackboardBusy(false);
                }
              }}
            >
              {blackboardBusy ? "Loading..." : "Refresh"}
            </Button>
          </div>
          {blackboardState ? (
            <>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                <Chip>Status: {String(blackboardState.status || "—")}</Chip>
                <Chip>Profile: {String(blackboardState.profile || "—")}</Chip>
                <Chip>Tasks: {String(blackboardState.count ?? "—")}</Chip>
              </div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                Goal: {String(blackboardState.goal || "—")}
              </div>
              <pre className="terminal" style={{ margin: 0, maxHeight: 420, whiteSpace: "pre-wrap" }}>
                {JSON.stringify(blackboardState.blackboard || blackboardState, null, 2)}
              </pre>
            </>
          ) : (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Enter a handoff id (or enqueue a role handoff) and refresh to watch Planner → Operator → Tester
              blackboard outputs in real time.
            </div>
          )}
        </div>
      </SlideOverDrawer>
    </div>
  );
}

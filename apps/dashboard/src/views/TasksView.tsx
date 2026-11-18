import { useMemo, useState } from "react";
import { ArrowUpDown, Plus, Trash2 } from "lucide-react";
import type { ApiClient } from "../api";
import type { DeviceRecord, TaskRecord } from "../types";
import {
  Button,
  Chip,
  Modal,
  SlideOverDrawer,
  StatusBadge,
  SurfaceCard,
} from "../components/ui";

const PAGE_SIZE = 10;

export function TasksView({
  api,
  tasks,
  devices,
  onRefresh,
  onToast,
}: {
  api: ApiClient;
  tasks: TaskRecord[];
  devices: DeviceRecord[];
  onRefresh: () => Promise<void>;
  onToast: (msg: string) => void;
}) {
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<"created_at" | "tool" | "status">("created_at");
  const [asc, setAsc] = useState(false);
  const [page, setPage] = useState(1);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Inspector Drawer
  const [inspectedTask, setInspectedTask] = useState<TaskRecord | null>(null);
  const [taskDetail, setTaskDetail] = useState<TaskRecord | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // Dispatch Modal
  const [dispatchOpen, setDispatchOpen] = useState(false);
  const [targetDevice, setTargetDevice] = useState(devices[0]?.id ?? "local");
  const [toolName, setToolName] = useState("get_system_info");
  const [argsJson, setArgsJson] = useState("{}");
  const [submitting, setSubmitting] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tasks
      .filter((t) => {
        if (filterStatus !== "all" && t.status !== filterStatus) return false;
        if (!q) return true;
        return (
          t.id.toLowerCase().includes(q) ||
          t.tool.toLowerCase().includes(q) ||
          t.device_id.toLowerCase().includes(q)
        );
      })
      .sort((a, b) => {
        if (sortKey === "created_at") {
          const av = a.created_at ?? 0;
          const bv = b.created_at ?? 0;
          return asc ? av - bv : bv - av;
        }
        const av = String(a[sortKey] ?? "").toLowerCase();
        const bv = String(b[sortKey] ?? "").toLowerCase();
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      });
  }, [tasks, filterStatus, query, sortKey, asc]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const slice = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  function statusKind(s: string): "ok" | "warn" | "err" | "muted" {
    if (s === "completed") return "ok";
    if (s === "queued" || s === "running" || s === "needs_approval") return "warn";
    if (s === "failed" || s === "timed_out") return "err";
    return "muted";
  }

  async function openTaskDrawer(task: TaskRecord) {
    setInspectedTask(task);
    setTaskDetail(task);
    setLoadingDetail(true);
    try {
      const full = await api.getTask(task.id);
      setTaskDetail(full);
    } catch {
      // Keep projected task data if detail fetch fails
    } finally {
      setLoadingDetail(false);
    }
  }

  async function handleDispatch() {
    setSubmitting(true);
    try {
      const parsedArgs = argsJson.trim() ? (JSON.parse(argsJson) as Record<string, unknown>) : {};
      await api.submitTask({
        device_id: targetDevice || devices[0]?.id || "local",
        tool: toolName.trim(),
        args: parsedArgs,
      });
      setDispatchOpen(false);
      onToast(`Task ${toolName} queued`);
      await onRefresh();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Failed to queue task");
    } finally {
      setSubmitting(false);
    }
  }

  function toggleSort(k: "created_at" | "tool" | "status") {
    if (sortKey === k) setAsc((v) => !v);
    else {
      setSortKey(k);
      setAsc(k !== "created_at");
    }
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
            Task Explorer
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Inspect remote tool invocations, filter execution statuses, and queue new tasks.
          </p>
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          <Button
            size="sm"
            onClick={async () => {
              const res = await api.clearFinishedTasks();
              onToast(`Cleared ${res.deleted_count} finished tasks`);
              await onRefresh();
            }}
          >
            <Trash2 size={13} /> Clear Finished
          </Button>
          <Button
            variant="primary"
            onClick={() => {
              if (!targetDevice && devices[0]) setTargetDevice(devices[0].id);
              setDispatchOpen(true);
            }}
          >
            <Plus size={14} /> Run Task
          </Button>
        </div>
      </div>

      <SurfaceCard style={{ overflow: "hidden" }}>
        <div
          style={{
            padding: "12px 16px",
            borderBottom: "1px solid var(--border-hair)",
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 10,
          }}
        >
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
            <input
              type="text"
              className="field"
              style={{ width: 230 }}
              placeholder="Search task ID, tool, device..."
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(1);
              }}
            />
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {(["all", "completed", "queued", "running", "needs_approval", "failed"] as const).map(
                (st) => (
                  <Chip
                    key={st}
                    active={filterStatus === st}
                    onClick={() => {
                      setFilterStatus(st);
                      setPage(1);
                    }}
                  >
                    {st === "all" ? "All" : st.replace("_", " ")}
                  </Chip>
                ),
              )}
            </div>
          </div>

          <span className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
            {selectedIds.size} selected
          </span>
        </div>

        <div className="table-wrap">
          <table className="saas-table">
            <thead>
              <tr>
                <th style={{ width: 34 }}>
                  <input
                    type="checkbox"
                    checked={slice.length > 0 && slice.every((t) => selectedIds.has(t.id))}
                    onChange={(e) => {
                      const next = new Set(selectedIds);
                      if (e.target.checked) slice.forEach((t) => next.add(t.id));
                      else slice.forEach((t) => next.delete(t.id));
                      setSelectedIds(next);
                    }}
                  />
                </th>
                <th>Task ID</th>
                <th style={{ cursor: "pointer" }} onClick={() => toggleSort("tool")}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    Tool <ArrowUpDown size={12} />
                  </span>
                </th>
                <th>Node</th>
                <th style={{ cursor: "pointer" }} onClick={() => toggleSort("status")}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    Status <ArrowUpDown size={12} />
                  </span>
                </th>
                <th style={{ cursor: "pointer" }} onClick={() => toggleSort("created_at")}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    Created <ArrowUpDown size={12} />
                  </span>
                </th>
              </tr>
            </thead>
            <tbody>
              {slice.length === 0 ? (
                <tr>
                  <td colSpan={6} style={{ textAlign: "center", padding: 28, color: "var(--text-muted)" }}>
                    No tasks match your filter.
                  </td>
                </tr>
              ) : (
                slice.map((t) => (
                  <tr key={t.id} style={{ cursor: "pointer" }} onClick={() => void openTaskDrawer(t)}>
                    <td onClick={(e) => e.stopPropagation()} style={{ cursor: "default" }}>
                      <input
                        type="checkbox"
                        checked={selectedIds.has(t.id)}
                        onChange={(e) => {
                          const next = new Set(selectedIds);
                          if (e.target.checked) next.add(t.id);
                          else next.delete(t.id);
                          setSelectedIds(next);
                        }}
                      />
                    </td>
                    <td className="mono" style={{ fontWeight: 500 }}>
                      {t.id}
                    </td>
                    <td>{t.tool}</td>
                    <td className="mono" style={{ color: "var(--text-secondary)" }}>
                      {t.device_id}
                    </td>
                    <td>
                      <StatusBadge status={statusKind(t.status)} label={t.status} />
                    </td>
                    <td className="mono" style={{ color: "var(--text-muted)" }}>
                      {t.created_at ? new Date(t.created_at * 1000).toLocaleTimeString() : "—"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        <div
          style={{
            padding: "10px 16px",
            borderTop: "1px solid var(--border-hair)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            fontSize: 12,
            color: "var(--text-muted)",
          }}
        >
          <span>
            Page {safePage} of {totalPages} ({filtered.length} tasks)
          </span>
          <div style={{ display: "flex", gap: 6 }}>
            <Button size="sm" disabled={safePage <= 1} onClick={() => setPage((p) => p - 1)}>
              Prev
            </Button>
            <Button size="sm" disabled={safePage >= totalPages} onClick={() => setPage((p) => p + 1)}>
              Next
            </Button>
          </div>
        </div>
      </SurfaceCard>

      {/* Run Task Modal */}
      <Modal open={dispatchOpen} title="Dispatch Remote Task" onClose={() => setDispatchOpen(false)}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>
              Target Device
            </label>
            <select
              className="field mono"
              value={targetDevice}
              onChange={(e) => setTargetDevice(e.target.value)}
            >
              <option value="local">local (Control Plane Host)</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.id})
                </option>
              ))}
            </select>
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>
              Tool Name
            </label>
            <input
              type="text"
              className="field mono"
              value={toolName}
              onChange={(e) => setToolName(e.target.value)}
              placeholder="get_system_info, list_processes, get_monitors..."
            />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>
              Arguments (JSON)
            </label>
            <textarea
              rows={3}
              className="field mono"
              value={argsJson}
              onChange={(e) => setArgsJson(e.target.value)}
            />
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, paddingTop: 8 }}>
            <Button variant="ghost" onClick={() => setDispatchOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" disabled={submitting} onClick={() => void handleDispatch()}>
              {submitting ? "Queueing..." : "Queue Task"}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Slide-Over Task Inspector */}
      <SlideOverDrawer
        open={Boolean(inspectedTask)}
        title={inspectedTask ? `Task · ${inspectedTask.tool}` : "Task Inspector"}
        subtitle={inspectedTask?.id}
        onClose={() => setInspectedTask(null)}
        footer={
          inspectedTask && (
            <>
              {(inspectedTask.status === "queued" || inspectedTask.status === "running") && (
                <Button
                  variant="danger"
                  onClick={async () => {
                    await api.cancelTask(inspectedTask.id);
                    onToast(`Cancelled ${inspectedTask.id}`);
                    setInspectedTask(null);
                    await onRefresh();
                  }}
                >
                  Cancel Task
                </Button>
              )}
              <Button variant="primary" onClick={() => setInspectedTask(null)}>
                Close
              </Button>
            </>
          )
        }
      >
        {taskDetail && (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <SurfaceCard style={{ padding: 14, display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                <span style={{ color: "var(--text-muted)" }}>Status</span>
                <StatusBadge status={statusKind(taskDetail.status)} label={taskDetail.status} />
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                <span style={{ color: "var(--text-muted)" }}>Node</span>
                <span className="mono">{taskDetail.device_id}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                <span style={{ color: "var(--text-muted)" }}>Security Tier</span>
                <span className="mono">Tier {taskDetail.tier ?? 1}</span>
              </div>
            </SurfaceCard>

            <div>
              <div style={{ fontSize: 11.5, color: "var(--text-muted)", marginBottom: 6 }}>
                Arguments
              </div>
              <div className="terminal">{taskDetail.args_json || "{}"}</div>
            </div>

            <div>
              <div style={{ fontSize: 11.5, color: "var(--text-muted)", marginBottom: 6 }}>
                Execution Output {loadingDetail ? "(loading...)" : ""}
              </div>
              <pre className="terminal" style={{ margin: 0, whiteSpace: "pre-wrap", maxHeight: 340 }}>
                {taskDetail.result || "(No result output yet)"}
              </pre>
            </div>
          </div>
        )}
      </SlideOverDrawer>
    </div>
  );
}

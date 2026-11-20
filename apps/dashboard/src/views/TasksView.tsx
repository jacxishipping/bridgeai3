import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowUpDown, ExternalLink, Plus, RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import type { ApiClient } from "../api";
import type { DeadLetterRecord, DeviceRecord, TaskRecord } from "../types";
import {
  Button,
  Chip,
  Modal,
  SlideOverDrawer,
  StatusBadge,
  SurfaceCard,
  Toggle,
  UnderlineTabs,
} from "../components/ui";

const PAGE_SIZE = 10;

export function TasksView({
  api,
  tasks,
  devices,
  onRefresh,
  onToast,
  onInspectTrace,
}: {
  api: ApiClient;
  tasks: TaskRecord[];
  devices: DeviceRecord[];
  onRefresh: () => Promise<void>;
  onToast: (msg: string) => void;
  onInspectTrace?: (traceId: string) => void;
}) {
  const [viewTab, setViewTab] = useState<"tasks" | "dlq">("tasks");
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<"created_at" | "tool" | "status">("created_at");
  const [asc, setAsc] = useState(false);
  const [page, setPage] = useState(1);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Dead-letter queue state (Release 2)
  const [dlqItems, setDlqItems] = useState<DeadLetterRecord[]>([]);
  const [includeReplayed, setIncludeReplayed] = useState(false);
  const [dlqLoading, setDlqLoading] = useState(false);
  const [dlqBusyId, setDlqBusyId] = useState<string | null>(null);

  // Inspector Drawer
  const [inspectedTask, setInspectedTask] = useState<TaskRecord | null>(null);
  const [taskDetail, setTaskDetail] = useState<TaskRecord | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // Dispatch Modal
  const [dispatchOpen, setDispatchOpen] = useState(false);
  const [targetDevice, setTargetDevice] = useState(devices[0]?.id ?? "local");
  const [toolName, setToolName] = useState("get_system_info");
  const [argsJson, setArgsJson] = useState("{}");
  const [maxAttempts, setMaxAttempts] = useState("1");
  const [submitting, setSubmitting] = useState(false);

  const loadDlq = useCallback(async () => {
    setDlqLoading(true);
    try {
      const res = await api.listDeadLetterTasks(includeReplayed);
      setDlqItems(res.items || []);
    } catch {
      // Non-fatal if viewer lacks permission or older plane
    } finally {
      setDlqLoading(false);
    }
  }, [api, includeReplayed]);

  useEffect(() => {
    void loadDlq();
  }, [loadDlq]);

  const unreplayedDlqCount = useMemo(
    () => dlqItems.filter((item) => !item.replayed_task_id).length,
    [dlqItems],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tasks
      .filter((t) => {
        if (filterStatus !== "all" && t.status !== filterStatus) return false;
        if (!q) return true;
        return (
          t.id.toLowerCase().includes(q) ||
          t.tool.toLowerCase().includes(q) ||
          t.device_id.toLowerCase().includes(q) ||
          (t.trace_id || "").toLowerCase().includes(q)
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
      const attempts = Math.max(1, Math.min(5, Number(maxAttempts) || 1));
      await api.submitTask({
        device_id: targetDevice || devices[0]?.id || "local",
        tool: toolName.trim(),
        args: parsedArgs,
        max_attempts: attempts,
      });
      setDispatchOpen(false);
      onToast(`Task ${toolName} queued (max attempts: ${attempts})`);
      await onRefresh();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Failed to queue task");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleReplayDlq(item: DeadLetterRecord) {
    setDlqBusyId(item.id);
    try {
      const res = await api.replayDeadLetterTask(item.id);
      onToast(`Replayed ${item.task_id} as new task ${String(res.task_id || "")}`);
      await Promise.all([loadDlq(), onRefresh()]);
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Replay failed");
    } finally {
      setDlqBusyId(null);
    }
  }

  async function handlePurgeDlq(item: DeadLetterRecord) {
    setDlqBusyId(item.id);
    try {
      await api.deleteDeadLetterTask(item.id);
      onToast(`Purged dead-letter entry ${item.id}`);
      await loadDlq();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Purge requires admin role");
    } finally {
      setDlqBusyId(null);
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
            Task Explorer &amp; Dead-Letter Queue
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Inspect remote tool invocations, retry budgets, dead-lettered failures, and end-to-end traces.
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

      <UnderlineTabs
        value={viewTab}
        onChange={setViewTab}
        tabs={[
          { id: "tasks", label: `All Tasks (${tasks.length})` },
          {
            id: "dlq",
            label: `Dead-Letter Queue (${unreplayedDlqCount})`,
          },
        ]}
      />

      {viewTab === "tasks" ? (
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
                style={{ width: 250 }}
                placeholder="Search task ID, tool, device, trace..."
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setPage(1);
                }}
              />
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {(["all", "completed", "queued", "running", "needs_approval", "failed", "timed_out"] as const).map(
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
                  <th>Trace</th>
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
                    <td colSpan={7} style={{ textAlign: "center", padding: 28, color: "var(--text-muted)" }}>
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
                        {Boolean(t.dead_lettered) && (
                          <span
                            className="mono"
                            style={{
                              marginLeft: 6,
                              fontSize: 10,
                              padding: "1px 5px",
                              borderRadius: 4,
                              background: "rgba(239,68,68,0.16)",
                              color: "var(--red)",
                            }}
                          >
                            DLQ
                          </span>
                        )}
                      </td>
                      <td>
                        <div>{t.tool}</div>
                        {(t.max_attempts ?? 1) > 1 && (
                          <div className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
                            attempt {t.attempt ?? 0}/{t.max_attempts}
                          </div>
                        )}
                      </td>
                      <td className="mono" style={{ color: "var(--text-secondary)" }}>
                        {t.device_id}
                      </td>
                      <td>
                        <StatusBadge status={statusKind(t.status)} label={t.status} />
                        {t.failure_class && (
                          <div className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)", marginTop: 2 }}>
                            {t.failure_class}
                          </div>
                        )}
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {t.trace_id ? (
                          <Chip
                            onClick={() => onInspectTrace?.(t.trace_id!)}
                          >
                            <span className="mono" style={{ fontSize: 11 }}>
                              {t.trace_id.slice(0, 14)}…
                            </span>
                          </Chip>
                        ) : (
                          <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>—</span>
                        )}
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
      ) : (
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
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, fontWeight: 600 }}>
              <AlertTriangle size={15} style={{ color: "var(--amber)" }} />
              Dead-Lettered Tasks ({dlqItems.length})
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-secondary)" }}>
                <Toggle checked={includeReplayed} onChange={setIncludeReplayed} />
                Include replayed
              </label>
              <Button size="sm" disabled={dlqLoading} onClick={() => void loadDlq()}>
                <RefreshCw size={12} /> Refresh
              </Button>
            </div>
          </div>

          <div className="table-wrap">
            <table className="saas-table">
              <thead>
                <tr>
                  <th>DLQ / Task</th>
                  <th>Tool &amp; Node</th>
                  <th>Failure Classification</th>
                  <th>Attempts</th>
                  <th>Trace</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {dlqItems.length === 0 ? (
                  <tr>
                    <td colSpan={6} style={{ textAlign: "center", padding: 28, color: "var(--text-muted)" }}>
                      {dlqLoading ? "Loading dead-letter queue..." : "No dead-lettered tasks. Exhausted retries and lease timeouts appear here."}
                    </td>
                  </tr>
                ) : (
                  dlqItems.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <div className="mono" style={{ fontWeight: 500, fontSize: 12 }}>{item.task_id}</div>
                        <div className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
                          {item.id} · {new Date(item.created_at * 1000).toLocaleString()}
                        </div>
                      </td>
                      <td>
                        <div style={{ fontWeight: 500 }}>{item.tool}</div>
                        <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                          node: {item.device_id}
                        </div>
                      </td>
                      <td>
                        <StatusBadge status="err" label={item.failure_class || "permanent"} />
                        <div style={{ fontSize: 11.5, color: "var(--text-secondary)", marginTop: 3, maxWidth: 260 }}>
                          {item.failure_reason || item.last_error || "—"}
                        </div>
                      </td>
                      <td className="mono">{item.attempts}</td>
                      <td>
                        {item.trace_id ? (
                          <Chip onClick={() => onInspectTrace?.(item.trace_id!)}>
                            <span className="mono" style={{ fontSize: 11 }}>
                              {item.trace_id.slice(0, 14)}…
                            </span>
                          </Chip>
                        ) : (
                          <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>—</span>
                        )}
                      </td>
                      <td>
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          {item.replayed_task_id ? (
                            <StatusBadge status="ok" label={`Replayed → ${item.replayed_task_id}`} />
                          ) : (
                            <Button
                              size="sm"
                              variant="primary"
                              disabled={dlqBusyId === item.id}
                              onClick={() => void handleReplayDlq(item)}
                            >
                              <RotateCcw size={11} /> Replay
                            </Button>
                          )}
                          <Button
                            size="sm"
                            variant="danger"
                            disabled={dlqBusyId === item.id}
                            onClick={() => void handlePurgeDlq(item)}
                          >
                            <Trash2 size={11} />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </SurfaceCard>
      )}

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
          <div className="split-2">
            <div>
              <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>
                Tool Name
              </label>
              <input
                type="text"
                className="field mono"
                value={toolName}
                onChange={(e) => setToolName(e.target.value)}
                placeholder="get_system_info, list_processes..."
              />
            </div>
            <div>
              <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>
                Max Attempts (Idempotent Retry)
              </label>
              <select
                className="field mono"
                value={maxAttempts}
                onChange={(e) => setMaxAttempts(e.target.value)}
              >
                <option value="1">1 (no auto-retry)</option>
                <option value="2">2 attempts</option>
                <option value="3">3 attempts</option>
                <option value="5">5 attempts</option>
              </select>
            </div>
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
              {taskDetail?.trace_id && onInspectTrace && (
                <Button
                  onClick={() => {
                    const tid = taskDetail.trace_id!;
                    setInspectedTask(null);
                    onInspectTrace(tid);
                  }}
                >
                  <ExternalLink size={12} /> Inspect Trace
                </Button>
              )}
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
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                <span style={{ color: "var(--text-muted)" }}>Attempts</span>
                <span className="mono">
                  {taskDetail.attempt ?? 0} / {taskDetail.max_attempts ?? 1}
                </span>
              </div>
              {taskDetail.progress_pct != null && (
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                  <span style={{ color: "var(--text-muted)" }}>Progress</span>
                  <span className="mono">
                    {taskDetail.progress_pct}% {taskDetail.status_message ? `· ${taskDetail.status_message}` : ""}
                  </span>
                </div>
              )}
              {taskDetail.failure_class && (
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                  <span style={{ color: "var(--text-muted)" }}>Failure</span>
                  <span className="mono" style={{ color: "var(--red)" }}>
                    {taskDetail.failure_class}: {taskDetail.failure_reason || "—"}
                  </span>
                </div>
              )}
              {taskDetail.trace_id && (
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12 }}>
                  <span style={{ color: "var(--text-muted)" }}>Trace ID</span>
                  <span className="mono" style={{ fontSize: 11 }}>{taskDetail.trace_id}</span>
                </div>
              )}
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

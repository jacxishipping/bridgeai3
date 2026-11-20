import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, GitCommitHorizontal, Search, ShieldCheck, Terminal } from "lucide-react";
import type { ApiClient } from "../api";
import type { AuditRecord, AuditVerifyResult, TraceTimeline } from "../types";
import { Button, Chip, StatusBadge, SurfaceCard, UnderlineTabs } from "../components/ui";

interface UnifiedTimelineEntry {
  key: string;
  ts: number;
  category: "TASK" | "EVENT" | "APPROVAL" | "AUDIT";
  title: string;
  subtitle: string;
  status: string;
  detail?: string;
}

export function AuditLogsView({
  api,
  audit,
  initialTraceId = "",
  onToast,
}: {
  api: ApiClient;
  audit: AuditRecord[];
  initialTraceId?: string;
  onToast: (msg: string) => void;
}) {
  const [tab, setTab] = useState<"ledger" | "trace" | "logs">(initialTraceId ? "trace" : "ledger");
  const [query, setQuery] = useState("");
  const [verifyRes, setVerifyRes] = useState<AuditVerifyResult | null>(null);
  const [logService, setLogService] = useState<"control" | "mcp" | "oauth" | "nginx">("control");
  const [logLines, setLogLines] = useState<string[]>([]);

  // Trace Viewer state (Release 2)
  const [traceInput, setTraceInput] = useState(initialTraceId);
  const [traceData, setTraceData] = useState<TraceTimeline | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState<string | null>(null);

  const lookupTrace = useCallback(
    async (tid: string) => {
      const clean = tid.trim();
      if (!clean) return;
      setTraceLoading(true);
      setTraceError(null);
      try {
        const res = await api.getTrace(clean);
        setTraceData(res);
      } catch (err) {
        setTraceData(null);
        setTraceError(err instanceof Error ? err.message : "Failed to load trace");
      } finally {
        setTraceLoading(false);
      }
    },
    [api],
  );

  useEffect(() => {
    if (initialTraceId) {
      setTraceInput(initialTraceId);
      setTab("trace");
      void lookupTrace(initialTraceId);
    }
  }, [initialTraceId, lookupTrace]);

  useEffect(() => {
    if (tab !== "logs") return;
    api
      .getServiceLogs(logService, 120)
      .then((res) => setLogLines(res.lines || []))
      .catch((err) =>
        setLogLines([err instanceof Error ? err.message : "Failed to load service logs"]),
      );
  }, [api, tab, logService]);

  const filteredAudit = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return audit;
    return audit.filter(
      (a) =>
        a.tool.toLowerCase().includes(q) ||
        a.actor.toLowerCase().includes(q) ||
        (a.device_id || "").toLowerCase().includes(q) ||
        a.decision.toLowerCase().includes(q) ||
        (a.trace_id || "").toLowerCase().includes(q),
    );
  }, [audit, query]);

  const recentTraceIds = useMemo(() => {
    const seen: string[] = [];
    for (const a of audit) {
      if (a.trace_id && !seen.includes(a.trace_id)) {
        seen.push(a.trace_id);
        if (seen.length >= 6) break;
      }
    }
    return seen;
  }, [audit]);

  const unifiedTimeline = useMemo<UnifiedTimelineEntry[]>(() => {
    if (!traceData) return [];
    const items: UnifiedTimelineEntry[] = [];
    for (const t of traceData.tasks) {
      items.push({
        key: `task-${t.id}`,
        ts: t.created_at ?? 0,
        category: "TASK",
        title: `${t.tool} (${t.id})`,
        subtitle: `node: ${t.device_id} · tier ${t.tier ?? 1} · attempt ${t.attempt ?? 0}/${t.max_attempts ?? 1}`,
        status: t.status,
        detail: t.failure_reason ? `${t.failure_class || "error"}: ${t.failure_reason}` : t.args_json,
      });
    }
    for (const ev of traceData.task_events) {
      items.push({
        key: `ev-${ev.id}`,
        ts: ev.created_at ?? 0,
        category: "EVENT",
        title: `task_event: ${ev.event}`,
        subtitle: `task: ${ev.task_id}`,
        status: ev.event,
        detail: ev.detail_json || (ev.detail ? JSON.stringify(ev.detail) : ""),
      });
    }
    for (const ap of traceData.approvals) {
      items.push({
        key: `ap-${ap.id}`,
        ts: ap.created_at ?? 0,
        category: "APPROVAL",
        title: `Approval ${ap.id} (${ap.tool || "operation"})`,
        subtitle: ap.decided_by ? `decided by ${ap.decided_by}` : "awaiting operator decision",
        status: ap.status,
        detail: ap.reason || "",
      });
    }
    for (const a of traceData.audit) {
      items.push({
        key: `aud-${a.id}`,
        ts: a.ts ?? 0,
        category: "AUDIT",
        title: `${a.tool} by ${a.actor}`,
        subtitle: a.device_id ? `node: ${a.device_id} · hash ${(a.entry_hash || "").slice(0, 14)}…` : `hash ${(a.entry_hash || "").slice(0, 14)}…`,
        status: a.decision,
        detail: a.args_json || "",
      });
    }
    return items.sort((a, b) => a.ts - b.ts);
  }, [traceData]);

  function badgeStatus(s: string): "ok" | "warn" | "err" | "muted" {
    if (["completed", "allowed", "executed", "approved", "created", "running", "progress"].includes(s)) return "ok";
    if (["queued", "pending", "needs_approval", "retry_scheduled", "requeued"].includes(s)) return "warn";
    if (["failed", "timed_out", "denied", "cancelled", "dead_lettered", "expired"].includes(s)) return "err";
    return "muted";
  }

  async function handleExportJsonl() {
    try {
      const res = await api.exportAudit();
      const jsonl = res.events.map((ev) => JSON.stringify(ev)).join("\n");
      const blob = new Blob([jsonl], { type: "application/x-ndjson" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `ai-bridge-audit-${Date.now()}.jsonl`;
      a.click();
      URL.revokeObjectURL(url);
      onToast(`Exported ${res.count} audit records`);
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Export requires admin role");
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
            Audit Trail, Trace Viewer &amp; Logs
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Cryptographically hash-chained audit ledger, end-to-end correlation traces, and service log tails.
          </p>
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {verifyRes && (
            <StatusBadge
              status={(verifyRes.ok ?? verifyRes.valid) ? "ok" : "err"}
              label={
                (verifyRes.ok ?? verifyRes.valid)
                  ? `Verified (${verifyRes.checked ?? 0})`
                  : `Broken at #${verifyRes.broken_at ?? "?"}`
              }
            />
          )}
          <Button
            size="sm"
            onClick={async () => {
              const res = await api.verifyAuditChain();
              const isOk = Boolean(res.ok ?? res.valid);
              setVerifyRes(res);
              onToast(isOk ? `Hash chain verified (${res.checked ?? 0} rows)` : "Hash mismatch detected");
            }}
          >
            <ShieldCheck size={13} /> Verify Chain
          </Button>
          <Button size="sm" variant="primary" onClick={() => void handleExportJsonl()}>
            <Download size={13} /> Export JSONL
          </Button>
        </div>
      </div>

      <UnderlineTabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "ledger", label: `Hash-Chained Ledger (${audit.length})` },
          { id: "trace", label: "Trace Viewer" },
          { id: "logs", label: "Service Logs" },
        ]}
      />

      {tab === "ledger" ? (
        <SurfaceCard style={{ overflow: "hidden" }}>
          <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border-hair)" }}>
            <input
              type="text"
              className="field"
              style={{ maxWidth: 340 }}
              placeholder="Filter actor, tool, device, decision, trace..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div className="table-wrap">
            <table className="saas-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Timestamp</th>
                  <th>Actor</th>
                  <th>Action / Tool</th>
                  <th>Decision</th>
                  <th>Trace ID</th>
                  <th>Entry SHA-256</th>
                </tr>
              </thead>
              <tbody>
                {filteredAudit.slice(0, 80).map((a) => (
                  <tr key={a.id}>
                    <td className="mono">{a.id}</td>
                    <td className="mono" style={{ color: "var(--text-muted)" }}>
                      {new Date(a.ts * 1000).toLocaleString()}
                    </td>
                    <td className="mono">{a.actor}</td>
                    <td>
                      <div style={{ fontWeight: 500 }}>{a.tool}</div>
                      {a.device_id && (
                        <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                          node: {a.device_id}
                        </div>
                      )}
                    </td>
                    <td>
                      <StatusBadge
                        status={
                          a.decision === "allowed" || a.decision === "executed"
                            ? "ok"
                            : a.decision === "denied"
                              ? "err"
                              : "warn"
                        }
                        label={a.decision}
                      />
                    </td>
                    <td>
                      {a.trace_id ? (
                        <Chip
                          onClick={() => {
                            setTraceInput(a.trace_id!);
                            setTab("trace");
                            void lookupTrace(a.trace_id!);
                          }}
                        >
                          <span className="mono" style={{ fontSize: 11 }}>
                            {a.trace_id.slice(0, 14)}…
                          </span>
                        </Chip>
                      ) : (
                        <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>—</span>
                      )}
                    </td>
                    <td className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                      {a.entry_hash ? a.entry_hash.slice(0, 18) + "…" : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </SurfaceCard>
      ) : tab === "trace" ? (
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 600 }}>
            <GitCommitHorizontal size={15} style={{ color: "var(--accent)" }} />
            End-to-End Correlation Trace Lookup
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <input
              type="text"
              className="field mono"
              style={{ flex: "1 1 280px" }}
              value={traceInput}
              onChange={(e) => setTraceInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void lookupTrace(traceInput);
              }}
              placeholder="Enter X-Correlation-ID / trace_id (e.g. ui-... or tr-...)"
            />
            <Button
              variant="primary"
              disabled={traceLoading || !traceInput.trim()}
              onClick={() => void lookupTrace(traceInput)}
            >
              <Search size={13} /> {traceLoading ? "Tracing..." : "Lookup Trace"}
            </Button>
          </div>

          {recentTraceIds.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>Recent traces:</span>
              {recentTraceIds.map((tid) => (
                <Chip
                  key={tid}
                  active={traceData?.trace_id === tid}
                  onClick={() => {
                    setTraceInput(tid);
                    void lookupTrace(tid);
                  }}
                >
                  <span className="mono" style={{ fontSize: 11 }}>{tid}</span>
                </Chip>
              ))}
            </div>
          )}

          {traceError && (
            <div style={{ fontSize: 12.5, color: "var(--red)" }}>{traceError}</div>
          )}

          {traceData && (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                <StatusBadge status="ok" label={`Tasks: ${traceData.tasks.length}`} />
                <StatusBadge status="warn" label={`Events: ${traceData.task_events.length}`} />
                <StatusBadge status="muted" label={`Approvals: ${traceData.approvals.length}`} />
                <StatusBadge status="ok" label={`Audit: ${traceData.audit.length}`} />
              </div>

              {unifiedTimeline.length === 0 ? (
                <div style={{ fontSize: 12.5, color: "var(--text-muted)", padding: "12px 0" }}>
                  No correlated records found for <span className="mono">{traceData.trace_id}</span>.
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {unifiedTimeline.map((entry) => (
                    <div
                      key={entry.key}
                      style={{
                        padding: 12,
                        borderRadius: 8,
                        background: "var(--bg-elevated)",
                        border: "1px solid var(--border-hair)",
                        display: "flex",
                        flexDirection: "column",
                        gap: 4,
                      }}
                    >
                      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <span
                            className="mono"
                            style={{
                              fontSize: 10.5,
                              fontWeight: 700,
                              padding: "2px 6px",
                              borderRadius: 4,
                              background: "rgba(99,102,241,0.14)",
                              color: "var(--accent)",
                            }}
                          >
                            {entry.category}
                          </span>
                          <span style={{ fontSize: 12.5, fontWeight: 600 }}>{entry.title}</span>
                          <StatusBadge status={badgeStatus(entry.status)} label={entry.status} />
                        </div>
                        <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                          {entry.ts ? new Date(entry.ts * 1000).toLocaleString() : "—"}
                        </span>
                      </div>
                      <div className="mono" style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                        {entry.subtitle}
                      </div>
                      {entry.detail && entry.detail !== "{}" && (
                        <pre
                          className="terminal"
                          style={{ margin: "4px 0 0", padding: 8, fontSize: 11, maxHeight: 120, whiteSpace: "pre-wrap" }}
                        >
                          {entry.detail}
                        </pre>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </SurfaceCard>
      ) : (
        <SurfaceCard style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
            <Terminal size={14} style={{ color: "var(--text-muted)" }} />
            {(["control", "mcp", "oauth", "nginx"] as const).map((svc) => (
              <Chip key={svc} active={logService === svc} onClick={() => setLogService(svc)}>
                {svc}
              </Chip>
            ))}
          </div>
          <pre className="terminal" style={{ margin: 0, maxHeight: 440, whiteSpace: "pre-wrap" }}>
            {logLines.join("\n") || "No log entries returned."}
          </pre>
        </SurfaceCard>
      )}
    </div>
  );
}

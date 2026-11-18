import { useEffect, useMemo, useState } from "react";
import { Download, ShieldCheck, Terminal } from "lucide-react";
import type { ApiClient } from "../api";
import type { AuditRecord, AuditVerifyResult } from "../types";
import { Button, Chip, StatusBadge, SurfaceCard, UnderlineTabs } from "../components/ui";

export function AuditLogsView({
  api,
  audit,
  onToast,
}: {
  api: ApiClient;
  audit: AuditRecord[];
  onToast: (msg: string) => void;
}) {
  const [tab, setTab] = useState<"ledger" | "logs">("ledger");
  const [query, setQuery] = useState("");
  const [verifyRes, setVerifyRes] = useState<AuditVerifyResult | null>(null);
  const [logService, setLogService] = useState<"control" | "mcp" | "oauth" | "nginx">("control");
  const [logLines, setLogLines] = useState<string[]>([]);

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
        a.decision.toLowerCase().includes(q),
    );
  }, [audit, query]);

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
            Audit Trail &amp; Diagnostics Logs
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Cryptographically hash-chained audit ledger, JSONL compliance exports, and service log tails.
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
          { id: "logs", label: "Service Logs" },
        ]}
      />

      {tab === "ledger" ? (
        <SurfaceCard style={{ overflow: "hidden" }}>
          <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border-hair)" }}>
            <input
              type="text"
              className="field"
              style={{ maxWidth: 320 }}
              placeholder="Filter actor, tool, device, decision..."
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
                    <td className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                      {a.entry_hash ? a.entry_hash.slice(0, 18) + "…" : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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

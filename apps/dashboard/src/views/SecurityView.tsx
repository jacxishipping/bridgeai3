import { useEffect, useState } from "react";
import { Check, RefreshCw, X } from "lucide-react";
import type { ApiClient } from "../api";
import type { ApprovalRecord, AuditRecord, PolicyHistoryEntry, PolicyState } from "../types";
import {
  Accordion,
  Button,
  SurfaceCard,
  TagInput,
  TimelineFeed,
  Toggle,
} from "../components/ui";

export function SecurityView({
  api,
  approvals,
  audit,
  onRefresh,
  onToast,
}: {
  api: ApiClient;
  approvals: ApprovalRecord[];
  audit: AuditRecord[];
  onRefresh: () => Promise<void>;
  onToast: (msg: string) => void;
}) {
  const [policyVersion, setPolicyVersion] = useState(0);
  const [history, setHistory] = useState<PolicyHistoryEntry[]>([]);
  const [policy, setPolicy] = useState<PolicyState>({
    allow_writes: true,
    allow_tier3: false,
    allow_shell: false,
    allow_file_deletion: false,
    allow_package_install: false,
    desktop_enabled: false,
    max_tier_unattended: 2,
    allowed_apps: [],
    note: "Updated via dashboard",
  });
  const [saving, setSaving] = useState(false);

  // Simulator
  const [simTool, setSimTool] = useState("run_shell");
  const [simResult, setSimResult] = useState<string>("");

  async function loadPolicyState() {
    const [cur, hist] = await Promise.all([
      api.getPolicy().catch(() => null),
      api.getPolicyHistory().catch(() => []),
    ]);
    if (cur) {
      setPolicyVersion(cur.version);
      if (cur.policy) setPolicy((p) => ({ ...p, ...cur.policy }));
    }
    setHistory(hist);
  }

  useEffect(() => {
    void loadPolicyState();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  const pendingApprovals = approvals.filter((a) => a.status === "pending");

  const timelineItems = audit.slice(0, 6).map((a) => ({
    id: a.id,
    title: `${a.tool} · ${a.decision}`,
    meta: `${new Date(a.ts * 1000).toLocaleString()} · actor=${a.actor || "system"}`,
    color:
      a.decision === "allowed" || a.decision === "executed"
        ? "var(--emerald)"
        : a.decision === "denied"
          ? "var(--rose)"
          : "var(--accent)",
  }));

  return (
    <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>
          Security Policy, Allowlists &amp; Approvals
        </h1>
        <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
          Review Tier 3/4 human approvals with command parameter inspection, publish versioned policies, and simulate rules.
        </p>
      </div>

      {/* Approvals Queue */}
      <SurfaceCard style={{ padding: 18 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 12,
          }}
        >
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>Pending Human-in-the-Loop Approvals</span>
          <span className="mono" style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {pendingApprovals.length} pending
          </span>
        </div>

        {pendingApprovals.length === 0 ? (
          <div style={{ fontSize: 12.5, color: "var(--text-muted)" }}>
            No Tier 3/4 requests awaiting human approval.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {pendingApprovals.map((ap) => (
              <div
                key={ap.id}
                style={{
                  padding: 12,
                  borderRadius: 8,
                  background: "var(--bg-elevated)",
                  border: "1px solid var(--border-hair)",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                }}
              >
                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
                  <div>
                    <div style={{ fontSize: 12.5, fontWeight: 600 }}>
                      {ap.tool || "task"} <span className="mono">(Tier {ap.tier ?? 3})</span>
                    </div>
                    <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                      {ap.id} · device {ap.device_id || "—"} · by {ap.requested_by || "agent"}
                    </div>
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={async () => {
                        await api.decideApproval(ap.id, "approved", "Approved in dashboard");
                        onToast(`Approved ${ap.id}`);
                        await onRefresh();
                      }}
                    >
                      <Check size={13} /> Approve
                    </Button>
                    <Button
                      size="sm"
                      onClick={async () => {
                        await api.replanApproval(ap.id, "Operator requested non-destructive replan.");
                        onToast(`Sent ${ap.id} for replan`);
                        await onRefresh();
                      }}
                    >
                      <RefreshCw size={12} /> Replan
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={async () => {
                        await api.decideApproval(ap.id, "denied", "Denied in dashboard");
                        onToast(`Denied ${ap.id}`);
                        await onRefresh();
                      }}
                    >
                      <X size={13} /> Deny
                    </Button>
                  </div>
                </div>
                {ap.args_json && (
                  <pre className="terminal" style={{ margin: 0, padding: 10, fontSize: 11 }}>
                    {ap.args_json}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
      </SurfaceCard>

      {/* Policy Controls + Simulator */}
      <div className="split-2">
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>Global Execution Policy</span>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
              v{policyVersion}
            </span>
          </div>

          <Toggle
            checked={policy.allow_writes}
            onChange={(v) => setPolicy((p) => ({ ...p, allow_writes: v }))}
            label="Allow Tier 2 File Writes"
          />
          <Toggle
            checked={policy.allow_tier3}
            onChange={(v) => setPolicy((p) => ({ ...p, allow_tier3: v }))}
            label="Allow Tier 3 System Actions"
          />
          <Toggle
            checked={Boolean(policy.allow_shell)}
            onChange={(v) => setPolicy((p) => ({ ...p, allow_shell: v }))}
            label="Allow Shell Execution (run_shell)"
          />
          <Toggle
            checked={Boolean(policy.desktop_enabled)}
            onChange={(v) => setPolicy((p) => ({ ...p, desktop_enabled: v }))}
            label="Enable Interactive Desktop Automation"
          />
          <Toggle
            checked={Boolean(policy.allow_file_deletion)}
            onChange={(v) => setPolicy((p) => ({ ...p, allow_file_deletion: v }))}
            label="Allow File Deletion (Tier 4)"
          />

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
            <span style={{ fontSize: 12 }}>Max Unattended Tier</span>
            <select
              className="field mono"
              style={{ width: 130 }}
              value={policy.max_tier_unattended}
              onChange={(e) => setPolicy((p) => ({ ...p, max_tier_unattended: Number(e.target.value) }))}
            >
              <option value={1}>Tier 1 (Read)</option>
              <option value={2}>Tier 2 (Write)</option>
              <option value={3}>Tier 3 (System)</option>
              <option value={4}>Tier 4 (Admin)</option>
            </select>
          </div>

          <div>
            <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 6 }}>
              Allowed Desktop Applications:
            </div>
            <TagInput
              tags={policy.allowed_apps ?? []}
              onChange={(apps) => setPolicy((p) => ({ ...p, allowed_apps: apps }))}
              placeholder="Add app binary + Enter..."
            />
          </div>

          <div style={{ paddingTop: 6 }}>
            <Button
              variant="primary"
              disabled={saving}
              onClick={async () => {
                setSaving(true);
                try {
                  const res = await api.savePolicy(policy);
                  setPolicyVersion(res.version);
                  onToast(`Saved global policy v${res.version}`);
                  await loadPolicyState();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Failed to save policy");
                } finally {
                  setSaving(false);
                }
              }}
            >
              {saving ? "Publishing..." : "Publish New Policy Version"}
            </Button>
          </div>
        </SurfaceCard>

        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>Policy Simulator (Dry-Run)</span>
          <p style={{ margin: 0, fontSize: 12, color: "var(--text-secondary)" }}>
            Test how the policy engine evaluates any tool under your draft toggles without executing anything.
          </p>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              type="text"
              className="field mono"
              value={simTool}
              onChange={(e) => setSimTool(e.target.value)}
              placeholder="run_shell, mouse_click, read_file..."
            />
            <Button
              onClick={async () => {
                try {
                  const res = await api.simulatePolicy(simTool, policy);
                  setSimResult(JSON.stringify(res, null, 2));
                } catch (err) {
                  setSimResult(err instanceof Error ? err.message : "Simulation failed");
                }
              }}
            >
              Simulate
            </Button>
          </div>
          <pre className="terminal" style={{ margin: 0, minHeight: 150, whiteSpace: "pre-wrap" }}>
            {simResult || "// Click Simulate to evaluate decide()"}
          </pre>

          {history.length > 0 && (
            <div style={{ marginTop: 4 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
                Recent Policy Versions ({history.length})
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4, maxHeight: 120, overflowY: "auto" }}>
                {history.slice(0, 6).map((h) => (
                  <div key={h.version} className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    v{h.version} · {h.created_by || "admin"} · {h.note || "policy update"}
                  </div>
                ))}
              </div>
            </div>
          )}
        </SurfaceCard>
      </div>

      {/* Audit Preview & Guardrail Accordion */}
      <div className="split-wide">
        <SurfaceCard style={{ padding: 18 }}>
          <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 14 }}>
            Recent Security Decisions
          </div>
          <TimelineFeed items={timelineItems} />
        </SurfaceCard>

        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>
            Guardrail Reference
          </span>
          <Accordion
            items={[
              {
                title: "How do Windows & Control Plane policies interact?",
                body: "Enforcement uses intersection (fail-closed): desktop control and shell execution require opt-in on both the Control Plane and the enrolled Windows agent.",
              },
              {
                title: "What protects device tokens on Windows nodes?",
                body: "Device credentials are sealed with user-scoped Windows DPAPI (CryptProtectData) locally and rotated via /api/agent/rotate.",
              },
              {
                title: "How does Emergency Stop work?",
                body: "Triggering Emergency Stop persists across Control Plane restarts, rejects new tasks, and instructs connected agents to cancel active tasks and release held inputs.",
              },
            ]}
          />
        </div>
      </div>
    </div>
  );
}

import { useEffect, useMemo, useState } from "react";
import { Check, Download, RefreshCw, ShieldAlert, Upload, X } from "lucide-react";
import type { ApiClient } from "../api";
import type { ApprovalRecord, AuditRecord, DeviceRecord, PolicyHistoryEntry, PolicyState } from "../types";
import {
  Accordion,
  Button,
  Modal,
  StateBanner,
  StatusBadge,
  SurfaceCard,
  TagInput,
  TimelineFeed,
  Toggle,
} from "../components/ui";

const DEFAULT_POLICY: PolicyState = {
  allow_writes: true,
  allow_tier3: false,
  allow_shell: false,
  allow_file_deletion: false,
  allow_package_install: false,
  desktop_enabled: false,
  max_tier_unattended: 2,
  allowed_apps: [],
  note: "Updated via dashboard",
};

const POLICY_DIFF_FIELDS: Array<{ key: keyof PolicyState; label: string; riskOnEnable?: boolean }> = [
  { key: "allow_writes", label: "Tier 2 File Writes", riskOnEnable: true },
  { key: "allow_tier3", label: "Tier 3 System Actions", riskOnEnable: true },
  { key: "allow_shell", label: "Shell Execution (run_shell)", riskOnEnable: true },
  { key: "desktop_enabled", label: "Interactive Desktop Automation", riskOnEnable: true },
  { key: "allow_file_deletion", label: "Tier 4 File Deletion", riskOnEnable: true },
  { key: "max_tier_unattended", label: "Max Unattended Tier", riskOnEnable: true },
  { key: "allowed_apps", label: "Allowed Desktop Apps" },
];

const PREPUBLISH_SIM_TOOLS = [
  "get_system_info",
  "write_workspace_file",
  "run_shell",
  "mouse_click",
  "delete_workspace_file",
];

function formatExpiry(expiresAt?: number | null): { label: string; expired: boolean } {
  if (!expiresAt) return { label: "No expiry set", expired: false };
  const remainingSec = Math.floor(expiresAt - Date.now() / 1000);
  if (remainingSec <= 0) return { label: "Expired", expired: true };
  if (remainingSec < 60) return { label: `Expires in ${remainingSec}s`, expired: false };
  const mins = Math.floor(remainingSec / 60);
  return { label: `Expires in ${mins}m ${remainingSec % 60}s`, expired: false };
}

export function SecurityView({
  api,
  approvals,
  audit,
  devices,
  onRefresh,
  onToast,
}: {
  api: ApiClient;
  approvals: ApprovalRecord[];
  audit: AuditRecord[];
  devices: DeviceRecord[];
  onRefresh: () => Promise<void>;
  onToast: (msg: string) => void;
}) {
  const [policyVersion, setPolicyVersion] = useState(0);
  const [history, setHistory] = useState<PolicyHistoryEntry[]>([]);
  const [livePolicy, setLivePolicy] = useState<PolicyState>(DEFAULT_POLICY);
  const [policy, setPolicy] = useState<PolicyState>(DEFAULT_POLICY);
  const [saving, setSaving] = useState(false);

  // Pre-publish Diff + Blast Radius + Simulation Review Modal (Release 3)
  const [reviewOpen, setReviewOpen] = useState(false);
  const [reviewSims, setReviewSims] = useState<Array<{ tool: string; result: Record<string, unknown> }>>([]);
  const [reviewLoading, setReviewLoading] = useState(false);

  // Policy-as-Code Signed Import/Export Modal (Release 4)
  const [importOpen, setImportOpen] = useState(false);
  const [importJson, setImportJson] = useState("");
  const [importPreview, setImportPreview] = useState<Record<string, unknown> | null>(null);
  const [importBusy, setImportBusy] = useState(false);

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
      if (cur.policy) {
        const merged = { ...DEFAULT_POLICY, ...cur.policy };
        setLivePolicy(merged);
        setPolicy(merged);
      }
    }
    setHistory(hist);
  }

  useEffect(() => {
    void loadPolicyState();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  const devicesById = useMemo(() => {
    const map = new Map<string, DeviceRecord>();
    devices.forEach((d) => map.set(d.id, d));
    return map;
  }, [devices]);

  const inheritingDevices = useMemo(
    () => devices.filter((d) => !d.revoked && !d.has_custom_policy),
    [devices],
  );
  const overrideDevicesCount = useMemo(
    () => devices.filter((d) => !d.revoked && d.has_custom_policy).length,
    [devices],
  );

  const policyDiffs = useMemo(() => {
    return POLICY_DIFF_FIELDS.filter(({ key }) => {
      return JSON.stringify(livePolicy[key]) !== JSON.stringify(policy[key]);
    }).map(({ key, label, riskOnEnable }) => {
      const before = livePolicy[key];
      const after = policy[key];
      const widened =
        Boolean(riskOnEnable) &&
        ((typeof after === "boolean" && after && !before) ||
          (typeof after === "number" && typeof before === "number" && after > before));
      return { key, label, before, after, widened };
    });
  }, [livePolicy, policy]);

  async function openPublishReview() {
    setReviewOpen(true);
    setReviewLoading(true);
    try {
      const results = await Promise.all(
        PREPUBLISH_SIM_TOOLS.map(async (tool) => {
          try {
            const res = await api.simulatePolicy(tool, policy);
            return { tool, result: res };
          } catch (err) {
            return { tool, result: { decision: "error", reason: err instanceof Error ? err.message : "sim error" } };
          }
        }),
      );
      setReviewSims(results);
    } finally {
      setReviewLoading(false);
    }
  }

  async function confirmPublishPolicy() {
    setSaving(true);
    try {
      const res = await api.savePolicy(policy);
      setPolicyVersion(res.version);
      setReviewOpen(false);
      onToast(`Published global policy v${res.version}`);
      await loadPolicyState();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Failed to save policy");
    } finally {
      setSaving(false);
    }
  }

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
          Review Tier 3/4 human approvals with rule, device context, expiry, and blast radius; diff and simulate policies before publishing.
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
          <StateBanner
            kind="empty"
            title="No pending approvals"
            detail="Tier 3/4 requests requiring human confirmation will appear here with full arguments, device context, expiry, and blast radius."
          />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {pendingApprovals.map((ap) => {
              const dev = ap.device_id ? devicesById.get(ap.device_id) : undefined;
              const expiry = formatExpiry(ap.expires_at);
              return (
                <div
                  key={ap.id}
                  style={{
                    padding: 14,
                    borderRadius: 8,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-hair)",
                    display: "flex",
                    flexDirection: "column",
                    gap: 10,
                  }}
                >
                  <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
                    <div>
                      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, fontSize: 13, fontWeight: 600 }}>
                        <span>{ap.tool || "task"}</span>
                        <StatusBadge status={(ap.tier ?? 3) >= 4 ? "err" : "warn"} label={`Tier ${ap.tier ?? 3}`} />
                        <StatusBadge status={expiry.expired ? "err" : "muted"} label={expiry.label} />
                      </div>
                      <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                        id={ap.id} · requested_by={ap.requested_by || "agent"}
                      </div>
                    </div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                      <Button
                        size="sm"
                        variant="primary"
                        disabled={expiry.expired}
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

                  {/* Rule, Device Context & Blast Radius Grid */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                      gap: 8,
                      padding: 10,
                      borderRadius: 6,
                      background: "var(--bg-card)",
                      border: "1px solid var(--border-hair)",
                      fontSize: 11.5,
                    }}
                  >
                    <div>
                      <div style={{ color: "var(--text-muted)", fontSize: 10.5 }}>Rule / Reason</div>
                      <div className="mono">{ap.reason || `Tier ${ap.tier ?? 3} requires explicit human approval`}</div>
                    </div>
                    <div>
                      <div style={{ color: "var(--text-muted)", fontSize: 10.5 }}>Device Context</div>
                      <div className="mono">
                        {dev
                          ? `${dev.name} (${dev.os || "node"} · ${dev.status}${dev.group ? ` · grp:${dev.group}` : ""})`
                          : ap.device_id || "local"}
                      </div>
                    </div>
                    <div>
                      <div style={{ color: "var(--text-muted)", fontSize: 10.5 }}>Blast Radius</div>
                      <div className="mono">
                        1 endpoint ({dev?.name || ap.device_id || "local"})
                      </div>
                    </div>
                  </div>

                  {ap.args_json && (
                    <div>
                      <div style={{ fontSize: 10.5, color: "var(--text-muted)", marginBottom: 4 }}>
                        Exact Invocation Arguments:
                      </div>
                      <pre className="terminal" style={{ margin: 0, padding: 10, fontSize: 11 }}>
                        {ap.args_json}
                      </pre>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </SurfaceCard>

      {/* Policy Controls + Simulator */}
      <div className="split-2">
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>Global Execution Policy</span>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
              v{policyVersion} · {inheritingDevices.length} inheriting
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

          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8, paddingTop: 6 }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              <Button
                size="sm"
                onClick={async () => {
                  try {
                    const bundle = await api.exportPolicyBundle();
                    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement("a");
                    a.href = url;
                    a.download = `policy-bundle-v${bundle.source_version}.json`;
                    a.click();
                    URL.revokeObjectURL(url);
                    onToast(`Exported signed policy bundle (v${bundle.source_version})`);
                  } catch (err) {
                    onToast(err instanceof Error ? err.message : "Export failed");
                  }
                }}
              >
                <Download size={12} /> Export Signed
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  setImportPreview(null);
                  setImportOpen(true);
                }}
              >
                <Upload size={12} /> Import Signed
              </Button>
            </div>
            <Button
              variant="primary"
              disabled={saving}
              onClick={() => void openPublishReview()}
            >
              <ShieldAlert size={13} /> Review Diff &amp; Publish ({policyDiffs.length})
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

      {/* Pre-Publish Diff, Blast Radius & Simulation Review Modal (Release 3) */}
      <Modal
        open={reviewOpen}
        title={`Pre-Publish Review · Policy v${policyVersion} → v${policyVersion + 1}`}
        onClose={() => setReviewOpen(false)}
        maxWidth={580}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <StateBanner
            kind={policyDiffs.some((d) => d.widened) ? "denied" : "degraded"}
            title={`Blast Radius: ${inheritingDevices.length} active device${inheritingDevices.length === 1 ? "" : "s"} inherit global policy`}
            detail={`${overrideDevicesCount} device${overrideDevicesCount === 1 ? "" : "s"} use custom per-device overrides and will not be overwritten.${
              policyDiffs.some((d) => d.widened)
                ? " One or more changes widen execution permissions."
                : ""
            }`}
          />

          <div>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
              Policy Diff ({policyDiffs.length} field{policyDiffs.length === 1 ? "" : "s"} changed)
            </div>
            {policyDiffs.length === 0 ? (
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                No toggle differences detected against live v{policyVersion}.
              </div>
            ) : (
              <SurfaceCard style={{ overflow: "hidden" }}>
                <table className="saas-table" style={{ fontSize: 11.5 }}>
                  <thead>
                    <tr>
                      <th>Setting</th>
                      <th>Current (v{policyVersion})</th>
                      <th>Proposed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {policyDiffs.map((d) => (
                      <tr key={String(d.key)}>
                        <td>{d.label}</td>
                        <td className="mono" style={{ color: "var(--text-muted)" }}>
                          {JSON.stringify(d.before)}
                        </td>
                        <td
                          className="mono"
                          style={{
                            fontWeight: 600,
                            color: d.widened ? "var(--amber)" : "var(--emerald)",
                          }}
                        >
                          {JSON.stringify(d.after)} {d.widened ? "▲ wider" : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </SurfaceCard>
            )}
          </div>

          <div>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
              Pre-Publish Simulation Results
            </div>
            {reviewLoading ? (
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Simulating representative tools...</div>
            ) : (
              <SurfaceCard style={{ overflow: "hidden" }}>
                <table className="saas-table" style={{ fontSize: 11.5 }}>
                  <thead>
                    <tr>
                      <th>Tool</th>
                      <th>Tier</th>
                      <th>Decision</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reviewSims.map(({ tool, result }) => {
                      const decision = String(result.decision ?? (result.allowed ? "allowed" : "denied"));
                      return (
                        <tr key={tool}>
                          <td className="mono">{tool}</td>
                          <td className="mono">Tier {String(result.tier ?? "?")}</td>
                          <td>
                            <StatusBadge
                              status={
                                decision === "allowed"
                                  ? "ok"
                                  : decision === "needs_approval"
                                    ? "warn"
                                    : "err"
                              }
                              label={decision}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </SurfaceCard>
            )}
          </div>

          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, paddingTop: 4 }}>
            <Button variant="ghost" disabled={saving} onClick={() => setReviewOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" disabled={saving} onClick={() => void confirmPublishPolicy()}>
              {saving ? "Publishing..." : `Confirm & Publish v${policyVersion + 1}`}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Policy-as-Code Signed Import Modal (Release 4) */}
      <Modal
        open={importOpen}
        title="Import Signed Policy Bundle (Policy-as-Code)"
        onClose={() => setImportOpen(false)}
        maxWidth={560}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
            Paste a signed <span className="mono">ai-bridge-policy-bundle/v1</span> JSON document. Signatures and SHA-256 digests are verified server-side; run Dry-Run first to inspect diffs and simulations.
          </div>
          <textarea
            rows={7}
            className="field mono"
            placeholder='{"schema": "ai-bridge-policy-bundle/v1", "signature": "...", ...}'
            value={importJson}
            onChange={(e) => {
              setImportJson(e.target.value);
              setImportPreview(null);
            }}
          />
          {importPreview && (
            <pre className="terminal" style={{ margin: 0, maxHeight: 180, whiteSpace: "pre-wrap", fontSize: 11 }}>
              {JSON.stringify(importPreview, null, 2)}
            </pre>
          )}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" disabled={importBusy} onClick={() => setImportOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={importBusy || !importJson.trim()}
              onClick={async () => {
                setImportBusy(true);
                try {
                  const parsed = JSON.parse(importJson) as Record<string, unknown>;
                  const res = await api.importPolicyBundle(parsed, true);
                  setImportPreview(res);
                  onToast("Signature verified (dry-run)");
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Dry-run verification failed");
                } finally {
                  setImportBusy(false);
                }
              }}
            >
              Verify &amp; Dry-Run
            </Button>
            <Button
              variant="primary"
              disabled={importBusy || !importPreview}
              onClick={async () => {
                setImportBusy(true);
                try {
                  const parsed = JSON.parse(importJson) as Record<string, unknown>;
                  const res = await api.importPolicyBundle(parsed, false);
                  setImportOpen(false);
                  onToast(`Imported signed policy v${String(res.version ?? "")}`);
                  await loadPolicyState();
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Import apply failed");
                } finally {
                  setImportBusy(false);
                }
              }}
            >
              Apply Signed Bundle
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

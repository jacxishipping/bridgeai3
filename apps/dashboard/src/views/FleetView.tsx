import { useMemo, useState } from "react";
import { ArrowUpDown, Plus, RefreshCw, ShieldAlert } from "lucide-react";
import type { ApiClient } from "../api";
import type { DeviceOverview, DeviceRecord, EnrollResult } from "../types";
import {
  Button,
  Chip,
  Modal,
  SlideOverDrawer,
  StatusBadge,
  SurfaceCard,
  TagInput,
  Toggle,
  UnderlineTabs,
} from "../components/ui";

export function FleetView({
  api,
  devices,
  selectedDevice,
  onSelectDevice,
  onRefresh,
  onToast,
}: {
  api: ApiClient;
  devices: DeviceRecord[];
  selectedDevice: DeviceRecord | null;
  onSelectDevice: (dev: DeviceRecord | null) => void;
  onRefresh: () => Promise<void>;
  onToast: (msg: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [osFilter, setOsFilter] = useState<"all" | "windows" | "linux" | "macos">("all");
  const [sortKey, setSortKey] = useState<"name" | "status" | "os">("name");
  const [asc, setAsc] = useState(true);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Enrollment Wizard Modal State
  const [enrollOpen, setEnrollOpen] = useState(false);
  const [wizStep, setWizStep] = useState<1 | 2 | 3>(1);
  const [devName, setDevName] = useState("win-workstation-01");
  const [devHost, setDevHost] = useState("DESKTOP-NODE");
  const [devOs, setDevOs] = useState("Windows 11 Pro");
  const [wizWrites, setWizWrites] = useState(true);
  const [wizTier3, setWizTier3] = useState(false);
  const [wizDesktop, setWizDesktop] = useState(false);
  const [enrollResult, setEnrollResult] = useState<EnrollResult | null>(null);
  const [enrolling, setEnrolling] = useState(false);

  // Drawer Inspector State
  const [drawerTab, setDrawerTab] = useState<"overview" | "processes" | "policy" | "tags">("overview");
  const [overview, setOverview] = useState<DeviceOverview | null>(null);
  const [localGroup, setLocalGroup] = useState("");
  const [localTags, setLocalTags] = useState<string[]>(["windows", "workstation"]);
  const [polWrites, setPolWrites] = useState(true);
  const [polTier3, setPolTier3] = useState(false);
  const [polDesktop, setPolDesktop] = useState(false);
  const [polShell, setPolShell] = useState(false);
  const [savingPolicy, setSavingPolicy] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return devices
      .filter((d) => {
        if (osFilter !== "all" && !(d.os || "").toLowerCase().includes(osFilter)) {
          return false;
        }
        if (!q) return true;
        return (
          d.name.toLowerCase().includes(q) ||
          d.id.toLowerCase().includes(q) ||
          (d.hostname || "").toLowerCase().includes(q) ||
          (d.group || "").toLowerCase().includes(q)
        );
      })
      .sort((a, b) => {
        const av = String(a[sortKey] ?? "").toLowerCase();
        const bv = String(b[sortKey] ?? "").toLowerCase();
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      });
  }, [devices, query, osFilter, sortKey, asc]);

  async function openInspector(dev: DeviceRecord) {
    onSelectDevice(dev);
    setDrawerTab("overview");
    setLocalGroup(dev.group || "");
    setLocalTags(dev.tags?.length ? dev.tags : ["workstation", (dev.os || "node").toLowerCase()]);
    const p = dev.policy || {};
    setPolWrites(Boolean(p.allow_writes ?? true));
    setPolTier3(Boolean(p.allow_tier3 ?? false));
    setPolDesktop(Boolean(p.desktop_enabled ?? false));
    setPolShell(Boolean(p.allow_shell ?? false));
    setOverview(null);
    try {
      const ov = await api.getDeviceOverview(dev.id);
      setOverview(ov);
    } catch {
      // Non-fatal
    }
  }

  async function handleEnrollFinish() {
    setEnrolling(true);
    try {
      const res = await api.enrollDevice({
        name: devName.trim() || "workstation-node",
        hostname: devHost.trim() || devName.trim(),
        os: devOs,
        policy: {
          inherit_global: false,
          allow_writes: wizWrites,
          allow_tier3: wizTier3,
          desktop_enabled: wizDesktop,
          allow_shell: wizTier3,
          max_tier_unattended: wizTier3 ? 3 : 2,
        },
      });
      setEnrollResult(res);
      onToast(`Enrolled ${res.device_id}`);
      await onRefresh();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Enrollment failed");
    } finally {
      setEnrolling(false);
    }
  }

  async function handleSaveDevicePolicy() {
    if (!selectedDevice) return;
    setSavingPolicy(true);
    try {
      await api.setDevicePolicy(selectedDevice.id, {
        inherit_global: false,
        allow_writes: polWrites,
        allow_tier3: polTier3,
        desktop_enabled: polDesktop,
        allow_shell: polShell,
        max_tier_unattended: polTier3 ? 3 : 2,
      });
      onToast(`Updated policy for ${selectedDevice.name}`);
      await onRefresh();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Failed to save device policy");
    } finally {
      setSavingPolicy(false);
    }
  }

  async function handleRevoke(deviceId: string) {
    try {
      await api.revokeDevice(deviceId);
      onToast(`Device ${deviceId} revoked`);
      onSelectDevice(null);
      await onRefresh();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Revoke failed");
    }
  }

  function toggleSort(key: "name" | "status" | "os") {
    if (sortKey === key) setAsc((v) => !v);
    else {
      setSortKey(key);
      setAsc(true);
    }
  }

  const origin = typeof window !== "undefined" ? window.location.origin : "https://your-plane";

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
            Fleet &amp; Device 360
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Enroll endpoints, inspect live host CPU/RAM/processes, manage group tags, and override guardrails.
          </p>
        </div>

        <Button
          variant="primary"
          onClick={() => {
            setEnrollResult(null);
            setWizStep(1);
            setEnrollOpen(true);
          }}
        >
          <Plus size={14} /> Enroll Node
        </Button>
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
              style={{ width: 240 }}
              placeholder="Search node name, host, group, ID..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <div style={{ display: "flex", gap: 4 }}>
              {(["all", "windows", "linux", "macos"] as const).map((os) => (
                <Chip key={os} active={osFilter === os} onClick={() => setOsFilter(os)}>
                  {os === "all" ? "All OS" : os.toUpperCase()}
                </Chip>
              ))}
            </div>
          </div>

          <span className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
            {selectedIds.size} selected · {filtered.length} visible
          </span>
        </div>

        <div className="table-wrap">
          <table className="saas-table">
            <thead>
              <tr>
                <th style={{ width: 34 }}>
                  <input
                    type="checkbox"
                    aria-label="Select all nodes"
                    checked={filtered.length > 0 && filtered.every((d) => selectedIds.has(d.id))}
                    onChange={(e) => {
                      if (e.target.checked) setSelectedIds(new Set(filtered.map((d) => d.id)));
                      else setSelectedIds(new Set());
                    }}
                  />
                </th>
                <th style={{ cursor: "pointer" }} onClick={() => toggleSort("name")}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    Node <ArrowUpDown size={12} />
                  </span>
                </th>
                <th style={{ cursor: "pointer" }} onClick={() => toggleSort("os")}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    OS <ArrowUpDown size={12} />
                  </span>
                </th>
                <th style={{ cursor: "pointer" }} onClick={() => toggleSort("status")}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    Status <ArrowUpDown size={12} />
                  </span>
                </th>
                <th>Tags / Group</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} style={{ textAlign: "center", padding: 28, color: "var(--text-muted)" }}>
                    No nodes match your current filter.
                  </td>
                </tr>
              ) : (
                filtered.map((dev) => {
                  const st = dev.revoked ? "err" : dev.status === "online" ? "ok" : "muted";
                  return (
                    <tr key={dev.id} style={{ cursor: "pointer" }} onClick={() => void openInspector(dev)}>
                      <td onClick={(e) => e.stopPropagation()} style={{ cursor: "default" }}>
                        <input
                          type="checkbox"
                          checked={selectedIds.has(dev.id)}
                          onChange={(e) => {
                            const next = new Set(selectedIds);
                            if (e.target.checked) next.add(dev.id);
                            else next.delete(dev.id);
                            setSelectedIds(next);
                          }}
                        />
                      </td>
                      <td>
                        <div style={{ fontWeight: 500 }}>{dev.name}</div>
                        <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                          {dev.id} · {dev.hostname || "—"}
                        </div>
                      </td>
                      <td>{dev.os || "—"}</td>
                      <td>
                        <StatusBadge
                          status={st}
                          label={dev.revoked ? "Revoked" : dev.status || "offline"}
                        />
                      </td>
                      <td className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                        {[dev.group, ...(dev.tags || [])].filter(Boolean).join(", ") || "—"}
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <Button size="sm" onClick={() => void openInspector(dev)}>
                          Inspect
                        </Button>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </SurfaceCard>

      {/* Multi-Step Enrollment Wizard Modal */}
      <Modal
        open={enrollOpen}
        title="Enroll Workstation or Agent"
        onClose={() => setEnrollOpen(false)}
        maxWidth={540}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div
            style={{
              height: 4,
              borderRadius: 999,
              background: "var(--bg-elevated)",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${(wizStep / 3) * 100}%`,
                background: "var(--accent)",
                transition: "width 0.25s var(--ease-out)",
              }}
            />
          </div>

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(3, 1fr)",
              gap: 8,
              paddingBottom: 12,
              borderBottom: "1px solid var(--border-hair)",
              fontSize: 12,
            }}
          >
            {[
              [1, "Identity"],
              [2, "Guardrails"],
              [3, "Token & Install"],
            ].map(([n, lbl]) => (
              <div
                key={String(n)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  opacity: wizStep === n ? 1 : 0.45,
                  fontWeight: 600,
                }}
              >
                <span
                  style={{
                    width: 22,
                    height: 22,
                    borderRadius: "50%",
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 11,
                    background: wizStep === n ? "var(--accent)" : "var(--bg-elevated)",
                    color: wizStep === n ? "#fff" : "var(--text-primary)",
                  }}
                >
                  {n}
                </span>
                {lbl}
              </div>
            ))}
          </div>

          {wizStep === 1 && (
            <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div>
                <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>
                  Display Name
                </label>
                <input type="text" className="field" value={devName} onChange={(e) => setDevName(e.target.value)} />
              </div>
              <div className="split-2">
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>
                    Hostname
                  </label>
                  <input type="text" className="field mono" value={devHost} onChange={(e) => setDevHost(e.target.value)} />
                </div>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 5 }}>
                    Operating System
                  </label>
                  <select className="field" value={devOs} onChange={(e) => setDevOs(e.target.value)}>
                    <option value="Windows 11 Pro">Windows 11 Pro</option>
                    <option value="Windows Server 2022">Windows Server 2022</option>
                    <option value="Ubuntu 24.04 LTS">Ubuntu 24.04 LTS</option>
                    <option value="macOS Sequoia">macOS Sequoia</option>
                  </select>
                </div>
              </div>
            </div>
          )}

          {wizStep === 2 && (
            <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <Toggle checked={wizWrites} onChange={setWizWrites} label="Allow Tier 2 Workspace Writes" />
              <Toggle checked={wizTier3} onChange={setWizTier3} label="Allow Tier 3 Shell / Execution" />
              <Toggle checked={wizDesktop} onChange={setWizDesktop} label="Enable Interactive Desktop Automation" />
            </div>
          )}

          {wizStep === 3 && (
            <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {!enrollResult ? (
                <div style={{ fontSize: 12.5, color: "var(--text-secondary)", lineHeight: 1.55 }}>
                  Ready to mint a one-time device token for{" "}
                  <span className="mono" style={{ color: "var(--text-primary)", fontWeight: 600 }}>
                    {devName}
                  </span>{" "}
                  ({devOs}). The token is shown once upon creation.
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  <StatusBadge status="ok" label={`Device ${enrollResult.device_id} Enrolled`} />
                  <div className="terminal">
                    <div style={{ color: "#94a3b8", marginBottom: 4 }}>One-Time Device Token:</div>
                    <div style={{ wordBreak: "break-all", color: "#34d399" }}>
                      {enrollResult.device_token}
                    </div>
                    <div style={{ color: "#94a3b8", marginTop: 10, marginBottom: 4 }}>
                      Quick Install Command ({devOs.toLowerCase().includes("win") ? "PowerShell" : "Bash"}):
                    </div>
                    <div style={{ wordBreak: "break-all", color: "#e2e8f0" }}>
                      {devOs.toLowerCase().includes("win")
                        ? `$env:AI_BRIDGE_TOKEN="${enrollResult.device_token}"; irm ${origin}/install.ps1 | iex`
                        : `curl -fsSL ${origin}/install.sh | sudo bash -s -- --token ${enrollResult.device_token} --url ${origin}`}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button
                      size="sm"
                      onClick={() => {
                        void navigator.clipboard?.writeText(enrollResult.device_token);
                        onToast("Copied device token");
                      }}
                    >
                      Copy Token
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              paddingTop: 12,
              borderTop: "1px solid var(--border-hair)",
            }}
          >
            <Button
              disabled={wizStep === 1 || Boolean(enrollResult)}
              onClick={() => setWizStep((s) => (s === 3 ? 2 : 1))}
            >
              Back
            </Button>
            {wizStep < 3 ? (
              <Button variant="primary" onClick={() => setWizStep((s) => (s === 1 ? 2 : 3))}>
                Continue
              </Button>
            ) : !enrollResult ? (
              <Button variant="primary" disabled={enrolling} onClick={handleEnrollFinish}>
                {enrolling ? "Minting Token..." : "Issue Device Token"}
              </Button>
            ) : (
              <Button variant="primary" onClick={() => setEnrollOpen(false)}>
                Done
              </Button>
            )}
          </div>
        </div>
      </Modal>

      {/* Slide-Over Device 360 Inspector Drawer */}
      <SlideOverDrawer
        open={Boolean(selectedDevice)}
        title={selectedDevice ? `Device 360 · ${selectedDevice.name}` : "Node Inspector"}
        subtitle={selectedDevice ? `${selectedDevice.id} · ${selectedDevice.os || "Unknown OS"}` : undefined}
        onClose={() => onSelectDevice(null)}
        footer={
          selectedDevice && (
            <>
              <Button
                variant="danger"
                onClick={() => void handleRevoke(selectedDevice.id)}
                disabled={Boolean(selectedDevice.revoked)}
              >
                <ShieldAlert size={13} /> Revoke Node
              </Button>
              <Button variant="primary" onClick={() => onSelectDevice(null)}>
                Done
              </Button>
            </>
          )
        }
      >
        {selectedDevice && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <UnderlineTabs
              value={drawerTab}
              onChange={setDrawerTab}
              tabs={[
                { id: "overview", label: "360 Telemetry" },
                { id: "processes", label: "System" },
                { id: "policy", label: "Policy" },
                { id: "tags", label: "Group & Tags" },
              ]}
            />

            {drawerTab === "overview" && (
              <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <SurfaceCard style={{ padding: 14, display: "flex", flexDirection: "column", gap: 8 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                    <span style={{ color: "var(--text-muted)" }}>Connection State</span>
                    <StatusBadge
                      status={selectedDevice.revoked ? "err" : selectedDevice.status === "online" ? "ok" : "muted"}
                      label={selectedDevice.revoked ? "Revoked" : selectedDevice.status}
                    />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                    <span style={{ color: "var(--text-muted)" }}>Hostname</span>
                    <span className="mono">{selectedDevice.hostname || "—"}</span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                    <span style={{ color: "var(--text-muted)" }}>IP Address</span>
                    <span className="mono">{selectedDevice.ip_address || "—"}</span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                    <span style={{ color: "var(--text-muted)" }}>CPU / Memory</span>
                    <span className="mono">
                      {overview?.resource_usage?.cpu_percent !== undefined && overview.resource_usage.cpu_percent !== null
                        ? `${overview.resource_usage.cpu_percent}% CPU / ${overview.resource_usage.memory_percent ?? "—"}% RAM`
                        : "Dispatch telemetry refresh"}
                    </span>
                  </div>
                </SurfaceCard>

                <div style={{ display: "flex", gap: 8 }}>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={async () => {
                      await api.refreshDeviceTelemetry(selectedDevice.id);
                      onToast("Queued diagnostics refresh (sysinfo, CPU/RAM, network)");
                    }}
                  >
                    <RefreshCw size={12} /> Dispatch Refresh
                  </Button>
                  <Button size="sm" onClick={() => void openInspector(selectedDevice)}>
                    Reload Overview
                  </Button>
                </div>
              </div>
            )}

            {drawerTab === "processes" && (
              <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                  Reported System Info &amp; Processes ({overview?.processes?.length ?? 0}):
                </div>
                <pre className="terminal" style={{ margin: 0, maxHeight: 360, whiteSpace: "pre-wrap" }}>
                  {JSON.stringify(
                    {
                      system_info: overview?.system_info ?? {},
                      processes: (overview?.processes ?? []).slice(0, 15),
                      network: overview?.network ?? null,
                    },
                    null,
                    2,
                  )}
                </pre>
              </div>
            )}

            {drawerTab === "policy" && (
              <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <Toggle checked={polWrites} onChange={setPolWrites} label="Allow Tier 2 File Writes" />
                <Toggle checked={polTier3} onChange={setPolTier3} label="Allow Tier 3 Unattended" />
                <Toggle checked={polShell} onChange={setPolShell} label="Allow Shell Execution (run_shell)" />
                <Toggle checked={polDesktop} onChange={setPolDesktop} label="Allow Desktop Control" />

                <div style={{ display: "flex", gap: 8, paddingTop: 10 }}>
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={savingPolicy}
                    onClick={() => void handleSaveDevicePolicy()}
                  >
                    {savingPolicy ? "Saving..." : "Save Override"}
                  </Button>
                  <Button
                    size="sm"
                    onClick={async () => {
                      await api.resetDevicePolicy(selectedDevice.id);
                      onToast("Reset to global policy");
                      await onRefresh();
                    }}
                  >
                    Reset to Global
                  </Button>
                </div>
              </div>
            )}

            {drawerTab === "tags" && (
              <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 4 }}>
                    Fleet Group
                  </label>
                  <input
                    type="text"
                    className="field mono"
                    placeholder="e.g. prod-windows, finance-workstations"
                    value={localGroup}
                    onChange={(e) => setLocalGroup(e.target.value)}
                  />
                </div>
                <div>
                  <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 6 }}>
                    Tags (press Enter to add):
                  </div>
                  <TagInput tags={localTags} onChange={setLocalTags} />
                </div>
                <div>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={async () => {
                      await api.setDeviceMetadata(selectedDevice.id, localGroup, localTags);
                      onToast(`Saved metadata for ${selectedDevice.name}`);
                      await onRefresh();
                    }}
                  >
                    Save Group &amp; Tags
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </SlideOverDrawer>
    </div>
  );
}

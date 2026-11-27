import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowUpDown, Bookmark, Plus, RefreshCw, ShieldAlert, Trash2, Zap } from "lucide-react";
import type { ApiClient } from "../api";
import type {
  DeviceIntelligenceProfile,
  DeviceOverview,
  DeviceRecord,
  EnrollResult,
  PolicyState,
} from "../types";
import {
  Button,
  Chip,
  Modal,
  SlideOverDrawer,
  StateBanner,
  StatusBadge,
  SurfaceCard,
  TagInput,
  Toggle,
  UnderlineTabs,
} from "../components/ui";

interface SavedFleetFilter {
  id: string;
  name: string;
  query: string;
  osFilter: "all" | "windows" | "linux" | "macos";
  groupFilter: string;
  statusFilter: "all" | "online" | "offline" | "revoked";
}

const SAVED_FILTERS_KEY = "aibridge.fleet.savedFilters.v1";
const PAGE_SIZE = 15;

function loadSavedFilters(): SavedFleetFilter[] {
  try {
    const raw = localStorage.getItem(SAVED_FILTERS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as SavedFleetFilter[]) : [];
  } catch {
    return [];
  }
}

function saveFiltersStorage(filters: SavedFleetFilter[]): void {
  try {
    localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify(filters));
  } catch {
    // Ignore storage errors
  }
}

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
  const [groupFilter, setGroupFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | "online" | "offline" | "revoked">("all");
  const [sortKey, setSortKey] = useState<"name" | "status" | "os">("name");
  const [asc, setAsc] = useState(true);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Server-side pagination state (Release 3)
  const [page, setPage] = useState(1);
  const [pagedDevices, setPagedDevices] = useState<DeviceRecord[] | null>(null);
  const [totalDevices, setTotalDevices] = useState<number>(devices.length);
  const [pageError, setPageError] = useState<string | null>(null);

  // Saved Filters (Release 3)
  const [savedFilters, setSavedFilters] = useState<SavedFleetFilter[]>(() => loadSavedFilters());
  const [filterName, setFilterName] = useState("");
  const [saveFilterOpen, setSaveFilterOpen] = useState(false);

  // Bulk Action Safeguard Modal (Release 3)
  const [bulkAction, setBulkAction] = useState<"refresh" | "revoke" | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

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
  const [deviceIntel, setDeviceIntel] = useState<DeviceIntelligenceProfile | null>(null);
  const [intelActionBusy, setIntelActionBusy] = useState<string | null>(null);
  const [globalPolicy, setGlobalPolicy] = useState<PolicyState | null>(null);
  const [localGroup, setLocalGroup] = useState("");
  const [localTags, setLocalTags] = useState<string[]>([]);
  const [polWrites, setPolWrites] = useState(true);
  const [polTier3, setPolTier3] = useState(false);
  const [polTier4, setPolTier4] = useState(false);
  const [polDesktop, setPolDesktop] = useState(false);
  const [polShell, setPolShell] = useState(false);
  const [polPackageInstall, setPolPackageInstall] = useState(false);
  const [polClipboard, setPolClipboard] = useState(true);
  const [polFullPc, setPolFullPc] = useState(false);
  const [polRequireLocalApproval, setPolRequireLocalApproval] = useState(false);
  const [polMaxTier, setPolMaxTier] = useState<number>(2);
  const [polAllowedApps, setPolAllowedApps] = useState<string[]>([]);
  const [savingPolicy, setSavingPolicy] = useState(false);

  const availableGroups = useMemo(() => {
    const set = new Set<string>();
    devices.forEach((d) => {
      if (d.group?.trim()) set.add(d.group.trim());
    });
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [devices]);

  const fetchServerPage = useCallback(async () => {
    try {
      const res = await api.listDevicesPaged({
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
        group: groupFilter !== "all" ? groupFilter : undefined,
        os: osFilter !== "all" ? osFilter : undefined,
        status: statusFilter !== "all" ? statusFilter : undefined,
        q: query.trim() || undefined,
      });
      setPagedDevices(res.items);
      setTotalDevices(res.total);
      setPageError(null);
    } catch (err) {
      setPagedDevices(null);
      setPageError(err instanceof Error ? err.message : "Failed to load device page");
    }
  }, [api, page, groupFilter, osFilter, statusFilter, query]);

  useEffect(() => {
    void fetchServerPage();
  }, [fetchServerPage, devices.length]);

  const filtered = useMemo(() => {
    const source = pagedDevices ?? devices.filter((d) => {
      if (osFilter !== "all" && !(d.os || "").toLowerCase().includes(osFilter)) return false;
      if (groupFilter !== "all" && (d.group || "") !== groupFilter) return false;
      if (statusFilter !== "all") {
        const eff = d.revoked ? "revoked" : (d.status || "offline").toLowerCase();
        if (eff !== statusFilter) return false;
      }
      const q = query.trim().toLowerCase();
      if (!q) return true;
      return (
        d.name.toLowerCase().includes(q) ||
        d.id.toLowerCase().includes(q) ||
        (d.hostname || "").toLowerCase().includes(q) ||
        (d.group || "").toLowerCase().includes(q) ||
        (d.tags || []).some((t) => t.toLowerCase().includes(q))
      );
    });
    return [...source].sort((a, b) => {
      const av = String(a[sortKey] ?? "").toLowerCase();
      const bv = String(b[sortKey] ?? "").toLowerCase();
      return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
  }, [pagedDevices, devices, query, osFilter, groupFilter, statusFilter, sortKey, asc]);

  const totalCount = pagedDevices ? totalDevices : filtered.length;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));

  function applyPolicyToForm(
    customPol: Record<string, unknown> | undefined,
    effectivePol: Record<string, unknown> | undefined,
    globalPol: PolicyState | null,
    hasCustom: boolean,
  ) {
    const src = (hasCustom ? { ...(effectivePol || {}), ...(customPol || {}) } : (effectivePol || globalPol || {})) as Record<
      string,
      unknown
    >;
    setPolWrites(Boolean(src.allow_writes ?? true));
    setPolTier3(Boolean(src.allow_tier3 ?? false));
    setPolTier4(Boolean(src.allow_file_deletion ?? false));
    setPolDesktop(Boolean(src.desktop_enabled ?? false));
    setPolShell(Boolean(src.allow_shell ?? false));
    setPolPackageInstall(Boolean(src.allow_package_install ?? false));
    setPolClipboard(Boolean(src.allow_clipboard ?? true));
    setPolFullPc(Boolean(src.allow_full_pc_access ?? src.unrestricted ?? false));
    setPolRequireLocalApproval(Boolean(src.require_local_approval ?? false));
    const rawTier = Number(src.max_tier_unattended ?? (src.allow_tier3 ? 3 : 2));
    setPolMaxTier(Number.isFinite(rawTier) ? Math.min(4, Math.max(1, rawTier)) : 2);
    setPolAllowedApps(Array.isArray(src.allowed_apps) ? (src.allowed_apps as string[]).map(String) : []);
  }

  async function openInspector(dev: DeviceRecord, keepTab = false) {
    onSelectDevice(dev);
    if (!keepTab) setDrawerTab("overview");
    setLocalGroup(dev.group || "");
    // Release 3 truthfulness: show actual tags (never invent default placeholder tags)
    setLocalTags(dev.tags ?? []);
    applyPolicyToForm(dev.policy, dev.effective_policy, globalPolicy, Boolean(dev.has_custom_policy));
    setOverview(null);
    setDeviceIntel(null);
    const [ov, gp, intel] = await Promise.all([
      api.getDeviceOverview(dev.id).catch(() => null),
      api.getPolicy().catch(() => null),
      api.getDeviceIntelligence(dev.id).catch(() => null),
    ]);
    if (ov) {
      setOverview(ov);
      if (ov.device) {
        onSelectDevice({ ...dev, ...ov.device });
      }
    }
    if (intel) setDeviceIntel(intel);
    const resolvedGlobal = gp?.policy ?? globalPolicy;
    if (gp?.policy) {
      setGlobalPolicy(gp.policy);
    }
    const resolvedDev = ov?.device ?? dev;
    applyPolicyToForm(
      resolvedDev.policy,
      resolvedDev.effective_policy,
      resolvedGlobal,
      Boolean(resolvedDev.has_custom_policy),
    );
  }

  function handleSaveCurrentFilter() {
    const trimmed = filterName.trim();
    if (!trimmed) return;
    const entry: SavedFleetFilter = {
      id: `flt-${Date.now().toString(36)}`,
      name: trimmed,
      query,
      osFilter,
      groupFilter,
      statusFilter,
    };
    const next = [...savedFilters, entry];
    setSavedFilters(next);
    saveFiltersStorage(next);
    setFilterName("");
    setSaveFilterOpen(false);
    onToast(`Saved filter "${trimmed}"`);
  }

  function applySavedFilter(f: SavedFleetFilter) {
    setQuery(f.query);
    setOsFilter(f.osFilter);
    setGroupFilter(f.groupFilter);
    setStatusFilter(f.statusFilter);
    setPage(1);
    onToast(`Applied filter "${f.name}"`);
  }

  function deleteSavedFilter(id: string) {
    const next = savedFilters.filter((f) => f.id !== id);
    setSavedFilters(next);
    saveFiltersStorage(next);
  }

  const selectedDevicesList = useMemo(
    () => devices.filter((d) => selectedIds.has(d.id)),
    [devices, selectedIds],
  );

  async function executeBulkAction() {
    if (!bulkAction || selectedDevicesList.length === 0) return;
    setBulkBusy(true);
    let okCount = 0;
    let errCount = 0;
    try {
      for (const dev of selectedDevicesList) {
        try {
          if (bulkAction === "refresh") {
            await api.refreshDeviceTelemetry(dev.id);
          } else if (bulkAction === "revoke" && !dev.revoked) {
            await api.revokeDevice(dev.id);
          }
          okCount += 1;
        } catch {
          errCount += 1;
        }
      }
      onToast(
        `${bulkAction === "revoke" ? "Revoked" : "Queued refresh on"} ${okCount} node(s)${
          errCount ? ` (${errCount} failed)` : ""
        }`,
      );
      setSelectedIds(new Set());
      setBulkAction(null);
      await onRefresh();
      await fetchServerPage();
    } finally {
      setBulkBusy(false);
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
        unrestricted: polFullPc,
        allow_full_pc_access: polFullPc,
        allow_unrestricted_apps: polFullPc,
        allow_writes: polWrites,
        allow_tier3: polTier3,
        allow_file_deletion: polTier4,
        desktop_enabled: polDesktop,
        allow_shell: polShell,
        allow_package_install: polPackageInstall,
        allow_clipboard: polClipboard,
        require_local_approval: polRequireLocalApproval,
        max_tier_unattended: polMaxTier,
        allowed_apps: polAllowedApps,
      });
      onToast(`Updated policy for ${selectedDevice.name}`);
      await Promise.all([onRefresh(), fetchServerPage()]);
      await openInspector(selectedDevice, true);
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

      {pageError && (
        <StateBanner
          kind="degraded"
          title="Server-side fleet pagination fallback active"
          detail={`Falling back to cached device list (${pageError}).`}
        />
      )}

      {/* Saved Filters & Group Quick-Select */}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
        <Button size="sm" onClick={() => setSaveFilterOpen(true)}>
          <Bookmark size={12} /> Save Filter
        </Button>
        {savedFilters.map((f) => (
          <span
            key={f.id}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "2px 6px",
              borderRadius: 6,
              border: "1px solid var(--border-hair)",
              background: "var(--bg-elevated)",
              fontSize: 11.5,
            }}
          >
            <button
              type="button"
              className="chip"
              style={{ padding: "2px 4px" }}
              onClick={() => applySavedFilter(f)}
            >
              {f.name}
            </button>
            <button
              type="button"
              aria-label={`Delete saved filter ${f.name}`}
              style={{
                background: "transparent",
                border: 0,
                color: "var(--text-muted)",
                cursor: "pointer",
                padding: 2,
                display: "inline-flex",
              }}
              onClick={() => deleteSavedFilter(f.id)}
            >
              <Trash2 size={11} />
            </button>
          </span>
        ))}
        {availableGroups.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 4, marginInlineStart: "auto" }}>
            <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>Group:</span>
            <Chip
              active={groupFilter === "all"}
              onClick={() => {
                setGroupFilter("all");
                setPage(1);
              }}
            >
              All
            </Chip>
            {availableGroups.map((grp) => (
              <Chip
                key={grp}
                active={groupFilter === grp}
                onClick={() => {
                  setGroupFilter(grp);
                  setPage(1);
                }}
              >
                {grp}
              </Chip>
            ))}
          </div>
        )}
      </div>

      {/* Bulk-Action Bar (Safeguarded by Confirmation Modal) */}
      {selectedIds.size > 0 && (
        <SurfaceCard
          style={{
            padding: "10px 14px",
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 10,
            borderColor: "var(--accent)",
          }}
        >
          <span className="mono" style={{ fontSize: 12, fontWeight: 600 }}>
            {selectedIds.size} node{selectedIds.size === 1 ? "" : "s"} selected
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" onClick={() => setBulkAction("refresh")}>
              <RefreshCw size={12} /> Bulk Refresh ({selectedIds.size})
            </Button>
            <Button size="sm" variant="danger" onClick={() => setBulkAction("revoke")}>
              <ShieldAlert size={12} /> Bulk Revoke ({selectedIds.size})
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelectedIds(new Set())}>
              Clear
            </Button>
          </div>
        </SurfaceCard>
      )}

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
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(1);
              }}
            />
            <div style={{ display: "flex", gap: 4 }}>
              {(["all", "windows", "linux", "macos"] as const).map((os) => (
                <Chip
                  key={os}
                  active={osFilter === os}
                  onClick={() => {
                    setOsFilter(os);
                    setPage(1);
                  }}
                >
                  {os === "all" ? "All OS" : os.toUpperCase()}
                </Chip>
              ))}
            </div>
            <div style={{ display: "flex", gap: 4 }}>
              {(["all", "online", "offline", "revoked"] as const).map((st) => (
                <Chip
                  key={st}
                  active={statusFilter === st}
                  onClick={() => {
                    setStatusFilter(st);
                    setPage(1);
                  }}
                >
                  {st === "all" ? "All Status" : st}
                </Chip>
              ))}
            </div>
          </div>

          <span className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
            {selectedIds.size} selected · {filtered.length} of {totalCount} matching
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
          <span className="mono">
            Page {page} of {totalPages} ({totalCount} total{pagedDevices ? " · server-paginated" : ""})
          </span>
          <div style={{ display: "flex", gap: 6 }}>
            <Button size="sm" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
              Prev
            </Button>
            <Button
              size="sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            >
              Next
            </Button>
          </div>
        </div>
      </SurfaceCard>

      {/* Save Filter Modal */}
      <Modal
        open={saveFilterOpen}
        title="Save Fleet Filter"
        onClose={() => setSaveFilterOpen(false)}
        maxWidth={400}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
            Saves current search (<span className="mono">{query || "any"}</span>), OS (
            <span className="mono">{osFilter}</span>), group (<span className="mono">{groupFilter}</span>), and status (
            <span className="mono">{statusFilter}</span>).
          </div>
          <input
            type="text"
            className="field"
            placeholder="Filter name (e.g. Prod Windows Online)"
            value={filterName}
            onChange={(e) => setFilterName(e.target.value)}
          />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" onClick={() => setSaveFilterOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" disabled={!filterName.trim()} onClick={handleSaveCurrentFilter}>
              Save Filter
            </Button>
          </div>
        </div>
      </Modal>

      {/* Bulk-Action Safeguard Confirmation Modal */}
      <Modal
        open={Boolean(bulkAction)}
        title={bulkAction === "revoke" ? "Confirm Bulk Node Revocation" : "Confirm Bulk Telemetry Refresh"}
        onClose={() => setBulkAction(null)}
        maxWidth={460}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <StateBanner
            kind={bulkAction === "revoke" ? "denied" : "degraded"}
            title={`Blast radius: ${selectedDevicesList.length} endpoint${selectedDevicesList.length === 1 ? "" : "s"}`}
            detail={
              bulkAction === "revoke"
                ? "Revoking device tokens immediately disconnects these agents and blocks further task execution until re-enrolled."
                : "Dispatches a diagnostic refresh (sysinfo, CPU/RAM, network) to every selected endpoint."
            }
          />
          <div
            className="terminal"
            style={{ maxHeight: 160, overflowY: "auto", fontSize: 11.5 }}
          >
            {selectedDevicesList.map((d) => (
              <div key={d.id}>
                • {d.name} ({d.id}) · {d.os || "node"} · {d.revoked ? "revoked" : d.status}
              </div>
            ))}
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button variant="ghost" disabled={bulkBusy} onClick={() => setBulkAction(null)}>
              Cancel
            </Button>
            <Button
              variant={bulkAction === "revoke" ? "danger" : "primary"}
              disabled={bulkBusy}
              onClick={() => void executeBulkAction()}
            >
              {bulkBusy
                ? "Executing..."
                : bulkAction === "revoke"
                  ? `Confirm Revoke (${selectedDevicesList.length})`
                  : `Confirm Refresh (${selectedDevicesList.length})`}
            </Button>
          </div>
        </div>
      </Modal>

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

                {deviceIntel && (
                  <SurfaceCard style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                      <span style={{ fontSize: 12.5, fontWeight: 600 }}>Unified Device Intelligence</span>
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
                    </div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                      <Chip>Transport: {deviceIntel.recommended_transport}</Chip>
                      <Chip>A11y: {deviceIntel.recommended_a11y_backend}</Chip>
                      <Chip>Anomalies: {deviceIntel.anomalies.length}</Chip>
                    </div>
                    {deviceIntel.suggested_actions.length > 0 && (
                      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                        <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>Suggested Actions</span>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                          {deviceIntel.suggested_actions.slice(0, 3).map((action) => (
                            <Button
                              key={`${action.action}-${action.tool}`}
                              size="sm"
                              disabled={intelActionBusy === action.tool}
                              onClick={async () => {
                                setIntelActionBusy(action.tool);
                                try {
                                  await api.submitTask({
                                    device_id: selectedDevice.id,
                                    tool: action.tool,
                                    args: {},
                                  });
                                  onToast(`Queued: ${action.title}`);
                                } catch (err) {
                                  onToast(err instanceof Error ? err.message : "Action failed");
                                } finally {
                                  setIntelActionBusy(null);
                                }
                              }}
                            >
                              <Zap size={11} /> {action.title}
                            </Button>
                          ))}
                        </div>
                      </div>
                    )}
                  </SurfaceCard>
                )}

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
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                  <span style={{ fontSize: 12.5, fontWeight: 600 }}>Inheritance &amp; Override Breakdown</span>
                  <StatusBadge
                    status={selectedDevice.has_custom_policy ? "warn" : "ok"}
                    label={selectedDevice.has_custom_policy ? "Custom Device Override" : "Inherits Global Policy"}
                  />
                </div>

                <SurfaceCard style={{ overflow: "hidden" }}>
                  <table className="saas-table" style={{ fontSize: 11.5 }}>
                    <thead>
                      <tr>
                        <th>Rule</th>
                        <th>Global</th>
                        <th>Override</th>
                        <th>Effective</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(
                        [
                          ["Tier 2 File Writes", "allow_writes", polWrites],
                          ["Tier 3 System Actions", "allow_tier3", polTier3],
                          ["Tier 4 File Deletion", "allow_file_deletion", polTier4],
                          ["Shell (run_shell)", "allow_shell", polShell],
                          ["Desktop Control", "desktop_enabled", polDesktop],
                          ["Package Install", "allow_package_install", polPackageInstall],
                          ["Clipboard Sync", "allow_clipboard", polClipboard],
                          ["Full PC / Unrestricted", "allow_full_pc_access", polFullPc],
                          ["Local Approval Popup", "require_local_approval", polRequireLocalApproval],
                        ] as const
                      ).map(([label, key, draftVal]) => {
                        const gMap = (globalPolicy || {}) as unknown as Record<string, unknown>;
                        const globalVal =
                          key in gMap && gMap[key] !== undefined && gMap[key] !== null
                            ? Boolean(gMap[key])
                            : key === "allow_clipboard"
                              ? true
                              : false;
                        const rawOverride =
                          selectedDevice.has_custom_policy && selectedDevice.policy
                            ? selectedDevice.policy[key] ??
                              (key === "allow_full_pc_access" ? selectedDevice.policy.unrestricted : undefined)
                            : undefined;
                        const hasOverride = rawOverride !== undefined && rawOverride !== null;
                        const overrideVal = hasOverride ? Boolean(rawOverride) : null;
                        const effMap = (overview?.device?.effective_policy ??
                          selectedDevice.effective_policy ??
                          {}) as Record<string, unknown>;
                        const effectiveVal =
                          key in effMap && effMap[key] !== undefined && effMap[key] !== null
                            ? Boolean(effMap[key])
                            : hasOverride
                              ? Boolean(overrideVal)
                              : Boolean(globalVal ?? draftVal);
                        return (
                          <tr key={key}>
                            <td>{label}</td>
                            <td className="mono">{globalVal ? "ON" : "OFF"}</td>
                            <td className="mono" style={{ color: hasOverride ? "var(--amber)" : "var(--text-muted)" }}>
                              {hasOverride ? (overrideVal ? "ON" : "OFF") : "inherit"}
                            </td>
                            <td className="mono" style={{ fontWeight: 600 }}>
                              {effectiveVal ? "ALLOW" : "DENY"}
                            </td>
                          </tr>
                        );
                      })}
                      <tr>
                        <td>Max Unattended Tier</td>
                        <td className="mono">Tier {globalPolicy?.max_tier_unattended ?? 2}</td>
                        <td
                          className="mono"
                          style={{
                            color:
                              selectedDevice.has_custom_policy &&
                              selectedDevice.policy?.max_tier_unattended !== undefined &&
                              selectedDevice.policy?.max_tier_unattended !== null
                                ? "var(--amber)"
                                : "var(--text-muted)",
                          }}
                        >
                          {selectedDevice.has_custom_policy &&
                          selectedDevice.policy?.max_tier_unattended !== undefined &&
                          selectedDevice.policy?.max_tier_unattended !== null
                            ? `Tier ${String(selectedDevice.policy.max_tier_unattended)}`
                            : "inherit"}
                        </td>
                        <td className="mono" style={{ fontWeight: 600 }}>
                          Tier{" "}
                          {String(
                            (overview?.device?.effective_policy ?? selectedDevice.effective_policy)
                              ?.max_tier_unattended ?? polMaxTier,
                          )}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </SurfaceCard>

                <Toggle checked={polWrites} onChange={setPolWrites} label="Allow Tier 2 File Writes" />
                <Toggle checked={polTier3} onChange={setPolTier3} label="Allow Tier 3 System Actions" />
                <Toggle checked={polTier4} onChange={setPolTier4} label="Allow Tier 4 File Deletion" />
                <Toggle checked={polShell} onChange={setPolShell} label="Allow Shell Execution (run_shell)" />
                <Toggle checked={polDesktop} onChange={setPolDesktop} label="Allow Interactive Desktop Control" />
                <Toggle checked={polPackageInstall} onChange={setPolPackageInstall} label="Allow Package Installation (install_package)" />
                <Toggle checked={polClipboard} onChange={setPolClipboard} label="Allow Clipboard Sync (get/set_clipboard)" />
                <Toggle checked={polFullPc} onChange={setPolFullPc} label="Allow Full PC / Unrestricted Access" />
                <Toggle checked={polRequireLocalApproval} onChange={setPolRequireLocalApproval} label="Require Local User Approval Popup on Device" />

                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 4 }}>
                    Max Unattended Tier (1–4)
                  </label>
                  <select
                    className="field"
                    value={polMaxTier}
                    onChange={(e) => setPolMaxTier(Number(e.target.value))}
                  >
                    <option value={1}>Tier 1 — Read-Only Only</option>
                    <option value={2}>Tier 2 — Safe Workspace Writes</option>
                    <option value={3}>Tier 3 — Shell &amp; Desktop Automation</option>
                    <option value={4}>Tier 4 — Full Autonomous Ceiling</option>
                  </select>
                </div>

                <div>
                  <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 6 }}>
                    Allowed Desktop Applications (press Enter to add):
                  </div>
                  <TagInput tags={polAllowedApps} onChange={setPolAllowedApps} />
                </div>

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
                      await Promise.all([onRefresh(), fetchServerPage()]);
                      await openInspector(selectedDevice, true);
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

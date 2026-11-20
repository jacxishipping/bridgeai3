import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Eye, Keyboard, Monitor, MousePointerClick, Pause, Play, RefreshCw, ShieldAlert } from "lucide-react";
import type { ApiClient } from "../api";
import type { DeviceRecord, MonitorDescriptor, PolicyState } from "../types";
import { Button, Chip, StateBanner, StatusBadge, SurfaceCard } from "../components/ui";

export function DesktopView({
  api,
  devices,
  onToast,
}: {
  api: ApiClient;
  devices: DeviceRecord[];
  onToast: (msg: string) => void;
}) {
  const [deviceId, setDeviceId] = useState(devices[0]?.id ?? "local");
  const [monitors, setMonitors] = useState<MonitorDescriptor[]>([]);
  const [selectedMonitor, setSelectedMonitor] = useState(0);
  const [streaming, setStreaming] = useState(false);
  const [controlArmed, setControlArmed] = useState(false);
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [frameError, setFrameError] = useState<string>("");
  const [retryCount, setRetryCount] = useState(0);
  const [globalPolicy, setGlobalPolicy] = useState<PolicyState | null>(null);
  const [typeText, setTypeText] = useState("");
  const [hotkey, setHotkey] = useState("ctrl+c");
  const lastObjectUrl = useRef<string | null>(null);

  useEffect(() => {
    api
      .getPolicy()
      .then((res) => setGlobalPolicy(res.policy ?? null))
      .catch(() => setGlobalPolicy(null));
  }, [api]);

  const selectedDevice = useMemo(
    () => devices.find((d) => d.id === deviceId) ?? null,
    [devices, deviceId],
  );

  const isOffline = Boolean(
    selectedDevice && (selectedDevice.revoked || selectedDevice.status !== "online"),
  );

  const effectiveDesktopEnabled = useMemo(() => {
    if (selectedDevice?.has_custom_policy && selectedDevice.policy && "desktop_enabled" in selectedDevice.policy) {
      return Boolean(selectedDevice.policy.desktop_enabled);
    }
    if (globalPolicy) {
      return Boolean(globalPolicy.desktop_enabled);
    }
    return true;
  }, [selectedDevice, globalPolicy]);

  const controlBlockedReason = useMemo(() => {
    if (selectedDevice?.revoked) return "Device is revoked; desktop control is permanently disabled.";
    if (isOffline) return "Selected device is offline. Reconnect the agent before arming interactive control.";
    if (!effectiveDesktopEnabled) {
      return "Interactive desktop automation is disabled by active policy (View-Only enforced).";
    }
    return null;
  }, [selectedDevice, isOffline, effectiveDesktopEnabled]);

  useEffect(() => {
    if (controlBlockedReason && controlArmed) {
      setControlArmed(false);
    }
  }, [controlBlockedReason, controlArmed]);

  // Keyboard safety: Escape immediately disarms interactive control
  useEffect(() => {
    if (!controlArmed) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setControlArmed(false);
        onToast("Interactive control disarmed (Escape) — returned to View-Only mode");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [controlArmed, onToast]);

  useEffect(() => {
    if (devices.length > 0 && deviceId === "local") {
      const winDev = devices.find((d) => (d.os || "").toLowerCase().includes("win"));
      if (winDev) setDeviceId(winDev.id);
    }
  }, [devices, deviceId]);

  useEffect(() => {
    let cancelled = false;
    api
      .getMonitors(deviceId)
      .then((m) => {
        if (!cancelled) {
          setMonitors(m);
          setSelectedMonitor(0);
        }
      })
      .catch(() => {
        if (!cancelled) setMonitors([]);
      });
    return () => {
      cancelled = true;
    };
  }, [api, deviceId]);

  const pullFrame = useCallback(async () => {
    try {
      const url = await api.fetchLiveFrameUrl(deviceId, 0.75, 75);
      if (lastObjectUrl.current) URL.revokeObjectURL(lastObjectUrl.current);
      lastObjectUrl.current = url;
      setFrameUrl(url);
      setFrameError("");
      setRetryCount(0);
    } catch (err) {
      setFrameError(err instanceof Error ? err.message : "Desktop frame unavailable");
      setRetryCount((c) => c + 1);
    }
  }, [api, deviceId]);

  useEffect(() => {
    return () => {
      if (lastObjectUrl.current) URL.revokeObjectURL(lastObjectUrl.current);
    };
  }, []);

  useEffect(() => {
    if (!streaming) return;
    void pullFrame();
    const timer = window.setInterval(() => {
      void pullFrame();
    }, 1200);
    return () => window.clearInterval(timer);
  }, [streaming, pullFrame]);

  async function handleImageClick(e: React.MouseEvent<HTMLImageElement>) {
    if (!controlArmed || controlBlockedReason) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const nx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const ny = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    const activeMon = monitors[selectedMonitor];
    try {
      await api.sendDesktopInput(deviceId, {
        action: e.button === 2 ? "right_click" : "click",
        x: nx,
        y: ny,
        normalized: true,
        screen_width: activeMon?.width ?? 1920,
        screen_height: activeMon?.height ?? 1080,
      });
      onToast(`Click dispatched (${Math.round(nx * 100)}%, ${Math.round(ny * 100)}%)`);
      window.setTimeout(() => void pullFrame(), 300);
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Input rejected by policy");
    }
  }

  const activeMonitor = monitors[selectedMonitor] ?? null;

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
            Live Desktop Studio
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Policy-gated live desktop view and dual-control input for interactive sessions.
          </p>
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
          <StatusBadge
            status={
              isOffline
                ? "err"
                : streaming && frameError
                  ? "warn"
                  : streaming
                    ? "ok"
                    : "muted"
            }
            label={
              isOffline
                ? "Device Offline"
                : streaming && frameError
                  ? `Degraded · Retrying (${retryCount})`
                  : streaming
                    ? "Connected · Live Feed"
                    : "Feed Idle"
            }
          />
          <StatusBadge
            status={controlArmed ? "warn" : controlBlockedReason ? "err" : "muted"}
            label={
              controlArmed
                ? "Interactive Control Armed"
                : controlBlockedReason
                  ? "View-Only (Policy / Offline)"
                  : "View-Only (Input Disabled)"
            }
          />
          <Button
            variant={streaming ? "danger" : "primary"}
            disabled={isOffline}
            onClick={() => {
              setRetryCount(0);
              setStreaming((s) => !s);
            }}
          >
            {streaming ? <Pause size={13} /> : <Play size={13} />}
            <span>{streaming ? "Pause Stream" : "Start Live View"}</span>
          </Button>
          <Button
            size="icon"
            aria-label="Capture single frame"
            title="Capture single frame"
            disabled={isOffline}
            onClick={() => void pullFrame()}
          >
            <RefreshCw size={14} />
          </Button>
        </div>
      </div>

      {/* Consent & Interruption Banners */}
      {(streaming || controlArmed) && (
        <div
          role="status"
          style={{
            padding: "10px 14px",
            borderRadius: 10,
            border: "1px solid rgba(99, 102, 241, 0.35)",
            background: "rgba(99, 102, 241, 0.08)",
            fontSize: 12,
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
          }}
        >
          <span>
            <strong>Session Notice &amp; Consent:</strong> Remote desktop observation and audit logging are active on{" "}
            <span className="mono">{selectedDevice?.name || deviceId}</span>.
            {controlArmed
              ? " Interactive input is armed — press Escape at any time to disarm immediately."
              : " Session is strictly View-Only; mouse and keyboard events are blocked."}
          </span>
          {controlArmed && (
            <Button size="sm" variant="danger" onClick={() => setControlArmed(false)}>
              Disarm Control (Esc)
            </Button>
          )}
        </div>
      )}

      {controlBlockedReason && (
        <StateBanner
          kind={isOffline ? "offline" : "denied"}
          title={isOffline ? "Target workstation offline" : "Interactive control restricted by policy"}
          detail={controlBlockedReason}
        />
      )}

      {streaming && frameError && (
        <StateBanner
          kind="degraded"
          title={`Stream interrupted (attempt ${retryCount})`}
          detail={`${frameError}. Automatic retry is running every 1.2s.`}
          action={
            <Button
              size="sm"
              onClick={() => {
                setFrameError("");
                setRetryCount(0);
                void pullFrame();
              }}
            >
              Reconnect Now
            </Button>
          }
        />
      )}

      {/* Controls Bar */}
      <SurfaceCard
        style={{
          padding: 14,
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <Monitor size={14} style={{ color: "var(--text-muted)" }} />
            <select
              aria-label="Target desktop device"
              className="field mono"
              style={{ width: 220 }}
              value={deviceId}
              onChange={(e) => {
                setControlArmed(false);
                setStreaming(false);
                setFrameError("");
                setDeviceId(e.target.value);
              }}
            >
              <option value="local">local (Host Desktop)</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.os || "node"} · {d.revoked ? "revoked" : d.status})
                </option>
              ))}
            </select>
          </div>

          {monitors.length > 0 ? (
            <select
              aria-label="Target monitor"
              className="field mono"
              style={{ width: 220 }}
              value={selectedMonitor}
              onChange={(e) => setSelectedMonitor(Number(e.target.value))}
            >
              {monitors.map((m) => (
                <option key={m.index} value={m.index}>
                  Display {m.index}: {m.name} ({m.width}×{m.height})
                </option>
              ))}
            </select>
          ) : (
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
              Display metadata unavailable
            </span>
          )}

          {activeMonitor && (
            <span className="mono" style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
              Active: {activeMonitor.width}×{activeMonitor.height}
            </span>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Chip
            active={controlArmed}
            disabled={Boolean(controlBlockedReason)}
            aria-pressed={controlArmed}
            onClick={() => {
              if (controlBlockedReason) return;
              setControlArmed((v) => !v);
              onToast(!controlArmed ? "Interactive control armed (press Escape to disarm)" : "Returned to View-Only mode");
            }}
          >
            {controlArmed ? <MousePointerClick size={13} /> : <Eye size={13} />}
            <span>
              {controlArmed
                ? "Control Armed (Esc to Disarm)"
                : controlBlockedReason
                  ? "Control Locked"
                  : "View Only · Arm Control"}
            </span>
          </Chip>
        </div>
      </SurfaceCard>

      {/* Viewport */}
      <SurfaceCard
        style={{
          overflow: "hidden",
          minHeight: 380,
          background: "var(--bg-terminal)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          position: "relative",
        }}
      >
        {frameUrl ? (
          <img
            src={frameUrl}
            alt={`Live remote desktop frame for ${selectedDevice?.name || deviceId}`}
            onClick={(e) => void handleImageClick(e)}
            onContextMenu={(e) => {
              if (controlArmed && !controlBlockedReason) {
                e.preventDefault();
                void handleImageClick(e);
              }
            }}
            style={{
              width: "100%",
              maxHeight: "68vh",
              objectFit: "contain",
              cursor: controlArmed ? "crosshair" : "default",
              display: "block",
            }}
          />
        ) : (
          <div style={{ textAlign: "center", padding: 36, color: "#94a3b8", maxWidth: 460 }}>
            <Monitor size={30} style={{ opacity: 0.55, marginBottom: 10 }} />
            <div style={{ fontSize: 13.5, fontWeight: 500, color: "#f1f5f9", marginBottom: 6 }}>
              {frameError ? "Desktop Feed Unavailable" : "Ready for Live Desktop Inspection"}
            </div>
            <div style={{ fontSize: 12, lineHeight: 1.55 }}>
              {frameError ||
                "Press Start Live View or Refresh to capture frames. Sessions start in View-Only mode by default."}
            </div>
          </div>
        )}
      </SurfaceCard>

      {/* Keyboard Dispatch Bar: ONLY rendered when interactive control is explicitly armed and permitted */}
      {controlArmed && !controlBlockedReason ? (
        <SurfaceCard style={{ padding: 14 }}>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
            <Keyboard size={15} style={{ color: "var(--amber)" }} />
            <input
              type="text"
              aria-label="Remote keyboard text input"
              className="field"
              style={{ flex: "1 1 220px" }}
              placeholder="Type text on active desktop window..."
              value={typeText}
              onChange={(e) => setTypeText(e.target.value)}
            />
            <Button
              size="sm"
              disabled={!typeText}
              onClick={async () => {
                try {
                  await api.sendDesktopInput(deviceId, { action: "type", text: typeText });
                  onToast("Text sent to desktop");
                  setTypeText("");
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Keyboard input denied");
                }
              }}
            >
              Send Text
            </Button>

            <input
              type="text"
              aria-label="Remote keyboard hotkey"
              className="field mono"
              style={{ width: 130 }}
              value={hotkey}
              onChange={(e) => setHotkey(e.target.value)}
            />
            <Button
              size="sm"
              onClick={async () => {
                try {
                  await api.sendDesktopInput(deviceId, { action: "hotkey", key: hotkey });
                  onToast(`Hotkey ${hotkey} sent`);
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Hotkey denied");
                }
              }}
            >
              Send Hotkey
            </Button>
          </div>
        </SurfaceCard>
      ) : (
        <SurfaceCard style={{ padding: "10px 14px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--text-muted)" }}>
            {controlBlockedReason ? <ShieldAlert size={14} /> : <Eye size={14} />}
            <span>
              <strong>View-Only Session:</strong> Mouse clicks, typing, and hotkeys are disabled.{" "}
              {controlBlockedReason
                ? controlBlockedReason
                : "Arm interactive control above only when dual-control input is required."}
            </span>
          </div>
        </SurfaceCard>
      )}
    </div>
  );
}

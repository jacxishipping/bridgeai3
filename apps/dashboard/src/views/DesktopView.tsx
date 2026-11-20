import React, { useCallback, useEffect, useRef, useState } from "react";
import { Keyboard, Monitor, MousePointerClick, Pause, Play, RefreshCw } from "lucide-react";
import type { ApiClient } from "../api";
import type { DeviceRecord, MonitorDescriptor } from "../types";
import { Button, Chip, StatusBadge, SurfaceCard } from "../components/ui";

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
  const [typeText, setTypeText] = useState("");
  const [hotkey, setHotkey] = useState("ctrl+c");
  const lastObjectUrl = useRef<string | null>(null);

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
        if (!cancelled) setMonitors(m);
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
    } catch (err) {
      setFrameError(err instanceof Error ? err.message : "Desktop frame unavailable");
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
    if (!controlArmed) return;
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
            Policy-gated live desktop view and dual-control input for interactive Windows sessions.
          </p>
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
          <StatusBadge
            status={streaming ? "ok" : "muted"}
            label={streaming ? "Live Feed Active" : "Feed Idle"}
          />
          <Button
            variant={streaming ? "danger" : "primary"}
            onClick={() => setStreaming((s) => !s)}
          >
            {streaming ? <Pause size={13} /> : <Play size={13} />}
            <span>{streaming ? "Pause Stream" : "Start Live View"}</span>
          </Button>
          <Button size="icon" title="Capture single frame" onClick={() => void pullFrame()}>
            <RefreshCw size={14} />
          </Button>
        </div>
      </div>

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
              className="field mono"
              style={{ width: 210 }}
              value={deviceId}
              onChange={(e) => {
                setControlArmed(false);
                setDeviceId(e.target.value);
              }}
            >
              <option value="local">local (Host Desktop)</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.os || "node"})
                </option>
              ))}
            </select>
          </div>

          {monitors.length > 0 && (
            <select
              className="field mono"
              style={{ width: 200 }}
              value={selectedMonitor}
              onChange={(e) => setSelectedMonitor(Number(e.target.value))}
            >
              {monitors.map((m) => (
                <option key={m.index} value={m.index}>
                  {m.index}: {m.name} ({m.width}×{m.height})
                </option>
              ))}
            </select>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Chip
            active={controlArmed}
            onClick={() => {
              setControlArmed((v) => !v);
              onToast(!controlArmed ? "Mouse click control armed" : "View-only mode");
            }}
          >
            <MousePointerClick size={13} />
            <span>{controlArmed ? "Control Armed (Click Image)" : "View Only"}</span>
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
            alt="Live remote desktop frame"
            onClick={(e) => void handleImageClick(e)}
            onContextMenu={(e) => {
              if (controlArmed) {
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
                "Press Start Live View or Refresh to capture frames. Requires desktop automation enabled in Control Plane & Windows client policy."}
            </div>
          </div>
        )}
      </SurfaceCard>

      {/* Keyboard Dispatch Bar */}
      <SurfaceCard style={{ padding: 14 }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
          <Keyboard size={15} style={{ color: "var(--text-muted)" }} />
          <input
            type="text"
            className="field"
            style={{ flex: "1 1 220px" }}
            disabled={!controlArmed}
            placeholder={
              controlArmed
                ? "Type text on active desktop window..."
                : "View-only mode — arm control above to send keyboard input"
            }
            value={typeText}
            onChange={(e) => setTypeText(e.target.value)}
          />
          <Button
            size="sm"
            disabled={!controlArmed || !typeText}
            onClick={async () => {
              if (!controlArmed) return;
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
            className="field mono"
            style={{ width: 130 }}
            disabled={!controlArmed}
            value={hotkey}
            onChange={(e) => setHotkey(e.target.value)}
          />
          <Button
            size="sm"
            disabled={!controlArmed}
            onClick={async () => {
              if (!controlArmed) return;
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
    </div>
  );
}

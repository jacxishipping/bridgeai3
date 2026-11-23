import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Eye, Keyboard, Monitor, MousePointerClick, Pause, Play, RefreshCw, ShieldAlert, Wifi } from "lucide-react";
import type { ApiClient } from "../api";
import type { DeviceDesktopCapabilities, DeviceRecord, MonitorDescriptor, PolicyState } from "../types";
import { Button, Chip, StateBanner, StatusBadge, SurfaceCard } from "../components/ui";

type TransportMode = "jpeg" | "webrtc";

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
  const [transport, setTransport] = useState<TransportMode>("jpeg");
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [frameError, setFrameError] = useState<string>("");
  const [retryCount, setRetryCount] = useState(0);
  const [globalPolicy, setGlobalPolicy] = useState<PolicyState | null>(null);
  const [deviceCaps, setDeviceCaps] = useState<DeviceDesktopCapabilities | null>(null);
  const [typeText, setTypeText] = useState("");
  const [hotkey, setHotkey] = useState("ctrl+c");
  const [webrtcStatus, setWebrtcStatus] = useState<string>("idle");
  const lastObjectUrl = useRef<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const pcRef = useRef<RTCPeerConnection | null>(null);
  const inputDcRef = useRef<RTCDataChannel | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const seenAgentIce = useRef(0);
  const pollTimerRef = useRef<number | null>(null);

  useEffect(() => {
    api
      .getPolicy()
      .then((res) => setGlobalPolicy(res.policy ?? null))
      .catch(() => setGlobalPolicy(null));
  }, [api]);

  useEffect(() => {
    if (!deviceId || deviceId === "local") {
      setDeviceCaps(null);
      return;
    }
    let cancelled = false;
    api
      .getDeviceDesktopCapabilities(deviceId)
      .then((c) => {
        if (!cancelled) setDeviceCaps(c);
      })
      .catch(() => {
        if (!cancelled) setDeviceCaps(null);
      });
    return () => {
      cancelled = true;
    };
  }, [api, deviceId]);

  const selectedDevice = useMemo(
    () => devices.find((d) => d.id === deviceId) ?? null,
    [devices, deviceId],
  );

  const isOffline = Boolean(
    selectedDevice && (selectedDevice.revoked || selectedDevice.status !== "online"),
  );

  const webrtcAvailable = Boolean(
    deviceCaps?.webrtc_stream_supported ||
      (selectedDevice?.os || "").toLowerCase().includes("win") ||
      deviceId === "local",
  );

  const effectiveDesktopEnabled = useMemo(() => {
    if (deviceCaps) {
      return Boolean(deviceCaps.interactive_input_supported);
    }
    if (selectedDevice?.has_custom_policy && selectedDevice.policy && "desktop_enabled" in selectedDevice.policy) {
      return Boolean(selectedDevice.policy.desktop_enabled);
    }
    if (globalPolicy) {
      return Boolean(globalPolicy.desktop_enabled);
    }
    return true;
  }, [deviceCaps, selectedDevice, globalPolicy]);

  const controlBlockedReason = useMemo(() => {
    if (selectedDevice?.revoked) return "Device is revoked; desktop control is permanently disabled.";
    if (isOffline) return "Selected device is offline. Reconnect the agent before arming interactive control.";
    if (deviceCaps && !deviceCaps.interactive_input_supported) {
      return deviceCaps.reason || "Interactive control is not supported on this platform/policy.";
    }
    if (!effectiveDesktopEnabled) {
      return "Interactive desktop automation is disabled by active policy (View-Only enforced).";
    }
    return null;
  }, [selectedDevice, isOffline, deviceCaps, effectiveDesktopEnabled]);

  useEffect(() => {
    if (controlBlockedReason && controlArmed) {
      setControlArmed(false);
    }
  }, [controlBlockedReason, controlArmed]);

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

  const stopWebrtc = useCallback(async () => {
    if (pollTimerRef.current) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    const sid = sessionIdRef.current;
    sessionIdRef.current = null;
    seenAgentIce.current = 0;
    inputDcRef.current = null;
    try {
      pcRef.current?.close();
    } catch {
      // ignore
    }
    pcRef.current = null;
    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }
    if (sid) {
      try {
        await api.terminateWebRtcSession(sid);
      } catch {
        // Session may already be gone
      }
    }
    setWebrtcStatus("idle");
  }, [api]);

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

  const startWebrtc = useCallback(async () => {
    setWebrtcStatus("connecting");
    setFrameError("");
    try {
      const iceServers = await api.getWebRtcIceServers();
      const mode = controlArmed && !controlBlockedReason ? "ai_assisted" : "view_only";
      const sess = await api.createWebRtcSession({
        device_id: deviceId,
        mode,
        fps: 30,
        scale: 1.0,
        monitor_index: selectedMonitor,
        bitrate_kbps: 4500,
      });
      const sessionId = sess.session_id;
      sessionIdRef.current = sessionId;

      const pc = new RTCPeerConnection({ iceServers: iceServers.length ? iceServers : [{ urls: "stun:stun.l.google.com:19302" }] });
      pcRef.current = pc;

      pc.ontrack = (ev) => {
        const stream = ev.streams[0] || new MediaStream([ev.track]);
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          void videoRef.current.play().catch(() => undefined);
        }
        setWebrtcStatus("connected");
        setFrameError("");
      };

      pc.ondatachannel = (ev) => {
        if (ev.channel.label === "input") {
          inputDcRef.current = ev.channel;
          ev.channel.onmessage = (msgEv) => {
            try {
              const raw = typeof msgEv.data === "string" ? msgEv.data : "";
              const parsed = JSON.parse(raw) as { type?: string; text?: string; ok?: boolean };
              if (parsed.type === "clipboard_data" && typeof parsed.text === "string") {
                void navigator.clipboard?.writeText(parsed.text).then(
                  () => onToast("Agent clipboard copied to local clipboard"),
                  () => onToast(`Agent clipboard: ${parsed.text?.slice(0, 120) || "(empty)"}`),
                );
              }
            } catch {
              // ignore non-JSON input-channel chatter (pong, etc.)
            }
          };
        }
      };

      pc.onicecandidate = (ev) => {
        if (!ev.candidate || !sessionIdRef.current) return;
        void api.postWebRtcIce(sessionIdRef.current, ev.candidate.toJSON(), "viewer").catch(() => undefined);
      };

      pc.onconnectionstatechange = () => {
        const state = pc.connectionState || "connecting";
        setWebrtcStatus(state);
        if (state === "failed" || state === "disconnected") {
          setFrameError(`WebRTC ${state} — falling back to JPEG live view`);
          // Production resilience: keep the session observable via JPEG poll.
          void (async () => {
            await stopWebrtc();
            setTransport("jpeg");
            setStreaming(true);
            onToast("WebRTC ICE failed — switched to JPEG fallback");
          })();
        }
      };

      const waitForOffer = async () => {
        const deadline = Date.now() + 45000;
        while (Date.now() < deadline) {
          const live = await api.getWebRtcSession(sessionId);
          if (live.error) throw new Error(live.error);
          const offer = live.offer;
          const sdp = offer?.sdp || (live as { offer_sdp?: string }).offer_sdp;
          if (sdp) {
            return live;
          }
          await new Promise((r) => setTimeout(r, 500));
        }
        throw new Error("Timed out waiting for agent WebRTC offer (is the Windows agent online?)");
      };

      const live = await waitForOffer();
      const offerSdp = live.offer?.sdp || (live as { offer_sdp?: string }).offer_sdp;
      const offerType = live.offer?.type || (live as { offer_type?: string }).offer_type || "offer";
      if (!offerSdp) throw new Error("Agent offer missing SDP");

      await pc.setRemoteDescription({ type: offerType as RTCSdpType, sdp: offerSdp });
      for (const cand of live.agent_candidates || []) {
        if (cand?.candidate) {
          try {
            await pc.addIceCandidate(cand as RTCIceCandidateInit);
          } catch {
            // ignore bad candidates
          }
        }
      }
      seenAgentIce.current = (live.agent_candidates || []).length;

      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      await api.postWebRtcAnswer(sessionId, answer.sdp || "", answer.type || "answer");

      pollTimerRef.current = window.setInterval(() => {
        void (async () => {
          if (!sessionIdRef.current || !pcRef.current) return;
          try {
            const snap = await api.getWebRtcSession(sessionIdRef.current);
            const agents = snap.agent_candidates || [];
            for (let i = seenAgentIce.current; i < agents.length; i++) {
              const c = agents[i];
              if (c?.candidate) {
                try {
                  await pcRef.current.addIceCandidate(c as RTCIceCandidateInit);
                } catch {
                  // ignore
                }
              }
            }
            seenAgentIce.current = agents.length;
            if (snap.status === "ended" || snap.status === "error") {
              setFrameError(snap.error || `Session ${snap.status}`);
              setStreaming(false);
            }
          } catch {
            // transient
          }
        })();
      }, 2000);
    } catch (err) {
      setFrameError(err instanceof Error ? err.message : "WebRTC start failed");
      setWebrtcStatus("failed");
      setRetryCount((c) => c + 1);
      await stopWebrtc();
      throw err;
    }
  }, [api, controlArmed, controlBlockedReason, deviceId, selectedMonitor, stopWebrtc, onToast]);

  useEffect(() => {
    return () => {
      if (lastObjectUrl.current) URL.revokeObjectURL(lastObjectUrl.current);
      void stopWebrtc();
    };
  }, [stopWebrtc]);

  useEffect(() => {
    if (!streaming) {
      void stopWebrtc();
      return;
    }

    let cancelled = false;
    if (transport === "jpeg") {
      void pullFrame();
      const timer = window.setInterval(() => {
        void pullFrame();
      }, 1200);
      return () => {
        cancelled = true;
        window.clearInterval(timer);
      };
    }

    void startWebrtc().catch((err) => {
      if (!cancelled) {
        onToast(err instanceof Error ? err.message : "WebRTC failed — falling back to JPEG");
        setTransport("jpeg");
      }
    });
    return () => {
      cancelled = true;
      void stopWebrtc();
    };
  }, [streaming, transport, pullFrame, startWebrtc, stopWebrtc, onToast]);

  useEffect(() => {
    if (!streaming || transport !== "webrtc" || !sessionIdRef.current) return;
    const mode = controlArmed && !controlBlockedReason ? "ai_assisted" : "view_only";
    void api.setWebRtcSessionMode(sessionIdRef.current, mode).catch(() => undefined);
  }, [api, controlArmed, controlBlockedReason, streaming, transport]);

  async function dispatchClick(nx: number, ny: number, button: "left" | "right") {
    if (!controlArmed || controlBlockedReason) return;
    const activeMon = monitors[selectedMonitor];
    const dc = inputDcRef.current;
    if (transport === "webrtc" && dc && dc.readyState === "open") {
      dc.send(
        JSON.stringify({
          type: "click",
          action: "click",
          x: nx,
          y: ny,
          button,
        }),
      );
      onToast(`Click via WebRTC (${Math.round(nx * 100)}%, ${Math.round(ny * 100)}%)`);
      return;
    }
    await api.sendDesktopInput(deviceId, {
      action: button === "right" ? "right_click" : "click",
      x: nx,
      y: ny,
      normalized: true,
      screen_width: activeMon?.width ?? 1920,
      screen_height: activeMon?.height ?? 1080,
    });
    onToast(`Click dispatched (${Math.round(nx * 100)}%, ${Math.round(ny * 100)}%)`);
    if (transport === "jpeg") {
      window.setTimeout(() => void pullFrame(), 300);
    }
  }

  async function handleImageClick(e: React.MouseEvent<HTMLImageElement | HTMLVideoElement>) {
    if (!controlArmed || controlBlockedReason) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const nx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const ny = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    try {
      await dispatchClick(nx, ny, e.button === 2 ? "right" : "left");
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
            Policy-gated live desktop view — JPEG poll or low-latency WebRTC H.264 dual-control.
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
                    ? transport === "webrtc"
                      ? `WebRTC · ${webrtcStatus}`
                      : "Connected · JPEG Live"
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
            title="Capture single JPEG frame"
            disabled={isOffline || transport === "webrtc"}
            onClick={() => void pullFrame()}
          >
            <RefreshCw size={14} />
          </Button>
        </div>
      </div>

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
          detail={`${frameError}. ${transport === "jpeg" ? "Automatic JPEG retry is running every 1.2s." : "Check agent WebRTC / TURN."}`}
          action={
            <Button
              size="sm"
              onClick={() => {
                setFrameError("");
                setRetryCount(0);
                if (transport === "jpeg") void pullFrame();
                else {
                  setStreaming(false);
                  window.setTimeout(() => setStreaming(true), 50);
                }
              }}
            >
              Reconnect Now
            </Button>
          }
        />
      )}

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

          <select
            aria-label="Stream transport"
            className="field mono"
            style={{ width: 160 }}
            value={transport}
            disabled={streaming}
            onChange={(e) => setTransport(e.target.value as TransportMode)}
            title={webrtcAvailable ? "JPEG poll (~1.2s) or WebRTC H.264" : "WebRTC requires a Windows desktop agent"}
          >
            <option value="jpeg">JPEG · ~1.2s</option>
            <option value="webrtc" disabled={!webrtcAvailable}>
              WebRTC · H.264
            </option>
          </select>

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
        {transport === "webrtc" && streaming ? (
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            aria-label={`Live WebRTC desktop for ${selectedDevice?.name || deviceId}`}
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
              background: "#000",
            }}
          />
        ) : frameUrl ? (
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
                "Press Start Live View. Choose JPEG for compatibility or WebRTC for low-latency H.264 on Windows agents."}
            </div>
            {transport === "webrtc" && (
              <div style={{ marginTop: 10, fontSize: 11.5, display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
                <Wifi size={12} /> WebRTC status: {webrtcStatus}
              </div>
            )}
          </div>
        )}
      </SurfaceCard>

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
                  const dc = inputDcRef.current;
                  if (transport === "webrtc" && dc && dc.readyState === "open") {
                    dc.send(JSON.stringify({ type: "type", action: "type", text: typeText }));
                  } else {
                    await api.sendDesktopInput(deviceId, { action: "type", text: typeText });
                  }
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
                  const dc = inputDcRef.current;
                  if (transport === "webrtc" && dc && dc.readyState === "open") {
                    // Approximate hotkey as key_down/key_up sequence for simple single keys
                    dc.send(JSON.stringify({ type: "key_down", key: hotkey }));
                    dc.send(JSON.stringify({ type: "key_up", key: hotkey }));
                  } else {
                    await api.sendDesktopInput(deviceId, { action: "hotkey", key: hotkey });
                  }
                  onToast(`Hotkey ${hotkey} sent`);
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Hotkey denied");
                }
              }}
            >
              Send Hotkey
            </Button>

            <Button
              size="sm"
              variant="ghost"
              onClick={async () => {
                try {
                  const dc = inputDcRef.current;
                  if (transport === "webrtc" && dc && dc.readyState === "open") {
                    dc.send(JSON.stringify({ type: "clipboard_sync", direction: "from_agent" }));
                    onToast("Requested agent clipboard");
                    return;
                  }
                  const sid = sessionIdRef.current;
                  if (sid) {
                    const res = await api.sessionClipboard(sid, { action: "get" });
                    const text = typeof res?.text === "string" ? res.text : "";
                    await navigator.clipboard?.writeText(text);
                    onToast("Agent clipboard copied");
                  } else {
                    onToast("Start a live session to sync clipboard");
                  }
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Clipboard get denied");
                }
              }}
            >
              Get Clipboard
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={async () => {
                try {
                  const text = (await navigator.clipboard?.readText()) || "";
                  const dc = inputDcRef.current;
                  if (transport === "webrtc" && dc && dc.readyState === "open") {
                    dc.send(JSON.stringify({ type: "clipboard_sync", direction: "to_agent", text }));
                    onToast("Local clipboard sent to agent");
                    return;
                  }
                  const sid = sessionIdRef.current;
                  if (sid) {
                    await api.sessionClipboard(sid, { action: "set", text });
                    onToast("Local clipboard sent to agent");
                  } else {
                    onToast("Start a live session to sync clipboard");
                  }
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Clipboard set denied");
                }
              }}
            >
              Send Clipboard
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

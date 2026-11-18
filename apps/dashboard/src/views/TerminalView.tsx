import { useEffect, useRef, useState } from "react";
import { Play, Plus, Square, Terminal } from "lucide-react";
import type { ApiClient } from "../api";
import type { DeviceRecord, TerminalSessionSummary } from "../types";
import { Button, Chip, StatusBadge, SurfaceCard } from "../components/ui";

const QUICK_SNIPPETS = [
  { label: "System Info", cmd: "uname -a || systeminfo | findstr /B /C:\"OS\"\n" },
  { label: "Top Processes", cmd: "ps aux --sort=-%cpu | head -n 12 || tasklist | head -n 15\n" },
  { label: "Disk Free", cmd: "df -h || wmic logicaldisk get size,freespace,caption\n" },
  { label: "Network Ports", cmd: "ss -tulpn || netstat -ano | findstr LISTENING\n" },
];

export function TerminalView({
  api,
  devices,
  onToast,
}: {
  api: ApiClient;
  devices: DeviceRecord[];
  onToast: (msg: string) => void;
}) {
  const [deviceId, setDeviceId] = useState(devices[0]?.id ?? "local");
  const [sessions, setSessions] = useState<TerminalSessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [outputLines, setOutputLines] = useState<string[]>([
    "AI Bridge Interactive PTY Terminal — select a node and open a session.",
  ]);
  const [cmdInput, setCmdInput] = useState("");
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const termEndRef = useRef<HTMLDivElement>(null);

  async function refreshSessions(targetDev: string) {
    try {
      const list = await api.listTerminalSessions(targetDev);
      setSessions(list);
      if (list.length > 0 && !activeSessionId) {
        setActiveSessionId(list[0].session_id);
      }
    } catch {
      setSessions([]);
    }
  }

  useEffect(() => {
    void refreshSessions(deviceId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  useEffect(() => {
    termEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [outputLines]);

  useEffect(() => {
    if (!activeSessionId) return;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    let seq = 0;
    const url = api.resolveUrl(
      `/api/devices/${encodeURIComponent(deviceId)}/terminal/${encodeURIComponent(activeSessionId)}/stream?since_seq=${seq}`,
    );

    fetch(url, {
      headers: { ...api.authHeaders() },
      signal: ctrl.signal,
    })
      .then(async (resp) => {
        if (!resp.ok || !resp.body) return;
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buffer = "";
        while (!ctrl.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += dec.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const f of frames) {
            const dataLine = f
              .split("\n")
              .find((l) => l.startsWith("data:"))
              ?.slice(5)
              .trim();
            if (!dataLine) continue;
            try {
              const ev = JSON.parse(dataLine) as { seq?: number; data?: string; output?: string };
              if (typeof ev.seq === "number") seq = ev.seq;
              const chunk = ev.data ?? ev.output ?? "";
              if (chunk) {
                setOutputLines((prev) => [...prev.slice(-300), chunk]);
              }
            } catch {
              setOutputLines((prev) => [...prev.slice(-300), dataLine]);
            }
          }
        }
      })
      .catch(() => {
        // Closed / aborted
      });

    return () => ctrl.abort();
  }, [api, deviceId, activeSessionId]);

  async function handleStartSession() {
    setBusy(true);
    try {
      const s = await api.createTerminalSession(deviceId, 120, 32);
      setOutputLines([`[session ${s.session_id} attached to ${deviceId}]`]);
      setActiveSessionId(s.session_id);
      await refreshSessions(deviceId);
      onToast(`Terminal session ${s.session_id} started`);
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Failed to start terminal");
    } finally {
      setBusy(false);
    }
  }

  async function handleSend(text: string) {
    if (!activeSessionId || !text) return;
    try {
      await api.sendTerminalInput(deviceId, activeSessionId, text);
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Failed to send input");
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
            Live Interactive Terminal
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Policy-gated PTY / relayed shell sessions streamed via SSE into enrolled endpoints.
          </p>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <StatusBadge
            status={activeSessionId ? "ok" : "muted"}
            label={activeSessionId ? `Attached (${activeSessionId.slice(0, 8)})` : "Detached"}
          />
          <Button variant="primary" disabled={busy} onClick={() => void handleStartSession()}>
            <Plus size={14} /> New PTY Session
          </Button>
          {activeSessionId && (
            <Button
              variant="danger"
              size="sm"
              onClick={async () => {
                await api.closeTerminalSession(deviceId, activeSessionId);
                setActiveSessionId(null);
                await refreshSessions(deviceId);
                onToast("Terminal session closed");
              }}
            >
              <Square size={12} /> Close
            </Button>
          )}
        </div>
      </div>

      <SurfaceCard
        style={{
          padding: 14,
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 10,
        }}
      >
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
          <select
            className="field mono"
            style={{ width: 230 }}
            value={deviceId}
            onChange={(e) => {
              setDeviceId(e.target.value);
              setActiveSessionId(null);
            }}
          >
            <option value="local">local (Control Plane Host)</option>
            {devices.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name} ({d.os || d.id})
              </option>
            ))}
          </select>

          {sessions.map((s) => (
            <Chip
              key={s.session_id}
              active={s.session_id === activeSessionId}
              onClick={() => setActiveSessionId(s.session_id)}
            >
              <Terminal size={12} /> {s.session_id.slice(0, 8)}
            </Chip>
          ))}
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {QUICK_SNIPPETS.map((sn) => (
            <Chip
              key={sn.label}
              onClick={() => void handleSend(sn.cmd)}
              title={sn.cmd.trim()}
            >
              {sn.label}
            </Chip>
          ))}
        </div>
      </SurfaceCard>

      <div
        className="terminal"
        style={{
          minHeight: 390,
          maxHeight: "62vh",
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word", flex: 1 }}>
          {outputLines.join("")}
        </pre>
        <div ref={termEndRef} />
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!cmdInput.trim()) return;
          void handleSend(cmdInput + "\n");
          setCmdInput("");
        }}
        style={{ display: "flex", gap: 8 }}
      >
        <input
          type="text"
          className="field mono"
          placeholder={
            activeSessionId
              ? "Type command + Enter..."
              : "Start a PTY session first..."
          }
          disabled={!activeSessionId}
          value={cmdInput}
          onChange={(e) => setCmdInput(e.target.value)}
        />
        <Button type="submit" variant="primary" disabled={!activeSessionId}>
          <Play size={13} /> Send
        </Button>
      </form>
    </div>
  );
}

import { useEffect, useState } from "react";
import { Cable, KeyRound, Trash2 } from "lucide-react";
import type { ApiClient } from "../api";
import type { ConnectionsSummary } from "../types";
import { Button, StatusBadge, SurfaceCard } from "../components/ui";

export function ConnectionsView({
  api,
  onToast,
}: {
  api: ApiClient;
  onToast: (msg: string) => void;
}) {
  const [connState, setConnState] = useState<ConnectionsSummary | null>(null);
  const [mcpPoliciesJson, setMcpPoliciesJson] = useState("{}");
  const [m365, setM365] = useState<Record<string, unknown> | null>(null);

  async function loadAll() {
    const [c, mp, m3] = await Promise.all([
      api.getConnections().catch(() => null),
      api.getMcpToolPolicies().catch(() => ({ policies: {} })),
      api.getM365Status().catch(() => null),
    ]);
    setConnState(c);
    setMcpPoliciesJson(JSON.stringify((mp as { policies?: unknown }).policies ?? {}, null, 2));
    setM365(m3);
  }

  useEffect(() => {
    void loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  return (
    <div className="anim-enter" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" }}>
          MCP Gateway, OAuth Vault &amp; Connectors
        </h1>
        <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
          Monitor MCP HTTP transport, revoke OAuth 2.0 ChatGPT/Claude clients, and configure MCP tool scopes.
        </p>
      </div>

      {/* Live Gateway Status */}
      <div className="split-2">
        <SurfaceCard lift style={{ padding: 16 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600 }}>
              <Cable size={14} /> MCP Streamable HTTP
            </span>
            <StatusBadge
              status={connState?.mcp?.up ? "ok" : "muted"}
              label={connState?.mcp?.up ? `Listening :${connState.mcp.port}` : "Standby / Embedded"}
            />
          </div>
          <div className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
            Endpoint: /mcp · Port {connState?.mcp?.port ?? 8001}
          </div>
        </SurfaceCard>

        <SurfaceCard lift style={{ padding: 16 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600 }}>
              <KeyRound size={14} /> OAuth 2.0 Gateway
            </span>
            <StatusBadge
              status={connState?.oauth_gateway?.up ? "ok" : "muted"}
              label={connState?.oauth_gateway?.up ? `Listening :${connState.oauth_gateway.port}` : "Standby / Embedded"}
            />
          </div>
          <div className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
            Active access tokens: {connState?.chatgpt?.active_access_tokens ?? 0} · Refresh:{" "}
            {connState?.chatgpt?.active_refresh_tokens ?? 0}
          </div>
        </SurfaceCard>
      </div>

      {/* OAuth Clients Table */}
      <SurfaceCard style={{ overflow: "hidden" }}>
        <div
          style={{
            padding: "12px 16px",
            borderBottom: "1px solid var(--border-hair)",
            fontSize: 13.5,
            fontWeight: 600,
          }}
        >
          Registered OAuth Clients ({connState?.chatgpt?.clients?.length ?? 0})
        </div>
        <div className="table-wrap">
          <table className="saas-table">
            <thead>
              <tr>
                <th>Client Name</th>
                <th>Client ID Prefix</th>
                <th>Redirect URIs</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {!connState?.chatgpt?.clients?.length ? (
                <tr>
                  <td colSpan={4} style={{ textAlign: "center", padding: 24, color: "var(--text-muted)" }}>
                    No OAuth chatbot clients registered yet.
                  </td>
                </tr>
              ) : (
                connState.chatgpt.clients.map((c) => (
                  <tr key={c.client_id}>
                    <td style={{ fontWeight: 500 }}>{c.client_name}</td>
                    <td className="mono">{c.client_id_prefix}…</td>
                    <td className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
                      {c.redirect_uris}
                    </td>
                    <td>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={async () => {
                          await api.deleteOAuthClient(c.client_id);
                          onToast(`Removed client ${c.client_name}`);
                          await loadAll();
                        }}
                      >
                        <Trash2 size={12} /> Revoke
                      </Button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </SurfaceCard>

      {/* MCP Tool Policies + M365 Connector */}
      <div className="split-2">
        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 10 }}>
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>MCP Tool Policy Overrides (JSON)</span>
          <textarea
            rows={7}
            className="field mono"
            value={mcpPoliciesJson}
            onChange={(e) => setMcpPoliciesJson(e.target.value)}
          />
          <div>
            <Button
              size="sm"
              variant="primary"
              onClick={async () => {
                try {
                  const parsed = JSON.parse(mcpPoliciesJson) as Record<string, unknown>;
                  await api.saveMcpToolPolicies(parsed);
                  onToast("Saved MCP tool policies");
                } catch (err) {
                  onToast(err instanceof Error ? err.message : "Invalid JSON");
                }
              }}
            >
              Save MCP Policies
            </Button>
          </div>
        </SurfaceCard>

        <SurfaceCard style={{ padding: 18, display: "flex", flexDirection: "column", gap: 10 }}>
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>Microsoft 365 Connector</span>
          <pre className="terminal" style={{ margin: 0, flex: 1 }}>
            {JSON.stringify(m365 ?? { configured: false }, null, 2)}
          </pre>
        </SurfaceCard>
      </div>
    </div>
  );
}

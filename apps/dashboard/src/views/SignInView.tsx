import React, { useEffect, useState } from "react";
import { ShieldCheck, Sun, Moon, Wifi } from "lucide-react";
import type { ApiClient } from "../api";
import type { AuthSession, ThemeMode } from "../types";
import { Button, Chip, StatusBadge, SurfaceCard } from "../components/ui";

export function SignInView({
  api,
  theme,
  onToggleTheme,
  onSignedIn,
  onToast,
}: {
  api: ApiClient;
  theme: ThemeMode;
  onToggleTheme: () => void;
  onSignedIn: (session: AuthSession) => void;
  onToast: (msg: string) => void;
}) {
  const [authTab, setAuthTab] = useState<"credentials" | "token">("credentials");
  const [controlUrl, setControlUrl] = useState("");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [adminToken, setAdminToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [probe, setProbe] = useState<{ ok: boolean; ms?: number; text: string } | null>(null);
  const [oidcEnabled, setOidcEnabled] = useState(false);

  useEffect(() => {
    void api.isOidcConfigured(controlUrl).then(setOidcEnabled);
  }, [api, controlUrl]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const oidcCode = params.get("oidc_code");
    if (!oidcCode) return;
    setBusy(true);
    api
      .exchangeOidcCode(controlUrl, oidcCode)
      .then((session) => {
        window.history.replaceState({}, document.title, window.location.pathname);
        onSignedIn(session);
        onToast("Signed in via OIDC");
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "OIDC exchange failed");
      })
      .finally(() => setBusy(false));
  }, [api, controlUrl, onSignedIn, onToast]);

  async function handleTestEndpoint() {
    setError("");
    try {
      const res = await api.pingHealth(controlUrl);
      setProbe({
        ok: res.data.status === "ok",
        ms: res.latencyMs,
        text: `Healthy · v${res.data.version ?? "0.3.0"} · ${res.latencyMs} ms`,
      });
    } catch (err) {
      setProbe({
        ok: false,
        text: err instanceof Error ? err.message : "Unreachable",
      });
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (authTab === "credentials") {
        if (!username.trim() || !password) {
          throw new Error("Enter both username and password.");
        }
        const session = await api.loginWithPassword(controlUrl, username.trim(), password);
        onSignedIn(session);
      } else {
        if (!adminToken.trim()) {
          throw new Error("Enter a Control Plane admin token.");
        }
        const session = await api.verifyAdminToken(controlUrl, adminToken.trim());
        onSignedIn(session);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authentication failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        padding: 20,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 8,
              background: "var(--accent)",
              color: "#fff",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 600,
              fontSize: 12,
            }}
          >
            AI
          </div>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600 }}>AI Bridge</div>
            <div className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
              Control Plane Console
            </div>
          </div>
        </div>
        <Button size="sm" onClick={onToggleTheme}>
          {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
          <span>{theme === "dark" ? "Light" : "Dark"}</span>
        </Button>
      </div>

      <div
        style={{
          flex: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "28px 0",
        }}
      >
        <SurfaceCard className="modal-card" style={{ width: "100%", maxWidth: 440, padding: 26 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
            <ShieldCheck size={18} style={{ color: "var(--accent)" }} />
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600, letterSpacing: "-0.01em" }}>
              Sign in to Control Plane
            </h1>
          </div>
          <p style={{ margin: "0 0 18px", fontSize: 12.5, color: "var(--text-secondary)" }}>
            Authenticate to manage enrolled nodes, dispatch tasks, inspect live desktop sessions, and enforce security policy.
          </p>

          <div
            style={{
              display: "inline-flex",
              padding: 4,
              borderRadius: 8,
              gap: 4,
              background: "var(--bg-input)",
              border: "1px solid var(--border-hair)",
              marginBottom: 18,
            }}
          >
            <Chip active={authTab === "credentials"} onClick={() => setAuthTab("credentials")}>
              Credentials
            </Chip>
            <Chip active={authTab === "token"} onClick={() => setAuthTab("token")}>
              Admin Token
            </Chip>
          </div>

          <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: 6,
                }}
              >
                <label style={{ fontSize: 12, fontWeight: 500 }}>Control Plane URL</label>
                <button
                  type="button"
                  onClick={handleTestEndpoint}
                  style={{
                    background: "transparent",
                    border: 0,
                    padding: 0,
                    color: "var(--accent)",
                    fontSize: 11.5,
                    cursor: "pointer",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <Wifi size={12} /> Test /health
                </button>
              </div>
              <input
                type="text"
                className="field mono"
                placeholder="Same origin (leave empty) or https://..."
                value={controlUrl}
                onChange={(e) => setControlUrl(e.target.value)}
              />
              {probe && (
                <div style={{ marginTop: 6 }}>
                  <StatusBadge status={probe.ok ? "ok" : "err"} label={probe.text} />
                </div>
              )}
            </div>

            {authTab === "credentials" ? (
              <>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
                    Username
                  </label>
                  <input
                    type="text"
                    className="field"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    autoComplete="username"
                  />
                </div>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
                    Password
                  </label>
                  <input
                    type="password"
                    className="field"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete="current-password"
                    placeholder="Enter operator or admin password"
                  />
                </div>
              </>
            ) : (
              <div>
                <label style={{ display: "block", fontSize: 12, fontWeight: 500, marginBottom: 6 }}>
                  Admin Token (<code className="mono">X-Admin-Token</code>)
                </label>
                <input
                  type="password"
                  className="field mono"
                  value={adminToken}
                  onChange={(e) => setAdminToken(e.target.value)}
                  placeholder="Paste bootstrap or CI admin token"
                />
              </div>
            )}

            {error && (
              <div
                style={{
                  padding: "9px 12px",
                  borderRadius: 8,
                  fontSize: 12,
                  background: "rgba(239,68,68,0.12)",
                  border: "1px solid rgba(239,68,68,0.28)",
                  color: "var(--rose)",
                }}
              >
                {error}
              </div>
            )}

            <Button type="submit" variant="primary" disabled={busy} style={{ width: "100%", marginTop: 4 }}>
              {busy ? "Authenticating..." : "Sign In"}
            </Button>

            {oidcEnabled && (
              <Button
                type="button"
                onClick={() => {
                  const base = controlUrl.trim().replace(/\/+$/, "");
                  window.location.href = `${base}/api/auth/oidc/login`;
                }}
                style={{ width: "100%" }}
              >
                Continue with OIDC SSO
              </Button>
            )}
          </form>
        </SurfaceCard>
      </div>
    </div>
  );
}

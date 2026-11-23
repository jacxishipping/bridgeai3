# Threat model

**Updated:** 23 November 2026 · Posture: [PRODUCTION_READINESS.md](../analysis/PRODUCTION_READINESS.md) · [security.md](./security.md)

Assets: the device's workspace, its services, credentials, and the audit
trail. Adversaries: a compromised/misled AI client, prompt-injection
content in files/logs/web pages, a stolen device token, a malicious
workspace file, and a network attacker on an exposed endpoint.

| Threat | Mitigation in v0.2 | Residual / future |
|---|---|---|
| Prompt injection in file/log content overriding policy | Tool results are data; policy lives in frozen admin config, never in prompt text; Tier-4 needs a human approval record | Human must still review approval requests carefully |
| Path traversal / symlink escape | confined_path() resolves symlinks against the resolved root; escaping symlinks omitted from listings | Workspace root itself must not be / or home |
| Arbitrary command execution | No generic shell tool; allowlisted argv only, shell=False | Test/linter commands are fixed strings only |
| Stolen device token | Tokens hashed (SHA-256) at rest, shown once, revocable; revocation checked on every agent call | Rotate tokens periodically; optional reverse-proxy mTLS + plane fingerprint gate (`AI_BRIDGE_DEVICE_MTLS`) — see docs/support-matrix.md |
| Stolen admin token | Fail-closed if unset in production readiness; dashboard stores it only in browser session storage (never localStorage) | Prefer OIDC (`AI_BRIDGE_OIDC_*`, `/api/auth/oidc/*`) over sharing the legacy admin token |
| Secret leakage in logs/results | redact() on audit + tool output; secret-named files unreadable | Redaction is pattern-based; keep secrets out of the workspace entirely |
| Destructive writes | Backup before edit/delete/deploy; Tier-4 approval; staging-only deploy | Backups are local; copy them off-device for real recovery |
| Runaway tasks | Timeouts, output caps, concurrency semaphore, process-group cancellation | Resource (CPU/RAM) cgroups are NOT implemented; only CPU/memory/file-size rlimits exist (see CHANGELOG 0.3.0). Do not claim cgroup enforcement. |
| Unauthorized policy change | Policy is env/config-owned; no tool edits it; no tool grants privileges | Signed config and admin audit alerts are NOT implemented (no code found as of 2026-11-17); do not list them as a v0.3 item. |
| Exposed plane on the internet | Loopback defaults; TLS + reverse proxy required for remote use (docs/remote-access.md) | Add rate-limit per tenant + WAF rules in production |

Out of scope for this deepen: Vault/KMS secret backends, macOS agent
verification, and privileged operations beyond the allowlisted set
(deliberately excluded). Signed agent updates are NOT out of scope any
more: Ed25519 manifest verification is implemented in v0.3
(`src/ai_bridge/updates.py`; see docs/security.md §2b) and fleet updates
must be verified on every update path. Plane-side device mTLS PKI: use
reverse-proxy client certs; the plane-side fingerprint gate is code
present, end-to-end verification pending (see
docs/implementation-status.md).
OIDC login: code present, flow unverified until E2E passes
(`control/oidc.py` + `app.py`).

# Security policy

- Report vulnerabilities privately to the repository maintainer; do not
  open a public issue with exploit details.
- Supported version: v0.3.x (this release). See [docs/security.md](docs/security.md)
  for the model and [docs/threat-model.md](docs/threat-model.md) for residual risks.
- Production posture: [analysis/PRODUCTION_READINESS.md](analysis/PRODUCTION_READINESS.md)
  (lab GO; controlled fleet CONDITIONAL GO; public SaaS NO-GO).
- Never run AI Bridge as root/SYSTEM, never expose `:8000`/`:8001` without
  TLS + token auth, and never put secrets in the workspace or chat.

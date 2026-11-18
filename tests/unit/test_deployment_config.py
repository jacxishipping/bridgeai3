"""Deployment-config regression tests (Wave 1A: R-P0-14, R-P0-15, P-03,
P-04, P-05, P-06, P-09, R-P1-53).

Stdlib only — no `ai_bridge` imports and no third-party dependency — so
these tests run even when the full package cannot be installed (the CI
`deployment-config` job runs them with plain `python3`).

Compose YAML is parsed with PyYAML when available; otherwise a regex
fallback scans the raw text for the same `VAR=value` entries. nginx and
the installer scripts are plain-text assertions.

Run either way:
    pytest -q tests/unit/test_deployment_config.py
    python3 tests/unit/test_deployment_config.py -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEV_COMPOSE = REPO_ROOT / "docker-compose.dev.yml"
RENDER = REPO_ROOT / "render.yaml"
CFN = REPO_ROOT / "deployment/aws/cloudformation.yml"
NGINX = REPO_ROOT / "deployment/docker/nginx.conf"
DEPLOY_PROD = REPO_ROOT / "deployment/linux/deploy-production.sh"
DOCKERFILE_PROD = REPO_ROOT / "deployment/docker/Dockerfile.prod"
MAGIC_SH = REPO_ROOT / "magic-install.sh"
MAGIC_SH_LINUX = REPO_ROOT / "deployment/linux/magic-install.sh"
MAGIC_PS1 = REPO_ROOT / "magic-install.ps1"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# Known default / placeholder secrets that must never appear in a shipped
# deployment file (R-P0-15). These literals appear here only as the
# denylist for the scan; the scanned files must not contain them.
KNOWN_DEFAULT_SECRETS = (
    "aibridge-dev-password",
    "change-me-in-production",
    "aibridge_turn_shared_secret_2026",
    "dev-admin-token",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose_env(path: Path) -> dict[str, str]:
    """Return {VAR: raw value} for every `VAR=...` in a compose file.

    Prefers a real YAML parse; falls back to a regex scan of the raw
    text when PyYAML is not installed.
    """
    text = _read(path)
    try:
        import yaml  # type: ignore[import]

        data = yaml.safe_load(text) or {}
        env: dict[str, str] = {}
        for service in (data.get("services") or {}).values():
            raw = (service or {}).get("environment") or []
            items = raw.items() if isinstance(raw, dict) else raw
            for item in items:
                if isinstance(item, str) and "=" in item:
                    key, value = item.split("=", 1)
                    env.setdefault(key.strip(), value.strip())
        if env:
            return env
    except ImportError:
        pass
    env = {}
    for match in re.finditer(r"^\s*-\s*([A-Z][A-Z0-9_]*)=(.*)$", text, re.M):
        env.setdefault(match.group(1), match.group(2).strip())
    return env


def _nginx_block(text: str, header_re: str) -> str:
    """Return the text of the first nginx block starting at `header_re`.

    Brace-counts from the opening `{` so nested locations are included.
    """
    match = re.search(header_re, text)
    assert match is not None, f"nginx block not found: {header_re}"
    start = text.index("{", match.start())
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces in nginx block: {header_re}")


class TestTier3Default(unittest.TestCase):
    def test_prod_compose_tier3_defaults_to_0(self):
        env = _compose_env(PROD_COMPOSE)
        self.assertIn("AI_BRIDGE_ALLOW_TIER3", env)
        self.assertEqual(env["AI_BRIDGE_ALLOW_TIER3"], "${AI_BRIDGE_ALLOW_TIER3:-0}")

    def test_prod_compose_desktop_enabled_defaults_to_0(self):
        # Desktop automation is a separate gate from Tier 3: the plane
        # reads AI_BRIDGE_DESKTOP_ENABLED (default 0) and the agent
        # denies desktop tools unless it is true on both sides. The
        # control-api never received this variable, so a WebRTC
        # session's start_webrtc_stream task failed with "Desktop
        # automation is not enabled" even with ALLOW_TIER3=1.
        env = _compose_env(PROD_COMPOSE)
        self.assertIn("AI_BRIDGE_DESKTOP_ENABLED", env)
        self.assertEqual(env["AI_BRIDGE_DESKTOP_ENABLED"], "${AI_BRIDGE_DESKTOP_ENABLED:-0}")

    def test_no_desktop_enabled_by_default_anywhere(self):
        for path in (PROD_COMPOSE, BASE_COMPOSE, DEV_COMPOSE, CFN):
            code = "\n".join(
                line for line in _read(path).splitlines()
                if not line.strip().startswith("#")
            )
            self.assertNotIn(
                "AI_BRIDGE_DESKTOP_ENABLED=1",
                code,
                f"{path.name} still enables desktop automation by default",
            )
            self.assertNotRegex(
                code, r"AI_BRIDGE_DESKTOP_ENABLED=\$\{[^}]*:-1\}",
            )

    def test_no_tier3_enabled_by_default_anywhere(self):
        # Strip full-line comments first: prose may mention the opt-in
        # value ("set it to 1") without enabling anything.
        for path in (PROD_COMPOSE, BASE_COMPOSE, DEV_COMPOSE, CFN):
            code = "\n".join(
                line for line in _read(path).splitlines()
                if not line.strip().startswith("#")
            )
            self.assertNotIn(
                "AI_BRIDGE_ALLOW_TIER3=1",
                code,
                f"{path.name} still enables Tier 3 by default",
            )
            self.assertNotRegex(
                code, r"AI_BRIDGE_ALLOW_TIER3=\$\{[^}]*:-1\}",
            )

    def test_render_tier3_explicitly_0(self):
        text = _read(RENDER)
        self.assertRegex(text, r"key: AI_BRIDGE_ALLOW_TIER3\s*\n\s*value: \"0\"")


class TestNoDefaultSecrets(unittest.TestCase):
    def test_no_known_default_secrets_in_compose_files(self):
        for path in (PROD_COMPOSE, BASE_COMPOSE, DEV_COMPOSE):
            text = _read(path)
            for secret in KNOWN_DEFAULT_SECRETS:
                self.assertNotIn(secret, text, f"{path.name} contains {secret!r}")

    def test_no_known_default_secrets_in_env_example(self):
        text = _read(ENV_EXAMPLE)
        for secret in KNOWN_DEFAULT_SECRETS:
            self.assertNotIn(secret, text, f".env.example contains {secret!r}")

    def test_postgres_password_fails_fast(self):
        for path in (PROD_COMPOSE, BASE_COMPOSE):
            env = _compose_env(path)
            self.assertIn("POSTGRES_PASSWORD", env, path.name)
            self.assertIn(
                ":?",
                env["POSTGRES_PASSWORD"],
                f"{path.name}: POSTGRES_PASSWORD must use ${{VAR:?message}}",
            )

    def test_turn_secret_fails_fast(self):
        # The TURN secret lives in the coturn `command:` list, not in an
        # `environment:` block, so check the raw text in both files.
        for path in (PROD_COMPOSE, BASE_COMPOSE):
            self.assertRegex(
                _read(path),
                r"--static-auth-secret=\$\{AI_BRIDGE_TURN_SECRET:\?[^}]+\}",
                f"{path.name}: TURN secret must use ${{VAR:?message}}",
            )

    def test_deploy_script_generates_turn_secret(self):
        text = _read(DEPLOY_PROD)
        self.assertIn("AI_BRIDGE_TURN_SECRET=${TURN_SECRET}", text)
        self.assertIn("TURN_SECRET=$(openssl rand -hex 32)", text)
        self.assertIn('chmod 600 "$ENV_FILE"', text)

    def test_deploy_script_writes_ai_bridge_domain(self):
        # EC2 deploy: docker-compose.prod.yml reads AI_BRIDGE_DOMAIN
        # (NGINX_DOMAIN, AI_BRIDGE_PUBLIC_URL/BASE_URL, CORS/DASHBOARD
        # URLs), but the deploy script only wrote DOMAIN — so a deploy
        # with DOMAIN=clideos.us silently left nginx and the plane
        # thinking the domain was jxiptvplayer.app. The generated
        # .env.production must carry AI_BRIDGE_DOMAIN from DOMAIN.
        text = _read(DEPLOY_PROD)
        self.assertIn('AI_BRIDGE_DOMAIN="${DOMAIN:-jxiptvplayer.app}"', text)

    def test_control_api_receives_turn_secret(self):
        # R-P0-15 follow-up (EC2 deploy): the production readiness check
        # reads AI_BRIDGE_TURN_SECRET inside the control-api process
        # (production_readiness.turn_credentials_configured, gated by
        # /health/ready). The secret was generated by the deploy script
        # and passed to coturn, but the control-api service never
        # received it, so /health/ready stayed 503 with
        # turn_secret_missing. The control-api environment must carry
        # it (fail fast when unset).
        env = _compose_env(PROD_COMPOSE)
        self.assertIn("AI_BRIDGE_TURN_SECRET", env)
        self.assertTrue(
            env["AI_BRIDGE_TURN_SECRET"].startswith("${AI_BRIDGE_TURN_SECRET:?"),
            "docker-compose.prod.yml must fail fast without AI_BRIDGE_TURN_SECRET",
        )

    def test_control_api_declares_legacy_admin_flag(self):
        # F-05-05 (Wave 2A): AI_BRIDGE_ADMIN_TOKEN alone does not enable
        # the legacy admin token; the operator must opt in with
        # AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN=1. The control-api
        # service must pass the flag through (default off) or a staging
        # operator cannot enable it from .env.production at all.
        env = _compose_env(PROD_COMPOSE)
        self.assertIn("AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN", env)
        self.assertIn(
            ":-0}",
            env["AI_BRIDGE_ENABLE_LEGACY_ADMIN_TOKEN"],
            "legacy admin flag must default to 0 (off) in compose",
        )

    def test_control_api_declares_turn_host(self):
        # F-04-18 / F13-11 (EC2 WebRTC): the ice-servers endpoint mints
        # ephemeral TURN credentials from AI_BRIDGE_TURN_SECRET +
        # AI_BRIDGE_TURN_HOST. The secret was wired but the host was
        # never passed, so the plane returned STUN-only and
        # restrictive-NAT peers had no relay path. The control-api
        # environment must declare the host, defaulting to the plane
        # domain (the coturn service runs on this host).
        env = _compose_env(PROD_COMPOSE)
        self.assertIn("AI_BRIDGE_TURN_HOST", env)
        self.assertIn(
            "AI_BRIDGE_DOMAIN",
            env["AI_BRIDGE_TURN_HOST"],
            "TURN host must default to the plane domain in compose",
        )


class TestNginx(unittest.TestCase):
    def setUp(self):
        self.text = _read(NGINX)

    def test_http_redirects_to_https(self):
        # P-03: :80 serves the ACME challenge and redirects everything else.
        http_block = _nginx_block(self.text, r"server\s*\{\s*\n\s*listen 80;")
        self.assertIn("/.well-known/acme-challenge/", http_block)
        self.assertIn("return 301 https://$host$request_uri;", http_block)
        self.assertNotIn("proxy_pass", http_block)

    def test_metrics_is_protected(self):
        metrics = _nginx_block(self.text, r"location /metrics\s*\{")
        self.assertIn("auth_request /internal/verify;", metrics)
        self.assertIn("limit_req", metrics)

    def test_mcp_uses_oauth_auth_request(self):
        mcp = _nginx_block(self.text, r"location = /mcp\s*\{")
        self.assertIn("auth_request /internal/verify;", mcp)
        # The MCP DNS-rebinding guard requires a local Host value.
        self.assertIn("proxy_set_header Host 127.0.0.1:8001;", mcp)

    def test_internal_verify_location_shipped(self):
        verify = _nginx_block(self.text, r"location = /internal/verify\s*\{")
        self.assertIn("internal;", verify)
        self.assertIn("proxy_pass http://oauth_gateway/internal/verify;", verify)
        self.assertIn("proxy_set_header Authorization $http_authorization;", verify)

    def test_control_plane_strips_mtls_fingerprint_headers(self):
        # F-05-06 (Wave 6A): nginx forwards unknown client headers by
        # default, so a client could spoof X-Client-Cert-Fingerprint
        # straight through to the plane. The control-plane location must
        # clear every fingerprint header the plane trusts. (Match on the
        # comment, not bare "location /" — the :80 server has one too.)
        plane = _nginx_block(self.text, r"# Proxy to Control Plane API\s*\n\s*location /\s*\{")
        self.assertIn("proxy_pass http://control_plane;", plane)
        for header in (
            "X-Client-Cert-Fingerprint",
            "X-SSL-Client-Fingerprint",
            "X-Forwarded-Tls-Client-Cert-Info",
            "X-Client-Cert",
        ):
            self.assertIn(f'proxy_set_header {header} "";', plane)


class TestInstallerTls(unittest.TestCase):
    def test_no_insecure_curl_in_magic_install_sh(self):
        for path in (MAGIC_SH, MAGIC_SH_LINUX):
            text = _read(path)
            self.assertNotRegex(text, r"curl\s+[^\n]*?(?:\s-k\b|--insecure\b)",
                                f"{path} still disables TLS verification")
            self.assertNotIn("curl -k", text)

    def test_enrollment_verifies_tls_with_explicit_ca_option(self):
        # P-09: enrollment must verify TLS; a private CA is supplied
        # explicitly via --cacert, never by skipping verification.
        for path in (MAGIC_SH, MAGIC_SH_LINUX):
            text = _read(path)
            self.assertIn("--fail", text)
            self.assertIn('AI_BRIDGE_CA_BUNDLE', text)
            self.assertIn('--cacert "$AI_BRIDGE_CA_BUNDLE"', text)

    def test_no_cert_bypass_in_magic_install_ps1(self):
        text = _read(MAGIC_PS1)
        self.assertNotIn("SkipCertificateCheck", text)
        self.assertNotIn("ServerCertificateValidationCallback", text)

    def test_deploy_script_health_probe_has_no_insecure_flag(self):
        text = _read(DEPLOY_PROD)
        self.assertNotRegex(text, r"curl\s+[^\n]*-k\b")


class TestEnvExample(unittest.TestCase):
    def test_all_required_vars_documented(self):
        text = _read(ENV_EXAMPLE)
        for var in (
            "AI_BRIDGE_ADMIN_TOKEN",
            "SECRET_KEY",
            "AI_BRIDGE_CSRF_SECRET",
            "DATABASE_URL",
            "REDIS_URL",
            "POSTGRES_PASSWORD",
            "AI_BRIDGE_PUBLIC_URL",
            "AI_BRIDGE_DOMAIN",
            "AI_BRIDGE_TURN_SECRET",
            "AI_BRIDGE_CORS_ORIGINS",
            "AI_BRIDGE_EMBED_MCP",
            "AI_BRIDGE_MCP_HOST",
            "AI_BRIDGE_OAUTH_HOST",
            "AI_BRIDGE_ALLOW_TIER3",
        ):
            self.assertRegex(text, rf"(?m)^{var}=", f".env.example omits {var}")

    def test_env_example_has_no_secret_defaults(self):
        # Non-secret operational defaults (port, db name, TTL) are fine;
        # anything that is or contains a credential must be empty.
        secret_vars = {
            "AI_BRIDGE_ADMIN_TOKEN",
            "SECRET_KEY",
            "AI_BRIDGE_CSRF_SECRET",
            "DATABASE_URL",
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "AI_BRIDGE_TURN_SECRET",
            "AI_BRIDGE_TURN_CREDENTIAL",
            "AI_BRIDGE_OIDC_CLIENT_SECRET",
            "AI_BRIDGE_M365_CLIENT_SECRET",
            "AI_BRIDGE_DEVICE_TOKEN",
        }
        for line in _read(ENV_EXAMPLE).splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in secret_vars:
                self.assertEqual(
                    value.strip(), "",
                    f".env.example ships a default for secret {key}",
                )
        self.assertIn("AI_BRIDGE_ALLOW_TIER3=0", _read(ENV_EXAMPLE))


class TestCsrfSharedSecret(unittest.TestCase):
    """F-05-02 (Wave 6): one shared CSRF secret across all workers.

    A per-process random secret breaks session CSRF on multi-worker
    deployments (observed live in Wave 5E). Each deployment surface
    must therefore declare the shared secret (or fail fast without it).
    """

    def test_render_generates_csrf_secret(self):
        text = _read(RENDER)
        self.assertRegex(
            text,
            r"(?m)^\s*- key: AI_BRIDGE_CSRF_SECRET\s*\n\s*generateValue: true",
            "render.yaml must generate one shared AI_BRIDGE_CSRF_SECRET",
        )

    def test_compose_requires_csrf_secret(self):
        env = _compose_env(PROD_COMPOSE)
        self.assertIn("AI_BRIDGE_CSRF_SECRET", env)
        self.assertTrue(
            env["AI_BRIDGE_CSRF_SECRET"].startswith("${AI_BRIDGE_CSRF_SECRET:?"),
            "docker-compose.prod.yml must fail fast without AI_BRIDGE_CSRF_SECRET",
        )

    def test_dockerfile_prod_workers_match_gunicorn(self):
        text = _read(DOCKERFILE_PROD)
        m_env = re.search(r"(?m)^ENV AI_BRIDGE_WORKERS=(\d+)", text)
        self.assertIsNotNone(m_env, "Dockerfile.prod must declare AI_BRIDGE_WORKERS")
        m_cmd = re.search(r'"-w",\s*"(\d+)"', text)
        self.assertIsNotNone(m_cmd, "Dockerfile.prod must run gunicorn with -w N")
        self.assertEqual(
            m_env.group(1), m_cmd.group(1),
            "AI_BRIDGE_WORKERS must match the gunicorn -w count or the "
            "boot fail-fast misfires",
        )
        self.assertNotEqual(m_env.group(1), "1", "prod image runs >1 worker")

    def test_deploy_script_generates_csrf_secret(self):
        text = _read(DEPLOY_PROD)
        self.assertIn("CSRF_SECRET=$(openssl rand -hex 32)", text)
        self.assertIn("AI_BRIDGE_CSRF_SECRET=${CSRF_SECRET}", text)


class TestPinnedDependencies(unittest.TestCase):
    """P-07 (Wave 7): locked deps, pinned prod extras, `npm ci`."""

    def test_dockerfile_prod_installs_pinned_prod_extra(self):
        text = _read(DOCKERFILE_PROD)
        self.assertIn("-e .[prod]", text, "Dockerfile.prod must install pyproject [prod]")
        self.assertNotIn("prometheus-client>=", text)
        self.assertNotIn("psycopg[binary]>=", text)
        self.assertNotIn('pip install --no-cache-dir gunicorn', text)

    def test_pyproject_prod_extra_pins_exactly(self):
        text = _read(REPO_ROOT / "pyproject.toml")
        for pinned in (
            "gunicorn==23.0.0",
            "psycopg[binary]==3.3.9",
            "prometheus-client==0.21.0",
        ):
            self.assertIn(pinned, text, f"pyproject [prod] must pin {pinned}")

    def test_dashboard_builds_use_npm_ci(self):
        self.assertTrue(
            (REPO_ROOT / "apps/dashboard/package-lock.json").exists(),
            "npm ci requires apps/dashboard/package-lock.json",
        )
        for path in (DOCKERFILE_PROD, RENDER):
            text = _read(path)
            self.assertIn("npm ci", text, f"{path.name} must build the dashboard with npm ci")
            # Strip comment lines first: the P-07 comments themselves
            # mention `npm install` while explaining why it is gone.
            code = "\n".join(
                line for line in text.splitlines()
                if not line.strip().startswith("#")
            )
            self.assertNotIn("npm install", code, f"{path.name} must not use npm install")

    def test_requirements_lock_exists_and_is_pinned(self):
        lock = REPO_ROOT / "requirements.lock"
        self.assertTrue(lock.exists(), "requirements.lock must exist (P-07)")
        text = _read(lock)
        header = "\n".join(text.splitlines()[:40])
        self.assertIn("pip freeze", header, "lock header must state how it was generated")
        self.assertIn("piptools", header, "lock header must record the pip-compile route")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            self.assertRegex(
                line, r"^(-e \.|[A-Za-z0-9_.\-]+==\S+)",
                f"lockfile line is not a pin: {line!r}",
            )
        for pinned in (
            "fastapi==0.163.1",
            "mcp==1.30.0",
            "psycopg==3.3.9",
            "uvicorn==0.54.0",
        ):
            self.assertIn(pinned, text, f"lockfile must pin {pinned}")

    def test_requirements_txt_no_longer_claims_locked(self):
        text = _read(REPO_ROOT / "requirements.txt")
        self.assertIn("requirements.lock", text)
        self.assertNotIn("Locked dependencies", text)


class TestCanonicalControlUrl(unittest.TestCase):
    """P-12 (Wave 7): one canonical default control URL (jxiptvplayer.app)."""

    @staticmethod
    def _default_url(path: Path, pattern: str) -> str:
        match = re.search(pattern, _read(path), re.M)
        assert match is not None, f"{path} must define DEFAULT_CONTROL_URL"
        return match.group(1)

    def test_all_install_paths_agree_on_production_host(self):
        canonical = "https://jxiptvplayer.app"
        self.assertEqual(
            self._default_url(REPO_ROOT / "src/ai_bridge/client.py",
                              r'^DEFAULT_CONTROL_URL = "([^"]+)"'),
            canonical,
        )
        self.assertEqual(
            self._default_url(REPO_ROOT / "deployment/windows/installer_gui.py",
                              r'^DEFAULT_CONTROL_URL = "([^"]+)"'),
            canonical,
        )
        self.assertEqual(
            self._default_url(MAGIC_SH, r'^DEFAULT_CONTROL_URL="([^"]+)"'),
            canonical,
        )
        self.assertEqual(
            self._default_url(MAGIC_SH_LINUX, r'^DEFAULT_CONTROL_URL="([^"]+)"'),
            canonical,
        )

    def test_sdk_has_no_silent_localhost_default(self):
        text = _read(REPO_ROOT / "src/ai_bridge/client.py")
        self.assertNotIn('or "http://127.0.0.1:8000"', text)
        self.assertIn("or DEFAULT_CONTROL_URL", text)

    def test_installer_normalizes_dashboard_host_to_canonical(self):
        text = _read(REPO_ROOT / "deployment/windows/installer_gui.py")
        self.assertIn("return DEFAULT_CONTROL_URL", text)
        self.assertNotIn('"https://ai-bridge-control.onrender.com"', text)


def _render_env_keys(path: Path) -> set[str]:
    return set(re.findall(r"(?m)^\s*- key: ([A-Z][A-Z0-9_]*)", _read(path)))


# P-15 (Wave 7): control-plane keys that must exist in BOTH render.yaml
# and docker-compose.prod.yml with the same meaning. Keys intentionally
# present in only one file are documented in render.yaml's matrix comment.
SHARED_CORE_KEYS = (
    "DATABASE_URL",
    "REDIS_URL",
    "AI_BRIDGE_ADMIN_TOKEN",
    "AI_BRIDGE_CSRF_SECRET",
    "AI_BRIDGE_PUBLIC_URL",
    "AI_BRIDGE_PUBLIC_BASE_URL",
    "AI_BRIDGE_OIDC_ISSUER",
    "AI_BRIDGE_OIDC_CLIENT_ID",
    "AI_BRIDGE_OIDC_CLIENT_SECRET",
    "AI_BRIDGE_OIDC_REDIRECT_URI",
    "AI_BRIDGE_OIDC_DASHBOARD_URL",
    "AI_BRIDGE_ALLOW_TIER3",
    "AI_BRIDGE_DEVICE_TOKEN_TTL_DAYS",
    "AI_BRIDGE_CORS_ORIGINS",
    "AI_BRIDGE_DASHBOARD_URL",
    "AI_BRIDGE_PROFILE",
)


class TestEnvMatrix(unittest.TestCase):
    """P-15 (Wave 7): render.yaml / docker-compose.prod.yml env matrix aligned."""

    def test_shared_core_keys_present_in_both(self):
        render_keys = _render_env_keys(RENDER)
        compose_env = _compose_env(PROD_COMPOSE)
        for key in SHARED_CORE_KEYS:
            self.assertIn(key, render_keys, f"render.yaml omits {key}")
            self.assertIn(key, compose_env, f"docker-compose.prod.yml omits {key}")

    def test_matrix_documented_in_both_files(self):
        self.assertIn("Env matrix (P-15", _read(RENDER))
        self.assertIn("Env matrix (P-15", _read(PROD_COMPOSE))

    def test_render_topology_basis_corrected(self):
        text = _read(RENDER)
        self.assertIn("LIVE staging plane", text)
        self.assertIn("Starter instances WITH managed Redis", text)


if __name__ == "__main__":
    unittest.main()

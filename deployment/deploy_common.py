"""Shared deployment helpers for deploy_to_aws.py / deploy_to_droplet.py.

P-08 (Wave 7): the two deploy scripts were ~90% duplicated (same SSH/SSM
runner, same compose commands), carried different defaults, and neither
had a post-deploy health gate — and both deployed a floating
``git pull --ff-only origin main`` (unsigned, unpinned).

This module is the single implementation of:

- :func:`run_remote` — the SSH/SSM remote runner (one copy, not two).
- Pinned deployments — :func:`resolve_pinned_commit` and
  :func:`pinned_checkout_command`. A deploy NEVER floats ``origin/main``:
  the commit comes from a verified signed release manifest
  (``ai_bridge.updates`` — Ed25519 signature verified BEFORE any fetch)
  or from an explicit ``--commit <40-hex SHA>`` argument. The remote
  checks out exactly that SHA and verifies ``HEAD == <sha>`` afterwards.
  With neither a manifest nor ``--commit``, deployment refuses to run.
- :func:`wait_for_health` — the post-deploy health gate, polling
  ``/health/ready`` until healthy or timeout (200 or 301 count as
  healthy, matching deployment/linux/deploy-production.sh, where nginx
  redirects :80 to HTTPS). A deploy that never becomes healthy exits
  non-zero instead of printing success.

No function in this module performs a deployment itself; the scripts
drive it. Do NOT run any of this against a live host from a test —
the unit tests cover the pure helpers plus the health gate against a
local fake HTTP endpoint only.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = str(_REPO_ROOT / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from ai_bridge.updates import load_signed_manifest, signed_update_configured  # noqa: E402

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Shared defaults (were duplicated, with divergent values, in both scripts).
COMPOSE = "sudo docker compose --env-file .env.production -f docker-compose.prod.yml"
DEFAULT_APP_DIR = "/opt/ai-bridge"
DEFAULT_AWS_REGION = "us-east-2"
HEALTH_PATH = "/health/ready"
DEFAULT_HEALTH_TIMEOUT_S = 300
DEFAULT_HEALTH_INTERVAL_S = 5
# deploy-production.sh treats 200 and 301 as healthy (nginx :80 -> HTTPS).
HEALTHY_STATUS = (200, 301)


def is_valid_commit_sha(value: object) -> bool:
    """True only for a full 40-hex commit SHA (case-insensitive)."""
    return isinstance(value, str) and bool(_COMMIT_SHA_RE.match(value.strip().lower()))


def normalize_commit_sha(value: object) -> str:
    """Return the lowercased SHA, or raise ValueError if it is not one."""
    if not is_valid_commit_sha(value):
        raise ValueError(
            f"not a full 40-hex commit SHA: {value!r} "
            "(pass --commit <40-hex SHA>, e.g. the SHA from a signed release manifest)"
        )
    return str(value).strip().lower()


def resolve_pinned_commit(
    *,
    explicit_commit: str | None = None,
    manifest_path: Path | str | None = None,
    signature_path: Path | str | None = None,
    public_key_path: Path | str | None = None,
) -> str:
    """Resolve the exact commit a deploy must check out. Never floats.

    Precedence:
    1. A configured signed release manifest (``AI_BRIDGE_UPDATE_*`` env
       vars or explicit paths): the Ed25519 signature is verified by
       ``ai_bridge.updates.load_signed_manifest`` BEFORE anything is
       fetched, and ``manifest["commit"]`` must be a full 40-hex SHA.
       A bad signature raises PermissionError; deployment refuses.
    2. An explicit ``--commit <40-hex SHA>`` argument (validated).
    3. Neither: ValueError — the caller must refuse to deploy.

    Returns the pinned SHA (lowercase).
    """
    if signed_update_configured(manifest_path, signature_path, public_key_path):
        manifest, _signature_b64 = load_signed_manifest(
            manifest_path, signature_path, public_key_path
        )
        return normalize_commit_sha(manifest.get("commit"))
    if explicit_commit:
        return normalize_commit_sha(explicit_commit)
    raise ValueError(
        "refusing an unpinned deploy: no signed release manifest is "
        "configured (AI_BRIDGE_UPDATE_MANIFEST_PATH / "
        "AI_BRIDGE_UPDATE_SIGNATURE_PATH / AI_BRIDGE_UPDATE_PUBLIC_KEY) "
        "and no --commit <40-hex SHA> was given. Deploy exactly a "
        "pinned commit — never a floating origin/main."
    )


def pinned_checkout_command(sha: str) -> str:
    """Remote shell fragment that checks out exactly ``sha`` and verifies HEAD.

    Runs inside the app dir (the caller prefixes ``cd <APP_DIR> &&``).
    ``git fetch origin`` (no branch) only fetches objects; the checkout
    is ``-f <sha>``, never ``origin/main``; the final ``test`` fails the
    step unless ``HEAD`` is exactly the pinned SHA.
    """
    pinned = normalize_commit_sha(sha)
    return (
        f"git fetch origin && git checkout -f {pinned} "
        f"&& test \"$(git rev-parse HEAD)\" = \"{pinned}\" "
        f"&& git log -1 --oneline"
    )


def run_remote(
    step: str,
    cmd: str,
    *,
    key: str = "",
    host: str = "",
    ssm_instance_id: str = "",
    app_dir: str = DEFAULT_APP_DIR,
    aws_region: str = DEFAULT_AWS_REGION,
) -> None:
    """Run ``cmd`` on the remote host (``cd <app_dir> && <cmd>``) and fail fast.

    SSM (``AWS-RunShellScript``) when ``ssm_instance_id`` is set, else SSH
    with ``key``. Prints the step and the command, streams stdout, and
    exits the process on a non-zero remote exit — identical semantics to
    the runner both deploy scripts used to duplicate.
    """
    print(f"\n=== {step} ===\n$ {cmd}")
    if ssm_instance_id:
        ssm_cmd = [
            "aws", "ssm", "send-command",
            "--instance-ids", ssm_instance_id,
            "--document-name", "AWS-RunShellScript",
            "--parameters", f"commands=['cd {app_dir} && {cmd}']",
            "--region", aws_region,
            "--output", "text",
        ]
        proc = subprocess.run(ssm_cmd, capture_output=True, text=True, timeout=30)
    else:
        if not key:
            sys.exit(
                "Set AI_BRIDGE_SSH_KEY to the path of your SSH private key, "
                "or set AI_BRIDGE_SSM_INSTANCE_ID for SSM."
            )
        ssh = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-i", key, host,
               f"cd {app_dir} && {cmd}"]
        # encoding="utf-8" + replace: Windows cp1252 readers blow up on
        # docker/apt build logs that contain non-ASCII bytes.
        proc = subprocess.run(
            ssh,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
    if (proc.stdout or "").strip():
        print(proc.stdout.strip())
    if (proc.stderr or "").strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if proc.returncode != 0:
        sys.exit(f"Step '{step}' failed with exit code {proc.returncode}")


def deploy_service_steps(
    compose: str = COMPOSE, *, force_recreate: bool = False
) -> list[tuple[str, str]]:
    """Build/restart/status steps shared by both deploy scripts.

    ``force_recreate`` reproduces deploy_to_droplet.py's old
    ``--force-recreate`` restart; the AWS script never used it. The
    checkout step is NOT here — it is :func:`pinned_checkout_command`
    and must run first, with the resolved pinned SHA.
    """
    recreate = " --force-recreate" if force_recreate else ""
    return [
        ("Build image", f"{compose} build control-api"),
        ("Restart app services",
         f"{compose} up -d --no-deps{recreate} control-api mcp-http oauth-gateway"),
        ("Recreate nginx (reload config)",
         f"{compose} up -d --no-deps --force-recreate nginx"),
        ("Status", "sudo docker ps --format '{{.Names}} {{.Status}}'"),
    ]


def health_url_for(
    *,
    explicit: str | None = None,
    domain: str = "",
    host: str = "",
) -> str:
    """Derive the ``/health/ready`` URL the post-deploy gate polls.

    Precedence: explicit ``AI_BRIDGE_HEALTH_URL`` > ``https://<domain>``
    (``AI_BRIDGE_DOMAIN``) > ``https://<host>`` (the SSH host part).
    A bare placeholder host (no dot, e.g. the old ``ec2-instance``
    default) cannot yield a URL — ValueError tells the operator to set
    AI_BRIDGE_DOMAIN or AI_BRIDGE_HEALTH_URL instead of probing a
    nonsense host. The gate must fail closed, not silently skip.
    """
    if explicit:
        base = explicit.rstrip("/")
        if base.endswith(HEALTH_PATH):
            return base
        if base.endswith("/health"):
            return base + "/ready"
        return base + HEALTH_PATH
    if domain:
        return f"https://{domain.strip().rstrip('/')}" + HEALTH_PATH
    host_part = (host or "").split("@")[-1].strip().rstrip("/")
    if host_part and "." in host_part:
        return f"https://{host_part}" + HEALTH_PATH
    raise ValueError(
        "cannot derive a health-check URL: set AI_BRIDGE_DOMAIN "
        "(e.g. jxiptvplayer.app) or AI_BRIDGE_HEALTH_URL "
        "(e.g. https://jxiptvplayer.app/health/ready); the configured "
        f"SSH host {host!r} is not a resolvable public hostname."
    )


def _default_urlopen(url, timeout=10):
    """GET ``url`` without following redirects (curl-like, as in
    deploy-production.sh: a 301 from nginx counts as healthy instead of
    being chased). A 3xx therefore surfaces as HTTPError with its code,
    which :func:`wait_for_health` scores against HEALTHY_STATUS."""
    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            return None

    return urllib.request.build_opener(_NoRedirectHandler()).open(url, timeout=timeout)


def wait_for_health(
    url: str,
    *,
    timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
    interval_s: float = DEFAULT_HEALTH_INTERVAL_S,
    urlopen=None,
) -> bool:
    """Poll ``url`` until it answers with a healthy status, or timeout.

    Returns True on the first 200/301 response, False when ``timeout_s``
    elapses without one. Connection errors, HTTP errors and timeouts
    count as "not yet healthy", never as an exception — the caller
    decides whether to fail the deploy. ``urlopen`` is injectable for
    tests (defaults to :func:`_default_urlopen`); ``interval_s``
    may be 0 in tests.
    """
    opener = urlopen or _default_urlopen
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            with opener(url, timeout=10) as resp:
                if resp.status in HEALTHY_STATUS:
                    print(f"Health gate: {url} -> {resp.status} (healthy)")
                    return True
                print(f"Health gate: {url} -> {resp.status} (waiting)")
        except urllib.error.HTTPError as exc:
            # Redirects are not followed (see _default_urlopen): a 301
            # from nginx IS the healthy signal deploy-production.sh uses.
            if exc.code in HEALTHY_STATUS:
                print(f"Health gate: {url} -> {exc.code} (healthy)")
                return True
            print(f"Health gate: {url} -> HTTP {exc.code} (waiting)")
        except Exception as exc:  # noqa: BLE001 - any probe failure means "not yet"
            print(f"Health gate: {url} -> {type(exc).__name__}: {exc} (waiting)")
        if time.monotonic() >= deadline:
            print(f"Health gate: {url} did not become healthy within {timeout_s:g}s")
            return False
        time.sleep(max(0.0, interval_s))


def health_gate_or_exit(
    url: str,
    *,
    timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
    interval_s: float = DEFAULT_HEALTH_INTERVAL_S,
) -> None:
    """Run the post-deploy health gate; exit non-zero if it never passes."""
    if not wait_for_health(url, timeout_s=timeout_s, interval_s=interval_s):
        sys.exit(
            f"Deployment finished but the control plane never became "
            f"healthy at {url} within {timeout_s:g}s — investigate before "
            f"announcing success (docker logs / /health/ready)."
        )
    print("\n✅ Control plane is healthy.")

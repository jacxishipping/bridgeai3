"""Multi-Channel Alert Dispatcher for AI Bridge Control Plane.

Dispatches structured, rich formatted alert notifications across:
- Slack (Block Kit webhook cards)
- Discord (Rich embedded webhooks)
- Telegram (Telegram Bot API Markdown messages)
- PagerDuty & Generic Webhooks (JSON incident payloads)
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse

import httpx

from ..security import validate_outbound_url

logger = logging.getLogger("ai_bridge.alerts")


def get_alert_config() -> dict[str, str]:
    """Retrieve alert webhook endpoints and tokens from environment."""
    return {
        "slack_webhook": os.environ.get("SLACK_WEBHOOK_URL", "").strip(),
        "discord_webhook": os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "generic_webhook": os.environ.get("ALERT_WEBHOOK_URL", "").strip(),
    }


def _send_http_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 6.0) -> bool:
    """Send an HTTP POST request with a JSON body.

    F-05-17 / SEC-11 (SSRF): the URL is validated (DNS resolution +
    rejection of private/loopback/link-local/reserved/metadata IPs)
    BEFORE connecting. Redirects are never followed automatically
    (``follow_redirects=False``); each redirect target is re-validated
    the same way, with a maximum of 3 hops.
    """
    req_headers = {"Content-Type": "application/json", "User-Agent": "AI-Bridge-AlertEngine/1.0"}
    if headers:
        req_headers.update(headers)

    try:
        target = validate_outbound_url(url)
    except ValueError as exc:
        logger.warning("Refusing to dispatch alert to %s: %s", url, exc)
        return False

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            resp = client.post(target, json=payload, headers=req_headers)
            hops = 0
            while resp.status_code in (301, 302, 303, 307, 308) and hops < 3:
                location = resp.headers.get("location")
                if not location:
                    break
                next_url = urllib.parse.urljoin(target, location)
                try:
                    target = validate_outbound_url(next_url)
                except ValueError as exc:
                    logger.warning("Refusing alert redirect to %s: %s", next_url, exc)
                    return False
                hops += 1
                resp = client.post(target, json=payload, headers=req_headers)
            if hops >= 3 and resp.status_code in (301, 302, 303, 307, 308):
                logger.warning("Alert dispatch to %s exceeded max redirects (3)", url)
                return False
            return resp.is_success
    except Exception as exc:
        logger.warning("Failed to dispatch alert to %s: %s", url, exc)
        return False


def _send_slack_alert(webhook_url: str, title: str, severity: str, details: dict, device_id: str | None) -> bool:
    """Format and send a Slack Block Kit alert message."""
    color = "#36a64f" if severity == "info" else "#f2c744" if severity == "warning" else "#e01e5a"
    fields = []
    if device_id:
        fields.append({"type": "mrkdwn", "text": f"*Device:* `{device_id}`"})
    for k, v in list(details.items())[:6]:
        fields.append({"type": "mrkdwn", "text": f"*{k.capitalize()}:* {v}"})

    payload = {
        "attachments": [
            {
                "color": color,
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*{severity.upper()}*: {title}"},
                    },
                    {"type": "section", "fields": fields} if fields else None,
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": f"AI Bridge Fleet Orchestrator • {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"}
                        ],
                    },
                ],
            }
        ]
    }
    # Clean None blocks
    payload["attachments"][0]["blocks"] = [b for b in payload["attachments"][0]["blocks"] if b is not None]
    return _send_http_json(webhook_url, payload)


def _send_discord_alert(webhook_url: str, title: str, severity: str, details: dict, device_id: str | None) -> bool:
    """Format and send a Discord embedded webhook alert."""
    color_int = 0x2ECC71 if severity == "info" else 0xF1C40F if severity == "warning" else 0xE74C3C
    fields = []
    if device_id:
        fields.append({"name": "Device ID", "value": f"`{device_id}`", "inline": True})
    for k, v in list(details.items())[:6]:
        fields.append({"name": str(k).capitalize(), "value": str(v), "inline": True})

    payload = {
        "embeds": [
            {
                "title": f"[{severity.upper()}] {title}",
                "color": color_int,
                "fields": fields,
                "footer": {"text": "AI Bridge Fleet Management"},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ]
    }
    return _send_http_json(webhook_url, payload)


def _send_telegram_alert(token: str, chat_id: str, title: str, severity: str, details: dict, device_id: str | None) -> bool:
    """Format and send a Telegram message via Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    emoji = "ℹ️" if severity == "info" else "⚠️" if severity == "warning" else "🚨"
    msg_lines = [f"{emoji} *[{severity.upper()}] {title}*"]
    if device_id:
        msg_lines.append(f"• *Device:* `{device_id}`")
    for k, v in list(details.items())[:6]:
        msg_lines.append(f"• *{k}:* {v}")
    msg_lines.append(f"\n_Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}_")

    payload = {
        "chat_id": chat_id,
        "text": "\n".join(msg_lines),
        "parse_mode": "Markdown",
    }
    return _send_http_json(url, payload)


def _send_generic_alert(webhook_url: str, event_type: str, severity: str, title: str, details: dict, device_id: str | None) -> bool:
    """Send generic JSON incident payload."""
    payload = {
        "event_type": event_type,
        "severity": severity,
        "title": title,
        "device_id": device_id,
        "details": details,
        "timestamp": time.time(),
        "source": "ai-bridge-control-plane",
    }
    return _send_http_json(webhook_url, payload)


def dispatch_alert(
    event_type: str,
    severity: str,
    title: str,
    details: dict | None = None,
    device_id: str | None = None,
    async_dispatch: bool = True,
    config_override: dict[str, str] | None = None,
) -> dict[str, bool]:
    """Dispatch an alert across all configured channels."""
    cfg = get_alert_config()
    if config_override:
        for k, v in config_override.items():
            if isinstance(v, str) and v.strip():
                cfg[k] = v.strip()
    details_dict = details or {}
    results = {}

    def _worker():
        if cfg["slack_webhook"]:
            results["slack"] = _send_slack_alert(cfg["slack_webhook"], title, severity, details_dict, device_id)
        if cfg["discord_webhook"]:
            results["discord"] = _send_discord_alert(cfg["discord_webhook"], title, severity, details_dict, device_id)
        if cfg["telegram_token"] and cfg["telegram_chat_id"]:
            results["telegram"] = _send_telegram_alert(cfg["telegram_token"], cfg["telegram_chat_id"], title, severity, details_dict, device_id)
        if cfg["generic_webhook"]:
            results["webhook"] = _send_generic_alert(cfg["generic_webhook"], event_type, severity, title, details_dict, device_id)

    if async_dispatch:
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return {"queued": True}
    else:
        _worker()
        return results


_LAST_ALERT_AT: dict[str, float] = {}
_ALERT_EVAL_LOCK = threading.Lock()


def evaluate_operational_alerts(
    conn,
    *,
    cooldown_sec: float = 300.0,
    config_override: dict[str, str] | None = None,
    dispatch: bool = True,
) -> list[dict]:
    """Evaluate operational health & DB capacity conditions and dispatch deduplicated alerts.

    Conditions checked:
    - ``stale_running_tasks``: running tasks past lease window (> 0)
    - ``stale_heartbeat_devices``: online devices with stale heartbeats (> 0)
    - ``dead_letter_backlog``: unreplayed DLQ entries >= threshold (default 5)
    - ``db_pool_exhaustion``: PostgreSQL pool waiting > 0 or utilization >= 85%
    - ``agent_spool_backlog``: agent offline spool pending >= threshold (default 25) or age >= 3600s
    - ``audit_chain_broken``: hash-chain verification failure
    """
    import json

    from . import db as dbmod
    from . import fleet_health as fhmod
    from . import orchestration as orchmod

    now_ts = dbmod.now()
    findings: list[dict] = []

    try:
        cutoff = now_ts - orchmod.DEFAULT_RUNNING_LEASE_SEC
        stale_tasks = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE status='running' AND updated_at < ?",
            (cutoff,),
        ).fetchone()["c"]
        if int(stale_tasks or 0) > 0:
            findings.append({
                "event_type": "stale_running_tasks",
                "severity": "warning",
                "title": f"{stale_tasks} stale running task(s) past lease window",
                "details": {"stale_running_tasks": int(stale_tasks), "runbook": "docs/runbooks.md#stale-running-tasks"},
            })
    except Exception:
        pass

    try:
        health = fhmod.summarize_fleet_health(conn)
        stale_devs = int(health.get("stale_device_count") or 0)
        if stale_devs > 0:
            findings.append({
                "event_type": "stale_heartbeat_devices",
                "severity": "warning",
                "title": f"{stale_devs} fleet device(s) have stale heartbeats",
                "details": {"stale_devices": stale_devs, "runbook": "docs/runbooks.md#stale-heartbeat-devices"},
            })
    except Exception:
        pass

    try:
        dlq_thresh = max(1, int(os.environ.get("AI_BRIDGE_ALERT_DLQ_THRESHOLD", "5") or "5"))
        dlq_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM task_dead_letters WHERE replayed_task_id IS NULL"
        ).fetchone()["c"]
        if int(dlq_cnt or 0) >= dlq_thresh:
            findings.append({
                "event_type": "dead_letter_backlog",
                "severity": "critical",
                "title": f"Dead-letter queue backlog reached {dlq_cnt} task(s)",
                "details": {"dead_letter_pending": int(dlq_cnt), "threshold": dlq_thresh, "runbook": "docs/runbooks.md#dead-letter-queue"},
            })
    except Exception:
        pass

    try:
        if hasattr(conn, "get_db_stats"):
            st = conn.get_db_stats()
            waiting = int(st.get("pool_waiting") or 0)
            util = float(st.get("pool_utilization") or 0.0)
            if waiting > 0 or util >= 0.85:
                findings.append({
                    "event_type": "db_pool_exhaustion",
                    "severity": "critical",
                    "title": f"Database connection pool pressure (waiting={waiting}, util={util:.0%})",
                    "details": {**st, "runbook": "docs/runbooks.md#database-pool-exhaustion"},
                })
    except Exception:
        pass

    try:
        spool_thresh = max(1, int(os.environ.get("AI_BRIDGE_ALERT_SPOOL_THRESHOLD", "25") or "25"))
        rows = conn.execute("SELECT id, name, spool_stats_json FROM devices WHERE revoked=0").fetchall()
        for r in rows:
            raw = r["spool_stats_json"] if hasattr(r, "keys") and "spool_stats_json" in r.keys() else ""
            if not raw:
                continue
            sp = json.loads(raw)
            if not isinstance(sp, dict):
                continue
            pending = int(sp.get("pending_count") or 0)
            age_s = float(sp.get("oldest_pending_age_sec") or 0.0)
            if pending >= spool_thresh or age_s >= 3600.0:
                findings.append({
                    "event_type": f"agent_spool_backlog:{r['id']}",
                    "severity": "warning",
                    "title": f"Agent spool backlog on {r['name']} (pending={pending}, oldest={int(age_s)}s)",
                    "device_id": r["id"],
                    "details": {"pending_count": pending, "oldest_pending_age_sec": round(age_s, 1), "runbook": "docs/runbooks.md#agent-spool-backlog"},
                })
    except Exception:
        pass

    try:
        if os.environ.get("AI_BRIDGE_ALERT_VERIFY_AUDIT", "0").strip().lower() in ("1", "true", "yes"):
            chain = dbmod.verify_audit_chain(conn)
            if not chain.get("ok"):
                findings.append({
                    "event_type": "audit_chain_broken",
                    "severity": "critical",
                    "title": f"Audit hash chain integrity failure at row {chain.get('broken_at')}",
                    "details": {**chain, "runbook": "docs/runbooks.md#audit-chain-failure"},
                })
    except Exception:
        pass

    fired: list[dict] = []
    if dispatch and findings:
        cfg = get_alert_config()
        if config_override:
            for k, v in config_override.items():
                if isinstance(v, str) and v.strip():
                    cfg[k] = v.strip()
        has_channel = bool(cfg["slack_webhook"] or cfg["discord_webhook"] or (cfg["telegram_token"] and cfg["telegram_chat_id"]) or cfg["generic_webhook"])
        with _ALERT_EVAL_LOCK:
            for item in findings:
                key = item["event_type"]
                last_at = _LAST_ALERT_AT.get(key, 0.0)
                if now_ts - last_at < cooldown_sec:
                    continue
                _LAST_ALERT_AT[key] = now_ts
                fired.append(item)
                if has_channel:
                    dispatch_alert(
                        event_type=key,
                        severity=item["severity"],
                        title=item["title"],
                        details=item.get("details"),
                        device_id=item.get("device_id"),
                        async_dispatch=True,
                        config_override=cfg,
                    )
    else:
        fired = findings
    return fired



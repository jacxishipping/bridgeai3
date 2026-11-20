"""Structured logging configuration and correlation context for AI Bridge.

Configures Python's logging with NDJSON output for production observability,
redacts device tokens, preserves standard ``extra={...}`` fields, and injects
request/task/device correlation identifiers via ``contextvars``.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.config
import os
import sys
import time
import traceback
from typing import Any

from .security import refuse_device_token_log

_CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar("ai_bridge_correlation_id", default="")
_TASK_ID: contextvars.ContextVar[str] = contextvars.ContextVar("ai_bridge_log_task_id", default="")
_DEVICE_ID: contextvars.ContextVar[str] = contextvars.ContextVar("ai_bridge_log_device_id", default="")
_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("ai_bridge_log_actor", default="")

# Standard logging.LogRecord attributes that should NOT be duplicated as extra fields.
_STANDARD_LOG_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def get_correlation_id() -> str:
    """Return the active correlation/trace ID for the current execution context."""
    return _CORRELATION_ID.get()


def set_log_context(
    *,
    correlation_id: str | None = None,
    task_id: str | None = None,
    device_id: str | None = None,
    actor: str | None = None,
) -> dict[str, contextvars.Token[str]]:
    """Bind structured correlation fields to the current contextvar scope."""
    tokens: dict[str, contextvars.Token[str]] = {}
    if correlation_id is not None:
        tokens["correlation_id"] = _CORRELATION_ID.set(str(correlation_id)[:128])
    if task_id is not None:
        tokens["task_id"] = _TASK_ID.set(str(task_id)[:128])
    if device_id is not None:
        tokens["device_id"] = _DEVICE_ID.set(str(device_id)[:128])
    if actor is not None:
        tokens["actor"] = _ACTOR.set(str(actor)[:128])
    return tokens


def reset_log_context(tokens: dict[str, contextvars.Token[str]]) -> None:
    """Restore contextvars using tokens returned by :func:`set_log_context`."""
    mapping = {
        "correlation_id": _CORRELATION_ID,
        "task_id": _TASK_ID,
        "device_id": _DEVICE_ID,
        "actor": _ACTOR,
    }
    for key, tok in tokens.items():
        var = mapping.get(key)
        if var is not None:
            with contextlib.suppress(Exception):
                var.reset(tok)


def clear_log_context() -> None:
    """Clear all correlation fields in the current execution context."""
    _CORRELATION_ID.set("")
    _TASK_ID.set("")
    _DEVICE_ID.set("")
    _ACTOR.set("")


@contextlib.contextmanager
def log_context(
    *,
    correlation_id: str | None = None,
    task_id: str | None = None,
    device_id: str | None = None,
    actor: str | None = None,
):
    """Context manager that binds correlation fields for the duration of a block."""
    tokens = set_log_context(
        correlation_id=correlation_id,
        task_id=task_id,
        device_id=device_id,
        actor=actor,
    )
    try:
        yield
    finally:
        reset_log_context(tokens)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return refuse_device_token_log(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


class JSONFormatter(logging.Formatter):
    """Emit log records as newline-delimited JSON objects with correlation context."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": refuse_device_token_log(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        cid = _CORRELATION_ID.get()
        if cid:
            log_entry["correlation_id"] = cid
        tid = _TASK_ID.get()
        if tid:
            log_entry["task_id"] = tid
        did = _DEVICE_ID.get()
        if did:
            log_entry["device_id"] = did
        actor = _ACTOR.get()
        if actor:
            log_entry["actor"] = actor

        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "value": refuse_device_token_log(str(record.exc_info[1])),
                "traceback": refuse_device_token_log(
                    "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
                ),
            }

        # Standard Python logger.info(..., extra={...}) attaches keys directly to record.__dict__.
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_ATTRS or key.startswith("_") or key == "extra":
                continue
            log_entry[key] = _scrub(value)

        # Preserve compatibility if a caller explicitly passed extra={"extra": {...}}.
        nested_extra = record.__dict__.get("extra")
        if isinstance(nested_extra, dict):
            for key, value in nested_extra.items():
                log_entry[key] = _scrub(value)

        return json.dumps(log_entry, default=str)


def configure_logging(level: str | None = None) -> None:
    """Configure structured logging for the AI Bridge control plane and agents.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). Defaults to AI_BRIDGE_LOG_LEVEL or INFO.
    """
    log_level = (level or os.environ.get("AI_BRIDGE_LOG_LEVEL") or "INFO").upper()
    use_json = os.environ.get("AI_BRIDGE_JSON_LOGS", "1").strip().lower() in ("1", "true", "yes")

    handlers: dict[str, Any] = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "formatter": "json" if use_json else "standard",
        },
    }

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": JSONFormatter,
            },
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S",
            },
        },
        "handlers": handlers,
        "root": {
            "level": log_level,
            "handlers": ["console"],
        },
        "loggers": {
            "ai_bridge": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False,
            },
            "uvicorn": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False,
            },
            "httpx": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
        },
    })

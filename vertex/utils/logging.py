"""Structured JSON-lines logging with run/job correlation. Secrets are never logged."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|bearer|authorization|password|secret)[\"'=: ]+([A-Za-z0-9._\-]{8,})"),
]

_context: dict[str, Any] = {}


def set_context(**kwargs: Any) -> None:
    _context.update({k: v for k, v in kwargs.items() if v is not None})


def clear_context(*keys: str) -> None:
    for k in keys:
        _context.pop(k, None)


def _redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact(record.getMessage()),
        }
        payload.update(_context)
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = " ".join(f"{k}={v}" for k, v in _context.items())
        base = f"{record.levelname:5} {record.name}: {_redact(record.getMessage())}"
        return f"{base} [{ctx}]" if ctx else base


def setup_logging(log_dir: Path | None = None, level: str | None = None) -> logging.Logger:
    root = logging.getLogger("vertex")
    if root.handlers:
        return root
    root.setLevel(level or os.environ.get("VERTEX_LOG_LEVEL", "INFO"))
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(HumanFormatter())
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "vertex.jsonl", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"vertex.{name}")

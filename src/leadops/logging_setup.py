"""Structured logging.

JSON by default, because these logs are meant to be queried during an incident
("show me every event where degraded=true between 09:00 and 10:00"), and grep on
prose does not answer that. `LOG_FORMAT=text` gives readable output while
developing.

The one rule enforced here: **PII does not go in log lines.** Email addresses and
phone numbers are the fields a client is legally exposed on, and a log
aggregator is the easiest place to leak them. `redact()` is applied to every
extra field, and `docs/security.md` states the policy.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# Phone matching is deliberately conservative. An earlier, looser pattern masked
# the middle of correlation ids (`cid_4155...`) as phone numbers, which destroyed
# the one field the logs exist to make searchable. Requirements now:
#   - not glued to a surrounding word character, so ids and hashes are left alone
#   - at least 9 digits, which no short numeric field reaches
#   - only characters that appear in real dialling formats
_PHONE = re.compile(r"(?<![0-9A-Za-z_])\+?[0-9][0-9\s().-]{7,}[0-9](?![0-9A-Za-z_])")


def _looks_like_phone(candidate: str) -> bool:
    return sum(character.isdigit() for character in candidate) >= 9


# Attributes the stdlib puts on every record; anything else came from `extra=`.
_RESERVED = frozenset(
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
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def redact(value: Any) -> Any:
    """Mask emails and phone numbers anywhere in a value, at any nesting depth."""
    if isinstance(value, str):
        masked = _EMAIL.sub("[email]", value)
        return _PHONE.sub(
            lambda m: "[phone]" if _looks_like_phone(m.group()) else m.group(), masked
        )
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = redact(value)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)[:2000]
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: redact(v)
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        suffix = "  " + " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        return f"{record.levelname:<7} {record.name}: {record.getMessage()}{suffix}"


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

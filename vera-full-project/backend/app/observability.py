"""Logging that is usable when something goes wrong at 2am.

Two things make the difference between a log you can act on and a wall of text:

* **A request id on every line.** It is generated (or accepted from the proxy)
  once per request, stored in a ContextVar, attached to every record logged
  while that request is in flight, and returned in the `X-Request-ID` response
  header. A customer quoting that id from an error message leads straight to
  the lines that produced it.
* **One machine-readable format.** Production emits JSON, because a log
  aggregator cannot reliably parse prose. A terminal gets the human format,
  because a person cannot comfortably read JSON.

Nothing here logs request bodies, headers or tokens: order payloads contain
personal data and Authorization headers contain credentials, and a log is the
easiest place to leak both by accident.
"""
import json
import logging
import sys
from contextvars import ContextVar
from typing import Optional

from app.config import settings

#: Set by RequestContextMiddleware; empty outside a request (worker, CLI).
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

#: Attributes LogRecord always carries — anything else is caller-supplied
#: context worth putting in the JSON payload.
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime", "message", "taskName",
}


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any extra=... fields merged in."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "env": settings.env,
        }
        if settings.GIT_SHA:
            payload["commit"] = settings.GIT_SHA
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in payload:
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    default_fmt = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"

    def __init__(self):
        super().__init__(self.default_fmt, datefmt="%H:%M:%S")


def configure_logging(level: Optional[str] = None) -> None:
    """Install one handler on the root logger, replacing uvicorn's.

    Called once at startup. Uvicorn's own loggers are left in place but have
    their handlers removed, so their records flow through this configuration
    instead of being printed twice in a different format.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else TextFormatter())
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel((level or settings.LOG_LEVEL or "INFO").upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        log.handlers = []
        log.propagate = True
    # The access line is emitted by our own middleware, with timing and the
    # request id attached, so uvicorn's duplicate is silenced.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # SQLAlchemy's pool chatter is noise unless something is being debugged.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

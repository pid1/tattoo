"""uvicorn logging in the same json shape as tattoo.log.

the app's own events were already structured, but uvicorn wrote its startup
and access lines as plain text ("INFO:     172.17.0.1:52026 - \"GET / HTTP/1.1\"
200 OK"). that left the container's stdout half-parseable, so a log viewer
could not filter the whole stream on one schema. these formatters emit the
same keys the log() helper does -- ts, level, subsystem, msg -- with the
request fields promoted so http lines are filterable by status or path.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    """server lifecycle lines (startup, shutdown, errors)."""

    subsystem = "server"

    def _base(self, record: logging.LogRecord) -> dict:
        return {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "subsystem": self.subsystem,
            "msg": record.getMessage(),
        }

    def format(self, record: logging.LogRecord) -> str:
        payload = self._base(record)
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class AccessFormatter(JsonFormatter):
    """uvicorn hands the access logger a fixed 5-tuple in record.args:
    (client_addr, method, full_path, http_version, status_code)."""

    subsystem = "http"

    def format(self, record: logging.LogRecord) -> str:
        payload = self._base(record)
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) == 5:
            client, method, path, _http_version, status = args
            payload["msg"] = f"{method} {path} {status}"
            payload["client"] = client
            payload["method"] = method
            payload["path"] = path
            try:
                payload["status"] = int(status)
            except TypeError, ValueError:
                payload["status"] = status
        return json.dumps(payload, ensure_ascii=False, default=str)


LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "tattoo.logconfig.JsonFormatter"},
        "json_access": {"()": "tattoo.logconfig.AccessFormatter"},
    },
    "handlers": {
        "default": {
            "formatter": "json",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
        "access": {
            "formatter": "json_access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}

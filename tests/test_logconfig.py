"""every line the container writes to stdout must be one json object, so a
log viewer can filter the whole stream on one schema. uvicorn's own lines
were the gap."""

import json
import logging

from tattoo.logconfig import AccessFormatter, JsonFormatter


def _record(msg="hello", level=logging.INFO, args=None, exc_info=None):
    return logging.LogRecord(
        name="uvicorn",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def test_server_line_is_json_with_shared_keys():
    payload = json.loads(JsonFormatter().format(_record("Application startup complete.")))
    assert payload["subsystem"] == "server"
    assert payload["level"] == "info"
    assert payload["msg"] == "Application startup complete."
    assert payload["ts"].endswith("+00:00")


def test_access_line_promotes_request_fields():
    rec = _record(
        msg='%s - "%s %s HTTP/%s" %d',
        args=("172.17.0.1:52026", "GET", "/dashboard/", "1.1", 200),
    )
    payload = json.loads(AccessFormatter().format(rec))
    assert payload["subsystem"] == "http"
    assert payload["method"] == "GET"
    assert payload["path"] == "/dashboard/"
    assert payload["status"] == 200
    assert payload["msg"] == "GET /dashboard/ 200"


def test_access_formatter_tolerates_unexpected_args():
    """never let a logging shape change take the server down."""
    payload = json.loads(AccessFormatter().format(_record("odd line", args=None)))
    assert payload["msg"] == "odd line"
    assert "status" not in payload


def test_exception_lines_carry_error_type():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        payload = json.loads(
            JsonFormatter().format(_record("failed", level=logging.ERROR, exc_info=sys.exc_info()))
        )
    assert payload["level"] == "error"
    assert payload["error_type"] == "RuntimeError"
    assert "boom" in payload["traceback"]

"""One-JSON-object-per-line logging to stdout.

JSON lines are what CloudWatch Logs Insights (Round 2) and `jq` (locally) query natively,
so every log line is machine-readable. Call events never include borrower data: only
event_id, outcome, retry and latency fields.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        else:
            payload["logger"] = record.name
            payload["msg"] = record.getMessage()
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Route uvicorn's server/error logs through the same JSON handler. uvicorn.access is
    # left alone so --no-access-log keeps working (the "call" line already covers each call).
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # httpx logs every request at INFO: one unstructured line per pipeline attempt, on the
    # hot path. Attempt failures are already counted in metrics and summarised per call.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event, e.g. log_event(log, "breaker_transition", from_="closed")."""
    logger.log(level, event, extra={"fields": {"event": event, **fields}})

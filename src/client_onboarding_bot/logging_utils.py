"""Structured logging utilities supporting human-readable console and JSONL file output."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """Format log records as single-line structured JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra_fields = getattr(record, "structured", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Clean human-readable console formatter."""

    def format(self, record: logging.LogRecord) -> str:
        level_tag = f"[{record.levelname}]"
        return f"{level_tag:<10} {record.getMessage()}"


def configure_logging(
    *,
    log_dir: Path | None = None,
    level: str = "INFO",
    json_logs: bool = False,
    run_id: str | None = None,
) -> logging.Logger:
    """Configure application logger with stream and optional persistent JSONL file sinks."""
    logger = logging.getLogger("client_onboarding_bot")
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)
    logger.propagate = False

    # Clear existing handlers cleanly so repeated CLI/pipeline runs never duplicate output.
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(numeric_level)
    stream_handler.setFormatter(JsonFormatter() if json_logs else ConsoleFormatter())
    logger.addHandler(stream_handler)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        suffix = run_id or datetime.now(timezone.utc).strftime("%Y%m%d")
        file_handler = logging.FileHandler(log_dir / f"onboarding_{suffix}.jsonl", encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    return logger

"""Structured JSON-lines logging with daily rotation and run_id context."""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Context variable to hold active run_id
_RUN_ID: ContextVar[str] = ContextVar("run_id", default="system")


def set_run_id(run_id: str) -> None:
    _RUN_ID.set(run_id)


def get_run_id() -> str:
    return _RUN_ID.get()


class JSONLinesHandler(logging.Handler):
    """Handler that writes structured JSON lines to daily log files."""

    def __init__(self, logs_dir: str | Path) -> None:
        super().__init__()
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._current_date: str | None = None
        self._file_handle: Any = None

    def _get_handle(self, now: datetime) -> Any:
        date_str = now.strftime("%Y-%m-%d")
        if self._file_handle is None or self._current_date != date_str:
            if self._file_handle is not None:
                self._file_handle.close()
            self._current_date = date_str
            file_path = self.logs_dir / f"ts-{date_str}.jsonl"
            self._file_handle = open(file_path, "a", encoding="utf-8")
        return self._file_handle

    def emit(self, record: logging.LogRecord) -> None:
        try:
            now = datetime.now(UTC)
            handle = self._get_handle(now)
            log_entry = {
                "ts_utc": now.isoformat(),
                "run_id": getattr(record, "run_id", get_run_id()),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "file": record.filename,
                "line": record.lineno,
            }
            if record.exc_info:
                log_entry["exception"] = self.formatException(record.exc_info)
            if hasattr(record, "extra_data"):
                log_entry["data"] = record.extra_data

            line = json.dumps(log_entry, ensure_ascii=False)
            handle.write(line + "\n")
            handle.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._file_handle is not None:
            self._file_handle.close()
            self._file_handle = None
        super().close()


def setup_logging(
    logs_dir: str | Path = "logs",
    level: str = "INFO",
    json_lines: bool = True,
    console: bool = True,
) -> None:
    """Configure system logging."""
    root_logger = logging.getLogger("ts")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    root_logger.handlers.clear()

    if json_lines:
        json_handler = JSONLinesHandler(logs_dir)
        json_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        root_logger.addHandler(json_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)


def get_logger(name: str = "ts") -> logging.Logger:
    """Obtain a namespaced logger."""
    return logging.getLogger(f"ts.{name}" if not name.startswith("ts") else name)

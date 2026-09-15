"""
Telemetry and Structured Logging module for flam.

Features:
- File-based logging with automatic log rotation (logs/flam.log, max 10MB x 5 backups).
- Structured event logging (logs/events.jsonl) for application metrics, form fills,
  and Groq token usage tracking.
- Query utilities for in-bot /logs inspections.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("flam.telemetry")

_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
_EVENT_LOG_FILE = _LOGS_DIR / "events.jsonl"
_MAIN_LOG_FILE = _LOGS_DIR / "flam.log"


def setup_telemetry(log_dir: Optional[Path] = None, log_level: int = logging.INFO) -> None:
    """
    Initialize directory and handlers for application logging and event telemetry.
    """
    global _LOGS_DIR, _EVENT_LOG_FILE, _MAIN_LOG_FILE
    target_dir = log_dir or _LOGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    _LOGS_DIR = target_dir
    _EVENT_LOG_FILE = _LOGS_DIR / "events.jsonl"
    _MAIN_LOG_FILE = _LOGS_DIR / "flam.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Format for file logs
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s"
    )

    # Check if RotatingFileHandler already attached
    has_file_handler = any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers)
    if not has_file_handler:
        file_handler = RotatingFileHandler(
            _MAIN_LOG_FILE,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)
        root_logger.addHandler(file_handler)

    logger.info("Telemetry and file logging initialized at %s", _LOGS_DIR)


def log_event(event_type: str, data: Optional[dict[str, Any]] = None, level: str = "INFO") -> None:
    """
    Record a structured event into events.jsonl.
    """
    record = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event": event_type,
        "data": data or {},
    }

    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(_EVENT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning("Failed to write telemetry event: %s", exc)

    # Also log to standard logger
    log_fn = getattr(logger, level.lower(), logger.info)
    log_fn("[EVENT:%s] %s", event_type, json.dumps(data or {}))


def get_recent_logs(lines: int = 25) -> list[str]:
    """
    Read the last N lines from flam.log for in-bot debugging.
    """
    if not _MAIN_LOG_FILE.exists():
        return ["No logs recorded yet."]

    try:
        with open(_MAIN_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return [l.rstrip() for l in all_lines[-lines:]]
    except Exception as exc:
        return [f"Error reading logs: {exc}"]


def get_telemetry_summary() -> dict[str, Any]:
    """
    Compute basic metrics from events.jsonl.
    """
    if not _EVENT_LOG_FILE.exists():
        return {"events_count": 0, "breakdown": {}}

    count = 0
    breakdown: dict[str, int] = {}
    try:
        with open(_EVENT_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ev = obj.get("event", "unknown")
                    breakdown[ev] = breakdown.get(ev, 0) + 1
                    count += 1
                except Exception:
                    pass
    except Exception:
        pass

    return {
        "events_count": count,
        "breakdown": breakdown,
    }

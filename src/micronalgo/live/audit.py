"""Structured audit log.

Every decision, order and fill is appended as one JSON object per line, with the
*reason* attached. When something goes wrong at 04:00 the only thing that helps
is a file that says what the process believed and why it acted -- not a stack
trace, and not a human-prose log line that omits the numbers.

The log is append-only and never rewritten, so it is also the reconciliation
source of last resort if the state file is lost.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ..calendar_nyse import NY

_LOGGER_NAME = "micronalgo"


def setup_logging(level: str = "INFO", log_dir: Path | str | None = None) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(Path(log_dir) / "micronalgo.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    logger.propagate = False
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)


class AuditLog:
    """Append-only JSONL event log."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> dict:
        record = {
            "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "ts_ny": dt.datetime.now(NY).isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return record

    def read(self, *, event: str | None = None, limit: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event is None or rec.get("event") == event:
                    out.append(rec)
        return out[-limit:] if limit else out

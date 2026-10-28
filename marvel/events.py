"""Allowlisted JSON events. Raw config, argv, exception text and secrets are not logged."""

from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .clock import stamp, utc_now
from .errors import ERROR_CODES, LogError

COMMANDS = frozenset({"validate", "init", "status", "pause", "migrate", "data", "risk", "paper", "research"})
EVENTS = frozenset({"command_started", "command_completed", "command_failed"})


class _StrictHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        raise LogError("LOG_IO", "Structured log write failed; review local storage before continuing.")


class EventLog:
    def __init__(self, path: Path, clock: Callable[[], datetime] = utc_now, *, max_bytes: int = 1_048_576):
        self.clock = clock
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handler = _StrictHandler(path, maxBytes=max_bytes, backupCount=3, encoding="utf-8")
            self.handler.setFormatter(logging.Formatter("%(message)s"))
        except OSError as exc:
            raise LogError("LOG_IO", "Cannot open the configured local log; no operation was started.") from exc

    def emit(self, event: str, command: str, *, code: str = "OK") -> None:
        # Do not accept arbitrary fields or caller-controlled error messages.
        if event not in EVENTS or command not in COMMANDS or code not in ERROR_CODES | {"OK"}:
            raise LogError("LOG_INVALID", "An unsupported structured log event was rejected.")
        payload = {"timestamp_utc": stamp(self.clock()), "level": "ERROR" if event == "command_failed" else "INFO", "event": event, "command": command, "code": code}
        record = logging.LogRecord("marvel", logging.INFO, "", 0, json.dumps(payload, separators=(",", ":")), (), None)
        try:
            self.handler.handle(record)
            self.handler.flush()
        except OSError as exc:
            raise LogError("LOG_IO", "Structured log write failed; review local storage before continuing.") from exc

    def close(self) -> None:
        self.handler.close()

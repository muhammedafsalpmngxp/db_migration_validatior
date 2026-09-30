"""What a check is doing, in words people can follow.

Every step - which table, what is being done to it, how long each server took, the result
- is written three ways:
  - to the backend console (the terminal running uvicorn),
  - to ATNM_LOG_FILE (default backend/.cache/atnm.log, rotated at 5 MB),
  - to the run's activity list, which the UI reads through /api/atnm/check/status.
"""
import itertools
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from . import settings

logger = logging.getLogger("atnm")
if not logger.handlers:     # once per process, however often the module is imported
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _fmt = logging.Formatter("%(asctime)s  ATNM  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    _console = logging.StreamHandler()
    _console.setFormatter(_fmt)
    logger.addHandler(_console)
    try:
        settings.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _file = RotatingFileHandler(settings.LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        _file.setFormatter(_fmt)
        logger.addHandler(_file)
    except OSError as exc:
        logger.warning("cannot write the log file %s: %s", settings.LOG_FILE, exc)

LEVELS = {"info": logging.INFO, "ok": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Activity:
    """The activity list of the current (or last) run: the most recent entries, and the
    step in progress."""

    def __init__(self, keep=1000):
        self.lock = threading.Lock()
        self.entries = deque(maxlen=keep)
        self.seq = itertools.count(1)
        self.last = 0
        self.step = None
        self.step_started_at = None

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.step = None
            self.step_started_at = None

    def add(self, text, level="info", table=None, database=None, step=False):
        """One entry. `step=True` also makes it the step now in progress."""
        entry = {"at": _now(), "level": level, "database": database, "table": table, "text": text}
        with self.lock:
            entry["seq"] = self.last = next(self.seq)
            self.entries.append(entry)
            if step:
                self.step, self.step_started_at = text, entry["at"]
        where = " / ".join(x for x in (database, table) if x)
        logger.log(LEVELS.get(level, logging.INFO), "%s%s", f"[{where}] " if where else "", text)
        return entry

    def since(self, seq):
        with self.lock:
            return [e for e in self.entries if e["seq"] > seq]

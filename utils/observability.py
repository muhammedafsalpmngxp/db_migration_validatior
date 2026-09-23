"""Logging, exception capture and LLM usage accounting for the whole project.

Two pieces, both process wide and thread safe:

* ``setup_logging()`` wires a console handler plus a rotating file handler at
  ``logs/dbcompare.log``. Safe to call many times (Streamlit reruns the script).
* ``LLM_STATS`` counts every call made through ``utils.call_llm.call_llm``:
  number of calls, prompt/completion/total tokens, wall time, and failures.

Nothing here raises: observability must never break a run.
"""
import logging
import logging.handlers
import os
import threading
import time
from collections import deque

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "dbcompare.log")

_setup_lock = threading.Lock()
_configured = False


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted records in memory so the UI can display them."""

    def __init__(self, capacity=2000):
        super().__init__()
        self.records = deque(maxlen=capacity)

    def emit(self, record):
        try:
            self.records.append(
                {
                    "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "formatted": self.format(record),
                }
            )
        except Exception:  # never let logging break the run
            pass

    def snapshot(self):
        return list(self.records)

    def clear(self):
        self.records.clear()


BUFFER = RingBufferHandler()


def _drop_closed_socket(record):
    """Silence Windows' websocket teardown noise.

    Closing or refreshing the Streamlit tab makes asyncio log a ConnectionResetError from
    `_ProactorBasePipeTransport._call_connection_lost` at ERROR level. It says nothing
    about the run, and at ERROR level it reads like a failure - so it is dropped, while
    every other asyncio error still comes through.
    """
    text = record.getMessage()
    return not ("_call_connection_lost" in text or "WinError 10054" in text)


def setup_logging(level=None):
    """Idempotently configure the root logger. Returns the in-memory buffer handler."""
    global _configured
    with _setup_lock:
        if _configured:
            return BUFFER
        level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        root = logging.getLogger()
        root.setLevel(getattr(logging, level, logging.INFO))

        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            fileh = logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            fileh.setFormatter(fmt)
            root.addHandler(fileh)
        except OSError as exc:  # read only fs, locked file on Windows, ...
            root.warning("File logging disabled: %s", exc)

        BUFFER.setFormatter(fmt)
        root.addHandler(BUFFER)

        # Third party noise
        for noisy in ("httpx", "httpcore", "openai", "anthropic", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        logging.getLogger("asyncio").addFilter(_drop_closed_socket)

        _configured = True
        return BUFFER


def get_logger(name):
    setup_logging()
    return logging.getLogger(name)


# ---------------------------------------------------------------- LLM usage


# How much of each prompt/response to keep in memory for the UI detail pane.
# LLM_TRACE_CHARS=0 turns the capture off entirely.
TRACE_CHARS = int(os.getenv("LLM_TRACE_CHARS", "20000"))


def _clip(text):
    text = text or ""
    if TRACE_CHARS <= 0:
        return ""
    if len(text) <= TRACE_CHARS:
        return text
    return text[:TRACE_CHARS] + f"\n... [{len(text) - TRACE_CHARS} more characters]"


def estimate_tokens(text):
    """Rough fallback when a provider returns no usage block (~4 chars per token)."""
    return max(1, len(text or "") // 4)


class LLMStats:
    """Counters for every LLM call in the process."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.calls = 0
            self.failures = 0
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.total_tokens = 0
            self.cached_tokens = 0
            self.seconds = 0.0
            self.estimated = False
            self.records = []

    def record(self, provider, model, prompt_tokens, completion_tokens, seconds,
               estimated=False, error=None, label=None, prompt="", response="",
               cached=0):
        with self._lock:
            self.calls += 1
            if error:
                self.failures += 1
            self.prompt_tokens += prompt_tokens or 0
            self.completion_tokens += completion_tokens or 0
            self.total_tokens += (prompt_tokens or 0) + (completion_tokens or 0)
            self.cached_tokens += cached or 0
            self.seconds += seconds or 0.0
            self.estimated = self.estimated or estimated
            self.records.append(
                {
                    "n": self.calls,
                    "label": label or "",
                    "provider": provider,
                    "model": model,
                    "prompt_tokens": prompt_tokens or 0,
                    "completion_tokens": completion_tokens or 0,
                    "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
                    "cached_tokens": cached or 0,
                    "seconds": round(seconds or 0.0, 2),
                    "estimated": estimated,
                    "error": str(error) if error else "",
                    "prompt": _clip(prompt),
                    "response": _clip(response),
                }
            )

    def summary(self):
        with self._lock:
            return {
                "calls": self.calls,
                "failures": self.failures,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "cached_tokens": self.cached_tokens,
                "seconds": round(self.seconds, 2),
                "estimated": self.estimated,
                "calls_detail": list(self.records),
            }


LLM_STATS = LLMStats()

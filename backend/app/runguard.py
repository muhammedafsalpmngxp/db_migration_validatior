"""One heavy step at a time on the shared database servers.

Every background run - the ATNM copy check, "Re-run all data checks" (data check, key
mapping, renames, target table checks) and the rename check - takes this slot for one
table or mapping at a time and gives it back after. Two runs started together therefore
take turns item by item instead of loading the servers twice; neither waits for the other
to finish completely, so they can never wait on each other forever.

RUN_PAUSE_SECONDS (backend/.env) leaves a gap between two heavy steps, whoever runs them,
so other users of the servers get through too. 0 = no gap.

The slot is re-entrant: a step that calls another guarded step in the same thread (the
data check run calling the rename check) keeps it.
"""
import threading
import time
from contextlib import contextmanager

from . import config

PAUSE = max(0.0, float(config._env("RUN_PAUSE_SECONDS", "0")))

_lock = threading.RLock()
_state = {"holder": None, "depth": 0, "free_at": 0.0}
_state_lock = threading.Lock()


class Cancelled(Exception):
    """The run was cancelled while it waited for its turn."""


def holder():
    """What holds the slot now (a short label), or None."""
    return _state["holder"]


@contextmanager
def slot(label, cancelled=lambda: False, on_wait=None):
    """Hold the slot while the block runs. Waits for its turn (and for the pause after the
    previous step); `on_wait(holder)` is called once if it has to wait; raises Cancelled
    when `cancelled()` turns true while waiting."""
    waited = False
    while True:
        if _lock.acquire(timeout=0.5):
            if _state["depth"] > 0:          # re-entered by the thread that holds it
                break
            gap = _state["free_at"] - time.time()
            if gap <= 0:
                break
            _lock.release()
            time.sleep(min(gap, 0.5))
        if cancelled():
            raise Cancelled("Cancelled.")
        if not waited and on_wait and _state["holder"]:
            waited = True
            on_wait(_state["holder"])
    with _state_lock:
        outer = _state["depth"] == 0
        _state["depth"] += 1
        if outer:
            _state["holder"] = label
    try:
        yield
    finally:
        with _state_lock:
            _state["depth"] -= 1
            if _state["depth"] == 0:
                _state["holder"] = None
                _state["free_at"] = time.time() + PAUSE
        _lock.release()

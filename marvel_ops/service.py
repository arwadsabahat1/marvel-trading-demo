"""Supervised worker: bounded backoff, graceful shutdown, heartbeat and no entry retries."""

from threading import Event
import signal

from .common import Stop, utc_now


class Worker:
    def __init__(self, store, tick, *, stop=None, now=utc_now):
        self.store, self.tick, self.stop, self.now = store, tick, stop or Event(), now

    def run(self, *, max_ticks=None, interval=20):
        if max_ticks is not None and (type(max_ticks) is not int or max_ticks < 1):
            raise Stop("OPS_WORKER_ARGUMENTS_INVALID")
        if not 1 <= interval <= 60:
            raise Stop("OPS_WORKER_ARGUMENTS_INVALID")
        failures, ticks = 0, 0
        with self.store.lock("service.lock"):
            try:
                while not self.stop.is_set() and (max_ticks is None or ticks < max_ticks):
                    try:
                        self.tick()
                        failures = 0
                        self.store.heartbeat("running", self.now())
                    except Exception:
                        failures += 1
                        self.store.heartbeat("degraded", self.now(), failures=failures)
                        if failures >= 3:
                            with self.store.lock():
                                state = self.store.load()
                                if "OPS_REPEATED_WORKER_FAILURE" not in state["halts"]:
                                    state["halts"].append("OPS_REPEATED_WORKER_FAILURE")
                                    self.store.save(state, "worker_review_halt")
                        # Next tick reconciles first. Failed entries are never retried here.
                    ticks += 1
                    if max_ticks is None or ticks < max_ticks:
                        self.stop.wait(min(60, interval * 2 ** min(failures, 3)))
            finally:
                self.store.heartbeat("stopped", self.now(), failures=failures)
        return {"ticks": ticks, "consecutive_failures": failures, "stopped": True}

    def install_signals(self):
        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: self.stop.set())

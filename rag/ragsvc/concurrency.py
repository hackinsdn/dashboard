# -*- encoding: utf-8 -*-
"""Admission control for generation.

On a CPU-only host two concurrent generations roughly double both latencies, so
requests are serialized (``max_concurrency=1`` by default) behind a *bounded*
queue. Overflow is rejected immediately rather than queued: the dashboard's
fallback ("I'm busy, open a support case") is a far better experience than
waiting four generations deep for an answer that may not come.

The wait for a slot is **also** bounded (``wait_timeout_s``). This is essential,
not a nicety: an unbounded wait lets a request whose HTTP client has already
timed out sit in the queue forever, holding its place. On a slow box, where a
generation can outlast the client timeout, retries then accumulate until the
queue is permanently full and *every* later request -- even from a lone user,
hours later -- is rejected as "busy" until the process restarts. A bounded wait
makes those abandoned requests evict themselves, so the queue self-heals.

Retrieval and embedding run outside the gate -- they are cheap and
parallelizable, and doing them first means a queued request is already retrieved
by the time the slot frees up.
"""
import threading
from contextlib import contextmanager

# Fallback bound used when a caller forgets to pass one. Deliberately finite:
# ``None`` (wait forever) is the one value that must never reach the semaphore.
DEFAULT_WAIT_TIMEOUT_S = 30.0


class QueueFull(Exception):
    """Raised when admitting the request would exceed the bounded queue."""


class GenerationGate:
    def __init__(self, max_concurrency=1, queue_max=4, wait_timeout_s=DEFAULT_WAIT_TIMEOUT_S):
        self.max_concurrency = max(1, max_concurrency)
        self.queue_max = max(0, queue_max)
        # Guard against an explicit None (wait forever), which pins the queue.
        self.wait_timeout_s = DEFAULT_WAIT_TIMEOUT_S if wait_timeout_s is None else wait_timeout_s
        self._slots = threading.Semaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._inflight = 0  # admitted: waiting + running
        self._running = 0
        self.rejected = 0
        self.peak_inflight = 0

    @property
    def capacity(self):
        return self.max_concurrency + self.queue_max

    @contextmanager
    def slot(self):
        with self._lock:
            if self._inflight >= self.capacity:
                self.rejected += 1
                raise QueueFull(
                    f"generation queue full ({self._inflight}/{self.capacity})"
                )
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
        acquired = False
        try:
            acquired = self._slots.acquire(timeout=self.wait_timeout_s)
            if not acquired:
                with self._lock:
                    self.rejected += 1
                raise QueueFull("timed out waiting for a generation slot")
            with self._lock:
                self._running += 1
            yield
        finally:
            with self._lock:
                self._inflight -= 1
                if acquired:
                    self._running -= 1
            if acquired:
                self._slots.release()

    def stats(self):
        with self._lock:
            return {
                "max_concurrency": self.max_concurrency,
                "queue_max": self.queue_max,
                "inflight": self._inflight,
                "running": self._running,
                "queued": max(0, self._inflight - self._running),
                "peak_inflight": self.peak_inflight,
                "rejected": self.rejected,
            }

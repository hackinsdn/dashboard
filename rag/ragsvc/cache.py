# -*- encoding: utf-8 -*-
"""Answer and embedding caches.

On a CPU-only host a cache hit is the difference between 15 seconds and 15
milliseconds, so this is a correctness-adjacent feature, not an optimization.

The answer key includes the index version, so re-ingesting the corpus
invalidates every cached answer without a manual flush -- a stale answer citing
a document that no longer says that is the failure mode worth designing out.
"""
import hashlib
import re
import threading
import time
import unicodedata

_WS_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[\s?!.,;:]+$")


def normalize_question(text):
    """Fold the variations that should share a cache entry (case, spacing, '?')."""
    text = unicodedata.normalize("NFKC", text or "").strip().lower()
    text = _WS_RE.sub(" ", text)
    return _TRAILING_PUNCT_RE.sub("", text)


def question_hash(text):
    return hashlib.sha256(normalize_question(text).encode("utf-8")).hexdigest()


class TTLCache:
    """Small thread-safe LRU with a TTL. Bounded, so it cannot leak."""

    def __init__(self, max_entries=512, ttl_s=86400, clock=time.time):
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._data = {}  # key -> (expires_at, value)
        self._order = []  # LRU, oldest first
        self.hits = 0
        self.misses = 0

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if expires_at is not None and expires_at <= self._clock():
                self._drop(key)
                self.misses += 1
                return None
            self._touch(key)
            self.hits += 1
            return value

    def set(self, key, value):
        with self._lock:
            if key in self._data:
                self._order.remove(key)
            expires_at = self._clock() + self.ttl_s if self.ttl_s else None
            self._data[key] = (expires_at, value)
            self._order.append(key)
            while len(self._order) > self.max_entries:
                self._drop(self._order[0])

    def clear(self):
        with self._lock:
            self._data.clear()
            self._order.clear()

    def _drop(self, key):
        self._data.pop(key, None)
        if key in self._order:
            self._order.remove(key)

    def _touch(self, key):
        if key in self._order:
            self._order.remove(key)
            self._order.append(key)

    @property
    def hit_ratio(self):
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def stats(self):
        return {
            "entries": len(self._data),
            "max_entries": self.max_entries,
            "hits": self.hits,
            "misses": self.misses,
            "hit_ratio": self.hit_ratio,
        }


def answer_key(question, locale, index_version):
    raw = f"{normalize_question(question)}|{locale}|{index_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

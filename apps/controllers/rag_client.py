# -*- encoding: utf-8 -*-
"""HTTP client for the hisdn-rag assistant service.

The dashboard's only dependency on the assistant is this module: it never
imports a model library, never holds an index and never blocks on one. Every
failure mode -- saturated, slow, down, misconfigured -- collapses into an
``unavailable`` result, so the request path can always fall back to the human
support flow.

A circuit breaker keeps the widget snappy while the service is down: after a few
consecutive failures the dashboard stops trying (and the widget stops offering
the assistant) until a reset window has passed.

See doc/rag-assistant-design.md.
"""
import hashlib
import logging
import threading
import time

import requests
from flask import current_app

log = logging.getLogger(__name__)


class Result:
    """Outcome of an /v1/answer call. Never an exception, always one of three."""

    def __init__(self, status, answer=None, sources=None, reason=None, usage=None, cached=False):
        self.status = status  # answered | refused | unavailable
        self.answer = answer
        self.sources = sources or []
        self.reason = reason
        self.usage = usage or {}
        self.cached = cached

    @property
    def answered(self):
        return self.status == "answered"

    def __repr__(self):
        return f"<rag.Result {self.status} reason={self.reason}>"


class CircuitBreaker:
    """Open after N consecutive failures; half-open one probe after the reset."""

    def __init__(self, failures=3, reset_s=60):
        self.threshold = max(1, failures)
        self.reset_s = reset_s
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = None

    @property
    def is_open(self):
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.reset_s:
                # half-open: let one request through to probe the service
                self._opened_at = None
                self._failures = self.threshold - 1
                return False
            return True

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                log.warning("rag: circuit breaker opened after %s failures", self._failures)

    def state(self):
        with self._lock:
            return {
                "open": self._opened_at is not None,
                "failures": self._failures,
                "threshold": self.threshold,
            }


_breaker = None
_breaker_lock = threading.Lock()


def get_breaker():
    global _breaker
    with _breaker_lock:
        if _breaker is None:
            _breaker = CircuitBreaker(
                failures=current_app.config.get("RAG_BREAKER_FAILURES", 3),
                reset_s=current_app.config.get("RAG_BREAKER_RESET_S", 60),
            )
        return _breaker


def reset_breaker():
    """Test hook: drop the process-wide breaker."""
    global _breaker
    with _breaker_lock:
        _breaker = None


def is_configured():
    return bool(
        current_app.config.get("RAG_ENABLED") and current_app.config.get("RAG_SERVICE_URL")
    )


def is_available():
    """Enabled, configured, and the breaker is not open."""
    return is_configured() and not get_breaker().is_open


def conversation_hash(thread_id):
    """Pseudonymous, stable per thread. The service never learns who the user is."""
    salt = current_app.config.get("SECRET_KEY", "")
    digest = hashlib.sha256(f"{salt}:{thread_id}".encode("utf-8")).hexdigest()
    return f"sha256:{digest[:32]}"


def _request(method, path, payload=None, timeout=None):
    base = (current_app.config.get("RAG_SERVICE_URL") or "").rstrip("/")
    token = current_app.config.get("RAG_SERVICE_TOKEN") or ""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(
        method,
        f"{base}{path}",
        json=payload,
        headers=headers,
        timeout=timeout or current_app.config.get("RAG_TIMEOUT_S", 45),
    )


def answer(question, locale="en", history=None, thread_id=None):
    """Ask the assistant. Returns a Result -- never raises."""
    if not is_configured():
        return Result("unavailable", reason="disabled")
    breaker = get_breaker()
    if breaker.is_open:
        return Result("unavailable", reason="circuit_open")

    payload = {
        "question": question,
        "locale": locale,
        "history": history or [],
        "conversation_id": conversation_hash(thread_id) if thread_id else None,
    }
    try:
        response = _request("POST", "/v1/answer", payload)
    except requests.RequestException as exc:
        breaker.record_failure()
        log.warning("rag: /v1/answer failed: %s", exc)
        reason = "timeout" if isinstance(exc, requests.Timeout) else "connection_error"
        return Result("unavailable", reason=reason)

    if response.status_code == 503:
        # Saturated, not broken: the service is answering correctly, so this
        # does not count toward opening the breaker.
        return Result("unavailable", reason="queue_full")
    if response.status_code >= 400:
        breaker.record_failure()
        log.warning("rag: /v1/answer returned %s", response.status_code)
        return Result("unavailable", reason=f"http_{response.status_code}")

    breaker.record_success()
    try:
        data = response.json()
    except ValueError:
        return Result("unavailable", reason="bad_response")

    status = data.get("status")
    if status == "answered":
        return Result(
            "answered",
            answer=data.get("answer"),
            sources=data.get("sources") or [],
            usage=data.get("usage") or {},
            cached=bool(data.get("cached")),
        )
    if status == "refused":
        return Result("refused", reason=data.get("reason"), usage=data.get("usage") or {})
    return Result("unavailable", reason=data.get("reason") or "unknown_status")


def health():
    """Service health, for the CLI and the admin panel. Never raises."""
    if not is_configured():
        return {"status": "disabled"}
    try:
        response = _request("GET", "/healthz", timeout=5)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        return {"status": "unreachable", "error": str(exc)}


def stats():
    """Service counters, for the admin panel. Never raises."""
    if not is_configured():
        return {"status": "disabled"}
    try:
        response = _request("GET", "/v1/stats", timeout=10)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        return {"status": "unreachable", "error": str(exc)}


def ingest(documents, prune=None, timeout=None):
    """Upsert documents. Raises requests.RequestException -- the CLI reports it."""
    payload = {"documents": documents}
    if prune:
        payload["prune"] = prune
    response = _request(
        "POST",
        "/v1/ingest",
        payload,
        timeout=timeout or current_app.config.get("RAG_INGEST_TIMEOUT_S", 300),
    )
    response.raise_for_status()
    return response.json()

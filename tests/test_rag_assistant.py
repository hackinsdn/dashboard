"""Pytest suite for the RAG assistant on top of the support chat.

Everything here runs without the hisdn-rag service and without model weights:
``apps.controllers.rag_client.answer`` is faked, which is the whole point of
keeping the dashboard a thin client. See doc/rag-assistant-design.md.

Covers mode routing (an assistant thread is not staff work), the three answer
outcomes (answered / refused / unavailable) and the fact that the user's message
survives all three, localization of the refusal text, escalation and its
telemetry snapshot, answer feedback, the circuit breaker, the per-user rate
limit, RAG_STORE_TRANSCRIPTS=False, and the ingestion collectors.

Runs entirely against a throwaway temporary SQLite database - it never touches
the real dev/production database.

Usage:
    pytest tests/test_rag_assistant.py -v

Note: the classes below are ordered, stateful workflows - pytest runs test
methods within a class in definition order. Do not run with pytest-randomly
without disabling it for this file.
"""
import json
import os
import sys
import tempfile
import types
from datetime import timedelta

import pytest
from flask_babel import refresh

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# --- isolate the test run from any real data --------------------------------
TEST_DATA_DIR = tempfile.mkdtemp(prefix="hackinsdn_rag_test_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("OPTIONAL_MODULES", "")

# Stub the clabernetes controller (hard-requires a binary at import time).
_fake_clabernetes = types.ModuleType("apps.controllers.clabernetes")


class _StubC9sController:
    def __getattr__(self, name):
        raise NotImplementedError("clabernetes stub - not needed for these tests")


_fake_clabernetes.C9sController = _StubC9sController
sys.modules["apps.controllers.clabernetes"] = _fake_clabernetes

from run import app as flask_app  # noqa: E402
from apps import db  # noqa: E402
from apps.audit_mixin import utcnow  # noqa: E402
from apps.authentication.models import Users  # noqa: E402
from apps.home.models import SupportThreads, SupportMessages  # noqa: E402
from apps.controllers import rag_client, support  # noqa: E402

flask_app.config["TESTING"] = True
flask_app.config["WTF_CSRF_ENABLED"] = False


# --- fixtures ----------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_data_dir():
    yield
    import shutil

    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def app():
    with flask_app.app_context():
        db.create_all()
        flask_app.config["RAG_ENABLED"] = True
        flask_app.config["RAG_SERVICE_URL"] = "http://rag.test.local:8080"
        flask_app.config["RAG_SERVICE_TOKEN"] = "test-token"
        # The per-user budget is a real limit over a real time window, and every
        # test in this file adds to Alice's usage; the limit itself is exercised
        # on its own, with its own user.
        flask_app.config["RAG_USER_RATE_LIMIT"] = 1000
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def ids(app):
    admin = Users(username="rag_admin", password="admin123", email="rag_admin@test.local", category="admin")
    alice = Users(username="rag_alice", password="alice123", email="rag_alice@test.local", category="student")
    bob = Users(username="rag_bob", password="bob123", email="rag_bob@test.local", category="student")
    db.session.add_all([admin, alice, bob])
    db.session.commit()
    return {"admin_id": admin.id, "alice_id": alice.id, "bob_id": bob.id}


@pytest.fixture(autouse=True)
def _reset_breaker():
    rag_client.reset_breaker()
    yield
    rag_client.reset_breaker()


@pytest.fixture(autouse=True)
def _as_alice(client, ids):
    """Every test starts logged in as Alice with no conversation in flight.

    The mode of a thread is decided when it is created, so a leftover open
    thread from a previous test would silently answer the next one in the wrong
    mode. Finishing first makes each test independent of the ones before it.
    """
    logout(client)
    assert login(client, "rag_alice", "alice123")
    post_json(client, "/api/support/thread/finish")
    yield


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    resp = client.post(
        "/login/", data={"identifier": username, "password": password, "login": "1"}
    )
    return resp.status_code == 302


def logout(client):
    client.get("/logout")


def post_json(client, url, payload=None):
    return client.post(
        url, data=json.dumps(payload or {}), content_type="application/json"
    )


def fake_answer(monkeypatch, **kwargs):
    """Replace the HTTP client with a canned Result and record the calls."""
    calls = []

    def _answer(question, locale="en", history=None, thread_id=None):
        calls.append(
            {"question": question, "locale": locale, "history": history, "thread_id": thread_id}
        )
        return rag_client.Result(**kwargs)

    monkeypatch.setattr(rag_client, "answer", _answer)
    return calls


ANSWERED = {
    "status": "answered",
    "answer": "Press the Extend button [1].",
    "sources": [{"index": 1, "title": "Labs > Extending a lab", "url": "/doc/DEV.md", "score": 0.8}],
    "usage": {"latency_ms": 9400},
}


class _Response:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


def start_thread(client, body="how do I extend a lab?", mode="assistant"):
    resp = post_json(
        client, "/api/support/thread/messages", {"body": body, "mode": mode, "page": "/labs"}
    )
    assert resp.status_code == 201
    return resp.get_json()["thread_id"]


def finish_open_thread(client):
    post_json(client, "/api/support/thread/finish")


# --- mode routing ------------------------------------------------------
class TestModeRouting:
    def test_status_reports_enabled_and_available(self, client, ids):
        logout(client)
        assert login(client, "rag_alice", "alice123")
        data = client.get("/api/support/assistant/status").get_json()
        assert data["enabled"] is True
        assert data["available"] is True
        assert data["feedback"] is True

    def test_new_thread_defaults_to_support(self, client, ids):
        thread_id = start_thread(client, "I need help with my account", mode="support")
        thread = db.session.get(SupportThreads, thread_id)
        assert thread.mode == "support"
        assert thread.locale  # the conversation records the language it runs in

    def test_assistant_thread_is_not_staff_work(self, client, ids):
        before = support.open_thread_count()
        thread_id = start_thread(client)
        assert db.session.get(SupportThreads, thread_id).mode == "assistant"
        # the sidebar badge counts cases awaiting staff, and this is not one
        assert support.open_thread_count() == before

    def test_mode_endpoint_switches_an_existing_thread(self, client, ids):
        thread_id = start_thread(client, "hello", mode="support")
        resp = post_json(client, "/api/support/thread/mode", {"mode": "assistant"})
        assert resp.status_code == 200
        assert db.session.get(SupportThreads, thread_id).mode == "assistant"

    def test_mode_endpoint_rejects_garbage(self, client, ids):
        assert post_json(client, "/api/support/thread/mode", {"mode": "wat"}).status_code == 400

    def test_mode_endpoint_without_a_thread_creates_nothing(self, client, ids):
        before = SupportThreads.query.count()
        resp = post_json(client, "/api/support/thread/mode", {"mode": "assistant"})
        assert resp.status_code == 200
        assert resp.get_json()["thread"] is None
        assert SupportThreads.query.count() == before


# --- the three answer outcomes ----------------------------------------
class TestAnswerOutcomes:
    def test_answered_is_stored_with_its_citations(self, client, ids, monkeypatch):
        calls = fake_answer(monkeypatch, **ANSWERED)
        thread_id = start_thread(client)
        resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["status"] == "answered"
        message = data["messages"][0]
        assert message["sender"] == "assistant"
        assert message["sources"][0]["title"] == "Labs > Extending a lab"
        # the question, not the whole transcript, is what the service is sent
        assert calls[0]["question"] == "how do I extend a lab?"
        assert calls[0]["thread_id"] == thread_id

    def test_refusal_is_shown_as_an_assistant_message(self, client, ids, monkeypatch):
        fake_answer(monkeypatch, status="refused", reason="no_relevant_context")
        thread_id = start_thread(client, "what is the airspeed of a swallow?")
        resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "refused"
        stored = SupportMessages.query.filter_by(thread_id=thread_id, sender="assistant").one()
        assert "could not find" in stored.body
        assert stored.meta_dict["reason"] == "no_relevant_context"
        assert stored.sources == []

    def test_outage_is_shown_and_the_user_message_survives(self, client, ids, monkeypatch):
        fake_answer(monkeypatch, status="unavailable", reason="queue_full")
        thread_id = start_thread(client, "how do I start a lab?")
        resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "unavailable"
        thread = db.session.get(SupportThreads, thread_id)
        senders = [m.sender for m in thread.messages]
        # the question is still there and can be escalated without retyping
        assert senders == ["user", "assistant"]
        assert thread.messages[0].body == "how do I start a lab?"
        assert "busy" in thread.messages[1].body or "moment" in thread.messages[1].body

    def test_history_is_sent_without_the_question_being_answered(self, client, ids, monkeypatch):
        calls = fake_answer(monkeypatch, **ANSWERED)
        thread_id = start_thread(client, "first question")
        post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        post_json(
            client,
            "/api/support/thread/messages",
            {"body": "and how do I undo that?", "thread_id": thread_id},
        )
        post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        history = calls[1]["history"]
        assert [turn["role"] for turn in history] == ["user", "assistant"]
        assert history[0]["content"] == "first question"
        assert calls[1]["question"] == "and how do I undo that?"

    def test_support_mode_thread_refuses_the_assistant_endpoint(self, client, ids, monkeypatch):
        fake_answer(monkeypatch, **ANSWERED)
        thread_id = start_thread(client, "human please", mode="support")
        resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        assert resp.status_code == 409

    def test_finished_thread_rejects_an_answer(self, client, ids, monkeypatch):
        fake_answer(monkeypatch, **ANSWERED)
        thread_id = start_thread(client)
        finish_open_thread(client)
        resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        assert resp.status_code == 409
        assert resp.get_json()["finished"] is True

    def test_another_users_thread_is_not_found(self, client, ids, monkeypatch):
        fake_answer(monkeypatch, **ANSWERED)
        thread_id = start_thread(client)
        logout(client)
        assert login(client, "rag_bob", "bob123")
        resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        assert resp.status_code == 404
        logout(client)
        assert login(client, "rag_alice", "alice123")


# --- localization ------------------------------------------------------
class TestLocalization:
    def test_locale_reaches_the_service_and_the_refusal_is_translated(self, client, ids, monkeypatch):
        calls = fake_answer(monkeypatch, status="refused", reason="no_relevant_context")
        logout(client)
        assert login(client, "rag_alice", "alice123")
        client.get("/set-locale/pt_BR")
        # The whole suite shares one app context (the `app` fixture), and
        # flask_babel caches the resolved locale on it; in production every
        # request gets a fresh context. Drop the cached value by hand.
        refresh()

        thread_id = start_thread(client, "como estender um lab?")
        post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})

        thread = db.session.get(SupportThreads, thread_id)
        assert thread.locale == "pt_BR"
        assert calls[0]["locale"] == "pt_BR"
        body = thread.messages[-1].body
        # the refusal comes from the pt_BR catalog, not from the model
        assert "documentação" in body or "não" in body
        client.get("/set-locale/en")
        refresh()


# --- escalation --------------------------------------------------------
class TestEscalation:
    def test_escalation_makes_it_staff_work_and_snapshots_telemetry(self, client, ids, monkeypatch):
        fake_answer(monkeypatch, status="refused", reason="no_relevant_context")
        before = support.open_thread_count()
        thread_id = start_thread(client, "something obscure", mode="assistant")
        post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})

        resp = post_json(
            client,
            "/api/support/thread/escalate",
            {"thread_id": thread_id, "page": "/labs/abc — Lab page"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["mode"] == "support"

        thread = db.session.get(SupportThreads, thread_id)
        assert thread.mode == "support"
        assert support.open_thread_count() == before + 1

        snapshot = support.escalation_snapshot(thread)
        assert snapshot["page"] == "/labs/abc — Lab page"
        assert snapshot["user_agent"] is not None
        assert snapshot["questions_asked"] == 1
        assert snapshot["refusals"] == 1
        assert snapshot["last_question"] == "something obscure"

    def test_escalation_keeps_both_pages(self, client, ids, monkeypatch):
        """The snapshot must not overwrite where the conversation started."""
        fake_answer(monkeypatch, **ANSWERED)
        resp = post_json(
            client,
            "/api/support/thread/messages",
            {"body": "started here", "mode": "assistant", "page": "/dashboard — Home"},
        )
        thread_id = resp.get_json()["thread_id"]
        post_json(
            client,
            "/api/support/thread/escalate",
            {"thread_id": thread_id, "page": "/labs/xyz — Lab page"},
        )
        thread = db.session.get(SupportThreads, thread_id)
        assert thread.origin_page == "/dashboard — Home"
        assert support.escalation_snapshot(thread)["page"] == "/labs/xyz — Lab page"

    def test_escalating_a_support_thread_is_a_no_op(self, client, ids):
        thread_id = start_thread(client, "hello", mode="support")
        resp = post_json(client, "/api/support/thread/escalate", {"thread_id": thread_id})
        assert resp.status_code == 200
        assert resp.get_json()["messages"] == []
        thread = db.session.get(SupportThreads, thread_id)
        assert support.escalation_snapshot(thread) is None


# --- feedback ----------------------------------------------------------
class TestFeedback:
    def _answered_message(self, client, monkeypatch):
        fake_answer(monkeypatch, **ANSWERED)
        thread_id = start_thread(client)
        resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
        return thread_id, resp.get_json()["messages"][0]["id"]

    def test_vote_up_then_down_leaves_one_vote(self, client, ids, monkeypatch):
        thread_id, message_id = self._answered_message(client, monkeypatch)
        assert post_json(
            client, f"/api/support/messages/{message_id}/feedback", {"vote": "up"}
        ).get_json()["feedback"] == "up"
        resp = post_json(
            client,
            f"/api/support/messages/{message_id}/feedback",
            {"vote": "down", "reason": "wrong"},
        )
        assert resp.get_json()["feedback"] == "down"
        message = db.session.get(SupportMessages, message_id)
        assert message.feedback == "down"
        assert message.feedback_reason == "wrong"
        assert message.feedback_at is not None

    def test_the_same_vote_twice_clears_it(self, client, ids, monkeypatch):
        thread_id, message_id = self._answered_message(client, monkeypatch)
        post_json(client, f"/api/support/messages/{message_id}/feedback", {"vote": "up"})
        resp = post_json(client, f"/api/support/messages/{message_id}/feedback", {"vote": "up"})
        assert resp.get_json()["feedback"] is None
        message = db.session.get(SupportMessages, message_id)
        assert message.feedback is None and message.feedback_at is None

    def test_a_reason_is_only_kept_with_a_down_vote(self, client, ids, monkeypatch):
        thread_id, message_id = self._answered_message(client, monkeypatch)
        post_json(
            client,
            f"/api/support/messages/{message_id}/feedback",
            {"vote": "up", "reason": "wrong"},
        )
        assert db.session.get(SupportMessages, message_id).feedback_reason is None

    def test_only_assistant_messages_can_be_rated(self, client, ids, monkeypatch):
        thread_id, _message_id = self._answered_message(client, monkeypatch)
        user_message = SupportMessages.query.filter_by(thread_id=thread_id, sender="user").first()
        resp = post_json(
            client, f"/api/support/messages/{user_message.id}/feedback", {"vote": "up"}
        )
        assert resp.status_code == 400

    def test_another_users_message_is_not_found(self, client, ids, monkeypatch):
        thread_id, message_id = self._answered_message(client, monkeypatch)
        logout(client)
        assert login(client, "rag_bob", "bob123")
        resp = post_json(client, f"/api/support/messages/{message_id}/feedback", {"vote": "up"})
        assert resp.status_code == 404
        logout(client)
        assert login(client, "rag_alice", "alice123")

    def test_summary_counts_what_the_panel_shows(self, client, ids, monkeypatch):
        thread_id, message_id = self._answered_message(client, monkeypatch)
        post_json(
            client,
            f"/api/support/messages/{message_id}/feedback",
            {"vote": "down", "reason": "unrelated"},
        )
        summary = support.feedback_summary()
        assert summary["down"] >= 1
        negative = [n for n in summary["recent_negative"] if n["message_id"] == message_id]
        assert negative and negative[0]["question"] == "how do I extend a lab?"
        assert negative[0]["reason"] == "unrelated"

    def test_admin_stats_endpoint_is_admin_only(self, client, ids, monkeypatch):
        monkeypatch.setattr(rag_client, "stats", lambda: {"corpus": {"chunks": 7}})
        resp = client.get("/api/support/assistant/stats")
        assert resp.status_code == 403
        logout(client)
        assert login(client, "rag_admin", "admin123")
        data = client.get("/api/support/assistant/stats").get_json()
        assert data["service"]["corpus"]["chunks"] == 7
        assert "feedback" in data
        logout(client)
        assert login(client, "rag_alice", "alice123")


# --- degradation -------------------------------------------------------
class TestDegradation:
    def test_breaker_opens_after_repeated_failures(self, app, monkeypatch):
        import requests

        def _boom(*args, **kwargs):
            raise requests.ConnectionError("nope")

        monkeypatch.setattr(rag_client, "_request", _boom)
        for _i in range(app.config["RAG_BREAKER_FAILURES"]):
            assert rag_client.answer("q").status == "unavailable"
        assert rag_client.get_breaker().is_open is True
        assert support.assistant_available() is False
        # ... and the widget stops offering the assistant
        assert rag_client.answer("q").reason == "circuit_open"

    def test_saturation_does_not_open_the_breaker(self, app, monkeypatch):
        saturated = _Response(503, {"status": "unavailable", "reason": "queue_full"})
        monkeypatch.setattr(rag_client, "_request", lambda *a, **k: saturated)
        for _i in range(app.config["RAG_BREAKER_FAILURES"] + 2):
            assert rag_client.answer("q").reason == "queue_full"
        # a service that correctly says "busy" is healthy, not broken
        assert rag_client.get_breaker().is_open is False

    def test_disabled_assistant_hides_the_chooser(self, client, app, ids):
        app.config["RAG_ENABLED"] = False
        try:
            data = client.get("/api/support/assistant/status").get_json()
            assert data["enabled"] is False and data["available"] is False
            assert post_json(client, "/api/support/assistant/answer", {}).status_code == 404
        finally:
            app.config["RAG_ENABLED"] = True

    def test_rate_limit_stops_calling_the_service(self, client, app, ids, monkeypatch):
        calls = fake_answer(monkeypatch, **ANSWERED)
        # Bob has asked nothing yet, so the budget starts from zero.
        logout(client)
        assert login(client, "rag_bob", "bob123")
        app.config["RAG_USER_RATE_LIMIT"] = 2
        try:
            thread_id = start_thread(client)
            for _i in range(2):
                post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
            resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
            assert resp.status_code == 429
            assert resp.get_json()["rate_limited"] is True
            assert len(calls) == 2  # the third question never reached the service
        finally:
            app.config["RAG_USER_RATE_LIMIT"] = 1000
            finish_open_thread(client)

    def test_transcripts_off_stores_nothing_and_offers_no_vote(self, client, app, ids, monkeypatch):
        fake_answer(monkeypatch, **ANSWERED)
        app.config["RAG_STORE_TRANSCRIPTS"] = False
        try:
            assert client.get("/api/support/assistant/status").get_json()["feedback"] is False
            thread_id = start_thread(client)
            resp = post_json(client, "/api/support/assistant/answer", {"thread_id": thread_id})
            assert resp.status_code == 201
            data = resp.get_json()
            assert data["stored"] is False
            assert data["messages"][0]["id"] is None  # nothing to attach a vote to
            thread = db.session.get(SupportThreads, thread_id)
            assert [m.sender for m in thread.messages] == ["user"]
        finally:
            app.config["RAG_STORE_TRANSCRIPTS"] = True


# --- the HTTP client ---------------------------------------------------
class TestRagClient:
    """The client must never raise into the request path: every failure is a Result."""

    def test_answered_payload_is_mapped(self, app, monkeypatch):
        monkeypatch.setattr(
            rag_client, "_request", lambda *a, **k: _Response(200, dict(ANSWERED, cached=True))
        )
        result = rag_client.answer("q", locale="pt_BR", thread_id=7)
        assert result.answered and result.cached is True
        assert result.sources[0]["index"] == 1

    def test_refusal_payload_is_mapped(self, app, monkeypatch):
        monkeypatch.setattr(
            rag_client,
            "_request",
            lambda *a, **k: _Response(200, {"status": "refused", "reason": "no_relevant_context"}),
        )
        result = rag_client.answer("q")
        assert result.status == "refused" and result.reason == "no_relevant_context"

    def test_malformed_response_is_unavailable(self, app, monkeypatch):
        monkeypatch.setattr(rag_client, "_request", lambda *a, **k: _Response(200, bad_json=True))
        assert rag_client.answer("q").reason == "bad_response"

    def test_server_error_counts_toward_the_breaker(self, app, monkeypatch):
        monkeypatch.setattr(rag_client, "_request", lambda *a, **k: _Response(500))
        assert rag_client.answer("q").reason == "http_500"
        assert rag_client.get_breaker().state()["failures"] == 1

    def test_disabled_client_answers_without_calling_out(self, app, monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError("must not be called when disabled")

        monkeypatch.setattr(rag_client, "_request", _fail)
        app.config["RAG_ENABLED"] = False
        try:
            assert rag_client.answer("q").reason == "disabled"
            assert rag_client.health() == {"status": "disabled"}
            assert rag_client.stats() == {"status": "disabled"}
        finally:
            app.config["RAG_ENABLED"] = True

    def test_health_and_stats_survive_an_unreachable_service(self, app, monkeypatch):
        import requests

        def _boom(*args, **kwargs):
            raise requests.ConnectionError("nope")

        monkeypatch.setattr(rag_client, "_request", _boom)
        assert rag_client.health()["status"] == "unreachable"
        assert rag_client.stats()["status"] == "unreachable"

    def test_conversation_hash_is_pseudonymous_and_stable(self, app):
        first = rag_client.conversation_hash(42)
        assert first == rag_client.conversation_hash(42)  # stable per thread
        assert first != rag_client.conversation_hash(43)  # distinct per thread
        # a one-way digest, not a reversible encoding of the id: fixed-width hex
        # under a "sha256:" prefix (a bare-id check would be flaky, since a hex
        # digest can contain the id's digits by chance)
        assert first.startswith("sha256:")
        body = first.split(":", 1)[1]
        assert len(body) == 32 and all(c in "0123456789abcdef" for c in body)

    def test_ingest_raises_so_the_cli_can_report_it(self, app, monkeypatch):
        import requests

        monkeypatch.setattr(rag_client, "_request", lambda *a, **k: _Response(500))
        with pytest.raises(requests.HTTPError):
            rag_client.ingest([{"doc_id": "x"}])

    def test_breaker_half_opens_after_the_reset_window(self, app, monkeypatch):
        breaker = rag_client.CircuitBreaker(failures=2, reset_s=0)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state()["open"] is True
        # a zero-length window means the next check probes the service again
        assert breaker.is_open is False


# --- e-mail batching ---------------------------------------------------
class TestEmailBatching:
    def test_assistant_threads_are_not_emailed_until_escalated(self, app, ids, monkeypatch):
        from apps.cli import support_notify

        sent = []
        monkeypatch.setattr(
            support_notify, "Mail", lambda a: types.SimpleNamespace(send=lambda m: sent.append(m))
        )
        app.config["MAIL_SENDTO"] = "support@test.local"

        thread = SupportThreads(user_id=ids["bob_id"], status="open", mode="assistant")
        db.session.add(thread)
        db.session.flush()
        message = support.add_message(thread, "user", "a question for the bot")
        message.created_at = utcnow().replace(tzinfo=None) - timedelta(hours=1)
        db.session.commit()

        support_notify.flush_support_emails(app)
        assert not any("a question for the bot" in m.body for m in sent)
        assert message.emailed_at is None  # not stamped either: it may be escalated later

        sent.clear()
        support.escalate_thread(thread, page="/labs/xyz", user_agent="pytest", ip="10.0.0.9")
        db.session.commit()
        support_notify.flush_support_emails(app)
        reported = [m for m in sent if "a question for the bot" in m.body]
        assert len(reported) == 1
        assert "Escalated from: /labs/xyz" in reported[0].body
        assert "1 question(s)" in reported[0].body


# --- ingestion ---------------------------------------------------------
class TestIngestion:
    def test_repo_docs_and_faq_are_collected_with_stable_hashes(self, app):
        from apps.cli.rag_ingest import collect_faq, collect_repo_docs

        docs = collect_repo_docs(app)
        assert docs and all(d["source"] == "repo-docs" for d in docs)
        assert {d["doc_id"] for d in docs} >= {"README.md", "doc/DEV.md"}
        assert collect_repo_docs(app)[0]["content_hash"] == docs[0]["content_hash"]

        faq = collect_faq(app)
        langs = {d["lang"] for d in faq}
        assert langs == {"en", "pt_BR"}  # the assistant answers in both

    def test_dry_run_sends_nothing(self, app, monkeypatch):
        from apps.cli import rag_ingest

        def _fail(*args, **kwargs):
            raise AssertionError("dry-run must not contact the service")

        monkeypatch.setattr(rag_ingest.rag_client, "ingest", _fail)
        totals = rag_ingest.run_ingest(app, sources=["repo-docs", "faq"], dry_run=True)
        assert totals["documents"] > 0
        assert totals["indexed"] == 0

    def test_prune_manifest_rides_on_the_last_batch(self, app, monkeypatch):
        from apps.cli import rag_ingest

        seen = []
        monkeypatch.setattr(
            rag_ingest.rag_client,
            "ingest",
            lambda documents, prune=None: seen.append((len(documents), prune)) or {"indexed": len(documents)},
        )
        app.config["RAG_INGEST_BATCH"] = 2
        try:
            rag_ingest.run_ingest(app, sources=["faq"])
        finally:
            app.config.pop("RAG_INGEST_BATCH", None)
        assert seen[-1][1]["source"] == "faq"
        assert len(seen[-1][1]["keep_doc_ids"]) == 2
        # a partially-sent corpus must never delete live documents
        assert all(prune is None for _n, prune in seen[:-1])

    def test_a_deleted_lab_leaves_the_keep_list(self, app, monkeypatch):
        from apps.cli import rag_ingest
        from apps.home.models import Labs

        lab = Labs(title="Doomed lab", description="about to go", is_deleted=False)
        db.session.add(lab)
        db.session.commit()
        assert any(d["doc_id"] == f"lab:{lab.id}" for d in rag_ingest.collect_lab_descriptions(app))

        lab.is_deleted = True
        db.session.commit()
        assert not any(
            d["doc_id"] == f"lab:{lab.id}" for d in rag_ingest.collect_lab_descriptions(app)
        )

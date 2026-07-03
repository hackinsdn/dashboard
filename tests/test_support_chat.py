"""Pytest suite for the Support Chat feature.

Exercises the user-facing widget endpoints (start/continue a thread, finish,
history scoped to the current user, empty-body validation, the 2h inactivity
boundary) and the admin thread-management page + staff-reply endpoint
(role gating, reply creation, marking user messages read).

Runs entirely against a throwaway temporary SQLite database - it never touches
the real dev/production database.

Usage:
    pytest tests/test_support_chat.py -v

Note: the classes below are ordered, stateful workflows - pytest runs test
methods within a class in definition order. Do not run with pytest-randomly
without disabling it for this file.
"""
import importlib
import json
import os
import sys
import tempfile
import types
from datetime import timedelta

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# --- isolate the test run from any real data --------------------------------
TEST_DATA_DIR = tempfile.mkdtemp(prefix="hackinsdn_test_")
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
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def ids(app):
    admin = Users(username="sc_admin", password="admin123", email="sc_admin@test.local", category="admin")
    alice = Users(username="sc_alice", password="alice123", email="sc_alice@test.local", category="student")
    bob = Users(username="sc_bob", password="bob123", email="sc_bob@test.local", category="student")
    db.session.add_all([admin, alice, bob])
    db.session.commit()
    return {"admin_id": admin.id, "alice_id": alice.id, "bob_id": bob.id}


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    resp = client.post(
        "/login/",
        data={"identifier": username, "password": password, "login": "1"},
    )
    return resp.status_code == 302


def logout(client):
    client.get("/logout")


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


# --- user widget flow --------------------------------------------------
class TestUserThreadFlow:
    def test_get_thread_empty_when_none(self, client, ids):
        logout(client)
        assert login(client, "sc_alice", "alice123")
        resp = client.get("/api/support/thread")
        assert resp.status_code == 200
        assert resp.get_json()["thread"] is None

    def test_empty_body_rejected(self, client, ids):
        resp = post_json(client, "/api/support/thread/messages", {"body": "   "})
        assert resp.status_code == 400

    def test_send_message_creates_thread(self, client, ids):
        resp = post_json(client, "/api/support/thread/messages", {"body": "hello there"})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["messages"][0]["body"] == "hello there"
        assert data["messages"][0]["sender"] == "user"
        thread = SupportThreads.query.filter_by(user_id=ids["alice_id"]).one()
        assert thread.status == "open"
        assert len(thread.messages) == 1

    def test_history_returned_on_get(self, client, ids):
        resp = client.get("/api/support/thread")
        thread = resp.get_json()["thread"]
        assert thread is not None
        assert thread["messages"][0]["body"] == "hello there"

    def test_second_message_reuses_same_thread(self, client, ids):
        post_json(client, "/api/support/thread/messages", {"body": "second one"})
        threads = SupportThreads.query.filter_by(user_id=ids["alice_id"]).all()
        assert len(threads) == 1
        assert len(threads[0].messages) == 2

    def test_inactivity_starts_new_thread(self, client, ids):
        thread = SupportThreads.query.filter_by(user_id=ids["alice_id"]).one()
        old_id = thread.id
        # backdate last activity beyond the 2h window
        thread.updated_at = utcnow() - timedelta(hours=3)
        db.session.commit()

        resp = post_json(client, "/api/support/thread/messages", {"body": "much later"})
        assert resp.status_code == 201
        threads = SupportThreads.query.filter_by(user_id=ids["alice_id"]).order_by(SupportThreads.id).all()
        assert len(threads) == 2
        old = db.session.get(SupportThreads, old_id)
        assert old.status == "finished"
        assert old.finished_at is not None
        assert threads[-1].status == "open"

    def test_finish_thread(self, client, ids):
        resp = post_json(client, "/api/support/thread/finish", {})
        assert resp.status_code == 200
        # active thread is gone; a GET now returns None
        assert client.get("/api/support/thread").get_json()["thread"] is None
        open_threads = SupportThreads.query.filter_by(user_id=ids["alice_id"], status="open").all()
        assert open_threads == []

    def test_thread_history_scoped_to_current_user(self, client, ids):
        # bob must not see alice's threads
        logout(client)
        assert login(client, "sc_bob", "bob123")
        assert client.get("/api/support/thread").get_json()["thread"] is None
        post_json(client, "/api/support/thread/messages", {"body": "bob message"})
        thread = client.get("/api/support/thread").get_json()["thread"]
        assert len(thread["messages"]) == 1
        assert thread["messages"][0]["body"] == "bob message"


# --- admin management + reply ------------------------------------------
class TestAdminSupport:
    def test_non_admin_rejected_from_list(self, client, ids):
        logout(client)
        login(client, "sc_alice", "alice123")
        resp = client.get("/support/threads")
        assert b"Unauthorized request" in resp.data

    def test_non_admin_reply_forbidden(self, client, ids):
        thread = SupportThreads.query.filter_by(user_id=ids["bob_id"]).first()
        resp = post_json(client, f"/api/support/threads/{thread.id}/messages", {"body": "nope"})
        assert resp.status_code == 403

    def test_admin_can_view_list(self, client, ids):
        logout(client)
        assert login(client, "sc_admin", "admin123")
        resp = client.get("/support/threads")
        assert resp.status_code == 200
        assert b"Support Chats" in resp.data
        # bob's open thread with an unread message should be listed
        assert b"bob" in resp.data

    def test_admin_reply_creates_support_message_and_marks_read(self, client, ids):
        thread = SupportThreads.query.filter_by(user_id=ids["bob_id"]).first()
        assert thread.unread_count == 1
        resp = post_json(
            client, f"/api/support/threads/{thread.id}/messages", {"body": "how can we help?"}
        )
        assert resp.status_code == 201
        assert resp.get_json()["messages"][0]["sender"] == "support"
        db.session.refresh(thread)
        assert thread.unread_count == 0
        senders = [m.sender for m in thread.messages]
        assert "support" in senders

    def test_admin_reply_missing_thread_404(self, client, ids):
        resp = post_json(client, "/api/support/threads/999999/messages", {"body": "x"})
        assert resp.status_code == 404

    def test_admin_view_thread_marks_read(self, client, ids):
        # a fresh unread message from bob
        logout(client)
        login(client, "sc_bob", "bob123")
        post_json(client, "/api/support/thread/messages", {"body": "still stuck"})
        thread = SupportThreads.query.filter_by(user_id=ids["bob_id"], status="open").first()
        assert thread.unread_count >= 1

        logout(client)
        login(client, "sc_admin", "admin123")
        resp = client.get(f"/support/threads/{thread.id}")
        assert resp.status_code == 200
        db.session.refresh(thread)
        assert thread.unread_count == 0
        logout(client)

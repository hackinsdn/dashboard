"""Pytest suite for the Support Chat feature.

Exercises the user-facing widget endpoints (start/continue a thread, finish,
history scoped to the current user, empty-body validation, reuse of an open
thread regardless of its age) and the admin thread-management page +
staff-reply endpoint (role gating, reply creation, marking user messages read).

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
from apps.controllers import support  # noqa: E402

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

    def test_old_open_thread_is_reused_not_auto_finished(self, client, ids):
        # cases are only finished by the user or an admin - never by inactivity
        thread = SupportThreads.query.filter_by(user_id=ids["alice_id"]).one()
        old_id = thread.id
        # backdate last activity far into the past
        thread.updated_at = utcnow() - timedelta(hours=48)
        db.session.commit()

        resp = post_json(client, "/api/support/thread/messages", {"body": "much later"})
        assert resp.status_code == 201
        threads = SupportThreads.query.filter_by(user_id=ids["alice_id"]).all()
        assert len(threads) == 1
        old = db.session.get(SupportThreads, old_id)
        assert old.status == "open"
        assert old.finished_at is None
        assert len(old.messages) == 3

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


# --- helper to create additional users on the shared DB ----------------
def make_user(username, category="student"):
    user = Users(
        username=username, password="pw123456",
        email=f"{username}@test.local", category=category,
    )
    db.session.add(user)
    db.session.commit()
    return user


# --- telemetry on conversation start -----------------------------------
class TestTelemetry:
    def test_telemetry_recorded_on_new_thread(self, client, ids):
        carol = make_user("sc_carol")
        logout(client)
        assert login(client, "sc_carol", "pw123456")
        resp = client.post(
            "/api/support/thread/messages",
            data=json.dumps({"body": "help please", "page": "/labs — Labs page"}),
            content_type="application/json",
            headers={"User-Agent": "PyTest-UA/9.9"},
        )
        assert resp.status_code == 201
        thread = SupportThreads.query.filter_by(user_id=carol.id).one()
        assert thread.origin_page == "/labs — Labs page"
        assert thread.user_agent == "PyTest-UA/9.9"
        assert thread.ip_address  # some remote address recorded
        # message should not be e-mailed yet (batched later)
        assert all(m.emailed_at is None for m in thread.messages)


# --- user-facing pages + read endpoint authorization -------------------
class TestUserPages:
    def test_user_sees_only_own_threads(self, client, ids):
        dave = make_user("sc_dave")
        logout(client)
        login(client, "sc_dave", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "dave needs help"})

        resp = client.get("/support/my")
        assert resp.status_code == 200
        dave_thread = SupportThreads.query.filter_by(user_id=dave.id).one()
        assert f"#{dave_thread.id}".encode() in resp.data

        # dave cannot open another user's thread page
        bob_thread = SupportThreads.query.filter_by(user_id=ids["bob_id"]).first()
        resp = client.get(f"/support/my/{bob_thread.id}")
        assert b"Support thread not found" in resp.data

    def test_read_endpoint_authorization(self, client, ids):
        dave_user = Users.query.filter_by(username="sc_dave").one()
        dave = SupportThreads.query.filter_by(user_id=dave_user.id).first()
        # owner can read
        logout(client)
        login(client, "sc_dave", "pw123456")
        assert client.get(f"/api/support/threads/{dave.id}").status_code == 200
        # a different non-admin user cannot
        logout(client)
        login(client, "sc_bob", "bob123")
        assert client.get(f"/api/support/threads/{dave.id}").status_code == 403
        # admin can
        logout(client)
        login(client, "sc_admin", "admin123")
        assert client.get(f"/api/support/threads/{dave.id}").status_code == 200
        logout(client)


# --- admin open-vs-all filter ------------------------------------------
class TestAdminFilter:
    def test_default_open_only_all_toggle(self, client, ids):
        finished = make_user("sc_erin")
        logout(client)
        login(client, "sc_erin", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "erin msg"})
        post_json(client, "/api/support/thread/finish", {})
        erin_thread = SupportThreads.query.filter_by(user_id=finished.id).one()
        assert erin_thread.status == "finished"

        logout(client)
        login(client, "sc_admin", "admin123")
        marker = f'/support/threads/{erin_thread.id}"'.encode()

        default = client.get("/support/threads")
        assert marker not in default.data  # finished thread hidden by default

        show_all = client.get("/support/threads?show=all")
        assert marker in show_all.data
        logout(client)


# --- navbar unread indicator -------------------------------------------
class TestNavbarUnread:
    def test_unread_reflects_staff_reply_and_clears_on_view(self, client, ids):
        frank = make_user("sc_frank")
        logout(client)
        login(client, "sc_frank", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "frank question"})
        thread = SupportThreads.query.filter_by(user_id=frank.id).one()
        assert thread.has_unseen_for_user is False  # only own message so far

        # admin replies -> becomes unseen for the user
        logout(client)
        login(client, "sc_admin", "admin123")
        post_json(client, f"/api/support/threads/{thread.id}/messages", {"body": "hi frank"})
        db.session.refresh(thread)
        assert thread.has_unseen_for_user is True

        # frank's navbar shows the badge, then clears after opening the thread
        logout(client)
        login(client, "sc_frank", "pw123456")
        page = client.get("/support/my")
        assert b"See All Messages" in page.data
        assert b'badge badge-danger navbar-badge">1' in page.data  # unread count badge
        client.get(f"/support/my/{thread.id}")
        db.session.refresh(thread)
        assert thread.has_unseen_for_user is False
        logout(client)


# --- batched support e-mail (CLI) --------------------------------------
class TestBatchEmail:
    def test_flush_groups_after_quiet_window(self, client, ids, monkeypatch):
        from apps.cli import support_notify

        sent = []

        class FakeMail:
            def __init__(self, app):
                pass

            def send(self, msg):
                sent.append(msg)

        monkeypatch.setattr(support_notify, "Mail", FakeMail)
        monkeypatch.setitem(flask_app.config, "MAIL_SENDTO", "support@test.local")

        # neutralize pending messages left behind by the earlier workflow tests
        # (e.g. user-finished threads with never-seen messages, which the flush
        # reports immediately) so this test only observes grace's thread
        db.session.query(SupportMessages).filter(
            SupportMessages.sender == "user", SupportMessages.emailed_at.is_(None)
        ).update({"emailed_at": utcnow()}, synchronize_session=False)
        db.session.commit()

        grace = make_user("sc_grace")
        logout(client)
        login(client, "sc_grace", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "grace one"})
        post_json(client, "/api/support/thread/messages", {"body": "grace two"})
        thread = SupportThreads.query.filter_by(user_id=grace.id).one()

        # recent messages -> nothing sent yet
        support_notify.flush_support_emails(flask_app)
        assert sent == []

        # user goes quiet: backdate the messages beyond the batch window
        for m in thread.messages:
            m.created_at = utcnow() - timedelta(minutes=30)
        db.session.commit()

        support_notify.flush_support_emails(flask_app)
        assert len(sent) == 1
        # both messages grouped in a single e-mail and marked emailed
        db.session.refresh(thread)
        assert all(m.emailed_at is not None for m in thread.messages)

        # second run sends nothing more
        support_notify.flush_support_emails(flask_app)
        assert len(sent) == 1
        logout(client)

    def test_flush_reports_ongoing_conversation_not_starved(self, client, ids, monkeypatch):
        """An old un-e-mailed message must be reported even if a newer one just arrived."""
        from apps.cli import support_notify

        sent = []

        class FakeMail:
            def __init__(self, app):
                pass

            def send(self, msg):
                sent.append(msg)

        monkeypatch.setattr(support_notify, "Mail", FakeMail)
        monkeypatch.setitem(flask_app.config, "MAIL_SENDTO", "support@test.local")

        olga = make_user("sc_olga")
        logout(client)
        login(client, "sc_olga", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "olga old"})
        post_json(client, "/api/support/thread/messages", {"body": "olga new"})
        thread = SupportThreads.query.filter_by(user_id=olga.id).one()

        # Ongoing conversation: the first message is well past the window, but the
        # latest one is recent. The old message must still be flushed (previously it
        # was starved by the newer message and never reported).
        msgs = sorted(thread.messages, key=lambda m: m.id)
        msgs[0].created_at = utcnow() - timedelta(minutes=30)   # old
        msgs[1].created_at = utcnow() - timedelta(minutes=1)    # recent activity
        db.session.commit()

        support_notify.flush_support_emails(flask_app)
        db.session.refresh(thread)
        # both pending messages grouped into a single e-mail and marked
        assert all(m.emailed_at is not None for m in thread.messages if m.sender == "user")
        assert any("olga old" in (m.body or "") for m in sent)
        logout(client)

    def test_flush_skips_finished_thread_already_seen_by_staff(self, client, ids, monkeypatch):
        """A case handled in-app (staff saw the messages) and finished must not be
        e-mailed; its pending messages are stamped so later runs don't re-scan it."""
        from apps.cli import support_notify

        sent = []

        class FakeMail:
            def __init__(self, app):
                pass

            def send(self, msg):
                sent.append(msg)

        monkeypatch.setattr(support_notify, "Mail", FakeMail)
        monkeypatch.setitem(flask_app.config, "MAIL_SENDTO", "support@test.local")

        pat = make_user("sc_pat")
        logout(client)
        login(client, "sc_pat", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "pat question"})
        thread = SupportThreads.query.filter_by(user_id=pat.id).one()
        for m in thread.messages:
            m.created_at = utcnow() - timedelta(minutes=30)
        db.session.commit()

        # support finishes the case (marks the user messages read)
        logout(client)
        login(client, "sc_admin", "admin123")
        post_json(client, f"/api/support/threads/{thread.id}/finish", {})

        support_notify.flush_support_emails(flask_app)
        assert sent == []

        # pending messages were stamped without sending -> nothing lingers
        db.session.refresh(thread)
        assert all(m.emailed_at is not None for m in thread.messages if m.sender == "user")
        support_notify.flush_support_emails(flask_app)
        assert sent == []
        logout(client)

    def test_flush_reports_user_finished_thread_with_unseen_messages(self, client, ids, monkeypatch):
        """A case the user wrote and closed before staff ever saw it is still
        e-mailed - immediately, without waiting for the quiet window."""
        from apps.cli import support_notify

        sent = []

        class FakeMail:
            def __init__(self, app):
                pass

            def send(self, msg):
                sent.append(msg)

        monkeypatch.setattr(support_notify, "Mail", FakeMail)
        monkeypatch.setitem(flask_app.config, "MAIL_SENDTO", "support@test.local")

        quinn = make_user("sc_quinn")
        logout(client)
        login(client, "sc_quinn", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "quinn drive-by question"})
        # user finishes right away; messages are recent (inside the quiet window)
        post_json(client, "/api/support/thread/finish", {})
        thread = SupportThreads.query.filter_by(user_id=quinn.id).one()
        assert thread.status == "finished"

        support_notify.flush_support_emails(flask_app)
        assert len(sent) == 1
        assert "quinn drive-by question" in sent[0].body

        db.session.refresh(thread)
        assert all(m.emailed_at is not None for m in thread.messages if m.sender == "user")
        # second run sends nothing more
        support_notify.flush_support_emails(flask_app)
        assert len(sent) == 1
        logout(client)


# --- admin sidebar open-cases badge count -------------------------------
class TestAdminOpenCount:
    def test_open_count_tracks_new_case_then_finish(self, client, ids):
        base = support.open_thread_count()

        heidi = make_user("sc_heidi")
        logout(client)
        login(client, "sc_heidi", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "heidi needs help"})
        thread = SupportThreads.query.filter_by(user_id=heidi.id).one()

        # one more case is open
        assert support.open_thread_count() == base + 1

        # the sidebar badge is rendered on an admin page (warning style)
        logout(client)
        login(client, "sc_admin", "admin123")
        resp = client.get("/support/threads")
        assert f'badge-warning right">{base + 1}<'.encode() in resp.data

        # replying does NOT close the case -> count unchanged
        post_json(client, f"/api/support/threads/{thread.id}/messages", {"body": "on it"})
        assert support.open_thread_count() == base + 1

        # finishing the case drops the count back
        post_json(client, f"/api/support/threads/{thread.id}/finish", {})
        assert support.open_thread_count() == base
        logout(client)


# --- multi-line message input ------------------------------------------
class TestMultilineMessage:
    def test_newlines_preserved(self, client, ids):
        make_user("sc_ivan")
        logout(client)
        login(client, "sc_ivan", "pw123456")
        # surrounding whitespace is trimmed, internal newlines are kept
        resp = post_json(
            client,
            "/api/support/thread/messages",
            {"body": "  line one\nline two\n\nline four  "},
        )
        assert resp.status_code == 201
        body = resp.get_json()["messages"][0]["body"]
        assert body == "line one\nline two\n\nline four"
        assert "\n" in body
        logout(client)


# --- finishing a conversation records a system message -----------------
def _system_bodies(thread):
    return [m.body for m in thread.messages if m.sender == "system"]


class TestFinishConversation:
    def test_user_finish_records_system_message(self, client, ids):
        jane = make_user("sc_jane")
        logout(client)
        login(client, "sc_jane", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "jane hi"})
        thread = SupportThreads.query.filter_by(user_id=jane.id).one()

        resp = post_json(client, "/api/support/thread/finish", {})
        assert resp.status_code == 200
        db.session.refresh(thread)
        assert thread.status == "finished"
        assert _system_bodies(thread) == ["Conversation finished by the user."]
        logout(client)

    def test_admin_finish_records_system_message_and_shows_on_page(self, client, ids):
        karl = make_user("sc_karl")
        logout(client)
        login(client, "sc_karl", "pw123456")
        post_json(client, "/api/support/thread/messages", {"body": "karl hi"})
        thread = SupportThreads.query.filter_by(user_id=karl.id).one()

        # a normal user cannot finish someone else's thread via the admin route
        logout(client)
        login(client, "sc_bob", "bob123")
        assert post_json(client, f"/api/support/threads/{thread.id}/finish", {}).status_code == 403

        logout(client)
        login(client, "sc_admin", "admin123")
        resp = post_json(client, f"/api/support/threads/{thread.id}/finish", {})
        assert resp.status_code == 200
        db.session.refresh(thread)
        assert thread.status == "finished"
        assert _system_bodies(thread) == ["Conversation finished by the support team."]
        # support finishing a case marks its user messages read
        assert thread.unread_count == 0

        # the closing note is rendered on the admin thread view
        page = client.get(f"/support/threads/{thread.id}")
        assert b"Conversation finished by the support team." in page.data

        # finishing again is idempotent (no duplicate system message)
        post_json(client, f"/api/support/threads/{thread.id}/finish", {})
        db.session.refresh(thread)
        assert len(_system_bodies(thread)) == 1
        logout(client)

    def test_admin_finish_missing_thread_404(self, client, ids):
        logout(client)
        login(client, "sc_admin", "admin123")
        assert post_json(client, "/api/support/threads/999999/finish", {}).status_code == 404
        logout(client)


# --- posting into a specific (possibly finished) thread ----------------
class TestPostIntoThread:
    def test_thread_id_appends_to_same_thread(self, client, ids):
        make_user("sc_leo")
        logout(client)
        login(client, "sc_leo", "pw123456")
        tid = post_json(client, "/api/support/thread/messages", {"body": "first"}).get_json()["thread_id"]
        resp = post_json(client, "/api/support/thread/messages", {"body": "second", "thread_id": tid})
        assert resp.status_code == 201
        assert resp.get_json()["thread_id"] == tid
        thread = db.session.get(SupportThreads, tid)
        assert sum(1 for m in thread.messages if m.sender == "user") == 2
        logout(client)

    def test_post_to_finished_thread_rejected(self, client, ids):
        mia = make_user("sc_mia")
        logout(client)
        login(client, "sc_mia", "pw123456")
        tid = post_json(client, "/api/support/thread/messages", {"body": "hi"}).get_json()["thread_id"]

        # admin finishes the conversation
        logout(client)
        login(client, "sc_admin", "admin123")
        post_json(client, f"/api/support/threads/{tid}/finish", {})

        # the user tries to post into that finished conversation
        logout(client)
        login(client, "sc_mia", "pw123456")
        resp = post_json(client, "/api/support/thread/messages", {"body": "still there?", "thread_id": tid})
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["finished"] is True
        assert "support team" in data["error"]
        # no new user message was recorded
        thread = db.session.get(SupportThreads, tid)
        assert sum(1 for m in thread.messages if m.sender == "user") == 1
        logout(client)

    def test_thread_id_of_another_user_404(self, client, ids):
        make_user("sc_nina")
        logout(client)
        login(client, "sc_nina", "pw123456")
        tid = post_json(client, "/api/support/thread/messages", {"body": "nina"}).get_json()["thread_id"]
        logout(client)
        login(client, "sc_bob", "bob123")
        resp = post_json(client, "/api/support/thread/messages", {"body": "sneaky", "thread_id": tid})
        assert resp.status_code == 404
        logout(client)

    def test_unknown_thread_id_404(self, client, ids):
        logout(client)
        login(client, "sc_bob", "bob123")
        resp = post_json(client, "/api/support/thread/messages", {"body": "x", "thread_id": 999999})
        assert resp.status_code == 404
        logout(client)

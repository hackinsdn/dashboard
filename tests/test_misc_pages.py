"""Pytest suite for the dashboard, feedback, and static home pages.

Mirrors tests/test_lab_categories.py: covers the routes in apps/home/routes.py
that had no direct coverage - index (dashboard + feedback-prompt logic),
feedback_view, hide_feedback, and the simple login-only pages (gallery,
documentation, contact, finished-lab-infos). Exercises the UserFeedbacks and
UserLikes models.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_misc_pages.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "mp" so they never collide with the
other test modules that share the same singleton app/database (apps/config.py
reads DATA_DIR once at import time, so every module ends up on one DB).
"""
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# --- isolate the test run from any real data --------------------------------
TEST_DATA_DIR = tempfile.mkdtemp(prefix="hackinsdn_test_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("OPTIONAL_MODULES", "")

# stub the clabernetes controller (needs a 'clabverter' binary at import time)
_fake_clabernetes = types.ModuleType("apps.controllers.clabernetes")


class _StubC9sController:
    def __getattr__(self, name):
        raise NotImplementedError("clabernetes stub - not needed for these tests")


_fake_clabernetes.C9sController = _StubC9sController
sys.modules["apps.controllers.clabernetes"] = _fake_clabernetes

from run import app as flask_app  # noqa: E402
from apps import db, cache  # noqa: E402
from apps.authentication.models import Users  # noqa: E402
from apps.home.models import UserFeedbacks, UserLikes  # noqa: E402

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


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def ids(app):
    """Seed the users/feedback fixtures shared by every test."""
    admin = Users(username="mpadmin", password="admin123", email="mpadmin@test.local", category="admin")
    student = Users(username="mpstudent", password="stud123", email="mpstudent@test.local", category="student")
    # a user whose account is old and has no feedback -> the index feedback
    # modal should trigger for them
    old_user = Users(username="mpolduser", password="old123", email="mpold@test.local", category="student")
    old_user.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    # a user who already left feedback -> the modal must NOT trigger
    fb_user = Users(username="mpfbuser", password="fb123", email="mpfb@test.local", category="student")
    db.session.add_all([admin, student, old_user, fb_user])
    db.session.commit()

    like = UserLikes(user_id=student.id)
    visible_fb = UserFeedbacks(user_id=student.id, stars=5, comment="VISIBLE-FB-mp", is_hidden=False)
    hidden_fb = UserFeedbacks(user_id=admin.id, stars=1, comment="HIDDEN-FB-mp", is_hidden=True)
    own_fb = UserFeedbacks(user_id=fb_user.id, stars=4, comment="OWN-FB-mp", is_hidden=False)
    db.session.add_all([like, visible_fb, hidden_fb, own_fb])
    db.session.commit()

    return {
        "admin_id": admin.id,
        "student_id": student.id,
        "old_user_id": old_user.id,
        "fb_user_id": fb_user.id,
        "visible_fb_id": visible_fb.id,
        "hidden_fb_id": hidden_fb.id,
    }


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    resp = client.post(
        "/login/",
        data={"identifier": username, "password": password, "login": "1"},
    )
    return resp.status_code == 302


def logout(client):
    client.get("/logout")


# --- login-only static pages --------------------------------------------
class TestStaticPages:
    def test_pages_render_for_logged_in_user(self, client, ids):
        logout(client)
        login(client, "mpstudent", "stud123")
        for path in ["/gallery", "/documentation", "/contact", "/finished-lab-infos/anything"]:
            resp = client.get(path)
            assert resp.status_code == 200, path

    def test_pages_require_login(self, client, ids):
        logout(client)
        resp = client.get("/gallery")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


# --- dashboard / index --------------------------------------------------
class TestIndex:
    def test_index_renders(self, client, ids):
        login(client, "mpstudent", "stud123")
        resp = client.get("/index")
        assert resp.status_code == 200

    def test_feedback_modal_triggers_for_old_user_without_feedback(self, client, ids):
        key = f"feedback_prompt_last_shown_{ids['old_user_id']}"
        cache.delete(key)
        login(client, "mpolduser", "old123")
        resp = client.get("/index")
        assert resp.status_code == 200
        # the modal path stamps this cache key when it decides to show
        assert cache.get(key) is not None

    def test_feedback_modal_skipped_for_user_with_feedback(self, client, ids):
        key = f"feedback_prompt_last_shown_{ids['fb_user_id']}"
        cache.delete(key)
        login(client, "mpfbuser", "fb123")
        resp = client.get("/index")
        assert resp.status_code == 200
        assert cache.get(key) is None
        logout(client)


# --- feedback listing ---------------------------------------------------
class TestFeedbackView:
    def test_admin_sees_hidden_and_visible(self, client, ids):
        login(client, "mpadmin", "admin123")
        resp = client.get("/feedback_view")
        assert resp.status_code == 200
        assert b"HIDDEN-FB-mp" in resp.data
        assert b"VISIBLE-FB-mp" in resp.data
        logout(client)

    def test_non_admin_only_sees_visible(self, client, ids):
        login(client, "mpstudent", "stud123")
        resp = client.get("/feedback_view")
        assert resp.status_code == 200
        assert b"VISIBLE-FB-mp" in resp.data
        assert b"HIDDEN-FB-mp" not in resp.data
        logout(client)


# --- hide/unhide feedback (admin-only) ----------------------------------
class TestHideFeedback:
    def test_non_admin_is_rejected(self, client, ids):
        login(client, "mpstudent", "stud123")
        resp = client.post("/feedback/hide", data={"feedback_id": ids["visible_fb_id"], "action": "hide"})
        assert b"Unauthorized request" in resp.data
        logout(client)

    def test_missing_params_redirect_without_change(self, client, ids):
        login(client, "mpadmin", "admin123")
        resp = client.post("/feedback/hide", data={"action": "hide"})
        assert resp.status_code == 302
        fb = db.session.get(UserFeedbacks, ids["visible_fb_id"])
        assert fb.is_hidden is False

    def test_admin_can_hide_and_unhide(self, client, ids):
        resp = client.post("/feedback/hide", data={"feedback_id": ids["visible_fb_id"], "action": "hide"})
        assert resp.status_code == 302
        fb = db.session.get(UserFeedbacks, ids["visible_fb_id"])
        assert fb.is_hidden is True

        resp = client.post("/feedback/hide", data={"feedback_id": ids["visible_fb_id"], "action": "unhide"})
        assert resp.status_code == 302
        db.session.refresh(fb)
        assert fb.is_hidden is False
        logout(client)

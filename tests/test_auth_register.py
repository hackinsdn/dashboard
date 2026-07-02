"""Pytest suite for the account registration flow.

Mirrors tests/test_email_required.py: covers register, confirm_page and
resend_code in apps/authentication/routes.py - form validation, duplicate
username/email rejection, the no-MAIL_SERVER fast path (user created directly),
the token-confirmation path (user stashed in session until confirmed), and the
wrong/expired token branches. No real email is ever sent.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_auth_register.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "ar" so they never collide with the
other test modules that share the same singleton app/database (apps/config.py
reads DATA_DIR once at import time, so every module ends up on one DB).
"""
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

# stub the clabernetes controller (needs a 'clabverter' binary at import time)
_fake_clabernetes = types.ModuleType("apps.controllers.clabernetes")


class _StubC9sController:
    def __getattr__(self, name):
        raise NotImplementedError("clabernetes stub - not needed for these tests")


_fake_clabernetes.C9sController = _StubC9sController
sys.modules["apps.controllers.clabernetes"] = _fake_clabernetes

from run import app as flask_app  # noqa: E402
from apps import db  # noqa: E402
from apps.authentication import routes as auth_routes  # noqa: E402
from apps.authentication.models import Users  # noqa: E402
from apps.audit_mixin import utcnow  # noqa: E402

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
    existing = Users(username="arexisting", password="pass123", email="arexisting@example.com", category="student")
    db.session.add(existing)
    db.session.commit()
    return {"existing_id": existing.id}


@pytest.fixture
def no_mail_server(monkeypatch):
    """Force the no-MAIL_SERVER fast path regardless of ambient env vars."""
    monkeypatch.setitem(flask_app.config, "MAIL_SERVER", "")


@pytest.fixture
def mail_outbox(monkeypatch):
    """Force the token path and capture sent messages instead of hitting the network."""
    outbox = []
    monkeypatch.setitem(flask_app.config, "MAIL_SERVER", "smtp.test.local")
    monkeypatch.setitem(flask_app.config, "MAIL_USERNAME", "noreply@test.local")
    monkeypatch.setattr(auth_routes.mail, "send", lambda msg: outbox.append(msg))
    return outbox


# --- helpers -----------------------------------------------------------
def register_form(**overrides):
    data = {"username": "", "email": "", "password": "pass123", "terms": "y"}
    data.update(overrides)
    return data


def get_session_value(client, key):
    with client.session_transaction() as sess:
        return sess.get(key)


def seed_pending(client, username, email, token="123456", when=None):
    """Populate the session as register()'s token path would."""
    with client.session_transaction() as sess:
        sess["confirmation_token"] = token
        sess["user"] = {"username": username, "email": email, "password": "pass123", "issuer": "LOCAL"}
        sess["datetime"] = when or utcnow()


def clear_pending(client):
    with client.session_transaction() as sess:
        sess.pop("confirmation_token", None)
        sess.pop("user", None)
        sess.pop("datetime", None)


# --- register -----------------------------------------------------------
class TestRegister:
    def test_get_form(self, client, ids):
        resp = client.get("/register")
        assert resp.status_code == 200

    def test_invalid_form_missing_terms(self, client, ids):
        resp = client.post("/register", data=register_form(username="arnope", email="arnope@example.com", terms=""))
        assert b"Failed to validate form" in resp.data

    def test_duplicate_username(self, client, ids):
        resp = client.post("/register", data=register_form(username="arexisting", email="fresh@example.com"))
        assert b"Username already registered" in resp.data

    def test_duplicate_email(self, client, ids):
        resp = client.post("/register", data=register_form(username="arbrandnew", email="arexisting@example.com"))
        assert b"Email already registered" in resp.data

    def test_fast_path_creates_user(self, client, ids, no_mail_server):
        resp = client.post("/register", data=register_form(username="arfast", email="arfast@example.com"))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

        user = Users.query.filter_by(username="arfast").first()
        assert user is not None
        assert user.email == "arfast@example.com"

    def test_token_path_stashes_user_and_sends_mail(self, client, ids, mail_outbox):
        resp = client.post("/register", data=register_form(username="artoken", email="artoken@example.com"))
        assert resp.status_code == 302
        assert "/confirm" in resp.headers["Location"]
        assert len(mail_outbox) == 1
        # user is not created until the token is confirmed
        assert Users.query.filter_by(username="artoken").first() is None


# --- confirm_page -------------------------------------------------------
class TestConfirmPage:
    def test_no_pending_registration_redirects_to_register(self, client, ids):
        clear_pending(client)
        resp = client.get("/confirm")
        assert resp.status_code == 302
        assert "/register" in resp.headers["Location"]

    def test_wrong_token_is_rejected(self, client, ids):
        seed_pending(client, "arconfa", "arconfa@test.local", token="654321")
        resp = client.post("/confirm", data={"confirmation_token": "000000", "confirm": "1"})
        assert b"Invalid token" in resp.data
        assert Users.query.filter_by(username="arconfa").first() is None

    def test_expired_token_is_rejected(self, client, ids):
        seed_pending(client, "arconfb", "arconfb@test.local", token="111111", when=utcnow() - timedelta(minutes=30))
        resp = client.post("/confirm", data={"confirmation_token": "111111", "confirm": "1"})
        assert b"Token expired" in resp.data
        assert Users.query.filter_by(username="arconfb").first() is None

    def test_correct_token_creates_user(self, client, ids):
        seed_pending(client, "arconfc", "arconfc@test.local", token="222222")
        resp = client.post("/confirm", data={"confirmation_token": "222222", "confirm": "1"})
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

        assert Users.query.filter_by(username="arconfc").first() is not None
        assert get_session_value(client, "confirmation_token") is None


# --- resend_code --------------------------------------------------------
class TestResendCode:
    def test_no_pending_registration_redirects_with_error(self, client, ids):
        clear_pending(client)
        resp = client.get("/resend-code")
        assert resp.status_code == 302
        assert "/confirm" in resp.headers["Location"]
        assert get_session_value(client, "error_msg")

    def test_resend_sends_mail(self, client, ids, mail_outbox):
        seed_pending(client, "arresend", "arresend@test.local", token="333333")
        resp = client.get("/resend-code")
        assert resp.status_code == 302
        assert "/confirm" in resp.headers["Location"]
        assert len(mail_outbox) == 1

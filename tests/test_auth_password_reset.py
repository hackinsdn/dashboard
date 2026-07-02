"""Pytest suite for the password-reset flow.

Mirrors tests/test_email_required.py: covers reset_password and
confirm_reset_password in apps/authentication/routes.py - form validation, the
no-user-enumeration generic response, the mail send, and the token-confirmation
branches (invalid/expired token, mismatched passwords, deleted user, success).
The reset token is seeded straight into the cache so confirm_reset_password can
be exercised without parsing the (random, email-embedded) token. No real email
is ever sent.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_auth_password_reset.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "ap" so they never collide with the
other test modules that share the same singleton app/database (apps/config.py
reads DATA_DIR once at import time, so every module ends up on one DB).
"""
import os
import sys
import tempfile
import types

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
from apps.authentication import routes as auth_routes  # noqa: E402
from apps.authentication.models import Users  # noqa: E402

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
    user = Users(username="apuser", password="pass123", email="apuser@example.com", category="student")
    deleted = Users(username="apdeleted", password="pass123", email="apdeleted@example.com", category="student", is_deleted=True)
    db.session.add_all([user, deleted])
    db.session.commit()
    return {"user_id": user.id, "deleted_id": deleted.id}


@pytest.fixture
def mail_outbox(monkeypatch):
    outbox = []
    monkeypatch.setitem(flask_app.config, "MAIL_SERVER", "smtp.test.local")
    monkeypatch.setitem(flask_app.config, "MAIL_USERNAME", "noreply@test.local")
    monkeypatch.setattr(auth_routes.mail, "send", lambda msg: outbox.append(msg))
    return outbox


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    resp = client.post(
        "/login/",
        data={"identifier": username, "password": password, "login": "1"},
    )
    return resp.status_code == 302


def logout(client):
    client.get("/logout")


# --- reset_password -----------------------------------------------------
class TestResetPassword:
    def test_get_renders_form(self, client, ids):
        resp = client.get("/reset-password")
        assert resp.status_code == 200

    def test_missing_identifier_is_rejected(self, client, ids):
        resp = client.post("/reset-password", data={})
        assert b"Errors validating form" in resp.data

    def test_unknown_user_gets_generic_message(self, client, ids):
        resp = client.post("/reset-password", data={"identifier": "nobody-here"})
        assert resp.status_code == 200
        assert b"If the user provided is valid" in resp.data

    def test_valid_user_sends_mail(self, client, ids, mail_outbox):
        resp = client.post("/reset-password", data={"identifier": "apuser"})
        assert resp.status_code == 200
        assert b"If the user provided is valid" in resp.data
        assert len(mail_outbox) == 1


# --- confirm_reset_password ---------------------------------------------
class TestConfirmResetPassword:
    def test_invalid_token_is_rejected(self, client, ids):
        resp = client.get("/confirm-reset-password/not-a-real-token")
        assert b"Invalid or expired token" in resp.data

    def test_mismatched_passwords_are_rejected(self, client, ids):
        cache.set("resetpw-tok-mismatch", ids["user_id"], timeout=3600)
        resp = client.post(
            "/confirm-reset-password/tok-mismatch",
            data={"password": "newpass123", "password_confirm": "different123"},
        )
        assert b"Passwords must match" in resp.data

    def test_deleted_user_is_rejected(self, client, ids):
        cache.set("resetpw-tok-deleted", ids["deleted_id"], timeout=3600)
        resp = client.post(
            "/confirm-reset-password/tok-deleted",
            data={"password": "newpass123", "password_confirm": "newpass123"},
        )
        assert b"Invalid password reset request" in resp.data

    def test_valid_token_resets_password(self, client, ids):
        cache.set("resetpw-tok-ok", ids["user_id"], timeout=3600)
        resp = client.post(
            "/confirm-reset-password/tok-ok",
            data={"password": "brandnew123", "password_confirm": "brandnew123"},
        )
        assert b"Password changed successfully" in resp.data
        assert cache.get("resetpw-tok-ok") is None

        logout(client)
        assert login(client, "apuser", "brandnew123")
        logout(client)
        assert not login(client, "apuser", "pass123")

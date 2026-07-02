"""Pytest suite for the "require e-mail after login" feature.

Exercises the same scenarios that were checked manually while building the
feature: login redirects to /email/required when Users.email is empty,
the same guard on the OAuth callback() path (which creates the Users row
itself, unlike local login), next_url is preserved through the detour, the
no-MAIL_SERVER fast path sets the email directly, duplicate emails are
rejected, a user who already has an email is bounced straight through (not
looped back into the flow), the full token-confirmation path (including
resend and expiry), and the Logout escape hatch is present on both new
pages.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3). No real email is ever sent: MAIL_SERVER is either
left falsy (default fast path) or monkeypatched together with
apps.authentication.routes.mail.send for the confirmation-path tests.

Usage:
    pip install -r requirements-test.txt
    pytest tests/test_email_required.py -v
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
# Must happen before `apps`/`run` are imported below, since apps/config.py
# reads DATA_DIR at import time.
TEST_DATA_DIR = tempfile.mkdtemp(prefix="hackinsdn_test_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("OPTIONAL_MODULES", "")

# apps/controllers/clabernetes.py hard-requires a 'clabverter' binary to be
# installed on disk at *import* time. That requirement is unrelated to this
# feature, so stub the module out in sys.modules before anything imports
# apps.controllers - this avoids needing that binary just to run these tests.
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
        db.drop_all()


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def ids(app):
    """Seed one dedicated user per scenario, so tests don't interfere."""
    users = {
        "taken_email": Users(username="takenemail", password="pass123", email="taken@example.com"),
        "login_redirect": Users(username="loginredirect", password="pass123", email=""),
        "nexturl": Users(username="nexturluser", password="pass123", email=""),
        "fastpath": Users(username="fastpathuser", password="pass123", email=""),
        "duplicate": Users(username="dupuser", password="pass123", email=""),
        "stale_next": Users(username="stalenextuser", password="pass123", email="stale1@example.com"),
        "stale_default": Users(username="staledefaultuser", password="pass123", email="stale2@example.com"),
        "token_flow": Users(username="tokenflowuser", password="pass123", email=""),
        "expired": Users(username="expireduser", password="pass123", email=""),
    }
    db.session.add_all(users.values())
    db.session.commit()
    return {name: user.id for name, user in users.items()}


@pytest.fixture
def no_mail_server(monkeypatch):
    """Force the no-MAIL_SERVER fast path regardless of ambient env vars."""
    monkeypatch.setitem(flask_app.config, "MAIL_SERVER", "")


@pytest.fixture
def mail_outbox(monkeypatch):
    """Force the token-confirmation path and capture sent messages instead
    of hitting the network."""
    outbox = []
    monkeypatch.setitem(flask_app.config, "MAIL_SERVER", "smtp.test.local")
    monkeypatch.setitem(flask_app.config, "MAIL_USERNAME", "noreply@test.local")
    monkeypatch.setattr(auth_routes.mail, "send", lambda msg: outbox.append(msg))
    return outbox


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    return client.post(
        "/login/",
        data={"identifier": username, "password": password, "login": "1"},
    )


def logout(client):
    client.get("/logout")


def get_session_value(client, key):
    with client.session_transaction() as sess:
        return sess.get(key)


def set_session_value(client, key, value):
    with client.session_transaction() as sess:
        sess[key] = value


# --- login redirect -------------------------------------------------------
class TestLoginRedirectsWhenEmailMissing:
    def test_login_redirects_to_email_required(self, client, ids):
        logout(client)
        resp = login(client, "loginredirect", "pass123")
        assert resp.status_code == 302
        assert "/email/required" in resp.headers["Location"]

    def test_email_required_form_is_shown_with_logout_link(self, client, ids):
        resp = client.get("/email/required")
        assert resp.status_code == 200
        assert b"email_require" in resp.data
        assert b'href="/logout"' in resp.data
        logout(client)


# --- OAuth callback with a missing e-mail claim ----------------------------
class TestOAuthCallbackWhenEmailMissing:
    """OAuth providers sometimes don't return an email claim at all
    (token.get("email") is None) - callback() creates the Users row itself
    rather than going through register(), so this exercises that path
    independently of the local-login tests above."""

    def test_oauth_callback_redirects_then_completes_email_required_flow(
        self, client, monkeypatch, no_mail_server
    ):
        logout(client)
        monkeypatch.setattr(
            auth_routes.oauth.provider,
            "authorize_access_token",
            lambda *a, **kw: {
                "userinfo": {
                    "sub": "oauth-noemail-sub",
                    "iss": "https://idp.example.com",
                    "given_name": "OAuth",
                    "family_name": "NoEmail",
                    "email": None,
                }
            },
        )

        resp = client.get("/login/callback")
        assert resp.status_code == 302
        assert "/email/required" in resp.headers["Location"]

        user = Users.query.filter_by(subject="oauth-noemail-sub").first()
        assert user is not None
        assert not user.email

        # same email_required.html/confirm flow local-login users go
        # through - no special-casing for OAuth-created accounts
        resp = client.post(
            "/email/required",
            data={"email": "oauthnoemail@example.com", "submit_email": "1"},
        )
        assert resp.status_code == 302
        assert "/email/required" not in resp.headers["Location"]

        db.session.refresh(user)
        assert user.email == "oauthnoemail@example.com"
        logout(client)


# --- next_url preserved through the detour --------------------------------
class TestNextUrlPreservedThroughEmailFlow:
    def test_next_url_survives_login_and_is_used_after_email_is_set(self, client, ids, no_mail_server):
        logout(client)
        set_session_value(client, "next_url", "/index")

        resp = login(client, "nexturluser", "pass123")
        assert resp.status_code == 302
        assert "/email/required" in resp.headers["Location"]
        # next_url must still be sitting in the session for later
        assert get_session_value(client, "next_url") == "/index"

        resp = client.post(
            "/email/required",
            data={"email": "nexturluser@example.com", "submit_email": "1"},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/index")
        assert get_session_value(client, "next_url") is None

        user = db.session.get(Users, ids["nexturl"])
        assert user.email == "nexturluser@example.com"
        logout(client)


# --- no-MAIL_SERVER fast path ---------------------------------------------
class TestNoMailServerFastPath:
    def test_submitting_email_sets_it_directly_without_confirmation(self, client, ids, no_mail_server):
        logout(client)
        login(client, "fastpathuser", "pass123")

        resp = client.post(
            "/email/required",
            data={"email": "fastpathuser@example.com", "submit_email": "1"},
        )
        assert resp.status_code == 302
        # no next_url was stashed -> falls back to the default route
        assert "/email/required" not in resp.headers["Location"]

        user = db.session.get(Users, ids["fastpath"])
        assert user.email == "fastpathuser@example.com"
        logout(client)

    def test_duplicate_email_is_rejected(self, client, ids, no_mail_server):
        logout(client)
        login(client, "dupuser", "pass123")

        resp = client.post(
            "/email/required",
            data={"email": "taken@example.com", "submit_email": "1"},
        )
        assert resp.status_code == 200
        assert b"already registered" in resp.data

        user = db.session.get(Users, ids["duplicate"])
        assert user.email == ""
        logout(client)


# --- stale visits once the email is already set --------------------------
class TestStaleVisitAfterEmailAlreadySet:
    def test_redirects_to_next_url_if_present(self, client, ids):
        logout(client)
        # email is already set, so login() itself would consume/redirect on
        # any next_url already in session - stash it *after* login instead,
        # simulating a next_url captured later (e.g. by the
        # @login_required guard on some other page), to isolate
        # require_email()'s own redirect branch.
        login(client, "stalenextuser", "pass123")
        set_session_value(client, "next_url", "/index")

        resp = client.get("/email/required")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/index")
        assert get_session_value(client, "next_url") is None
        logout(client)

    def test_redirects_to_default_route_otherwise(self, client, ids):
        logout(client)
        login(client, "staledefaultuser", "pass123")

        resp = client.get("/email/required")
        assert resp.status_code == 302
        assert "/email/required" not in resp.headers["Location"]
        logout(client)


# --- full token-confirmation path -----------------------------------------
class TestTokenConfirmationPath:
    def test_full_flow_with_wrong_token_resend_and_success(self, client, ids, mail_outbox):
        logout(client)
        login(client, "tokenflowuser", "pass123")

        resp = client.post(
            "/email/required",
            data={"email": "tokenflowuser@example.com", "submit_email": "1"},
        )
        assert resp.status_code == 302
        assert "/email/required/confirm" in resp.headers["Location"]
        assert len(mail_outbox) == 1

        user = db.session.get(Users, ids["token_flow"])
        assert user.email == ""

        resp = client.get("/email/required/confirm")
        assert resp.status_code == 200
        assert b'href="/logout"' in resp.data

        # wrong token
        resp = client.post(
            "/email/required/confirm",
            data={"confirmation_token": "000000", "confirm": "1"},
        )
        assert resp.status_code == 200
        assert b"Invalid token" in resp.data
        db.session.refresh(user)
        assert user.email == ""

        # resend re-sends the same token
        token_before = get_session_value(client, "email_confirmation_token")
        resp = client.get("/email/required/resend-code")
        assert resp.status_code == 302
        assert len(mail_outbox) == 2
        assert get_session_value(client, "email_confirmation_token") == token_before

        # correct token
        resp = client.post(
            "/email/required/confirm",
            data={"confirmation_token": token_before, "confirm": "1"},
        )
        assert resp.status_code == 302
        assert "/email/required" not in resp.headers["Location"]
        assert get_session_value(client, "email_confirmation_token") is None

        db.session.refresh(user)
        assert user.email == "tokenflowuser@example.com"
        logout(client)

    def test_expired_token_is_rejected(self, client, ids, mail_outbox):
        logout(client)
        login(client, "expireduser", "pass123")

        client.post(
            "/email/required",
            data={"email": "expireduser@example.com", "submit_email": "1"},
        )
        token = get_session_value(client, "email_confirmation_token")
        set_session_value(client, "email_datetime", utcnow() - timedelta(minutes=20))

        resp = client.post(
            "/email/required/confirm",
            data={"confirmation_token": token, "confirm": "1"},
        )
        assert resp.status_code == 200
        assert b"Token expired" in resp.data

        user = db.session.get(Users, ids["expired"])
        assert user.email == ""
        logout(client)

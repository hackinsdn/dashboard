"""Pytest suite for the login/session auth routes.

Mirrors tests/test_email_required.py: covers the branches of
apps/authentication/routes.py not touched by the e-mail suite - route_default,
the login GET/bad-password/already-authenticated paths, login_oauth,
reload_profile, logout, and the unauthorized_handler next_url capture.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_auth_session.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "as" so they never collide with the
other test modules that share the same singleton app/database (apps/config.py
reads DATA_DIR once at import time, so every module ends up on one DB).
"""
import os
import sys
import tempfile
import types

import pytest
from flask import redirect

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
from apps.authentication.models import Users, LoginLogging  # noqa: E402

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
    user = Users(username="asuser", password="pass123", email="asuser@example.com", category="student")
    db.session.add(user)
    db.session.commit()
    return {"user_id": user.id}


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


# --- route_default & login ----------------------------------------------
class TestRouteDefaultAndLogin:
    def test_route_default_redirects_to_login(self, client, ids):
        logout(client)
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_login_get_renders_form(self, client, ids):
        resp = client.get("/login/")
        assert resp.status_code == 200

    def test_bad_password_is_rejected_and_logged(self, client, ids):
        resp = login(client, "asuser", "wrongpass")
        assert resp.status_code == 200
        assert b"Wrong user or password" in resp.data
        assert LoginLogging.query.filter_by(login="asuser", success=False).first() is not None

    def test_login_get_while_authenticated_redirects_home(self, client, ids):
        assert login(client, "asuser", "pass123").status_code == 302
        resp = client.get("/login/")
        assert resp.status_code == 302
        assert "/index" in resp.headers["Location"]
        logout(client)


# --- login_oauth --------------------------------------------------------
class TestLoginOauth:
    def test_oauth_login_redirects_to_provider(self, client, ids, monkeypatch):
        monkeypatch.setattr(
            auth_routes.oauth.provider,
            "authorize_redirect",
            lambda *a, **k: redirect("https://idp.example.com/authorize"),
        )
        resp = client.get("/login/oauth")
        assert resp.status_code == 302
        assert "idp.example.com" in resp.headers["Location"]


# --- reload_profile & logout --------------------------------------------
class TestReloadProfileAndLogout:
    def test_reload_profile_redirects_to_default(self, client, ids):
        login(client, "asuser", "pass123")
        resp = client.get("/profile/reload")
        assert resp.status_code == 302

    def test_logout_clears_session(self, client, ids):
        login(client, "asuser", "pass123")
        resp = client.get("/logout")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        # now a protected page bounces back to login
        resp = client.get("/index")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


# --- unauthorized_handler -----------------------------------------------
class TestUnauthorizedHandler:
    def test_protected_page_stashes_next_url(self, client, ids):
        logout(client)
        resp = client.get("/index")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        assert get_session_value(client, "next_url") == "/index"

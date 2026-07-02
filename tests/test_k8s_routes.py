"""Pytest suite for apps/k8s/routes.py.

Covers the three admin-only listing routes (/k8s/pods, /k8s/deployments,
/k8s/services): role gating, the success render, and the swallow-and-render
behaviour when the controller raises. The controller calls are monkeypatched.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_k8s_routes.py -v

The seeded usernames are prefixed "kr" so they never collide with the other
test modules that share the same singleton app/database.
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
from apps import db  # noqa: E402
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
    admin = Users(username="kradmin", password="admin123", email="kradmin@test.local", category="admin")
    teacher = Users(username="krteacher", password="teach123", email="krteacher@test.local", category="teacher")
    db.session.add_all([admin, teacher])
    db.session.commit()
    return {"admin_id": admin.id, "teacher_id": teacher.id}


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    resp = client.post(
        "/login/",
        data={"identifier": username, "password": password, "login": "1"},
    )
    return resp.status_code == 302


def logout(client):
    client.get("/logout")


ROUTES = [
    ("/k8s/pods", "list_pods"),
    ("/k8s/deployments", "list_deployments"),
    ("/k8s/services", "list_services"),
]


# --- role gating --------------------------------------------------------
class TestRoleGating:
    def test_non_admin_is_rejected(self, client, ids):
        logout(client)
        login(client, "krteacher", "teach123")
        for path, _ in ROUTES:
            resp = client.get(path)
            assert b"Unauthorized request" in resp.data, path
        logout(client)


# --- admin success + error swallow --------------------------------------
class TestAdminAccess:
    def test_admin_success(self, client, ids, monkeypatch):
        login(client, "kradmin", "admin123")
        for path, fn in ROUTES:
            monkeypatch.setattr(f"apps.k8s.routes.k8s.{fn}", lambda: [])
            resp = client.get(path)
            assert resp.status_code == 200, path

    def test_controller_error_is_swallowed(self, client, ids, monkeypatch):
        def boom():
            raise RuntimeError("cluster down")

        for path, fn in ROUTES:
            monkeypatch.setattr(f"apps.k8s.routes.k8s.{fn}", boom)
            resp = client.get(path)
            assert resp.status_code == 200, path  # error is logged, page still renders
        logout(client)

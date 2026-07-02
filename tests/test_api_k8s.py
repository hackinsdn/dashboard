"""Pytest suite for the k8s/git-backed /api endpoints.

Mirrors tests/test_lab_categories.py: covers get_pods, get_lab_status,
delete_lab, get_nodes, list_kubernetes_templates and get_kubernetes_template in
apps/api/routes.py. Only the happy paths touch Kubernetes/git, so those are
covered by monkeypatching the `k8s`/`git` modules used by the routes; every
role/ownership/validation branch returns before any external call.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_api_k8s.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "ak" so they never collide with the
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
from apps import db  # noqa: E402
from apps.authentication.models import Users  # noqa: E402
from apps.home.models import Labs, LabInstances  # noqa: E402

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
    admin = Users(username="akadmin", password="admin123", email="akadmin@test.local", category="admin")
    student = Users(username="akstudent", password="stud123", email="akstudent@test.local", category="student")
    other = Users(username="akother", password="stud123", email="akother@test.local", category="student")
    pending = Users(username="akpending", password="pend123", email="akpending@test.local", category="user")
    db.session.add_all([admin, student, other, pending])
    db.session.commit()

    lab = Labs(title="Ak Lab", description="lab for k8s api")
    db.session.add(lab)
    db.session.commit()
    inst = LabInstances(user_id=student.id, lab_id=lab.id, is_deleted=False)
    inst.k8s_resources = []
    inst_del = LabInstances(user_id=student.id, lab_id=lab.id, is_deleted=False)
    inst_del.k8s_resources = []
    db.session.add_all([inst, inst_del])
    db.session.commit()

    return {
        "admin_id": admin.id,
        "student_id": student.id,
        "other_id": other.id,
        "lab_id": lab.id,
        "inst_id": inst.id,
        "inst_del_id": inst_del.id,
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


# --- get_pods -----------------------------------------------------------
class TestGetPods:
    def test_unapproved_user_is_rejected(self, client, ids):
        logout(client)
        login(client, "akpending", "pend123")
        resp = client.get("/api/pods/somelab")
        assert resp.status_code == 404
        logout(client)

    def test_missing_auth_header_is_rejected(self, client, ids):
        login(client, "akstudent", "stud123")
        resp = client.get("/api/pods/somelab")
        assert resp.status_code == 400

    def test_invalid_token_is_rejected(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.k8s.validate_token", lambda t: False)
        resp = client.get("/api/pods/somelab", headers={"Authorization": "Bearer bad"})
        assert resp.status_code == 404

    def test_valid_token_returns_pods(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.k8s.validate_token", lambda t: True)
        monkeypatch.setattr("apps.api.routes.k8s.get_pods_by_lab_id", lambda lab_id: [{"name": "p1"}])
        resp = client.get("/api/pods/somelab", headers={"Authorization": "Bearer good"})
        assert resp.status_code == 200
        logout(client)


# --- get_lab_status -----------------------------------------------------
class TestGetLabStatus:
    def test_unapproved_user_is_rejected(self, client, ids):
        login(client, "akpending", "pend123")
        resp = client.get(f"/api/lab/status/{ids['inst_id']}")
        assert resp.status_code == 404
        logout(client)

    def test_missing_instance(self, client, ids):
        login(client, "akstudent", "stud123")
        resp = client.get("/api/lab/status/does-not-exist")
        assert resp.status_code == 404

    def test_non_owner_is_rejected(self, client, ids):
        logout(client)
        login(client, "akother", "stud123")
        resp = client.get(f"/api/lab/status/{ids['inst_id']}")
        assert resp.status_code == 401
        logout(client)

    def test_owner_gets_status(self, client, ids, monkeypatch):
        monkeypatch.setattr(
            "apps.api.routes.k8s.get_resources_by_name",
            lambda res: [{"kind": "pod", "metadata": {"name": "p1"}, "is_ok": True}],
        )
        login(client, "akstudent", "stud123")
        resp = client.get(f"/api/lab/status/{ids['inst_id']}")
        assert resp.status_code == 200
        logout(client)


# --- delete_lab ---------------------------------------------------------
class TestDeleteLab:
    def test_unapproved_user_is_rejected(self, client, ids):
        login(client, "akpending", "pend123")
        resp = client.delete(f"/api/lab/{ids['inst_del_id']}")
        assert resp.status_code == 404
        logout(client)

    def test_missing_instance(self, client, ids):
        login(client, "akstudent", "stud123")
        resp = client.delete("/api/lab/does-not-exist")
        assert resp.status_code == 404

    def test_non_owner_is_rejected(self, client, ids):
        logout(client)
        login(client, "akother", "stud123")
        resp = client.delete(f"/api/lab/{ids['inst_del_id']}")
        assert resp.status_code == 401
        logout(client)

    def test_owner_can_delete(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.k8s.delete_resources_by_name", lambda res: [])
        login(client, "akstudent", "stud123")
        resp = client.delete(f"/api/lab/{ids['inst_del_id']}")
        assert resp.status_code == 200

        inst = db.session.get(LabInstances, ids["inst_del_id"])
        assert inst.is_deleted is True
        logout(client)


# --- get_nodes ----------------------------------------------------------
class TestGetNodes:
    def test_unapproved_user_is_rejected(self, client, ids):
        login(client, "akpending", "pend123")
        resp = client.get("/api/nodes")
        assert resp.status_code == 404
        logout(client)

    def test_returns_nodes(self, client, ids, monkeypatch):
        monkeypatch.setattr(
            "apps.api.routes.k8s.get_nodes",
            lambda: [{"name": "n1", "latitude": 1.0, "longitude": 2.0, "status": "Ready"}],
        )
        login(client, "akstudent", "stud123")
        resp = client.get("/api/nodes")
        assert resp.status_code == 200
        logout(client)


# --- list_kubernetes_templates (public) ---------------------------------
class TestListTemplates:
    def test_not_defined_when_no_git_url(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "LAB_TEMPLATES_GIT_URL", "")
        resp = client.get("/api/templates/list")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "not-defined"

    def test_lists_templates(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.git.update_repo", lambda *a, **k: None)
        monkeypatch.setattr("apps.api.routes.git.list_files", lambda *a, **k: ["a.yaml", "sub/b.yaml"])
        resp = client.get("/api/templates/list")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == ["a", "sub/b"]

    def test_failure_is_reported(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.git.update_repo", lambda *a, **k: None)
        def boom(*a, **k):
            raise RuntimeError("git blew up")
        monkeypatch.setattr("apps.api.routes.git.list_files", boom)
        resp = client.get("/api/templates/list")
        assert resp.status_code == 400


# --- get_kubernetes_template --------------------------------------------
class TestGetTemplate:
    def test_non_staff_is_rejected(self, client, ids):
        login(client, "akstudent", "stud123")
        resp = client.get("/api/templates/foo")
        assert b"Unauthorized request" in resp.data
        logout(client)

    def test_admin_gets_template(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.git.get_file", lambda d, f: (True, "yaml-content"))
        login(client, "akadmin", "admin123")
        resp = client.get("/api/templates/foo")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == "yaml-content"

    def test_admin_missing_template_is_reported(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.git.get_file", lambda d, f: (False, "not found"))
        resp = client.get("/api/templates/foo")
        assert resp.status_code == 400
        logout(client)

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


def _service_resource(name="web", port=8080, name_str="http", node_port=30080,
                      is_ok=True, created="now"):
    """A Service resource dict shaped like k8s.get_resources_by_name() output.

    ``created`` defaults to "now" (a fresh, tz-aware creation_timestamp); pass a
    datetime to control the proxy-wait deadline, or None to omit it.
    """
    from datetime import datetime, timezone
    if created == "now":
        created = datetime.now(timezone.utc)
    return {
        "kind": "Service",
        "metadata": {"name": name, "creation_timestamp": created},
        "spec": {"ports": [{"name": name_str, "port": port, "node_port": node_port}]},
        "is_ok": is_ok,
    }


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

    def test_no_proxy_probe_when_domain_unset(self, client, ids, monkeypatch):
        # Service present, but PROXY_DOMAIN empty -> no proxy entries, no probe
        monkeypatch.setitem(flask_app.config, "PROXY_DOMAIN", "")
        monkeypatch.setattr(
            "apps.api.routes.k8s.get_resources_by_name",
            lambda res: [_service_resource(is_ok=True)],
        )
        called = []
        monkeypatch.setattr(
            "apps.api.routes._probe_proxy_vhost",
            lambda url, timeout: called.append(url) or True,
        )
        login(client, "akstudent", "stud123")
        resp = client.get(f"/api/lab/status/{ids['inst_id']}")
        assert resp.status_code == 200
        names = [r["name"] for r in resp.get_json()["result"]]
        assert not any(n.startswith("Proxy__") for n in names)
        assert called == []
        logout(client)

    def test_proxy_probe_reports_reachable_vhost(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "PROXY_DOMAIN", "labs.example.com")
        monkeypatch.setitem(flask_app.config, "PROXY_PROBE_MAX_WAIT", 30)
        monkeypatch.setattr(
            "apps.api.routes.k8s.get_resources_by_name",
            lambda res: [_service_resource(is_ok=True)],
        )
        monkeypatch.setattr(
            "apps.api.routes._probe_proxy_vhost", lambda url, timeout: True
        )
        login(client, "akstudent", "stud123")
        resp = client.get(f"/api/lab/status/{ids['inst_id']}")
        assert resp.status_code == 200
        result = {r["name"]: r["status"] for r in resp.get_json()["result"]}
        assert result["Proxy__8080-web.labs.example.com"] == "ok"
        logout(client)

    def test_proxy_probe_reports_unreachable_vhost(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "PROXY_DOMAIN", "labs.example.com")
        # keep the wait tiny so the test does not block for the full budget
        monkeypatch.setitem(flask_app.config, "PROXY_PROBE_MAX_WAIT", 1)
        monkeypatch.setattr(
            "apps.api.routes.k8s.get_resources_by_name",
            lambda res: [_service_resource(is_ok=True)],
        )
        monkeypatch.setattr(
            "apps.api.routes._probe_proxy_vhost", lambda url, timeout: False
        )
        login(client, "akstudent", "stud123")
        resp = client.get(f"/api/lab/status/{ids['inst_id']}")
        assert resp.status_code == 200
        result = {r["name"]: r["status"] for r in resp.get_json()["result"]}
        assert result["Proxy__8080-web.labs.example.com"] == "not-ok"
        logout(client)

    def test_proxy_probe_gives_up_after_deadline(self, client, ids, monkeypatch):
        # unreachable vhost, but its Service was created long before the wait
        # window -> stop gating and report ok
        from datetime import datetime, timezone, timedelta
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        monkeypatch.setitem(flask_app.config, "PROXY_DOMAIN", "labs.example.com")
        monkeypatch.setitem(flask_app.config, "PROXY_PROBE_MAX_WAIT", 30)
        monkeypatch.setattr(
            "apps.api.routes.k8s.get_resources_by_name",
            lambda res: [_service_resource(is_ok=True, created=old)],
        )
        monkeypatch.setattr(
            "apps.api.routes._probe_proxy_vhost", lambda url, timeout: False
        )
        login(client, "akstudent", "stud123")
        resp = client.get(f"/api/lab/status/{ids['inst_id']}")
        assert resp.status_code == 200
        result = {r["name"]: r["status"] for r in resp.get_json()["result"]}
        assert result["Proxy__8080-web.labs.example.com"] == "ok"
        logout(client)

    def test_proxy_probe_skipped_while_resources_not_ready(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "PROXY_DOMAIN", "labs.example.com")
        monkeypatch.setattr(
            "apps.api.routes.k8s.get_resources_by_name",
            lambda res: [_service_resource(is_ok=False)],
        )
        called = []
        monkeypatch.setattr(
            "apps.api.routes._probe_proxy_vhost",
            lambda url, timeout: called.append(url) or True,
        )
        login(client, "akstudent", "stud123")
        resp = client.get(f"/api/lab/status/{ids['inst_id']}")
        assert resp.status_code == 200
        assert called == []  # no probe until every resource is ready
        logout(client)


# --- _build_proxy_urls --------------------------------------------------
class TestBuildProxyUrls:
    def test_only_httpish_nodeport_services_get_a_vhost(self):
        from datetime import datetime, timezone
        from apps.api.routes import _build_proxy_urls

        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        resources = [
            # http service with NodePort -> included, mapped to its creation time
            _service_resource(name="web", port=8080, name_str="http",
                              node_port=30080, created=created),
            # ssh service -> excluded (not http/https)
            _service_resource(name="jump", port=22, name_str="ssh", node_port=30022),
            # http service without a NodePort -> excluded
            {"kind": "Service", "metadata": {"name": "clusterip"},
             "spec": {"ports": [{"name": "http", "port": 80, "node_port": None}]}},
            # non-Service resource -> excluded
            {"kind": "Pod", "metadata": {"name": "p1"}, "spec": {}},
        ]
        urls = _build_proxy_urls(resources, "labs.example.com")
        assert urls == {"https://8080-web.labs.example.com": created}


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

    def test_partial_resource_removal_keeps_instance(self, client, ids, monkeypatch):
        # a resource left behind must NOT mark the instance deleted, so it can
        # be retried instead of being orphaned with no DB record.
        inst = LabInstances(user_id=ids["student_id"], lab_id=ids["lab_id"], is_deleted=False)
        inst.k8s_resources = [{"kind": "Deployment", "name": "d1"}, {"kind": "Service", "name": "s1"}]
        db.session.add(inst)
        db.session.commit()
        inst_id = inst.id
        monkeypatch.setattr("apps.api.routes.k8s.delete_resources_by_name", lambda res: [True, False])
        login(client, "akstudent", "stud123")
        resp = client.delete(f"/api/lab/{inst_id}")
        assert resp.status_code == 400
        assert resp.get_json()["status"] == "fail"
        assert "Service/s1=fail" in resp.get_json()["result"]
        assert db.session.get(LabInstances, inst_id).is_deleted is False
        logout(client)


# --- delete_labs (bulk) -------------------------------------------------
class TestDeleteLabsBulk:
    @pytest.fixture
    def bulk_ids(self, ids):
        """Two fresh running instances owned by akstudent, plus one by akother."""
        mine = [LabInstances(user_id=ids["student_id"], lab_id=ids["lab_id"], is_deleted=False) for _ in range(2)]
        theirs = LabInstances(user_id=ids["other_id"], lab_id=ids["lab_id"], is_deleted=False)
        for inst in mine + [theirs]:
            inst.k8s_resources = []
        db.session.add_all(mine + [theirs])
        db.session.commit()
        return {"mine": [i.id for i in mine], "theirs": theirs.id}

    def test_unapproved_user_is_rejected(self, client, bulk_ids):
        logout(client)
        login(client, "akpending", "pend123")
        resp = client.delete("/api/labs", json=bulk_ids["mine"])
        assert resp.status_code == 404
        logout(client)

    def test_invalid_payload_is_rejected(self, client, bulk_ids):
        login(client, "akstudent", "stud123")
        resp = client.delete("/api/labs", json={"nope": 1})
        assert resp.status_code == 400

    def test_missing_instance_aborts_whole_batch(self, client, bulk_ids):
        resp = client.delete("/api/labs", json=[bulk_ids["mine"][0], "does-not-exist"])
        assert resp.status_code == 400
        # nothing was deleted
        assert db.session.get(LabInstances, bulk_ids["mine"][0]).is_deleted is False

    def test_non_owner_is_rejected(self, client, bulk_ids):
        resp = client.delete("/api/labs", json=[bulk_ids["theirs"]])
        assert resp.status_code == 400
        assert db.session.get(LabInstances, bulk_ids["theirs"]).is_deleted is False
        logout(client)

    def test_owner_can_delete_many(self, client, bulk_ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.k8s.delete_resources_by_name", lambda res: [])
        login(client, "akstudent", "stud123")
        resp = client.delete("/api/labs", json=bulk_ids["mine"])
        assert resp.status_code == 200
        for inst_id in bulk_ids["mine"]:
            assert db.session.get(LabInstances, inst_id).is_deleted is True
        logout(client)

    def test_admin_can_delete_other_users_labs(self, client, bulk_ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.k8s.delete_resources_by_name", lambda res: [])
        login(client, "akadmin", "admin123")
        resp = client.delete("/api/labs", json=[bulk_ids["theirs"]])
        assert resp.status_code == 200
        inst = db.session.get(LabInstances, bulk_ids["theirs"])
        assert inst.is_deleted is True
        assert inst.finish_reason == "Finished by the admin"
        logout(client)

    def test_k8s_failure_reports_error(self, client, bulk_ids, monkeypatch):
        def boom(res):
            raise RuntimeError("k8s down")

        monkeypatch.setattr("apps.api.routes.k8s.delete_resources_by_name", boom)
        login(client, "akstudent", "stud123")
        resp = client.delete("/api/labs", json=bulk_ids["mine"])
        assert resp.status_code == 400
        for inst_id in bulk_ids["mine"]:
            assert db.session.get(LabInstances, inst_id).is_deleted is False
        logout(client)

    def test_partial_resource_removal_keeps_instance(self, client, bulk_ids, monkeypatch):
        # a resource left behind must NOT mark the instance deleted, so it can
        # be retried instead of being orphaned with no DB record.
        inst_id = bulk_ids["mine"][0]
        inst = db.session.get(LabInstances, inst_id)
        inst.k8s_resources = [{"kind": "pod", "name": "p1"}, {"kind": "pod", "name": "p2"}]
        db.session.commit()
        monkeypatch.setattr("apps.api.routes.k8s.delete_resources_by_name", lambda res: [True, False])
        login(client, "akstudent", "stud123")
        resp = client.delete("/api/labs", json=[inst_id])
        assert resp.status_code == 400
        assert resp.get_json()["status"] == "fail"
        assert "pod/p2" in resp.get_json()["result"]
        assert db.session.get(LabInstances, inst_id).is_deleted is False
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


# --- list_kubernetes_templates (staff only) -----------------------------
class TestListTemplates:
    def test_anonymous_is_redirected_to_login(self, client, ids):
        logout(client)
        resp = client.get("/api/templates/list")
        assert resp.status_code == 302
        assert "/login/" in resp.headers["Location"]

    def test_non_staff_is_rejected(self, client, ids):
        login(client, "akstudent", "stud123")
        resp = client.get("/api/templates/list")
        assert b"Unauthorized request" in resp.data
        logout(client)

    def test_not_defined_when_no_git_url(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "LAB_TEMPLATES_GIT_URL", "")
        login(client, "akadmin", "admin123")
        resp = client.get("/api/templates/list")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "not-defined"
        logout(client)

    def test_lists_templates(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.git.update_repo", lambda *a, **k: None)
        monkeypatch.setattr("apps.api.routes.git.list_files", lambda *a, **k: ["a.yaml", "sub/b.yaml"])
        login(client, "akadmin", "admin123")
        resp = client.get("/api/templates/list")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == ["a", "sub/b"]
        logout(client)

    def test_failure_is_reported(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.api.routes.git.update_repo", lambda *a, **k: None)
        def boom(*a, **k):
            raise RuntimeError("git blew up")
        monkeypatch.setattr("apps.api.routes.git.list_files", boom)
        login(client, "akadmin", "admin123")
        resp = client.get("/api/templates/list")
        assert resp.status_code == 400
        logout(client)


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

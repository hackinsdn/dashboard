"""Pytest suite for the Lab Instance lifecycle routes.

Mirrors tests/test_lab_categories.py: covers running_labs, run_lab,
check_lab_status, xterm, view_lab_instance, cancel_restart_lab_instance and
view_finished_labs in apps/home/routes.py, exercising the LabInstances and
HomeLogging models - ownership/visibility gating, per-group filtering,
expiration validation and the running/finished/cancel flows.

The handful of paths that talk to Kubernetes are covered by monkeypatching the
`k8s` module used by apps/home/routes.py; every permission/validation branch
returns before any k8s call and needs no stubbing.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_lab_instances.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on (e.g. the cancel tests
run last because they soft-delete a shared instance). Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "li" so they never collide with the
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
from apps.authentication.models import Users, Groups  # noqa: E402
from apps.home.models import Labs, LabInstances, HomeLogging  # noqa: E402

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
    """Seed the users/group/labs/instances fixtures shared by every test."""
    admin = Users(username="liadmin", password="admin123", email="liadmin@test.local", category="admin")
    teacher = Users(username="liteacher", password="teach123", email="liteacher@test.local", category="teacher")
    student = Users(username="listudent", password="stud123", email="listudent@test.local", category="student")
    other = Users(username="liother", password="stud123", email="liother@test.local", category="student")
    db.session.add_all([admin, teacher, student, other])
    db.session.commit()

    # teacher owns the group; student is a member (owner => privileged group)
    group = Groups(groupname="LiGroup", organization="ORG1")
    group.owners.append(teacher)
    group.members.append(student)
    db.session.add(group)

    lab_a = Labs(title="Instance Lab A", description="lab with instances")
    lab_a.set_lab_guide_md("# guide")  # view_lab_instance renders lab_guide_html_str
    lab_a.allowed_groups.append(group)
    lab_b = Labs(title="Run Lab B", description="used for run_lab GET/POST")
    lab_c = Labs(title="Run Lab C", description="used for run_lab failure path")
    db.session.add_all([lab_a, lab_b, lab_c])
    db.session.commit()

    inst_student = LabInstances(user_id=student.id, lab_id=lab_a.id, is_deleted=False)
    inst_student.k8s_resources = []
    inst_other = LabInstances(user_id=other.id, lab_id=lab_a.id, is_deleted=False)
    inst_other.k8s_resources = []
    inst_finished = LabInstances(user_id=student.id, lab_id=lab_a.id, is_deleted=True, finish_reason="done-reason")
    inst_finished.k8s_resources = []
    db.session.add_all([inst_student, inst_other, inst_finished])
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_id": teacher.id,
        "student_id": student.id,
        "other_id": other.id,
        "group_id": group.id,
        "lab_a_id": lab_a.id,
        "lab_b_id": lab_b.id,
        "lab_c_id": lab_c.id,
        "inst_student_id": inst_student.id,
        "inst_other_id": inst_other.id,
        "inst_finished_id": inst_finished.id,
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


# --- running_labs -------------------------------------------------------
class TestRunningLabs:
    def test_student_sees_own_running_instance(self, client, ids):
        logout(client)
        login(client, "listudent", "stud123")
        resp = client.get("/running/")
        assert resp.status_code == 200
        assert b"Instance Lab A" in resp.data

    def test_bad_group_filter_is_rejected(self, client, ids):
        resp = client.get("/running/?filter_group=999999")
        assert b"Group not found" in resp.data
        logout(client)

    def test_admin_group_filter_shows_only_members(self, client, ids):
        login(client, "liadmin", "admin123")
        resp = client.get(f"/running/?filter_group={ids['group_id']}")
        assert resp.status_code == 200
        assert b"listudent@test.local" in resp.data      # member's instance
        assert b"liother@test.local" not in resp.data     # non-member's instance
        logout(client)

    def test_group_owner_sees_member_instance(self, client, ids):
        login(client, "liteacher", "teach123")
        resp = client.get(f"/running/?filter_group={ids['group_id']}")
        assert resp.status_code == 200
        assert b"listudent@test.local" in resp.data
        logout(client)


# --- run_lab ------------------------------------------------------------
class TestRunLab:
    def test_get_missing_lab(self, client, ids):
        login(client, "listudent", "stud123")
        resp = client.get("/run_lab/does-not-exist")
        assert b"Lab not found" in resp.data

    def test_get_renders_form(self, client, ids):
        resp = client.get(f"/run_lab/{ids['lab_b_id']}")
        assert resp.status_code == 200

    def test_admin_gets_never_expires_option(self, client, ids):
        logout(client)
        login(client, "liadmin", "admin123")
        resp = client.get(f"/run_lab/{ids['lab_b_id']}")
        assert b"Never expires" in resp.data
        logout(client)
        login(client, "listudent", "stud123")

    def test_already_running_redirects(self, client, ids):
        resp = client.get(f"/run_lab/{ids['lab_a_id']}")
        assert resp.status_code == 302
        assert f"/lab_instance/view/{ids['inst_student_id']}" in resp.headers["Location"]

    def test_post_invalid_expiration(self, client, ids):
        resp = client.post(f"/run_lab/{ids['lab_b_id']}", data={"lab_expiration": "99"})
        assert b"Invalid lab duration/expiration" in resp.data

    def test_post_success_creates_instance_and_log(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.home.routes.k8s.create_lab", lambda *a, **k: (True, []))
        resp = client.post(f"/run_lab/{ids['lab_b_id']}", data={"lab_expiration": "4"})
        assert resp.status_code == 200

        inst = LabInstances.query.filter_by(user_id=ids["student_id"], lab_id=ids["lab_b_id"], is_deleted=False).first()
        assert inst is not None
        log = HomeLogging.query.filter_by(action="create_lab", success=True, lab_id=ids["lab_b_id"], user_id=ids["student_id"]).first()
        assert log is not None

    def test_post_failure_logs_error(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.home.routes.k8s.create_lab", lambda *a, **k: (False, "boom"))
        resp = client.post(f"/run_lab/{ids['lab_c_id']}", data={"lab_expiration": "4"})
        assert b"Error Running Labs" in resp.data

        assert LabInstances.query.filter_by(user_id=ids["student_id"], lab_id=ids["lab_c_id"]).first() is None
        log = HomeLogging.query.filter_by(action="create_lab", success=False, lab_id=ids["lab_c_id"], user_id=ids["student_id"]).first()
        assert log is not None
        logout(client)


# --- check_lab_status ---------------------------------------------------
class TestCheckLabStatus:
    def test_missing_instance(self, client, ids):
        login(client, "listudent", "stud123")
        resp = client.get("/lab_status/does-not-exist")
        assert b"Lab not found" in resp.data

    def test_other_users_instance_is_rejected(self, client, ids):
        resp = client.get(f"/lab_status/{ids['inst_other_id']}")
        assert b"not authorized" in resp.data

    def test_owner_sees_status(self, client, ids):
        resp = client.get(f"/lab_status/{ids['inst_student_id']}")
        assert resp.status_code == 200
        logout(client)


# --- xterm --------------------------------------------------------------
class TestXterm:
    def test_missing_instance(self, client, ids):
        login(client, "listudent", "stud123")
        resp = client.get("/xterm/does-not-exist/pod/p1/c1")
        assert b"Lab not found" in resp.data

    def test_other_users_instance_is_rejected(self, client, ids):
        resp = client.get(f"/xterm/{ids['inst_other_id']}/pod/p1/c1")
        assert b"not authorized" in resp.data

    def test_owner_gets_terminal(self, client, ids):
        resp = client.get(f"/xterm/{ids['inst_student_id']}/pod/p1/c1")
        assert resp.status_code == 200
        assert b"pod/p1/c1" in resp.data
        logout(client)


# --- view_lab_instance --------------------------------------------------
class TestViewLabInstance:
    def test_missing_instance(self, client, ids):
        login(client, "listudent", "stud123")
        resp = client.get("/lab_instance/view/does-not-exist")
        assert b"Lab not found" in resp.data

    def test_deleted_instance(self, client, ids):
        resp = client.get(f"/lab_instance/view/{ids['inst_finished_id']}")
        assert b"Lab finished" in resp.data

    def test_unauthorized_user_is_rejected(self, client, ids):
        # student is only a *member* (not owner/assistant) so has no privileged
        # group access to another user's instance
        resp = client.get(f"/lab_instance/view/{ids['inst_other_id']}")
        assert b"Not authorized to access this Lab" in resp.data

    def test_owner_views_instance(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.home.routes.k8s.get_lab_resources", lambda *a, **k: [])
        resp = client.get(f"/lab_instance/view/{ids['inst_student_id']}")
        assert resp.status_code == 200
        logout(client)


# --- view_finished_labs -------------------------------------------------
class TestFinishedLabs:
    def test_student_sees_own_finished_instance(self, client, ids):
        login(client, "listudent", "stud123")
        resp = client.get("/finished_labs")
        assert resp.status_code == 200
        assert b"Instance Lab A" in resp.data
        assert b"done-reason" in resp.data

    def test_bad_group_filter_is_rejected(self, client, ids):
        resp = client.get("/finished_labs?filter_group=999999")
        assert b"Group not found" in resp.data
        logout(client)


# --- cancel_restart_lab_instance (runs last: soft-deletes inst_student) --
class TestCancelInstance:
    def test_missing_instance(self, client, ids):
        login(client, "listudent", "stud123")
        resp = client.get("/lab_instance/cancel/does-not-exist")
        assert b"Lab not found" in resp.data

    def test_unauthorized_user_is_rejected(self, client, ids):
        resp = client.get(f"/lab_instance/cancel/{ids['inst_other_id']}")
        assert b"Not authorized to access this Lab" in resp.data

    def test_owner_can_cancel(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.home.routes.k8s.delete_resources_by_name", lambda *a, **k: [])
        resp = client.get(f"/lab_instance/cancel/{ids['inst_student_id']}")
        assert resp.status_code == 302

        inst = db.session.get(LabInstances, ids["inst_student_id"])
        assert inst.is_deleted is True
        logout(client)

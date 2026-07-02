"""Pytest suite for the /api engagement endpoints.

Mirrors tests/test_lab_categories.py: covers feedback, add_user_like,
del_user_like, join_group, extend_lab and bulk_approve_users in
apps/api/routes.py - role gating, content validation, group access-token
joining, lab-duration extension bounds, and bulk user approval. Exercises the
UserFeedbacks and UserLikes models.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_api_engagement.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "an" so they never collide with the
other test modules that share the same singleton app/database (apps/config.py
reads DATA_DIR once at import time, so every module ends up on one DB). Cache
keys shared across modules (user_likes, user_feedbacks) are cleared before use.
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
from apps.authentication.models import Users, Groups  # noqa: E402
from apps.home.models import Labs, LabInstances, UserFeedbacks, UserLikes  # noqa: E402

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
    admin = Users(username="anadmin", password="admin123", email="anadmin@test.local", category="admin")
    teacher = Users(username="anteacher", password="teach123", email="anteacher@test.local", category="teacher")
    student = Users(username="anstudent", password="stud123", email="anstudent@test.local", category="student")
    other = Users(username="another", password="stud123", email="another@test.local", category="student")
    pending = Users(username="anpending", password="pend123", email="anpending@test.local", category="user")
    bulk1 = Users(username="anbulk1", password="b123", email="anbulk1@test.local", category="user")
    bulk2 = Users(username="anbulk2", password="b123", email="anbulk2@test.local", category="user")
    db.session.add_all([admin, teacher, student, other, pending, bulk1, bulk2])
    db.session.commit()

    group = Groups(groupname="AnGroup", organization="ORG1", accesstoken="secret-token")
    system = Groups(groupname="AnSystem", organization="SYSTEM", accesstoken="sys-token")
    db.session.add_all([group, system])

    lab = Labs(title="An Lab", description="lab for extend")
    db.session.add(lab)
    db.session.commit()
    inst = LabInstances(user_id=student.id, lab_id=lab.id, is_deleted=False)
    inst.k8s_resources = []
    db.session.add(inst)
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_id": teacher.id,
        "student_id": student.id,
        "other_id": other.id,
        "pending_id": pending.id,
        "bulk1_id": bulk1.id,
        "bulk2_id": bulk2.id,
        "group_id": group.id,
        "system_id": system.id,
        "inst_id": inst.id,
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


# --- feedback -----------------------------------------------------------
class TestFeedback:
    def test_unapproved_user_is_rejected(self, client, ids):
        logout(client)
        login(client, "anpending", "pend123")
        resp = client.get("/api/feedback")
        assert resp.status_code == 401
        logout(client)

    def test_get_returns_recent_feedbacks(self, client, ids):
        cache.delete("user_feedbacks")
        login(client, "anstudent", "stud123")
        resp = client.get("/api/feedback")
        assert resp.status_code == 200
        assert "recent_feedbacks" in resp.get_json()

    def test_post_without_stars_is_rejected(self, client, ids):
        resp = client.post("/api/feedback", json={"comment": "no stars"})
        assert resp.status_code == 400

    def test_post_creates_then_updates_feedback(self, client, ids):
        resp = client.post("/api/feedback", json={"stars": 5, "comment": "great"})
        assert resp.status_code == 200
        fb = UserFeedbacks.query.filter_by(user_id=ids["student_id"]).all()
        assert len(fb) == 1 and fb[0].stars == 5

        resp = client.post("/api/feedback", json={"stars": 3, "comment": "meh"})
        assert resp.status_code == 200
        fb = UserFeedbacks.query.filter_by(user_id=ids["student_id"]).all()
        assert len(fb) == 1 and fb[0].stars == 3  # updated in place
        logout(client)


# --- user likes ---------------------------------------------------------
class TestUserLikes:
    def test_add_and_remove_like(self, client, ids):
        cache.delete("user_likes")
        login(client, "anstudent", "stud123")

        resp = client.post("/api/user_like")
        assert resp.status_code == 200
        assert db.session.get(UserLikes, ids["student_id"]) is not None

        # liking again is idempotent
        resp = client.post("/api/user_like")
        assert resp.status_code == 200

        resp = client.delete("/api/user_like")
        assert resp.status_code == 200
        assert db.session.get(UserLikes, ids["student_id"]) is None
        logout(client)


# --- join_group ---------------------------------------------------------
class TestJoinGroup:
    def test_empty_content_is_rejected(self, client, ids):
        login(client, "anstudent", "stud123")
        resp = client.post(f"/api/groups/join/{ids['group_id']}", json={})
        assert resp.status_code == 400

    def test_unknown_group(self, client, ids):
        resp = client.post("/api/groups/join/999999", json={"accessToken": "secret-token"})
        assert resp.status_code == 404

    def test_system_group_non_admin_is_rejected(self, client, ids):
        resp = client.post(f"/api/groups/join/{ids['system_id']}", json={"accessToken": "sys-token"})
        assert resp.status_code == 404

    def test_wrong_token_is_rejected(self, client, ids):
        resp = client.post(f"/api/groups/join/{ids['group_id']}", json={"accessToken": "nope"})
        assert resp.status_code == 400

    def test_correct_token_joins_then_reports_already_member(self, client, ids):
        resp = client.post(f"/api/groups/join/{ids['group_id']}", json={"accessToken": "secret-token"})
        assert resp.status_code == 200
        group = db.session.get(Groups, ids["group_id"])
        assert group.is_member(ids["student_id"])

        resp = client.post(f"/api/groups/join/{ids['group_id']}", json={"accessToken": "secret-token"})
        assert resp.status_code == 200
        assert b"Already member" in resp.data
        logout(client)


# --- extend_lab ---------------------------------------------------------
class TestExtendLab:
    def test_missing_instance(self, client, ids):
        login(client, "anstudent", "stud123")
        resp = client.post("/api/lab/does-not-exist/extend", json={"extend_hours": 4})
        assert resp.status_code == 404

    def test_non_owner_is_rejected(self, client, ids):
        logout(client)
        login(client, "another", "stud123")
        resp = client.post(f"/api/lab/{ids['inst_id']}/extend", json={"extend_hours": 4})
        assert resp.status_code == 401
        logout(client)

    def test_missing_extend_hours_is_rejected(self, client, ids):
        login(client, "anstudent", "stud123")
        resp = client.post(f"/api/lab/{ids['inst_id']}/extend", json={})
        assert resp.status_code == 400

    def test_out_of_range_hours_is_rejected(self, client, ids):
        resp = client.post(f"/api/lab/{ids['inst_id']}/extend", json={"extend_hours": 800})
        assert resp.status_code == 400

    def test_valid_extension(self, client, ids):
        resp = client.post(f"/api/lab/{ids['inst_id']}/extend", json={"extend_hours": 24})
        assert resp.status_code == 200
        inst = db.session.get(LabInstances, ids["inst_id"])
        assert inst.expiration_ts is not None
        logout(client)


# --- bulk_approve_users -------------------------------------------------
class TestBulkApproveUsers:
    def test_non_staff_is_rejected(self, client, ids):
        login(client, "anstudent", "stud123")
        resp = client.post("/api/users/bulk-approve", json=[str(ids["bulk1_id"])])
        assert resp.status_code == 401
        logout(client)

    def test_empty_content_is_rejected(self, client, ids):
        login(client, "anadmin", "admin123")
        resp = client.post("/api/users/bulk-approve", json=[])
        assert resp.status_code == 400

    def test_non_user_category_in_list_is_rejected(self, client, ids):
        # student is not a category=="user" account -> invalid to approve
        resp = client.post("/api/users/bulk-approve", json=[str(ids["student_id"])])
        assert resp.status_code == 400

    def test_valid_list_is_approved(self, client, ids):
        resp = client.post("/api/users/bulk-approve", json=[str(ids["bulk1_id"]), str(ids["bulk2_id"])])
        assert resp.status_code == 200
        assert db.session.get(Users, ids["bulk1_id"]).category == "student"
        assert db.session.get(Users, ids["bulk2_id"]).category == "student"
        logout(client)

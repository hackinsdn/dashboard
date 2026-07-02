"""Pytest suite for the Users CRUD feature.

Mirrors tests/test_lab_categories.py: exercises sidebar/route visibility per
role, self-profile edit, admin cross-user edit, username validation, password
change, and the admin-only soft-delete API (including the "user still has a
running lab" block).

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_users.py -v

Note: the classes below are ordered, stateful workflows (e.g. create, then
edit, then delete) rather than independent unit tests - pytest runs test
methods within a class in definition order by default, which this suite
relies on. Do not run with a random-order plugin (e.g. pytest-randomly)
without disabling it for this file.

The seeded usernames are all prefixed "us" so they never collide with the
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
# Must happen before `apps`/`run` are imported below, since apps/config.py
# reads DATA_DIR at import time.
TEST_DATA_DIR = tempfile.mkdtemp(prefix="hackinsdn_test_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("OPTIONAL_MODULES", "")

# apps/controllers/clabernetes.py hard-requires a 'clabverter' binary to be
# installed on disk at *import* time; stub it out so these tests don't need it.
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
    """Seed the users/lab fixtures shared by every test."""
    admin = Users(username="usadmin", password="admin123", email="usadmin@test.local", category="admin")
    teacher = Users(username="usteacher", password="teach123", email="usteacher@test.local", category="teacher")
    student = Users(username="usstudent", password="stud123", email="usstudent@test.local", category="student")
    labcreator = Users(username="uslabcreator", password="lc123", email="uslc@test.local", category="labcreator")
    # an unapproved user - only these show up in a teacher's /users listing
    pending = Users(username="uspending", password="pend123", email="uspending@test.local", category="user")
    # dedicated targets so cross-user/delete tests don't mutate the actors above
    edit_target = Users(username="usedittarget", password="et123", email="uset@test.local", category="student")
    pw_target = Users(username="uspwtarget", password="pw123", email="uspw@test.local", category="student")
    del_target = Users(username="usdeltarget", password="del123", email="usdel@test.local", category="student")
    del_busy = Users(username="usdelbusy", password="busy123", email="usbusy@test.local", category="student")
    db.session.add_all(
        [admin, teacher, student, labcreator, pending, edit_target, pw_target, del_target, del_busy]
    )
    db.session.commit()

    # del_busy has a still-running lab instance, which must block deletion
    lab = Labs(title="Blocker Lab", description="lab used to block user delete")
    db.session.add(lab)
    db.session.commit()
    lab_inst = LabInstances(user_id=del_busy.id, lab_id=lab.id, is_deleted=False)
    db.session.add(lab_inst)
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_id": teacher.id,
        "student_id": student.id,
        "labcreator_id": labcreator.id,
        "pending_id": pending.id,
        "edit_target_id": edit_target.id,
        "pw_target_id": pw_target.id,
        "del_target_id": del_target.id,
        "del_busy_id": del_busy.id,
    }


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    """Log in and return True if the app accepted the credentials.

    A 302 on POST /login/ (vs. a 200 re-render of the login form on bad
    credentials) is the reliable signal that authentication succeeded.
    """
    resp = client.post(
        "/login/",
        data={"identifier": username, "password": password, "login": "1"},
    )
    return resp.status_code == 302


def logout(client):
    client.get("/logout")


# --- role gating --------------------------------------------------------
class TestRoleGating:
    def test_admin_login_succeeds(self, client, ids):
        logout(client)
        assert login(client, "usadmin", "admin123")

    def test_student_is_rejected_from_users_list(self, client, ids):
        logout(client)
        login(client, "usstudent", "stud123")
        resp = client.get("/users")
        assert b"Unauthorized request" in resp.data

    def test_admin_can_view_users_list(self, client, ids):
        login(client, "usadmin", "admin123")
        resp = client.get("/users")
        assert resp.status_code == 200
        assert b"usstudent" in resp.data

    def test_teacher_only_sees_unapproved_users(self, client, ids):
        login(client, "usteacher", "teach123")
        resp = client.get("/users")
        assert resp.status_code == 200
        # teachers only see category=="user" rows
        assert b"uspending" in resp.data
        assert b"usstudent" not in resp.data
        logout(client)


# --- self-service profile edit ------------------------------------------
class TestSelfEdit:
    def test_user_can_view_own_profile(self, client, ids):
        login(client, "usstudent", "stud123")
        resp = client.get("/profile")
        assert resp.status_code == 200
        assert b"usstudent" in resp.data

    def test_user_can_update_own_profile(self, client, ids):
        resp = client.post(
            "/profile",
            data={
                "username": "usstudent",
                "email": "usstudent-new@test.local",
                "given_name": "Student",
                "family_name": "Aye",
                "password": "",
            },
        )
        assert b"User profile updated successfully" in resp.data

        user = db.session.get(Users, ids["student_id"])
        assert user.email == "usstudent-new@test.local"
        assert user.given_name == "Student"

    def test_invalid_username_is_rejected(self, client, ids):
        resp = client.post(
            "/profile",
            data={
                "username": "ab",  # too short (min 3 chars)
                "email": "usstudent-new@test.local",
                "given_name": "Student",
                "family_name": "Aye",
                "password": "",
            },
        )
        assert b"Invalid username" in resp.data
        logout(client)

    def test_password_change_takes_effect(self, client, ids):
        login(client, "uspwtarget", "pw123")
        resp = client.post(
            "/profile",
            data={
                "username": "uspwtarget",
                "email": "uspw@test.local",
                "given_name": "",
                "family_name": "",
                "password": "newpw456",
            },
        )
        assert b"User profile updated successfully" in resp.data
        logout(client)

        assert login(client, "uspwtarget", "newpw456")
        logout(client)
        assert not login(client, "uspwtarget", "pw123")


# --- cross-user access rules --------------------------------------------
class TestCrossUserAccess:
    def test_non_admin_cannot_view_other_user(self, client, ids):
        login(client, "usstudent", "stud123")
        resp = client.get(f"/users/{ids['edit_target_id']}")
        assert b"You dont have access for this page" in resp.data
        logout(client)

    def test_admin_can_edit_other_user_and_change_category(self, client, ids):
        login(client, "usadmin", "admin123")
        resp = client.post(
            f"/users/{ids['edit_target_id']}",
            data={
                "username": "usedittarget",
                "email": "uset@test.local",
                "given_name": "Edited",
                "family_name": "ByAdmin",
                "password": "",
                "user_category": "teacher",
            },
        )
        assert b"User profile updated successfully" in resp.data

        user = db.session.get(Users, ids["edit_target_id"])
        assert user.category == "teacher"
        assert user.given_name == "Edited"

    def test_admin_edit_of_missing_user_is_rejected(self, client, ids):
        resp = client.get("/users/999999")
        assert b"User not found or deactivated" in resp.data
        logout(client)


# --- admin-only soft-delete API -----------------------------------------
class TestDeleteApi:
    def test_student_cannot_delete_user(self, client, ids):
        login(client, "usstudent", "stud123")
        resp = client.delete(f"/api/users/{ids['del_target_id']}")
        assert resp.status_code == 401
        logout(client)

    def test_teacher_cannot_delete_user(self, client, ids):
        login(client, "usteacher", "teach123")
        resp = client.delete(f"/api/users/{ids['del_target_id']}")
        assert resp.status_code == 401
        logout(client)

    def test_delete_blocked_while_user_has_running_lab(self, client, ids):
        login(client, "usadmin", "admin123")
        resp = client.delete(f"/api/users/{ids['del_busy_id']}")
        assert resp.status_code == 400
        assert b"labs running" in resp.data

    def test_admin_can_delete_user(self, client, ids):
        resp = client.delete(f"/api/users/{ids['del_target_id']}")
        assert resp.status_code == 200

        user = db.session.get(Users, ids["del_target_id"])
        assert user.is_deleted is True

    def test_deleting_already_deleted_user_returns_404(self, client, ids):
        resp = client.delete(f"/api/users/{ids['del_target_id']}")
        assert resp.status_code == 404
        logout(client)

"""Pytest suite for the Groups CRUD feature.

Mirrors tests/test_lab_categories.py: exercises list visibility per role
(including the SYSTEM-organization restriction), create/edit workflows,
teacher ownership rules, approved-users e-mail validation, membership
mutation, and the admin/teacher-owner soft-delete API.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_groups.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "gr" so they never collide with the
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
    """Seed the users/groups fixtures shared by every test."""
    admin = Users(username="gradmin", password="admin123", email="gradmin@test.local", category="admin")
    teacher_a = Users(username="grteachera", password="teach123", email="grta@test.local", category="teacher")
    teacher_b = Users(username="grteacherb", password="teach123", email="grtb@test.local", category="teacher")
    student = Users(username="grstudent", password="stud123", email="grsa@test.local", category="student")
    labcreator = Users(username="grlabcreator", password="lc123", email="grlc@test.local", category="labcreator")
    db.session.add_all([admin, teacher_a, teacher_b, student, labcreator])
    db.session.commit()

    normal = Groups(groupname="NormalGroup", organization="ORG1")
    system = Groups(groupname="SystemGroup", organization="SYSTEM")
    # a group owned by teacherA, used for the ownership tests
    owned = Groups(groupname="TeacherOwned", organization="ORG2")
    owned.owners.append(teacher_a)
    # a group with a member, deleted by admin (guards the dict_keys fix)
    deleteme = Groups(groupname="DeleteMe", organization="ORG3")
    deleteme.members.append(student)
    db.session.add_all([normal, system, owned, deleteme])
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_a_id": teacher_a.id,
        "teacher_b_id": teacher_b.id,
        "student_id": student.id,
        "labcreator_id": labcreator.id,
        "normal_gid": normal.id,
        "system_gid": system.id,
        "owned_gid": owned.id,
        "deleteme_gid": deleteme.id,
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


def group_form(**overrides):
    """A complete /groups/edit POST body; override individual fields as needed."""
    data = {
        "groupname": "",
        "description": "",
        "organization": "",
        "expiration": "",
        "accesstoken": "",
        "approved_users": "",
    }
    data.update(overrides)
    return data


# --- list visibility ----------------------------------------------------
class TestListVisibility:
    def test_student_sees_normal_but_not_system_group(self, client, ids):
        logout(client)
        login(client, "grstudent", "stud123")
        resp = client.get("/groups/list")
        assert resp.status_code == 200
        assert b"NormalGroup" in resp.data
        assert b"SystemGroup" not in resp.data

    def test_admin_sees_system_group(self, client, ids):
        login(client, "gradmin", "admin123")
        resp = client.get("/groups/list")
        assert resp.status_code == 200
        assert b"SystemGroup" in resp.data
        assert b"NormalGroup" in resp.data
        logout(client)


# --- create workflow ----------------------------------------------------
class TestCreate:
    def test_admin_can_create_group(self, client, ids):
        login(client, "gradmin", "admin123")
        resp = client.post(
            "/groups/edit/new",
            data=group_form(groupname="AdminCreated", organization="ORG-NEW"),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Group updated successfully" in resp.data

        group = Groups.query.filter_by(groupname="AdminCreated").first()
        assert group is not None
        assert group.organization == "ORG-NEW"
        logout(client)

    def test_student_cannot_create_group(self, client, ids):
        login(client, "grstudent", "stud123")
        resp = client.post("/groups/edit/new", data=group_form(groupname="StudentGroup"))
        assert b"You don&#39;t have permission to edit this group." in resp.data
        logout(client)

    def test_non_admin_cannot_create_system_group(self, client, ids):
        login(client, "grteachera", "teach123")
        resp = client.post(
            "/groups/edit/new",
            data=group_form(groupname="SneakySystem", organization="SYSTEM"),
        )
        assert b"Only admins can create/change System groups." in resp.data
        logout(client)


# --- teacher ownership rules --------------------------------------------
class TestOwnership:
    def test_teacher_non_owner_cannot_edit(self, client, ids):
        login(client, "grteacherb", "teach123")
        resp = client.get(f"/groups/edit/{ids['owned_gid']}")
        assert b"You don&#39;t have permission to edit this group." in resp.data
        logout(client)

    def test_teacher_owner_can_edit(self, client, ids):
        login(client, "grteachera", "teach123")
        resp = client.post(
            f"/groups/edit/{ids['owned_gid']}",
            data=group_form(
                groupname="TeacherOwned",
                organization="ORG2",
                description="edited by owner",
            ),
        )
        assert b"Group updated successfully" in resp.data

        group = db.session.get(Groups, ids["owned_gid"])
        assert group.description == "edited by owner"
        logout(client)


# --- approved-users validation ------------------------------------------
class TestApprovedUsers:
    def test_invalid_email_is_rejected(self, client, ids):
        login(client, "gradmin", "admin123")
        resp = client.post(
            f"/groups/edit/{ids['normal_gid']}",
            data=group_form(groupname="NormalGroup", organization="ORG1", approved_users="not-an-email"),
        )
        assert b"invalid approved users" in resp.data

    def test_valid_approved_users_are_saved(self, client, ids):
        resp = client.post(
            f"/groups/edit/{ids['normal_gid']}",
            data=group_form(
                groupname="NormalGroup",
                organization="ORG1",
                approved_users="a@b.com, c@d.com",
            ),
        )
        assert b"Group updated successfully" in resp.data

        group = db.session.get(Groups, ids["normal_gid"])
        assert group.approved_users_list == ["a@b.com", "c@d.com"]
        logout(client)


# --- no-op detection ----------------------------------------------------
class TestNoOp:
    def test_resubmitting_identical_values_reports_no_changes(self, client, ids):
        login(client, "gradmin", "admin123")
        # first create the group (approved_users "" is normalized to '[]')
        client.post(
            "/groups/edit/new",
            data=group_form(groupname="NoOpGroup", organization="ORG-NOOP"),
            follow_redirects=True,
        )
        group = Groups.query.filter_by(groupname="NoOpGroup").first()

        # re-submit the exact stored values (approved_users '[]' matches, so
        # nothing changes)
        resp = client.post(
            f"/groups/edit/{group.id}",
            data=group_form(groupname="NoOpGroup", organization="ORG-NOOP", approved_users="[]"),
        )
        assert b"No changes were made to the group." in resp.data
        logout(client)


# --- membership mutation ------------------------------------------------
class TestMembership:
    def test_add_then_remove_member(self, client, ids):
        login(client, "gradmin", "admin123")
        client.post(
            "/groups/edit/new",
            data=group_form(groupname="MemberGroup", organization="ORG-MEM"),
            follow_redirects=True,
        )
        group = Groups.query.filter_by(groupname="MemberGroup").first()

        # add studentA as a member
        resp = client.post(
            f"/groups/edit/{group.id}",
            data=group_form(
                groupname="MemberGroup",
                organization="ORG-MEM",
                approved_users="[]",
                group_members=str(ids["student_id"]),
            ),
        )
        assert b"Group updated successfully" in resp.data
        db.session.refresh(group)
        assert ids["student_id"] in group.members_dict

        # remove the member (submit with no group_members)
        resp = client.post(
            f"/groups/edit/{group.id}",
            data=group_form(groupname="MemberGroup", organization="ORG-MEM", approved_users="[]"),
        )
        assert b"Group updated successfully" in resp.data
        db.session.refresh(group)
        assert ids["student_id"] not in group.members_dict
        logout(client)


# --- soft-delete API ----------------------------------------------------
class TestDeleteApi:
    def test_student_cannot_delete_group(self, client, ids):
        login(client, "grstudent", "stud123")
        resp = client.delete(f"/api/groups/{ids['normal_gid']}")
        assert resp.status_code == 401
        logout(client)

    def test_labcreator_cannot_delete_group(self, client, ids):
        login(client, "grlabcreator", "lc123")
        resp = client.delete(f"/api/groups/{ids['normal_gid']}")
        assert resp.status_code == 401
        logout(client)

    def test_teacher_non_owner_cannot_delete_group(self, client, ids):
        login(client, "grteacherb", "teach123")
        resp = client.delete(f"/api/groups/{ids['normal_gid']}")
        assert resp.status_code == 401
        logout(client)

    def test_non_admin_cannot_delete_system_group(self, client, ids):
        login(client, "grteachera", "teach123")
        resp = client.delete(f"/api/groups/{ids['system_gid']}")
        assert resp.status_code == 404
        assert b"Only admins can change System groups" in resp.data
        logout(client)

    def test_delete_missing_group_returns_404(self, client, ids):
        login(client, "gradmin", "admin123")
        resp = client.delete("/api/groups/999999")
        assert resp.status_code == 404

    def test_admin_can_delete_group_with_member(self, client, ids):
        resp = client.delete(f"/api/groups/{ids['deleteme_gid']}")
        assert resp.status_code == 200

        group = db.session.get(Groups, ids["deleteme_gid"])
        assert group.is_deleted is True
        logout(client)

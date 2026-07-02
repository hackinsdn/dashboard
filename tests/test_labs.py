"""Pytest suite for the Labs CRUD feature.

Mirrors tests/test_lab_categories.py: exercises route/role gating,
create/edit workflows, the "at least one category" and invalid-category
validation, labcreator ownership rules, per-group view filtering, and the
"no categories exist yet" guard.

There is no soft-delete route for catalog Labs (the /api/lab/<id> DELETE
endpoint removes LabInstances, which is out of scope here), so this suite
covers view + edit only.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_labs.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "lb" so they never collide with the
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
from apps.home.models import Labs, LabCategories  # noqa: E402

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
    """Seed the users/category/group fixtures shared by every test."""
    admin = Users(username="lbadmin", password="admin123", email="lbadmin@test.local", category="admin")
    teacher = Users(username="lbteacher", password="teach123", email="lbta@test.local", category="teacher")
    student = Users(username="lbstudent", password="stud123", email="lbsa@test.local", category="student")
    labcreator = Users(username="lblabcreator", password="lc123", email="lblc@test.local", category="labcreator")
    db.session.add_all([admin, teacher, student, labcreator])
    db.session.commit()

    category = LabCategories(category="Networking", color_cls="dark")
    category.updated_by = None
    db.session.add(category)

    # studentA is a member of this group; the admin-created lab will be
    # restricted to it, exercising the per-group view filter.
    student_group = Groups(groupname="StudentGroup", organization="ORG1")
    student_group.members.append(student)
    db.session.add(student_group)
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_id": teacher.id,
        "student_id": student.id,
        "labcreator_id": labcreator.id,
        "category_id": category.id,
        "student_group_id": student_group.id,
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


def lab_form(**overrides):
    """A complete /labs/edit POST body; override individual fields as needed."""
    data = {
        "lab_title": "",
        "lab_description": "",
        "lab_extended_desc": "extended description",
        "lab_guide": "# guide",
        "lab_manifest": "apiVersion: v1",
        "lab_goals": "",
    }
    data.update(overrides)
    return data


# --- role gating --------------------------------------------------------
class TestRoleGating:
    def test_student_cannot_edit_labs(self, client, ids):
        logout(client)
        login(client, "lbstudent", "stud123")
        resp = client.get("/labs/edit/new")
        assert b"Unauthorized request" in resp.data

    def test_student_can_view_labs(self, client, ids):
        resp = client.get("/labs/view")
        assert resp.status_code == 200
        logout(client)


# --- admin create/edit workflow -----------------------------------------
class TestAdminCrudWorkflow:
    def test_admin_can_create_lab(self, client, ids):
        login(client, "lbadmin", "admin123")
        resp = client.post(
            "/labs/edit/new",
            data=lab_form(
                lab_title="Admin Networking Lab",
                lab_description="created by admin",
                lab_categories=str(ids["category_id"]),
                lab_allowed_groups=str(ids["student_group_id"]),
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200

        lab = Labs.query.filter_by(title="Admin Networking Lab").first()
        assert lab is not None
        assert len(lab.categories) == 1 and lab.categories[0].id == ids["category_id"]
        assert len(lab.allowed_groups) == 1 and lab.allowed_groups[0].id == ids["student_group_id"]

    def test_create_requires_at_least_one_category(self, client, ids):
        resp = client.post(
            "/labs/edit/new",
            data=lab_form(lab_title="No Category Lab", lab_description="x"),
        )
        assert b"Please select at least one category" in resp.data

    def test_create_rejects_invalid_category(self, client, ids):
        resp = client.post(
            "/labs/edit/new",
            data=lab_form(
                lab_title="Bad Category Lab",
                lab_description="x",
                lab_categories="999999",
            ),
        )
        assert b"Invalid Lab Category" in resp.data

    def test_admin_can_edit_lab(self, client, ids):
        lab = Labs.query.filter_by(title="Admin Networking Lab").first()
        resp = client.post(
            f"/labs/edit/{lab.id}",
            data=lab_form(
                lab_title="Admin Networking Lab v2",
                lab_description="edited by admin",
                lab_categories=str(ids["category_id"]),
                lab_allowed_groups=str(ids["student_group_id"]),
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200

        db.session.refresh(lab)
        assert lab.title == "Admin Networking Lab v2"

    def test_edit_missing_lab_is_rejected(self, client, ids):
        resp = client.get("/labs/edit/does-not-exist")
        assert b"Lab not found" in resp.data
        logout(client)


# --- labcreator ownership rules -----------------------------------------
class TestLabCreatorOwnership:
    def test_labcreator_can_create_own_lab(self, client, ids):
        login(client, "lblabcreator", "lc123")
        resp = client.post(
            "/labs/edit/new",
            data=lab_form(
                lab_title="LabCreator Private Lab",
                lab_description="created by labcreator",
                lab_categories=str(ids["category_id"]),
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200

        lab = Labs.query.filter_by(title="LabCreator Private Lab").first()
        assert lab is not None
        assert lab.updated_by == ids["labcreator_id"]

    def test_labcreator_cannot_edit_others_lab(self, client, ids):
        lab = Labs.query.filter_by(title="Admin Networking Lab v2").first()
        resp = client.get(f"/labs/edit/{lab.id}")
        assert b"You don&#39;t have permission to edit this Lab." in resp.data

    def test_labcreator_can_edit_own_lab(self, client, ids):
        lab = Labs.query.filter_by(title="LabCreator Private Lab").first()
        resp = client.post(
            f"/labs/edit/{lab.id}",
            data=lab_form(
                lab_title="LabCreator Private Lab v2",
                lab_description="edited by labcreator",
                lab_categories=str(ids["category_id"]),
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200

        db.session.refresh(lab)
        assert lab.title == "LabCreator Private Lab v2"
        logout(client)


# --- per-group view filtering -------------------------------------------
class TestViewFiltering:
    def test_admin_sees_all_labs(self, client, ids):
        login(client, "lbadmin", "admin123")
        resp = client.get("/labs/view")
        assert b"Admin Networking Lab v2" in resp.data
        assert b"LabCreator Private Lab v2" in resp.data
        logout(client)

    def test_student_only_sees_labs_for_their_groups(self, client, ids):
        login(client, "lbstudent", "stud123")
        resp = client.get("/labs/view")
        # admin lab is restricted to StudentGroup, which studentA belongs to
        assert b"Admin Networking Lab v2" in resp.data
        # labcreator's lab has no allowed groups and studentA didn't author it
        assert b"LabCreator Private Lab v2" not in resp.data
        logout(client)


# --- "no categories yet" guard ------------------------------------------
class TestNoCategoriesGuard:
    def test_edit_lab_guard_when_no_categories_exist(self, client, ids):
        login(client, "lbadmin", "admin123")
        # Deactivate every currently-active category (other test modules that
        # share this singleton DB may have seeded their own), then restore.
        active = LabCategories.query.filter_by(is_deleted=False).all()
        for category in active:
            category.is_deleted = True
        db.session.commit()
        try:
            resp = client.get("/labs/edit/new")
            assert b"No Lab Categories found" in resp.data
        finally:
            for category in active:
                category.is_deleted = False
            db.session.commit()
            logout(client)

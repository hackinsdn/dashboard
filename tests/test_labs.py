"""Pytest suite for the Labs CRUD feature.

Mirrors tests/test_lab_categories.py: exercises route/role gating,
create/edit workflows, the "at least one category" and invalid-category
validation, labcreator ownership rules, per-group view filtering, the
"no categories exist yet" guard, and the soft-delete route.

Soft-delete for catalog Labs lives at DELETE /api/labs/<id> (distinct from
DELETE /api/lab/<id>, which removes a running LabInstance). It flips the
Labs.is_deleted flag, mirrors edit permissions, is blocked while the lab has
running instances, and hides deleted labs from the catalog views.

Admins can undelete via POST /api/labs/<id>/restore, reveal soft-deleted labs
in the catalog with ?show_deleted=1, and open a deleted lab in the editor to
restore it (all covered by TestRestore).

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
from apps.home.models import Labs, LabCategories, LabInstances  # noqa: E402

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


# --- soft-delete route --------------------------------------------------
def _make_lab(ids, title, updated_by=None):
    """Create and persist a catalog Lab bound to the seeded category."""
    lab = Labs(title=title, description="x")
    lab.categories.append(db.session.get(LabCategories, ids["category_id"]))
    if updated_by is not None:
        lab.updated_by = updated_by
    db.session.add(lab)
    db.session.commit()
    return lab.id


class TestSoftDelete:
    def test_student_cannot_delete_lab(self, client, ids):
        lab_id = _make_lab(ids, "Delete Me Student")
        logout(client)
        login(client, "lbstudent", "stud123")
        resp = client.delete(f"/api/labs/{lab_id}")
        assert resp.status_code == 401
        assert db.session.get(Labs, lab_id).is_deleted is False
        logout(client)

    def test_delete_missing_lab_returns_404(self, client, ids):
        login(client, "lbadmin", "admin123")
        resp = client.delete("/api/labs/does-not-exist")
        assert resp.status_code == 404

    def test_delete_blocked_when_running_instance_exists(self, client, ids):
        lab_id = _make_lab(ids, "Delete Me With Instance")
        instance = LabInstances(lab_id=lab_id, user_id=ids["student_id"], is_deleted=False)
        db.session.add(instance)
        db.session.commit()

        resp = client.delete(f"/api/labs/{lab_id}")
        assert resp.status_code == 400
        assert b"running instance" in resp.data
        assert db.session.get(Labs, lab_id).is_deleted is False

        # a finished (soft-deleted) instance no longer blocks deletion
        instance.is_deleted = True
        db.session.commit()
        resp = client.delete(f"/api/labs/{lab_id}")
        assert resp.status_code == 200
        assert db.session.get(Labs, lab_id).is_deleted is True

    def test_admin_can_delete_lab_and_it_disappears_from_view(self, client, ids):
        lab_id = _make_lab(ids, "Admin Deletable Lab")
        resp = client.get("/labs/view")
        assert b"Admin Deletable Lab" in resp.data

        resp = client.delete(f"/api/labs/{lab_id}")
        assert resp.status_code == 200
        assert db.session.get(Labs, lab_id).is_deleted is True

        resp = client.get("/labs/view")
        assert b"Admin Deletable Lab" not in resp.data

    def test_delete_already_deleted_returns_404(self, client, ids):
        lab_id = _make_lab(ids, "Already Deleted Lab")
        db.session.get(Labs, lab_id).is_deleted = True
        db.session.commit()
        resp = client.delete(f"/api/labs/{lab_id}")
        assert resp.status_code == 404

    def test_deleted_lab_edit_returns_not_found_for_non_admin(self, client, ids):
        # non-admins never see a soft-deleted lab in the editor (admins can,
        # to restore it - covered by TestRestore)
        lab_id = _make_lab(ids, "Deleted Lab Edit Guard")
        db.session.get(Labs, lab_id).is_deleted = True
        db.session.commit()
        logout(client)
        login(client, "lbteacher", "teach123")
        resp = client.get(f"/labs/edit/{lab_id}")
        assert b"Lab not found" in resp.data
        logout(client)

    def test_labcreator_cannot_delete_others_lab(self, client, ids):
        lab_id = _make_lab(ids, "Admin Owned Lab", updated_by=ids["admin_id"])
        logout(client)
        login(client, "lblabcreator", "lc123")
        resp = client.delete(f"/api/labs/{lab_id}")
        assert resp.status_code == 401
        assert db.session.get(Labs, lab_id).is_deleted is False
        logout(client)

    def test_labcreator_can_delete_own_lab(self, client, ids):
        lab_id = _make_lab(ids, "LabCreator Owned Lab", updated_by=ids["labcreator_id"])
        login(client, "lblabcreator", "lc123")
        resp = client.delete(f"/api/labs/{lab_id}")
        assert resp.status_code == 200
        assert db.session.get(Labs, lab_id).is_deleted is True
        logout(client)


# --- admin restore (undelete) -------------------------------------------
def _make_deleted_lab(ids, title, allowed_group_id=None):
    lab = Labs(title=title, description="x", is_deleted=True)
    db.session.add(lab)
    lab.categories.append(db.session.get(LabCategories, ids["category_id"]))
    if allowed_group_id is not None:
        lab.allowed_groups.append(db.session.get(Groups, allowed_group_id))
    db.session.commit()
    return lab.id


class TestRestore:
    def test_teacher_cannot_restore_lab(self, client, ids):
        lab_id = _make_deleted_lab(ids, "Restore Teacher Denied")
        logout(client)
        login(client, "lbteacher", "teach123")
        resp = client.post(f"/api/labs/{lab_id}/restore")
        assert resp.status_code == 401
        assert db.session.get(Labs, lab_id).is_deleted is True
        logout(client)

    def test_labcreator_cannot_restore_own_lab(self, client, ids):
        lab_id = _make_deleted_lab(ids, "Restore LabCreator Denied")
        db.session.get(Labs, lab_id).updated_by = ids["labcreator_id"]
        db.session.commit()
        login(client, "lblabcreator", "lc123")
        resp = client.post(f"/api/labs/{lab_id}/restore")
        assert resp.status_code == 401
        assert db.session.get(Labs, lab_id).is_deleted is True
        logout(client)

    def test_restore_missing_lab_returns_404(self, client, ids):
        login(client, "lbadmin", "admin123")
        resp = client.post("/api/labs/does-not-exist/restore")
        assert resp.status_code == 404

    def test_restore_non_deleted_lab_returns_404(self, client, ids):
        login(client, "lbadmin", "admin123")
        lab_id = _make_lab(ids, "Restore Not Deleted")
        resp = client.post(f"/api/labs/{lab_id}/restore")
        assert resp.status_code == 404
        assert db.session.get(Labs, lab_id).is_deleted is False

    def test_admin_can_restore_and_lab_reappears_in_view(self, client, ids):
        login(client, "lbadmin", "admin123")
        lab_id = _make_deleted_lab(ids, "Restore Me Admin")

        # hidden from the default catalog view
        resp = client.get("/labs/view")
        assert b"Restore Me Admin" not in resp.data

        resp = client.post(f"/api/labs/{lab_id}/restore")
        assert resp.status_code == 200
        assert db.session.get(Labs, lab_id).is_deleted is False

        # visible again after restore
        resp = client.get("/labs/view")
        assert b"Restore Me Admin" in resp.data
        logout(client)

    def test_admin_show_deleted_reveals_deleted_labs(self, client, ids):
        login(client, "lbadmin", "admin123")
        _make_deleted_lab(ids, "Show Deleted Toggle Lab")

        # default view hides deleted labs
        resp = client.get("/labs/view")
        assert b"Show Deleted Toggle Lab" not in resp.data

        # show_deleted=1 reveals them for admins
        resp = client.get("/labs/view?show_deleted=1")
        assert b"Show Deleted Toggle Lab" in resp.data
        logout(client)

    def test_admin_can_open_deleted_lab_in_editor(self, client, ids):
        login(client, "lbadmin", "admin123")
        lab_id = _make_deleted_lab(ids, "Editable Deleted Lab")
        resp = client.get(f"/labs/edit/{lab_id}")
        assert resp.status_code == 200
        assert b"Lab not found" not in resp.data
        assert b"Restore Lab" in resp.data
        logout(client)

    def test_non_admin_show_deleted_does_not_reveal(self, client, ids):
        # a deleted lab restricted to the student's group stays hidden even
        # when show_deleted is requested by a non-admin
        _make_deleted_lab(ids, "Hidden From Student Deleted", allowed_group_id=ids["student_group_id"])
        login(client, "lbstudent", "stud123")
        resp = client.get("/labs/view?show_deleted=1")
        assert b"Hidden From Student Deleted" not in resp.data
        logout(client)

"""Pytest suite for the Lab Categories CRUD feature.

Exercises the same scenarios that were checked manually while building the
feature: sidebar/route visibility per role, list/create/edit, delete blocked
while a category is still attached to a Lab, teacher ownership restrictions,
admin-only delete, input validation, and the config-driven color list.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-test.txt
    pytest tests/test_lab_categories.py -v

Note: the classes below are ordered, stateful workflows (e.g. create, then
edit, then delete) rather than independent unit tests - pytest runs test
methods within a class in definition order by default, which this suite
relies on. Do not run with a random-order plugin (e.g. pytest-randomly)
without disabling it for this file.
"""
import importlib
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
# installed on disk at *import* time. That requirement is unrelated to Lab
# Categories, so stub the module out in sys.modules before anything imports
# apps.controllers - this avoids needing that binary just to run these tests.
_fake_clabernetes = types.ModuleType("apps.controllers.clabernetes")


class _StubC9sController:
    def __getattr__(self, name):
        raise NotImplementedError("clabernetes stub - not needed for these tests")


_fake_clabernetes.C9sController = _StubC9sController
sys.modules["apps.controllers.clabernetes"] = _fake_clabernetes

from run import app as flask_app  # noqa: E402
from apps import db  # noqa: E402
from apps.authentication.models import Users  # noqa: E402
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
        db.drop_all()


@pytest.fixture(scope="session")
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def ids(app):
    """Seed the users/lab/legacy-category fixtures shared by every test."""
    admin = Users(username="admin", password="admin123", email="admin@test.local", category="admin")
    teacher_a = Users(username="teacherA", password="teach123", email="ta@test.local", category="teacher")
    teacher_b = Users(username="teacherB", password="teach123", email="tb@test.local", category="teacher")
    student = Users(username="studentA", password="stud123", email="sa@test.local", category="student")
    db.session.add_all([admin, teacher_a, teacher_b, student])
    db.session.commit()

    # a "legacy" category with no recorded owner, like rows created before
    # the ownership column existed (e.g. via dbinit.py's db.create_all() seed)
    legacy_cat = LabCategories(category="Legacy", color_cls="dark")
    legacy_cat.updated_by = None
    db.session.add(legacy_cat)

    lab = Labs(title="Test Lab", description="lab used to test delete-blocking")
    db.session.add(lab)
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_a_id": teacher_a.id,
        "teacher_b_id": teacher_b.id,
        "student_id": student.id,
        "legacy_cat_id": legacy_cat.id,
        "lab_id": lab.id,
    }


# --- helpers -----------------------------------------------------------
def login(client, username, password):
    """Log in and return True if the app accepted the credentials.

    Note: the app's post-login redirect target (authentication_blueprint's
    route_default) unconditionally bounces back to /login/ unless a
    'next_url' was previously stashed in session by an earlier unauthorized
    request - it is not a reliable signal of anything by itself. A 302 on the
    POST /login/ response (vs. a 200 re-render of the login form on bad
    credentials) is the correct signal that authentication succeeded.
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
        assert login(client, "admin", "admin123")

    def test_admin_can_view_lab_categories_list(self, client, ids):
        login(client, "admin", "admin123")
        resp = client.get("/lab_categories/list")
        assert resp.status_code == 200
        assert b"Lab Categories" in resp.data

    def test_student_is_rejected_from_list(self, client, ids):
        logout(client)
        login(client, "studentA", "stud123")
        resp = client.get("/lab_categories/list")
        assert b"Unauthorized request" in resp.data

    def test_student_is_rejected_from_edit(self, client, ids):
        resp = client.get(f"/lab_categories/edit/{ids['legacy_cat_id']}")
        assert b"Unauthorized request" in resp.data

    def test_student_is_rejected_by_delete_api(self, client, ids):
        resp = client.delete(f"/api/lab_categories/{ids['legacy_cat_id']}")
        assert resp.status_code == 401
        logout(client)


# --- admin create/edit/delete workflow -----------------------------------
class TestAdminCrudWorkflow:
    def test_admin_can_create_category(self, client, ids):
        login(client, "admin", "admin123")
        resp = client.post(
            "/lab_categories/edit/new",
            data={"category": "Wireless Security", "color_cls": "info"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Lab Category created successfully" in resp.data

        cat = LabCategories.query.filter_by(category="Wireless Security").first()
        assert cat is not None
        assert cat.color_cls == "info"

    def test_admin_can_edit_category(self, client, ids):
        cat = LabCategories.query.filter_by(category="Wireless Security").first()
        resp = client.post(
            f"/lab_categories/edit/{cat.id}",
            data={"category": "Wireless Security Advanced", "color_cls": "danger"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Lab Category updated successfully" in resp.data

        db.session.refresh(cat)
        assert cat.category == "Wireless Security Advanced"
        assert cat.color_cls == "danger"

    def test_delete_is_blocked_while_attached_to_a_lab(self, client, ids):
        lab = db.session.get(Labs, ids["lab_id"])
        cat = LabCategories.query.filter_by(category="Wireless Security Advanced").first()
        lab.categories.append(cat)
        db.session.commit()

        resp = client.delete(f"/api/lab_categories/{cat.id}")
        assert resp.status_code == 400
        assert b"still in use" in resp.data

    def test_delete_succeeds_once_unassigned(self, client, ids):
        lab = db.session.get(Labs, ids["lab_id"])
        cat = LabCategories.query.filter_by(category="Wireless Security Advanced").first()
        lab.categories.remove(cat)
        db.session.commit()

        resp = client.delete(f"/api/lab_categories/{cat.id}")
        assert resp.status_code == 200
        assert b'"status":"ok"' in resp.data.replace(b" ", b"")

        db.session.refresh(cat)
        assert cat.is_deleted is True

        resp = client.get("/lab_categories/list")
        assert b"Wireless Security Advanced" not in resp.data
        logout(client)


# --- teacher ownership rules ---------------------------------------------
class TestTeacherOwnership:
    def test_teacher_can_create_own_category(self, client, ids):
        login(client, "teacherA", "teach123")
        resp = client.post(
            "/lab_categories/edit/new",
            data={"category": "TeacherA Only", "color_cls": "success"},
            follow_redirects=True,
        )
        assert b"Lab Category created successfully" in resp.data

        cat = LabCategories.query.filter_by(category="TeacherA Only").first()
        assert cat is not None
        assert cat.updated_by == ids["teacher_a_id"]

    def test_teacher_can_edit_own_category(self, client, ids):
        cat = LabCategories.query.filter_by(category="TeacherA Only").first()
        resp = client.post(
            f"/lab_categories/edit/{cat.id}",
            data={"category": "TeacherA Only Renamed", "color_cls": "success"},
            follow_redirects=True,
        )
        assert b"Lab Category updated successfully" in resp.data

    def test_teacher_cannot_edit_legacy_category(self, client, ids):
        resp = client.get(f"/lab_categories/edit/{ids['legacy_cat_id']}")
        assert b"Unauthorized access" in resp.data

    def test_teacher_cannot_delete_own_category(self, client, ids):
        """Delete is admin-only, regardless of ownership."""
        cat = LabCategories.query.filter_by(category="TeacherA Only Renamed").first()
        resp = client.delete(f"/api/lab_categories/{cat.id}")
        assert resp.status_code == 401
        logout(client)

    def test_teacher_b_does_not_see_edit_link_for_teacher_a_category(self, client, ids):
        cat = LabCategories.query.filter_by(category="TeacherA Only Renamed").first()
        login(client, "teacherB", "teach123")
        resp = client.get("/lab_categories/list")
        assert f'/lab_categories/edit/{cat.id}"'.encode() not in resp.data

    def test_teacher_b_cannot_edit_teacher_a_category_directly(self, client, ids):
        cat = LabCategories.query.filter_by(category="TeacherA Only Renamed").first()
        resp = client.get(f"/lab_categories/edit/{cat.id}")
        assert b"Unauthorized access" in resp.data
        logout(client)

    def test_admin_can_edit_and_delete_a_teachers_category(self, client, ids):
        cat = LabCategories.query.filter_by(category="TeacherA Only Renamed").first()
        login(client, "admin", "admin123")

        resp = client.post(
            f"/lab_categories/edit/{cat.id}",
            data={"category": "TeacherA Only Renamed By Admin", "color_cls": "warning"},
            follow_redirects=True,
        )
        assert b"Lab Category updated successfully" in resp.data

        resp = client.delete(f"/api/lab_categories/{cat.id}")
        assert resp.status_code == 200
        logout(client)


# --- input validation -----------------------------------------------------
class TestInputValidation:
    def test_empty_category_name_is_rejected(self, client, ids):
        login(client, "admin", "admin123")
        resp = client.post("/lab_categories/edit/new", data={"category": "", "color_cls": "primary"})
        assert b"Category name is required" in resp.data

    def test_invalid_color_is_rejected(self, client, ids):
        resp = client.post(
            "/lab_categories/edit/new",
            data={"category": "Bad Color", "color_cls": "not-a-real-color"},
        )
        assert b"Invalid color selected" in resp.data
        logout(client)


# --- config-driven color list ----------------------------------------------
def test_lab_category_colors_are_config_driven(monkeypatch):
    """LAB_CATEGORY_COLORS must come from apps/config.py's env var, not a
    hardcoded list, so operators can extend it without a code change."""
    monkeypatch.setenv("LAB_CATEGORY_COLORS", "primary,my-custom-color")
    import apps.config as config_module

    importlib.reload(config_module)
    try:
        assert "my-custom-color" in config_module.Config.LAB_CATEGORY_COLORS
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)

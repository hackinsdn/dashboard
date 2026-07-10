"""Pytest suite for version control of editable Lab fields.

Exercises the LabFieldVersions capture on lab create/edit (one snapshot per
changed tracked field - manifest, lab_guide, extended_desc - in the same
transaction as the save), the read-only history/diff API and its permission
rules (admin/teacher any lab, labcreator only own labs), the restore-through-
the-editor flow (saving an old version records it as a new one), and the
per-field retention cap (LAB_FIELD_VERSIONS_MAX).

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3).

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_lab_field_versions.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "lv" so they never collide with the
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
from apps.home.models import Labs, LabCategories, LabFieldVersions  # noqa: E402

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
    admin = Users(username="lvadmin", password="admin123", email="lvadmin@test.local", category="admin")
    teacher = Users(username="lvteacher", password="teach123", email="lvteacher@test.local", category="teacher")
    student = Users(username="lvstudent", password="stud123", email="lvstudent@test.local", category="student")
    labcreator = Users(username="lvlabcreator", password="lc123", email="lvlc@test.local", category="labcreator")
    db.session.add_all([admin, teacher, student, labcreator])
    db.session.commit()

    category = LabCategories(category="lvVersioning", color_cls="dark")
    category.updated_by = None
    db.session.add(category)
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_id": teacher.id,
        "student_id": student.id,
        "labcreator_id": labcreator.id,
        "category_id": category.id,
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
        "lab_extended_desc": "extended description v1",
        "lab_guide": "# guide v1",
        "lab_manifest": "apiVersion: v1",
        "lab_goals": "",
    }
    data.update(overrides)
    return data


def versions_of(lab_id, field):
    return (
        LabFieldVersions.query.filter_by(lab_id=lab_id, field=field)
        .order_by(LabFieldVersions.version)
        .all()
    )


def get_lab(title):
    return Labs.query.filter_by(title=title).first()


# --- capture on create/edit ----------------------------------------------
class TestVersionCapture:
    def test_create_records_version_1_of_each_field(self, client, ids):
        logout(client)
        login(client, "lvadmin", "admin123")
        resp = client.post(
            "/labs/edit/new",
            data=lab_form(
                lab_title="lv Versioned Lab",
                lab_description="x",
                lab_categories=str(ids["category_id"]),
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        lab = get_lab("lv Versioned Lab")
        assert lab is not None

        for field, content in (
            ("manifest", "apiVersion: v1"),
            ("lab_guide", "# guide v1"),
            ("extended_desc", "extended description v1"),
        ):
            versions = versions_of(lab.id, field)
            assert [v.version for v in versions] == [1]
            assert versions[0].content == content
            assert versions[0].updated_by == ids["admin_id"]

    def test_edit_records_new_version_only_for_changed_fields(self, client, ids):
        lab = get_lab("lv Versioned Lab")
        resp = client.post(
            f"/labs/edit/{lab.id}",
            data=lab_form(
                lab_title="lv Versioned Lab",
                lab_description="x",
                lab_categories=str(ids["category_id"]),
                lab_manifest="apiVersion: v2",
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200

        assert [v.version for v in versions_of(lab.id, "manifest")] == [1, 2]
        assert versions_of(lab.id, "manifest")[-1].content == "apiVersion: v2"
        assert [v.version for v in versions_of(lab.id, "lab_guide")] == [1]
        assert [v.version for v in versions_of(lab.id, "extended_desc")] == [1]

    def test_no_change_save_records_nothing(self, client, ids):
        lab = get_lab("lv Versioned Lab")
        before = LabFieldVersions.query.filter_by(lab_id=lab.id).count()
        resp = client.post(
            f"/labs/edit/{lab.id}",
            data=lab_form(
                lab_title="lv Versioned Lab",
                lab_description="x",
                lab_categories=str(ids["category_id"]),
                lab_manifest="apiVersion: v2",
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert LabFieldVersions.query.filter_by(lab_id=lab.id).count() == before

    def test_failed_validation_records_nothing(self, client, ids):
        lab = get_lab("lv Versioned Lab")
        before = LabFieldVersions.query.filter_by(lab_id=lab.id).count()
        resp = client.post(
            f"/labs/edit/{lab.id}",
            data=lab_form(
                lab_title="lv Versioned Lab",
                lab_description="x",
                lab_categories="999999",  # invalid category aborts the save
                lab_manifest="apiVersion: v3-not-saved",
            ),
        )
        assert b"Invalid Lab Category" in resp.data
        assert LabFieldVersions.query.filter_by(lab_id=lab.id).count() == before
        logout(client)


# --- history/diff API and permissions ------------------------------------
class TestVersionApi:
    def test_admin_lists_versions_with_author(self, client, ids):
        logout(client)
        login(client, "lvadmin", "admin123")
        lab = get_lab("lv Versioned Lab")
        resp = client.get(f"/api/labs/{lab.id}/field_versions/manifest")
        assert resp.status_code == 200
        result = resp.get_json()["result"]
        assert [v["version"] for v in result] == [2, 1]  # most recent first
        assert all(v["author"] for v in result)

    def test_get_version_content(self, client, ids):
        lab = get_lab("lv Versioned Lab")
        resp = client.get(f"/api/labs/{lab.id}/field_versions/manifest/1")
        assert resp.status_code == 200
        assert resp.get_json()["result"] == "apiVersion: v1"

    def test_get_version_diff(self, client, ids):
        lab = get_lab("lv Versioned Lab")
        resp = client.get(f"/api/labs/{lab.id}/field_versions/manifest/1?diff=1")
        assert resp.status_code == 200
        diff = resp.get_json()["result"]
        assert "-apiVersion: v1" in diff
        assert "+apiVersion: v2" in diff

    def test_unknown_field_rejected(self, client, ids):
        lab = get_lab("lv Versioned Lab")
        resp = client.get(f"/api/labs/{lab.id}/field_versions/title")
        assert resp.status_code == 400

    def test_unknown_version_404(self, client, ids):
        lab = get_lab("lv Versioned Lab")
        resp = client.get(f"/api/labs/{lab.id}/field_versions/manifest/999")
        assert resp.status_code == 404

    def test_missing_lab_404(self, client, ids):
        resp = client.get("/api/labs/does-not-exist/field_versions/manifest")
        assert resp.status_code == 404
        logout(client)

    def test_student_denied(self, client, ids):
        login(client, "lvstudent", "stud123")
        lab = get_lab("lv Versioned Lab")
        resp = client.get(f"/api/labs/{lab.id}/field_versions/manifest")
        assert resp.status_code == 403
        logout(client)

    def test_labcreator_denied_on_others_lab_allowed_on_own(self, client, ids):
        login(client, "lvlabcreator", "lc123")
        admin_lab = get_lab("lv Versioned Lab")
        resp = client.get(f"/api/labs/{admin_lab.id}/field_versions/manifest")
        assert resp.status_code == 403

        # own lab: full access to its history
        resp = client.post(
            "/labs/edit/new",
            data=lab_form(
                lab_title="lv Creator Lab",
                lab_description="x",
                lab_categories=str(ids["category_id"]),
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        own_lab = get_lab("lv Creator Lab")
        resp = client.get(f"/api/labs/{own_lab.id}/field_versions/manifest")
        assert resp.status_code == 200
        assert [v["version"] for v in resp.get_json()["result"]] == [1]
        logout(client)

    def test_teacher_can_list_versions(self, client, ids):
        login(client, "lvteacher", "teach123")
        lab = get_lab("lv Versioned Lab")
        resp = client.get(f"/api/labs/{lab.id}/field_versions/manifest")
        assert resp.status_code == 200
        logout(client)


# --- rollback through the editor ------------------------------------------
class TestRestoreFlow:
    def test_saving_an_old_version_records_it_as_a_new_one(self, client, ids):
        login(client, "lvadmin", "admin123")
        lab = get_lab("lv Versioned Lab")

        # fetch v1 (as the "Restore into editor" button does) and save it back
        old = client.get(f"/api/labs/{lab.id}/field_versions/manifest/1").get_json()["result"]
        resp = client.post(
            f"/labs/edit/{lab.id}",
            data=lab_form(
                lab_title="lv Versioned Lab",
                lab_description="x",
                lab_categories=str(ids["category_id"]),
                lab_manifest=old,
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200

        db.session.refresh(lab)
        assert lab.manifest == "apiVersion: v1"
        versions = versions_of(lab.id, "manifest")
        assert [v.version for v in versions] == [1, 2, 3]
        assert versions[-1].content == "apiVersion: v1"  # rollback is non-destructive
        logout(client)


# --- retention cap ----------------------------------------------------------
class TestRetention:
    def test_oldest_versions_pruned_beyond_cap(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "LAB_FIELD_VERSIONS_MAX", 3)
        login(client, "lvadmin", "admin123")
        lab = get_lab("lv Versioned Lab")

        for i in range(4, 9):  # v4..v8 of the manifest
            resp = client.post(
                f"/labs/edit/{lab.id}",
                data=lab_form(
                    lab_title="lv Versioned Lab",
                    lab_description="x",
                    lab_categories=str(ids["category_id"]),
                    lab_manifest=f"apiVersion: v{i}",
                ),
                follow_redirects=True,
            )
            assert resp.status_code == 200

        versions = versions_of(lab.id, "manifest")
        assert [v.version for v in versions] == [6, 7, 8]  # only the newest 3 remain
        # other fields are unaffected by the manifest pruning
        assert [v.version for v in versions_of(lab.id, "lab_guide")] == [1]
        logout(client)


# --- delete a single version ------------------------------------------------
class TestVersionDelete:
    """DELETE /api/labs/<lab>/field_versions/<field>/<version>.

    Uses its own lab ("lv Delete Lab") so removing versions never disturbs the
    other stateful classes above.
    """

    def _make_lab(self, client, ids):
        login(client, "lvadmin", "admin123")
        client.post(
            "/labs/edit/new",
            data=lab_form(
                lab_title="lv Delete Lab",
                lab_description="x",
                lab_categories=str(ids["category_id"]),
                lab_manifest="apiVersion: del-v1",
            ),
            follow_redirects=True,
        )
        lab = get_lab("lv Delete Lab")
        # add a second manifest version so we have v1 and v2 to work with
        client.post(
            f"/labs/edit/{lab.id}",
            data=lab_form(
                lab_title="lv Delete Lab",
                lab_description="x",
                lab_categories=str(ids["category_id"]),
                lab_manifest="apiVersion: del-v2",
            ),
            follow_redirects=True,
        )
        return lab

    def test_admin_deletes_a_version(self, client, ids):
        logout(client)
        lab = self._make_lab(client, ids)
        assert [v.version for v in versions_of(lab.id, "manifest")] == [1, 2]

        resp = client.delete(f"/api/labs/{lab.id}/field_versions/manifest/1")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

        # only v1 is gone; v2 and other fields untouched
        assert [v.version for v in versions_of(lab.id, "manifest")] == [2]
        assert [v.version for v in versions_of(lab.id, "lab_guide")] == [1]
        logout(client)

    def test_delete_unknown_version_404(self, client, ids):
        login(client, "lvadmin", "admin123")
        lab = get_lab("lv Delete Lab")
        resp = client.delete(f"/api/labs/{lab.id}/field_versions/manifest/999")
        assert resp.status_code == 404
        logout(client)

    def test_delete_unknown_field_rejected(self, client, ids):
        login(client, "lvadmin", "admin123")
        lab = get_lab("lv Delete Lab")
        resp = client.delete(f"/api/labs/{lab.id}/field_versions/title/1")
        assert resp.status_code == 400
        logout(client)

    def test_delete_missing_lab_404(self, client, ids):
        login(client, "lvadmin", "admin123")
        resp = client.delete("/api/labs/does-not-exist/field_versions/manifest/1")
        assert resp.status_code == 404
        logout(client)

    def test_student_denied_delete(self, client, ids):
        login(client, "lvstudent", "stud123")
        lab = get_lab("lv Delete Lab")
        resp = client.delete(f"/api/labs/{lab.id}/field_versions/manifest/2")
        assert resp.status_code == 403
        # nothing was removed
        assert [v.version for v in versions_of(lab.id, "manifest")] == [2]
        logout(client)

    def test_labcreator_denied_on_others_lab(self, client, ids):
        login(client, "lvlabcreator", "lc123")
        lab = get_lab("lv Delete Lab")  # owned by admin
        resp = client.delete(f"/api/labs/{lab.id}/field_versions/manifest/2")
        assert resp.status_code == 403
        assert [v.version for v in versions_of(lab.id, "manifest")] == [2]
        logout(client)

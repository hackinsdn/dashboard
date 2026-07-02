"""Pytest suite for the ContainerLab upsert route.

Mirrors tests/test_lab_categories.py: covers upsert in apps/clabs/routes.py -
role gating, load/permission branches (not-found, non-clab redirect, labcreator
ownership, no-categories guard), the manifest-only create/edit happy path, the
registry-secret path (k8s faked), the file-upload/topology-conversion path (c9s
faked) with its failure branches, the too-many-files guard, and the automatic
"ContainerLab" category attachment.

The `c9s`/`k8s` controllers are lazy proxies pointed at a stub that raises on
attribute access; they are only reached when secrets or files are submitted, so
those tests replace the whole module-level name with a small fake. Every other
branch returns before any controller call.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3). Uploaded files land in the temp DATA_DIR's containerlab/.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_clabs.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "cl" so they never collide with the
other test modules that share the same singleton app/database (apps/config.py
reads DATA_DIR once at import time, so every module ends up on one DB).
"""
import io
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
from apps.home.models import Labs, LabCategories, LabMetadata  # noqa: E402
import apps.clabs.routes  # noqa: E402,F401  (attaches the routes to the blueprint)
from apps.clabs import blueprint as clabs_blueprint  # noqa: E402

flask_app.config["TESTING"] = True
flask_app.config["WTF_CSRF_ENABLED"] = False

# The clabs blueprint is an optional module; the shared test harness disables
# OPTIONAL_MODULES, so register it here if it isn't already (idempotent, and
# safe because no request has been served yet at import time).
if "clabs_blueprint" not in flask_app.blueprints:
    flask_app.register_blueprint(clabs_blueprint)


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
    admin = Users(username="cladmin", password="admin123", email="cladmin@test.local", category="admin")
    creator = Users(username="clcreator", password="lc123", email="clcreator@test.local", category="labcreator")
    creator2 = Users(username="clcreator2", password="lc123", email="clcreator2@test.local", category="labcreator")
    student = Users(username="clstudent", password="stud123", email="clstudent@test.local", category="student")
    db.session.add_all([admin, creator, creator2, student])
    db.session.commit()

    category = LabCategories(category="ClCat", color_cls="dark")
    category.updated_by = None
    db.session.add(category)

    group = Groups(groupname="ClGroup", organization="ORG1")
    db.session.add(group)

    # a plain (non-clab) lab -> upsert must redirect it to the normal editor
    plain = Labs(title="Plain Lab", description="not a containerlab")
    db.session.add(plain)
    db.session.commit()

    # an existing clab owned by cladmin -> used for the labcreator gate
    existing = Labs(title="Existing Clab", description="owned by cladmin")
    existing.updated_by = admin.id
    db.session.add(existing)
    db.session.commit()
    existing_md = LabMetadata(lab=existing, is_clab=True)
    db.session.add(existing_md)
    db.session.commit()

    return {
        "admin_id": admin.id,
        "creator_id": creator.id,
        "creator2_id": creator2.id,
        "student_id": student.id,
        "category_id": category.id,
        "group_id": group.id,
        "plain_lab_id": plain.id,
        "existing_clab_id": existing.id,
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


def clab_form(**overrides):
    """A complete /containerlabs/upsert POST body; override individual fields as needed."""
    data = {
        "clab_title": "",
        "clab_desc": "",
        "clab_extended_desc": "extended description",
        "clab_guide": "# guide",
        "clab_yaml": "",
        "clab_goals": "",
    }
    data.update(overrides)
    return data


def fake_k8s():
    return types.SimpleNamespace(
        create_registry_secret=lambda **kwargs: (True, ""),
        delete_secret_by_name=lambda name: None,
    )


def fake_c9s(process=(True, {"ports": {}, "nodes": {}}), convert=(True, "converted-manifest")):
    return types.SimpleNamespace(
        process_clab_topology=lambda *a, **k: process,
        convert_clab=lambda *a, **k: convert,
        check_topology_secrets=lambda *a, **k: [],
    )


# --- role gating --------------------------------------------------------
class TestRoleGating:
    def test_student_is_rejected(self, client, ids):
        logout(client)
        login(client, "clstudent", "stud123")
        resp = client.get("/containerlabs/upsert/new")
        assert b"Unauthorized request" in resp.data
        logout(client)


# --- load & permissions -------------------------------------------------
class TestLoadAndPermissions:
    def test_missing_clab(self, client, ids):
        login(client, "cladmin", "admin123")
        resp = client.get("/containerlabs/upsert/does-not-exist")
        assert b"ContainerLab not found" in resp.data

    def test_non_clab_redirects_to_edit_lab(self, client, ids):
        resp = client.get(f"/containerlabs/upsert/{ids['plain_lab_id']}")
        assert resp.status_code == 302
        assert f"/labs/edit/{ids['plain_lab_id']}" in resp.headers["Location"]
        logout(client)

    def test_labcreator_cannot_edit_others_clab(self, client, ids):
        login(client, "clcreator", "lc123")
        resp = client.get(f"/containerlabs/upsert/{ids['existing_clab_id']}")
        assert b"You don&#39;t have permission to edit this Lab." in resp.data
        logout(client)

    def test_no_categories_guard(self, client, ids):
        login(client, "cladmin", "admin123")
        active = LabCategories.query.filter_by(is_deleted=False).all()
        for cat in active:
            cat.is_deleted = True
        db.session.commit()
        try:
            resp = client.get("/containerlabs/upsert/new")
            assert b"No Lab Categories found" in resp.data
        finally:
            for cat in active:
                cat.is_deleted = False
            db.session.commit()
            logout(client)


# --- GET form -----------------------------------------------------------
class TestGetForm:
    def test_get_new_renders_form(self, client, ids):
        login(client, "cladmin", "admin123")
        resp = client.get("/containerlabs/upsert/new")
        assert resp.status_code == 200
        logout(client)


# --- create (manifest-only, no controllers) -----------------------------
class TestCreate:
    def test_requires_git_or_files_or_manifest(self, client, ids):
        login(client, "cladmin", "admin123")
        resp = client.post(
            "/containerlabs/upsert/new",
            data=clab_form(clab_title="Empty Clab", clab_categories=str(ids["category_id"])),
        )
        assert resp.status_code == 400
        assert b"Provide GIT URL or files" in resp.data

    def test_requires_a_category(self, client, ids):
        resp = client.post(
            "/containerlabs/upsert/new",
            data=clab_form(clab_title="No Cat Clab", clab_yaml="name: topo"),
        )
        assert b"Please select at least one category" in resp.data

    def test_manifest_only_create_succeeds(self, client, ids):
        resp = client.post(
            "/containerlabs/upsert/new",
            data=clab_form(
                clab_title="Manifest Clab",
                clab_desc="made from a manifest",
                clab_yaml="name: topo",
                clab_categories=str(ids["category_id"]),
                clab_allowed_groups=str(ids["group_id"]),
            ),
        )
        assert resp.status_code == 201
        assert resp.get_json()["ok"] is True

        clab = Labs.query.filter_by(title="Manifest Clab").first()
        assert clab is not None
        assert clab.is_clab is True
        assert clab.manifest == "name: topo"
        logout(client)


# --- edit existing ------------------------------------------------------
class TestEditExisting:
    def test_get_existing_renders(self, client, ids):
        login(client, "cladmin", "admin123")
        clab = Labs.query.filter_by(title="Manifest Clab").first()
        resp = client.get(f"/containerlabs/upsert/{clab.id}")
        assert resp.status_code == 200

    def test_edit_updates_title(self, client, ids):
        clab = Labs.query.filter_by(title="Manifest Clab").first()
        resp = client.post(
            f"/containerlabs/upsert/{clab.id}",
            data=clab_form(
                clab_title="Manifest Clab v2",
                clab_yaml="name: topo",
                clab_categories=str(ids["category_id"]),
            ),
        )
        assert resp.status_code == 201

        db.session.refresh(clab)
        assert clab.title == "Manifest Clab v2"
        logout(client)


# --- registry secrets (k8s faked) ---------------------------------------
class TestSecrets:
    def test_create_with_secret_persists_it(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.clabs.routes.k8s", fake_k8s())
        login(client, "cladmin", "admin123")
        resp = client.post(
            "/containerlabs/upsert/new",
            data=clab_form(
                clab_title="Secret Clab",
                clab_yaml="name: topo",
                clab_categories=str(ids["category_id"]),
                **{
                    "secret-name": "myreg",
                    "secret-server": "registry.example.com",
                    "secret-user": "bob",
                    "secret-pass": "s3cr3t",
                },
            ),
        )
        assert resp.status_code == 201

        clab = Labs.query.filter_by(title="Secret Clab").first()
        assert "myreg" in clab.lab_metadata.md.get("secrets", {}).get("name", {})
        logout(client)


# --- file upload / topology conversion (c9s faked) ----------------------
class TestFileUpload:
    def _upload(self, client, ids, title, content=b"nodes: {}"):
        return client.post(
            "/containerlabs/upsert/new",
            data=clab_form(
                clab_title=title,
                clab_categories=str(ids["category_id"]),
                clab_files=(io.BytesIO(content), "topo.yaml"),
                relative_paths="topo.yaml",
            ),
            content_type="multipart/form-data",
        )

    def test_upload_converts_and_saves_manifest(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.clabs.routes.c9s", fake_c9s())
        login(client, "cladmin", "admin123")
        resp = self._upload(client, ids, "Uploaded Clab")
        assert resp.status_code == 201

        clab = Labs.query.filter_by(title="Uploaded Clab").first()
        assert clab.manifest == "converted-manifest"

    def test_topology_parse_failure_is_reported(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.clabs.routes.c9s", fake_c9s(process=(False, "bad topo")))
        resp = self._upload(client, ids, "Bad Topo Clab")
        assert resp.status_code == 400
        assert b"Failed to parse ContainerLab topology" in resp.data

    def test_conversion_failure_is_reported(self, client, ids, monkeypatch):
        monkeypatch.setattr("apps.clabs.routes.c9s", fake_c9s(convert=(False, "convert err")))
        resp = self._upload(client, ids, "Bad Convert Clab")
        assert resp.status_code == 400
        assert b"convert err" in resp.data
        logout(client)


# --- too many files (needs the files_count -> len(files) fix) -----------
class TestTooManyFiles:
    def test_over_the_limit_is_rejected(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "CLABS_UPLOAD_MAX_FILES", 1)
        login(client, "cladmin", "admin123")
        resp = client.post(
            "/containerlabs/upsert/new",
            data=clab_form(
                clab_title="Too Many Files Clab",
                clab_categories=str(ids["category_id"]),
                clab_files=[
                    (io.BytesIO(b"a"), "a.yaml"),
                    (io.BytesIO(b"b"), "b.yaml"),
                ],
                relative_paths=["a.yaml", "b.yaml"],
            ),
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert b"Too many files" in resp.data
        logout(client)


# --- automatic "ContainerLab" category (runs last: seeds that category) --
class TestAutoAppendContainerLabCategory:
    def test_container_lab_category_is_auto_attached(self, client, ids):
        clab_cat = LabCategories(category="ContainerLab", color_cls="info")
        clab_cat.updated_by = None
        db.session.add(clab_cat)
        db.session.commit()

        login(client, "cladmin", "admin123")
        resp = client.post(
            "/containerlabs/upsert/new",
            data=clab_form(clab_title="Auto Cat Clab", clab_yaml="name: topo"),
        )
        assert resp.status_code == 201

        clab = Labs.query.filter_by(title="Auto Cat Clab").first()
        assert clab_cat.id in [c.id for c in clab.categories]
        logout(client)

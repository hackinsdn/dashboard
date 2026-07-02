"""Pytest suite for the Lab file-upload feature.

Mirrors tests/test_lab_categories.py: covers upload_lab_file, serve_upload,
get_lab_uploads and delete_lab_upload in apps/home/routes.py, exercising the
LabMetadata model - extension/size validation, saving to the temp UPLOAD_DIR,
persisting the upload reference into a lab's metadata, labcreator ownership
gating, and delete edge cases.

Runs entirely against a throwaway temporary SQLite database created in a temp
directory - it never touches the real dev/production database
(apps/data/db.sqlite3). Uploaded files land in the temp DATA_DIR's uploads/.

Usage:
    pip install -r requirements-dev.txt
    pytest tests/test_lab_uploads.py -v

Note: the classes below are ordered, stateful workflows rather than
independent unit tests - pytest runs test methods within a class in
definition order by default, which this suite relies on. Do not run with a
random-order plugin (e.g. pytest-randomly) without disabling it for this file.

The seeded usernames are all prefixed "up" so they never collide with the
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
from apps.authentication.models import Users  # noqa: E402
from apps.home.models import Labs, LabMetadata  # noqa: E402

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
    """Seed the users/lab/metadata fixtures shared by every test."""
    admin = Users(username="upadmin", password="admin123", email="upadmin@test.local", category="admin")
    teacher = Users(username="upteacher", password="teach123", email="upteacher@test.local", category="teacher")
    creator = Users(username="upcreator", password="lc123", email="upcreator@test.local", category="labcreator")
    creator2 = Users(username="upcreator2", password="lc123", email="upcreator2@test.local", category="labcreator")
    student = Users(username="upstudent", password="stud123", email="upstudent@test.local", category="student")
    db.session.add_all([admin, teacher, creator, creator2, student])
    db.session.commit()

    # a lab owned by `creator`, seeded with one upload in its metadata
    owned = Labs(title="Owned Upload Lab", description="owned by upcreator")
    owned.updated_by = creator.id
    db.session.add(owned)
    db.session.commit()
    owned_md = LabMetadata(lab=owned, is_clab=False)
    owned_md.md = {"uploads": [{"filename": "seed.txt", "original_name": "seed.txt", "url": "/uploads/seed.txt"}]}
    db.session.add(owned_md)

    # a lab with no metadata at all (delete -> "No uploads found")
    nometa = Labs(title="No Meta Lab", description="no metadata")
    nometa.updated_by = admin.id
    db.session.add(nometa)
    db.session.commit()

    return {
        "admin_id": admin.id,
        "teacher_id": teacher.id,
        "creator_id": creator.id,
        "creator2_id": creator2.id,
        "student_id": student.id,
        "owned_lab_id": owned.id,
        "nometa_lab_id": nometa.id,
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


def upload(client, filename, content=b"hello", lab_id=None):
    data = {"file": (io.BytesIO(content), filename)}
    if lab_id is not None:
        data["lab_id"] = lab_id
    return client.post("/labs/upload-file", data=data, content_type="multipart/form-data")


# --- upload_lab_file ----------------------------------------------------
class TestUploadFile:
    def test_student_is_rejected(self, client, ids):
        logout(client)
        login(client, "upstudent", "stud123")
        resp = upload(client, "notes.txt")
        assert b"Unauthorized request" in resp.data
        logout(client)

    def test_missing_file_part(self, client, ids):
        login(client, "upcreator", "lc123")
        resp = client.post("/labs/upload-file", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert b"No file part" in resp.data

    def test_empty_filename(self, client, ids):
        resp = upload(client, "")
        assert resp.status_code == 400
        assert b"No file selected" in resp.data

    def test_disallowed_extension(self, client, ids):
        resp = upload(client, "malware.exe")
        assert resp.status_code == 400
        assert b"File extension not allowed" in resp.data

    def test_file_too_large(self, client, ids, monkeypatch):
        monkeypatch.setitem(flask_app.config, "LAB_UPLOAD_MAX_SIZE", 4)
        resp = upload(client, "big.txt", content=b"way too big")
        assert resp.status_code == 400
        assert b"exceeds maximum allowed size" in resp.data

    def test_valid_upload_without_lab(self, client, ids):
        resp = upload(client, "notes.txt", content=b"file-body-1")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["status"] == "ok"
        saved = payload["saved_filename"]
        assert os.path.exists(os.path.join(flask_app.config["UPLOAD_DIR"], saved))

    def test_valid_upload_persists_metadata(self, client, ids):
        resp = upload(client, "attached.txt", content=b"file-body-2", lab_id=ids["owned_lab_id"])
        assert resp.status_code == 200
        saved = resp.get_json()["saved_filename"]

        lab = db.session.get(Labs, ids["owned_lab_id"])
        filenames = [u["filename"] for u in lab.lab_metadata.md.get("uploads", [])]
        assert saved in filenames
        logout(client)


# --- serve_upload -------------------------------------------------------
class TestServeUpload:
    def test_uploaded_file_is_served(self, client, ids):
        login(client, "upcreator", "lc123")
        resp = upload(client, "served.txt", content=b"served-body")
        saved = resp.get_json()["saved_filename"]

        resp = client.get(f"/uploads/{saved}")
        assert resp.status_code == 200
        assert resp.data == b"served-body"

    def test_unknown_file_is_404(self, client, ids):
        resp = client.get("/uploads/nope-does-not-exist.txt")
        assert resp.status_code == 404
        logout(client)


# --- get_lab_uploads ----------------------------------------------------
class TestGetLabUploads:
    def test_missing_lab_is_404(self, client, ids):
        login(client, "upadmin", "admin123")
        resp = client.get("/labs/does-not-exist/uploads")
        assert resp.status_code == 404
        logout(client)

    def test_labcreator_non_owner_is_forbidden(self, client, ids):
        login(client, "upcreator2", "lc123")
        resp = client.get(f"/labs/{ids['owned_lab_id']}/uploads")
        assert resp.status_code == 403
        logout(client)

    def test_owner_gets_uploads(self, client, ids):
        login(client, "upcreator", "lc123")
        resp = client.get(f"/labs/{ids['owned_lab_id']}/uploads")
        assert resp.status_code == 200
        assert b"seed.txt" in resp.data
        logout(client)


# --- delete_lab_upload --------------------------------------------------
class TestDeleteLabUpload:
    def test_missing_lab_is_404(self, client, ids):
        login(client, "upadmin", "admin123")
        resp = client.delete("/labs/does-not-exist/uploads/seed.txt")
        assert resp.status_code == 404

    def test_lab_without_metadata_is_404(self, client, ids):
        resp = client.delete(f"/labs/{ids['nometa_lab_id']}/uploads/seed.txt")
        assert resp.status_code == 404
        assert b"No uploads found" in resp.data

    def test_unknown_filename_is_404(self, client, ids):
        resp = client.delete(f"/labs/{ids['owned_lab_id']}/uploads/not-there.txt")
        assert resp.status_code == 404
        assert b"File not found in uploads list" in resp.data
        logout(client)

    def test_labcreator_non_owner_is_forbidden(self, client, ids):
        login(client, "upcreator2", "lc123")
        resp = client.delete(f"/labs/{ids['owned_lab_id']}/uploads/seed.txt")
        assert resp.status_code == 403
        logout(client)

    def test_owner_can_delete_upload(self, client, ids):
        login(client, "upcreator", "lc123")
        resp = client.delete(f"/labs/{ids['owned_lab_id']}/uploads/seed.txt")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

        lab = db.session.get(Labs, ids["owned_lab_id"])
        filenames = [u["filename"] for u in lab.lab_metadata.md.get("uploads", [])]
        assert "seed.txt" not in filenames
        logout(client)
